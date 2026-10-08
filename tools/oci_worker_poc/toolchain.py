# SPDX-License-Identifier: Apache-2.0
"""User-owned pinned rootless OCI toolchain resolution and provenance capture.

No privileged installation is used or authorized. The primary toolchain is the temp-only
user-owned provisioning under ``/tmp/kilo/kernel78-tooling`` (podman 6.1.3 built from the
pinned upstream commit, crun 1.30.1 official signature-verified release asset, conmon 2.2.1
release asset, netavark/busybox pinned noble archive extractions); its ``provenance.json``
records exact sources and hashes. This module only resolves, verifies and records what
already exists; a missing component refuses so the native proof stays UNPROVED instead of
silently weakening.
"""

import json
import os
import subprocess  # noqa: S404 - developer tool executes explicit fixed argv, no shell.
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import ToolRecord, sha256_path

TOOLROOT_ENV = "CREATIDY_TEST_OCI_TOOLROOT"
DEFAULT_TOOLROOT = Path("/tmp/kilo/kernel78-tooling")  # noqa: S108 - pinned disposable toolroot, not a general temp file.

# USER=nobody selects podman's documented rootless single-UID mapping: this host has a
# /etc/subuid range but no setuid newuidmap, and installing one is a privileged change
# outside this task's authorization. Multi-UID confinement is a recorded limitation.
SINGLE_UID_USER = "nobody"


@dataclass(frozen=True, slots=True)
class Toolchain:
    root: Path
    podman: ToolRecord
    crun: ToolRecord
    conmon: ToolRecord
    netavark: ToolRecord
    busybox: ToolRecord

    @property
    def containers_conf(self) -> Path:
        return self.root / "containers.conf"

    @property
    def storage_conf(self) -> Path:
        return self.root / "storage.conf"

    @property
    def policy_file(self) -> Path:
        return self.root / "config" / "containers" / "policy.json"

    @property
    def provenance_file(self) -> Path:
        return self.root / "provenance.json"

    def env(self) -> dict[str, str]:
        """Closed invocation environment for controller-side podman calls.

        XDG_RUNTIME_DIR must be the real systemd user session directory: podman needs the
        session bus for its systemd cgroup manager, and a silent cgroupfs fallback would
        leave memory/pids limits unenforced. The harness refuses that fallback.
        """
        return {
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "LC_ALL": "C",
            "USER": SINGLE_UID_USER,
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
            "CONTAINERS_CONF": str(self.containers_conf),
            "CONTAINERS_STORAGE_CONF": str(self.storage_conf),
        }


def _version(path: Path, argv: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(path), *argv],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )  # noqa: S603 - fixed tool argv built from resolved paths, never workload input.
    return result.stdout.strip().splitlines()[0].strip()


def _record(name: str, path: Path, version_argv: list[str], env: dict[str, str]) -> ToolRecord:
    if not path.is_file():
        raise FileNotFoundError(f"pinned toolchain component missing: {path} (see toolroot provenance.json / SETUP)")
    version = _version(path, version_argv, env) if version_argv else "static"
    return ToolRecord(
        name=name,
        path=str(path),
        version=version,
        sha256=sha256_path(path),
        origin=f"user-owned toolroot {path.parent} provisioning; see {TOOLROOT_ENV}/provenance.json",
    )


def resolve_toolchain() -> Toolchain:
    """Resolve the user-owned pinned toolchain or raise with the precise missing component."""
    configured = os.environ.get(TOOLROOT_ENV)
    root = Path(configured).resolve() if configured else DEFAULT_TOOLROOT
    bin_dir = root / "bin"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "LC_ALL": "C",
    }
    for required in (
        bin_dir,
        root / "home",
        root / "tmp",
        root / "cache",
        root / "config",
        root / "data",
        root / "runtime",
        root / "containers.conf",
        root / "storage.conf",
        root / "config" / "containers" / "policy.json",
        root / "provenance.json",
    ):
        if not Path(required).exists():
            raise FileNotFoundError(f"toolchain incomplete: {required} missing under {root}")
    return Toolchain(
        root=root,
        podman=_record("podman", bin_dir / "podman", ["--version"], env),
        crun=_record("crun", bin_dir / "crun", ["--version"], env),
        conmon=_record("conmon", bin_dir / "conmon", ["--version"], env),
        netavark=_record("netavark", bin_dir / "netavark", ["--version"], env),
        busybox=_record("busybox", bin_dir / "busybox", [], env),
    )


def read_provenance(toolchain: Toolchain) -> dict[str, Any]:
    """Load the toolroot provenance record (exact upstream pins, hashes, verification)."""
    return json.loads(toolchain.provenance_file.read_text())  # type: ignore[no-any-return]


def host_python_closure() -> tuple[Path, tuple[Path, ...]]:
    """Read-only host Python 3.12 runtime closure: binary, loader, shared objects, stdlib.

    This mirrors the ADR 0008 trusted toolchain-mount pattern. The files stay host-owned and
    are mounted read-only; nothing is copied into the image.
    """
    binary = Path("/usr/bin/python3.12")
    if not binary.is_file():
        raise FileNotFoundError("host python3.12 closure required for the worker toolchain mount")
    lib_dir = Path("/usr/lib/x86_64-linux-gnu")
    libs = tuple(
        path
        for name in (
            "libpython3.12.so.1.0",
            "libc.so.6",
            "libm.so.6",
            "libz.so.1",
            "libexpat.so.1",
            "libffi.so.8",
        )
        if (path := lib_dir / name).is_file()
    )
    stdlib = Path("/usr/lib/python3.12")
    loader = Path("/lib64/ld-linux-x86-64.so.2")
    missing = [str(path) for path in (stdlib, loader, *libs) if not path.exists()]
    if missing:
        raise FileNotFoundError("incomplete host python closure: " + ", ".join(missing))
    return binary, (stdlib, loader, *libs)
