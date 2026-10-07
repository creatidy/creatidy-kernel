# SPDX-License-Identifier: Apache-2.0
"""Native hostile CHILD fixtures only; no model, owner secrets or host state.

Set CREATIDY_TEST_BWRAP_BIN to the externally built pinned utility to run native
reception. Missing setup is explicitly unproved, never a passing denial receipt.
"""

import fcntl
import hashlib
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from creatidy_kernel.adapters.bubblewrap_verification import (
    MAX_OUTPUT,
    BubblewrapVerifier,
    VerificationRefused,
    resource_digest,
)
from creatidy_kernel.adapters.bubblewrap_verification import (
    _pidfd_open as open_pidfd,  # pyright: ignore[reportPrivateUsage] - exact owned exit proof, no PID polling.
)
from creatidy_kernel.core.verification import EvidenceSubject
from creatidy_kernel.ports.verification import VerificationInvocation, VerificationResource, VerificationSnapshot

SUBJECT = EvidenceSubject("candidate", "attempt", "spec", "policy", "synthetic-repo", "base", "head")


@pytest.fixture
def native(tmp_path: Path) -> tuple[BubblewrapVerifier, tuple[VerificationResource, ...]]:
    configured = os.environ.get("CREATIDY_TEST_BWRAP_BIN")
    if configured is None:
        pytest.skip("native verification boundary UNPROVED: explicit pinned bwrap setup required")
    binary = Path(configured).resolve(strict=True)
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    # Explicit runtime closure only, no host /usr, HOME, compiler or package cache.
    stdlib = toolchain / "python3.12"
    shutil.copytree(
        "/usr/lib/python3.12",
        stdlib,
        ignore=shutil.ignore_patterns("config-*", "__pycache__", "dist-packages", "site-packages"),
    )
    closure = (
        (Path("/usr/bin/python3.12"), "/toolchain/bin/python"),
        (Path("/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"), "/toolchain/ld.so"),
        (Path("/usr/lib/x86_64-linux-gnu/libc.so.6"), "/toolchain/lib/libc.so.6"),
        (Path("/usr/lib/x86_64-linux-gnu/libm.so.6"), "/toolchain/lib/libm.so.6"),
        (Path("/usr/lib/x86_64-linux-gnu/libz.so.1"), "/toolchain/lib/libz.so.1"),
        (Path("/usr/lib/x86_64-linux-gnu/libexpat.so.1"), "/toolchain/lib/libexpat.so.1"),
        (Path("/usr/lib/x86_64-linux-gnu/libffi.so.8"), "/toolchain/lib/libffi.so.8"),
        (stdlib, "/toolchain/lib/python3.12"),
    )
    resources = tuple(
        VerificationResource(source.resolve(), PurePosixPath(destination), resource_digest(source.resolve()))
        for source, destination in closure
    )
    library = Path("/usr/lib/x86_64-linux-gnu/libseccomp.so.2").resolve()
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    verifier = BubblewrapVerifier(
        binary,
        hashlib.sha256(binary.read_bytes()).hexdigest(),
        library,
        hashlib.sha256(library.read_bytes()).hexdigest(),
        lambda invocation, now: invocation.subject == SUBJECT,
        resources,
        scratch,
    )
    return verifier, resources


def invocation(resources: tuple[VerificationResource, ...], snapshot: VerificationSnapshot) -> VerificationInvocation:
    return VerificationInvocation(
        SUBJECT,
        ("/toolchain/ld.so", "--library-path", "/toolchain/lib", "/toolchain/bin/python", "-B", "/workspace/proof.py"),
        resources,
        snapshot.digest,
        10,
    )


