# SPDX-License-Identifier: Apache-2.0
"""Composed command-integrity crashes and conservative resource headroom."""

import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from test_allocation_recovery import WORKSPACE, CountingAllocator
from test_product_review import CandidateCollector, Dispatcher, ReviewRuntime
from test_sqlite_store import OWNER, WORKER, spec
from test_task_execution import FixtureConnection, deflake_edit, fixture_task, fixture_verification, make_source, run
from test_work_budget import Clock, policy, prepare

from creatidy_kernel.adapters import reference, task_execution
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import TaskInterrupted, VerificationCommand
from creatidy_kernel.core.domain import ActivateProgram, FinishAttempt, StartAttempt
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.verification import VerificationPolicy
from creatidy_kernel.core.work_budget import BudgetRefused, ResourceCeiling, WorkKind
from creatidy_kernel.ports.application import advance_work_unit

pytest_plugins = ["test_sqlite_store"]


@pytest.mark.parametrize("effect", ["tracked", "untracked"])
def test_dirty_zero_exit_command_cannot_be_cleaned_into_a_pass_after_verification_crash(
    sqlite_tmp_path: Path, effect: str
) -> None:
    source = make_source(sqlite_tmp_path)
    script = (
        "p=pathlib.Path('tests/test_e2e_execution.py'); p.write_text(p.read_text()+'# touched\\n')"
        if effect == "tracked"
        else "pathlib.Path('leftover-artifact.txt').write_text('verification residue')"
    )
    command = fixture_verification(source, script)
    task = replace(fixture_task(repeats=1), verification=(VerificationCommand(command, 30),))
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    with pytest.raises(TaskInterrupted):
        run(control, source, connection, task=task, fault="verification")
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        results = [
            item
            for item in cast(
                list[dict[str, object]], store.work_budget_status(task_execution.PROGRAM_ID)["reservations"]
            )
            if item["category"] == "tool"
        ]
        key = cast(str, results[0]["operation"])
        receipt = json.loads(store.artifact(task_execution.OPERATION_ID, key))
        integrity = json.loads(store.artifact(task_execution.OPERATION_ID, f"command-integrity:{key}"))
        assert receipt["exit_code"] == 0 and integrity["clean"] is False and integrity["complete"] is True
    # Fresh controller, existing durable result, no re-execution of the command.
    recovered = run(control, source, FixtureConnection(deflake_edit), task=task)
    assert recovered["condition"] == "rejected" and "acceptance" not in recovered and "pr" not in recovered
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        assert store.operation(key).attempts == 1
        assert len(cast(list[object], store.work_budget_status(task_execution.PROGRAM_ID)["usage_history"])) == 1


