# SPDX-License-Identifier: Apache-2.0
"""Build both archives and verify a real, dependency-free installed wheel."""

import argparse
import email
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--native-runtime", action="store_true", help="Run synthetic native factory cells from the rebuilt installed wheel"
)
options = parser.parse_args()
if options.native_runtime and any(
    not os.environ.get(name) for name in ("CREATIDY_TEST_OCI_TOOLROOT", "CREATIDY_TEST_BWRAP_BIN")
):
    raise SystemExit("Installed native factory UNPROVED: explicit OCI toolroot and bwrap pins required")


def _resolve_native_codex(probe_root: Path) -> tuple[str, Path]:
    """Use the same version-pinned environment/PATH resolver as the source native suite."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.codex_oci_proof.codexbin import CODEX_BIN_ENV, resolve_codex_binary

    return CODEX_BIN_ENV, resolve_codex_binary(probe_root).path


with tempfile.TemporaryDirectory(
    prefix="creatidy-kernel-package-",
    dir="/tmp/kilo" if options.native_runtime else None,  # noqa: S108 - approved retained native proof root.
    delete=not options.native_runtime,
) as directory:
    work = Path(directory)
    if options.native_runtime:
        codex_variable, codex_binary = _resolve_native_codex(work / "codex-probe")
        os.environ[codex_variable] = str(codex_binary)
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
    environment = work / "venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], cwd=work, check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", "--link-mode", "copy", str(rebuilt_wheel)],
        cwd=work,
        check=True,
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
            "assert a.select(ResourceRequest('unit', frozenset(), 0)).provider_id == 'local'; "
            "from tempfile import TemporaryDirectory; from pathlib import Path; "
            "from creatidy_kernel.adapters.worker_collection import collect_directory; "
            "from creatidy_kernel.adapters.worker_files import read_bounded_regular; "
            "from creatidy_kernel.adapters.worker_profile import "
            "WorkerProfile, attached_run_argv, owned_run_prefix, rootless_run_prefix; "
            "from creatidy_kernel.adapters.oci_lifecycle import LifetimeSpec, OCIResources, OwnedOCI; "
            "from creatidy_kernel.adapters.oci_lifetime import LifetimeChannel; "
            "from creatidy_kernel.adapters.git_workspace import GitWorkspace; "
            "assert callable(GitWorkspace.materialize) and callable(GitWorkspace.reconcile); "
            "profile = WorkerProfile('synthetic', (), (), Path('/explicit/seccomp.json'), 1, 1024, 0.5, 1); "
            "prefix = rootless_run_prefix(Path('/explicit/podman'), profile, 'owned'); "
            "assert prefix[:3] == ['/explicit/podman', 'run', '--pull=never']; "
            "assert '--network=none' in prefix; "
            "owned = owned_run_prefix(Path('/explicit/podman'), profile, 'owned'); "
            "assert '--rm' not in owned and '--cgroup-manager=cgroupfs' in owned; "
            "assert '--cgroupns=private' in owned; "
            "attached = attached_run_argv(Path('/explicit/podman'), profile, 'owned', 'localhost/proof:v1', "
            "('/explicit/entrypoint',)); "
            "assert attached == (*owned[:2], '-i', *owned[2:], 'localhost/proof:v1', '/explicit/entrypoint'); "
            "import sys\n"
            "with TemporaryDirectory() as directory:\n"
            "    root = Path(directory); target = root / 'candidate'; target.write_bytes(b'bounded')\n"
            "    assert read_bounded_regular(target) == b'bounded'\n"
            "    (root / 'refused-link').symlink_to(target)\n"
            "    collected = collect_directory(root)\n"
            "    assert collected.files['candidate'].size == 7\n"
            "    assert collected.refusals == {'refused-link': 'symlink-refused'}\n"
            "    assert not any(name == 'tools' or name.startswith('tools.') for name in sys.modules)",
        ],
        cwd=work,
        check=True,
    )
    subprocess.run([str(python), "-I", "-m", "creatidy_kernel.adapters.cli", "--help"], cwd=work, check=True)
    cli = environment / ("Scripts/creatidy-kernel.exe" if os.name == "nt" else "bin/creatidy-kernel")
    subprocess.run([str(cli), "reference", "--help"], cwd=work, check=True)
    for command in ("preflight", "run", "status", "export"):
        subprocess.run([str(cli), "task", command, "--help"], cwd=work, check=True)
    storage_root = Path(os.environ.get("CREATIDY_TEST_STORAGE_DIR", str(work))).expanduser()
    storage_root.mkdir(parents=True, exist_ok=True)
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
    if options.native_runtime:
        requirements = work / "locked-development.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--only-group",
                "dev",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--offline",
                "--python",
                str(python),
                "--constraint",
                str(requirements),
                "pytest==" + importlib.metadata.version("pytest"),
            ],
            cwd=work,
            check=True,
        )
        completed = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import sys; from pathlib import Path; import creatidy_kernel; "
                "assert Path(creatidy_kernel.__file__).resolve().is_relative_to(Path(sys.prefix)); "
                f"sys.path.insert(0, {str(ROOT)!r}); "
                "import pytest; "
                f"result = pytest.main([{str(ROOT / 'tests/test_oci_runtime_conformance.py')!r}, "
                "'-p', 'tools.native_reception', '-o', 'pythonpath=', '-o', 'addopts=', '-q', '--tb=short']); "
                "assert all(Path(module.__file__).resolve().is_relative_to(Path(sys.prefix)) "
                "for name, module in sys.modules.items() if name.startswith('creatidy_kernel') "
                "and getattr(module, '__file__', None)); "
                "raise SystemExit(result)",
            ],
            cwd=work,
            check=True,
        )
        print(f"Installed native factory reception passed; wheel/environment retained at {work}")
print("Package check: wheel metadata/license, sdist rebuild, isolated install and CLI replay/export passed.")
