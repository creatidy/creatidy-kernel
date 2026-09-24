# SPDX-License-Identifier: Apache-2.0
import copy
from dataclasses import FrozenInstanceError, replace

import pytest

from creatidy_kernel.core import domain as domain_module
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AmendProgramSpec,
    AttemptSpec,
    AttemptStatus,
    AuthorityEnvelope,
    AuthorityViolation,
    BudgetExceeded,
    BudgetPolicy,
    CancelProgram,
    CancelWorkUnit,
    CycleDetected,
    DomainAction,
    FailAttempt,
    FinishAttempt,
    IllegalTransition,
    InputBinding,
    InvalidDomainValue,
    MissingInput,
    MissingReference,
    PauseProgram,
    PrepareAttempt,
    Program,
    ProgramSpec,
    ProgramStatus,
    ResumeProgram,
    SatisfyWorkUnit,
    SpecAmendment,
    StaleRevision,
    StartAttempt,
    TrustedSatisfaction,
    WorkUnit,
    WorkUnitStatus,
)

OWNER = "owner"


def graph_spec(
    *,
    budget: BudgetPolicy | None = None,
    authority: AuthorityEnvelope | None = None,
) -> ProgramSpec:
    return ProgramSpec(
        program_id="program",
        objective="build the bounded result",
        initial_inputs=frozenset({"seed"}),
        work_units=(
            WorkUnit("first", required_inputs=frozenset({"seed"}), outputs=frozenset({"first-output"})),
            WorkUnit(
                "second",
                dependencies=("first",),
                required_inputs=frozenset({"first-output"}),
                outputs=frozenset({"final-output"}),
            ),
        ),
        budget=BudgetPolicy() if budget is None else budget,
        authority=AuthorityEnvelope(OWNER) if authority is None else authority,
    )


def attempt_spec(program: Program, attempt_id: str, work_unit_id: str, input_name: str) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        program_id=program.program_id,
        work_unit_id=work_unit_id,
        spec_revision=program.spec.revision,
        spec_digest=program.spec.digest,
        effective_inputs=(InputBinding(input_name, f"ref:{input_name}"),),
        agent_definition_reference="agent:v1",
    )


def satisfaction(program: Program, work_unit_id: str, reference_id: str, attempt_id: str | None) -> TrustedSatisfaction:
    unit = program.spec.work_unit(work_unit_id)
    return TrustedSatisfaction(
        reference_id=reference_id,
        program_id=program.program_id,
        work_unit_id=work_unit_id,
        spec_revision=program.spec.revision,
        spec_digest=program.spec.digest,
        work_unit_digest=unit.digest,
        issuer_id=OWNER,
        source_attempt_id=attempt_id,
    )


def test_two_node_graph_advances_only_through_legal_commands() -> None:
    program = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))

    assert program.status is ProgramStatus.ACTIVE
    assert program.ready_work_unit_ids == ("first",)

    prepared = program.apply(PrepareAttempt(program.revision, OWNER, attempt_spec(program, "a1", "first", "seed")))
    executing = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1"))
    finished = executing.apply(FinishAttempt(executing.revision, OWNER, "a1"))
    assert finished.state("first").status is WorkUnitStatus.READY
    assert finished.state("first").active_attempt_id is None
    assert finished.attempt("a1").status is AttemptStatus.FINISHED
    advanced = finished.apply(SatisfyWorkUnit(finished.revision, OWNER, satisfaction(finished, "first", "s1", "a1")))

    assert advanced.state("first").status is WorkUnitStatus.SATISFIED
    assert advanced.ready_work_unit_ids == ("second",)
    assert advanced.attempt("a1").status is AttemptStatus.FINISHED

    second = advanced.apply(
        PrepareAttempt(advanced.revision, OWNER, attempt_spec(advanced, "a2", "second", "first-output"))
    )
    assert second.state("second").status is WorkUnitStatus.ACTIVE


