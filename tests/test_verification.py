# SPDX-License-Identifier: Apache-2.0
"""Synthetic exact-subject verification and remediation regressions for issue #7."""

from dataclasses import replace

import pytest

from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AuthorityEnvelope,
    FinishAttempt,
    InputBinding,
    InvalidDomainValue,
    PolicyReference,
    PrepareAttempt,
    Program,
    ProgramSpec,
    ProgramStatus,
    StaleRevision,
    StartAttempt,
    WorkUnit,
    WorkUnitStatus,
)
from creatidy_kernel.core.execution import Artifact, ArtifactManifest
from creatidy_kernel.core.verification import (
    AcceptedResult,
    CandidateResult,
    Evidence,
    EvidenceSubject,
    Finding,
    RejectedResult,
    RemediationStatus,
    VerificationPolicy,
    admit_accepted,
    assess_remediation,
    parse_candidate,
    verify_candidate,
)

POLICY = PolicyReference("verification", "v1", "sha256:policy")
CHECKS = ("Program accepted", "node accepted")


class Checks:
    def __init__(self) -> None:
        self.missing: str | None = None
        self.fail: str | None = None
        self.stale = False
        self.worker = False
        self.subject_override: EvidenceSubject | None = None

    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        if name == self.missing:
            return None
        return Evidence(
            f"evidence:{name}",
            name,
            self.subject_override or subject,
            "worker" if self.worker else "reviewer" if name == "independent_review" else "trusted-checker",
            "v1",
            f"ref:{name}",
            1,
            1 if self.stale else 20,
            name != self.fail,
            "cold-session" if name == "independent_review" else None,
        )


def program() -> Program:
    spec = ProgramSpec(
        "program",
        "synthetic two-node delivery",
        (
            WorkUnit(
                "one",
                "deliver one",
                required_inputs=frozenset({"seed"}),
                outputs=frozenset({"product"}),
                acceptance_criteria=("node accepted",),
            ),
            WorkUnit(
                "two",
                "consume one",
                dependencies=("one",),
                required_inputs=frozenset({"product"}),
                acceptance_criteria=("node accepted",),
            ),
        ),
        initial_inputs=(InputBinding("seed", "ref:seed"),),
        acceptance_criteria=("Program accepted",),
        policy_references=(POLICY,),
        authority=AuthorityEnvelope(
            "owner",
            max_attempts=3,
            trusted_satisfaction_issuers=frozenset({"verifier"}),
            delegated_actor_ids=frozenset({"worker"}),
        ),
    )
    draft = Program.create(spec)
    return draft.apply(ActivateProgram(draft.revision, "owner"))


def finish(program: Program, name: str, attempt_id: str) -> Program:
    spec = AttemptSpec(
        attempt_id,
        program.program_id,
        name,
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs(name),
        workspace_reference=f"workspace:{attempt_id}",
    )
    prepared = program.apply(PrepareAttempt(program.revision, "worker", spec))
    running = prepared.apply(StartAttempt(prepared.revision, "worker", attempt_id))
    return running.apply(FinishAttempt(running.revision, "worker", attempt_id))


def candidate(attempt_id: str, outputs: str = '{"product":"product.txt"}') -> tuple[CandidateResult, ArtifactManifest]:
    manifest = ArtifactManifest(f"workspace:{attempt_id}", (Artifact("product.txt", "sha256:product"),))
    data = (
        f'{{"candidate_id":"candidate:{attempt_id}","attempt_id":"{attempt_id}",'
        f'"spec_digest":"SPEC","outputs":{outputs}}}'
    )
    return parse_candidate(data, manifest), manifest


def verify(
    program: Program, proposal: CandidateResult, manifest: ArtifactManifest, checks: Checks | None = None
) -> AcceptedResult | RejectedResult:
    return verify_candidate(
        program,
        proposal,
        manifest,
        VerificationPolicy(POLICY, CHECKS),
        Checks() if checks is None else checks,
        repository="repo",
        base_revision="base",
        head_revision="head",
        verifier_id="verifier",
        reference_id="accepted:" + proposal.candidate_id,
        now=10,
    )


def proposal_for(
    program: Program, attempt_id: str, outputs: str = '{"product":"product.txt"}'
) -> tuple[CandidateResult, ArtifactManifest]:
    proposal, manifest = candidate(attempt_id, outputs)
    return replace(
        proposal, publication=replace(proposal.publication, spec_digest=program.attempt(attempt_id).spec.digest)
    ), manifest


def admit(program: Program, result: AcceptedResult, manifest: ArtifactManifest, *, head: str = "head") -> Program:
    return admit_accepted(
        program,
        result,
        manifest,
        VerificationPolicy(POLICY, CHECKS),
        repository="repo",
        base_revision="base",
        head_revision=head,
        now=10,
    )


def test_two_node_acceptance_and_stale_subject_preserves_old_evidence() -> None:
    first = finish(program(), "one", "a1")
    assert first.state("two").status is WorkUnitStatus.PENDING
    proposal, manifest = proposal_for(first, "a1")
    accepted = verify(first, proposal, manifest)
    assert isinstance(accepted, AcceptedResult)
    assert len(accepted.evidence) == 3
    next_program = admit(first, accepted, manifest)
    assert next_program.state("two").status is WorkUnitStatus.READY
    assert next_program.resolved_inputs("two") == (
        InputBinding(
            "product", "artifact:sha256:product", "predecessor", "one", accepted.satisfaction.reference_id, "product"
        ),
    )
    second = finish(next_program, "two", "a2")
    next_candidate, next_manifest = proposal_for(second, "a2", "{}")
    second_result = verify(second, next_candidate, next_manifest)
    assert isinstance(second_result, AcceptedResult)
    assert admit(second, second_result, next_manifest).status is ProgramStatus.COMPLETED
    assert first.state("two").status is WorkUnitStatus.PENDING
    with pytest.raises(StaleRevision):
        verify(second, proposal, manifest)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"candidate_id":"x","candidate_id":"y"}',
        '{"candidate_id":"x","attempt_id":"a","spec_digest":"d","outputs":{},"pass":true}',
        '{"candidate_id":"x","attempt_id":"a","spec_digest":"d","outputs":{"a":"p","a":"q"}}',
    ],
)
def test_c_malformed_extra_duplicate_candidate_data(raw: str) -> None:
    with pytest.raises(InvalidDomainValue):
        parse_candidate(raw, ArtifactManifest("workspace:a", ()))


def test_schema_valid_proposal_is_not_accepted_without_exact_fresh_evidence() -> None:
    state = finish(program(), "one", "a1")
    proposal, manifest = proposal_for(state, "a1")
    for change in ("missing", "fail", "stale", "worker", "subject_override"):
        checks = Checks()
        if change in ("missing", "fail"):
            setattr(checks, change, "independent_review")
        elif change == "subject_override":
            checks.subject_override = EvidenceSubject("other", "attempt", "spec", "policy", "repo", "base", "head")
        else:
            setattr(checks, change, True)
        result = verify(state, proposal, manifest, checks)
        assert isinstance(result, RejectedResult)
        assert result.findings
        assert state.state("one").status is WorkUnitStatus.READY
    with pytest.raises(StaleRevision):
        verify(state, proposal, ArtifactManifest("workspace:a1", ()), Checks())
    with pytest.raises(InvalidDomainValue):
        verify_candidate(
            state,
            proposal,
            manifest,
            VerificationPolicy(POLICY, ("other",)),
            Checks(),
            repository="repo",
            base_revision="base",
            head_revision="head",
            verifier_id="verifier",
            reference_id="s1",
            now=10,
        )


def test_d_e_transport_success_is_not_domain_or_review_acceptance() -> None:
    state = finish(program(), "one", "a1")
    proposal, manifest = proposal_for(state, "a1")
    checks = Checks()
    checks.missing = "independent_review"  # delivery without semantic acceptance
    result = verify(state, proposal, manifest, checks)
    assert isinstance(result, RejectedResult)
    assert state.state("two").status is WorkUnitStatus.PENDING
    checks.missing = None
    checks.fail = "independent_review"  # domain rejection, not a wait for acceptance
    assert isinstance(verify(state, proposal, manifest, checks), RejectedResult)


def test_policy_artifacts_and_changed_head_invalidate_existing_evidence() -> None:
    state = finish(program(), "one", "a1")
    proposal, manifest = proposal_for(state, "a1")
    collected = replace(manifest, artifacts=(Artifact("product.txt", "sha256:tampered"),))
    with pytest.raises(StaleRevision):
        verify(state, proposal, collected)
    wrong_policy = VerificationPolicy(PolicyReference("verification", "v2", "sha256:new"), CHECKS)
    with pytest.raises(StaleRevision):
        verify_candidate(
            state,
            proposal,
            manifest,
            wrong_policy,
            Checks(),
            repository="repo",
            base_revision="base",
            head_revision="head",
            verifier_id="verifier",
            reference_id="s1",
            now=10,
        )
    old = verify(state, proposal, manifest)
    assert isinstance(old, AcceptedResult)
    checks = Checks()
    checks.subject_override = old.subject
    moved = verify_candidate(
        state,
        proposal,
        manifest,
        VerificationPolicy(POLICY, CHECKS),
        checks,
        repository="repo",
        base_revision="base",
        head_revision="changed",
        verifier_id="verifier",
        reference_id="s1",
        now=10,
    )
    assert isinstance(moved, RejectedResult)
    assert len(moved.findings) == 3
    with pytest.raises(StaleRevision):
        admit(state, old, manifest, head="changed")
    with pytest.raises(StaleRevision):
        admit(state, replace(old, evidence=()), manifest)


def test_f_new_blockers_progress_oscillation_pause_and_authority_gate() -> None:
    state = finish(program(), "one", "a1")
    proposal, _ = proposal_for(state, "a1")
    subject = EvidenceSubject("candidate", "attempt", "spec", "policy", "repo", "base", "head")

    def rejection(fingerprint: str) -> RejectedResult:
        return RejectedResult(proposal, subject, (), (Finding(fingerprint, fingerprint, subject, "failed"),))

    a, b, c = rejection("a"), rejection("b"), rejection("c")
    assert assess_remediation((a, b, c), max_changes=3, used=2, authorized=True).status is RemediationStatus.RETRY
    assert assess_remediation((a, b, c), max_changes=3, used=2, authorized=True).progress
    oscillating = assess_remediation((a, b, a), max_changes=3, used=2, authorized=True)
    assert oscillating.oscillation and oscillating.status is RemediationStatus.RETRY
    assert assess_remediation((a, b), max_changes=2, used=2, authorized=True).status is RemediationStatus.PAUSE
    assert assess_remediation((a,), max_changes=3, used=0, authorized=False).status is RemediationStatus.PAUSE
    assert (
        assess_remediation((a,), max_changes=3, used=0, authorized=True, owner_decision_required=True).status
        is RemediationStatus.HUMAN_GATE
    )
