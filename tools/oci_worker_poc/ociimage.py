# SPDX-License-Identifier: Apache-2.0
"""Deterministic controller-owned OCI image for the synthetic worker.

The image is a docker-archive built locally from the pinned busybox-static binary with
all-zero file ownership, so the rootless single-UID store can unpack it. No registry is
contacted; podman's own ``image load`` applies the user-owned policy file. The archive
SHA-256, config digest and layer diff-id are recorded as evidence. This is provenance
pinning of controller-built bytes, not an upstream authorship claim.
"""

import hashlib
import io
import json
import subprocess  # noqa: S404 - developer tool executes explicit fixed argv, no shell.
import tarfile
from dataclasses import dataclass
from pathlib import Path

IMAGE_REF = "localhost/kernel78-worker:v1"

# Default-allow with an explicit fail-closed deny set for namespace/mount/keyring/ptrace/
# async-runtime surfaces, mirroring the ADR 0008 direction. clone-with-namespace-flags
# cannot be expressed as a masked predicate in this profile format and stays a recorded
# residual alongside the other shared-kernel syscalls.
SECCOMP_DENY_NAMES = (
    "unshare",
    "setns",
    "mount",
    "umount2",
    "pivot_root",
    "chroot",
    "ptrace",
    "keyctl",
    "add_key",
    "request_key",
    "kexec_load",
    "kexec_file_load",
    "init_module",
    "finit_module",
    "delete_module",
    "open_by_handle_at",
    "name_to_handle_at",
    "bpf",
    "perf_event_open",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "clone3",
    "process_vm_readv",
    "process_vm_writev",
    "lookup_dcookie",
    "reboot",
    "swapon",
    "swapoff",
    "iopl",
    "ioperm",
)


def seccomp_profile_json() -> str:
    return json.dumps(
        {
            "defaultAction": "SCMP_ACT_ALLOW",
            "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"],
            "syscalls": [
                {
                    "names": list(SECCOMP_DENY_NAMES),
                    "action": "SCMP_ACT_ERRNO",
                    "errnoRet": 1,
                }
            ],
        },
        indent=1,
    )


@dataclass(frozen=True, slots=True)
class ImageRecord:
    reference: str
    archive_path: Path
    archive_sha256: str
    config_digest: str
    diff_id: str


def build_image_archive(busybox_bin: Path, archive_path: Path) -> ImageRecord:
    """Build the all-zero-ownership busybox docker-archive deterministically."""
    if not busybox_bin.is_file():
        raise FileNotFoundError(f"pinned busybox binary missing: {busybox_bin}")
    layer = _busybox_layer(busybox_bin)
    diff_id = "sha256:" + hashlib.sha256(layer).hexdigest()
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": ["PATH=/bin"], "Cmd": ["/bin/busybox", "sh"]},
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        "history": [{"created_by": "creatidy-kernel #78 poc deterministic busybox layer"}],
    }
    config_bytes = json.dumps(config, indent=1).encode()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest = [
        {
            "Config": "config.json",
            "RepoTags": [IMAGE_REF],
            "Layers": ["layer.tar"],
        }
    ]
    manifest_bytes = json.dumps(manifest, indent=1).encode()
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("wb") as out:
        with tarfile.open(fileobj=out, mode="w") as archive:
            for name, blob in (("layer.tar", layer), ("config.json", config_bytes), ("manifest.json", manifest_bytes)):
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                info.uid = 0
                info.gid = 0
                archive.addfile(info, io.BytesIO(blob))
    from .evidence import sha256_path

    return ImageRecord(
        reference=IMAGE_REF,
        archive_path=archive_path,
        archive_sha256=sha256_path(archive_path),
        config_digest="sha256:" + config_digest,
        diff_id=diff_id,
    )


def _busybox_layer(busybox_bin: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:

        def directory(name: str, mode: int) -> None:
            info = tarfile.TarInfo(name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = mode
            info.type = tarfile.DIRTYPE
            archive.addfile(info)

        def file_bytes(name: str, data: bytes, mode: int) -> None:
            info = tarfile.TarInfo(name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.size = len(data)
            info.mode = mode
            archive.addfile(info, io.BytesIO(data))

        def symlink(name: str, target: str) -> None:
            info = tarfile.TarInfo(name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o777
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)

        directory("bin/", 0o755)
        file_bytes("bin/busybox", busybox_bin.read_bytes(), 0o755)
        symlink("bin/sh", "busybox")
        directory("tmp/", 0o1777)
        directory("workspace/", 0o755)
        directory("home/", 0o755)
    return buf.getvalue()


def image_exists(podman_argv_prefix: list[str], env: dict[str, str], reference: str) -> bool:
    result = subprocess.run(
        [*podman_argv_prefix, "image", "exists", reference],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )  # noqa: S603 - fixed tool argv, never workload input.
    return result.returncode == 0


def load_archive(podman_argv_prefix: list[str], env: dict[str, str], archive_path: Path) -> None:
    """Insert the archive via podman's own image load (user-owned policy, no registry)."""
    result = subprocess.run(
        [*podman_argv_prefix, "image", "load", "--input", str(archive_path)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )  # noqa: S603 - fixed tool argv built from resolved paths, never workload input.
    if result.returncode != 0:
        raise RuntimeError(f"podman image load failed: {result.stderr.strip() or result.stdout.strip()}")