def test_finished_attempt_releases_work_unit_for_retry_or_cancellation() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    prepared = active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))
    executing = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1"))
    finished = executing.apply(FinishAttempt(executing.revision, OWNER, "a1"))

    retry = finished.apply(PrepareAttempt(finished.revision, OWNER, attempt_spec(finished, "a2", "first", "seed")))
    assert retry.state("first").status is WorkUnitStatus.ACTIVE
    assert retry.attempt("a1").status is AttemptStatus.FINISHED

    cancelled = finished.apply(CancelWorkUnit(finished.revision, OWNER, "first"))
    assert cancelled.state("first").status is WorkUnitStatus.CANCELLED
    assert cancelled.state("second").status is WorkUnitStatus.CANCELLED
    assert cancelled.status is ProgramStatus.CANCELLED


def test_finished_attempt_consumes_finite_budget_without_corrupting_ready_state() -> None:
    active = Program.create(graph_spec(budget=BudgetPolicy(max_attempts=1, max_active_attempts=1))).apply(
        ActivateProgram(0, OWNER)
    )
    prepared = active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))
    executing = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1"))
    finished = executing.apply(FinishAttempt(executing.revision, OWNER, "a1"))

    with pytest.raises(BudgetExceeded):
        finished.apply(PrepareAttempt(finished.revision, OWNER, attempt_spec(finished, "a2", "first", "seed")))

    assert finished.state("first").status is WorkUnitStatus.READY
    assert finished.attempt("a1").status is AttemptStatus.FINISHED


def test_trusted_satisfaction_without_source_closes_active_attempt_bookkeeping() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    prepared = active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))
    reference = satisfaction(prepared, "first", "s1", None)

    advanced = prepared.apply(SatisfyWorkUnit(prepared.revision, OWNER, reference))

    assert advanced.state("first").status is WorkUnitStatus.SATISFIED
    assert advanced.attempt("a1").status is AttemptStatus.FINISHED
    assert advanced.ready_work_unit_ids == ("second",)


def test_graph_rejects_missing_references_inputs_and_cycles() -> None:
    with pytest.raises(MissingReference):
        ProgramSpec("p", "objective", (WorkUnit("child", dependencies=("missing",)),))
    with pytest.raises(MissingInput):
        ProgramSpec("p", "objective", (WorkUnit("root", required_inputs=frozenset({"missing"})),))
    with pytest.raises(CycleDetected):
        ProgramSpec(
            "p",
            "objective",
            (WorkUnit("a", dependencies=("b",)), WorkUnit("b", dependencies=("a",))),
        )


def test_expected_revisions_and_illegal_transitions_are_deterministic() -> None:
    program = Program.create(graph_spec())
    with pytest.raises(InvalidDomainValue):
        Program(graph_spec(), status=ProgramStatus.ACTIVE)
    with pytest.raises(StaleRevision):
        program.apply(ActivateProgram(1, OWNER))

    active = program.apply(ActivateProgram(0, OWNER))
    with pytest.raises(IllegalTransition):
        active.apply(ActivateProgram(active.revision, OWNER))
    with pytest.raises(MissingReference):
        active.apply(FinishAttempt(active.revision, OWNER, "unknown"))


def test_dataclass_replace_cannot_inject_a_lifecycle_state() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))

    with pytest.raises(ValueError):
        replace(active, status=ProgramStatus.DRAFT)
    with pytest.raises(TypeError):
        replace(active, _copy_seal=None)  # type: ignore[call-arg]

    replacement_spec = ProgramSpec(
        "replacement",
        "forged replacement",
        (WorkUnit("replacement"),),
        authority=AuthorityEnvelope(OWNER, frozenset()),
    )
    with pytest.raises(ValueError):
        replace(
            active,
            spec=replacement_spec,
            status=ProgramStatus.DRAFT,
            revision=0,
            work_unit_states=(),
            attempts=(),
            satisfactions=(),
            spec_history=(),
        )


