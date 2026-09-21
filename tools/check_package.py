# SPDX-License-Identifier: Apache-2.0
"""Build both archives and verify a real, dependency-free installed wheel."""

import email
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix="creatidy-kernel-package-") as directory:
    work = Path(directory)
    dist = work / "dist"
    subprocess.run(["uv", "build", "--no-sources", "--out-dir", str(dist)], cwd=ROOT, check=True)
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("Expected exactly one wheel and sdist")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_name))
        if metadata.get("Requires-Dist") or metadata.get("License-Expression") != "Apache-2.0":
            raise SystemExit("Wheel dependency/license contract violated")
        if "creatidy_kernel/py.typed" not in names:
            raise SystemExit("Typed-package marker missing")
        license_name = next(name for name in names if name.endswith(".dist-info/licenses/LICENSE"))
        if archive.read(license_name) != (ROOT / "LICENSE").read_bytes():
            raise SystemExit("Wheel must include the complete project license")
        if any(not (name.startswith("creatidy_kernel/") or ".dist-info/" in name) for name in names):
            raise SystemExit("Unexpected non-package content in wheel")
        package_bytes = {name: archive.read(name) for name in names if name.startswith("creatidy_kernel/")}
    rebuilt = work / "rebuilt"
    subprocess.run(
        ["uv", "build", str(sdists[0]), "--wheel", "--no-sources", "--out-dir", str(rebuilt)], cwd=work, check=True
    )
    rebuilt_wheel = next(rebuilt.glob("*.whl"))
    with zipfile.ZipFile(rebuilt_wheel) as archive:
        rebuilt_bytes = {name: archive.read(name) for name in archive.namelist() if name.startswith("creatidy_kernel/")}
    if package_bytes != rebuilt_bytes:
        raise SystemExit("sdist does not rebuild the same package contents")
    environment = work / "venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], cwd=work, check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(rebuilt_wheel)], cwd=work, check=True
    )
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "from creatidy_kernel.adapters.fixed_allocator import FixedAllocator; "
            "from creatidy_kernel.core.resources import Allocation, ResourceRequest; "
            "a = FixedAllocator(Allocation('runtime', 'local', 'model', frozenset(), 0, 'smoke')); "
            "assert a.select(ResourceRequest('unit', frozenset(), 0)).provider_id == 'local'",
        ],
        cwd=work,
        check=True,
    )
print("Package check: wheel metadata/license, sdist rebuild and isolated install passed.")
