# SPDX-License-Identifier: Apache-2.0
"""Application clock callbacks share durable clock proof; fresh proof failures leave no store."""

import inspect
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import cast

import pytest
from test_sqlite_store import spec
from test_task_execution import ALLOCATION, FixtureConnection, deflake_edit, fixture_task, make_source
from test_work_budget import initialize, policy, prepare

from creatidy_kernel.adapters.boot_clock import read_boot_clock
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import OPERATION_ID, PROGRAM_ID, run_task
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ProgramSpec
from creatidy_kernel.core.execution import UnsupportedExecution
from creatidy_kernel.core.work_budget import BudgetRefused

pytest_plugins = ["test_sqlite_store"]


class ClockCut(BaseException):
    pass


class Clock:
    def __init__(self) -> None:
        self.reading: BootClockReading | None = BootClockReading(100, 1_000_000_000, "synthetic-task-clock")
        self.calls = 0
        self.armed = False
        self.high_seen = False
        self.cut = False

    def __call__(self) -> BootClockReading | None:
        self.calls += 1
        if self.armed:
            self.armed = False
            self.high_seen = True
            self.reading = BootClockReading(103, 3_000_000_000, "synthetic-task-clock")
            if self.cut:
                raise ClockCut("synthetic interruption after acquiring higher clock")
            return BootClockReading(105, 2_000_000_000, "synthetic-task-clock")
        return self.reading


class ArmingConnection(FixtureConnection):
    def __init__(self, clock: Clock) -> None:
        super().__init__(deflake_edit)
        self.clock = clock
        self.turns = 0

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        result = super().request(method, params)
        if method == "turn/start":
            self.turns += 1
            self.clock.armed = True
        return result


def execute(
    control: Path,
    source: Path,
    connection: FixtureConnection,
    clock: Clock,
    *,
    fault: str | None = None,
    deadline: int | None = 110,
) -> dict[str, object]:
    return run_task(
        control,
        task=fixture_task(repeats=1),
        source_repository=source,
        owner_approved=True,
        trusted_development_acknowledged=True,
        connection=connection,
        version=connection.version,
        allocation=ALLOCATION,
        deadline=deadline,
        boot_clock=clock,
        fault=fault,
    )


def clock_row(store: SQLiteProgramStore, program_id: str = PROGRAM_ID) -> sqlite3.Row:
    connection = cast(sqlite3.Connection, inspect.getattr_static(store, "_connection"))
    row = connection.execute(
        "SELECT wall, boot_ns, pending FROM work_clock_state WHERE program_id = ?", (program_id,)
    ).fetchone()
    assert row is not None
    return cast(sqlite3.Row, row)


