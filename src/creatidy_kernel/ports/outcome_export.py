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

from creatidy_kernel.core.deadlines import DeadlineProjection
from creatidy_kernel.core.domain import Program
from creatidy_kernel.core.execution import Artifact, ArtifactManifest, Candidate, RuntimeIdentity
from creatidy_kernel.core.outcomes import Provenance
from creatidy_kernel.core.verification import CandidateResult
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.allocation import decode_identity, decode_runtime_receipt_with_group, load_allocation
from creatidy_kernel.ports.intake import draft_from
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
    def outcome_revocation(self, program_id: str) -> dict[str, object] | None: ...
    def outcome_artifact_metadata(
        self, program_id: str, operation_id: str, prefix: str
    ) -> tuple[dict[str, object], ...]: ...


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
    deadline = DeadlineProjection.decode(binding["projection"]) if binding is not None else None
    revocation = store.outcome_revocation(program_id)
    if revocation is not None and revocation.get("program") != program_id:
        raise ValueError("recorded revocation differs from selected Program")
    reservations = _items(budget.get("reservations", []))
    history = _items(budget.get("usage_history", []))
    effective = _items(budget.get("effective_usage", []))
    attempt_by_id = {item.attempt_id: item for item in program.attempts}
    native_subjects: dict[str, dict[str, object]] = {}
    native_subject_output: list[dict[str, object]] = []
    for attempt in program.attempts:
        context_reference = attempt.spec.context_reference
        if context_reference is None or not context_reference.startswith("ordinary-proposal:"):
            continue
        proposal_digest = context_reference.removeprefix("ordinary-proposal:")
        raw = store.find_artifact(f"ordinary-program:proposal:{proposal_digest}", "record")
        if raw is None or hashlib.sha256(raw).hexdigest() != proposal_digest:
            raise ValueError("original ordinary proposal evidence is missing or inconsistent")
        proposal = _mapping(json.loads(raw))
        drafts = _items(proposal.get("drafts"))
        if (
            proposal.get("version") != 1
            or proposal.get("kind") != "ordinary-program-proposal"
            or proposal.get("program") != program_id
        ):
            raise ValueError("original ordinary proposal belongs to another Program")
        matches = [
            draft_from(json.dumps(item, sort_keys=True, separators=(",", ":")).encode())
            for item in drafts
            if item.get("task_id") == attempt.spec.work_unit_id
        ]
        if len(matches) != 1:
            raise ValueError("original ordinary WorkUnit has no unique native subject")
        subject = matches[0].evidence.subject
        subject_key = json.dumps(
            [subject.origin, subject.repository, subject.issue], ensure_ascii=True, separators=(",", ":")
        )
        native_subjects[attempt.attempt_id] = subject.payload()
        native_subject_output.append(
            {
                "attempt": ref("attempt", attempt.attempt_id),
                "work_unit": ref("work-unit", attempt.spec.work_unit_id),
                "driver": "ordinary-proposal-v1",
                "issue": ref("native-issue", subject_key),
                "origin": ref("origin", subject.origin),
                "repository": ref("native-repository", subject.repository),
                "branch": ref("branch", subject.branch),
                "base": ref("revision", subject.base),
                "current_remote_state": None,
            }
        )
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
                "attempt": ref("attempt", item.get("attempt_id")),
                "attempt_binding": "recorded" if item.get("attempt_id") in attempt_by_id else "unknown",
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
    findings: list[dict[str, object]] = []
    native_effects: list[dict[str, object]] = []
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
        if operation is not None:
            native_request = _mapping(_mapping(json.loads(operation.request_json)).get("request"))
            if native_request.get("kind") == "ordinary-publication":
                attempt = attempt_by_id.get(attempt_id)
                native = native_subjects.get(attempt_id)
                accepted = _mapping(native_request.get("accepted"))
                subject = _mapping(native_request.get("subject"))
                action = native_request.get("action")
                satisfaction = next(
                    (item for item in program.satisfactions if item.reference_id == accepted.get("satisfaction")), None
                )
                if (
                    attempt is None
                    or native is None
                    or action not in {"pr", "receipt"}
                    or satisfaction is None
                    or satisfaction.source_attempt_id != attempt_id
                    or accepted.get("attempt") != attempt.spec.digest
                    or accepted.get("program") != program_id
                    or accepted.get("unit") != attempt.spec.work_unit_id
                    or accepted.get("spec") != attempt.spec.spec_digest
                    or accepted.get("issue") != native.get("issue")
                    or native_request.get("origin") != native.get("origin")
                    or subject.get("attempt_digest") != attempt.spec.digest
                    or subject.get("spec_digest") != attempt.spec.spec_digest
                    or subject.get("candidate_digest") != accepted.get("candidate")
                    or subject.get("base_revision") != accepted.get("base")
                    or subject.get("head_revision") != accepted.get("head")
                ):
                    raise ValueError("ordinary publication differs from its original accepted subject")
                receipt_data = store.find_artifact(operation_id, "native-receipt")
                receipt_reference = None
                if receipt_data is not None:
                    receipt = _mapping(json.loads(receipt_data))
                    identity = _mapping(receipt.get("operation"))
                    if (
                        receipt.get("profile") != "forgejo-native-readback:v1"
                        or receipt.get("action") != action
                        or receipt.get("accepted") != accepted
                        or identity
                        != {
                            "operation_id": operation.operation_id,
                            "effect_key": operation.effect_key,
                            "request_digest": operation.request_digest,
                        }
                        or type(receipt.get("reference")) is not str
                        or (operation.status == "accepted" and receipt.get("reference") != operation.accepted_reference)
                    ):
                        raise ValueError("native receipt differs from its original publication")
                    receipt_reference = receipt["reference"]
                subject_key = json.dumps(
                    [native["origin"], native["repository"], native["issue"]], ensure_ascii=True, separators=(",", ":")
                )
                native_effects.append(
                    {
                        "driver": "ordinary-forgejo",
                        "action": action,
                        "operation": ref("operation", operation_id),
                        "attempt": ref("attempt", attempt_id),
                        "work_unit": ref("work-unit", attempt.spec.work_unit_id),
                        "issue": ref("native-issue", subject_key),
                        "origin": ref("origin", native["origin"]),
                        "repository": ref("native-repository", native["repository"]),
                        "candidate": ref("candidate", accepted.get("candidate")),
                        "satisfaction": ref("satisfaction", accepted.get("satisfaction")),
                        "receipt_recorded": receipt_data is not None,
                        "receipt": ref("native-record", receipt_reference),
                        "receipt_profile": None if receipt_data is None else "forgejo-native-readback:v1",
                        "current_remote_state": None,
                    }
                )
            elif native_request.get("schema") in {"reference-pr-v1", "task-pr-v1"}:
                attempt = attempt_by_id.get(attempt_id)
                acceptance = store.find_operation(f"acceptance:{attempt_id}")
                if attempt is None or acceptance is None:
                    raise ValueError("reference publication lacks original accepted Attempt")
                accepted = _mapping(_mapping(json.loads(acceptance.request_json)).get("request"))
                if (
                    native_request.get("acceptance_reference") != accepted.get("manifest")
                    or native_request.get("head") != accepted.get("head")
                    or native_request.get("mode") not in {"synthetic", "live"}
                ):
                    raise ValueError("reference publication differs from original acceptance")
                if native_request.get("schema") == "task-pr-v1":
                    mode_operation = store.find_operation("task_execution:mode")
                    mode = (
                        None
                        if mode_operation is None
                        else _mapping(_mapping(json.loads(mode_operation.request_json)).get("request"))
                    )
                    task_digest = None if mode is None else mode.get("task_digest")
                    if (
                        program_id != "task_execution"
                        or operation_id != "task_execution:pr"
                        or mode_operation is None
                        or mode_operation.effect_key != "task_execution:mode"
                        or type(task_digest) is not str
                        or not task_digest
                        or not any(
                            item.name == "task" and item.reference == "task_execution:task:" + task_digest
                            for item in program.spec.initial_inputs
                        )
                        or mode is None
                        or native_request.get("repository") != mode.get("repository")
                        or native_request.get("mode") != mode.get("mode")
                    ):
                        raise ValueError("task publication differs from original bounded task")
                known_data = store.find_artifact(operation_id, "known-reference")
                known_reference = None if known_data is None else known_data.decode("utf-8")
                if (
                    known_reference is not None
                    and operation.accepted_reference is not None
                    and known_reference != operation.accepted_reference
                ):
                    raise ValueError("reference publication receipt differs from original effect")
                native_effects.append(
                    {
                        "driver": native_request["schema"],
                        "action": "pr",
                        "transport_mode": native_request["mode"],
                        "operation": ref("operation", operation_id),
                        "attempt": ref("attempt", attempt_id),
                        "work_unit": ref("work-unit", attempt.spec.work_unit_id),
                        "repository": ref("native-repository", native_request.get("repository")),
                        "base_branch": ref("branch", native_request.get("base_branch")),
                        "head": ref("revision", native_request.get("head")),
                        "acceptance_manifest": ref("artifact", native_request.get("acceptance_reference")),
                        "receipt": ref("native-record", operation.accepted_reference or known_reference),
                        "receipt_recorded": operation.accepted_reference is not None or known_reference is not None,
                        "current_remote_state": None,
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
            review_findings: list[dict[str, object]] = []
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
                    review_findings = [
                        {
                            "id": ref("finding", finding.finding_id),
                            "fingerprint": ref("finding-fingerprint", finding.fingerprint),
                        }
                        for finding in receipt.findings
                    ]
                    findings.extend(
                        {
                            **item,
                            "attempt": ref("attempt", attempt_id),
                            "review_operation": ref("operation", operation_id),
                            "source": "product-review",
                        }
                        for item in review_findings
                    )
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
                    "findings": review_findings,
                    "observed_at": observed_at,
                    "fresh_until": fresh_until,
                    "freshness": "not-evaluated",
                }
            )

    attempts: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    checks: list[dict[str, object]] = []
    check_results: list[dict[str, object]] = []
    manifests: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []
    for item in reservations:
        operation_id, attempt_id = item.get("operation"), item.get("attempt")
        if item.get("category") != "verification" or type(operation_id) is not str or type(attempt_id) is not str:
            continue
        prefix = f"verification:{attempt_id}:"
        if not operation_id.startswith(prefix):
            continue
        operation = store.find_operation(operation_id)
        original_attempt = attempt_by_id.get(attempt_id)
        if operation is None or original_attempt is None:
            raise ValueError("verification reservation lacks original Attempt or operation")
        request = _mapping(_mapping(json.loads(operation.request_json)).get("request"))
        subject, check = _mapping(request.get("subject")), request.get("check")
        if type(check) is not str or not check:
            raise ValueError("invalid original verification check")
        expected = hashlib.sha256(
            json.dumps([check, subject], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            operation_id != prefix + expected
            or subject.get("attempt_digest") != original_attempt.spec.digest
            or subject.get("spec_digest") != original_attempt.spec.spec_digest
        ):
            raise ValueError("verification operation differs from original Attempt or subject")
        data = store.find_artifact(operation_id, "result")
        value = None if data is None else json.loads(data)
        check_results.append(
            {
                "attempt": ref("attempt", attempt_id),
                "operation": ref("operation", operation_id),
                "check": ref("check", check),
                "result_recorded": data is not None,
                "evidence_recorded": value is not None,
                "candidate": ref("candidate", subject.get("candidate_digest")),
            }
        )
        if value is None:
            continue
        observation = _mapping(value)
        if (
            observation.get("subject") != subject
            or observation.get("check") != check
            or type(observation.get("passed")) is not bool
            or type(observation.get("observed_at")) is not int
            or type(observation.get("fresh_until")) is not int
            or any(
                type(observation.get(name)) is not str or not observation.get(name)
                for name in ("evidence_id", "producer", "tool_version", "artifact_reference")
            )
        ):
            raise ValueError("invalid original verification result")
        checks.append(
            {
                "attempt": ref("attempt", attempt_id),
                "manifest": None,
                "operation": ref("operation", operation_id),
                "evidence": ref("evidence", observation.get("evidence_id")),
                "check": ref("check", check),
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

    def record_manifest(
        attempt_id: str, spec_digest: str, original_spec: str, data: bytes, reference: str, decision: str | None
    ) -> None:
        if reference != "sha256:" + hashlib.sha256(data).hexdigest():
            raise ValueError("verification evidence reference mismatch")
        manifest = _mapping(json.loads(data))
        subject = _mapping(manifest.get("subject"))
        evidence = _items(manifest.get("evidence"))
        if subject.get("attempt_digest") != spec_digest or subject.get("spec_digest") != original_spec:
            raise ValueError("verification evidence differs from original Attempt")
        manifests.append(
            {
                "attempt": ref("attempt", attempt_id),
                "manifest": ref("artifact", reference),
                "candidate": ref("candidate", subject.get("candidate_digest")),
                "decision": decision,
                "freshness": "not-evaluated",
            }
        )
        for observation in evidence:
            if (
                observation.get("subject") != subject
                or type(observation.get("passed")) is not bool
                or type(observation.get("observed_at")) is not int
                or type(observation.get("fresh_until")) is not int
                or any(
                    type(observation.get(name)) is not str or not observation.get(name)
                    for name in ("evidence_id", "check", "producer", "tool_version", "artifact_reference")
                )
            ):
                raise ValueError("invalid exact-subject check evidence")
            checks.append(
                {
                    "attempt": ref("attempt", attempt_id),
                    "manifest": ref("artifact", reference),
                    "operation": None,
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
        if "findings" in manifest:
            for finding in _items(manifest["findings"]):
                if (
                    finding.get("subject") != subject
                    or type(finding.get("finding_id")) is not str
                    or not finding.get("finding_id")
                    or type(finding.get("fingerprint")) is not str
                    or not finding.get("fingerprint")
                ):
                    raise ValueError("finding differs from original verification subject")
                findings.append(
                    {
                        "id": ref("finding", finding.get("finding_id")),
                        "fingerprint": ref("finding-fingerprint", finding.get("fingerprint")),
                        "attempt": ref("attempt", attempt_id),
                        "manifest": ref("artifact", reference),
                        "candidate": ref("candidate", subject.get("candidate_digest")),
                        "source": "verification-manifest",
                    }
                )

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
        terminal_data = store.find_artifact(runtime_id, "terminal-outcome") if operation is not None else None
        terminal = None if terminal_data is None else _mapping(json.loads(terminal_data))
        if terminal is not None and (
            terminal.get("version") != 1
            or terminal.get("attempt") != spec.digest
            or terminal.get("candidate") not in {"present", "absent", "unknown", "not_requested"}
            or type(terminal.get("fence")) is not int
            or type(terminal.get("handle")) is not str
            or (
                operation is not None
                and (cast(int, terminal["fence"]) < 1 or cast(int, terminal["fence"]) > operation.fence)
            )
            or (
                operation is not None
                and operation.accepted_reference is not None
                and terminal.get("handle") != operation.accepted_reference
            )
        ):
            raise ValueError("terminal outcome differs from original runtime Attempt")
        ordinary_data = store.find_artifact(runtime_id, "ordinary-candidate") if operation is not None else None
        if ordinary_data is not None:
            original = _mapping(json.loads(ordinary_data))
            patches = store.outcome_artifact_metadata(program_id, runtime_id, "candidate.patch")
            original_spec = next((item for item in program.spec_history if item.digest == spec.spec_digest), None)
            if (
                original.get("version") != 1
                or type(original.get("head")) is not str
                or type(original.get("scope")) is not str
                or type(original.get("paths")) is not list
                or len(patches) != 1
                or type(patches[0].get("digest")) is not str
                or spec.workspace_reference is None
                or original_spec is None
            ):
                raise ValueError("ordinary candidate lacks its original immutable metadata")
            artifact = Artifact("candidate.patch", "sha256:" + cast(str, patches[0]["digest"]))
            candidate = Candidate(
                attempt.attempt_id, spec.digest, ArtifactManifest(spec.workspace_reference, (artifact,))
            )
            unit = original_spec.work_unit(spec.work_unit_id)
            candidate_digest = CandidateResult(
                f"candidate:{attempt.attempt_id}",
                candidate,
                tuple((name, "candidate.patch") for name in sorted(unit.outputs)),
            ).digest
            candidates.append(
                {
                    "attempt": ref("attempt", attempt.attempt_id),
                    "candidate": ref("candidate", candidate_digest),
                    "source": "ordinary-candidate-v1",
                    "head": ref("revision", original.get("head")),
                    "workspace_subject": ref("workspace-subject", original.get("scope")),
                    "patch": ref("artifact", artifact.digest),
                    "patch_integrity": "recorded-digest-only-not-read",
                    "current_remote_state": None,
                }
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
            if status not in {None, "approved", "changes_requested", "owner_decision_needed", "comment", "invalid"}:
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
        recorded_manifests: dict[str, bytes] = {}
        if verification_data is not None:
            if type(verification_reference) is not str:
                raise ValueError("verification reference is missing")
            recorded_manifests[verification_reference] = verification_data
        if operation is not None:
            for metadata in store.outcome_artifact_metadata(program_id, runtime_id, "verification:"):
                name, checksum = metadata.get("name"), metadata.get("digest")
                if type(name) is not str or type(checksum) is not str or name != "verification:" + checksum:
                    raise ValueError("invalid retained verification artifact identity")
                data = store.find_artifact(runtime_id, name)
                if data is None:
                    raise ValueError("retained verification artifact is missing")
                reference = "sha256:" + checksum
                if reference in recorded_manifests and recorded_manifests[reference] != data:
                    raise ValueError("conflicting original verification artifacts")
                recorded_manifests[reference] = data
        for reference, data in sorted(recorded_manifests.items()):
            decision = (
                (
                    "accepted"
                    if acceptance is not None
                    else "rejected"
                    if rejection is not None and verification_data is not None
                    else None
                )
                if reference == verification_reference
                else None
            )
            record_manifest(attempt.attempt_id, spec.digest, spec.spec_digest, data, reference, decision)
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
                "terminal_outcome": None
                if terminal is None
                else {
                    "candidate": terminal["candidate"],
                    "runtime_fence": terminal["fence"],
                    "handle": ref("runtime-handle", terminal["handle"]),
                    "current_activity": None,
                },
                "call_identity": None,
            }
        )

    claims: list[dict[str, object]] = []
    original_claims: dict[str, tuple[dict[str, object], list[dict[str, object]]]] = {}
    count = store.served_claim_count(program_id)
    for offset in range(0, count, 8):
        for item in store.served_claims(program_id, offset=offset):
            command = item.get("claim")
            if type(command) is not str:
                raise ValueError("invalid durable claim identity")
            evidence = store.outcome_claim_evidence(program_id, command)
            original_claim = _mapping(evidence.get("original_claim"))
            claim_attempt = _mapping(original_claim.get("attempt"))
            claim_attempt_id = claim_attempt.get("attempt_id")
            if type(claim_attempt_id) is not str or claim_attempt_id != item.get("attempt"):
                raise ValueError("claim differs from original Attempt")
            proofs = _items(evidence.get("proofs"))
            if claim_attempt_id in original_claims:
                raise ValueError("Attempt has competing original claims")
            original_claims[claim_attempt_id] = (original_claim, proofs)
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
                        for proof in proofs
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
    cancellations: list[dict[str, object]] = []
    if program_id == "task_execution" and all(item.attempt_id != "task_execution:change" for item in program.attempts):
        mode_operation = store.find_operation("task_execution:mode")
        pre_attempt = store.find_operation("cancel:task_execution:change")
        if pre_attempt is not None:
            mode = (
                None
                if mode_operation is None
                else _mapping(_mapping(json.loads(mode_operation.request_json)).get("request"))
            )
            request = _mapping(_mapping(json.loads(pre_attempt.request_json)).get("request"))
            task_digest = None if mode is None else mode.get("task_digest")
            if (
                mode_operation is None
                or mode_operation.effect_key != "task_execution:mode"
                or type(task_digest) is not str
                or not task_digest
                or not any(
                    item.name == "task" and item.reference == "task_execution:task:" + task_digest
                    for item in program.spec.initial_inputs
                )
                or not any(item.work_unit_id == "change" for item in program.spec.work_units)
                or pre_attempt.effect_key != "cancel:task_execution:change"
                or request != {"operation": "runtime:task_execution:change", "descendants": "unproven"}
            ):
                raise ValueError("pre-Attempt cancellation differs from original bounded task")
            runtime = store.find_operation("runtime:task_execution:change")
            settlement_data = store.find_artifact(pre_attempt.operation_id, "owned-group-settlement")
            settlement = None if settlement_data is None else _mapping(json.loads(settlement_data))
            qualified: bool | None = None
            if settlement is not None:
                if (
                    settlement.get("version") != 1
                    or settlement.get("attempt_id") != "task_execution:change"
                    or settlement.get("operation_id") != "runtime:task_execution:change"
                    or settlement.get("handle") is not None
                    or settlement.get("runtime_fence") is not None
                    or type(settlement.get("cancellation_fence")) is not int
                    or type(settlement.get("transport_succeeded")) is not bool
                    or type(settlement.get("result_reconciled")) is not bool
                    or (
                        settlement.get("owned_group_settled") is not None
                        and type(settlement.get("owned_group_settled")) is not bool
                    )
                    or settlement.get("descendants") != "unproven"
                ):
                    raise ValueError("invalid original pre-Attempt settlement")
                group = settlement.get("owned_group_reference")
                expected_group = settlement.get("expected_group_reference")
                qualified = bool(
                    runtime is None
                    and pre_attempt.status == "accepted"
                    and settlement.get("cancellation_fence") == pre_attempt.fence
                    and type(group) is str
                    and bool(group)
                    and group == expected_group
                    and settlement.get("owned_group_settled") is True
                    and settlement.get("result_reconciled") is True
                    and settlement.get("transport_succeeded") is True
                )
            cancellations.append(
                {
                    "driver": "bounded-task-pre-attempt",
                    "attempt": None,
                    "planned_attempt": ref("attempt", "task_execution:change"),
                    "operation": ref("operation", pre_attempt.operation_id),
                    "runtime_operation": ref("operation", "runtime:task_execution:change"),
                    "runtime_operation_recorded": runtime is not None,
                    "no_dispatch_observed": runtime is None,
                    "request_recorded": True,
                    "operation_status": pre_attempt.status,
                    "cancellation_fence": pre_attempt.fence,
                    "settlement_recorded": settlement_data is not None,
                    "settlement_evidence": None
                    if settlement_data is None
                    else ref("artifact", "sha256:" + hashlib.sha256(settlement_data).hexdigest()),
                    "settlement_fence": None if settlement is None else settlement.get("cancellation_fence"),
                    "owned_group": None
                    if settlement is None
                    else ref("runtime-group", settlement.get("owned_group_reference")),
                    "expected_group": None
                    if settlement is None
                    else ref("runtime-group", settlement.get("expected_group_reference")),
                    "reported_owned_group_settled": None
                    if settlement is None
                    else settlement.get("owned_group_settled"),
                    "result_reconciled": None if settlement is None else settlement.get("result_reconciled"),
                    "transport_succeeded": None if settlement is None else settlement.get("transport_succeeded"),
                    "qualified_original_no_dispatch_group": qualified,
                    "current_activity": None,
                    "physical_settlement": None,
                    "descendants": "unproven",
                }
            )
    for attempt in program.attempts:
        operation_id = f"ordinary-cancel:{attempt.attempt_id}"
        operation = store.find_operation(operation_id)
        if operation is None:
            continue
        request = _mapping(_mapping(json.loads(operation.request_json)).get("request"))
        original = original_claims.get(attempt.attempt_id)
        if (
            request.get("kind") != "ordinary-cancellation"
            or original is None
            or request.get("claim") != json.loads(json.dumps(original[0]))
            or original[0].get("mode") != "managed"
        ):
            raise ValueError("cancellation differs from original managed claim")
        settlement_data = store.find_artifact(operation_id, "settlement")
        group = None
        if settlement_data is not None:
            settlement = _mapping(json.loads(settlement_data))
            stop = [item for item in original[1] if item.get("stage") == "stop"]
            if (
                set(settlement) != {"claim", "profile", "group"}
                or settlement.get("claim") != request.get("claim")
                or settlement.get("profile") != original[0].get("profile")
                or len(stop) != 1
                or stop[0].get("reference") != settlement.get("group")
            ):
                raise ValueError("cancellation settlement differs from original target")
            group = settlement.get("group")
        cancellations.append(
            {
                "attempt": ref("attempt", attempt.attempt_id),
                "claim": ref("claim", original[0].get("command_key")),
                "operation": ref("operation", operation_id),
                "request_recorded": True,
                "reason": ref("evidence", request.get("reason")),
                "settlement_recorded": settlement_data is not None,
                "settled_original_group": ref("runtime-group", group),
                "terminal_observed": None,
                "physical_settlement": None,
            }
        )
    for attempt in program.attempts:
        operation_id = f"cancel:{attempt.attempt_id}"
        cancellation = store.find_operation(operation_id)
        if cancellation is None:
            continue
        runtime_id = f"runtime:{attempt.attempt_id}"
        runtime = store.find_operation(runtime_id)
        request = _mapping(_mapping(json.loads(cancellation.request_json)).get("request"))
        if cancellation.effect_key != operation_id or request.get("operation") != runtime_id:
            raise ValueError("independent cancellation differs from original runtime")
        handle = None if runtime is None else runtime.accepted_reference
        if request.get("handle") is not None and request.get("handle") != handle:
            raise ValueError("independent cancellation differs from original runtime handle")
        receipt_data = None if runtime is None else store.find_artifact(runtime_id, "runtime-receipt")
        receipt_handle = None
        receipt_fence = None
        expected_group = None
        if receipt_data is not None:
            try:
                receipt_handle, receipt_fence, _, expected_group = decode_runtime_receipt_with_group(receipt_data)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        receipt_bound = (
            runtime is not None and receipt_handle == handle and receipt_fence == runtime.fence and handle is not None
        )
        evidence = store.outcome_effect_evidence(program_id, operation_id)
        runtime_observations: list[dict[str, object]] = []
        for metadata in store.outcome_artifact_metadata(program_id, operation_id, "observation:"):
            name, checksum = metadata.get("name"), metadata.get("digest")
            if type(name) is not str or type(checksum) is not str or name != "observation:" + checksum:
                raise ValueError("invalid original cancellation observation identity")
            raw = store.find_artifact(operation_id, name)
            if raw is None or hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError("missing original cancellation observation")
            value = _mapping(json.loads(raw))
            if (
                value.get("activity") not in {"running", "waiting", "terminal", "unknown"}
                or type(value.get("handle")) is not str
                or type(value.get("observed_at")) is not int
                or type(value.get("fresh_until")) is not int
            ):
                raise ValueError("invalid original cancellation observation")
            runtime_observations.append(
                {
                    "record": ref("artifact", "sha256:" + checksum),
                    "handle": ref("runtime-handle", value.get("handle")),
                    "matches_original_handle": handle is not None and value.get("handle") == handle,
                    "activity": value["activity"],
                    "observed_at": value.get("observed_at"),
                    "fresh_until": value.get("fresh_until"),
                    "current_activity": None,
                }
            )
        proof_records: list[dict[str, object]] = []
        named: list[tuple[str, bytes]] = []
        for metadata in store.outcome_artifact_metadata(program_id, operation_id, "owned-observation:"):
            name, checksum = metadata.get("name"), metadata.get("digest")
            if type(name) is not str or type(checksum) is not str or name != "owned-observation:" + checksum:
                raise ValueError("invalid original cancellation proof identity")
            data = store.find_artifact(operation_id, name)
            if data is None or hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError("missing original cancellation proof")
            named.append(("sha256:" + checksum, data))
        settlement = store.find_artifact(operation_id, "owned-group-settlement")
        if settlement is not None:
            settlement_reference = "sha256:" + hashlib.sha256(settlement).hexdigest()
            if all(reference != settlement_reference for reference, _ in named):
                named.append((settlement_reference, settlement))
        for reference, data in named:
            proof = _mapping(json.loads(data))
            if proof.get("version") != 1:
                raise ValueError("unsupported original cancellation proof")
            if "attempt_id" in proof:
                proof_bound = (
                    proof.get("attempt_id") == attempt.attempt_id
                    and proof.get("operation_id") == runtime_id
                    and proof.get("handle") == handle
                    and proof.get("runtime_fence") == (None if runtime is None else runtime.fence)
                    and proof.get("cancellation_fence") == cancellation.fence
                )
                group = proof.get("owned_group_reference")
                group_bound = (
                    expected_group is not None
                    and group == expected_group
                    and proof.get("expected_group_reference") == expected_group
                )
                transport = proof.get("transport_succeeded")
            else:
                proof_bound = (
                    runtime is not None
                    and proof.get("attempt") == attempt.spec.digest
                    and proof.get("operation") == runtime_id
                    and proof.get("effect_key") == runtime.effect_key
                    and proof.get("request_digest") == runtime.request_digest
                    and proof.get("runtime_fence") == runtime.fence
                    and proof.get("cancellation_fence") == cancellation.fence
                    and proof.get("handle") == handle
                )
                group = proof.get("owned_group_reference")
                group_bound = expected_group is not None and group == expected_group
                transport = None
            settled, reconciled = proof.get("owned_group_settled"), proof.get("result_reconciled")
            if (
                (settled is not None and type(settled) is not bool)
                or type(reconciled) is not bool
                or (transport is not None and type(transport) is not bool)
                or proof.get("descendants") != "unproven"
            ):
                raise ValueError("invalid original cancellation settlement")
            qualified = bool(
                proof_bound
                and receipt_bound
                and group_bound
                and settled is True
                and reconciled is True
                and (transport is not False)
            )
            proof_records.append(
                {
                    "record": ref("artifact", reference),
                    "binding_verified": bool(proof_bound and receipt_bound),
                    "owned_group": ref("runtime-group", group),
                    "original_group_matches": group_bound,
                    "reported_owned_group_settled": settled,
                    "result_reconciled": reconciled,
                    "transport_succeeded": transport,
                    "qualified_original_group": qualified,
                    "descendants": "unproven",
                    "current_activity": None,
                }
            )
        cancellations.append(
            {
                "driver": "independent-runtime-cancellation",
                "attempt": ref("attempt", attempt.attempt_id),
                "operation": ref("operation", operation_id),
                "runtime_operation": ref("operation", runtime_id),
                "runtime_fence": None if runtime is None else runtime.fence,
                "cancellation_fence": cancellation.fence,
                "handle": ref("runtime-handle", handle),
                "request_recorded": True,
                "runtime_receipt_bound": receipt_bound,
                "settlement_recorded": settlement is not None,
                "operation_status": cancellation.status,
                "deliveries": [
                    {"fence": row["fence"], "claimed_at": row["claimed_at"], "lease_until": row["lease_until"]}
                    for row in _items(evidence.get("deliveries"))
                ],
                "transports": [
                    {"fence": row["fence"], "succeeded": bool(row["succeeded"])}
                    for row in _items(evidence.get("transports"))
                ],
                "operation_observations": [
                    {
                        "id": ref("effect-observation", row["observation_id"]),
                        "fence": row["fence"],
                        "kind": row["kind"],
                        "reference": ref("effect-receipt", row["reference"]),
                    }
                    for row in _items(evidence.get("observations"))
                ],
                "runtime_observations": runtime_observations,
                "settlement_proofs": proof_records,
                "current_activity": None,
                "physical_settlement": None,
            }
        )
    if len(claims) != count or store.served_change_token() != token:
        raise ValueError("durable outcome changed during export")
    deduplicated_checks: dict[str, dict[str, object]] = {}
    for observation in checks:
        comparable = {key: value for key, value in observation.items() if key not in {"manifest", "operation"}}
        identity = json.dumps(comparable, sort_keys=True, separators=(",", ":"))
        current = deduplicated_checks.setdefault(identity, {**comparable, "manifests": [], "operations": []})
        for source, field in (("manifest", "manifests"), ("operation", "operations")):
            value = observation.get(source)
            values = cast(list[str], current[field])
            if type(value) is str and value not in values:
                values.append(value)
    checks = list(deduplicated_checks.values())
    for attempt in attempts:
        original = attempt["remediation_of"]
        linked = {
            (cast(str, finding["id"]), cast(str, finding["fingerprint"]))
            for finding in findings
            if finding.get("attempt") == original
        }
        attempt["remediation_findings"] = (
            [{"id": identity, "fingerprint": fingerprint} for identity, fingerprint in sorted(linked)]
            if original is not None and linked
            else None
        )
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
        "native_subjects": native_subject_output,
        "native_effects": native_effects,
        "reviews": reviews,
        "checks": checks,
        "check_results": check_results,
        "verification_manifests": manifests,
        "candidates": candidates,
        "findings": findings,
        "decisions": decisions,
        "cancellations": cancellations,
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
            "original_deadline": None
            if deadline is None
            else {
                "wall_deadline": deadline.wall_deadline,
                "wall_anchor": deadline.wall_anchor,
                "boot_anchor_ns": deadline.boot_anchor_ns,
                "boot_deadline_ns": deadline.boot_deadline_ns,
                "boot_id": ref("boot", deadline.boot_id),
                "clock": deadline.clock,
            },
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
            "work_revocation_recorded": revocation is not None,
            "revocation_evidence": None if revocation is None else ref("evidence", revocation.get("evidence")),
            "strict_inference_profile": None if budget_policy is None else budget_policy.strict_inference,
            "hard_inference_cap": budget.get("hard_inference_cap") if configured else None,
            "effect_inventory": "recorded-reservations-only" if configured else "unknown-budget-not-bound",
            "review_inventory": "recorded-review-reservations-only" if configured else "unknown-budget-not-bound",
            "external_client_usage": None,
            "route_and_call_identity": None,
            "complete_cost": None,
            "usage_total_scope": "per-source-known-subtotal-not-lineage-or-cross-source-total",
            "unattributed_usage_observations": sum(item.get("attempt_id") not in attempt_by_id for item in history),
        },
    }
