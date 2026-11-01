# SPDX-License-Identifier: Apache-2.0
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
from test_clock_transaction_boundaries import connection
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path
from test_work_budget import Clock, initialize, policy, prepare
from test_work_ownership import claim, client, workspace

from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram
from creatidy_kernel.core.intake import digest, encode
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.served import ProgressBuffer, ProgressPosition, ProgressRefused, command_prefix
from creatidy_kernel.ports.served import OwnerProjection

PRODUCER = "a" * 32


def test_replay_is_scoped_bounded_and_read_only() -> None:
    events = ProgressBuffer(PRODUCER, "program-a", capacity=2)
    start = events.position
    first = events.append("snapshot-changed", 1)
    second = events.append("decision-required", 2)
    assert events.read(start, limit=1).events == (first,)
    assert events.read(first.position).events == (second,)
    assert events.read(first.position) == events.read(first.position)
    assert events.position == second.position
    events.append("snapshot-changed", 3)
    assert events.read(start).gap is True
    assert events.read(first.position).gap is False


def test_restart_scope_and_future_position_require_resync() -> None:
    events = ProgressBuffer(PRODUCER, "program-a")
    for position in (
        ProgressPosition("b" * 32, "program-a", 0),
        ProgressPosition(PRODUCER, "program-b", 0),
        ProgressPosition(PRODUCER, "program-a", 1),
    ):
        assert events.read(position).gap is True


def test_invalid_event_is_not_appended() -> None:
    events = ProgressBuffer(PRODUCER, "program-a", capacity=1)
    with pytest.raises(ProgressRefused):
        events.append("arbitrary-private-payload", 0)
    assert events.position.sequence == 0


def test_snapshot_uses_existing_owner_and_read_does_not_admit_or_dispatch(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "served.db") as store:
        program = store.create(reference_spec(), "create")
        projection = OwnerProjection(store, program.program_id, capacity=2)
        before_history, before_operations = store.history(program.program_id), store.operations()
        first = projection.snapshot()
        assert first.status == "draft" and first.position.sequence == 0
        assert first.route_identity is None and first.physical_settlement is None
        assert projection.snapshot() == first
        assert projection.events(first.position).events == ()
        assert (store.history(program.program_id), store.operations()) == (before_history, before_operations)
        store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
        assert projection.events(first.position).gap is True
        current = projection.snapshot()
        assert current.status == "active" and current.position.sequence == 1
        assert projection.events(current.position).events == ()


def test_absent_program_is_explicit_and_creation_requires_resync(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "absent.db") as store:
        spec = reference_spec()
        projection = OwnerProjection(store, spec.program_id)
        absent = projection.snapshot()
        assert absent.status == "absent" and absent.program_revision is None and absent.spec_digest is None
        store.create(spec, "create")
        assert projection.events(absent.position).gap is True
        created = projection.snapshot()
        assert created.status == "draft" and created.spec_digest == spec.digest


def test_snapshot_correlates_real_reservation_without_claiming_usage_or_settlement(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "budgeted.db", boot_clock=Clock()) as store:
        program = initialize(store)
        assert store.find_operation("missing") is None
        projection = OwnerProjection(store, program.program_id)
        before = projection.snapshot()
        prepare(store, program, "worker-attempt")
        current = projection.snapshot()
        assert current.position.sequence == 1
        assert current.missing_usage == "unknown"
        assert current.budget_revoked is False
        assert current.physical_settlement is None
        assert current.reservations == (("worker-attempt", "runtime:worker-attempt", "attempt", "intent"),)
        assert store.find_operation("runtime:worker-attempt") == store.operation("runtime:worker-attempt")
        assert projection.events(before.position).gap is True
        store.claim("runtime:worker-attempt", now=100, lease_seconds=5)
        changed = projection.snapshot()
        assert changed.program_revision == current.program_revision
        assert changed.position.sequence == current.position.sequence + 1
        assert changed.reservations == (("worker-attempt", "runtime:worker-attempt", "attempt", "dispatched"),)
        store.record_usage(
            "runtime:worker-attempt",
            UsageObservation(
                "observation",
                "worker-attempt",
                "receipt",
                "tokens",
                "token",
                Decimal(3),
                Provenance.MEASUREMENT,
                "source",
                101,
            ),
        )
        usage = projection.snapshot()
        assert usage.usage_subtotals[0][:4] == ("tokens", "token", "measurement", "3")
        assert usage.missing_usage == "unknown" and usage.position.sequence == changed.position.sequence + 1
        store.revoke_work_budget(program.program_id, "owner-stop")
        revoked = projection.snapshot()
        assert revoked.program_revision == usage.program_revision
        assert revoked.budget_revoked is True
        assert revoked.position.sequence == usage.position.sequence + 1


