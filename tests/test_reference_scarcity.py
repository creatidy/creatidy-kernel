# SPDX-License-Identifier: Apache-2.0
"""K8 consumer proof: public recommendation bytes, native-shaped RPC, synthetic Forge."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference import (
    ReferenceInterrupted,
    export_reference,
    reference_commit,
    reference_git,
    reference_spec,
    run_reference,
)
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import DomainCommandType, Program
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable
from creatidy_kernel.ports.program_store import OperationRecord

pytest_plugins = ["test_sqlite_store"]

FIXTURE = Path(__file__).parent / "fixtures" / "scarcity_router_selected.json"


class NativeConnection:
    """An external native fixture survives controller reconstruction; no inference."""

    version = "k8-native-fixture-v1"
    methods = frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"})

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.starts = 0
        self.turns = 0
        self.requests: list[tuple[str, dict[str, object]]] = []

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.requests.append((method, params))
        if method == "thread/start":
            assert params["model"] == "fixture-model"
            assert params["modelProvider"] == "openai"
            assert cast(dict[str, object], params["config"])["model_reasoning_effort"] == "high"
            self.starts += 1
            return {
                "thread": {"id": f"thread-{self.starts}"},
                "model": "fixture-model",
                "modelProvider": "openai",
                "reasoningEffort": "high",
            }
        if method == "turn/start":
            assert params["effort"] == "high"
            assert params["threadId"] == f"thread-{self.starts}"
            self.turns += 1
            (self.directory / "repository" / "result.txt").write_bytes(
                b"first\n" if self.turns == 1 else b"first\nsecond\n"
            )
            return {"turn": {"id": f"turn-{self.turns}", "status": "inProgress"}}
        if method == "thread/read":
            thread = str(params["threadId"])
            return {
                "thread": {"id": thread, "turns": [{"id": thread.replace("thread", "turn"), "status": "completed"}]}
            }
        raise AssertionError(f"unexpected native method {method}")


class Recommendations:
    def __init__(self) -> None:
        self.body = FIXTURE.read_bytes()
        self.unavailable = False
        self.calls = 0

    def exchange(self, requirement: dict[str, object]) -> bytes:
        assert requirement["hard_constraints"] == {"minimum_input_context_tokens": 128, "requires_tool_use": True}
        self.calls += 1
        if self.unavailable:
            raise AllocationUnavailable("synthetic router unavailable")
        return self.body


def scenario(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Recommendations, NativeConnection, SyntheticForgeTransport, Callable[[], dict[str, object]]]:
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: 10)
    recommendations = Recommendations()

    def exchange(_allocator: ScarcityRouterAllocator, requirement: dict[str, object]) -> bytes:
        return recommendations.exchange(requirement)

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", exchange)
    binding = Allocation("codex", "openai", "fixture-model", frozenset({"reference"}), 128, "runtime fixture", "high")
    allocator = ScarcityRouterAllocator("http://127.0.0.1:8765", (binding,))
    connection = NativeConnection(directory)
    repository = Reference("forgejo:synthetic/reference")
    transport = SyntheticForgeTransport(repository)
    forge = ForgejoForge(transport, transport, lambda _: True)
    objects = directory / "objects.git"
    objects.mkdir()
    reference_git(objects, "init", "--bare")
    transport.branches["develop"] = reference_commit(objects, b"")

    def run() -> dict[str, object]:
        return run_live_reference(
            directory,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            allocator=allocator,
            supported_efforts=frozenset({("openai", "fixture-model", "high")}),
            forge=forge,
            repository=repository,
            object_source=objects,
            deadline=100,
            max_observations=8,
        )

    return recommendations, connection, transport, run


def assert_proof(result: dict[str, object], connection: NativeConnection, transport: SyntheticForgeTransport) -> None:
    assert result["status"] == "completed"
    assert result["automatic_merge"] is False and result["automatic_deploy"] is False
    attempts = cast(list[dict[str, object]], result["attempts"])
    assert len(attempts) == 2
    for entry in attempts:
        allocation = cast(dict[str, object], entry["allocation"])
        assert allocation["provider_id"] == "openai"
        assert allocation["model_id"] == "fixture-model"
        assert allocation["reasoning_effort"] == "high"
        assert allocation["variant"] == "opaque-configuration"
        provenance = cast(dict[str, object], json.loads(str(allocation["decision_provenance"])))
        assert provenance == json.loads(FIXTURE.read_bytes())
        identity = cast(dict[str, object], entry["identity"])
        assert identity["requested"] == identity["resolved"] == "fixture-model"
        assert identity["requested_provider"] == identity["resolved_provider"] == "openai"
        assert identity["requested_effort"] == identity["resolved_effort"] == "high"
        assert identity["observed"] is None
        assert identity["observed_provider"] is None and identity["observed_effort"] is None
        attempt = cast(dict[str, object], entry["attempt"])
        spec = cast(dict[str, object], attempt["spec"])
        assert spec["spec_digest"] == reference_spec().digest
        assert entry["verification"] is not None
    assert connection.starts == connection.turns == 2
    assert len(transport.pulls) == 1
    assert cast(dict[str, object], result["pr"])["status"] == "accepted"


def test_router_native_two_work_unit_proof(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recommendations, connection, transport, run = scenario(sqlite_tmp_path, monkeypatch)
    result = run()
    assert_proof(result, connection, transport)
    assert recommendations.calls == 2
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = store.load("reference")
        second = program.attempt("reference:second").spec
        assert second.effective_inputs == program.resolved_inputs("second")
        assert second.effective_inputs[0].source_work_unit_id == "first"
        assert store.artifact("runtime:reference:first", "output") == b"first\n"
        assert store.artifact("runtime:reference:second", "output") == b"first\nsecond\n"
    recommendations.unavailable = True
    assert run()["attempts"] == result["attempts"]
    assert recommendations.calls == 2


@pytest.mark.parametrize("router_change", ["outage", "different"])
@pytest.mark.parametrize("boundary", ["receipt-evidence", "receipt", "identity"])
def test_restart_uses_original_native_allocation_without_router(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, router_change: str, boundary: str
) -> None:
    recommendations, connection, transport, run = scenario(sqlite_tmp_path, monkeypatch)
    finalize = SQLiteProgramStore.finalize_artifact

    def crash_after_identity(store: SQLiteProgramStore, operation: str, name: str, data: bytes) -> str:
        result = finalize(store, operation, name, data)
        if operation == "runtime:reference:first" and (
            (boundary == "identity" and name == "identity")
            or (boundary == "receipt-evidence" and name == "runtime-receipt")
        ):
            raise ReferenceInterrupted("native configuration evidence is durable")
        return result

    observe = SQLiteProgramStore.observe

    def crash_after_receipt(
        store: SQLiteProgramStore,
        operation_id: str,
        fence: int,
        observation_id: str,
        kind: str,
        *,
        reference: str | None = None,
    ) -> OperationRecord:
        result = observe(store, operation_id, fence, observation_id, kind, reference=reference)
        if boundary == "receipt" and operation_id == "runtime:reference:first" and kind == "accepted":
            raise ReferenceInterrupted("receipt accepted before identity artifact publication")
        return result

    with (
        patch.object(SQLiteProgramStore, "finalize_artifact", crash_after_identity),
        patch.object(SQLiteProgramStore, "observe", crash_after_receipt),
    ):
        with pytest.raises(ReferenceInterrupted):
            run()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        assert store.find_artifact("runtime:reference:first", "runtime-receipt") is not None
        if boundary != "identity":
            assert store.find_artifact("runtime:reference:first", "identity") is None
        assert (store.operation("runtime:reference:first").accepted_reference is None) == (
            boundary == "receipt-evidence"
        )
    before = cast(list[dict[str, object]], export_reference(sqlite_tmp_path)["attempts"])[0]["allocation"]
    assert recommendations.calls == connection.starts == connection.turns == 1
    if router_change == "outage":
        recommendations.unavailable = True
    else:
        document = cast(dict[str, object], json.loads(recommendations.body))
        decision = cast(dict[str, object], document["decision"])
        selected = cast(dict[str, object], decision["selected"])
        selected["reasoning_effort"] = "low"
        recommendations.body = json.dumps(document).encode()
    admit = SQLiteProgramStore.admit_with_intent

    def stop_before_new_attempt(
        store: SQLiteProgramStore,
        program_id: str,
        command_key: str,
        command: DomainCommandType,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
    ) -> Program:
        result = admit(store, program_id, command_key, command, operation_id, effect_key, request)
        if operation_id == "acceptance:reference:first":
            raise ReferenceInterrupted("existing Attempt accepted; no new Attempt authorized by this test step")
        return result

    with patch.object(SQLiteProgramStore, "admit_with_intent", stop_before_new_attempt):
        with pytest.raises(ReferenceInterrupted):
            run()
    assert recommendations.calls == connection.starts == connection.turns == 1
    after = export_reference(sqlite_tmp_path)
    assert cast(list[dict[str, object]], after["attempts"])[0]["allocation"] == before
    assert len(cast(list[object], after["accepted"])) == 1
    recommendations.unavailable = False
    recommendations.body = FIXTURE.read_bytes()
    assert_proof(run(), connection, transport)
    assert recommendations.calls == 2


def test_router_can_replace_allocator_in_offline_reference(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def exchange(_allocator: ScarcityRouterAllocator, _requirement: dict[str, object]) -> bytes:
        nonlocal calls
        calls += 1
        return FIXTURE.read_bytes()

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", exchange)
    binding = Allocation("fake", "openai", "fixture-model", frozenset({"reference"}), 128, "synthetic runtime", "high")
    allocator = ScarcityRouterAllocator("http://127.0.0.1:8765", (binding,))
    with pytest.raises(ReferenceInterrupted):
        run_reference(sqlite_tmp_path, owner_approved=True, allocator=allocator, fault="prepared")
    assert calls == 1
    result = run_reference(sqlite_tmp_path, owner_approved=True, allocator=allocator)
    assert result["status"] == "completed" and calls == 2
    assert result["automatic_merge"] is False and result["automatic_deploy"] is False


@pytest.mark.parametrize("failure", ["no-selection", "malformed", "schema", "runtime-mismatch"])
def test_failed_recommendation_cannot_prepare_attempt_or_deliver(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    recommendations, connection, transport, run = scenario(sqlite_tmp_path, monkeypatch)
    document = cast(dict[str, object], json.loads(recommendations.body))
    decision = cast(dict[str, object], document["decision"])
    if failure == "no-selection":
        decision.update(selected=None, alternatives=[], degraded=False, reason_codes=["no_eligible_candidate"])
    elif failure == "schema":
        document["schema_version"] = 2
    elif failure == "runtime-mismatch":
        selected = cast(dict[str, object], decision["selected"])
        cast(dict[str, object], selected["identity"])["model"] = "incompatible-model"
    recommendations.body = b"{broken" if failure == "malformed" else json.dumps(document).encode()
    with pytest.raises(AllocationUnavailable):
        run()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        assert store.load("reference").attempts == ()
        assert store.operations("runtime:") == ()
    assert connection.requests == [] and transport.pulls == []
    assert recommendations.calls == 1
