# SPDX-License-Identifier: Apache-2.0
import os
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from ordinary_flow_fixture import run_ordinary_flow
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path

from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
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


@pytest.mark.parametrize("mode", ["pull", "managed"])
def test_private_outcome_export_uses_actual_shared_producers(sqlite_tmp_path: Path, mode: str) -> None:
    result = run_ordinary_flow(
        sqlite_tmp_path / f"private-outcome-{mode}", None, mode=mode, exercise="private-outcome-export"
    )
    assert result["status"] == "completed" and result["satisfactions"] == 2


def test_materialization_cannot_precede_atomic_claim(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize = OrdinaryGitWorkspace.initialize

    def guarded(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec, check_current: Callable[[], None]) -> str:
        program = workspace.store.load(attempt.program_id)
        assert any(item.attempt_id == attempt.attempt_id for item in program.attempts), (
            "workspace mutation began before the Attempt and resource claim were admitted"
        )
        return initialize(workspace, attempt, check_current)

    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", guarded)
    run_ordinary_flow(sqlite_tmp_path / "claimed-before-materialization", None, mode="pull")


def test_work_request_uses_callers_exact_revision_and_replay_identity(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "expected-revision", None, mode="pull", exercise="expected-revision")
    assert result == {
        "status": "checked",
        "native_sends": 0,
    }


def test_work_request_rechecks_caller_revision_after_blocking_check(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "revision-race", None, mode="pull", exercise="expected-revision-race"
    ) == {"status": "checked", "native_sends": 0}


def test_stop_domain_and_budget_revocation_rollback_together(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "stop-failure", None, mode="pull", exercise="stop-atomic-failure") == {
        "status": "refused",
        "native_sends": 0,
    }


@pytest.mark.parametrize("exercise", ["served-owner-stop", "served-owner-stale"])
def test_host_bound_owner_stop_is_scoped_and_revision_checked(sqlite_tmp_path: Path, exercise: str) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise)
    assert result["status"] == ("stopped" if exercise == "served-owner-stop" else "refused")


@pytest.mark.parametrize("exercise", ["served-worker-stop", "served-worker-approve"])
def test_worker_capability_cannot_issue_owner_commands(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise) == {
        "status": "refused",
        "native_sends": 0,
    }


def test_owner_channel_stops_only_its_original_managed_target(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-managed-stop", None, mode="managed", exercise="served-managed-stop"
    ) == {"status": "stopped", "native_sends": 0}


def test_start_rechecks_caller_revision_after_workspace_observation(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "start-revision-race", None, mode="pull", exercise="start-revision-race"
    ) == {"status": "refused", "native_sends": 0}


def test_quiescence_rechecks_caller_revision_at_proof_commit(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "quiesce-revision-race", None, mode="pull", exercise="quiesce-revision-race"
    ) == {"status": "refused", "native_sends": 0}


def test_renewal_rechecks_caller_revision_after_observation(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "renew-revision-race", None, mode="pull", exercise="renew-revision-race"
    ) == {"status": "refused", "native_sends": 0}


def test_report_rechecks_caller_revision_at_candidate_commit(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "report-revision-race", None, mode="pull", exercise="report-revision-race"
    ) == {"status": "refused", "native_sends": 0}


def test_host_bound_local_worker_claim_deduplicates_exact_request(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "served-claim", None, mode="pull", exercise="served-claim") == {
        "status": "checked",
        "native_sends": 0,
    }


@pytest.mark.parametrize("exercise", ["claim-final-observe-race", "claim-final-observe-replay-race"])
def test_stale_revision_during_final_preparation_observation(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise) == {
        "status": "refused",
        "native_sends": 0,
    }


def test_worker_claim_identity_cannot_be_reused_for_start(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-cross-action-id", None, mode="pull", exercise="served-cross-action-id"
    ) == {"status": "refused", "native_sends": 0}


def test_owner_channel_starts_managed_once_then_stops_original_target(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-managed-start", None, mode="managed", exercise="served-managed-start"
    ) == {"status": "stopped", "native_sends": 0}


def test_worker_cannot_start_managed_handoff_before_owner(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "managed-worker-start", None, mode="managed", exercise="served-managed-worker-start"
    ) == {"status": "stopped", "native_sends": 0}


