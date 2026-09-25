# SPDX-License-Identifier: Apache-2.0
"""Deterministic persistence, recovery, and command-admission regressions."""

import ctypes
import errno
import hashlib
import json
import os
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import creatidy_kernel.adapters.sqlite_store as sqlite_store_module
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


@pytest.fixture
def sqlite_tmp_path(tmp_path: Path) -> Iterator[Path]:
    configured_root = os.environ.get("CREATIDY_TEST_STORAGE_DIR")
    if configured_root is None:
        yield tmp_path
        return
    storage_root = Path(configured_root).expanduser()
    storage_root.mkdir(parents=True, exist_ok=True)
    test_directory = Path(tempfile.mkdtemp(prefix="creatidy-kernel-", dir=storage_root))
    try:
        yield test_directory
    finally:
        shutil.rmtree(test_directory)


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


def _spec_with_permutable_collections() -> ProgramSpec:
    base = spec()
    first, second = base.work_units
    first_with_two_inputs = WorkUnit(
        work_unit_id=first.work_unit_id,
        obligation=first.obligation,
        dependencies=first.dependencies,
        required_inputs=frozenset({"seed", "seed-2"}),
        outputs=first.outputs,
        acceptance_criteria=first.acceptance_criteria,
        acceptance_policy_reference=first.acceptance_policy_reference,
    )
    return ProgramSpec(
        program_id=base.program_id,
        objective=base.objective,
        work_units=(second, first_with_two_inputs),
        initial_inputs=(InputBinding("seed-2", "approved:seed-2"), base.initial_inputs[0]),
        budget=base.budget,
        authority=base.authority,
        revision=base.revision,
        parent_digest=base.parent_digest,
        acceptance_criteria=("the Program evidence is complete", "the Program result is complete"),
        policy_references=(PolicyReference("safety", "v1", "sha256:safety"), *base.policy_references),
    )


def _mountinfo_record(
    mount_id: int,
    parent_id: int,
    device: str,
    root: str | bytes,
    mountpoint: str | bytes,
    filesystem_type: str,
    source: str | bytes,
    *,
    mount_options: str = "rw,relatime",
    super_options: str = "rw",
) -> bytes:
    def escape(value: str | bytes) -> bytes:
        raw = os.fsencode(value) if isinstance(value, str) else value
        result = bytearray()
        for item in raw:
            if item == 0x5C:
                result.extend(b"\\134")
            elif item == 0x20:
                result.extend(b"\\040")
            elif item == 0x09:
                result.extend(b"\\011")
            elif item == 0x0A:
                result.extend(b"\\012")
            else:
                result.append(item)
        return bytes(result)

    before = b" ".join(
        (
            str(mount_id).encode("ascii"),
            str(parent_id).encode("ascii"),
            device.encode("ascii"),
            escape(root),
            escape(mountpoint),
            mount_options.encode("ascii"),
        )
    )
    after = b" ".join((filesystem_type.encode("ascii"), escape(source), super_options.encode("ascii")))
    return before + b" - " + after


def _parse_mount_records(records: tuple[bytes, ...]) -> dict[int, object]:
    parser_name = "_parse_mountinfo"
    parser = cast(Callable[[bytes], dict[int, object]], getattr(sqlite_store_module, parser_name))
    return parser(b"\n".join(records))


