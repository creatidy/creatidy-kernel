# SPDX-License-Identifier: Apache-2.0
"""Exercise installed ordinary handlers against deterministic local external peers."""

import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import creatidy_kernel

ROOT = Path(__file__).resolve().parents[1]
if Path(creatidy_kernel.__file__).resolve().is_relative_to(ROOT):
    raise SystemExit("ordinary acceptance must use the installed package, not checkout sources")

sys.path.insert(0, str(ROOT / "tests"))
fixture = runpy.run_path(str(ROOT / "tests/ordinary_flow_fixture.py"))
run_ordinary_flow = cast(Callable[..., dict[str, object]], fixture["run_ordinary_flow"])

storage = Path(sys.argv[1])
for mode in ("pull", "managed", "mixed"):
    for exercise in ("normal", "reopen", "remediation", "lost-reply"):
        result = run_ordinary_flow(storage / f"{mode}-{exercise}", None, mode=mode, exercise=exercise)
        if result["status"] != "completed" or result["satisfactions"] != 2 or result["native_sends"] != 4:
            raise SystemExit(f"installed ordinary {mode}/{exercise} did not complete exact accepted work")
for mode in ("pull", "managed"):
    result = run_ordinary_flow(storage / f"{mode}-stop", None, mode=mode, exercise="stop")
    if result["status"] != "stopped" or result["native_sends"] != 0:
        raise SystemExit(f"installed ordinary {mode} STOP did not revoke authority")
    if result["settled"] != (False if mode == "pull" else "cancelled"):
        raise SystemExit(f"installed ordinary {mode} STOP misreported task-scope settlement")
for exercise in ("branch-pr", "review-lost", "review-lost-reopen", "diamond"):
    result = run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise)
    expected = 4 if exercise == "diamond" else 2
    if result["status"] != "completed" or result["satisfactions"] != expected or result["review_calls"] != expected:
        raise SystemExit(f"installed ordinary {exercise} recovery did not retain exact completed work")
if run_ordinary_flow(storage / "owner-binding", None, mode="pull", exercise="owner-binding") != {
    "status": "unassessed"
}:
    raise SystemExit("installed source assessment was reused for a different declaration")
if run_ordinary_flow(storage / "ambiguous-pr", None, mode="pull", exercise="ambiguous-pr") != {
    "status": "unknown",
    "native_sends": 1,
}:
    raise SystemExit("installed ambiguous Forgejo marker was accepted or retried")
print("Installed ordinary pull/managed/mixed normal/reopen/remediation/lost-reply and STOP journeys passed.")
