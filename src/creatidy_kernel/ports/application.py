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
from creatidy_kernel.ports.execution import Runtime
from creatidy_kernel.ports.program_store import ApplicationStore
from creatidy_kernel.ports.resources import ResourceAllocator


def manifest_bytes(value: object) -> bytes:
    """Canonical JSON for public, non-secret application evidence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


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
) -> str:
    """Perform at most one dispatch/observation, never retry an uncertain effect blindly.

    The caller operates in trusted-development mode and supplies pinned workspace,
    verifier and runtime bindings. No worker-provided checks execute here.
    """
    if program.state(unit_id).status is WorkUnitStatus.SATISFIED:
        return "accepted"
    attempt_id = f"{program.program_id}:{unit_id}"
    operation_id = f"runtime:{attempt_id}"
    allocation = allocator.select(ResourceRequest(unit_id, frozenset({"reference"}), 128))
    allocation_data = asdict(allocation)
    allocation_data["capabilities"] = sorted(allocation.capabilities)
    allocation_bytes = manifest_bytes(allocation_data)
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
    if not any(item.spec.attempt_id == attempt_id for item in program.attempts):
        program = store.admit(
            program.program_id, f"prepare:{attempt_id}", PrepareAttempt(program.revision, "worker", attempt)
        )
    else:
        attempt = program.attempt(attempt_id).spec
    if program.attempt(attempt_id).status is AttemptStatus.PREPARED:
        program = store.admit_with_intent(
            program.program_id,
            f"start:{attempt_id}",
            StartAttempt(program.revision, "worker", attempt_id),
            operation_id,
            operation_id,
            {"attempt": attempt.digest, "workspace": workspace.key},
        )
    operation = store.operation(operation_id)
    store.finalize_artifact(operation_id, "allocation", allocation_bytes)
    store.finalize_artifact(operation_id, "context", context)
    key = OperationKey(operation_id, operation.effect_key, operation.request_digest)
    fault("commit")
    if operation.status in {"dispatched", "unknown"}:
        if operation.lease_until is not None and now < operation.lease_until:
            return "waiting"
        lookup = runtime.reconcile(key, operation.accepted_reference)
        operation = store.reconcile(
            operation_id,
            operation.fence,
            lookup.presence.value,
            now=now,
            evidence=f"runtime-lookup:{now}:{lookup.presence.value}",
            reference=lookup.handle,
            matched_digest=key.request_digest if lookup.presence is Presence.FOUND else None,
            authoritative_absence=lookup.presence is Presence.ABSENT,
        )
        if lookup.presence is Presence.UNKNOWN:
            return "unknown"
    if operation.status == "intent" or operation.retry_proof is not None:
        fence = store.claim(operation_id, now=now, lease_seconds=1)
        operation = store.operation(operation_id)
        request = ExecutionRequest(
            key,
            attempt,
            workspace,
            context_ref,
            allocation_ref,
            "owner-approved-reference",
            RuntimeIdentity(allocation.model_id, None, None, "reference:v1"),
            fence,
        )
        handle = runtime.start(request)
        fault("send")
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
        RuntimeIdentity(allocation.model_id, None, None, "reference:v1"),
        operation.fence,
    )
    if restore is not None:
        restore(request, handle)
    observation = runtime.observe(handle, now=now)
    observation_data = manifest_bytes(asdict(observation))
    store.finalize_artifact(
        operation_id, f"observation:{hashlib.sha256(observation_data).hexdigest()}", observation_data
    )
    identity = observation.identity
    if (
        observation.handle != handle
        or identity.requested != allocation.model_id
        or identity.agent_definition_version != attempt.agent_definition_reference
        or identity.resolved not in (None, allocation.model_id)
        or identity.observed not in (None, allocation.model_id)
    ):
        return "identity_unavailable"
    persisted_identity = store.find_artifact(operation_id, "identity")
    if persisted_identity is None:
        if identity.resolved is None:
            return "identity_unavailable"
        store.finalize_artifact(operation_id, "identity", manifest_bytes(asdict(identity)))
    else:
        # The immutable Operation owns the accepted handle and its original runtime
        # resolution. A restored adapter's unknown identity cannot erase that fact.
        raw: object = json.loads(persisted_identity)
        if not isinstance(raw, dict):
            return "identity_unavailable"
        recorded = cast(dict[str, object], raw)
        if (
            set(recorded) != {"requested", "resolved", "observed", "agent_definition_version"}
            or recorded["requested"] != allocation.model_id
            or recorded["resolved"] != allocation.model_id
            or recorded["observed"] not in (None, allocation.model_id)
            or recorded["agent_definition_version"] != attempt.agent_definition_reference
        ):
            return "identity_unavailable"
    if observation.activity is not Activity.TERMINAL:
        return observation.activity.value
    candidate = runtime.candidate(handle)
    if candidate is None:
        return "unknown"
    collection = collector.collect(request, candidate, now=now)
    if program.attempt(attempt_id).status is AttemptStatus.EXECUTING:
        store.observe(operation_id, operation.fence, f"terminal:{operation_id}", "terminal", reference=handle)
        program = store.admit(
            program.program_id, f"finish:{attempt_id}", FinishAttempt(program.revision, "worker", attempt_id)
        )
    unit = program.spec.work_unit(unit_id)
    proposal = CandidateResult(
        f"candidate:{attempt_id}", candidate, tuple((name, "result.txt") for name in sorted(unit.outputs))
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
    admit_accepted(
        program,
        result,
        collection.manifest,
        policy,
        repository=collection.repository,
        base_revision=collection.base,
        head_revision=collection.head,
        now=now,
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