def test_native_positive_enforcement(native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]]) -> None:
    verifier, resources = native
    namespaces = ("user", "pid", "net", "ipc", "uts", "cgroup", "mnt")
    host_namespaces = {name: os.readlink(f"/proc/self/ns/{name}") for name in namespaces}
    snapshot = VerificationSnapshot(
        (
            (
                "proof.py",
                b"""
import os, pathlib, ctypes
assert os.environ['HOME'] == '/home/test'
assert os.environ['TMPDIR'] == '/tmp'
assert set(os.environ) <= {'HOME','PATH','LANG','TMPDIR','PWD','PYTHONHOME','PYTHONDONTWRITEBYTECODE'}
assert os.getpid() == 2
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'Seccomp:\t2' in status
for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
    assert int(next(line.split()[1] for line in status.splitlines() if line.startswith(key+':')),16) == 0
# --assert-userns-disabled checks before seccomp/exec; the leaf sysctl is not
# the exhausted parent namespace's counter. Prove the command's refusal too.
lib = ctypes.CDLL(None,use_errno=True)
assert lib.unshare(0x10000000) == -1
pathlib.Path('/workspace/result').write_text('synthetic success')
print('NATIVE_POSITIVE')
""",
            ),
        )
    )
    snapshot = VerificationSnapshot(
        (
            (
                "proof.py",
                snapshot.files[0][1]
                + f"""
for name, host_id in {host_namespaces!r}.items():
    assert os.readlink('/proc/self/ns/'+name) != host_id, name
""".encode(),
            ),
        )
    )
    result = verifier.run(invocation(resources, snapshot), snapshot)
    assert result.condition == "completed", result.stderr
    assert result.stdout == b"NATIVE_POSITIVE\n"
    assert result.settlement == "pid_namespace_terminated"
    assert result.retained_scratch is None


@pytest.mark.parametrize(
    "label", ("controller.db", "artifacts", "owner", "provider", "forge", "container.sock", "host-parent")
)
def test_native_host_canary_denial(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    tmp_path: Path,
    label: str,
) -> None:
    verifier, resources = native
    canary = tmp_path / label
    canary.write_bytes(b"synthetic canary, not a credential")
    payload = f"""
import pathlib, os
path = pathlib.Path({str(canary)!r})
assert not path.exists()
for action in ('read', 'write'):
    try:
        path.read_bytes() if action == 'read' else path.write_bytes(b'changed')
    except OSError:
        pass
    else:
        raise AssertionError(action)
try:
    os.symlink({str(canary)!r}, '/workspace/escape')
    pathlib.Path('/workspace/escape').read_bytes()
except OSError:
    pass
else:
    raise AssertionError('symlink escape')
print('DENIED')
"""
    snapshot = VerificationSnapshot((("proof.py", payload.encode()),))
    result = verifier.run(invocation(resources, snapshot), snapshot)
    assert result.condition == "completed", result.stderr
    assert result.stdout == b"DENIED\n"
    assert canary.read_bytes() == b"synthetic canary, not a credential"


