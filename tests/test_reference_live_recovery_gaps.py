# SPDX-License-Identifier: Apache-2.0
"""Composed durable-claim and restore gaps never replay work or adopt a new group."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from test_reference_live import Connection
from test_reference_live_expiry import Clock, OwnedConnection

from creatidy_kernel.adapters.codex_runtime import CodexRuntime
from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference import reference_commit, reference_git
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.execution import RuntimeObservation
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.ports.program_store import OperationRecord

pytest_plugins = ["test_sqlite_store"]


class RecoveryCut(RuntimeError):
    pass


class TrackedConnection(OwnedConnection):
    def __init__(self, directory: Path, clock: Clock, group: str, settlement: bool | None | str = True) -> None:
        super().__init__(directory, clock, "none", settlement)
        self.group = group
        self.calls: list[str] = []
        self.interrupt_completes = False

    @property
    def owned_group_reference(self) -> str | None:
        return None if self.settlement == "missing" else self.group

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append(method)
        result = super().request(method, params)
        if method == "turn/interrupt" and self.interrupt_completes:
            self.status = "interrupted"
        return result


class UnqualifiedConnection(Connection):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.calls: list[str] = []

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append(method)
        return super().request(method, params)


class Scenario:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.clock = Clock()
        self.connections: list[TrackedConnection] = []
        self.repository = Reference("forgejo:synthetic/reference")
        self.transport = SyntheticForgeTransport(self.repository)
        self.forge = ForgejoForge(self.transport, self.transport, lambda _: True)
        self.objects = directory / "objects.git"
        self.objects.mkdir()
        reference_git(self.objects, "init", "--bare")
        self.transport.branches["develop"] = reference_commit(self.objects, b"")

    def connection(
        self, group: str = "original-birth-group", settlement: bool | None | str = True
    ) -> TrackedConnection:
        result = TrackedConnection(self.directory, self.clock, group, settlement)
        self.connections.append(result)
        return result

    def run(self, connection: Connection) -> dict[str, object]:
        return run_live_reference(
            self.directory,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            allocation=Allocation(
                "codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner selection"
            ),
            forge=self.forge,
            repository=self.repository,
            object_source=self.objects,
            deadline=110,
            max_observations=3,
            boot_clock=self.clock,
        )

    def crash_after_receipt(self, connection: Connection, monkeypatch: pytest.MonkeyPatch) -> bytes:
        finalize = SQLiteProgramStore.finalize_artifact

        def cut(store: SQLiteProgramStore, operation_id: str, name: str, data: bytes) -> str:
            result = finalize(store, operation_id, name, data)
            if name == "runtime-receipt":
                raise RecoveryCut("synthetic crash after durable original runtime receipt")
            return result

        with monkeypatch.context() as crash:
            crash.setattr(SQLiteProgramStore, "finalize_artifact", cut)
            with pytest.raises(RecoveryCut, match="durable original"):
                self.run(connection)
        with SQLiteProgramStore(self.directory / "kernel.sqlite3", boot_clock=self.clock) as store:
            return store.artifact("runtime:reference:first", "runtime-receipt")

    def durable_stop(self, *, claimed: bool) -> None:
        with SQLiteProgramStore(self.directory / "kernel.sqlite3", boot_clock=self.clock) as store:
            operation = store.operation("runtime:reference:first")
            receipt = json.loads(store.artifact(operation.operation_id, "runtime-receipt"))
            handle = cast(str, receipt["handle"])
            store.record_transport(operation.operation_id, operation.fence, True)
            store.observe(operation.operation_id, operation.fence, f"accepted:{handle}", "accepted", reference=handle)
            store.revoke_work_budget("reference", "interruption:live-budget")
            if claimed:
                store.intent(
                    "cancel:reference:first",
                    "cancel:reference:first",
                    {"operation": operation.operation_id, "handle": handle},
                )
                store.claim("cancel:reference:first", now=100, lease_seconds=1)


@pytest.fixture
def scenario(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Scenario]:
    result = Scenario(sqlite_tmp_path)
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: result.clock.reading.wall)
    try:
        yield result
    finally:
        for connection in result.connections:
            connection.retire_fixture()


@pytest.mark.parametrize("status", ["completed", "inProgress"])
@pytest.mark.parametrize("settlement", [True, False, None, "missing", "error"])
def test_durable_interrupt_claim_does_not_suppress_original_owner_retirement(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, status: str, settlement: bool | None | str
) -> None:
    original = scenario.connection(settlement=settlement)
    original.status = status
    receipt = scenario.crash_after_receipt(original, monkeypatch)
    scenario.durable_stop(claimed=True)
    assert original.process.poll() is None and original.close_calls == 0
    first = scenario.run(original)
    assert original.interrupts == 0 and original.starts == 1 and scenario.transport.pulls == []
    assert original.close_calls == (0 if settlement == "missing" else 1)
    expected_settled = settlement is True and status == "completed"
    assert first["condition"] == ("budget_exhausted" if expected_settled else "cancel_uncertain")
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        assert store.operation("cancel:reference:first").attempts == 1
        assert store.artifact("runtime:reference:first", "runtime-receipt") == receipt
        assert (store.find_artifact("cancel:reference:first", "owned-group-settlement") is not None) is expected_settled
        assert not store.operations("acceptance:")
    second = scenario.run(original)
    assert original.close_calls == (0 if settlement == "missing" else 1 if settlement is True else 2)
    assert original.interrupts == 0 and original.starts == 1 and second["accepted"] == []
    assert (scenario.directory / "repository/protected-worker-output").read_text() == "retain"


@pytest.mark.parametrize("boundary", ["unknown-observation", "observation-proof", "settlement-proof"])
def test_cleanup_crashes_after_claim_or_retirement_remain_bounded_and_recoverable(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    original = scenario.connection()
    original.status = "completed" if boundary == "unknown-observation" else "inProgress"
    original.interrupt_completes = True
    scenario.crash_after_receipt(original, monkeypatch)
    scenario.durable_stop(claimed=False)
    observe = SQLiteProgramStore.observe
    finalize = SQLiteProgramStore.finalize_artifact

    def cut_observation(
        store: SQLiteProgramStore,
        operation_id: str,
        fence: int,
        observation_id: str,
        kind: str,
        *,
        reference: str | None = None,
    ) -> OperationRecord:
        if operation_id.startswith("cancel:") and kind == "unknown":
            raise RecoveryCut("synthetic loss immediately after cancellation claim commit")
        return observe(store, operation_id, fence, observation_id, kind, reference=reference)

    def cut_proof(store: SQLiteProgramStore, operation_id: str, name: str, data: bytes) -> str:
        if operation_id.startswith("cancel:") and (
            (boundary == "observation-proof" and name.startswith("owned-observation:"))
            or (boundary == "settlement-proof" and name == "owned-group-settlement")
        ):
            raise RecoveryCut("synthetic loss after original group retirement before proof publication")
        return finalize(store, operation_id, name, data)

    with monkeypatch.context() as cut:
        if boundary == "unknown-observation":
            cut.setattr(SQLiteProgramStore, "observe", cut_observation)
        else:
            cut.setattr(SQLiteProgramStore, "finalize_artifact", cut_proof)
        with pytest.raises(RecoveryCut):
            scenario.run(original)
    assert original.process.poll() is not None and original.close_calls == 1
    interrupts = original.interrupts
    recovered = scenario.run(original)
    assert recovered["accepted"] == [] and scenario.transport.pulls == []
    assert original.starts == 1 and original.interrupts == interrupts and original.close_calls == 1
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        assert store.operation("cancel:reference:first").attempts == 1
        proof = json.loads(store.artifact("cancel:reference:first", "owned-group-settlement"))
        assert proof["owned_group_settled"] is True and proof["result_reconciled"] is True


@pytest.mark.parametrize("replacement", ["different-group", "unqualified", "same-group-unavailable"])
def test_nonfound_restore_stops_without_observing_unregistered_or_adopting_replacement(
    scenario: Scenario, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    original = scenario.connection()
    receipt = scenario.crash_after_receipt(original, monkeypatch)
    original_calls = list(original.calls)
    current: Connection
    if replacement == "different-group":
        current = scenario.connection("replacement-birth-group")
    elif replacement == "unqualified":
        current = UnqualifiedConnection(scenario.directory)
    else:
        current = original
        original.read_failure = True

    def forbidden_observation(*_args: object, **_kwargs: object) -> RuntimeObservation:
        raise AssertionError("non-found restoration must not observe an unregistered handle")

    with monkeypatch.context() as recovery:
        recovery.setattr(CodexRuntime, "observe", forbidden_observation)
        result = scenario.run(current)
    assert result["condition"] == "cancel_uncertain" and result["accepted"] == []
    assert scenario.transport.pulls == [] and original.starts == 1 and original.interrupts == 0
    if current is not original:
        assert current.calls == [] and current.starts == 0 and original.calls == original_calls
        assert original.process.poll() is None and original.close_calls == 0
        if isinstance(current, TrackedConnection):
            assert current.process.poll() is None and current.close_calls == 0
    else:
        assert original.process.poll() is not None and original.close_calls == 1
    with SQLiteProgramStore(scenario.directory / "kernel.sqlite3", boot_clock=scenario.clock) as store:
        assert store.artifact("runtime:reference:first", "runtime-receipt") == receipt
        assert store.operations("interruption:live-restoration") and not store.operations("acceptance:")
        assert store.find_artifact("cancel:reference:first", "owned-group-settlement") is None
    assert (scenario.directory / "repository/protected-worker-output").read_text() == "retain"
    # Returning the actual original owner can reconcile STOP, never resume work.
    original.read_failure = False
    original.status = "interrupted"
    stopped = scenario.run(original)
    assert stopped["accepted"] == [] and scenario.transport.pulls == [] and original.starts == 1
    # A mismatched replacement never claimed/sent an interrupt. Returning the
    # actual owner may send the first scoped interrupt, but never replay one.
    assert original.close_calls == 1 and original.interrupts == (0 if current is original else 1)
    if isinstance(current, TrackedConnection) and current is not original:
        assert current.close_calls == 0 and current.process.poll() is None
