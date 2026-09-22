# SPDX-License-Identifier: Apache-2.0
from dataclasses import FrozenInstanceError

import pytest

from creatidy_kernel.core.domain import (
    ActivateProgram,
    AmendProgramSpec,
    AttemptSpec,
    AttemptStatus,
    AuthorityEnvelope,
    AuthorityViolation,
    BudgetExceeded,
    BudgetPolicy,
    CycleDetected,
    DomainAction,
    FailAttempt,
    FinishAttempt,
    IllegalTransition,
    InputBinding,
    InvalidDomainValue,
    MissingInput,
    MissingReference,
    PrepareAttempt,
    Program,
    ProgramSpec,
    ProgramStatus,
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
    advanced = finished.apply(SatisfyWorkUnit(finished.revision, OWNER, satisfaction(finished, "first", "s1", "a1")))

    assert advanced.state("first").status is WorkUnitStatus.SATISFIED
    assert advanced.ready_work_unit_ids == ("second",)
    assert advanced.attempt("a1").status is AttemptStatus.FINISHED

    second = advanced.apply(
        PrepareAttempt(advanced.revision, OWNER, attempt_spec(advanced, "a2", "second", "first-output"))
    )
    assert second.state("second").status is WorkUnitStatus.ACTIVE


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


def test_authority_and_admission_budgets_are_checked_before_attempts() -> None:
    restricted = AuthorityEnvelope(OWNER, frozenset({DomainAction.ACTIVATE_PROGRAM}))
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
