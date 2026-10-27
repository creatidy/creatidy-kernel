# SPDX-License-Identifier: Apache-2.0
"""Natural receipt-publication cuts are recovered for STOP, not silently skipped."""

import inspect
import json
import sqlite3
from typing import cast

import pytest
from test_reference_live_expiry import Clock
from test_reference_live_recovery_gaps import RecoveryCut, Scenario, TrackedConnection, UnqualifiedConnection
from test_reference_live_recovery_gaps import scenario as scenario

from creatidy_kernel.adapters.reference import reference_export
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.ports.program_store import OperationRecord

pytest_plugins = ["test_sqlite_store"]


def transport_report(store: SQLiteProgramStore, operation: OperationRecord) -> bool | None:
    connection = cast(sqlite3.Connection, inspect.getattr_static(store, "_connection"))
    row = connection.execute(
        "SELECT succeeded FROM operation_transports WHERE operation_id = ? AND fence = ?",
        (operation.operation_id, operation.fence),
    ).fetchone()
    return None if row is None else bool(row[0])


def refuse_new_work(scenario: Scenario, monkeypatch: pytest.MonkeyPatch, authority: str) -> None:
    if authority == "expired":
        scenario.clock.expire()
    elif authority == "revoked":
        with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
            store.revoke_work_budget("reference", "synthetic owner stop after natural receipt crash")
    else:

        def unavailable(_clock: Clock) -> BootClockReading | None:
            return None

        monkeypatch.setattr(Clock, "__call__", unavailable)


@pytest.mark.parametrize("authority", ["expired", "revoked", "unverifiable"])
@pytest.mark.parametrize(
    "owner", ["original", "failed-settlement", "missing-settlement", "unavailable", "mismatched", "unqualified"]
)
def test_natural_receipt_cut_stops_started_target_without_manual_acceptance(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, authority: str, owner: str
) -> None:
    original = scenario.connection()
    receipt = scenario.crash_after_receipt(original, monkeypatch)
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        pending = store.operation("runtime:reference:first")
        assert pending.status == "dispatched" and pending.accepted_reference is None
        assert transport_report(store, pending) is None and pending.fence == 1 and pending.attempts == 1
        assert not store.operations("cancel:")
        request_digest = pending.request_digest
        attempt_digest = store.load("reference").attempt("reference:first").spec.digest
    assert original.process.poll() is None and original.close_calls == 0
    current = original
    if owner == "failed-settlement":
        original.settlement = False
    elif owner == "missing-settlement":
        original.settlement = None
    elif owner in {"unavailable", "mismatched"}:
        current = scenario.connection("replacement-birth-group", "missing" if owner == "unavailable" else True)
    replacement = UnqualifiedConnection(scenario.directory) if owner == "unqualified" else current
    refuse_new_work(scenario, monkeypatch, authority)
    result = scenario.run(replacement)
    assert result["accepted"] == [] and scenario.transport.pulls == [] and original.starts == 1
    assert original.close_calls == (1 if owner in {"original", "failed-settlement", "missing-settlement"} else 0)
    assert result["condition"] == ("budget_exhausted" if owner == "original" else "cancel_uncertain")
    cleanup = cast(list[dict[str, object]], result["owned_cleanup"])
    assert len(cleanup) == 1
    if replacement is not original:
        assert replacement.calls == [] and replacement.starts == 0
        assert original.process.poll() is None and original.interrupts == 0
        if isinstance(replacement, TrackedConnection):
            assert replacement.close_calls == 0 and replacement.process.poll() is None
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        recovered = store.operation("runtime:reference:first")
        assert recovered.fence == pending.fence and recovered.attempts == 1
        assert recovered.accepted_reference == json.loads(receipt)["handle"]
        assert transport_report(store, recovered) is True
        assert recovered.request_digest == request_digest
        assert store.artifact(recovered.operation_id, "runtime-receipt") == receipt
        assert store.load("reference").attempt("reference:first").spec.digest == attempt_digest
        cancellation = store.operation("cancel:reference:first")
        assert cancellation.attempts == (1 if replacement is original else 0)
        witness = store.find_artifact(cancellation.operation_id, "owned-group-settlement")
        assert (witness is not None) is (owner == "original")
        if witness is not None:
            proof = json.loads(witness)
            assert proof["runtime_fence"] == 1 and proof["attempt"] == attempt_digest
            assert proof["request_digest"] == request_digest and proof["owned_group_settled"] is True
        assert not store.operations("acceptance:") and len(store.operations("reference:live-poll:")) == 1
    assert (scenario.directory / "repository/protected-worker-output").read_text() == "retain"