def test_separate_owner_client_stops_only_original_managed_target(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-managed-stop-cli", None, mode="managed", exercise="served-managed-stop-cli"
    ) == {"status": "stopped", "native_sends": 0}


def test_host_bound_start_replays_receipt_without_redispatch(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "served-start", None, mode="pull", exercise="served-start") == {
        "status": "checked",
        "native_sends": 0,
    }


def test_managed_start_rechecks_revision_after_native_input_preparation(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-managed-start-race", None, mode="managed", exercise="served-managed-start-race"
    ) == {"status": "refused", "native_sends": 0}


def test_lost_start_receipt_returns_unknown_without_redispatch(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-start-crash", None, mode="pull", exercise="served-start-crash"
    ) == {"status": "unknown", "native_sends": 0}


def test_host_bound_quiescence_replays_cooperative_receipt_without_physical_claim(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "served-quiesce", None, mode="pull", exercise="served-quiesce") == {
        "status": "checked",
        "native_sends": 0,
    }


def test_host_bound_renewal_replays_original_expiry_without_renewing_twice(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "served-renew", None, mode="pull", exercise="served-renew") == {
        "status": "checked",
        "native_sends": 0,
    }


def test_expired_worker_cannot_start_but_can_read_original_renewal_receipt(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-start-expired", None, mode="pull", exercise="served-start-expired"
    ) == {"status": "refused", "native_sends": 0}
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-renew-expired", None, mode="pull", exercise="served-renew-expired"
    ) == {"status": "checked", "native_sends": 0}


def test_host_bound_submit_and_report_use_trusted_verification(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "served-report", None, mode="pull", exercise="served-report") == {
        "status": "accepted",
        "native_sends": 0,
    }


def test_rejected_report_emits_one_local_decision_alert(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-report-decision", None, mode="pull", exercise="served-report-decision"
    ) == {"status": "rejected", "native_sends": 0}


def test_direct_sdk_review_emits_one_local_decision_alert(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "sdk-review-notify", None, mode="pull", exercise="sdk-review-notify"
    ) == {"status": "rejected", "native_sends": 0}


def test_served_unknown_review_becomes_historical_after_rejection(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-unknown-rejected", None, mode="pull", exercise="served-unknown-rejected"
    ) == {"status": "rejected", "native_sends": 0}


def test_worker_cannot_supply_its_own_review_verdict(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-report-forged", None, mode="pull", exercise="served-report-forged"
    ) == {"status": "refused", "native_sends": 0}


def test_lost_report_receipt_is_unknown_without_new_review(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-report-crash", None, mode="pull", exercise="served-report-crash"
    ) == {"status": "unknown", "native_sends": 0}


def test_report_revision_fences_verification_after_owner_change(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "report-verification-revision-race",
        None,
        mode="pull",
        exercise="report-verification-revision-race",
    ) == {"status": "refused", "native_sends": 0}


def test_report_revision_fences_review_after_authorization_callback(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "report-review-revision-race", None, mode="pull", exercise="report-review-revision-race"
    ) == {"status": "unknown", "native_sends": 0}


def test_owner_reconciliation_rechecks_revision_after_capture(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-reconcile-race", None, mode="pull", exercise="served-reconcile-race"
    ) == {"status": "refused", "native_sends": 0}


def test_separate_local_work_client_uses_host_bound_claim_capability(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-claim-process", None, mode="pull", exercise="served-claim-process"
    ) == {"status": "checked", "native_sends": 0}


