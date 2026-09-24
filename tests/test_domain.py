# SPDX-License-Identifier: Apache-2.0
"""Pure present-fact regressions for the Issue #4 domain contract."""

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
    CancelAttempt,
    CancelProgram,
    CancelWorkUnit,
    CycleDetected,
    DomainAction,
    DomainError,
    DuplicateAttempt,
    FailAttempt,
    FinishAttempt,
    IllegalTransition,
    InputBinding,
    InvalidDomainValue,
    InvalidGraph,
    MissingInput,
    MissingReference,
    PauseProgram,
    PolicyReference,
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
DELEGATE = "delegate"
VERIFIER = "verifier"
POLICY = PolicyReference("acceptance", "v1", "sha256:policy-v1")
SEED = InputBinding("seed", "ref:approved-seed")


def unit(
    work_unit_id: str,
    *,
    obligation: str | None = None,
    dependencies: tuple[str, ...] = (),
    required_inputs: frozenset[str] = frozenset(),
    outputs: frozenset[str] = frozenset(),
) -> WorkUnit:
    return WorkUnit(
        work_unit_id,
        f"deliver {work_unit_id}" if obligation is None else obligation,
        dependencies=dependencies,
        required_inputs=required_inputs,
        outputs=outputs,
        acceptance_criteria=(f"accept {work_unit_id}",),
    )


def graph_spec(
    *,
    work_units: tuple[WorkUnit, ...] | None = None,
    initial_inputs: tuple[InputBinding, ...] = (SEED,),
    objective: str = "deliver the bounded result",
    acceptance_criteria: tuple[str, ...] = ("Program result is accepted",),
    policy_references: tuple[PolicyReference, ...] = (POLICY,),
    budget: BudgetPolicy | None = None,
    authority: AuthorityEnvelope | None = None,
) -> ProgramSpec:
    if work_units is None:
        work_units = (
            unit("first", required_inputs=frozenset({"seed"}), outputs=frozenset({"first-output"})),
            unit("second", dependencies=("first",), required_inputs=frozenset({"first-output"})),
        )
    return ProgramSpec(
        program_id="program",
        objective=objective,
        work_units=work_units,
        initial_inputs=initial_inputs,
        budget=BudgetPolicy() if budget is None else budget,
        authority=(
            AuthorityEnvelope(OWNER, trusted_satisfaction_issuers=frozenset({VERIFIER}))
            if authority is None
            else authority
        ),
        acceptance_criteria=acceptance_criteria,
        policy_references=policy_references,
    )


def active(spec: ProgramSpec | None = None) -> Program:
    draft = Program.create(graph_spec() if spec is None else spec)
    return draft.apply(ActivateProgram(draft.revision, OWNER))


def attempt_spec(
    program: Program,
    attempt_id: str,
    work_unit_id: str,
    *,
    effective_inputs: tuple[InputBinding, ...] | None = None,
) -> AttemptSpec:
    return AttemptSpec(
        attempt_id=attempt_id,
        program_id=program.program_id,
        work_unit_id=work_unit_id,
        spec_revision=program.spec.revision,
        spec_digest=program.spec.digest,
        effective_inputs=(program.resolved_inputs(work_unit_id) if effective_inputs is None else effective_inputs),
        agent_definition_reference="agent:v1",
    )


def prepared(program: Program, work_unit_id: str = "first", attempt_id: str = "a1") -> Program:
    return program.apply(PrepareAttempt(program.revision, OWNER, attempt_spec(program, attempt_id, work_unit_id)))


def finished(program: Program, work_unit_id: str = "first", attempt_id: str = "a1") -> Program:
    pending = prepared(program, work_unit_id, attempt_id)
    executing = pending.apply(StartAttempt(pending.revision, OWNER, attempt_id))
    return executing.apply(FinishAttempt(executing.revision, OWNER, attempt_id))


def satisfaction(
    program: Program,
    work_unit_id: str,
    reference_id: str,
    attempt_id: str,
    *,
    output_bindings: tuple[InputBinding, ...] | None = None,
    issuer_id: str = VERIFIER,
) -> TrustedSatisfaction:
    source_attempt = program.attempt(attempt_id)
    source_spec = next(
        item
        for item in program.spec_history
        if item.revision == source_attempt.spec.spec_revision and item.digest == source_attempt.spec.spec_digest
    )
    if output_bindings is None:
        output_bindings = tuple(
            InputBinding(name, f"ref:{reference_id}:{name}")
            for name in sorted(source_spec.work_unit(work_unit_id).outputs)
        )
    return TrustedSatisfaction(
        reference_id=reference_id,
        program_id=program.program_id,
        work_unit_id=work_unit_id,
        spec_revision=source_spec.revision,
        spec_digest=source_spec.digest,
        work_unit_fingerprint=source_spec.work_unit_applicability_fingerprint(work_unit_id),
        issuer_id=issuer_id,
        source_attempt_id=attempt_id,
        output_bindings=output_bindings,
    )


def satisfied(
    program: Program, work_unit_id: str = "first", attempt_id: str = "a1", reference_id: str = "s1"
) -> Program:
    return program.apply(
        SatisfyWorkUnit(
            program.revision,
            VERIFIER,
            satisfaction(program, work_unit_id, reference_id, attempt_id),
        )
    )


def amend(
    program: Program,
    *,
    objective: str | None = None,
    work_units: tuple[WorkUnit, ...] | None = None,
    initial_inputs: tuple[InputBinding, ...] | None = None,
    budget: BudgetPolicy | None = None,
    authority: AuthorityEnvelope | None = None,
) -> Program:
    return program.apply(
        AmendProgramSpec(
            program.revision,
            OWNER,
            SpecAmendment(
                program.spec.revision,
                objective=objective,
                work_units=work_units,
                initial_inputs=initial_inputs,
                budget=budget,
                authority=authority,
            ),
        )
    )


def test_two_node_dag_requires_independent_satisfaction_before_successor_is_ready() -> None:
    program = active()
    assert program.ready_work_unit_ids == ("first",)
    assert program.state("second").status is WorkUnitStatus.PENDING

    done = finished(program)
    assert done.attempt("a1").status is AttemptStatus.FINISHED
    assert done.state("first").status is WorkUnitStatus.READY
    assert done.ready_work_unit_ids == ("first",)

    accepted = satisfied(done)
    assert accepted.attempt("a1").status is AttemptStatus.FINISHED
    assert accepted.state("first").status is WorkUnitStatus.SATISFIED
    assert accepted.ready_work_unit_ids == ("second",)

    successor = accepted.resolved_inputs("second")
    assert successor == (
        InputBinding("first-output", "ref:s1:first-output", "predecessor", "first", "s1", "first-output"),
    )
    completed = satisfied(finished(accepted, "second", "a2"), "second", "a2", "s2")
    assert completed.status is ProgramStatus.COMPLETED
    assert completed.ready_work_unit_ids == ()


def test_finished_attempt_is_not_acceptance_and_can_be_retried_with_distinct_inputs() -> None:
    done = finished(active())
    retry = prepared(done, "first", "a2")
    assert retry.attempt("a1").status is AttemptStatus.FINISHED
    assert retry.attempt("a2").status is AttemptStatus.PREPARED
    assert retry.attempt("a1").spec is not retry.attempt("a2").spec
    assert retry.attempt("a1").spec.effective_inputs == retry.attempt("a2").spec.effective_inputs
    assert retry.state("first").active_attempt_id == "a2"


@pytest.mark.parametrize("obligation", ["", "  "])
def test_work_unit_requires_nonblank_obligation(obligation: str) -> None:
    with pytest.raises(InvalidDomainValue):
        WorkUnit("only", obligation, acceptance_criteria=("accepted",))


