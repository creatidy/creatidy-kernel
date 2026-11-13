# SPDX-License-Identifier: Apache-2.0
"""One ordinary Program application; native transports and worker requests have no authority.

The trusted host binds its principals, source/check producer and handoff profile.
Pull and managed start share request/report, Core selection and the same immutable
Attempt/Operation. There is no hidden planner, alternate scheduler or model loop.
"""

import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, replace
from typing import Protocol, cast

from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AmendProgramSpec,
    AttemptSpec,
    FinishAttempt,
    InputBinding,
    Program,
    ProgramSpec,
    SpecAmendment,
    StartAttempt,
    WorkUnit,
)
from creatidy_kernel.core.drivers import DriverFact, DriverSubject, EdgeObligation, WorkObservation
from creatidy_kernel.core.execution import Candidate
from creatidy_kernel.core.intake import (
    Declaration,
    Draft,
    IntakeRefused,
    IssueSubject,
    OwnerPolicy,
    digest,
    encode,
    object_from,
)
from creatidy_kernel.core.ordinary import DependencyEvidence, OrdinaryProposal
from creatidy_kernel.core.ordinary_qualified import (
    QualifiedBaseline,
    QualifiedDependencyCut,
    QualifiedDraft,
    QualifiedReadiness,
)
from creatidy_kernel.core.ownership import HandoffMode, WorkClaim
from creatidy_kernel.core.verification import CandidateResult, EvidenceSubject, ProductReviewRequest, VerificationPolicy
from creatidy_kernel.ports.application import Collection, receive_candidate, remediation_context
from creatidy_kernel.ports.drivers import SourceManagement, WorkManagement
from creatidy_kernel.ports.intake import IntakeStore, OrdinaryIntake, draft_from
from creatidy_kernel.ports.ownership import OwnershipStore, WorkOwnership
from creatidy_kernel.ports.program_store import OperationRecord
from creatidy_kernel.ports.verification import (
    ProductReviewDispatcher,
    ProductReviewStatus,
    dispatch_product_review,
    product_review_operation_id,
)


class OrdinaryStore(IntakeStore, OwnershipStore, Protocol):
    def work_budget_status(self, program_id: str) -> dict[str, object]: ...
    def report_revision_guard(self, program_id: str, revision: int) -> AbstractContextManager[None]: ...
    def work_decision_time(self, program_id: str) -> int: ...
    def check_work_authority(self, program_id: str) -> None: ...
    def find_operation(self, operation_id: str) -> OperationRecord | None: ...
    def revoke_work_budget(self, program_id: str, evidence: str, *, expected_revision: int | None = None) -> None: ...
    def stop_ordinary_program(
        self, program_id: str, command_key: str, actor: str, evidence: str, *, expected_revision: int | None = None
    ) -> Program: ...
    def accept_ordinary_preparation(
        self, claim: WorkClaim, owner: str, subject: str, *, expected_revision: int | None = None
    ) -> None: ...
    def find_ordinary_preparation(self, claim: WorkClaim) -> bool: ...


class DependencyReader(Protocol):
    def read_dependencies(self, subject: IssueSubject) -> DependencyEvidence: ...


class QualifiedBaselineReader(Protocol):
    def read_baseline(self, source: DriverSubject, recipe_digest: str) -> QualifiedBaseline | None: ...


class QualifiedReadinessReader(Protocol):
    def read_readiness(
        self, cut: QualifiedDependencyCut, source: DriverSubject, declaration: Declaration
    ) -> QualifiedReadiness | None: ...


class OrdinaryWorkspace(Protocol):
    @property
    def authority_deadline(self) -> int: ...
    @property
    def reference(self) -> str: ...
    def initialize(self, attempt: AttemptSpec, check_current: Callable[[], None]) -> str: ...
    def restore(self, attempt: AttemptSpec) -> None: ...
    def collect(self, attempt: AttemptSpec, claim: WorkClaim) -> tuple[Candidate, Collection]: ...


