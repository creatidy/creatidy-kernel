# SPDX-License-Identifier: Apache-2.0
"""Single-controller SQLite persistence for versioned K1 facts and projections."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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
from creatidy_kernel.core.domain import (
    DomainCommandType,
    Program,
    ProgramSpec,
    ProgramStatus,
)

SCHEMA_VERSION = 1
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

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS) -> None:
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        raw_path = str(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise UnsupportedSQLiteConfiguration("the store requires a file-backed local SQLite database path")
        self._path = Path(path).expanduser().resolve(strict=False)
        if not self._path.parent.is_dir():
            raise FileNotFoundError(f"SQLite database parent directory does not exist: {self._path.parent}")
        if self._path.exists() and not self._path.is_file():
            raise UnsupportedSQLiteConfiguration("SQLite database path must be a regular file")
        self._storage = _inspect_local_storage(self._path.parent, database_path=self._path)
        self._owner_pid = os.getpid()
        self._owner_thread = threading.get_ident()
        self._owner_thread_local = threading.local()
        self._owner_thread_token = object()
        self._owner_thread_local.store_token = self._owner_thread_token
        self._gate = threading.RLock()
        self._busy_timeout_ms = busy_timeout_ms
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
            self._migrate()
            self.rebuild_projections()
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
        self._validate_command_key(command_key)
        if type(spec) is not ProgramSpec:
            raise TypeError("spec must be an exact K1 ProgramSpec")
        input_json = create_input_json(spec)
        input_digest = digest_json(json.loads(input_json))
        with self._gate, self._transaction():
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

    def admit(self, program_id: str, command_key: str, command: DomainCommandType) -> Program:
        self._assert_writer_thread()
        self._validate_program_id(program_id)
        self._validate_command_key(command_key)
        input_json = command_input_json(program_id, command)
        input_digest = digest_json(json.loads(input_json))
        command_type = cast(str, json.loads(input_json)["command_type"])
        with self._gate, self._transaction():
            duplicate = self._duplicate_result(command_key, input_digest)
            if duplicate is not None:
                return duplicate
            current = self._load_head(program_id)
            next_program = current.apply(command)
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
                manifest_value = {
                    "format": "creatidy-kernel-sqlite-backup",
                    "manifest_version": 1,
                    "schema_version": schema_version,
                    "sqlite_runtime_version": sqlite3.sqlite_version,
                    "history_record_count": record_count,
                    "database_sha256": file_digest,
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
        with self._transaction():
            version = cast(int, self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise UnsupportedSQLiteConfiguration(f"database schema version {version} is newer than supported")
            if version == 0:
                self._migrate_0_to_1()
                version = 1
            if version != SCHEMA_VERSION:
                raise UnsupportedSQLiteConfiguration(f"no migration path from schema version {version}")
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._validate_schema()

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

    def _validate_schema(self) -> None:
        tables = {
            cast(str, row[0])
            for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        required = {"history", "command_admissions", "program_projection", "work_unit_projection", "attempt_projection"}
        if not required <= tables:
            raise UnsupportedSQLiteConfiguration("database schema is missing required K2 tables")
        triggers = {
            cast(str, row[0])
            for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        }
        required_triggers = {"history_no_update", "history_no_delete", "admissions_no_update", "admissions_no_delete"}
        if not required_triggers <= triggers:
            raise UnsupportedSQLiteConfiguration("database schema is missing append-only history constraints")
        if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise CorruptHistory("database contains a foreign-key violation")

    @contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        self._assert_writer_thread()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

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
