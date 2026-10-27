# SPDX-License-Identifier: Apache-2.0
"""Clock evidence commits independently; uncertain acquisition never grants authority."""

import inspect
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Literal, cast

import pytest
from test_work_budget import Clock, initialize, prepare

from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.work_budget import BudgetRefused, WorkKind
from tools.check_schema_upgrade import PINNED_PRECLOCK_ID, install_pinned_reader

pytest_plugins = ["test_sqlite_store"]


class CountingClock(Clock):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def __call__(self) -> BootClockReading | None:
        self.calls += 1
        return super().__call__()


class CommitFailure(sqlite3.Connection):
    remaining = 0

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if sql == "COMMIT" and self.remaining:
            self.remaining -= 1
            if self.remaining == 0:
                raise sqlite3.OperationalError("synthetic commit failure")
        return super().execute(sql, cast(Sequence[object] | Mapping[str, object], parameters))


@pytest.fixture
def failing_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    original = sqlite3.connect

    def connect(
        database: str,
        *,
        timeout: float = 5.0,
        isolation_level: Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"] | None = "DEFERRED",
        uri: bool = False,
    ) -> CommitFailure:
        return original(database, timeout=timeout, isolation_level=isolation_level, uri=uri, factory=CommitFailure)

    monkeypatch.setattr(sqlite3, "connect", connect)


def connection(store: SQLiteProgramStore) -> sqlite3.Connection:
    return cast(sqlite3.Connection, inspect.getattr_static(store, "_connection"))


def state(store: SQLiteProgramStore) -> tuple[int, int, int]:
    row = (
        connection(store)
        .execute("SELECT wall, boot_ns, pending FROM work_clock_state WHERE program_id = 'program-1'")
        .fetchone()
    )
    return cast(tuple[int, int, int], tuple(row))