def test_internal_transition_builder_is_not_a_supported_construction_path() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))

    assert not hasattr(active, "_next")
    assert not hasattr(Program, "_from_transition")
    assert not hasattr(domain_module, "_TRANSITION_TOKEN")
    with pytest.raises(AttributeError):
        active._next(status=ProgramStatus.CANCELLED, _token=None)  # type: ignore[attr-defined]
    with pytest.raises(InvalidDomainValue):
        active.apply(object())  # type: ignore[arg-type]
    assert active.status is ProgramStatus.ACTIVE


def test_program_subclass_cannot_override_validation_or_authorization() -> None:
    def forged_authorize(
        self: Program,
        action: DomainAction,
        actor_id: str,
        *,
        trusted_satisfaction: bool = False,
    ) -> None:
        del self, action, actor_id, trusted_satisfaction

    def forged_validate(self: Program, *, allow_transition: bool) -> None:
        del self, allow_transition

    with pytest.raises(TypeError):
        type(
            "ForgedProgram",
            (Program,),
            {"_authorize": forged_authorize, "_validate": forged_validate},
        )


def test_amendment_retains_a_control_path_for_an_active_program() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    amendment = AmendProgramSpec(
        active.revision,
        OWNER,
        SpecAmendment(
            expected_revision=1,
            authority=AuthorityEnvelope(OWNER, frozenset({DomainAction.CANCEL_PROGRAM})),
        ),
    )

    amended = active.apply(amendment)

    assert amended.status is ProgramStatus.ACTIVE
    cancelled = amended.apply(CancelProgram(amended.revision, OWNER))
    assert cancelled.status is ProgramStatus.CANCELLED


def test_amendment_cannot_strand_an_active_program_without_control_actions() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    amendment = AmendProgramSpec(
        active.revision,
        OWNER,
        SpecAmendment(expected_revision=1, authority=AuthorityEnvelope(OWNER, frozenset())),
    )

    with pytest.raises(AuthorityViolation):
        active.apply(amendment)


def test_paused_amendment_cannot_remove_control_needed_after_resume() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    paused = active.apply(PauseProgram(active.revision, OWNER))
    amendment = AmendProgramSpec(
        paused.revision,
        OWNER,
        SpecAmendment(
            expected_revision=1,
            authority=AuthorityEnvelope(OWNER, frozenset({DomainAction.RESUME_PROGRAM})),
        ),
    )

    with pytest.raises(AuthorityViolation):
        paused.apply(amendment)


def test_resume_transition_rejects_an_actionless_active_state() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    paused = active.apply(PauseProgram(active.revision, OWNER))
    actionless_spec = paused.spec.amend(
        SpecAmendment(
            expected_revision=1,
            authority=AuthorityEnvelope(OWNER, frozenset({DomainAction.RESUME_PROGRAM})),
        )
    )

    with pytest.raises(AuthorityViolation):
        paused.apply(
            AmendProgramSpec(
                paused.revision,
                OWNER,
                SpecAmendment(
                    expected_revision=1,
                    authority=actionless_spec.authority,
                ),
            )
        )


def test_paused_amendment_and_resume_retain_an_operable_control_path() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    paused = active.apply(PauseProgram(active.revision, OWNER))
    amendment = AmendProgramSpec(
        paused.revision,
        OWNER,
        SpecAmendment(
            expected_revision=1,
            authority=AuthorityEnvelope(
                OWNER,
                frozenset({DomainAction.RESUME_PROGRAM, DomainAction.CANCEL_PROGRAM}),
            ),
        ),
    )

    amended = paused.apply(amendment)
    resumed = amended.apply(ResumeProgram(amended.revision, OWNER))

    assert resumed.status is ProgramStatus.ACTIVE
    assert resumed.spec.authority.allowed_actions == frozenset(
        {DomainAction.RESUME_PROGRAM, DomainAction.CANCEL_PROGRAM}
    )
    assert resumed.apply(CancelProgram(resumed.revision, OWNER)).status is ProgramStatus.CANCELLED


def test_paused_amendment_remains_a_real_control_path_without_resume_or_cancel() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    paused = active.apply(PauseProgram(active.revision, OWNER))
    amendment = AmendProgramSpec(
        paused.revision,
        OWNER,
        SpecAmendment(
            expected_revision=1,
            authority=AuthorityEnvelope(OWNER, frozenset({DomainAction.AMEND_SPEC})),
        ),
    )

    amended = paused.apply(amendment)
    changed = amended.apply(
        AmendProgramSpec(
            amended.revision,
            OWNER,
            SpecAmendment(expected_revision=2, objective="revised while paused"),
        )
    )

    assert changed.status is ProgramStatus.PAUSED
    assert changed.spec.objective == "revised while paused"
    assert changed.spec.authority.allowed_actions == frozenset({DomainAction.AMEND_SPEC})


def test_work_unit_cancellation_propagates_and_closes_an_unfinishable_graph() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))

    cancelled = active.apply(CancelWorkUnit(active.revision, OWNER, "first"))

    assert cancelled.state("first").status is WorkUnitStatus.CANCELLED
    assert cancelled.state("second").status is WorkUnitStatus.CANCELLED
    assert cancelled.status is ProgramStatus.CANCELLED


def test_least_privilege_active_work_unit_cancellation_needs_no_program_cancel() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset({DomainAction.ACTIVATE_PROGRAM, DomainAction.CANCEL_WORK_UNIT}),
    )
    spec = ProgramSpec("p", "objective", (WorkUnit("only"),), authority=authority)

    active = Program.create(spec).apply(ActivateProgram(0, OWNER))

    assert DomainAction.CANCEL_PROGRAM not in active.spec.authority.allowed_actions
    assert active.apply(CancelWorkUnit(active.revision, OWNER, "only")).status is ProgramStatus.CANCELLED


def test_active_program_can_retain_amendment_control_without_program_cancellation() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset({DomainAction.ACTIVATE_PROGRAM, DomainAction.AMEND_SPEC}),
    )
    active = Program.create(ProgramSpec("p", "objective", (WorkUnit("only"),), authority=authority)).apply(
        ActivateProgram(0, OWNER)
    )

    amended = active.apply(
        AmendProgramSpec(
            active.revision,
            OWNER,
            SpecAmendment(expected_revision=1, objective="revised objective"),
        )
    )

    assert amended.status is ProgramStatus.ACTIVE
    assert amended.spec.objective == "revised objective"
    assert DomainAction.CANCEL_PROGRAM not in amended.spec.authority.allowed_actions


def test_active_and_paused_states_without_any_legal_path_are_rejected() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset({DomainAction.ACTIVATE_PROGRAM, DomainAction.PAUSE_PROGRAM}),
    )
    spec = ProgramSpec("p", "objective", (WorkUnit("only"),), authority=authority)

    with pytest.raises(AuthorityViolation):
        Program.create(spec)


def test_attempt_admission_rejects_a_prepared_state_with_no_legal_followup() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset(
            {
                DomainAction.ACTIVATE_PROGRAM,
                DomainAction.PREPARE_ATTEMPT,
                DomainAction.FINISH_ATTEMPT,
                DomainAction.CANCEL_WORK_UNIT,
            }
        ),
    )
    active = Program.create(ProgramSpec("p", "objective", (WorkUnit("only"),), authority=authority)).apply(
        ActivateProgram(0, OWNER)
    )

    with pytest.raises(AuthorityViolation):
        active.apply(
            PrepareAttempt(
                active.revision,
                OWNER,
                AttemptSpec("a1", active.program_id, "only", active.spec.revision, active.spec.digest),
            )
        )

    assert active.state("only").status is WorkUnitStatus.READY
    assert active.attempts == ()


def test_least_authority_finish_then_cancel_path_remains_live() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset(
            {
                DomainAction.ACTIVATE_PROGRAM,
                DomainAction.PREPARE_ATTEMPT,
                DomainAction.START_ATTEMPT,
                DomainAction.FINISH_ATTEMPT,
                DomainAction.CANCEL_WORK_UNIT,
            }
        ),
    )
    draft = Program.create(ProgramSpec("p", "objective", (WorkUnit("only"),), authority=authority))
    active = draft.apply(ActivateProgram(draft.revision, OWNER))
    prepared = active.apply(
        PrepareAttempt(
            active.revision,
            OWNER,
            AttemptSpec("a1", active.program_id, "only", active.spec.revision, active.spec.digest),
        )
    )
    with pytest.raises(AuthorityViolation):
        prepared.apply(FailAttempt(prepared.revision, OWNER, "a1"))
    executing = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1"))
    finished = executing.apply(FinishAttempt(executing.revision, OWNER, "a1"))
    cancelled = finished.apply(CancelWorkUnit(finished.revision, OWNER, "only"))

    assert finished.state("only").status is WorkUnitStatus.READY
    assert finished.attempt("a1").status is AttemptStatus.FINISHED
    assert cancelled.status is ProgramStatus.CANCELLED


def test_least_authority_prepare_then_trusted_satisfaction_needs_no_start() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset(
            {
                DomainAction.ACTIVATE_PROGRAM,
                DomainAction.PREPARE_ATTEMPT,
                DomainAction.SATISFY_WORK_UNIT,
            }
        ),
    )
    draft = Program.create(ProgramSpec("p", "objective", (WorkUnit("only"),), authority=authority))
    active = draft.apply(ActivateProgram(draft.revision, OWNER))
    prepared = active.apply(
        PrepareAttempt(
            active.revision,
            OWNER,
            AttemptSpec("a1", active.program_id, "only", active.spec.revision, active.spec.digest),
        )
    )
    with pytest.raises(AuthorityViolation):
        prepared.apply(StartAttempt(prepared.revision, OWNER, "a1"))
    satisfied = prepared.apply(
        SatisfyWorkUnit(
            prepared.revision,
            OWNER,
            satisfaction(prepared, "only", "s1", None),
        )
    )

    assert prepared.state("only").status is WorkUnitStatus.ACTIVE
    assert prepared.attempt("a1").status is AttemptStatus.PREPARED
    assert satisfied.status is ProgramStatus.COMPLETED
    assert satisfied.attempt("a1").status is AttemptStatus.FINISHED


def test_pause_requires_a_viable_resume_or_termination_path() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset(
            {
                DomainAction.ACTIVATE_PROGRAM,
                DomainAction.PAUSE_PROGRAM,
                DomainAction.RESUME_PROGRAM,
                DomainAction.CANCEL_WORK_UNIT,
            }
        ),
    )
    active = Program.create(ProgramSpec("p", "objective", (WorkUnit("only"),), authority=authority)).apply(
        ActivateProgram(0, OWNER)
    )

    paused = active.apply(PauseProgram(active.revision, OWNER))
    resumed = paused.apply(ResumeProgram(paused.revision, OWNER))

    assert resumed.status is ProgramStatus.ACTIVE
    assert resumed.apply(CancelWorkUnit(resumed.revision, OWNER, "only")).status is ProgramStatus.CANCELLED


def test_objective_amendment_preserves_cancelled_work_unit() -> None:
    spec = ProgramSpec("p", "objective", (WorkUnit("a"), WorkUnit("b")))
    active = Program.create(spec).apply(ActivateProgram(0, OWNER))
    cancelled = active.apply(CancelWorkUnit(active.revision, OWNER, "a"))

    amended = cancelled.apply(
        AmendProgramSpec(
            cancelled.revision,
            OWNER,
            SpecAmendment(expected_revision=1, objective="new objective"),
        )
    )

    assert amended.state("a").status is WorkUnitStatus.CANCELLED
    assert amended.state("b").status is WorkUnitStatus.READY


def test_unrelated_amendment_preserves_cancelled_dependency_branch() -> None:
    units = (
        WorkUnit("a-root"),
        WorkUnit("a-child", dependencies=("a-root",)),
        WorkUnit("b-root"),
        WorkUnit("b-child", dependencies=("b-root",)),
    )
    active = Program.create(ProgramSpec("p", "objective", units)).apply(ActivateProgram(0, OWNER))
    cancelled = active.apply(CancelWorkUnit(active.revision, OWNER, "a-root"))

    amended = cancelled.apply(
        AmendProgramSpec(
            cancelled.revision,
            OWNER,
            SpecAmendment(expected_revision=1, objective="new objective"),
        )
    )

    assert amended.state("a-root").status is WorkUnitStatus.CANCELLED
    assert amended.state("a-child").status is WorkUnitStatus.CANCELLED
    assert amended.state("b-root").status is WorkUnitStatus.READY
    assert amended.state("b-child").status is WorkUnitStatus.PENDING


