# SPDX-License-Identifier: Apache-2.0
"""Acceptance freshness and resource provenance apply at the final owner boundary."""

import inspect
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from test_budget_acceptance import business_operations, configured_policy
from test_clock_transaction_boundaries import CommitFailure, connection
from test_intake import OWNER, SUBJECT, Scenario, declaration
from test_sqlite_store import spec
from test_work_budget import Clock, policy, prepare

from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading, DeadlineProjection
from creatidy_kernel.core.domain import ActivateProgram, CancelProgram
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.work_budget import BudgetRefused, ResourceCeiling, WorkKind
from creatidy_kernel.ports.intake import OrdinaryIntake

pytest_plugins = ["test_sqlite_store", "test_clock_transaction_boundaries"]


@pytest.mark.parametrize("kind", ["initial", "amendment", "replay"])
@pytest.mark.parametrize("late", ["expiry", "rollback", "reboot", "unavailable"])
def test_final_callback_cannot_spend_stale_boot_authority(sqlite_tmp_path: Path, kind: str, late: str) -> None:
    path = sqlite_tmp_path / "intake.sqlite3"
    clock = Clock()
    scenario = Scenario()
    selected = configured_policy()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        identity = "original"
        if kind != "initial":
            intake.approve(OWNER, draft, selected, decision_id=identity, expires_at=110)
        if kind == "amendment":
            draft = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
            identity = "amended"
        before = (
            store.find_program("ordinary:task"),
            store.history("ordinary:task"),
            business_operations(store),
            store.work_budget_status("ordinary:task"),
        )
        calls = 0

        def final_wall_clock() -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.reading = {
                    "expiry": BootClockReading(100, 21_000_000_000, "synthetic-boot-a"),
                    "rollback": BootClockReading(100, 500_000_000, "synthetic-boot-a"),
                    "reboot": BootClockReading(100, 2_000_000_000, "synthetic-boot-b"),
                    "unavailable": None,
                }[late]
            return 100

        intake.clock = final_wall_clock
        with pytest.raises(BudgetRefused):
            intake.approve(OWNER, draft, selected, decision_id=identity, expires_at=110)
        assert calls == 2
        assert (
            store.find_program("ordinary:task"),
            store.history("ordinary:task"),
            business_operations(store),
            store.work_budget_status("ordinary:task"),
        ) == before
        if kind != "initial" and late == "expiry":
            assert tuple(
                connection(store).execute("SELECT wall, boot_ns, pending FROM work_clock_state").fetchone()
            ) == (100, 21_000_000_000, 0)
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        assert (
            store.find_program("ordinary:task"),
            store.history("ordinary:task"),
            business_operations(store),
            store.work_budget_status("ordinary:task"),
        ) == before
        if kind != "initial":
            assert store.original_work_deadline("ordinary:task") == 110
            with pytest.raises(BudgetRefused, match="renewed"):
                store.bind_work_budget("ordinary:task", "project", policy(), policy(), 120)
        else:
            origin = store.operations("work-budget:v1:clock-origin:")
            assert len(origin) == 1 and store.original_work_deadline("ordinary:task") == 110
            intake = OrdinaryIntake(store, scenario.reader, lambda: 100)
            with pytest.raises(BudgetRefused):
                intake.approve(OWNER, draft, selected, decision_id=identity, expires_at=110)
            with pytest.raises(BudgetRefused, match="renewed"):
                intake.approve(OWNER, draft, selected, decision_id=identity, expires_at=120)
            assert store.operations("work-budget:v1:clock-origin:") == origin
            assert not store.find_program("ordinary:task") and business_operations(store) == before[2]


@pytest.mark.parametrize("kind", ["initial", "amendment", "replay"])
def test_fresh_same_boot_acceptance_never_reanchors_after_callback(sqlite_tmp_path: Path, kind: str) -> None:
    events: list[str] = []

    class ObservedClock(Clock):
        def __call__(self) -> BootClockReading | None:
            events.append("boot")
            return super().__call__()

    clock = ObservedClock()
    scenario = Scenario()
    selected = configured_policy()
    with SQLiteProgramStore(sqlite_tmp_path / "fresh.sqlite3", boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: 100)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        if kind != "initial":
            intake.approve(OWNER, draft, selected, decision_id="original", expires_at=110)
        if kind == "amendment":
            draft = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        wall_calls = 0

        def final_wall_clock() -> int:
            nonlocal wall_calls
            wall_calls += 1
            events.append("wall")
            if wall_calls == 2:
                clock.reading = BootClockReading(100, 5_000_000_000, "synthetic-boot-a")
            return 100

        events.clear()
        intake.clock = final_wall_clock
        accepted = intake.approve(
            OWNER, draft, selected, decision_id="amended" if kind == "amendment" else "original", expires_at=110
        )
        assert events[-1] == "boot" and events.count("wall") == 2
        binding = cast(dict[str, object], store.work_budget_status("ordinary:task")["binding"])
        assert binding["projection"] == {
            "wall_deadline": 110,
            "wall_anchor": 100,
            "boot_anchor_ns": 1_000_000_000,
            "boot_deadline_ns": 11_000_000_000,
            "boot_id": "synthetic-boot-a",
            "clock": "linux:CLOCK_BOOTTIME",
        }
        assert accepted.program.spec.revision == (2 if kind == "amendment" else 1)
        assert tuple(connection(store).execute("SELECT wall, boot_ns, pending FROM work_clock_state").fetchone()) == (
            100,
            5_000_000_000,
            0,
        )


