# SPDX-License-Identifier: Apache-2.0
"""Durable candidate-bound product review transport and exact-subject rules."""

import json
import time
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest

from creatidy_kernel.adapters.reference import POLICY as REFERENCE_POLICY
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AuthorityEnvelope,
    BudgetPolicy,
    FinishAttempt,
    PolicyReference,
    PrepareAttempt,
    Program,
    ProgramSpec,
    StartAttempt,
)
from creatidy_kernel.core.execution import (
    Activity,
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionRequest,
    Lookup,
    OperationKey,
    Presence,
    RuntimeObservation,
    TrustMode,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.resources import Allocation, ResourceRequest
from creatidy_kernel.core.verification import (
    CandidateResult,
    Evidence,
    EvidenceSubject,
    Finding,
    ProductReviewReceipt,
    ProductReviewRequest,
    ProductReviewVerdict,
    VerificationPolicy,
    validate_product_review,
)
from creatidy_kernel.core.work_budget import WorkBudgetPolicy
from creatidy_kernel.ports.application import Collection, advance_work_unit
from creatidy_kernel.ports.verification import (
    ProductReviewStatus,
    ReviewLookup,
    ReviewLookupStatus,
)
from creatidy_kernel.ports.verification import (
    dispatch_product_review as _dispatch_product_review,
)

pytest_plugins = ["test_sqlite_store"]

POLICY = PolicyReference("product-review", "v1", "sha256:policy")


def configure_budget(store: SQLiteProgramStore, program: Program) -> None:
    budget = WorkBudgetPolicy.basic(
        program.spec.budget.max_attempts,
        program.spec.budget.max_active_attempts,
        verifications=program.spec.budget.max_attempts * 2,
        retained_bytes=65536,
    )
    store.bind_work_budget(
        program.program_id,
        "fixture-review",
        budget,
        budget,
        store.original_work_deadline(program.program_id) or int(time.time()) + 3600,
    )


def dispatch_product_review(
    store: SQLiteProgramStore,
    review: ProductReviewRequest,
    dispatcher: "Dispatcher",
    *,
    now: int,
    lease_seconds: int = 60,
):
    program = store.find_program("reference") or store.create(reference_spec(), "create")
    configure_budget(store, program)
    if not program.attempts:
        program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
        attempt = AttemptSpec(
            "a1",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
        )
        store.admit_with_intent(
            program.program_id,
            "prepare:a1",
            PrepareAttempt(program.revision, "worker", attempt),
            "runtime:a1",
            "runtime:a1",
            {"attempt": attempt.digest},
        )
        program = store.load(program.program_id)
        program = store.admit(program.program_id, "start:a1", StartAttempt(program.revision, "worker", "a1"))
        store.admit(program.program_id, "finish:a1", FinishAttempt(program.revision, "worker", "a1"))
    return _dispatch_product_review(store, review, dispatcher, now=now, lease_seconds=lease_seconds)


def request() -> ProductReviewRequest:
    program = Program.create(reference_spec())
    attempt = AttemptSpec(
        "a1", program.program_id, "first", program.spec.revision, program.spec.digest, program.spec.initial_inputs
    )
    manifest = ArtifactManifest("workspace:a1", (Artifact("result.txt", "sha256:output"),))
    candidate = CandidateResult("candidate:a1", Candidate("a1", attempt.digest, manifest), (("output", "result.txt"),))
    subject = EvidenceSubject(
        candidate.digest, attempt.digest, program.spec.digest, POLICY.digest, "repo", "base", "head"
    )
    return ProductReviewRequest(candidate, subject, "a1", "worker", POLICY)


class Dispatcher:
    def __init__(self, verdict: ProductReviewVerdict = ProductReviewVerdict.APPROVE) -> None:
        self.verdict = verdict
        self.calls = 0
        self.receipt: ProductReviewReceipt | None = None
        self.fail_once = False

    def dispatch(self, operation: OperationKey, request: ProductReviewRequest, *, fence: int) -> ProductReviewReceipt:
        self.calls += 1
        assert operation.operation_id.startswith("product-review:")
        assert fence > 0
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("synthetic lost review reply")
        findings = (
            (Finding("finding-1", "fingerprint-1", request.subject, "missing regression"),)
            if self.verdict is ProductReviewVerdict.REQUEST_CHANGES
            else ()
        )
        self.receipt = ProductReviewReceipt(
            "review-1",
            request.input_digest,
            request.subject,
            "reviewer",
            "cold-session-1",
            self.verdict,
            findings,
            10,
            100,
            "policy-choice" if self.verdict is ProductReviewVerdict.OWNER_DECISION_NEEDED else None,
            "Accepted policy leaves the candidate acceptance threshold unresolved."
            if self.verdict is ProductReviewVerdict.OWNER_DECISION_NEEDED
            else None,
            ("retain existing scope", "expand acceptance scope")
            if self.verdict is ProductReviewVerdict.OWNER_DECISION_NEEDED
            else (),
        )
        return self.receipt

    def reconcile(self, operation: OperationKey, request_digest: str) -> ReviewLookup:
        assert operation.operation_id.startswith("product-review:")
        if self.receipt is None:
            return ReviewLookup(ReviewLookupStatus.ABSENT)
        return ReviewLookup(ReviewLookupStatus.FOUND, self.receipt)


class AlternatingDispatcher(Dispatcher):
    def dispatch(self, operation: OperationKey, request: ProductReviewRequest, *, fence: int) -> ProductReviewReceipt:
        self.verdict = ProductReviewVerdict.REQUEST_CHANGES if self.calls == 0 else ProductReviewVerdict.APPROVE
        return super().dispatch(operation, request, fence=fence)


class LostReplyAfterDispatch(Dispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.lose_once = True

    def dispatch(self, operation: OperationKey, request: ProductReviewRequest, *, fence: int) -> ProductReviewReceipt:
        receipt = super().dispatch(operation, request, fence=fence)
        if self.lose_once:
            self.lose_once = False
            raise ConnectionError("synthetic reply lost after remote review completed")
        return receipt


class SelfReviewDispatcher(Dispatcher):
    def dispatch(self, operation: OperationKey, request: ProductReviewRequest, *, fence: int) -> ProductReviewReceipt:
        return replace(super().dispatch(operation, request, fence=fence), reviewer_id=request.author_actor_id)


class ReviewRuntime:
    def __init__(self) -> None:
        self.requests: dict[str, ExecutionRequest] = {}

    def start(self, request: ExecutionRequest) -> str:
        handle = f"handle:{request.attempt.attempt_id}"
        self.requests[handle] = request
        return handle

    def observe(self, handle: str, *, now: int) -> RuntimeObservation:
        request = self.requests[handle]
        identity = replace(
            request.identity,
            resolved=request.identity.requested,
            observed=request.identity.requested,
            resolved_provider=request.identity.requested_provider,
            observed_provider=request.identity.requested_provider,
            resolved_effort=request.identity.requested_effort,
            observed_effort=request.identity.requested_effort,
        )
        return RuntimeObservation(handle, Activity.TERMINAL, now, now + 100, identity, False)

    def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup:
        return Lookup(Presence.FOUND, handle or f"handle:{operation.operation_id.removeprefix('runtime:')}")

    def candidate(self, handle: str) -> Candidate:
        request = self.requests[handle]
        content = f"candidate for {request.attempt.attempt_id}\n".encode()
        self.content[handle] = content
        artifact = Artifact("result.txt", "sha256:" + sha256(content).hexdigest())
        return Candidate(
            request.attempt.attempt_id,
            request.attempt.digest,
            ArtifactManifest(request.workspace.key, (artifact,)),
        )

    def cancel(self, handle: str) -> RuntimeObservation:
        return self.observe(handle, now=10)

    content: dict[str, bytes] = {}


class ExactCheck:
    def check(self, name: str, subject: EvidenceSubject) -> Evidence:
        return Evidence(
            f"{subject.candidate_digest}:{name}",
            name,
            subject,
            "synthetic-checker",
            "fixture:v1",
            "sha256:check",
            10,
            100,
            True,
        )


class CandidateCollector:
    def collect(self, request: ExecutionRequest, candidate: Candidate, *, now: int) -> Collection:
        del now
        return Collection(
            candidate.artifacts,
            "fixture-repository",
            "base-revision",
            f"head:{request.attempt.attempt_id}",
            ExactCheck(),
        )


class FixedAllocator:
    def select(self, request: ResourceRequest) -> Allocation:
        del request
        return Allocation("fixture", "provider", "model", frozenset({"reference"}), 128, "synthetic")


def _advance_review(
    store: SQLiteProgramStore,
    program: Program,
    reviewer: Dispatcher,
    runtime: ReviewRuntime,
    workspace: WorkspaceHandle,
    *,
    remediation_of: str | None = None,
) -> str:
    configure_budget(store, program)
    return advance_work_unit(
        store,
        program,
        "first",
        allocator=FixedAllocator(),
        runtime=runtime,
        workspace=workspace,
        collector=CandidateCollector(),
        policy=VerificationPolicy(REFERENCE_POLICY.reference, ("exact-content",), True),
        now=10,
        product_reviewer=reviewer,
        remediation_of=remediation_of,
    )


def test_review_receipt_requires_independent_fresh_exact_subject() -> None:
    review = request()
    receipt = Dispatcher().dispatch(OperationKey("product-review:fixture", "review", "a" * 64), review, fence=1)
    assert validate_product_review(review, receipt) is not None
    moved = replace(receipt, subject=replace(receipt.subject, head_revision="moved"))
    assert validate_product_review(review, moved) is None
    assert validate_product_review(review, replace(receipt, reviewer_id="worker")) is None
    assert validate_product_review(review, replace(receipt, reviewer_session="a1")) is None
    assert validate_product_review(review, replace(receipt, verdict=ProductReviewVerdict.COMMENT)) is None


def test_cold_review_input_is_candidate_and_subject_bound() -> None:
    review = request()
    moved = replace(review, subject=replace(review.subject, head_revision="moved"))
    assert review.input_reference == f"candidate:{review.candidate.digest}"
    assert moved.input_reference == review.input_reference
    assert moved.input_digest != review.input_digest


def test_receipt_codec_reads_prior_schema_without_losing_approval(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewer = Dispatcher()
    review = request()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        initial = dispatch_product_review(store, review, reviewer, now=10)
        assert initial.receipt is not None
        legacy = asdict(initial.receipt)
        legacy["verdict"] = initial.receipt.verdict.value
        legacy["version"] = 1
        del legacy["owner_decision_options"]
        del legacy["owner_decision_reason"]
        del legacy["owner_decision_reference"]
        encoded_legacy = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        find_artifact = store.find_artifact

        def read_legacy_receipt(operation_id: str, name: str) -> bytes | None:
            if name == "review-receipt":
                return encoded_legacy
            return find_artifact(operation_id, name)

        monkeypatch.setattr(store, "find_artifact", read_legacy_receipt)
        replay = dispatch_product_review(store, review, reviewer, now=11)
        assert replay.receipt == initial.receipt
        assert replay.status is ProductReviewStatus.APPROVED


def test_review_intent_and_receipt_recover_without_duplicate_dispatch(sqlite_tmp_path: Path) -> None:
    reviewer = Dispatcher()
    review = request()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        first = dispatch_product_review(store, review, reviewer, now=10)
        assert first.status is ProductReviewStatus.APPROVED
        assert first.evidence is not None and first.receipt is not None
        assert first.operation.status == "accepted"
        second = dispatch_product_review(store, review, reviewer, now=11)
        assert second == first
        assert reviewer.calls == 1
        assert store.artifact(first.operation.operation_id, "review-request")
        assert store.artifact(first.operation.operation_id, "review-receipt")


def test_lost_review_reply_requires_lookup_before_retry(sqlite_tmp_path: Path) -> None:
    reviewer = Dispatcher()
    reviewer.fail_once = True
    review = request()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        pending = dispatch_product_review(store, review, reviewer, now=10, lease_seconds=1)
        assert pending.status is ProductReviewStatus.UNKNOWN
        assert pending.operation.status == "unknown"
        recovered = dispatch_product_review(store, review, reviewer, now=12, lease_seconds=1)
        assert recovered.status is ProductReviewStatus.APPROVED
        assert reviewer.calls == 2


def test_lookup_recovers_completed_review_without_second_dispatch(sqlite_tmp_path: Path) -> None:
    reviewer = LostReplyAfterDispatch()
    review = request()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        pending = dispatch_product_review(store, review, reviewer, now=10, lease_seconds=1)
        assert pending.status is ProductReviewStatus.UNKNOWN
        recovered = dispatch_product_review(store, review, reviewer, now=12, lease_seconds=1)
        assert recovered.status is ProductReviewStatus.APPROVED
        assert reviewer.calls == 1


def test_change_request_retains_findings_and_is_not_platform_approval(sqlite_tmp_path: Path) -> None:
    reviewer = Dispatcher(ProductReviewVerdict.REQUEST_CHANGES)
    review = request()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        result = dispatch_product_review(store, review, reviewer, now=10)
        assert result.status is ProductReviewStatus.CHANGES_REQUESTED
        assert result.evidence is None
        assert result.receipt is not None and result.receipt.findings[0].fingerprint == "fingerprint-1"
        assert result.operation.status == "accepted"
        assert reviewer.calls == 1


def test_owner_decision_stays_distinct_from_review_findings(sqlite_tmp_path: Path) -> None:
    reviewer = Dispatcher(ProductReviewVerdict.OWNER_DECISION_NEEDED)
    review = request()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        result = dispatch_product_review(store, review, reviewer, now=10)
        assert result.status is ProductReviewStatus.OWNER_DECISION_NEEDED
        assert result.evidence is None
        assert result.receipt is not None
        assert result.receipt.findings == ()
        assert result.receipt.owner_decision_options == ("retain existing scope", "expand acceptance scope")


def test_self_review_is_invalid_for_approval_and_change_requests(sqlite_tmp_path: Path) -> None:
    review = request()
    for verdict in (ProductReviewVerdict.APPROVE, ProductReviewVerdict.REQUEST_CHANGES):
        reviewer = SelfReviewDispatcher(verdict)
        with SQLiteProgramStore(sqlite_tmp_path / f"{verdict.value}.sqlite3") as store:
            result = dispatch_product_review(store, review, reviewer, now=10)
            assert result.status is ProductReviewStatus.INVALID
            assert result.evidence is None


def test_candidate_review_remediates_as_fresh_budgeted_attempt(sqlite_tmp_path: Path) -> None:
    reviewer = AlternatingDispatcher()
    runtime = ReviewRuntime()
    workspace = WorkspaceHandle(
        "workspace:first",
        WorkspaceSpec("fixture", "base-revision", (), "tools:v1", "synthetic", TrustMode.TRUSTED_DEVELOPMENT),
    )
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = store.create(reference_spec(), "create")
        store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))

        first = store.load("reference")
        assert _advance_review(store, first, reviewer, runtime, workspace) == "rejected"
        first_attempt = first.program_id + ":first"
        rejected = store.load("reference").attempt(first_attempt)
        assert rejected.spec.remediation_of is None

        second = store.load("reference")
        assert _advance_review(store, second, reviewer, runtime, workspace, remediation_of=first_attempt) == "accepted"
        completed = store.load("reference")
        retries = [item for item in completed.attempts if item.spec.remediation_of is not None]
        assert len(retries) == 1 and retries[0].spec.remediation_of == first_attempt
        assert completed.state("first").satisfaction_id is not None
        retry_context = store.artifact(f"runtime:{retries[0].attempt_id}", "context")
        assert b"fingerprint-1" in retry_context and first_attempt.encode() in retry_context
        assert reviewer.calls == 2


def test_repeated_findings_pause_before_remaining_attempt_budget(sqlite_tmp_path: Path) -> None:
    reviewer = Dispatcher(ProductReviewVerdict.REQUEST_CHANGES)
    runtime = ReviewRuntime()
    workspace = WorkspaceHandle(
        "workspace:first",
        WorkspaceSpec("fixture", "base-revision", (), "tools:v1", "synthetic", TrustMode.TRUSTED_DEVELOPMENT),
    )
    base = reference_spec()
    spec = ProgramSpec(
        base.program_id,
        base.objective,
        base.work_units,
        base.initial_inputs,
        BudgetPolicy(max_attempts=3),
        AuthorityEnvelope(
            "owner",
            max_attempts=3,
            delegated_actor_ids=frozenset({"worker"}),
            trusted_satisfaction_issuers=frozenset({"verifier"}),
        ),
        acceptance_criteria=base.acceptance_criteria,
        policy_references=base.policy_references,
    )
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = store.create(spec, "create")
        store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
        assert _advance_review(store, store.load("reference"), reviewer, runtime, workspace) == "rejected"
        source_id = "reference:first"
        assert (
            _advance_review(store, store.load("reference"), reviewer, runtime, workspace, remediation_of=source_id)
            == "rejected"
        )
        retry_id = store.load("reference").attempts[-1].attempt_id
        assert (
            _advance_review(store, store.load("reference"), reviewer, runtime, workspace, remediation_of=retry_id)
            == "remediation_paused"
        )
        assert reviewer.calls == 2 and len(runtime.requests) == 2


def test_owner_decision_does_not_authorize_remediation(sqlite_tmp_path: Path) -> None:
    reviewer = Dispatcher(ProductReviewVerdict.OWNER_DECISION_NEEDED)
    runtime = ReviewRuntime()
    workspace = WorkspaceHandle(
        "workspace:first",
        WorkspaceSpec("fixture", "base-revision", (), "tools:v1", "synthetic", TrustMode.TRUSTED_DEVELOPMENT),
    )
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = store.create(reference_spec(), "create")
        store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
        assert _advance_review(store, store.load("reference"), reviewer, runtime, workspace) == "owner_decision_needed"
        assert (
            _advance_review(
                store,
                store.load("reference"),
                reviewer,
                runtime,
                workspace,
                remediation_of="reference:first",
            )
            == "remediation_paused"
        )
        assert reviewer.calls == 1 and len(runtime.requests) == 1
