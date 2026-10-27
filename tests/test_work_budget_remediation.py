# SPDX-License-Identifier: Apache-2.0
"""Review counterexamples: current authority, source receipts and command ownership."""

import selectors
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from test_sqlite_store import OWNER, WORKER
from test_work_budget import Clock, initialize, policy, prepare

from creatidy_kernel.adapters import task_execution
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import (
    AmendProgramSpec,
    CancelAttempt,
    CancelWorkUnit,
    FinishAttempt,
    PauseProgram,
    SpecAmendment,
    StartAttempt,
)
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.work_budget import BudgetRefused, ResourceCeiling, WorkKind

pytest_plugins = ["test_sqlite_store"]


def test_historical_review_receipt_replays_after_amendment_without_new_dispatch(sqlite_tmp_path: Path) -> None:
    from test_product_review import Dispatcher, request
    from test_product_review import dispatch_product_review as prepare_review

    from creatidy_kernel.ports.verification import dispatch_product_review

    path = sqlite_tmp_path / "review.sqlite3"
    with SQLiteProgramStore(path) as store:
        original = request()
        dispatcher = Dispatcher()
        first = prepare_review(store, original, dispatcher, now=10)
        program = store.load("reference")
        store.admit(
            program.program_id,
            "amend-after-review",
            AmendProgramSpec(
                program.revision,
                "owner",
                SpecAmendment(program.spec.revision, objective="new meaning after original review"),
            ),
        )
        replay = dispatch_product_review(store, original, dispatcher, now=20)
        assert replay.receipt == first.receipt and dispatcher.calls == 1


@pytest.mark.parametrize("change", ["attempt", "unit", "pause", "specification"])
def test_reserved_runtime_claim_rechecks_current_exact_authority(sqlite_tmp_path: Path, change: str) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        if change == "attempt":
            command = CancelAttempt(program.revision, OWNER, "a1")
        elif change == "unit":
            command = CancelWorkUnit(program.revision, OWNER, "first", "synthetic owner stop")
        elif change == "pause":
            command = PauseProgram(program.revision, OWNER)
        else:
            command = AmendProgramSpec(
                program.revision, OWNER, SpecAmendment(program.spec.revision, objective="amended synthetic task")
            )
        store.admit(program.program_id, "change", command)
        if change == "specification":
            store.bind_work_budget(program.program_id, "synthetic-project", policy(), policy(), 110)
        before = store.work_budget_status(program.program_id)["reservations"]
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        with pytest.raises(BudgetRefused, match="state|superseded"):
            store.claim("runtime:a1", now=100, lease_seconds=1)
        assert store.operation("runtime:a1").attempts == 0
        assert store.work_budget_status("program-1")["reservations"] == before