def test_work_unit_requires_acceptance_criteria_or_pinned_policy() -> None:
    with pytest.raises(InvalidDomainValue):
        WorkUnit("only", "produce the result")
    assert (
        WorkUnit("only", "produce the result", acceptance_policy_reference=POLICY).acceptance_policy_reference == POLICY
    )


def test_program_requires_nonempty_acceptance_and_pinned_policy_references() -> None:
    with pytest.raises(InvalidDomainValue):
        graph_spec(acceptance_criteria=())
    with pytest.raises(InvalidDomainValue):
        graph_spec(policy_references=())
    with pytest.raises(InvalidDomainValue):
        PolicyReference("acceptance", "v1", " ")
    assert graph_spec().policy_references == (POLICY,)


def test_missing_dependencies_inputs_and_cycles_are_rejected() -> None:
    with pytest.raises(MissingReference):
        graph_spec(work_units=(unit("only", dependencies=("absent",)),))
    with pytest.raises(MissingInput):
        graph_spec(work_units=(unit("only", required_inputs=frozenset({"unknown"})),))
    with pytest.raises(MissingInput):
        graph_spec(initial_inputs=())
    with pytest.raises(CycleDetected):
        graph_spec(work_units=(unit("a", dependencies=("b",)), unit("b", dependencies=("a",))))


def test_required_input_must_be_initial_or_direct_predecessor_output() -> None:
    with pytest.raises(MissingInput):
        graph_spec(
            work_units=(
                unit("root", outputs=frozenset({"produced"})),
                unit("middle", dependencies=("root",)),
                unit("leaf", dependencies=("middle",), required_inputs=frozenset({"produced"})),
            ),
            initial_inputs=(),
        )


def test_ambiguous_logical_input_producers_are_rejected() -> None:
    with pytest.raises(InvalidGraph):
        graph_spec(
            work_units=(
                unit("left", outputs=frozenset({"duplicate"})),
                unit("right", outputs=frozenset({"duplicate"})),
                unit("child", dependencies=("left", "right"), required_inputs=frozenset({"duplicate"})),
            ),
            initial_inputs=(),
        )
    with pytest.raises(InvalidGraph):
        graph_spec(work_units=(unit("root", outputs=frozenset({"seed"})),))


def test_approved_initial_bindings_are_immutable_opaque_references() -> None:
    with pytest.raises(InvalidDomainValue):
        InputBinding("seed", " ")
    spec = graph_spec(initial_inputs=(InputBinding("seed", "opaque://v1"),))
    assert active(spec).resolved_inputs("first") == (InputBinding("seed", "opaque://v1"),)
    with pytest.raises(FrozenInstanceError):
        spec.initial_inputs[0].reference = "opaque://v2"  # type: ignore[misc]


def test_full_spec_digest_changes_with_every_pinned_intent_dimension() -> None:
    baseline = graph_spec()
    variants = (
        graph_spec(objective="different objective"),
        graph_spec(acceptance_criteria=("different Program acceptance",)),
        graph_spec(policy_references=(PolicyReference("acceptance", "v2", "sha256:v2"),)),
        graph_spec(initial_inputs=(InputBinding("seed", "ref:changed-seed"),)),
        graph_spec(
            work_units=(
                unit(
                    "first",
                    obligation="changed",
                    required_inputs=frozenset({"seed"}),
                    outputs=frozenset({"first-output"}),
                ),
                baseline.work_unit("second"),
            )
        ),
        graph_spec(budget=BudgetPolicy(max_attempts=9)),
        graph_spec(
            authority=AuthorityEnvelope(OWNER, max_attempts=9, trusted_satisfaction_issuers=frozenset({VERIFIER}))
        ),
    )
    assert all(updated.digest != baseline.digest for updated in variants)


def test_work_unit_acceptance_changes_its_intent_and_program_digest() -> None:
    first = unit("first", required_inputs=frozenset({"seed"}), outputs=frozenset({"first-output"}))
    criteria = WorkUnit(
        "first",
        first.obligation,
        required_inputs=first.required_inputs,
        outputs=first.outputs,
        acceptance_criteria=("different acceptance",),
    )
    policy = WorkUnit(
        "first",
        first.obligation,
        required_inputs=first.required_inputs,
        outputs=first.outputs,
        acceptance_criteria=first.acceptance_criteria,
        acceptance_policy_reference=POLICY,
    )
    base = graph_spec(work_units=(first, graph_spec().work_unit("second")))
    for changed in (criteria, policy):
        revised = graph_spec(work_units=(changed, base.work_unit("second")))
        assert revised.digest != base.digest
        assert revised.work_unit_applicability_fingerprint("second") != base.work_unit_applicability_fingerprint(
            "second"
        )


def test_applicability_fingerprint_excludes_unrelated_units_budget_authority_and_metadata() -> None:
    original = graph_spec(work_units=(*graph_spec().work_units, unit("unrelated")))
    changed = graph_spec(
        work_units=(*original.work_units[:2], unit("unrelated", outputs=frozenset({"new-output"}))),
        budget=BudgetPolicy(max_attempts=8),
        authority=AuthorityEnvelope(OWNER, max_attempts=8, trusted_satisfaction_issuers=frozenset({VERIFIER})),
    )
    revised = original.amend(SpecAmendment(original.revision, budget=BudgetPolicy(max_attempts=8)))
    assert original.digest != changed.digest != revised.digest
    assert original.work_unit_applicability_fingerprint("first") == changed.work_unit_applicability_fingerprint("first")
    assert original.work_unit_applicability_fingerprint("second") == changed.work_unit_applicability_fingerprint(
        "second"
    )
    assert original.work_unit_applicability_fingerprint("first") == revised.work_unit_applicability_fingerprint("first")


def test_unused_approved_initial_binding_does_not_change_acceptance_fingerprint() -> None:
    base = graph_spec(initial_inputs=(SEED, InputBinding("unused", "ref:old")))
    changed = graph_spec(initial_inputs=(SEED, InputBinding("unused", "ref:new")))
    assert base.digest != changed.digest
    assert base.work_unit_applicability_fingerprint("second") == changed.work_unit_applicability_fingerprint("second")


def test_consumed_owner_intent_changes_fingerprint() -> None:
    baseline = graph_spec().work_unit_applicability_fingerprint("second")
    variants = (
        graph_spec(objective="new objective"),
        graph_spec(acceptance_criteria=("new Program criterion",)),
        graph_spec(policy_references=(PolicyReference("acceptance", "v2", "sha256:v2"),)),
        graph_spec(initial_inputs=(InputBinding("seed", "ref:new-seed"),)),
    )
    assert all(item.work_unit_applicability_fingerprint("second") != baseline for item in variants)


def test_transitive_predecessor_definition_changes_descendant_fingerprint() -> None:
    original = graph_spec(
        work_units=(
            unit("root"),
            unit("middle", dependencies=("root",)),
            unit("leaf", dependencies=("middle",)),
            unit("independent"),
        ),
        initial_inputs=(),
    )
    changed = graph_spec(
        work_units=(
            WorkUnit("root", "new obligation", acceptance_criteria=("accept root",)),
            *(item for item in original.work_units if item.id != "root"),
        ),
        initial_inputs=(),
    )
    assert original.work_unit_applicability_fingerprint("leaf") != changed.work_unit_applicability_fingerprint("leaf")
    assert original.work_unit_applicability_fingerprint("independent") == changed.work_unit_applicability_fingerprint(
        "independent"
    )


@pytest.mark.parametrize(
    "bindings",
    [
        (),
        (SEED, InputBinding("extra", "ref:extra")),
        (InputBinding("seed", "ref:unapproved"),),
        (InputBinding("seed", SEED.reference, "predecessor", "other", "s1", "seed"),),
    ],
)
def test_attempt_admission_rejects_missing_extra_arbitrary_or_forged_provenance(
    bindings: tuple[InputBinding, ...],
) -> None:
    program = active()
    with pytest.raises(DomainError):
        program.apply(
            PrepareAttempt(
                program.revision,
                OWNER,
                attempt_spec(program, "bad", "first", effective_inputs=bindings),
            )
        )
    assert program.attempts == ()


def test_attempt_admission_rejects_foreign_and_stale_spec_subjects() -> None:
    program = active()
    valid = attempt_spec(program, "a1", "first")
    foreign = AttemptSpec(
        "foreign", "other-program", "first", valid.spec_revision, valid.spec_digest, valid.effective_inputs
    )
    stale = AttemptSpec(
        "stale", program.program_id, "first", valid.spec_revision, "unknown-digest", valid.effective_inputs
    )
    with pytest.raises(AuthorityViolation):
        program.apply(PrepareAttempt(program.revision, OWNER, foreign))
    with pytest.raises(StaleRevision):
        program.apply(PrepareAttempt(program.revision, OWNER, stale))


def test_attempt_bindings_must_match_current_predecessor_satisfaction() -> None:
    accepted = satisfied(finished(active()))
    valid = attempt_spec(accepted, "a2", "second")
    wrong = AttemptSpec(
        "wrong",
        accepted.program_id,
        "second",
        accepted.spec.revision,
        accepted.spec.digest,
        (InputBinding("first-output", "ref:s1:first-output"),),
    )
    with pytest.raises(DomainError):
        accepted.apply(PrepareAttempt(accepted.revision, OWNER, wrong))
    asserted = accepted.apply(PrepareAttempt(accepted.revision, OWNER, valid))
    assert asserted.attempt("a2").spec.effective_inputs == accepted.resolved_inputs("second")


def test_expected_aggregate_and_spec_revisions_are_independent() -> None:
    program = Program.create(graph_spec())
    with pytest.raises(StaleRevision):
        program.apply(ActivateProgram(1, OWNER))
    activated = program.apply(ActivateProgram(0, OWNER))
    with pytest.raises(StaleRevision):
        activated.apply(AmendProgramSpec(activated.revision, OWNER, SpecAmendment(2, objective="new")))
    assert activated.revision == 1
    assert activated.spec.revision == 1


def test_illegal_lifecycle_transitions_do_not_mutate_history() -> None:
    program = active()
    with pytest.raises(IllegalTransition):
        program.apply(ActivateProgram(program.revision, OWNER))
    with pytest.raises(MissingReference):
        program.apply(FinishAttempt(program.revision, OWNER, "unknown"))
    pending = prepared(program)
    with pytest.raises(IllegalTransition):
        pending.apply(FinishAttempt(pending.revision, OWNER, "a1"))
    executing = pending.apply(StartAttempt(pending.revision, OWNER, "a1"))
    with pytest.raises(IllegalTransition):
        executing.apply(StartAttempt(executing.revision, OWNER, "a1"))
    done = executing.apply(FinishAttempt(executing.revision, OWNER, "a1"))
    with pytest.raises(IllegalTransition):
        done.apply(FailAttempt(done.revision, OWNER, "a1"))
    assert program.revision == 1


def test_satisfaction_requires_finished_source_and_never_finishes_an_attempt() -> None:
    pending = prepared(active())
    fact = satisfaction(pending, "first", "s1", "a1")
    with pytest.raises(IllegalTransition):
        pending.apply(SatisfyWorkUnit(pending.revision, VERIFIER, fact))
    executing = pending.apply(StartAttempt(pending.revision, OWNER, "a1"))
    with pytest.raises(IllegalTransition):
        executing.apply(SatisfyWorkUnit(executing.revision, VERIFIER, fact))
    assert executing.attempt("a1").status is AttemptStatus.EXECUTING
    accepted = satisfied(executing.apply(FinishAttempt(executing.revision, OWNER, "a1")))
    assert accepted.attempt("a1").status is AttemptStatus.FINISHED
    assert accepted.satisfactions[0].source_attempt_id == "a1"


def test_finished_attempt_may_be_satisfied_while_another_attempt_is_live() -> None:
    done = finished(active())
    retry = prepared(done, "first", "a2")
    accepted = satisfied(retry, "first", "a1", "s1")
    assert accepted.state("first").status is WorkUnitStatus.SATISFIED
    assert accepted.attempt("a2").status is AttemptStatus.PREPARED
    assert accepted.attempt_cancellations == ()
    with pytest.raises(IllegalTransition):
        accepted.apply(StartAttempt(accepted.revision, OWNER, "a2"))
    observed = accepted.apply(FailAttempt(accepted.revision, OWNER, "a2"))
    assert observed.attempt("a2").status is AttemptStatus.FAILED
    assert observed.state("first").status is WorkUnitStatus.SATISFIED


def test_late_terminal_observation_remains_legal_after_satisfaction_completes_program() -> None:
    spec = graph_spec(work_units=(unit("only"),), initial_inputs=())
    done = finished(active(spec), "only", "a1")
    retry = prepared(done, "only", "a2")
    executing = retry.apply(StartAttempt(retry.revision, OWNER, "a2"))
    completed = satisfied(executing, "only", "a1", "s1")
    assert completed.status is ProgramStatus.COMPLETED
    assert completed.attempt("a2").status is AttemptStatus.EXECUTING
    observed = completed.apply(FinishAttempt(completed.revision, OWNER, "a2"))
    assert observed.status is ProgramStatus.COMPLETED
    assert observed.attempt("a2").status is AttemptStatus.FINISHED


def test_satisfaction_requires_trusted_issuer_equal_to_command_actor() -> None:
    done = finished(active())
    fact = satisfaction(done, "first", "s1", "a1")
    with pytest.raises(AuthorityViolation):
        done.apply(SatisfyWorkUnit(done.revision, OWNER, fact))
    with pytest.raises(AuthorityViolation):
        done.apply(
            SatisfyWorkUnit(done.revision, "untrusted", satisfaction(done, "first", "bad", "a1", issuer_id="untrusted"))
        )
    assert satisfied(done).state("first").status is WorkUnitStatus.SATISFIED


def test_missing_trusted_fact_does_not_invalidate_finished_work_or_the_program() -> None:
    authority = AuthorityEnvelope(OWNER, trusted_satisfaction_issuers=frozenset())
    done = finished(active(graph_spec(authority=authority)))
    assert done.status is ProgramStatus.ACTIVE
    assert done.state("first").status is WorkUnitStatus.READY
    assert done.state("second").status is WorkUnitStatus.PENDING
    assert done.satisfactions == ()
    with pytest.raises(AuthorityViolation):
        done.apply(SatisfyWorkUnit(done.revision, VERIFIER, satisfaction(done, "first", "s1", "a1")))


def test_operational_worker_cannot_also_be_registered_as_trusted_issuer() -> None:
    with pytest.raises(InvalidDomainValue):
        AuthorityEnvelope(
            OWNER,
            delegated_actor_ids=frozenset({DELEGATE}),
            trusted_satisfaction_issuers=frozenset({DELEGATE}),
        )


