# SPDX-License-Identifier: Apache-2.0
"""Owner-local, metadata-only projection of durable Program outcomes.

References are keyed pseudonyms scoped to one export, not anonymization or
permission to share. Source content and free-form text never enter the result.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext
from typing import Protocol, cast

from creatidy_kernel.core.domain import Program
from creatidy_kernel.core.execution import RuntimeIdentity
from creatidy_kernel.core.outcomes import Provenance
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.allocation import decode_identity, decode_runtime_receipt_with_group, load_allocation
from creatidy_kernel.ports.program_store import ApplicationStore, OperationRecord
from creatidy_kernel.ports.verification import decode_product_review_receipt

PRIVATE_METADATA = "private-metadata-v1"
_METRICS = frozenset({"money", "cost", "tokens", "compute", "quota", "attention", "elapsed"})
_UNITS = frozenset(
    {"USD", "EUR", "GBP", "input_tokens", "output_tokens", "tokens", "cpu_seconds", "seconds", "minutes"}
)
_PROVENANCE = frozenset(item.value for item in Provenance)


class OutcomeExportStore(ApplicationStore, Protocol):
    """Existing owner-thread facts consumed without dispatch or renewal."""

    def work_budget_revoked(self, program_id: str) -> bool | None: ...
    def find_operation(self, operation_id: str) -> OperationRecord | None: ...
    def work_budget_status(self, program_id: str) -> dict[str, object]: ...
    def served_claim_count(self, program_id: str) -> int: ...
    def served_claims(self, program_id: str, *, offset: int = 0, limit: int = 8) -> tuple[dict[str, object], ...]: ...
    def served_change_token(self) -> tuple[int, int]: ...
    def outcome_effect_evidence(self, program_id: str, operation_id: str) -> dict[str, object]: ...
    def outcome_claim_evidence(self, program_id: str, command_key: str) -> dict[str, object]: ...


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("invalid durable outcome record")
    return cast(dict[str, object], value)


def _items(value: object) -> list[dict[str, object]]:
    if type(value) is not list:
        raise ValueError("invalid durable outcome collection")
    return [_mapping(item) for item in cast(list[object], value)]


def _quantity(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("invalid durable usage quantity")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid durable usage quantity") from error
    if not number.is_finite() or number < 0:
        raise ValueError("invalid durable usage quantity")
    return value


def export_private_outcome(
    store: OutcomeExportStore,
    program_id: str,
    *,
    owner_policy: str,
    pseudonym_key: bytes | None = None,
) -> dict[str, object]:
    """Export a consistent local Program cut, refusing unsupported policies.

    The optional key is owner-controlled and must remain private; if omitted,
    a fresh ephemeral key makes references linkable only inside this result.
    No source text, raw identifiers, credential, prompt, URL or workspace path
    is copied. Unknown producer facts remain null, never inferred from status.
    """
    if owner_policy != PRIVATE_METADATA:
        raise ValueError("unsupported owner export policy")
    key = secrets.token_bytes(32) if pseudonym_key is None else pseudonym_key
    if type(key) is not bytes or len(key) != 32:
        raise ValueError("pseudonym key must contain exactly 32 bytes")

    def ref(kind: str, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value:
            raise ValueError("invalid durable reference")
        raw = json.dumps([kind, value], ensure_ascii=True, separators=(",", ":")).encode()
        return "hmac-sha256:" + hmac.new(key, raw, hashlib.sha256).hexdigest()

    token = store.served_change_token()
    program: Program = store.load(program_id)
    configured = store.work_budget_revoked(program_id) is not None
    budget = store.work_budget_status(program_id) if configured else {}
    binding = _mapping(budget["binding"]) if configured else None
    budget_policy = WorkBudgetPolicy.decode(binding["policy"]) if binding is not None else None
    reservations = _items(budget.get("reservations", []))
    history = _items(budget.get("usage_history", []))
    effective = _items(budget.get("effective_usage", []))
    attempt_by_id = {item.attempt_id: item for item in program.attempts}
    grouped: dict[tuple[str, str, str, str], Decimal | None] = {}
    for item in effective:
        source, metric, unit, provenance = (
            item.get("source"),
            item.get("metric"),
            item.get("unit"),
            item.get("provenance"),
        )
        if (
            type(source) is not str
            or not source
            or type(metric) is not str
            or type(unit) is not str
            or type(provenance) is not str
            or provenance not in _PROVENANCE
        ):
            raise ValueError("unsupported durable usage total")
        quantity = _quantity(item.get("quantity"))
        group = (source, metric, unit, provenance)
        if group not in grouped:
            grouped[group] = None
        if quantity is not None:
            value = Decimal(quantity)
            previous = grouped[group]
            if previous is None:
                grouped[group] = value
            else:
                previous_exponent = previous.as_tuple().exponent
                value_exponent = value.as_tuple().exponent
                if type(previous_exponent) is not int or type(value_exponent) is not int:
                    raise ValueError("invalid finite usage quantity")
                precision = max(previous.adjusted(), value.adjusted()) - min(previous_exponent, value_exponent) + 2
                if precision > 10000:
                    raise ValueError("usage subtotal exceeds private export precision bound")
                with localcontext() as context:
                    context.prec = max(precision, 1)
                    grouped[group] = previous + value
    effective_ids = {(item["operation"], item["source"], item["observation_id"]) for item in effective}

    observations: list[dict[str, object]] = []
    for item in history:
        metric, unit, provenance = item.get("metric"), item.get("unit"), item.get("provenance")
        if type(metric) is not str or type(unit) is not str or provenance not in _PROVENANCE:
            raise ValueError("unsupported durable usage record")
        source = item.get("source")
        if type(source) is not str or not source:
            raise ValueError("invalid durable usage source")

        def observation_ref(value: object, source_id: str = source) -> str | None:
            if value is None:
                return None
            if type(value) is not str or not value:
                raise ValueError("invalid durable observation reference")
            return ref("observation", json.dumps([source_id, value], ensure_ascii=True, separators=(",", ":")))

        receipt = item.get("receipt_id")
        if type(receipt) is not str or not receipt:
            raise ValueError("invalid durable usage receipt")
        receipt_identity = json.dumps([source, receipt, metric, unit], ensure_ascii=True, separators=(",", ":"))
        observations.append(
            {
                "observation": observation_ref(item.get("observation_id")),
                "attempt": ref("attempt", item.get("attempt")),
                "attempt_binding": "recorded" if item.get("attempt") in attempt_by_id else "unknown",
                "operation": ref("operation", item.get("operation")),
                "receipt": ref("usage-receipt", receipt_identity),
                "source": ref("source", source),
                "metric": metric if metric in _METRICS else None,
                "metric_reference": ref("metric", metric),
                "unit": unit if unit in _UNITS else None,
                "unit_reference": ref("unit", unit),
                "quantity": _quantity(item.get("quantity")),
                "provenance": provenance,
                "observed_at": item.get("observed_at"),
                "supersedes": observation_ref(item.get("supersedes_id")),
                "effective": (item["operation"], item["source"], item["observation_id"]) in effective_ids,
            }
        )

    effects: list[dict[str, object]] = []
    reviews: list[dict[str, object]] = []
    for item in reservations:
        operation_id, attempt_id, category = item.get("operation"), item.get("attempt"), item.get("category")
        if type(operation_id) is not str or type(attempt_id) is not str or type(category) is not str:
            raise ValueError("invalid durable reservation")
        reserved_bytes = item.get("retained_bytes")
        if type(reserved_bytes) is not int or reserved_bytes < 0:
            raise ValueError("invalid durable retained-byte reservation")
        operation: OperationRecord | None = store.find_operation(operation_id)
        effect_evidence = store.outcome_effect_evidence(program_id, operation_id)
        effects.append(
            {
                "operation": ref("operation", operation_id),
                "attempt": ref("attempt", attempt_id),
                "attempt_binding": "recorded" if attempt_id in attempt_by_id else "unknown",
                "category": category if category in {item.value for item in WorkKind} else None,
                "recorded": operation is not None,
                "reserved_retained_bytes": reserved_bytes,
                "reservation_is_usage": False,
                "status": None if operation is None else operation.status,
                "dispatch_count": None if operation is None else operation.attempts,
                "fence": None if operation is None else operation.fence,
                "effect": None if operation is None else ref("effect", operation.effect_key),
                "request_digest": None if operation is None else ref("digest", operation.request_digest),
                "receipt": None if operation is None else ref("effect-receipt", operation.accepted_reference),
                "retry_proof": None if operation is None else operation.retry_proof,
                "deliveries": [
                    {"fence": row["fence"], "claimed_at": row["claimed_at"], "lease_until": row["lease_until"]}
                    for row in _items(effect_evidence.get("deliveries"))
                ],
                "transports": [
                    {"fence": row["fence"], "succeeded": bool(row["succeeded"])}
                    for row in _items(effect_evidence.get("transports"))
                ],
                "observations": [
                    {
                        "id": ref("effect-observation", row["observation_id"]),
                        "fence": row["fence"],
                        "kind": row["kind"],
                        "reference": ref("effect-receipt", row["reference"]),
                    }
                    for row in _items(effect_evidence.get("observations"))
                ],
                "reconciliations": [
                    {
                        "sequence": row["sequence"],
                        "fence": row["fence"],
                        "outcome": row["outcome"],
                        "reference": ref("effect-receipt", row["reference"]),
                        "evidence": ref("evidence", row["evidence"]),
                        "matched_digest": ref("digest", row["matched_digest"]),
                        "authoritative_absence": bool(row["authoritative_absence"]),
                    }
                    for row in _items(effect_evidence.get("reconciliations"))
                ],
            }
        )
        if category == "review" and operation_id.startswith("product-review:") and operation is not None:
            request_data = store.find_artifact(operation_id, "review-request")
            receipt_data = store.find_artifact(operation_id, "review-receipt")
            attempt = attempt_by_id.get(attempt_id)
            if request_data is None or attempt is None:
                reviews.append(
                    {
                        "operation": ref("operation", operation_id),
                        "attempt": ref("attempt", attempt_id),
                        "request_recorded": request_data is not None,
                        "operation_status": operation.status,
                        "receipt_recorded": receipt_data is not None,
                        "binding": "unknown",
                        "verdict": None,
                    }
                )
                continue
            if operation_id != "product-review:" + hashlib.sha256(request_data).hexdigest():
                raise ValueError("unverifiable product review request")
            request = _mapping(json.loads(request_data))
            subject = _mapping(request.get("subject"))
            policy = _mapping(request.get("policy"))
            review_input = {
                "candidate_digest": request.get("candidate_digest"),
                "policy": policy,
                "subject": subject,
            }
            input_digest = hashlib.sha256(
                json.dumps(review_input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                request.get("version") != 1
                or request.get("author_attempt_id") != attempt_id
                or request.get("author_actor_id") != attempt.actor_id
                or subject.get("attempt_digest") != attempt.spec.digest
                or subject.get("spec_digest") != attempt.spec.spec_digest
                or subject.get("candidate_digest") != request.get("candidate_digest")
                or subject.get("policy_digest") != policy.get("digest")
                or request.get("input_digest") != input_digest
                or request.get("input_reference") != f"candidate:{request.get('candidate_digest')}"
            ):
                raise ValueError("product review does not match its original Attempt")
            verdict = None
            receipt_ref = None
            reviewer = None
            reviewer_session = None
            observed_at = None
            fresh_until = None
            if receipt_data is not None:
                try:
                    receipt = decode_product_review_receipt(receipt_data)
                    if (
                        receipt.request_digest != request.get("input_digest")
                        or asdict(receipt.subject) != subject
                        or receipt.reviewer_id == attempt.actor_id
                        or receipt.reviewer_session == attempt_id
                    ):
                        raise ValueError("review receipt differs from original request")
                    verdict = receipt.verdict.value
                    receipt_ref = ref("review", receipt.review_id)
                    reviewer = ref("actor", receipt.reviewer_id)
                    reviewer_session = ref("runtime-handle", receipt.reviewer_session)
                    observed_at, fresh_until = receipt.observed_at, receipt.fresh_until
                except (TypeError, ValueError, json.JSONDecodeError):
                    # An invalid receipt is evidence of neither approval nor a verdict.
                    verdict = None
            reviews.append(
                {
                    "operation": ref("operation", operation_id),
                    "attempt": ref("attempt", attempt_id),
                    "candidate": ref("candidate", request.get("candidate_digest")),
                    "policy": ref("policy", subject.get("policy_digest")),
                    "repository": ref("repository", subject.get("repository")),
                    "base": ref("revision", subject.get("base_revision")),
                    "head": ref("revision", subject.get("head_revision")),
                    "request_recorded": True,
                    "binding": "original-attempt",
                    "operation_status": operation.status,
                    "receipt_recorded": receipt_data is not None,
                    "review": receipt_ref,
                    "reviewer": reviewer,
                    "reviewer_session": reviewer_session,
                    "verdict": verdict,
                    "observed_at": observed_at,
                    "fresh_until": fresh_until,
                    "freshness": "not-evaluated",
                }
            )

    attempts: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []
    for attempt in program.attempts:
        spec = attempt.spec
        runtime_id = f"runtime:{attempt.attempt_id}"
        operation = store.find_operation(runtime_id)
        identity_data = store.find_artifact(runtime_id, "identity") if operation is not None else None
        receipt_data = store.find_artifact(runtime_id, "runtime-receipt") if operation is not None else None
        identity = decode_identity(identity_data) if identity_data is not None else None
        receipt_identity = None
        runtime_handle = None
        runtime_group = None
        runtime_fence = None
        if receipt_data is not None:
            runtime_handle, runtime_fence, receipt_identity, runtime_group = decode_runtime_receipt_with_group(
                receipt_data
            )

        def identity_view(value: RuntimeIdentity | None) -> dict[str, str | None] | None:
            if value is None:
                return None
            fields = asdict(value)
            return {
                name: ref(
                    "provider"
                    if "provider" in name
                    else "effort"
                    if "effort" in name
                    else "agent"
                    if name == "agent_definition_version"
                    else "model",
                    field,
                )
                for name, field in fields.items()
            }

        allocation = None
        if operation is not None:
            published_allocation = store.find_artifact(runtime_id, "allocation")
            has_pinned_inputs = (
                spec.allocation_reference is not None
                and spec.context_reference is not None
                and spec.allocation_reference.startswith("sha256:")
                and spec.context_reference.startswith("sha256:")
            )
            if published_allocation is not None or has_pinned_inputs:
                allocation = load_allocation(store, spec)
        rejection_id = f"rejection:{attempt.attempt_id}"
        rejection = store.find_operation(rejection_id)
        verification_data: bytes | None = None
        verification_reference: object = None
        if rejection is not None:
            artifact = store.find_artifact(rejection_id, "rejected-result")
            request = _mapping(_mapping(json.loads(rejection.request_json)).get("request"))
            verification_data, verification_reference = artifact, request.get("manifest")
            status = request.get("product_review_status")
            if status not in {None, "changes_requested", "owner_decision_needed", "comment", "invalid"}:
                raise ValueError("invalid recorded review decision")
            decisions.append(
                {
                    "attempt": ref("attempt", attempt.attempt_id),
                    "operation": ref("operation", rejection_id),
                    "review_operation": ref("operation", request.get("review_operation_id")),
                    "review_status": status,
                    "verification_manifest": ref("artifact", request.get("manifest")),
                    "rejection_recorded": artifact is not None,
                }
            )
        acceptance = store.find_operation(f"acceptance:{attempt.attempt_id}")
        if acceptance is not None:
            request = _mapping(_mapping(json.loads(acceptance.request_json)).get("request"))
            name = request.get("name")
            if type(name) is not str or request.get("operation") != runtime_id:
                raise ValueError("invalid original acceptance evidence")
            verification_data = store.find_artifact(runtime_id, name)
            verification_reference = request.get("manifest")
            if verification_data is None:
                raise ValueError("accepted verification evidence is missing")
        if verification_data is not None:
            if verification_reference != "sha256:" + hashlib.sha256(verification_data).hexdigest():
                raise ValueError("verification evidence reference mismatch")
            manifest = _mapping(json.loads(verification_data))
            subject = _mapping(manifest.get("subject"))
            evidence = _items(manifest.get("evidence"))
            if subject.get("attempt_digest") != spec.digest or subject.get("spec_digest") != spec.spec_digest:
                raise ValueError("verification evidence differs from original Attempt")
            for observation in evidence:
                if (
                    observation.get("subject") != subject
                    or type(observation.get("passed")) is not bool
                    or type(observation.get("observed_at")) is not int
                    or type(observation.get("fresh_until")) is not int
                ):
                    raise ValueError("invalid exact-subject check evidence")
                checks.append(
                    {
                        "attempt": ref("attempt", attempt.attempt_id),
                        "manifest": ref("artifact", verification_reference),
                        "evidence": ref("evidence", observation.get("evidence_id")),
                        "check": ref("check", observation.get("check")),
                        "producer": ref("actor", observation.get("producer")),
                        "tool_version": ref("tool-version", observation.get("tool_version")),
                        "artifact": ref("artifact", observation.get("artifact_reference")),
                        "reviewer_session": ref("runtime-handle", observation.get("reviewer_session")),
                        "candidate": ref("candidate", subject.get("candidate_digest")),
                        "policy": ref("policy", subject.get("policy_digest")),
                        "repository": ref("repository", subject.get("repository")),
                        "base": ref("revision", subject.get("base_revision")),
                        "head": ref("revision", subject.get("head_revision")),
                        "observed_at": observation["observed_at"],
                        "fresh_until": observation["fresh_until"],
                        "passed": observation["passed"],
                        "freshness": "not-evaluated",
                    }
                )
        attempts.append(
            {
                "id": ref("attempt", attempt.attempt_id),
                "work_unit": ref("work-unit", spec.work_unit_id),
                "spec_revision": spec.spec_revision,
                "spec": ref("spec", spec.spec_digest),
                "attempt_spec": ref("attempt-spec", spec.digest),
                "status": attempt.status.value,
                "actor": ref("actor", attempt.actor_id),
                "remediation_of": ref("attempt", spec.remediation_of),
                "allocation_reference": ref("artifact", spec.allocation_reference),
                "context_reference": ref("artifact", spec.context_reference),
                "agent_definition_reference": ref("agent", spec.agent_definition_reference),
                "workspace_reference": ref("workspace", spec.workspace_reference),
                "inputs": [
                    {
                        "name": ref("input-name", item.name),
                        "reference": ref("artifact", item.reference),
                        "source_kind": item.source_kind,
                        "source_work_unit": ref("work-unit", item.source_work_unit_id),
                        "satisfaction": ref("satisfaction", item.satisfaction_id),
                        "output_name": ref("input-name", item.output_name),
                    }
                    for item in spec.effective_inputs
                ],
                "allocation": None
                if allocation is None
                else {
                    "runtime": ref("runtime", allocation.runtime_id),
                    "provider": ref("provider", allocation.provider_id),
                    "model": ref("model", allocation.model_id),
                    "effort": ref("effort", allocation.reasoning_effort),
                    "variant": ref("variant", allocation.variant),
                    "decision_provenance": ref("provenance", allocation.decision_provenance),
                    "requirements_provenance": ref("provenance", allocation.requirements_provenance),
                    "knowledge_utilization": None
                    if allocation.knowledge_utilization is None
                    else {
                        "state": allocation.knowledge_utilization.state.value,
                        "source": ref("source", allocation.knowledge_utilization.source),
                        "identifier": ref("knowledge", allocation.knowledge_utilization.identifier),
                        "version": ref("knowledge-version", allocation.knowledge_utilization.version),
                        "rights": None,
                    },
                    "route": None,
                },
                "identity": identity_view(identity),
                "runtime_receipt_identity": identity_view(receipt_identity),
                "runtime_receipt_recorded": receipt_data is not None,
                "runtime_receipt_fence": runtime_fence,
                "runtime_handle": ref("runtime-handle", runtime_handle),
                "runtime_group": ref("runtime-group", runtime_group),
                "call_identity": None,
            }
        )

    claims: list[dict[str, object]] = []
    count = store.served_claim_count(program_id)
    for offset in range(0, count, 8):
        for item in store.served_claims(program_id, offset=offset):
            command = item.get("claim")
            if type(command) is not str:
                raise ValueError("invalid durable claim identity")
            evidence = store.outcome_claim_evidence(program_id, command)
            mode = item.get("mode")
            quarantine = None if evidence.get("quarantine") is None else _mapping(evidence["quarantine"])
            release = None if evidence.get("release") is None else _mapping(evidence["release"])
            claims.append(
                {
                    "attempt": ref("attempt", item.get("attempt")),
                    "claim": ref("claim", item.get("claim")),
                    "claimant": ref("actor", item.get("claimant")),
                    "generation": item.get("generation"),
                    "mode": item.get("mode"),
                    "original_expires_at": item.get("original_expires_at"),
                    "recorded_expires_at": item.get("recorded_expires_at"),
                    "profile": ref("profile", evidence.get("profile")),
                    "resources": [
                        {"kind": resource.get("kind"), "key": ref("resource", resource.get("key"))}
                        for resource in _items(evidence.get("resources"))
                    ],
                    "handoff_proofs": [
                        {
                            "stage": proof.get("stage"),
                            "source": ref("source", proof.get("source")),
                            "profile": ref("profile", proof.get("profile")),
                            "reference": ref(
                                "runtime-group" if mode == "managed" else "handoff-proof", proof.get("reference")
                            ),
                            "generation": proof.get("generation"),
                        }
                        for proof in _items(evidence.get("proofs"))
                    ],
                    "quarantine_evidence": None if quarantine is None else ref("evidence", quarantine.get("evidence")),
                    "release_evidence": None if release is None else ref("evidence", release.get("evidence")),
                    "workspace_reconciliations": [
                        {
                            "subject": ref("workspace-subject", row.get("subject")),
                            "owner": ref("actor", row.get("owner")),
                            "evidence": ref("evidence", row.get("evidence")),
                        }
                        for row in _items(evidence.get("reconciliations"))
                    ],
                    "released_recorded": item.get("released_recorded"),
                    "quarantined_recorded": item.get("quarantined_recorded"),
                    "last_recorded_subject": ref("workspace-subject", item.get("latest_recorded_subject")),
                    "recorded_owner_reconciliation_needed": item.get("recorded_owner_reconciliation_needed"),
                    "cooperative_quiescence_recorded": item.get("cooperative_quiescence_recorded"),
                    "managed_settlement_recorded": item.get("managed_settlement_recorded"),
                    "physical_settlement": None,
                }
            )
    if len(claims) != count or store.served_change_token() != token:
        raise ValueError("durable outcome changed during export")
    return {
        "schema": "creatidy.outcome.private/1",
        "policy": PRIVATE_METADATA,
        "distribution": "local-only; no source-content sharing authority",
        "pseudonyms": "per-export unless owner supplies the same private key; not anonymization",
        "source_rights": "not_retained; source content omitted",
        "program": {
            "id": ref("program", program.program_id),
            "revision": program.revision,
            "status": program.status.value,
            "spec": ref("spec", program.spec.digest),
            "spec_history": [
                {"revision": item.revision, "spec": ref("spec", item.digest), "parent": ref("spec", item.parent_digest)}
                for item in program.spec_history
            ],
        },
        "work_units": [
            {
                "id": ref("work-unit", item.work_unit_id),
                "status": item.status.value,
                "active_attempt": ref("attempt", item.active_attempt_id),
                "satisfaction": ref("satisfaction", item.satisfaction_id),
            }
            for item in program.work_unit_states
        ],
        "attempts": attempts,
        "satisfactions": [
            {
                "id": ref("satisfaction", item.reference_id),
                "work_unit": ref("work-unit", item.work_unit_id),
                "attempt": ref("attempt", item.source_attempt_id),
                "spec": ref("spec", item.spec_digest),
                "record_order": item.record_order,
                "outputs": [
                    {"name": ref("input-name", output.name), "reference": ref("artifact", output.reference)}
                    for output in item.output_bindings
                ],
            }
            for item in program.satisfactions
        ],
        "claims": claims,
        "effects": effects,
        "reviews": reviews,
        "checks": checks,
        "decisions": decisions,
        "work_budget": None
        if budget_policy is None or binding is None
        else {
            "project": ref("project", binding.get("project")),
            "max_active": budget_policy.max_active,
            "retained_bytes": budget_policy.retained_bytes,
            "counts": [{"category": kind.value, "limit": limit} for kind, limit in budget_policy.counts],
            "resources": [
                {
                    "metric": item.metric if item.metric in _METRICS else None,
                    "metric_reference": ref("metric", item.metric),
                    "unit": item.unit if item.unit in _UNITS else None,
                    "unit_reference": ref("unit", item.unit),
                    "quantity": str(item.quantity),
                }
                for item in budget_policy.resources
            ],
            "strict_inference": budget_policy.strict_inference,
        },
        "usage_history": observations,
        "usage_totals": [
            {
                "source": ref("source", source),
                "metric": metric if metric in _METRICS else None,
                "metric_reference": ref("metric", metric),
                "unit": unit if unit in _UNITS else None,
                "unit_reference": ref("unit", unit),
                "provenance": provenance,
                "known_subtotal": None if subtotal is None else str(subtotal),
                "quantity": None,
                "complete": False,
            }
            for (source, metric, unit, provenance), subtotal in sorted(grouped.items())
        ],
        "coverage": {
            "budget_configured": configured,
            "strict_inference_profile": None if budget_policy is None else budget_policy.strict_inference,
            "hard_inference_cap": budget.get("hard_inference_cap") if configured else None,
            "effect_inventory": "recorded-reservations-only" if configured else "unknown-budget-not-bound",
            "review_inventory": "recorded-review-reservations-only" if configured else "unknown-budget-not-bound",
            "external_client_usage": None,
            "route_and_call_identity": None,
            "complete_cost": None,
            "usage_total_scope": "per-source-known-subtotal-not-lineage-or-cross-source-total",
            "unattributed_usage_observations": sum(item.get("attempt") not in attempt_by_id for item in history),
        },
    }