@pytest.mark.parametrize("amend", [False, True])
@pytest.mark.parametrize("cut", ["pending", "flush"])
@pytest.mark.usefixtures("failing_connection")
def test_final_capture_failure_preserves_origin_pending_and_committed_floors(
    sqlite_tmp_path: Path, amend: bool, cut: str
) -> None:
    path = sqlite_tmp_path / "capture.sqlite3"
    clock = Clock()
    scenario = Scenario()
    selected = configured_policy()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: 100)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        if amend:
            intake.approve(OWNER, draft, selected, decision_id="original", expires_at=110)
            draft = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        before = (
            store.find_program("ordinary:task"),
            store.history("ordinary:task"),
            business_operations(store),
            store.work_budget_status("ordinary:task"),
        )
        calls = 0

        def fail_final_capture() -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.reading = BootClockReading(100, 5_000_000_000, "synthetic-boot-a")
                cast(CommitFailure, connection(store)).remaining = 1 if cut == "pending" else 2
            return 100

        intake.clock = fail_final_capture
        with pytest.raises(BudgetRefused):
            intake.approve(OWNER, draft, selected, decision_id="refused", expires_at=110)
        assert (
            store.find_program("ordinary:task"),
            store.history("ordinary:task"),
            business_operations(store),
            store.work_budget_status("ordinary:task"),
        ) == before
        row = connection(store).execute("SELECT wall, boot_ns, pending FROM work_clock_state").fetchone()
        if cut == "flush" or amend:
            assert tuple(row) == (100, 1_000_000_000, int(cut == "flush"))
        else:
            assert row is None
        cached = cast(dict[str, BootClockReading], inspect.getattr_static(store, "_clock_watermarks"))
        if amend:
            assert cached["ordinary:task"] == BootClockReading(100, 1_000_000_000, "synthetic-boot-a")
        else:
            assert "ordinary:task" not in cached
        assert store.original_work_deadline("ordinary:task") == 110
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: 100)
        if cut == "flush":
            with pytest.raises(BudgetRefused, match="persistence unverified"):
                intake.approve(OWNER, draft, selected, decision_id="refused", expires_at=110)
        else:
            accepted = intake.approve(OWNER, draft, selected, decision_id="refused", expires_at=110)
            binding = cast(dict[str, object], store.work_budget_status("ordinary:task")["binding"])
            assert DeadlineProjection.decode(binding["projection"]).boot_anchor_ns == 1_000_000_000
            assert accepted.program.spec.revision == (2 if amend else 1)


def test_final_boot_reader_mutation_cannot_authorize_stale_program(sqlite_tmp_path: Path) -> None:
    class ReentrantClock(Clock):
        action = False

        def __call__(self) -> BootClockReading | None:
            if self.action:
                self.action = False
                current = store.load("ordinary:task")
                store.admit("ordinary:task", "callback-cancel", CancelProgram(current.revision, "owner"))
            return super().__call__()

    clock = ReentrantClock()
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "reentrant.sqlite3", boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: 100)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, configured_policy(), decision_id="original", expires_at=110)
        proposed = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        calls = 0

        def arm_reader() -> int:
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.action = True
            return 100

        intake.clock = arm_reader
        with pytest.raises(BudgetRefused):
            intake.approve(OWNER, proposed, configured_policy(), decision_id="refused", expires_at=110)
        assert store.load("ordinary:task").spec == approved.program.spec
        assert store.load("ordinary:task").status.value == "cancelled"
        assert not store.operations("ordinary:decision:refused")


