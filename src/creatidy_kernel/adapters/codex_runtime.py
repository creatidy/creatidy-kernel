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
    RuntimeObservation,
    TrustMode,
    UnsupportedExecution,
)
from creatidy_kernel.ports.execution import Runtime


class CodexConnection(Protocol):
    version: str
    methods: frozenset[str]

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
    candidate: Candidate | None = None

    @property
    def handle(self) -> str | None:
        if self.thread is None or self.turn is None:
            return None
        return f"codex:{self.thread}:{self.turn}"


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
    ) -> None:
        if not version or connection.version != version or freshness <= 0:
            raise UnsupportedExecution("Codex version mismatch or invalid freshness contract")
        required = {"thread/start", "turn/start", "thread/read", "turn/interrupt"}
        if not required <= connection.methods:
            raise UnsupportedExecution("Codex version lacks required native methods")
        self.connection = connection
        self.resolve = resolve
        self.authorize = authorize
        self.collect = collect
        self.freshness = freshness
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
        if request.identity.observed is not None:
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
            response = self.connection.request(
                "thread/start",
                {
                    "model": inputs.model,
                    "modelProvider": inputs.provider,
                    "cwd": inputs.cwd,
                    "sandbox": inputs.sandbox,
                    "approvalPolicy": inputs.approval_policy,
                },
            )
            run.thread = _field(_object(response.get("thread")), "id")
            resolved = _field(response, "model")
            provider = _field(response, "modelProvider")
            if resolved != inputs.model or provider != inputs.provider:
                raise CodexRejected("resolved model/provider does not match requested allocation")
            run.identity_model = resolved
            turn = self.connection.request(
                "turn/start", {"threadId": run.thread, "input": [{"type": "text", "text": inputs.prompt}]}
            )
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
        identity = replace(run.request.identity, resolved=run.identity_model, observed=None)
        return RuntimeObservation(
            handle, activity, now, now + self.freshness if state else now, identity, run.cancelled
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
            replace(run.request.identity, resolved=run.identity_model, observed=None),
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
