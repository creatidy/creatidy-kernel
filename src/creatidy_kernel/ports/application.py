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
    DomainAction,
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
    Evidence,
    EvidenceSubject,
    ProductReviewRequest,
    ProductReviewVerdict,
    VerificationPolicy,
    admit_accepted,
    verify_candidate,
)
from creatidy_kernel.core.work_budget import BudgetRefused, WorkKind
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
from creatidy_kernel.ports.verification import (
    DurableProductReviewChecks,
    ProductReviewDispatcher,
    ProductReviewStatus,
    product_review_operation_id,
)


def manifest_bytes(value: object) -> bytes:
    """Canonical JSON for public, non-secret application evidence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _remediation_context(store: ApplicationStore, source_attempt_id: str) -> tuple[str | None, list[dict[str, str]]]:
    operation_id = f"rejection:{source_attempt_id}"
    operation = store.operation(operation_id)
    envelope_value: object = json.loads(operation.request_json)
    if type(envelope_value) is not dict:
        raise ValueError("invalid persisted candidate rejection intent")
    envelope = cast(dict[str, object], envelope_value)
    if set(envelope) != {"version", "request"} or envelope["version"] != 1:
        raise ValueError("invalid persisted candidate rejection intent")
    request_value = envelope["request"]
    if type(request_value) is not dict:
        raise ValueError("invalid persisted candidate rejection request")
    request = cast(dict[str, object], request_value)
    if set(request) != {"manifest", "product_review_status", "reason", "review_operation_id"}:
        raise ValueError("invalid persisted candidate rejection request")
    result_value: object = json.loads(store.artifact(operation_id, "rejected-result"))
    if type(result_value) is not dict:
        raise ValueError("invalid persisted rejected candidate result")
    result = cast(dict[str, object], result_value)
    if type(result.get("findings")) is not list or type(result.get("subject")) is not dict:
        raise ValueError("invalid persisted rejected candidate result")
    findings: object = result["findings"]
    review_operation_id = request["review_operation_id"]
    review_status = request["product_review_status"]
    if review_operation_id is not None and type(review_operation_id) is not str:
        raise ValueError("invalid persisted product review reference")
    if review_status is not None and type(review_status) is not str:
        raise ValueError("invalid persisted product review status")
    if review_status is not None and review_status not in {item.value for item in ProductReviewStatus}:
        raise ValueError("unknown persisted product review status")
    if review_status in {
        ProductReviewStatus.COMMENT.value,
        ProductReviewStatus.INVALID.value,
        ProductReviewStatus.OWNER_DECISION_NEEDED.value,
    }:
        return review_operation_id, []
    if review_operation_id is not None:
        receipt_value: object = json.loads(store.artifact(review_operation_id, "review-receipt"))
        if type(receipt_value) is not dict:
            raise ValueError("product review receipt differs from rejected candidate subject")
        receipt = cast(dict[str, object], receipt_value)
        if receipt.get("subject") != result["subject"]:
            raise ValueError("product review receipt differs from rejected candidate subject")
        verdict = receipt.get("verdict")
        if verdict == ProductReviewVerdict.REQUEST_CHANGES.value:
            findings = receipt.get("findings")
    if type(findings) is not list:
        raise ValueError("persisted rejection findings are invalid")
    normalized: list[dict[str, str]] = []
    for item in cast(list[object], findings):
        if type(item) is not dict:
            raise ValueError("persisted finding is malformed")
        finding = cast(dict[str, object], item)
        if any(type(finding.get(name)) is not str for name in ("finding_id", "fingerprint", "reason")):
            raise ValueError("persisted finding is malformed")
        normalized.append(
            {
                "finding_id": cast(str, finding["finding_id"]),
                "fingerprint": cast(str, finding["fingerprint"]),
                "reason": cast(str, finding["reason"]),
            }
        )
    return review_operation_id, normalized


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


class BudgetedChecks:
    def __init__(
        self, store: ApplicationStore, checks: CheckProducer, program_id: str, attempt_id: str, clock: Callable[[], int]
    ) -> None:
        self.store = store
        self.checks = checks
        self.program_id = program_id
        self.attempt_id = attempt_id
        self.clock = clock

    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        self.store.check_work_authority(self.program_id)
        if name == "independent_review":
            if not isinstance(self.checks, DurableProductReviewChecks):
                raise BudgetRefused("independent review requires the durable budgeted dispatcher")
            return self.checks.check(name, subject)
        key = f"verification:{self.attempt_id}:{hashlib.sha256(manifest_bytes([name, asdict(subject)])).hexdigest()}"
        operation = self.store.budgeted_intent(
            self.program_id,
            self.attempt_id,
            WorkKind.VERIFICATION,
            key,
            key,
            {"subject": asdict(subject), "check": name},
        )
        cached = self.store.find_artifact(key, "result")
        if cached is not None:
            self._record_result(key, cached)
            raw: object = json.loads(cached)
            if raw is None:
                return None
            if type(raw) is not dict:
                raise ValueError("invalid durable verification result")
            value = cast(dict[str, object], raw)
            if value.get("subject") != asdict(subject) or value.get("check") != name:
                raise ValueError("verification result differs from original subject")
            return Evidence(
                cast(str, value["evidence_id"]),
                name,
                subject,
                cast(str, value["producer"]),
                cast(str, value["tool_version"]),
                cast(str, value["artifact_reference"]),
                cast(int, value["observed_at"]),
                cast(int, value["fresh_until"]),
                cast(bool, value["passed"]),
                cast(str | None, value["reviewer_session"]),
            )
        if operation.attempts:
            self.store.retry_work(key)
            # Rechecking trusted deterministic observations is not replay of an
            # uncertain external effect. Native command producers retain their
            # separate exact per-command reservations and crash receipts.
            return self._run(name, subject, key)
        self.store.claim(key, now=self.clock(), lease_seconds=1)
        return self._run(name, subject, key)

    def _run(self, name: str, subject: EvidenceSubject, key: str) -> Evidence | None:
        self.store.check_work_dispatch(key)
        result = self.checks.check(name, subject)
        data = manifest_bytes(None if result is None else asdict(result))
        self.store.finalize_artifact(key, "result", data)
        self._record_result(key, data)
        return result

    def _record_result(self, key: str, data: bytes) -> None:
        reference = f"sha256:{hashlib.sha256(data).hexdigest()}"
        operation = self.store.operation(key)
        if operation.status == "dispatched":
            self.store.observe(
                key, operation.fence, f"verification-result:{reference}", "accepted", reference=reference
            )
        self.store.record_work_settlement(key, reference, expected_fence=operation.fence)


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
    product_reviewer: ProductReviewDispatcher | None = None,
    remediation_of: str | None = None,
) -> str:
    """Perform at most one dispatch/observation, never retry an uncertain effect blindly.

    The caller operates in trusted-development mode and supplies pinned workspace,
    verifier and runtime bindings. No worker-provided checks execute here.
    A supplied cancellation predicate journals the request before returning true;
    it is consumed at external-return boundaries before subsequent durable writes.
    """
    program = store.load(program.program_id)
    decision_clock = clock if clock is not None else lambda: now
    if "independent_review" in policy.checks and not policy.reviewer_required:
        raise BudgetRefused("explicit independent_review requires the durable product review policy")
    if program.state(unit_id).status is WorkUnitStatus.SATISFIED:
        return "accepted"
    if policy.reviewer_required and product_reviewer is None:
        return "product_review_dispatcher_required"
    remediation_data: dict[str, object] | None = None
    if remediation_of is None:
        attempt_id = f"{program.program_id}:{unit_id}"
    else:
        if type(remediation_of) is not str or not remediation_of.strip():
            raise ValueError("remediation requires an exact source Attempt")
        attempt_id = (
            f"{program.program_id}:{unit_id}:remediation:{hashlib.sha256(remediation_of.encode()).hexdigest()[:16]}"
        )
        if not any(item.spec.attempt_id == attempt_id for item in program.attempts):
            unit_attempts = [item for item in program.attempts if item.spec.work_unit_id == unit_id]
            if not unit_attempts or unit_attempts[-1].attempt_id != remediation_of:
                return "remediation_source_not_latest"
            source = program.attempt(remediation_of)
            if source.status is not AttemptStatus.FINISHED or program.state(unit_id).status is not WorkUnitStatus.READY:
                return "remediation_source_not_rejected"
            authority = program.spec.authority
            if DomainAction.PREPARE_ATTEMPT not in authority.allowed_actions or "worker" not in {
                authority.owner_id,
                *authority.delegated_actor_ids,
            }:
                return "remediation_paused"
            attempt_limit = min(program.spec.budget.max_attempts, authority.max_attempts)
            if len(program.attempts) >= attempt_limit:
                return "remediation_paused"
            if store.find_artifact(f"rejection:{remediation_of}", "rejected-result") is None:
                return "remediation_paused"
            review_operation_id, findings = _remediation_context(store, remediation_of)
            if not findings:
                return "remediation_paused"
            history: list[frozenset[str]] = []
            for previous in unit_attempts:
                previous_data = store.find_artifact(f"rejection:{previous.attempt_id}", "rejected-result")
                if previous_data is None:
                    continue
                _, previous_findings = _remediation_context(store, previous.attempt_id)
                if previous_findings:
                    history.append(frozenset(item["fingerprint"] for item in previous_findings))
            current_fingerprints = frozenset(item["fingerprint"] for item in findings)
            if len(history) > 1 and (
                current_fingerprints == history[-2] or (len(history) > 2 and current_fingerprints in history[:-1])
            ):
                return "remediation_paused"
            remediation_data = {
                "findings": findings,
                "review_operation_id": review_operation_id,
                "source_attempt_id": remediation_of,
            }
        elif program.attempt(attempt_id).spec.remediation_of != remediation_of:
            raise ValueError("persisted remediation Attempt differs from its source reference")
    operation_id = f"runtime:{attempt_id}"
    if store.find_artifact(f"rejection:{attempt_id}", "rejected-result") is not None:
        return "rejected"
    if not any(item.spec.attempt_id == attempt_id for item in program.attempts):
        if any(item.name == "ordinary-declaration" for item in program.spec.initial_inputs):
            raise ValueError("ordinary dispatch is not supported by the reference composition")
        store.check_work_authority(program.program_id)
        if cancel_requested is not None and cancel_requested():
            return "cancel_requested"
        if deadline is not None and decision_clock() >= deadline:
            return "expired"
        allocation = allocator.select(ResourceRequest(unit_id, frozenset({"reference"}), 128))
        if cancel_requested is not None and cancel_requested():
            return "cancel_requested"
        allocation_bytes = encode_allocation(allocation)
        allocation_ref = "sha256:" + hashlib.sha256(allocation_bytes).hexdigest()
        resolved_inputs = [asdict(item) for item in program.resolved_inputs(unit_id)]
        context = manifest_bytes(resolved_inputs)
        if remediation_data is not None:
            context = manifest_bytes({"inputs": resolved_inputs, "remediation": remediation_data, "version": 1})
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
            remediation_of,
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
        store.check_work_authority(program.program_id)
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
        store.check_work_dispatch(operation_id)
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
            manifest_bytes(
                {
                    "version": 2,
                    "handle": handle,
                    "fence": fence,
                    "identity": asdict(receipt_identity),
                    "owned_group_reference": started.owned_group_reference,
                }
            ),
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
            manifest_bytes(
                {
                    "version": 2,
                    "handle": handle,
                    "fence": operation.fence,
                    "identity": asdict(recorded),
                    # The restored adapter cannot vouch for the previous process group.
                    "owned_group_reference": None,
                }
            ),
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
    store.check_work_authority(program.program_id)
    unit = program.spec.work_unit(unit_id)
    proposal = CandidateResult(
        f"candidate:{attempt_id}", candidate, tuple((name, artifact_path) for name in sorted(unit.outputs))
    )
    checks = collection.checks
    review_request: ProductReviewRequest | None = None
    review_checks: DurableProductReviewChecks | None = None
    if product_reviewer is not None and policy.reviewer_required:
        subject = EvidenceSubject(
            proposal.digest,
            program.attempt(attempt_id).spec.digest,
            program.spec.digest,
            policy.reference.digest,
            collection.repository,
            collection.base,
            collection.head,
        )
        review_request = ProductReviewRequest(
            proposal,
            subject,
            attempt_id,
            program.attempt(attempt_id).actor_id,
            policy.reference,
        )
        review_checks = DurableProductReviewChecks(
            store,
            collection.checks,
            lambda _: review_request,
            product_reviewer,
            clock=decision_clock,
        )
        checks = review_checks
    result = verify_candidate(
        program,
        proposal,
        collection.manifest,
        policy,
        BudgetedChecks(store, checks, program.program_id, attempt_id, decision_clock),
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
    if cancel_requested is not None and cancel_requested():
        return "cancel_requested"
    if deadline is not None and decision_clock() >= deadline:
        return "expired"
    store.check_work_authority(program.program_id)
    if not isinstance(result, AcceptedResult):
        review_result = review_checks.last_result if review_checks is not None else None
        if review_result is not None and review_result.status is ProductReviewStatus.UNKNOWN:
            return "review_unknown"
        rejection_id = f"rejection:{attempt_id}"
        store.intent(
            rejection_id,
            rejection_id,
            {
                "manifest": reference,
                "product_review_status": review_result.status.value if review_result is not None else None,
                "reason": "verification failed; bounded run paused",
                "review_operation_id": (
                    product_review_operation_id(review_request)
                    if review_request is not None and review_result is not None
                    else None
                ),
            },
        )
        store.finalize_artifact(rejection_id, "rejected-result", data)
        store.record_work_completion(operation_id, reference)
        if review_result is not None and review_result.status in {
            ProductReviewStatus.OWNER_DECISION_NEEDED,
            ProductReviewStatus.COMMENT,
            ProductReviewStatus.INVALID,
        }:
            return review_result.status.value
        return "rejected"
    if cancel_requested is not None and cancel_requested():
        return "cancel_requested"
    accepted_at = decision_clock()
    store.check_work_authority(program.program_id)
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
    store.record_work_completion(operation_id, reference)
    return "accepted"
