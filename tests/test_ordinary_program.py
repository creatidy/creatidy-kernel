# SPDX-License-Identifier: Apache-2.0
import os
from contextlib import closing
from pathlib import Path

import pytest
from ordinary_flow_fixture import run_ordinary_flow
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path

from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.intake import IntakeRefused
from creatidy_kernel.core.ownership import OwnershipConflict
from creatidy_kernel.ports.ownership import WorkOwnership


@pytest.mark.parametrize("mode", ["pull", "managed", "mixed"])
@pytest.mark.parametrize("exercise", ["normal", "reopen", "remediation", "lost-reply"])
def test_ordinary_installed_handlers_with_synthetic_external_peers(
    sqlite_tmp_path: Path, mode: str, exercise: str
) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / mode, None, mode=mode, exercise=exercise)
    assert result["status"] == "completed"
    assert result["satisfactions"] == 2
    assert result["native_sends"] == 4
    assert result["native_kilo"] is False


def test_materialization_cannot_precede_atomic_claim(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize = OrdinaryGitWorkspace.initialize

    def guarded(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec) -> str:
        program = workspace.store.load(attempt.program_id)
        assert any(item.attempt_id == attempt.attempt_id for item in program.attempts), (
            "workspace mutation began before the Attempt and resource claim were admitted"
        )
        return initialize(workspace, attempt)

    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", guarded)
    run_ordinary_flow(sqlite_tmp_path / "claimed-before-materialization", None, mode="pull")


def test_changed_preparation_is_preserved_and_not_auto_attributed(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize = OrdinaryGitWorkspace.initialize
    touched: list[Path] = []

    def edit_after_preparation(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec) -> str:
        subject = initialize(workspace, attempt)
        target = workspace.workspace.path / "alpha.py"
        target.write_bytes(b"manual change must be retained\n")
        touched.append(target)
        return subject

    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", edit_after_preparation)
    with pytest.raises(IntakeRefused, match="prepared workspace changed"):
        run_ordinary_flow(sqlite_tmp_path / "changed-preparation", None, mode="pull")
    assert touched[0].read_bytes() == b"manual change must be retained\n"


def test_refused_claim_never_materializes_workspace(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OwnershipConflict("synthetic resource conflict")

    def forbidden(*_args: object, **_kwargs: object) -> str:
        pytest.fail("controller wrote the workspace after a refused claim")

    monkeypatch.setattr(WorkOwnership, "claim", refuse)
    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", forbidden)
    with pytest.raises(OwnershipConflict, match="resource conflict"):
        run_ordinary_flow(sqlite_tmp_path / "refused-claim", None, mode="pull")


def test_owner_source_judgement_is_bound_to_exact_declaration(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "owner-binding", None, mode="pull", exercise="owner-binding") == {
        "status": "unassessed"
    }


def test_lost_agit_reply_ignores_unrelated_branch_pr_without_duplicate_write(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "branch-pr", None, mode="pull", exercise="branch-pr")
    assert result["status"] == "completed"
    assert result["native_sends"] == 4


def test_copied_marker_on_unrelated_branch_is_unknown_without_retry(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "ambiguous-pr", None, mode="pull", exercise="ambiguous-pr") == {
        "status": "unknown",
        "native_sends": 1,
    }


@pytest.mark.parametrize("exercise", ["review-lost", "review-lost-reopen"])
def test_completed_review_receipt_recovers_without_second_prompt(sqlite_tmp_path: Path, exercise: str) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise)
    assert result["status"] == "completed"
    assert result["review_calls"] == 2


def test_diamond_dag_composes_shared_ancestor_once(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "diamond", None, mode="pull", exercise="diamond")
    assert result["status"] == "completed"
    assert result["satisfactions"] == 4
    assert result["native_sends"] == 8


def test_out_of_scope_source_renamed_to_allowed_destination_is_rejected(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "rename-outside", None, mode="pull", exercise="rename-outside") == {
        "status": "rejected",
        "native_sends": 0,
    }


def test_topic_intent_recovers_exact_original_after_artifact_crash_and_reopen(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "topic-crash", None, mode="pull", exercise="topic-crash") == {
        "status": "topic-recovered",
        "native_sends": 2,
    }


def test_topic_artifact_different_from_original_intent_never_publishes(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "topic-artifact-conflict", None, mode="pull", exercise="topic-artifact-conflict"
    ) == {"status": "unknown", "native_sends": 0}


def test_diamond_dag_preserves_conflicting_predecessors_without_accepting_join(sqlite_tmp_path: Path) -> None:
    root = sqlite_tmp_path / "diamond-conflict"
    with pytest.raises(IntakeRefused, match="bounded ordinary Git operation refused"):
        run_ordinary_flow(root, None, mode="pull", exercise="diamond-conflict")
    with closing(SQLiteProgramStore(root / "state.db")) as store:
        program = store.load("everyday-program")
        assert {item.work_unit_id for item in program.satisfactions} == {"alpha", "beta", "gamma"}
        assert program.state("delta").satisfaction_id is None


@pytest.mark.parametrize("exercise", ["rejected-replay", "rejected-replay-reopen"])
def test_rejected_report_replays_exact_evidence_after_time_and_reopen(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise)["status"] == "rejected"


def test_unpaginated_comment_collection_above_thirty_is_complete(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "many-comments", None, mode="pull", exercise="many-comments")
    assert result["status"] == "completed" and result["native_sends"] == 4


def test_completed_command_evidence_recovers_after_aggregate_commit_failure(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(
        sqlite_tmp_path / "aggregate-crash", None, mode="pull", exercise="command-evidence-crash"
    )
    assert result["status"] == "completed" and result["satisfactions"] == 2


def test_command_aggregate_recovery_refuses_missing_original_witness(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(
        sqlite_tmp_path / "missing-witness", None, mode="pull", exercise="command-evidence-no-witness"
    )
    assert result == {"status": "unknown", "native_sends": 0}


def test_command_aggregate_recovery_refuses_partial_results_without_rerun(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(
        sqlite_tmp_path / "partial-results", None, mode="pull", exercise="command-evidence-partial"
    )
    assert result == {"status": "unknown", "native_sends": 0}


def test_completed_program_can_revoke_remaining_publication_without_rewriting_acceptance(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "completed-stop", None, mode="pull", exercise="stop-completed") == {
        "status": "completed-revoked",
        "native_sends": 0,
    }


@pytest.mark.parametrize("exercise", ["managed-program-stop", "managed-budget-revoke"])
def test_managed_reconciliation_retires_original_target_after_revocation(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="managed", exercise=exercise) == {
        "status": "stopped",
        "native_sends": 0,
    }


@pytest.mark.parametrize("exercise", ["baseline-pending", "baseline-unsupported"])
def test_unresolved_native_baseline_cannot_use_failing_baseline_exception(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise) == {"status": "refused"}


@pytest.mark.parametrize("exercise", ["baseline-failure", "baseline-error"])
def test_exact_completed_native_failure_allows_explicit_repair_policy(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise) == {"status": "approved"}


@pytest.mark.parametrize("exercise", ["duplicate-comments", "oversized-comments"])
def test_ambiguous_or_oversized_comment_collection_never_retries(sqlite_tmp_path: Path, exercise: str) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise)
    assert result["status"] == "unknown"


def test_interrupted_preparation_retains_claim_and_unknown_bytes(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[Path] = []
    attempts: list[str] = []

    def interrupted(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec) -> str:
        target = workspace.workspace.path / "interrupted.txt"
        target.write_bytes(b"preserve interrupted controller data\n")
        touched.append(target)
        attempts.append(attempt.attempt_id)
        raise RuntimeError("synthetic preparation interruption")

    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", interrupted)
    root = sqlite_tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="preparation interruption"):
        run_ordinary_flow(root, None, mode="pull")
    assert touched[0].read_bytes() == b"preserve interrupted controller data\n"
    with closing(SQLiteProgramStore(root / "state.db")) as store:
        assert store.operations("work-budget:v1:ownership-claim:")
        assert not store.operations("work-budget:v1:ownership-release:")
        assert store.operation(f"runtime:{attempts[0]}").attempts == 0
        assert store.find_artifact(f"ordinary-workspace:{attempts[0]}", "prepared") is None


@pytest.mark.parametrize("mode", ["pull", "managed", "mixed"])
def test_ordinary_two_obligation_program_with_real_native_cold_review(sqlite_tmp_path: Path, mode: str) -> None:
    binary = os.environ.get("CREATIDY_KILO_NATIVE_TEST_BINARY")
    if not binary:
        pytest.skip("explicit pinned native binary required; no live inference or automatic dependency download")
    result = run_ordinary_flow(sqlite_tmp_path / mode, Path(binary), mode=mode)
    assert result["status"] == "completed"
    assert result["satisfactions"] == 2
    assert result["native_sends"] == 4


@pytest.mark.parametrize("mode", ["pull", "managed"])
def test_ordinary_stop_revokes_authority_without_accepting_or_publishing(sqlite_tmp_path: Path, mode: str) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / mode, None, mode=mode, exercise="stop")
    assert result["status"] == "stopped"
    assert result["native_sends"] == 0
    if mode == "pull":
        assert result["settled"] is False
    else:
        assert result["settled"] == "cancelled"
