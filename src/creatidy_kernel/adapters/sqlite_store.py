# SPDX-License-Identifier: Apache-2.0
"""Single-controller SQLite persistence for versioned K1 facts and projections."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.boot_clock import read_boot_clock
from creatidy_kernel.adapters.sqlite_codec import (
    CODEC_VERSION,
    RecordCodecError,
    attempt_projection_json,
    canonical_json,
    command_input_json,
    create_input_json,
    digest_json,
    program_from_json,
    program_json,
    validate_create_input,
    validate_history_input,
)
from creatidy_kernel.core.deadlines import BootClockReading, DeadlineProjection
from creatidy_kernel.core.domain import (
    AmendProgramSpec,
    AttemptSpec,
    AttemptStatus,
    DomainCommandType,
    FinishAttempt,
    PrepareAttempt,
    Program,
    ProgramSpec,
    ProgramStatus,
    SatisfyWorkUnit,
    StartAttempt,
    WorkUnitStatus,
)
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.ownership import (
    COOPERATIVE_PROFILE,
    HandoffMode,
    HandoffProof,
    OwnershipConflict,
    ResourceScope,
    WorkClaim,
)
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.program_store import OperationRecord, ProgramReadChanged

SCHEMA_VERSION = 5
_APPLICATION_ID = 0x43544B31
_DEFAULT_BUSY_TIMEOUT_MS = 5_000
_LOCAL_FILESYSTEMS = {
    "bcachefs",
    "btrfs",
    "ext2",
    "ext3",
    "ext4",
    "f2fs",
    "ntfs3",
    "xfs",
    "zfs",
}


class SQLiteStoreError(RuntimeError):
    """Base class for SQLite store failures."""


class UnsupportedSQLiteConfiguration(SQLiteStoreError):
    """The runtime cannot provide the configured durability contract."""


class ConcurrentWriter(SQLiteStoreError):
    """Another controller already owns this database's exclusive SQLite connection."""


class WrongWriterThread(SQLiteStoreError):
    """SQLite access was attempted outside the store's dedicated writer thread."""


class WrongWriterProcess(SQLiteStoreError):
    """SQLite access was attempted from a forked or otherwise different process."""


class StoreClosed(SQLiteStoreError):
    """The store has already been closed."""


class ProgramNotFound(SQLiteStoreError):
    """No durable Program exists for the requested ID."""


class ProgramAlreadyExists(SQLiteStoreError):
    """A Program ID has already been initialized."""


class IdempotencyConflict(SQLiteStoreError):
    """A command key was reused for different canonical input."""


class CorruptHistory(SQLiteStoreError):
    """Durable history or its verified projection is inconsistent."""


class OperationConflict(SQLiteStoreError):
    """An operation identity, fence, receipt, or retry violates the journal contract."""


_OBSERVATION_KINDS = frozenset({"accepted", "rejected", "running", "waiting", "terminal", "unknown"})


@dataclass(frozen=True, slots=True)
class SQLiteStartupEvidence:
    database_path: str
    data_directory: str
    cache_mode: str
    filesystem_type: str
    filesystem_mountpoint: str
    filesystem_mount_id: int
    filesystem_mount_options: tuple[str, ...]
    filesystem_super_options: tuple[str, ...]
    sqlite_runtime_version: str
    sqlite_runtime_version_info: tuple[int, int, int]
    sqlite_threadsafety: int
    compile_options: tuple[str, ...]
    journal_mode: str
    locking_mode: str
    synchronous: int
    foreign_keys: bool
    busy_timeout_ms: int
    application_id: int
    schema_version: int
    controller_topology: str
    writer_process_id: int
    writer_thread_id: int


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    program_id: str
    sequence: int
    schema_version: int
    command_key: str
    command_type: str
    actor_id: str
    input_digest: str
    input_json: str
    aggregate_revision: int
    facts_digest: str
    facts_json: str


@dataclass(frozen=True, slots=True)
class BackupBundle:
    directory: Path
    database: Path
    manifest: Path
    sha256: str
    history_record_count: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class _MountEntry:
    mount_id: int
    parent_id: int
    device: bytes
    root: bytes
    mountpoint: bytes
    mount_options: frozenset[bytes]
    filesystem_type: str
    source: bytes
    super_options: frozenset[bytes]


@dataclass(frozen=True, slots=True)
class _StorageTopology:
    data_directory: Path
    filesystem_type: str
    mountpoint: Path
    mount_id: int
    mount_options: tuple[str, ...]
    super_options: tuple[str, ...]