def _topology_for_mount_ids(
    data_directory: Path,
    directory_mount_id: int,
    child_mount_ids: dict[str, int],
    mounts: dict[int, object],
) -> object:
    validator_name = "_topology_from_mount_ids"
    validator = cast(Callable[..., object], getattr(sqlite_store_module, validator_name))
    return validator(
        data_directory.resolve(),
        directory_mount_id=directory_mount_id,
        child_mount_ids=child_mount_ids,
        mounts=mounts,
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


def test_round_trip_preserves_all_final_k1_facts_and_actor_authority_order(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
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
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
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


def test_all_remaining_k1_command_codecs_validate_on_reopen(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
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
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
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
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        expected = _complete_and_cancel(store)
        _connection(store).execute("DELETE FROM program_projection")

        def forbidden_apply(self: Program, command: object) -> Program:
            del self, command
            raise AssertionError("projection recovery must not replay domain commands")

        monkeypatch.setattr(Program, "apply", forbidden_apply)
        assert store.rebuild_projections() == 1
        assert store.load("program-1") == expected


def test_schema_zero_migrates_and_startup_evidence_reports_runtime_capabilities(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 0")
    connection.close()

    with SQLiteProgramStore(path, busy_timeout_ms=1234) as store:
        evidence = store.startup_evidence
        assert evidence.sqlite_runtime_version == sqlite3.sqlite_version
        assert evidence.sqlite_runtime_version_info == sqlite3.sqlite_version_info
        assert evidence.sqlite_threadsafety == sqlite3.threadsafety
        assert evidence.compile_options
        assert evidence.data_directory == str(path.parent)
        assert evidence.cache_mode == "private"
        assert evidence.filesystem_type
        assert evidence.filesystem_mountpoint
        assert evidence.filesystem_mount_id > 0
        assert evidence.locking_mode == "exclusive"
        assert evidence.journal_mode == "wal"
        assert evidence.synchronous == 2
        assert evidence.foreign_keys
        assert evidence.busy_timeout_ms == 1234
        assert evidence.schema_version == 1
        assert "locking_mode=EXCLUSIVE" in evidence.controller_topology
        assert evidence.writer_process_id == os.getpid()
        assert not Path(f"{path}.writer.lock").exists()
        assert _connection(store).execute("PRAGMA user_version").fetchone()[0] == 1
        with pytest.raises(ConcurrentWriter):
            SQLiteProgramStore(path, busy_timeout_ms=25)


def test_store_enforces_its_dedicated_writer_thread(sqlite_tmp_path: Path) -> None:
    store = SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3")
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


def test_terminated_creator_thread_token_survives_identifier_reuse(sqlite_tmp_path: Path) -> None:
    database = sqlite_tmp_path / "thread-owner.sqlite3"
    script = textwrap.dedent(
        """
        import json
        import os
        import sys
        import threading
        from pathlib import Path

        from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
        from creatidy_kernel.core.domain import AuthorityEnvelope, PolicyReference, ProgramSpec, WorkUnit

        holder = []

        def create_store():
            spec = ProgramSpec(
                program_id="thread-owner",
                objective="verify creator-thread identity",
                work_units=(WorkUnit("unit", "remain durable", acceptance_criteria=("state is valid",)),),
                acceptance_criteria=("ownership is bound to one thread",),
                policy_references=(PolicyReference("thread-test", "v1", "sha256:thread-test"),),
                authority=AuthorityEnvelope("owner"),
            )
            store = SQLiteProgramStore(Path(sys.argv[1]))
            store.create(spec, "create")
            holder.append((store, store.startup_evidence.writer_thread_id))

        creator = threading.Thread(target=create_store)
        creator.start()
        creator.join()
        store, creator_ident = holder[0]
        native_get_ident = threading.get_ident
        outcomes = []

        def access_from_replacement_thread():
            threading.get_ident = lambda: creator_ident
            try:
                for operation in (lambda: store.load("thread-owner"), store.close):
                    try:
                        operation()
                    except Exception as error:
                        outcomes.append(type(error).__name__)
                    else:
                        outcomes.append("allowed")
            finally:
                threading.get_ident = native_get_ident
            print(json.dumps(outcomes), flush=True)
            os._exit(0)

        replacement = threading.Thread(target=access_from_replacement_thread)
        replacement.start()
        replacement.join()
        os._exit(1)
        """
    )
    # Fixed local regression harness; no command or script content comes from PR input.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(database)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["WrongWriterThread", "WrongWriterThread"]


def test_store_explicitly_forces_private_cache_against_shared_cache_peer(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    real_connect = sqlite3.connect
    observed: list[tuple[str, bool]] = []

    def capture_connect(database: str | Path, **kwargs: object) -> sqlite3.Connection:
        observed.append((str(database), bool(kwargs.get("uri"))))
        connector = cast(Callable[..., sqlite3.Connection], real_connect)
        return connector(database, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_store_module.sqlite3, "connect", capture_connect)
        with SQLiteProgramStore(path) as store:
            store.create(spec(), "create")
            assert store.startup_evidence.cache_mode == "private"
            peer: sqlite3.Connection | None = None
            try:
                with pytest.raises(sqlite3.OperationalError):
                    peer = real_connect(
                        f"{path.as_uri()}?cache=shared",
                        timeout=0.05,
                        isolation_level=None,
                        uri=True,
                    )
                    peer.execute("BEGIN EXCLUSIVE")
            finally:
                if peer is not None:
                    peer.close()

    assert observed == [(f"{path.as_uri()}?cache=private", True)]


def test_replacement_controller_opens_after_owner_death_with_inert_child(sqlite_tmp_path: Path) -> None:
    if not sys.platform.startswith("linux") or not hasattr(os, "fork"):
        pytest.skip("requires Linux POSIX locks and fork semantics")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = cast(Callable[..., int], libc.prctl)
    previous_subreaper = ctypes.c_int()
    if prctl(37, ctypes.byref(previous_subreaper), 0, 0, 0) != 0:
        pytest.skip("PR_GET_CHILD_SUBREAPER is unavailable")
    if prctl(36, 1, 0, 0, 0) != 0:
        pytest.skip("cannot configure test process as child subreaper")

    database = sqlite_tmp_path / "kernel.sqlite3"
    read_fd, write_fd = os.pipe()
    controller: subprocess.Popen[bytes] | None = None
    child_pid: int | None = None
    child_reaped = False
    script = textwrap.dedent(
        """
        import os
        import signal
        import sys
        from pathlib import Path

        from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore, WrongWriterProcess
        from creatidy_kernel.core.domain import (
            ActivateProgram,
            AuthorityEnvelope,
            PolicyReference,
            ProgramSpec,
            WorkUnit,
        )

        database = Path(sys.argv[1])
        ready_fd = int(sys.argv[2])
        program_spec = ProgramSpec(
            program_id="process-test",
            objective="verify replacement ownership",
            work_units=(WorkUnit("unit", "remain durable", acceptance_criteria=("state is recoverable",)),),
            acceptance_criteria=("committed facts survive controller death",),
            policy_references=(PolicyReference("process-test", "v1", "sha256:process-test"),),
            authority=AuthorityEnvelope("owner"),
        )
        store = SQLiteProgramStore(database, busy_timeout_ms=1000)
        store.create(program_spec, "controller-create")
        child_pid = os.fork()
        if child_pid == 0:
            try:
                store.close()
            except WrongWriterProcess:
                pass
            else:
                os._exit(20)
            os.write(ready_fd, f"{os.getpid()}\\n".encode())
            os.close(ready_fd)
            while True:
                signal.pause()
        os.close(ready_fd)
        while True:
            signal.pause()
        """
    )
    try:
        # Fixed synthetic harness: no command or script content comes from PR input.
        controller = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", script, str(database), str(write_fd)],
            pass_fds=(write_fd,),
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        os.close(write_fd)
        write_fd = -1
        readable, _, _ = select.select((read_fd,), (), (), 10)
        assert readable, "controller did not start its inert forked child"
        payload = os.read(read_fd, 64)
        if not payload:
            _, stderr = controller.communicate(timeout=5)
            pytest.fail(f"controller exited before starting its child: {stderr.decode('utf-8', errors='replace')}")
        child_pid = int(payload.decode("ascii").strip())
        assert controller.poll() is None

        controller.kill()
        controller.wait(timeout=10)
        os.kill(child_pid, 0)

        with SQLiteProgramStore(database, busy_timeout_ms=500) as replacement:
            recovered = replacement.load("process-test")
            assert recovered.status is ProgramStatus.DRAFT
            active = replacement.admit("process-test", "replacement-activate", ActivateProgram(0, "owner"))
            assert active.status is ProgramStatus.ACTIVE
            assert replacement.startup_evidence.locking_mode == "exclusive"

        os.kill(child_pid, signal.SIGKILL)
        waited_pid, status = os.waitpid(child_pid, 0)
        child_reaped = True
        assert waited_pid == child_pid
        assert os.WIFSIGNALED(status)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if read_fd >= 0:
            os.close(read_fd)
        if controller is not None and controller.poll() is None:
            controller.kill()
            controller.wait(timeout=10)
        if child_pid is not None and not child_reaped:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
        prctl(36, previous_subreaper.value, 0, 0, 0)


def test_online_backup_bundle_preserves_history_and_has_a_verifiable_manifest(sqlite_tmp_path: Path) -> None:
    database = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(database) as store:
        expected = _complete_and_cancel(store)
        history = store.history("program-1")
        exported = json.loads(store.export_history("program-1"))
        assert exported["schema_version"] == 1
        assert len(exported["records"]) == len(history)
        bundle = store.backup(sqlite_tmp_path / "backup")

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


def test_online_backup_syncs_bundle_before_and_after_atomic_publish(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracked_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def tracked_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(sqlite_store_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(sqlite_store_module.os, "replace", tracked_replace)
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        store.create(spec(), "create")
        bundle = store.backup(sqlite_tmp_path / "backup")

    assert events == ["fsync", "fsync", "fsync", "replace", "fsync"]
    assert bundle.database.is_file()
    assert bundle.manifest.is_file()


def test_online_backup_failure_before_publish_leaves_no_bundle(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    sync_count = 0

    def fail_manifest_sync(descriptor: int) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            raise OSError("injected manifest sync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(sqlite_store_module.os, "fsync", fail_manifest_sync)
    destination = sqlite_tmp_path / "backup"
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        store.create(spec(), "create")
        with pytest.raises(OSError, match="injected manifest sync failure"):
            store.backup(destination)
    assert not destination.exists()


def test_whole_directory_bind_mount_is_accepted_with_unicode_mountpoint() -> None:
    data_directory = Path("/synthetic/data directory\u00a0\u0085\u2028\u2029")
    raw_mountpoint = os.fsencode(data_directory)
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(7, 1, "8:1", b"/persistent", raw_mountpoint, "ext4", b"/dev/root"),
        )
    )
    topology = _topology_for_mount_ids(data_directory, 7, {"database": 7}, mounts)

    assert cast(Any, topology).mount_id == 7
    assert cast(Any, topology).data_directory == data_directory
    assert os.fsencode(cast(Any, topology).mountpoint) == raw_mountpoint


def test_hidden_deeper_native_mount_does_not_override_actual_visible_mount() -> None:
    data_directory = Path("/synthetic/srv/app/data")
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(5, 1, "8:1", b"/apps", b"/synthetic/srv/app", "ext4", b"/dev/root"),
            _mountinfo_record(7, 5, "8:1", b"/apps/data", b"/synthetic/srv/app/data", "ext4", b"/dev/root"),
            _mountinfo_record(8, 5, "0:42", b"/", b"/synthetic/srv/app", "tmpfs", b"tmpfs"),
        )
    )

    with pytest.raises(UnsupportedSQLiteConfiguration, match="native local filesystem"):
        _topology_for_mount_ids(data_directory, 8, {"database": 8}, mounts)


def test_file_only_database_bind_mount_is_rejected_even_on_same_device() -> None:
    data_directory = Path("/synthetic/database-directory")
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(7, 1, "8:1", b"/persistent", b"/synthetic/database-directory", "ext4", b"/dev/root"),
            _mountinfo_record(
                8,
                7,
                "8:1",
                b"/persistent/kernel.sqlite3",
                b"/synthetic/database-directory/kernel.sqlite3",
                "ext4",
                b"/dev/root",
            ),
        )
    )

    with pytest.raises(UnsupportedSQLiteConfiguration, match="different mount"):
        _topology_for_mount_ids(data_directory, 7, {"database": 8}, mounts)


def test_separately_mounted_wal_sidecar_is_rejected_even_on_same_device() -> None:
    data_directory = Path("/synthetic/database-directory")
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(7, 1, "8:1", b"/persistent", b"/synthetic/database-directory", "ext4", b"/dev/root"),
            _mountinfo_record(
                8,
                7,
                "8:1",
                b"/persistent/kernel.sqlite3-wal",
                b"/synthetic/database-directory/kernel.sqlite3-wal",
                "ext4",
                b"/dev/root",
            ),
        )
    )

    with pytest.raises(UnsupportedSQLiteConfiguration, match="different mount"):
        _topology_for_mount_ids(data_directory, 7, {"database": 7, "sidecar -wal": 8}, mounts)


@pytest.mark.parametrize("filesystem_type", ("tmpfs", "ramfs", "overlay", "nfs4"))
def test_volatile_and_remote_mount_ids_are_rejected(filesystem_type: str) -> None:
    data_directory = Path("/synthetic/database-directory")
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(
                7,
                1,
                "0:42",
                b"/",
                b"/synthetic/database-directory",
                filesystem_type,
                filesystem_type.encode("ascii"),
                super_options="rw,upperdir=/upper,workdir=/work,volatile",
            ),
        )
    )

    with pytest.raises(UnsupportedSQLiteConfiguration, match="native local filesystem"):
        _topology_for_mount_ids(data_directory, 7, {"database": 7}, mounts)


