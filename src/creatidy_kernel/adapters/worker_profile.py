# SPDX-License-Identifier: Apache-2.0
"""Inert fixed OCI command composition, not admission or isolation attestation."""

import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath


@dataclass(frozen=True, slots=True)
class WorkerMount:
    source: Path
    destination: PurePosixPath
    writable: bool


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    attempt: str
    mounts: tuple[WorkerMount, ...]
    env_allowlist: tuple[tuple[str, str], ...]
    seccomp_path: Path
    pids_limit: int
    memory_bytes: int
    cpus: float
    deadline_seconds: int


def _validate_path(path: object) -> None:
    if (
        not isinstance(path, (Path, PurePosixPath))
        or not path.is_absolute()
        or ".." in path.parts
        or ":" in str(path)
        or any(ord(char) < 32 or ord(char) == 127 for char in str(path))
    ):
        raise ValueError("invalid OCI resource path")


def rootless_run_prefix(podman: Path, profile: WorkerProfile, container_name: str) -> list[str]:
    """Serialize explicit controller inputs without discovery, launch or ambient environment.

    This validates representation, not resource provenance/ownership, overlap, actual
    cgroup enforcement or rootlessness. The deadline is metadata, not a timer here.
    """
    if type(profile) is not WorkerProfile:
        raise ValueError("typed OCI profile required")
    for name in (profile.attempt, container_name):
        if type(name) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None:
            raise ValueError("invalid OCI identity")
    for value in (profile.pids_limit, profile.memory_bytes, profile.deadline_seconds):
        if type(value) is not int or not 0 < value <= 2**63 - 1:
            raise ValueError("invalid OCI integer limit")
    if type(profile.cpus) not in (int, float) or not 0 < profile.cpus <= 2**63 - 1 or not math.isfinite(profile.cpus):
        raise ValueError("invalid OCI CPU limit")
    if type(profile.mounts) is not tuple or type(profile.env_allowlist) is not tuple:
        raise ValueError("OCI resources must be immutable")
    paths: list[PurePath] = [podman, profile.seccomp_path]
    destinations: set[PurePosixPath] = set()
    for mount in profile.mounts:
        if type(mount) is not WorkerMount or type(mount.writable) is not bool:
            raise ValueError("typed OCI mount required")
        _validate_path(mount.destination)
        if mount.destination in destinations:
            raise ValueError("duplicate OCI mount")
        destinations.add(mount.destination)
        paths.extend((mount.source, mount.destination))
    for path in paths:
        _validate_path(path)
    names: set[str] = set()
    for entry in profile.env_allowlist:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError("invalid OCI environment entry")
        key, value = entry
        if (
            type(key) is not str
            or type(value) is not str
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None
            or key in names
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("invalid or duplicate OCI environment")
        names.add(key)
    argv = [
        str(podman),
        "run",
        "--pull=never",
        "--rm",
        "--name",
        container_name,
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
        "/tmp:rw,size=32m,mode=1777",  # noqa: S108 - fixed container-internal tmpfs.
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
        argv += ["-v", f"{mount.source}:{mount.destination}:{'rw' if mount.writable else 'ro'}"]
    for key, value in profile.env_allowlist:
        argv += ["--env", f"{key}={value}"]
    return argv