def test_material_work_unit_amendment_reevaluates_prior_cancellation() -> None:
    first = WorkUnit("first")
    second = WorkUnit("second")
    active = Program.create(ProgramSpec("p", "objective", (first, second))).apply(ActivateProgram(0, OWNER))
    cancelled = active.apply(CancelWorkUnit(active.revision, OWNER, "first"))
    amended = cancelled.apply(
        AmendProgramSpec(
            cancelled.revision,
            OWNER,
            SpecAmendment(
                expected_revision=1,
                work_units=(WorkUnit("first", outputs=frozenset({"new-output"})), second),
            ),
        )
    )

    assert amended.state("first").status is WorkUnitStatus.READY


def test_satisfying_remaining_work_after_cancellation_closes_cancelled_program() -> None:
    spec = ProgramSpec("p", "objective", (WorkUnit("cancelled"), WorkUnit("remaining")))
    active = Program.create(spec).apply(ActivateProgram(0, OWNER))
    cancelled = active.apply(CancelWorkUnit(active.revision, OWNER, "cancelled"))
    prepared = cancelled.apply(
        PrepareAttempt(
            cancelled.revision,
            OWNER,
            AttemptSpec(
                "a1",
                cancelled.program_id,
                "remaining",
                cancelled.spec.revision,
                cancelled.spec.digest,
            ),
        )
    )
    executing = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1"))
    finished = executing.apply(FinishAttempt(executing.revision, OWNER, "a1"))
    terminated = finished.apply(
        SatisfyWorkUnit(
            finished.revision,
            OWNER,
            satisfaction(finished, "remaining", "s1", "a1"),
        )
    )

    assert terminated.state("cancelled").status is WorkUnitStatus.CANCELLED
    assert terminated.state("remaining").status is WorkUnitStatus.SATISFIED
    assert terminated.status is ProgramStatus.CANCELLED


def test_ordinary_copying_and_subclassing_cannot_change_immutable_intent() -> None:
    spec = graph_spec()
    attempt = attempt_spec(Program.create(spec).apply(ActivateProgram(0, OWNER)), "a1", "first", "seed")

    with pytest.raises(InvalidDomainValue):
        copy.copy(spec)
    with pytest.raises(InvalidDomainValue):
        copy.deepcopy(attempt)
    with pytest.raises(ValueError):
        replace(spec, objective="changed")
    with pytest.raises(ValueError):
        replace(attempt, effective_inputs=(InputBinding("seed", "changed"),))

    def forged_payload(self: WorkUnit) -> dict[str, object]:
        del self
        return {"forged": True}

    with pytest.raises(TypeError):
        type("ForgedWorkUnit", (WorkUnit,), {"payload": forged_payload})

    def forged_spec_payload(self: ProgramSpec) -> dict[str, object]:
        del self
        return {"forged": True}

    with pytest.raises(TypeError):
        type("ForgedProgramSpec", (ProgramSpec,), {"payload": forged_spec_payload})

    def forged_input_reference(self: InputBinding) -> str:
        del self
        return "changed"

    with pytest.raises(TypeError):
        type(
            "ForgedInputBinding",
            (InputBinding,),
            {"reference": property(forged_input_reference)},
        )


