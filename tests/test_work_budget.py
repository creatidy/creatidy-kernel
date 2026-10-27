# SPDX-License-Identifier: Apache-2.0
"""Actual single-writer admission, boot recovery and streaming enforcement."""

import inspect
import json
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from test_sqlite_store import OWNER, WORKER, spec

from creatidy_kernel.adapters import task_execution
from creatidy_kernel.adapters.sqlite_codec import program_json
from creatidy_kernel.adapters.sqlite_store import ConcurrentWriter, OperationConflict, SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import VerificationCommand
from creatidy_kernel.core.deadlines import BootClockReading, DeadlineProjection
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt, Program, ProgramSpec
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.work_budget import BudgetRefused, ResourceCeiling, WorkBudgetPolicy, WorkKind

pytest_plugins = ["test_sqlite_store"]

run_command = cast(
    Callable[[Path, VerificationCommand, int, Mapping[str, str], Callable[[], bool], int], dict[str, object]],
    inspect.getattr_static(task_execution, "_run_command"),
)


class Clock:
    def __init__(self) -> None:
        self.reading: BootClockReading | None = BootClockReading(100, 1_000_000_000, "synthetic-boot-a")

    def __call__(self) -> BootClockReading | None:
        return self.reading


def policy(*, active: int = 1, attempts: int = 3) -> WorkBudgetPolicy:
    return WorkBudgetPolicy.basic(attempts, active, verifications=6, retained_bytes=4096)


def initialize(
    store: SQLiteProgramStore, program_id: str = "program-1", *, project: WorkBudgetPolicy | None = None
) -> Program:
    original = spec()
    configured = ProgramSpec(
        program_id,
        original.objective,
        original.work_units,
        original.initial_inputs,
        original.budget,
        original.authority,
        acceptance_criteria=original.acceptance_criteria,
        policy_references=original.policy_references,
    )
    program = store.create(configured, f"create:{program_id}")
    store.bind_work_budget(program_id, "synthetic-project", project or policy(), policy(), 110)
    return store.admit(program_id, f"activate:{program_id}", ActivateProgram(program.revision, OWNER))


def prepare(store: SQLiteProgramStore, program: Program, attempt_id: str) -> Program:
    attempt = AttemptSpec(
        attempt_id,
        program.program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
    )
    return store.admit_with_intent(
        program.program_id,
        f"prepare:{attempt_id}",
        PrepareAttempt(program.revision, WORKER, attempt),
        f"runtime:{attempt_id}",
        f"runtime:{attempt_id}",
        {"attempt": attempt.digest},
    )


def test_project_capacity_atomic_across_programs_and_reopen(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    clock = Clock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        first = initialize(store)
        second = initialize(store, "program-2")
        original = program_json(second)
        prepare(store, first, "push-attempt")
        with pytest.raises(BudgetRefused, match="active work"):
            prepare(store, second, "pull-attempt")
        assert program_json(store.load("program-2")) == original
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation("runtime:pull-attempt")
        assert store.work_budget_status("program-2")["reservations"] == []
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match="active work"):
            prepare(store, store.load("program-2"), "pull-attempt")
        store.record_work_settlement("runtime:push-attempt", "synthetic:exact-settlement")
        prepare(store, store.load("program-2"), "pull-attempt")
        assert len(cast(list[object], store.work_budget_status("program-2")["reservations"])) == 1


def test_concurrent_store_owner_cannot_oversubscribe(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    errors: list[BaseException] = []
    clock = Clock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        first = initialize(store)
        initialize(store, "program-2")

        def contender() -> None:
            try:
                with SQLiteProgramStore(path, busy_timeout_ms=20, boot_clock=clock) as second:
                    prepare(second, second.load("program-2"), "racing-pull")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=contender)
        thread.start()
        prepare(store, first, "winning-push")
        thread.join(timeout=5)
        assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], ConcurrentWriter)
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match="active work"):
            prepare(store, store.load("program-2"), "racing-pull")


