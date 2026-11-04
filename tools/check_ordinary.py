# SPDX-License-Identifier: Apache-2.0
"""Exercise installed ordinary handlers against deterministic local external peers."""

import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import creatidy_kernel
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.intake import IntakeRefused
from creatidy_kernel.ports.outcome_export import PRIVATE_METADATA, export_private_outcome

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
for exercise, exit_code in (("command-export-crash", 0), ("command-export-failed", 7)):
    root = storage / exercise
    if run_ordinary_flow(root, None, mode="pull", exercise=exercise)["status"] != "interrupted":
        raise SystemExit("installed command interruption was not exercised")
    with SQLiteProgramStore(root / "state.db") as store:
        before = store.served_change_token()
        exported = export_private_outcome(
            store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        commands = cast(list[dict[str, object]], exported["command_results"])
        if (
            len(commands) != 1
            or commands[0]["exit_code"] != exit_code
            or commands[0]["settlement"] != "witnessed"
            or exported["satisfactions"]
            or store.served_change_token() != before
            or "synthetic-private-command-output" in str(exported)
        ):
            raise SystemExit("installed reopened export lost or disclosed original command evidence")
if run_ordinary_flow(storage / "private-stop-uncertain", None, mode="managed", exercise="private-stop-uncertain") != {
    "status": "stopped-unknown",
    "native_sends": 0,
}:
    raise SystemExit("installed outcome export concealed uncertain managed cancellation")
for exercise in ("outcome-candidate-cut", "outcome-check-cut", "outcome-manifest-stop-cut"):
    root = storage / exercise
    if run_ordinary_flow(root, None, mode="pull", exercise=exercise) != {"status": "retained", "native_sends": 0}:
        raise SystemExit("installed partial outcome cut did not preserve its original evidence")
    with SQLiteProgramStore(root / "state.db") as store:
        exported = export_private_outcome(
            store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        if len(cast(list[object], exported["candidates"])) != 1 or exported["satisfactions"]:
            raise SystemExit("installed reopened partial outcome misrepresented candidate acceptance")
        if exercise == "outcome-check-cut" and not exported["checks"]:
            raise SystemExit("installed reopened partial outcome lost a recorded check")
        if exercise == "outcome-manifest-stop-cut" and not exported["verification_manifests"]:
            raise SystemExit("installed reopened partial outcome lost the pre-decision manifest")
with SQLiteProgramStore(storage / "private-stop-uncertain" / "state.db") as store:
    exported = export_private_outcome(store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    cancellations = cast(list[dict[str, object]], exported["cancellations"])
    if len(cancellations) != 1 or cancellations[0]["settlement_recorded"] is not False:
        raise SystemExit("installed reopened export misstated uncertain original cancellation")
for mode in ("pull", "managed"):
    result = run_ordinary_flow(storage / f"private-outcome-{mode}", None, mode=mode, exercise="private-outcome-export")
    if result["status"] != "completed" or result["satisfactions"] != 2:
        raise SystemExit("installed private outcome export did not preserve original producers")
for mode in ("pull", "managed"):
    result = run_ordinary_flow(storage / f"{mode}-stop", None, mode=mode, exercise="stop")
    if result["status"] != "stopped" or result["native_sends"] != 0:
        raise SystemExit(f"installed ordinary {mode} STOP did not revoke authority")
    if result["settled"] != (False if mode == "pull" else "cancelled"):
        raise SystemExit(f"installed ordinary {mode} STOP misreported task-scope settlement")
    result = run_ordinary_flow(storage / f"{mode}-review-remediation", None, mode=mode, exercise="review-remediation")
    if result["status"] != "completed" or result["satisfactions"] != 2:
        raise SystemExit(f"installed ordinary {mode} failed to deliver substantive review findings")
    with SQLiteProgramStore(storage / f"{mode}-review-remediation" / "state.db") as store:
        exported = export_private_outcome(
            store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        reviews = cast(list[dict[str, object]], exported["reviews"])
        attempts = cast(list[dict[str, object]], exported["attempts"])
        review = next(item for item in reviews if item["verdict"] == "request_changes")
        remediation = next(item for item in attempts if item["remediation_of"] == review["attempt"])
        original_findings = cast(list[dict[str, object]], review["findings"])
        if not original_findings or not all(
            item in cast(list[dict[str, object]], remediation["remediation_findings"]) for item in original_findings
        ):
            raise SystemExit("installed reopened export lost exact review finding lineage")
if run_ordinary_flow(
    storage / "review-remediation-tampered", None, mode="pull", exercise="review-remediation-tampered"
) != {"status": "refused", "native_sends": 0}:
    raise SystemExit("installed remediation accepted a review receipt from another subject")
for exercise in (
    "branch-pr",
    "review-lost",
    "review-lost-reopen",
    "diamond",
    "diamond-inherited",
    "many-comments",
    "command-evidence-crash",
):
    result = run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise)
    expected = 4 if exercise in {"diamond", "diamond-inherited"} else 2
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
with SQLiteProgramStore(storage / "stop-completed" / "state.db") as store:
    exported = export_private_outcome(store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    if cast(dict[str, object], exported["coverage"])["work_revocation_recorded"] is not True:
        raise SystemExit("installed reopened export lost completed-Program revocation")
for exercise in ("managed-program-stop", "managed-budget-revoke"):
    if run_ordinary_flow(storage / exercise, None, mode="managed", exercise=exercise) != {
        "status": "stopped",
        "native_sends": 0,
    }:
        raise SystemExit("installed managed reconciliation failed to retire the original target")
if run_ordinary_flow(storage / "served-claim-process", None, mode="pull", exercise="served-claim-process") != {
    "status": "checked",
    "native_sends": 0,
}:
    raise SystemExit("installed capability-bound work client failed its exact claim and replay")
if run_ordinary_flow(storage / "served-multi-client", None, mode="pull", exercise="served-multi-client") != {
    "status": "checked",
    "native_sends": 0,
}:
    raise SystemExit("installed one-shot clients did not share the host-bound worker session")
for exercise, expected in (("served-start", "checked"), ("served-start-crash", "unknown")):
    if run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise) != {
        "status": expected,
        "native_sends": 0,
    }:
        raise SystemExit("installed capability-bound start did not preserve its receipt or unknown outcome")
for exercise, expected in (
    ("served-quiesce", "checked"),
    ("served-owner-stop", "stopped"),
    ("served-owner-stale", "refused"),
    ("served-worker-stop", "refused"),
    ("served-worker-approve", "refused"),
    ("served-owner-approve", "approved"),
    ("served-owner-approve-stale", "refused"),
    ("served-owner-amend", "amended"),
    ("served-owner-multi-client", "stopped"),
    ("served-report", "accepted"),
    ("served-report-decision", "rejected"),
    ("sdk-review-notify", "rejected"),
    ("served-unknown-rejected", "rejected"),
    ("claim-final-observe-race", "refused"),
    ("claim-final-observe-replay-race", "refused"),
    ("served-owner-interleaved-amend", "stopped"),
    ("served-report-forged", "refused"),
    ("served-report-crash", "unknown"),
    ("served-reconcile-race", "refused"),
    ("served-renew", "checked"),
    ("served-renew-expired", "checked"),
    ("served-start-expired", "refused"),
    ("start-revision-race", "refused"),
    ("quiesce-revision-race", "refused"),
    ("renew-revision-race", "refused"),
    ("report-revision-race", "refused"),
    ("report-verification-revision-race", "refused"),
    ("report-review-revision-race", "unknown"),
    ("served-cross-action-id", "refused"),
    ("approve-revision-race", "refused"),
    ("stop-atomic-failure", "refused"),
):
    if run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise) != {
        "status": expected,
        "native_sends": 0,
    }:
        raise SystemExit("installed role-bound command did not preserve its authority boundary")
remediation = run_ordinary_flow(
    storage / "review-remediation-served", None, mode="pull", exercise="review-remediation-served"
)
if remediation["status"] != "completed" or remediation["satisfactions"] != 2:
    raise SystemExit("installed worker did not receive and complete original remediation context")
managed_lost = run_ordinary_flow(storage / "managed-review-lost", None, mode="managed", exercise="review-lost")
if managed_lost["status"] != "completed" or managed_lost["review_calls"] != 2:
    raise SystemExit("installed managed unknown review did not recover without a second prompt")
if run_ordinary_flow(storage / "served-managed-stop", None, mode="managed", exercise="served-managed-stop") != {
    "status": "stopped",
    "native_sends": 0,
}:
    raise SystemExit("installed owner STOP did not settle the original managed target")
if run_ordinary_flow(storage / "served-managed-stop-cli", None, mode="managed", exercise="served-managed-stop-cli") != {
    "status": "stopped",
    "native_sends": 0,
}:
    raise SystemExit("installed owner CLI failed to stop its original managed target")
if run_ordinary_flow(storage / "served-managed-start", None, mode="managed", exercise="served-managed-start") != {
    "status": "stopped",
    "native_sends": 0,
}:
    raise SystemExit("installed managed start/replay/STOP did not retain the original target")
if run_ordinary_flow(
    storage / "served-managed-worker-start", None, mode="managed", exercise="served-managed-worker-start"
) != {"status": "stopped", "native_sends": 0}:
    raise SystemExit("installed managed worker start consumed or stranded the owner target")
if run_ordinary_flow(
    storage / "served-managed-start-race", None, mode="managed", exercise="served-managed-start-race"
) != {"status": "refused", "native_sends": 0}:
    raise SystemExit("installed managed start failed to fence a stale caller revision")
for exercise in ("baseline-pending", "baseline-unsupported", "baseline-failure", "baseline-error"):
    result = run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise)
    expected = "refused" if exercise in {"baseline-pending", "baseline-unsupported"} else "approved"
    if result["status"] != expected:
        raise SystemExit("installed native baseline state was mistaken for a completed result")
for exercise in ("rejected-replay", "rejected-replay-reopen"):
    result = run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise)
    if result["status"] != "rejected":
        raise SystemExit("installed original rejected result did not replay")
