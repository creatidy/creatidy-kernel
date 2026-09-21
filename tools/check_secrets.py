# SPDX-License-Identifier: Apache-2.0
"""Fail on findings from an existing scanner, without printing secret values."""

import json
import subprocess
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
files = subprocess.run(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=True,
).stdout.split("\0")
# Explicit Git-visible paths avoid scanning ignored virtualenvs, credentials and local runtime state.
paths = [path for path in files if path and (ROOT / path).is_file() and not (ROOT / path).is_symlink()]
if not paths:
    raise SystemExit("No source files found for secret scan")
result = subprocess.run(
    ["uv", "run", "--locked", "detect-secrets", "scan", "--no-verify", "--", *paths],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=True,
)
scan = cast(dict[str, object], json.loads(result.stdout))
findings = cast(dict[str, object], scan["results"])
if findings:
    raise SystemExit("Secret scanner findings in: " + ", ".join(sorted(findings)))
print("Secret scan: no findings (no remote credential verification).")
