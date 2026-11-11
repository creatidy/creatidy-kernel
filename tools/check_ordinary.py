# SPDX-License-Identifier: Apache-2.0
"""Prepare installed ordinary state for the pinned older-reader proof."""

import json
import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import creatidy_kernel  # noqa: E402

run_ordinary_flow = cast(Callable[..., dict[str, object]], import_module("ordinary_flow_fixture").run_ordinary_flow)

if Path(creatidy_kernel.__file__).resolve().is_relative_to(ROOT):
    raise SystemExit("Ordinary fixture imported source instead of the installed wheel")
if len(sys.argv) != 3 or sys.argv[2] not in {"--preparation-only", "--proposal-chunks-only"}:
    raise SystemExit("Expected a directory and --preparation-only or --proposal-chunks-only")

if sys.argv[2] == "--proposal-chunks-only":
    result = run_ordinary_flow(
        Path(sys.argv[1]) / "proposal-chunks", None, mode="pull", exercise="served-large-installed-client"
    )
    if result != {"status": "read", "native_sends": 0}:
        raise SystemExit("Installed large proposal client journey failed")
    print("Installed large proposal client journey passed.")
    raise SystemExit(0)

result = run_ordinary_flow(Path(sys.argv[1]) / "pull-normal", None, mode="pull", exercise="normal")
if result["status"] != "completed" or result["satisfactions"] != 2 or result["native_sends"] != 4:
    raise SystemExit("Installed ordinary preparation did not complete the accepted journey")
(Path(sys.argv[1]) / "result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
print("Installed ordinary compatibility preparation passed.")
