# SPDX-License-Identifier: Apache-2.0
"""Deterministic controller-built OCI image variant for the real-Codex worker.

Same shape and policy as the #78 synthetic worker image (all-zero ownership, single layer,
no registry contact, ``image load`` under the user-owned policy file) with two content
additions the real harness's native terminal needs: busybox applet symlinks on PATH (the
#78 synthetic worker invoked ``/bin/busybox <applet>`` explicitly, while Codex's native
exec runs bare commands through bash) and a minimal static ``/etc`` for identity lookups.
This is provenance pinning of controller-built bytes, not an upstream authorship claim.
"""

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from tools.oci_worker_poc.evidence import sha256_path

IMAGE_REF = "localhost/kernel53-codex-worker:v1"

# Busybox applets the synthetic scenarios use through normal PATH lookups.
APPLETS: tuple[str, ...] = (
    "cat",
    "touch",
    "ls",
    "mkdir",
    "rm",
    "cp",
    "mv",
    "printf",
    "echo",
    "sleep",
    "sha256sum",
    "grep",
    "sed",
    "setsid",
    "env",
    "date",
    "head",
    "tail",
    "wc",
    "chmod",
    "ln",
    "find",
    "true",
    "false",
    "uname",
    "id",
    "whoami",
)

ETC_FILES: tuple[tuple[str, str], ...] = (
    ("etc/passwd", "root:x:0:0:root:/:/bin/sh\n"),
    ("etc/group", "root:x:0:\n"),
    ("etc/nsswitch.conf", "passwd: files\ngroup: files\nshadow: files\nhosts: files dns\n"),
)


@dataclass(frozen=True, slots=True)
class CodexImageRecord:
    reference: str
    archive_path: Path
    archive_sha256: str
    config_digest: str
    diff_id: str


def _layer(busybox_bin: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:

        def entry(name: str, mode: int, type: bytes, data: bytes | None = None, linkname: str = "") -> None:
            info = tarfile.TarInfo(name)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = mode
            info.type = type
            if data is not None:
                info.size = len(data)
            info.linkname = linkname
            archive.addfile(info, io.BytesIO(data) if data is not None else None)

        entry("bin/", 0o755, tarfile.DIRTYPE)
        entry("bin/busybox", 0o755, tarfile.REGTYPE, busybox_bin.read_bytes())
        for applet in APPLETS:
            entry(f"bin/{applet}", 0o777, tarfile.SYMTYPE, linkname="busybox")
        entry("bin/sh", 0o777, tarfile.SYMTYPE, linkname="busybox")
        entry("etc/", 0o755, tarfile.DIRTYPE)
        for name, content in ETC_FILES:
            entry(name, 0o644, tarfile.REGTYPE, content.encode())
        for directory in ("tmp/", "workspace/", "home/"):
            entry(directory, 0o755 if directory != "tmp/" else 0o1777, tarfile.DIRTYPE)
    return buf.getvalue()


def build_codex_image_archive(busybox_bin: Path | str, archive_path: Path) -> CodexImageRecord:
    """Build the all-zero-ownership codex-worker docker-archive deterministically."""
    binary = Path(busybox_bin)
    if not binary.is_file():
        raise FileNotFoundError(f"pinned busybox binary missing: {binary}")
    layer = _layer(binary)
    diff_id = "sha256:" + hashlib.sha256(layer).hexdigest()
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": ["PATH=/bin"], "Cmd": ["/bin/busybox", "sh"]},
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
        "history": [{"created_by": "creatidy-kernel #53 deterministic codex-worker layer"}],
    }
    config_bytes = json.dumps(config, indent=1).encode()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest = [{"Config": "config.json", "RepoTags": [IMAGE_REF], "Layers": ["layer.tar"]}]
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
    return CodexImageRecord(
        reference=IMAGE_REF,
        archive_path=archive_path,
        archive_sha256=sha256_path(archive_path),
        config_digest="sha256:" + config_digest,
        diff_id=diff_id,
    )


__all__ = ["APPLETS", "CodexImageRecord", "IMAGE_REF", "build_codex_image_archive"]
