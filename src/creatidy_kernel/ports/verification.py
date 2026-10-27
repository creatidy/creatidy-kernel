# SPDX-License-Identifier: Apache-2.0
"""Verification-only trusted caller seam, not worker or remote authentication.

The controller supplies the recipe, resources and authorizer outside candidate
data. A snapshot contains only explicitly collected file bytes, never a checkout
directory (which could expose .git, untracked secrets, mounts or sockets).
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.verification import (
    CheckProducer,
    Evidence,
    EvidenceSubject,
    Finding,
    ProductReviewReceipt,
    ProductReviewRequest,
    ProductReviewVerdict,
    product_review_matches_request,
    validate_product_review,
)
from creatidy_kernel.core.work_budget import WorkKind
from creatidy_kernel.ports.program_store import ApplicationStore, OperationRecord


class VerificationProfile(StrEnum):
    TRUSTED_DEVELOPMENT = "trusted_development"  # Non-isolated; never auto-upgraded.
    LINUX_BUBBLEWRAP = "linux_bubblewrap_verification_v1"


@dataclass(frozen=True, slots=True)
class VerificationResource:
    source: Path
    destination: PurePosixPath
    sha256: str


@dataclass(frozen=True, slots=True)
class VerificationSnapshot:
    files: tuple[tuple[str, bytes], ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.files) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not bytes
            for item in self.files
        ):
            raise ValueError("immutable snapshot file bytes required")

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for path, content in self.files:
            digest.update(path.encode() + b"\0" + hashlib.sha256(content).digest())
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationInvocation:
    subject: EvidenceSubject
    recipe: tuple[str, ...]
    resources: tuple[VerificationResource, ...]
    snapshot_digest: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        if (
            type(self.subject) is not EvidenceSubject
            or type(self.recipe) is not tuple
            or any(type(arg) is not str for arg in self.recipe)
            or type(self.resources) is not tuple
            or any(type(resource) is not VerificationResource for resource in self.resources)
            or type(self.snapshot_digest) is not str
            or type(self.timeout_seconds) is not int
        ):
            raise ValueError("immutable verification invocation required")


# Implemented by an authenticated trusted controller, not a caller-name check.
# Must check exact subject/spec/Attempt/repo/base/head, recipe/resources,
# expiry and revocation at every call. No grant can add mounts or network.
VerificationAuthorizer = Callable[[VerificationInvocation, int], bool]


class ReviewLookupStatus(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReviewLookup:
    status: ReviewLookupStatus
    receipt: ProductReviewReceipt | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not ReviewLookupStatus or (
            (self.status is ReviewLookupStatus.FOUND) != (type(self.receipt) is ProductReviewReceipt)
        ):
            raise ValueError("review lookup requires an explicit status and matching receipt")


class ProductReviewDispatcher(Protocol):
    """Cold reviewer transport with exact-operation reconciliation."""

    def dispatch(
        self, operation: OperationKey, request: ProductReviewRequest, *, fence: int
    ) -> ProductReviewReceipt: ...

    def reconcile(self, operation: OperationKey, request_digest: str) -> ReviewLookup: ...


class ProductReviewStatus(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENT = "comment"
    OWNER_DECISION_NEEDED = "owner_decision_needed"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProductReviewResult:
    status: ProductReviewStatus
    operation: OperationRecord
    receipt: ProductReviewReceipt | None
    evidence: Evidence | None


def _subject_data(subject: EvidenceSubject) -> dict[str, str]:
    return {
        "attempt_digest": subject.attempt_digest,
        "base_revision": subject.base_revision,
        "candidate_digest": subject.candidate_digest,
        "head_revision": subject.head_revision,
        "policy_digest": subject.policy_digest,
        "repository": subject.repository,
        "spec_digest": subject.spec_digest,
    }


def _review_request_bytes(request: ProductReviewRequest) -> bytes:
    return json.dumps(
        {
            "author_actor_id": request.author_actor_id,
            "author_attempt_id": request.author_attempt_id,
            "candidate_digest": request.candidate.digest,
            "input_digest": request.input_digest,
            "input_reference": request.input_reference,
            "policy": request.policy.payload(),
            "subject": _subject_data(request.subject),
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def product_review_operation_id(request: ProductReviewRequest) -> str:
    """Stable operation identity for one exact candidate/review-input pair."""
    return f"product-review:{hashlib.sha256(_review_request_bytes(request)).hexdigest()}"


def _receipt_bytes(receipt: ProductReviewReceipt) -> bytes:
    return json.dumps(
        {
            "findings": [
                {
                    "finding_id": item.finding_id,
                    "fingerprint": item.fingerprint,
                    "reason": item.reason,
                    "subject": _subject_data(item.subject),
                }
                for item in receipt.findings
            ],
            "fresh_until": receipt.fresh_until,
            "observed_at": receipt.observed_at,
            "owner_decision_options": receipt.owner_decision_options,
            "owner_decision_reason": receipt.owner_decision_reason,
            "owner_decision_reference": receipt.owner_decision_reference,
            "request_digest": receipt.request_digest,
            "review_id": receipt.review_id,
            "reviewer_id": receipt.reviewer_id,
            "reviewer_session": receipt.reviewer_session,
            "subject": _subject_data(receipt.subject),
            "verdict": receipt.verdict.value,
            "version": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _decode_subject(value: object) -> EvidenceSubject:
    fields = cast(dict[str, object], value) if type(value) is dict else {}
    expected = {
        "attempt_digest",
        "base_revision",
        "candidate_digest",
        "head_revision",
        "policy_digest",
        "repository",
        "spec_digest",
    }
    if set(fields) != expected or any(type(item) is not str for item in fields.values()):
        raise ValueError("invalid review subject")
    return EvidenceSubject(
        cast(str, fields["candidate_digest"]),
        cast(str, fields["attempt_digest"]),
        cast(str, fields["spec_digest"]),
        cast(str, fields["policy_digest"]),
        cast(str, fields["repository"]),
        cast(str, fields["base_revision"]),
        cast(str, fields["head_revision"]),
    )


def _decode_receipt(data: bytes) -> ProductReviewReceipt:
    value: object = json.loads(data)
    expected = {
        "findings",
        "fresh_until",
        "observed_at",
        "owner_decision_options",
        "owner_decision_reason",
        "owner_decision_reference",
        "request_digest",
        "review_id",
        "reviewer_id",
        "reviewer_session",
        "subject",
        "verdict",
        "version",
    }
    if type(value) is not dict:
        raise ValueError("invalid product review receipt record")
    value_fields: dict[str, object] = cast(dict[str, object], value)
    if set(value_fields) == expected and value_fields["version"] == 2:
        pass
    elif (
        set(value_fields)
        == expected
        - {
            "owner_decision_options",
            "owner_decision_reason",
            "owner_decision_reference",
        }
        and value_fields["version"] == 1
    ):
        value_fields = {
            **value_fields,
            "owner_decision_options": [],
            "owner_decision_reason": None,
            "owner_decision_reference": None,
        }
    else:
        raise ValueError("invalid product review receipt record")
    raw = value_fields
    if type(raw["findings"]) is not list:
        raise ValueError("invalid product review findings")
    findings: list[Finding] = []
    for item in cast(list[object], raw["findings"]):
        record = cast(dict[str, object], item) if type(item) is dict else {}
        if set(record) != {"finding_id", "fingerprint", "reason", "subject"} or any(
            type(record[name]) is not str for name in ("finding_id", "fingerprint", "reason")
        ):
            raise ValueError("invalid product review finding")
        findings.append(
            Finding(
                cast(str, record["finding_id"]),
                cast(str, record["fingerprint"]),
                _decode_subject(record["subject"]),
                cast(str, record["reason"]),
            )
        )
    if any(type(raw[name]) is not str for name in ("request_digest", "review_id", "reviewer_id", "reviewer_session")):
        raise ValueError("invalid product review receipt identity")
    if type(raw["observed_at"]) is not int or type(raw["fresh_until"]) is not int:
        raise ValueError("invalid product review receipt time")
    raw_options = raw["owner_decision_options"]
    if type(raw_options) is not list or any(type(item) is not str for item in cast(list[object], raw_options)):
        raise ValueError("invalid owner decision options")
    return ProductReviewReceipt(
        cast(str, raw["review_id"]),
        cast(str, raw["request_digest"]),
        _decode_subject(raw["subject"]),
        cast(str, raw["reviewer_id"]),
        cast(str, raw["reviewer_session"]),
        ProductReviewVerdict(cast(str, raw["verdict"])),
        tuple(findings),
        raw["observed_at"],
        raw["fresh_until"],
        cast(str | None, raw["owner_decision_reference"]),
        cast(str | None, raw["owner_decision_reason"]),
        tuple(cast(list[str], raw_options)),
    )


def _review_outcome(
    request: ProductReviewRequest, receipt: ProductReviewReceipt
) -> tuple[ProductReviewStatus, Evidence | None]:
    if not product_review_matches_request(request, receipt):
        return ProductReviewStatus.INVALID, None
    evidence = validate_product_review(request, receipt)
    if evidence is not None:
        status = ProductReviewStatus.APPROVED
    elif receipt.verdict is ProductReviewVerdict.APPROVE:
        status = ProductReviewStatus.INVALID
    elif receipt.verdict is ProductReviewVerdict.REQUEST_CHANGES:
        status = ProductReviewStatus.CHANGES_REQUESTED
    elif receipt.verdict is ProductReviewVerdict.OWNER_DECISION_NEEDED:
        status = ProductReviewStatus.OWNER_DECISION_NEEDED
    else:
        status = ProductReviewStatus.COMMENT
    return status, evidence


def _validate_review_receipt(request: ProductReviewRequest, receipt: ProductReviewReceipt) -> None:
    if type(receipt) is not ProductReviewReceipt or (
        receipt.request_digest != request.input_digest or receipt.subject != request.subject
    ):
        raise ValueError("product review receipt differs from the exact request subject")


def dispatch_product_review(
    store: ApplicationStore,
    request: ProductReviewRequest,
    dispatcher: ProductReviewDispatcher,
    *,
    now: int,
    lease_seconds: int = 60,
) -> ProductReviewResult:
    """Persist review intent and receipt; reconcile uncertain delivery before retry."""
    request_bytes = _review_request_bytes(request)
    operation_id = product_review_operation_id(request)
    program_id = store.work_program_for_attempt(request.author_attempt_id)
    program = store.load(program_id)
    if (
        request.subject.attempt_digest != program.attempt(request.author_attempt_id).spec.digest
        or request.subject.spec_digest != program.spec.digest
    ):
        raise ValueError("product review differs from current exact Attempt/specification")
    operation = store.budgeted_intent(
        program_id,
        request.author_attempt_id,
        WorkKind.REVIEW,
        operation_id,
        operation_id,
        {
            "candidate": request.candidate.digest,
            "input": request.input_digest,
            "kind": "product-review",
            "subject": _subject_data(request.subject),
        },
    )
    stored_request = store.find_artifact(operation_id, "review-request")
    if stored_request is None:
        store.finalize_artifact(operation_id, "review-request", request_bytes)
    elif stored_request != request_bytes:
        raise ValueError("product review request differs from its immutable intent")
    receipt_bytes = store.find_artifact(operation_id, "review-receipt")
    operation = store.operation(operation_id)
    if receipt_bytes is not None:
        receipt = _decode_receipt(receipt_bytes)
        _validate_review_receipt(request, receipt)
        if operation.status in {"dispatched", "unknown"}:
            operation = store.observe(
                operation_id,
                operation.fence,
                f"product-review-recovered:{hashlib.sha256(receipt_bytes).hexdigest()}",
                "accepted",
                reference=f"sha256:{hashlib.sha256(receipt_bytes).hexdigest()}",
            )
        status, evidence = _review_outcome(request, receipt)
        store.record_work_settlement(operation_id, f"sha256:{hashlib.sha256(receipt_bytes).hexdigest()}")
        return ProductReviewResult(status, operation, receipt, evidence)

    key = OperationKey(operation_id, operation.effect_key, operation.request_digest)
    if operation.status in {"dispatched", "unknown"}:
        if operation.lease_until is not None and operation.lease_until > now:
            return ProductReviewResult(ProductReviewStatus.UNKNOWN, operation, None, None)
        lookup = dispatcher.reconcile(key, operation.request_digest)
        if lookup.status is ReviewLookupStatus.UNKNOWN:
            operation = store.reconcile(
                operation_id,
                operation.fence,
                "unknown",
                now=now,
                evidence=f"product-review-lookup:{now}:unknown",
            )
            return ProductReviewResult(ProductReviewStatus.UNKNOWN, operation, None, None)
        if lookup.status is ReviewLookupStatus.FOUND:
            receipt = cast(ProductReviewReceipt, lookup.receipt)
            _validate_review_receipt(request, receipt)
            receipt_data = _receipt_bytes(receipt)
            reference = store.finalize_artifact(operation_id, "review-receipt", receipt_data)
            operation = store.reconcile(
                operation_id,
                operation.fence,
                "found",
                now=now,
                evidence=f"product-review-lookup:{now}:found",
                reference=reference,
                matched_digest=operation.request_digest,
            )
            status, evidence = _review_outcome(request, receipt)
            store.record_work_settlement(operation_id, reference)
            return ProductReviewResult(status, operation, receipt, evidence)
        store.reconcile(
            operation_id,
            operation.fence,
            "absent",
            now=now,
            evidence=f"product-review-lookup:{now}:absent",
            matched_digest=operation.request_digest,
            authoritative_absence=True,
        )

    fence = store.claim(operation_id, now=now, lease_seconds=lease_seconds)
    operation = store.operation(operation_id)
    key = OperationKey(operation_id, operation.effect_key, operation.request_digest)
    try:
        receipt = dispatcher.dispatch(key, request, fence=fence)
    except Exception:
        store.record_transport(operation_id, fence, False)
        operation = store.observe(operation_id, fence, f"product-review-unknown:{fence}", "unknown")
        return ProductReviewResult(ProductReviewStatus.UNKNOWN, operation, None, None)
    _validate_review_receipt(request, receipt)
    store.record_transport(operation_id, fence, True)
    receipt_data = _receipt_bytes(receipt)
    reference = store.finalize_artifact(operation_id, "review-receipt", receipt_data)
    operation = store.observe(
        operation_id,
        fence,
        f"product-review:{hashlib.sha256(receipt_data).hexdigest()}",
        "accepted",
        reference=reference,
    )
    status, evidence = _review_outcome(request, receipt)
    store.record_work_settlement(operation_id, reference)
    return ProductReviewResult(status, operation, receipt, evidence)


class DurableProductReviewChecks:
    """Add one durable independent review producer to the deterministic checks."""

    def __init__(
        self,
        store: ApplicationStore,
        checks: CheckProducer,
        request_for_subject: Callable[[EvidenceSubject], ProductReviewRequest],
        dispatcher: ProductReviewDispatcher,
        *,
        clock: Callable[[], int],
        lease_seconds: int = 60,
    ) -> None:
        self.store = store
        self.checks = checks
        self.request_for_subject = request_for_subject
        self.dispatcher = dispatcher
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.last_result: ProductReviewResult | None = None

    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        if name != "independent_review":
            return self.checks.check(name, subject)
        request = self.request_for_subject(subject)
        if request.subject != subject:
            return None
        now = self.clock()
        if type(now) is not int:
            raise ValueError("trusted review decision time must be an integer")
        result = dispatch_product_review(
            self.store,
            request,
            self.dispatcher,
            now=now,
            lease_seconds=self.lease_seconds,
        )
        self.last_result = result
        return result.evidence
