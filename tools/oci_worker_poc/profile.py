# SPDX-License-Identifier: Apache-2.0
"""Whole-worker isolation profile: the exact rootless OCI execution boundary.

The profile defines every permitted resource and every restriction for one synthetic
Attempt. Candidate/task data can never choose mounts, environment, namespace policy or
limits: everything is fixed by the controller-owned profile builder, mirroring the ADR 0008
authorization seam. Rootless enforcement properties:

- one dedicated user namespace per Attempt (podman rootless), single-UID mapping
- dedicated PID/IPC/UTS namespaces and an isolated, unconfigured network namespace
- empty-environment closure (explicit allowlist only; host env is not inherited)
- read-only root filesystem plus read-only trusted toolchain/source inputs
- one writable disposable workspace and private tmpfs for /tmp and HOME
- all capabilities dropped, no-new-privileges, seccomp deny-list, cgroup memory/pids bounds
- no container-engine socket, no host credential paths, no ambient networking
"""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from creatidy_kernel.adapters.worker_profile import WorkerMount, WorkerProfile, rootless_run_prefix

from .ociimage import seccomp_profile_json
from .toolchain import Toolchain, host_python_closure

# Paths that must never be mounted or passed into the worker boundary.
FORBIDDEN_BIND_PREFIXES = (
    Path("/etc/ssh"),
    Path("/home"),
    Path("/root"),
    Path("/var/run/docker.sock"),
    Path("/run/docker.sock"),
    Path("/run/containerd"),
    Path("/var/run/containers"),
    Path("/run/podman"),
)


@dataclass(frozen=True, slots=True)
class AttemptResources:
    """Controller-prepared disposable resources for exactly one Attempt."""

    attempt: str
    workspace: Path
    home: Path  # Synthetic HOME backing (mounted as private tmpfs at /home/worker).
    scratch: Path  # Synthetic tmp parent (tmpfs at /tmp).
    relay_socket: Path
    source_ro: tuple[Path, ...] = ()
    pids_limit: int = 64
    memory_bytes: int = 256 * 1024 * 1024
    cpus: float = 0.5


def validate_resource_paths(resources: AttemptResources) -> None:
    """Refuse mounts of forbidden host prefixes and non-canonical paths."""
    candidates = [resources.workspace, resources.home, resources.scratch, resources.relay_socket, *resources.source_ro]
    for path in candidates:
        resolved = Path(path).resolve()
        if str(resolved) != str(path):
            raise ValueError(f"non-canonical resource path: {path}")
        for prefix in FORBIDDEN_BIND_PREFIXES:
            if resolved == prefix or prefix in resolved.parents:
                raise ValueError(f"forbidden resource path {resolved} under {prefix}")


def write_seccomp_profile(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(seccomp_profile_json())
    return path


def build_profile(
    toolchain: Toolchain, resources: AttemptResources, worker_entry: Path, deadline_seconds: int
) -> WorkerProfile:
    """Build the fixed execution profile; every mount is enumerated here, never derived
    from Attempt/candidate data."""
    validate_resource_paths(resources)
    python, (stdlib, loader, *libs) = host_python_closure()
    lib_dest = PurePosixPath("/toolchain/lib")
    mounts: list[WorkerMount] = [
        WorkerMount(python, PurePosixPath("/toolchain/bin/python3.12"), False),
        WorkerMount(stdlib, PurePosixPath("/toolchain/lib/python3.12"), False),
        WorkerMount(loader, PurePosixPath("/lib64/ld-linux-x86-64.so.2"), False),
        WorkerMount(resources.relay_socket, PurePosixPath("/relay/effects.sock"), False),
        WorkerMount(worker_entry, PurePosixPath("/worker/main.py"), False),
    ]
    for lib in libs:
        mounts.append(WorkerMount(lib, lib_dest / lib.name, False))
    for source in resources.source_ro:
        mounts.append(WorkerMount(source, PurePosixPath("/source") / source.name, False))
    mounts.append(WorkerMount(resources.workspace, PurePosixPath("/workspace"), True))
    env_allowlist: tuple[tuple[str, str], ...] = (
        ("PATH", "/bin:/toolchain/bin"),
        ("PYTHONHOME", "/toolchain"),
        ("LD_LIBRARY_PATH", str(lib_dest)),
        ("LANG", "C.UTF-8"),
        ("ATTEMPT_ID", resources.attempt),
        ("HOME", "/home/worker"),
        ("TMPDIR", "/tmp"),  # noqa: S108 - container-internal tmpfs path, not a host temp file.
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


def build_run_argv(
    toolchain: Toolchain,
    profile: WorkerProfile,
    image_ref: str,
    container_name: str,
) -> list[str]:
    """Full rootless podman run argv for the worker boundary (stop/kill reuse the prefix)."""
    argv = rootless_run_prefix(toolchain.root / "bin/podman", profile, container_name)
    argv += [
        image_ref,
        "/bin/busybox",
        "sh",
        "-c",
        "exec /toolchain/bin/python3.12 -S /worker/main.py",
    ]
    return argv