def test_native_mount_with_volatile_sync_option_is_rejected() -> None:
    data_directory = Path("/synthetic/database-directory")
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(
                7,
                1,
                "8:1",
                b"/persistent",
                b"/synthetic/database-directory",
                "ext4",
                b"/dev/root",
                super_options="rw,errors=remount-ro,fsync=volatile",
            ),
        )
    )

    with pytest.raises(UnsupportedSQLiteConfiguration, match="volatile filesystem mode"):
        _topology_for_mount_ids(data_directory, 7, {}, mounts)


@pytest.mark.parametrize(
    "checkpoint_option",
    ("checkpoint=disable", "checkpoint=disable:8192", "checkpoint=disable:5%"),
)
@pytest.mark.parametrize("option_location", ("mount", "super"))
def test_checkpoint_disabled_f2fs_is_rejected_from_mountinfo_options(
    checkpoint_option: str, option_location: str
) -> None:
    data_directory = Path("/synthetic/f2fs-database-directory")
    mount_options = "rw,relatime"
    super_options = "rw"
    if option_location == "mount":
        mount_options = f"{mount_options},{checkpoint_option}"
    else:
        super_options = f"{super_options},{checkpoint_option}"
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(
                7,
                1,
                "8:1",
                b"/persistent",
                b"/synthetic/f2fs-database-directory",
                "f2fs",
                b"/dev/f2fs",
                mount_options=mount_options,
                super_options=super_options,
            ),
        )
    )

    with pytest.raises(UnsupportedSQLiteConfiguration, match="checkpoint-disabled F2FS"):
        _topology_for_mount_ids(data_directory, 7, {"database": 7}, mounts)