@pytest.mark.parametrize("change", ["attempt", "specification"])
def test_application_rechecks_after_claim_before_runtime_start(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from test_allocation_recovery import WORKSPACE, CountingAllocator, NoCollection, RecordingRuntime, initial

    from creatidy_kernel.adapters.reference import POLICY
    from creatidy_kernel.ports.application import advance_work_unit

    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = initial(store)
        runtime = RecordingRuntime()
        original = store.claim

        def changed(operation_id: str, *, now: int, lease_seconds: int) -> int:
            fence = original(operation_id, now=now, lease_seconds=lease_seconds)
            current = store.load(program.program_id)
            if change == "attempt":
                store.admit(
                    current.program_id,
                    "cancel-after-claim",
                    CancelAttempt(current.revision, "owner", "reference:first"),
                )
            else:
                store.admit(
                    current.program_id,
                    "amend-after-claim",
                    AmendProgramSpec(
                        current.revision,
                        "owner",
                        SpecAmendment(current.spec.revision, objective="new synthetic meaning"),
                    ),
                )
            return fence

        monkeypatch.setattr(store, "claim", changed)
        with pytest.raises(BudgetRefused):
            advance_work_unit(
                store,
                program,
                "first",
                allocator=CountingAllocator(),
                runtime=runtime,
                workspace=WORKSPACE,
                collector=NoCollection(),
                policy=POLICY,
                now=100,
            )
        assert runtime.requests == []


@pytest.mark.parametrize("different_attempt", [False, True])
def test_source_receipt_identity_cannot_move_between_operations(sqlite_tmp_path: Path, different_attempt: bool) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        store.reserve_work("program-1", "a1", "op-a", WorkKind.ATTEMPT)
        store.record_work_settlement("op-a", "synthetic:original-operation-complete")
        store.reserve_work("program-1", "a2" if different_attempt else "a1", "op-b", WorkKind.REMEDIATION)
        original = UsageObservation(
            "o1", "a1", "receipt-r", "money", "USD", Decimal(2), Provenance.PROVIDER_REPORT, "source-s", 100
        )
        store.record_usage("op-a", original)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        store.record_usage("op-a", original)
        for observation in (
            replace(original, observation_id="o2", attempt_id="a2" if different_attempt else "a1"),
            replace(
                original,
                observation_id="correction-other-op",
                attempt_id="a2" if different_attempt else "a1",
                quantity=Decimal(9),
                supersedes_id="o1",
            ),
        ):
            with pytest.raises(BudgetRefused, match="original.*attribution"):
                store.record_usage("op-b", observation)
        store.record_usage("op-a", replace(original, observation_id="o3", quantity=Decimal(3), supersedes_id="o1"))
        status = store.work_budget_status("program-1")
        assert len(cast(list[object], status["usage_history"])) == 2
        facts = cast(list[dict[str, object]], status["effective_usage"])
        assert len(facts) == 1 and facts[0]["quantity"] == "3" and facts[0]["operation"] == "op-a"


@pytest.mark.parametrize("scope", ["program", "project"])
@pytest.mark.parametrize("correction", [Decimal(20), None])
def test_existing_reservation_cannot_bypass_corrected_ceiling(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, correction: Decimal | None
) -> None:
    base = policy()
    limited = replace(base, resources=(ResourceCeiling("money", "USD", Decimal(10)),))
    task_policy, project_policy = (limited, base) if scope == "program" else (base, limited)
    path = sqlite_tmp_path / "limited.sqlite3"
    from test_sqlite_store import spec

    from creatidy_kernel.core.domain import ActivateProgram

    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        store.bind_work_budget(program.program_id, "limited-project", project_policy, task_policy, 110)
        program = store.admit(program.program_id, "activate", ActivateProgram(0, OWNER))
        program = prepare(store, program, "a1")
        fact = UsageObservation(
            "o1", "a1", "r1", "money", "USD", Decimal(1), Provenance.PROVIDER_REPORT, "provider", 100
        )
        store.record_usage("runtime:a1", fact)
        program = store.admit(program.program_id, "start", StartAttempt(program.revision, WORKER, "a1"))
        store.admit(program.program_id, "finish", FinishAttempt(program.revision, WORKER, "a1"))
        request: dict[str, object] = {"subject": "synthetic candidate"}

        # Preserve the earlier-contract counterexample with an already-reserved B.
        # New production admissions now refuse this partial-coverage setup, tested
        # separately; an imported historical reservation must still fail at claim.
        def prior_headroom(
            _store: SQLiteProgramStore, _program_id: str, *, exclude_operation: str | None = None
        ) -> None:
            return None

        with monkeypatch.context() as legacy:
            legacy.setattr(SQLiteProgramStore, "_check_resource_headroom", prior_headroom)
            store.budgeted_intent(program.program_id, "a1", WorkKind.REVIEW, "review-b", "review-b", request)
        before = store.work_budget_status(program.program_id)["reservations"]
        store.record_usage("runtime:a1", replace(fact, observation_id="o2", quantity=correction, supersedes_id="o1"))
        # Historical intent replay remains possible but is not a new dispatch permit.
        store.budgeted_intent(program.program_id, "a1", WorkKind.REVIEW, "review-b", "review-b", request)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        with pytest.raises(BudgetRefused, match=f"{scope} .*exhausted|{scope} .*unknown"):
            store.claim("review-b", now=100, lease_seconds=1)
        assert store.operation("review-b").attempts == 0
        assert store.work_budget_status(program.program_id)["reservations"] == before
        assert cast(list[dict[str, object]], store.work_budget_status(program.program_id)["effective_usage"])[0][
            "quantity"
        ] == (None if correction is None else "20")


@pytest.mark.parametrize("have_result", [False, True])
def test_command_unknown_protects_workspace_and_requires_original_live_owner(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, have_result: bool
) -> None:
    from test_task_execution import FixtureConnection, deflake_edit, fixture_task, make_source, run

    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    owners: dict[str, task_execution.OwnedVerificationCommand] = {}

    def abandoned(
        workspace: Path,
        command: task_execution.VerificationCommand,
        repeat: int,
        supplied: Mapping[str, str],
        authorized: Callable[[], bool],
        max_bytes: int,
        *,
        owner: task_execution.OwnedVerificationCommand | None = None,
    ) -> dict[str, object]:
        assert owner is not None
        process = subprocess.Popen(
            (
                sys.executable,
                "-c",
                "import pathlib,time; pathlib.Path('protected-workspace').write_text('must remain'); "
                "print('ready', flush=True); time.sleep(60)",
            ),
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        owner.attach(process)
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(5)
            assert process.stdout.readline() == b"ready\n"
        if not have_result:
            raise task_execution.TaskInterrupted("synthetic controller loss with an owned command still running")
        return {
            "argv": list(command.argv),
            "repeat": repeat,
            "exit_code": None,
            "timed_out": True,
            "duration_ms": 1,
            "observed_at": 100,
            "stdout_tail": "",
            "stderr_tail": "",
            "retained_bytes": 0,
            "output_exhausted": False,
            "authority_lost": False,
            "owned_group_settled": False,
        }

    monkeypatch.setattr(task_execution, "_run_command", abandoned)
    try:
        if have_result:
            first = run(control, source, connection, task=fixture_task(repeats=1), command_owners=owners)
            assert first["condition"] == "cancel_uncertain"
        else:
            with pytest.raises(task_execution.TaskInterrupted):
                run(control, source, connection, task=fixture_task(repeats=1), command_owners=owners)
        marker = control / "workspace/protected-workspace"
        assert marker.read_text() == "must remain"
        key, owner = next(iter(owners.items()))
        assert owner.process is not None and owner.process.poll() is None

        def no_restore(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("unknown command must block candidate restoration")

        with monkeypatch.context() as recovery:
            recovery.setattr(task_execution, "_install_candidate", no_restore)
            recovered = run(control, source, connection, task=fixture_task(repeats=1))
            assert recovered["condition"] == "cancel_uncertain" and recovered["unresolved_commands"] == [key]
            with SQLiteProgramStore(control / "kernel.sqlite3") as store:
                wrong = task_execution.OwnedVerificationCommand(store.operation(key))
            forged = run(control, source, connection, task=fixture_task(repeats=1), command_owners={key: wrong})
            assert forged["condition"] == "cancel_uncertain" and wrong.process is None

            def failed_retirement(_owner: task_execution.OwnedVerificationCommand) -> bool:
                return False

            recovery.setattr(task_execution.OwnedVerificationCommand, "retire", failed_retirement)
            failed = run(control, source, connection, task=fixture_task(repeats=1), command_owners=owners)
            assert failed["condition"] == "cancel_uncertain"
        assert marker.read_text() == "must remain" and owner.process.poll() is None
        status = task_execution.task_status(control)
        assert status["condition"] == "cancel_uncertain" and status["unresolved_commands"] == [key]
        with SQLiteProgramStore(control / "kernel.sqlite3") as store:
            assert store.find_artifact("rejection:task_execution:attempt", "rejected-result") is None
            assert store.find_artifact(task_execution.OPERATION_ID, f"command-owner:{key}") is not None
        # Retire only through the retained original Popen owner. A missing result
        # can enter cancellation, never ordinary rejection or a fabricated pass.
        reconciled = run(control, source, connection, task=fixture_task(repeats=1), command_owners=owners)
        assert owner.process.poll() is not None
        assert reconciled["condition"] in ({"rejected"} if have_result else {"cancel_uncertain", "cancelled"})
        if not have_result:
            assert marker.read_text() == "must remain" and "acceptance" not in reconciled
    finally:
        for owner in owners.values():
            owner.retire()
            if owner.process is not None:
                if owner.process.stdout is not None:
                    owner.process.stdout.close()
                if owner.process.stderr is not None:
                    owner.process.stderr.close()
