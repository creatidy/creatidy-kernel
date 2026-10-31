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
preparation_only = len(sys.argv) == 3 and sys.argv[2] == "--preparation-only"
if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and not preparation_only):
    raise SystemExit("expected storage directory and optional --preparation-only")
for mode in ("pull", "managed", "mixed"):
    for exercise in ("normal", "reopen", "remediation", "lost-reply"):
        if preparation_only and (mode, exercise) != ("pull", "normal"):
            continue
        result = run_ordinary_flow(storage / f"{mode}-{exercise}", None, mode=mode, exercise=exercise)
        if result["status"] != "completed" or result["satisfactions"] != 2 or result["native_sends"] != 4:
            raise SystemExit(f"installed ordinary {mode}/{exercise} did not complete exact accepted work")
if preparation_only:
    print("Installed ordinary preparation fixture passed.")
    raise SystemExit(0)
for mode in ("pull", "managed"):
    result = run_ordinary_flow(storage / f"{mode}-stop", None, mode=mode, exercise="stop")
    if result["status"] != "stopped" or result["native_sends"] != 0:
        raise SystemExit(f"installed ordinary {mode} STOP did not revoke authority")
    if result["settled"] != (False if mode == "pull" else "cancelled"):
        raise SystemExit(f"installed ordinary {mode} STOP misreported task-scope settlement")
    result = run_ordinary_flow(storage / f"{mode}-review-remediation", None, mode=mode, exercise="review-remediation")
    if result["status"] != "completed" or result["satisfactions"] != 2:
        raise SystemExit(f"installed ordinary {mode} failed to deliver substantive review findings")
if run_ordinary_flow(
    storage / "review-remediation-tampered", None, mode="pull", exercise="review-remediation-tampered"
) != {"status": "refused", "native_sends": 0}:
    raise SystemExit("installed remediation accepted a review receipt from another subject")
for exercise in (
    "branch-pr",
    "review-lost",
    "review-lost-reopen",
    "diamond",
    "many-comments",
    "command-evidence-crash",
):
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
for exercise in ("duplicate-comments", "oversized-comments"):
    result = run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise)
    if result["status"] != "unknown":
        raise SystemExit("installed ambiguous comment collection was accepted")
for exercise in ("command-evidence-no-witness", "command-evidence-partial"):
    if run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise) != {
        "status": "unknown",
        "native_sends": 0,
    }:
        raise SystemExit("installed recovery accepted incomplete original command evidence")
if run_ordinary_flow(storage / "rename-outside", None, mode="pull", exercise="rename-outside") != {
    "status": "rejected",
    "native_sends": 0,
}:
    raise SystemExit("installed scope gate accepted an out-of-scope rename source")
if run_ordinary_flow(storage / "topic-crash", None, mode="pull", exercise="topic-crash") != {
    "status": "topic-recovered",
    "native_sends": 2,
}:
    raise SystemExit("installed publication failed to recover its original topic")
if run_ordinary_flow(storage / "topic-artifact-conflict", None, mode="pull", exercise="topic-artifact-conflict") != {
    "status": "unknown",
    "native_sends": 0,
}:
    raise SystemExit("installed publication accepted a conflicting topic artifact")
if run_ordinary_flow(storage / "stop-completed", None, mode="pull", exercise="stop-completed") != {
    "status": "completed-revoked",
    "native_sends": 0,
}:
    raise SystemExit("installed STOP changed completion or allowed later publication")
for exercise in ("managed-program-stop", "managed-budget-revoke"):
    if run_ordinary_flow(storage / exercise, None, mode="managed", exercise=exercise) != {
        "status": "stopped",
        "native_sends": 0,
    }:
        raise SystemExit("installed managed reconciliation failed to retire the original target")
for exercise in ("baseline-pending", "baseline-unsupported", "baseline-failure", "baseline-error"):
    result = run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise)
    expected = "refused" if exercise in {"baseline-pending", "baseline-unsupported"} else "approved"
    if result["status"] != expected:
        raise SystemExit("installed native baseline state was mistaken for a completed result")
for exercise in ("rejected-replay", "rejected-replay-reopen"):
    result = run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise)
    if result["status"] != "rejected":
        raise SystemExit("installed original rejected result did not replay")
print("Installed ordinary pull/managed/mixed normal/reopen/remediation/lost-reply and STOP journeys passed.")