@pytest.mark.parametrize("checkpoint_option", (None, "checkpoint=enable"))
def test_default_or_checkpoint_enabled_f2fs_remains_supported(checkpoint_option: str | None) -> None:
    data_directory = Path("/synthetic/f2fs-database-directory")
    super_options = "rw" if checkpoint_option is None else f"rw,{checkpoint_option}"
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(
                7,
                1,
                "8:1",
                b"/persistent",
                b"/synthetic/f2fs-database-directory",
                "f2fs",
                b"/dev/f2fs",
                super_options=super_options,
            ),
        )
    )

    topology = _topology_for_mount_ids(data_directory, 7, {"database": 7}, mounts)
    assert cast(Any, topology).filesystem_type == "f2fs"


def test_mountinfo_parser_preserves_non_utf8_path_bytes() -> None:
    raw_mountpoint = b"/synthetic/non-utf8-\xff"
    mounts = _parse_mount_records(
        (
            _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root"),
            _mountinfo_record(7, 1, "8:1", b"/persistent", raw_mountpoint, "ext4", b"/dev/root"),
        )
    )
    topology = _topology_for_mount_ids(Path(os.fsdecode(raw_mountpoint)), 7, {}, mounts)
    assert os.fsencode(cast(Any, topology).mountpoint) == raw_mountpoint


