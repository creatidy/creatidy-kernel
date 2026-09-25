# SPDX-License-Identifier: Apache-2.0
"""Offline Runtime/Workspace contract and policy-simulation regressions."""

from dataclasses import replace

import pytest

from creatidy_kernel.adapters.fake_execution import FakeRuntime, FakeWorkspace
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.execution import (
    Activity,
    Artifact,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    OperationKey,
    Overlay,
    Presence,
    RuntimeIdentity,
    TrustMode,
    UnsupportedExecution,
    WorkerScope,
    WorkspaceSpec,
)
from creatidy_kernel.ports.execution import Runtime, Workspace


def setup() -> tuple[FakeWorkspace, FakeRuntime, ExecutionRequest, WorkerScope]:
    workspace = FakeWorkspace()
    spec = WorkspaceSpec(
        "repo:one",
        "base:abc",
        (Overlay("src/source", "sha256:abc"),),
        "image:1",
        "trusted",
        TrustMode.TRUSTED_DEVELOPMENT,
    )
    handle = workspace.materialize("ws-1", spec)
    attempt = AttemptSpec(
        "attempt-1",
        "program-1",
        "unit-1",
        1,
        "approved:1",
        allocation_reference="allocation:1",
        agent_definition_reference="agent:1",
        workspace_reference="ws-1",
        context_reference="context:1",
    )
    request = ExecutionRequest(
        OperationKey("op-1", "run-1", "digest:1"),
        attempt,
        handle,
        "context:1",
        "allocation:1",
        "capability:1",
        RuntimeIdentity("requested:1", "resolved:1", "observed:1", "agent:1"),
        3,
    )
    scope = WorkerScope(
        "ws-1",
        "attempt-1",
        3,
        100,
        frozenset({"src"}),
        frozenset({"write", "publish_candidate"}),
        frozenset(),
        frozenset(),
    )
    return workspace, FakeRuntime(), request, scope


def test_replaceable_ports_and_duplicate_start_lost_reply() -> None:
    workspace, runtime, request, scope = setup()
    runtime_port: Runtime = runtime
    workspace_port: Workspace = workspace
    handle = runtime_port.start(request)
    assert runtime_port.start(request) == handle
    assert runtime_port.reconcile(request.operation).presence is Presence.FOUND
    assert runtime_port.reconcile(request.operation, handle).handle == handle
    assert workspace_port.reconcile("ws-1").presence is Presence.FOUND
    with pytest.raises(ExecutionConflict):
        runtime.start(replace(request, operation=replace(request.operation, request_digest="other")))
    with pytest.raises(ExecutionConflict):
        workspace.materialize("ws-1", replace(request.workspace.spec, base_revision="other"))
    manifest = workspace.publish_artifact(request.workspace, scope, Artifact("src/patch", "sha256:result"), now=5)
    candidate = Candidate("attempt-1", request.attempt.digest, manifest)
    runtime.publish(handle, scope, candidate, now=6)
    runtime.publish(handle, scope, candidate, now=7)
    assert runtime.candidate(handle) == candidate
    assert runtime.observe(handle, now=900).activity is Activity.TERMINAL
    assert runtime.cancel(handle).activity is Activity.TERMINAL
    with pytest.raises(ExecutionConflict):
        runtime.publish(handle, scope, replace(candidate, spec_digest="other"), now=8)
    with pytest.raises(ExecutionConflict):
        workspace_port.cleanup(request.workspace, worker_settled=False)
    assert workspace_port.collect(request.workspace) == manifest
    workspace_port.cleanup(request.workspace, worker_settled=True)
    assert workspace_port.reconcile("ws-1").presence is Presence.ABSENT


def test_freshness_healthy_wait_cancel_race_and_unknown_recovery() -> None:
    _, runtime, request, _ = setup()
    handle = runtime.start(request)
    runtime.progress(handle, now=100, fresh_for=200, waiting=True)
    assert runtime.observe(handle, now=299).activity is Activity.WAITING
    assert runtime.cancel(handle).cancellation_requested
    assert runtime.observe(handle, now=301).activity is Activity.UNKNOWN
    assert runtime.reconcile(request.operation, "wrong-handle").presence is Presence.UNKNOWN
    runtime.lose(handle)
    assert runtime.reconcile(request.operation).presence is Presence.UNKNOWN
    assert FakeRuntime(recoverable=False).reconcile(request.operation).presence is Presence.UNKNOWN
    assert FakeRuntime().reconcile(request.operation).presence is Presence.ABSENT


def test_unsupported_identity_and_stale_or_invalid_publication() -> None:
    workspace, _, request, scope = setup()
    runtime = FakeRuntime(identity_supported=False)
    handle = runtime.start(request)
    observation = runtime.observe(handle, now=0)
    assert observation.identity.requested == "requested:1"
    assert observation.identity.resolved is None and observation.identity.observed is None
    candidate = Candidate("attempt-1", request.attempt.digest, workspace.collect(request.workspace))
    for invalid in (replace(scope, fence=4), replace(scope, expires_at=0), replace(scope, operations=frozenset())):
        with pytest.raises(ExecutionConflict):
            runtime.publish(handle, invalid, candidate, now=1)
    with pytest.raises(ExecutionConflict):
        runtime.publish(handle, scope, replace(candidate, attempt_id="another"), now=1)
    runtime.publish(handle, scope, candidate, now=1)
    assert runtime.candidate(handle) == candidate  # Candidate is not a verification verdict.


def test_workspace_policy_escape_and_honest_trust_mode() -> None:
    workspace, _, request, scope = setup()
    handle = request.workspace
    for path in ("../control", "/etc/escape", "src/../control", "src//file"):
        with pytest.raises(ExecutionConflict):
            workspace.access(handle, scope, now=1, operation="write", path=path)
    with pytest.raises(ExecutionConflict):
        workspace.access(handle, scope, now=1, operation="write", path="src/file", symlink_target="control/secrets")
    for operation in ("grant", "accept", "gate", "control", "credentials"):
        with pytest.raises(ExecutionConflict):
            workspace.access(
                handle, replace(scope, operations=scope.operations | {operation}), now=1, operation=operation
            )
    with pytest.raises(ExecutionConflict):
        workspace.access(handle, scope, now=1, operation="write", network="example.invalid")
    with pytest.raises(ExecutionConflict):
        workspace.access(handle, scope, now=1, operation="write", mount="/var/run/docker.sock")
    with pytest.raises(ExecutionConflict):
        workspace.publish_artifact(handle, replace(scope, expires_at=1), Artifact("src/file", "sha256:1"), now=1)
    with pytest.raises(UnsupportedExecution):
        workspace.materialize("isolated", replace(handle.spec, trust_mode=TrustMode.ISOLATED))
    with pytest.raises(UnsupportedExecution):
        workspace.materialize("network", replace(handle.spec, network_destinations=frozenset({"example.invalid"})))
    workspace.lose(handle.key)
    assert workspace.reconcile(handle.key).presence is Presence.UNKNOWN
    with pytest.raises(ExecutionConflict):
        workspace.materialize(handle.key, handle.spec)
