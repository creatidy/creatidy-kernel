# SPDX-License-Identifier: Apache-2.0
"""Budget acceptance has one commit, including ordinary meaning and initial mode."""

import inspect
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_clock_transaction_boundaries import CommitFailure, connection
from test_intake import OWNER, SUBJECT, Scenario, declaration, policy
from test_reference_live import Connection, controlled_reference_clock
from test_sqlite_store import spec
from test_task_clock_sampling import Clock as TaskClock
from test_task_clock_sampling import execute
from test_task_execution import ALLOCATION, FixtureConnection, fixture_task, make_source
from test_work_budget import Clock, initialize
from test_work_budget import policy as budget

from creatidy_kernel.adapters import reference, task_execution
from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.sqlite_codec import program_json
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import CancelProgram, ProgramStatus
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.intake import IntakeRefused, OwnerPolicy
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.core.work_budget import BudgetRefused
from creatidy_kernel.ports.intake import OrdinaryIntake

pytest_plugins = ["test_sqlite_store", "test_clock_transaction_boundaries"]


def configured_policy() -> OwnerPolicy:
    return replace(policy(), work_budget=budget(), project_budget=budget(), project_id="project")


@pytest.mark.parametrize("change", ["remove", "expiry", "project", "task-policy", "project-policy"])
def test_refused_envelope_change_keeps_prior_meaning_applicable(sqlite_tmp_path: Path, change: str) -> None:
    path = sqlite_tmp_path / "intake.sqlite3"
    scenario = Scenario()
    selected = configured_policy()
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, selected, decision_id="original", expires_at=200)
        before = store.history("ordinary:task"), store.operations(), store.work_budget_status("ordinary:task")
        changed = {
            "remove": policy(),
            "expiry": selected,
            "project": replace(selected, project_id="other"),
            "task-policy": replace(selected, work_budget=budget(attempts=4)),
            "project-policy": replace(selected, project_budget=budget(attempts=4)),
        }[change]
        with pytest.raises(BudgetRefused):
            intake.approve(OWNER, draft, changed, decision_id="refused", expires_at=201 if change == "expiry" else 200)
        assert (store.history("ordinary:task"), store.operations(), store.work_budget_status("ordinary:task")) == before
        assert intake.approve(OWNER, draft, selected, decision_id="original", expires_at=200) == approved
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.approve(OWNER, draft, selected, decision_id="original", expires_at=200) == approved


