# SPDX-License-Identifier: Apache-2.0
"""Controller-side execution boundary for the real Codex app-server Attempt.

Composes the exact #78 whole-worker profile (rootless single-OCI Attempt: dedicated
user/PID/IPC/UTS/network namespaces, empty-environment closure, read-only root plus
enumerated read-only inputs, one writable disposable workspace, capability/seccomp/cgroup
bounds, no engine socket) around the pinned Codex binary, its in-container exec shell, the
in-boundary launcher and the synthetic model backend. The generated per-Attempt wrapper lets
the production ``CodexStdio`` transport drive the in-boundary ``app-server`` unchanged.
"""

from __future__ import annotations

import json
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tools.oci_worker_poc.evidence import ToolRecord, sha256_path
from tools.oci_worker_poc.ociimage import ImageRecord, seccomp_profile_json
from tools.oci_worker_poc.profile import AttemptResources, WorkerMount, WorkerProfile, validate_resource_paths
from tools.oci_worker_poc.toolchain import Toolchain, host_python_closure

from .codexbin import CodexBinary


@dataclass(frozen=True, slots=True)
class CodexResources:
    """Controller-prepared disposable resources for exactly one real-Codex Attempt."""

    attempt: str
    workspace: Path
    home: Path  # Synthetic HOME backing (mounted as private tmpfs at /home/worker).
    scratch: Path  # Synthetic tmp parent (tmpfs at /tmp); also holds the generated seccomp profile.
    relay_socket: Path
    codex_home: Path  # Backed by workspace/codex-home; fresh synthetic CODEX_HOME, never owner state.
    # Codex's native async runtime spawns a thread per core plus executor helpers, so the
    # bounded budget starts where the synthetic #78 worker ended. The tighter-limit denial
    # cells (fork flood et al.) live in the #78 record; every codex Attempt in this slice
    # runs this single default budget (overridable per attempt, none tighter here).
    pids_limit: int = 256
    memory_bytes: int = 1024 * 1024 * 1024
    cpus: float = 1.0

    def as_attempt_resources(self) -> AttemptResources:
        """Project onto the shared #78 resource shape for forbidden-prefix validation."""
        return AttemptResources(
            attempt=self.attempt,
            workspace=self.workspace,
            home=self.home,
            scratch=self.scratch,
            relay_socket=self.relay_socket,
        )


def prepare_codex_sandbox(base: Path, attempt: str) -> tuple[Path, CodexResources]:
    """Create the disposable per-Attempt directories; the workspace is the only writable bind."""
    root = base / f"attempt-{attempt}"
    workspace = root / "workspace"
    home = root / "home"
    scratch = root / "scratch"
    relay_dir = root / "relay"
    codex_home = workspace / "codex-home"
    candidate = workspace / "candidate"
    for directory in (workspace, home, scratch, relay_dir, codex_home, candidate):
        directory.mkdir(parents=True, exist_ok=True)
    resources = CodexResources(
        attempt=attempt,
        workspace=workspace,
        home=home,
        scratch=scratch,
        relay_socket=relay_dir / "effects.sock",
        codex_home=codex_home,
    )
    return root, resources


