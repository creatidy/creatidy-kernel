# SPDX-License-Identifier: Apache-2.0
"""Build both archives and verify a real, dependency-free installed wheel."""

import email
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix="creatidy-kernel-package-") as directory, ExitStack() as cleanup:
    started = time.perf_counter()
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    quota_path = Path("/sys/fs/cgroup/cpu.max")
    quota = quota_path.read_text(encoding="ascii").strip() if quota_path.is_file() else "unavailable"
    print(f"Package test runner CPU affinity: {affinity}; cgroup CPU quota: {quota}", flush=True)
    work = Path(directory)
    dist = work / "dist"
    build = ["uv", "build", "--no-sources", "--no-build-isolation", "--python", sys.executable]
    subprocess.run([*build, "--out-dir", str(dist)], cwd=ROOT, check=True)
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
        notice_name = next(name for name in names if name.endswith(".dist-info/licenses/NOTICE"))
        if archive.read(notice_name) != (ROOT / "NOTICE").read_bytes():
            raise SystemExit("Wheel must include adapted-source attribution")
        if any(not (name.startswith("creatidy_kernel/") or ".dist-info/" in name) for name in names):
            raise SystemExit("Unexpected non-package content in wheel")
        package_bytes = {name: archive.read(name) for name in names if name.startswith("creatidy_kernel/")}
    rebuilt = work / "rebuilt"
    subprocess.run([*build, str(sdists[0]), "--wheel", "--out-dir", str(rebuilt)], cwd=work, check=True)
    rebuilt_wheel = next(rebuilt.glob("*.whl"))
    with zipfile.ZipFile(rebuilt_wheel) as archive:
        rebuilt_bytes = {name: archive.read(name) for name in archive.namelist() if name.startswith("creatidy_kernel/")}
    if package_bytes != rebuilt_bytes:
        raise SystemExit("sdist does not rebuild the same package contents")
    print(f"Package archives and reproducibility: {time.perf_counter() - started:.2f}s", flush=True)
    started = time.perf_counter()
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
            "from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator; "
            "from creatidy_kernel.adapters.bubblewrap_verification import BubblewrapVerifier; "
            "from creatidy_kernel.ports.verification import VerificationProfile; "
            "assert VerificationProfile.TRUSTED_DEVELOPMENT.value == 'trusted_development'; "
            "from creatidy_kernel.core.resources import Allocation, ResourceRequest; "
            "a = FixedAllocator(Allocation('runtime', 'local', 'model', frozenset(), 0, 'smoke')); "
            "assert a.select(ResourceRequest('unit', frozenset(), 0)).provider_id == 'local'",
        ],
        cwd=work,
        check=True,
    )
    subprocess.run([str(python), "-I", "-m", "creatidy_kernel.adapters.cli", "--help"], cwd=work, check=True)
    cli = environment / ("Scripts/creatidy-kernel.exe" if os.name == "nt" else "bin/creatidy-kernel")
    storage_root = Path(os.environ.get("CREATIDY_TEST_STORAGE_DIR", str(work))).expanduser()
    storage_root.mkdir(parents=True, exist_ok=True)
    ordinary_directory = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="ordinary-", dir=storage_root)))
    subprocess.run(
        [str(python), "-I", str(ROOT / "tools/check_ordinary.py"), str(ordinary_directory), "--preparation-only"],
        cwd=work,
        check=True,
    )
    subprocess.run(
        [str(python), "-I", str(ROOT / "tools/check_ordinary.py"), str(ordinary_directory), "--proposal-chunks-only"],
        cwd=work,
        check=True,
    )
    if not (shutil.which("sops") and shutil.which("age-keygen")):
        print("Installed SOPS/age migration: UNPROVED (local sops and age-keygen required)")
    subprocess.run([str(cli), "reference", "--help"], cwd=work, check=True)
    for command in ("preflight", "run", "status", "export"):
        subprocess.run([str(cli), "task", command, "--help"], cwd=work, check=True)
    with tempfile.TemporaryDirectory(prefix="reference-", dir=storage_root) as state_directory:
        state = Path(state_directory)
        reference_attempts: object = None
        for arguments in (
            ["reference", "--data-dir", str(state), "--approve"],
            ["reference", "--data-dir", str(state), "--approve"],
            ["export", "--data-dir", str(state)],
        ):
            completed = subprocess.run([str(cli), *arguments], cwd=work, check=True, capture_output=True, text=True)
            exported = json.loads(completed.stdout)
            if exported["status"] != "completed" or len(exported["accepted"]) != 2:
                raise SystemExit("Installed CLI did not complete both accepted WorkUnits")
            if exported["automatic_merge"] or exported["automatic_deploy"]:
                raise SystemExit("Installed reference must not authorize merge or deployment")
            if reference_attempts is None:
                reference_attempts = exported["attempts"]
            elif reference_attempts != exported["attempts"]:
                raise SystemExit("Installed CLI rerun/export changed the logical Attempts")
    with tempfile.TemporaryDirectory(prefix="prepared-", dir=storage_root) as state_directory:
        subprocess.run(
            [str(python), "-I", str(ROOT / "tools/check_prepared.py"), state_directory], cwd=work, check=True
        )
    with tempfile.TemporaryDirectory(prefix="source-", dir=storage_root) as source_directory:
        subprocess.run([str(python), "-I", str(ROOT / "tools/check_source.py"), source_directory], cwd=work, check=True)
    with tempfile.TemporaryDirectory(prefix="work-budget-", dir=storage_root) as budget_directory:
        subprocess.run(
            [str(python), "-I", str(ROOT / "tools/check_work_budget.py"), budget_directory], cwd=work, check=True
        )
    with tempfile.TemporaryDirectory(prefix="work-ownership-", dir=storage_root) as ownership_directory:
        subprocess.run(
            [str(python), "-I", str(ROOT / "tools/check_work_ownership.py"), ownership_directory], cwd=work, check=True
        )
    with tempfile.TemporaryDirectory(prefix="drivers-", dir=storage_root) as driver_directory:
        subprocess.run(
            [str(python), "-I", str(ROOT / "tools/check_drivers.py"), driver_directory], cwd=work, check=True
        )
    with tempfile.TemporaryDirectory(prefix="served-", dir=storage_root) as served_directory:
        subprocess.run([str(python), "-I", str(ROOT / "tools/check_served.py"), served_directory], cwd=work, check=True)
    with tempfile.TemporaryDirectory(prefix="outcome-", dir=storage_root) as outcome_directory:
        subprocess.run(
            [str(python), "-I", str(ROOT / "tools/check_outcome.py"), outcome_directory], cwd=work, check=True
        )
    print(f"Dependency-free installed journeys: {time.perf_counter() - started:.2f}s", flush=True)
    started = time.perf_counter()
    reader_cache = work / "historical-readers"
    reader_cache.mkdir(mode=0o700)
    test_environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(work),
        "LANG": "C.UTF-8",
        "UV_PROJECT_ENVIRONMENT": str(environment),
        "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv")),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "CREATIDY_TEST_INSTALLED_PREFIX": str(environment),
        "CREATIDY_TEST_INSTALLED_CLI": str(cli),
        "CREATIDY_TEST_PREPARED_ORDINARY": str(ordinary_directory),
        "CREATIDY_TEST_READER_CACHE": str(reader_cache),
    }
    for name in ("UV_OFFLINE", "CREATIDY_TEST_STORAGE_DIR"):
        if name in os.environ:
            test_environment[name] = os.environ[name]
    subprocess.run(
        ["uv", "sync", "--locked", "--only-group", "test", "--no-install-project", "--inexact"],
        cwd=ROOT,
        env=test_environment,
        check=True,
    )
    installed = subprocess.run(
        [str(python), "-I", "-c", "import creatidy_kernel; print(creatidy_kernel.__file__)"],
        cwd=work,
        env=test_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    installed_package = Path(installed.stdout.strip()).resolve(strict=True).parent
    if not installed_package.is_relative_to(environment.resolve()):
        raise SystemExit("Test interpreter did not import the installed wheel")
    for name, content in rebuilt_bytes.items():
        if (installed_package / Path(name).relative_to("creatidy_kernel")).read_bytes() != content:
            raise SystemExit(f"Installed test package differs from rebuilt wheel: {name}")
    subprocess.run(
        [
            str(python),
            "-I",
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-n",
            "8",
            "--dist",
            "worksteal",
            "-m",
            "not serial_native",
            str(ROOT / "tests"),
        ],
        cwd=work,
        env=test_environment,
        check=True,
    )
    print(f"Installed pytest suite and test environment: {time.perf_counter() - started:.2f}s", flush=True)
    started = time.perf_counter()
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/check_schema_upgrade.py"),
            str(python),
            str(storage_root),
            str(ordinary_directory),
        ],
        cwd=work,
        env=test_environment,
        check=True,
    )
    print(f"Installed historical compatibility: {time.perf_counter() - started:.2f}s", flush=True)
    subprocess.run(
        [sys.executable, str(ROOT / "tools/check_uninstall.py"), str(python), str(cli), str(storage_root)],
        cwd=work,
        check=True,
    )
print("Package check: wheel metadata/license, sdist rebuild, isolated install and CLI replay/export passed.")
