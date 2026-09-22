# SPDX-License-Identifier: Apache-2.0
"""Pure K1 domain values and deterministic Program transitions.

This module deliberately stops at intent, admission and lifecycle state.  A
trusted prevalidated satisfaction reference is an input to the domain; the
producer of that reference belongs to a later verification slice.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import cast


class DomainError(Exception):
    """Base class for deterministic domain failures."""


class InvalidDomainValue(DomainError, ValueError):
    """A domain value does not satisfy its local invariant."""


class InvalidGraph(InvalidDomainValue):
    """A ProgramSpec graph is not a finite valid DAG."""


class MissingReference(InvalidGraph):
    """A graph or command references an unknown identity."""


class MissingInput(InvalidGraph):
    """A WorkUnit requires a logical input with no declared producer."""


class CycleDetected(InvalidGraph):
    """The WorkUnit dependency graph contains a cycle."""


class StaleRevision(DomainError):
    """A command was built against an older aggregate or spec revision."""


class IllegalTransition(DomainError):
    """A command is not legal for the current domain state."""


class AuthorityViolation(DomainError):
    """A command exceeds the owner-approved authority envelope."""


class BudgetExceeded(DomainError):
    """A deterministic admission budget would be exceeded."""


class DuplicateAttempt(DomainError):
    """An attempt identity is already present in the Program."""


class ProgramStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkUnitStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    ACTIVE = "active"
    SATISFIED = "satisfied"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DomainAction(StrEnum):
    ACTIVATE_PROGRAM = "activate_program"
    PAUSE_PROGRAM = "pause_program"
    RESUME_PROGRAM = "resume_program"
    CANCEL_PROGRAM = "cancel_program"
    CANCEL_WORK_UNIT = "cancel_work_unit"
    PREPARE_ATTEMPT = "prepare_attempt"
    START_ATTEMPT = "start_attempt"
    FINISH_ATTEMPT = "finish_attempt"
    FAIL_ATTEMPT = "fail_attempt"
    CANCEL_ATTEMPT = "cancel_attempt"
    SATISFY_WORK_UNIT = "satisfy_work_unit"
    AMEND_SPEC = "amend_spec"


def _nonempty(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise InvalidDomainValue(f"{field} must be a nonempty string")
    return value


def _optional_nonempty(value: object, field: str) -> None:
    if value is not None:
        _nonempty(value, field)


def _revision(value: object, field: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        qualifier = "nonnegative" if allow_zero else "positive"
        raise InvalidDomainValue(f"{field} must be a {qualifier} integer")


def _immutable_strings(value: object, field: str) -> frozenset[str]:
    if type(value) is not frozenset:
        raise InvalidDomainValue(f"{field} must be an immutable set of nonempty identifiers")
    values = cast(frozenset[object], value)
    if any(type(item) is not str or not item.strip() for item in values):
        raise InvalidDomainValue(f"{field} must be an immutable set of nonempty identifiers")
    return cast(frozenset[str], values)


def _immutable_tuple(value: object, field: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise InvalidDomainValue(f"{field} must be an immutable tuple")
    return cast(tuple[object, ...], value)


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Finite admission limits; no runtime consumption or provider accounting."""

    max_attempts: int = 3
    max_active_attempts: int = 1

    def __post_init__(self) -> None:
        _revision(self.max_attempts, "max_attempts", allow_zero=True)
        _revision(self.max_active_attempts, "max_active_attempts", allow_zero=True)


@dataclass(frozen=True, slots=True)
class AuthorityEnvelope:
    """Owner-approved command scope and finite attempt ceiling."""

    owner_id: str
    allowed_actions: frozenset[DomainAction] = frozenset(DomainAction)
    max_attempts: int = 3
    trusted_satisfaction_issuers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _nonempty(self.owner_id, "owner_id")
        if type(self.allowed_actions) is not frozenset:
            raise InvalidDomainValue("allowed_actions must be an immutable set")
        actions: frozenset[DomainAction] = frozenset()
        try:
            actions = frozenset(
                item if isinstance(item, DomainAction) else DomainAction(item)
                for item in cast(frozenset[object], self.allowed_actions)
            )
        except (TypeError, ValueError) as error:
            raise InvalidDomainValue("allowed_actions must contain domain actions") from error
        object.__setattr__(self, "allowed_actions", actions)
        _revision(self.max_attempts, "max_attempts", allow_zero=True)
        issuers = _immutable_strings(self.trusted_satisfaction_issuers, "trusted_satisfaction_issuers")
        object.__setattr__(self, "trusted_satisfaction_issuers", issuers)

    def payload(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id,
            "allowed_actions": tuple(sorted(action.value for action in self.allowed_actions)),
            "max_attempts": self.max_attempts,
            "trusted_satisfaction_issuers": tuple(sorted(self.trusted_satisfaction_issuers)),
        }


