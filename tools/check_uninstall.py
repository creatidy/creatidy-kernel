# SPDX-License-Identifier: Apache-2.0
"""Exercise uninstall in a disposable installed environment, preserving synthetic data."""

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


def main(python: Path, cli: Path, storage_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="uninstall-", dir=storage_root) as directory:
        lifecycle = Path(directory)
        state = lifecycle / "state"
        profile = lifecycle / "owner-profile.env"
        profile.write_text("EXAMPLE=fixture\n", encoding="utf-8")
        subprocess.run(
            [str(cli), "reference", "--data-dir", str(state), "--approve"],
            cwd=lifecycle,
            check=True,
            capture_output=True,
        )

        def snapshot() -> dict[str, tuple[str, int, int]]:
            return {
                str(path.relative_to(lifecycle)): (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                    path.stat().st_mode,
                )
                for path in lifecycle.rglob("*")
                if path.is_file() and not path.is_symlink()
            }

        before = snapshot()
        subprocess.run(
            ["uv", "pip", "uninstall", "--python", str(python), "creatidy-kernel"], cwd=lifecycle, check=True
        )
        if snapshot() != before or cli.exists():
            raise RuntimeError("uninstall changed retained state/profile or left installed CLI")
        absent = subprocess.run(
            [str(python), "-I", "-c", "import creatidy_kernel"], cwd=lifecycle, capture_output=True, check=False
        )
        if absent.returncode == 0:
            raise RuntimeError("uninstalled package remains importable")
        print("Installed lifecycle: uninstall retained synthetic state and owner profile bytes/mtime.")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
