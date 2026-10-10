# SPDX-License-Identifier: Apache-2.0
"""Journal-to-native admission using existing producer-shaped synthetic responses."""

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from test_codex_runtime import NativeConnection

from creatidy_kernel.adapters.codex_runtime import CodexInputs, CodexRuntime
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.authority import AuthorityGrant, OperationIntent, Principal
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
from creatidy_kernel.core.execution import (
    ExecutionConflict,
    ExecutionRequest,
    OperationKey,
    Presence,
    RuntimeIdentity,
    TrustMode,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.ports.allocation import encode_allocation
from creatidy_kernel.ports.application import manifest_bytes
from creatidy_kernel.ports.program_store import OperationRecord

pytest_plugins = ["test_sqlite_store"]


@dataclass
class Prepared:
    store: SQLiteProgramStore
    request: ExecutionRequest
    intent: OperationIntent
    grant: AuthorityGrant
    root: Path
    database: Path
    decoder: Callable[[OperationRecord], OperationIntent]

    def claim(self) -> None:
        assert self.store.claim(self.intent.operation_id, now=1, lease_seconds=10) == 1

    def runtime(
        self,
        *,
        clock: Callable[[], int] = lambda: 1,
        connection: NativeConnection | None = None,
        cwd: str | None = None,
    ) -> tuple[CodexRuntime, NativeConnection]:
        connection = connection if connection is not None else NativeConnection()
        dispatch, recovery = self.store.runtime_authorizers(clock)

        def resolve(request: ExecutionRequest) -> CodexInputs:
            return CodexInputs(
                request.workspace.key,
                request.context_reference,
                request.allocation_reference,
                request.capability_reference,
                cwd if cwd is not None else str(self.root),
                "Perform the bounded synthetic task",
                "model-one",
                "provider-one",
            )

        return CodexRuntime(
            connection,
            version=connection.version,
            resolve=resolve,
            authorize=lambda _: True,
            authorize_resolved=dispatch,
            authorize_recovery=recovery,
            collect=lambda _: None,
        ), connection

    def receipt(self, runtime: CodexRuntime, handle: str) -> None:
        identity = runtime.observe(handle, now=1).identity
        self.store.finalize_artifact(
            self.intent.operation_id,
            "runtime-receipt",
            manifest_bytes(
                {
                    "version": 1,
                    "handle": handle,
                    "fence": 1,
                    "identity": asdict(identity),
                }
            ),
        )
        self.store.observe(self.intent.operation_id, 1, "accepted", "accepted", reference=handle)


@pytest.fixture
def grant_operations() -> frozenset[str]:
    return frozenset({"execute"})


@pytest.fixture
def prepared(sqlite_tmp_path: Path, grant_operations: frozenset[str]) -> Iterator[Prepared]:
    root = sqlite_tmp_path / "workspace"
    root.mkdir()
    database = sqlite_tmp_path / "kernel.db"
    expected: dict[str, object] = {}

    def decoder(operation: OperationRecord) -> OperationIntent:
        envelope = cast(dict[str, object], json.loads(operation.request_json))
        if envelope != {"version": 1, "request": expected}:
            raise ValueError("original request differs from this controller's producer")
        return OperationIntent(
            operation.operation_id,
            operation.effect_key,
            operation.request_digest,
            "execute",
            "repo-one",
            Path("."),
        )

    with SQLiteProgramStore(database, resolve_authority_intent=decoder) as store:
        program = store.create(
            ProgramSpec(
                "program-one",
                "bounded task",
                (WorkUnit("unit-one", "produce a candidate", acceptance_criteria=("verified",)),),
                acceptance_criteria=("independently verified",),
                authority=AuthorityEnvelope("owner"),
                policy_references=(PolicyReference("acceptance", "v1", "sha256:synthetic-policy"),),
            ),
            "create",
        )
        program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
        workspace = WorkspaceHandle(
            "workspace-one",
            WorkspaceSpec(
                "repo-one",
                "base-one",
                (),
                "toolchain-one",
                "trusted",
                TrustMode.TRUSTED_DEVELOPMENT,
            ),
        )
        allocation = Allocation("codex", "provider-one", "model-one", frozenset({"reference"}), 128, "synthetic")
        allocation_bytes = encode_allocation(allocation)
        context = manifest_bytes([])
        attempt = AttemptSpec(
            "attempt-one",
            program.program_id,
            "unit-one",
            program.spec.revision,
            program.spec.digest,
            allocation_reference="sha256:" + hashlib.sha256(allocation_bytes).hexdigest(),
            context_reference="sha256:" + hashlib.sha256(context).hexdigest(),
            workspace_reference=workspace.key,
            agent_definition_reference="agent-one",
        )
        expected.update(
            {
                "version": 1,
                "attempt": attempt.digest,
                "workspace": workspace.key,
                "allocation": allocation_bytes.decode(),
                "context": context.decode(),
            }
        )
        operation_id = f"runtime:{attempt.attempt_id}"
        store.admit_with_intent(
            program.program_id,
            "prepare",
            PrepareAttempt(program.revision, "owner", attempt),
            operation_id,
            operation_id,
            expected,
        )
        grant = AuthorityGrant(
            "grant-one",
            "owner",
            attempt.digest,
            program.program_id,
            program.spec.digest,
            workspace.spec.repository,
            root,
            frozenset({root}),
            frozenset(),
            grant_operations,
            100,
            True,
        )
        store.issue(Principal("owner", "owner"), grant)
        intent = decoder(store.operation(operation_id))
        store.check(Principal(attempt.attempt_id, "worker"), grant.grant_id, attempt, intent, 1)
        request = ExecutionRequest(
            OperationKey(intent.operation_id, intent.effect_key, intent.request_digest),
            attempt,
            workspace,
            cast(str, attempt.context_reference),
            cast(str, attempt.allocation_reference),
            grant.grant_id,
            RuntimeIdentity("model-one", None, None, "agent-one", requested_provider="provider-one"),
            1,
            allocation,
        )
        yield Prepared(store, request, intent, grant, root, database, decoder)


def test_claim_native_stages_and_exact_duplicate_receipt(prepared: Prepared) -> None:
    runtime, connection = prepared.runtime()
    with pytest.raises(ExecutionConflict, match="authority"):
        runtime.start(prepared.request)
    assert connection.calls == []
    prepared.claim()
    handle = runtime.start(prepared.request)
    assert handle == "codex:thread-one:turn-one"
    assert runtime.start(prepared.request) == handle
    assert [method for method, _ in connection.calls] == ["thread/start", "turn/start"]
    assert prepared.store.operation(prepared.intent.operation_id).attempts == 1
    another, other_connection = prepared.runtime()
    with pytest.raises(ExecutionConflict, match="authority"):
        another.start(prepared.request)
    assert other_connection.calls == []


def test_cwd_changed_scope_fence_and_expiry_are_not_execution_permissions(prepared: Prepared) -> None:
    prepared.claim()
    for request in (
        replace(prepared.request, fence=2),
        replace(prepared.request, capability_reference="unknown-grant"),
        replace(
            prepared.request,
            workspace=replace(
                prepared.request.workspace, spec=replace(prepared.request.workspace.spec, repository="other")
            ),
        ),
    ):
        runtime, connection = prepared.runtime()
        with pytest.raises(ExecutionConflict, match="authority"):
            runtime.start(request)
        assert connection.calls == []
    for cwd, clock in ((str(prepared.database.parent), lambda: 1), (str(prepared.root), lambda: 11)):
        runtime, connection = prepared.runtime(cwd=cwd, clock=clock)
        with pytest.raises(ExecutionConflict, match="authority"):
            runtime.start(prepared.request)
        assert connection.calls == []


def test_revocation_between_thread_and_turn_stops_next_native_send(prepared: Prepared) -> None:
    prepared.claim()

    class RevokeOnThread(NativeConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            response = super().request(method, params)
            if method == "thread/start":
                prepared.store.revoke(Principal("owner", "owner"), prepared.grant.grant_id)
            return response

    runtime, connection = prepared.runtime(connection=RevokeOnThread())
    with pytest.raises(ExecutionConflict, match="turn inputs"):
        runtime.start(prepared.request)
    assert [method for method, _ in connection.calls] == ["thread/start"]
    assert runtime.reconcile(prepared.request.operation).presence is Presence.UNKNOWN
    with pytest.raises(ExecutionConflict, match="uncertain"):
        runtime.start(prepared.request)


@pytest.mark.parametrize("lost_method", ("thread/start", "turn/start"))
def test_lost_reply_cannot_replay_native_start_on_new_adapter_or_reopen(prepared: Prepared, lost_method: str) -> None:
    prepared.claim()
    runtime, connection = prepared.runtime()
    connection.fail = lost_method
    with pytest.raises(OSError, match="lost response"):
        runtime.start(prepared.request)
    fresh, fresh_connection = prepared.runtime()
    with pytest.raises(ExecutionConflict, match="authority"):
        fresh.start(prepared.request)
    assert fresh_connection.calls == []
    prepared.store.close()
    with SQLiteProgramStore(prepared.database, resolve_authority_intent=prepared.decoder) as reopened:
        dispatch, _ = reopened.runtime_authorizers(lambda: 2)
        assert not dispatch(prepared.request, str(prepared.root), "thread/start")
        assert reopened.operation(prepared.intent.operation_id).attempts == 1
        with pytest.raises(OperationConflict, match="reconciliation"):
            reopened.claim(prepared.intent.operation_id, now=20, lease_seconds=10)


def test_restart_cannot_adopt_a_preexisting_claim_even_before_native_send(prepared: Prepared) -> None:
    prepared.claim()
    prepared.store.close()
    with SQLiteProgramStore(prepared.database, resolve_authority_intent=prepared.decoder) as reopened:
        dispatch, _ = reopened.runtime_authorizers(lambda: 2)
        assert not dispatch(prepared.request, str(prepared.root), "thread/start")
        assert reopened.find_artifact(prepared.intent.operation_id, "runtime-dispatch:1:thread") is None


def test_owned_readonly_recovery_survives_expiry_revocation_cancel_and_reopen(prepared: Prepared) -> None:
    prepared.claim()
    runtime, _ = prepared.runtime()
    handle = runtime.start(prepared.request)
    prepared.receipt(runtime, handle)
    prepared.store.revoke(Principal("owner", "owner"), prepared.grant.grant_id)
    program = prepared.store.load(prepared.request.attempt.program_id)
    prepared.store.admit(
        program.program_id, "cancel", CancelAttempt(program.revision, "owner", prepared.request.attempt.attempt_id)
    )
    prepared.store.close()
    with SQLiteProgramStore(prepared.database, resolve_authority_intent=prepared.decoder) as reopened:
        prepared.store = reopened
        recovered, connection = prepared.runtime(clock=lambda: 100)
        assert recovered.restore(prepared.request, handle).presence is Presence.FOUND
        assert [method for method, _ in connection.calls] == ["thread/read"]
        assert recovered.start(prepared.request) == handle  # Cached receipt, not another send.
        assert [method for method, _ in connection.calls] == ["thread/read"]
        new_start, new_connection = prepared.runtime(clock=lambda: 100)
        with pytest.raises(ExecutionConflict, match="authority"):
            new_start.start(prepared.request)
        assert new_connection.calls == []


def test_foreign_handle_changed_workspace_and_missing_receipt_refuse_readback(prepared: Prepared) -> None:
    prepared.claim()
    runtime, _ = prepared.runtime()
    handle = runtime.start(prepared.request)
    no_receipt, connection = prepared.runtime()
    with pytest.raises(ExecutionConflict, match="not owned"):
        no_receipt.restore(prepared.request, handle)
    assert connection.calls == []
    prepared.receipt(runtime, handle)
    changed = replace(
        prepared.request,
        workspace=replace(
            prepared.request.workspace, spec=replace(prepared.request.workspace.spec, base_revision="different")
        ),
    )
    for request, target in ((prepared.request, "codex:foreign:foreign"), (changed, handle)):
        other, other_connection = prepared.runtime()
        with pytest.raises(ExecutionConflict, match="not owned"):
            other.restore(request, target)
        assert other_connection.calls == []


def test_expiry_during_permit_publication_still_sends_nothing(prepared: Prepared) -> None:
    prepared.claim()
    times = iter((1, 100))
    runtime, connection = prepared.runtime(clock=lambda: next(times))
    with pytest.raises(ExecutionConflict, match="authority"):
        runtime.start(prepared.request)
    assert connection.calls == []
    assert prepared.store.find_artifact(prepared.intent.operation_id, "runtime-dispatch:1:thread") is not None