@dataclass(frozen=True, slots=True)
class WorkUnit:
    """One bounded graph node with logical inputs and outputs."""

    work_unit_id: str
    dependencies: tuple[str, ...] = ()
    required_inputs: frozenset[str] = frozenset()
    outputs: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _nonempty(self.work_unit_id, "work_unit_id")
        dependencies = _immutable_tuple(self.dependencies, "dependencies")
        if any(type(item) is not str or not item.strip() for item in dependencies):
            raise InvalidDomainValue("dependencies must contain nonempty identifiers")
        if len(set(dependencies)) != len(dependencies):
            raise InvalidDomainValue(f"WorkUnit {self.work_unit_id!r} repeats a dependency")
        object.__setattr__(self, "dependencies", tuple(sorted(cast(tuple[str, ...], dependencies))))
        required_inputs = _immutable_strings(self.required_inputs, "required_inputs")
        outputs = _immutable_strings(self.outputs, "outputs")
        if required_inputs & outputs:
            raise InvalidDomainValue(f"WorkUnit {self.work_unit_id!r} consumes and produces the same input")

    @property
    def id(self) -> str:
        """Short identity alias for graph-oriented callers."""

        return self.work_unit_id

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.dependencies

    @property
    def input_names(self) -> frozenset[str]:
        return self.required_inputs

    @property
    def output_names(self) -> frozenset[str]:
        return self.outputs

    def payload(self) -> dict[str, object]:
        return {
            "work_unit_id": self.work_unit_id,
            "dependencies": self.dependencies,
            "required_inputs": tuple(sorted(self.required_inputs)),
            "outputs": tuple(sorted(self.outputs)),
        }

    @property
    def digest(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class ProgramSpec:
    """Immutable owner intent and a validated finite WorkUnit graph."""

    program_id: str
    objective: str
    work_units: tuple[WorkUnit, ...]
    initial_inputs: frozenset[str] = frozenset()
    budget: BudgetPolicy = BudgetPolicy()
    authority: AuthorityEnvelope = AuthorityEnvelope("owner")
    revision: int = 1
    parent_digest: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.program_id, "program_id")
        _nonempty(self.objective, "objective")
        work_units = _immutable_tuple(self.work_units, "work_units")
        if not work_units or any(not isinstance(unit, WorkUnit) for unit in work_units):
            raise InvalidDomainValue("work_units must be a nonempty tuple of WorkUnit values")
        units = cast(tuple[WorkUnit, ...], work_units)
        unit_ids = tuple(unit.work_unit_id for unit in units)
        if len(set(unit_ids)) != len(unit_ids):
            raise InvalidGraph("work_units must have unique identifiers")
        initial_inputs = _immutable_strings(self.initial_inputs, "initial_inputs")
        if type(cast(object, self.budget)) is not BudgetPolicy:
            raise InvalidDomainValue("budget must be a BudgetPolicy")
        if type(cast(object, self.authority)) is not AuthorityEnvelope:
            raise InvalidDomainValue("authority must be an AuthorityEnvelope")
        _revision(self.revision, "revision")
        _optional_nonempty(self.parent_digest, "parent_digest")
        by_id = {unit.work_unit_id: unit for unit in units}
        output_producers: dict[str, str] = {}
        for unit in units:
            for dependency in unit.dependencies:
                if dependency not in by_id:
                    raise MissingReference(f"WorkUnit {unit.work_unit_id!r} depends on missing WorkUnit {dependency!r}")
            for output in unit.outputs:
                if output in initial_inputs:
                    raise InvalidGraph(f"input {output!r} is both initial and produced")
                previous = output_producers.get(output)
                if previous is not None:
                    raise InvalidGraph(f"input {output!r} has multiple producers: {previous!r} and {unit.id!r}")
                output_producers[output] = unit.work_unit_id
        _topological_order(by_id)
        for unit in units:
            available = set(initial_inputs)
            for dependency in unit.dependencies:
                available.update(by_id[dependency].outputs)
            missing = sorted(set(unit.required_inputs) - available)
            if missing:
                raise MissingInput(
                    f"WorkUnit {unit.work_unit_id!r} has missing inputs: {', '.join(repr(item) for item in missing)}"
                )

    @property
    def digest(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "program_id": self.program_id,
            "objective": self.objective,
            "work_units": tuple(unit.payload() for unit in self.work_units),
            "initial_inputs": tuple(sorted(self.initial_inputs)),
            "budget": {
                "max_attempts": self.budget.max_attempts,
                "max_active_attempts": self.budget.max_active_attempts,
            },
            "authority": self.authority.payload(),
            "revision": self.revision,
            "parent_digest": self.parent_digest,
        }

    @property
    def topological_order(self) -> tuple[str, ...]:
        return _topological_order({unit.work_unit_id: unit for unit in self.work_units})

    def work_unit(self, work_unit_id: str) -> WorkUnit:
        for unit in self.work_units:
            if unit.work_unit_id == work_unit_id:
                return unit
        raise MissingReference(f"unknown WorkUnit {work_unit_id!r}")

    def amend(self, amendment: SpecAmendment) -> ProgramSpec:
        if amendment.expected_revision != self.revision:
            raise StaleRevision(
                f"expected spec revision {amendment.expected_revision}, current revision is {self.revision}"
            )
        return ProgramSpec(
            program_id=self.program_id,
            objective=self.objective if amendment.objective is None else amendment.objective,
            work_units=self.work_units if amendment.work_units is None else amendment.work_units,
            initial_inputs=self.initial_inputs if amendment.initial_inputs is None else amendment.initial_inputs,
            budget=self.budget if amendment.budget is None else amendment.budget,
            authority=self.authority if amendment.authority is None else amendment.authority,
            revision=self.revision + 1,
            parent_digest=self.digest,
        )


def _topological_order(units: dict[str, WorkUnit]) -> tuple[str, ...]:
    remaining = {unit_id: set(unit.dependencies) for unit_id, unit in units.items()}
    order: list[str] = []
    while remaining:
        available = sorted(unit_id for unit_id, dependencies in remaining.items() if not dependencies)
        if not available:
            cycle = ", ".join(sorted(remaining))
            raise CycleDetected(f"WorkUnit dependency cycle involves: {cycle}")
        order.extend(available)
        for unit_id in available:
            del remaining[unit_id]
        completed = set(available)
        for dependencies in remaining.values():
            dependencies.difference_update(completed)
    return tuple(order)


