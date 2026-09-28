# SPDX-License-Identifier: Apache-2.0
"""Immutable allocation recovery before/after every durable preparation boundary."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.reference import POLICY, ReferenceInterrupted, reference_spec
from creatidy_kernel.adapters.sqlite_store import CorruptHistory, OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AttemptStatus,
    PrepareAttempt,
    Program,
    StartAttempt,
)
from creatidy_kernel.core.execution import (
    Activity,
    Candidate,
    ExecutionRequest,
    Lookup,
    OperationKey,
    Presence,
    RuntimeIdentity,
    RuntimeObservation,
    TrustMode,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.ports.allocation import (
    decode_allocation,
    decode_identity,
    decode_runtime_receipt,
    encode_allocation,
    identity_matches,
    is_legacy_allocation,
    load_allocation,
)
from creatidy_kernel.ports.application import Collection, advance_work_unit, manifest_bytes

pytest_plugins = ["test_sqlite_store"]
OPERATION = "runtime:reference:first"
SELECTED = Allocation(
    "test-runtime",
    "provider",
    "model",
    frozenset({"reference"}),
    128,
    "original rationale",
    reasoning_effort="high",
    variant="opaque-not-effort",
    decision_provenance='{"decision":"original"}',
)
WORKSPACE = WorkspaceHandle(
    "workspace:first", WorkspaceSpec("synthetic", "base", (), "tools", "trusted", TrustMode.TRUSTED_DEVELOPMENT)
)


class CountingAllocator:
    def __init__(self, allocation: Allocation | None = SELECTED) -> None:
        self.allocation = allocation
        self.calls = 0

    def select(self, request: ResourceRequest) -> Allocation:
        self.calls += 1
        assert request == ResourceRequest("first", frozenset({"reference"}), 128)
        if self.allocation is None:
            raise AllocationUnavailable("synthetic Router outage")
        return self.allocation


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []
        self.override: RuntimeIdentity | None = None
        self.restored: list[ExecutionRequest] = []

    def start(self, request: ExecutionRequest) -> str:
        self.requests.append(request)
        return "handle:first"

    def restore(self, request: ExecutionRequest, handle: str) -> None:
        assert handle == "handle:first"
        self.restored.append(request)

    def observe(self, handle: str, *, now: int) -> RuntimeObservation:
        requested = self.requests[0].identity
        identity = self.override or replace(
            requested,
            resolved=requested.requested,
            resolved_provider=requested.requested_provider,
            resolved_effort=requested.requested_effort,
        )
        return RuntimeObservation(handle, Activity.RUNNING, now, now, identity, False)

    def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup:
        assert operation == self.requests[0].operation
        return Lookup(Presence.FOUND, "handle:first")

    def candidate(self, handle: str) -> Candidate | None:
        raise AssertionError("running worker must not be collected")

    def cancel(self, handle: str) -> RuntimeObservation:
        raise AssertionError("recovery must not cancel")


class NoCollection:
    def collect(self, request: ExecutionRequest, candidate: Candidate, *, now: int) -> Collection:
        raise AssertionError("running worker must not be collected")


def initial(store: SQLiteProgramStore) -> Program:
    store.create(reference_spec(), "create")
    return store.admit("reference", "activate", ActivateProgram(0, "owner"))


def step(
    store: SQLiteProgramStore,
    allocator: CountingAllocator,
    runtime: RecordingRuntime,
    *,
    fault: str | None = None,
    program: Program | None = None,
    now: int = 10,
) -> str:
    def interrupt(point: str) -> None:
        if point == fault:
            raise ReferenceInterrupted(point)

    return advance_work_unit(
        store,
        program or store.load("reference"),
        "first",
        allocator=allocator,
        runtime=runtime,
        workspace=WORKSPACE,
        collector=NoCollection(),
        policy=POLICY,
        now=now,
        fault=interrupt,
        restore=runtime.restore,
    )


@pytest.mark.parametrize(
    "fault", ["prepared", "allocation", "artifacts", "started", "commit", "send", "receipt-evidence", "receipt"]
)
@pytest.mark.parametrize("replacement", [None, replace(SELECTED, model_id="different", reasoning_effort="low")])
def test_every_committed_boundary_recovers_without_selection(
    sqlite_tmp_path: Path,
    fault: str,
    replacement: Allocation | None,
) -> None:
    database = sqlite_tmp_path / "kernel.sqlite3"
    original, runtime = CountingAllocator(), RecordingRuntime()
    with SQLiteProgramStore(database) as store:
        stale = initial(store)
        with pytest.raises(ReferenceInterrupted, match=fault):
            step(store, original, runtime, fault=fault)
        attempt = store.load("reference").attempt("reference:first")
        request = store.operation(OPERATION)
        assert load_allocation(store, attempt.spec) == SELECTED
        assert original.calls == 1
        if fault in {"prepared", "allocation", "artifacts"}:
            assert attempt.status is AttemptStatus.PREPARED and runtime.requests == []
        if fault == "prepared":
            assert store.find_artifact(OPERATION, "allocation") is None
        if fault in {"prepared", "allocation"}:
            assert store.find_artifact(OPERATION, "context") is None
    recovered = CountingAllocator(replacement)
    with SQLiteProgramStore(database) as store:
        assert step(store, recovered, runtime, program=stale, now=12) == "running"
        assert recovered.calls == 0
        after = store.operation(OPERATION)
        assert (after.request_json, after.request_digest) == (request.request_json, request.request_digest)
        assert store.load("reference").attempt("reference:first").spec == attempt.spec
        assert store.artifact(OPERATION, "allocation") == encode_allocation(SELECTED)
        assert step(store, recovered, runtime) == "running"
        assert recovered.calls == 0 and len(runtime.requests) == 1 and after.attempts == 1
        assert runtime.requests[0].allocation == SELECTED
        assert runtime.restored[-1].allocation == SELECTED
        assert runtime.restored[-1].context_reference == attempt.spec.context_reference
        assert runtime.restored[-1].identity.requested_effort == "high"


def test_precommit_failure_has_no_attempt_and_allows_fresh_selection(sqlite_tmp_path: Path) -> None:
    runtime, first = RecordingRuntime(), CountingAllocator()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        with pytest.raises(ReferenceInterrupted, match="selected"):
            step(store, first, runtime, fault="selected")
        assert store.load("reference").attempts == ()
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation(OPERATION)
        changed = CountingAllocator(replace(SELECTED, model_id="new-model"))
        assert step(store, changed, runtime) == "running"
        assert first.calls == changed.calls == 1
        assert runtime.requests[0].allocation == changed.allocation


def test_failed_intent_transaction_rolls_back_prepare(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = initial(store)
        connection_attribute = "_connection"
        connection = cast(sqlite3.Connection, getattr(store, connection_attribute))
        connection.execute(
            "CREATE TRIGGER synthetic_outbox_failure BEFORE INSERT ON operation_outbox "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        allocator, runtime = CountingAllocator(), RecordingRuntime()
        with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
            step(store, allocator, runtime)
        assert store.load("reference") == program
        assert allocator.calls == 1 and not runtime.requests
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation(OPERATION)
        connection.execute("DROP TRIGGER synthetic_outbox_failure")
        assert step(store, allocator, runtime) == "running"
        assert allocator.calls == 2


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_published_allocation_blob_damage_cannot_be_hidden_by_journal_copy(
    sqlite_tmp_path: Path,
    damage: str,
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        runtime = RecordingRuntime()
        with pytest.raises(ReferenceInterrupted, match="allocation"):
            step(store, CountingAllocator(), runtime, fault="allocation")
        digest = hashlib.sha256(encode_allocation(SELECTED)).hexdigest()
        blob = sqlite_tmp_path / "kernel.sqlite3.artifacts" / digest
        if damage == "missing":
            blob.unlink()
        else:
            blob.write_bytes(b"corrupt")
        unavailable = CountingAllocator(None)
        with pytest.raises(CorruptHistory):
            step(store, unavailable, runtime)
        assert unavailable.calls == 0 and not runtime.requests


def test_existing_attempt_without_operation_fails_before_select(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        runtime = RecordingRuntime()
        with pytest.raises(ReferenceInterrupted, match="prepared"):
            step(store, CountingAllocator(), runtime, fault="prepared")
        unavailable = CountingAllocator(None)
        with patch.object(store, "operation", side_effect=OperationConflict("unknown operation")):
            with pytest.raises(OperationConflict, match="unknown operation"):
                step(store, unavailable, runtime)
        assert unavailable.calls == 0 and not runtime.requests


def legacy_attempt(store: SQLiteProgramStore, *, effort: str | None = None, publish: bool = True) -> AttemptSpec:
    program = initial(store)
    allocation = replace(SELECTED, reasoning_effort=effort, variant=None, decision_provenance=None)
    raw = asdict(allocation)
    raw["capabilities"] = sorted(allocation.capabilities)
    del raw["variant"], raw["decision_provenance"]
    data = manifest_bytes(raw)
    context = manifest_bytes([asdict(item) for item in program.resolved_inputs("first")])
    attempt = AttemptSpec(
        "reference:first",
        "reference",
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
        "sha256:" + hashlib.sha256(data).hexdigest(),
        "reference:v1",
        WORKSPACE.key,
        "sha256:" + hashlib.sha256(context).hexdigest(),
    )
    program = store.admit("reference", "prepare:reference:first", PrepareAttempt(program.revision, "worker", attempt))
    store.admit_with_intent(
        "reference",
        "start:reference:first",
        StartAttempt(program.revision, "worker", attempt.attempt_id),
        OPERATION,
        OPERATION,
        {"attempt": attempt.digest, "workspace": WORKSPACE.key},
    )
    if publish:
        store.finalize_artifact(OPERATION, "allocation", data)
        store.finalize_artifact(OPERATION, "context", context)
    return attempt


def test_shipped_legacy_allocation_and_model_only_identity_still_recover(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        attempt = legacy_attempt(store)
        runtime, unavailable = RecordingRuntime(), CountingAllocator(None)
        runtime.override = RuntimeIdentity("model", "model", None, "reference:v1")
        assert step(store, unavailable, runtime) == "running"
        assert unavailable.calls == 0
        assert runtime.requests[0].allocation == load_allocation(store, attempt)
        data = store.artifact(OPERATION, "allocation")
        assert is_legacy_allocation(data)
        assert json.loads(data).get("version") is None


@pytest.mark.parametrize("missing", ["allocation", "context"])
def test_legacy_missing_original_bytes_never_reselects(sqlite_tmp_path: Path, missing: str) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        legacy_attempt(store)
        find = store.find_artifact
        unavailable, runtime = CountingAllocator(None), RecordingRuntime()

        def missing_artifact(op: str, name: str) -> bytes | None:
            return None if name == missing else find(op, name)

        with patch.object(store, "find_artifact", missing_artifact):
            with pytest.raises(ValueError, match="original legacy Attempt input is missing"):
                step(store, unavailable, runtime)
        assert unavailable.calls == 0 and not runtime.requests


@pytest.mark.parametrize("fault", ["prepared", "started"])
@pytest.mark.parametrize("damage", ["digest", "allocation", "context", "version", "attempt", "workspace"])
def test_corrupt_original_request_fails_without_selection_or_dispatch(
    sqlite_tmp_path: Path,
    fault: str,
    damage: str,
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        runtime = RecordingRuntime()
        with pytest.raises(ReferenceInterrupted):
            step(store, CountingAllocator(), runtime, fault=fault)
        original = store.operation(OPERATION)
        envelope = cast(dict[str, object], json.loads(original.request_json))
        request = cast(dict[str, object], envelope["request"])
        if damage != "digest":
            request[damage] = 2 if damage == "version" else "corrupt"
        data = manifest_bytes(envelope).decode()
        corrupt = replace(
            original,
            request_json=data,
            request_digest="bad" if damage == "digest" else hashlib.sha256(data.encode()).hexdigest(),
        )
        unavailable = CountingAllocator(None)
        with patch.object(store, "operation", return_value=corrupt):
            with pytest.raises(ValueError):
                step(store, unavailable, runtime)
        assert unavailable.calls == 0 and runtime.requests == []


@pytest.mark.parametrize("effort", [None, "high"])
def test_new_input_cannot_use_legacy_model_only_resolution(sqlite_tmp_path: Path, effort: str | None) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        runtime = RecordingRuntime()
        runtime.override = RuntimeIdentity("model", "model", None, "reference:v1")
        allocator = CountingAllocator(replace(SELECTED, reasoning_effort=effort))
        assert step(store, allocator, runtime) == "identity_unavailable"
        assert store.find_artifact(OPERATION, "identity") is None


def test_legacy_explicit_effort_does_not_accept_model_only_identity(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        legacy_attempt(store, effort="high")
        runtime = RecordingRuntime()
        runtime.override = RuntimeIdentity("model", "model", None, "reference:v1")
        assert step(store, CountingAllocator(None), runtime) == "identity_unavailable"


@pytest.mark.parametrize("field", ["resolved_provider", "resolved_effort", "requested_effort", "observed_effort"])
@pytest.mark.parametrize("value", [None, "different"])
def test_explicit_identity_requires_exact_resolution_preserves_unknown_observations(
    sqlite_tmp_path: Path,
    field: str,
    value: str | None,
) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        runtime = RecordingRuntime()
        identity = RuntimeIdentity(
            "model",
            "model",
            None,
            "reference:v1",
            requested_provider="provider",
            resolved_provider="provider",
            requested_effort="high",
            resolved_effort="high",
        )
        runtime.override = replace(identity, **{field: value})
        expected = "running" if (field, value) == ("observed_effort", None) else "identity_unavailable"
        assert step(store, CountingAllocator(), runtime) == expected


def test_restored_unknown_resolution_uses_original_durable_evidence(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        runtime = RecordingRuntime()
        assert step(store, CountingAllocator(), runtime) == "running"
        persisted = store.artifact(OPERATION, "identity")
        runtime.override = runtime.requests[0].identity
        assert step(store, CountingAllocator(None), runtime) == "running"
        assert store.artifact(OPERATION, "identity") == persisted
        identity = decode_identity(persisted)
        assert identity.observed is identity.observed_provider is identity.observed_effort is None
        assert identity.resolved_effort == "high"
        runtime.override = replace(runtime.override, observed_effort="different")
        assert step(store, CountingAllocator(None), runtime) == "identity_unavailable"


@pytest.mark.parametrize(
    "data", [b"null", b"{}", b'{"version":2,"allocation":{}}', b'{"version":true,"allocation":{}}']
)
def test_allocation_codec_rejects_invalid_documents(data: bytes) -> None:
    with pytest.raises(ValueError):
        decode_allocation(data)


def test_allocation_codec_and_identity_default_effort() -> None:
    assert decode_allocation(encode_allocation(SELECTED)) == SELECTED
    assert not is_legacy_allocation(encode_allocation(SELECTED))
    allocation = replace(SELECTED, reasoning_effort=None)
    identity = RuntimeIdentity(
        "model",
        "model",
        None,
        "reference:v1",
        requested_provider="provider",
        resolved_provider="provider",
        resolved_effort="runtime-default",
    )
    assert identity_matches(identity, allocation, "reference:v1", require_resolved=True)
    assert decode_identity(manifest_bytes(asdict(identity))) == identity


@pytest.mark.parametrize("value", ["", " ", 1, False, {}, []])
def test_unconfigured_effort_still_requires_well_formed_identity(value: object) -> None:
    selected = replace(SELECTED, reasoning_effort=None)
    identity = RuntimeIdentity(
        "model",
        "model",
        None,
        "reference:v1",
        requested_provider="provider",
        resolved_provider="provider",
        resolved_effort=cast(str, value),
    )
    assert not identity_matches(identity, selected, "reference:v1", require_resolved=True)


def test_unconfigured_effort_does_not_allow_changed_resolution_or_observation(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial(store)
        selected = replace(SELECTED, reasoning_effort=None)
        runtime = RecordingRuntime()
        runtime.override = RuntimeIdentity(
            "model",
            "model",
            None,
            "reference:v1",
            requested_provider="provider",
            resolved_provider="provider",
            resolved_effort="high",
        )
        assert step(store, CountingAllocator(selected), runtime) == "running"
        original = store.artifact(OPERATION, "identity")
        runtime.override = replace(runtime.override, resolved_effort=None)
        assert step(store, CountingAllocator(None), runtime) == "running"
        unknown = runtime.override
        assert unknown is not None
        for change in ({"resolved_effort": "low"}, {"observed_effort": "low"}):
            runtime.override = replace(unknown, **change)
            assert step(store, CountingAllocator(None), runtime) == "identity_unavailable"
            assert store.artifact(OPERATION, "identity") == original


@pytest.mark.parametrize(
    "field,value", [("version", True), ("version", 2), ("fence", 0), ("fence", False), ("handle", ""), ("identity", {})]
)
def test_runtime_receipt_codec_fails_closed(field: str, value: object) -> None:
    document: dict[str, object] = {
        "version": 1,
        "handle": "native-handle",
        "fence": 1,
        "identity": asdict(RuntimeIdentity("model", None, None, "reference:v1")),
    }
    document[field] = value
    with pytest.raises(ValueError):
        decode_runtime_receipt(manifest_bytes(document))
