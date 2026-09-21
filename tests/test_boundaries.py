# SPDX-License-Identifier: Apache-2.0
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_imports_and_fixed_allocator_work_without_site_packages(tmp_path: Path) -> None:
    code = f"""
import sys
sys.path.insert(0, {str(ROOT / "src")!r})
from creatidy_kernel.core.resources import Allocation, ResourceRequest
from creatidy_kernel.ports.resources import ResourceAllocator
assert not any(name.startswith('creatidy_kernel.adapters') for name in sys.modules)
from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
allocator: ResourceAllocator = FixedAllocator(Allocation('runtime', 'local', 'model', frozenset(), 0, 'test'))
assert allocator.select(ResourceRequest('work-unit', frozenset(), 0)).provider_id == 'local'
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_core_and_ports_have_only_standard_library_or_internal_imports() -> None:
    allowed = sys.stdlib_module_names | {"creatidy_kernel"}
    for layer in ("core", "ports"):
        for path in (ROOT / "src" / "creatidy_kernel" / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    assert all(alias.name.split(".")[0] in allowed for alias in node.names), path
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    assert node.module is not None and node.module.split(".")[0] in allowed, path
