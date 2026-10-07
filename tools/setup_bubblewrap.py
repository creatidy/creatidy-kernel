# SPDX-License-Identifier: Apache-2.0
"""Offline unprivileged build from the exact upstream source archive.

Setup only, never run by pytest. Does not download, install or modify upstream
source, host packages/settings or credentials. Run with locked sandbox-build
tools; libcap headers/pkg-config must already be in an explicit extracted prefix.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

SOURCE = "9ca3b05ec787acfb4b17bed37db5719fa777834f"  # pragma: allowlist secret - public upstream revision
# Public upstream archive digest, not an authentication credential.
ARCHIVE_SHA256 = "552cec9c79bb85c8ecd25ae3e55efca9af03390c17d829885ee45722dedada4f"  # pragma: allowlist secret


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory below an existing owned parent")
    parser.add_argument(
        "--libcap-prefix", type=Path, required=True, help="already extracted matching development/runtime prefix"
    )
    args = parser.parse_args()
    archive = Path(args.archive).resolve(strict=True)
    output = Path(args.output).absolute()
    prefix = Path(args.libcap_prefix).resolve(strict=True)
    if sys.platform != "linux" or os.getuid() == 0 or output.exists() or output.parent.resolve() != output.parent:
        raise SystemExit("setup refused: platform/output ownership")
    if output.parent.stat().st_uid != os.getuid():
        raise SystemExit("setup refused: output parent ownership")
    if archive.stat().st_size > 4 * 1024 * 1024 or hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise SystemExit("setup refused: source archive provenance")
    output.mkdir(mode=0o700)
    with tarfile.open(archive) as source_archive:
        members = source_archive.getmembers()
        if len(members) > 2000 or sum(member.size for member in members) > 32 * 1024 * 1024:
            raise SystemExit("setup refused: archive bounds")
        if any(
            not (member.isfile() or member.isdir())
            and not (member.issym() and member.name == f"bubblewrap-{SOURCE}/LICENSE" and member.linkname == "COPYING")
            for member in members
        ):
            raise SystemExit("setup refused: archive topology")
        source_archive.extractall(output, filter="data")
    source = output / f"bubblewrap-{SOURCE}"
    build = output / "build"
    home = output / "home"
    home.mkdir(mode=0o700)
    environment = {
        "HOME": str(home),
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PKG_CONFIG_LIBDIR": str(prefix / "usr/lib/x86_64-linux-gnu/pkgconfig"),
        "PKG_CONFIG_SYSROOT_DIR": str(prefix),
        "LDFLAGS": f"-L{prefix}/usr/lib/x86_64-linux-gnu",
        "CC": "/usr/bin/gcc",
    }
    meson = [sys.executable, "-m", "mesonbuild.mesonmain"]
    subprocess.run(
        [
            *meson,
            "setup",
            str(build),
            str(source),
            "-Dtests=false",
            "-Dman=disabled",
            "-Dselinux=disabled",
            "-Dbash_completion=disabled",
            "-Dzsh_completion=disabled",
            f"-Dpython={sys.executable}",
        ],
        cwd=output,
        env=environment,
        check=True,
        timeout=120,
    )
    subprocess.run([*meson, "compile", "-C", str(build), "bwrap"], cwd=output, env=environment, check=True, timeout=120)
    binary = build / "bwrap"
    version = subprocess.run(
        [str(binary), "--version"],
        cwd=output,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    if version != b"bubblewrap 0.11.0":
        raise SystemExit("setup refused: native version")
    # Receipt is build metadata, not a security verdict or source/binary signature.
    print(
        json.dumps(
            {
                "source_revision": SOURCE,
                "archive_sha256": ARCHIVE_SHA256,
                "binary": str(binary),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "version": version.decode(),
                "upstream_modified": False,
                "denial_proven": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