@pytest.mark.parametrize("family", (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX))
def test_native_socket_syscalls_denied(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    family: int,
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot(
        (
            (
                "proof.py",
                f"""
import socket, errno
for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
    try:
        socket.socket({int(family)}, kind)
    except OSError as error:
        assert error.errno == errno.EPERM
    else:
        raise AssertionError('socket allowed')
try:
    socket.socketpair()
except OSError as error:
    assert error.errno == errno.EPERM
else:
    raise AssertionError('socketpair allowed')
print('NO_NETWORK')
""".encode(),
            ),
        )
    )
    result = verifier.run(invocation(resources, snapshot), snapshot)
    assert result.condition == "completed", result.stderr
    assert result.stdout == b"NO_NETWORK\n"


def test_native_escape_syscalls_and_fds(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    tmp_path: Path,
) -> None:
    verifier, resources = native
    canary = tmp_path / "fd-canary"
    canary.write_bytes(b"synthetic inherited descriptor")
    with canary.open("rb") as host_file, socket.socket(socket.AF_UNIX) as host_socket:
        os.set_inheritable(host_file.fileno(), True)
        os.set_inheritable(host_socket.fileno(), True)
        snapshot = VerificationSnapshot(
            (
                (
                    "proof.py",
                    b"""
import ctypes, errno, os, pathlib
lib = ctypes.CDLL(None, use_errno=True)
for name, args in (
    ('unshare', (0x10000000,)), ('setns', (-1,0)),
    ('mount', (b'none',b'/tmp',b'tmpfs',0,0)), ('chroot', (b'/workspace',)),
    ('ptrace',(0,0,0,0)),
):
    assert getattr(lib,name)(*args) == -1
    assert ctypes.get_errno() == errno.EPERM, name
assert lib.syscall(435, 0, 0) == -1 and ctypes.get_errno() == errno.ENOSYS # clone3
for number in (425,426,427,428,429,430,431,432,433,438):
    assert lib.syscall(number,0,0,0,0,0,0) == -1 and ctypes.get_errno() == errno.EPERM
assert lib.syscall(56, 0x10000000,0,0,0,0) == -1 and ctypes.get_errno() == errno.EPERM
for fd in pathlib.Path('/proc/self/fd').iterdir():
    try:
        target = os.readlink(fd)
    except FileNotFoundError:
        continue
    assert int(fd.name) <= 2, (fd.name,target)
try:
    parent_fds = list(pathlib.Path('/proc/1/fd').iterdir())
except (PermissionError,FileNotFoundError):
    parent_fds = []
assert not pathlib.Path('/proc/1/fd').exists(), 'reaper proc subtree must be masked'
for fd in parent_fds:
    if int(fd.name) <= 2:
        continue
    try:
        opened = os.open(fd,os.O_RDONLY|os.O_NONBLOCK)
    except PermissionError:
        pass
    else:
        os.close(opened)
        raise AssertionError('reaper descriptor reachable')
assert not pathlib.Path('/proc/1/root').exists()
try:
    pathlib.Path('/toolchain/bin/python').write_bytes(b'changed')
except OSError as error:
    assert error.errno == errno.EROFS
else:
    raise AssertionError('toolchain writable')
try:
    opened = os.open('/proc/sys/user/max_user_namespaces',os.O_WRONLY)
except OSError:
    pass
else:
    os.close(opened) # Do not change any kernel setting, even in a failed fixture.
    raise AssertionError('namespace sysctl writable')
print('ESCAPES_DENIED')
""",
                ),
            )
        )
        result = verifier.run(invocation(resources, snapshot), snapshot)
    assert result.condition == "completed", result.stderr
    assert result.stdout == b"ESCAPES_DENIED\n"


def test_native_timeout_setsid_descendant_and_flood(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot(
        (
            (
                "proof.py",
                b"""
import os, time
if os.fork() == 0:
    os.setsid()
    while True:
        os.write(1,b'x'*8192)
else:
    time.sleep(30)
""",
            ),
        )
    )
    result = verifier.run(replace(invocation(resources, snapshot), timeout_seconds=1), snapshot)
    assert result.condition == "timeout"
    assert result.settlement == "pid_namespace_terminated"
    assert len(result.stdout) == MAX_OUTPUT
    assert result.retained_scratch is None


@pytest.mark.parametrize(
    "bad",
    (
        "subject",
        "attempt",
        "spec",
        "policy",
        "repository",
        "base",
        "recipe",
        "resources",
        "expired",
        "revoked",
        "snapshot",
    ),
)
def test_native_authorization_no_effect(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    tmp_path: Path,
    bad: str,
) -> None:
    verifier, resources = native
    marker = tmp_path / "must-not-run"
    snapshot = VerificationSnapshot((("proof.py", f"open({str(marker)!r},'w').write('effect')".encode()),))
    exact = invocation(resources, snapshot)

    def authorize(request: VerificationInvocation, now: int) -> bool:
        return request == exact and now < 100 and bad != "revoked"

    verifier.authorize = authorize
    verifier.clock = lambda: 100 if bad == "expired" else 99
    changed = exact
    if bad == "subject":
        changed = replace(exact, subject=replace(SUBJECT, head_revision="stale"))
    elif bad in {"attempt", "spec", "policy", "repository", "base"}:
        fields = {
            "attempt": "attempt_digest",
            "spec": "spec_digest",
            "policy": "policy_digest",
            "repository": "repository",
            "base": "base_revision",
        }
        changed = replace(exact, subject=replace(SUBJECT, **{fields[bad]: "other"}))
    elif bad == "recipe":
        changed = replace(exact, recipe=(*exact.recipe, "prompt-injected-network"))
    elif bad == "resources":
        changed = replace(exact, resources=())
    elif bad == "snapshot":
        changed = replace(exact, snapshot_digest="different")
    with pytest.raises(VerificationRefused, match="authorization_denied"):
        verifier.run(changed, snapshot)
    assert not marker.exists()


def test_resource_topology_rejects_symlink_socket_and_mutability(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"synthetic")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(VerificationRefused):
        resource_digest(link)
    with socket.socket(socket.AF_UNIX) as sock:
        path = tmp_path / "socket"
        sock.bind(str(path))
        with pytest.raises(VerificationRefused):
            resource_digest(path)
    target.chmod(0o666)
    with pytest.raises(VerificationRefused):
        resource_digest(target)


def test_platform_refuses_before_binary_or_authorizer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.platform.system", lambda: "Darwin")
    verifier = BubblewrapVerifier(
        Path("/missing"), "no", Path("/missing"), "no", lambda request, now: True, (), Path("/missing")
    )
    snapshot = VerificationSnapshot(())
    with pytest.raises(VerificationRefused, match="platform_unsupported"):
        verifier.run(invocation((), snapshot), snapshot)


def test_native_alternate_abis_cannot_bypass_filter(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot(
        (
            (
                "proof.py",
                rb"""
import ctypes, mmap, os, signal
for abi in ('x32','x86'):
    pid = os.fork()
    if pid == 0:
        if abi == 'x32':
            ctypes.CDLL(None).syscall(0x40000029,2,1,0)
        else:
            code = mmap.mmap(-1,4096,prot=mmap.PROT_READ|mmap.PROT_WRITE|mmap.PROT_EXEC)
            code.write(b'\xb8\x14\x00\x00\x00\xcd\x80\xc3') # x86 int80 getpid
            address = ctypes.addressof(ctypes.c_char.from_buffer(code))
            ctypes.CFUNCTYPE(ctypes.c_int)(address)()
        os._exit(90)
    _, status = os.waitpid(pid,0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGSYS, (abi,status)
print('ALTERNATE_ABIS_DENIED')
""",
            ),
        )
    )
    result = verifier.run(invocation(resources, snapshot), snapshot)
    assert result.condition == "completed", result.stderr
    assert result.stdout == b"ALTERNATE_ABIS_DENIED\n"


@pytest.mark.parametrize("loss", ("cancel", "revoke", "expire", "parent-death"))
def test_native_owned_descendant_settlement(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    loss: str,
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot(
        (
            (
                "proof.py",
                b"""
import fcntl, os, pathlib, signal
if os.fork() == 0:
    os.setsid()
    with open('/workspace/held-lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        pathlib.Path('/workspace/ready').write_text('owned descendant ready')
        signal.pause()
else:
    signal.pause()
""",
            ),
        )
    )
    exact = replace(invocation(resources, snapshot), timeout_seconds=5)
    held: list[int] = []

    def ready() -> bool:
        for work in verifier.scratch_parent.iterdir():
            if (work / "workspace/ready").exists():
                if not held:
                    held.append(os.open(work / "workspace/held-lock", os.O_RDONLY))
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(held[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
        return False

    if loss != "parent-death":
        if loss in {"revoke", "expire"}:
            if loss == "expire":
                verifier.clock = lambda: 100 if ready() else 99

            def authorize(request: VerificationInvocation, now: int) -> bool:
                return request == exact and (now < 100 if loss == "expire" else not ready())

            verifier.authorize = authorize
        result = verifier.run(exact, snapshot, cancelled=ready if loss == "cancel" else lambda: False)
        assert result.condition == ("cancelled" if loss == "cancel" else "authorization_lost")
        assert result.settlement == "pid_namespace_terminated"
    else:
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)

            def announce() -> bool:
                if ready():
                    os.write(write_fd, b"R")
                return False

            try:
                verifier.run(exact, snapshot, cancelled=announce)
            finally:
                os._exit(92)
        os.close(write_fd)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(read_fd, selectors.EVENT_READ)
                assert selector.select(5), "native descendant did not establish ownership"
                assert os.read(read_fd, 1) == b"R"
            assert ready()
            os.kill(child, signal.SIGKILL)  # Exact child test controller, not a process group.
            assert os.waitpid(child, 0)[0] == child
        finally:
            os.close(read_fd)
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass
    assert held
    try:
        # Blocking flock in a finite owned process proves the actual setsid
        # descendant released its lock, not just that bubblewrap returned.
        subprocess.run(  # noqa: S603 - own synthetic descriptor, fixed proof code.
            [sys.executable, "-c", f"import fcntl; fcntl.flock({held[0]},fcntl.LOCK_EX)"],
            pass_fds=(held[0],),
            env={"PATH": os.defpath, "HOME": str(verifier.scratch_parent)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=5,
        )
    finally:
        os.close(held[0])


@pytest.mark.parametrize("endpoint", ("ipv4", "ipv6", "pathname", "abstract", "container"))
def test_native_host_listeners_unreachable(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    tmp_path: Path,
    endpoint: str,
) -> None:
    verifier, resources = native
    family = socket.AF_INET if endpoint == "ipv4" else socket.AF_INET6 if endpoint == "ipv6" else socket.AF_UNIX
    address: str | tuple[str, int] = (
        ("127.0.0.1", 0)
        if endpoint == "ipv4"
        else ("::1", 0)
        if endpoint == "ipv6"
        else "\0kernel-proof-" + str(os.getpid())
        if endpoint == "abstract"
        else str(tmp_path / (endpoint + ".sock"))
    )
    with socket.socket(family) as listener:
        listener.bind(address)
        listener.listen(1)
        listener.setblocking(False)
        os.set_inheritable(listener.fileno(), True)
        snapshot = VerificationSnapshot(
            (
                (
                    "proof.py",
                    f"""
import socket, errno
try:
    sock = socket.socket({int(family)},socket.SOCK_STREAM)
    sock.connect({listener.getsockname()!r})
except OSError as error:
    assert error.errno == errno.EPERM
else:
    raise AssertionError('host listener reached')
print('HOST_LISTENER_DENIED')
""".encode(),
                ),
            )
        )
        result = verifier.run(invocation(resources, snapshot), snapshot)
        assert result.condition == "completed", result.stderr
        assert result.stdout == b"HOST_LISTENER_DENIED\n"
        with pytest.raises(BlockingIOError):
            listener.accept()


@pytest.mark.parametrize(
    "bad", ("digest", "missing_library", "overlap", "escape", "symlink", "socket", "dotgit", "traversal", "setuid")
)
def test_security_prerequisites_refuse_before_candidate_launch(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot((("proof.py", b"raise AssertionError('must not execute')"),))
    exact = invocation(resources, snapshot)

    def no_launch(*args: object, **kwargs: object) -> None:
        pytest.fail("candidate launched after invalid prerequisite")

    if bad not in {"dotgit", "traversal"}:
        monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.run", no_launch)
    else:
        monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.Popen", no_launch)

        def version(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess([], 0, b"bubblewrap 0.11.0\n")

        monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.run", version)
    if bad == "digest":
        verifier.binary_sha256 = "0" * 64
    elif bad == "missing_library":
        verifier.seccomp_library = tmp_path / "missing-library"
    elif bad in {"overlap", "escape"}:
        modified = replace(
            resources[0], destination=PurePosixPath("/toolchain" if bad == "overlap" else "/toolchain/../escape")
        )
        verifier.resources = (modified, *resources[1:])
        exact = replace(exact, resources=verifier.resources)
    elif bad in {"symlink", "socket", "setuid"}:
        unsafe = tmp_path / "unsafe"
        if bad == "symlink":
            unsafe.symlink_to(resources[0].source)
        elif bad == "setuid":
            unsafe.write_bytes(b"synthetic")
            unsafe.chmod(0o4755)
        else:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(unsafe))
        verifier.resources = (replace(resources[0], source=unsafe), *resources[1:])
        exact = replace(exact, resources=verifier.resources)
    else:
        snapshot = VerificationSnapshot(((".git/config" if bad == "dotgit" else "../escape", b"untrusted"),))
        exact = replace(exact, snapshot_digest=snapshot.digest)
    with pytest.raises(VerificationRefused):
        verifier.run(exact, snapshot)


def test_missing_lifetime_evidence_retains_scratch_and_refuses_pass(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot((("proof.py", b"print('synthetic completed command')"),))

    class MissingLifetime(selectors.EpollSelector):
        def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
            events = super().select(timeout)
            result: list[tuple[selectors.SelectorKey, int]] = []
            for key, mask in events:
                if key.data == 2:
                    self.unregister(key.fileobj)  # Synthetic observer loss, not a native denial claim.
                else:
                    result.append((key, mask))
            return result

    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.selectors.DefaultSelector", MissingLifetime)
    result = verifier.run(invocation(resources, snapshot), snapshot)
    assert result.exit_code == 0
    assert result.condition == "settlement_unknown"
    assert result.settlement == "unknown"
    assert result.retained_scratch is not None and result.retained_scratch.is_dir()


def test_native_cancel_does_not_signal_bystander(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot(
        (
            (
                "proof.py",
                b"import signal,pathlib; pathlib.Path('/workspace/ready').write_text('ready'); signal.pause()",
            ),
        )
    )
    with subprocess.Popen(  # noqa: S603 - own bounded synthetic bystander, closed environment.
        [sys.executable, "-c", "import signal; signal.pause()"],
        env={"HOME": str(verifier.scratch_parent), "PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as bystander:

        def cancel() -> bool:
            return any((work / "workspace/ready").exists() for work in verifier.scratch_parent.iterdir())

        try:
            result = verifier.run(invocation(resources, snapshot), snapshot, cancelled=cancel)
            assert result.condition == "cancelled"
            assert result.settlement == "pid_namespace_terminated"
            assert bystander.poll() is None
        finally:
            bystander.kill()
            bystander.wait(timeout=5)


def test_native_parent_crash_before_exec_handoff_has_no_candidate_effect(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier, resources = native
    snapshot = VerificationSnapshot((("proof.py", b"open('/workspace/unauthorized-effect','w').close()"),))
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        init = [0]
        original_write = os.write

        def pin(pid: int) -> int:
            descriptor = open_pidfd(pid)
            if pid != os.getpid():
                init[0] = pid
            return descriptor

        def crash_boundary(fd: int, data: bytes) -> int:
            if data == b"1":
                # Fault injection after both native/bootstrap readiness and
                # exact authority check, BEFORE the candidate exec token.
                original_write(write_fd, str(init[0]).encode())
                return 1
            return original_write(fd, data)

        monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification._pidfd_open", pin)
        monkeypatch.setattr(os, "write", crash_boundary)
        try:
            verifier.run(invocation(resources, snapshot), snapshot)
        finally:
            os._exit(93)
    os.close(write_fd)
    pidfd: int | None = None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            assert selector.select(5), "native bootstrap did not reach exec handoff"
            init_pid = int(os.read(read_fd, 64))
        assert init_pid > 0
        pidfd = open_pidfd(init_pid)
        os.kill(child, signal.SIGKILL)  # Only the owned synthetic controller.
        assert os.waitpid(child, 0)[0] == child
        with selectors.DefaultSelector() as selector:
            selector.register(pidfd, selectors.EVENT_READ)
            assert selector.select(5), "owned namespace init did not settle after parent death"
        assert not any((work / "workspace/unauthorized-effect").exists() for work in verifier.scratch_parent.iterdir())
    finally:
        os.close(read_fd)
        if pidfd is not None:
            os.close(pidfd)
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass
