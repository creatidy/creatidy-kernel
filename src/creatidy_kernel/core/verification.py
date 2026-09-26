# SPDX-License-Identifier: Apache-2.0
"""Exact-subject verification decisions made outside the worker boundary.

Callers must supply evidence from trusted, independently operated check producers;
this module validates their subjects and makes the acceptance decision, not their truth.
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from creatidy_kernel.core.domain import (
    AttemptStatus,
    InputBinding,
    InvalidDomainValue,
    PolicyReference,
    Program,
    SatisfyWorkUnit,
    StaleRevision,
    TrustedSatisfaction,
    WorkUnitStatus,
)
from creatidy_kernel.core.execution import ArtifactManifest, Candidate


def _required(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise InvalidDomainValue("verification identifiers must be nonempty strings")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidDomainValue("duplicate candidate field")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CandidateResult:
    candidate_id: str
    publication: Candidate
    outputs: tuple[tuple[str, str], ...]  # declared output name -> artifact path

    def __post_init__(self) -> None:
        _required(self.candidate_id)
        if type(self.publication) is not Candidate or type(self.outputs) is not tuple:
            raise InvalidDomainValue("candidate publication and outputs must be immutable")
        if any(type(item) is not tuple or len(item) != 2 for item in self.outputs):
            raise InvalidDomainValue("candidate outputs must be name/path pairs")
        for name, path in self.outputs:
            _required(name)
            _required(path)
        if len({name for name, _ in self.outputs}) != len(self.outputs):
            raise InvalidDomainValue("candidate output names must be unique")
        if len({path for _, path in self.outputs}) != len(self.outputs):
            raise InvalidDomainValue("candidate output artifacts must be unique")

    @property
    def digest(self) -> str:
        manifest = self.publication.artifacts
        payload = (
            self.candidate_id,
            self.publication.attempt_id,
            self.publication.spec_digest,
            manifest.workspace_key,
            sorted((item.path, item.digest) for item in manifest.artifacts),
            sorted(self.outputs),
        )
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def parse_candidate(data: str, manifest: ArtifactManifest) -> CandidateResult:
    """Strictly parse the untrusted proposal; never interpret a claimed verdict."""
    try:
        raw = cast(object, json.loads(data, object_pairs_hook=_unique_object))
    except (ValueError, TypeError) as exc:
        raise InvalidDomainValue("malformed candidate") from exc
    if type(raw) is not dict:
        raise InvalidDomainValue("candidate fields must match the versioned schema")
    fields = cast(dict[str, object], raw)
    if set(fields) != {"candidate_id", "attempt_id", "spec_digest", "outputs"}:
        raise InvalidDomainValue("candidate fields must match the versioned schema")
    outputs = fields["outputs"]
    if type(outputs) is not dict:
        raise InvalidDomainValue("candidate outputs must map names to artifact paths")
    output_fields = cast(dict[object, object], outputs)
    if any(type(k) is not str or type(v) is not str for k, v in output_fields.items()):
        raise InvalidDomainValue("candidate outputs must map names to artifact paths")
    for key in ("candidate_id", "attempt_id", "spec_digest"):
        _required(cast(str, fields[key]))
    if type(manifest) is not ArtifactManifest:
        raise InvalidDomainValue("trusted collected manifest required")
    return CandidateResult(
        cast(str, fields["candidate_id"]),
        Candidate(cast(str, fields["attempt_id"]), cast(str, fields["spec_digest"]), manifest),
        tuple(sorted(cast(dict[str, str], outputs).items())),
    )


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    reference: PolicyReference
    checks: tuple[str, ...]
    reviewer_required: bool = True

    def __post_init__(self) -> None:
        if type(self.reference) is not PolicyReference or type(self.checks) is not tuple or not self.checks:
            raise InvalidDomainValue("pinned policy and nonempty checks required")
        for check in self.checks:
            _required(check)
        if len(set(self.checks)) != len(self.checks) or type(self.reviewer_required) is not bool:
            raise InvalidDomainValue("checks must be unique and reviewer requirement explicit")


@dataclass(frozen=True, slots=True)
class EvidenceSubject:
    candidate_digest: str
    attempt_digest: str
    spec_digest: str
    policy_digest: str
    repository: str
    base_revision: str
    head_revision: str

    def __post_init__(self) -> None:
        for value in (
            self.candidate_digest,
            self.attempt_digest,
            self.spec_digest,
            self.policy_digest,
            self.repository,
            self.base_revision,
            self.head_revision,
        ):
            _required(value)


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    check: str
    subject: EvidenceSubject
    producer: str
    tool_version: str
    artifact_reference: str
    observed_at: int
    fresh_until: int
    passed: bool
    reviewer_session: str | None = None

    def __post_init__(self) -> None:
        for value in (self.evidence_id, self.check, self.producer, self.tool_version, self.artifact_reference):
            _required(value)
        if (
            type(self.subject) is not EvidenceSubject
            or type(self.observed_at) is not int
            or (type(self.fresh_until) is not int or self.fresh_until < self.observed_at)
            or type(self.passed) is not bool
        ):
            raise InvalidDomainValue("invalid evidence subject, time, or result")
        if self.reviewer_session is not None:
            _required(self.reviewer_session)


class CheckProducer(Protocol):
    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None: ...


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str
    fingerprint: str
    subject: EvidenceSubject
    reason: str


@dataclass(frozen=True, slots=True)
class AcceptedResult:
    candidate: CandidateResult
    subject: EvidenceSubject
    evidence: tuple[Evidence, ...]
    policy: PolicyReference
    satisfaction: TrustedSatisfaction


@dataclass(frozen=True, slots=True)
class RejectedResult:
    candidate: CandidateResult
    subject: EvidenceSubject
    evidence: tuple[Evidence, ...]
    findings: tuple[Finding, ...]


def verify_candidate(
    program: Program,
    candidate: CandidateResult,
    collected: ArtifactManifest,
    policy: VerificationPolicy,
    producer: CheckProducer,
    *,
    repository: str,
    base_revision: str,
    head_revision: str,
    verifier_id: str,
    reference_id: str,
    now: int,
) -> AcceptedResult | RejectedResult:
    """Run required trusted checks; only matching fresh evidence permits satisfaction."""
    if type(program) is not Program or type(candidate) is not CandidateResult or type(policy) is not VerificationPolicy:
        raise InvalidDomainValue("trusted Program, candidate and policy required")
    if type(collected) is not ArtifactManifest or type(now) is not int:
        raise InvalidDomainValue("trusted collection and observation time required")
    for value in (repository, base_revision, head_revision, verifier_id, reference_id):
        _required(value)
    attempt = program.attempt(candidate.publication.attempt_id)
    spec = attempt.spec
    unit = program.spec.work_unit(spec.work_unit_id)
    if (
        program.state(unit.id).status is not WorkUnitStatus.READY
        or any(item.source_attempt_id == spec.attempt_id for item in program.satisfactions)
        or attempt.status is not AttemptStatus.FINISHED
        or spec.spec_digest != program.spec.digest
        or (spec.spec_revision != program.spec.revision)
    ):
        raise StaleRevision("verification requires a finished Attempt against current approved intent")
    if candidate.publication.spec_digest != spec.digest or collected != candidate.publication.artifacts:
        raise StaleRevision("candidate Attempt or collected artifact subject differs")
    if (unit.acceptance_policy_reference is not None and policy.reference != unit.acceptance_policy_reference) or (
        policy.reference not in program.spec.policy_references and policy.reference != unit.acceptance_policy_reference
    ):
        raise StaleRevision("verification policy is not pinned in current approved intent")
    if not set((*program.spec.acceptance_criteria, *unit.acceptance_criteria)).issubset(policy.checks):
        raise InvalidDomainValue("trusted checks must cover approved acceptance criteria")
    if verifier_id not in program.spec.authority.trusted_satisfaction_issuers or verifier_id == attempt.actor_id:
        raise InvalidDomainValue("worker cannot issue trusted acceptance")
    artifacts = collected.artifacts
    if collected.workspace_key != spec.workspace_reference or len({item.path for item in artifacts}) != len(artifacts):
        raise InvalidDomainValue("collected artifacts do not identify a unique Attempt workspace")
    if frozenset(name for name, _ in candidate.outputs) != unit.outputs:
        raise InvalidDomainValue("candidate must bind exactly declared output names")
    by_path = {item.path: item for item in artifacts}
    if any(path not in by_path for _, path in candidate.outputs):
        raise InvalidDomainValue("candidate output is absent from collected artifacts")
    subject = EvidenceSubject(
        candidate.digest,
        spec.digest,
        program.spec.digest,
        policy.reference.digest,
        repository,
        base_revision,
        head_revision,
    )
    required = (*policy.checks, *(("independent_review",) if policy.reviewer_required else ()))
    if len(set(required)) != len(required):
        raise InvalidDomainValue("independent review check cannot duplicate deterministic checks")
    observations = tuple(producer.check(name, subject) for name in required)
    evidence = tuple(item for item in observations if type(item) is Evidence)
    if len({item.evidence_id for item in evidence}) != len(evidence):
        raise InvalidDomainValue("duplicate evidence IDs")
    findings: list[Finding] = []
    for name, item in zip(required, observations, strict=True):
        valid = (
            type(item) is Evidence
            and item.check == name
            and item.subject == subject
            and item.producer != attempt.actor_id
            and item.observed_at <= now <= item.fresh_until
            and (
                name != "independent_review"
                or (
                    item.reviewer_session is not None
                    and item.reviewer_session != spec.attempt_id
                    and item.producer != verifier_id
                )
            )
        )
        if not valid or not isinstance(item, Evidence) or not item.passed:
            findings.append(
                Finding(
                    f"{candidate.candidate_id}:{name}",
                    name,
                    subject,
                    "missing, stale, mismatched or failing trusted evidence",
                )
            )
    if findings:
        return RejectedResult(candidate, subject, evidence, tuple(findings))
    outputs = tuple(InputBinding(name, f"artifact:{by_path[path].digest}") for name, path in candidate.outputs)
    fact = TrustedSatisfaction(
        reference_id,
        program.program_id,
        unit.id,
        program.spec.revision,
        program.spec.digest,
        program.spec.work_unit_applicability_fingerprint(unit.id),
        verifier_id,
        spec.attempt_id,
        outputs,
    )
    return AcceptedResult(candidate, subject, evidence, policy.reference, fact)


def admit_accepted(
    program: Program,
    result: AcceptedResult,
    collected: ArtifactManifest,
    policy: VerificationPolicy,
    *,
    repository: str,
    base_revision: str,
    head_revision: str,
    now: int,
) -> Program:
    """Recheck current subjects and all evidence before applying a trusted fact."""
    if (
        type(result) is not AcceptedResult
        or result.satisfaction.source_attempt_id != result.candidate.publication.attempt_id
    ):
        raise InvalidDomainValue("verified accepted result required")
    if result.policy != policy.reference:
        raise StaleRevision("pinned acceptance policy changed")

    class RecordedChecks:
        def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
            del subject
            return next((item for item in result.evidence if item.check == name), None)

    checked = verify_candidate(
        program,
        result.candidate,
        collected,
        policy,
        RecordedChecks(),
        repository=repository,
        base_revision=base_revision,
        head_revision=head_revision,
        verifier_id=result.satisfaction.issuer_id,
        reference_id=result.satisfaction.reference_id,
        now=now,
    )
    if checked != result:
        raise StaleRevision("accepted result lacks current exact-subject evidence")
    return program.apply(SatisfyWorkUnit(program.revision, result.satisfaction.issuer_id, result.satisfaction))


class RemediationStatus(StrEnum):
    RETRY = "retry"
    PAUSE = "pause"
    HUMAN_GATE = "human_gate"


@dataclass(frozen=True, slots=True)
class RemediationDecision:
    status: RemediationStatus
    reason: str
    used: int
    progress: bool
    oscillation: bool


def assess_remediation(
    history: tuple[RejectedResult, ...],
    *,
    max_changes: int,
    used: int,
    authorized: bool,
    owner_decision_required: bool = False,
) -> RemediationDecision:
    """Track finding fingerprints across changed subjects without erasing old findings."""
    if not history or type(max_changes) is not int or type(used) is not int or used < 0 or max_changes < 0:
        raise InvalidDomainValue("finite remediation history and budget required")
    if type(authorized) is not bool or type(owner_decision_required) is not bool:
        raise InvalidDomainValue("authority distinctions must be explicit")
    sets = [frozenset(item.fingerprint for item in result.findings) for result in history]
    progress = len(sets) == 1 or bool(sets[-2] - sets[-1])
    oscillation = len(sets) > 2 and sets[-1] in sets[:-2]
    if owner_decision_required:
        return RemediationDecision(
            RemediationStatus.HUMAN_GATE, "new owner authority required", used, progress, oscillation
        )
    if not authorized or used >= max_changes:
        return RemediationDecision(
            RemediationStatus.PAUSE, "no authorized remediation budget", used, progress, oscillation
        )
    return RemediationDecision(
        RemediationStatus.RETRY,
        "oscillation diagnosed" if oscillation else "bounded retry",
        used + 1,
        progress,
        oscillation,
    )