def test_snapshot_exposes_only_scoped_command_receipt_state(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "commands.db") as store:
        program = store.create(reference_spec(), "create")
        projection = OwnerProjection(store, program.program_id)
        before = projection.snapshot()
        operation = command_prefix(program.program_id) + digest(encode(["owner", "stop-1"]))
        store.intent(
            operation,
            operation,
            {
                "version": 1,
                "action": "stop",
                "scope": program.program_id,
                "actor": "owner",
                "command_id": "stop-1",
                "reason": "not exposed",
            },
        )
        unrelated = command_prefix("other-program") + digest(encode(["other", "private"]))
        store.intent(
            unrelated,
            unrelated,
            {"version": 1, "action": "stop", "scope": "other-program", "actor": "other", "command_id": "private"},
        )
        unknown = projection.snapshot()
        assert unknown.commands == (("stop-1", "stop", "owner", "intent", "unknown"),)
        assert projection.events(before.position).gap is True
        store.finalize_artifact(operation, "receipt", encode({"version": 1, "status": "accepted"}))
        recorded = projection.snapshot()
        assert recorded.commands == (("stop-1", "stop", "owner", "intent", "recorded"),)
        assert recorded.program_revision == unknown.program_revision
        assert projection.events(unknown.position).gap is True


def test_snapshot_reports_recorded_claim_and_manual_change_without_claiming_fresh_settlement(
    sqlite_tmp_path: Path,
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "claims.db", boot_clock=Clock()) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        worker = client(store, tree)
        control = client(store, tree, owner=True)
        granted = claim(store, worker, tree)
        projection = OwnerProjection(store, program.program_id)
        initial = projection.snapshot()
        assert initial.claims[0]["attempt"] == granted.attempt.attempt_id
        assert initial.claims[0]["mode"] == "pull"
        assert initial.claims[0]["recorded_proofs"] == ()
        assert initial.claims[0]["recorded_owner_reconciliation_needed"] is False
        (tree.path / "manual.txt").write_text("private manual content")
        changed = control.observe(granted)
        current = projection.snapshot()
        assert current.claims[0]["latest_recorded_subject"] == changed
        assert current.claims[0]["recorded_owner_reconciliation_needed"] is True
        assert current.physical_settlement is None
        assert "private manual content" not in str(current)
        assert projection.events(initial.position).gap is True


def test_claim_projection_isolates_programs(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "scoped-claims.db", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=2))
        initialize(store, "program-2", project=policy(active=2))
        first_tree = workspace(sqlite_tmp_path, "first")
        second_tree = workspace(sqlite_tmp_path, "second")
        first = claim(store, client(store, first_tree), first_tree)
        second = claim(store, client(store, second_tree), second_tree, "program-2", "a2")
        assert [item["claim"] for item in store.served_claims("program-1")] == [first.command_key]
        assert [item["claim"] for item in store.served_claims("program-2")] == [second.command_key]


def test_repeated_workspace_subject_does_not_fabricate_last_observation(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "revisited.db", boot_clock=Clock()) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "revisited")
        granted = claim(store, client(store, tree), tree)
        control = client(store, tree, owner=True)
        initial = control.observe(granted)
        initial_snapshot = tree.capture()[1]
        (tree.path / "manual.txt").write_text("changed")
        assert control.observe(granted) != initial
        store.observe_claim_workspace(granted, initial, initial_snapshot, owner="owner")
        recorded = OwnerProjection(store, program.program_id).snapshot().claims[0]
        assert recorded["latest_recorded_subject"] is None
        assert recorded["recorded_owner_reconciliation_needed"] is None


def test_snapshot_projects_recorded_candidate_session_and_decision(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "decisions.db", boot_clock=Clock()) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "decision")
        granted = claim(store, client(store, tree), tree)
        projection = OwnerProjection(store, program.program_id)
        before = projection.snapshot()
        store.finalize_artifact(
            granted.operation_id,
            "ordinary-candidate",
            encode({"version": 1, "head": "a" * 40, "scope": "b" * 64, "paths": []}),
        )
        store.finalize_artifact(granted.operation_id, "runtime-receipt", encode({"version": 2, "handle": "session-1"}))
        rejection = f"rejection:{granted.attempt.attempt_id}"
        store.intent(rejection, rejection, {"product_review_status": "owner_decision_needed"})
        store.finalize_artifact(rejection, "rejected-result", b"synthetic rejection")
        current = projection.snapshot()
        assert current.correlations == (
            {
                "attempt": granted.attempt.attempt_id,
                "workspace_reference": str(tree.path),
                "candidate_head": "a" * 40,
                "candidate_scope": "b" * 64,
                "runtime_handle": "session-1",
            },
        )
        assert current.decisions == (
            {
                "source": "product-review",
                "attempt": granted.attempt.attempt_id,
                "status": "owner_decision_needed",
                "applicability": "pending",
            },
        )
        assert projection.events(before.position).gap is True