def test_success_result_without_integrity_stays_unknown_before_restore(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    command = fixture_verification(source, "pathlib.Path('protected-unchecked-effect').write_text('retain')")
    task = replace(fixture_task(repeats=1), verification=(VerificationCommand(command, 30),))
    control = sqlite_tmp_path / "control"
    original = SQLiteProgramStore.finalize_artifact

    def missing_integrity(store: SQLiteProgramStore, operation_id: str, name: str, data: bytes) -> str:
        if name.startswith("command-integrity:"):
            raise TaskInterrupted("synthetic loss before original integrity result")
        return original(store, operation_id, name, data)

    with monkeypatch.context() as crash:
        crash.setattr(SQLiteProgramStore, "finalize_artifact", missing_integrity)
        with pytest.raises(TaskInterrupted):
            run(control, source, FixtureConnection(deflake_edit), task=task)
    marker = control / "workspace/protected-unchecked-effect"
    assert marker.read_text() == "retain"

    def forbidden_restore(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unchecked command effects must not be erased")

    monkeypatch.setattr(task_execution, "_install_candidate", forbidden_restore)
    recovered = run(control, source, FixtureConnection(deflake_edit), task=task)
    assert recovered["condition"] in {"cancel_uncertain", "cancelled"} and "acceptance" not in recovered
    assert marker.read_text() == "retain"


def test_bounded_incomplete_integrity_is_never_a_cached_pass(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    task = fixture_task(repeats=1)

    def exhausted(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OverflowError("synthetic integrity output exceeds bounded capture")

    monkeypatch.setattr(task_execution, "run_git_bounded", exhausted)
    with pytest.raises(TaskInterrupted):
        run(control, source, FixtureConnection(deflake_edit), task=task, fault="verification")
    recovered = run(control, source, FixtureConnection(deflake_edit), task=task)
    assert recovered["condition"] == "rejected" and "acceptance" not in recovered
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        records = cast(list[dict[str, object]], store.work_budget_status(task_execution.PROGRAM_ID)["reservations"])
        for record in records:
            if record["category"] == "tool":
                data = store.artifact(task_execution.OPERATION_ID, f"command-integrity:{record['operation']}")
                assert len(data) <= task_execution.MAX_CAPTURE_BYTES
                integrity = json.loads(data)
                assert integrity["complete"] is False and integrity["clean"] is False


@pytest.mark.parametrize("scope", ["program", "project"])
@pytest.mark.parametrize("provenance", [Provenance.ESTIMATE, Provenance.PROVIDER_REPORT])
def test_zero_or_partial_receipt_never_releases_unknown_headroom(
    sqlite_tmp_path: Path, scope: str, provenance: Provenance
) -> None:
    path = sqlite_tmp_path / "resource.sqlite3"
    basic = policy()
    limited = replace(basic, resources=(ResourceCeiling("money", "USD", Decimal(10)),))
    project, task = (basic, limited) if scope == "program" else (limited, basic)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        store.create(spec(), "create")
        store.bind_work_budget("program-1", "project", project, task, 110)
        store.reserve_work("program-1", "a1", "running-a", WorkKind.ATTEMPT)
        first = UsageObservation(
            "o1", "a1", "partial-r", "money", "USD", Decimal(0), provenance, "synthetic-provider", 100
        )
        store.record_usage("running-a", first)
        with pytest.raises(BudgetRefused, match=f"{scope} resource headroom unknown"):
            store.reserve_work("program-1", "a1", "b", WorkKind.REVIEW)
        # Physical completion is not a producer guarantee of complete billing.
        store.record_work_settlement("running-a", "synthetic:exact-physical-completion")
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        store.record_usage("running-a", replace(first, observation_id="o2", quantity=None, supersedes_id="o1"))
        with pytest.raises(BudgetRefused, match="unknown"):
            store.reserve_work("program-1", "a1", "b", WorkKind.REVIEW)
        store.record_usage("running-a", replace(first, observation_id="o3", quantity=Decimal(2), supersedes_id="o2"))
        with pytest.raises(BudgetRefused, match="unknown"):
            store.reserve_work("program-1", "a1", "b", WorkKind.REVIEW)
        final = store.work_budget_status("program-1")
        assert len(cast(list[object], final["reservations"])) == 1 and final["hard_inference_cap"] is False
        assert len(cast(list[object], final["usage_history"])) == 3


def test_uncapped_basic_profile_keeps_unknown_usage_without_blocking_all_work(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "basic.sqlite3", boot_clock=Clock()) as store:
        store.create(spec(), "create")
        store.bind_work_budget("program-1", "project", policy(), policy(), 110)
        store.reserve_work("program-1", "a1", "a", WorkKind.ATTEMPT)
        store.record_usage(
            "a",
            UsageObservation(
                "o1", "a1", "r", "money", "USD", None, Provenance.PROVIDER_REPORT, "synthetic-client", 100
            ),
        )
        store.reserve_work("program-1", "a1", "b", WorkKind.REVIEW)
        assert store.work_budget_status("program-1")["hard_inference_cap"] is False


@pytest.mark.parametrize("entrypoint", ["reserve_work", "budgeted_intent"])
@pytest.mark.parametrize(
    ("category", "quantity"),
    [(WorkKind.TOOL, -4096), (WorkKind.TOOL, True), (WorkKind.TOOL, 1.5), (WorkKind.TOOL, "4096"), ("tool", 0)],
)
def test_every_reservation_entry_validates_before_replay_and_preserves_capacity(
    sqlite_tmp_path: Path, entrypoint: str, category: object, quantity: object
) -> None:
    path = sqlite_tmp_path / "validated-reservations.sqlite3"
    base = policy()
    configured = replace(
        base, counts=tuple((kind, 2 if kind is WorkKind.TOOL else count) for kind, count in base.counts)
    )
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        store.bind_work_budget(program.program_id, "project", configured, configured, 110)
        program = store.admit(program.program_id, "activate", ActivateProgram(0, OWNER))
        program = prepare(store, program, "a1")
        program = store.admit(program.program_id, "start", StartAttempt(program.revision, WORKER, "a1"))
        store.admit(program.program_id, "finish", FinishAttempt(program.revision, WORKER, "a1"))
        before = store.work_budget_status(program.program_id)
        operations = store.operations()
        if entrypoint == "reserve_work":
            with pytest.raises(ValueError, match="invalid work reservation"):
                store.reserve_work(
                    program.program_id, "a1", "invalid", cast(WorkKind, category), retained_bytes=cast(int, quantity)
                )
        else:
            with pytest.raises(ValueError, match="invalid work reservation"):
                store.budgeted_intent(
                    program.program_id,
                    "a1",
                    cast(WorkKind, category),
                    "invalid",
                    "invalid",
                    {},
                    retained_bytes=cast(int, quantity),
                )
        assert store.work_budget_status(program.program_id) == before and store.operations() == operations
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        assert store.work_budget_status("program-1") == before and store.available_retained_bytes("program-1") == 4096
        store.budgeted_intent("program-1", "a1", WorkKind.TOOL, "valid", "valid", {}, retained_bytes=4096)
        retained = store.work_budget_status("program-1")
        with pytest.raises(ValueError, match="invalid work reservation"):
            store.budgeted_intent(
                "program-1", "a1", cast(WorkKind, category), "valid", "valid", {}, retained_bytes=cast(int, quantity)
            )
        with pytest.raises(BudgetRefused, match="retained streaming byte capacity"):
            store.budgeted_intent("program-1", "a1", WorkKind.TOOL, "excess", "excess", {}, retained_bytes=8192)
        assert store.work_budget_status("program-1") == retained and store.available_retained_bytes("program-1") == 0
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        assert store.work_budget_status("program-1") == retained and store.available_retained_bytes("program-1") == 0


@pytest.mark.parametrize("supply_dispatcher", [False, True])
def test_explicit_review_check_cannot_bypass_zero_review_capacity(
    sqlite_tmp_path: Path, supply_dispatcher: bool
) -> None:
    runtime = ReviewRuntime()
    allocator = CountingAllocator()
    reviewer = Dispatcher()
    with SQLiteProgramStore(sqlite_tmp_path / "review-alias.sqlite3", boot_clock=Clock()) as store:
        program = store.create(reference.reference_spec(), "create")
        basic = policy()
        configured = replace(
            basic, counts=tuple((kind, 0 if kind is WorkKind.REVIEW else count) for kind, count in basic.counts)
        )
        store.bind_work_budget(program.program_id, "project", configured, configured, 110)
        program = store.admit(program.program_id, "activate", ActivateProgram(0, "owner"))
        original = store.work_budget_status(program.program_id)
        operations = store.operations()
        with pytest.raises(BudgetRefused, match="explicit independent_review requires"):
            advance_work_unit(
                store,
                program,
                "first",
                allocator=allocator,
                runtime=runtime,
                workspace=WORKSPACE,
                collector=CandidateCollector(),
                policy=VerificationPolicy(reference.POLICY.reference, ("exact-content", "independent_review"), False),
                now=100,
                product_reviewer=reviewer if supply_dispatcher else None,
            )
        assert runtime.requests == {} and allocator.calls == 0 and reviewer.calls == 0
        assert store.work_budget_status(program.program_id) == original and store.operations() == operations
