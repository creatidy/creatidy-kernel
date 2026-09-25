# SPDX-License-Identifier: Apache-2.0
"""Synthetic K2B crash and effect-boundary regressions."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest
from test_sqlite_store import spec

from creatidy_kernel.adapters.sqlite_store import BackupBundle, CorruptHistory, OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.domain import ActivateProgram, PauseProgram, ProgramStatus

pytest_plugins = ["test_sqlite_store"]


def test_b_intent_is_not_dispatch_or_review_acceptance(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        program = store.create(spec(), "command-1")
        op = store.intent("review-1", "logical-review-1", {"head": "abc", "action": "review"})
        assert op.status == "intent" and op.attempts == 0
        assert json.loads(op.request_json) == {"version": 1, "request": {"head": "abc", "action": "review"}}
        assert op.request_digest == hashlib.sha256(op.request_json.encode()).hexdigest()
        assert store.load(program.program_id).status is ProgramStatus.DRAFT
        with pytest.raises(OperationConflict):
            store.intent("review-2", "logical-review-1", {"head": "abc"})
        with pytest.raises(OperationConflict):
            store.intent("review-1", "logical-review-1", {"head": "changed"})
    with SQLiteProgramStore(path) as reopened:
        assert reopened.operation("review-1").status == "intent"
        assert reopened.load("program-1").status is ProgramStatus.DRAFT
        fence = reopened.claim("review-1", now=10, lease_seconds=5)
        assert fence == 1
        assert reopened.operation("review-1").status == "dispatched"
        with pytest.raises(OperationConflict):
            reopened.observe("review-1", fence, "wait-1", "waiting", reference="review:1")
        assert reopened.load("program-1").status is ProgramStatus.DRAFT


def test_d_transport_success_is_not_domain_acceptance(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        store.intent("review", "effect", {"head": "abc"})

        def fake(effect_key: str, request: str) -> tuple[bool, str | None, str | None]:
            assert effect_key == "effect"
            assert json.loads(request)["request"] == {"head": "abc"}
            assert store.operation("review").attempts == 1
            connection_attribute = "_connection"
            assert not cast(sqlite3.Connection, getattr(store, connection_attribute)).in_transaction
            return True, "rejected", "invalid-head"

        rejected = store.deliver_fake("review", "receipt-rejected", now=0, lease_seconds=2, fake=fake)
        fence = rejected.fence
        connection_attribute = "_connection"
        transport_row = (
            cast(sqlite3.Connection, getattr(store, connection_attribute))
            .execute("SELECT succeeded FROM operation_transports WHERE operation_id = 'review'")
            .fetchone()
        )
        assert transport_row[0] == 1
        assert rejected.status == "rejected" and rejected.accepted_reference is None
        assert store.observe("review", fence, "receipt-rejected", "rejected", reference="invalid-head") == rejected
        with pytest.raises(OperationConflict):
            store.observe("review", fence, "receipt-rejected", "accepted", reference="review:1")
        with pytest.raises(OperationConflict):
            store.claim("review", now=3, lease_seconds=2)


def test_e_missing_rejected_accepted_and_uncertain_receipts(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        store.intent("review", "effect", {"head": "abc"})

        def crash_after_possible_effect(effect_key: str, request: str) -> tuple[bool, str | None, str | None]:
            assert effect_key == "effect" and request
            raise RuntimeError("synthetic crash after effect")

        with pytest.raises(RuntimeError, match="synthetic crash"):
            store.deliver_fake("review", "lost-receipt", now=10, lease_seconds=2, fake=crash_after_possible_effect)
        fence = store.operation("review").fence
        with pytest.raises(OperationConflict, match="lease"):
            store.claim("review", now=11, lease_seconds=2)
    with SQLiteProgramStore(path) as store:
        assert store.operation("review").status == "dispatched"
        with pytest.raises(OperationConflict, match="reconciliation"):
            store.claim("review", now=12, lease_seconds=2)
        assert store.reconcile("review", fence, "unknown", now=12, evidence="lookup incomplete").status == "unknown"
        with pytest.raises(OperationConflict):
            store.claim("review", now=13, lease_seconds=2)
        with pytest.raises(OperationConflict, match="authoritative"):
            store.reconcile("review", fence, "absent", now=13, evidence="one search page")
        store.reconcile(
            "review", fence, "absent", now=13, evidence="authoritative empty lookup", authoritative_absence=True
        )
        second = store.claim("review", now=14, lease_seconds=2)
        assert second == 2
        with pytest.raises(OperationConflict, match="stale"):
            store.observe("review", fence, "old-callback", "accepted", reference="review:1")
        with pytest.raises(OperationConflict):
            store.observe("review", second, "missing-reference", "accepted")
        accepted = store.observe("review", second, "remote-event-1", "accepted", reference="review:1")
        assert accepted.accepted_reference == "review:1"
        with pytest.raises(OperationConflict):
            store.observe("review", second, "remote-event-1", "accepted", reference="review:2")
        with pytest.raises(OperationConflict):
            store.observe("review", second, "wrong-wait", "waiting", reference="review:2")
        assert store.observe("review", second, "matching-wait", "waiting", reference="review:1").status == "waiting"
        with pytest.raises(OperationConflict):
            store.claim("review", now=17, lease_seconds=2)
        assert store.observe("review", second, "finished", "terminal", reference="review:1").status == "terminal"
        with pytest.raises(OperationConflict):
            store.observe("review", second, "late", "running", reference="review:1")


def test_failed_transport_is_recorded_without_inventing_domain_receipt(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        store.intent("op", "effect", {"head": "abc"})

        def fake(_key: str, _request: str) -> tuple[bool, str | None, str | None]:
            return False, None, None

        result = store.deliver_fake("op", "not-a-receipt", now=1, lease_seconds=2, fake=fake)
        assert result.status == "dispatched" and result.accepted_reference is None
        connection_attribute = "_connection"
        connection = cast(sqlite3.Connection, getattr(store, connection_attribute))
        assert connection.execute("SELECT succeeded FROM operation_transports").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM operation_observations").fetchone()[0] == 0
        with pytest.raises(OperationConflict):
            store.claim("op", now=3, lease_seconds=2)


def test_k2a_migration_and_artifacts_survive_bundle_with_manifest(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        expected = store.create(spec(), "same-command")
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE artifact_references")
    connection.execute("DROP TABLE operation_reconciliations")
    connection.execute("DROP TABLE operation_observations")
    connection.execute("DROP TABLE operation_transports")
    connection.execute("DROP TABLE delivery_attempts")
    connection.execute("DROP TABLE operation_outbox")
    connection.execute("DROP TABLE operations")
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    old_bundle_dir = sqlite_tmp_path / "k2a-bundle"
    old_bundle_dir.mkdir()
    old_database = old_bundle_dir / "kernel.sqlite3"
    old_database.write_bytes(path.read_bytes())
    old_digest = hashlib.sha256(old_database.read_bytes()).hexdigest()
    old_manifest = old_bundle_dir / "manifest.json"
    old_manifest.write_text(
        json.dumps({"format": "creatidy-kernel-sqlite-backup", "schema_version": 1, "database_sha256": old_digest})
    )
    old_bundle = BackupBundle(old_bundle_dir, old_database, old_manifest, old_digest, 1, 1)
    migrated = SQLiteProgramStore.restore_backup(old_bundle, sqlite_tmp_path / "migrated.sqlite3")
    with SQLiteProgramStore(migrated) as old_restore:
        assert old_restore.load("program-1") == expected
        assert old_restore.startup_evidence.schema_version == 2
    with SQLiteProgramStore(path) as store:
        assert store.load("program-1") == expected
        assert store.create(spec(), "same-command") == expected
        store.intent("op", "effect", {"purpose": "artifact"})
        ref = store.finalize_artifact("op", "result", b"durable evidence")
        assert ref == "sha256:" + hashlib.sha256(b"durable evidence").hexdigest()
        assert store.finalize_artifact("op", "result", b"durable evidence") == ref
        with pytest.raises(OperationConflict):
            store.finalize_artifact("op", "result", b"different")
        bundle = store.backup(sqlite_tmp_path / "bundle")
    manifest = json.loads(bundle.manifest.read_text())
    assert manifest["artifacts"] == [{"digest": ref[7:], "size": len(b"durable evidence")}]
    restored_path = SQLiteProgramStore.restore_backup(bundle, sqlite_tmp_path / "restored.sqlite3")
    with SQLiteProgramStore(restored_path) as restored:
        assert restored.artifact("op", "result") == b"durable evidence"
        assert restored.load("program-1") == expected
    with SQLiteProgramStore(restored_path) as reopened_restore:
        assert reopened_restore.artifact("op", "result") == b"durable evidence"
    with SQLiteProgramStore(bundle.database) as restored:
        assert restored.artifact("op", "result") == b"durable evidence"
        assert restored.load("program-1") == expected
    with SQLiteProgramStore(path) as reopened:
        assert reopened.artifact("op", "result") == b"durable evidence"
    blob = bundle.directory / "kernel.sqlite3.artifacts" / ref[7:]
    blob.write_bytes(b"tampered")
    with pytest.raises(CorruptHistory):
        SQLiteProgramStore(bundle.database)


def test_restart_rejects_forged_acceptance_and_missing_outbox(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        store.intent("review", "effect", {"head": "abc"})
        store.claim("review", now=1, lease_seconds=2)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE operations SET status = 'accepted', accepted_reference = 'forged' WHERE operation_id = 'review'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(CorruptHistory, match="acceptance"):
        SQLiteProgramStore(path)


def test_command_and_effect_outbox_are_one_transaction(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        store.create(spec(), "create")
        command = ActivateProgram(0, "owner")
        expected = store.admit_with_intent("program-1", "activate", command, "op", "effect", {"head": "abc"})
        assert expected.status is ProgramStatus.ACTIVE
        assert store.operation("op").status == "intent"
        assert store.admit_with_intent("program-1", "activate", command, "op", "effect", {"head": "abc"}) == expected
        with pytest.raises(OperationConflict):
            store.admit_with_intent("program-1", "activate", command, "op2", "effect2", {"head": "abc"})
        with pytest.raises(OperationConflict):
            store.admit_with_intent("program-1", "pause", PauseProgram(1, "owner"), "op", "effect", {"head": "x"})
        assert len(store.history("program-1")) == 2
        assert store.operation("op").request_digest
        store.intent("preexisting", "preexisting-effect", {"head": "abc"})
        with pytest.raises(OperationConflict, match="previously committed"):
            store.admit_with_intent(
                "program-1", "pause", PauseProgram(1, "owner"), "preexisting", "preexisting-effect", {"head": "abc"}
            )
        assert len(store.history("program-1")) == 2


def test_reconciliation_requires_original_request_for_idempotent_retry(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        op = store.intent("op", "stable-effect", {"head": "abc"})
        first = store.claim("op", now=1, lease_seconds=2)
        with pytest.raises(OperationConflict, match="lease"):
            store.reconcile("op", first, "idempotent", now=2, evidence="same-key API", matched_digest=op.request_digest)
        with pytest.raises(OperationConflict, match="original request"):
            store.reconcile("op", first, "idempotent", now=3, evidence="different input", matched_digest="0" * 64)
        store.reconcile("op", first, "idempotent", now=3, evidence="same-key API", matched_digest=op.request_digest)
        with pytest.raises(OperationConflict, match="late callback"):
            store.observe("op", first, "late", "accepted", reference="remote:1")
        assert store.claim("op", now=4, lease_seconds=2) == first + 1


def test_outbox_failure_rolls_back_command_admission(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        store.create(spec(), "create")
        connection_attribute = "_connection"
        connection = cast(sqlite3.Connection, getattr(store, connection_attribute))
        connection.execute(
            "CREATE TRIGGER synthetic_outbox_failure BEFORE INSERT ON operation_outbox "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
            store.admit_with_intent(
                "program-1", "activate", ActivateProgram(0, "owner"), "op", "effect", {"head": "abc"}
            )
        assert len(store.history("program-1")) == 1
        assert store.load("program-1").status is ProgramStatus.DRAFT
        assert connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM command_admissions").fetchone()[0] == 1


def test_restoration_rejects_changed_bundle_bytes(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path) as store:
        store.intent("op", "effect", {"action": "fake"})
        bundle = store.backup(sqlite_tmp_path / "bundle")
    bundle.database.write_bytes(b"invalid database bytes")
    with pytest.raises(CorruptHistory, match="manifest"):
        SQLiteProgramStore.restore_backup(bundle, sqlite_tmp_path / "restored.sqlite3")
    assert not (sqlite_tmp_path / "restored.sqlite3").exists()