@pytest.mark.parametrize(
    "outputs",
    [
        (),
        (InputBinding("unexpected", "ref:unexpected"),),
        (InputBinding("first-output", "ref:one"), InputBinding("unexpected", "ref:extra")),
    ],
)
def test_satisfaction_rejects_missing_or_undeclared_output_bindings(outputs: tuple[InputBinding, ...]) -> None:
    done = finished(active())
    with pytest.raises(DomainError):
        done.apply(
            SatisfyWorkUnit(
                done.revision,
                VERIFIER,
                satisfaction(done, "first", "bad", "a1", output_bindings=outputs),
            )
        )


def test_satisfaction_subject_cannot_be_foreign_or_misbound_to_attempt() -> None:
    done = finished(active())
    foreign = TrustedSatisfaction(
        "foreign",
        "other-program",
        "first",
        done.spec.revision,
        done.spec.digest,
        done.spec.work_unit_applicability_fingerprint("first"),
        VERIFIER,
        "a1",
        (InputBinding("first-output", "ref:foreign"),),
    )
    with pytest.raises(DomainError):
        done.apply(SatisfyWorkUnit(done.revision, VERIFIER, foreign))
    missing_attempt = TrustedSatisfaction(
        "missing",
        done.program_id,
        "first",
        done.spec.revision,
        done.spec.digest,
        done.spec.work_unit_applicability_fingerprint("first"),
        VERIFIER,
        "unknown",
        (InputBinding("first-output", "ref:missing"),),
    )
    with pytest.raises(MissingReference):
        done.apply(SatisfyWorkUnit(done.revision, VERIFIER, missing_attempt))
    another_branch = finished(satisfied(done), "second", "a2")
    wrong_unit = TrustedSatisfaction(
        "misbound",
        another_branch.program_id,
        "first",
        another_branch.spec.revision,
        another_branch.spec.digest,
        another_branch.spec.work_unit_applicability_fingerprint("first"),
        VERIFIER,
        "a2",
        (InputBinding("first-output", "ref:misbound"),),
    )
    with pytest.raises(AuthorityViolation):
        another_branch.apply(SatisfyWorkUnit(another_branch.revision, VERIFIER, wrong_unit))


def test_recorded_satisfaction_has_stamped_order_and_immutable_historical_identity() -> None:
    accepted = satisfied(finished(active()))
    fact = accepted.satisfactions[0]
    assert fact.record_order > 0
    assert fact.spec_revision == 1
    assert fact.spec_digest == accepted.spec_history[0].digest
    assert fact.work_unit_fingerprint == accepted.spec.work_unit_applicability_fingerprint("first")
    with pytest.raises(FrozenInstanceError):
        fact.spec_revision = 2  # type: ignore[misc]


def test_latest_applicable_satisfaction_wins_by_record_order_not_attempt_order() -> None:
    done_twice = finished(finished(active()), "first", "a2")
    first = satisfied(done_twice, "first", "a2", "s2")
    second = satisfied(first, "first", "a1", "s1")
    assert tuple(item.source_attempt_id for item in second.satisfactions) == ("a2", "a1")
    assert second.satisfactions[0].record_order < second.satisfactions[1].record_order
    assert second.state("first").satisfaction_id == "s1"
    assert second.resolved_inputs("second") == (
        InputBinding("first-output", "ref:s1:first-output", "predecessor", "first", "s1", "first-output"),
    )


def test_new_predecessor_output_reference_invalidates_old_child_acceptance() -> None:
    spec = graph_spec(
        work_units=(*graph_spec().work_units, unit("unrelated")),
        budget=BudgetPolicy(max_attempts=4),
        authority=AuthorityEnvelope(OWNER, max_attempts=4, trusted_satisfaction_issuers=frozenset({VERIFIER})),
    )
    done_twice = finished(finished(active(spec)), "first", "a2")
    first = satisfied(done_twice, "first", "a1", "s1")
    child = satisfied(finished(first, "second", "a3"), "second", "a3", "s-child")
    assert child.status is ProgramStatus.ACTIVE
    assert child.state("second").status is WorkUnitStatus.SATISFIED

    replaced = satisfied(child, "first", "a2", "s2")
    assert replaced.state("first").satisfaction_id == "s2"
    assert replaced.state("second").status is WorkUnitStatus.READY
    assert replaced.state("second").satisfaction_id is None
    assert replaced.resolved_inputs("second") == (
        InputBinding("first-output", "ref:s2:first-output", "predecessor", "first", "s2", "first-output"),
    )
    assert tuple(item.reference_id for item in replaced.satisfactions) == ("s1", "s-child", "s2")


def test_older_spec_satisfaction_can_be_admitted_when_its_subject_still_applies() -> None:
    done = finished(active())
    historical = satisfaction(done, "first", "s1", "a1")
    amended = amend(done, budget=BudgetPolicy(max_attempts=8))
    assert historical.spec_revision < amended.spec.revision
    accepted = amended.apply(SatisfyWorkUnit(amended.revision, VERIFIER, historical))
    assert accepted.state("first").status is WorkUnitStatus.SATISFIED
    assert accepted.satisfactions[0].spec_revision == 1


def test_delayed_historical_satisfaction_is_recorded_while_current_intent_differs() -> None:
    done = finished(active())
    historical = satisfaction(done, "first", "s1", "a1")
    v2 = amend(done, objective="different current objective")
    admitted = v2.apply(SatisfyWorkUnit(v2.revision, VERIFIER, historical))
    assert admitted.state("first").status is WorkUnitStatus.READY
    assert admitted.satisfactions[0].spec_revision == 1
    v3 = amend(admitted, objective=done.spec.objective)
    assert v3.spec.revision == 3
    assert v3.state("first").satisfaction_id == "s1"
    assert v3.satisfactions == admitted.satisfactions


def test_delayed_satisfaction_of_removed_work_unit_reappears_on_reintroduction() -> None:
    target = unit("target")
    remaining = unit("remaining")
    done = finished(active(graph_spec(work_units=(target, remaining), initial_inputs=())), "target", "a1")
    historical = satisfaction(done, "target", "s1", "a1")
    removed = amend(done, work_units=(remaining,))
    admitted = removed.apply(SatisfyWorkUnit(removed.revision, VERIFIER, historical))
    assert tuple(state.work_unit_id for state in admitted.work_unit_states) == ("remaining",)
    assert admitted.satisfactions[0].spec_revision == 1
    restored = amend(admitted, work_units=(target, remaining))
    assert restored.state("target").satisfaction_id == "s1"
    assert restored.state("remaining").status is WorkUnitStatus.READY


def test_satisfaction_rejects_forged_subject_fingerprint() -> None:
    done = finished(active())
    forged = TrustedSatisfaction(
        "forged",
        done.program_id,
        "first",
        done.spec.revision,
        done.spec.digest,
        "not-the-current-subject",
        VERIFIER,
        "a1",
        (InputBinding("first-output", "ref:forged"),),
    )
    with pytest.raises(DomainError):
        done.apply(SatisfyWorkUnit(done.revision, VERIFIER, forged))


def test_program_spec_amendment_appends_full_history_and_never_retargets_finished_attempt() -> None:
    done = finished(active())
    first_spec = done.spec
    amended = amend(done, objective="new owner objective")
    assert amended.spec.revision == 2
    assert amended.spec.parent_digest == first_spec.digest
    assert amended.spec_history == (first_spec, amended.spec)
    assert amended.attempt("a1").status is AttemptStatus.FINISHED
    assert amended.attempt("a1").spec.spec_digest == first_spec.digest
    assert amended.attempt("a1").spec.effective_inputs == (SEED,)