@dataclass(frozen=True, slots=True)
class InputBinding:
    """An immutable logical input reference pinned into one AttemptSpec."""

    name: str
    reference: str

    def __post_init__(self) -> None:
        _nonempty(self.name, "input name")
        _nonempty(self.reference, "input reference")


@dataclass(frozen=True, slots=True)
class AttemptSpec:
    """Immutable effective inputs for one execution attempt."""

    attempt_id: str
    program_id: str
    work_unit_id: str
    spec_revision: int
    spec_digest: str
    effective_inputs: tuple[InputBinding, ...] = ()
    allocation_reference: str | None = None
    agent_definition_reference: str | None = None
    workspace_reference: str | None = None
    context_reference: str | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.attempt_id, "attempt_id"),
            (self.program_id, "program_id"),
            (self.work_unit_id, "work_unit_id"),
            (self.spec_digest, "spec_digest"),
        ):
            _nonempty(value, field)
        _revision(self.spec_revision, "spec_revision")
        values = _immutable_tuple(self.effective_inputs, "effective_inputs")
        if any(not isinstance(item, InputBinding) for item in values):
            raise InvalidDomainValue("effective_inputs must contain InputBinding values")
        inputs = cast(tuple[InputBinding, ...], values)
        names = tuple(item.name for item in inputs)
        if len(set(names)) != len(names):
            raise InvalidDomainValue("effective_inputs must contain one binding per input name")
        object.__setattr__(self, "effective_inputs", tuple(sorted(inputs, key=lambda item: item.name)))
        for value, field in (
            (self.allocation_reference, "allocation_reference"),
            (self.agent_definition_reference, "agent_definition_reference"),
            (self.workspace_reference, "workspace_reference"),
            (self.context_reference, "context_reference"),
        ):
            _optional_nonempty(value, field)

    @property
    def input_names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.effective_inputs)

    @property
    def digest(self) -> str:
        return _digest(
            {
                "attempt_id": self.attempt_id,
                "program_id": self.program_id,
                "work_unit_id": self.work_unit_id,
                "spec_revision": self.spec_revision,
                "spec_digest": self.spec_digest,
                "effective_inputs": tuple(
                    {"name": item.name, "reference": item.reference} for item in self.effective_inputs
                ),
                "allocation_reference": self.allocation_reference,
                "agent_definition_reference": self.agent_definition_reference,
                "workspace_reference": self.workspace_reference,
                "context_reference": self.context_reference,
            }
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    """Attempt record whose effective specification is never rewritten."""

    spec: AttemptSpec
    status: AttemptStatus = AttemptStatus.PREPARED

    def __post_init__(self) -> None:
        if type(cast(object, self.spec)) is not AttemptSpec:
            raise InvalidDomainValue("spec must be an AttemptSpec")
        if type(cast(object, self.status)) is not AttemptStatus:
            raise InvalidDomainValue("status must be an AttemptStatus")

    @property
    def attempt_id(self) -> str:
        return self.spec.attempt_id


@dataclass(frozen=True, slots=True)
class TrustedSatisfaction:
    """A typed, prevalidated reference used to advance one WorkUnit."""

    reference_id: str
    program_id: str
    work_unit_id: str
    spec_revision: int
    spec_digest: str
    work_unit_digest: str
    issuer_id: str
    source_attempt_id: str | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.reference_id, "reference_id"),
            (self.program_id, "program_id"),
            (self.work_unit_id, "work_unit_id"),
            (self.spec_digest, "spec_digest"),
            (self.work_unit_digest, "work_unit_digest"),
            (self.issuer_id, "issuer_id"),
        ):
            _nonempty(value, field)
        _revision(self.spec_revision, "spec_revision")
        _optional_nonempty(self.source_attempt_id, "source_attempt_id")


@dataclass(frozen=True, slots=True)
class WorkUnitState:
    """Current projection for a WorkUnit within one immutable Program value."""

    work_unit_id: str
    status: WorkUnitStatus = WorkUnitStatus.PENDING
    active_attempt_id: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.work_unit_id, "work_unit_id")
        if type(cast(object, self.status)) is not WorkUnitStatus:
            raise InvalidDomainValue("status must be a WorkUnitStatus")
        _optional_nonempty(self.active_attempt_id, "active_attempt_id")
        if self.status is not WorkUnitStatus.ACTIVE and self.active_attempt_id is not None:
            raise InvalidDomainValue("only an active WorkUnit may have an active attempt")


@dataclass(frozen=True, slots=True)
class SpecAmendment:
    """Owner-authored replacement fields for one new ProgramSpec revision."""

    expected_revision: int
    objective: str | None = None
    work_units: tuple[WorkUnit, ...] | None = None
    initial_inputs: frozenset[str] | None = None
    budget: BudgetPolicy | None = None
    authority: AuthorityEnvelope | None = None
    reason: str = "owner amendment"

    def __post_init__(self) -> None:
        _revision(self.expected_revision, "expected_revision")
        _nonempty(self.reason, "reason")
        if all(
            value is None
            for value in (self.objective, self.work_units, self.initial_inputs, self.budget, self.authority)
        ):
            raise InvalidDomainValue("an amendment must replace at least one field")
        if self.objective is not None:
            _nonempty(self.objective, "objective")
        if self.work_units is not None:
            values = _immutable_tuple(self.work_units, "work_units")
            if not values or any(not isinstance(item, WorkUnit) for item in values):
                raise InvalidDomainValue("work_units must be a nonempty tuple of WorkUnit values")
        if self.initial_inputs is not None:
            _immutable_strings(self.initial_inputs, "initial_inputs")
        if self.budget is not None and type(cast(object, self.budget)) is not BudgetPolicy:
            raise InvalidDomainValue("budget must be a BudgetPolicy")
        if self.authority is not None and type(cast(object, self.authority)) is not AuthorityEnvelope:
            raise InvalidDomainValue("authority must be an AuthorityEnvelope")