def test_refused_capacity_and_same_second_recheck_keep_durable_highwater(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "clock.sqlite3"
    clock = CountingClock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        prepare(store, initialize(store), "a1")
        original = store.work_budget_status("program-1")
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        clock.calls = 0
        with pytest.raises(BudgetRefused, match="tool capacity"):
            store.reserve_work("program-1", "a1", "refused-tool", WorkKind.TOOL)
        assert clock.calls == 1 and state(store) == (105, 2_000_000_000, 0)
        store.check_work_authority("program-1")
        assert state(store) == (105, 2_000_000_000, 0)
        assert store.work_budget_status("program-1") == original
        assert connection(store).execute("SELECT COUNT(*) FROM work_clock_state").fetchone()[0] == 1
    clock.reading = BootClockReading(103, 3_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match="rollback"):
            store.check_work_authority("program-1")
        with pytest.raises(BudgetRefused, match="rollback"):
            store.claim("runtime:a1", now=103, lease_seconds=1)
        assert store.operation("runtime:a1").attempts == 0


@pytest.mark.usefixtures("failing_connection")
def test_admission_commit_failure_does_not_rollback_clock_evidence(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "clock.sqlite3"
    clock = CountingClock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        program = initialize(store)
        before = store.work_budget_status(program.program_id)
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        # Pending marker, completed observation, then ordinary admission commit.
        cast(CommitFailure, connection(store)).remaining = 3
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            prepare(store, program, "a1")
        assert store.load(program.program_id) == program and store.work_budget_status(program.program_id) == before
        assert state(store) == (105, 2_000_000_000, 0)
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation("runtime:a1")
    clock.reading = BootClockReading(103, 3_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match="rollback"):
            store.check_work_authority("program-1")


@pytest.mark.usefixtures("failing_connection")
@pytest.mark.parametrize("retry_same_owner", [False, True])
def test_failed_clock_flush_has_committed_pending_marker_and_no_false_cache(
    sqlite_tmp_path: Path, retry_same_owner: bool
) -> None:
    path = sqlite_tmp_path / "clock.sqlite3"
    clock = CountingClock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        prepare(store, initialize(store), "a1")
        before = store.work_budget_status("program-1")
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        cast(CommitFailure, connection(store)).remaining = 2
        with pytest.raises(BudgetRefused, match="persistence unverified"):
            store.reserve_work("program-1", "a1", "refused-tool", WorkKind.TOOL)
        assert state(store) == (100, 1_000_000_000, 1)
        cached = cast(dict[str, BootClockReading], inspect.getattr_static(store, "_clock_watermarks"))
        assert cached["program-1"].wall == 100 and store.work_budget_status("program-1") == before
        if retry_same_owner:
            store.check_work_authority("program-1")
            assert state(store) == (105, 2_000_000_000, 0)
        else:
            bundle = store.backup(sqlite_tmp_path / "pending-backup")
            restored = SQLiteProgramStore.restore_backup(bundle, sqlite_tmp_path / "pending-restored.sqlite3")
            with SQLiteProgramStore(restored, boot_clock=clock) as copy:
                with pytest.raises(BudgetRefused, match="persistence unverified"):
                    copy.check_work_authority("program-1")
    clock.reading = BootClockReading(103, 3_000_000_000, "synthetic-boot-a")
    clock.calls = 0
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match="rollback" if retry_same_owner else "persistence unverified"):
            store.claim("runtime:a1", now=103, lease_seconds=1)
        assert store.operation("runtime:a1").attempts == 0
        if not retry_same_owner:
            assert clock.calls == 0 and state(store)[2] == 1


@pytest.mark.usefixtures("failing_connection")
def test_failed_pending_marker_commit_never_acquires_unjournaled_clock(sqlite_tmp_path: Path) -> None:
    clock = CountingClock()
    with SQLiteProgramStore(sqlite_tmp_path / "clock.sqlite3", boot_clock=clock) as store:
        prepare(store, initialize(store), "a1")
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        clock.calls = 0
        cast(CommitFailure, connection(store)).remaining = 1
        with pytest.raises(BudgetRefused):
            store.check_work_authority("program-1")
        assert clock.calls == 0 and state(store) == (100, 1_000_000_000, 0)
        store.check_work_authority("program-1")
        assert state(store) == (105, 2_000_000_000, 0)


def test_interrupted_acquisition_remains_unknown_on_fresh_recovery(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "clock.sqlite3"

    class InterruptedClock(CountingClock):
        interrupted = False

        def __call__(self) -> BootClockReading | None:
            if self.interrupted:
                raise RuntimeError("synthetic interruption after durable clock-read marker")
            return super().__call__()

    clock = InterruptedClock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        prepare(store, initialize(store), "a1")
        clock.interrupted = True
        with pytest.raises(RuntimeError, match="interruption"):
            store.check_work_authority("program-1")
        assert state(store)[2] == 1
    clock.interrupted = False
    clock.reading = BootClockReading(103, 3_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match="persistence unverified"):
            store.claim("runtime:a1", now=103, lease_seconds=1)


def test_real_schema_three_clock_floor_migrates_without_reprojection(sqlite_tmp_path: Path, tmp_path: Path) -> None:
    old_python = install_pinned_reader(tmp_path, commit_id=PINNED_PRECLOCK_ID)
    path = sqlite_tmp_path / "legacy-clock.sqlite3"
    script = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt
from creatidy_kernel.core.work_budget import WorkBudgetPolicy
reading = BootClockReading(100, 1000000000, 'synthetic-boot-a')
with SQLiteProgramStore(Path(sys.argv[1]), boot_clock=lambda: reading) as store:
    assert store.startup_evidence.schema_version == 3
    program = store.create(reference_spec(), 'create')
    budget = WorkBudgetPolicy.basic(1, 1, verifications=1, retained_bytes=1024)
    store.bind_work_budget('reference', 'project', budget, budget, 110)
    program = store.admit('reference', 'activate', ActivateProgram(0, 'owner'))
    attempt = AttemptSpec('a1', 'reference', 'first', program.spec.revision,
                          program.spec.digest, program.resolved_inputs('first'))
    store.admit_with_intent('reference', 'prepare', PrepareAttempt(program.revision, 'worker', attempt),
                           'runtime:a1', 'runtime:a1', {'attempt': attempt.digest})
    reading = BootClockReading(105, 2000000000, 'synthetic-boot-a')
    store.check_work_authority('reference')
"""
    subprocess.run([str(old_python), "-I", "-c", script, str(path)], check=True)  # noqa: S603 - exact pinned own source, synthetic fixture.
    with closing(sqlite3.connect(path)) as original:
        records = original.execute("SELECT operation_id, request_json FROM operations ORDER BY operation_id").fetchall()
    clock = CountingClock()
    clock.reading = BootClockReading(103, 3_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        assert store.startup_evidence.schema_version == 4 and store.original_work_deadline("reference") == 110
        with pytest.raises(BudgetRefused, match="rollback"):
            store.claim("runtime:a1", now=103, lease_seconds=1)
        assert store.operation("runtime:a1").attempts == 0
        retained = (
            connection(store)
            .execute("SELECT operation_id, request_json FROM operations ORDER BY operation_id")
            .fetchall()
        )
        assert [tuple(row) for row in retained] == records
