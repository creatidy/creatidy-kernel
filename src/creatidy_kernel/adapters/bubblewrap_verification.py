# SPDX-License-Identifier: Apache-2.0
"""Opt-in Linux x86-64 child verification, not whole harness isolation.

Invokes pinned external bubblewrap; libseccomp compiles a fixed policy lazily.
There is no fallback, policy parser, credential access or runtime activation.
The trusted host/controller must protect this composition and its toolchain.
"""

import ctypes
import errno
import hashlib
import json
import os
import platform
import select
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from creatidy_kernel.ports.verification import (
    VerificationAuthorizer,
    VerificationInvocation,
    VerificationProfile,
    VerificationResource,
    VerificationSnapshot,
)

BWRAP_VERSION = "bubblewrap 0.13.0"
BWRAP_SOURCE = "719a4fd474d44b26906bcf2b1b0fb6eddd8d56d0"  # pragma: allowlist secret - public upstream revision
MAX_OUTPUT = 65536
MAX_SNAPSHOT = 16 * 1024 * 1024
# Trusted bootstrap consumes anonymous setup FDs before candidate exec. EOF is
# refusal, unlike bubblewrap's --block-fd (whose EOF also releases the command).
# -P/-S keep startup independent of candidate cwd, site hooks and .pth files.
_EXEC_GATE = (
    "import os,sys; "
    "gate,ready=int(sys.argv[1]),int(sys.argv[2]); "
    "os.write(ready,b'R'); os.close(ready); "
    "token=os.read(gate,1); os.close(gate); "
    "os._exit(125) if token != b'1' else os.execv(sys.argv[3],sys.argv[3:])"
)
DENIED_SYSCALLS = (
    "socket",
    "socketpair",
    "connect",
    "bind",
    "listen",
    "accept",
    "accept4",
    "sendto",
    "sendmsg",
    "sendmmsg",
    "recvfrom",
    "recvmsg",
    "recvmmsg",
    "shutdown",
    "setsockopt",
    "getsockopt",
    "getpeername",
    "getsockname",
    "unshare",
    "setns",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "mount_setattr",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "pidfd_getfd",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "keyctl",
    "add_key",
    "request_key",
    "kexec_load",
    "kexec_file_load",
    "reboot",
    "init_module",
    "finit_module",
    "delete_module",
    "swapon",
    "swapoff",
)
# clone3 has an indirect argument structure: refuse it with ENOSYS so libc
# can use ordinary clone for threads. All namespace flags on clone are denied.
CLONE_NAMESPACE_FLAGS = (0x80, 0x20000, 0x2000000, 0x4000000, 0x8000000, 0x10000000, 0x20000000, 0x40000000)


class VerificationRefused(ValueError):
    """Closed-vocabulary failure; never embed candidate/native stderr."""


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    invocation: VerificationInvocation
    profile: VerificationProfile
    binary_sha256: str
    source_revision: str
    native_version: str
    seccomp_version: tuple[int, int, int]
    kernel: str
    architecture: str
    observed_at: int
    exit_code: int | None
    condition: str
    settlement: str
    stdout: bytes = field(repr=False)  # Bounded private data, never public diagnostics.
    stderr: bytes = field(repr=False)
    retained_scratch: Path | None


def resource_digest(source: Path) -> str:
    """Hash an explicit trusted regular-file closure, rejecting unsafe topology."""
    if not source.is_absolute() or source.resolve() != source or source == Path("/") or not source.exists():
        raise VerificationRefused("resource_path")
    # Reject nested bind mounts, including single-file mounts, not just st_dev.
    mounts = {
        line.split()[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
    }
    digest = hashlib.sha256()
    paths = (source, *sorted(source.rglob("*"))) if source.is_dir() else (source,)
    if len(paths) > 20000:
        raise VerificationRefused("resource_limit")
    for path in paths:
        info = path.lstat()
        if (
            path.is_symlink()
            or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
            or str(path) in mounts
        ):
            raise VerificationRefused("resource_topology")
        if path.is_file():
            try:
                capabilities = os.getxattr(path, "security.capability")
            except OSError as error:
                if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                    raise VerificationRefused("resource_capabilities") from error
            else:
                if capabilities:
                    raise VerificationRefused("resource_capabilities")
        digest.update(str(path.relative_to(source)).encode() + b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class _Version(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint), ("minor", ctypes.c_uint), ("micro", ctypes.c_uint)]


class _Comparison(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]


def _pidfd_open(pid: int) -> int:
    # Some supported Python builds lack the os wrapper despite a capable kernel
    # and libc. Use libc's maintained ABI, never a hand-numbered syscall or PID
    # polling substitute for the required exact process-exit observation.
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
        libc.pidfd_open.restype = ctypes.c_int
        fd = libc.pidfd_open(pid, 0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "pidfd unavailable")
        return fd
    except AttributeError as error:
        raise VerificationRefused("pidfd_unavailable") from error


def _process_status(pid: int) -> dict[str, str]:
    """Read-only metadata of an already identified, owned native process."""
    return dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())


