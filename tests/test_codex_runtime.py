# SPDX-License-Identifier: Apache-2.0
"""Synthetic native app-server responses, never live model execution."""

from dataclasses import replace

import pytest

from creatidy_kernel.adapters.codex_runtime import CodexInputs, CodexRejected, CodexRuntime
from creatidy_kernel.adapters.fake_execution import FakeWorkspace
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.execution import (
    Activity,
    ArtifactManifest,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    OperationKey,
    Presence,
    RuntimeIdentity,
    TrustMode,
    UnsupportedExecution,
    WorkspaceSpec,
)
from creatidy_kernel.ports.execution import Runtime


class NativeConnection:
    version = "codex-cli 1.2.3"
    methods = frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.status = "inProgress"
        self.fail: str | None = None
        self.missing = False
        self.model = "model-one"

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, params))
        if self.fail == method:
            raise OSError("lost response after delivery")
        if method == "thread/start":
            return {"thread": {"id": "thread-one"}, "model": self.model, "modelProvider": "provider-one"}
        if method == "turn/start":
            return {"turn": {"id": "turn-one", "status": "inProgress"}}
        if method == "thread/read":
            return {
                "thread": {
                    "id": "thread-one",
                    "turns": [] if self.missing else [{"id": "turn-one", "status": self.status}],
                }
            }
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)


def setup() -> tuple[NativeConnection, CodexRuntime, ExecutionRequest]:
    workspace = FakeWorkspace().materialize(
        "workspace-one", WorkspaceSpec("repo", "revision", (), "image", "trusted", TrustMode.TRUSTED_DEVELOPMENT)
    )
    attempt = AttemptSpec(
        "attempt-one",
        "program-one",
        "unit-one",
        1,
        "spec-one",
        allocation_reference="allocation-one",
        agent_definition_reference="agent-one",
        workspace_reference=workspace.key,
        context_reference="context-one",
    )
    request = ExecutionRequest(
        OperationKey("operation-one", "effect-one", "digest-one"),
        attempt,
        workspace,
        "context-one",
        "allocation-one",
        "capability-one",
        RuntimeIdentity("model-one", None, None, "agent-one"),
        1,
    )
    connection = NativeConnection()

    def resolve(value: ExecutionRequest) -> CodexInputs:
        return CodexInputs(
            value.workspace.key,
            value.context_reference,
            value.allocation_reference,
            value.capability_reference,
            "/sandbox/checkout",
            "Implement the pinned task",
            "model-one",
            "provider-one",
        )

    def collect(value: ExecutionRequest) -> Candidate:
        return Candidate(value.attempt.attempt_id, value.attempt.digest, ArtifactManifest(value.workspace.key, ()))

    runtime = CodexRuntime(
        connection, version=connection.version, resolve=resolve, authorize=lambda _: True, collect=collect
    )
    return connection, runtime, request


def test_native_start_duplicate_and_terminal_replay() -> None:
    connection, runtime, request = setup()
    port: Runtime = runtime
    handle = port.start(request)
    assert handle == "codex:thread-one:turn-one"
    assert port.start(request) == handle
    assert [method for method, _ in connection.calls].count("thread/start") == 1
    assert port.reconcile(request.operation, handle).presence is Presence.FOUND
    observation = port.observe(handle, now=10)
    assert observation.activity is Activity.RUNNING and observation.fresh_until == 40
    assert observation.identity.requested == "model-one"
    assert observation.identity.resolved == "model-one" and observation.identity.observed is None
    connection.status = "completed"
    assert port.observe(handle, now=100).activity is Activity.TERMINAL
    assert port.observe(handle, now=200).activity is Activity.TERMINAL
    assert port.candidate(handle) == Candidate(
        request.attempt.attempt_id, request.attempt.digest, ArtifactManifest("workspace-one", ())
    )

    def missing(_value: ExecutionRequest) -> None:
        return None

    runtime.collect = missing
    with pytest.raises(ExecutionConflict, match="terminal replay"):
        port.candidate(handle)
    with pytest.raises(ExecutionConflict):
        port.start(replace(request, operation=replace(request.operation, request_digest="changed")))


@pytest.mark.parametrize("lost_at", ["thread/start", "turn/start"])
def test_lost_replies_are_uncertain_and_never_duplicate_turns(lost_at: str) -> None:
    connection, runtime, request = setup()
    connection.fail = lost_at
    with pytest.raises(OSError):
        runtime.start(request)
    assert runtime.reconcile(request.operation).presence is Presence.UNKNOWN
    with pytest.raises(ExecutionConflict, match="uncertain"):
        runtime.start(request)
    assert [method for method, _ in connection.calls].count("turn/start") <= 1


def test_restart_requires_durable_handle_and_missing_session_stays_unknown() -> None:
    connection, runtime, request = setup()
    handle = runtime.start(request)
    _, restarted, _ = setup()
    assert restarted.reconcile(request.operation).presence is Presence.UNKNOWN
    assert restarted.restore(request, handle).presence is Presence.FOUND
    connection.missing = True
    assert runtime.reconcile(request.operation, handle).presence is Presence.UNKNOWN
    assert runtime.observe(handle, now=90).activity is Activity.UNKNOWN


def test_quiet_healthy_work_and_cancel_race() -> None:
    connection, runtime, request = setup()
    handle = runtime.start(request)
    assert runtime.observe(handle, now=900).activity is Activity.RUNNING
    assert runtime.cancel(handle).cancellation_requested
    assert runtime.cancel(handle).activity is Activity.UNKNOWN
    connection.status = "interrupted"
    assert runtime.observe(handle, now=1000).activity is Activity.TERMINAL
    assert runtime.candidate(handle) is None
    connection.status = "inProgress"
    assert runtime.observe(handle, now=1001).activity is Activity.TERMINAL
    connection.status = "completed"
    with pytest.raises(ExecutionConflict, match="terminal replay"):
        runtime.observe(handle, now=1002)


def test_domain_rejection_mismatch_and_candidate_validation() -> None:
    connection, runtime, request = setup()
    connection.model = "different-model"
    with pytest.raises(CodexRejected, match="resolved model"):
        runtime.start(request)
    assert runtime.reconcile(request.operation).presence is Presence.UNKNOWN
    connection, runtime, request = setup()
    handle = runtime.start(request)
    connection.status = "completed"

    def invalid(value: ExecutionRequest) -> Candidate:
        return Candidate("another-attempt", value.attempt.digest, ArtifactManifest(value.workspace.key, ()))

    runtime.collect = invalid
    with pytest.raises(ExecutionConflict, match="candidate"):
        runtime.candidate(handle)


def test_capability_and_version_mismatch_fail_before_delivery() -> None:
    connection, runtime, request = setup()
    with pytest.raises(UnsupportedExecution, match="version"):
        CodexRuntime(
            connection, version="other", resolve=runtime.resolve, authorize=runtime.authorize, collect=runtime.collect
        )
    connection.methods = frozenset({"thread/start"})
    with pytest.raises(UnsupportedExecution, match="native methods"):
        CodexRuntime(
            connection,
            version=connection.version,
            resolve=runtime.resolve,
            authorize=runtime.authorize,
            collect=runtime.collect,
        )
    with pytest.raises(UnsupportedExecution, match="isolation"):
        runtime.start(
            replace(
                request,
                workspace=replace(
                    request.workspace, spec=replace(request.workspace.spec, trust_mode=TrustMode.ISOLATED)
                ),
            )
        )
    with pytest.raises(UnsupportedExecution, match="attest"):
        runtime.start(replace(request, identity=replace(request.identity, observed="self-report")))
    assert connection.calls == []