def test_claim_count_is_scoped_even_for_large_history(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "bounded-claims.db", boot_clock=Clock()) as store:
        initialize(store)
        initialize(store, "program-2")
        rows = ((f"work-budget:v1:ownership-claim:{index:064x}", f"synthetic:{index}") for index in range(4097))
        connection(store).executemany(
            "INSERT INTO operations (operation_id, effect_key, request_json, request_digest) "
            'VALUES (?, ?, \'{"request":{"program":"program-1"}}\', ?)',
            ((operation, effect, "0" * 64) for operation, effect in rows),
        )
        assert store.served_claim_count("program-1") == 4097
        assert store.served_claims("program-2") == ()


def test_command_history_overflow_keeps_snapshot_and_resync_available(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "command-history.db", boot_clock=Clock()) as store:
        program = initialize(store)
        projection = OwnerProjection(store, program.program_id)
        initial = projection.snapshot()
        prefix = command_prefix(program.program_id)
        rows: list[tuple[str, str, str, str]] = []
        for index in range(4097):
            command_id = f"{index:05d}" + "x" * 251
            operation = prefix + digest(encode(["worker", command_id]))
            request = {
                "request": {
                    "command_id": command_id,
                    "action": "renew",
                    "scope": program.program_id,
                    "actor": "worker",
                }
            }
            rows.append((operation, operation, encode(request).decode(), "0" * 64))
        connection(store).executemany(
            "INSERT INTO operations (operation_id, effect_key, request_json, request_digest) VALUES (?, ?, ?, ?)", rows
        )
        current = projection.snapshot()
        assert current.command_coverage == (64, 4097, False)
        assert len(current.commands) == 64
        assert len(encode(asdict(current))) < 1024 * 1024
        assert projection.events(initial.position).gap is True
        assert projection.events(current.position).gap is True


def test_snapshot_displays_claim_and_task_expiry_without_renewal(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "horizons.db", boot_clock=clock) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "horizon")
        granted = claim(store, client(store, tree), tree)
        before_operations, before_changes = store.operations(), connection(store).total_changes
        before = OwnerProjection(store, program.program_id).snapshot()
        assert before.authority["task_deadline"] == 110
        assert before.authority["cutoff"] == "not-expired-observed"
        assert before.claims[0]["original_expires_at"] == granted.expires_at == 105
        assert before.claims[0]["cutoff"] == "not-expired-observed"
        clock.reading = BootClockReading(106, 7_000_000_000, "synthetic-boot-a")
        reconnected = OwnerProjection(store, program.program_id).snapshot()
        assert reconnected.status == "active" and reconnected.claims[0]["cutoff"] == "expired"
        assert reconnected.authority["cutoff"] == "not-expired-observed"
        clock.reading = BootClockReading(110, 11_000_000_000, "synthetic-boot-a")
        expired = OwnerProjection(store, program.program_id).snapshot()
        assert expired.authority["cutoff"] == "expired"
        assert expired.authority["safe_next_action"] == "reconcile-or-stop"
        assert expired.physical_settlement is None
        assert store.operations() == before_operations and connection(store).total_changes == before_changes


def test_snapshot_reports_original_and_renewed_horizons_with_unverifiable_clock(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "renewed-horizon.db", boot_clock=clock) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "renewed-horizon")
        worker = client(store, tree)
        granted = claim(store, worker, tree)
        clock.reading = BootClockReading(102, 3_000_000_000, "synthetic-boot-a")
        assert worker.renew("renew-original", granted, lease_seconds=6) == 108
        clock.reading = BootClockReading(106, 7_000_000_000, "synthetic-boot-a")
        observed = OwnerProjection(store, program.program_id).snapshot()
        assert observed.claims[0]["original_expires_at"] == 105
        assert observed.claims[0]["recorded_expires_at"] == 108
        assert observed.claims[0]["cutoff"] == "not-expired-observed"
        clock.reading = BootClockReading(109, 10_000_000_000, "synthetic-boot-a")
        assert OwnerProjection(store, program.program_id).snapshot().claims[0]["cutoff"] == "expired"
        clock.reading = None
        uncertain = OwnerProjection(store, program.program_id).snapshot()
        assert uncertain.authority["clock"] == "unknown" and uncertain.claims[0]["cutoff"] == "unknown"