def _filter_count(status: dict[str, str]) -> int:
    count = int(status["Seccomp_filters"])
    mode = int(status["Seccomp"])
    if count < 0 or (mode != 2 if count else mode != 0):
        raise ValueError("unsupported seccomp observation")
    return count


def _reaper_armed(pid: int, pidfd: int, monitor: subprocess.Popen[bytes], inherited_filters: int) -> bool:
    # This indicator depends on the exact unmodified BWRAP_SOURCE, not a generic
    # version string: do_init (bubblewrap.c:585) performs its pdeath setup via
    # handle_die_with_parent (611) BEFORE installing our sole filter via
    # seccomp_programs_apply (613). Bootstrap R proves only the other fork
    # branch (3533/3539).
    live = select.poll()
    live.register(pidfd, select.POLLIN)
    if monitor.poll() is not None or live.poll(0):
        raise ValueError("owned native process exited")
    parent = _process_status(monitor.pid)
    reaper = _process_status(pid)
    if (
        int(parent["Pid"]) != monitor.pid
        or int(parent["PPid"]) != os.getpid()
        or int(reaper["Pid"]) != pid
        or int(reaper["PPid"]) != monitor.pid
        or tuple(map(int, reaper["NSpid"].split()))[0] != pid
        or tuple(map(int, reaper["NSpid"].split()))[-1] != 1
        or _filter_count(parent) != inherited_filters
        or _filter_count(reaper) not in {inherited_filters, inherited_filters + 1}
    ):
        raise ValueError("inconsistent owned reaper observation")
    if monitor.poll() is not None or live.poll(0):
        raise ValueError("owned native process exited")
    return _filter_count(reaper) == inherited_filters + 1


def _seccomp(fd: int, library: Path) -> tuple[int, int, int]:
    """Use the maintained compiler, not hand-authored BPF or syscall numbers."""
    try:
        lib = ctypes.CDLL(str(library))
        lib.seccomp_init.argtypes = [ctypes.c_uint32]
        lib.seccomp_init.restype = ctypes.c_void_p
        lib.seccomp_release.argtypes = [ctypes.c_void_p]
        lib.seccomp_arch_native.restype = ctypes.c_uint32
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
        lib.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_Comparison),
        ]
        lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32]
        lib.seccomp_version.restype = ctypes.POINTER(_Version)
        version = lib.seccomp_version().contents
        versions = (version.major, version.minor, version.micro)
        if versions < (2, 5, 5) or lib.seccomp_arch_native() != 0xC000003E:
            raise VerificationRefused("seccomp_architecture_version")
        context = lib.seccomp_init(0x7FFF0000)  # ALLOW; explicit network/escape denial below.
        if not context:
            raise VerificationRefused("seccomp_compile")
        try:
            # Bad/alternate ABI terminates the process. Do not add x86/x32 ABIs.
            if lib.seccomp_attr_set(context, 2, 0x80000000) != 0:  # ACT_BADARCH, KILL_PROCESS
                raise VerificationRefused("seccomp_compile")
            for name in (*DENIED_SYSCALLS, "clone3", "clone"):
                number = lib.seccomp_syscall_resolve_name(name.encode())
                if number < 0:
                    raise VerificationRefused("seccomp_syscall_unavailable")
                action = 0x00050000 | (errno.ENOSYS if name == "clone3" else errno.EPERM)
                comparisons = (
                    tuple(_Comparison(0, 7, flag, flag) for flag in CLONE_NAMESPACE_FLAGS) if name == "clone" else ()
                )
                if name == "clone":
                    for comparison in comparisons:
                        if lib.seccomp_rule_add_array(context, action, number, 1, ctypes.byref(comparison)) != 0:
                            raise VerificationRefused("seccomp_compile")
                elif lib.seccomp_rule_add_array(context, action, number, 0, None) != 0:
                    raise VerificationRefused("seccomp_compile")
            if lib.seccomp_export_bpf(context, fd) != 0:
                raise VerificationRefused("seccomp_compile")
        finally:
            lib.seccomp_release(context)
        os.lseek(fd, 0, os.SEEK_SET)
        return versions
    except (OSError, AttributeError) as error:
        raise VerificationRefused("seccomp_unavailable") from error