def test_run_task_higher_decision_read_then_rollback_never_accepts_or_redispatches(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.task_execution.time.time", lambda: 100)
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    clock = Clock()
    connection = ArmingConnection(clock)
    result = execute(control, source, connection, clock)
    assert clock.high_seen and connection.starts == 1 and connection.turns == 1
    assert result["condition"] in {"unknown", "cancel_uncertain", "cancelled"}
    assert "acceptance" not in result and "pr" not in result
    with SQLiteProgramStore(control / "kernel.sqlite3", boot_clock=clock) as store:
        assert clock_row(store)["wall"] == 105 and clock_row(store)["pending"] == 0
        assert store.operation(OPERATION_ID).attempts == 1 and not store.operations("acceptance:")
        projection = store.work_budget_status(PROGRAM_ID)["binding"]
    for _ in range(2):
        stopped = execute(control, source, connection, clock)
        assert stopped["condition"] in {"unknown", "cancel_uncertain", "cancelled"}
        assert connection.starts == 1 and connection.turns == 1 and "acceptance" not in stopped and "pr" not in stopped
    with SQLiteProgramStore(control / "kernel.sqlite3", boot_clock=clock) as store:
        assert store.work_budget_status(PROGRAM_ID)["binding"] == projection


@pytest.mark.parametrize("cut", ["acquisition", "durable-return"])
def test_run_task_interruption_after_higher_read_keeps_pending_or_committed_proof(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.task_execution.time.time", lambda: 100)
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    clock = Clock()
    clock.cut = cut == "acquisition"
    connection = ArmingConnection(clock)
    decision = SQLiteProgramStore.work_decision_time
    interrupted = False

    def after_durable(store: SQLiteProgramStore, program_id: str) -> int:
        nonlocal interrupted
        value = decision(store, program_id)
        if cut == "durable-return" and clock.high_seen and value == 105 and not interrupted:
            interrupted = True
            raise ClockCut("synthetic interruption after durable higher decision time")
        return value

    with monkeypatch.context() as fault:
        fault.setattr(SQLiteProgramStore, "work_decision_time", after_durable)
        with pytest.raises(ClockCut):
            execute(control, source, connection, clock)
    assert connection.starts == 1 and connection.turns == 1 and clock.high_seen
    low = Clock()
    low.reading = BootClockReading(103, 3_000_000_000, "synthetic-task-clock")
    with SQLiteProgramStore(control / "kernel.sqlite3", boot_clock=low) as store:
        row = clock_row(store)
        assert (row["pending"] == 1) is (cut == "acquisition")
        if cut == "durable-return":
            assert row["wall"] == 105
        assert store.operation(OPERATION_ID).attempts == 1 and not store.operations("acceptance:")
    recovered = execute(control, source, connection, low)
    assert recovered["condition"] in {"unknown", "cancel_uncertain", "cancelled"}
    assert connection.starts == 1 and connection.turns == 1 and "acceptance" not in recovered and "pr" not in recovered
    if cut == "acquisition":
        assert low.calls == 0  # No unjournaled final-status or recovery-wall sample.


@pytest.mark.parametrize("failure", ["unavailable", "projection", "reader-error"])
@pytest.mark.parametrize("deadline", [None, 110])
def test_fresh_clock_failure_then_available_same_directory_initializes_normally(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, deadline: int | None
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.task_execution.time.time", lambda: 100)
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(lambda _path: None)
    connection.status = "inProgress"

    class InitiallyBad(Clock):
        bad = True

        def __call__(self) -> BootClockReading | None:
            if self.bad:
                if failure == "reader-error":
                    raise OSError("synthetic unavailable initial clock")
                return (
                    None
                    if failure == "unavailable"
                    else BootClockReading(3400 if deadline is None else deadline, 1_000_000_000, "synthetic-task-clock")
                )
            return super().__call__()

    clock = InitiallyBad()
    expected = UnsupportedExecution if failure == "unavailable" else ValueError if failure == "projection" else OSError
    with pytest.raises(expected):
        execute(control, source, connection, clock, deadline=deadline)
    assert connection.starts == 0 and not (control / "kernel.sqlite3").exists()
    assert not control.exists() or not tuple(control.iterdir())
    clock.bad = False
    result = execute(control, source, connection, clock, deadline=deadline)
    assert (
        result["condition"] == "running"
        and connection.starts == 1
        and result["deadline"] == (3400 if deadline is None else deadline)
    )


@pytest.mark.parametrize("with_metadata", [False, True])
def test_preexisting_unrelated_store_is_never_adopted_as_a_fresh_task(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_metadata: bool
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.task_execution.time.time", lambda: 100)
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    control.mkdir()
    clock = Clock()
    with SQLiteProgramStore(control / "kernel.sqlite3", boot_clock=clock) as store:
        if with_metadata:
            store.intent("unrelated:metadata", "unrelated:metadata", {"synthetic": True})
        original = tuple((item.operation_id, item.request_json) for item in store.operations())
    connection = FixtureConnection(lambda _path: None)
    with pytest.raises(ValueError, match="existing database is not this neutral task"):
        execute(control, source, connection, clock)
    assert connection.starts == 0
    with SQLiteProgramStore(control / "kernel.sqlite3", boot_clock=clock) as store:
        assert tuple((item.operation_id, item.request_json) for item in store.operations()) == original


def test_binding_new_work_also_journals_clock_observed_with_existing_envelopes(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "clock.sqlite3", boot_clock=clock) as store:
        prepare(store, initialize(store), "a1")
        original = spec()
        second = ProgramSpec(
            "program-2",
            original.objective,
            original.work_units,
            original.initial_inputs,
            original.budget,
            original.authority,
            acceptance_criteria=original.acceptance_criteria,
            policy_references=original.policy_references,
        )
        store.create(second, "create-second")
        clock.armed = True
        store.bind_work_budget("program-2", "synthetic-project", policy(), policy(), 110)
        assert clock_row(store, "program-1")["wall"] == 105
        with pytest.raises(BudgetRefused, match="rollback"):
            store.claim("runtime:a1", now=103, lease_seconds=1)
        assert store.operation("runtime:a1").attempts == 0


def test_real_linux_boot_clock_acquisition_remains_explicitly_verified() -> None:
    if sys.platform != "linux":
        pytest.skip("Linux CLOCK_BOOTTIME qualification requires Linux")
    before = read_boot_clock()
    after = read_boot_clock()
    assert before is not None and after is not None
    assert before.clock == "linux:CLOCK_BOOTTIME" and before.boot_id == after.boot_id
    assert str(uuid.UUID(before.boot_id)) == before.boot_id and after.boot_ns >= before.boot_ns