def test_identical_semantic_amendment_is_a_complete_noop() -> None:
    before = finished(active())
    amendment = SpecAmendment(
        before.spec.revision,
        objective=before.spec.objective,
        work_units=before.spec.work_units,
        initial_inputs=before.spec.initial_inputs,
        budget=BudgetPolicy(before.spec.budget.max_attempts, before.spec.budget.max_active_attempts),
        authority=AuthorityEnvelope(
            OWNER,
            before.spec.authority.allowed_actions,
            before.spec.authority.max_attempts,
            before.spec.authority.trusted_satisfaction_issuers,
        ),
        acceptance_criteria=before.spec.acceptance_criteria,
        policy_references=before.spec.policy_references,
        reason="a different explanation is not a semantic change",
    )
    assert before.spec.amend(amendment) is before.spec
    after = before.apply(AmendProgramSpec(before.revision, OWNER, amendment))
    assert after is before
    assert after.revision == before.revision
    assert after.spec_history == before.spec_history
    assert after.attempts == before.attempts


def test_budget_and_authority_change_is_material_but_preserves_acceptance_applicability() -> None:
    accepted = satisfied(finished(active()))
    changed = amend(
        accepted,
        budget=BudgetPolicy(max_attempts=8),
        authority=AuthorityEnvelope(OWNER, max_attempts=8, trusted_satisfaction_issuers=frozenset({VERIFIER})),
    )
    assert changed.spec.digest != accepted.spec.digest
    assert changed.spec.revision == 2
    assert changed.state("first").status is WorkUnitStatus.SATISFIED
    assert changed.state("first").satisfaction_id == "s1"
    assert changed.ready_work_unit_ids == ("second",)


@pytest.mark.parametrize("field", ["objective", "acceptance_criteria", "policy_references"])
def test_each_program_wide_intent_change_invalidates_existing_acceptance(field: str) -> None:
    accepted = satisfied(finished(active()))
    if field == "objective":
        replacement = SpecAmendment(accepted.spec.revision, objective="new objective")
    elif field == "acceptance_criteria":
        replacement = SpecAmendment(accepted.spec.revision, acceptance_criteria=("new Program criterion",))
    else:
        replacement = SpecAmendment(
            accepted.spec.revision,
            policy_references=(PolicyReference("acceptance", "v2", "sha256:policy-v2"),),
        )
    changed = accepted.apply(AmendProgramSpec(accepted.revision, OWNER, replacement))
    assert changed.state("first").status is WorkUnitStatus.READY
    assert changed.state("second").status is WorkUnitStatus.PENDING
    assert changed.satisfactions == accepted.satisfactions


@pytest.mark.parametrize("field", ["obligation", "acceptance", "policy", "inputs", "outputs"])
def test_each_local_definition_change_invalidates_only_its_chain(field: str) -> None:
    spec = graph_spec(work_units=(*graph_spec().work_units, unit("unrelated")))
    accepted = satisfied(finished(active(spec)))
    accepted = satisfied(finished(accepted, "unrelated", "a2"), "unrelated", "a2", "s2")
    first = spec.work_unit("first")
    changed_first = WorkUnit(
        "first",
        "different obligation" if field == "obligation" else first.obligation,
        required_inputs=frozenset() if field == "inputs" else first.required_inputs,
        outputs=first.outputs | {"extra-output"} if field == "outputs" else first.outputs,
        acceptance_criteria=("different local criterion",) if field == "acceptance" else first.acceptance_criteria,
        acceptance_policy_reference=PolicyReference("local", "v2", "sha256:local") if field == "policy" else None,
    )
    changed = amend(accepted, work_units=(changed_first, spec.work_unit("second"), spec.work_unit("unrelated")))
    assert changed.state("first").status is WorkUnitStatus.READY
    assert changed.state("second").status is WorkUnitStatus.PENDING
    assert changed.state("unrelated").status is WorkUnitStatus.SATISFIED


def test_dependency_change_invalidates_changed_node_and_transitive_descendants() -> None:
    root = unit("root", outputs=frozenset({"root-output"}))
    child = unit(
        "child", dependencies=("root",), required_inputs=frozenset({"root-output"}), outputs=frozenset({"child-output"})
    )
    leaf = unit("leaf", dependencies=("child",), required_inputs=frozenset({"child-output"}))
    support = unit("support")
    spec = graph_spec(work_units=(root, child, leaf, support), initial_inputs=())
    accepted = active(spec)
    for work_unit_id in ("root", "child", "leaf"):
        attempt_id = f"a-{work_unit_id}"
        accepted = satisfied(
            finished(accepted, work_unit_id, attempt_id), work_unit_id, attempt_id, f"s-{work_unit_id}"
        )
    changed_child = unit(
        "child",
        dependencies=("root", "support"),
        required_inputs=frozenset({"root-output"}),
        outputs=frozenset({"child-output"}),
    )
    changed = amend(accepted, work_units=(root, changed_child, leaf, support))
    assert changed.state("root").status is WorkUnitStatus.SATISFIED
    assert changed.state("child").status is WorkUnitStatus.PENDING
    assert changed.state("leaf").status is WorkUnitStatus.PENDING
    assert changed.state("support").status is WorkUnitStatus.READY


def test_unrelated_definition_and_historical_metadata_leave_acceptance_intact() -> None:
    spec = graph_spec(work_units=(*graph_spec().work_units, unit("unrelated")))
    accepted = satisfied(finished(active(spec)))
    changed = amend(
        accepted,
        work_units=(spec.work_unit("first"), spec.work_unit("second"), unit("unrelated", obligation="other work")),
    )
    assert changed.state("first").satisfaction_id == "s1"
    assert changed.ready_work_unit_ids == ("second", "unrelated")
    metadata_only = ProgramSpec(
        spec.program_id,
        spec.objective,
        spec.work_units,
        initial_inputs=spec.initial_inputs,
        budget=spec.budget,
        authority=spec.authority,
        revision=2,
        parent_digest="different-parent",
        acceptance_criteria=spec.acceptance_criteria,
        policy_references=spec.policy_references,
    )
    assert metadata_only.digest != spec.digest
    assert metadata_only.work_unit_applicability_fingerprint("first") == spec.work_unit_applicability_fingerprint(
        "first"
    )


def test_v1_v2_v1_reversion_restores_historical_satisfaction_without_rewriting_it() -> None:
    accepted = satisfied(finished(active()))
    first = accepted.spec.work_unit("first")
    child = accepted.spec.work_unit("second")
    changed = WorkUnit(
        "first",
        "changed obligation",
        required_inputs=first.required_inputs,
        outputs=first.outputs,
        acceptance_criteria=first.acceptance_criteria,
    )
    v2 = amend(accepted, work_units=(changed, child))
    assert v2.state("first").status is WorkUnitStatus.READY
    assert v2.state("second").status is WorkUnitStatus.PENDING
    v3 = amend(v2, work_units=(first, child))
    assert v3.spec.revision == 3
    assert v3.spec.digest != accepted.spec.digest
    assert v3.state("first").status is WorkUnitStatus.SATISFIED
    assert v3.state("first").satisfaction_id == "s1"
    assert v3.ready_work_unit_ids == ("second",)
    assert v3.satisfactions == accepted.satisfactions
    assert v3.attempt("a1").spec.spec_revision == 1