def build_codex_profile(
    toolchain: Toolchain,
    resources: CodexResources,
    worker_entry: Path,
    mock_source: Path,
    codex_binary: CodexBinary,
    deadline_seconds: int,
) -> WorkerProfile:
    """Build the fixed execution profile; every mount is enumerated here, never derived
    from Attempt/candidate data."""
    validate_resource_paths(resources.as_attempt_resources())
    python, (stdlib, loader, *libs) = host_python_closure()
    bash = Path("/usr/bin/bash")
    if not bash.is_file():
        raise FileNotFoundError("host bash required as the in-container native exec shell")
    lib_dir = Path("/usr/lib/x86_64-linux-gnu")
    libtinfo = lib_dir / "libtinfo.so.6"
    if not libtinfo.is_file():
        raise FileNotFoundError("host libtinfo.so.6 required by the in-container bash closure")
    if not worker_entry.is_file() or not mock_source.is_file():
        raise FileNotFoundError("in-boundary launcher and synthetic model sources must exist before profile build")
    lib_dest = PurePosixPath("/toolchain/lib")
    mounts: list[WorkerMount] = [
        WorkerMount(python, PurePosixPath("/toolchain/bin/python3.12"), False),
        WorkerMount(stdlib, PurePosixPath("/toolchain/lib/python3.12"), False),
        WorkerMount(loader, PurePosixPath("/lib64/ld-linux-x86-64.so.2"), False),
        WorkerMount(bash, PurePosixPath("/bin/bash"), False),
        WorkerMount(libtinfo, lib_dest / libtinfo.name, False),
        WorkerMount(codex_binary.path, PurePosixPath("/toolchain/codex/codex"), False),
        WorkerMount(resources.relay_socket, PurePosixPath("/relay/effects.sock"), False),
        WorkerMount(worker_entry, PurePosixPath("/worker/main.py"), False),
        WorkerMount(mock_source, PurePosixPath("/worker/mockmodel.py"), False),
    ]
    if codex_binary.code_mode_host is not None:
        mounts.append(
            WorkerMount(codex_binary.code_mode_host, PurePosixPath("/toolchain/codex/codex-code-mode-host"), False)
        )
    for lib in libs:
        mounts.append(WorkerMount(lib, lib_dest / lib.name, False))
    mounts.append(WorkerMount(resources.workspace, PurePosixPath("/workspace"), True))
    env_allowlist: tuple[tuple[str, str], ...] = (
        ("PATH", "/bin:/toolchain/bin"),
        ("PYTHONHOME", "/toolchain"),
        ("LD_LIBRARY_PATH", str(lib_dest)),
        ("LANG", "C.UTF-8"),
        ("LC_ALL", "C"),
        ("ATTEMPT_ID", resources.attempt),
        ("HOME", "/home/worker"),
        ("TMPDIR", "/tmp"),  # noqa: S108 - container-internal tmpfs path, not a host temp file.
        ("SHELL", "/bin/sh"),
        ("CODEX_HOME", "/workspace/codex-home"),
    )
    return WorkerProfile(
        attempt=resources.attempt,
        mounts=tuple(mounts),
        env_allowlist=env_allowlist,
        seccomp_path=write_seccomp_profile(Path(resources.scratch) / "seccomp.json"),
        pids_limit=resources.pids_limit,
        memory_bytes=resources.memory_bytes,
        cpus=resources.cpus,
        deadline_seconds=deadline_seconds,
    )


