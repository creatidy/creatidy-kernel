# SPDX-License-Identifier: Apache-2.0
"""Bounded application step; adapters supply execution and independent observations."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol, cast

from creatidy_kernel.core.domain import (
    AttemptSpec,
    AttemptStatus,
    FinishAttempt,
    PrepareAttempt,
    Program,
    SatisfyWorkUnit,
    StartAttempt,
    WorkUnitStatus,
)
from creatidy_kernel.core.execution import (
    Activity,
    ArtifactManifest,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    OperationKey,
    Presence,
    RuntimeIdentity,
    WorkspaceHandle,
)
from creatidy_kernel.core.resources import ResourceRequest
from creatidy_kernel.core.verification import (
    AcceptedResult,
    CandidateResult,
    CheckProducer,
    VerificationPolicy,
    admit_accepted,
    verify_candidate,
)
from creatidy_kernel.ports.allocation import (
    decode_allocation,
    decode_identity,
    decode_runtime_receipt,
    encode_allocation,
    identity_matches,
    is_legacy_allocation,
    load_attempt_inputs,
)
from creatidy_kernel.ports.execution import Runtime
from creatidy_kernel.ports.program_store import ApplicationStore, OperationRecord
from creatidy_kernel.ports.resources import ResourceAllocator


def manifest_bytes(value: object) -> bytes:
    """Canonical JSON for public, non-secret application evidence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def terminal_outcome(store: ApplicationStore, attempt: AttemptSpec, operation: OperationRecord) -> str | None:
    data = store.find_artifact(operation.operation_id, "terminal-outcome")
    if data is None:
        return None
    raw: object = json.loads(data)
    if not isinstance(raw, dict):
        raise ValueError("invalid terminal outcome")
    value = cast("dict[str, object]", raw)
    disposition = value.get("candidate")
    if (
        type(disposition) is not str
        or disposition not in {"absent", "present", "not_requested", "unknown"}
        or value
        != {
            "version": 1,
            "attempt": attempt.digest,
            "handle": operation.accepted_reference,
            "fence": operation.fence,
            "candidate": disposition,
        }
    ):
        raise ValueError("terminal outcome differs from original Attempt")
    return disposition


@dataclass(frozen=True)
class Collection:
    manifest: ArtifactManifest
    repository: str
    base: str
    head: str
    checks: CheckProducer


class Collector(Protocol):
    def collect(self, request: ExecutionRequest, candidate: Candidate, *, now: int) -> Collection: ...


