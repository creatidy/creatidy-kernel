# SPDX-License-Identifier: Apache-2.0
"""Deterministic persistence, recovery, and command-admission regressions."""

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from creatidy_kernel.adapters.sqlite_codec import CODEC_VERSION, RecordCodecError, program_from_json, program_json
from creatidy_kernel.adapters.sqlite_store import (
    ConcurrentWriter,
    IdempotencyConflict,
    SQLiteProgramStore,
    UnsupportedSQLiteConfiguration,
    WrongWriterThread,
)
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AmendProgramSpec,
    AttemptSpec,
    AttemptStatus,
    AuthorityEnvelope,
    CancelAttempt,
    CancelProgram,
    CancelWorkUnit,
    DomainCommandType,
    FailAttempt,
    FinishAttempt,
    InputBinding,
    PauseProgram,
    PolicyReference,
    PrepareAttempt,
    Program,
    ProgramConclusion,
    ProgramSpec,
    ProgramStatus,
    ResumeProgram,
    SatisfyWorkUnit,
    SpecAmendment,
    StartAttempt,
    TrustedSatisfaction,
    WorkUnit,
    WorkUnitStatus,
)

OWNER = "owner"
WORKER = "worker-7"
VERIFIER = "verifier"


def spec() -> ProgramSpec:
    return ProgramSpec(
        program_id="program-1",
        objective="deliver a bounded result",
        work_units=(
            WorkUnit(
                "first",
                "produce an approved output",
                required_inputs=frozenset({"seed"}),
                outputs=frozenset({"artifact"}),
                acceptance_criteria=("first is accepted",),
            ),
            WorkUnit(
                "second",
                "consume the approved output",
                dependencies=("first",),
                required_inputs=frozenset({"artifact"}),
                acceptance_criteria=("second is accepted",),
            ),
        ),
        initial_inputs=(InputBinding("seed", "approved:seed:v1"),),
        authority=AuthorityEnvelope(
            OWNER,
            trusted_satisfaction_issuers=frozenset({VERIFIER}),
            delegated_actor_ids=frozenset({WORKER}),
        ),
        acceptance_criteria=("the Program result is complete",),
        policy_references=(PolicyReference("acceptance", "v1", "sha256:policy"),),
    )


def _attempt(program: Program, attempt_id: str, work_unit_id: str) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        program_id=program.program_id,
        work_unit_id=work_unit_id,
        spec_revision=program.spec.revision,
        spec_digest=program.spec.digest,
        effective_inputs=program.resolved_inputs(work_unit_id),
        agent_definition_reference="agent:worker-v7",
        allocation_reference="allocation:fixed-1",
        workspace_reference="workspace:local-1",
        context_reference="context:approved-1",
    )


def _satisfaction(program: Program) -> TrustedSatisfaction:
    return TrustedSatisfaction(
        reference_id="satisfaction-1",
        program_id=program.program_id,
        work_unit_id="first",
        spec_revision=program.spec.revision,
        spec_digest=program.spec.digest,
        work_unit_fingerprint=program.spec.work_unit_applicability_fingerprint("first"),
        issuer_id=VERIFIER,
        source_attempt_id="attempt-1",
        output_bindings=(InputBinding("artifact", "approved:artifact:v1"),),
    )


def _apply(store: SQLiteProgramStore, key: str, command: DomainCommandType) -> Program:
    return store.admit("program-1", key, command)


def _connection(store: SQLiteProgramStore) -> sqlite3.Connection:
    connection_attribute = "_connection"
    return cast(sqlite3.Connection, getattr(store, connection_attribute))


def _projection_writer(store: SQLiteProgramStore) -> Callable[..., None]:
    writer_attribute = "_write_projections"
    return cast(Callable[..., None], getattr(store, writer_attribute))


