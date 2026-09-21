# SPDX-License-Identifier: Apache-2.0
"""Audit locked third-party packages, not the unpublished editable project."""

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="creatidy-kernel-audit-") as directory:
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-emit-project",
            "--format",
            "pylock.toml",
            "--output-file",
            str(Path(directory) / "pylock.toml"),
            "--quiet",
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(["uv", "run", "--locked", "pip-audit", "--strict", "--locked", directory], cwd=ROOT, check=True)