def test_transitive_predecessor_change_invalidates_descendants_but_not_other_branches() -> None:
    spec = graph_spec(
        work_units=(
            unit("root", outputs=frozenset({"root-out"})),
            unit(
                "middle",
                dependencies=("root",),
                required_inputs=frozenset({"root-out"}),
                outputs=frozenset({"middle-out"}),
            ),
            unit("leaf", dependencies=("middle",), required_inputs=frozenset({"middle-out"})),
            unit("independent"),
        ),
        initial_inputs=(),
        budget=BudgetPolicy(max_attempts=5, max_active_attempts=2),
        authority=AuthorityEnvelope(OWNER, max_attempts=5, trusted_satisfaction_issuers=frozenset({VERIFIER})),
    )
    program = active(spec)
    for work_unit_id in ("root", "middle", "leaf", "independent"):
        attempt_id = f"a-{work_unit_id}"
        program = satisfied(finished(program, work_unit_id, attempt_id), work_unit_id, attempt_id, f"s-{work_unit_id}")
    assert program.status is ProgramStatus.COMPLETED
    changed_root = WorkUnit(
        "root", "new root obligation", outputs=frozenset({"root-out"}), acceptance_criteria=("accept root",)
    )
    reopened = amend(program, work_units=(changed_root, *(item for item in spec.work_units if item.id != "root")))
    assert reopened.status is ProgramStatus.ACTIVE
    assert reopened.state("root").status is WorkUnitStatus.READY
    assert reopened.state("middle").status is WorkUnitStatus.PENDING
    assert reopened.state("leaf").status is WorkUnitStatus.PENDING
    assert reopened.state("independent").status is WorkUnitStatus.SATISFIED
    assert len(reopened.satisfactions) == 4
    reverted = amend(reopened, work_units=spec.work_units)
    assert reverted.status is ProgramStatus.COMPLETED
    assert reverted.state("leaf").satisfaction_id == "s-leaf"
    assert reverted.attempt("a-leaf").spec.spec_revision == 1


def test_amending_an_approved_initial_reference_invalidates_only_consuming_chain() -> None:
    spec = graph_spec(work_units=(*graph_spec().work_units, unit("independent")))
    program = active(spec)
    program = satisfied(finished(program), "first", "a1", "s1")
    program = satisfied(finished(program, "independent", "a2"), "independent", "a2", "s2")
    changed = amend(program, initial_inputs=(InputBinding("seed", "ref:replacement"),))
    assert changed.state("first").status is WorkUnitStatus.READY
    assert changed.state("second").status is WorkUnitStatus.PENDING
    assert changed.state("independent").status is WorkUnitStatus.SATISFIED
    assert changed.resolved_inputs("first") == (InputBinding("seed", "ref:replacement"),)


def test_explicit_work_unit_cancellation_records_a_decision_and_propagates() -> None:
    program = active()
    cancelled = program.apply(CancelWorkUnit(program.revision, OWNER, "first", "owner abandons chain"))
    assert cancelled.status is ProgramStatus.ACTIVE
    assert cancelled.state("first").status is WorkUnitStatus.CANCELLED
    assert cancelled.state("second").status is WorkUnitStatus.CANCELLED
    assert len(cancelled.cancellations) == 1
    assert cancelled.cancellations[0].work_unit_id == "first"
    assert cancelled.cancellations[0].reason == "owner abandons chain"
    assert cancelled.cancellations[0].record_order > 0
    assert cancelled.satisfactions == ()
    with pytest.raises(FrozenInstanceError):
        cancelled.cancellations[0].reason = "rewritten"  # type: ignore[misc]


def test_owner_cancellation_supersedes_recorded_satisfaction_without_deleting_history() -> None:
    accepted = satisfied(finished(active()))
    abandoned = accepted.apply(CancelWorkUnit(accepted.revision, OWNER, "first"))
    assert abandoned.status is ProgramStatus.ACTIVE
    assert abandoned.state("first").status is WorkUnitStatus.CANCELLED
    assert abandoned.state("second").status is WorkUnitStatus.CANCELLED
    assert abandoned.satisfactions == accepted.satisfactions
    assert abandoned.cancellations[0].work_unit_fingerprint == accepted.spec.work_unit_applicability_fingerprint(
        "first"
    )


def test_cancellation_scope_changes_with_intent_and_restores_on_reversion() -> None:
    program = active(graph_spec(work_units=(unit("a"), unit("b")), initial_inputs=()))
    cancelled = program.apply(CancelWorkUnit(program.revision, OWNER, "a"))
    original = cancelled.spec.work_unit("a")
    changed_a = WorkUnit("a", "new obligation", acceptance_criteria=("accept a",))
    v2 = amend(cancelled, work_units=(changed_a, cancelled.spec.work_unit("b")))
    assert v2.state("a").status is WorkUnitStatus.READY
    assert v2.state("b").status is WorkUnitStatus.READY
    reverted = amend(v2, work_units=(original, v2.spec.work_unit("b")))
    assert reverted.state("a").status is WorkUnitStatus.CANCELLED
    assert reverted.state("b").status is WorkUnitStatus.READY
    assert reverted.cancellations == cancelled.cancellations


def test_cancellation_inheritance_is_rebuilt_from_current_graph_not_previous_state() -> None:
    root = unit("root")
    child = unit("child", dependencies=("root",))
    unrelated = unit("unrelated")
    program = active(graph_spec(work_units=(root, child, unrelated), initial_inputs=()))
    cancelled = program.apply(CancelWorkUnit(program.revision, OWNER, "root"))
    assert cancelled.status is ProgramStatus.ACTIVE
    detached = amend(cancelled, work_units=(root, unit("child"), unrelated))
    assert detached.state("root").status is WorkUnitStatus.CANCELLED
    assert detached.state("child").status is WorkUnitStatus.READY
    reattached = amend(detached, work_units=(root, child, unrelated))
    assert reattached.state("root").status is WorkUnitStatus.CANCELLED
    assert reattached.state("child").status is WorkUnitStatus.CANCELLED
    assert reattached.state("unrelated").status is WorkUnitStatus.READY


def test_wholly_cancelled_two_node_graph_can_reconnect_and_reintroduce_work() -> None:
    program = active()
    first = program.spec.work_unit("first")
    second = program.spec.work_unit("second")
    cancelled = program.apply(CancelWorkUnit(program.revision, OWNER, "first"))
    assert cancelled.status is ProgramStatus.ACTIVE
    assert cancelled.state("second").status is WorkUnitStatus.CANCELLED

    detached_second = WorkUnit("second", second.obligation, acceptance_criteria=second.acceptance_criteria)
    detached = amend(cancelled, work_units=(first, detached_second))
    assert detached.state("first").status is WorkUnitStatus.CANCELLED
    assert detached.state("second").status is WorkUnitStatus.READY
    removed = amend(detached, work_units=(detached_second,))
    assert removed.ready_work_unit_ids == ("second",)
    reconnected = amend(removed, work_units=(first, second))
    assert reconnected.state("first").status is WorkUnitStatus.CANCELLED
    assert reconnected.state("second").status is WorkUnitStatus.CANCELLED
    assert reconnected.status is ProgramStatus.ACTIVE
    assert reconnected.cancellations == cancelled.cancellations


def test_removed_satisfaction_and_cancellation_subjects_reappear_from_history() -> None:
    target = unit("target")
    unrelated = unit("unrelated")
    pending = unit("pending")
    spec = graph_spec(work_units=(target, unrelated, pending), initial_inputs=())
    program = satisfied(finished(active(spec), "target", "a1"), "target", "a1", "s1")
    program = program.apply(CancelWorkUnit(program.revision, OWNER, "unrelated"))
    removed = amend(program, work_units=(pending,))
    assert tuple(state.work_unit_id for state in removed.work_unit_states) == ("pending",)
    restored = amend(removed, work_units=(target, unrelated, pending))
    assert restored.state("target").satisfaction_id == "s1"
    assert restored.state("unrelated").status is WorkUnitStatus.CANCELLED
    assert restored.state("pending").status is WorkUnitStatus.READY
    assert restored.satisfactions == program.satisfactions
    assert restored.cancellations == program.cancellations