def test_separate_client_reads_original_remediation_context(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(
        sqlite_tmp_path / "review-remediation-served", None, mode="pull", exercise="review-remediation-served"
    )
    assert result["status"] == "completed"


def test_separate_client_invocations_share_host_bound_worker_session(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-multi-client", None, mode="pull", exercise="served-multi-client"
    ) == {"status": "checked", "native_sends": 0}


@pytest.mark.parametrize("exercise", ["served-stale", "served-role-injection"])
def test_local_work_capability_refuses_stale_or_forged_identity(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise) == {
        "status": "refused",
        "native_sends": 0,
    }


@pytest.mark.parametrize(
    "exercise",
    [
        "claim-expired",
        "claim-expired-reopen",
        "claim-revoked",
        "claim-stale",
        "claim-generation",
        "claim-revision-stale",
    ],
)
def test_historical_claim_without_authority_cannot_begin_preparation(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise) == {
        "status": "refused",
        "native_sends": 0,
    }


def test_current_claim_can_recover_interrupted_preparation_without_new_grant(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "claim-current", None, mode="pull", exercise="claim-current") == {
        "status": "recovered",
        "native_sends": 0,
    }


def test_revocation_during_preparation_stops_before_next_git_mutation(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "claim-revoked-during-preparation",
        None,
        mode="pull",
        exercise="claim-revoked-during-preparation",
    ) == {"status": "refused", "native_sends": 0}


@pytest.mark.parametrize("exercise", ["injected-file", "injected-symlink", "injected-hardlink"])
def test_preparation_preserves_late_manual_entry_without_following_links(sqlite_tmp_path: Path, exercise: str) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise) == {
        "status": "refused",
        "native_sends": 0,
    }


@pytest.mark.parametrize("kind", ["file", "symlink", "hardlink", "directory"])
def test_materialization_exclusive_create_blocks_last_moment_insertion(sqlite_tmp_path: Path, kind: str) -> None:
    parent = sqlite_tmp_path / "owned"
    parent.mkdir(mode=0o700)
    workspace = LocalOwnedWorkspace(parent, "leaf", create=True)
    external = sqlite_tmp_path / "external"
    external.write_bytes(b"original manual data\n")
    external.chmod(0o640)
    calls = 0

    def check_current() -> None:
        nonlocal calls
        calls += 1
        if calls != 2:
            return
        target = workspace.path / ("directory" if kind == "directory" else "file")
        if kind == "file":
            target.write_bytes(b"original manual data\n")
            target.chmod(0o640)
        elif kind == "symlink":
            target.symlink_to(external)
        elif kind == "hardlink":
            os.link(external, target)
        else:
            target.mkdir()
            (target / "manual").write_bytes(b"original manual data\n")

    relative = "directory/file" if kind == "directory" else "file"
    with pytest.raises(OwnershipConflict):
        workspace.materialize(((relative, b"controller data\n", 0o755),), check_current)
    preserved = workspace.path / "file" if kind == "file" else external
    assert preserved.read_bytes() == b"original manual data\n"
    assert preserved.stat().st_mode & 0o777 == 0o640
    if kind == "directory":
        assert (workspace.path / "directory/manual").read_bytes() == b"original manual data\n"


def test_materialization_refuses_renamed_open_file_without_following_replacement(sqlite_tmp_path: Path) -> None:
    parent = sqlite_tmp_path / "owned"
    parent.mkdir(mode=0o700)
    workspace = LocalOwnedWorkspace(parent, "leaf", create=True)
    external = sqlite_tmp_path / "external"
    external.write_bytes(b"outside data\n")
    external.chmod(0o640)
    calls = 0

    def check_current() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            (workspace.path / "file").rename(workspace.path / "controller-file")
            (workspace.path / "file").symlink_to(external)

    with pytest.raises(OwnershipConflict):
        workspace.materialize((("file", b"controller data\n", 0o755),), check_current)
    assert external.read_bytes() == b"outside data\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_materialization_preserves_partial_file_when_authority_is_revoked(sqlite_tmp_path: Path) -> None:
    parent = sqlite_tmp_path / "owned"
    parent.mkdir(mode=0o700)
    workspace = LocalOwnedWorkspace(parent, "leaf", create=True)
    calls = 0

    def check_current() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OwnershipConflict("synthetic authority revocation")

    with pytest.raises(OwnershipConflict, match="revocation"):
        workspace.materialize((("file", b"controller data\n", 0o755),), check_current)
    assert (workspace.path / "file").read_bytes() == b""


def test_materialization_refuses_replaced_directory_without_following_link(sqlite_tmp_path: Path) -> None:
    parent = sqlite_tmp_path / "owned"
    parent.mkdir(mode=0o700)
    workspace = LocalOwnedWorkspace(parent, "leaf", create=True)
    external = sqlite_tmp_path / "outside"
    external.mkdir()
    (external / "b").write_bytes(b"outside bytes\n")
    calls = 0

    def check_current() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            (workspace.path / "directory").rename(workspace.path / "retained-directory")
            (workspace.path / "directory").symlink_to(external, target_is_directory=True)

    with pytest.raises(OwnershipConflict):
        workspace.materialize((("directory/a", b"first\n", 0o644), ("directory/b", b"second\n", 0o644)), check_current)
    assert (external / "b").read_bytes() == b"outside bytes\n"
    assert (workspace.path / "retained-directory/a").read_bytes() == b"first\n"


