# SPDX-License-Identifier: Apache-2.0
"""Native hostile CHILD fixtures only; no model, owner secrets or host state.

Set CREATIDY_TEST_BWRAP_BIN to the externally built pinned utility to run native
reception. Missing setup is explicitly unproved, never a passing denial receipt.
"""

import ctypes
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
from typing import Any, cast

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
            return subprocess.CompletedProcess([], 0, b"bubblewrap 0.13.0\n")

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


def _kill_owned_pidfd(fd: int) -> None:
    """Fixture cleanup only: exact owned PID, even when native pdeath is not armed."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    libc.pidfd_send_signal.restype = ctypes.c_int
    if libc.pidfd_send_signal(fd, signal.SIGKILL, None, 0) != 0:
        assert ctypes.get_errno() == 3  # ESRCH: the exact owned process already exited.


@pytest.mark.parametrize("loss", ("post-arm", "cancel", "controller-death"))
def test_native_forced_reaper_prearming_interval(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    monkeypatch: pytest.MonkeyPatch,
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
        pathlib.Path('/workspace/effect').write_text('actual candidate effect')
        with open('/workspace/effect-pipe','wb',buffering=0) as notify:
            notify.write(b'M')
        signal.pause()
else:
    signal.pause()
""",
            ),
        )
    )
    notify_read, notify_write = os.pipe()
    command_read, command_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(notify_read)
        os.close(command_write)
        original_popen = subprocess.Popen
        original_write = os.write
        phase = [False, False]
        cancel = [False]
        init = [0]

        def notify(message: str) -> None:
            original_write(notify_write, (message + "\n").encode())

        def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
            if "--unshare-pid" in argv:
                workspace = cast(Path, kwargs["cwd"]) / "workspace"
                os.mkfifo(workspace / "pre-arm", mode=0o600)
                os.mkfifo(workspace / "effect-pipe", mode=0o600)
                notify("W" + str(workspace))
                index = argv.index("--")
                argv = [*argv[:index], "--lock-file", "/workspace/pre-arm", *argv[index:]]
            return cast("subprocess.Popen[bytes]", original_popen(argv, **kwargs))

        def pin(pid: int) -> int:
            fd = open_pidfd(pid)
            if pid != os.getpid():
                init[0] = pid
                notify("I" + str(pid))
            return fd

        def token(fd: int, data: bytes) -> int:
            result = original_write(fd, data)
            if data == b"1":
                notify("T")
            return result

        class Checkpoint(selectors.EpollSelector):
            def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
                if phase[0] and init[0] and not phase[1]:
                    phase[1] = True
                    # This checkpoint is AFTER one full gate decision following
                    # native bootstrap R, not a delay guessed to trigger a race.
                    notify("Q")
                    command = os.read(command_read, 1)
                    cancel[0] = command == b"C"
                events = super().select(timeout)
                if any(key.data == 5 for key, _ in events):
                    phase[0] = True
                return events

        monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.Popen", spawn)
        monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification._pidfd_open", pin)
        monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.selectors.DefaultSelector", Checkpoint)
        monkeypatch.setattr(os, "write", token)
        try:
            result = verifier.run(
                replace(invocation(resources, snapshot), timeout_seconds=10), snapshot, cancelled=lambda: cancel[0]
            )
            notify("F" + result.condition + ":" + result.settlement)
        finally:
            os._exit(94)
    os.close(notify_write)
    os.close(command_read)
    init_fd: int | None = None
    monitor_fd: int | None = None
    effect_fd: int | None = None
    held_fd: int | None = None
    pending = bytearray()
    messages: list[str] = []
    workspace: Path | None = None
    bystander = subprocess.Popen(  # noqa: S603 - owned fixture, not a service or shared process group.
        [sys.executable, "-c", "import signal; signal.pause()"],
        env={"HOME": str(verifier.scratch_parent), "PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with selectors.DefaultSelector() as observer:
            observer.register(notify_read, selectors.EVENT_READ)
            while "Q" not in messages:
                assert observer.select(5), "owned native checkpoint missing"
                data = os.read(notify_read, 4096)
                assert data, "fixture controller exited before checkpoint"
                pending.extend(data)
                while b"\n" in pending:
                    line, _, rest = pending.partition(b"\n")
                    pending = bytearray(rest)
                    messages.append(line.decode())
                if workspace is None:
                    paths = [message[1:] for message in messages if message.startswith("W")]
                    if paths:
                        workspace = Path(paths[0])
                        effect_fd = os.open(workspace / "effect-pipe", os.O_RDWR | os.O_NONBLOCK)
            assert workspace is not None and effect_fd is not None
            init_pid = int(next(message[1:] for message in messages if message.startswith("I")))
            init_fd = open_pidfd(init_pid)
            status = dict(line.split(":", 1) for line in Path(f"/proc/{init_pid}/status").read_text().splitlines())
            monitor_pid = int(status["PPid"])
            monitor_fd = open_pidfd(monitor_pid)
            monitor_status = dict(
                line.split(":", 1) for line in Path(f"/proc/{monitor_pid}/status").read_text().splitlines()
            )
            assert int(status["Seccomp_filters"]) == int(monitor_status["Seccomp_filters"]), (
                "FIFO must hold exact init before 611/613"
            )
            if "T" in messages:
                # Pre-fix evidence: actually executed descendant writes M and
                # holds its own lock while PID-1 is still before pdeath arming.
                with selectors.DefaultSelector() as effects:
                    effects.register(effect_fd, selectors.EVENT_READ)
                    assert effects.select(5), "premature exec did not reach candidate fixture"
                    assert os.read(effect_fd, 1) == b"M"
                assert (workspace / "effect").exists()
                held_fd = os.open(workspace / "held-lock", os.O_RDONLY)
                with pytest.raises(BlockingIOError):
                    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert "T" not in messages, "actual candidate marker/held descendant observed before native PID-1 armed"
            assert not (workspace / "effect").exists()
            if loss == "controller-death":
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                with selectors.DefaultSelector() as exited:
                    exited.register(monitor_fd, selectors.EVENT_READ)
                    assert exited.select(5), "owned monitor did not exit"
                with selectors.DefaultSelector() as alive:
                    alive.register(init_fd, selectors.EVENT_READ)
                    assert not alive.select(0), "held pre-arm init must remain unknown, not be falsely settled"
            else:
                os.write(command_write, b"C" if loss == "cancel" else b"U")
            if loss != "cancel":
                subprocess.run(  # noqa: S603 - bounded handshake on our own synthetic FIFO.
                    [
                        sys.executable,
                        "-c",
                        "import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY); os.close(fd)",
                        str(workspace / "pre-arm"),
                    ],
                    env={"HOME": str(verifier.scratch_parent), "PATH": os.defpath},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=5,
                )
            if loss == "post-arm":
                with selectors.DefaultSelector() as effects:
                    effects.register(effect_fd, selectors.EVENT_READ)
                    assert effects.select(5), "post-arm candidate did not execute"
                    assert os.read(effect_fd, 1) == b"M"
                armed = dict(line.split(":", 1) for line in Path(f"/proc/{init_pid}/status").read_text().splitlines())
                assert int(armed["Seccomp_filters"]) > int(monitor_status["Seccomp_filters"])
                held_fd = os.open(workspace / "held-lock", os.O_RDONLY)
                with pytest.raises(BlockingIOError):
                    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
            elif loss == "cancel":
                while not any(message.startswith("F") for message in messages):
                    assert observer.select(8), "bounded cancellation receipt missing"
                    data = os.read(notify_read, 4096)
                    assert data
                    pending.extend(data)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        messages.append(line.decode())
                assert "Fsettlement_unknown:unknown" in messages
                assert workspace.is_dir() and not (workspace / "effect").exists()
                _kill_owned_pidfd(init_fd)
            with selectors.DefaultSelector() as exited:
                exited.register(init_fd, selectors.EVENT_READ)
                assert exited.select(5), "owned namespace init failed to settle"
            if held_fd is not None:
                fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if loss != "post-arm":
                assert not (workspace / "effect").exists()
            assert bystander.poll() is None
    finally:
        try:
            if init_fd is not None:
                try:
                    _kill_owned_pidfd(init_fd)
                    with selectors.DefaultSelector() as exited:
                        exited.register(init_fd, selectors.EVENT_READ)
                        assert exited.select(5), "exact owned init cleanup failed"
                    if held_fd is not None:
                        fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(init_fd)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass
            for descriptor in (monitor_fd, effect_fd, held_fd):
                if descriptor is not None:
                    os.close(descriptor)
            os.close(notify_read)
            os.close(command_write)
            bystander.kill()
            bystander.wait(timeout=5)


@pytest.mark.parametrize("fault", ("missing", "mode", "count", "namespace"))
def test_native_reaper_observation_fault_refuses_exec(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from creatidy_kernel.adapters.bubblewrap_verification import (
        _process_status as read_status,  # pyright: ignore[reportPrivateUsage] - owned metadata fault only.
    )

    verifier, resources = native
    snapshot = VerificationSnapshot((("proof.py", b"print('MUST_NOT_EXECUTE')"),))

    def unavailable(pid: int) -> dict[str, str]:
        status = read_status(pid)
        if status.get("NSpid", "").split()[-1:] == ["1"]:
            if fault == "missing":
                status.pop("Seccomp_filters", None)
            elif fault == "mode":
                status["Seccomp"] = "1"
            elif fault == "count":
                status["Seccomp_filters"] = "-1"
            else:
                status["NSpid"] = str(pid) + " 2"
        return status

    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification._process_status", unavailable)
    result = verifier.run(invocation(resources, snapshot), snapshot)
    assert result.condition in {"enforcement_unavailable", "settlement_unknown"}
    assert result.stdout == b""
    # Injected observer faults are refusal regressions, NOT passing native
    # denial receipts. Existing supported native tests independently prove it.


def _direct_profile_argv(
    binary: Path,
    resources: tuple[VerificationResource, ...],
    fixtures: list[str],
    command: list[str],
) -> list[str]:
    """Maintained fail-closed namespace/capability profile for direct utility fixtures.

    Seccomp and the PID-1 proc mask are runtime controls, deliberately absent here:
    the subject is upstream setup-time path resolution, which runs before either
    exists. #79 regressions only; the production invocation is unchanged.
    """
    argv = [
        str(binary),
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--disable-userns",
        "--assert-userns-disabled",
        "--cap-drop",
        "ALL",
        "--new-session",
        "--die-with-parent",
        "--clearenv",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108 - new private namespace mount, not host /tmp.
        *fixtures,
    ]
    for key, value in {
        "HOME": "/home/test",
        "PATH": "/toolchain/bin",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",  # noqa: S108 - private namespace tmpfs, not host /tmp.
        "PYTHONHOME": "/toolchain",
    }.items():
        argv.extend(("--setenv", key, value))
    for resource in resources:
        argv.extend(("--ro-bind", str(resource.source), str(resource.destination)))
    argv.extend(("--remount-ro", "/", "--", *command))
    return argv


def _run_direct(argv: list[str], tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    # Exact pinned binary with fixed synthetic fixtures; closed minimal environment.
    return subprocess.run(  # noqa: S603 - exact pinned binary, fixed synthetic fixture argv.
        argv,
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
    )


def test_native_setup_creation_writes_through_writable_bind(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    tmp_path: Path,
) -> None:
    """Positive control for the setup-time escape regressions below.

    The same maintained creation op genuinely writes through a writable bind of
    real attacker content, so the refusals below are the malicious topology,
    not an inert profile. The probe lands on the bound host directory itself.
    """
    _, resources = native
    binary = Path(os.environ["CREATIDY_TEST_BWRAP_BIN"]).resolve(strict=True)
    content = tmp_path / "untrusted-content"
    (content / "subdir").mkdir(parents=True)
    argv = _direct_profile_argv(
        binary,
        resources,
        ["--bind", str(content), "/content", "--dir", "/content/subdir/escape-probe"],
        [
            "/toolchain/ld.so",
            "--library-path",
            "/toolchain/lib",
            "/toolchain/bin/python",
            "-B",
            "-P",
            "-S",
            "-c",
            "print('SETUP_COMPLETE')",
        ],
    )
    completed = _run_direct(argv, tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b"SETUP_COMPLETE\n"
    assert (content / "subdir" / "escape-probe").is_dir()


@pytest.mark.parametrize("vector", ("oldroot-alias", "proc-magiclink"))
def test_native_setup_time_escape_has_no_host_canary_effect(
    native: tuple[BubblewrapVerifier, tuple[VerificationResource, ...]],
    tmp_path: Path,
    vector: str,
) -> None:
    """Synthetic setup-time GHSA-pxhw-h44j-8pfx / CVE-2026-87766 regression.

    Reconstructs the advisory precondition that the production invocation cannot
    reach: attacker-controlled symlinked content bound into the sandbox plus a
    maintained creation op targeting a path under it. The pinned patched utility
    must die during setup with no effect on a synthetic host canary. The direct
    escape aliases are exactly the two resolution vectors the fix confines:
    the post-pivot host root (`/oldroot`, a sibling of the future sandbox root)
    and a live host `/proc/<pid>/fd/<fd>` magic link.
    """
    _, resources = native
    binary = Path(os.environ["CREATIDY_TEST_BWRAP_BIN"]).resolve(strict=True)
    canary = tmp_path / "host-canary"
    canary.write_bytes(b"synthetic host canary")
    content = tmp_path / "untrusted-content"
    content.mkdir()
    escape_fd: int | None = None
    if vector == "oldroot-alias":
        (content / "subdir").symlink_to("/oldroot" + str(tmp_path))
    else:
        escape_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            (content / "subdir").symlink_to(f"/proc/{os.getpid()}/fd/{escape_fd}")
        except OSError:
            os.close(escape_fd)
            raise
    before = sorted(path.name for path in tmp_path.iterdir())
    argv = _direct_profile_argv(
        binary,
        resources,
        ["--bind", str(content), "/content", "--dir", "/content/subdir/escape-probe"],
        [
            "/toolchain/ld.so",
            "--library-path",
            "/toolchain/lib",
            "/toolchain/bin/python",
            "-B",
            "-P",
            "-S",
            "-c",
            "print('MUST_NOT_RUN')",
        ],
    )
    try:
        completed = _run_direct(argv, tmp_path)
    finally:
        if escape_fd is not None:
            os.close(escape_fd)
    assert completed.returncode != 0, completed.stdout
    assert b"Can't mkdir parents for /content/subdir/escape-probe" in completed.stderr
    assert completed.stdout == b""
    assert canary.read_bytes() == b"synthetic host canary"
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def _structural_verifier(tmp_path: Path) -> BubblewrapVerifier:
    """Offline verifier fixtures: synthetic provenance files, one valid resource."""
    binary = tmp_path / "bwrap-fixture"
    binary.write_bytes(b"synthetic pinned utility bytes")
    library = tmp_path / "libseccomp-fixture"
    library.write_bytes(b"synthetic seccomp library bytes")
    resource = tmp_path / "toolchain-file"
    resource.write_bytes(b"synthetic trusted resource")
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    return BubblewrapVerifier(
        binary,
        hashlib.sha256(binary.read_bytes()).hexdigest(),
        library,
        hashlib.sha256(library.read_bytes()).hexdigest(),
        lambda req, now: True,
        (
            VerificationResource(
                resource.resolve(), PurePosixPath("/toolchain/lib.so"), resource_digest(resource.resolve())
            ),
        ),
        scratch,
    )


@pytest.mark.parametrize("reported", (b"bubblewrap 0.11.0", b"bubblewrap 0.13.0-fake", b"Bubblewrap 0.13.0"))
def test_unsupported_or_fake_native_version_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported: bytes
) -> None:
    verifier = _structural_verifier(tmp_path)
    snapshot = VerificationSnapshot(())
    exact = invocation(verifier.resources, snapshot)

    def fake_version(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, reported + b"\n")

    def no_launch(*args: object, **kwargs: object) -> None:
        pytest.fail("candidate launched after version refusal")

    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.run", fake_version)
    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.Popen", no_launch)
    with pytest.raises(VerificationRefused, match="native_version"):
        verifier.run(exact, snapshot)


@pytest.mark.parametrize("unsafe", ("group-writable", "world-writable", "symlinked", "not-directory"))
def test_unsafe_scratch_tree_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str) -> None:
    scratch = tmp_path / "unsafe-scratch"
    if unsafe == "symlinked":
        target = tmp_path / "scratch-target"
        target.mkdir(mode=0o700)
        scratch.symlink_to(target)
    elif unsafe == "not-directory":
        scratch.write_bytes(b"not a directory")
    else:
        scratch.mkdir()
        scratch.chmod(0o770 if unsafe == "group-writable" else 0o757)
    verifier = _structural_verifier(tmp_path)
    verifier.scratch_parent = scratch
    snapshot = VerificationSnapshot(())
    exact = invocation(verifier.resources, snapshot)

    def correct_version(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, b"bubblewrap 0.13.0\n")

    def no_launch(*args: object, **kwargs: object) -> None:
        pytest.fail("candidate launched after unsafe setup-tree refusal")

    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.run", correct_version)
    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.Popen", no_launch)
    with pytest.raises(VerificationRefused, match="scratch_ownership"):
        verifier.run(exact, snapshot)


def test_fixed_invocation_keeps_fail_closed_setup_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the composed invocation to the fail-closed profile: no fail-open
    --not-a-security-boundary mode, no weaker -try namespace variants, no host
    device binds, and every required enforcement flag present at launch."""
    verifier = _structural_verifier(tmp_path)
    snapshot = VerificationSnapshot(())
    exact = invocation(verifier.resources, snapshot)
    captured: list[list[str]] = []

    def correct_version(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 0, b"bubblewrap 0.13.0\n")

    def capture_launch(argv: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        captured.append([str(argument) for argument in argv])
        raise AssertionError("launch recorded")

    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.run", correct_version)
    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification.subprocess.Popen", capture_launch)

    def synthetic_seccomp(fd: int, library: Path) -> tuple[int, int, int]:
        return (2, 5, 5)

    monkeypatch.setattr("creatidy_kernel.adapters.bubblewrap_verification._seccomp", synthetic_seccomp)
    with pytest.raises(AssertionError, match="launch recorded"):
        verifier.run(exact, snapshot)
    assert len(captured) == 1
    argv = captured[0]
    for flag in (
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--disable-userns",
        "--assert-userns-disabled",
        "--cap-drop",
        "ALL",
        "--new-session",
        "--die-with-parent",
        "--seccomp",
        "--sync-fd",
        "--info-fd",
    ):
        assert flag in argv, flag

    def option_values(option: str) -> list[str]:
        return [argv[index + 1] for index, argument in enumerate(argv) if argument == option]

    assert option_values("--proc") == ["/proc"]
    for value in ("/proc/1", "/tmp", "/home/test"):  # noqa: S108 - namespace tmpfs value, not host /tmp.
        assert value in option_values("--tmpfs"), value
    for value in ("/proc/1", "/"):
        assert value in option_values("--remount-ro"), value
    assert "--not-a-security-boundary" not in argv
    assert not any(argument.endswith("-try") for argument in argv)
    assert "--dev-bind" not in argv