@dataclass(frozen=True, slots=True)
class DomainCommand:
    """Base for owner or trusted-prevalidated domain commands."""

    expected_revision: int
    actor_id: str

    def __post_init__(self) -> None:
        _revision(self.expected_revision, "expected_revision", allow_zero=True)
        _nonempty(self.actor_id, "actor_id")


@dataclass(frozen=True, slots=True)
class ActivateProgram(DomainCommand):
    pass


@dataclass(frozen=True, slots=True)
class PauseProgram(DomainCommand):
    pass


@dataclass(frozen=True, slots=True)
class ResumeProgram(DomainCommand):
    pass


@dataclass(frozen=True, slots=True)
class CancelProgram(DomainCommand):
    pass


@dataclass(frozen=True, slots=True)
class CancelWorkUnit(DomainCommand):
    work_unit_id: str

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        _nonempty(self.work_unit_id, "work_unit_id")


@dataclass(frozen=True, slots=True)
class PrepareAttempt(DomainCommand):
    attempt: AttemptSpec

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        if type(cast(object, self.attempt)) is not AttemptSpec:
            raise InvalidDomainValue("attempt must be an AttemptSpec")


@dataclass(frozen=True, slots=True)
class StartAttempt(DomainCommand):
    attempt_id: str

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        _nonempty(self.attempt_id, "attempt_id")


@dataclass(frozen=True, slots=True)
class FinishAttempt(DomainCommand):
    attempt_id: str

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        _nonempty(self.attempt_id, "attempt_id")


@dataclass(frozen=True, slots=True)
class FailAttempt(DomainCommand):
    attempt_id: str

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        _nonempty(self.attempt_id, "attempt_id")


@dataclass(frozen=True, slots=True)
class CancelAttempt(DomainCommand):
    attempt_id: str

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        _nonempty(self.attempt_id, "attempt_id")


@dataclass(frozen=True, slots=True)
class SatisfyWorkUnit(DomainCommand):
    satisfaction: TrustedSatisfaction

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        if type(cast(object, self.satisfaction)) is not TrustedSatisfaction:
            raise InvalidDomainValue("satisfaction must be a TrustedSatisfaction")


@dataclass(frozen=True, slots=True)
class AmendProgramSpec(DomainCommand):
    amendment: SpecAmendment

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        if type(cast(object, self.amendment)) is not SpecAmendment:
            raise InvalidDomainValue("amendment must be a SpecAmendment")


type DomainCommandType = (
    ActivateProgram
    | PauseProgram
    | ResumeProgram
    | CancelProgram
    | CancelWorkUnit
    | PrepareAttempt
    | StartAttempt
    | FinishAttempt
    | FailAttempt
    | CancelAttempt
    | SatisfyWorkUnit
    | AmendProgramSpec
)


def _mark_ready(spec: ProgramSpec, states: tuple[WorkUnitState, ...]) -> tuple[WorkUnitState, ...]:
    by_id = {state.work_unit_id: state for state in states}
    satisfied = {state.work_unit_id for state in states if state.status is WorkUnitStatus.SATISFIED}
    updated: list[WorkUnitState] = []
    for unit in spec.work_units:
        state = by_id[unit.work_unit_id]
        if state.status in (WorkUnitStatus.PENDING, WorkUnitStatus.READY):
            ready = all(dependency in satisfied for dependency in unit.dependencies)
            state = WorkUnitState(
                unit.work_unit_id,
                WorkUnitStatus.READY if ready else WorkUnitStatus.PENDING,
            )
        updated.append(state)
    return tuple(updated)


def _descendants(spec: ProgramSpec, seeds: set[str]) -> set[str]:
    dependents: dict[str, set[str]] = {unit.work_unit_id: set() for unit in spec.work_units}
    for unit in spec.work_units:
        for dependency in unit.dependencies:
            dependents[dependency].add(unit.work_unit_id)
    affected = set(seeds) & dependents.keys()
    pending = sorted(affected)
    while pending:
        work_unit_id = pending.pop(0)
        for dependent in sorted(dependents[work_unit_id]):
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return affected


def _amendment_affected_work_units(previous: ProgramSpec, amended: ProgramSpec) -> set[str]:
    previous_units = {unit.work_unit_id: unit for unit in previous.work_units}
    amended_units = {unit.work_unit_id: unit for unit in amended.work_units}
    changed = {
        work_unit_id
        for work_unit_id in previous_units.keys() | amended_units.keys()
        if previous_units.get(work_unit_id) != amended_units.get(work_unit_id)
    }
    changed_inputs = previous.initial_inputs ^ amended.initial_inputs
    changed.update(unit.work_unit_id for unit in amended.work_units if unit.required_inputs & changed_inputs)
    return _descendants(previous, changed) | _descendants(amended, changed)


