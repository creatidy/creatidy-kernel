# SPDX-License-Identifier: Apache-2.0
"""One ordinary Program application; native transports and worker requests have no authority.

The trusted host binds its principals, source/check producer and handoff profile.
Pull and managed start share request/report, Core selection and the same immutable
Attempt/Operation. There is no hidden planner, alternate scheduler or model loop.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AmendProgramSpec,
    AttemptSpec,
    CancelProgram,
    FinishAttempt,
    InputBinding,
    Program,
    ProgramSpec,
    SpecAmendment,
    StartAttempt,
    WorkUnit,
)
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
from creatidy_kernel.core.ownership import HandoffMode, WorkClaim
from creatidy_kernel.core.verification import CandidateResult, EvidenceSubject, ProductReviewRequest, VerificationPolicy
from creatidy_kernel.ports.application import Collection, receive_candidate
from creatidy_kernel.ports.intake import IntakeStore, OrdinaryIntake, draft_from
from creatidy_kernel.ports.ownership import OwnershipStore, WorkOwnership
from creatidy_kernel.ports.verification import (
    ProductReviewDispatcher,
    ProductReviewStatus,
    dispatch_product_review,
    product_review_operation_id,
)


class OrdinaryStore(IntakeStore, OwnershipStore, Protocol):
    def work_decision_time(self, program_id: str) -> int: ...
    def revoke_work_budget(self, program_id: str, evidence: str) -> None: ...
    def accept_ordinary_preparation(self, claim: WorkClaim, owner: str, subject: str) -> None: ...


class DependencyReader(Protocol):
    def read_dependencies(self, subject: IssueSubject) -> DependencyEvidence: ...


class OrdinaryWorkspace(Protocol):
    @property
    def authority_deadline(self) -> int: ...
    @property
    def reference(self) -> str: ...
    def initialize(self, attempt: AttemptSpec) -> str: ...
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

    def propose(
        self,
        program_id: str,
        objective: str,
        selections: tuple[tuple[str, IssueSubject, Declaration], ...],
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
        proposal = OrdinaryProposal(program_id, objective, tuple(drafts), tuple(received))
        self._spec(proposal, None)  # Core validates missing references, outputs and finite DAG/cycles.
        self._record(f"ordinary-program:proposal:{proposal.digest}", proposal.payload)
        return proposal

    def _record(self, key: str, payload: dict[str, object]) -> bytes:
        raw = encode(payload)
        self.store.intent(key, key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()})
        self.store.finalize_artifact(key, "record", raw)
        return raw

    def _spec(self, proposal: OrdinaryProposal, previous: Program | None) -> ProgramSpec:
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
        reads = tuple(self.intake.reader.read(item.evidence.subject, item.declaration) for item in proposal.drafts)
        edges = tuple(self.dependencies.read_dependencies(item.evidence.subject) for item in proposal.drafts)
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
        )
        if expires_at > minimum:
            raise IntakeRefused("received evidence narrows the original deadline; explicit approval amendment required")
        now = self.clock()
        for draft, received, age in zip(proposal.drafts, reads, ages, strict=True):
            OrdinaryIntake.check_time(draft, self.policy, received, expires_at, age, now)
        if any(not item.observed_at <= now <= item.fresh_until for item in (*proposal.dependencies, *edges)):
            raise IntakeRefused("original dependency observation expired; refresh and explicitly approve")

    def approve(self, proposal: OrdinaryProposal, *, decision_id: str, expires_at: int) -> OrdinaryApproval:
        # The caller's expiry is an upper bound. Original producer evidence may
        # expire earlier; bind that bound into the boot-clock work envelope too.
        expires_at = min(
            expires_at,
            *(item.evidence.observed_at + self.policy.freshness_seconds for item in proposal.drafts),
            *(self.intake.baseline_age(item.evidence) + self.policy.freshness_seconds for item in proposal.drafts),
            *(item.fresh_until for item in proposal.dependencies),
        )
        previous = self.store.find_program(proposal.program_id)
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
        if (
            set(value) != {"version", "kind", "program", "objective", "drafts", "dependencies"}
            or value["version"] != 1
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
        proposal = OrdinaryProposal(cast(str, value["program"]), cast(str, value["objective"]), drafts, tuple(edges))
        program = self.store.load(proposal.program_id)
        if (
            encode(proposal.payload) != raw
            or proposal.digest != record["proposal"]
            or record["policy"] != self.policy.digest
            or record["issuer"] != self.owner.actor_id
            or record["execution"] != self.execution_reference
            or record["spec"] != program.spec.digest
        ):
            raise IntakeRefused("historical approval, current policy or specification differs")
        return OrdinaryApproval(proposal, self.policy.digest, decision_id, cast(int, record["expires_at"]), program)

    def stop(self, approved: OrdinaryApproval, reason: str) -> None:
        if not reason.strip() or self.recover(approved.decision_id).proposal != approved.proposal:
            raise IntakeRefused("exact owner decision and substantive STOP reason required")
        program = self.store.load(approved.program.program_id)
        if program.status.value != "cancelled":
            self.store.admit(
                program.program_id,
                "ordinary-stop:" + approved.decision_id,
                CancelProgram(program.revision, self.owner.actor_id),
            )
        self.store.revoke_work_budget(program.program_id, "ordinary-owner-stop:" + approved.decision_id)

    def check(self, approved: OrdinaryApproval) -> None:
        key = f"ordinary-program:approval:{approved.decision_id}"

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

    def request(self, command_key: str, *, lease_seconds: int, remediation_of: str | None = None) -> OrdinaryHandoff:
        app, approved = self.application, self.approved
        program = app.store.load(approved.program.program_id)
        # Command identity is supplied for retry, never a caller-selected Attempt,
        # fence, unit, resource, principal, budget or authority envelope.
        attempt_id = "ordinary-" + digest(encode([program.program_id, command_key]))
        prior = next((item for item in program.attempts if item.attempt_id == attempt_id), None)
        saved_revision = None
        if prior is None:
            app.check(approved)
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
        if prior is None and saved_revision is None:
            app.store.intent(
                f"ordinary-client:{attempt_id}",
                f"ordinary-client:{attempt_id}",
                {"kind": "ordinary-intake-record", "version": 1, "bytes": encode({"revision": revision}).decode()},
            )
            app.store.finalize_artifact(f"ordinary-client:{attempt_id}", "revision", str(revision).encode())
        claim = self.worker.claim(command_key, revision, attempt, lease_seconds=lease_seconds)
        if prior is None or prior.status.value == "prepared":
            subject = self.workspace.initialize(attempt)
            if self.control.observe(claim) != subject:
                raise IntakeRefused("prepared workspace changed before owner admission")
            app.store.accept_ordinary_preparation(claim, app.owner.actor_id, subject)
        return OrdinaryHandoff(attempt, claim)

    def start(self, handoff: OrdinaryHandoff) -> int | None:
        app = self.application
        app.check(self.approved)
        self.control.observe(handoff.claim)
        self.workspace.restore(handoff.attempt)
        if handoff.claim.mode is HandoffMode.MANAGED:
            self.worker.witness(handoff.claim, "start")
        program = app.store.load(handoff.attempt.program_id)
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
        return encode(
            {
                "version": 1,
                "attempt": handoff.attempt.digest,
                "work_unit": handoff.attempt.work_unit_id,
                "approved": self.approved.proposal.payload,
                "inputs": [item.payload() for item in handoff.attempt.effective_inputs],
                "workspace": self.workspace.reference,
                "remediation": None
                if remediation is None
                else {
                    "attempt": remediation,
                    "rejection": object_from(
                        self.application.store.artifact(f"rejection:{remediation}", "rejected-result")
                    ),
                },
                "identity_and_guarantees": (
                    "cooperative trusted-development; no hostile isolation or model measurement attested"
                ),
            }
        )

    def renew(self, command_key: str, handoff: OrdinaryHandoff, *, lease_seconds: int) -> int:
        self.application.check(self.approved)
        return self.worker.renew(command_key, handoff.claim, lease_seconds=lease_seconds)

    def quiesce(self, handoff: OrdinaryHandoff, receipt: str | None = None) -> None:
        if handoff.claim.mode is HandoffMode.PULL:
            self.application.check(self.approved)
            if receipt is None:
                raise IntakeRefused("explicit authenticated cooperative quiescence receipt required")
            self.worker.quiesce(handoff.claim, receipt)
        else:
            if receipt is not None:
                raise IntakeRefused("managed group completion cannot be asserted by a string")
            self.worker.witness(handoff.claim, "complete")

    def report(self, handoff: OrdinaryHandoff, *, cooperative_receipt: str | None = None) -> str:
        app, attempt, claim = self.application, handoff.attempt, handoff.claim
        historical = app.store.load(attempt.program_id)
        state = historical.state(attempt.work_unit_id)
        if historical.spec.digest == attempt.spec_digest and any(
            fact.reference_id == state.satisfaction_id and fact.source_attempt_id == attempt.attempt_id
            for fact in historical.satisfactions
        ):
            return "accepted"  # Historical read, not another acceptance or freshness renewal.
        app.check(self.approved)
        if cooperative_receipt is not None:
            self.quiesce(handoff, cooperative_receipt)
        # Changed bytes require a separate exact owner reconciliation call. A
        # worker report cannot silently attribute manual edits to its runtime.
        submitted = app.store.find_artifact(claim.operation_id, "ownership-candidate")
        if submitted is None:
            self.worker.submit(claim)
        elif self.worker.observe(claim) != digest(submitted):
            raise IntakeRefused("workspace differs from the original submitted candidate")
        self.workspace.restore(attempt)
        candidate, collection = self.workspace.collect(attempt, claim)
        program = app.store.load(attempt.program_id)
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
