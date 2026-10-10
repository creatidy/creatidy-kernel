# SPDX-License-Identifier: Apache-2.0
"""Durable authority admission over synthetic requests, never worker execution."""

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from creatidy_kernel.adapters.sqlite_store import (
    CorruptHistory,
    OperationConflict,
    SQLiteProgramStore,
    UnsupportedSQLiteConfiguration,
    WrongWriterThread,
)
from creatidy_kernel.core.authority import AuthorityDenied, AuthorityGrant, OperationIntent, Principal
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AuthorityEnvelope,
    CancelAttempt,
    PolicyReference,
    PrepareAttempt,
    ProgramSpec,
    WorkUnit,
)
from creatidy_kernel.ports.authority import AuthorityBroker
from creatidy_kernel.ports.program_store import OperationRecord

pytest_plugins = ["test_sqlite_store"]


def resolve(record: OperationRecord) -> OperationIntent:
    """Interpret this fixture producer's complete original bytes, not worker assertions."""
    envelope = cast(dict[str, object], json.loads(record.request_json))
    request = cast(dict[str, str], envelope["request"])
    return OperationIntent(
        record.operation_id,
        record.effect_key,
        record.request_digest,
        request["operation"],
        request["repository"],
        Path(request["path"]),
    )


def original(store: SQLiteProgramStore, operation_id: str = "op-1") -> OperationIntent:
    return resolve(
        store.intent(
            operation_id,
            f"effect:{operation_id}",
            {
                "operation": "write",
                "repository": "repo-1",
                "path": "work/output",
            },
        )
    )


@pytest.fixture
def authority(sqlite_tmp_path: Path) -> Iterator[tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path]]:
    tmp_path = sqlite_tmp_path
    (tmp_path / "work").mkdir()
    database = tmp_path / "kernel.db"
    with SQLiteProgramStore(database, resolve_authority_intent=resolve) as store:
        program = store.create(
            ProgramSpec(
                "program-1",
                "bounded synthetic work",
                (WorkUnit("unit-1", "produce a candidate", acceptance_criteria=("candidate is verified",)),),
                authority=AuthorityEnvelope("owner-1"),
                acceptance_criteria=("bounded result is independently accepted",),
                policy_references=(PolicyReference("acceptance", "v1", "sha256:synthetic-policy"),),
            ),
            "create",
        )
        program = store.admit("program-1", "activate", ActivateProgram(program.revision, "owner-1"))
        attempt = AttemptSpec("attempt-1", "program-1", "unit-1", program.spec.revision, program.spec.digest)
        store.admit("program-1", "prepare", PrepareAttempt(program.revision, "owner-1", attempt))
        grant = AuthorityGrant(
            "grant-1",
            "owner-1",
            attempt.digest,
            attempt.program_id,
            attempt.spec_digest,
            "repo-1",
            tmp_path,
            frozenset({tmp_path / "work"}),
            frozenset(),
            frozenset({"read", "write"}),
            100,
            True,
        )
        yield store, attempt, grant, database