@pytest.mark.parametrize("change", ["expiry", "project", "task-policy", "project-policy", "strict"])
def test_refused_amendment_preserves_prior_approval_bytes_history_and_budget(
    sqlite_tmp_path: Path, change: str
) -> None:
    path = sqlite_tmp_path / "intake.sqlite3"
    clock = Clock()
    scenario = Scenario()
    original_policy = configured_policy()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, original_policy, decision_id="original", expires_at=200)
        proposed = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        before = (
            program_json(approved.program),
            store.history("ordinary:task"),
            store.operations(),
            store.work_budget_status("ordinary:task"),
        )
        changed = {
            "expiry": original_policy,
            "project": replace(original_policy, project_id="other"),
            "task-policy": replace(original_policy, work_budget=budget(attempts=4)),
            "project-policy": replace(original_policy, project_budget=budget(attempts=4)),
            "strict": replace(original_policy, work_budget=replace(budget(), strict_inference=True)),
        }[change]
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        with pytest.raises(BudgetRefused):
            intake.approve(
                OWNER, proposed, changed, decision_id="refused", expires_at=201 if change == "expiry" else 200
            )
        assert (
            program_json(store.load("ordinary:task")),
            store.history("ordinary:task"),
            store.operations(),
            store.work_budget_status("ordinary:task"),
        ) == before
        assert intake.historical_decision("original") == approved.decision_bytes
        assert store.find_artifact("ordinary:decision:refused", "record") is None
        assert tuple(connection(store).execute("SELECT wall, boot_ns, pending FROM work_clock_state").fetchone()) == (
            105,
            2_000_000_000,
            0,
        )
    clock.reading = BootClockReading(103, 3_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        assert program_json(store.load("ordinary:task")) == before[0]
        with pytest.raises(BudgetRefused, match="rollback"):
            store.check_work_authority("ordinary:task")


@pytest.mark.parametrize("failure", ["strict", "other-project-policy", "clock"])
def test_refused_initial_approval_has_no_program_or_consumed_decision(sqlite_tmp_path: Path, failure: str) -> None:
    clock = Clock()
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.sqlite3", boot_clock=clock) as store:
        selected = configured_policy()
        if failure == "other-project-policy":
            initialize(store, project=budget(attempts=4))
            selected = replace(selected, project_id="synthetic-project")
        if failure == "strict":
            selected = replace(selected, project_budget=replace(budget(), strict_inference=True))
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("new", SUBJECT, declaration(), expected_parent=None)
        original = store.operations()
        if failure == "clock":
            clock.reading = None
        with pytest.raises(BudgetRefused):
            intake.approve(OWNER, draft, selected, decision_id="refused", expires_at=200)
        assert store.find_program("ordinary:new") is None and store.operations() == original
        assert store.history("ordinary:new") == ()
        assert store.find_artifact("ordinary:decision:refused", "record") is None


def test_success_amend_replay_and_reopen_keep_exact_original_projection(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "intake.sqlite3"
    clock = Clock()
    scenario = Scenario()
    selected = configured_policy()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        original = intake.approve(OWNER, draft, selected, decision_id="original", expires_at=200)
        binding = cast(dict[str, object], store.work_budget_status("ordinary:task")["binding"])
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        scenario.time = 105
        proposed = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        approved = intake.approve(OWNER, proposed, selected, decision_id="amended", expires_at=200)
        assert approved.program.spec.revision == 2
        assert intake.approve(OWNER, proposed, selected, decision_id="amended", expires_at=200) == approved
        assert intake.historical_decision("original") == original.decision_bytes
        assert len(store.history("ordinary:task")) == 2
        current = cast(dict[str, object], store.work_budget_status("ordinary:task")["binding"])
        assert current["projection"] == binding["projection"] and current["spec_digest"] == approved.program.spec.digest
        rows = store.operations()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.approve(OWNER, proposed, selected, decision_id="amended", expires_at=200) == approved
        assert store.operations() == rows
        assert (
            cast(dict[str, object], store.work_budget_status("ordinary:task")["binding"])["projection"]
            == binding["projection"]
        )


@pytest.mark.usefixtures("failing_connection")
@pytest.mark.parametrize("amend", [False, True])
def test_real_acceptance_commit_failure_rolls_back_every_business_fact_and_retries(
    sqlite_tmp_path: Path, amend: bool
) -> None:
    scenario = Scenario()
    clock = Clock()
    path = sqlite_tmp_path / "intake.sqlite3"
    selected = configured_policy()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        if amend:
            intake.approve(OWNER, draft, selected, decision_id="original", expires_at=200)
            draft = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        before = store.find_program("ordinary:task"), store.history("ordinary:task"), store.operations()
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        # Existing-envelope observation commits pending/floor before the business commit.
        cast(CommitFailure, connection(store)).remaining = 3 if amend else 1
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            intake.approve(OWNER, draft, selected, decision_id="retry", expires_at=200)
        assert (store.find_program("ordinary:task"), store.history("ordinary:task"), store.operations()) == before
        assert store.find_artifact("ordinary:decision:retry", "record") is None
        if amend:
            assert tuple(
                connection(store).execute("SELECT wall, boot_ns, pending FROM work_clock_state").fetchone()
            ) == (105, 2_000_000_000, 0)
        else:
            assert not cast(dict[str, BootClockReading], inspect.getattr_static(store, "_clock_watermarks"))
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        accepted = intake.approve(OWNER, draft, selected, decision_id="retry", expires_at=200)
        assert accepted.program.spec.revision == (2 if amend else 1)
        assert store.has_work_budget("ordinary:task")
        assert intake.approve(OWNER, draft, selected, decision_id="retry", expires_at=200) == accepted


@pytest.mark.parametrize("change", ["cancel", "draft"])
def test_configured_final_clock_callback_mutation_is_preserved_without_consumption(
    sqlite_tmp_path: Path, change: str
) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.sqlite3", boot_clock=Clock()) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        selected = configured_policy()
        original = intake.approve(OWNER, draft, selected, decision_id="original", expires_at=200)
        proposed = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        clocks = 0

        def clock() -> int:
            nonlocal clocks
            clocks += 1
            if clocks == 2:
                if change == "cancel":
                    store.admit("ordinary:task", "cancel", CancelProgram(original.program.revision, "owner"))
                else:
                    intake.prepare(
                        "task", SUBJECT, declaration(context=["callback mutation"]), expected_parent=proposed.digest
                    )
            return scenario.time

        intake.clock = clock
        with pytest.raises(IntakeRefused, match="changed"):
            intake.approve(OWNER, proposed, selected, decision_id="refused", expires_at=200)
        assert clocks == 2 and store.load("ordinary:task").spec == original.program.spec
        if change == "cancel":
            assert store.load("ordinary:task").status is ProgramStatus.CANCELLED
        else:
            assert len(intake.history("task")) == 3
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation("ordinary:decision:refused")


@pytest.mark.usefixtures("failing_connection")
def test_owner_primitive_rolls_back_mode_create_binding_and_artifact_refs_together(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "initialize.sqlite3"
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        cast(CommitFailure, connection(store)).remaining = 1
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            store.accept_work_budget(
                spec(),
                "create",
                "project",
                budget(),
                budget(),
                200,
                records=(("mode", {"mode": "fixture"}, b"synthetic bytes"),),
            )
        assert store.find_program("program-1") is None and store.operations() == () and store.history("program-1") == ()
        assert store.find_artifact("mode", "record") is None
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        store.accept_work_budget(
            spec(),
            "create",
            "project",
            budget(),
            budget(),
            200,
            records=(("mode", {"mode": "fixture"}, b"synthetic bytes"),),
        )
        assert store.artifact("mode", "record") == b"synthetic bytes" and store.has_work_budget("program-1")


@pytest.mark.usefixtures("failing_connection")
def test_task_atomic_commit_failure_allows_corrected_initialization(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_execution.time, "time", lambda: 100)
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    clock = TaskClock()
    client = FixtureConnection(lambda _: None)
    client.status = "inProgress"
    original = CommitFailure.execute
    calls = 0

    def fail_initial(database: CommitFailure, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        nonlocal calls
        if sql.startswith("INSERT INTO history"):
            calls += 1
            database.remaining = 1
        return original(database, sql, parameters)

    with monkeypatch.context() as fault:
        fault.setattr(CommitFailure, "execute", fail_initial)
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            execute(control, source, client, clock)
    assert calls == 1 and client.starts == 0 and not (control / "kernel.sqlite3").exists()
    assert execute(control, source, client, clock)["condition"] == "running" and client.starts == 1


def test_task_strict_policy_refuses_before_initialization(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_execution.time, "time", lambda: 100)
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    client = FixtureConnection(lambda _: None)
    with pytest.raises(BudgetRefused, match="strict inference"):
        task_execution.run_task(
            control,
            task=fixture_task(repeats=1),
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=client,
            version=client.version,
            allocation=ALLOCATION,
            deadline=110,
            boot_clock=TaskClock(),
            work_policy=replace(budget(), strict_inference=True),
        )
    assert client.starts == 0 and not control.exists()


@pytest.mark.usefixtures("failing_connection")
def test_failed_approval_clock_flush_retains_pending_without_false_cache(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "intake.sqlite3"
    clock = Clock()
    scenario = Scenario()
    selected = configured_policy()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, selected, decision_id="original", expires_at=200)
        proposed = intake.prepare("task", SUBJECT, declaration(context=["amendment"]), expected_parent=draft.digest)
        original = store.operations()
        clock.reading = BootClockReading(105, 2_000_000_000, "synthetic-boot-a")
        cast(CommitFailure, connection(store)).remaining = 2
        with pytest.raises(BudgetRefused):
            intake.approve(OWNER, proposed, selected, decision_id="refused", expires_at=200)
        assert store.operations() == original and store.load("ordinary:task") == approved.program
        assert tuple(connection(store).execute("SELECT wall, boot_ns, pending FROM work_clock_state").fetchone()) == (
            100,
            1_000_000_000,
            1,
        )
        cached = cast(dict[str, BootClockReading], inspect.getattr_static(store, "_clock_watermarks"))
        assert cached["ordinary:task"].wall == 100
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        with pytest.raises(BudgetRefused):
            intake.approve(OWNER, proposed, selected, decision_id="refused", expires_at=200)
        assert store.operations() == original and store.load("ordinary:task") == approved.program


@pytest.mark.parametrize("composition", ["offline", "live"])
def test_reference_refused_clock_leaves_no_mode_or_program_then_corrected_retry(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, composition: str
) -> None:
    monkeypatch.setattr(reference.time, "time", lambda: 100)
    control = sqlite_tmp_path / "reference"
    control.mkdir()
    original_init = SQLiteProgramStore.__init__
    if composition == "offline":

        def initialize_store(store: SQLiteProgramStore, path: Path, **kwargs: object) -> None:
            original_init(store, path, boot_clock=lambda: None)

        monkeypatch.setattr(SQLiteProgramStore, "__init__", initialize_store)

        def run_offline() -> dict[str, object]:
            return reference.run_reference(control, owner_approved=True)

        run = run_offline
    else:
        objects = sqlite_tmp_path / "objects.git"
        objects.mkdir()
        reference.reference_git(objects, "init", "--bare")
        repository = Reference("forgejo:synthetic/reference")
        transport = SyntheticForgeTransport(repository)
        transport.branches["develop"] = reference.reference_commit(objects, b"")
        forge = ForgejoForge(transport, transport, lambda _: True)
        client = Connection(control)
        available = False

        def run_live() -> dict[str, object]:
            return run_live_reference(
                control,
                owner_approved=True,
                trusted_development_acknowledged=True,
                connection=client,
                version=client.version,
                allocation=Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "owner selection"),
                forge=forge,
                repository=repository,
                object_source=objects,
                deadline=200 if available else 201,
                max_observations=3,
                boot_clock=controlled_reference_clock if available else lambda: None,
            )

        run = run_live

    with pytest.raises(BudgetRefused, match="clock unavailable"):
        run()
    if composition == "offline":
        monkeypatch.setattr(SQLiteProgramStore, "__init__", original_init)
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        assert store.find_program("reference") is None and not store.operations()
    available = True
    assert run()["status"] == "completed"
