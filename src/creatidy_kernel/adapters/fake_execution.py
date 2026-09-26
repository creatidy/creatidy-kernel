# SPDX-License-Identifier: Apache-2.0
"""Deterministic in-memory execution seams; never a hostile-code sandbox."""

from dataclasses import dataclass, replace

from creatidy_kernel.core.execution import (
    Activity,
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    Lookup,
    OperationKey,
    Presence,
    RuntimeObservation,
    TrustMode,
    UnsupportedExecution,
    WorkerScope,
    WorkspaceHandle,
    WorkspaceSpec,
    validate_relative_path,
)
from creatidy_kernel.ports.execution import Runtime, Workspace


class FakeWorkspace(Workspace):
    """Policy simulation only: no paths, network, mounts or processes are created."""

    def __init__(self, *, network: frozenset[str] = frozenset(), mounts: frozenset[str] = frozenset()) -> None:
        self.network = network
        self.mounts = mounts
        self._handles: dict[str, WorkspaceHandle] = {}
        self._manifests: dict[str, ArtifactManifest] = {}
        self._lost: set[str] = set()
        self._partial_launch = False

    def fail_next_materialize(self) -> None:
        """Simulate a launch whose workspace outcome cannot be proven absent."""
        self._partial_launch = True

    def materialize(self, key: str, spec: WorkspaceSpec) -> WorkspaceHandle:
        if spec.trust_mode is not TrustMode.TRUSTED_DEVELOPMENT:
            raise UnsupportedExecution("fake cannot attest isolated hostile-worker execution")
        if not spec.network_destinations <= self.network or not spec.mounts <= self.mounts:
            raise UnsupportedExecution("workspace network or mount policy cannot be enforced")
        existing = self._handles.get(key)
        if existing is not None:
            if existing.spec != spec:
                raise ExecutionConflict("occupied workspace handle has different inputs")
            return existing
        if key in self._lost:
            raise ExecutionConflict("lost workspace cannot be replaced without reconciliation")
        if self._partial_launch:
            self._partial_launch = False
            self._lost.add(key)
            raise ExecutionConflict("partial workspace launch has an unknown outcome")
        handle = WorkspaceHandle(key, spec)
        self._handles[key] = handle
        self._manifests[key] = ArtifactManifest(key, ())
        return handle

    def _require(self, handle: WorkspaceHandle) -> None:
        if self._handles.get(handle.key) != handle:
            raise ExecutionConflict("unknown or mismatched workspace handle")

    def access(
        self,
        handle: WorkspaceHandle,
        scope: WorkerScope,
        *,
        now: int,
        operation: str,
        path: str | None = None,
        symlink_target: str | None = None,
        network: str | None = None,
        mount: str | None = None,
    ) -> None:
        """Synthetic policy check, not OS enforcement or a worker-facing filesystem API."""
        self._require(handle)
        if scope.workspace_key != handle.key or now >= scope.expires_at or operation not in scope.operations:
            raise ExecutionConflict("workspace authority unavailable")
        if operation in {"grant", "accept", "gate", "control", "credentials"}:
            raise ExecutionConflict("control-plane actions are never worker operations")
        if path is not None:
            validate_relative_path(path)
            if not any(path == root or path.startswith(root + "/") for root in scope.paths):
                raise ExecutionConflict("path outside worker scope")
        if symlink_target is not None:
            validate_relative_path(symlink_target)
            if not any(symlink_target == root or symlink_target.startswith(root + "/") for root in scope.paths):
                raise ExecutionConflict("symlink target outside worker scope")
        if network is not None and (
            network not in scope.network_destinations or network not in handle.spec.network_destinations
        ):
            raise ExecutionConflict("network destination forbidden")
        if mount is not None and (mount not in scope.mounts or mount not in handle.spec.mounts):
            raise ExecutionConflict("mount forbidden")

    def publish_artifact(
        self, handle: WorkspaceHandle, scope: WorkerScope, artifact: Artifact, *, now: int
    ) -> ArtifactManifest:
        self.access(handle, scope, now=now, operation="write", path=artifact.path)
        previous = self._manifests[handle.key]
        if any(item.path == artifact.path and item != artifact for item in previous.artifacts):
            raise ExecutionConflict("artifact subject is immutable")
        if artifact in previous.artifacts:
            return previous
        manifest = ArtifactManifest(handle.key, (*previous.artifacts, artifact))
        self._manifests[handle.key] = manifest
        return manifest

    def collect(self, handle: WorkspaceHandle) -> ArtifactManifest:
        self._require(handle)
        return self._manifests[handle.key]

    def lose(self, key: str) -> None:
        if key in self._handles:
            self._lost.add(key)
            del self._handles[key]

    def reconcile(self, key: str, handle: str | None = None) -> Lookup:
        if key in self._lost:
            return Lookup(Presence.UNKNOWN)
        found = self._handles.get(key)
        if found is not None:
            return (
                Lookup(Presence.FOUND, found.key) if handle is None or handle == found.key else Lookup(Presence.UNKNOWN)
            )
        return Lookup(Presence.ABSENT)

    def cleanup(self, handle: WorkspaceHandle, *, worker_settled: bool) -> None:
        self._require(handle)
        if not worker_settled:
            raise ExecutionConflict("worker termination is uncertain")
        del self._handles[handle.key]