def test_issue_owner_and_operation_are_independently_authorized(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, attempt, grant, _ = authority
    broker: AuthorityBroker = store
    for principal in (Principal(attempt.attempt_id, "worker"), Principal("not-owner", "owner")):
        with pytest.raises(AuthorityDenied):
            broker.issue(principal, replace(grant, issuer=principal.actor_id))
    with pytest.raises(AuthorityDenied, match="durable Attempt"):
        broker.issue(Principal("owner-1", "owner"), replace(grant, subject="different-attempt"))
    assert broker.issue(Principal("owner-1", "owner"), grant) == grant
    assert store.operations() == ()  # Grant management never becomes a dispatchable effect.
    with pytest.raises(AuthorityDenied, match="already exists"):
        broker.issue(Principal("owner-1", "owner"), grant)
    intent = original(store)
    for principal in (Principal("owner-1", "owner"), Principal("other-attempt", "worker")):
        with pytest.raises(AuthorityDenied):
            broker.check(principal, grant.grant_id, attempt, intent, 1)
    with pytest.raises(AuthorityDenied):
        broker.check(
            Principal(attempt.attempt_id, "worker"),
            grant.grant_id,
            AttemptSpec(
                attempt.attempt_id,
                attempt.program_id,
                attempt.work_unit_id,
                attempt.spec_revision,
                attempt.spec_digest,
                context_reference="changed",
            ),
            intent,
            1,
        )


def test_single_use_exact_binding_survives_reopen(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, attempt, grant, database = authority
    store.issue(Principal("owner-1", "owner"), grant)
    intent = original(store)
    worker = Principal(attempt.attempt_id, "worker")
    assert store.check(worker, grant.grant_id, attempt, intent, 1) == intent
    assert store.check(worker, grant.grant_id, attempt, intent, 2) == intent
    assert store.operation(intent.operation_id).attempts == 0
    assert store.operation(intent.operation_id).status == "intent"
    store.close()
    with SQLiteProgramStore(database, resolve_authority_intent=resolve) as reopened:
        assert reopened.check(worker, grant.grant_id, attempt, intent, 3) == intent
        with pytest.raises(AuthorityDenied, match="consumed"):
            reopened.check(worker, grant.grant_id, attempt, original(reopened, "op-2"), 3)
        assert reopened.operation("op-2").attempts == 0


def test_changed_input_or_grant_never_rebinds_operation(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, attempt, grant, _ = authority
    owner = Principal("owner-1", "owner")
    worker = Principal(attempt.attempt_id, "worker")
    store.issue(owner, grant)
    intent = original(store)
    for changed in (
        replace(intent, request_digest="different"),
        replace(intent, effect_key="other"),
        replace(intent, path=Path("work/other")),
        replace(intent, repository="other"),
        replace(intent, path=Path("../outside")),
        replace(intent, destination="outside.example.test"),
        replace(intent, operation="execute"),
    ):
        with pytest.raises(AuthorityDenied):
            store.check(worker, grant.grant_id, attempt, changed, 1)
    assert store.check(worker, grant.grant_id, attempt, intent, 1) == intent
    store.issue(owner, replace(grant, grant_id="grant-2"))
    with pytest.raises(AuthorityDenied, match="different authority"):
        store.check(worker, "grant-2", attempt, intent, 1)


def test_expiry_revocation_cancel_and_current_principal_are_rechecked(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, attempt, grant, database = authority
    owner = Principal("owner-1", "owner")
    worker = Principal(attempt.attempt_id, "worker")
    store.issue(owner, grant)
    intent = original(store)
    store.check(worker, grant.grant_id, attempt, intent, 1)
    with pytest.raises(AuthorityDenied, match="expired"):
        store.check(worker, grant.grant_id, attempt, intent, 100)
    with pytest.raises(AuthorityDenied):
        store.revoke(worker, grant.grant_id)
    assert store.revoke(owner, grant.grant_id).revoked
    store.close()
    with SQLiteProgramStore(database, resolve_authority_intent=resolve) as reopened:
        with pytest.raises(AuthorityDenied, match="revoked"):
            reopened.check(worker, grant.grant_id, attempt, intent, 2)
        other = replace(grant, grant_id="grant-2")
        reopened.issue(owner, other)
        program = reopened.load(attempt.program_id)
        reopened.admit(attempt.program_id, "cancel", CancelAttempt(program.revision, "owner-1", attempt.attempt_id))
        with pytest.raises(AuthorityDenied, match="authority changed"):
            reopened.check(worker, other.grant_id, attempt, intent, 2)


def test_resolver_failure_rolls_back_consumption_and_dispatch_requires_prior_admission(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, attempt, grant, database = authority
    owner = Principal("owner-1", "owner")
    worker = Principal(attempt.attempt_id, "worker")
    store.issue(owner, grant)
    intent = original(store)
    store.close()

    def failing(_record: OperationRecord) -> OperationIntent:
        raise RuntimeError("synthetic interrupted interpretation")

    with SQLiteProgramStore(database, resolve_authority_intent=failing) as reopened:
        with pytest.raises(RuntimeError, match="interrupted"):
            reopened.check(worker, grant.grant_id, attempt, intent, 1)
    with SQLiteProgramStore(database, resolve_authority_intent=resolve) as reopened:
        assert reopened.check(worker, grant.grant_id, attempt, intent, 1) == intent
        fence = reopened.claim(intent.operation_id, now=1, lease_seconds=1)
        # Broker replay neither claims again nor establishes permission to retry.
        assert reopened.check(worker, grant.grant_id, attempt, intent, 1) == intent
        assert reopened.operation(intent.operation_id).fence == fence
        with pytest.raises(OperationConflict, match="reconciliation"):
            reopened.claim(intent.operation_id, now=3, lease_seconds=1)
        other = replace(grant, grant_id="grant-2")
        reopened.issue(owner, other)
        dispatched = original(reopened, "already-dispatched")
        reopened.claim(dispatched.operation_id, now=1, lease_seconds=1)
        with pytest.raises(AuthorityDenied, match="retroactively"):
            reopened.check(worker, other.grant_id, attempt, dispatched, 1)


def test_no_request_interpretation_no_authority_and_thread_is_not_a_controller(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, attempt, grant, database = authority
    store.issue(Principal("owner-1", "owner"), grant)
    intent = original(store)
    errors: list[Exception] = []

    def foreign_thread() -> None:
        try:
            store.check(Principal(attempt.attempt_id, "worker"), grant.grant_id, attempt, intent, 1)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=foreign_thread)
    thread.start()
    thread.join(timeout=5)
    assert len(errors) == 1 and isinstance(errors[0], WrongWriterThread)
    store.close()
    with SQLiteProgramStore(database) as reopened:
        with pytest.raises(AuthorityDenied, match="interpretation"):
            reopened.check(Principal(attempt.attempt_id, "worker"), grant.grant_id, attempt, intent, 1)


def test_additive_schema_migration_preserves_history_and_original_requests(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, _, _, database = authority
    intent = original(store)
    history = store.history("program-1")
    operation = store.operation(intent.operation_id)
    store.close()
    with closing(sqlite3.connect(database)) as connection, connection:
        for table in ("authority_bindings", "authority_revocations", "authority_grants"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 2")
    with SQLiteProgramStore(database) as reopened:
        assert reopened.startup_evidence.schema_version == 3
        assert reopened.history("program-1") == history
        assert reopened.operation(intent.operation_id) == operation
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(UnsupportedSQLiteConfiguration, match="newer"):
        SQLiteProgramStore(database)


def test_future_authority_record_is_refused_without_rewriting_it(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, _, _, database = authority
    store.close()
    payload = '{"grant":{},"version":999}'
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "INSERT INTO authority_grants VALUES (?, ?, ?)",
            ("future", payload, hashlib.sha256(payload.encode()).hexdigest()),
        )
    with pytest.raises(CorruptHistory, match="unsupported authority"):
        SQLiteProgramStore(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        assert (
            connection.execute("SELECT grant_json FROM authority_grants WHERE grant_id='future'").fetchone()[0]
            == payload
        )


def test_workspace_retirement_preserves_history_but_cannot_renew_authority(
    authority: tuple[SQLiteProgramStore, AttemptSpec, AuthorityGrant, Path],
) -> None:
    store, attempt, grant, database = authority
    root = database.parent / "retired-workspace"
    (root / "work").mkdir(parents=True)
    bounded = replace(grant, root=root, paths=frozenset({root / "work"}))
    store.issue(Principal("owner-1", "owner"), bounded)
    intent = original(store)
    worker = Principal(attempt.attempt_id, "worker")
    store.check(worker, bounded.grant_id, attempt, intent, 1)
    history = store.history(attempt.program_id)
    operation = store.operation(intent.operation_id)
    (root / "work").rmdir()
    root.rmdir()
    store.close()
    with SQLiteProgramStore(database, resolve_authority_intent=resolve) as reopened:
        assert reopened.history(attempt.program_id) == history
        assert reopened.operation(intent.operation_id) == operation
        with pytest.raises(AuthorityDenied, match="root"):
            reopened.check(worker, bounded.grant_id, attempt, intent, 2)
        assert reopened.revoke(Principal("owner-1", "owner"), bounded.grant_id).revoked
        with pytest.raises(AuthorityDenied, match="revoked"):
            reopened.check(worker, bounded.grant_id, attempt, intent, 2)
        bundle = reopened.backup(database.parent / "backup")
    with SQLiteProgramStore(bundle.database, resolve_authority_intent=resolve) as restored:
        assert restored.operation(intent.operation_id) == operation
        assert restored.history(attempt.program_id) == history