def test_repeated_initialization_cannot_rewrite_semantic_values() -> None:
    spec = graph_spec()
    first = spec.work_unit("first")
    binding = InputBinding("seed", "ref:seed")
    program = Program.create(spec).apply(ActivateProgram(0, OWNER))
    prepared = program.apply(PrepareAttempt(program.revision, OWNER, attempt_spec(program, "a1", "first", "seed")))
    executing = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1"))
    finished = executing.apply(FinishAttempt(executing.revision, OWNER, "a1"))
    finished_attempt = finished.attempt("a1")
    pinned_attempt = finished_attempt.spec
    original_digest = spec.digest

    with pytest.raises(InvalidDomainValue):
        spec.__init__("changed", "changed", (WorkUnit("changed"),))
    with pytest.raises(InvalidDomainValue):
        first.__init__("changed")
    with pytest.raises(InvalidDomainValue):
        binding.__init__("seed", "changed")
    with pytest.raises(InvalidDomainValue):
        pinned_attempt.__init__(
            "a2",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            (InputBinding("seed", "changed"),),
        )
    with pytest.raises(InvalidDomainValue):
        finished_attempt.__init__(pinned_attempt, AttemptStatus.FAILED)
    with pytest.raises(InvalidDomainValue):
        finished.__init__(spec=spec, status=ProgramStatus.DRAFT)

    assert spec.digest == original_digest
    assert finished_attempt.attempt_id == "a1"
    assert finished_attempt.status is AttemptStatus.FINISHED
    assert pinned_attempt.input_names == frozenset({"seed"})


def test_forged_command_subclass_cannot_authorize_one_action_and_execute_another() -> None:
    authority = AuthorityEnvelope(
        OWNER,
        frozenset(
            {
                DomainAction.ACTIVATE_PROGRAM,
                DomainAction.PREPARE_ATTEMPT,
                DomainAction.CANCEL_PROGRAM,
            }
        ),
    )
    program = Program.create(graph_spec(authority=authority))

    class ForgedActivate(ActivateProgram):
        action = DomainAction.PREPARE_ATTEMPT

    with pytest.raises(InvalidDomainValue):
        program.apply(ForgedActivate(0, OWNER))


def test_authority_and_admission_budgets_are_checked_before_attempts() -> None:
    restricted = AuthorityEnvelope(OWNER, frozenset({DomainAction.ACTIVATE_PROGRAM, DomainAction.CANCEL_PROGRAM}))
    active = Program.create(graph_spec(authority=restricted)).apply(ActivateProgram(0, OWNER))
    with pytest.raises(AuthorityViolation):
        active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))

    budgeted = Program.create(graph_spec(budget=BudgetPolicy(max_attempts=1, max_active_attempts=1))).apply(
        ActivateProgram(0, OWNER)
    )
    prepared = budgeted.apply(PrepareAttempt(budgeted.revision, OWNER, attempt_spec(budgeted, "a1", "first", "seed")))
    failed = prepared.apply(FailAttempt(prepared.revision, OWNER, "a1"))
    with pytest.raises(BudgetExceeded):
        failed.apply(PrepareAttempt(failed.revision, OWNER, attempt_spec(failed, "a2", "first", "seed")))


def test_attempt_inputs_are_immutable_and_amendments_preserve_history() -> None:
    program = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    prepared = program.apply(PrepareAttempt(program.revision, OWNER, attempt_spec(program, "a1", "first", "seed")))
    finished = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1")).apply(
        FinishAttempt(prepared.revision + 1, OWNER, "a1")
    )
    old_attempt = finished.attempt("a1")

    with pytest.raises(FrozenInstanceError):
        old_attempt.spec.effective_inputs = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        old_attempt.spec.effective_inputs[0] = InputBinding("seed", "changed")  # type: ignore[index]

    amended = finished.apply(
        AmendProgramSpec(
            finished.revision,
            OWNER,
            SpecAmendment(expected_revision=1, objective="amended objective"),
        )
    )
    assert amended.spec.revision == 2
    assert amended.spec.parent_digest == finished.spec.digest
    assert amended.attempt("a1").spec.spec_revision == 1
    assert amended.attempt("a1").spec.spec_digest == finished.spec.digest
    assert amended.spec_history == (finished.spec, amended.spec)