def test_history_projection_is_deterministic_for_same_command_and_facts() -> None:
    program = satisfied(finished(active()))
    command = AmendProgramSpec(program.revision, OWNER, SpecAmendment(1, objective="changed objective"))
    first = program.apply(command)
    second = program.apply(command)
    assert first == second
    assert first.state("first").status is WorkUnitStatus.READY


def test_work_unit_cancellation_does_not_count_toward_program_completion() -> None:
    spec = graph_spec(work_units=(unit("abandoned"), unit("fulfilled")), initial_inputs=())
    program = active(spec)
    cancelled = program.apply(CancelWorkUnit(program.revision, OWNER, "abandoned"))
    assert cancelled.status is ProgramStatus.ACTIVE
    result = satisfied(finished(cancelled, "fulfilled", "a1"), "fulfilled", "a1", "s1")
    assert result.status is ProgramStatus.ACTIVE
    assert result.state("fulfilled").status is WorkUnitStatus.SATISFIED


def test_program_cancellation_is_owner_decision_not_acceptance() -> None:
    program = active()
    cancelled = program.apply(CancelProgram(program.revision, OWNER))
    assert cancelled.status is ProgramStatus.CANCELLED
    assert cancelled.work_unit_states == program.work_unit_states
    assert cancelled.cancellations == ()
    assert cancelled.satisfactions == ()
    with pytest.raises(IllegalTransition):
        cancelled.apply(ActivateProgram(cancelled.revision, OWNER))


def test_owner_amendment_records_affected_attempt_cancellation_without_fabricating_completion() -> None:
    pending = prepared(active())
    amended = amend(pending, objective="changed while first is prepared")
    assert amended.attempt("a1").status is AttemptStatus.CANCELLED
    assert amended.attempt("a1").spec == pending.attempt("a1").spec
    assert amended.attempt_cancellations[0].attempt_id == "a1"
    assert amended.attempt_cancellations[0].actor_id == OWNER
    assert amended.attempt_cancellations[0].record_order > pending.revision
    assert amended.satisfactions == ()


def test_explicit_attempt_cancellation_before_amendment_is_preserved_as_history() -> None:
    pending = prepared(active())
    cancelled = pending.apply(CancelAttempt(pending.revision, OWNER, "a1"))
    assert cancelled.attempt("a1").status is AttemptStatus.CANCELLED
    assert cancelled.attempt_cancellations[0].attempt_id == "a1"
    amended = amend(cancelled, objective="changed after explicit cancellation")
    assert amended.attempt("a1").status is AttemptStatus.CANCELLED
    assert amended.attempt_cancellations == cancelled.attempt_cancellations
    assert amended.attempt("a1").spec.spec_revision == 1


def test_unaffected_live_attempt_remains_bound_to_its_original_spec() -> None:
    spec = graph_spec(
        work_units=(unit("a"), unit("b")),
        initial_inputs=(),
        budget=BudgetPolicy(max_attempts=4, max_active_attempts=2),
    )
    pending = prepared(active(spec), "b", "b1")
    modified = amend(
        pending, work_units=(WorkUnit("a", "changed", acceptance_criteria=("accept a",)), spec.work_unit("b"))
    )
    assert modified.state("b").status is WorkUnitStatus.ACTIVE
    assert modified.state("b").active_attempt_id == "b1"
    assert modified.attempt("b1").status is AttemptStatus.PREPARED
    assert modified.attempt("b1").spec.spec_digest == spec.digest


def test_replaced_predecessor_output_preserves_live_child_for_separate_terminal_decision() -> None:
    spec = graph_spec(budget=BudgetPolicy(max_attempts=3))
    first_twice = finished(finished(active(spec)), "first", "a2")
    accepted = satisfied(first_twice, "first", "a1", "s1")
    child = prepared(accepted, "second", "a3")
    replaced = satisfied(child, "first", "a2", "s2")
    assert replaced.attempt("a3").status is AttemptStatus.PREPARED
    assert replaced.attempt_cancellations == ()
    assert replaced.state("second").status is WorkUnitStatus.READY
    assert replaced.attempt("a3").spec.effective_inputs == child.attempt("a3").spec.effective_inputs
    with pytest.raises(IllegalTransition):
        prepared(replaced, "second", "a4")
    stopped = replaced.apply(CancelAttempt(replaced.revision, OWNER, "a3"))
    assert stopped.attempt("a3").status is AttemptStatus.CANCELLED
    assert stopped.attempt_cancellations[-1].actor_id == OWNER
    assert stopped.state("second").status is WorkUnitStatus.READY


def test_authority_amendment_withdrawing_delegate_cancels_its_live_attempt() -> None:
    delegated = AuthorityEnvelope(
        OWNER,
        delegated_actor_ids=frozenset({DELEGATE}),
        trusted_satisfaction_issuers=frozenset({VERIFIER}),
    )
    program = active(graph_spec(authority=delegated))
    pending = program.apply(PrepareAttempt(program.revision, DELEGATE, attempt_spec(program, "a1", "first")))
    restricted = amend(pending, authority=AuthorityEnvelope(OWNER, trusted_satisfaction_issuers=frozenset({VERIFIER})))
    assert restricted.attempt("a1").actor_id == DELEGATE
    assert restricted.attempt("a1").status is AttemptStatus.CANCELLED
    assert restricted.attempt_cancellations[-1].record_order == restricted.revision


def test_terminal_attempts_never_reopen_across_amendments() -> None:
    program = finished(active())
    first = program.spec.work_unit("first")
    changed = WorkUnit(
        "first",
        "new obligation",
        required_inputs=first.required_inputs,
        outputs=first.outputs,
        acceptance_criteria=first.acceptance_criteria,
    )
    revised = amend(program, work_units=(changed, program.spec.work_unit("second")))
    restored = amend(revised, work_units=program.spec.work_units)
    assert restored.attempt("a1").status is AttemptStatus.FINISHED
    assert restored.attempt("a1").spec == program.attempt("a1").spec


def test_amendments_do_not_reset_attempt_admission_history_or_budget() -> None:
    program = active(graph_spec(budget=BudgetPolicy(max_attempts=1, max_active_attempts=1)))
    done = finished(program)
    changed = amend(done, budget=BudgetPolicy(max_attempts=1, max_active_attempts=1), objective="new objective")
    with pytest.raises(BudgetExceeded):
        prepared(changed, "first", "a2")
    assert tuple(item.attempt_id for item in changed.attempts) == ("a1",)


def test_exhausted_admission_and_pause_do_not_block_lawful_terminal_observations() -> None:
    program = active(graph_spec(budget=BudgetPolicy(max_attempts=1, max_active_attempts=1)))
    pending = prepared(program)
    executing = pending.apply(StartAttempt(pending.revision, OWNER, "a1"))
    paused = executing.apply(PauseProgram(executing.revision, OWNER))
    failed = paused.apply(FailAttempt(paused.revision, OWNER, "a1"))
    assert failed.attempt("a1").status is AttemptStatus.FAILED
    assert failed.status is ProgramStatus.PAUSED
    resumed = failed.apply(ResumeProgram(failed.revision, OWNER))
    with pytest.raises(BudgetExceeded):
        prepared(resumed, "first", "a2")


def test_executing_attempt_can_finish_while_program_is_paused() -> None:
    pending = prepared(active(graph_spec(budget=BudgetPolicy(max_attempts=1))))
    executing = pending.apply(StartAttempt(pending.revision, OWNER, "a1"))
    paused = executing.apply(PauseProgram(executing.revision, OWNER))
    completed_attempt = paused.apply(FinishAttempt(paused.revision, OWNER, "a1"))
    assert completed_attempt.status is ProgramStatus.PAUSED
    assert completed_attempt.attempt("a1").status is AttemptStatus.FINISHED
    assert completed_attempt.state("first").active_attempt_id is None