def _complete_and_cancel(store: SQLiteProgramStore) -> Program:
    store.create(spec(), "create-program")
    program = _apply(store, "amend-spec", AmendProgramSpec(0, OWNER, SpecAmendment(1, objective="amended intent")))
    program = _apply(store, "activate", ActivateProgram(program.revision, OWNER))
    program = _apply(
        store,
        "prepare-first",
        PrepareAttempt(program.revision, WORKER, _attempt(program, "attempt-1", "first")),
    )
    program = _apply(store, "start-first", StartAttempt(program.revision, WORKER, "attempt-1"))
    program = _apply(store, "finish-first", FinishAttempt(program.revision, WORKER, "attempt-1"))
    program = _apply(store, "satisfy-first", SatisfyWorkUnit(program.revision, VERIFIER, _satisfaction(program)))
    program = _apply(
        store,
        "prepare-second",
        PrepareAttempt(program.revision, WORKER, _attempt(program, "attempt-2", "second")),
    )
    program = _apply(store, "cancel-second", CancelWorkUnit(program.revision, OWNER, "second", "owner narrowed scope"))
    return _apply(store, "cancel-program", CancelProgram(program.revision, OWNER))


def test_round_trip_preserves_all_final_k1_facts_and_actor_authority_order(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        expected = _complete_and_cancel(store)
        records = store.history(expected.program_id)
        assert [record.sequence for record in records] == list(range(1, len(records) + 1))
        assert [record.command_type for record in records] == [
            "create_program",
            "amend_spec",
            "activate_program",
            "prepare_attempt",
            "start_attempt",
            "finish_attempt",
            "satisfy_work_unit",
            "prepare_attempt",
            "cancel_work_unit",
            "cancel_program",
        ]
        assert [record.actor_id for record in records] == [
            OWNER,
            OWNER,
            OWNER,
            WORKER,
            WORKER,
            WORKER,
            VERIFIER,
            WORKER,
            OWNER,
            OWNER,
        ]

    with SQLiteProgramStore(path) as reopened:
        actual = reopened.load("program-1")

    assert actual == expected
    assert tuple(item.revision for item in actual.spec_history) == (1, 2)
    assert actual.spec_history[0].initial_inputs == (InputBinding("seed", "approved:seed:v1"),)
    assert actual.spec.authority.delegated_actor_ids == frozenset({WORKER})
    assert actual.spec.authority.trusted_satisfaction_issuers == frozenset({VERIFIER})
    assert actual.attempt("attempt-1").actor_id == WORKER
    assert actual.attempt("attempt-1").spec.effective_inputs == (InputBinding("seed", "approved:seed:v1"),)
    assert actual.attempt("attempt-2").spec.effective_inputs == (
        InputBinding("artifact", "approved:artifact:v1", "predecessor", "first", "satisfaction-1", "artifact"),
    )
    assert actual.attempt("attempt-2").status is AttemptStatus.CANCELLED
    assert actual.satisfactions[0].issuer_id == VERIFIER
    assert actual.satisfactions[0].record_order == 6
    assert actual.cancellations[0].actor_id == OWNER
    assert actual.cancellations[0].reason == "owner narrowed scope"
    assert actual.cancellations[0].record_order == 8
    assert actual.attempt_cancellations[0].actor_id == OWNER
    assert actual.attempt_cancellations[0].record_order == 8
    assert actual.conclusions == (ProgramConclusion(ProgramStatus.CANCELLED, 2, actual.spec.digest, 9),)
    assert actual.state("second").status is WorkUnitStatus.CANCELLED


def test_duplicate_command_returns_prior_result_without_reapplying_and_conflicts_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(tmp_path / "kernel.sqlite3") as store:
        store.create(spec(), "create")
        activate = ActivateProgram(0, OWNER)
        prior_result = store.admit("program-1", "activate-once", activate)
        later_result = store.admit("program-1", "pause-once", PauseProgram(1, OWNER))
        assert later_result.status is ProgramStatus.PAUSED

        def forbidden_apply(self: Program, command: object) -> Program:
            del self, command
            raise AssertionError("a duplicate command must not be applied again")

        monkeypatch.setattr(Program, "apply", forbidden_apply)
        duplicate = store.admit("program-1", "activate-once", activate)
        assert duplicate == prior_result
        assert duplicate.status is ProgramStatus.ACTIVE
        with pytest.raises(IdempotencyConflict):
            store.admit("program-1", "activate-once", ActivateProgram(1, OWNER))
        assert len(store.history("program-1")) == 3


def test_all_remaining_k1_command_codecs_validate_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        store.create(spec(), "create")
        program = _apply(store, "activate", ActivateProgram(0, OWNER))
        program = _apply(store, "pause", PauseProgram(program.revision, OWNER))
        program = _apply(store, "resume", ResumeProgram(program.revision, OWNER))
        program = _apply(
            store,
            "prepare-first",
            PrepareAttempt(program.revision, WORKER, _attempt(program, "attempt-1", "first")),
        )
        program = _apply(store, "start-first", StartAttempt(program.revision, WORKER, "attempt-1"))
        program = _apply(store, "fail-first", FailAttempt(program.revision, WORKER, "attempt-1"))
        program = _apply(
            store,
            "prepare-retry",
            PrepareAttempt(program.revision, WORKER, _attempt(program, "attempt-2", "first")),
        )
        expected = _apply(store, "cancel-retry", CancelAttempt(program.revision, OWNER, "attempt-2"))

    with SQLiteProgramStore(path) as reopened:
        assert reopened.load("program-1") == expected
        assert [record.command_type for record in reopened.history("program-1")] == [
            "create_program",
            "activate_program",
            "pause_program",
            "resume_program",
            "prepare_attempt",
            "start_attempt",
            "fail_attempt",
            "prepare_attempt",
            "cancel_attempt",
        ]


def test_history_dedupe_and_projections_roll_back_as_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(tmp_path / "kernel.sqlite3") as store:
        initial = store.create(spec(), "create")
        original_writer = _projection_writer(store)

        def fail_projection(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("injected projection failure")

        monkeypatch.setattr(store, "_write_projections", fail_projection)
        with pytest.raises(RuntimeError, match="injected projection failure"):
            store.admit("program-1", "activate", ActivateProgram(initial.revision, OWNER))
        assert store.load("program-1") == initial
        assert len(store.history("program-1")) == 1
        assert (
            _connection(store)
            .execute("SELECT COUNT(*) FROM command_admissions WHERE command_key = 'activate'")
            .fetchone()[0]
            == 0
        )

        monkeypatch.setattr(store, "_write_projections", original_writer)
        accepted = store.admit("program-1", "activate", ActivateProgram(initial.revision, OWNER))
        assert accepted.status is ProgramStatus.ACTIVE
        assert len(store.history("program-1")) == 2


def test_projection_rebuild_decodes_versioned_facts_without_replaying_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(tmp_path / "kernel.sqlite3") as store:
        expected = _complete_and_cancel(store)
        _connection(store).execute("DELETE FROM program_projection")

        def forbidden_apply(self: Program, command: object) -> Program:
            del self, command
            raise AssertionError("projection recovery must not replay domain commands")

        monkeypatch.setattr(Program, "apply", forbidden_apply)
        assert store.rebuild_projections() == 1
        assert store.load("program-1") == expected


def test_schema_zero_migrates_and_startup_evidence_reports_runtime_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.close()

    with SQLiteProgramStore(path, busy_timeout_ms=1234) as store:
        evidence = store.startup_evidence
        assert evidence.sqlite_runtime_version == sqlite3.sqlite_version
        assert evidence.sqlite_runtime_version_info == sqlite3.sqlite_version_info
        assert evidence.sqlite_threadsafety == sqlite3.threadsafety
        assert evidence.compile_options
        assert evidence.filesystem_type
        assert evidence.filesystem_mountpoint
        assert evidence.journal_mode == "wal"
        assert evidence.synchronous == 2
        assert evidence.foreign_keys
        assert evidence.busy_timeout_ms == 1234
        assert evidence.schema_version == 1
        assert "one process lock" in evidence.controller_topology
        assert _connection(store).execute("PRAGMA user_version").fetchone()[0] == 1
        with pytest.raises(ConcurrentWriter):
            SQLiteProgramStore(path)


def test_store_enforces_its_dedicated_writer_thread(tmp_path: Path) -> None:
    store = SQLiteProgramStore(tmp_path / "kernel.sqlite3")
    errors: list[BaseException] = []

    def load_from_other_thread() -> None:
        try:
            store.load("program-1")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=load_from_other_thread)
    thread.start()
    thread.join()
    store.close()
    assert len(errors) == 1
    assert isinstance(errors[0], WrongWriterThread)


def test_online_backup_bundle_preserves_history_and_has_a_verifiable_manifest(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(database) as store:
        expected = _complete_and_cancel(store)
        history = store.history("program-1")
        exported = json.loads(store.export_history("program-1"))
        assert exported["schema_version"] == 1
        assert len(exported["records"]) == len(history)
        bundle = store.backup(tmp_path / "backup")

    manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
    assert manifest["format"] == "creatidy-kernel-sqlite-backup"
    assert manifest["schema_version"] == 1
    assert manifest["history_record_count"] == len(history)
    assert manifest["source_filesystem_type"]
    assert manifest["database_sha256"] == hashlib.sha256(bundle.database.read_bytes()).hexdigest()
    assert manifest["database_sha256"] == bundle.sha256

    with SQLiteProgramStore(bundle.database) as backup_store:
        assert backup_store.load("program-1") == expected
        assert backup_store.history("program-1") == history
        previous = backup_store.admit("program-1", "cancel-program", CancelProgram(8, OWNER))
        assert previous == expected
        assert len(backup_store.history("program-1")) == len(history)


def test_codec_rejects_unknown_versions_and_append_only_history_rejects_mutation(tmp_path: Path) -> None:
    initial = Program.create(spec())
    payload = json.loads(program_json(initial))
    payload["schema_version"] = CODEC_VERSION + 1
    with pytest.raises(RecordCodecError, match="unsupported"):
        program_from_json(json.dumps(payload))

    with SQLiteProgramStore(tmp_path / "kernel.sqlite3") as store:
        store.create(spec(), "create")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            _connection(store).execute("UPDATE history SET actor_id = 'forged' WHERE sequence = 1")


def test_noop_amendment_is_recorded_in_history_without_inventing_k1_revision(tmp_path: Path) -> None:
    with SQLiteProgramStore(tmp_path / "kernel.sqlite3") as store:
        initial = store.create(spec(), "create")
        unchanged = store.admit(
            "program-1",
            "noop-amendment",
            AmendProgramSpec(
                initial.revision, OWNER, SpecAmendment(initial.spec.revision, objective=initial.spec.objective)
            ),
        )
        assert unchanged == initial
        assert unchanged.revision == initial.revision
        assert unchanged.spec_history == initial.spec_history
        records = store.history("program-1")
        assert [item.sequence for item in records] == [1, 2]
        assert [item.aggregate_revision for item in records] == [0, 0]


def test_store_rejects_non_file_backed_database_paths(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedSQLiteConfiguration, match="file-backed"):
        SQLiteProgramStore(":memory:")


def test_missing_append_only_guard_fails_closed_on_reopen(tmp_path: Path) -> None:
    path = tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        store.create(spec(), "create")
        connection = _connection(store)
        connection.execute("DROP TRIGGER history_no_update")
        connection.execute("UPDATE history SET facts_json = '{}' WHERE sequence = 1")

    with pytest.raises(UnsupportedSQLiteConfiguration, match="append-only"):
        SQLiteProgramStore(path)