def write_seccomp_profile(path: Path) -> Path:
    """#78 deny-list with one compatibility correction: ``clone3`` returns ENOSYS, not EPERM.

    glibc's ``pthread_create`` only falls back from ``clone3`` to ``clone`` on ENOSYS and
    honors EPERM as a final refusal, so the EPERM variant of the same deny set breaks every
    native thread spawn (observed: codex's async runtime and the in-boundary model backend
    could not start threads). Returning ENOSYS is the upstream OCI default-profile practice
    for clone3; the syscall remains denied and every other entry is unchanged.
    """
    profile = json.loads(seccomp_profile_json())
    for group in profile["syscalls"]:
        names: list[str] = group.get("names", [])
        if "clone3" in names:
            names.remove("clone3")
            group["names"] = names
    profile["syscalls"].append({"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=1))
    return path


def container_name_for(attempt: str) -> str:
    return f"kernel53-codex-{attempt}"


def new_attempt_id() -> str:
    return uuid.uuid4().hex[:12]


def build_run_argv(toolchain: Toolchain, profile: WorkerProfile, image_ref: str, container: str) -> list[str]:
    """Full rootless podman attach argv for the Codex worker boundary (stop/kill reuse the prefix).

    The policy flags are identical to the #78 synthetic worker profile; ``-i`` additionally
    attaches the controller's stdio to the in-boundary app-server.
    """
    argv = [
        str(toolchain.root / "bin/podman"),
        "run",
        "-i",
        "--pull=never",
        "--rm",
        "--name",
        container,
        "--hostname",
        profile.attempt,
        "--network=none",
        "--cap-drop=all",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        f"seccomp={profile.seccomp_path}",
        "--read-only",
        "--read-only-tmpfs",
        "--tmpfs",
        "/tmp:rw,size=32m,mode=1777",  # noqa: S108 - private in-container tmpfs, not host /tmp.
        "--tmpfs",
        "/home/worker:rw,size=8m,mode=700",
        "--pids-limit",
        str(profile.pids_limit),
        "--memory",
        str(profile.memory_bytes),
        "--cpus",
        f"{profile.cpus:g}",
        "--stop-timeout",
        "10",
    ]
    for mount in profile.mounts:
        mode = "rw" if mount.writable else "ro"
        argv += ["-v", f"{mount.source}:{mount.destination}:{mode}"]
    for key, value in profile.env_allowlist:
        argv += ["--env", f"{key}={value}"]
    argv += [
        image_ref,
        "/bin/busybox",
        "sh",
        "-c",
        "exec /toolchain/bin/python3.12 -S /worker/main.py",
    ]
    return argv


def write_wrapper_script(
    path: Path,
    toolchain: Toolchain,
    profile: WorkerProfile,
    image_record: ImageRecord,
    container: str,
    codex_path: Path,
    probe_home: Path,
) -> Path:
    """Generate the per-Attempt wrapper the production CodexStdio transport invokes.

    ``--version`` probes the exact hash-pinned binary host-side; ``app-server`` execs the
    closed-environment podman attach that runs the boundary. All values are embedded and
    shell-quoted at generation time; the wrapper reads nothing else from its environment.
    """
    environment = toolchain.env()
    podman_argv = build_run_argv(toolchain, profile, image_record.reference, container)
    quoted_flags = " ".join(shlex.quote(part) for part in podman_argv[1:])
    env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in environment.items())
    stderr_log = Path(profile.seccomp_path).parent.parent / "podman-stderr.log"
    script = f"""#!/bin/sh
# Generated per-Attempt controller wrapper (creatidy-kernel #53 bounded native proof).
# Invocation: <wrapper> --version   |   <wrapper> app-server
set -eu
case "${{1:-}}" in
  --version)
    exec env -i PATH=/usr/bin:/bin HOME={shlex.quote(str(probe_home))} \\
      CODEX_HOME={shlex.quote(str(probe_home / "codex-home"))} LC_ALL=C \\
      {shlex.quote(str(codex_path))} --version
    ;;
  app-server)
    exec env -i {env_prefix} {shlex.quote(str(toolchain.root / "bin/podman"))} {quoted_flags} \\
      2>>{shlex.quote(str(stderr_log))}
    ;;
  *)
    echo "wrapper usage: --version | app-server" >&2
    exit 2
    ;;
esac
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    path.chmod(0o700)
    return path


def toolchain_records(toolchain: Toolchain, codex_records: list[ToolRecord]) -> list[ToolRecord]:
    records = [toolchain.podman, toolchain.crun, toolchain.conmon, toolchain.netavark, toolchain.busybox]
    records.extend(codex_records)
    records.append(
        ToolRecord(
            name="synthetic-model-backend",
            path="controller-authored tools/codex_oci_proof/mockmodel.py",
            version="synthetic-responses/1",
            sha256=sha256_path(Path(__file__).parent / "mockmodel.py"),
            origin="repository-authored synthetic fixture; runs inside the disposable boundary on container loopback",
        )
    )
    return records


__all__ = [
    "CodexResources",
    "build_codex_profile",
    "build_run_argv",
    "container_name_for",
    "new_attempt_id",
    "prepare_codex_sandbox",
    "toolchain_records",
    "write_wrapper_script",
]