def test_owner_control_does_not_depend_on_delegated_operational_allowlist() -> None:
    spec = graph_spec(
        authority=AuthorityEnvelope(
            OWNER,
            allowed_actions=frozenset(
                {DomainAction.ACTIVATE_PROGRAM, DomainAction.PAUSE_PROGRAM, DomainAction.RESUME_PROGRAM}
            ),
            max_attempts=0,
            trusted_satisfaction_issuers=frozenset({VERIFIER}),
        )
    )
    program = active(spec)
    assert program.ready_work_unit_ids == ("first",)
    with pytest.raises(AuthorityViolation):
        prepared(program)
    paused = program.apply(PauseProgram(program.revision, OWNER))
    assert paused.apply(ResumeProgram(paused.revision, OWNER)).status is ProgramStatus.ACTIVE
    assert paused.apply(CancelProgram(paused.revision, OWNER)).status is ProgramStatus.CANCELLED
    assert program.apply(CancelWorkUnit(program.revision, OWNER, "first")).status is ProgramStatus.ACTIVE
    assert amend(program, objective="owner-approved change").spec.revision == 2


def test_owner_control_survives_a_literally_empty_delegated_allowlist() -> None:
    empty = AuthorityEnvelope(OWNER, allowed_actions=frozenset())
    draft = Program.create(graph_spec(authority=empty))
    changed = amend(draft, objective="owner can still revise intent")
    assert changed.spec.revision == 2
    assert changed.apply(CancelProgram(changed.revision, OWNER)).status is ProgramStatus.CANCELLED
    activated = active()
    restricted = amend(activated, authority=empty)
    assert restricted.status is ProgramStatus.ACTIVE
    assert restricted.apply(CancelWorkUnit(restricted.revision, OWNER, "first")).status is ProgramStatus.ACTIVE


def test_actionless_and_zero_budget_programs_are_valid_present_facts() -> None:
    spec = graph_spec(
        budget=BudgetPolicy(max_attempts=0, max_active_attempts=0),
        authority=AuthorityEnvelope(
            OWNER,
            allowed_actions=frozenset({DomainAction.ACTIVATE_PROGRAM}),
            max_attempts=0,
            trusted_satisfaction_issuers=frozenset({VERIFIER}),
        ),
    )
    draft = Program.create(spec)
    assert draft.status is ProgramStatus.DRAFT
    assert draft.ready_work_unit_ids == ()
    activated = draft.apply(ActivateProgram(draft.revision, OWNER))
    assert activated.status is ProgramStatus.ACTIVE
    assert activated.state("first").status is WorkUnitStatus.READY
    assert activated.satisfactions == ()


def test_delegate_cannot_amend_owner_intent_or_expand_own_authority() -> None:
    envelope = AuthorityEnvelope(
        OWNER,
        allowed_actions=frozenset(
            {
                DomainAction.ACTIVATE_PROGRAM,
                DomainAction.PREPARE_ATTEMPT,
                DomainAction.START_ATTEMPT,
                DomainAction.FINISH_ATTEMPT,
            }
        ),
        delegated_actor_ids=frozenset({DELEGATE}),
        trusted_satisfaction_issuers=frozenset({VERIFIER}),
    )
    program = active(graph_spec(authority=envelope))
    with pytest.raises(AuthorityViolation):
        program.apply(
            AmendProgramSpec(program.revision, DELEGATE, SpecAmendment(1, authority=AuthorityEnvelope(OWNER)))
        )
    allowed = program.apply(PrepareAttempt(program.revision, DELEGATE, attempt_spec(program, "a1", "first")))
    assert allowed.attempt("a1").status is AttemptStatus.PREPARED


def test_owner_is_not_an_execution_budget_or_delegation_bypass() -> None:
    restricted = active(
        graph_spec(
            authority=AuthorityEnvelope(
                OWNER,
                allowed_actions=frozenset({DomainAction.ACTIVATE_PROGRAM}),
                trusted_satisfaction_issuers=frozenset({VERIFIER}),
            )
        )
    )
    with pytest.raises(AuthorityViolation):
        prepared(restricted)
    bounded = active(graph_spec(budget=BudgetPolicy(max_attempts=1)))
    done = finished(bounded)
    with pytest.raises(BudgetExceeded):
        prepared(done, "first", "a2")


def test_duplicate_attempts_and_unsupported_commands_are_rejected() -> None:
    program = finished(active())
    with pytest.raises(DuplicateAttempt):
        prepared(program, "first", "a1")
    with pytest.raises(InvalidDomainValue):
        program.apply(object())  # type: ignore[arg-type]


def test_public_construction_and_dataclass_replace_cannot_forge_lifecycle_state() -> None:
    current = active()
    with pytest.raises(InvalidDomainValue):
        Program(current.spec, status=ProgramStatus.COMPLETED)
    with pytest.raises(ValueError):
        replace(current, status=ProgramStatus.CANCELLED)
    with pytest.raises(ValueError):
        replace(current.spec, objective="forged objective")
    assert not hasattr(current, "set_state")
    assert not hasattr(current, "force_status")
    assert not hasattr(domain_module, "_TRANSITION_TOKEN")


def test_copy_subclass_and_reinitialization_cannot_change_pinned_intent() -> None:
    program = active()
    pinned = attempt_spec(program, "a1", "first")
    with pytest.raises(InvalidDomainValue):
        copy.copy(program.spec)
    with pytest.raises(InvalidDomainValue):
        copy.deepcopy(pinned)
    with pytest.raises(InvalidDomainValue):
        program.spec.__init__("forged", "forged", (unit("forged"),))
    with pytest.raises(InvalidDomainValue):
        SEED.__init__("seed", "ref:forged")
    with pytest.raises(TypeError):
        type("ForgedWorkUnit", (WorkUnit,), {})
    with pytest.raises(TypeError):
        type("ForgedProgram", (Program,), {})
    assert program.spec.initial_inputs == (SEED,)


def test_attempt_effective_inputs_are_frozen_even_after_spec_amendment() -> None:
    done = finished(active())
    old_inputs = done.attempt("a1").spec.effective_inputs
    with pytest.raises(FrozenInstanceError):
        done.attempt("a1").spec.effective_inputs = ()  # type: ignore[misc]
    amended = amend(done, initial_inputs=(InputBinding("seed", "ref:new-seed"),))
    assert amended.attempt("a1").spec.effective_inputs == old_inputs
    assert amended.resolved_inputs("first") != old_inputs


def test_new_attempt_pins_newly_approved_inputs_without_mutating_previous_attempt() -> None:
    done = finished(active())
    revised = amend(done, initial_inputs=(InputBinding("seed", "ref:approved-v2"),))
    retry = prepared(revised, "first", "a2")
    assert retry.attempt("a1").spec.effective_inputs == (SEED,)
    assert retry.attempt("a2").spec.effective_inputs == (InputBinding("seed", "ref:approved-v2"),)
    assert retry.attempt("a1").spec.spec_revision == 1
    assert retry.attempt("a2").spec.spec_revision == 2


def test_command_subclass_cannot_relabel_authorization() -> None:
    class ForgedActivate(ActivateProgram):
        action = DomainAction.PREPARE_ATTEMPT

    program = Program.create(graph_spec())
    with pytest.raises(InvalidDomainValue):
        program.apply(ForgedActivate(program.revision, OWNER))
    assert program.status is ProgramStatus.DRAFT