class BubblewrapVerifier:
    """Trusted composition; no environment discovery or candidate-selected mounts.

    Resources are fixed read-only paths under /toolchain. The recipe must invoke
    that closure directly, including its loader if needed. The only writable host
    bind is a fresh private snapshot; HOME and tmp are synthetic namespace tmpfs.
    """

    def __init__(
        self,
        binary: Path,
        binary_sha256: str,
        seccomp_library: Path,
        seccomp_sha256: str,
        authorize: VerificationAuthorizer,
        resources: tuple[VerificationResource, ...],
        scratch_parent: Path,
        *,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.binary = binary
        self.binary_sha256 = binary_sha256
        self.seccomp_library = seccomp_library
        self.seccomp_sha256 = seccomp_sha256
        self.authorize = authorize
        self.resources = resources
        self.scratch_parent = scratch_parent
        self.clock = clock

    def run(
        self,
        invocation: VerificationInvocation,
        snapshot: VerificationSnapshot,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> VerificationReceipt:
        if platform.system() != "Linux" or platform.machine() != "x86_64" or os.getuid() == 0:
            raise VerificationRefused("platform_unsupported")
        if not self.authorize(invocation, self.clock()):
            raise VerificationRefused("authorization_denied")
        try:
            os.close(_pidfd_open(os.getpid()))
        except OSError as error:
            raise VerificationRefused("pidfd_unavailable") from error
        if (
            not invocation.recipe
            or not invocation.recipe[0].startswith("/toolchain/")
            or any("\0" in arg for arg in invocation.recipe)
            or type(invocation.timeout_seconds) is not int
            or not 1 <= invocation.timeout_seconds <= 3600
            or snapshot.digest != invocation.snapshot_digest
            or not invocation.resources
            or invocation.resources != self.resources
            or len(snapshot.files) > 10000
            or sum(len(content) for _, content in snapshot.files) > MAX_SNAPSHOT
        ):
            raise VerificationRefused("invocation_invalid")
        destinations: list[PurePosixPath] = []
        for resource in invocation.resources:
            dest = resource.destination
            if (
                not dest.is_relative_to("/toolchain")
                or dest == PurePosixPath("/toolchain")
                or ".." in dest.parts
                or any(dest.is_relative_to(old) or old.is_relative_to(dest) for old in destinations)
                or resource.source.is_relative_to(self.scratch_parent)
                or self.scratch_parent.is_relative_to(resource.source)
                or resource.source
                in {
                    Path(value)
                    for value in (
                        "/usr",
                        "/usr/lib",
                        "/usr/local",
                        "/usr/bin",
                        "/usr/share",
                        "/etc",
                        "/home",
                        "/root",
                        "/run",
                        "/var",
                        "/proc",
                        "/sys",
                        "/dev",
                    )
                }
                or (resource.source.is_dir() and resource.source.parent == Path("/home"))
                or resource_digest(resource.source) != resource.sha256
            ):
                raise VerificationRefused("resource_manifest")
            destinations.append(dest)
        for binary, expected in ((self.binary, self.binary_sha256), (self.seccomp_library, self.seccomp_sha256)):
            resource_digest(binary)
            if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
                raise VerificationRefused("native_provenance")
        closed_environment = {
            "HOME": "/home/test",
            "PATH": "/toolchain/bin",
            "LANG": "C.UTF-8",
            "TMPDIR": "/tmp",  # noqa: S108 - private namespace tmpfs, not host /tmp.
        }
        version = subprocess.run(  # noqa: S603 - provenance checked trusted binary, closed environment.
            [str(self.binary), "--version"],
            env=closed_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            close_fds=True,
        )
        if version.returncode != 0 or version.stdout.strip() != BWRAP_VERSION.encode():
            raise VerificationRefused("native_version")
        if not self.scratch_parent.is_dir():
            raise VerificationRefused("scratch_ownership")
        scratch_info = self.scratch_parent.lstat()
        if (
            self.scratch_parent.resolve() != self.scratch_parent
            or not stat.S_ISDIR(scratch_info.st_mode)
            or scratch_info.st_uid != os.getuid()
            or scratch_info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise VerificationRefused("scratch_ownership")
        work = Path(tempfile.mkdtemp(prefix="kernel-verification-", dir=self.scratch_parent))
        settled = False
        process: subprocess.Popen[bytes] | None = None
        try:
            workspace = work / "workspace"
            workspace.mkdir()
            seen: set[str] = set()
            for name, content in snapshot.files:
                path = PurePosixPath(name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or ".git" in path.parts
                    or str(path) != name
                    or not name
                    or name in seen
                    or any(part.startswith(".") for part in path.parts)
                ):
                    raise VerificationRefused("snapshot_path")
                seen.add(name)
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            with tempfile.TemporaryFile(dir=work) as filter_file:
                seccomp_version = _seccomp(filter_file.fileno(), self.seccomp_library)
                sync_read, sync_write = os.pipe()
                info_read, info_write = os.pipe()
                gate_read, gate_write = os.pipe()
                ready_read, ready_write = os.pipe()
                init_pidfd: int | None = None
                try:
                    argv = [
                        str(self.binary),
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
                        # Same-UID proc access can reopen PID-1's event/lifetime
                        # FDs. Mask the reaper, retaining only self/child proc.
                        "--tmpfs",
                        "/proc/1",
                        "--remount-ro",
                        "/proc/1",
                        "--dev",
                        "/dev",
                        "--tmpfs",
                        "/tmp",  # noqa: S108 - new private namespace mount.
                        "--tmpfs",
                        "/home/test",
                        "--bind",
                        str(workspace),
                        "/workspace",
                        "--chdir",
                        "/workspace",
                        "--seccomp",
                        str(filter_file.fileno()),
                        "--sync-fd",
                        str(sync_write),
                        "--info-fd",
                        str(info_write),
                    ]
                    for key, value in {
                        **closed_environment,
                        "PYTHONHOME": "/toolchain",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }.items():
                        argv.extend(("--setenv", key, value))
                    for resource in invocation.resources:
                        argv.extend(("--ro-bind", str(resource.source), str(resource.destination)))
                    argv.extend(("--remount-ro", "/"))
                    argv.extend(
                        (
                            "--",
                            "/toolchain/ld.so",
                            "--library-path",
                            "/toolchain/lib",
                            "/toolchain/bin/python",
                            "-B",
                            "-P",
                            "-S",
                            "-c",
                            _EXEC_GATE,
                            str(gate_read),
                            str(ready_write),
                            *invocation.recipe,
                        )
                    )
                    if cancelled() or not self.authorize(invocation, self.clock()):
                        raise VerificationRefused("authorization_denied")
                    process = subprocess.Popen(  # noqa: S603 - fixed enforcement and trusted bound recipe.
                        argv,
                        cwd=work,
                        env=closed_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        close_fds=True,
                        pass_fds=(filter_file.fileno(), sync_write, info_write, gate_read, ready_write),
                    )
                    os.close(sync_write)
                    sync_write = -1
                    os.close(info_write)
                    info_write = -1
                    os.close(gate_read)
                    gate_read = -1
                    os.close(ready_write)
                    ready_write = -1
                    output = [bytearray(), bytearray()]
                    condition = "completed"
                    deadline = time.monotonic() + invocation.timeout_seconds
                    with selectors.DefaultSelector() as selector:
                        for index, stream in enumerate((process.stdout, process.stderr)):
                            if stream is None:
                                raise VerificationRefused("output_pipe_unavailable")
                            os.set_blocking(stream.fileno(), False)
                            selector.register(stream, selectors.EVENT_READ, index)
                        os.set_blocking(sync_read, False)
                        selector.register(sync_read, selectors.EVENT_READ, 2)
                        os.set_blocking(info_read, False)
                        selector.register(info_read, selectors.EVENT_READ, 3)
                        os.set_blocking(ready_read, False)
                        selector.register(ready_read, selectors.EVENT_READ, 5)
                        info = bytearray()
                        lifetime_closed = False
                        init_exited = False
                        bootstrap_ready = False
                        owned_init = False
                        init_pid: int | None = None
                        inherited_filters: int | None = None
                        released = False
                        stopping: float | None = None
                        try:
                            while selector.get_map():
                                if stopping is None:
                                    if cancelled():
                                        condition = "cancelled"
                                    elif not self.authorize(invocation, self.clock()):
                                        condition = "authorization_lost"
                                    elif time.monotonic() >= deadline:
                                        condition = "timeout"
                                    if condition != "completed":
                                        # Only this owned child PID; never a shared process group.
                                        process.kill()
                                        stopping = time.monotonic() + 5
                                if stopping is None and bootstrap_ready and owned_init and not released:
                                    try:
                                        if init_pid is None or init_pidfd is None or inherited_filters is None:
                                            raise ValueError("missing owned reaper observation")
                                        armed = _reaper_armed(init_pid, init_pidfd, process, inherited_filters)
                                        if (
                                            process.poll() is not None
                                            or cancelled()
                                            or not self.authorize(invocation, self.clock())
                                        ):
                                            condition = "authorization_lost"
                                        elif armed:
                                            os.write(gate_write, b"1")
                                            released = True
                                    except (OSError, ValueError, KeyError, IndexError):
                                        condition = "enforcement_unavailable"
                                    if condition != "completed":
                                        process.kill()
                                        stopping = time.monotonic() + 5
                                if stopping is not None and time.monotonic() >= stopping:
                                    break
                                for key, _ in selector.select(0.05):
                                    if key.data == 4:
                                        # pidfd becomes readable after PID-1's
                                        # namespace descendant teardown, unlike
                                        # its earlier exit_files/lifetime EOF.
                                        init_exited = True
                                        settled = lifetime_closed
                                        selector.unregister(key.fileobj)
                                        continue
                                    data = os.read(key.fd, 8192)
                                    if not data:
                                        selector.unregister(key.fileobj)
                                        if key.data == 2:
                                            lifetime_closed = True
                                            settled = init_exited
                                        elif key.data == 3:
                                            try:
                                                native = json.loads(info)
                                                pid = native["child-pid"]
                                                if type(pid) is not int or pid <= 0:
                                                    raise ValueError("invalid owned init PID")
                                                init_pidfd = _pidfd_open(pid)
                                                # The pinned producer has exactly
                                                # one direct namespace child. It
                                                # cannot fork the candidate while
                                                # our setup gate remains blocked.
                                                status = _process_status(pid)
                                                if int(status["PPid"]) != process.pid:
                                                    raise ValueError("init ownership mismatch")
                                                inherited_filters = _filter_count(_process_status(process.pid))
                                                init_pid = pid
                                                selector.register(init_pidfd, selectors.EVENT_READ, 4)
                                                owned_init = True
                                            except (OSError, ValueError, KeyError, TypeError, StopIteration):
                                                condition = "enforcement_unavailable"
                                            if condition != "completed":
                                                process.kill()
                                                stopping = time.monotonic() + 5
                                        elif key.data == 5 and not bootstrap_ready:
                                            condition = "enforcement_unavailable"
                                            process.kill()
                                            stopping = time.monotonic() + 5
                                    elif key.data == 3:
                                        info.extend(data)
                                        if len(info) > 4096:
                                            condition = "enforcement_unavailable"
                                            process.kill()
                                            stopping = time.monotonic() + 5
                                            selector.unregister(key.fileobj)
                                    elif key.data == 5:
                                        if data == b"R" and not bootstrap_ready:
                                            bootstrap_ready = True
                                        else:
                                            condition = "enforcement_unavailable"
                                            process.kill()
                                            stopping = time.monotonic() + 5
                                    elif key.data != 2:
                                        index = int(key.data)
                                        output[index].extend(data[: max(0, MAX_OUTPUT - len(output[index]))])
                            try:
                                exit_code = process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                settled = False
                                exit_code = None
                        finally:
                            if process.poll() is None:
                                process.kill()
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    settled = False
                            for stream in (process.stdout, process.stderr):
                                if stream is not None:
                                    stream.close()
                    if condition == "completed" and exit_code != 0:
                        condition = "command_or_enforcement_failed"
                    if condition == "completed" and not released:
                        condition = "enforcement_unavailable"
                    observed_at = self.clock()
                    if condition == "completed" and not self.authorize(invocation, observed_at):
                        condition = "authorization_lost"
                    if not settled:
                        condition = "settlement_unknown"
                    return VerificationReceipt(
                        invocation,
                        VerificationProfile.LINUX_BUBBLEWRAP,
                        self.binary_sha256,
                        BWRAP_SOURCE,
                        BWRAP_VERSION,
                        seccomp_version,
                        platform.release(),
                        platform.machine(),
                        observed_at,
                        exit_code,
                        condition,
                        "pid_namespace_terminated" if settled else "unknown",
                        bytes(output[0]),
                        bytes(output[1]),
                        None if settled else work,
                    )
                finally:
                    os.close(sync_read)
                    if sync_write != -1:
                        os.close(sync_write)
                    os.close(info_read)
                    if info_write != -1:
                        os.close(info_write)
                    if gate_read != -1:
                        os.close(gate_read)
                    os.close(gate_write)
                    os.close(ready_read)
                    if ready_write != -1:
                        os.close(ready_write)
                    if init_pidfd is not None:
                        os.close(init_pidfd)
        finally:
            # Only controller-created scratch, never a source checkout. A failure
            # before launch has no child; unknown live settlement retains scratch.
            if process is not None and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    settled = False
            if settled or process is None:
                shutil.rmtree(work)