@dataclass(slots=True)
class _Execution:
    request: ExecutionRequest
    observation: RuntimeObservation
    candidate: Candidate | None = None


class FakeRuntime(Runtime):
    def __init__(self, *, recoverable: bool = True, identity_supported: bool = True) -> None:
        self.recoverable = recoverable
        self.identity_supported = identity_supported
        self._by_key: dict[str, _Execution] = {}
        self._by_handle: dict[str, _Execution] = {}
        self._lost: set[str] = set()

    def start(self, request: ExecutionRequest) -> str:
        key = request.operation.effect_key
        previous = self._by_key.get(key)
        if previous is not None:
            if previous.request != request:
                raise ExecutionConflict("duplicate effect key has different immutable inputs")
            return previous.observation.handle
        handle = f"fake:{key}"
        identity = request.identity
        if not self.identity_supported:
            identity = replace(identity, resolved=None, observed=None)
        observation = RuntimeObservation(handle, Activity.RUNNING, 0, 0, identity, False)
        execution = _Execution(request, observation)
        self._by_key[key] = execution
        self._by_handle[handle] = execution
        return handle

    def observe(self, handle: str, *, now: int) -> RuntimeObservation:
        execution = self._by_handle.get(handle)
        if execution is None:
            raise ExecutionConflict("execution handle cannot be observed")
        observation = execution.observation
        if observation.activity is not Activity.TERMINAL and now > observation.fresh_until:
            return replace(observation, activity=Activity.UNKNOWN)
        return observation

    def progress(self, handle: str, *, now: int, fresh_for: int, waiting: bool = False) -> RuntimeObservation:
        execution = self._by_handle[handle]
        if execution.observation.activity is Activity.TERMINAL or fresh_for < 0:
            raise ExecutionConflict("terminal activity cannot progress")
        activity = Activity.WAITING if waiting else Activity.RUNNING
        execution.observation = replace(
            execution.observation, activity=activity, observed_at=now, fresh_until=now + fresh_for
        )
        return execution.observation

    def publish(self, handle: str, scope: WorkerScope, candidate: Candidate, *, now: int) -> None:
        execution = self._by_handle[handle]
        request = execution.request
        if (
            scope.attempt_id != request.attempt.attempt_id
            or scope.workspace_key != request.workspace.key
            or scope.fence != request.fence
            or now >= scope.expires_at
            or "publish_candidate" not in scope.operations
        ):
            raise ExecutionConflict("candidate publication has stale or missing authority")
        if candidate.attempt_id != request.attempt.attempt_id or candidate.spec_digest != request.attempt.digest:
            raise ExecutionConflict("candidate does not bind the immutable Attempt")
        if candidate.artifacts.workspace_key != request.workspace.key:
            raise ExecutionConflict("candidate artifacts belong to another workspace")
        if execution.candidate is not None:
            if execution.candidate != candidate:
                raise ExecutionConflict("terminal replay changes candidate")
            return
        execution.candidate = candidate
        execution.observation = replace(
            execution.observation, activity=Activity.TERMINAL, observed_at=now, fresh_until=now
        )

    def candidate(self, handle: str) -> Candidate | None:
        return self._by_handle[handle].candidate

    def cancel(self, handle: str) -> RuntimeObservation:
        execution = self._by_handle[handle]
        execution.observation = replace(execution.observation, cancellation_requested=True)
        return execution.observation

    def lose(self, handle: str) -> None:
        execution = self._by_handle.pop(handle)
        self._lost.add(execution.request.operation.effect_key)

    def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup:
        if not self.recoverable:
            return Lookup(Presence.UNKNOWN)
        execution = self._by_key.get(operation.effect_key)
        if execution is None:
            return Lookup(Presence.ABSENT)
        if execution.request.operation != operation:
            raise ExecutionConflict("reconciliation key differs from original Operation")
        if operation.effect_key in self._lost:
            return Lookup(Presence.UNKNOWN)
        if handle is not None and handle != execution.observation.handle:
            return Lookup(Presence.UNKNOWN)
        return Lookup(Presence.FOUND, execution.observation.handle)
