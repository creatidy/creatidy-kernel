# SPDX-License-Identifier: Apache-2.0
"""Pure K1 domain values and deterministic Program transitions.

This module deliberately stops at intent, admission and lifecycle state.  A
trusted prevalidated satisfaction reference is an input to the domain; the
producer of that reference belongs to a later verification slice.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dataclass_field
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


def _begin_init(instance: object, expected_type: type[object], first_field: str) -> None:
    if type(instance) is not expected_type:
        raise InvalidDomainValue(f"{expected_type.__name__} values cannot be subclassed")
    if hasattr(instance, first_field):
        raise InvalidDomainValue(f"{expected_type.__name__} values cannot be reinitialized")


def _set_fields(instance: object, **values: object) -> None:
    for name, value in values.items():
        object.__setattr__(instance, name, value)


def _reject_copy(instance: object) -> None:
    raise InvalidDomainValue(f"{type(instance).__name__} values cannot be copied")


def _reject_deepcopy(instance: object, memo: dict[int, object]) -> None:
    del memo
    _reject_copy(instance)


def _seal_type(domain_type: type[object]) -> None:
    def reject_subclass(subclass: type[object], **kwargs: object) -> None:
        del subclass, kwargs
        raise TypeError(f"{domain_type.__name__} does not support subclassing")

    type.__setattr__(domain_type, "__init_subclass__", classmethod(reject_subclass))
    type.__setattr__(domain_type, "__copy__", _reject_copy)
    type.__setattr__(domain_type, "__deepcopy__", _reject_deepcopy)


@dataclass(frozen=True, slots=True, init=False)
class BudgetPolicy:
    """Finite admission limits; no runtime consumption or provider accounting."""

    max_attempts: int = dataclass_field(default=3, init=False)
    max_active_attempts: int = dataclass_field(default=1, init=False)

    def __init__(self, max_attempts: int = 3, max_active_attempts: int = 1) -> None:
        _begin_init(self, BudgetPolicy, "max_attempts")
        _set_fields(self, max_attempts=max_attempts, max_active_attempts=max_active_attempts)
        self.__post_init__()

    def __post_init__(self) -> None:
        _revision(self.max_attempts, "max_attempts", allow_zero=True)
        _revision(self.max_active_attempts, "max_active_attempts", allow_zero=True)


@dataclass(frozen=True, slots=True, init=False)
class AuthorityEnvelope:
    """Owner-approved command scope and finite attempt ceiling."""

    owner_id: str = dataclass_field(init=False)
    allowed_actions: frozenset[DomainAction] = dataclass_field(default=frozenset(DomainAction), init=False)
    max_attempts: int = dataclass_field(default=3, init=False)
    trusted_satisfaction_issuers: frozenset[str] = dataclass_field(default=frozenset(), init=False)

    def __init__(
        self,
        owner_id: str,
        allowed_actions: frozenset[DomainAction] = frozenset(DomainAction),
        max_attempts: int = 3,
        trusted_satisfaction_issuers: frozenset[str] = frozenset(),
    ) -> None:
        _begin_init(self, AuthorityEnvelope, "owner_id")
        _set_fields(
            self,
            owner_id=owner_id,
            allowed_actions=allowed_actions,
            max_attempts=max_attempts,
            trusted_satisfaction_issuers=trusted_satisfaction_issuers,
        )
        self.__post_init__()

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


_DEFAULT_BUDGET = BudgetPolicy()
_DEFAULT_AUTHORITY = AuthorityEnvelope("owner")


@dataclass(frozen=True, slots=True, init=False)
class WorkUnit:
    """One bounded graph node with logical inputs and outputs."""

    work_unit_id: str = dataclass_field(init=False)
    dependencies: tuple[str, ...] = dataclass_field(default=(), init=False)
    required_inputs: frozenset[str] = dataclass_field(default=frozenset(), init=False)
    outputs: frozenset[str] = dataclass_field(default=frozenset(), init=False)

    def __init__(
        self,
        work_unit_id: str,
        dependencies: tuple[str, ...] = (),
        required_inputs: frozenset[str] = frozenset(),
        outputs: frozenset[str] = frozenset(),
    ) -> None:
        _begin_init(self, WorkUnit, "work_unit_id")
        _set_fields(
            self,
            work_unit_id=work_unit_id,
            dependencies=dependencies,
            required_inputs=required_inputs,
            outputs=outputs,
        )
        self.__post_init__()

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


@dataclass(frozen=True, slots=True, init=False)
class ProgramSpec:
    """Immutable owner intent and a validated finite WorkUnit graph."""

    program_id: str = dataclass_field(init=False)
    objective: str = dataclass_field(init=False)
    work_units: tuple[WorkUnit, ...] = dataclass_field(init=False)
    initial_inputs: frozenset[str] = dataclass_field(default=frozenset(), init=False)
    budget: BudgetPolicy = dataclass_field(default=_DEFAULT_BUDGET, init=False)
    authority: AuthorityEnvelope = dataclass_field(default=_DEFAULT_AUTHORITY, init=False)
    revision: int = dataclass_field(default=1, init=False)
    parent_digest: str | None = dataclass_field(default=None, init=False)

    def __init__(
        self,
        program_id: str,
        objective: str,
        work_units: tuple[WorkUnit, ...],
        initial_inputs: frozenset[str] = frozenset(),
        budget: BudgetPolicy = _DEFAULT_BUDGET,
        authority: AuthorityEnvelope = _DEFAULT_AUTHORITY,
        revision: int = 1,
        parent_digest: str | None = None,
    ) -> None:
        _begin_init(self, ProgramSpec, "program_id")
        _set_fields(
            self,
            program_id=program_id,
            objective=objective,
            work_units=work_units,
            initial_inputs=initial_inputs,
            budget=budget,
            authority=authority,
            revision=revision,
            parent_digest=parent_digest,
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        _nonempty(self.program_id, "program_id")
        _nonempty(self.objective, "objective")
        work_units = _immutable_tuple(self.work_units, "work_units")
        if not work_units or any(type(unit) is not WorkUnit for unit in work_units):
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


@dataclass(frozen=True, slots=True, init=False)
class InputBinding:
    """An immutable logical input reference pinned into one AttemptSpec."""

    name: str = dataclass_field(init=False)
    reference: str = dataclass_field(init=False)

    def __init__(self, name: str, reference: str) -> None:
        _begin_init(self, InputBinding, "name")
        _set_fields(self, name=name, reference=reference)
        self.__post_init__()

    def __post_init__(self) -> None:
        _nonempty(self.name, "input name")
        _nonempty(self.reference, "input reference")


@dataclass(frozen=True, slots=True, init=False)
class AttemptSpec:
    """Immutable effective inputs for one execution attempt."""

    attempt_id: str = dataclass_field(init=False)
    program_id: str = dataclass_field(init=False)
    work_unit_id: str = dataclass_field(init=False)
    spec_revision: int = dataclass_field(init=False)
    spec_digest: str = dataclass_field(init=False)
    effective_inputs: tuple[InputBinding, ...] = dataclass_field(default=(), init=False)
    allocation_reference: str | None = dataclass_field(default=None, init=False)
    agent_definition_reference: str | None = dataclass_field(default=None, init=False)
    workspace_reference: str | None = dataclass_field(default=None, init=False)
    context_reference: str | None = dataclass_field(default=None, init=False)

    def __init__(
        self,
        attempt_id: str,
        program_id: str,
        work_unit_id: str,
        spec_revision: int,
        spec_digest: str,
        effective_inputs: tuple[InputBinding, ...] = (),
        allocation_reference: str | None = None,
        agent_definition_reference: str | None = None,
        workspace_reference: str | None = None,
        context_reference: str | None = None,
    ) -> None:
        _begin_init(self, AttemptSpec, "attempt_id")
        _set_fields(
            self,
            attempt_id=attempt_id,
            program_id=program_id,
            work_unit_id=work_unit_id,
            spec_revision=spec_revision,
            spec_digest=spec_digest,
            effective_inputs=effective_inputs,
            allocation_reference=allocation_reference,
            agent_definition_reference=agent_definition_reference,
            workspace_reference=workspace_reference,
            context_reference=context_reference,
        )
        self.__post_init__()

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
        if any(type(item) is not InputBinding for item in values):
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


@dataclass(frozen=True, slots=True, init=False)
class Attempt:
    """Attempt record whose effective specification is never rewritten."""

    spec: AttemptSpec = dataclass_field(init=False)
    status: AttemptStatus = dataclass_field(default=AttemptStatus.PREPARED, init=False)

    def __init__(self, spec: AttemptSpec, status: AttemptStatus = AttemptStatus.PREPARED) -> None:
        _begin_init(self, Attempt, "spec")
        _set_fields(self, spec=spec, status=status)
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(cast(object, self.spec)) is not AttemptSpec:
            raise InvalidDomainValue("spec must be an AttemptSpec")
        if type(cast(object, self.status)) is not AttemptStatus:
            raise InvalidDomainValue("status must be an AttemptStatus")

    @property
    def attempt_id(self) -> str:
        return self.spec.attempt_id


@dataclass(frozen=True, slots=True, init=False)
class TrustedSatisfaction:
    """A typed, prevalidated reference used to advance one WorkUnit."""

    reference_id: str = dataclass_field(init=False)
    program_id: str = dataclass_field(init=False)
    work_unit_id: str = dataclass_field(init=False)
    spec_revision: int = dataclass_field(init=False)
    spec_digest: str = dataclass_field(init=False)
    work_unit_digest: str = dataclass_field(init=False)
    issuer_id: str = dataclass_field(init=False)
    source_attempt_id: str | None = dataclass_field(default=None, init=False)

    def __init__(
        self,
        reference_id: str,
        program_id: str,
        work_unit_id: str,
        spec_revision: int,
        spec_digest: str,
        work_unit_digest: str,
        issuer_id: str,
        source_attempt_id: str | None = None,
    ) -> None:
        _begin_init(self, TrustedSatisfaction, "reference_id")
        _set_fields(
            self,
            reference_id=reference_id,
            program_id=program_id,
            work_unit_id=work_unit_id,
            spec_revision=spec_revision,
            spec_digest=spec_digest,
            work_unit_digest=work_unit_digest,
            issuer_id=issuer_id,
            source_attempt_id=source_attempt_id,
        )
        self.__post_init__()

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


@dataclass(frozen=True, slots=True, init=False)
class WorkUnitState:
    """Current projection for a WorkUnit within one immutable Program value."""

    work_unit_id: str = dataclass_field(init=False)
    status: WorkUnitStatus = dataclass_field(default=WorkUnitStatus.PENDING, init=False)
    active_attempt_id: str | None = dataclass_field(default=None, init=False)

    def __init__(
        self,
        work_unit_id: str,
        status: WorkUnitStatus = WorkUnitStatus.PENDING,
        active_attempt_id: str | None = None,
    ) -> None:
        _begin_init(self, WorkUnitState, "work_unit_id")
        _set_fields(
            self,
            work_unit_id=work_unit_id,
            status=status,
            active_attempt_id=active_attempt_id,
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        _nonempty(self.work_unit_id, "work_unit_id")
        if type(cast(object, self.status)) is not WorkUnitStatus:
            raise InvalidDomainValue("status must be a WorkUnitStatus")
        _optional_nonempty(self.active_attempt_id, "active_attempt_id")
        if self.status is not WorkUnitStatus.ACTIVE and self.active_attempt_id is not None:
            raise InvalidDomainValue("only an active WorkUnit may have an active attempt")


@dataclass(frozen=True, slots=True, init=False)
class SpecAmendment:
    """Owner-authored replacement fields for one new ProgramSpec revision."""

    expected_revision: int = dataclass_field(init=False)
    objective: str | None = dataclass_field(default=None, init=False)
    work_units: tuple[WorkUnit, ...] | None = dataclass_field(default=None, init=False)
    initial_inputs: frozenset[str] | None = dataclass_field(default=None, init=False)
    budget: BudgetPolicy | None = dataclass_field(default=None, init=False)
    authority: AuthorityEnvelope | None = dataclass_field(default=None, init=False)
    reason: str = dataclass_field(default="owner amendment", init=False)

    def __init__(
        self,
        expected_revision: int,
        objective: str | None = None,
        work_units: tuple[WorkUnit, ...] | None = None,
        initial_inputs: frozenset[str] | None = None,
        budget: BudgetPolicy | None = None,
        authority: AuthorityEnvelope | None = None,
        reason: str = "owner amendment",
    ) -> None:
        _begin_init(self, SpecAmendment, "expected_revision")
        _set_fields(
            self,
            expected_revision=expected_revision,
            objective=objective,
            work_units=work_units,
            initial_inputs=initial_inputs,
            budget=budget,
            authority=authority,
            reason=reason,
        )
        self.__post_init__()

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
            if not values or any(type(item) is not WorkUnit for item in values):
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
class _ReachAttempt:
    attempt_id: str
    work_unit_id: str
    status: AttemptStatus
    current_spec: bool


@dataclass(frozen=True, slots=True)
class _ReachState:
    status: ProgramStatus
    work_unit_states: tuple[WorkUnitState, ...]
    attempts: tuple[_ReachAttempt, ...]
    satisfaction_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _ReachAction:
    action: DomainAction
    work_unit_id: str | None = None
    attempt_id: str | None = None
    attempt_spec: AttemptSpec | None = None
    satisfaction: TrustedSatisfaction | None = None
    actor_id: str | None = None


def _reach_state(
    status: ProgramStatus,
    spec: ProgramSpec,
    states: tuple[WorkUnitState, ...],
    attempts: tuple[Attempt, ...],
    satisfactions: tuple[TrustedSatisfaction, ...] = (),
) -> _ReachState:
    return _ReachState(
        status,
        states,
        tuple(
            _ReachAttempt(
                attempt.attempt_id,
                attempt.spec.work_unit_id,
                attempt.status,
                attempt.spec.spec_revision == spec.revision and attempt.spec.spec_digest == spec.digest,
            )
            for attempt in attempts
        ),
        frozenset(item.reference_id for item in satisfactions),
    )


def _unit_can_be_cancelled(spec: ProgramSpec, states: tuple[WorkUnitState, ...], work_unit_id: str) -> bool:
    by_id = {state.work_unit_id: state for state in states}
    current = by_id[work_unit_id]
    if current.status in (WorkUnitStatus.SATISFIED, WorkUnitStatus.CANCELLED) or current.active_attempt_id is not None:
        return False
    descendants = _descendants(spec, {work_unit_id})
    return not any(by_id[descendant].status is WorkUnitStatus.SATISFIED for descendant in descendants)


def _logical_inputs_available(spec: ProgramSpec, states: tuple[WorkUnitState, ...], work_unit_id: str) -> bool:
    """Check declared producers from state; do not invent concrete input references."""

    unit = spec.work_unit(work_unit_id)
    by_id = {state.work_unit_id: state for state in states}
    available = set(spec.initial_inputs)
    for dependency in unit.dependencies:
        if by_id[dependency].status is not WorkUnitStatus.SATISFIED:
            return False
        available.update(spec.work_unit(dependency).outputs)
    return unit.required_inputs <= available


def _action_inapplicability(
    spec: ProgramSpec,
    state: _ReachState,
    action: _ReachAction,
) -> DomainError | None:
    if action.action not in spec.authority.allowed_actions:
        return AuthorityViolation(f"action {action.action.value!r} is outside the authority envelope")
    status = state.status
    if action.action is DomainAction.ACTIVATE_PROGRAM:
        return None if status is ProgramStatus.DRAFT else IllegalTransition("Program is not a draft")
    if action.action is DomainAction.PAUSE_PROGRAM:
        return None if status is ProgramStatus.ACTIVE else IllegalTransition("Program is not active")
    if action.action is DomainAction.RESUME_PROGRAM:
        return None if status is ProgramStatus.PAUSED else IllegalTransition("Program is not paused")
    if action.action is DomainAction.CANCEL_PROGRAM:
        return (
            None
            if status in (ProgramStatus.DRAFT, ProgramStatus.ACTIVE, ProgramStatus.PAUSED)
            else IllegalTransition("Program cannot be cancelled from its current status")
        )
    if action.action is DomainAction.AMEND_SPEC:
        return (
            None
            if status
            in (
                ProgramStatus.DRAFT,
                ProgramStatus.ACTIVE,
                ProgramStatus.PAUSED,
                ProgramStatus.COMPLETED,
            )
            else IllegalTransition("Program cannot be amended from its current status")
        )
    attempt_controls = (DomainAction.FAIL_ATTEMPT, DomainAction.CANCEL_ATTEMPT)
    if action.action in attempt_controls:
        if status not in (ProgramStatus.ACTIVE, ProgramStatus.PAUSED) or action.work_unit_id is None:
            return IllegalTransition("attempt cannot be failed or cancelled in the current state")
    elif status is not ProgramStatus.ACTIVE or action.work_unit_id is None:
        return IllegalTransition("WorkUnit action is not available in the current Program state")
    try:
        unit = spec.work_unit(action.work_unit_id)
    except MissingReference as error:
        return error
    work_state = next(item for item in state.work_unit_states if item.work_unit_id == unit.work_unit_id)
    if action.action is DomainAction.CANCEL_WORK_UNIT:
        return (
            None
            if _unit_can_be_cancelled(spec, state.work_unit_states, unit.work_unit_id)
            else IllegalTransition(f"WorkUnit {unit.work_unit_id!r} cannot be cancelled")
        )
    if action.action is DomainAction.PREPARE_ATTEMPT:
        attempt_spec = action.attempt_spec
        if attempt_spec is not None:
            if action.attempt_id is not None and any(item.attempt_id == action.attempt_id for item in state.attempts):
                return DuplicateAttempt(f"attempt {action.attempt_id!r} already exists")
            if attempt_spec.program_id != spec.program_id:
                return AuthorityViolation("attempt is scoped to a different Program")
            if attempt_spec.spec_revision != spec.revision or attempt_spec.spec_digest != spec.digest:
                return StaleRevision("attempt inputs must pin the current ProgramSpec revision and digest")
            try:
                attempt_unit = spec.work_unit(attempt_spec.work_unit_id)
            except MissingReference as error:
                return error
            if attempt_spec.input_names != attempt_unit.required_inputs:
                missing = sorted(attempt_unit.required_inputs - attempt_spec.input_names)
                extra = sorted(attempt_spec.input_names - attempt_unit.input_names)
                details: list[str] = []
                if missing:
                    details.append(f"missing {', '.join(missing)}")
                if extra:
                    details.append(f"unexpected {', '.join(extra)}")
                return MissingInput(f"attempt inputs for {attempt_unit.work_unit_id!r}: {'; '.join(details)}")
        if work_state.status is not WorkUnitStatus.READY:
            return IllegalTransition(f"WorkUnit {unit.work_unit_id!r} is not ready")
        if not _logical_inputs_available(spec, state.work_unit_states, unit.work_unit_id):
            return MissingInput(f"WorkUnit {unit.work_unit_id!r} has unavailable logical inputs")
        if (
            attempt_spec is None
            and action.attempt_id is not None
            and any(item.attempt_id == action.attempt_id for item in state.attempts)
        ):
            return DuplicateAttempt(f"attempt {action.attempt_id!r} already exists")
        active_count = sum(
            1 for attempt in state.attempts if attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
        )
        if len(state.attempts) >= min(spec.budget.max_attempts, spec.authority.max_attempts):
            return BudgetExceeded("attempt admission limit exceeded")
        if active_count >= spec.budget.max_active_attempts:
            return BudgetExceeded("active attempt admission limit exceeded")
        return None
    if action.action is DomainAction.SATISFY_WORK_UNIT:
        reference = action.satisfaction
        if reference is not None:
            if reference.issuer_id != action.actor_id:
                return AuthorityViolation("satisfaction issuer and command actor must match")
            if reference.program_id != spec.program_id:
                return AuthorityViolation("satisfaction is scoped to a different Program")
            if reference.spec_revision != spec.revision or reference.spec_digest != spec.digest:
                return StaleRevision("satisfaction must reference the current ProgramSpec")
            if reference.work_unit_digest != unit.digest:
                return StaleRevision("satisfaction must reference the current WorkUnit definition")
            if reference.reference_id in state.satisfaction_ids:
                return IllegalTransition(f"satisfaction reference {reference.reference_id!r} was already applied")
            if reference.source_attempt_id is not None:
                source = next(
                    (item for item in state.attempts if item.attempt_id == reference.source_attempt_id),
                    None,
                )
                if source is None:
                    return MissingReference(f"unknown attempt {reference.source_attempt_id!r}")
                if source.work_unit_id != unit.work_unit_id:
                    return AuthorityViolation("satisfaction source attempt targets a different WorkUnit")
                if not source.current_spec:
                    return StaleRevision("satisfaction source attempt is pinned to an older ProgramSpec")
                if source.status not in (AttemptStatus.EXECUTING, AttemptStatus.FINISHED):
                    return IllegalTransition("satisfaction source attempt is not executable or finished")
                if work_state.status is WorkUnitStatus.READY and source.status is not AttemptStatus.FINISHED:
                    return IllegalTransition("a ready WorkUnit requires a finished satisfaction source")
                if work_state.status is WorkUnitStatus.ACTIVE and work_state.active_attempt_id != source.attempt_id:
                    return IllegalTransition("satisfaction source is not the active attempt for its WorkUnit")
        if work_state.status is WorkUnitStatus.READY:
            available = reference is None and any(
                attempt.work_unit_id == unit.work_unit_id
                and attempt.current_spec
                and attempt.status is AttemptStatus.FINISHED
                for attempt in state.attempts
            )
            if reference is not None:
                available = reference.source_attempt_id is not None
            return None if available else IllegalTransition("a ready WorkUnit needs a finished current-spec Attempt")
        if work_state.status is WorkUnitStatus.ACTIVE and work_state.active_attempt_id is not None:
            active_attempt = next(
                (item for item in state.attempts if item.attempt_id == work_state.active_attempt_id),
                None,
            )
            available = active_attempt is not None and active_attempt.status in (
                AttemptStatus.PREPARED,
                AttemptStatus.EXECUTING,
            )
            return None if available else IllegalTransition("active WorkUnit has no satisfiable Attempt")
        return IllegalTransition("WorkUnit is not in a satisfiable state")
    if action.action in (
        DomainAction.START_ATTEMPT,
        DomainAction.FINISH_ATTEMPT,
        DomainAction.FAIL_ATTEMPT,
        DomainAction.CANCEL_ATTEMPT,
    ):
        if work_state.status is not WorkUnitStatus.ACTIVE or work_state.active_attempt_id is None:
            return IllegalTransition("WorkUnit has no active Attempt")
        active_attempt = next(
            (item for item in state.attempts if item.attempt_id == work_state.active_attempt_id),
            None,
        )
        if active_attempt is None:
            return MissingReference(f"active Attempt {work_state.active_attempt_id!r} is missing")
        if action.attempt_id is not None and action.attempt_id != active_attempt.attempt_id:
            return IllegalTransition("command Attempt is not the active Attempt for its WorkUnit")
        if action.action is DomainAction.START_ATTEMPT:
            return (
                None
                if active_attempt.status is AttemptStatus.PREPARED
                else IllegalTransition("only a prepared Attempt can start")
            )
        if action.action is DomainAction.FINISH_ATTEMPT:
            return (
                None
                if status is ProgramStatus.ACTIVE and active_attempt.status is AttemptStatus.EXECUTING
                else IllegalTransition("only an executing Attempt in an active Program can finish")
            )
        return (
            None
            if active_attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
            else IllegalTransition("only a prepared or executing Attempt can fail or cancel")
        )
    return IllegalTransition(f"unsupported domain action: {action.action.value}")


def _action_applicable(spec: ProgramSpec, state: _ReachState, action: _ReachAction) -> bool:
    return _action_inapplicability(spec, state, action) is None


def _replace_reach_attempt(
    attempts: tuple[_ReachAttempt, ...],
    attempt_id: str,
    status: AttemptStatus,
) -> tuple[_ReachAttempt, ...]:
    return tuple(
        _ReachAttempt(item.attempt_id, item.work_unit_id, status, item.current_spec)
        if item.attempt_id == attempt_id
        else item
        for item in attempts
    )


def _reach_successor(spec: ProgramSpec, state: _ReachState, action: _ReachAction) -> _ReachState:
    """Canonical lifecycle reducer shared by command application and reachability."""

    if action.action is DomainAction.ACTIVATE_PROGRAM:
        return _ReachState(ProgramStatus.ACTIVE, _mark_ready(spec, state.work_unit_states), state.attempts)
    if action.action is DomainAction.PAUSE_PROGRAM:
        return _ReachState(ProgramStatus.PAUSED, state.work_unit_states, state.attempts)
    if action.action is DomainAction.RESUME_PROGRAM:
        states = _mark_ready(spec, state.work_unit_states)
        return _ReachState(_status_after_work_unit_change(states), states, state.attempts)
    if action.action is DomainAction.CANCEL_PROGRAM:
        states = tuple(
            item
            if item.status is WorkUnitStatus.SATISFIED
            else WorkUnitState(item.work_unit_id, WorkUnitStatus.CANCELLED)
            for item in state.work_unit_states
        )
        attempts = tuple(
            _ReachAttempt(item.attempt_id, item.work_unit_id, AttemptStatus.CANCELLED, item.current_spec)
            if item.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
            else item
            for item in state.attempts
        )
        return _ReachState(ProgramStatus.CANCELLED, states, attempts)

    if action.work_unit_id is None:
        raise InvalidDomainValue("a WorkUnit action requires a target identity")
    work_unit_id = action.work_unit_id
    current = next(item for item in state.work_unit_states if item.work_unit_id == work_unit_id)
    if action.action is DomainAction.CANCEL_WORK_UNIT:
        descendants = _descendants(spec, {work_unit_id})
        states = tuple(
            WorkUnitState(item.work_unit_id, WorkUnitStatus.CANCELLED) if item.work_unit_id in descendants else item
            for item in state.work_unit_states
        )
        attempts = tuple(
            _ReachAttempt(item.attempt_id, item.work_unit_id, AttemptStatus.CANCELLED, item.current_spec)
            if item.work_unit_id in descendants and item.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
            else item
            for item in state.attempts
        )
        return _ReachState(_status_after_work_unit_change(states), states, attempts)
    if action.action is DomainAction.PREPARE_ATTEMPT:
        # Symbolic reachability carries an identity/status summary only, never an AttemptSpec.
        attempt_id = action.attempt_id
        if attempt_id is None:
            attempt_id = f"__reachability_attempt_{len(state.attempts)}"
            while any(item.attempt_id == attempt_id for item in state.attempts):
                attempt_id = f"_{attempt_id}"
        states = tuple(
            WorkUnitState(work_unit_id, WorkUnitStatus.ACTIVE, attempt_id)
            if item.work_unit_id == work_unit_id
            else item
            for item in state.work_unit_states
        )
        attempts = (*state.attempts, _ReachAttempt(attempt_id, work_unit_id, AttemptStatus.PREPARED, True))
        return _ReachState(state.status, states, attempts)
    if action.action is DomainAction.SATISFY_WORK_UNIT:
        attempts = state.attempts
        if current.active_attempt_id is not None:
            attempts = _replace_reach_attempt(attempts, current.active_attempt_id, AttemptStatus.FINISHED)
        states = _mark_ready(
            spec,
            tuple(
                WorkUnitState(item.work_unit_id, WorkUnitStatus.SATISFIED)
                if item.work_unit_id == work_unit_id
                else item
                for item in state.work_unit_states
            ),
        )
        return _ReachState(_status_after_work_unit_change(states), states, attempts)
    if action.action in (
        DomainAction.START_ATTEMPT,
        DomainAction.FINISH_ATTEMPT,
        DomainAction.FAIL_ATTEMPT,
        DomainAction.CANCEL_ATTEMPT,
    ):
        if current.active_attempt_id is None:
            raise IllegalTransition("attempt action requires an active Attempt")
        target_attempt = next(item for item in state.attempts if item.attempt_id == current.active_attempt_id)
        if action.action is DomainAction.START_ATTEMPT:
            attempts = _replace_reach_attempt(state.attempts, target_attempt.attempt_id, AttemptStatus.EXECUTING)
            return _ReachState(state.status, state.work_unit_states, attempts)
        terminal_attempt_status = {
            DomainAction.FINISH_ATTEMPT: AttemptStatus.FINISHED,
            DomainAction.FAIL_ATTEMPT: AttemptStatus.FAILED,
            DomainAction.CANCEL_ATTEMPT: AttemptStatus.CANCELLED,
        }[action.action]
        attempts = _replace_reach_attempt(state.attempts, target_attempt.attempt_id, terminal_attempt_status)
        states = _mark_ready(
            spec,
            tuple(
                WorkUnitState(work_unit_id, WorkUnitStatus.READY) if item.work_unit_id == work_unit_id else item
                for item in state.work_unit_states
            ),
        )
        return _ReachState(state.status, states, attempts)
    raise InvalidDomainValue(f"unsupported reachability action: {action.action.value}")


def _reach_actions(spec: ProgramSpec, state: _ReachState) -> tuple[_ReachAction, ...]:
    actions = [
        _ReachAction(action)
        for action in (
            DomainAction.ACTIVATE_PROGRAM,
            DomainAction.PAUSE_PROGRAM,
            DomainAction.RESUME_PROGRAM,
            DomainAction.CANCEL_PROGRAM,
            DomainAction.AMEND_SPEC,
        )
    ]
    for unit in spec.work_units:
        actions.extend(
            _ReachAction(action, unit.work_unit_id)
            for action in (
                DomainAction.CANCEL_WORK_UNIT,
                DomainAction.PREPARE_ATTEMPT,
                DomainAction.SATISFY_WORK_UNIT,
                DomainAction.START_ATTEMPT,
                DomainAction.FINISH_ATTEMPT,
                DomainAction.FAIL_ATTEMPT,
                DomainAction.CANCEL_ATTEMPT,
            )
        )
    return tuple(actions)


def _reach_key(state: _ReachState) -> tuple[object, ...]:
    active_attempts = tuple(
        sorted(
            (attempt.work_unit_id, attempt.status.value)
            for attempt in state.attempts
            if attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
        )
    )
    finished_sources = tuple(
        sorted(
            {
                attempt.work_unit_id
                for attempt in state.attempts
                if attempt.current_spec and attempt.status is AttemptStatus.FINISHED
            }
        )
    )
    return state.status, state.work_unit_states, active_attempts, len(state.attempts), finished_sources


def _has_legal_path(
    status: ProgramStatus,
    spec: ProgramSpec,
    states: tuple[WorkUnitState, ...],
    attempts: tuple[Attempt, ...],
    satisfactions: tuple[TrustedSatisfaction, ...] = (),
) -> bool:
    """Search finite K1 states via shared command applicability and transition reduction.

    WorkUnits and status enums are finite; PrepareAttempt is bounded by the declared admission
    budgets. Reachability uses symbolic Attempt summaries and never constructs external input or
    trusted-satisfaction values.
    """

    initial = _reach_state(status, spec, states, attempts, satisfactions)
    pending = deque([initial])
    visited = {_reach_key(initial)}
    while pending:
        current = pending.popleft()
        if current.status in (ProgramStatus.COMPLETED, ProgramStatus.CANCELLED):
            return True
        for action in _reach_actions(spec, current):
            if not _action_applicable(spec, current, action):
                continue
            if action.action is DomainAction.AMEND_SPEC:
                return True
            successor = _reach_successor(spec, current, action)
            if successor.status in (ProgramStatus.COMPLETED, ProgramStatus.CANCELLED):
                return True
            successor_key = _reach_key(successor)
            if successor_key not in visited:
                visited.add(successor_key)
                pending.append(successor)
    return False


def _require_legal_path(
    status: ProgramStatus,
    spec: ProgramSpec,
    states: tuple[WorkUnitState, ...],
    attempts: tuple[Attempt, ...],
    satisfactions: tuple[TrustedSatisfaction, ...],
) -> None:
    if status in (ProgramStatus.DRAFT, ProgramStatus.ACTIVE, ProgramStatus.PAUSED) and not _has_legal_path(
        status, spec, states, attempts, satisfactions
    ):
        raise AuthorityViolation(f"{status.value} Program state has no legal progress or control path")


def _status_after_work_unit_change(states: tuple[WorkUnitState, ...]) -> ProgramStatus:
    if all(state.status is WorkUnitStatus.SATISFIED for state in states):
        return ProgramStatus.COMPLETED
    if all(state.status in (WorkUnitStatus.SATISFIED, WorkUnitStatus.CANCELLED) for state in states):
        return ProgramStatus.CANCELLED
    return ProgramStatus.ACTIVE


@dataclass(frozen=True, slots=True, init=False)
class Program:
    """Immutable aggregate enforcing legal Program and WorkUnit transitions."""

    spec: ProgramSpec = dataclass_field(init=False)
    status: ProgramStatus = dataclass_field(default=ProgramStatus.DRAFT, init=False)
    revision: int = dataclass_field(default=0, init=False)
    work_unit_states: tuple[WorkUnitState, ...] = dataclass_field(default=(), init=False)
    attempts: tuple[Attempt, ...] = dataclass_field(default=(), init=False)
    satisfactions: tuple[TrustedSatisfaction, ...] = dataclass_field(default=(), init=False)
    spec_history: tuple[ProgramSpec, ...] = dataclass_field(default=(), init=False)

    def __init__(
        self,
        spec: ProgramSpec,
        status: ProgramStatus = ProgramStatus.DRAFT,
        revision: int = 0,
        work_unit_states: tuple[WorkUnitState, ...] = (),
        attempts: tuple[Attempt, ...] = (),
        satisfactions: tuple[TrustedSatisfaction, ...] = (),
        spec_history: tuple[ProgramSpec, ...] = (),
    ) -> None:
        _begin_init(self, Program, "spec")
        _set_fields(
            self,
            spec=spec,
            status=status,
            revision=revision,
            work_unit_states=work_unit_states,
            attempts=attempts,
            satisfactions=satisfactions,
            spec_history=spec_history,
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        self._validate(allow_transition=False)

    def _validate(self, *, allow_transition: bool) -> None:
        if type(self) is not Program:
            raise InvalidDomainValue("Program values cannot be subclassed")
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
        if self.status is ProgramStatus.CANCELLED and any(
            state.status not in (WorkUnitStatus.SATISFIED, WorkUnitStatus.CANCELLED) for state in states
        ):
            raise InvalidDomainValue("a cancelled Program must have no remaining WorkUnits")
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
        _require_legal_path(self.status, self.spec, states, self.attempts, self.satisfactions)

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
        if type(self) is not Program:
            raise InvalidDomainValue("Program values cannot be subclassed")

        if type(command) is ActivateProgram:
            action = DomainAction.ACTIVATE_PROGRAM
        elif type(command) is PauseProgram:
            action = DomainAction.PAUSE_PROGRAM
        elif type(command) is ResumeProgram:
            action = DomainAction.RESUME_PROGRAM
        elif type(command) is CancelProgram:
            action = DomainAction.CANCEL_PROGRAM
        elif type(command) is CancelWorkUnit:
            action = DomainAction.CANCEL_WORK_UNIT
        elif type(command) is PrepareAttempt:
            action = DomainAction.PREPARE_ATTEMPT
        elif type(command) is StartAttempt:
            action = DomainAction.START_ATTEMPT
        elif type(command) is FinishAttempt:
            action = DomainAction.FINISH_ATTEMPT
        elif type(command) is FailAttempt:
            action = DomainAction.FAIL_ATTEMPT
        elif type(command) is CancelAttempt:
            action = DomainAction.CANCEL_ATTEMPT
        elif type(command) is SatisfyWorkUnit:
            action = DomainAction.SATISFY_WORK_UNIT
        elif type(command) is AmendProgramSpec:
            action = DomainAction.AMEND_SPEC
        else:
            raise InvalidDomainValue(f"unsupported domain command: {type(command).__name__}")

        self._check_revision(command.expected_revision)
        self._authorize(action, command.actor_id, trusted_satisfaction=action is DomainAction.SATISFY_WORK_UNIT)
        target_work_unit_id: str | None = None
        attempt_id: str | None = None
        if type(command) is CancelWorkUnit:
            target_work_unit_id = command.work_unit_id
        elif type(command) is PrepareAttempt:
            target_work_unit_id = command.attempt.work_unit_id
            attempt_id = command.attempt.attempt_id
        elif type(command) is SatisfyWorkUnit:
            target_work_unit_id = command.satisfaction.work_unit_id
        elif type(command) is StartAttempt:
            attempt_id = command.attempt_id
            target_work_unit_id = self.attempt(attempt_id).spec.work_unit_id
        elif type(command) is FinishAttempt:
            attempt_id = command.attempt_id
            target_work_unit_id = self.attempt(attempt_id).spec.work_unit_id
        elif type(command) is FailAttempt:
            attempt_id = command.attempt_id
            target_work_unit_id = self.attempt(attempt_id).spec.work_unit_id
        elif type(command) is CancelAttempt:
            attempt_id = command.attempt_id
            target_work_unit_id = self.attempt(attempt_id).spec.work_unit_id
        current_reach_state = _reach_state(
            self.status,
            self.spec,
            self.work_unit_states,
            self.attempts,
            self.satisfactions,
        )
        attempt_spec = command.attempt if type(command) is PrepareAttempt else None
        satisfaction = command.satisfaction if type(command) is SatisfyWorkUnit else None
        reach_action = _ReachAction(
            action,
            target_work_unit_id,
            attempt_id,
            attempt_spec,
            satisfaction,
            command.actor_id,
        )
        inapplicability = _action_inapplicability(self.spec, current_reach_state, reach_action)
        if inapplicability is not None:
            raise inapplicability

        def assemble(
            *,
            next_spec: ProgramSpec,
            next_status: ProgramStatus,
            next_states: tuple[WorkUnitState, ...],
            next_attempts: tuple[Attempt, ...],
            next_satisfactions: tuple[TrustedSatisfaction, ...],
            next_spec_history: tuple[ProgramSpec, ...],
        ) -> Program:
            instance = object.__new__(Program)
            object.__setattr__(instance, "spec", next_spec)
            object.__setattr__(instance, "status", next_status)
            object.__setattr__(instance, "revision", self.revision + 1)
            object.__setattr__(instance, "work_unit_states", next_states)
            object.__setattr__(instance, "attempts", next_attempts)
            object.__setattr__(instance, "satisfactions", next_satisfactions)
            object.__setattr__(instance, "spec_history", next_spec_history)
            Program._validate(instance, allow_transition=True)
            return instance

        def transition(
            *,
            prepared_attempt: AttemptSpec | None = None,
            satisfaction: TrustedSatisfaction | None = None,
        ) -> Program:
            next_state = _reach_successor(self.spec, current_reach_state, reach_action)
            current_attempts = {attempt.attempt_id: attempt for attempt in self.attempts}
            next_attempts_list: list[Attempt] = []
            for item in next_state.attempts:
                previous = current_attempts.get(item.attempt_id)
                if previous is not None:
                    next_attempts_list.append(Attempt(previous.spec, item.status))
                elif prepared_attempt is not None and item.attempt_id == prepared_attempt.attempt_id:
                    next_attempts_list.append(Attempt(prepared_attempt, item.status))
            next_attempts = tuple(next_attempts_list)
            next_satisfactions = (*self.satisfactions, satisfaction) if satisfaction is not None else self.satisfactions
            return assemble(
                next_spec=self.spec,
                next_status=next_state.status,
                next_states=next_state.work_unit_states,
                next_attempts=next_attempts,
                next_satisfactions=next_satisfactions,
                next_spec_history=self.spec_history,
            )

        def amend_transition(
            amended_spec: ProgramSpec,
            amended_states: tuple[WorkUnitState, ...],
            amended_attempts: tuple[Attempt, ...],
        ) -> Program:
            next_status = self.status
            if self.status in (ProgramStatus.ACTIVE, ProgramStatus.COMPLETED):
                amended_states = _mark_ready(amended_spec, amended_states)
                next_status = _status_after_work_unit_change(amended_states)
            return assemble(
                next_spec=amended_spec,
                next_status=next_status,
                next_states=amended_states,
                next_attempts=amended_attempts,
                next_satisfactions=self.satisfactions,
                next_spec_history=(*self.spec_history, amended_spec),
            )

        if type(command) is ActivateProgram:
            return transition()

        if type(command) is PauseProgram:
            return transition()

        if type(command) is ResumeProgram:
            return transition()

        if type(command) is CancelProgram:
            return transition()

        if type(command) is CancelWorkUnit:
            return transition()

        if type(command) is PrepareAttempt:
            return transition(prepared_attempt=command.attempt)

        if type(command) is StartAttempt:
            return transition()

        if type(command) is FinishAttempt:
            return transition()

        if type(command) is FailAttempt:
            return transition()

        if type(command) is CancelAttempt:
            return transition()

        if type(command) is SatisfyWorkUnit:
            return transition(satisfaction=command.satisfaction)

        if type(command) is AmendProgramSpec:
            new_spec = self.spec.amend(command.amendment)
            affected = _amendment_affected_work_units(self.spec, new_spec)
            old_states = {state.work_unit_id: state for state in self.work_unit_states}
            old_units = {unit.work_unit_id: unit for unit in self.spec.work_units}
            new_states: list[WorkUnitState] = []
            for unit in new_spec.work_units:
                prior = old_states.get(unit.work_unit_id)
                unchanged = unit.work_unit_id not in affected and old_units.get(unit.work_unit_id) == unit
                preserved_satisfied = (
                    prior is not None
                    and prior.status is WorkUnitStatus.SATISFIED
                    and unchanged
                    and any(
                        item.work_unit_id == unit.work_unit_id and item.work_unit_digest == unit.digest
                        for item in self.satisfactions
                    )
                )
                preserved_cancelled = prior is not None and prior.status is WorkUnitStatus.CANCELLED and unchanged
                state_status = (
                    WorkUnitStatus.SATISFIED
                    if preserved_satisfied
                    else WorkUnitStatus.CANCELLED
                    if preserved_cancelled
                    else WorkUnitStatus.PENDING
                )
                new_states.append(WorkUnitState(unit.work_unit_id, state_status))
            states = tuple(new_states)
            attempts = tuple(
                Attempt(attempt.spec, AttemptStatus.CANCELLED)
                if attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
                else attempt
                for attempt in self.attempts
            )
            return amend_transition(new_spec, states, attempts)

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


for _sealed_type in (
    BudgetPolicy,
    AuthorityEnvelope,
    WorkUnit,
    ProgramSpec,
    InputBinding,
    AttemptSpec,
    Attempt,
    TrustedSatisfaction,
    WorkUnitState,
    SpecAmendment,
    Program,
):
    _seal_type(_sealed_type)
del _sealed_type


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
