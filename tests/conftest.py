# SPDX-License-Identifier: Apache-2.0
"""Test bootstrap: expose the development-tool PoC package without packaging it."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _assert_installed_package() -> None:
    configured = os.environ.get("CREATIDY_TEST_INSTALLED_PREFIX")
    if configured is None:
        return
    prefix = Path(configured).resolve(strict=True)
    if Path(sys.prefix).resolve(strict=True) != prefix:
        raise RuntimeError("Installed test interpreter is not the designated wheel environment")
    import creatidy_kernel

    origin = Path(creatidy_kernel.__file__).resolve(strict=True)
    if not origin.is_relative_to(prefix) or origin.is_relative_to(ROOT):
        raise RuntimeError("Installed tests imported the checkout instead of the wheel")
    for name, module in tuple(sys.modules.items()):
        if name.startswith("creatidy_kernel.") and (filename := getattr(module, "__file__", None)) is not None:
            if not Path(filename).resolve(strict=True).is_relative_to(prefix):
                raise RuntimeError(f"Installed tests imported {name} outside the wheel environment")


def pytest_sessionstart() -> None:
    _assert_installed_package()


def pytest_sessionfinish() -> None:
    _assert_installed_package()