@dataclass(frozen=True, slots=True)
class Program:
    """Immutable aggregate enforcing legal Program and WorkUnit transitions."""

    spec: ProgramSpec
    status: ProgramStatus = ProgramStatus.DRAFT
    revision: int = 0
    work_unit_states: tuple[WorkUnitState, ...] = ()
    attempts: tuple[Attempt, ...] = ()
    satisfactions: tuple[TrustedSatisfaction, ...] = ()
    spec_history: tuple[ProgramSpec, ...] = ()

    def __post_init__(self) -> None:
        self._validate(allow_transition=False)

    def _validate(self, *, allow_transition: bool) -> None:
        if type(cast(object, self.spec)) is not ProgramSpec:
            raise InvalidDomainValue("spec must be a ProgramSpec")
        if type(cast(object, self.status)) is not ProgramStatus:
            raise InvalidDomainValue("status must be a ProgramStatus")
        if not allow_transition and any((self.work_unit_states, self.attempts, self.satisfactions, self.spec_history)):
            raise InvalidDomainValue("use Program.create or a domain command to construct Program state")
        if not allow_transition and self.revision != 0:
            raise InvalidDomainValue("a new Program must start at aggregate revision zero")
        if not allow_transition and self.status is not ProgramStatus.DRAFT:
            raise InvalidDomainValue("non-draft Programs must be produced by a domain command")
        _revision(self.revision, "revision", allow_zero=True)
        if type(self.work_unit_states) is not tuple:
            raise InvalidDomainValue("work_unit_states must be an immutable tuple")
        if type(self.attempts) is not tuple or any(type(cast(object, item)) is not Attempt for item in self.attempts):
            raise InvalidDomainValue("attempts must be an immutable tuple of Attempt values")
        if type(self.satisfactions) is not tuple or any(
            type(cast(object, item)) is not TrustedSatisfaction for item in self.satisfactions
        ):
            raise InvalidDomainValue("satisfactions must be an immutable tuple of references")
        if type(self.spec_history) is not tuple:
            raise InvalidDomainValue("spec_history must be an immutable tuple")
        if not self.spec_history:
            object.__setattr__(self, "spec_history", (self.spec,))
        elif self.spec_history[-1] != self.spec:
            raise InvalidDomainValue("spec_history must end with the current spec")
        states = self.work_unit_states
        if not states:
            states = tuple(WorkUnitState(unit.work_unit_id) for unit in self.spec.work_units)
        if any(type(cast(object, item)) is not WorkUnitState for item in states):
            raise InvalidDomainValue("work_unit_states must contain WorkUnitState values")
        ids = tuple(item.work_unit_id for item in states)
        expected_ids = tuple(unit.work_unit_id for unit in self.spec.work_units)
        if ids != expected_ids:
            raise InvalidDomainValue("work_unit_states must follow the current spec WorkUnit order")
        if self.status is ProgramStatus.ACTIVE:
            states = _mark_ready(self.spec, states)
        object.__setattr__(self, "work_unit_states", states)
        if self.status is ProgramStatus.COMPLETED and any(
            state.status is not WorkUnitStatus.SATISFIED for state in states
        ):
            raise InvalidDomainValue("a completed Program must have every WorkUnit satisfied")
        if self.status is ProgramStatus.ACTIVE and all(state.status is WorkUnitStatus.SATISFIED for state in states):
            raise InvalidDomainValue("an active Program cannot have every WorkUnit satisfied")
        attempt_ids = tuple(item.attempt_id for item in self.attempts)
        if len(set(attempt_ids)) != len(attempt_ids):
            raise DuplicateAttempt("attempt identities must be unique")
        for attempt in self.attempts:
            if attempt.spec.program_id != self.spec.program_id:
                raise InvalidDomainValue("attempt belongs to a different Program")
            if attempt.spec.spec_revision > self.spec.revision:
                raise InvalidDomainValue("attempt cannot reference a future spec revision")

    @classmethod
    def _from_transition(
        cls,
        *,
        spec: ProgramSpec,
        status: ProgramStatus,
        revision: int,
        work_unit_states: tuple[WorkUnitState, ...],
        attempts: tuple[Attempt, ...],
        satisfactions: tuple[TrustedSatisfaction, ...],
        spec_history: tuple[ProgramSpec, ...],
    ) -> Program:
        instance = object.__new__(cls)
        object.__setattr__(instance, "spec", spec)
        object.__setattr__(instance, "status", status)
        object.__setattr__(instance, "revision", revision)
        object.__setattr__(instance, "work_unit_states", work_unit_states)
        object.__setattr__(instance, "attempts", attempts)
        object.__setattr__(instance, "satisfactions", satisfactions)
        object.__setattr__(instance, "spec_history", spec_history)
        instance._validate(allow_transition=True)
        return instance

    @classmethod
    def create(cls, spec: ProgramSpec) -> Program:
        return cls(spec=spec)

    @property
    def program_id(self) -> str:
        return self.spec.program_id

    @property
    def aggregate_revision(self) -> int:
        return self.revision

    @property
    def spec_revision(self) -> int:
        return self.spec.revision

    @property
    def ready_work_unit_ids(self) -> tuple[str, ...]:
        if self.status is not ProgramStatus.ACTIVE:
            return ()
        return tuple(state.work_unit_id for state in self.work_unit_states if state.status is WorkUnitStatus.READY)

    @property
    def ready_work_units(self) -> tuple[WorkUnit, ...]:
        return tuple(self.spec.work_unit(work_unit_id) for work_unit_id in self.ready_work_unit_ids)

    def state(self, work_unit_id: str) -> WorkUnitState:
        for state in self.work_unit_states:
            if state.work_unit_id == work_unit_id:
                return state
        raise MissingReference(f"unknown WorkUnit {work_unit_id!r}")

    def attempt(self, attempt_id: str) -> Attempt:
        for attempt in self.attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
        raise MissingReference(f"unknown attempt {attempt_id!r}")

    def apply(self, command: DomainCommandType) -> Program:
        if type(command) is ActivateProgram:
            return self._activate(command)
        if type(command) is PauseProgram:
            return self._pause(command)
        if type(command) is ResumeProgram:
            return self._resume(command)
        if type(command) is CancelProgram:
            return self._cancel_program(command)
        if type(command) is CancelWorkUnit:
            return self._cancel_work_unit(command)
        if type(command) is PrepareAttempt:
            return self._prepare_attempt(command)
        if type(command) is StartAttempt:
            return self._start_attempt(command)
        if type(command) is FinishAttempt:
            return self._finish_attempt(command)
        if type(command) is FailAttempt:
            return self._fail_attempt(command)
        if type(command) is CancelAttempt:
            return self._cancel_attempt(command)
        if type(command) is SatisfyWorkUnit:
            return self._satisfy_work_unit(command)
        if type(command) is AmendProgramSpec:
            return self._amend_spec(command)
        raise InvalidDomainValue(f"unsupported domain command: {type(command).__name__}")

    def dispatch(self, command: DomainCommandType) -> Program:
        return self.apply(command)

    def execute(self, command: DomainCommandType) -> Program:
        return self.apply(command)

    def activate(self, expected_revision: int, actor_id: str) -> Program:
        return self.apply(ActivateProgram(expected_revision, actor_id))

    def pause(self, expected_revision: int, actor_id: str) -> Program:
        return self.apply(PauseProgram(expected_revision, actor_id))

    def resume(self, expected_revision: int, actor_id: str) -> Program:
        return self.apply(ResumeProgram(expected_revision, actor_id))

    def cancel(self, expected_revision: int, actor_id: str) -> Program:
        return self.apply(CancelProgram(expected_revision, actor_id))

    def _check_revision(self, expected_revision: int) -> None:
        if expected_revision != self.revision:
            raise StaleRevision(f"expected aggregate revision {expected_revision}, current revision is {self.revision}")

    def _authorize(self, action: DomainAction, actor_id: str, *, trusted_satisfaction: bool = False) -> None:
        if action not in self.spec.authority.allowed_actions:
            raise AuthorityViolation(f"action {action.value!r} is outside the authority envelope")
        if actor_id == self.spec.authority.owner_id:
            return
        if trusted_satisfaction and actor_id in self.spec.authority.trusted_satisfaction_issuers:
            return
        raise AuthorityViolation(f"actor {actor_id!r} is not authorized for {action.value!r}")

    def _require_status(self, *allowed: ProgramStatus) -> None:
        if self.status not in allowed:
            names = ", ".join(item.value for item in allowed)
            raise IllegalTransition(f"Program is {self.status.value!r}; requires {names}")

    def _replace_state(self, replacement: WorkUnitState) -> tuple[WorkUnitState, ...]:
        return tuple(
            replacement if state.work_unit_id == replacement.work_unit_id else state for state in self.work_unit_states
        )

    def _replace_attempt(self, replacement: Attempt) -> tuple[Attempt, ...]:
        return tuple(
            replacement if attempt.attempt_id == replacement.attempt_id else attempt for attempt in self.attempts
        )

    def _next(
        self,
        *,
        status: ProgramStatus | None = None,
        spec: ProgramSpec | None = None,
        states: tuple[WorkUnitState, ...] | None = None,
        attempts: tuple[Attempt, ...] | None = None,
        satisfactions: tuple[TrustedSatisfaction, ...] | None = None,
        spec_history: tuple[ProgramSpec, ...] | None = None,
    ) -> Program:
        return Program._from_transition(
            spec=self.spec if spec is None else spec,
            status=self.status if status is None else status,
            revision=self.revision + 1,
            work_unit_states=self.work_unit_states if states is None else states,
            attempts=self.attempts if attempts is None else attempts,
            satisfactions=self.satisfactions if satisfactions is None else satisfactions,
            spec_history=self.spec_history if spec_history is None else spec_history,
        )

    def _activate(self, command: ActivateProgram) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.ACTIVATE_PROGRAM, command.actor_id)
        self._require_status(ProgramStatus.DRAFT)
        return self._next(status=ProgramStatus.ACTIVE, states=_mark_ready(self.spec, self.work_unit_states))

    def _pause(self, command: PauseProgram) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.PAUSE_PROGRAM, command.actor_id)
        self._require_status(ProgramStatus.ACTIVE)
        return self._next(status=ProgramStatus.PAUSED)

    def _resume(self, command: ResumeProgram) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.RESUME_PROGRAM, command.actor_id)
        self._require_status(ProgramStatus.PAUSED)
        states = _mark_ready(self.spec, self.work_unit_states)
        status = (
            ProgramStatus.COMPLETED
            if all(item.status is WorkUnitStatus.SATISFIED for item in states)
            else ProgramStatus.ACTIVE
        )
        return self._next(status=status, states=states)

    def _cancel_program(self, command: CancelProgram) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.CANCEL_PROGRAM, command.actor_id)
        self._require_status(ProgramStatus.DRAFT, ProgramStatus.ACTIVE, ProgramStatus.PAUSED)
        states = tuple(
            state
            if state.status is WorkUnitStatus.SATISFIED
            else WorkUnitState(state.work_unit_id, WorkUnitStatus.CANCELLED)
            for state in self.work_unit_states
        )
        attempts = tuple(
            Attempt(attempt.spec, AttemptStatus.CANCELLED)
            if attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
            else attempt
            for attempt in self.attempts
        )
        return self._next(status=ProgramStatus.CANCELLED, states=states, attempts=attempts)

    def _cancel_work_unit(self, command: CancelWorkUnit) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.CANCEL_WORK_UNIT, command.actor_id)
        self._require_status(ProgramStatus.ACTIVE)
        current = self.state(command.work_unit_id)
        if current.status in (WorkUnitStatus.SATISFIED, WorkUnitStatus.CANCELLED):
            raise IllegalTransition(f"WorkUnit {command.work_unit_id!r} is already terminal")
        if current.active_attempt_id is not None:
            raise IllegalTransition("cancel the active attempt before cancelling its WorkUnit")
        return self._next(states=self._replace_state(WorkUnitState(command.work_unit_id, WorkUnitStatus.CANCELLED)))

    def _prepare_attempt(self, command: PrepareAttempt) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.PREPARE_ATTEMPT, command.actor_id)
        self._require_status(ProgramStatus.ACTIVE)
        attempt_spec = command.attempt
        if any(attempt.attempt_id == attempt_spec.attempt_id for attempt in self.attempts):
            raise DuplicateAttempt(f"attempt {attempt_spec.attempt_id!r} already exists")
        if attempt_spec.program_id != self.program_id:
            raise AuthorityViolation("attempt is scoped to a different Program")
        if attempt_spec.spec_revision != self.spec.revision or attempt_spec.spec_digest != self.spec.digest:
            raise StaleRevision("attempt inputs must pin the current ProgramSpec revision and digest")
        unit = self.spec.work_unit(attempt_spec.work_unit_id)
        current = self.state(unit.work_unit_id)
        if current.status is not WorkUnitStatus.READY:
            raise IllegalTransition(f"WorkUnit {unit.work_unit_id!r} is {current.status.value!r}, not ready")
        if attempt_spec.input_names != unit.required_inputs:
            missing = sorted(unit.required_inputs - attempt_spec.input_names)
            extra = sorted(attempt_spec.input_names - unit.required_inputs)
            details: list[str] = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if extra:
                details.append(f"unexpected {', '.join(extra)}")
            raise MissingInput(f"attempt inputs for {unit.work_unit_id!r}: {'; '.join(details)}")
        active_count = sum(
            1 for attempt in self.attempts if attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
        )
        if len(self.attempts) >= min(self.spec.budget.max_attempts, self.spec.authority.max_attempts):
            raise BudgetExceeded("attempt admission limit exceeded")
        if active_count >= self.spec.budget.max_active_attempts:
            raise BudgetExceeded("active attempt admission limit exceeded")
        attempt = Attempt(attempt_spec)
        states = self._replace_state(WorkUnitState(unit.work_unit_id, WorkUnitStatus.ACTIVE, attempt.attempt_id))
        return self._next(states=states, attempts=(*self.attempts, attempt))

    def _start_attempt(self, command: StartAttempt) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.START_ATTEMPT, command.actor_id)
        self._require_status(ProgramStatus.ACTIVE)
        attempt = self.attempt(command.attempt_id)
        if attempt.status is not AttemptStatus.PREPARED:
            raise IllegalTransition(f"attempt {attempt.attempt_id!r} is {attempt.status.value!r}, not prepared")
        state = self.state(attempt.spec.work_unit_id)
        if state.active_attempt_id != attempt.attempt_id:
            raise IllegalTransition("attempt is not the active attempt for its WorkUnit")
        return self._next(attempts=self._replace_attempt(Attempt(attempt.spec, AttemptStatus.EXECUTING)))

    def _finish_attempt(self, command: FinishAttempt) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.FINISH_ATTEMPT, command.actor_id)
        self._require_status(ProgramStatus.ACTIVE)
        attempt = self.attempt(command.attempt_id)
        if attempt.status is not AttemptStatus.EXECUTING:
            raise IllegalTransition(f"attempt {attempt.attempt_id!r} is {attempt.status.value!r}, not executing")
        return self._next(attempts=self._replace_attempt(Attempt(attempt.spec, AttemptStatus.FINISHED)))

    def _fail_attempt(self, command: FailAttempt) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.FAIL_ATTEMPT, command.actor_id)
        self._require_status(ProgramStatus.ACTIVE, ProgramStatus.PAUSED)
        attempt = self.attempt(command.attempt_id)
        if attempt.status not in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING):
            raise IllegalTransition(f"attempt {attempt.attempt_id!r} cannot fail from {attempt.status.value!r}")
        states = self.work_unit_states
        state = self.state(attempt.spec.work_unit_id)
        if state.active_attempt_id == attempt.attempt_id:
            states = self._replace_state(WorkUnitState(state.work_unit_id, WorkUnitStatus.READY))
            states = _mark_ready(self.spec, states)
        return self._next(
            states=states,
            attempts=self._replace_attempt(Attempt(attempt.spec, AttemptStatus.FAILED)),
        )

    def _cancel_attempt(self, command: CancelAttempt) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.CANCEL_ATTEMPT, command.actor_id)
        self._require_status(ProgramStatus.ACTIVE, ProgramStatus.PAUSED)
        attempt = self.attempt(command.attempt_id)
        if attempt.status not in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING):
            raise IllegalTransition(f"attempt {attempt.attempt_id!r} cannot cancel from {attempt.status.value!r}")
        states = self.work_unit_states
        state = self.state(attempt.spec.work_unit_id)
        if state.active_attempt_id == attempt.attempt_id:
            states = self._replace_state(WorkUnitState(state.work_unit_id, WorkUnitStatus.READY))
            states = _mark_ready(self.spec, states)
        return self._next(
            states=states,
            attempts=self._replace_attempt(Attempt(attempt.spec, AttemptStatus.CANCELLED)),
        )

    def _satisfy_work_unit(self, command: SatisfyWorkUnit) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.SATISFY_WORK_UNIT, command.actor_id, trusted_satisfaction=True)
        self._require_status(ProgramStatus.ACTIVE)
        reference = command.satisfaction
        if reference.issuer_id != command.actor_id:
            raise AuthorityViolation("satisfaction issuer and command actor must match")
        if reference.program_id != self.program_id:
            raise AuthorityViolation("satisfaction is scoped to a different Program")
        if reference.spec_revision != self.spec.revision or reference.spec_digest != self.spec.digest:
            raise StaleRevision("satisfaction must reference the current ProgramSpec")
        unit = self.spec.work_unit(reference.work_unit_id)
        if reference.work_unit_digest != unit.digest:
            raise StaleRevision("satisfaction must reference the current WorkUnit definition")
        state = self.state(unit.work_unit_id)
        if state.status is not WorkUnitStatus.ACTIVE:
            raise IllegalTransition(f"WorkUnit {unit.work_unit_id!r} is {state.status.value!r}, not active")
        if any(item.reference_id == reference.reference_id for item in self.satisfactions):
            raise IllegalTransition(f"satisfaction reference {reference.reference_id!r} was already applied")
        attempts = self.attempts
        if reference.source_attempt_id is not None:
            source = self.attempt(reference.source_attempt_id)
            if source.spec.work_unit_id != unit.work_unit_id:
                raise AuthorityViolation("satisfaction source attempt targets a different WorkUnit")
            if source.spec.spec_digest != self.spec.digest or source.spec.spec_revision != self.spec.revision:
                raise StaleRevision("satisfaction source attempt is pinned to an older ProgramSpec")
            if source.status not in (AttemptStatus.EXECUTING, AttemptStatus.FINISHED):
                raise IllegalTransition("satisfaction source attempt is not executable or finished")
            attempts = self._replace_attempt(Attempt(source.spec, AttemptStatus.FINISHED))
        elif state.active_attempt_id is not None:
            active_attempt = self.attempt(state.active_attempt_id)
            if active_attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING):
                attempts = self._replace_attempt(Attempt(active_attempt.spec, AttemptStatus.FINISHED))
        states = self._replace_state(WorkUnitState(unit.work_unit_id, WorkUnitStatus.SATISFIED))
        states = _mark_ready(self.spec, states)
        status = (
            ProgramStatus.COMPLETED
            if all(item.status is WorkUnitStatus.SATISFIED for item in states)
            else ProgramStatus.ACTIVE
        )
        return self._next(
            status=status,
            states=states,
            attempts=attempts,
            satisfactions=(*self.satisfactions, reference),
        )

    def _amend_spec(self, command: AmendProgramSpec) -> Program:
        self._check_revision(command.expected_revision)
        self._authorize(DomainAction.AMEND_SPEC, command.actor_id)
        self._require_status(
            ProgramStatus.DRAFT,
            ProgramStatus.ACTIVE,
            ProgramStatus.PAUSED,
            ProgramStatus.COMPLETED,
        )
        new_spec = self.spec.amend(command.amendment)
        affected = _amendment_affected_work_units(self.spec, new_spec)
        old_states = {state.work_unit_id: state for state in self.work_unit_states}
        old_units = {unit.work_unit_id: unit for unit in self.spec.work_units}
        new_states: list[WorkUnitState] = []
        for unit in new_spec.work_units:
            prior = old_states.get(unit.work_unit_id)
            preserved = (
                prior is not None
                and prior.status is WorkUnitStatus.SATISFIED
                and unit.work_unit_id not in affected
                and old_units.get(unit.work_unit_id) == unit
                and any(
                    item.work_unit_id == unit.work_unit_id and item.work_unit_digest == unit.digest
                    for item in self.satisfactions
                )
            )
            new_states.append(
                WorkUnitState(
                    unit.work_unit_id,
                    WorkUnitStatus.SATISFIED if preserved else WorkUnitStatus.PENDING,
                )
            )
        states = tuple(new_states)
        status = self.status
        if self.status in (ProgramStatus.ACTIVE, ProgramStatus.COMPLETED):
            states = _mark_ready(new_spec, states)
            status = (
                ProgramStatus.COMPLETED
                if all(item.status is WorkUnitStatus.SATISFIED for item in states)
                else ProgramStatus.ACTIVE
            )
        attempts = tuple(
            Attempt(attempt.spec, AttemptStatus.CANCELLED)
            if attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
            else attempt
            for attempt in self.attempts
        )
        return self._next(
            status=status,
            spec=new_spec,
            states=states,
            attempts=attempts,
            spec_history=(*self.spec_history, new_spec),
        )


__all__ = [
    "ActivateProgram",
    "AmendProgramSpec",
    "Attempt",
    "AttemptSpec",
    "AttemptStatus",
    "AuthorityEnvelope",
    "AuthorityViolation",
    "BudgetExceeded",
    "BudgetPolicy",
    "CancelAttempt",
    "CancelProgram",
    "CancelWorkUnit",
    "CycleDetected",
    "DomainAction",
    "DomainCommand",
    "DomainCommandType",
    "DomainError",
    "DuplicateAttempt",
    "FailAttempt",
    "FinishAttempt",
    "IllegalTransition",
    "InputBinding",
    "InvalidDomainValue",
    "InvalidGraph",
    "MissingInput",
    "MissingReference",
    "PauseProgram",
    "PrepareAttempt",
    "Program",
    "ProgramSpec",
    "ProgramStatus",
    "ResumeProgram",
    "SatisfyWorkUnit",
    "SpecAmendment",
    "StaleRevision",
    "StartAttempt",
    "TrustedSatisfaction",
    "WorkUnit",
    "WorkUnitState",
    "WorkUnitStatus",
]