def test_amendment_rechecks_readiness_without_deleting_satisfaction_reference() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    prepared = active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))
    finished = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1")).apply(
        FinishAttempt(prepared.revision + 1, OWNER, "a1")
    )
    advanced = finished.apply(SatisfyWorkUnit(finished.revision, OWNER, satisfaction(finished, "first", "s1", "a1")))

    amended = advanced.apply(
        AmendProgramSpec(
            advanced.revision,
            OWNER,
            SpecAmendment(expected_revision=1, objective="new objective"),
        )
    )
    assert amended.state("first").status is WorkUnitStatus.SATISFIED
    assert amended.ready_work_unit_ids == ("second",)
    assert amended.satisfactions[0].reference_id == "s1"


def test_amendment_cancels_attempts_pinned_to_the_old_spec() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    prepared = active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))

    amended = prepared.apply(
        AmendProgramSpec(
            prepared.revision,
            OWNER,
            SpecAmendment(expected_revision=1, objective="new objective"),
        )
    )

    assert amended.attempt("a1").status is AttemptStatus.CANCELLED
    assert amended.state("first").status is WorkUnitStatus.READY
    replacement = amended.apply(PrepareAttempt(amended.revision, OWNER, attempt_spec(amended, "a2", "first", "seed")))
    assert replacement.state("first").active_attempt_id == "a2"


def test_amendment_removing_last_pending_work_unit_completes_active_program() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    prepared = active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))
    finished = prepared.apply(StartAttempt(prepared.revision, OWNER, "a1")).apply(
        FinishAttempt(prepared.revision + 1, OWNER, "a1")
    )
    satisfied = finished.apply(SatisfyWorkUnit(finished.revision, OWNER, satisfaction(finished, "first", "s1", "a1")))
    first = satisfied.spec.work_unit("first")

    amended = satisfied.apply(
        AmendProgramSpec(
            satisfied.revision,
            OWNER,
            SpecAmendment(expected_revision=1, work_units=(first,)),
        )
    )

    assert amended.status is ProgramStatus.COMPLETED
    assert amended.ready_work_unit_ids == ()


def test_amendment_invalidates_downstream_satisfaction_after_predecessor_change() -> None:
    active = Program.create(graph_spec()).apply(ActivateProgram(0, OWNER))
    first_prepared = active.apply(PrepareAttempt(active.revision, OWNER, attempt_spec(active, "a1", "first", "seed")))
    first_finished = first_prepared.apply(StartAttempt(first_prepared.revision, OWNER, "a1")).apply(
        FinishAttempt(first_prepared.revision + 1, OWNER, "a1")
    )
    first_satisfied = first_finished.apply(
        SatisfyWorkUnit(
            first_finished.revision,
            OWNER,
            satisfaction(first_finished, "first", "s1", "a1"),
        )
    )
    second_prepared = first_satisfied.apply(
        PrepareAttempt(
            first_satisfied.revision,
            OWNER,
            attempt_spec(first_satisfied, "a2", "second", "first-output"),
        )
    )
    second_finished = second_prepared.apply(StartAttempt(second_prepared.revision, OWNER, "a2")).apply(
        FinishAttempt(second_prepared.revision + 1, OWNER, "a2")
    )
    both_satisfied = second_finished.apply(
        SatisfyWorkUnit(
            second_finished.revision,
            OWNER,
            satisfaction(second_finished, "second", "s2", "a2"),
        )
    )
    changed_first = WorkUnit(
        "first",
        required_inputs=frozenset({"seed"}),
        outputs=frozenset({"first-output", "additional-output"}),
    )

    amended = both_satisfied.apply(
        AmendProgramSpec(
            both_satisfied.revision,
            OWNER,
            SpecAmendment(
                expected_revision=1,
                work_units=(changed_first, both_satisfied.spec.work_unit("second")),
            ),
        )
    )

    assert amended.status is ProgramStatus.ACTIVE
    assert amended.state("first").status is WorkUnitStatus.READY
    assert amended.state("second").status is WorkUnitStatus.PENDING
    assert amended.ready_work_unit_ids == ("first",)
    assert tuple(item.reference_id for item in amended.satisfactions) == ("s1", "s2")