class OrdinaryReviewer(ProductReviewDispatcher, Protocol):
    @property
    def authority_deadline(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class OrdinaryApproval:
    proposal: OrdinaryProposal
    policy_digest: str
    decision_id: str
    expires_at: int
    program: Program


@dataclass(frozen=True, slots=True)
class OrdinaryHandoff:
    attempt: AttemptSpec
    claim: WorkClaim


class OrdinaryProgram:
    def __init__(
        self,
        store: OrdinaryStore,
        intake: OrdinaryIntake,
        dependencies: DependencyReader,
        owner: Principal,
        policy: OwnerPolicy,
        verification: VerificationPolicy,
        clock: Callable[[], int],
        *,
        execution_profile: bytes,
        qualified_work: tuple[WorkManagement, ...] = (),
        qualified_scm: tuple[SourceManagement, ...] = (),
        qualified_baselines: QualifiedBaselineReader | None = None,
        qualified_readiness: QualifiedReadinessReader | None = None,
    ) -> None:
        if (
            owner.role != "owner"
            or owner.actor_id != policy.authority.owner_id
            or verification.reference != policy.reference
            or not verification.reviewer_required
            or not {"paths", "structural", "commands"}.issubset(verification.checks)
            or "independent_review" in verification.checks
            or policy.work_budget is None
            or policy.project_budget is None
            or policy.project_id is None
        ):
            raise IntakeRefused("trusted owner, finite work envelope and required independent review must be bound")
        self.store, self.intake, self.dependencies = store, intake, dependencies
        self.owner, self.policy, self.verification, self.clock = owner, policy, verification, clock
        if any(driver.binding.family != "work" for driver in qualified_work) or len(
            {driver.binding for driver in qualified_work}
        ) != len(qualified_work):
            raise IntakeRefused("distinct explicitly bound work drivers required")
        self.qualified_work = qualified_work
        if any(driver.binding.family != "scm" for driver in qualified_scm) or len(
            {driver.binding for driver in qualified_scm}
        ) != len(qualified_scm):
            raise IntakeRefused("distinct explicitly bound SCM drivers required")
        self.qualified_scm, self.qualified_baselines = qualified_scm, qualified_baselines
        self.qualified_readiness = qualified_readiness
        profile = object_from(execution_profile)
        if (
            set(profile) != {"harness", "provider", "model", "effort", "trust"}
            or profile["harness"] not in {"kilo-pull", "codex", "kilo-pull+codex"}
            or profile["trust"] != "trusted_development"
            or any(
                value is not None and (type(value) is not str or not value)
                for value in (profile["provider"], profile["model"], profile["effort"])
            )
        ):
            raise IntakeRefused("closed owner-bound basic harness profile required; no secrets or executable recipes")
        self.execution_profile = execution_profile
        self.execution_reference = "basic-harness-policy:" + digest(execution_profile)
        self._record("ordinary-harness:" + digest(execution_profile), {"profile": profile})

    def observe_qualified_dependencies(
        self,
        driver: WorkManagement,
        subject: DriverSubject,
        classifications: tuple[tuple[str, bool], ...],
        *,
        obligation: EdgeObligation | None = None,
    ) -> QualifiedDependencyCut:
        """Read an inert qualified cut; this does not approve or admit a Program."""
        if subject.binding != driver.binding or subject.binding.family != "work" or subject.revision is None:
            raise IntakeRefused("exact independently bound work subject required")
        work = driver.work(subject.key)
        if work.fact.subject != subject:
            raise IntakeRefused("qualified work subject changed or is unavailable")
        return QualifiedDependencyCut.capture(work, classifications, self.clock(), obligation=obligation)

    def recheck_qualified_dependencies(self, driver: WorkManagement, cut: QualifiedDependencyCut) -> None:
        """Reobserve an inert cut without renewing its original freshness or granting authority."""
        if driver.binding != cut.subject.binding:
            raise IntakeRefused("qualified work driver binding differs")
        current = driver.work(cut.subject.key)
        cut.recheck(current, self.clock())

    def prepare_qualified(
        self,
        task_id: str,
        declaration: Declaration,
        cut: QualifiedDependencyCut,
        source: DriverSubject,
        *,
        relevance_reason: str,
        ci_subjects: tuple[DriverSubject, ...] = (),
    ) -> QualifiedDraft:
        """Capture inert qualified meaning from explicitly configured trusted producers."""
        drivers = {driver.binding: driver for driver in self.qualified_scm}
        driver = drivers.get(source.binding)
        if (
            driver is None
            or source.kind != "ref"
            or source.revision is None
            or self.qualified_baselines is None
            or self.qualified_readiness is None
        ):
            raise IntakeRefused("exact SCM ref and baseline producer required")
        observed = driver.ref(source.key)
        now = self.clock()
        observed.require_current(now)
        if (
            observed.subject != source
            or not observed.complete
            or not observed.qualification
            or driver.binding != source.binding
        ):
            raise IntakeRefused("qualified source observation changed or is incomplete")
        baseline = self.qualified_baselines.read_baseline(source, digest(encode(declaration.value["recipes"])))
        if baseline is None or baseline.source != source or not baseline.proof:
            raise IntakeRefused("exact qualified baseline unavailable")
        readiness = self.qualified_readiness.read_readiness(cut, source, declaration)
        if readiness is None:
            raise IntakeRefused("qualified relevance unavailable")
        baseline = replace(baseline, observed_at=self._qualified_baseline_age(baseline))
        readiness = replace(readiness, observed_at=self._qualified_readiness_age(readiness))
        return QualifiedDraft(
            task_id,
            declaration,
            cut,
            baseline,
            readiness,
            observed.observation_digest,
            observed.provenance,
            observed.qualification,
            observed.observed_at,
            observed.fresh_until,
            relevance_reason,
            ci_subjects,
        )

    def _qualified_baseline_age(self, baseline: QualifiedBaseline) -> int:
        return self._qualified_receipt_age("baseline", baseline.meaning, baseline.observed_at)

    def _qualified_readiness_age(self, readiness: QualifiedReadiness) -> int:
        return self._qualified_receipt_age("readiness", readiness.meaning, readiness.observed_at)

    def _qualified_receipt_age(self, kind: str, meaning: dict[str, object], observed_at: int) -> int:
        prefix = f"ordinary-program:qualified-{kind}-age:"
        ages = [observed_at]
        if kind == "baseline":
            for operation in self.store.operations("ordinary:"):
                if not (
                    operation.operation_id.startswith("ordinary:baseline-age:")
                    or operation.operation_id.startswith("ordinary:record:ordinary-draft:")
                    or re.fullmatch(r"ordinary:[A-Za-z0-9_-]+:draft:[1-9][0-9]*", operation.operation_id)
                ):
                    continue
                raw = self.store.find_artifact(operation.operation_id, "record")
                if raw is None:
                    raise IntakeRefused("legacy receipt age record is incomplete")
                request = object_from(encode(object_from(operation.request_json.encode()).get("request")))
                if (
                    operation.effect_key != operation.operation_id
                    or operation.attempts
                    or request != {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()}
                ):
                    raise IntakeRefused("legacy receipt age record differs")
                value = object_from(raw)
                if value.get("kind") == "ordinary-draft":
                    evidence = draft_from(raw).evidence
                    old_producer, old_reference, age = (
                        evidence.baseline_producer,
                        evidence.baseline_reference,
                        evidence.observed_at,
                    )
                elif value.get("kind") == "ordinary-baseline-age":
                    identity = object_from(encode(value.get("identity")))
                    if (
                        set(value) != {"kind", "version", "identity", "observed_at"}
                        or type(value["version"]) is not int
                        or value["version"] != 1
                        or type(value["observed_at"]) is not int
                        or value["observed_at"] < 0
                        or operation.operation_id
                        != "ordinary:baseline-age:" + digest(encode(identity)) + ":" + str(value["observed_at"])
                    ):
                        raise IntakeRefused("legacy baseline age record differs")
                    old_producer, old_reference, age = (
                        identity.get("baseline_producer"),
                        identity.get("baseline_reference"),
                        value["observed_at"],
                    )
                else:
                    raise IntakeRefused("unexpected legacy receipt age record")
                if (old_producer, old_reference) == (meaning["producer"], meaning["reference"]):
                    ages.append(age)
        for operation in self.store.operations(prefix):
            raw = self.store.find_artifact(operation.operation_id, "record")
            if raw is None:
                raise IntakeRefused("qualified receipt age record is incomplete")
            value = object_from(raw)
            old_meaning = object_from(encode(value.get("meaning")))
            request = object_from(encode(object_from(operation.request_json.encode()).get("request")))
            if (
                set(value) != {"meaning", "observed_at"}
                or type(value["observed_at"]) is not int
                or value["observed_at"] < 0
                or type(old_meaning.get("producer")) is not str
                or type(old_meaning.get("reference")) is not str
                or operation.operation_id != prefix + digest(encode(old_meaning)) + ":" + str(value["observed_at"])
                or operation.effect_key != operation.operation_id
                or operation.attempts
                or request != {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()}
            ):
                raise IntakeRefused("qualified receipt age record differs")
            if (old_meaning["producer"], old_meaning["reference"]) == (meaning["producer"], meaning["reference"]):
                ages.append(value["observed_at"])
        earliest = min(ages)
        if observed_at < min(ages[1:], default=observed_at + 1):
            self._record(
                prefix + digest(encode(meaning)) + ":" + str(earliest), {"meaning": meaning, "observed_at": earliest}
            )
        return earliest

    def propose(
        self,
        program_id: str,
        objective: str,
        selections: tuple[tuple[str, IssueSubject, Declaration], ...],
        *,
        qualified_cuts: tuple[QualifiedDependencyCut, ...] = (),
        qualified_drafts: tuple[QualifiedDraft, ...] = (),
    ) -> OrdinaryProposal:
        drafts: list[Draft] = []
        received: list[DependencyEvidence] = []
        for task_id, subject, declaration in selections:
            history = self.intake.history(task_id)
            draft = self.intake.prepare(
                task_id, subject, declaration, expected_parent=None if not history else history[-1].digest
            )
            drafts.append(draft)
            received.append(self.dependencies.read_dependencies(subject))
        proposal = OrdinaryProposal(
            program_id, objective, tuple(drafts), tuple(received), qualified_cuts, qualified_drafts
        )
        self._spec(proposal, None)  # Core validates missing references, outputs and finite DAG/cycles.
        self._record(f"ordinary-program:proposal:{proposal.digest}", proposal.payload)
        return proposal

    def _record(self, key: str, payload: dict[str, object]) -> bytes:
        raw = encode(payload)
        self.store.intent(key, key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()})
        self.store.finalize_artifact(key, "record", raw)
        return raw

    def _spec(self, proposal: OrdinaryProposal, previous: Program | None) -> ProgramSpec:
        if proposal.qualified_drafts and self.verification.checks[:2] != ("paths", "structural"):
            raise IntakeRefused("qualified tooling requires path and structural checks first")
        if any(cut.required for cut in proposal.qualified_cuts):
            raise IntakeRefused("qualified predecessors cannot yet be mapped to ordinary work units")
        identities = {
            (item.evidence.subject.origin, item.evidence.subject.repository, item.evidence.subject.issue): item.task_id
            for item in proposal.drafts
        }
        if len(identities) != len(proposal.drafts):
            raise IntakeRefused("one unambiguous obligation per selected native issue required")
        inputs = [
            InputBinding("ordinary-declaration", "sha256:" + proposal.digest),
            InputBinding("ordinary-harness-policy", self.execution_reference),
        ]
        units: list[WorkUnit] = []
        for draft, dependency in zip(proposal.drafts, proposal.dependencies, strict=True):
            if not dependency.complete or any(item not in identities for item in dependency.required):
                raise IntakeRefused(
                    "required native predecessor is unknown, unsupported or outside the approved finite Program"
                )
            predecessors = tuple(sorted(identities[item] for item in dependency.required))
            name = f"meaning:{draft.task_id}"
            inputs.append(InputBinding(name, "sha256:" + digest(encode(draft.payload()))))
            units.append(
                WorkUnit(
                    draft.task_id,
                    cast(str, draft.declaration.value["outcome"]),
                    predecessors,
                    frozenset(
                        {
                            name,
                            "ordinary-declaration",
                            "ordinary-harness-policy",
                            *(f"result:{item}" for item in predecessors),
                        }
                    ),
                    frozenset({f"result:{draft.task_id}"}),
                    self.verification.checks,
                    self.policy.reference,
                )
            )
        qualified_ids = {item.cut.subject: item.task_id for item in proposal.qualified_drafts}
        qualified_by_subject = {item.cut.subject: item for item in proposal.qualified_drafts}
        for draft in proposal.qualified_drafts:
            if (not draft.ci_subjects) != ("ci" not in self.verification.checks):
                raise IntakeRefused("qualified CI check and exact build selection must agree")
            if any(required not in qualified_ids for required in draft.cut.required):
                raise IntakeRefused("qualified predecessor is outside the exact approved Program")
            if any(
                (
                    qualified_by_subject[required].baseline.source.binding,
                    qualified_by_subject[required].baseline.source.revision,
                )
                != (draft.baseline.source.binding, draft.baseline.source.revision)
                for required in draft.cut.required
            ):
                raise IntakeRefused("qualified predecessor source repository or base differs")
            predecessors = tuple(sorted(qualified_ids[item] for item in draft.cut.required))
            name = f"meaning:{draft.task_id}"
            inputs.append(InputBinding(name, "sha256:" + digest(encode(draft.payload()))))
            units.append(
                WorkUnit(
                    draft.task_id,
                    cast(str, draft.declaration.value["outcome"]),
                    predecessors,
                    frozenset(
                        {
                            name,
                            "ordinary-declaration",
                            "ordinary-harness-policy",
                            *(f"result:{item}" for item in predecessors),
                        }
                    ),
                    frozenset({f"result:{draft.task_id}"}),
                    self.verification.checks,
                    self.policy.reference,
                )
            )
        # Core criteria name executable trusted predicates. The complete natural
        # language meaning remains an immutable input and is mandatory review input.
        criteria = self.verification.checks
        if previous is None:
            return ProgramSpec(
                proposal.program_id,
                proposal.objective,
                tuple(units),
                tuple(inputs),
                self.policy.budget,
                self.policy.authority,
                acceptance_criteria=criteria,
                policy_references=(self.policy.reference,),
            )
        return previous.spec.amend(
            SpecAmendment(
                previous.spec.revision,
                objective=proposal.objective,
                work_units=tuple(units),
                initial_inputs=tuple(inputs),
                budget=self.policy.budget,
                authority=self.policy.authority,
                acceptance_criteria=criteria,
                policy_references=(self.policy.reference,),
            )
        )

    def current(self, proposal: OrdinaryProposal, expires_at: int) -> None:
        # Complete network/source/history reads before the final pure temporal cut.
        if proposal.qualified_drafts and self.verification.checks[:2] != ("paths", "structural"):
            raise IntakeRefused("qualified tooling requires path and structural checks first")
        reads = tuple(self.intake.reader.read(item.evidence.subject, item.declaration) for item in proposal.drafts)
        edges = tuple(self.dependencies.read_dependencies(item.evidence.subject) for item in proposal.drafts)
        drivers = {driver.binding: driver for driver in self.qualified_work}
        qualified_reads: list[WorkObservation] = []
        cuts = (*proposal.qualified_cuts, *(item.cut for item in proposal.qualified_drafts))
        for cut in cuts:
            driver = drivers.get(cut.subject.binding)
            if driver is None or driver.binding != cut.subject.binding:
                raise IntakeRefused("qualified work driver unavailable or changed")
            qualified_reads.append(driver.work(cut.subject.key))
            if driver.binding != cut.subject.binding:
                raise IntakeRefused("qualified work driver changed during observation")
        scm = {driver.binding: driver for driver in self.qualified_scm}
        qualified_sources: list[DriverFact] = []
        baseline_ages: list[int] = []
        readiness_ages: list[int] = []
        readiness_deadlines: list[int] = []
        for draft in proposal.qualified_drafts:
            driver = scm.get(draft.baseline.source.binding)
            if driver is None or self.qualified_baselines is None or self.qualified_readiness is None:
                raise IntakeRefused("qualified SCM, baseline or relevance producer unavailable")
            source = driver.ref(draft.baseline.source.key)
            baseline = self.qualified_baselines.read_baseline(draft.baseline.source, draft.baseline.recipe_digest)
            readiness = self.qualified_readiness.read_readiness(draft.cut, draft.baseline.source, draft.declaration)
            if (
                driver.binding != draft.baseline.source.binding
                or baseline is None
                or not baseline.proof
                or baseline.meaning != draft.baseline.meaning
                or readiness is None
                or readiness.meaning != draft.readiness.meaning
            ):
                raise IntakeRefused("qualified source or baseline identity changed")
            baseline_ages.append(self._qualified_baseline_age(baseline))
            readiness_ages.append(self._qualified_readiness_age(readiness))
            readiness_deadlines.append(readiness.fresh_until)
            qualified_sources.append(source)
            if readiness.owner_actor != self.owner.actor_id:
                raise IntakeRefused("authenticated owner required")
            OrdinaryIntake.validate_shared_policy(
                self.owner,
                draft.declaration,
                self.policy,
                baseline.producer,
                baseline.reference,
                baseline.result,
                readiness.producer,
            )
        ages: list[int] = []
        for draft, received in zip(proposal.drafts, reads, strict=True):
            self.intake.validate_authority(self.owner, draft, self.policy, received)
            ages.append(self.intake.baseline_age(received))
        for original, current in zip(proposal.dependencies, edges, strict=True):
            if current.meaning != original.meaning or not current.complete:
                raise IntakeRefused("native dependency meaning changed; explicit proposal amendment required")
        minimum = min(
            *(item.evidence.observed_at + self.policy.freshness_seconds for item in proposal.drafts),
            *(item.observed_at + self.policy.freshness_seconds for item in reads),
            *(age + self.policy.freshness_seconds for age in ages),
            *(item.fresh_until for item in (*proposal.dependencies, *edges)),
            *(item.fresh_until for item in cuts),
            *(item.observed_at + self.policy.freshness_seconds for item in cuts),
            *(item.fact.fresh_until for item in qualified_reads),
            *(item.fact.observed_at + self.policy.freshness_seconds for item in qualified_reads),
            *(item.source_fresh_until for item in proposal.qualified_drafts),
            *(item.source_observed_at + self.policy.freshness_seconds for item in proposal.qualified_drafts),
            *(item.fresh_until for item in qualified_sources),
            *(item.observed_at + self.policy.freshness_seconds for item in qualified_sources),
            *(item.baseline.observed_at + self.policy.freshness_seconds for item in proposal.qualified_drafts),
            *(item + self.policy.freshness_seconds for item in baseline_ages),
            *(item.readiness.observed_at + self.policy.freshness_seconds for item in proposal.qualified_drafts),
            *(item + self.policy.freshness_seconds for item in readiness_ages),
            *(item.readiness.fresh_until for item in proposal.qualified_drafts),
            *readiness_deadlines,
        )
        if expires_at > minimum:
            raise IntakeRefused("received evidence narrows the original deadline; explicit approval amendment required")
        now = self.clock()
        for draft, received, age in zip(proposal.drafts, reads, ages, strict=True):
            OrdinaryIntake.check_time(draft, self.policy, received, expires_at, age, now)
        if any(not item.observed_at <= now <= item.fresh_until for item in (*proposal.dependencies, *edges)):
            raise IntakeRefused("original dependency observation expired; refresh and explicitly approve")
        for cut, received in zip(cuts, qualified_reads, strict=True):
            cut.recheck(received, now)
        for draft, source, age, ready_age in zip(
            proposal.qualified_drafts, qualified_sources, baseline_ages, readiness_ages, strict=True
        ):
            source.require_current(now)
            if (
                source.subject != draft.baseline.source
                or not source.complete
                or source.observation_digest != draft.source_digest
                or source.provenance != draft.source_provenance
                or source.qualification != draft.source_qualification
                or not draft.source_observed_at <= now <= draft.source_fresh_until
                or age > now
                or draft.baseline.observed_at > now
                or ready_age > now
                or draft.readiness.observed_at > now
            ):
                raise IntakeRefused("qualified source or baseline changed, expired or is from the future")

    def check_proposal_record(self, proposal: OrdinaryProposal) -> None:
        """Require the exact durable proposal used by approval recovery."""
        raw_proposal = encode(proposal.payload)
        if self.store.find_artifact(f"ordinary-program:proposal:{digest(raw_proposal)}", "record") != raw_proposal:
            raise IntakeRefused("exact proposal record is unavailable")

    def approval_scope(self, proposal: OrdinaryProposal) -> dict[str, object]:
        """Canonical owner-visible inputs that determine the approval's meaning."""
        if self.owner.role != "owner" or self.owner.actor_id != self.policy.authority.owner_id:
            raise IntakeRefused("owner binding is invalid")
        if (
            self.verification.reference != self.policy.reference
            or not self.verification.reviewer_required
            or not {"paths", "structural", "commands"}.issubset(self.verification.checks)
            or "independent_review" in self.verification.checks
            or self.policy.work_budget is None
            or self.policy.project_budget is None
            or self.policy.project_id is None
        ):
            raise IntakeRefused("verification policy binding is invalid")
        profile = object_from(self.execution_profile)
        if (
            set(profile) != {"harness", "provider", "model", "effort", "trust"}
            or profile["harness"] not in {"kilo-pull", "codex", "kilo-pull+codex"}
            or profile["trust"] != "trusted_development"
            or any(
                value is not None and (type(value) is not str or not value)
                for value in (profile["provider"], profile["model"], profile["effort"])
            )
            or self.execution_reference != "basic-harness-policy:" + digest(self.execution_profile)
        ):
            raise IntakeRefused("execution profile binding is invalid")
        scope: dict[str, object] = {
            "kind": "ordinary-approval-scope",
            "version": 1,
            "owner": self.owner.actor_id,
            "proposal": proposal.payload,
            "policy": self.policy.payload(),
            "verification": {
                "reference": self.verification.reference.payload(),
                "checks": list(self.verification.checks),
                "reviewer_required": self.verification.reviewer_required,
            },
            "execution_profile": profile,
            "execution_reference": self.execution_reference,
        }
        if proposal.qualified_cuts or proposal.qualified_drafts:
            scope["qualified_work_bindings"] = [asdict(driver.binding) for driver in self.qualified_work]
        if proposal.qualified_drafts:
            scope["qualified_scm_bindings"] = [asdict(driver.binding) for driver in self.qualified_scm]
        return scope

    def approve(
        self,
        proposal: OrdinaryProposal,
        *,
        decision_id: str,
        expires_at: int,
        expected_revision: int | None = None,
        final_guard: Callable[[], None] | None = None,
    ) -> OrdinaryApproval:
        self.check_proposal_record(proposal)
        if any(
            cut.obligation is not None and cut.obligation.decision_reference != decision_id
            for cut in (*proposal.qualified_cuts, *(item.cut for item in proposal.qualified_drafts))
        ):
            raise IntakeRefused("visible dependency scope must bind this exact owner decision")
        # The caller's expiry is an upper bound. Original producer evidence may
        # expire earlier; bind that bound into the boot-clock work envelope too.
        expires_at = min(
            expires_at,
            *(item.evidence.observed_at + self.policy.freshness_seconds for item in proposal.drafts),
            *(self.intake.baseline_age(item.evidence) + self.policy.freshness_seconds for item in proposal.drafts),
            *(item.fresh_until for item in proposal.dependencies),
            *(item.fresh_until for item in proposal.qualified_cuts),
            *(item.cut.fresh_until for item in proposal.qualified_drafts),
            *(item.cut.observed_at + self.policy.freshness_seconds for item in proposal.qualified_drafts),
            *(item.source_fresh_until for item in proposal.qualified_drafts),
            *(item.source_observed_at + self.policy.freshness_seconds for item in proposal.qualified_drafts),
            *(item.baseline.observed_at + self.policy.freshness_seconds for item in proposal.qualified_drafts),
            *(item.readiness.observed_at + self.policy.freshness_seconds for item in proposal.qualified_drafts),
            *(item.readiness.fresh_until for item in proposal.qualified_drafts),
        )
        previous = self.store.find_program(proposal.program_id)
        if expected_revision is not None:
            if type(expected_revision) is not int or expected_revision < -1:
                raise IntakeRefused("caller expected revision is invalid")
            actual_revision = -1 if previous is None else previous.revision
            if actual_revision != expected_revision:
                raise IntakeRefused("caller expected revision is stale")
        key = f"ordinary-program:approval:{decision_id}"
        prior = self.store.find_artifact(key, "record")
        # Recovery is the same decision, not another amendment or a renewed expiry.
        if prior is not None:
            expected = encode(
                {
                    "proposal": proposal.digest,
                    "policy": self.policy.digest,
                    "issuer": self.owner.actor_id,
                    "expires_at": expires_at,
                    "spec": None if previous is None else previous.spec.digest,
                    "execution": self.execution_reference,
                }
            )
            if prior != expected or previous is None:
                raise IntakeRefused("approval identity changed")
            return OrdinaryApproval(proposal, self.policy.digest, decision_id, expires_at, previous)
        self.current(proposal, expires_at)
        if final_guard is not None:
            final_guard()
        spec = self._spec(proposal, previous)
        record = {
            "proposal": proposal.digest,
            "policy": self.policy.digest,
            "issuer": self.owner.actor_id,
            "expires_at": expires_at,
            "spec": spec.digest,
            "execution": self.execution_reference,
        }
        raw = encode(record)
        if self.policy.work_budget is None or self.policy.project_budget is None or self.policy.project_id is None:
            raise IntakeRefused("finite work and project envelopes are required")
        amendment = (
            None
            if previous is None
            else AmendProgramSpec(
                previous.revision,
                self.owner.actor_id,
                SpecAmendment(
                    previous.spec.revision,
                    objective=spec.objective,
                    work_units=spec.work_units,
                    initial_inputs=spec.initial_inputs,
                    budget=spec.budget,
                    authority=spec.authority,
                    acceptance_criteria=spec.acceptance_criteria,
                    policy_references=spec.policy_references,
                ),
            )
        )
        program = self.store.accept_work_budget(
            spec,
            key,
            self.policy.project_id,
            self.policy.project_budget,
            self.policy.work_budget,
            expires_at,
            amendment=amendment,
            records=((key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()}, raw),),
            validate=lambda: self.current(proposal, expires_at),
            final_guard=final_guard,
            expected_revision=expected_revision,
        )
        return OrdinaryApproval(proposal, self.policy.digest, decision_id, expires_at, program)

    def activate(self, approved: OrdinaryApproval) -> Program:
        self.check(approved)
        program = self.store.load(approved.program.program_id)
        if program.status.value in {"active", "completed"}:
            return program
        return self.store.admit(
            program.program_id,
            f"ordinary-activate:{approved.decision_id}",
            ActivateProgram(program.revision, self.owner.actor_id),
        )

    def recover(self, decision_id: str) -> OrdinaryApproval:
        """Read the exact installed decision without discovery or renewed authority."""
        record = object_from(self.store.artifact(f"ordinary-program:approval:{decision_id}", "record"))
        raw = self.store.artifact(f"ordinary-program:proposal:{record['proposal']}", "record")
        value = object_from(raw)
        legacy_fields = {"version", "kind", "program", "objective", "drafts", "dependencies"}
        if (
            type(value.get("version")) is not int
            or (value["version"] == 1 and set(value) != legacy_fields)
            or (value["version"] == 2 and set(value) != legacy_fields | {"qualified_cuts"})
            or (value["version"] == 3 and set(value) != legacy_fields | {"qualified_cuts", "qualified_drafts"})
            or value["version"] not in (1, 2, 3)
            or value["kind"] != "ordinary-program-proposal"
        ):
            raise IntakeRefused("unsupported historical ordinary proposal")
        drafts = tuple(draft_from(encode(item)) for item in cast(list[object], value["drafts"]))
        edges: list[DependencyEvidence] = []
        for draft, item in zip(drafts, cast(list[dict[str, object]], value["dependencies"]), strict=True):
            if item["subject"] != draft.evidence.subject.payload():
                raise IntakeRefused("historical native dependency subject differs")
            edges.append(
                DependencyEvidence(
                    draft.evidence.subject,
                    tuple((edge[0], edge[1], edge[2]) for edge in cast(list[tuple[str, str, int]], item["required"])),
                    cast(str, item["reference"]),
                    cast(int, item["observed_at"]),
                    cast(int, item["fresh_until"]),
                    cast(bool, item["complete"]),
                )
            )
        cuts = tuple(
            QualifiedDependencyCut.parse(encode(item)) for item in cast(list[object], value.get("qualified_cuts", []))
        )
        qualified_drafts = tuple(
            QualifiedDraft.parse(encode(item)) for item in cast(list[object], value.get("qualified_drafts", []))
        )
        proposal = OrdinaryProposal(
            cast(str, value["program"]), cast(str, value["objective"]), drafts, tuple(edges), cuts, qualified_drafts
        )
        program = self.store.load(proposal.program_id)
        if (
            encode(proposal.payload) != raw
            or proposal.digest != record["proposal"]
            or record["policy"] != self.policy.digest
            or record["issuer"] != self.owner.actor_id
            or record["execution"] != self.execution_reference
            or record["spec"] != program.spec.digest
            or any(
                cut.obligation is not None and cut.obligation.decision_reference != decision_id
                for cut in (*cuts, *(item.cut for item in qualified_drafts))
            )
        ):
            raise IntakeRefused("historical approval, current policy or specification differs")
        return OrdinaryApproval(proposal, self.policy.digest, decision_id, cast(int, record["expires_at"]), program)

    def stop(self, approved: OrdinaryApproval, reason: str, *, expected_revision: int | None = None) -> None:
        if not reason.strip() or self.recover(approved.decision_id).proposal != approved.proposal:
            raise IntakeRefused("exact owner decision and substantive STOP reason required")
        program = self.store.load(approved.program.program_id)
        if expected_revision is not None and program.revision != expected_revision:
            raise IntakeRefused("caller expected revision is stale")
        self.store.stop_ordinary_program(
            program.program_id,
            "ordinary-stop:" + approved.decision_id,
            self.owner.actor_id,
            "ordinary-owner-stop:" + approved.decision_id,
            expected_revision=expected_revision,
        )

    def check(self, approved: OrdinaryApproval) -> None:
        key = f"ordinary-program:approval:{approved.decision_id}"
        if any(
            cut.obligation is not None and cut.obligation.decision_reference != approved.decision_id
            for cut in (*approved.proposal.qualified_cuts, *(item.cut for item in approved.proposal.qualified_drafts))
        ):
            raise IntakeRefused("visible dependency scope differs from owner decision")

        def read(program: Program) -> None:
            if (
                approved.policy_digest != self.policy.digest
                or self.store.artifact(key, "record")
                != encode(
                    {
                        "proposal": approved.proposal.digest,
                        "policy": self.policy.digest,
                        "issuer": self.owner.actor_id,
                        "expires_at": approved.expires_at,
                        "spec": approved.program.spec.digest,
                        "execution": self.execution_reference,
                    }
                )
                or program.spec.digest != approved.program.spec.digest
            ):
                raise IntakeRefused("ordinary approval or current Program differs")
            self.current(approved.proposal, approved.expires_at)

        captured, _, _ = self.store.program_read_cut(approved.program.program_id, read, self.clock)
        now = self.store.work_decision_time(captured.program_id)
        if now >= approved.expires_at:
            raise IntakeRefused("original boot-clock qualified approval expired")

        def close(program: Program) -> None:
            if (program.revision, program.spec.digest) != (captured.revision, captured.spec.digest):
                raise IntakeRefused("ordinary read cut changed during qualified clock acquisition")

        self.store.program_read_cut(captured.program_id, close, lambda: now)
        self.store.check_work_authority(captured.program_id)

    def client(
        self,
        approved: OrdinaryApproval,
        principal: Principal,
        workspace: OrdinaryWorkspace,
        worker: WorkOwnership,
        control: WorkOwnership,
        reviewer: OrdinaryReviewer,
    ) -> "OrdinaryWorkClient":
        if principal.role != "worker" or principal.actor_id not in self.policy.authority.delegated_actor_ids:
            raise IntakeRefused("host-authenticated delegated worker required")
        if workspace.authority_deadline != approved.expires_at or reviewer.authority_deadline != approved.expires_at:
            raise IntakeRefused("original approval deadline must be bound into both concrete producer transports")
        return OrdinaryWorkClient(self, approved, principal, workspace, worker, control, reviewer)