@pytest.mark.parametrize("authority", ["expired", "still-valid"])
@pytest.mark.parametrize(
    "receipt_case",
    [
        "missing",
        "malformed",
        "unsupported",
        "receipt-fence",
        "current-fence",
        "agent",
        "provider",
        "model",
        "handle",
        "conflicting-accepted-handle",
    ],
)
def test_unverified_dispatched_receipt_never_disappears_from_cleanup_accounting(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, receipt_case: str, authority: str
) -> None:
    original = scenario.connection()
    finalize = SQLiteProgramStore.finalize_artifact
    stored: list[bytes] = []

    def cut(store: SQLiteProgramStore, operation_id: str, name: str, data: bytes) -> str:
        if name == "runtime-receipt":
            if receipt_case == "missing":
                raise RecoveryCut("synthetic crash before runtime receipt publication")
            payload = json.loads(data)
            if receipt_case == "unsupported":
                payload["version"] = 999
            elif receipt_case == "receipt-fence":
                payload["fence"] += 1
            elif receipt_case == "agent":
                payload["identity"]["agent_definition_version"] = "another-definition"
            elif receipt_case in {"provider", "model"}:
                payload["identity"]["requested_provider" if receipt_case == "provider" else "requested"] = (
                    "another-selection"
                )
            elif receipt_case == "handle":
                payload["handle"] = "not-a-codex-handle"
            published = (
                b"{malformed"
                if receipt_case == "malformed"
                else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            )
            stored.append(published)
            finalize(store, operation_id, name, published)
            raise RecoveryCut("synthetic crash with unverified receipt evidence")
        return finalize(store, operation_id, name, data)

    with monkeypatch.context() as crash:
        crash.setattr(SQLiteProgramStore, "finalize_artifact", cut)
        with pytest.raises(RecoveryCut):
            scenario.run(original)
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        pending = store.operation("runtime:reference:first")
        assert (
            pending.status == "dispatched"
            and pending.accepted_reference is None
            and transport_report(store, pending) is None
        )
        if receipt_case == "current-fence":
            store.reconcile(
                pending.operation_id,
                pending.fence,
                "absent",
                now=102,
                evidence="synthetic authoritative absence fixture",
                authoritative_absence=True,
            )
            store.claim(pending.operation_id, now=102, lease_seconds=1)
        elif receipt_case == "conflicting-accepted-handle":
            store.record_transport(pending.operation_id, pending.fence, True)
            store.observe(
                pending.operation_id,
                pending.fence,
                "synthetic conflicting receipt",
                "accepted",
                reference="codex:other-thread:other-turn",
            )
        pending = store.operation(pending.operation_id)
    calls = list(original.calls)
    if authority == "expired":
        scenario.clock.expire()
    result = scenario.run(original)
    assert result["condition"] == "cancel_uncertain" and result["accepted"] == [] and scenario.transport.pulls == []
    assert original.close_calls == 0 and original.process.poll() is None and original.calls == calls
    cleanup = cast(list[dict[str, object]], result["owned_cleanup"])
    assert len(cleanup) == 1 and cleanup[0]["operation"] == pending.operation_id
    assert cleanup[0]["runtime_fence"] == pending.fence and cleanup[0]["owned_group_settled"] is None
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        unchanged = store.operation(pending.operation_id)
        assert unchanged.accepted_reference == pending.accepted_reference and unchanged.attempts == pending.attempts
        assert (
            unchanged.fence == pending.fence and not store.operations("acceptance:") and not store.operations("cancel:")
        )
        assert store.operations("interruption:live-restoration")
        if stored:
            assert store.artifact(pending.operation_id, "runtime-receipt") == stored[0]
    assert (scenario.directory / "repository/protected-worker-output").read_text() == "retain"
    repeated = scenario.run(original)
    assert repeated["condition"] == "cancel_uncertain" and original.calls == calls and original.close_calls == 0