for exercise in (
    "claim-expired",
    "claim-expired-reopen",
    "claim-revoked",
    "claim-stale",
    "claim-generation",
    "claim-revision-stale",
):
    if run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise) != {
        "status": "refused",
        "native_sends": 0,
    }:
        raise SystemExit("installed preparation used historical claim authority")
for exercise in ("injected-file", "injected-symlink", "injected-hardlink"):
    if run_ordinary_flow(storage / exercise, None, mode="pull", exercise=exercise) != {
        "status": "refused",
        "native_sends": 0,
    }:
        raise SystemExit("installed materialization accepted an unexpected existing entry")
if run_ordinary_flow(
    storage / "claim-revoked-during-preparation", None, mode="pull", exercise="claim-revoked-during-preparation"
) != {"status": "refused", "native_sends": 0}:
    raise SystemExit("installed preparation continued after authority revocation")
if run_ordinary_flow(storage / "claim-current", None, mode="pull", exercise="claim-current") != {
    "status": "recovered",
    "native_sends": 0,
}:
    raise SystemExit("installed current claim failed deterministic recovery")
conflict = storage / "diamond-conflict"
try:
    run_ordinary_flow(conflict, None, mode="pull", exercise="diamond-conflict")
except IntakeRefused:
    with SQLiteProgramStore(conflict / "state.db") as store:
        if store.load("everyday-program").state("delta").satisfaction_id is not None:
            raise SystemExit("installed conflicting join was accepted") from None
else:
    raise SystemExit("installed conflicting diamond was not refused")
print("Installed ordinary pull/managed/mixed normal/reopen/remediation/lost-reply and STOP journeys passed.")