def test_mountinfo_rejects_duplicate_and_missing_mount_ids() -> None:
    record = _mountinfo_record(1, 0, "8:1", b"/", b"/", "ext4", b"/dev/root")
    with pytest.raises(UnsupportedSQLiteConfiguration, match="duplicate mount ID"):
        _parse_mount_records((record, record))

    mounts = _parse_mount_records((record,))
    with pytest.raises(UnsupportedSQLiteConfiguration, match="absent from mountinfo"):
        _topology_for_mount_ids(Path("/synthetic/database-directory"), 999, {}, mounts)


def test_storage_probe_rejects_dangling_sidecar_symlink_and_closes_probes(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = sqlite_tmp_path / "kernel.sqlite3"
    Path(f"{database}-wal").symlink_to(sqlite_tmp_path / "missing-wal-target")
    real_open = os.open
    opened_descriptors: list[int] = []

    def tracked_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened_descriptors.append(descriptor)
        return descriptor

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_store_module.os, "open", tracked_open)
        with pytest.raises(UnsupportedSQLiteConfiguration, match="regular single-link file"):
            SQLiteProgramStore(database)

    for descriptor in opened_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_storage_probe_rejects_hardlinked_database_and_special_sidecar(sqlite_tmp_path: Path) -> None:
    database = sqlite_tmp_path / "hardlinked.sqlite3"
    database.write_bytes(b"placeholder")
    os.link(database, sqlite_tmp_path / "database-alias")
    with pytest.raises(UnsupportedSQLiteConfiguration, match="regular single-link file"):
        SQLiteProgramStore(database)

    fifo_database = sqlite_tmp_path / "fifo.sqlite3"
    os.mkfifo(f"{fifo_database}-wal")
    with pytest.raises(UnsupportedSQLiteConfiguration, match="regular single-link file"):
        SQLiteProgramStore(fifo_database)