class OrdinaryWorkClient:
    """Installed SDK request/report API, with host-bound identity, not request-body roles."""

    def __init__(
        self,
        application: OrdinaryProgram,
        approved: OrdinaryApproval,
        principal: Principal,
        workspace: OrdinaryWorkspace,
        worker: WorkOwnership,
        control: WorkOwnership,
        reviewer: OrdinaryReviewer,
    ) -> None:
        self.application, self.approved, self.principal = application, approved, principal
        self.workspace, self.worker, self.control, self.reviewer = workspace, worker, control, reviewer

    def request(
        self,
        command_key: str,
        *,
        lease_seconds: int,
        remediation_of: str | None = None,
        expected_revision: int | None = None,
    ) -> OrdinaryHandoff:
        app, approved = self.application, self.approved
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise IntakeRefused("caller expected revision is invalid")
        program = app.store.load(approved.program.program_id)
        # Command identity is supplied for retry, never a caller-selected Attempt,
        # fence, unit, resource, principal, budget or authority envelope.
        attempt_id = "ordinary-" + digest(encode([program.program_id, command_key]))
        prior = next((item for item in program.attempts if item.attempt_id == attempt_id), None)
        saved_revision = None
        if prior is None:
            app.check(approved)
            program = app.store.load(approved.program.program_id)
            if expected_revision is not None and expected_revision != program.revision:
                raise IntakeRefused("caller expected revision is stale")
            ready = program.ready_work_units
            if not ready:
                raise IntakeRefused("no ready obligation in the approved Program")
            unit_id = ready[0].work_unit_id
            earlier = next((item for item in reversed(program.attempts) if item.spec.work_unit_id == unit_id), None)
            if earlier is not None and remediation_of != earlier.attempt_id:
                raise IntakeRefused("a successor to rejected work must retain exact remediation lineage")
            if remediation_of is not None:
                source = program.attempt(remediation_of)
                rejection = app.store.find_artifact(f"rejection:{remediation_of}", "rejected-result")
                if source.spec.work_unit_id != unit_id or rejection is None:
                    raise IntakeRefused("remediation must address the selected current obligation")
                record = app.store.operation(f"rejection:{remediation_of}")
                disposition = object_from(record.request_json.encode())["request"]
                if cast(dict[str, object], disposition).get("product_review_status") not in {None, "changes_requested"}:
                    raise IntakeRefused(
                        "review uncertainty, comment or owner decision does not authorize automatic remediation"
                    )
            attempt = AttemptSpec(
                attempt_id,
                program.program_id,
                unit_id,
                program.spec.revision,
                program.spec.digest,
                program.resolved_inputs(unit_id),
                workspace_reference=self.workspace.reference,
                context_reference=f"ordinary-proposal:{approved.proposal.digest}",
                allocation_reference=app.execution_reference,
                agent_definition_reference=app.execution_reference,
                remediation_of=remediation_of,
            )
            saved_revision = app.store.find_artifact(f"ordinary-client:{attempt_id}", "revision")
            revision = program.revision if saved_revision is None else int(saved_revision)
        else:
            attempt = prior.spec
            if remediation_of != attempt.remediation_of:
                raise IntakeRefused("historical request remediation identity differs")
            # Claim replay compares the original expected revision, not current HEAD.
            raw = app.store.artifact(f"ordinary-client:{attempt_id}", "revision")
            revision = int(raw)
        if expected_revision is not None and expected_revision != revision:
            raise IntakeRefused("caller expected revision differs from original command")
        if prior is None and saved_revision is None:
            app.store.intent(
                f"ordinary-client:{attempt_id}",
                f"ordinary-client:{attempt_id}",
                {"kind": "ordinary-intake-record", "version": 1, "bytes": encode({"revision": revision}).decode()},
            )
            app.store.finalize_artifact(f"ordinary-client:{attempt_id}", "revision", str(revision).encode())
        claim = self.worker.claim(command_key, revision, attempt, lease_seconds=lease_seconds)
        if prior is not None and prior.status.value == "prepared" and app.store.find_ordinary_preparation(claim):
            return OrdinaryHandoff(attempt, claim)  # Historical result only; no authority is renewed.
        if prior is None or prior.status.value == "prepared":
            app.check(approved)
            claim_revision = None if expected_revision is None else revision + 1
            self.worker.check_current(claim, expected_revision=claim_revision)
            subject = self.workspace.initialize(
                attempt, lambda: self.worker.check_current(claim, expected_revision=claim_revision)
            )
            if self.control.observe(claim) != subject:
                raise IntakeRefused("prepared workspace changed before owner admission")
            app.store.accept_ordinary_preparation(claim, app.owner.actor_id, subject, expected_revision=claim_revision)
        return OrdinaryHandoff(attempt, claim)

    def start(self, handoff: OrdinaryHandoff, *, expected_revision: int | None = None) -> int | None:
        app = self.application
        if expected_revision is not None:
            if type(expected_revision) is not int or expected_revision < 0:
                raise IntakeRefused("caller expected revision is invalid")
            if app.store.load(handoff.attempt.program_id).revision != expected_revision:
                raise IntakeRefused("caller expected revision is stale")
        app.check(self.approved)
        self.control.observe(handoff.claim)
        self.workspace.restore(handoff.attempt)
        if handoff.claim.mode is HandoffMode.MANAGED:
            self.worker.witness(handoff.claim, "start", expected_revision=expected_revision)
        program = app.store.load(handoff.attempt.program_id)
        if expected_revision is not None and program.revision != expected_revision:
            raise IntakeRefused("caller expected revision changed during handoff checks")
        if program.attempt(handoff.attempt.attempt_id).status.value == "prepared":
            app.store.admit(
                program.program_id,
                f"ordinary-start:{handoff.attempt.attempt_id}",
                StartAttempt(program.revision, self.principal.actor_id, handoff.attempt.attempt_id),
            )
        return (
            self.control.dispatch(handoff.claim, handoff.claim.operation_id, lease_seconds=60)
            if handoff.claim.mode is HandoffMode.MANAGED
            else None
        )

    def context(self, handoff: OrdinaryHandoff) -> bytes:
        self.application.check(self.approved)
        self.worker.observe(handoff.claim)
        remediation = handoff.attempt.remediation_of
        remediation_data: dict[str, object] | None = None
        if remediation is not None:
            review_operation_id, findings = remediation_context(self.application.store, remediation)
            if not findings:
                raise IntakeRefused("substantive original remediation findings are unavailable")
            remediation_data = {
                "attempt": remediation,
                "rejection": object_from(
                    self.application.store.artifact(f"rejection:{remediation}", "rejected-result")
                ),
                "review_operation_id": review_operation_id,
                "findings": findings,
            }
        return encode(
            {
                "version": 1,
                "attempt": handoff.attempt.digest,
                "work_unit": handoff.attempt.work_unit_id,
                "approved": self.approved.proposal.payload,
                "inputs": [item.payload() for item in handoff.attempt.effective_inputs],
                "workspace": self.workspace.reference,
                "remediation": remediation_data,
                "identity_and_guarantees": (
                    "cooperative trusted-development; no hostile isolation or model measurement attested"
                ),
            }
        )

    def renew(
        self, command_key: str, handoff: OrdinaryHandoff, *, lease_seconds: int, expected_revision: int | None = None
    ) -> int:
        self.application.check(self.approved)
        return self.worker.renew(
            command_key, handoff.claim, lease_seconds=lease_seconds, expected_revision=expected_revision
        )

    def quiesce(
        self, handoff: OrdinaryHandoff, receipt: str | None = None, *, expected_revision: int | None = None
    ) -> None:
        if handoff.claim.mode is HandoffMode.PULL:
            self.application.check(self.approved)
            if receipt is None:
                raise IntakeRefused("explicit authenticated cooperative quiescence receipt required")
            self.worker.quiesce(handoff.claim, receipt, expected_revision=expected_revision)
        else:
            if receipt is not None:
                raise IntakeRefused("managed group completion cannot be asserted by a string")
            self.worker.witness(handoff.claim, "complete", expected_revision=expected_revision)

    def report(
        self, handoff: OrdinaryHandoff, *, cooperative_receipt: str | None = None, expected_revision: int | None = None
    ) -> str:
        app, attempt, claim = self.application, handoff.attempt, handoff.claim
        historical = app.store.load(attempt.program_id)
        state = historical.state(attempt.work_unit_id)
        if historical.spec.digest == attempt.spec_digest and any(
            fact.reference_id == state.satisfaction_id and fact.source_attempt_id == attempt.attempt_id
            for fact in historical.satisfactions
        ):
            return "accepted"  # Historical read, not another acceptance or freshness renewal.
        rejection_id = f"rejection:{attempt.attempt_id}"
        rejected = app.store.find_artifact(rejection_id, "rejected-result")
        if rejected is not None:
            record = app.store.operation(rejection_id)
            request = cast(dict[str, object], object_from(record.request_json.encode()).get("request", {}))
            result = object_from(rejected)
            subject = result.get("subject")
            if (
                claim.attempt != attempt
                or historical.spec.digest != attempt.spec_digest
                or record.operation_id != rejection_id
                or record.effect_key != rejection_id
                or request.get("manifest") != "sha256:" + digest(rejected)
                or type(subject) is not dict
                or cast(dict[str, object], subject).get("attempt_digest") != attempt.digest
                or cast(dict[str, object], subject).get("spec_digest") != attempt.spec_digest
            ):
                raise IntakeRefused("retained rejection differs from the original exact Attempt")
            status = request.get("product_review_status")
            if status in {"owner_decision_needed", "comment", "invalid"}:
                return cast(str, status)
            if status not in {None, "changes_requested"}:
                raise IntakeRefused("retained rejection has an unsupported review status")
            return "rejected"  # Historical read only; no fresh evidence or authority.
        app.check(self.approved)
        if expected_revision is not None and app.store.load(attempt.program_id).revision != expected_revision:
            raise IntakeRefused("caller expected revision is stale")
        if cooperative_receipt is not None:
            self.quiesce(handoff, cooperative_receipt, expected_revision=expected_revision)
        # Changed bytes require a separate exact owner reconciliation call. A
        # worker report cannot silently attribute manual edits to its runtime.
        submitted = app.store.find_artifact(claim.operation_id, "ownership-candidate")
        if submitted is None:
            self.worker.submit(claim, expected_revision=expected_revision)
        elif self.worker.observe(claim) != digest(submitted):
            raise IntakeRefused("workspace differs from the original submitted candidate")
        self.workspace.restore(attempt)
        candidate, collection = self.workspace.collect(attempt, claim)
        program = app.store.load(attempt.program_id)
        if expected_revision is not None and program.revision != expected_revision:
            raise IntakeRefused("caller expected revision changed during candidate collection")
        if program.attempt(attempt.attempt_id).status.value == "executing":
            program = app.store.admit(
                program.program_id,
                f"ordinary-finish:{attempt.attempt_id}",
                FinishAttempt(program.revision, self.principal.actor_id, attempt.attempt_id),
            )

        if submitted is not None and app.verification.reviewer_required:
            result = CandidateResult(
                f"candidate:{attempt.attempt_id}",
                candidate,
                tuple(
                    (name, "candidate.patch") for name in sorted(program.spec.work_unit(attempt.work_unit_id).outputs)
                ),
            )
            subject = EvidenceSubject(
                result.digest,
                attempt.digest,
                program.spec.digest,
                app.verification.reference.digest,
                collection.repository,
                collection.base,
                collection.head,
            )
            request = ProductReviewRequest(
                result,
                subject,
                attempt.attempt_id,
                program.attempt(attempt.attempt_id).actor_id,
                app.verification.reference,
            )
            operation_id = product_review_operation_id(request)
            operations = app.store.operations(operation_id)
            if (
                len(operations) == 1
                and operations[0].operation_id == operation_id
                and operations[0].status in {"unknown", "dispatched"}
            ):

                def no_new_prompt(_operation: str, _lease: int) -> int:
                    raise IntakeRefused("review recovery may not dispatch a new native prompt")

                recovered = dispatch_product_review(
                    app.store,
                    request,
                    self.reviewer,
                    now=app.store.work_decision_time(attempt.program_id),
                    delivery=no_new_prompt,
                )
                if recovered.status is ProductReviewStatus.UNKNOWN:
                    return "review_unknown"

        def current() -> None:
            app.check(self.approved)
            self.control.candidate_subject(claim)

        guard = (
            nullcontext()
            if expected_revision is None
            else app.store.report_revision_guard(program.program_id, program.revision)
        )
        with guard:
            return receive_candidate(
                app.store,
                program,
                attempt.attempt_id,
                candidate,
                collection,
                app.verification,
                now=app.store.work_decision_time(attempt.program_id),
                clock=lambda: app.store.work_decision_time(attempt.program_id),
                artifact_path="candidate.patch",
                product_reviewer=self.reviewer,
                deadline=self.approved.expires_at,
                current_subject=current,
                review_delivery=lambda operation, lease: self.control.dispatch(claim, operation, lease_seconds=lease),
                admitted_checks=collection.checks,
            )
