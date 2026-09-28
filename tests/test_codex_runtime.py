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
from creatidy_kernel.core.resources import Allocation
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
        self.provider = "provider-one"
        self.effort: object = None

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, params))
        if self.fail == method:
            raise OSError("lost response after delivery")
        if method == "thread/start":
            # Native response subset from openai/codex@3fd5160cd6c78f2051bb54359d53f09207171733:
            # codex-rs/app-server-protocol/schema/typescript/v2/ThreadStartResponse.ts.
            return {
                "thread": {"id": "thread-one"},
                "model": self.model,
                "modelProvider": self.provider,
                "reasoningEffort": self.effort,
            }
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


def exact_setup(effort: str | None = "high") -> tuple[NativeConnection, CodexRuntime, ExecutionRequest]:
    connection, original, request = setup()
    allocation = Allocation("codex", "provider-one", "model-one", frozenset(), 0, "fixed", effort)
    request = replace(
        request,
        allocation=allocation,
        identity=replace(request.identity, requested_provider="provider-one", requested_effort=effort),
    )
    connection.effort = effort
    runtime = CodexRuntime(
        connection,
        version=connection.version,
        resolve=lambda value: replace(original.resolve(value), reasoning_effort=effort),
        authorize=original.authorize,
        collect=original.collect,
        supported_efforts=frozenset({("provider-one", "model-one", "high")}),
    )
    return connection, runtime, request


def test_exact_effort_dispatch_and_native_resolution_remain_distinct_from_observation() -> None:
    connection, runtime, request = exact_setup()
    handle = runtime.start(request)
    assert connection.calls == [
        (
            "thread/start",
            {
                "model": "model-one",
                "modelProvider": "provider-one",
                "cwd": "/sandbox/checkout",
                "sandbox": "workspace-write",
                "approvalPolicy": "never",
                "config": {"model_reasoning_effort": "high"},
            },
        ),
        (
            "turn/start",
            {
                "threadId": "thread-one",
                "input": [{"type": "text", "text": "Implement the pinned task"}],
                "effort": "high",
            },
        ),
    ]
    identity = runtime.observe(handle, now=10).identity
    assert (identity.requested_provider, identity.resolved_provider, identity.observed_provider) == (
        "provider-one",
        "provider-one",
        None,
    )
    assert (identity.requested_effort, identity.resolved_effort, identity.observed_effort) == ("high", "high", None)
    assert runtime.cancel(handle).identity == identity
    _, restarted, _ = exact_setup()
    assert restarted.restore(request, handle).presence is Presence.FOUND
    restored = restarted.observe(handle, now=11).identity
    assert restored.requested_provider == "provider-one" and restored.requested_effort == "high"
    assert restored.resolved is None and restored.resolved_provider is None and restored.resolved_effort is None


@pytest.mark.parametrize("field", ["runtime_id", "provider_id", "model_id", "reasoning_effort"])
def test_exact_allocation_rejects_configured_identity_mismatch_before_delivery(field: str) -> None:
    connection, runtime, request = exact_setup()
    assert request.allocation is not None
    allocation = replace(request.allocation, **{field: "other"})
    identity = replace(
        request.identity,
        requested=allocation.model_id,
        requested_provider=allocation.provider_id,
        requested_effort=allocation.reasoning_effort,
    )
    request = replace(request, allocation=allocation, identity=identity)
    with pytest.raises(ExecutionConflict):
        runtime.start(request)
    assert connection.calls == []


@pytest.mark.parametrize("field", ["requested", "requested_provider", "requested_effort"])
def test_execution_request_rejects_allocation_identity_mismatch(field: str) -> None:
    _, _, request = exact_setup()
    with pytest.raises(ExecutionConflict, match="requested identity"):
        replace(request, identity=replace(request.identity, **{field: "other"}))


@pytest.mark.parametrize("effort", [None, "medium", "", 1, ["high"]])
def test_explicit_effort_requires_exact_native_resolution_before_turn(effort: object) -> None:
    connection, runtime, request = exact_setup()
    connection.effort = effort
    with pytest.raises(CodexRejected, match="reasoning"):
        runtime.start(request)
    assert [method for method, _ in connection.calls] == ["thread/start"]
    with pytest.raises(ExecutionConflict, match="uncertain"):
        runtime.start(request)


def test_provider_receipt_must_match_exactly_before_turn() -> None:
    connection, runtime, request = exact_setup()
    connection.provider = "other-provider"
    with pytest.raises(CodexRejected, match="model/provider"):
        runtime.start(request)
    assert [method for method, _ in connection.calls] == ["thread/start"]


@pytest.mark.parametrize(
    "bindings",
    [
        frozenset[tuple[str, str, str]](),
        frozenset({("provider-one", "model-one", "medium")}),
        frozenset({("other", "model-one", "high")}),
        frozenset({("provider-one", "other", "high")}),
    ],
)
def test_effort_support_is_explicit_and_provider_model_specific(bindings: frozenset[tuple[str, str, str]]) -> None:
    connection, original, request = exact_setup()
    runtime = CodexRuntime(
        connection,
        version=connection.version,
        resolve=original.resolve,
        authorize=original.authorize,
        collect=original.collect,
        supported_efforts=bindings,
    )
    with pytest.raises(UnsupportedExecution, match="trusted support"):
        runtime.start(request)
    assert connection.calls == []


@pytest.mark.parametrize("resolved", [None, "high"])
def test_null_effort_preserves_legacy_defaults_without_inventing_evidence(resolved: str | None) -> None:
    connection, runtime, request = exact_setup(None)
    connection.effort = resolved
    handle = runtime.start(request)
    assert "config" not in connection.calls[0][1]
    assert "effort" not in connection.calls[1][1]
    identity = runtime.observe(handle, now=10).identity
    assert identity.requested_effort is None
    assert identity.resolved_effort == resolved
    assert identity.observed_effort is None


@pytest.mark.parametrize("field", ["observed_provider", "observed_effort"])
def test_no_requested_attestation_can_be_fabricated(field: str) -> None:
    connection, runtime, request = exact_setup()
    with pytest.raises(UnsupportedExecution, match="attest"):
        runtime.start(replace(request, identity=replace(request.identity, **{field: "claim"})))
    assert connection.calls == []


def test_selected_effort_cannot_be_silently_omitted_by_input_resolver() -> None:
    connection, runtime, request = exact_setup()
    original = runtime.resolve

    def omit_effort(value: ExecutionRequest) -> CodexInputs:
        return replace(original(value), reasoning_effort=None)

    runtime.resolve = omit_effort
    with pytest.raises(ExecutionConflict, match="effort"):
        runtime.start(request)
    assert connection.calls == []


def test_opaque_variant_never_changes_native_effort() -> None:
    connection, runtime, request = exact_setup()
    assert request.allocation is not None
    request = replace(request, allocation=replace(request.allocation, variant="opaque-not-an-effort"))
    runtime.start(request)
    assert connection.calls[1][1]["effort"] == "high"
    assert all("variant" not in params for _, params in connection.calls)


def test_requested_resolution_claims_do_not_become_native_evidence_after_restore() -> None:
    _, runtime, request = exact_setup()
    request = replace(
        request,
        identity=replace(
            request.identity, resolved="model-one", resolved_provider="provider-one", resolved_effort="high"
        ),
    )
    runtime.restore(request, "codex:thread-one:turn-one")
    identity = runtime.observe("codex:thread-one:turn-one", now=10).identity
    assert identity.resolved is None and identity.resolved_provider is None and identity.resolved_effort is None