def test_second_natural_receipt_cut_preserves_prior_acceptance_but_never_accepts_pending_work(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = scenario.connection()
    finalize = SQLiteProgramStore.finalize_artifact

    def cut_second(store: SQLiteProgramStore, operation_id: str, name: str, data: bytes) -> str:
        result = finalize(store, operation_id, name, data)
        if operation_id == "runtime:reference:second" and name == "runtime-receipt":
            raise RecoveryCut("synthetic loss after second native receipt publication")
        return result

    with monkeypatch.context() as cut:
        cut.setattr(SQLiteProgramStore, "finalize_artifact", cut_second)
        with pytest.raises(RecoveryCut):
            scenario.run(original)
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        before = store.operation("runtime:reference:second")
        assert before.accepted_reference is None and transport_report(store, before) is None
        receipt = store.artifact(before.operation_id, "runtime-receipt")
        assert len(store.operations("acceptance:")) == 1
        accepted_before = reference_export(store)["accepted"]
    scenario.clock.expire()
    result = scenario.run(original)
    assert result["accepted"] == accepted_before and scenario.transport.pulls == []
    assert original.starts == 2 and original.close_calls == 1 and original.process.poll() is not None
    cleanup = cast(list[dict[str, object]], result["owned_cleanup"])
    assert {item["operation"] for item in cleanup} == {"runtime:reference:first", "runtime:reference:second"}
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        stopped = store.operation(before.operation_id)
        assert stopped.accepted_reference == json.loads(receipt)["handle"] and stopped.attempts == 1
        assert len(store.operations("acceptance:")) == 1 and store.operation("cancel:reference:second").attempts == 1
        assert store.artifact(before.operation_id, "runtime-receipt") == receipt
    assert (scenario.directory / "repository/protected-worker-output").read_text() == "retain"


def test_natural_transport_before_accepted_observation_cut_still_retires_original(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = scenario.connection()
    transport = SQLiteProgramStore.record_transport

    def cut(store: SQLiteProgramStore, operation_id: str, fence: int, succeeded: bool) -> None:
        transport(store, operation_id, fence, succeeded)
        if operation_id == "runtime:reference:first":
            raise RecoveryCut("synthetic crash after transport publication before accepted observation")

    with monkeypatch.context() as crash:
        crash.setattr(SQLiteProgramStore, "record_transport", cut)
        with pytest.raises(RecoveryCut):
            scenario.run(original)
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        pending = store.operation("runtime:reference:first")
        assert (
            pending.accepted_reference is None
            and pending.status == "dispatched"
            and transport_report(store, pending) is True
        )
        receipt = store.artifact(pending.operation_id, "runtime-receipt")
    assert original.process.poll() is None and original.close_calls == 0
    scenario.clock.expire()
    result = scenario.run(original)
    assert result["accepted"] == [] and scenario.transport.pulls == [] and original.starts == 1
    assert original.close_calls == 1 and original.process.poll() is not None
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        assert store.artifact(pending.operation_id, "runtime-receipt") == receipt
        assert store.find_artifact("cancel:reference:first", "owned-group-settlement") is not None
        assert not store.operations("acceptance:")