def test_storage_probe_does_not_treat_permission_error_as_missing_sidecar(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = sqlite_tmp_path / "kernel.sqlite3"
    real_open = os.open

    def deny_wal(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == b"kernel.sqlite3-wal" and dir_fd is not None:
            raise PermissionError(errno.EACCES, "injected WAL probe denial")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(sqlite_store_module.os, "open", deny_wal)
    with pytest.raises(UnsupportedSQLiteConfiguration, match="cannot inspect SQLite sidecar -wal"):
        SQLiteProgramStore(database)


def test_reordered_semantically_identical_amendment_reuses_command_result(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial = store.create(_spec_with_permutable_collections(), "create")
        current = initial.spec
        amendment = SpecAmendment(
            expected_revision=current.revision,
            work_units=current.work_units,
            initial_inputs=current.initial_inputs,
            reason="normalize order for deduplication",
            acceptance_criteria=current.acceptance_criteria,
            policy_references=current.policy_references,
        )
        reordered_amendment = SpecAmendment(
            expected_revision=current.revision,
            work_units=tuple(reversed(current.work_units)),
            initial_inputs=tuple(reversed(current.initial_inputs)),
            reason="normalize order for deduplication",
            acceptance_criteria=tuple(reversed(current.acceptance_criteria)),
            policy_references=tuple(reversed(current.policy_references)),
        )
        first_result = store.admit(
            "program-1", "reordered-amendment", AmendProgramSpec(initial.revision, OWNER, amendment)
        )
        assert first_result == initial

        def forbidden_apply(self: Program, command: DomainCommandType) -> Program:
            del self, command
            raise AssertionError("normalized duplicate amendment must not be applied again")

        monkeypatch.setattr(Program, "apply", forbidden_apply)
        duplicate = store.admit(
            "program-1",
            "reordered-amendment",
            AmendProgramSpec(initial.revision, OWNER, reordered_amendment),
        )
        assert duplicate == first_result
        assert len(store.history("program-1")) == 2


def test_codec_rejects_unknown_versions_and_append_only_history_rejects_mutation(sqlite_tmp_path: Path) -> None:
    initial = Program.create(spec())
    payload = json.loads(program_json(initial))
    payload["schema_version"] = CODEC_VERSION + 1
    with pytest.raises(RecordCodecError, match="unsupported"):
        program_from_json(json.dumps(payload))

    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        store.create(spec(), "create")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            _connection(store).execute("UPDATE history SET actor_id = 'forged' WHERE sequence = 1")


def test_rehydration_rejects_unreferenced_and_out_of_order_terminal_facts() -> None:
    initial = Program.create(spec())
    payload = json.loads(program_json(initial))
    payload["value"]["attempt_cancellations"].append(
        {"attempt_id": "missing-attempt", "actor_id": OWNER, "reason": "test", "record_order": 1}
    )
    with pytest.raises(RecordCodecError, match="existing cancelled Attempt"):
        program_from_json(json.dumps(payload))

    payload = json.loads(program_json(initial))
    payload["value"]["conclusions"].append(
        {
            "status": ProgramStatus.COMPLETED.value,
            "spec_revision": 999,
            "spec_digest": "sha256:missing-spec",
            "record_order": 999,
        }
    )
    with pytest.raises(RecordCodecError, match="historical ProgramSpec"):
        program_from_json(json.dumps(payload))

    payload = json.loads(program_json(initial))
    payload["value"]["conclusions"].append(
        {
            "status": ProgramStatus.COMPLETED.value,
            "spec_revision": initial.spec.revision,
            "spec_digest": initial.spec.digest,
            "record_order": 999,
        }
    )
    with pytest.raises(RecordCodecError, match="authoritative aggregate record order"):
        program_from_json(json.dumps(payload))

    active = initial.apply(ActivateProgram(initial.revision, OWNER))
    prepared = active.apply(PrepareAttempt(active.revision, WORKER, _attempt(active, "attempt-1", "first")))
    payload = json.loads(program_json(prepared))
    payload["value"]["attempt_cancellations"].append(
        {"attempt_id": "attempt-1", "actor_id": OWNER, "reason": "test", "record_order": prepared.revision}
    )
    with pytest.raises(RecordCodecError, match="cancelled Attempt"):
        program_from_json(json.dumps(payload))


def test_noop_amendment_is_recorded_in_history_without_inventing_k1_revision(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
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


def test_missing_append_only_guard_fails_closed_on_reopen(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        store.create(spec(), "create")
        connection = _connection(store)
        connection.execute("DROP TRIGGER history_no_update")
        connection.execute("UPDATE history SET facts_json = '{}' WHERE sequence = 1")

    with pytest.raises(UnsupportedSQLiteConfiguration, match="append-only"):
        SQLiteProgramStore(path)