def test_reservation_rolls_back_with_failed_program_commit(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = initialize(store)

        def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic disk failure")

        monkeypatch.setattr(store, "_insert_history", fail)
        with pytest.raises(RuntimeError, match="disk failure"):
            prepare(store, program, "a1")
        assert store.load(program.program_id) == program
        assert store.work_budget_status(program.program_id)["reservations"] == []


@pytest.mark.parametrize(
    "reading",
    [
        None,
        BootClockReading(101, 2_000_000_000, "another-boot"),
        BootClockReading(99, 2_000_000_000, "synthetic-boot-a"),
        BootClockReading(101, 12_000_000_000, "synthetic-boot-a"),
    ],
)
def test_recovery_never_renews_or_claims_after_clock_failure(
    sqlite_tmp_path: Path, reading: BootClockReading | None
) -> None:
    clock = Clock()
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        program = prepare(store, initialize(store), "a1")
        before = store.work_budget_status(program.program_id)
    clock.reading = reading
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        assert store.work_budget_status(program.program_id) == before
        with pytest.raises(BudgetRefused, match="clock|expired"):
            store.claim("runtime:a1", now=101, lease_seconds=1)
        assert store.operation("runtime:a1").attempts == 0
        with pytest.raises(BudgetRefused, match="renewed"):
            store.bind_work_budget(program.program_id, "synthetic-project", policy(), policy(), 120)


def test_same_boot_recovery_reuses_original_projection(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store)
        before = store.work_budget_status("program-1")
    clock.reading = BootClockReading(102, 4_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        store.bind_work_budget("program-1", "synthetic-project", policy(), policy(), 110)
        assert store.work_budget_status("program-1") == before
        store.check_work_authority("program-1")


def test_revocation_fences_an_existing_reserved_dispatch(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        prepare(store, initialize(store), "a1")
        store.revoke_work_budget("program-1", "synthetic:stop")
        with pytest.raises(BudgetRefused, match="revoked"):
            store.claim("runtime:a1", now=100, lease_seconds=1)
        assert store.operation("runtime:a1").attempts == 0


def test_wall_rollback_above_original_anchor_still_refuses_after_reopen(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    clock = Clock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store)
        clock.reading = BootClockReading(105, 6_000_000_000, "synthetic-boot-a")
        store.check_work_authority("program-1")
    clock.reading = BootClockReading(103, 7_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match="rollback"):
            store.check_work_authority("program-1")


def test_retained_reservation_survives_crash_and_cannot_be_credited_as_zero(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    allowed = replace(
        policy(), counts=tuple((kind, 2 if kind is WorkKind.TOOL else count) for kind, count in policy().counts)
    )
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        store.bind_work_budget(program.program_id, "project", allowed, allowed, 110)
        store.reserve_work(program.program_id, "a1", "tool:a1", WorkKind.TOOL, retained_bytes=4096)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        assert store.available_retained_bytes("program-1") == 0
        with pytest.raises(BudgetRefused, match="retained streaming"):
            store.reserve_work("program-1", "a1", "tool:a2", WorkKind.TOOL, retained_bytes=1)
        assert store.work_budget_status("program-1")["effective_usage"] == []


def test_task_recovery_boot_change_never_starts_or_reprojects(sqlite_tmp_path: Path) -> None:
    from test_task_execution import ALLOCATION, FixtureConnection, deflake_edit, fixture_task, make_source

    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    clock = Clock()
    clock.reading = BootClockReading(int(time.time()), 1_000_000_000, "synthetic-boot-a")
    connection = FixtureConnection(deflake_edit)
    deadline = int(time.time()) + 20

    def execute(fault: str | None = None) -> dict[str, object]:
        return task_execution.run_task(
            control,
            task=fixture_task(),
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version="1.2.3",
            deadline=deadline,
            allocation=ALLOCATION,
            boot_clock=clock,
            fault=fault,
        )

    with pytest.raises(task_execution.TaskInterrupted):
        execute("prepared")
    original = task_execution.export_task(control)["deadline_projection"]
    clock.reading = BootClockReading(int(time.time()), 2_000_000_000, "different-boot")
    result = execute()
    assert result["condition"] == "unknown" and connection.starts == 0
    assert result["deadline_projection"] == original and "pr" not in result and "acceptance" not in result


def test_durable_command_result_recovers_accounting_without_reexecution(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_task_execution import FixtureConnection, deflake_edit, fixture_task, make_source, run

    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    calls: list[tuple[str, ...]] = []

    def counted(
        workspace: Path,
        command: VerificationCommand,
        repeat: int,
        supplied: Mapping[str, str],
        authorized: Callable[[], bool],
        max_bytes: int,
    ) -> dict[str, object]:
        calls.append(command.argv)
        return run_command(workspace, command, repeat, supplied, authorized, max_bytes)

    def crash_accounting(_store: SQLiteProgramStore, _key: str, _bytes: int) -> None:
        raise task_execution.TaskInterrupted("synthetic crash after durable command result")

    monkeypatch.setattr(task_execution, "_run_command", counted)
    with monkeypatch.context() as failure:
        failure.setattr(SQLiteProgramStore, "record_retained_bytes", crash_accounting)
        with pytest.raises(task_execution.TaskInterrupted):
            run(control, source, connection, task=fixture_task(repeats=1))
    recovered = run(control, source, connection, task=fixture_task(repeats=1))
    expected = [command.argv for command in fixture_task(repeats=1).verification for _ in range(command.repeats)]
    assert recovered["condition"] == "accepted" and calls == expected
    status = cast(dict[str, object], recovered["work_budget"])
    assert len(cast(list[object], status["usage_history"])) == len(expected)


def test_review_retries_tools_and_auxiliary_share_finite_capacity(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store, project=policy(attempts=1)), "a1")
        store.budgeted_intent(
            program.program_id, "a1", WorkKind.REVIEW, "review:a1", "review:a1", {"subject": "candidate-a1"}
        )
        with pytest.raises(BudgetRefused, match="review capacity"):
            store.budgeted_intent(
                program.program_id, "a1", WorkKind.REVIEW, "review:a2", "review:a2", {"subject": "changed"}
            )
        for kind in (WorkKind.TOOL, WorkKind.HELPER, WorkKind.AUXILIARY):
            with pytest.raises(BudgetRefused, match="capacity"):
                store.reserve_work(program.program_id, "a1", f"forbidden:{kind}", kind)
        fence = store.claim("review:a1", now=100, lease_seconds=1)
        with pytest.raises(OperationConflict, match="reconciliation"):
            store.claim("review:a1", now=102, lease_seconds=1)
        store.reconcile(
            "review:a1",
            fence,
            "absent",
            now=102,
            evidence="synthetic:authoritative-absence",
            authoritative_absence=True,
        )
        store.claim("review:a1", now=102, lease_seconds=1)
        assert (
            len(
                [
                    item
                    for item in cast(
                        list[dict[str, object]], store.work_budget_status(program.program_id)["reservations"]
                    )
                    if item["category"] == "retry"
                ]
            )
            == 1
        )


def test_unitful_usage_dedupe_corrections_and_unknowns_survive_reopen(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        store.reserve_work("program-1", "a1", "operation:a1", WorkKind.ATTEMPT)
        first = UsageObservation(
            "observation-1",
            "a1",
            "receipt-1",
            "money",
            "USD",
            Decimal("2"),
            Provenance.PROVIDER_REPORT,
            "source-a",
            100,
        )
        store.record_usage("operation:a1", first)
        store.record_usage("operation:a1", first)
        store.record_usage("operation:a1", replace(first, source="source-b", quantity=Decimal("3")))
        store.record_usage(
            "operation:a1",
            replace(first, observation_id="correction", quantity=Decimal("4"), supersedes_id=first.observation_id),
        )
        store.record_usage(
            "operation:a1",
            replace(first, observation_id="quota", metric="quota", unit="pool:synthetic-requests", quantity=None),
        )
        store.record_usage(
            "operation:a1",
            replace(first, observation_id="tokens", metric="tokens", unit="input_tokens", quantity=Decimal(50)),
        )
        store.record_usage(
            "operation:a1",
            replace(first, observation_id="compute", metric="compute", unit="cpu_seconds", quantity=Decimal(2)),
        )
        store.record_usage(
            "operation:a1",
            replace(first, observation_id="attention", metric="attention", unit="minutes", quantity=Decimal(1)),
        )
        store.record_usage(
            "operation:a1",
            replace(first, observation_id="elapsed", metric="elapsed", unit="seconds", quantity=Decimal(3)),
        )
        store.record_usage(
            "operation:a1", replace(first, observation_id="other-currency", unit="EUR", quantity=Decimal(1))
        )
        with pytest.raises((BudgetRefused, OperationConflict)):
            store.record_usage("operation:a1", replace(first, quantity=Decimal(100)))
        result = store.work_budget_status("program-1")
        effective = cast(list[dict[str, object]], result["effective_usage"])
        assert sorted(
            cast(str, item["quantity"]) for item in effective if item["metric"] == "money" and item["unit"] == "USD"
        ) == ["3", "4"]
        assert next(item for item in effective if item["metric"] == "quota")["quantity"] is None
        assert result["reservation_is_usage"] is False and result["hard_inference_cap"] is False
        assert all(item["quantity"] is None for item in cast(list[dict[str, object]], result["resource_totals"]))
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        assert store.work_budget_status("program-1") == result


def test_strict_profile_refused_and_observed_ceiling_cannot_erase_unknowns(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        strict = replace(policy(), strict_inference=True)
        with pytest.raises(BudgetRefused, match="strict inference"):
            store.bind_work_budget(program.program_id, "project", strict, strict, 110)
        bounded = replace(policy(), resources=(ResourceCeiling("money", "USD", Decimal(10)),))
        store.bind_work_budget(program.program_id, "project", bounded, bounded, 110)
        store.reserve_work(program.program_id, "a1", "op:a1", WorkKind.ATTEMPT)
        with pytest.raises(BudgetRefused, match="unknown"):
            store.reserve_work(program.program_id, "a1", "review:a1", WorkKind.REVIEW)
        usage = UsageObservation(
            "cost", "a1", "receipt", "money", "USD", Decimal(20), Provenance.PROVIDER_REPORT, "source", 101
        )
        store.record_usage("op:a1", usage)
        with pytest.raises(BudgetRefused, match="ceiling exhausted"):
            store.reserve_work(program.program_id, "a1", "review:a1", WorkKind.REVIEW)


def test_legacy_policy_bytes_unchanged_and_missing_proof_is_not_reconstructed(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        before = program_json(program)
        store.bind_work_budget(program.program_id, "project", policy(), policy(), 110)
        assert program_json(store.load(program.program_id)) == before
    with SQLiteProgramStore(sqlite_tmp_path / "legacy.sqlite3", boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        program = store.admit(program.program_id, "activate", ActivateProgram(0, OWNER))
        prepare(store, program, "legacy-a1")
        with pytest.raises(BudgetRefused, match="original work-budget/clock"):
            store.claim("runtime:legacy-a1", now=100, lease_seconds=1)
        with pytest.raises(BudgetRefused, match="legacy Attempt"):
            store.bind_work_budget(program.program_id, "project", policy(), policy(), 110)


def test_streaming_bytes_hang_and_midcommand_revocation_are_bounded(tmp_path: Path) -> None:
    huge = VerificationCommand((sys.executable, "-c", "import os; os.write(1, b'x' * 2000000)"), 5)
    result = run_command(tmp_path, huge, 1, {}, lambda: True, 1024)
    assert result["retained_bytes"] == 1024 and result["output_exhausted"] is True
    hanging = VerificationCommand((sys.executable, "-c", "import time; time.sleep(30)"), 1)
    result = run_command(tmp_path, hanging, 1, {}, lambda: True, 65536)
    assert result["timed_out"] is True and cast(int, result["duration_ms"]) < 6000
    calls = [0]

    def authorized() -> bool:
        calls[0] += 1
        return calls[0] < 3

    result = run_command(tmp_path, hanging, 1, {}, authorized, 65536)
    assert result["authority_lost"] is True and cast(int, result["duration_ms"]) < 2000


def test_suspend_projection_not_monotonic_or_recovery_wall_time() -> None:
    projection = DeadlineProjection.bind(110, BootClockReading(100, 1_000_000_000, "boot"))
    assert projection.decision_time(BootClockReading(101, 12_000_000_000, "boot")) >= 110
    assert (
        DeadlineProjection.decode(
            json.loads(
                json.dumps(
                    {
                        "wall_deadline": 110,
                        "wall_anchor": 100,
                        "boot_anchor_ns": 1_000_000_000,
                        "boot_deadline_ns": 11_000_000_000,
                        "boot_id": "boot",
                        "clock": "linux:CLOCK_BOOTTIME",
                    }
                )
            )
        )
        == projection
    )