def test_supplied_initial_projection_must_be_fresh_not_merely_well_formed(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    projection = DeadlineProjection.bind(110, BootClockReading(100, 1_000_000_000, "synthetic-boot-a"))
    clock.reading = BootClockReading(100, 21_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(sqlite_tmp_path / "supplied.sqlite3", boot_clock=clock) as store:
        with pytest.raises(BudgetRefused):
            store.accept_work_budget(spec(), "create", "project", policy(), policy(), projection)
        assert store.find_program("program-1") is None and not store.history("program-1")


def test_inert_original_projection_cannot_be_renewed_by_other_binding_paths(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "original.sqlite3"
    clock = Clock()
    original = DeadlineProjection.bind(110, BootClockReading(100, 1_000_000_000, "synthetic-boot-a"))
    clock.reading = BootClockReading(100, 21_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused):
            store.accept_work_budget(spec(), "create", "project", policy(), policy(), original)
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        assert store.pending_work_projection(spec()) == original
        program = store.create(spec(), "legacy-pure-draft")
        with pytest.raises(BudgetRefused, match="renewed"):
            store.bind_work_budget("program-1", "project", policy(), policy(), 120)
        assert clock.reading is not None
        with pytest.raises(BudgetRefused, match="renewed"):
            store.configure_work_budget(
                "program-1", "project", policy(), policy(), DeadlineProjection.bind(110, clock.reading)
            )
        assert not store.has_work_budget("program-1") and store.load("program-1") == program
        assert store.pending_work_projection(spec()) == original


@pytest.mark.parametrize("scope", ["program", "project"])
@pytest.mark.parametrize("observation", ["owner-over", "estimate-correction"])
def test_current_operation_provenance_cannot_erase_resource_hold(
    sqlite_tmp_path: Path, scope: str, observation: str
) -> None:
    path = sqlite_tmp_path / "provenance.sqlite3"
    clock = Clock()
    metric, unit = ("attention", "minutes") if observation == "owner-over" else ("money", "USD")
    limited = replace(policy(), resources=(ResourceCeiling(metric, unit, Decimal(10)),))
    project, task = (limited, policy()) if scope == "project" else (policy(), limited)
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        program = store.create(spec(), "create")
        store.bind_work_budget(program.program_id, "project", project, task, 110)
        program = store.admit(program.program_id, "activate", ActivateProgram(0, "owner"))
        prepare(store, program, "a1")
        original = UsageObservation(
            "original",
            "a1",
            "receipt",
            metric,
            unit,
            Decimal(20),
            Provenance.OWNER_RECORD if observation == "owner-over" else Provenance.PROVIDER_REPORT,
            "synthetic-source",
            100,
        )
        store.record_usage("runtime:a1", original)
        if observation == "estimate-correction":
            store.record_usage(
                "runtime:a1",
                replace(
                    original,
                    observation_id="estimate",
                    quantity=Decimal(1),
                    provenance=Provenance.ESTIMATE,
                    supersedes_id="original",
                ),
            )
        before = store.work_budget_status(program.program_id), store.operation("runtime:a1")
        with pytest.raises(BudgetRefused, match=f"{scope} .*exhausted|{scope} .*unknown"):
            store.claim("runtime:a1", now=100, lease_seconds=1)
        assert (store.work_budget_status(program.program_id), store.operation("runtime:a1")) == before
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(BudgetRefused, match=f"{scope} .*exhausted|{scope} .*unknown"):
            store.claim("runtime:a1", now=100, lease_seconds=1)
        assert (store.work_budget_status("program-1"), store.operation("runtime:a1")) == before
        effective = cast(list[dict[str, object]], before[0]["effective_usage"])
        assert len(effective) == 1 and effective[0]["provenance"] == (
            Provenance.OWNER_RECORD if observation == "owner-over" else Provenance.ESTIMATE
        )


@pytest.mark.parametrize("scope", ["program", "project"])
@pytest.mark.parametrize("provenance", list(Provenance))
def test_current_resource_receipt_preserves_supported_known_and_estimate_unknown(
    sqlite_tmp_path: Path, scope: str, provenance: Provenance
) -> None:
    path = sqlite_tmp_path / "lower.sqlite3"
    limited = replace(policy(), resources=(ResourceCeiling("attention", "minutes", Decimal(10)),))
    project, task = (limited, policy()) if scope == "project" else (policy(), limited)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        store.bind_work_budget(program.program_id, "project", project, task, 110)
        program = store.admit(program.program_id, "activate", ActivateProgram(0, "owner"))
        prepare(store, program, "a1")
        observation = UsageObservation(
            "lower", "a1", "receipt", "attention", "minutes", Decimal(1), provenance, "synthetic-source", 100
        )
        store.record_usage("runtime:a1", replace(observation, observation_id="over", quantity=Decimal(20)))
        observation = replace(observation, supersedes_id="over")
        store.record_usage("runtime:a1", observation)
        store.record_usage("runtime:a1", observation)
        # Other units/currencies remain distinct; never a conversion or an additive attention fact.
        store.record_usage(
            "runtime:a1",
            replace(
                observation,
                observation_id="other-unit",
                receipt_id="other-receipt",
                unit="seconds",
                quantity=Decimal(100),
                supersedes_id=None,
            ),
        )
        before = store.work_budget_status(program.program_id)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        assert store.work_budget_status("program-1") == before
        if provenance is Provenance.ESTIMATE:
            with pytest.raises(BudgetRefused, match=f"{scope} resource headroom unknown"):
                store.claim("runtime:a1", now=100, lease_seconds=1)
        else:
            assert store.claim("runtime:a1", now=100, lease_seconds=1) == 1
        store.record_work_settlement("runtime:a1", "synthetic:original-owned-group-retired")
        with pytest.raises(BudgetRefused, match=f"{scope} resource headroom unknown"):
            store.reserve_work("program-1", "a2", "another-operation", WorkKind.ATTEMPT)
        assert store.work_budget_status("program-1")["effective_usage"] == before["effective_usage"]