def advance_work_unit(
    store: ApplicationStore,
    program: Program,
    unit_id: str,
    *,
    allocator: ResourceAllocator,
    runtime: Runtime,
    workspace: WorkspaceHandle,
    collector: Collector,
    policy: VerificationPolicy,
    now: int,
    fault: Callable[[str], None] = lambda _: None,
    restore: Callable[[ExecutionRequest, str], None] | None = None,
    artifact_path: str = "result.txt",
    clock: Callable[[], int] | None = None,
    deadline: int | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> str:
    """Perform at most one dispatch/observation, never retry an uncertain effect blindly.

    The caller operates in trusted-development mode and supplies pinned workspace,
    verifier and runtime bindings. No worker-provided checks execute here.
    A supplied cancellation predicate journals the request before returning true;
    it is consumed at external-return boundaries before subsequent durable writes.
    """
    program = store.load(program.program_id)
    decision_clock = clock if clock is not None else lambda: now
    if program.state(unit_id).status is WorkUnitStatus.SATISFIED:
        return "accepted"
    attempt_id = f"{program.program_id}:{unit_id}"
    operation_id = f"runtime:{attempt_id}"
    if not any(item.spec.attempt_id == attempt_id for item in program.attempts):
        if cancel_requested is not None and cancel_requested():
            return "cancel_requested"
        if deadline is not None and decision_clock() >= deadline:
            return "expired"
        allocation = allocator.select(ResourceRequest(unit_id, frozenset({"reference"}), 128))
        if cancel_requested is not None and cancel_requested():
            return "cancel_requested"
        allocation_bytes = encode_allocation(allocation)
        allocation_ref = "sha256:" + hashlib.sha256(allocation_bytes).hexdigest()
        context = manifest_bytes([asdict(item) for item in program.resolved_inputs(unit_id)])
        context_ref = "sha256:" + hashlib.sha256(context).hexdigest()
        attempt = AttemptSpec(
            attempt_id,
            program.program_id,
            unit_id,
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs(unit_id),
            allocation_ref,
            "reference:v1",
            workspace.key,
            context_ref,
        )
        fault("selected")
        program = store.admit_with_intent(
            program.program_id,
            f"prepare:{attempt_id}",
            PrepareAttempt(program.revision, "worker", attempt),
            operation_id,
            operation_id,
            {
                "version": 1,
                "attempt": attempt.digest,
                "workspace": workspace.key,
                "allocation": allocation_bytes.decode(),
                "context": context.decode(),
            },
        )
        fault("prepared")
    attempt = program.attempt(attempt_id).spec
    if workspace.key != attempt.workspace_reference:
        raise ValueError("workspace differs from original Attempt")
    allocation_bytes, context = load_attempt_inputs(store, attempt)
    allocation = decode_allocation(allocation_bytes)
    legacy = is_legacy_allocation(allocation_bytes)
    allocation_ref, context_ref = attempt.allocation_reference, attempt.context_reference
    if allocation_ref is None or context_ref is None or attempt.agent_definition_reference is None:
        raise ValueError("Attempt lacks immutable execution references")
    store.finalize_artifact(operation_id, "allocation", allocation_bytes)
    fault("allocation")
    store.finalize_artifact(operation_id, "context", context)
    fault("artifacts")
    if program.attempt(attempt_id).status is AttemptStatus.PREPARED:
        # Prepare already atomically owns the immutable Operation. Starting changes
        # only Program state, never the original request or its selection.
        program = store.admit(
            program.program_id, f"start:{attempt_id}", StartAttempt(program.revision, "worker", attempt_id)
        )
        fault("started")
    operation = store.operation(operation_id)
    key = OperationKey(operation_id, operation.effect_key, operation.request_digest)
    fault("commit")
    receipt_identity: RuntimeIdentity | None = None
    receipt = store.find_artifact(operation_id, "runtime-receipt")
    if receipt is not None:
        receipt_handle, receipt_fence, receipt_identity = decode_runtime_receipt(receipt)
        if (
            receipt_fence != operation.fence
            or operation.accepted_reference not in (None, receipt_handle)
            or not identity_matches(
                receipt_identity, allocation, attempt.agent_definition_reference, require_resolved=False, legacy=legacy
            )
        ):
            raise ValueError("runtime receipt differs from original execution")
        if operation.accepted_reference is None:
            if operation.status not in {"dispatched", "unknown"} or operation.retry_proof is not None:
                raise ValueError("runtime receipt has no matching delivery")
            store.record_transport(operation_id, receipt_fence, True)
            operation = store.observe(
                operation_id, receipt_fence, f"accepted:{receipt_handle}", "accepted", reference=receipt_handle
            )
    candidate_status = terminal_outcome(store, attempt, operation)
    if candidate_status is not None and operation.status != "terminal":
        if operation.status != "accepted" or operation.accepted_reference is None:
            raise ValueError("terminal outcome has no matching accepted runtime")
        operation = store.observe(
            operation_id,
            operation.fence,
            f"terminal:{operation_id}",
            "terminal",
            reference=operation.accepted_reference,
        )
    if operation.status == "terminal":
        if program.attempt(attempt_id).status is AttemptStatus.EXECUTING:
            program = store.admit(
                program.program_id, f"finish:{attempt_id}", FinishAttempt(program.revision, "worker", attempt_id)
            )
        if candidate_status == "absent":
            return "terminal_no_candidate"
        if candidate_status == "not_requested":
            return "terminal_candidate_unknown"
    if operation.status in {"dispatched", "unknown"}:
        if operation.lease_until is not None and now < operation.lease_until:
            return "waiting"
        lookup = runtime.reconcile(key, operation.accepted_reference)
        if cancel_requested is not None:
            cancel_requested()
        reconciled_at = decision_clock()
        operation = store.reconcile(
            operation_id,
            operation.fence,
            lookup.presence.value,
            now=reconciled_at,
            evidence=f"runtime-lookup:{reconciled_at}:{lookup.presence.value}",
            reference=lookup.handle,
            matched_digest=key.request_digest if lookup.presence is Presence.FOUND else None,
            authoritative_absence=lookup.presence is Presence.ABSENT,
        )
        if lookup.presence is Presence.UNKNOWN:
            return "unknown"
    if operation.status == "intent" or operation.retry_proof is not None:
        if deadline is not None and decision_clock() >= deadline:
            return "expired"
        if cancel_requested is not None and cancel_requested():
            return "cancel_requested"
        fence = store.claim(operation_id, now=decision_clock(), lease_seconds=1)
        operation = store.operation(operation_id)
        request = ExecutionRequest(
            key,
            attempt,
            workspace,
            context_ref,
            allocation_ref,
            "owner-approved-reference",
            RuntimeIdentity(
                allocation.model_id,
                None,
                None,
                attempt.agent_definition_reference,
                requested_provider=allocation.provider_id,
                requested_effort=allocation.reasoning_effort,
            ),
            fence,
            allocation,
        )
        handle = runtime.start(request)
        if cancel_requested is not None:
            cancel_requested()
        fault("send")
        # Publish the native handle and configuration together before accepting
        # the receipt. A process crash cannot retain one while losing the other.
        started = runtime.observe(handle, now=decision_clock())
        if cancel_requested is not None:
            cancel_requested()
        if started.handle != handle or not identity_matches(
            started.identity, allocation, attempt.agent_definition_reference, require_resolved=False, legacy=legacy
        ):
            return "identity_unavailable"
        receipt_identity = started.identity
        store.finalize_artifact(
            operation_id,
            "runtime-receipt",
            manifest_bytes({"version": 1, "handle": handle, "fence": fence, "identity": asdict(receipt_identity)}),
        )
        fault("receipt-evidence")
        store.record_transport(operation_id, fence, True)
        operation = store.observe(operation_id, fence, f"accepted:{handle}", "accepted", reference=handle)
        fault("receipt")
    handle = operation.accepted_reference
    if handle is None:
        return "unknown"
    request = ExecutionRequest(
        key,
        attempt,
        workspace,
        context_ref,
        allocation_ref,
        "owner-approved-reference",
        RuntimeIdentity(
            allocation.model_id,
            None,
            None,
            attempt.agent_definition_reference,
            requested_provider=allocation.provider_id,
            requested_effort=allocation.reasoning_effort,
        ),
        operation.fence,
        allocation,
    )
    if restore is not None:
        restore(request, handle)
        if cancel_requested is not None:
            cancel_requested()
    observation = runtime.observe(handle, now=decision_clock())
    if cancel_requested is not None:
        cancel_requested()
    observation_data = manifest_bytes(asdict(observation))
    store.finalize_artifact(
        operation_id, f"observation:{hashlib.sha256(observation_data).hexdigest()}", observation_data
    )
    identity = observation.identity
    if observation.handle != handle or not identity_matches(
        identity,
        allocation,
        attempt.agent_definition_reference,
        require_resolved=False,
        legacy=legacy,
        recorded=receipt_identity,
    ):
        return "identity_unavailable"
    persisted_identity = store.find_artifact(operation_id, "identity")
    recorded: RuntimeIdentity | None
    if persisted_identity is None:
        original = (
            receipt_identity if receipt_identity is not None and receipt_identity.resolved is not None else identity
        )
        if not identity_matches(
            original, allocation, attempt.agent_definition_reference, require_resolved=True, legacy=legacy
        ):
            if observation.activity is not Activity.TERMINAL:
                return "identity_unavailable"
            # Exact-handle terminality does not prove model resolution or acceptance.
            recorded = None
        else:
            store.finalize_artifact(operation_id, "identity", manifest_bytes(asdict(original)))
            recorded = original
    else:
        # The immutable Operation owns the accepted handle and its original runtime
        # resolution. A restored adapter's unknown identity cannot erase that fact.
        try:
            recorded = decode_identity(persisted_identity)
        except ValueError:
            return "identity_unavailable"
        if not identity_matches(
            recorded, allocation, attempt.agent_definition_reference, require_resolved=True, legacy=legacy
        ) or not identity_matches(
            identity,
            allocation,
            attempt.agent_definition_reference,
            require_resolved=False,
            legacy=legacy,
            recorded=recorded,
        ):
            return "identity_unavailable"
    if receipt is None and receipt_identity is None and recorded is not None:
        # A proved exact-operation lookup can recover a lost reply. Publish only
        # independently validated original resolution, never requested identity.
        store.finalize_artifact(
            operation_id,
            "runtime-receipt",
            manifest_bytes({"version": 1, "handle": handle, "fence": operation.fence, "identity": asdict(recorded)}),
        )
    if observation.activity is not Activity.TERMINAL:
        return "terminal_candidate_unknown" if operation.status == "terminal" else observation.activity.value
    retrieval_unknown = False
    try:
        candidate = None if cancel_requested is not None and cancel_requested() else runtime.candidate(handle)
    except ExecutionConflict:
        candidate = None
        retrieval_unknown = True
    if cancel_requested is not None:
        cancel_requested()
    if program.attempt(attempt_id).status is AttemptStatus.EXECUTING:
        store.finalize_artifact(
            operation_id,
            "terminal-outcome",
            manifest_bytes(
                {
                    "version": 1,
                    "attempt": attempt.digest,
                    "handle": handle,
                    "fence": operation.fence,
                    "candidate": "not_requested"
                    if cancel_requested is not None and cancel_requested()
                    else "unknown"
                    if retrieval_unknown
                    else "present"
                    if candidate is not None
                    else "absent",
                }
            ),
        )
        store.observe(operation_id, operation.fence, f"terminal:{operation_id}", "terminal", reference=handle)
        program = store.admit(
            program.program_id, f"finish:{attempt_id}", FinishAttempt(program.revision, "worker", attempt_id)
        )
    if cancel_requested is not None and cancel_requested():
        return "cancel_requested"
    if retrieval_unknown:
        return "terminal_candidate_unknown"
    if candidate is None:
        return "terminal_no_candidate"
    if recorded is None:
        return "identity_unavailable"
    if deadline is not None and decision_clock() >= deadline:
        return "expired"
    collection = collector.collect(request, candidate, now=decision_clock())
    unit = program.spec.work_unit(unit_id)
    proposal = CandidateResult(
        f"candidate:{attempt_id}", candidate, tuple((name, artifact_path) for name in sorted(unit.outputs))
    )
    result = verify_candidate(
        program,
        proposal,
        collection.manifest,
        policy,
        collection.checks,
        repository=collection.repository,
        base_revision=collection.base,
        head_revision=collection.head,
        verifier_id="verifier",
        reference_id=f"accepted:{attempt_id}",
        now=now,
        clock=decision_clock,
    )
    data = manifest_bytes(asdict(result))
    digest = hashlib.sha256(data).hexdigest()
    reference = store.finalize_artifact(operation_id, f"verification:{digest}", data)
    if not isinstance(result, AcceptedResult):
        store.intent(
            f"rejection:{attempt_id}:{digest}",
            f"rejection:{attempt_id}:{digest}",
            {"manifest": reference, "reason": "verification failed; bounded run paused"},
        )
        return "rejected"
    if cancel_requested is not None and cancel_requested():
        return "cancel_requested"
    accepted_at = decision_clock()
    if deadline is not None and accepted_at >= deadline:
        return "expired"
    admit_accepted(
        program,
        result,
        collection.manifest,
        policy,
        repository=collection.repository,
        base_revision=collection.base,
        head_revision=collection.head,
        now=accepted_at,
    )
    store.admit_with_intent(
        program.program_id,
        f"accept:{attempt_id}",
        SatisfyWorkUnit(program.revision, "verifier", result.satisfaction),
        f"acceptance:{attempt_id}",
        f"acceptance:{attempt_id}",
        {
            "manifest": reference,
            "name": f"verification:{digest}",
            "operation": operation_id,
            "head": collection.head,
            "base": collection.base,
        },
    )
    return "accepted"
