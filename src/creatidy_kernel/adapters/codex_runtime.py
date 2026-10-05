# SPDX-License-Identifier: Apache-2.0
"""Codex app-server Runtime adapter; the operation journal remains the dispatch authority.

The caller must durably claim an Operation before start and persist the returned handle
before waiting. An uncertain start is never retried by this adapter. The controller must
not redeliver an uncertain operation after restart without independent reconciliation.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast

from creatidy_kernel.adapters.codex_stdio import CodexRPCError
from creatidy_kernel.core.execution import (
    Activity,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    Lookup,
    OperationKey,
    Presence,
    RuntimeIdentity,
    RuntimeObservation,
    TrustMode,
    UnsupportedExecution,
)
from creatidy_kernel.ports.execution import Runtime


class CodexConnection(Protocol):
    @property
    def version(self) -> str: ...

    @property
    def methods(self) -> frozenset[str]: ...

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]: ...


class CodexRejected(Exception):
    """Native app-server domain rejection, distinct from uncertain transport delivery."""


@dataclass(frozen=True, slots=True)
class CodexInputs:
    """Trusted controller-resolved inputs, not worker-provided reference strings."""

    workspace_key: str
    context_reference: str
    allocation_reference: str
    capability_reference: str
    cwd: str
    prompt: str
    model: str
    provider: str
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    reasoning_effort: str | None = None


@dataclass(slots=True)
class _Run:
    request: ExecutionRequest
    thread: str | None = None
    turn: str | None = None
    uncertain: bool = False
    cancelled: bool = False
    terminal: bool = False
    terminal_status: str | None = None
    identity_model: str | None = None
    identity_provider: str | None = None
    identity_effort: str | None = None
    candidate: Candidate | None = None

    @property
    def handle(self) -> str | None:
        if self.thread is None or self.turn is None:
            return None
        return f"codex:{self.thread}:{self.turn}"

    @property
    def identity(self) -> RuntimeIdentity:
        return replace(
            self.request.identity,
            resolved=self.identity_model,
            resolved_provider=self.identity_provider,
            resolved_effort=self.identity_effort,
            observed=None,
            observed_provider=None,
            observed_effort=None,
        )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CodexRejected("invalid app-server response")
    data = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in data):
        raise CodexRejected("invalid app-server response keys")
    return {str(key): item for key, item in data.items()}


def _field(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if type(item) is not str or not item:
        raise CodexRejected(f"missing app-server {key}")
    return item


class CodexRuntime(Runtime):
    """Single controller-side adapter, never a scheduler, sandbox, or authority broker."""

    def __init__(
        self,
        connection: CodexConnection,
        *,
        version: str,
        resolve: Callable[[ExecutionRequest], CodexInputs],
        authorize: Callable[[ExecutionRequest], bool],
        collect: Callable[[ExecutionRequest], Candidate | None],
        freshness: int = 30,
        supported_efforts: frozenset[tuple[str, str, str]] = frozenset(),
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not version or connection.version != version or freshness <= 0:
            raise UnsupportedExecution("Codex version mismatch or invalid freshness contract")
        required = {"thread/start", "turn/start", "thread/read", "turn/interrupt"}
        if not required <= connection.methods:
            raise UnsupportedExecution("Codex version lacks required native methods")
        if type(supported_efforts) is not frozenset or any(
            type(binding) is not tuple
            or len(binding) != 3
            or any(type(value) is not str or not value.strip() for value in binding)
            for binding in supported_efforts
        ):
            raise ValueError("supported efforts must be immutable provider/model/effort bindings")
        self.connection = connection
        self.resolve = resolve
        self.authorize = authorize
        self.collect = collect
        self.freshness = freshness
        self.clock = clock
        # Trusted support evidence for this pinned connection version, never a
        # vocabulary inferred from an allocator's requested configuration.
        self.supported_efforts = supported_efforts
        self._by_key: dict[str, _Run] = {}
        self._by_handle: dict[str, _Run] = {}

    def _inputs(self, request: ExecutionRequest) -> CodexInputs:
        inputs = self.resolve(request)
        if (
            inputs.workspace_key != request.workspace.key
            or inputs.context_reference != request.context_reference
            or inputs.allocation_reference != request.allocation_reference
            or inputs.capability_reference != request.capability_reference
            or inputs.model != request.identity.requested
            or not all((inputs.cwd, inputs.prompt, inputs.model, inputs.provider))
            or not Path(inputs.cwd).is_absolute()
        ):
            raise ExecutionConflict("resolved Codex inputs differ from immutable Attempt")
        if request.workspace.spec.trust_mode is not TrustMode.TRUSTED_DEVELOPMENT:
            raise UnsupportedExecution("Codex settings do not attest hostile-worker isolation")
        if request.workspace.spec.network_destinations or request.workspace.spec.mounts:
            raise UnsupportedExecution("Codex cannot enforce workspace network or mount grants")
        if inputs.sandbox != "workspace-write" or inputs.approval_policy != "never":
            raise UnsupportedExecution("unsupported Codex permission configuration")
        if request.allocation is not None and (
            request.allocation.runtime_id != "codex"
            or request.allocation.provider_id != inputs.provider
            or request.allocation.model_id != inputs.model
            or request.allocation.reasoning_effort != inputs.reasoning_effort
        ):
            raise ExecutionConflict("configured Codex runtime/provider/model/effort differs from Allocation")
        if request.identity.requested_provider not in {None, inputs.provider} or (
            request.identity.requested_effort != inputs.reasoning_effort
        ):
            raise ExecutionConflict("configured Codex inputs differ from requested identity")
        if (
            inputs.reasoning_effort is not None
            and (inputs.provider, inputs.model, inputs.reasoning_effort) not in self.supported_efforts
        ):
            raise UnsupportedExecution("Codex provider/model reasoning effort has no trusted support binding")
        if any(
            value is not None
            for value in (
                request.identity.observed,
                request.identity.observed_provider,
                request.identity.observed_effort,
            )
        ):
            raise UnsupportedExecution("app-server does not attest generated-turn identity")
        return inputs

    def start(self, request: ExecutionRequest) -> str:
        key = request.operation.effect_key
        previous = self._by_key.get(key)
        if previous is not None:
            if previous.request != request:
                raise ExecutionConflict("duplicate effect key has different immutable inputs")
            if previous.handle is None or previous.uncertain:
                raise ExecutionConflict("uncertain Codex start cannot be redelivered")
            return previous.handle
        inputs = self._inputs(request)
        if not self.authorize(request):
            raise ExecutionConflict("operation lacks a durable delivery claim")
        run = _Run(request, uncertain=True)
        self._by_key[key] = run
        try:
            thread_params: dict[str, object] = {
                "model": inputs.model,
                "modelProvider": inputs.provider,
                "cwd": inputs.cwd,
                "sandbox": inputs.sandbox,
                "approvalPolicy": inputs.approval_policy,
            }
            if inputs.reasoning_effort is not None:
                thread_params["config"] = {"model_reasoning_effort": inputs.reasoning_effort}
            response = self.connection.request("thread/start", thread_params)
            run.thread = _field(_object(response.get("thread")), "id")
            resolved = _field(response, "model")
            provider = _field(response, "modelProvider")
            if resolved != inputs.model or provider != inputs.provider:
                raise CodexRejected("resolved model/provider does not match requested allocation")
            effort = response.get("reasoningEffort")
            if effort is not None and (type(effort) is not str or not effort.strip()):
                raise CodexRejected("invalid app-server reasoningEffort")
            if inputs.reasoning_effort is not None and effort != inputs.reasoning_effort:
                raise CodexRejected("resolved reasoning effort does not match requested allocation")
            run.identity_model = resolved
            run.identity_provider = provider
            run.identity_effort = effort
            turn_params: dict[str, object] = {
                "threadId": run.thread,
                "input": [{"type": "text", "text": inputs.prompt}],
            }
            if inputs.reasoning_effort is not None:
                turn_params["effort"] = inputs.reasoning_effort
            if not self.authorize(request):
                raise ExecutionConflict("operation authority expired before turn dispatch")
            turn = self.connection.request("turn/start", turn_params)
            run.turn = _field(_object(turn.get("turn")), "id")
        except (OSError, TimeoutError, CodexRejected):
            # A domain error might be definitive for one RPC, but thread creation or a
            # prior turn may already have happened; never issue another start here.
            raise
        run.uncertain = False
        handle = run.handle
        if handle is None:
            raise ExecutionConflict("Codex receipt lacks a thread or turn")
        self._by_handle[handle] = run
        return handle

    def restore(self, request: ExecutionRequest, handle: str) -> Lookup:
        """Bind an externally durable, accepted receipt before inspecting its native turn."""
        self._inputs(request)
        prefix = "codex:"
        parts = handle.removeprefix(prefix).split(":") if handle.startswith(prefix) else []
        if len(parts) != 2 or not all(parts):
            raise ExecutionConflict("invalid durable Codex receipt handle")
        existing = self._by_key.get(request.operation.effect_key)
        if existing is not None and (existing.request != request or existing.handle != handle):
            raise ExecutionConflict("Codex receipt conflicts with immutable Operation")
        if existing is None:
            run = _Run(request, thread=parts[0], turn=parts[1])
            self._by_key[request.operation.effect_key] = run
            self._by_handle[handle] = run
        return self.reconcile(request.operation, handle)

    def _read(self, run: _Run) -> tuple[str, str | None] | None:
        if run.thread is None or run.turn is None:
            return None
        try:
            response = self.connection.request("thread/read", {"threadId": run.thread, "includeTurns": True})
            thread = _object(response.get("thread"))
            if _field(thread, "id") != run.thread:
                return None
            turns = thread.get("turns")
            if not isinstance(turns, list):
                return None
            for entry in cast(list[object], turns):
                turn = _object(entry)
                if turn.get("id") == run.turn:
                    return _field(turn, "status"), run.identity_model
        except (OSError, TimeoutError, CodexRejected, CodexRPCError):
            return None
        return None

    def observe(self, handle: str, *, now: int) -> RuntimeObservation:
        run = self._by_handle.get(handle)
        if run is None:
            raise ExecutionConflict("unknown Codex receipt handle")
        state = self._read(run)
        observed_at = self.clock() if self.clock is not None else now
        activity = Activity.UNKNOWN
        if state is not None:
            status, _ = state
            if status in {"completed", "failed", "interrupted"}:
                if run.terminal_status is not None and run.terminal_status != status:
                    raise ExecutionConflict("Codex terminal replay changed status")
                run.terminal = True
                run.terminal_status = status
                activity = Activity.TERMINAL
            elif status == "inProgress":
                activity = Activity.TERMINAL if run.terminal else Activity.RUNNING
        return RuntimeObservation(
            handle,
            activity,
            observed_at,
            observed_at + self.freshness if state else observed_at,
            run.identity,
            run.cancelled,
        )

    def candidate(self, handle: str) -> Candidate | None:
        run = self._by_handle.get(handle)
        if run is None:
            raise ExecutionConflict("unknown Codex receipt handle")
        state = self._read(run)
        if state is None or state[0] != "completed" or run.terminal_status not in {None, "completed"}:
            return None
        run.terminal = True
        run.terminal_status = "completed"
        candidate = self.collect(run.request)
        if candidate is not None and (
            candidate.attempt_id != run.request.attempt.attempt_id
            or candidate.spec_digest != run.request.attempt.digest
            or candidate.artifacts.workspace_key != run.request.workspace.key
        ):
            raise ExecutionConflict("Codex candidate does not bind the immutable Attempt")
        if run.candidate is not None and candidate != run.candidate:
            raise ExecutionConflict("Codex terminal replay changes candidate")
        if candidate is not None:
            run.candidate = candidate
        return candidate

    def cancel(self, handle: str) -> RuntimeObservation:
        run = self._by_handle.get(handle)
        if run is None or run.thread is None or run.turn is None:
            raise ExecutionConflict("unknown Codex receipt handle")
        run.cancelled = True
        if not run.terminal:
            try:
                self.connection.request("turn/interrupt", {"threadId": run.thread, "turnId": run.turn})
            except (OSError, TimeoutError, CodexRejected, CodexRPCError):
                pass  # Acknowledgment is not proof that cancellation completed.
        return RuntimeObservation(
            handle,
            Activity.TERMINAL if run.terminal else Activity.UNKNOWN,
            0,
            0,
            run.identity,
            True,
        )

    def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup:
        run = self._by_key.get(operation.effect_key)
        if run is None:
            return Lookup(Presence.UNKNOWN)  # Codex has no Operation-key absence proof.
        if run.request.operation != operation:
            raise ExecutionConflict("reconciliation key differs from original Operation")
        if run.handle is None or run.uncertain or (handle is not None and handle != run.handle):
            return Lookup(Presence.UNKNOWN)
        return Lookup(Presence.FOUND, run.handle) if self._read(run) is not None else Lookup(Presence.UNKNOWN)
