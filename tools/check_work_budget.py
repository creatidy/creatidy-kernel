# SPDX-License-Identifier: Apache-2.0
"""Installed, synthetic actual Attempt/review capacity and crash-reopen denial."""

import sys
from dataclasses import replace
from pathlib import Path

from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt
from creatidy_kernel.core.execution import Artifact, ArtifactManifest, Candidate, OperationKey
from creatidy_kernel.core.verification import (
    CandidateResult,
    EvidenceSubject,
    ProductReviewReceipt,
    ProductReviewRequest,
)
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.verification import ReviewLookup, ReviewLookupStatus, dispatch_product_review


class NoDispatch:
    def dispatch(self, operation: OperationKey, request: ProductReviewRequest, *, fence: int) -> ProductReviewReceipt:
        raise AssertionError("exhausted installed review must never be dispatched")

    def reconcile(self, operation: OperationKey, request_digest: str) -> ReviewLookup:
        return ReviewLookup(ReviewLookupStatus.UNKNOWN)


state = Path(sys.argv[1])
path = state / "budget.sqlite3"
clock = BootClockReading(100, 1_000_000_000, "installed-synthetic-boot")
policy = WorkBudgetPolicy.basic(1, 1, verifications=1, retained_bytes=1024)
policy = replace(policy, counts=tuple((kind, 0 if kind is WorkKind.REVIEW else count) for kind, count in policy.counts))
with SQLiteProgramStore(path, boot_clock=lambda: clock) as store:
    program = store.create(reference_spec(), "create")
    store.bind_work_budget(program.program_id, "installed-synthetic-project", policy, policy, 110)
    program = store.admit(program.program_id, "activate", ActivateProgram(0, "owner"))
    attempt = AttemptSpec(
        "installed-attempt",
        program.program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
    )
    store.admit_with_intent(
        program.program_id,
        "prepare",
        PrepareAttempt(program.revision, "worker", attempt),
        "runtime:installed-attempt",
        "runtime:installed-attempt",
        {"attempt": attempt.digest},
    )
    manifest = ArtifactManifest("synthetic-workspace", (Artifact("result.txt", "sha256:synthetic"),))
    candidate = CandidateResult(
        "installed-candidate",
        Candidate(attempt.attempt_id, attempt.digest, manifest),
        (("first-output", "result.txt"),),
    )
    acceptance_policy = program.spec.policy_references[0]
    subject = EvidenceSubject(
        candidate.digest,
        attempt.digest,
        program.spec.digest,
        acceptance_policy.digest,
        "synthetic-repository",
        "base",
        "head",
    )
    review = ProductReviewRequest(candidate, subject, attempt.attempt_id, "worker", acceptance_policy)
    try:
        dispatch_product_review(store, review, NoDispatch(), now=100)
    except BudgetRefused:
        pass
    else:
        raise AssertionError("installed review capacity was not enforced")
    before = store.work_budget_status(program.program_id)
with SQLiteProgramStore(path, boot_clock=lambda: clock) as store:
    if store.work_budget_status(program.program_id) != before:
        raise AssertionError("installed crash-reopen changed reservations")
    try:
        store.reserve_work(program.program_id, "successor", "runtime:successor", WorkKind.ATTEMPT)
    except BudgetRefused:
        pass
    else:
        raise AssertionError("installed crash-reopen reset finite work capacity")
    clock = BootClockReading(101, 12_000_000_000, "installed-synthetic-boot")
    try:
        store.claim("runtime:installed-attempt", now=101, lease_seconds=1)
    except BudgetRefused:
        pass
    else:
        raise AssertionError("suspend-inclusive installed expiry failed")
print("Installed work budget: atomic Attempt reservation, review exhaustion, crash-reopen and boot expiry passed.")