class SQLiteProgramStore:
    """A file-backed K1 store with one SQLite-exclusive connection and one owner process/thread."""

    _connection: sqlite3.Connection
    _startup_evidence: SQLiteStartupEvidence

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
        boot_clock: Callable[[], BootClockReading | None] = read_boot_clock,
    ) -> None:
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        raw_path = str(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise UnsupportedSQLiteConfiguration("the store requires a file-backed local SQLite database path")
        self._path = Path(path).expanduser().resolve(strict=False)
        self._artifact_directory = self._path.parent / f"{self._path.name}.artifacts"
        if not self._path.parent.is_dir():
            raise FileNotFoundError(f"SQLite database parent directory does not exist: {self._path.parent}")
        if self._path.exists() and not self._path.is_file():
            raise UnsupportedSQLiteConfiguration("SQLite database path must be a regular file")
        self._storage = _inspect_local_storage(self._path.parent, database_path=self._path)
        if self._path.name == "kernel.sqlite3" and (self._path.parent / "manifest.json").exists():
            self._verify_backup_manifest(self._path.parent)
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._owner_thread_local = threading.local()
        self._owner_thread_token = object()
        self._owner_thread_local.store_token = self._owner_thread_token
        self._gate = threading.RLock()
        self._busy_timeout_ms = busy_timeout_ms
        self._boot_clock = boot_clock
        self._clock_watermarks: dict[str, BootClockReading] = {}
        self._clock_uncommitted: dict[str, BootClockReading | None] = {}
        self._transaction_clock_readings: dict[str, BootClockReading | None] | None = None
        self._workspace_scopes: dict[str, ResourceScope | None] | None = None
        self._workspace_artifacts: dict[tuple[str, str], str] | None = None
        self._cache_mode = "private"
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                f"{self._path.as_uri()}?cache=private",
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
                uri=True,
            )
            self._connection.row_factory = sqlite3.Row
            try:
                self._configure_runtime()
                self._acquire_exclusive_ownership()
                self._assert_storage_topology_unchanged()
                self._enable_wal()
                self._assert_storage_topology_unchanged()
                self._set_application_id()
            except sqlite3.OperationalError as error:
                if _is_sqlite_lock_contention(error):
                    raise ConcurrentWriter("another controller owns SQLite EXCLUSIVE locking mode") from error
                raise
            self._startup_evidence = self._read_startup_evidence()
            self._migrate()
            self.rebuild_projections()
            self._validate_operation_records()
            self._validate_ownership_records()
            self._startup_evidence = self._read_startup_evidence()
        except BaseException:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise

    @property
    def startup_evidence(self) -> SQLiteStartupEvidence:
        self._assert_writer_thread()
        return self._startup_evidence

    def __enter__(self) -> SQLiteProgramStore:
        self._assert_writer_thread()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()

    def close(self) -> None:
        if os.getpid() != self._owner_pid:
            raise WrongWriterProcess("inherited SQLite stores must not be used or closed in another process")
        self._assert_owner_thread()
        with self._gate:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def create(self, spec: ProgramSpec, command_key: str) -> Program:
        self._assert_writer_thread()
        with self._gate, self._transaction():
            return self._create(spec, command_key)

    def _create(self, spec: ProgramSpec, command_key: str) -> Program:
        self._assert_writer_thread()
        self._validate_command_key(command_key)
        if type(spec) is not ProgramSpec:
            raise TypeError("spec must be an exact K1 ProgramSpec")
        input_json = create_input_json(spec)
        input_digest = digest_json(json.loads(input_json))
        duplicate = self._duplicate_result(command_key, input_digest)
        if duplicate is not None:
            return duplicate
        existing = self._connection.execute(
            "SELECT 1 FROM program_projection WHERE program_id = ?", (spec.program_id,)
        ).fetchone()
        if existing is not None:
            raise ProgramAlreadyExists(f"Program {spec.program_id!r} already exists")
        initial = Program.create(spec)
        facts_json = program_json(initial)
        facts_digest = hashlib.sha256(facts_json.encode("utf-8")).hexdigest()
        self._insert_history(
            program_id=spec.program_id,
            sequence=1,
            command_key=command_key,
            command_type="create_program",
            actor_id=spec.authority.owner_id,
            input_digest=input_digest,
            input_json=input_json,
            program=initial,
            facts_json=facts_json,
            facts_digest=facts_digest,
        )
        self._write_projections(initial, history_sequence=1, facts_json=facts_json, facts_digest=facts_digest)
        self._insert_command_result(
            command_key=command_key,
            program_id=spec.program_id,
            command_type="create_program",
            input_digest=input_digest,
            result_sequence=1,
            result_json=facts_json,
            result_digest=facts_digest,
        )
        return initial

    def load(self, program_id: str) -> Program:
        self._assert_writer_thread()
        self._validate_program_id(program_id)
        with self._gate:
            row = self._connection.execute(
                "SELECT history_sequence, aggregate_revision, schema_version, program_json, facts_digest "
                "FROM program_projection WHERE program_id = ?",
                (program_id,),
            ).fetchone()
            if row is None:
                raise ProgramNotFound(f"Program {program_id!r} does not exist")
            program_json_value = cast(str, row["program_json"])
            if hashlib.sha256(program_json_value.encode("utf-8")).hexdigest() != row["facts_digest"]:
                raise CorruptHistory("Program projection digest does not match its contents")
            program = self._decode_facts(program_json_value, program_id, cast(int, row["aggregate_revision"]))
            latest = self._connection.execute(
                "SELECT sequence, facts_digest FROM history WHERE program_id = ? ORDER BY sequence DESC LIMIT 1",
                (program_id,),
            ).fetchone()
            if (
                latest is None
                or latest["sequence"] != row["history_sequence"]
                or latest["facts_digest"] != row["facts_digest"]
                or row["schema_version"] != CODEC_VERSION
            ):
                raise CorruptHistory("Program projection does not match the authoritative history head")
            return program

    def find_program(self, program_id: str) -> Program | None:
        """Optional local aggregate lookup; corruption and access errors are not absence."""
        try:
            return self.load(program_id)
        except ProgramNotFound:
            return None

    def program_read_cut[T](
        self, program_id: str, read: Callable[[Program], T], clock: Callable[[], int]
    ) -> tuple[Program, T, int]:
        """Finish local reads before time sampling; callbacks may not invalidate the cut.

        Existing exclusive connection/writer-thread ownership rules out another
        store writer. SQLite's cached total_changes detects reentrant callback
        writes (including cancellation) without a database read after the clock.
        This is not an atomic remote snapshot or a hostile same-UID boundary.
        """
        self._assert_writer_thread()
        with self._gate:
            changes = self._connection.total_changes
            program = self.load(program_id)
            value = read(program)
            now = clock()
            if self._closed or self._connection.total_changes != changes:
                raise ProgramReadChanged("final store read cut changed during a callback")
            return program, value, now

    def admit(self, program_id: str, command_key: str, command: DomainCommandType) -> Program:
        return self._admit(program_id, command_key, command, intent=None)

    def admit_with_intent(
        self,
        program_id: str,
        command_key: str,
        command: DomainCommandType,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
    ) -> Program:
        """Atomically admit a K1 decision and its K2B logical effect/outbox."""
        self._validate_command_key(operation_id)
        self._validate_command_key(effect_key)
        if type(request) is not dict:
            raise TypeError("request must be a JSON object")
        return self._admit(program_id, command_key, command, intent=(operation_id, effect_key, request))

    def _admit(
        self,
        program_id: str,
        command_key: str,
        command: DomainCommandType,
        intent: tuple[str, str, dict[str, object]] | None,
    ) -> Program:
        self._assert_writer_thread()
        references: tuple[str | None, ...] | None = None
        if isinstance(command, PrepareAttempt):
            references = (command.attempt.workspace_reference,)
        elif isinstance(command, (StartAttempt, FinishAttempt, SatisfyWorkUnit)):
            references = ()
        with self._gate, self._workspace_cut(references) as readings, self._transaction(readings):
            return self._admit_decision(program_id, command_key, command, intent)

    def _admit_decision(
        self,
        program_id: str,
        command_key: str,
        command: DomainCommandType,
        intent: tuple[str, str, dict[str, object]] | None,
        *,
        retained_bytes: int = 0,
    ) -> Program:
        self._assert_writer_thread()
        self._validate_program_id(program_id)
        self._validate_command_key(command_key)
        input_json = command_input_json(program_id, command)
        input_digest = digest_json(json.loads(input_json))
        command_type = cast(str, json.loads(input_json)["command_type"])
        duplicate = self._duplicate_result(command_key, input_digest)
        if duplicate is not None:
            if intent is not None:
                operation_id, effect_key, request = intent
                raw = canonical_json({"version": 1, "request": request})
                row = self._connection.execute(
                    "SELECT effect_key, request_json FROM operations WHERE operation_id = ?", (operation_id,)
                ).fetchone()
                if row is None or (row["effect_key"], row["request_json"]) != (effect_key, raw):
                    raise OperationConflict("duplicate command has no matching atomically admitted intent")
            return duplicate
        current = self._load_head(program_id)
        self._check_ownership_command(program_id, command)
        next_program = current.apply(command)
        if isinstance(command, SatisfyWorkUnit) and self._work_binding(program_id) is not None:
            self.check_work_authority(program_id)
            completed_reservations = [
                item
                for item in self._work_records("reservation")
                if item["program"] == program_id
                and item["attempt"] == command.satisfaction.source_attempt_id
                and item["category"] == WorkKind.ATTEMPT.value
            ]
            if len(completed_reservations) != 1:
                raise BudgetRefused("acceptance lacks original Attempt capacity reservation")
            completed_operation = cast(str, completed_reservations[0]["operation"])
            if self._work_record("settlement", completed_operation) is None:
                self._write_work_record(
                    "settlement",
                    completed_operation,
                    {"operation": completed_operation, "evidence": command.satisfaction.reference_id},
                )
        if isinstance(command, PrepareAttempt):
            # Pure historical domain facts remain writable/readable. They do
            # not authorize a dispatch without original envelope proof.
            binding = self._work_binding(program_id)
            if binding is not None:
                attempt = command.attempt
                if attempt.remediation_of is not None:
                    self._reserve_work(
                        program_id, attempt.attempt_id, f"remediation:{attempt.attempt_id}", WorkKind.REMEDIATION
                    )
                self._reserve_work(
                    program_id,
                    attempt.attempt_id,
                    intent[0] if intent is not None else f"runtime:{attempt.attempt_id}",
                    WorkKind.ATTEMPT,
                    retained_bytes=retained_bytes,
                )
            elif intent is not None:
                self._write_work_record(
                    "unbound-attempt",
                    intent[0],
                    {"program": program_id, "attempt": command.attempt.attempt_id, "operation": intent[0]},
                )
        if next_program.program_id != program_id:
            raise CorruptHistory("K1 returned a result for a different aggregate")
        head = self._connection.execute(
            "SELECT history_sequence FROM program_projection WHERE program_id = ?", (program_id,)
        ).fetchone()
        if head is None:
            raise ProgramNotFound(f"Program {program_id!r} does not exist")
        sequence = cast(int, head["history_sequence"]) + 1
        facts_json = program_json(next_program)
        facts_digest = hashlib.sha256(facts_json.encode("utf-8")).hexdigest()
        self._insert_history(
            program_id=program_id,
            sequence=sequence,
            command_key=command_key,
            command_type=command_type,
            actor_id=command.actor_id,
            input_digest=input_digest,
            input_json=input_json,
            program=next_program,
            facts_json=facts_json,
            facts_digest=facts_digest,
        )
        self._write_projections(next_program, sequence, facts_json, facts_digest)
        self._insert_command_result(
            command_key=command_key,
            program_id=program_id,
            command_type=command_type,
            input_digest=input_digest,
            result_sequence=sequence,
            result_json=facts_json,
            result_digest=facts_digest,
        )
        if intent is not None:
            self._insert_intent(*intent, allow_existing=False)
            if isinstance(command, PrepareAttempt) and self._work_binding(program_id) is not None:
                self._bind_work_dispatch(program_id, command.attempt.attempt_id, intent[0])
        return next_program

    def history(self, program_id: str) -> tuple[HistoryRecord, ...]:
        self._assert_writer_thread()
        self._validate_program_id(program_id)
        with self._gate:
            rows = self._connection.execute(
                "SELECT program_id, sequence, schema_version, command_key, command_type, actor_id, input_digest, "
                "input_json, aggregate_revision, facts_digest, facts_json FROM history "
                "WHERE program_id = ? ORDER BY sequence",
                (program_id,),
            ).fetchall()
            return tuple(
                HistoryRecord(
                    program_id=cast(str, row["program_id"]),
                    sequence=cast(int, row["sequence"]),
                    schema_version=cast(int, row["schema_version"]),
                    command_key=cast(str, row["command_key"]),
                    command_type=cast(str, row["command_type"]),
                    actor_id=cast(str, row["actor_id"]),
                    input_digest=cast(str, row["input_digest"]),
                    input_json=cast(str, row["input_json"]),
                    aggregate_revision=cast(int, row["aggregate_revision"]),
                    facts_digest=cast(str, row["facts_digest"]),
                    facts_json=cast(str, row["facts_json"]),
                )
                for row in rows
            )

    def intent(self, operation_id: str, effect_key: str, request: dict[str, object]) -> OperationRecord:
        """Commit the original logical effect and outbox together, independently of K2A admission."""
        self._assert_writer_thread()
        for value in (operation_id, effect_key):
            self._validate_command_key(value)
        if type(request) is not dict:
            raise TypeError("request must be a JSON object")
        with self._gate, self._transaction():
            self._insert_intent(operation_id, effect_key, request)
        return self.operation(operation_id)

    def _insert_intent(
        self, operation_id: str, effect_key: str, request: dict[str, object], *, allow_existing: bool = True
    ) -> None:
        request_json = canonical_json({"version": 1, "request": request})
        digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        existing = self._connection.execute(
            "SELECT operation_id, effect_key, request_digest FROM operations WHERE operation_id = ? OR effect_key = ?",
            (operation_id, effect_key),
        ).fetchall()
        if existing:
            if not allow_existing:
                raise OperationConflict("new command cannot claim a previously committed effect")
            if len(existing) != 1 or any(
                row["operation_id"] != operation_id
                or row["effect_key"] != effect_key
                or row["request_digest"] != digest
                for row in existing
            ):
                raise OperationConflict("logical effect identity is bound to a different request")
        else:
            self._connection.execute(
                "INSERT INTO operations (operation_id, effect_key, request_json, request_digest) VALUES (?, ?, ?, ?)",
                (operation_id, effect_key, request_json, digest),
            )
            self._connection.execute("INSERT INTO operation_outbox (operation_id) VALUES (?)", (operation_id,))

    def operation(self, operation_id: str) -> OperationRecord:
        self._assert_writer_thread()
        self._validate_command_key(operation_id)
        with self._gate:
            row = self._connection.execute(
                "SELECT o.*, (SELECT COUNT(*) FROM delivery_attempts a WHERE a.operation_id = o.operation_id) "
                "AS attempts FROM operations o WHERE o.operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise OperationConflict("unknown operation")
            return OperationRecord(
                *(
                    row[key]
                    for key in (
                        "operation_id",
                        "effect_key",
                        "request_digest",
                        "request_json",
                        "fence",
                        "lease_until",
                        "status",
                        "accepted_reference",
                        "retry_proof",
                        "attempts",
                    )
                )
            )

    def operations(self, prefix: str = "") -> tuple[OperationRecord, ...]:
        """Return stable operation identities for bounded application recovery/export."""
        self._assert_writer_thread()
        with self._gate:
            rows = self._connection.execute("SELECT operation_id FROM operations ORDER BY operation_id").fetchall()
            return tuple(self.operation(row["operation_id"]) for row in rows if row["operation_id"].startswith(prefix))

    def claim(self, operation_id: str, *, now: int, lease_seconds: int) -> int:
        return self._claim_operation(operation_id, now=now, lease_seconds=lease_seconds)

    def claim_owned_operation(self, claim: WorkClaim, operation_id: str, subject: str, lease_seconds: int) -> int:
        return self._claim_operation(operation_id, now=0, lease_seconds=lease_seconds, ownership=(claim, subject))

    def _claim_operation(
        self,
        operation_id: str,
        *,
        now: int,
        lease_seconds: int,
        ownership: tuple[WorkClaim, str] | None = None,
    ) -> int:
        """Durably record the attempt before the caller can invoke the fake effect seam."""
        self._assert_writer_thread()
        if type(now) is not int or type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("now must be an integer and lease_seconds positive")
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            op = self.operation(operation_id)
            reservation = self._work_record("reservation", operation_id)
            if ownership is not None and (
                reservation is None
                or (reservation["program"], reservation["attempt"])
                != (
                    ownership[0].attempt.program_id,
                    ownership[0].attempt.attempt_id,
                )
            ):
                raise OwnershipConflict("delivery target is outside original claim reservation")
            if reservation is not None:
                owned = self._attempt_work_claim(cast(str, reservation["program"]), cast(str, reservation["attempt"]))
                if owned is not None:
                    if ownership is None or ownership[0] != owned:
                        raise OwnershipConflict("owned operation requires the shared current-subject handler")
                    current_claim, subject = ownership
                    now = self.work_decision_time(current_claim.attempt.program_id)
                    if operation_id == current_claim.operation_id:
                        self._check_workspace_subject(current_claim, subject)
                    else:
                        self._check_claim_candidate(current_claim, subject)
            if reservation is None and (
                operation_id.startswith("runtime:") or self._work_record("unbound-attempt", operation_id) is not None
            ):
                raise BudgetRefused("runtime dispatch lacks original work-budget/clock proof")
            if reservation is not None:
                self.check_work_dispatch(operation_id)
                if op.attempts:
                    self._reserve_work(
                        cast(str, reservation["program"]),
                        cast(str, reservation["attempt"]),
                        f"retry:{operation_id}:{op.fence + 1}",
                        WorkKind.RETRY,
                    )
            if json.loads(op.request_json)["request"].get("kind") == "ordinary-intake-record":
                raise OperationConflict("inert intake metadata cannot be dispatched")
            if json.loads(op.request_json)["request"].get("kind") == "work-budget-record":
                raise OperationConflict("budget metadata cannot be dispatched")
            if op.lease_until is not None and op.lease_until > now:
                raise OperationConflict("delivery lease is held")
            if op.status in {"accepted", "running", "waiting", "terminal", "rejected"}:
                raise OperationConflict("operation already has a domain outcome")
            if op.attempts and op.retry_proof not in {"absent", "idempotent"}:
                raise OperationConflict("uncertain delivery requires reconciliation before retry")
            fence = op.fence + 1
            self._connection.execute(
                "UPDATE operations SET fence = ?, lease_until = ?, status = 'dispatched', retry_proof = NULL "
                "WHERE operation_id = ?",
                (fence, now + lease_seconds, operation_id),
            )
            self._connection.execute(
                "INSERT INTO delivery_attempts (operation_id, fence, claimed_at, lease_until) VALUES (?, ?, ?, ?)",
                (operation_id, fence, now, now + lease_seconds),
            )
            return fence

    @staticmethod
    def _work_key(record_type: str, identity: str) -> str:
        return f"work-budget:v1:{record_type}:{hashlib.sha256(identity.encode()).hexdigest()}"

    def _work_record(self, record_type: str, identity: str) -> dict[str, object] | None:
        key = self._work_key(record_type, identity)
        row = self._connection.execute("SELECT request_json FROM operations WHERE operation_id = ?", (key,)).fetchone()
        return None if row is None else cast(dict[str, object], json.loads(row["request_json"])["request"])

    def _work_records(self, record_type: str) -> list[dict[str, object]]:
        return [
            cast(dict[str, object], json.loads(item.request_json)["request"])
            for item in self.operations(f"work-budget:v1:{record_type}:")
        ]

    def _write_work_record(self, record_type: str, identity: str, fields: dict[str, object]) -> None:
        key = self._work_key(record_type, identity)
        self._insert_intent(key, key, {"kind": "work-budget-record", "type": record_type, **fields})

    def _work_binding(self, program_id: str) -> dict[str, object] | None:
        original = self._work_record("binding", program_id)
        if original is None:
            return None
        current_digest = self._load_head(program_id).spec.digest
        revised = self._work_record("binding-revision", canonical_json([program_id, current_digest]))
        return original if revised is None else revised

    def _decode_work_claim(self, record: dict[str, object]) -> WorkClaim:
        attempt = self._load_head(cast(str, record["program"])).attempt(cast(str, record["attempt"])).spec
        if record["attempt_digest"] != attempt.digest:
            raise OwnershipConflict("original claim Attempt changed")
        return WorkClaim(
            cast(str, record["command"]),
            cast(str, record["claimant"]),
            attempt,
            cast(int, record["generation"]),
            HandoffMode(cast(str, record["mode"])),
            cast(str, record["profile"]),
            tuple(ResourceScope(**item) for item in cast(list[dict[str, str]], record["resources"])),
            cast(str, record["workspace_subject"]),
            cast(int, record["expires_at"]),
        )

    def _validate_ownership_records(self) -> None:
        fields = {
            "ownership-claim": {
                "command",
                "input",
                "program",
                "attempt",
                "attempt_digest",
                "claimant",
                "generation",
                "mode",
                "profile",
                "resources",
                "workspace_subject",
                "expires_at",
            },
            "ownership-renew": {"claim", "claimant", "generation", "lease_seconds", "expires_at"},
            "ownership-proof": {"claim", "proof"},
            "ownership-observation": {"claim", "subject", "attribution", "order"},
            "ownership-reconcile": {"claim", "subject", "owner", "evidence"},
            "ownership-candidate": {"claim", "subject"},
            "ownership-release": {"claim", "evidence"},
            "ownership-unknown": {"claim", "evidence"},
        }
        for operation in self.operations("work-budget:v1:ownership-"):
            record = cast(dict[str, object], json.loads(operation.request_json)["request"])
            kind = record.get("type")
            if not isinstance(kind, str) or kind not in fields or record.get("kind") != "work-budget-record":
                raise CorruptHistory("unsupported ownership record type/version")
            allowed = fields[kind] | {"kind", "type"}
            if set(record) != allowed and not (
                kind == "ownership-release" and set(record) == allowed | {"subject", "owner"}
            ):
                raise CorruptHistory("unsupported ownership record fields")
            try:
                if kind == "ownership-claim":
                    claim = self._decode_work_claim(record)
                    if self._work_key(kind, claim.command_key) != operation.operation_id:
                        raise OwnershipConflict("claim namespace differs from command identity")
                else:
                    claim_record = self._work_record("ownership-claim", cast(str, record["claim"]))
                    if claim_record is None:
                        raise OwnershipConflict("ownership record has no original claim")
                    claim = self._decode_work_claim(claim_record)
                    if kind == "ownership-proof":
                        proof = cast(dict[str, object], record["proof"])
                        if set(proof) != {
                            "source",
                            "profile",
                            "mode",
                            "attempt_digest",
                            "generation",
                            "resources",
                            "stage",
                            "reference",
                        }:
                            raise OwnershipConflict("unsupported ownership proof fields")
                        HandoffProof(
                            cast(str, proof["source"]),
                            cast(str, proof["profile"]),
                            HandoffMode(cast(str, proof["mode"])),
                            cast(str, proof["attempt_digest"]),
                            cast(int, proof["generation"]),
                            tuple(ResourceScope(**item) for item in cast(list[dict[str, str]], proof["resources"])),
                            cast(str, proof["stage"]),
                            cast(str, proof["reference"]),
                        ).check(claim, cast(str, proof["stage"]))
                    if kind == "ownership-renew" and (
                        record["claimant"] != claim.claimant
                        or record["generation"] != claim.generation
                        or type(record["expires_at"]) is not int
                        or type(record["lease_seconds"]) is not int
                        or record["expires_at"] > cast(int, self.original_work_deadline(claim.attempt.program_id))
                    ):
                        raise OwnershipConflict("renewal differs from original authority horizon")
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise CorruptHistory("invalid exact ownership subject/receipt") from error

    @property
    def ownership_authority(self) -> str:
        self._assert_writer_thread()
        info = self._path.stat()
        return hashlib.sha256(canonical_json([str(self._path), info.st_dev, info.st_ino]).encode()).hexdigest()

    def _attempt_work_claim(self, program_id: str, attempt_id: str) -> WorkClaim | None:
        records = [
            item
            for item in self._work_records("ownership-claim")
            if (item["program"], item["attempt"]) == (program_id, attempt_id)
        ]
        if len(records) > 1:
            raise OwnershipConflict("Attempt has competing work claims")
        return None if not records else self._decode_work_claim(records[0])

    def _claim_owner(self, claim: WorkClaim) -> str:
        for spec in self._load_head(claim.attempt.program_id).spec_history:
            if (spec.revision, spec.digest) == (claim.attempt.spec_revision, claim.attempt.spec_digest):
                return spec.authority.owner_id
        raise OwnershipConflict("original work claim owner/specification unavailable")

    @staticmethod
    def _work_claim_input(
        command: PrepareAttempt,
        mode: HandoffMode,
        profile: str,
        resources: tuple[ResourceScope, ...],
        lease_seconds: int,
    ) -> str:
        return canonical_json(
            {
                "command": command_input_json(command.attempt.program_id, command),
                "claimant": command.actor_id,
                "mode": mode.value,
                "profile": profile,
                "resources": [asdict(item) for item in resources],
                "lease_seconds": lease_seconds,
            }
        )

    def find_work_claim(
        self,
        command_key: str,
        claimant: str,
        expected_revision: int,
        attempt: AttemptSpec,
        mode: HandoffMode,
        profile: str,
        resources: tuple[ResourceScope, ...],
        lease_seconds: int,
    ) -> WorkClaim | None:
        """Read original explicit-request outcome without clocks, filesystem or authority renewal."""
        self._assert_writer_thread()
        self._validate_command_key(command_key)
        with self._gate:
            previous = self._work_record("ownership-claim", command_key)
            if previous is None:
                return None
            decoded: object = json.loads(cast(str, previous["input"]))
            if type(decoded) is not dict:
                raise CorruptHistory("original work claim request must be an object")
            original = cast(dict[str, object], decoded)
            # Initial #59 records also retained the first observation in input.
            # Preserve those bytes and verify it, but do not recapture it on replay.
            if original.pop("workspace_subject", previous["workspace_subject"]) != previous["workspace_subject"]:
                raise CorruptHistory("original claim observation differs from its retained baseline")
            expected = self._work_claim_input(
                PrepareAttempt(expected_revision, claimant, attempt),
                mode,
                profile,
                resources,
                lease_seconds,
            )
            if canonical_json(original) != expected:
                raise IdempotencyConflict("work claim command already has different input or claimant")
            historical = self._decode_work_claim(previous)
            if (
                historical.command_key,
                historical.claimant,
                historical.attempt,
                historical.mode,
                historical.profile,
                historical.resources,
            ) != (
                command_key,
                claimant,
                attempt,
                mode,
                profile,
                resources,
            ):
                raise CorruptHistory("recorded claim differs from its original explicit request")
            return historical

    def claim_work(
        self,
        command_key: str,
        claimant: str,
        expected_revision: int,
        attempt: AttemptSpec,
        mode: HandoffMode,
        profile: str,
        resources: tuple[ResourceScope, ...],
        workspace_subject: str,
        snapshot: bytes,
        lease_seconds: int,
    ) -> WorkClaim:
        """One owner acceptance: resource claim, preparation, budget and outbox.

        Snapshot/producer work is finished before this method. No filesystem,
        authentication, witness or other business callback runs inside SQL.
        """
        self._assert_writer_thread()
        historical = self.find_work_claim(
            command_key,
            claimant,
            expected_revision,
            attempt,
            mode,
            profile,
            resources,
            lease_seconds,
        )
        if historical is not None:
            return historical
        if (
            type(mode) is not HandoffMode
            or not profile.strip()
            or not claimant.strip()
            or (mode is HandoffMode.PULL and profile != COOPERATIVE_PROFILE)
            or type(lease_seconds) is not int
            or lease_seconds <= 0
            or type(resources) is not tuple
            or not resources
            or sum(item.kind == "workspace" for item in resources) != 1
            or len(set(resources)) != len(resources)
            or hashlib.sha256(snapshot).hexdigest() != workspace_subject
        ):
            raise OwnershipConflict("invalid exact handoff input")
        self._validate_command_key(command_key)
        command = PrepareAttempt(expected_revision, claimant, attempt)
        data = self._work_claim_input(command, mode, profile, resources, lease_seconds)
        # Reject ineligible/oversized work before staging orphan-tolerant bytes;
        # the authoritative checks still repeat after the final clock cut.
        self.check_work_authority(attempt.program_id)
        self._load_head(attempt.program_id).apply(command)
        if len(snapshot) > self.available_retained_bytes(attempt.program_id):
            raise BudgetRefused("retained workspace artifact byte capacity exhausted; preserve source bytes")
        with (
            self._gate,
            self._workspace_cut(
                (attempt.workspace_reference,), ((f"runtime:{attempt.attempt_id}", "ownership-baseline", snapshot),)
            ) as readings,
            self._transaction(readings),
        ):
            historical = self.find_work_claim(
                command_key,
                claimant,
                expected_revision,
                attempt,
                mode,
                profile,
                resources,
                lease_seconds,
            )
            if historical is not None:
                return historical
            if self._connection.execute(
                "SELECT 1 FROM command_admissions WHERE command_key = ?", (command_key,)
            ).fetchone():
                raise OwnershipConflict("work claim cannot adopt a preexisting domain command")
            self.check_work_authority(attempt.program_id)
            if attempt.workspace_reference != next(item.key for item in resources if item.kind == "workspace"):
                raise OwnershipConflict("Attempt workspace differs from exact owned resource")
            records = self._work_records("ownership-claim")
            for record in records:
                if self._work_record("ownership-release", cast(str, record["command"])) is not None:
                    continue
                other = self._decode_work_claim(record)
                if (other.attempt.program_id, other.attempt.work_unit_id) == (attempt.program_id, attempt.work_unit_id):
                    raise OwnershipConflict("work already claimed; expiry is not settlement")
                if any(left.conflicts(right) for left in resources for right in other.resources):
                    raise OwnershipConflict("conflicting workspace/session/effect remains owned")
            self._check_generic_workspace_work(next(item for item in resources if item.kind == "workspace"))
            generation = max((cast(int, item["generation"]) for item in records), default=0) + 1
            now = self.work_decision_time(attempt.program_id)
            deadline = self.original_work_deadline(attempt.program_id)
            assert deadline is not None  # noqa: S101 - original authority checked above.
            expires_at = min(deadline, now + lease_seconds)
            operation_id = f"runtime:{attempt.attempt_id}"
            self._admit_decision(
                attempt.program_id,
                command_key,
                command,
                (operation_id, operation_id, {"attempt": attempt.digest}),
                retained_bytes=len(snapshot),
            )
            self._write_work_record(
                "ownership-claim",
                command_key,
                {
                    "command": command_key,
                    "input": data,
                    "program": attempt.program_id,
                    "attempt": attempt.attempt_id,
                    "attempt_digest": attempt.digest,
                    "claimant": claimant,
                    "generation": generation,
                    "mode": mode.value,
                    "profile": profile,
                    "resources": [asdict(item) for item in resources],
                    "workspace_subject": workspace_subject,
                    "expires_at": expires_at,
                },
            )
            self._finalize_artifact(operation_id, "ownership-baseline", snapshot)
            return self._decode_work_claim(cast(dict[str, object], self._work_record("ownership-claim", command_key)))

    def _check_work_claim(
        self,
        claim: WorkClaim,
        *,
        authority: bool = True,
        settled: bool = False,
        exclude_operation: str | None = None,
        publication: bool = False,
    ) -> None:
        original = self._work_record("ownership-claim", claim.command_key)
        if original is None or self._decode_work_claim(original) != claim:
            raise OwnershipConflict("claimant, Attempt or original generation differs")
        if self._work_record("ownership-release", claim.command_key) is not None:
            raise OwnershipConflict("claim generation released; no further authority")
        if self._work_record("ownership-unknown", claim.command_key) is not None:
            raise OwnershipConflict(
                "task resource scope unknown; quarantine requires explicit isolation/reconciliation"
            )
        if authority or settled:
            self._check_workspace_claims(claim.attempt)
        for item in self._work_records("ownership-claim"):
            if cast(int, item["generation"]) > claim.generation:
                newer = self._decode_work_claim(item)
                if any(left.conflicts(right) for left in claim.resources for right in newer.resources):
                    raise OwnershipConflict("stale resource generation")
        if authority:
            self.check_work_authority(claim.attempt.program_id)
            program = self._load_head(claim.attempt.program_id)
            allowed_statuses = (
                {ProgramStatus.ACTIVE, ProgramStatus.COMPLETED} if publication else {ProgramStatus.ACTIVE}
            )
            if program.status not in allowed_statuses or program.spec.digest != claim.attempt.spec_digest:
                raise OwnershipConflict("claim no longer belongs to current active specification")
            attempt = program.attempt(claim.attempt.attempt_id)
            state = program.state(claim.attempt.work_unit_id)
            if (
                attempt.status in {AttemptStatus.CANCELLED, AttemptStatus.FAILED}
                or state.status
                in {
                    WorkUnitStatus.CANCELLED,
                }
                or (state.status is WorkUnitStatus.SATISFIED and not publication)
            ):
                raise OwnershipConflict("claim Attempt/WorkUnit no longer eligible")
            renewals = [item for item in self._work_records("ownership-renew") if item["claim"] == claim.command_key]
            horizon = max([claim.expires_at, *(cast(int, item["expires_at"]) for item in renewals)])
            if self.work_decision_time(claim.attempt.program_id) >= horizon:
                raise OwnershipConflict("work claim expired; resources remain quarantined")
        if settled:
            if not any(
                self._work_record("ownership-proof", f"{claim.command_key}:{stage}") for stage in ("complete", "stop")
            ):
                raise OwnershipConflict("task-scope settlement unknown; preserve conflicting resources")
            for item in self._work_records("reservation"):
                if (item["program"], item["attempt"]) == (claim.attempt.program_id, claim.attempt.attempt_id):
                    if (
                        item["operation"] != exclude_operation
                        and self._work_record("settlement", cast(str, item["operation"])) is None
                    ):
                        raise OwnershipConflict("unreconciled effect retains conflicting resources")

    def check_work_claim(self, claim: WorkClaim, *, settled: bool = False) -> None:
        self._assert_writer_thread()
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            self._check_work_claim(claim, settled=settled)

    def renew_work_claim(self, command_key: str, claim: WorkClaim, lease_seconds: int) -> int:
        self._assert_writer_thread()
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise OwnershipConflict("renewal needs a positive bounded duration")
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            previous = self._work_record("ownership-renew", command_key)
            if previous is not None:
                if (previous["claim"], previous["claimant"], previous["lease_seconds"]) != (
                    claim.command_key,
                    claim.claimant,
                    lease_seconds,
                ):
                    raise IdempotencyConflict("renew command input or claimant changed")
                self._check_work_claim(claim)
                return cast(int, previous["expires_at"])
            self._check_work_claim(claim)
            now = self.work_decision_time(claim.attempt.program_id)
            deadline = self.original_work_deadline(claim.attempt.program_id)
            assert deadline is not None  # noqa: S101 - original authority checked above.
            expiry = min(deadline, now + lease_seconds)
            self._write_work_record(
                "ownership-renew",
                command_key,
                {
                    "claim": claim.command_key,
                    "claimant": claim.claimant,
                    "generation": claim.generation,
                    "lease_seconds": lease_seconds,
                    "expires_at": expiry,
                },
            )
            return expiry

    def record_claim_proof(self, claim: WorkClaim, proof: HandoffProof, *, worker_report: bool = False) -> None:
        self._assert_writer_thread()
        proof.check(claim, proof.stage)
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            # Exact trusted reconciliation is allowed after revoke/expiry, unlike
            # worker renewal, candidate reports, acceptance or publication.
            self._check_work_claim(claim, authority=worker_report or proof.stage == "start")
            if claim.mode is HandoffMode.MANAGED and proof.stage != "start":
                started = self._work_record("ownership-proof", f"{claim.command_key}:start")
                if started is None:
                    raise OwnershipConflict("managed settlement lacks original exact start witness")
                original = cast(dict[str, object], started["proof"])
                if (proof.source, proof.reference) != (original["source"], original["reference"]):
                    raise OwnershipConflict("managed settlement differs from original owned target/source")
            self._write_work_record(
                "ownership-proof",
                f"{claim.command_key}:{proof.stage}",
                {
                    "claim": claim.command_key,
                    "proof": proof.payload(),
                },
            )
            if proof.stage != "start" and self._work_record("settlement", claim.operation_id) is None:
                self._write_work_record(
                    "settlement",
                    claim.operation_id,
                    {
                        "operation": claim.operation_id,
                        "evidence": proof.reference,
                    },
                )

    def observe_claim_workspace(
        self,
        claim: WorkClaim,
        subject: str,
        snapshot: bytes,
        *,
        owner: str | None = None,
    ) -> None:
        self._assert_writer_thread()
        if hashlib.sha256(snapshot).hexdigest() != subject:
            raise OwnershipConflict("workspace snapshot digest differs")
        with (
            self._gate,
            self._workspace_cut((), ((claim.operation_id, f"workspace:{subject}", snapshot),)) as readings,
            self._transaction(readings),
        ):
            self._check_work_claim(claim, authority=owner is None)
            if owner is not None and self._claim_owner(claim) != owner:
                raise OwnershipConflict("only original owner can observe for expired resource reconciliation")
            identity = f"{claim.command_key}:{subject}"
            if self._work_record("ownership-observation", identity) is None:
                order = (
                    max((cast(int, item["order"]) for item in self._work_records("ownership-observation")), default=0)
                    + 1
                )
                self._write_work_record(
                    "ownership-observation",
                    identity,
                    {
                        "claim": claim.command_key,
                        "subject": subject,
                        "attribution": "unknown",
                        "order": order,
                    },
                )
            self._finalize_artifact(claim.operation_id, f"workspace:{subject}", snapshot)

    def reconcile_claim_workspace(self, claim: WorkClaim, owner: str, subject: str, evidence: str) -> None:
        self._assert_writer_thread()
        if not evidence.strip():
            raise OwnershipConflict("explicit reconciliation evidence required")
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            self._check_work_claim(claim, authority=False, settled=True)
            if self._claim_owner(claim) != owner:
                raise OwnershipConflict("only original owner can reconcile workspace changes")
            if self._work_record("ownership-observation", f"{claim.command_key}:{subject}") is None:
                raise OwnershipConflict("reconciliation needs an exact retained observation")
            self._write_work_record(
                "ownership-reconcile",
                f"{claim.command_key}:{subject}",
                {
                    "claim": claim.command_key,
                    "subject": subject,
                    "owner": owner,
                    "evidence": evidence,
                },
            )

    def _check_workspace_subject(self, claim: WorkClaim, subject: str) -> None:
        if (
            subject != claim.workspace_subject
            and self._work_record(
                "ownership-reconcile",
                f"{claim.command_key}:{subject}",
            )
            is None
        ):
            raise OwnershipConflict("changed/manual workspace bytes require explicit exact-subject reconciliation")

    def _check_claim_candidate(self, claim: WorkClaim, subject: str) -> None:
        candidate = self._work_record("ownership-candidate", claim.command_key)
        observations = [
            item for item in self._work_records("ownership-observation") if item["claim"] == claim.command_key
        ]
        if (
            candidate is None
            or candidate["subject"] != subject
            or (observations and max(observations, key=lambda item: cast(int, item["order"]))["subject"] != subject)
        ):
            raise OwnershipConflict("publication/acceptance requires unchanged exact candidate handoff")

    def check_claim_candidate(self, claim: WorkClaim, subject: str) -> None:
        self._assert_writer_thread()
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            self._check_work_claim(claim, settled=True)
            self._check_claim_candidate(claim, subject)

    def submit_claim_candidate(self, claim: WorkClaim, subject: str, snapshot: bytes) -> str:
        self._assert_writer_thread()
        if hashlib.sha256(snapshot).hexdigest() != subject:
            raise OwnershipConflict("candidate snapshot digest differs")
        with (
            self._gate,
            self._workspace_cut((), ((claim.operation_id, "ownership-candidate", snapshot),)) as readings,
            self._transaction(readings),
        ):
            self._check_work_claim(claim, settled=True)
            self._check_workspace_subject(claim, subject)
            self._write_work_record(
                "ownership-candidate", claim.command_key, {"claim": claim.command_key, "subject": subject}
            )
            self._finalize_artifact(claim.operation_id, "ownership-candidate", snapshot)
            return subject

    def cleanup_work_claim(
        self,
        claim: WorkClaim,
        owner: str,
        subject: str,
        snapshot: bytes,
        evidence: str,
        retire: Callable[[], None],
    ) -> None:
        self._assert_writer_thread()
        if not evidence.strip() or hashlib.sha256(snapshot).hexdigest() != subject:
            raise OwnershipConflict("exact resource retirement evidence required")
        # Validate before changing the filesystem, then revalidate at release.
        # No callback/effect executes in the business SQL transaction. A failure
        # or crash retains ownership, including after physical retirement.
        with self._gate:
            with self._workspace_cut(()) as readings, self._transaction(readings):
                self._check_work_claim(claim, authority=False, settled=True)
                if self._claim_owner(claim) != owner:
                    raise OwnershipConflict("only original owner can retire resources")
                self._check_workspace_subject(claim, subject)
            retire()
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            self._check_work_claim(claim, authority=False, settled=True)
            self._write_work_record(
                "ownership-release", claim.command_key, {"claim": claim.command_key, "evidence": evidence}
            )

    def authorize_claim_publication(
        self,
        claim: WorkClaim,
        owner: str,
        subject: str,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
    ) -> OperationRecord:
        self._assert_writer_thread()
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            self._check_work_claim(claim, settled=True, publication=True)
            if self._claim_owner(claim) != owner:
                raise OwnershipConflict("only original owner authorizes protected effects")
            self._check_claim_candidate(claim, subject)
            self._reserve_work(claim.attempt.program_id, claim.attempt.attempt_id, operation_id, WorkKind.AUXILIARY)
            self._insert_intent(operation_id, effect_key, request)
            self._bind_work_dispatch(claim.attempt.program_id, claim.attempt.attempt_id, operation_id)
            self.check_work_dispatch(operation_id)
        return self.operation(operation_id)

    def release_claim_resources(
        self,
        claim: WorkClaim,
        owner: str,
        subject: str,
        snapshot: bytes,
        evidence: str,
    ) -> None:
        self._assert_writer_thread()
        if not evidence.strip() or hashlib.sha256(snapshot).hexdigest() != subject:
            raise OwnershipConflict("exact owner reconciliation subject required")
        with (
            self._gate,
            self._workspace_cut((), ((claim.operation_id, "ownership-release", snapshot),)) as readings,
            self._transaction(readings),
        ):
            self._check_work_claim(claim, authority=False, settled=True)
            if self._claim_owner(claim) != owner:
                raise OwnershipConflict("only original owner can release settled resources")
            self._check_workspace_subject(claim, subject)
            self._finalize_artifact(claim.operation_id, "ownership-release", snapshot)
            self._write_work_record(
                "ownership-release",
                claim.command_key,
                {
                    "claim": claim.command_key,
                    "subject": subject,
                    "owner": owner,
                    "evidence": evidence,
                },
            )

    def quarantine_work_claim(self, claim: WorkClaim, owner: str | None, evidence: str) -> None:
        self._assert_writer_thread()
        if not evidence.strip():
            raise OwnershipConflict("scope uncertainty requires evidence")
        with self._gate, self._transaction():
            self._check_work_claim(claim, authority=False)
            # None is the trusted application-bound profile producer path, never
            # a worker request parameter. Explicit owner calls remain checked.
            if owner is not None and self._claim_owner(claim) != owner:
                raise OwnershipConflict("only original owner can record scope quarantine")
            self._write_work_record(
                "ownership-unknown", claim.command_key, {"claim": claim.command_key, "evidence": evidence}
            )

    @staticmethod
    def _resolve_workspace_scope(reference: str) -> ResourceScope | None:
        try:
            workspace = ResourceScope("workspace", reference)
            return workspace if str(Path(reference).resolve(strict=False)) == reference else None
        except (OwnershipConflict, OSError, RuntimeError):
            return None

    def _workspace_references(self, additional: tuple[str | None, ...]) -> frozenset[str]:
        references = set(additional)
        for row in self._connection.execute("SELECT program_id FROM program_projection").fetchall():
            references.update(
                item.spec.workspace_reference for item in self._load_head(cast(str, row["program_id"])).attempts
            )
        return frozenset(reference for reference in references if reference is not None and reference.startswith("/"))

    @contextmanager
    def _workspace_cut(
        self,
        additional: tuple[str | None, ...] | None,
        artifacts: tuple[tuple[str, str, bytes], ...] = (),
    ) -> Generator[dict[str, BootClockReading | None] | None, None, None]:
        if additional is None:
            yield None
            return
        if self._connection.in_transaction:
            raise ProgramReadChanged("physical workspace preflight cannot join business SQL")
        changes = self._connection.total_changes
        references = self._workspace_references(additional)
        scopes = {reference: self._resolve_workspace_scope(reference) for reference in sorted(references)}
        prepared: dict[tuple[str, str], str] = {}
        for operation_id, name, data in artifacts:
            self._validate_command_key(operation_id)
            self._validate_command_key(name)
            if type(data) is not bytes:
                raise TypeError("artifact must be bytes")
            digest = hashlib.sha256(data).hexdigest()
            self._check_owned_artifact_budget(operation_id, digest, len(data))
            self._stage_blob(digest, data)
            prepared[(operation_id, name)] = digest
        verified = {reference: self._resolve_workspace_scope(reference) for reference in sorted(references)}
        stable = not self._closed and changes == self._connection.total_changes and scopes == verified
        if self._closed:
            raise ProgramReadChanged("owner closed during physical workspace preflight")
        # Potentially blocking validation precedes the independently durable
        # clock cut. Business guards below only consume these decision-local facts.
        readings = self._prepare_work_clocks()
        if not stable or self._workspace_references(additional) != references:
            raise ProgramReadChanged("physical workspace/store read cut changed during preflight")
        previous = self._workspace_scopes
        previous_artifacts = self._workspace_artifacts
        self._workspace_scopes = scopes
        self._workspace_artifacts = prepared
        try:
            yield readings
        finally:
            self._workspace_scopes = previous
            self._workspace_artifacts = previous_artifacts

    def _physical_workspace_scope(self, reference: str | None) -> ResourceScope | None:
        # Legacy keys are opaque, not paths to resolve or silently rewrite.
        if reference is None or not reference.startswith("/"):
            return None
        ResourceScope("workspace", reference)
        if self._workspace_scopes is None or reference not in self._workspace_scopes:
            raise ProgramReadChanged("physical workspace lacks this decision's preflight")
        scope = self._workspace_scopes[reference]
        if scope is None:
            raise OwnershipConflict("physical workspace must already be canonical; aliases require reconciliation")
        return scope

    def _check_workspace_claims(self, attempt: AttemptSpec) -> None:
        workspace = self._physical_workspace_scope(attempt.workspace_reference)
        if workspace is None:
            return
        for item in self._work_records("ownership-claim"):
            if self._work_record("ownership-release", cast(str, item["command"])) is not None:
                continue
            other = self._decode_work_claim(item)
            if (other.attempt.program_id, other.attempt.attempt_id) == (attempt.program_id, attempt.attempt_id):
                continue
            if any(workspace.conflicts(scope) for scope in other.resources):
                raise OwnershipConflict(
                    "unreleased workspace ownership blocks competing preparation/reservation/delivery"
                )
        self._check_generic_workspace_work(workspace, exclude=attempt)

    def _check_generic_workspace_work(self, workspace: ResourceScope, *, exclude: AttemptSpec | None = None) -> None:
        records = self._work_records("reservation")
        for row in self._connection.execute("SELECT program_id FROM program_projection").fetchall():
            program = self._load_head(cast(str, row["program_id"]))
            for attempt in program.attempts:
                if exclude is not None and (program.program_id, attempt.attempt_id) == (
                    exclude.program_id,
                    exclude.attempt_id,
                ):
                    continue
                if self._attempt_work_claim(program.program_id, attempt.attempt_id) is not None:
                    continue  # Owned generations are governed by their durable release, not Attempt status.
                other_workspace = self._physical_workspace_scope(attempt.spec.workspace_reference)
                if other_workspace is None or not workspace.conflicts(other_workspace):
                    continue
                effects = [
                    item
                    for item in records
                    if (item["program"], item["attempt"])
                    == (
                        program.program_id,
                        attempt.attempt_id,
                    )
                    and item["category"] not in {WorkKind.RETRY.value, WorkKind.REMEDIATION.value}
                ]
                if not effects or any(
                    self._work_record("settlement", cast(str, item["operation"])) is None for item in effects
                ):
                    raise OwnershipConflict(
                        "generic workspace work lacks applicable exact settlement; cannot adopt its resource"
                    )

    def _check_ownership_command(self, program_id: str, command: DomainCommandType) -> None:
        if isinstance(command, PrepareAttempt):
            self._check_workspace_claims(command.attempt)
            for item in self._work_records("ownership-claim"):
                if item["program"] != program_id or self._work_record("ownership-release", cast(str, item["command"])):
                    continue
                claim = self._decode_work_claim(item)
                if claim.attempt.work_unit_id == command.attempt.work_unit_id:
                    raise OwnershipConflict("unreleased task resources block successor Attempt")
        if isinstance(command, (StartAttempt, FinishAttempt, SatisfyWorkUnit)):
            attempt_id = (
                command.satisfaction.source_attempt_id if isinstance(command, SatisfyWorkUnit) else command.attempt_id
            )
            known = next(
                (item.spec for item in self._load_head(program_id).attempts if item.attempt_id == attempt_id), None
            )
            if known is not None:
                self._check_workspace_claims(known)
            claim = self._attempt_work_claim(program_id, attempt_id)
            if claim is not None:
                self._check_work_claim(claim, settled=not isinstance(command, StartAttempt))
                if isinstance(command, StartAttempt) and any(
                    self._work_record("ownership-proof", f"{claim.command_key}:{stage}")
                    for stage in ("complete", "stop")
                ):
                    raise OwnershipConflict("settled task capability cannot be restarted")
                if isinstance(command, StartAttempt) and claim.mode is HandoffMode.MANAGED:
                    if self._work_record("ownership-proof", f"{claim.command_key}:start") is None:
                        raise OwnershipConflict("managed start lacks exact profile witness")
                if (
                    isinstance(command, SatisfyWorkUnit)
                    and self._work_record("ownership-candidate", claim.command_key) is None
                ):
                    raise OwnershipConflict("acceptance lacks exact reconciled candidate handoff")
                if isinstance(command, SatisfyWorkUnit):
                    candidate = cast(dict[str, object], self._work_record("ownership-candidate", claim.command_key))
                    self._check_claim_candidate(claim, cast(str, candidate["subject"]))

    def configure_work_budget(
        self,
        program_id: str,
        project_id: str,
        project: WorkBudgetPolicy,
        task: WorkBudgetPolicy,
        projection: DeadlineProjection,
    ) -> None:
        """Bind immutable approved policy on the existing single writer, without rewriting old bytes.

        Project capacity spans Programs in THIS store only. Separate databases do
        not constitute one project capacity authority.
        """
        self._assert_writer_thread()
        self._validate_program_id(program_id)
        self._validate_command_key(project_id)
        if project.strict_inference or task.strict_inference:
            raise BudgetRefused("strict inference bounds unavailable for the installed profiles")
        with self._gate, self._transaction():
            self._configure_work_budget(program_id, project_id, project, task, projection)

    def _configure_work_budget(
        self,
        program_id: str,
        project_id: str,
        project: WorkBudgetPolicy,
        task: WorkBudgetPolicy,
        projection: DeadlineProjection,
    ) -> None:
        existing = self._work_binding(program_id)
        if existing is None and self._load_head(program_id).attempts:
            raise BudgetRefused("legacy Attempt lacks original work-budget proof; reconciliation required")
        origin = self._work_record("clock-origin", program_id)
        if origin is not None:
            if projection != DeadlineProjection.decode(origin["projection"]):
                raise BudgetRefused("original work projection cannot be renewed")
            if existing is None and origin["spec_digest"] != self._load_head(program_id).spec.digest:
                raise BudgetRefused("prepared clock origin belongs to a different specification")
        self._write_work_record("project", project_id, {"project": project_id, "policy": project.payload()})
        fields: dict[str, object] = {
            "program": program_id,
            "project": project_id,
            "policy": task.payload(),
            "projection": asdict(projection),
            "spec_digest": self._load_head(program_id).spec.digest,
        }
        original = self._work_record("binding", program_id)
        if existing is not None and original is not None and original["spec_digest"] != fields["spec_digest"]:
            if any(existing[name] != fields[name] for name in ("program", "project", "policy", "projection")):
                raise BudgetRefused("specification revision cannot reset original work envelope")
            self._write_work_record("binding-revision", canonical_json([program_id, fields["spec_digest"]]), fields)
        else:
            self._write_work_record("binding", program_id, fields)

    def accept_work_budget(
        self,
        spec: ProgramSpec,
        command_key: str,
        project_id: str,
        project: WorkBudgetPolicy,
        task: WorkBudgetPolicy,
        deadline: int | DeadlineProjection,
        *,
        amendment: AmendProgramSpec | None = None,
        records: tuple[tuple[str, dict[str, object], bytes | None], ...] = (),
        validate: Callable[[], None] | None = None,
    ) -> Program:
        """Publish one budgeted Program decision, its inert records and binding atomically.

        Only these private write internals share the transaction. Clock evidence
        and caller validation commit/run beforehand; rejection cannot erase a
        reentrant cancellation or an observed clock floor. Orphan blob bytes are
        harmless: their references join the same acceptance commit.
        """
        self._assert_writer_thread()
        self._validate_program_id(spec.program_id)
        self._validate_command_key(command_key)
        self._validate_command_key(project_id)
        with self._gate:
            readings = self._prepare_work_clocks()
            changes = self._connection.total_changes
            current = self.find_program(spec.program_id)
            existing = self._work_binding(spec.program_id)
            origin = self.pending_work_projection(spec)
            if existing is not None or origin is not None:
                projection = DeadlineProjection.decode(existing["projection"]) if existing is not None else origin
                assert projection is not None  # noqa: S101 - original evidence checked above.
                if (
                    deadline.wall_deadline if isinstance(deadline, DeadlineProjection) else deadline
                ) != projection.wall_deadline:
                    raise BudgetRefused("original work expiry cannot be renewed")
                if isinstance(deadline, DeadlineProjection) and deadline != projection:
                    raise BudgetRefused("original work projection cannot be renewed")
                if existing is not None and (existing["project"] != project_id or existing["policy"] != task.payload()):
                    raise BudgetRefused("specification revision cannot reset original work envelope")
                if existing is None or validate is not None or amendment is not None:
                    self._check_work_projection(spec.program_id, projection, readings.get(spec.program_id))
            else:
                projection = self._new_work_projection(deadline, readings)
            if project.strict_inference or task.strict_inference:
                raise BudgetRefused("strict inference bounds unavailable for the installed profiles")
            project_record = self._work_record("project", project_id)
            if project_record is not None and project_record["policy"] != project.payload():
                raise BudgetRefused("project policy cannot replace original capacity authority")
            if current is not None and existing is None and current.attempts:
                raise BudgetRefused("legacy Attempt lacks original work-budget proof; reconciliation required")
            if self._closed or self._connection.total_changes != changes:
                raise ProgramReadChanged("final store read cut changed during a callback")
            if existing is None and origin is None:
                # A prepared clock origin is evidence, not an accepted budget or
                # Program. Keep it independently so failure/retry cannot renew it.
                with self._transaction(readings):
                    self._write_work_record(
                        "clock-origin",
                        spec.program_id,
                        {"program": spec.program_id, "spec_digest": spec.digest, "projection": asdict(projection)},
                    )
                changes = self._connection.total_changes
            if validate is not None:
                validate()
            if self._closed or self._connection.total_changes != changes:
                raise ProgramReadChanged("final store read cut changed during a callback")
            # Nothing that can block on caller validation runs after this final
            # independent capture. Preserve the original anchors, not a new bind.
            readings = self._prepare_work_clocks()
            if existing is None or validate is not None or amendment is not None:
                self._check_work_projection(spec.program_id, projection, readings.get(spec.program_id))
            with self._transaction(readings):
                if current is None:
                    if amendment is not None:
                        raise ProgramReadChanged("approval amendment has no original Program")
                    program = self._create(spec, command_key)
                elif current.spec == spec:
                    program = current
                elif amendment is not None:
                    program = self._admit_decision(spec.program_id, command_key, amendment, None)
                else:
                    raise ProgramReadChanged("current Program binding changed")
                if program.spec != spec:
                    raise ProgramReadChanged("admitted Program differs from approved specification")
                self._configure_work_budget(spec.program_id, project_id, project, task, projection)
                for key, request, raw in records:
                    self._validate_command_key(key)
                    self._insert_intent(key, key, request)
                    if raw is not None:
                        self._finalize_artifact(key, "record", raw)
            return program

    def has_work_budget(self, program_id: str) -> bool:
        self._assert_writer_thread()
        return self._work_binding(program_id) is not None

    def pending_work_projection(self, spec: ProgramSpec) -> DeadlineProjection | None:
        """Recover a prepared clock origin, never accepted work/dispatch authority."""
        self._assert_writer_thread()
        origin = self._work_record("clock-origin", spec.program_id)
        if origin is None or self._work_binding(spec.program_id) is not None:
            return None
        if origin["spec_digest"] != spec.digest:
            raise BudgetRefused("prepared clock origin belongs to a different specification")
        return DeadlineProjection.decode(origin["projection"])

    def _check_work_projection(
        self, program_id: str, projection: DeadlineProjection, reading: BootClockReading | None
    ) -> None:
        if reading is None:
            raise BudgetRefused("clock observation persistence unverified; no new work authority")
        if not projection.verifiable(reading):
            raise BudgetRefused("original work deadline expired or boot clock unverifiable")
        previous = self._clock_watermarks[program_id]
        if reading.wall < previous.wall or reading.boot_ns < previous.boot_ns:
            raise BudgetRefused("observed wall/boot clock rollback; no new work authority")
        if projection.decision_time(reading) >= projection.wall_deadline:
            raise BudgetRefused("original work deadline expired or boot clock unverifiable")

    def _new_work_projection(
        self, deadline: int | DeadlineProjection, readings: dict[str, BootClockReading | None]
    ) -> DeadlineProjection:
        if readings:
            if any(item is None for item in readings.values()):
                raise BudgetRefused("clock observation persistence unverified; no new work envelope")
            reading = next(iter(readings.values()))
            assert reading is not None  # noqa: S101 - checked all captured readings above.
            for program_id in readings:
                previous = self._clock_watermarks[program_id]
                if previous.boot_id == reading.boot_id and (
                    reading.wall < previous.wall or reading.boot_ns < previous.boot_ns
                ):
                    raise BudgetRefused("observed wall/boot clock rollback; no new work envelope")
        elif isinstance(deadline, DeadlineProjection):
            return deadline  # Caller already captured this original proof, not a recovery reprojection.
        else:
            reading = self._boot_clock()
        if reading is None:
            raise BudgetRefused("verified suspend-inclusive clock unavailable")
        if isinstance(deadline, DeadlineProjection):
            if not deadline.verifiable(reading) or deadline.decision_time(reading) >= deadline.wall_deadline:
                raise BudgetRefused("original work deadline expired or boot clock unverifiable")
            return deadline
        return DeadlineProjection.bind(deadline, reading)

    def bind_work_budget(
        self, program_id: str, project_id: str, project: WorkBudgetPolicy, task: WorkBudgetPolicy, deadline: int
    ) -> None:
        """First binding only; reopening never projects a deadline again."""
        self._assert_writer_thread()
        existing = self._work_binding(program_id)
        origin = existing if existing is not None else self._work_record("clock-origin", program_id)
        if origin is not None:
            projection = DeadlineProjection.decode(origin["projection"])
            if projection.wall_deadline != deadline:
                raise BudgetRefused("original work expiry cannot be renewed")
        else:
            projection = self._new_work_projection(
                deadline, self._prepare_work_clocks() if self._work_records("binding") else {}
            )
        self.configure_work_budget(program_id, project_id, project, task, projection)

    def original_work_deadline(self, program_id: str) -> int | None:
        self._assert_writer_thread()
        binding = self._work_binding(program_id)
        origin = binding if binding is not None else self._work_record("clock-origin", program_id)
        return None if origin is None else DeadlineProjection.decode(origin["projection"]).wall_deadline

    def check_work_authority(self, program_id: str) -> None:
        self._assert_writer_thread()
        binding = self._work_binding(program_id)
        if binding is None:
            raise BudgetRefused("configured original work envelope required")
        if binding["spec_digest"] != self._load_head(program_id).spec.digest:
            raise BudgetRefused("work envelope belongs to a different current specification")
        projection = DeadlineProjection.decode(binding["projection"])
        if self.work_decision_time(program_id) >= projection.wall_deadline:
            raise BudgetRefused("original work deadline expired or boot clock unverifiable")
        if (
            self._work_record("revoked", program_id) is not None
            or self._load_head(program_id).status is ProgramStatus.CANCELLED
        ):
            raise BudgetRefused("work authority revoked")

    def work_decision_time(self, program_id: str) -> int:
        """Use the same durably captured reading for application authority clocks.

        This proves time only, not dispatch permission. Expired/revoked work may
        still reconcile receipts; unverifiable time or rollback never becomes a
        fresh projection or a new authority grant.
        """
        self._assert_writer_thread()
        binding = self._work_binding(program_id)
        if binding is None:
            raise BudgetRefused("configured original work envelope required")
        projection = DeadlineProjection.decode(binding["projection"])
        readings = self._transaction_clock_readings if self._connection.in_transaction else self._prepare_work_clocks()
        state = self._connection.execute(
            "SELECT * FROM work_clock_state WHERE program_id = ?", (program_id,)
        ).fetchone()
        if state is None or state["pending"] or readings is None or program_id not in readings:
            raise BudgetRefused("clock observation persistence unverified; no new work authority")
        reading = readings[program_id]
        if not projection.verifiable(reading):
            raise BudgetRefused("original work deadline expired or boot clock unverifiable")
        assert reading is not None  # noqa: S101 - projection verification established reading.
        previous = self._clock_watermarks[program_id]
        if reading.wall < previous.wall or reading.boot_ns < previous.boot_ns:
            raise BudgetRefused("observed wall/boot clock rollback; no new work authority")
        return projection.decision_time(reading)

    def _flush_clock_observations(self) -> None:
        """Commit only captured clock evidence, never capacity/outbox/domain rows."""
        if not self._clock_uncommitted:
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for program_id, reading in self._clock_uncommitted.items():
                row = self._connection.execute(
                    "SELECT * FROM work_clock_state WHERE program_id = ?", (program_id,)
                ).fetchone()
                if row is None:
                    raise BudgetRefused("original pending clock observation is missing")
                wall, boot_ns = cast(int, row["wall"]), cast(int, row["boot_ns"])
                if reading is not None and (reading.boot_id, reading.clock) == (row["boot_id"], row["clock"]):
                    wall, boot_ns = max(wall, reading.wall), max(boot_ns, reading.boot_ns)
                self._connection.execute(
                    "UPDATE work_clock_state SET wall = ?, boot_ns = ?, pending = 0 WHERE program_id = ?",
                    (wall, boot_ns, program_id),
                )
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        # Cache only committed guard floors. These maxima never reproject expiry.
        for program_id in self._clock_uncommitted:
            row = self._connection.execute(
                "SELECT * FROM work_clock_state WHERE program_id = ?", (program_id,)
            ).fetchone()
            assert row is not None  # noqa: S101 - transaction retained the existing row.
            self._clock_watermarks[program_id] = BootClockReading(
                row["wall"], row["boot_ns"], row["boot_id"], row["clock"]
            )
        self._clock_uncommitted.clear()

    def _prepare_work_clocks(self) -> dict[str, BootClockReading | None]:
        """Durable pending marker BEFORE acquisition; durable floors BEFORE admission.

        A lost pending read cannot be reconstructed from a recovery wall clock.
        The same live owner may retry flushing the exact captured sample. Historical
        reads and cancellation journaling remain possible when authority is unknown.
        """
        if self._connection.in_transaction:
            return {}  # Never roll back or join an outer admission transaction.
        if self._connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return {}
        bindings = self._work_records("binding")
        bound = {item["program"] for item in bindings}
        bindings.extend(item for item in self._work_records("clock-origin") if item["program"] not in bound)
        if not bindings:
            return {}
        result: dict[str, BootClockReading | None] = {cast(str, item["program"]): None for item in bindings}
        try:
            self._flush_clock_observations()
            self._connection.execute("BEGIN IMMEDIATE")
            eligible: list[str] = []
            try:
                for binding in bindings:
                    program_id = cast(str, binding["program"])
                    row = self._connection.execute(
                        "SELECT * FROM work_clock_state WHERE program_id = ?", (program_id,)
                    ).fetchone()
                    if row is None:
                        projection = DeadlineProjection.decode(binding["projection"])
                        legacy = self._work_records(f"clock-{hashlib.sha256(program_id.encode()).hexdigest()}")
                        wall = max([projection.wall_anchor, *(cast(int, item["wall"]) for item in legacy)])
                        boot_ns = max([projection.boot_anchor_ns, *(cast(int, item["boot_ns"]) for item in legacy)])
                        self._connection.execute(
                            "INSERT INTO work_clock_state VALUES (?, ?, ?, ?, ?, 0)",
                            (program_id, wall, boot_ns, projection.boot_id, projection.clock),
                        )
                    elif row["pending"]:
                        continue
                    self._connection.execute(
                        "UPDATE work_clock_state SET pending = 1 WHERE program_id = ?", (program_id,)
                    )
                    eligible.append(program_id)
                self._connection.execute("COMMIT")
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            if not eligible:
                return result
            changes = self._connection.total_changes
            try:
                reading = self._boot_clock()
            except (OSError, ValueError):
                reading = None
            callback_changed = self._closed or self._connection.total_changes != changes
            for program_id in eligible:
                self._clock_uncommitted[program_id] = reading
            self._flush_clock_observations()
            for program_id in eligible:
                result[program_id] = None if callback_changed else reading
        except (OSError, sqlite3.DatabaseError, BudgetRefused):
            # No authority is derived from an uncommitted sample. The committed
            # pending marker survives failed flush and fresh-process recovery.
            return result
        return result

    def revoke_work_budget(self, program_id: str, evidence: str) -> None:
        self._assert_writer_thread()
        self._validate_command_key(evidence)
        with self._gate, self._transaction():
            if self._work_record("revoked", program_id) is None:
                self._write_work_record("revoked", program_id, {"program": program_id, "evidence": evidence})

    def _bind_work_dispatch(self, program_id: str, attempt_id: str, operation_id: str) -> None:
        attempt = self._load_head(program_id).attempt(attempt_id).spec
        operation = self.operation(operation_id)
        self._write_work_record(
            "dispatch-binding",
            operation_id,
            {
                "program": program_id,
                "attempt": attempt_id,
                "attempt_digest": attempt.digest,
                "spec_digest": attempt.spec_digest,
                "spec_revision": attempt.spec_revision,
                "operation": operation_id,
                "effect_key": operation.effect_key,
                "request_digest": operation.request_digest,
            },
        )

    def check_work_dispatch(self, operation_id: str) -> None:
        """Revalidate NEW dispatch, not historical receipt or reservation replay."""
        self._assert_writer_thread()
        if self._connection.in_transaction:
            self._check_work_dispatch(operation_id)
            return
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            self._check_work_dispatch(operation_id)

    def _check_work_dispatch(self, operation_id: str) -> None:
        reservation = self._work_record("reservation", operation_id)
        if reservation is None:
            raise BudgetRefused("dispatch lacks original work reservation")
        program_id, attempt_id = cast(str, reservation["program"]), cast(str, reservation["attempt"])
        self.check_work_authority(program_id)
        program = self._load_head(program_id)
        try:
            attempt = program.attempt(attempt_id)
        except ValueError as error:
            raise BudgetRefused("dispatch lacks its originating Attempt") from error
        self._check_workspace_claims(attempt.spec)
        if (attempt.spec.spec_revision, attempt.spec.spec_digest) != (program.spec.revision, program.spec.digest):
            raise BudgetRefused("reserved Attempt belongs to a superseded specification")
        operation = self.operation(operation_id)
        binding = self._work_record("dispatch-binding", operation_id)
        expected = {
            "program": program_id,
            "attempt": attempt_id,
            "attempt_digest": attempt.spec.digest,
            "spec_digest": attempt.spec.spec_digest,
            "spec_revision": attempt.spec.spec_revision,
            "operation": operation_id,
            "effect_key": operation.effect_key,
            "request_digest": operation.request_digest,
        }
        if binding is None:
            # Existing original typed requests remain evidence, never a new projection.
            request = cast(dict[str, object], json.loads(operation.request_json)["request"])
            subject = request.get("subject")
            subject_fields = cast(dict[str, object], subject) if type(subject) is dict else {}
            if not (
                reservation["category"] == WorkKind.ATTEMPT.value and request.get("attempt") == attempt.spec.digest
            ) and not (
                subject_fields.get("attempt_digest") == attempt.spec.digest
                and subject_fields.get("spec_digest") == program.spec.digest
            ):
                raise BudgetRefused("original exact dispatch binding unavailable")
        elif binding != {"kind": "work-budget-record", "type": "dispatch-binding", **expected}:
            raise BudgetRefused("Operation differs from original Attempt dispatch binding")
        state = program.state(attempt.spec.work_unit_id)
        category = WorkKind(cast(str, reservation["category"]))
        claim = self._attempt_work_claim(program_id, attempt_id)
        if claim is not None:
            self._check_work_claim(
                claim,
                settled=category is WorkKind.AUXILIARY,
                exclude_operation=operation_id,
                publication=category is WorkKind.AUXILIARY,
            )
            if category is WorkKind.AUXILIARY:
                candidate = self._work_record("ownership-candidate", claim.command_key)
                if candidate is None:
                    raise OwnershipConflict("publication lacks exact candidate handoff")
                self._check_claim_candidate(claim, cast(str, candidate["subject"]))
            if operation_id == claim.operation_id:
                if claim.mode is HandoffMode.PULL:
                    raise OwnershipConflict("pull handoff is not managed Runtime dispatch authority")
                if self._work_record("ownership-proof", f"{claim.command_key}:start") is None:
                    raise OwnershipConflict("managed dispatch lacks exact profile start witness")
                if any(
                    self._work_record("ownership-proof", f"{claim.command_key}:{stage}")
                    for stage in ("complete", "stop")
                ):
                    raise OwnershipConflict("settled native capability cannot be restarted")
        if category is WorkKind.ATTEMPT:
            if json.loads(operation.request_json)["request"].get("attempt") != attempt.spec.digest:
                raise BudgetRefused("runtime Operation request differs from originating Attempt")
            permitted = (
                program.status is ProgramStatus.ACTIVE
                and attempt.status in {AttemptStatus.PREPARED, AttemptStatus.EXECUTING}
                and state.status is WorkUnitStatus.ACTIVE
                and state.active_attempt_id == attempt_id
            )
        elif category is WorkKind.AUXILIARY:
            permitted = (
                program.status in {ProgramStatus.ACTIVE, ProgramStatus.COMPLETED}
                and attempt.status is AttemptStatus.FINISHED
                and state.status is WorkUnitStatus.SATISFIED
                and any(
                    item.reference_id == state.satisfaction_id and item.source_attempt_id == attempt_id
                    for item in program.satisfactions
                )
            )
        else:
            latest = next(
                (item for item in reversed(program.attempts) if item.spec.work_unit_id == attempt.spec.work_unit_id),
                None,
            )
            permitted = (
                program.status is ProgramStatus.ACTIVE
                and attempt.status is AttemptStatus.FINISHED
                and state.status is WorkUnitStatus.READY
                and latest is not None
                and latest.attempt_id == attempt_id
            )
        if not permitted:
            raise BudgetRefused("Attempt/WorkUnit state no longer permits this dispatch")
        self._check_resource_headroom(program_id, exclude_operation=operation_id)

    def _check_resource_headroom(self, program_id: str, *, exclude_operation: str | None = None) -> None:
        binding = self._work_binding(program_id)
        if binding is None:
            raise BudgetRefused("configured original work envelope required")
        project = self._work_record("project", cast(str, binding["project"]))
        if project is None:
            raise BudgetRefused("project capacity authority unavailable")
        records = self._work_records("reservation")
        for scope, identity, policy in (
            ("program", program_id, WorkBudgetPolicy.decode(binding["policy"])),
            ("project", binding["project"], WorkBudgetPolicy.decode(project["policy"])),
        ):
            scoped = [item for item in records if item[scope] == identity]
            effective = self._effective_usage(scoped)
            for ceiling in policy.resources:
                authoritative = {
                    Provenance.MEASUREMENT.value,
                    Provenance.PROVIDER_REPORT.value,
                    Provenance.OWNER_RECORD.value,
                }
                matching = [
                    item for item in effective if (item["metric"], item["unit"]) == (ceiling.metric, ceiling.unit)
                ]
                if (
                    sum(
                        (
                            Decimal(cast(str, item["quantity"]))
                            for item in matching
                            if item["quantity"] is not None and item["provenance"] in authoritative
                        ),
                        Decimal(0),
                    )
                    >= ceiling.quantity
                ):
                    raise BudgetRefused(f"{scope} observed resource ceiling exhausted")
                # A numeric receipt, even after physical settlement, does not prove
                # complete hidden-client consumption. No installed profile supplies
                # a resource-specific conservative final bound to release this hold.
                if any(item["operation"] != exclude_operation for item in scoped) or any(
                    item["quantity"] is None or item["provenance"] not in authoritative for item in matching
                ):
                    raise BudgetRefused(f"{scope} resource headroom unknown")

    def _reserve_work(
        self, program_id: str, attempt_id: str, operation_id: str, category: WorkKind, retained_bytes: int = 0
    ) -> None:
        if type(category) is not WorkKind or type(retained_bytes) is not int or retained_bytes < 0:
            raise ValueError("invalid work reservation")
        binding = self._work_binding(program_id)
        if binding is None:
            raise BudgetRefused("configured original work envelope required")
        reservation = {
            "program": program_id,
            "project": binding["project"],
            "attempt": attempt_id,
            "operation": operation_id,
            "category": category.value,
            "retained_bytes": retained_bytes,
        }
        prior = self._work_record("reservation", operation_id)
        known = next(
            (item.spec for item in self._load_head(program_id).attempts if item.attempt_id == attempt_id), None
        )
        if known is not None:
            self._check_workspace_claims(known)
        if prior is not None:
            if prior != {"kind": "work-budget-record", "type": "reservation", **reservation}:
                raise BudgetRefused("reservation identity conflicts with original input")
            return
        self.check_work_authority(program_id)
        claim = self._attempt_work_claim(program_id, attempt_id)
        if claim is not None:
            self._check_work_claim(claim, publication=category is WorkKind.AUXILIARY)
        records = self._work_records("reservation")
        if category is WorkKind.ATTEMPT and any(
            item["program"] == program_id and item["attempt"] == attempt_id and item["category"] == category.value
            for item in records
        ):
            raise BudgetRefused("Attempt capacity already belongs to another Operation")
        project = self._work_record("project", cast(str, binding["project"]))
        if project is None:
            raise BudgetRefused("project capacity authority unavailable")
        for scope, identity, policy in (
            ("program", program_id, WorkBudgetPolicy.decode(binding["policy"])),
            ("project", binding["project"], WorkBudgetPolicy.decode(project["policy"])),
        ):
            scoped = [item for item in records if item[scope] == identity]
            if sum(item["category"] == category.value for item in scoped) >= dict(policy.counts)[category]:
                raise BudgetRefused(f"{scope} {category.value} capacity exhausted")
            active = {
                (cast(str, item["program"]), cast(str, item["attempt"]))
                for item in scoped
                if item["category"] not in {WorkKind.REMEDIATION.value, WorkKind.RETRY.value}
                and self._work_record("settlement", cast(str, item["operation"])) is None
            }
            if (program_id, attempt_id) not in active and len(active) >= policy.max_active:
                raise BudgetRefused(f"{scope} active work capacity exhausted")
            retained = 0
            for item in scoped:
                retained += self._retained_work_bytes(item)
            if retained + retained_bytes > policy.retained_bytes:
                raise BudgetRefused(f"{scope} retained streaming byte capacity exhausted")
        self._check_resource_headroom(program_id)
        self._write_work_record("reservation", operation_id, reservation)

    def reserve_work(
        self, program_id: str, attempt_id: str, operation_id: str, category: WorkKind, *, retained_bytes: int = 0
    ) -> None:
        self._assert_writer_thread()
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            self._reserve_work(program_id, attempt_id, operation_id, category, retained_bytes)

    def budgeted_intent(
        self,
        program_id: str,
        attempt_id: str,
        category: WorkKind,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
        *,
        retained_bytes: int = 0,
    ) -> OperationRecord:
        self._assert_writer_thread()
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            existed = self._work_record("reservation", operation_id) is not None
            self._reserve_work(program_id, attempt_id, operation_id, category, retained_bytes)
            self._insert_intent(operation_id, effect_key, request)
            if not existed:
                self._bind_work_dispatch(program_id, attempt_id, operation_id)
        return self.operation(operation_id)

    def work_program_for_attempt(self, attempt_id: str) -> str:
        self._assert_writer_thread()
        programs = {
            cast(str, item["program"]) for item in self._work_records("reservation") if item["attempt"] == attempt_id
        }
        if len(programs) != 1:
            raise BudgetRefused("exact Attempt work-budget binding unavailable")
        program_id = programs.pop()
        if not any(item.attempt_id == attempt_id for item in self._load_head(program_id).attempts):
            raise BudgetRefused("reserved work is not a prepared Attempt")
        return program_id

    def retry_work(self, operation_id: str) -> None:
        self._assert_writer_thread()
        with self._gate, self._transaction():
            original = self._work_record("reservation", operation_id)
            if original is None:
                raise BudgetRefused("retry lacks original reservation")
            prefix = f"work-retry:{operation_id}:"
            count = sum(cast(str, item["operation"]).startswith(prefix) for item in self._work_records("reservation"))
            self._reserve_work(
                cast(str, original["program"]), cast(str, original["attempt"]), f"{prefix}{count + 1}", WorkKind.RETRY
            )

    def record_work_settlement(self, operation_id: str, evidence: str) -> None:
        """Trusted profile-specific exact witness, never a lease/turn-terminal inference."""
        self._assert_writer_thread()
        self._validate_command_key(evidence)
        with self._gate, self._workspace_cut(()) as readings, self._transaction(readings):
            reservation = self._work_record("reservation", operation_id)
            if reservation is None:
                raise BudgetRefused("settlement has no original reservation")
            claim = self._attempt_work_claim(cast(str, reservation["program"]), cast(str, reservation["attempt"]))
            if claim is not None and operation_id == claim.operation_id:
                self._check_work_claim(claim, authority=False, settled=True)
            if self._work_record("settlement", operation_id) is None:
                self._write_work_record("settlement", operation_id, {"operation": operation_id, "evidence": evidence})

    def record_retained_bytes(self, operation_id: str, retained_bytes: int) -> None:
        self._assert_writer_thread()
        with self._gate, self._transaction():
            reservation = self._work_record("reservation", operation_id)
            if (
                reservation is None
                or type(retained_bytes) is not int
                or not 0 <= retained_bytes <= cast(int, reservation["retained_bytes"])
            ):
                raise BudgetRefused("retained bytes exceed original reservation")
            self._write_work_record("retained", operation_id, {"operation": operation_id, "bytes": retained_bytes})

    def available_retained_bytes(self, program_id: str) -> int:
        self._assert_writer_thread()
        binding = self._work_binding(program_id)
        if binding is None:
            raise BudgetRefused("configured work envelope required")
        project = self._work_record("project", cast(str, binding["project"]))
        if project is None:
            raise BudgetRefused("configured project envelope required")
        records = self._work_records("reservation")
        available: list[int] = []
        for scope, identity, policy in (
            ("program", program_id, WorkBudgetPolicy.decode(binding["policy"])),
            ("project", binding["project"], WorkBudgetPolicy.decode(project["policy"])),
        ):
            used = 0
            for item in records:
                if item[scope] == identity:
                    used += self._retained_work_bytes(item)
            available.append(policy.retained_bytes - used)
        return max(0, min(available))

    def _effective_usage(self, reservations: list[dict[str, object]]) -> list[dict[str, object]]:
        operations = {item["operation"] for item in reservations}
        facts = [item for item in self._work_records("usage") if item["operation"] in operations]
        superseded = {(item["source"], item["supersedes_id"]) for item in facts if item["supersedes_id"] is not None}
        return [item for item in facts if (item["source"], item["observation_id"]) not in superseded]

    def record_usage(self, operation_id: str, observation: UsageObservation) -> None:
        self._assert_writer_thread()
        with self._gate, self._transaction():
            reservation = self._work_record("reservation", operation_id)
            if reservation is None or reservation["attempt"] != observation.attempt_id:
                raise BudgetRefused("usage must bind its original reserved Attempt/Operation")
            fields = {"operation": operation_id, **observation.payload()}
            observation_key = canonical_json([observation.source, observation.observation_id])
            existing = self._work_record("usage", observation_key)
            if existing is not None:
                self._write_work_record("usage", observation_key, fields)
                return
            facts = self._effective_usage(self._work_records("reservation"))
            identity = (observation.source, observation.receipt_id, observation.metric, observation.unit)
            prior = [
                item for item in facts if (item["source"], item["receipt_id"], item["metric"], item["unit"]) == identity
            ]
            if any(item["operation"] != operation_id or item["attempt_id"] != observation.attempt_id for item in prior):
                raise BudgetRefused("source receipt retains its original Attempt/Operation attribution")
            if (not prior and observation.supersedes_id is not None) or (
                prior and (len(prior) != 1 or observation.supersedes_id != prior[0]["observation_id"])
            ):
                raise BudgetRefused("usage correction requires the latest same-source receipt and unit")
            self._write_work_record("usage", observation_key, fields)

    def work_budget_status(self, program_id: str) -> dict[str, object]:
        self._assert_writer_thread()
        binding = self._work_binding(program_id)
        reservations = [item for item in self._work_records("reservation") if item["program"] == program_id]
        effective = self._effective_usage(reservations)
        groups = {
            (cast(str, item["metric"]), cast(str, item["unit"]), cast(str, item["provenance"])) for item in effective
        }
        totals: list[dict[str, object]] = []
        for metric, unit, provenance in sorted(groups):
            observations = [
                item
                for item in effective
                if (item["metric"], item["unit"], item["provenance"]) == (metric, unit, provenance)
            ]
            subtotal = sum(
                (Decimal(cast(str, item["quantity"])) for item in observations if item["quantity"] is not None),
                Decimal(0),
            )
            missing = len({item["operation"] for item in reservations} - {item["operation"] for item in observations})
            unknown = sum(item["quantity"] is None for item in observations)
            totals.append(
                {
                    "metric": metric,
                    "unit": unit,
                    "provenance": provenance,
                    "known_subtotal": str(subtotal),
                    "quantity": None,
                    "coverage": "observed receipts only; hidden external-client work unverified",
                    "unknown_receipts": unknown,
                    "unreported_operations": missing,
                }
            )
        return {
            "version": 1,
            "binding": binding,
            "reservations": reservations,
            "effective_usage": effective,
            "resource_totals": totals,
            "usage_history": [
                item
                for item in self._work_records("usage")
                if item["operation"] in {record["operation"] for record in reservations}
            ],
            "reservation_is_usage": False,
            "missing_usage": "unknown",
            "inference_usage_coverage": "unknown; a known receipt does not prove hidden work absent",
            "hard_inference_cap": False,
            "scope": "single configured SQLite owner; no cross-database capacity claim",
        }

    def deliver_fake(
        self,
        operation_id: str,
        observation_id: str,
        *,
        now: int,
        lease_seconds: int,
        fake: Callable[[str, str], tuple[bool, str | None, str | None]],
    ) -> OperationRecord:
        """Invoke a synthetic effect only after the delivery claim has committed."""
        fence = self.claim(operation_id, now=now, lease_seconds=lease_seconds)
        op = self.operation(operation_id)
        transported, kind, reference = fake(op.effect_key, op.request_json)
        self.record_transport(operation_id, fence, transported)
        if not transported or kind is None:
            return self.operation(operation_id)
        return self.observe(operation_id, fence, observation_id, kind, reference=reference)

    def record_transport(self, operation_id: str, fence: int, succeeded: bool) -> None:
        """Persist a transport report without treating it as domain acceptance."""
        self._assert_writer_thread()
        if type(succeeded) is not bool:
            raise TypeError("transport outcome must be boolean")
        with self._gate, self._transaction():
            op = self.operation(operation_id)
            if op.fence != fence or op.attempts == 0 or op.retry_proof is not None:
                raise OperationConflict("stale transport fence")
            prior = self._connection.execute(
                "SELECT succeeded FROM operation_transports WHERE operation_id = ? AND fence = ?",
                (operation_id, fence),
            ).fetchone()
            if prior is not None:
                if bool(prior["succeeded"]) != succeeded:
                    raise OperationConflict("transport report conflicts with prior outcome")
            else:
                self._connection.execute(
                    "INSERT INTO operation_transports VALUES (?, ?, ?)", (operation_id, fence, int(succeeded))
                )

    def observe(
        self,
        operation_id: str,
        fence: int,
        observation_id: str,
        kind: str,
        *,
        reference: str | None = None,
    ) -> OperationRecord:
        """Record a domain receipt, not a transport result; reject stale publications."""
        self._assert_writer_thread()
        self._validate_command_key(observation_id)
        if kind not in _OBSERVATION_KINDS:
            raise ValueError("unsupported domain observation")
        if kind in {"accepted", "waiting"} and (type(reference) is not str or not reference.strip()):
            raise OperationConflict("acceptance and waiting require a matching durable reference")
        with self._gate, self._transaction():
            op = self.operation(operation_id)
            if type(fence) is not int or op.fence != fence or op.attempts == 0:
                raise OperationConflict("stale or missing delivery fence")
            if op.retry_proof is not None:
                raise OperationConflict("reconciled fence cannot publish a late callback")
            duplicate = self._connection.execute(
                "SELECT operation_id, fence, kind, reference FROM operation_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
            if duplicate is not None:
                if tuple(duplicate) != (operation_id, fence, kind, reference):
                    raise OperationConflict("observation identity is bound to different evidence")
                return op
            if op.status in {"terminal", "rejected"} or (
                op.status in {"accepted", "running", "waiting"} and kind == "rejected"
            ):
                raise OperationConflict("domain outcome cannot regress")
            if kind in {"running", "waiting", "terminal"} and op.accepted_reference is None:
                raise OperationConflict("execution observation requires domain acceptance")
            if kind in {"running", "waiting", "terminal"} and reference != op.accepted_reference:
                raise OperationConflict("execution reference differs from accepted domain receipt")
            if kind == "accepted" and op.accepted_reference not in (None, reference):
                raise OperationConflict("conflicting domain acceptance")
            if kind == "accepted" and op.status in {"running", "waiting"}:
                raise OperationConflict("domain progress cannot regress to acceptance")
            if kind in {"running", "waiting"} and op.status == "unknown":
                raise OperationConflict("unknown delivery must be reconciled")
            next_status = (
                kind if kind != "unknown" or op.status not in {"accepted", "running", "waiting"} else op.status
            )
            self._connection.execute(
                "INSERT INTO operation_observations (observation_id, operation_id, fence, kind, reference) "
                "VALUES (?, ?, ?, ?, ?)",
                (observation_id, operation_id, fence, kind, reference),
            )
            self._connection.execute(
                "UPDATE operations SET status = ?, accepted_reference = COALESCE(accepted_reference, ?) "
                "WHERE operation_id = ?",
                (next_status, reference if kind == "accepted" else None, operation_id),
            )
        return self.operation(operation_id)

    def reconcile(
        self,
        operation_id: str,
        fence: int,
        outcome: str,
        *,
        now: int,
        evidence: str,
        reference: str | None = None,
        matched_digest: str | None = None,
        authoritative_absence: bool = False,
    ) -> OperationRecord:
        """Persist synthetic lookup evidence; only authoritative absence or same-key guarantee permits retry."""
        self._assert_writer_thread()
        if outcome not in {"absent", "idempotent", "found", "unknown"}:
            raise ValueError("unsupported reconciliation outcome")
        if type(now) is not int:
            raise ValueError("reconciliation time must be an integer")
        if type(evidence) is not str or not evidence.strip():
            raise OperationConflict("reconciliation requires durable lookup evidence")
        with self._gate, self._transaction():
            op = self.operation(operation_id)
            if op.fence != fence or not op.attempts or op.status not in {"dispatched", "unknown"}:
                raise OperationConflict("stale fence or finalized operation")
            if op.lease_until is not None and op.lease_until > now:
                raise OperationConflict("delivery lease is still held")
            if outcome in {"found", "idempotent"} and matched_digest != op.request_digest:
                raise OperationConflict("remote effect or idempotency guarantee does not match original request")
            if outcome == "absent" and authoritative_absence is not True:
                raise OperationConflict("retry requires authoritative absence, not an incomplete lookup")
            if outcome == "found" and (not reference or op.accepted_reference not in (None, reference)):
                raise OperationConflict("found effect requires matching domain reference")
            if outcome in {"absent", "idempotent"} and op.accepted_reference is not None:
                raise OperationConflict("accepted effect cannot be retried")
            status = "accepted" if outcome == "found" else "unknown"
            self._connection.execute(
                "INSERT INTO operation_reconciliations "
                "(operation_id, fence, outcome, reference, evidence, matched_digest, authoritative_absence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (operation_id, fence, outcome, reference, evidence, matched_digest, int(authoritative_absence)),
            )
            self._connection.execute(
                "UPDATE operations SET status = ?, accepted_reference = COALESCE(accepted_reference, ?), "
                "retry_proof = ?, lease_until = NULL WHERE operation_id = ?",
                (
                    status,
                    reference if outcome == "found" else None,
                    outcome if outcome in {"absent", "idempotent"} else None,
                    operation_id,
                ),
            )
        return self.operation(operation_id)

    def finalize_artifact(self, operation_id: str, name: str, data: bytes) -> str:
        """Flush bytes to a content address before publishing an immutable DB reference."""
        self._assert_writer_thread()
        with self._gate, self._transaction():
            return self._finalize_artifact(operation_id, name, data)

    def _finalize_artifact(self, operation_id: str, name: str, data: bytes) -> str:
        self._assert_writer_thread()
        self.operation(operation_id)
        self._validate_command_key(name)
        if type(data) is not bytes:
            raise TypeError("artifact must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        self._check_owned_artifact_budget(operation_id, digest, len(data))
        with self._gate:
            if self._workspace_scopes is not None:
                if self._workspace_artifacts is None or self._workspace_artifacts.get((operation_id, name)) != digest:
                    raise ProgramReadChanged("ownership artifact lacks this decision's completed preflight")
            else:
                self._stage_blob(digest, data)
            existing = self._connection.execute(
                "SELECT digest, size FROM artifact_references WHERE operation_id = ? AND name = ?",
                (operation_id, name),
            ).fetchone()
            if existing is not None:
                if (existing["digest"], existing["size"]) != (digest, len(data)):
                    raise OperationConflict("artifact reference is immutable")
            else:
                self._connection.execute(
                    "INSERT INTO artifact_references VALUES (?, ?, ?, ?)",
                    (operation_id, name, digest, len(data)),
                )
        return f"sha256:{digest}"

    def _stage_blob(self, digest: str, data: bytes) -> None:
        directory = self._artifact_directory
        if not directory.exists():
            directory.mkdir(mode=0o700)
            _fsync_directory(directory.parent)
        if directory.is_symlink() or not directory.is_dir():
            raise CorruptHistory("artifact directory must be a real directory")
        _inspect_local_storage(directory)
        blob = directory / digest
        if blob.exists():
            self._verify_blob(blob, digest, len(data))
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if blob.exists():
                self._verify_blob(blob, digest, len(data))
            else:
                os.replace(temporary, blob)
                _fsync_directory(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _retained_work_bytes(self, reservation: dict[str, object]) -> int:
        operation_id = cast(str, reservation["operation"])
        final = self._work_record("retained", operation_id)
        held = cast(int, reservation["retained_bytes"] if final is None else final["bytes"])
        if self._attempt_work_claim(cast(str, reservation["program"]), cast(str, reservation["attempt"])) is not None:
            actual = self._connection.execute(
                "SELECT COALESCE(SUM(size), 0) FROM (SELECT DISTINCT digest, size FROM artifact_references "
                "WHERE operation_id = ?)",
                (operation_id,),
            ).fetchone()[0]
            held = max(held, cast(int, actual))
        return held

    def _check_owned_artifact_budget(self, operation_id: str, digest: str, size: int) -> None:
        reservation = self._work_record("reservation", operation_id)
        if (
            reservation is None
            or self._attempt_work_claim(
                cast(str, reservation["program"]),
                cast(str, reservation["attempt"]),
            )
            is None
        ):
            return
        actual, present = self._connection.execute(
            "SELECT COALESCE(SUM(size), 0), COALESCE(MAX(digest = ?), 0) "
            "FROM (SELECT DISTINCT digest, size FROM artifact_references WHERE operation_id = ?)",
            (digest, operation_id),
        ).fetchone()
        current = self._retained_work_bytes(reservation)
        extra = max(current, cast(int, actual) + (0 if present else size)) - current
        binding = self._work_binding(cast(str, reservation["program"]))
        assert binding is not None  # noqa: S101 - reservation has its original policy.
        project = self._work_record("project", cast(str, reservation["project"]))
        if project is None:
            raise BudgetRefused("original snapshot capacity authority unavailable")
        for scope, identity, policy in (
            ("program", reservation["program"], WorkBudgetPolicy.decode(binding["policy"])),
            ("project", reservation["project"], WorkBudgetPolicy.decode(project["policy"])),
        ):
            used = sum(
                self._retained_work_bytes(item) for item in self._work_records("reservation") if item[scope] == identity
            )
            if used + extra > policy.retained_bytes:
                raise BudgetRefused(
                    f"{scope} retained workspace artifact byte capacity exhausted; preserve source bytes"
                )

    def artifact(self, operation_id: str, name: str) -> bytes:
        data = self.find_artifact(operation_id, name)
        if data is None:
            raise OperationConflict("unknown artifact reference")
        return data

    def find_artifact(self, operation_id: str, name: str) -> bytes | None:
        """Read optional durable evidence without hiding a corrupt or missing blob."""
        self._assert_writer_thread()
        with self._gate:
            row = self._connection.execute(
                "SELECT digest, size FROM artifact_references WHERE operation_id = ? AND name = ?",
                (operation_id, name),
            ).fetchone()
            if row is None:
                return None
            blob = self._artifact_directory / cast(str, row["digest"])
            self._verify_blob(blob, cast(str, row["digest"]), cast(int, row["size"]))
            return blob.read_bytes()

    @staticmethod
    def _verify_blob(blob: Path, digest: str, size: int) -> None:
        if blob.is_symlink() or not blob.is_file() or blob.stat().st_nlink != 1 or blob.stat().st_size != size:
            raise CorruptHistory("artifact blob is missing or invalid")
        with blob.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise CorruptHistory("artifact digest mismatch")

    def _validate_operation_records(self) -> None:
        for row in self._connection.execute(
            "SELECT operation_id, request_json, request_digest, fence, status, accepted_reference, retry_proof "
            "FROM operations"
        ):
            raw = cast(str, row["request_json"])
            try:
                value = json.loads(raw)
                if set(value) != {"version", "request"} or value["version"] != 1 or type(value["request"]) is not dict:
                    raise ValueError("invalid versioned request")
                if canonical_json(value) != raw or hashlib.sha256(raw.encode()).hexdigest() != row["request_digest"]:
                    raise ValueError("request digest mismatch")
            except (ValueError, TypeError) as error:
                raise CorruptHistory("operation request is not canonical or digest-valid") from error
            if (
                self._connection.execute(
                    "SELECT 1 FROM operation_outbox WHERE operation_id = ?", (row["operation_id"],)
                ).fetchone()
                is None
            ):
                raise CorruptHistory("operation is missing its outbox entry")
            attempts = self._connection.execute(
                "SELECT COUNT(*), MAX(fence) FROM delivery_attempts WHERE operation_id = ?", (row["operation_id"],)
            ).fetchone()
            if row["fence"] != attempts[0] or (attempts[0] and row["fence"] != attempts[1]):
                raise CorruptHistory("operation fence differs from durable delivery attempts")
            status = row["status"]
            if (status == "intent") != (attempts[0] == 0):
                raise CorruptHistory("operation intent and attempts disagree")
            accepted = self._connection.execute(
                "SELECT 1 FROM operation_observations WHERE operation_id = ? AND kind = 'accepted' "
                "AND reference = ? UNION SELECT 1 FROM operation_reconciliations WHERE operation_id = ? "
                "AND outcome = 'found' AND reference = ?",
                (row["operation_id"], row["accepted_reference"], row["operation_id"], row["accepted_reference"]),
            ).fetchone()
            if (row["accepted_reference"] is not None) != (accepted is not None):
                raise CorruptHistory("domain acceptance has no matching durable receipt")
            if status in {"accepted", "running", "waiting", "terminal"} and accepted is None:
                raise CorruptHistory("operation advanced without domain acceptance")
            if status in {"rejected", "running", "waiting", "terminal"}:
                receipt = self._connection.execute(
                    "SELECT 1 FROM operation_observations WHERE operation_id = ? AND kind = ?",
                    (row["operation_id"], status),
                ).fetchone()
                if receipt is None:
                    raise CorruptHistory("operation status has no matching durable observation")
            if row["retry_proof"] is not None:
                proof = self._connection.execute(
                    "SELECT 1 FROM operation_reconciliations WHERE operation_id = ? AND fence = ? AND outcome = ?",
                    (row["operation_id"], row["fence"], row["retry_proof"]),
                ).fetchone()
                if proof is None:
                    raise CorruptHistory("retry proof has no matching reconciliation")
        for row in self._connection.execute("SELECT digest, size FROM artifact_references"):
            self._verify_blob(
                self._artifact_directory / cast(str, row["digest"]), cast(str, row["digest"]), cast(int, row["size"])
            )

    @staticmethod
    def _verify_backup_manifest(directory: Path) -> None:
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            if (
                manifest["format"] != "creatidy-kernel-sqlite-backup"
                or type(manifest["schema_version"]) is not int
                or manifest["schema_version"] not in (1, 2, 3, 4, 5)
            ):
                raise ValueError("unsupported backup manifest")
            with (directory / "kernel.sqlite3").open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["database_sha256"]:
                    raise ValueError("backup database digest mismatch")
            entries = cast(list[dict[str, object]], manifest["artifacts"]) if manifest["schema_version"] >= 2 else []
            if type(entries) is not list:
                raise ValueError("invalid artifact manifest")
            with closing(
                sqlite3.connect(f"{(directory / 'kernel.sqlite3').as_uri()}?mode=ro&immutable=1", uri=True)
            ) as image:
                if image.execute("PRAGMA user_version").fetchone()[0] != manifest["schema_version"]:
                    raise ValueError("backup manifest/database schema mismatch")
                expected = (
                    sorted(image.execute("SELECT DISTINCT digest, size FROM artifact_references").fetchall())
                    if manifest["schema_version"] >= 2
                    else []
                )
            listed: list[tuple[str, int]] = []
            for entry in entries:
                digest, size = entry["digest"], entry["size"]
                if (
                    type(digest) is not str
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or type(size) is not int
                    or size < 0
                ):
                    raise ValueError("invalid artifact manifest entry")
                listed.append((digest, size))
                SQLiteProgramStore._verify_blob(directory / "kernel.sqlite3.artifacts" / digest, digest, size)
            if sorted(listed) != expected:
                raise ValueError("artifact manifest does not cover database references exactly")
        except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError) as error:
            raise CorruptHistory("backup bundle manifest verification failed") from error

    @classmethod
    def restore_backup(cls, bundle: BackupBundle, target: str | Path) -> Path:
        """Restore only a verified Kernel bundle into a new, offline database path."""
        if type(bundle) is not BackupBundle:
            raise TypeError("restore requires a Kernel BackupBundle")
        cls._verify_backup_manifest(bundle.directory)
        destination = Path(target).expanduser().resolve(strict=False)
        artifacts = destination.parent / f"{destination.name}.artifacts"
        if destination.exists() or artifacts.exists() or not destination.parent.is_dir():
            raise ValueError("restore requires a new database path and artifact directory")
        _inspect_local_storage(destination.parent)
        manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
        entries = manifest.get("artifacts", [])
        if entries:
            artifacts.mkdir(mode=0o700)
            for entry in entries:
                source = bundle.directory / "kernel.sqlite3.artifacts" / entry["digest"]
                blob = artifacts / entry["digest"]
                shutil.copyfile(source, blob)
                _fsync_file(blob)
                cls._verify_blob(blob, entry["digest"], entry["size"])
            _fsync_directory(artifacts)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream, bundle.database.open("rb") as source:
                shutil.copyfileobj(source, stream)
                stream.flush()
                os.fsync(stream.fileno())
            with open(temporary, "rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["database_sha256"]:
                    raise CorruptHistory("restored SQLite image differs from verified backup")
            os.replace(temporary, destination)
            _fsync_directory(destination.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return destination

    def export_history(self, program_id: str) -> str:
        """Export all versioned command envelopes and resulting K1 fact snapshots."""
        records = self.history(program_id)
        if not records:
            raise ProgramNotFound(f"Program {program_id!r} does not exist")
        return canonical_json(
            {
                "format": "creatidy-kernel-history-export",
                "schema_version": SCHEMA_VERSION,
                "program_id": program_id,
                "records": [
                    {
                        "sequence": record.sequence,
                        "schema_version": record.schema_version,
                        "command_key": record.command_key,
                        "command_type": record.command_type,
                        "actor_id": record.actor_id,
                        "input_digest": record.input_digest,
                        "input": json.loads(record.input_json),
                        "aggregate_revision": record.aggregate_revision,
                        "facts_digest": record.facts_digest,
                        "facts": json.loads(record.facts_json),
                    }
                    for record in records
                ],
            }
        )

    def rebuild_projections(self) -> int:
        """Rebuild current projections by decoding explicit versioned fact snapshots only."""
        self._assert_writer_thread()
        with self._gate, self._transaction():
            rows = self._connection.execute(
                "SELECT program_id, sequence, schema_version, command_key, command_type, actor_id, input_digest, "
                "input_json, aggregate_revision, facts_digest, facts_json FROM history "
                "ORDER BY program_id, sequence"
            ).fetchall()
            latest: dict[str, tuple[Program, int, str, str]] = {}
            expected_sequence: dict[str, int] = {}
            for row in rows:
                program_id = cast(str, row["program_id"])
                sequence = cast(int, row["sequence"])
                command_key = cast(str, row["command_key"])
                command_type = cast(str, row["command_type"])
                actor_id = cast(str, row["actor_id"])
                input_json = cast(str, row["input_json"])
                input_digest = cast(str, row["input_digest"])
                facts_json = cast(str, row["facts_json"])
                facts_digest = cast(str, row["facts_digest"])
                schema_version = cast(int, row["schema_version"])
                aggregate_revision = cast(int, row["aggregate_revision"])
                if schema_version != CODEC_VERSION:
                    raise CorruptHistory(f"unsupported history record schema {schema_version}")
                if not command_key.strip():
                    raise CorruptHistory("history command key is empty")
                if hashlib.sha256(facts_json.encode("utf-8")).hexdigest() != facts_digest:
                    raise CorruptHistory("history fact snapshot digest does not match its contents")
                previous_sequence = expected_sequence.get(program_id, 0)
                if sequence != previous_sequence + 1:
                    raise CorruptHistory("aggregate history sequence is not contiguous")
                if command_type == "create_program":
                    if sequence != 1 or program_id in latest:
                        raise CorruptHistory("Program creation record is not the first aggregate record")
                    validate_create_input(input_json, input_digest, program_id, actor_id)
                else:
                    if program_id not in latest:
                        raise CorruptHistory("domain command record precedes Program creation")
                    validate_history_input(input_json, input_digest, program_id, command_type, actor_id)
                try:
                    program = self._decode_facts(facts_json, program_id, aggregate_revision)
                except RecordCodecError as error:
                    raise CorruptHistory("history contains an invalid K1 fact snapshot") from error
                previous = latest.get(program_id)
                if previous is not None and program.revision < previous[0].revision:
                    raise CorruptHistory("aggregate revision regressed in authoritative history")
                if sequence == 1 and (program.revision != 0 or program.status is not ProgramStatus.DRAFT):
                    raise CorruptHistory("Program creation must record the initial K1 aggregate")
                admission = self._connection.execute(
                    "SELECT program_id, command_type, input_digest, result_sequence, result_digest, result_json "
                    "FROM command_admissions WHERE command_key = ?",
                    (command_key,),
                ).fetchone()
                if (
                    admission is None
                    or admission["program_id"] != program_id
                    or admission["command_type"] != command_type
                    or admission["input_digest"] != input_digest
                    or admission["result_sequence"] != sequence
                    or admission["result_digest"] != facts_digest
                    or admission["result_json"] != facts_json
                ):
                    raise CorruptHistory("history record and command admission are not atomically consistent")
                expected_sequence[program_id] = sequence
                latest[program_id] = (program, sequence, facts_json, facts_digest)

            self._connection.execute("DELETE FROM program_projection")
            for _program_id, (program, sequence, facts_json, facts_digest) in latest.items():
                self._write_projections(program, sequence, facts_json, facts_digest)
            return len(latest)

    def backup(self, destination: str | Path) -> BackupBundle:
        """Create an atomic backup bundle using SQLite's online backup API and a digest manifest."""
        self._assert_writer_thread()
        target = Path(destination).expanduser().resolve(strict=False)
        if target == self._path or target.exists():
            raise ValueError("backup destination must be a new directory distinct from the database")
        if not target.parent.is_dir():
            raise FileNotFoundError(f"backup parent directory does not exist: {target.parent}")
        destination_storage = _inspect_local_storage(target.parent)
        with self._gate:
            temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
            database = temporary / "kernel.sqlite3"
            manifest = temporary / "manifest.json"
            try:
                temporary_storage = _inspect_local_storage(temporary)
                if temporary_storage.mount_id != destination_storage.mount_id:
                    raise UnsupportedSQLiteConfiguration("backup staging directory changed storage mounts")
                backup_connection = sqlite3.connect(
                    f"{database.as_uri()}?cache=private",
                    isolation_level=None,
                    uri=True,
                )
                try:
                    self._connection.backup(backup_connection)
                    integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or integrity[0] != "ok":
                        raise CorruptHistory("SQLite backup failed its integrity check")
                    record_count = cast(int, backup_connection.execute("SELECT COUNT(*) FROM history").fetchone()[0])
                    schema_version = cast(int, backup_connection.execute("PRAGMA user_version").fetchone()[0])
                finally:
                    backup_connection.close()
                backup_storage = _inspect_local_storage(temporary, database_path=database)
                if backup_storage.mount_id != destination_storage.mount_id:
                    raise UnsupportedSQLiteConfiguration("backup database or sidecars changed storage mounts")
                _fsync_file(database)
                with database.open("rb") as stream:
                    file_digest = hashlib.file_digest(stream, "sha256").hexdigest()
                artifacts = (
                    []
                    if schema_version == 1
                    else [
                        (cast(str, row["digest"]), cast(int, row["size"]))
                        for row in self._connection.execute(
                            "SELECT DISTINCT digest, size FROM artifact_references ORDER BY digest"
                        )
                    ]
                )
                if artifacts:
                    artifact_target = temporary / "kernel.sqlite3.artifacts"
                    artifact_target.mkdir()
                    for digest, size in artifacts:
                        source = self._artifact_directory / digest
                        self._verify_blob(source, digest, size)
                        destination_blob = artifact_target / digest
                        shutil.copyfile(source, destination_blob)
                        _fsync_file(destination_blob)
                        self._verify_blob(destination_blob, digest, size)
                    _fsync_directory(artifact_target)
                manifest_value = {
                    "format": "creatidy-kernel-sqlite-backup",
                    "manifest_version": 1,
                    "schema_version": schema_version,
                    "sqlite_runtime_version": sqlite3.sqlite_version,
                    "history_record_count": record_count,
                    "database_sha256": file_digest,
                    "artifacts": [{"digest": digest, "size": size} for digest, size in artifacts],
                    "controller_topology": self._startup_evidence.controller_topology,
                    "cache_mode": self._startup_evidence.cache_mode,
                    "source_data_directory": str(self._storage.data_directory),
                    "source_filesystem_type": self._storage.filesystem_type,
                    "source_filesystem_mount_id": self._storage.mount_id,
                    "source_filesystem_mount_options": self._storage.mount_options,
                    "source_filesystem_super_options": self._storage.super_options,
                    "destination_data_directory": str(destination_storage.data_directory),
                    "destination_filesystem_type": destination_storage.filesystem_type,
                    "destination_filesystem_mount_id": destination_storage.mount_id,
                    "destination_filesystem_mount_options": destination_storage.mount_options,
                    "destination_filesystem_super_options": destination_storage.super_options,
                }
                with manifest.open("w", encoding="utf-8") as stream:
                    stream.write(canonical_json(manifest_value) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                _fsync_directory(temporary)
                os.replace(temporary, target)
                _fsync_directory(target.parent)
                published_storage = _inspect_local_storage(target, database_path=target / "kernel.sqlite3")
                if published_storage.mount_id != destination_storage.mount_id:
                    raise UnsupportedSQLiteConfiguration("published backup changed storage mounts")
            except BaseException:
                if temporary.exists():
                    for child in temporary.iterdir():
                        if child.is_dir():
                            for blob in child.iterdir():
                                blob.unlink()
                            child.rmdir()
                        else:
                            child.unlink()
                    temporary.rmdir()
                raise
        return BackupBundle(
            directory=target,
            database=target / "kernel.sqlite3",
            manifest=target / "manifest.json",
            sha256=file_digest,
            history_record_count=record_count,
            schema_version=schema_version,
        )

    def _configure_runtime(self) -> None:
        self._assert_writer_thread()
        connection = self._connection
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        locking_mode = connection.execute("PRAGMA main.locking_mode = EXCLUSIVE").fetchone()
        self._locking_mode = "" if locking_mode is None else str(locking_mode[0]).lower()
        if self._locking_mode != "exclusive":
            raise UnsupportedSQLiteConfiguration("SQLite did not enable main.locking_mode=EXCLUSIVE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        self._foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        self._actual_busy_timeout_ms = cast(int, connection.execute("PRAGMA busy_timeout").fetchone()[0])
        if not self._foreign_keys or self._actual_busy_timeout_ms != self._busy_timeout_ms:
            raise UnsupportedSQLiteConfiguration("SQLite is missing required FK or busy-timeout capability")

    def _acquire_exclusive_ownership(self) -> None:
        connection = self._connection
        connection.execute("BEGIN EXCLUSIVE")
        try:
            application_id = cast(int, connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id not in (0, _APPLICATION_ID):
                raise UnsupportedSQLiteConfiguration("database application_id belongs to another application")
            quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise CorruptHistory("SQLite quick_check failed")
            if str(connection.execute("PRAGMA main.locking_mode").fetchone()[0]).lower() != "exclusive":
                raise UnsupportedSQLiteConfiguration("SQLite lost EXCLUSIVE locking mode during acquisition")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        self._application_id = application_id

    def _assert_storage_topology_unchanged(self) -> None:
        current = _inspect_local_storage(self._path.parent, database_path=self._path)
        if current != self._storage:
            raise UnsupportedSQLiteConfiguration("SQLite storage topology changed during initialization")

    def _set_application_id(self) -> None:
        if self._application_id == _APPLICATION_ID:
            return
        with self._transaction():
            application_id = cast(int, self._connection.execute("PRAGMA application_id").fetchone()[0])
            if application_id not in (0, _APPLICATION_ID):
                raise UnsupportedSQLiteConfiguration("database application_id changed during initialization")
            if application_id == 0:
                self._connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        self._application_id = _APPLICATION_ID

    def _enable_wal(self) -> None:
        connection = self._connection
        mode = connection.execute("PRAGMA main.journal_mode = WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise UnsupportedSQLiteConfiguration("SQLite did not enable WAL for the file-backed database")
        self._journal_mode = str(connection.execute("PRAGMA main.journal_mode").fetchone()[0]).lower()
        self._locking_mode = str(connection.execute("PRAGMA main.locking_mode").fetchone()[0]).lower()
        self._synchronous = cast(int, connection.execute("PRAGMA synchronous").fetchone()[0])
        self._actual_busy_timeout_ms = cast(int, connection.execute("PRAGMA busy_timeout").fetchone()[0])
        if (
            self._journal_mode != "wal"
            or self._locking_mode != "exclusive"
            or self._synchronous != 2
            or not self._foreign_keys
            or self._actual_busy_timeout_ms != self._busy_timeout_ms
            or not callable(connection.backup)
        ):
            raise UnsupportedSQLiteConfiguration(
                "SQLite is missing a required EXCLUSIVE, WAL, FULL, FK, timeout, or backup capability"
            )

    def _migrate(self) -> None:
        original_version = cast(int, self._connection.execute("PRAGMA user_version").fetchone()[0])
        if original_version in (1, 2, 3):
            self._backup_before_schema_four()
        if original_version in (1, 2, 3, 4):
            self._backup_before_schema_four(target_schema=5)
        with self._transaction():
            version = cast(int, self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise UnsupportedSQLiteConfiguration(f"database schema version {version} is newer than supported")
            if version == 0:
                self._migrate_0_to_1()
                version = 1
            if version == 1:
                self._migrate_1_to_2()
                version = 2
            if version == 2:
                self._migrate_2_to_3()
                version = 3
            if version == 3:
                self._migrate_3_to_4()
                version = 4
            if version == 4:
                self._migrate_4_to_5()
                version = 5
            if version != SCHEMA_VERSION:
                raise UnsupportedSQLiteConfiguration(f"no migration path from schema version {version}")
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._validate_schema()

    def _backup_before_schema_four(self, *, target_schema: int = 4) -> None:
        # A content-qualified name reuses the same complete backup after an
        # interrupted upgrade, never overwriting an earlier recovery image.
        digest = hashlib.sha256()
        for statement in self._connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
        target = self._path.parent / f"{self._path.name}.pre-schema-{target_schema}-{digest.hexdigest()}"
        if target.exists():
            self._verify_backup_manifest(target)
            original = hashlib.sha256()
            with closing(
                sqlite3.connect(f"{(target / 'kernel.sqlite3').as_uri()}?mode=ro&immutable=1", uri=True)
            ) as backup:
                for statement in backup.iterdump():
                    original.update(statement.encode("utf-8"))
                    original.update(b"\n")
            if original.digest() != digest.digest():
                raise CorruptHistory("pre-upgrade backup differs from current original records")
        else:
            self.backup(target)

    def _migrate_0_to_1(self) -> None:
        statements = (
            """
            CREATE TABLE history (
                program_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                command_key TEXT NOT NULL UNIQUE,
                command_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                input_digest TEXT NOT NULL CHECK(length(input_digest) = 64),
                input_json TEXT NOT NULL,
                aggregate_revision INTEGER NOT NULL CHECK(aggregate_revision >= 0),
                facts_digest TEXT NOT NULL CHECK(length(facts_digest) = 64),
                facts_json TEXT NOT NULL,
                PRIMARY KEY(program_id, sequence)
            ) WITHOUT ROWID;
            """,
            """
            CREATE TRIGGER history_no_update BEFORE UPDATE ON history
                BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
            """,
            """
            CREATE TRIGGER history_no_delete BEFORE DELETE ON history
                BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
            """,
            """
            CREATE TABLE command_admissions (
                command_key TEXT PRIMARY KEY,
                program_id TEXT NOT NULL,
                command_type TEXT NOT NULL,
                input_digest TEXT NOT NULL CHECK(length(input_digest) = 64),
                result_sequence INTEGER NOT NULL,
                result_digest TEXT NOT NULL CHECK(length(result_digest) = 64),
                result_json TEXT NOT NULL,
                FOREIGN KEY(program_id, result_sequence) REFERENCES history(program_id, sequence)
            ) WITHOUT ROWID;
            """,
            """
            CREATE TRIGGER admissions_no_update BEFORE UPDATE ON command_admissions
                BEGIN SELECT RAISE(ABORT, 'command admissions are immutable'); END;
            """,
            """
            CREATE TRIGGER admissions_no_delete BEFORE DELETE ON command_admissions
                BEGIN SELECT RAISE(ABORT, 'command admissions are immutable'); END;
            """,
            """
            CREATE TABLE program_projection (
                program_id TEXT PRIMARY KEY,
                aggregate_revision INTEGER NOT NULL CHECK(aggregate_revision >= 0),
                history_sequence INTEGER NOT NULL CHECK(history_sequence > 0),
                schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                facts_digest TEXT NOT NULL CHECK(length(facts_digest) = 64),
                program_json TEXT NOT NULL,
                FOREIGN KEY(program_id, history_sequence) REFERENCES history(program_id, sequence)
            ) WITHOUT ROWID;
            """,
            """
            CREATE TABLE work_unit_projection (
                program_id TEXT NOT NULL,
                work_unit_id TEXT NOT NULL,
                status TEXT NOT NULL,
                active_attempt_id TEXT,
                satisfaction_id TEXT,
                PRIMARY KEY(program_id, work_unit_id),
                FOREIGN KEY(program_id) REFERENCES program_projection(program_id) ON DELETE CASCADE
            ) WITHOUT ROWID;
            """,
            """
            CREATE TABLE attempt_projection (
                program_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_json TEXT NOT NULL,
                PRIMARY KEY(program_id, attempt_id),
                FOREIGN KEY(program_id) REFERENCES program_projection(program_id) ON DELETE CASCADE
            ) WITHOUT ROWID;
            """,
        )
        for statement in statements:
            self._connection.execute(statement)

    def _migrate_1_to_2(self) -> None:
        statements = (
            """CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY, effect_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL, request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
                fence INTEGER NOT NULL DEFAULT 0 CHECK(fence >= 0), lease_until INTEGER,
                status TEXT NOT NULL DEFAULT 'intent' CHECK(status IN
                    ('intent','dispatched','accepted','rejected','running','waiting','terminal','unknown')),
                accepted_reference TEXT, retry_proof TEXT CHECK(retry_proof IN ('absent','idempotent'))
            ) WITHOUT ROWID""",
            """CREATE TABLE operation_outbox (
                operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id)
            ) WITHOUT ROWID""",
            """CREATE TABLE delivery_attempts (
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                fence INTEGER NOT NULL CHECK(fence > 0), claimed_at INTEGER NOT NULL,
                lease_until INTEGER NOT NULL, PRIMARY KEY(operation_id, fence)
            ) WITHOUT ROWID""",
            """CREATE TABLE operation_observations (
                observation_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL,
                fence INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN
                    ('accepted','rejected','running','waiting','terminal','unknown')),
                reference TEXT, FOREIGN KEY(operation_id, fence) REFERENCES delivery_attempts(operation_id, fence)
            ) WITHOUT ROWID""",
            """CREATE TABLE operation_transports (
                operation_id TEXT NOT NULL, fence INTEGER NOT NULL,
                succeeded INTEGER NOT NULL CHECK(succeeded IN (0,1)), PRIMARY KEY(operation_id, fence),
                FOREIGN KEY(operation_id, fence) REFERENCES delivery_attempts(operation_id, fence)
            ) WITHOUT ROWID""",
            """CREATE TABLE operation_reconciliations (
                sequence INTEGER PRIMARY KEY,
                operation_id TEXT NOT NULL, fence INTEGER NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('absent','idempotent','found','unknown')),
                reference TEXT, evidence TEXT NOT NULL CHECK(length(evidence) > 0), matched_digest TEXT,
                authoritative_absence INTEGER NOT NULL CHECK(authoritative_absence IN (0,1)),
                FOREIGN KEY(operation_id, fence) REFERENCES delivery_attempts(operation_id, fence)
            )""",
            """CREATE TABLE artifact_references (
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                name TEXT NOT NULL, digest TEXT NOT NULL CHECK(length(digest) = 64),
                size INTEGER NOT NULL CHECK(size >= 0), PRIMARY KEY(operation_id, name)
            ) WITHOUT ROWID""",
            """CREATE TRIGGER operations_no_delete BEFORE DELETE ON operations
                BEGIN SELECT RAISE(ABORT, 'operations cannot be deleted'); END""",
            """CREATE TRIGGER outbox_no_update BEFORE UPDATE ON operation_outbox
                BEGIN SELECT RAISE(ABORT, 'outbox is immutable'); END""",
            """CREATE TRIGGER outbox_no_delete BEFORE DELETE ON operation_outbox
                BEGIN SELECT RAISE(ABORT, 'outbox is immutable'); END""",
        )
        for statement in statements:
            self._connection.execute(statement)
        for table in (
            "delivery_attempts",
            "operation_observations",
            "operation_transports",
            "operation_reconciliations",
            "artifact_references",
        ):
            for action in ("UPDATE", "DELETE"):
                self._connection.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END"
                )

    def _migrate_2_to_3(self) -> None:
        # This is an authority compatibility boundary, not a data rewrite. Older
        # readers reject user_version=3 before they can ignore work-budget facts.
        self._connection.execute(
            "CREATE TABLE store_contract (schema_version INTEGER PRIMARY KEY CHECK(schema_version = 3), "
            "authority_contract TEXT NOT NULL CHECK(authority_contract = 'work-budget:v1')) WITHOUT ROWID"
        )
        self._connection.execute("INSERT INTO store_contract VALUES (3, 'work-budget:v1')")
        for action in ("UPDATE", "DELETE"):
            self._connection.execute(
                f"CREATE TRIGGER store_contract_no_{action.lower()} BEFORE {action} ON store_contract "
                "BEGIN SELECT RAISE(ABORT, 'store contract is immutable'); END"
            )

    def _migrate_3_to_4(self) -> None:
        self._connection.execute(
            "CREATE TABLE work_clock_state (program_id TEXT PRIMARY KEY, wall INTEGER NOT NULL CHECK(wall >= 0), "
            "boot_ns INTEGER NOT NULL CHECK(boot_ns >= 0), boot_id TEXT NOT NULL, "
            "clock TEXT NOT NULL CHECK(clock = 'linux:CLOCK_BOOTTIME'), "
            "pending INTEGER NOT NULL CHECK(pending IN (0,1))) WITHOUT ROWID"
        )

    def _migrate_4_to_5(self) -> None:
        # Old schema-4 readers must refuse before ignoring resource generations.
        # Existing records, IDs, clock rows and the schema-3 marker stay intact.
        self._connection.execute(
            "CREATE TABLE ownership_contract (version INTEGER PRIMARY KEY CHECK(version = 1), "
            "contract TEXT NOT NULL CHECK(contract = 'work-resource-ownership:v1')) WITHOUT ROWID"
        )
        self._connection.execute("INSERT INTO ownership_contract VALUES (1, 'work-resource-ownership:v1')")
        for action in ("UPDATE", "DELETE"):
            self._connection.execute(
                f"CREATE TRIGGER ownership_contract_no_{action.lower()} BEFORE {action} ON ownership_contract "
                "BEGIN SELECT RAISE(ABORT, 'ownership contract is immutable'); END"
            )

    def _validate_schema(self) -> None:
        tables = {
            cast(str, row[0])
            for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        required = {
            "history",
            "command_admissions",
            "program_projection",
            "work_unit_projection",
            "attempt_projection",
            "operations",
            "operation_outbox",
            "delivery_attempts",
            "operation_observations",
            "operation_transports",
            "operation_reconciliations",
            "artifact_references",
            "store_contract",
            "work_clock_state",
            "ownership_contract",
        }
        if not required <= tables:
            raise UnsupportedSQLiteConfiguration("database schema is missing required K2 tables")
        columns = {
            "operations": {
                "operation_id",
                "effect_key",
                "request_json",
                "request_digest",
                "fence",
                "lease_until",
                "status",
                "accepted_reference",
                "retry_proof",
            },
            "operation_outbox": {"operation_id"},
            "delivery_attempts": {"operation_id", "fence", "claimed_at", "lease_until"},
            "operation_observations": {"observation_id", "operation_id", "fence", "kind", "reference"},
            "operation_transports": {"operation_id", "fence", "succeeded"},
            "operation_reconciliations": {
                "sequence",
                "operation_id",
                "fence",
                "outcome",
                "reference",
                "evidence",
                "matched_digest",
                "authoritative_absence",
            },
            "artifact_references": {"operation_id", "name", "digest", "size"},
            "store_contract": {"schema_version", "authority_contract"},
            "work_clock_state": {"program_id", "wall", "boot_ns", "boot_id", "clock", "pending"},
            "ownership_contract": {"version", "contract"},
        }
        for table, expected in columns.items():
            actual = {cast(str, row["name"]) for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise UnsupportedSQLiteConfiguration(f"database schema has invalid {table} columns")
        triggers = {
            cast(str, row[0])
            for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        }
        required_triggers = {"history_no_update", "history_no_delete", "admissions_no_update", "admissions_no_delete"}
        required_triggers |= {"operations_no_delete", "outbox_no_update", "outbox_no_delete"}
        required_triggers |= {"store_contract_no_update", "store_contract_no_delete"}
        required_triggers |= {"ownership_contract_no_update", "ownership_contract_no_delete"}
        required_triggers |= {
            f"{table}_no_{action}"
            for table in (
                "delivery_attempts",
                "operation_observations",
                "operation_transports",
                "operation_reconciliations",
                "artifact_references",
            )
            for action in ("update", "delete")
        }
        if not required_triggers <= triggers:
            raise UnsupportedSQLiteConfiguration("database schema is missing append-only history constraints")
        if [
            tuple(row)
            for row in self._connection.execute("SELECT schema_version, authority_contract FROM store_contract")
        ] != [(3, "work-budget:v1")]:
            raise UnsupportedSQLiteConfiguration("database authority compatibility contract is invalid")
        if [tuple(row) for row in self._connection.execute("SELECT version, contract FROM ownership_contract")] != [
            (1, "work-resource-ownership:v1")
        ]:
            raise UnsupportedSQLiteConfiguration("database ownership compatibility contract is invalid")
        if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CorruptHistory("database contains a foreign-key violation")

    @contextmanager
    def _transaction(self, readings: dict[str, BootClockReading | None] | None = None) -> Generator[None, None, None]:
        self._assert_writer_thread()
        if readings is None:
            readings = self._prepare_work_clocks()
        self._connection.execute("BEGIN IMMEDIATE")
        self._transaction_clock_readings = readings
        try:
            yield
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        finally:
            self._transaction_clock_readings = None

    def _duplicate_result(self, command_key: str, input_digest: str) -> Program | None:
        row = self._connection.execute(
            "SELECT input_digest, result_digest, result_json FROM command_admissions WHERE command_key = ?",
            (command_key,),
        ).fetchone()
        if row is None:
            return None
        if row["input_digest"] != input_digest:
            raise IdempotencyConflict("command key was already bound to different canonical input")
        result_json = cast(str, row["result_json"])
        if hashlib.sha256(result_json.encode("utf-8")).hexdigest() != row["result_digest"]:
            raise CorruptHistory("recorded command result digest does not match its contents")
        try:
            return program_from_json(result_json)
        except RecordCodecError as error:
            raise CorruptHistory("recorded command result cannot be decoded") from error

    def _load_head(self, program_id: str) -> Program:
        row = self._connection.execute(
            "SELECT aggregate_revision, history_sequence, schema_version, facts_digest, program_json "
            "FROM program_projection WHERE program_id = ?",
            (program_id,),
        ).fetchone()
        if row is None:
            raise ProgramNotFound(f"Program {program_id!r} does not exist")
        program_json_value = cast(str, row["program_json"])
        if hashlib.sha256(program_json_value.encode("utf-8")).hexdigest() != row["facts_digest"]:
            raise CorruptHistory("Program projection digest does not match its contents")
        program = self._decode_facts(program_json_value, program_id, cast(int, row["aggregate_revision"]))
        latest = self._connection.execute(
            "SELECT sequence, facts_digest FROM history WHERE program_id = ? ORDER BY sequence DESC LIMIT 1",
            (program_id,),
        ).fetchone()
        if (
            latest is None
            or latest["sequence"] != row["history_sequence"]
            or latest["facts_digest"] != row["facts_digest"]
            or row["schema_version"] != CODEC_VERSION
        ):
            raise CorruptHistory("Program projection does not match the authoritative history head")
        return program

    def _decode_facts(self, raw: str, program_id: str, revision: int) -> Program:
        try:
            program = program_from_json(raw)
        except RecordCodecError as error:
            raise CorruptHistory("stored K1 facts cannot be decoded or validated") from error
        if program.program_id != program_id or program.revision != revision:
            raise CorruptHistory("stored K1 facts do not match their aggregate envelope")
        return program

    def _insert_history(
        self,
        *,
        program_id: str,
        sequence: int,
        command_key: str,
        command_type: str,
        actor_id: str,
        input_digest: str,
        input_json: str,
        program: Program,
        facts_json: str,
        facts_digest: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO history (program_id, sequence, schema_version, command_key, command_type, actor_id, "
            "input_digest, input_json, aggregate_revision, facts_digest, facts_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                program_id,
                sequence,
                CODEC_VERSION,
                command_key,
                command_type,
                actor_id,
                input_digest,
                input_json,
                program.revision,
                facts_digest,
                facts_json,
            ),
        )

    def _write_projections(self, program: Program, history_sequence: int, facts_json: str, facts_digest: str) -> None:
        self._connection.execute("DELETE FROM program_projection WHERE program_id = ?", (program.program_id,))
        self._connection.execute(
            "INSERT INTO program_projection (program_id, aggregate_revision, history_sequence, schema_version, "
            "facts_digest, program_json) VALUES (?, ?, ?, ?, ?, ?)",
            (program.program_id, program.revision, history_sequence, CODEC_VERSION, facts_digest, facts_json),
        )
        self._connection.executemany(
            "INSERT INTO work_unit_projection (program_id, work_unit_id, status, active_attempt_id, satisfaction_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (
                    program.program_id,
                    state.work_unit_id,
                    state.status.value,
                    state.active_attempt_id,
                    state.satisfaction_id,
                )
                for state in program.work_unit_states
            ),
        )
        self._connection.executemany(
            "INSERT INTO attempt_projection (program_id, attempt_id, status, attempt_json) VALUES (?, ?, ?, ?)",
            (
                (
                    program.program_id,
                    attempt.attempt_id,
                    attempt.status.value,
                    attempt_projection_json(attempt),
                )
                for attempt in program.attempts
            ),
        )

    def _insert_command_result(
        self,
        *,
        command_key: str,
        program_id: str,
        command_type: str,
        input_digest: str,
        result_sequence: int,
        result_json: str,
        result_digest: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO command_admissions (command_key, program_id, command_type, input_digest, result_sequence, "
            "result_digest, result_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (command_key, program_id, command_type, input_digest, result_sequence, result_digest, result_json),
        )

    def _read_startup_evidence(self) -> SQLiteStartupEvidence:
        compile_options = tuple(
            sorted(cast(str, row[0]) for row in self._connection.execute("PRAGMA compile_options").fetchall())
        )
        schema_version = cast(int, self._connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = cast(int, self._connection.execute("PRAGMA application_id").fetchone()[0])
        return SQLiteStartupEvidence(
            database_path=str(self._path),
            data_directory=str(self._storage.data_directory),
            cache_mode=self._cache_mode,
            filesystem_type=self._storage.filesystem_type,
            filesystem_mountpoint=str(self._storage.mountpoint),
            filesystem_mount_id=self._storage.mount_id,
            filesystem_mount_options=self._storage.mount_options,
            filesystem_super_options=self._storage.super_options,
            sqlite_runtime_version=sqlite3.sqlite_version,
            sqlite_runtime_version_info=sqlite3.sqlite_version_info,
            sqlite_threadsafety=sqlite3.threadsafety,
            compile_options=compile_options,
            journal_mode=self._journal_mode,
            locking_mode=self._locking_mode,
            synchronous=self._synchronous,
            foreign_keys=self._foreign_keys,
            busy_timeout_ms=self._actual_busy_timeout_ms,
            application_id=application_id,
            schema_version=schema_version,
            controller_topology="one cache=private SQLite connection; retained main.locking_mode=EXCLUSIVE",
            writer_process_id=self._owner_pid,
            writer_thread_id=self._owner_thread,
        )

    def _assert_writer_thread(self) -> None:
        if os.getpid() != self._owner_pid:
            raise WrongWriterProcess("SQLite access must use the process that created the store")
        self._assert_owner_thread()
        if getattr(self, "_closed", False):
            raise StoreClosed("SQLite store is closed")

    def _assert_owner_thread(self) -> None:
        if getattr(self._owner_thread_local, "store_token", None) is not self._owner_thread_token:
            raise WrongWriterThread("SQLite access must use the store's creating thread")

    @staticmethod
    def _validate_program_id(program_id: str) -> None:
        if type(program_id) is not str or not program_id.strip():
            raise ValueError("program_id must be a nonempty string")

    @staticmethod
    def _validate_command_key(command_key: str) -> None:
        if type(command_key) is not str or not command_key.strip():
            raise ValueError("command_key must be a nonempty string")


def _is_sqlite_lock_contention(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return type(code) is int and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _unescape_mountinfo_path(value: bytes) -> bytes:
    escaped_values = {0o040, 0o011, 0o012, 0o134}
    output = bytearray()
    offset = 0
    while offset < len(value):
        if value[offset] == 0x5C and offset + 3 < len(value):
            octal = value[offset + 1 : offset + 4]
            if all(0x30 <= digit <= 0x37 for digit in octal):
                decoded = int(octal, 8)
                if decoded in escaped_values:
                    output.append(decoded)
                    offset += 4
                    continue
        output.append(value[offset])
        offset += 1
    return bytes(output)


def _parse_mountinfo(contents: bytes) -> dict[int, _MountEntry]:
    entries: dict[int, _MountEntry] = {}
    for record in contents.split(b"\n"):
        if not record:
            continue
        before, separator, after = record.partition(b" - ")
        if not separator:
            raise UnsupportedSQLiteConfiguration("mount table contains an invalid record")
        mount_fields = [field for field in before.split(b" ") if field]
        filesystem_fields = [field for field in after.split(b" ") if field]
        if len(mount_fields) < 6 or len(filesystem_fields) < 3:
            raise UnsupportedSQLiteConfiguration("mount table record is incomplete")
        try:
            mount_id = int(mount_fields[0])
            parent_id = int(mount_fields[1])
            filesystem_type = filesystem_fields[0].decode("ascii").lower()
        except (ValueError, UnicodeDecodeError) as error:
            raise UnsupportedSQLiteConfiguration("mount table has an invalid ID or filesystem type") from error
        if mount_id in entries:
            raise UnsupportedSQLiteConfiguration("mount table contains a duplicate mount ID")
        entries[mount_id] = _MountEntry(
            mount_id=mount_id,
            parent_id=parent_id,
            device=mount_fields[2],
            root=mount_fields[3],
            mountpoint=mount_fields[4],
            mount_options=frozenset(mount_fields[5].split(b",")),
            filesystem_type=filesystem_type,
            source=filesystem_fields[1],
            super_options=frozenset(filesystem_fields[2].split(b",")),
        )
    if not entries:
        raise UnsupportedSQLiteConfiguration("mount table is empty")
    return entries


def _require_durable_mount(mount: _MountEntry) -> None:
    if mount.filesystem_type not in _LOCAL_FILESYSTEMS:
        raise UnsupportedSQLiteConfiguration(
            f"SQLite WAL requires a verified native local filesystem; found {mount.filesystem_type!r}"
        )
    if b"ro" in mount.mount_options or b"rw" not in mount.mount_options:
        raise UnsupportedSQLiteConfiguration("SQLite data directory mount is not verified writable")
    options = mount.mount_options | mount.super_options
    if mount.filesystem_type == "f2fs" and any(
        option == b"checkpoint=disable" or option.startswith(b"checkpoint=disable:") for option in options
    ):
        raise UnsupportedSQLiteConfiguration("checkpoint-disabled F2FS is not durable SQLite storage")
    if b"volatile" in options or b"fsync=volatile" in options:
        raise UnsupportedSQLiteConfiguration("volatile filesystem mode is unsupported")


def _topology_from_mount_ids(
    data_directory: Path,
    *,
    directory_mount_id: int,
    child_mount_ids: dict[str, int],
    mounts: dict[int, _MountEntry],
) -> _StorageTopology:
    directory_mount = mounts.get(directory_mount_id)
    if directory_mount is None:
        raise UnsupportedSQLiteConfiguration("data directory mount ID is absent from mountinfo")
    _require_durable_mount(directory_mount)
    for label, mount_id in child_mount_ids.items():
        mount = mounts.get(mount_id)
        if mount is None:
            raise UnsupportedSQLiteConfiguration(f"SQLite {label} mount ID is absent from mountinfo")
        _require_durable_mount(mount)
        if mount_id != directory_mount_id:
            raise UnsupportedSQLiteConfiguration(f"SQLite {label} is on a different mount from its data directory")
    return _StorageTopology(
        data_directory=data_directory,
        filesystem_type=directory_mount.filesystem_type,
        mountpoint=Path(os.fsdecode(_unescape_mountinfo_path(directory_mount.mountpoint))),
        mount_id=directory_mount.mount_id,
        mount_options=tuple(os.fsdecode(option) for option in sorted(directory_mount.mount_options)),
        super_options=tuple(os.fsdecode(option) for option in sorted(directory_mount.super_options)),
    )


def _fd_mount_id(descriptor: int) -> int:
    try:
        contents = Path(f"/proc/self/fdinfo/{descriptor}").read_bytes()
    except OSError as error:
        raise UnsupportedSQLiteConfiguration("cannot inspect descriptor mount identity") from error
    for line in contents.split(b"\n"):
        key, separator, value = line.partition(b":")
        if key == b"mnt_id" and separator:
            mount_id = value.strip(b" \t")
            if mount_id.isdigit():
                return int(mount_id)
            raise UnsupportedSQLiteConfiguration("descriptor has a malformed mount ID")
    raise UnsupportedSQLiteConfiguration("runtime does not expose fdinfo mount IDs")


def _probe_child(name: bytes, *, directory_fd: int, flags: int, label: str) -> int | None:
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise UnsupportedSQLiteConfiguration(f"cannot inspect SQLite {label} path") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise UnsupportedSQLiteConfiguration(f"SQLite {label} must be a regular single-link file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _inspect_local_storage(directory: Path, *, database_path: Path | None = None) -> _StorageTopology:
    if not sys.platform.startswith("linux"):
        raise UnsupportedSQLiteConfiguration("SQLite WAL topology requires a runtime that can inspect fdinfo mount IDs")
    data_directory = directory.expanduser().resolve(strict=True)
    if not data_directory.is_dir():
        raise UnsupportedSQLiteConfiguration("SQLite data directory must be an existing directory")
    path_flag = getattr(os, "O_PATH", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow_flag = getattr(os, "O_NOFOLLOW", None)
    if any(type(flag) is not int for flag in (path_flag, directory_flag, nofollow_flag)):
        raise UnsupportedSQLiteConfiguration("runtime lacks O_PATH/O_DIRECTORY/O_NOFOLLOW topology probes")

    descriptors: dict[str, int] = {}
    try:
        directory_flags = cast(int, path_flag) | cast(int, directory_flag) | cast(int, nofollow_flag) | os.O_CLOEXEC
        try:
            directory_fd = os.open(data_directory, directory_flags)
        except OSError as error:
            raise UnsupportedSQLiteConfiguration("cannot open SQLite data directory for topology validation") from error
        descriptors["data_directory"] = directory_fd
        directory_mount_id = _fd_mount_id(directory_fd)
        child_mount_ids: dict[str, int] = {}

        if database_path is not None:
            database = database_path.expanduser().resolve(strict=False)
            if database.parent != data_directory:
                raise UnsupportedSQLiteConfiguration("SQLite database must be directly inside its data directory")
            child_flags = cast(int, path_flag) | cast(int, nofollow_flag) | os.O_CLOEXEC
            child_names = (("database", os.fsencode(database.name)),) + tuple(
                (f"sidecar {suffix}", os.fsencode(database.name) + suffix.encode("ascii"))
                for suffix in ("-wal", "-shm", "-journal")
            )
            for label, name in child_names:
                descriptor = _probe_child(name, directory_fd=directory_fd, flags=child_flags, label=label)
                if descriptor is None:
                    continue
                descriptors[label] = descriptor
                child_mount_ids[label] = _fd_mount_id(descriptor)

        try:
            mounts = _parse_mountinfo(Path("/proc/self/mountinfo").read_bytes())
        except OSError as error:
            raise UnsupportedSQLiteConfiguration("cannot read the Linux mount table") from error
        topology = _topology_from_mount_ids(
            data_directory,
            directory_mount_id=directory_mount_id,
            child_mount_ids=child_mount_ids,
            mounts=mounts,
        )

        # O_PATH probes do not take SQLite POSIX locks and keep their mount IDs from being reused.
        for label, descriptor in descriptors.items():
            expected_id = directory_mount_id if label == "data_directory" else child_mount_ids[label]
            if _fd_mount_id(descriptor) != expected_id:
                raise UnsupportedSQLiteConfiguration(f"SQLite {label} mount identity changed during validation")
        second_mounts = _parse_mountinfo(Path("/proc/self/mountinfo").read_bytes())
        for label, expected_id in (("data directory", directory_mount_id), *child_mount_ids.items()):
            if second_mounts.get(expected_id) != mounts.get(expected_id):
                raise UnsupportedSQLiteConfiguration(f"SQLite {label} mount options changed during validation")
        return topology
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def validate_local_storage(directory: Path, *, database_path: Path | None = None) -> str:
    """Read-only readiness using the same native WAL topology gate as store startup."""
    return _inspect_local_storage(directory, database_path=database_path).filesystem_type


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