def test_changed_preparation_is_preserved_and_not_auto_attributed(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize = OrdinaryGitWorkspace.initialize
    touched: list[Path] = []

    def edit_after_preparation(
        workspace: OrdinaryGitWorkspace, attempt: AttemptSpec, check_current: Callable[[], None]
    ) -> str:
        subject = initialize(workspace, attempt, check_current)
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


def test_owner_approval_checks_caller_expectation_for_absent_program(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "approve-expected", None, mode="pull", exercise="approve-expected") == {
        "status": "approved",
        "native_sends": 0,
    }


def test_owner_approval_rechecks_absence_after_external_validation(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "approve-revision-race", None, mode="pull", exercise="approve-revision-race"
    ) == {"status": "refused", "native_sends": 0}


@pytest.mark.parametrize("exercise", ["served-owner-approve", "served-owner-approve-stale"])
def test_host_bound_approval_uses_exact_proposal_and_expected_revision(sqlite_tmp_path: Path, exercise: str) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise)
    assert result["status"] == ("approved" if exercise == "served-owner-approve" else "refused")


def test_interleaved_owner_channels_keep_amended_approval(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "owner-interleaved-amend", None, mode="pull", exercise="served-owner-interleaved-amend"
    ) == {"status": "stopped", "native_sends": 0}


def test_host_bound_amendment_preserves_original_deadline_and_replays(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-owner-amend", None, mode="pull", exercise="served-owner-amend"
    ) == {"status": "amended", "native_sends": 0}


def test_lost_agit_reply_ignores_unrelated_branch_pr_without_duplicate_write(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "branch-pr", None, mode="pull", exercise="branch-pr")
    assert result["status"] == "completed"
    assert result["native_sends"] == 4


def test_copied_marker_on_unrelated_branch_is_unknown_without_retry(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(sqlite_tmp_path / "ambiguous-pr", None, mode="pull", exercise="ambiguous-pr") == {
        "status": "unknown",
        "native_sends": 1,
    }


def test_separate_owner_clients_share_host_bound_approval(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "served-owner-multi-client", None, mode="pull", exercise="served-owner-multi-client"
    ) == {"status": "stopped", "native_sends": 0}


@pytest.mark.parametrize("exercise", ["review-lost", "review-lost-reopen"])
def test_completed_review_receipt_recovers_without_second_prompt(sqlite_tmp_path: Path, exercise: str) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / exercise, None, mode="pull", exercise=exercise)
    assert result["status"] == "completed"
    assert result["review_calls"] == 2


def test_managed_unknown_review_is_visible_and_recovers_without_reprompt(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "managed-review-lost", None, mode="managed", exercise="review-lost")
    assert result["status"] == "completed" and result["review_calls"] == 2


def test_diamond_dag_composes_shared_ancestor_once(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "diamond", None, mode="pull", exercise="diamond")
    assert result["status"] == "completed"
    assert result["satisfactions"] == 4
    assert result["native_sends"] == 8


def test_diamond_branch_advances_shared_ancestor_while_other_inherits(sqlite_tmp_path: Path) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / "diamond-inherited", None, mode="pull", exercise="diamond-inherited")
    assert result["status"] == "completed"
    assert result["satisfactions"] == 4


@pytest.mark.parametrize("mode", ["pull", "managed"])
def test_substantive_review_findings_reach_remediation_author(sqlite_tmp_path: Path, mode: str) -> None:
    result = run_ordinary_flow(sqlite_tmp_path / mode, None, mode=mode, exercise="review-remediation")
    assert result["status"] == "completed" and result["satisfactions"] == 2


def test_remediation_refuses_review_receipt_with_different_subject(sqlite_tmp_path: Path) -> None:
    assert run_ordinary_flow(
        sqlite_tmp_path / "tampered-review", None, mode="pull", exercise="review-remediation-tampered"
    ) == {"status": "refused", "native_sends": 0}


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

    def interrupted(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec, _check_current: Callable[[], None]) -> str:
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
