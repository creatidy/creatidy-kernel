# SPDX-License-Identifier: Apache-2.0
"""Pure K1 intent, present-fact command legality, and historical projection.

References and actor authority enter this boundary as trusted, opaque values.
Neither external execution nor verification of the referenced bytes belongs here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import cast


class DomainError(Exception):
    """Base class for deterministic domain failures."""


class InvalidDomainValue(DomainError, ValueError):
    """A domain value fails its local invariant."""


class InvalidGraph(InvalidDomainValue):
    """The owner-approved graph has invalid references or producers."""


class MissingReference(InvalidGraph):
    """An identity is absent from the relevant scope."""


class MissingInput(InvalidGraph):
    """A required logical input has no unique approved source."""


class CycleDetected(InvalidGraph):
    """The WorkUnit graph contains a cycle."""


class StaleRevision(DomainError):
    """A command or fact references an incorrect aggregate/spec revision."""


class IllegalTransition(DomainError):
    """The current concrete lifecycle does not admit the command."""


class AuthorityViolation(DomainError):
    """The actor or issuer lacks the required authority."""


class BudgetExceeded(DomainError):
    """An admission ceiling would be exceeded."""


class DuplicateAttempt(DomainError):
    """An Attempt ID has already been admitted."""


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
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise InvalidDomainValue(f"{field} must be a {'nonnegative' if allow_zero else 'positive'} integer")


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


def _criteria(value: object, field: str) -> tuple[str, ...]:
    values = _immutable_tuple(value, field)
    if any(type(item) is not str or not item.strip() for item in values):
        raise InvalidDomainValue(f"{field} must contain nonblank criteria")
    result = cast(tuple[str, ...], values)
    if len(result) != len(set(result)):
        raise InvalidDomainValue(f"{field} must not repeat a criterion")
    return tuple(sorted(result))


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


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
    """Finite admission limits, not runtime usage accounting."""

    max_attempts: int = dataclass_field(default=3, init=False)
    max_active_attempts: int = dataclass_field(default=1, init=False)

    def __init__(self, max_attempts: int = 3, max_active_attempts: int = 1) -> None:
        _begin_init(self, BudgetPolicy, "max_attempts")
        _revision(max_attempts, "max_attempts", allow_zero=True)
        _revision(max_active_attempts, "max_active_attempts", allow_zero=True)
        _set_fields(self, max_attempts=max_attempts, max_active_attempts=max_active_attempts)


@dataclass(frozen=True, slots=True, init=False)
class AuthorityEnvelope:
    """Owner identity, delegated operational scope, and trusted fact issuers."""

    owner_id: str = dataclass_field(init=False)
    allowed_actions: frozenset[DomainAction] = dataclass_field(init=False)
    max_attempts: int = dataclass_field(init=False)
    trusted_satisfaction_issuers: frozenset[str] = dataclass_field(init=False)
    delegated_actor_ids: frozenset[str] = dataclass_field(init=False)

    def __init__(
        self,
        owner_id: str,
        allowed_actions: frozenset[DomainAction] = frozenset(DomainAction),
        max_attempts: int = 3,
        trusted_satisfaction_issuers: frozenset[str] = frozenset(),
        delegated_actor_ids: frozenset[str] = frozenset(),
    ) -> None:
        _begin_init(self, AuthorityEnvelope, "owner_id")
        _nonempty(owner_id, "owner_id")
        if type(allowed_actions) is not frozenset or any(type(item) is not DomainAction for item in allowed_actions):
            raise InvalidDomainValue("allowed_actions must be an immutable set of DomainAction values")
        _revision(max_attempts, "max_attempts", allow_zero=True)
        _immutable_strings(trusted_satisfaction_issuers, "trusted_satisfaction_issuers")
        _immutable_strings(delegated_actor_ids, "delegated_actor_ids")
        if trusted_satisfaction_issuers & delegated_actor_ids:
            raise InvalidDomainValue("ordinary delegated workers cannot be trusted satisfaction issuers")
        _set_fields(
            self,
            owner_id=owner_id,
            allowed_actions=allowed_actions,
            max_attempts=max_attempts,
            trusted_satisfaction_issuers=trusted_satisfaction_issuers,
            delegated_actor_ids=delegated_actor_ids,
        )

    def payload(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id,
            "allowed_actions": sorted(action.value for action in self.allowed_actions),
            "max_attempts": self.max_attempts,
            "trusted_satisfaction_issuers": sorted(self.trusted_satisfaction_issuers),
            "delegated_actor_ids": sorted(self.delegated_actor_ids),
        }


@dataclass(frozen=True, slots=True, init=False)
class PolicyReference:
    """Opaque policy identity pinned to an immutable version/digest."""

    policy_id: str = dataclass_field(init=False)
    version: str = dataclass_field(init=False)
    digest: str = dataclass_field(init=False)

    def __init__(self, policy_id: str, version: str, digest: str) -> None:
        _begin_init(self, PolicyReference, "policy_id")
        for field, value in (("policy_id", policy_id), ("version", version), ("digest", digest)):
            _nonempty(value, field)
        _set_fields(self, policy_id=policy_id, version=version, digest=digest)

    def payload(self) -> dict[str, str]:
        return {"policy_id": self.policy_id, "version": self.version, "digest": self.digest}


def _policies(value: object, field: str) -> tuple[PolicyReference, ...]:
    values = _immutable_tuple(value, field)
    if any(type(item) is not PolicyReference for item in values):
        raise InvalidDomainValue(f"{field} must contain PolicyReference values")
    refs = cast(tuple[PolicyReference, ...], values)
    if len({(ref.policy_id, ref.version, ref.digest) for ref in refs}) != len(refs):
        raise InvalidDomainValue(f"{field} must not repeat a reference")
    return tuple(sorted(refs, key=lambda ref: (ref.policy_id, ref.version, ref.digest)))


@dataclass(frozen=True, slots=True, init=False)
class InputBinding:
    """Approved opaque reference; Attempt inputs additionally pin source provenance."""

    name: str = dataclass_field(init=False)
    reference: str = dataclass_field(init=False)
    source_kind: str = dataclass_field(init=False)
    source_work_unit_id: str | None = dataclass_field(init=False)
    satisfaction_id: str | None = dataclass_field(init=False)
    output_name: str | None = dataclass_field(init=False)

    def __init__(
        self,
        name: str,
        reference: str,
        source_kind: str = "initial",
        source_work_unit_id: str | None = None,
        satisfaction_id: str | None = None,
        output_name: str | None = None,
    ) -> None:
        _begin_init(self, InputBinding, "name")
        _nonempty(name, "input name")
        _nonempty(reference, "input reference")
        if source_kind == "initial":
            if any(value is not None for value in (source_work_unit_id, satisfaction_id, output_name)):
                raise InvalidDomainValue("initial inputs cannot cite a predecessor")
        elif source_kind == "predecessor":
            for field, value in (
                ("source_work_unit_id", source_work_unit_id),
                ("satisfaction_id", satisfaction_id),
                ("output_name", output_name),
            ):
                _nonempty(value, field)
            if output_name != name:
                raise InvalidDomainValue("a predecessor binding must name its declared output")
        else:
            raise InvalidDomainValue("source_kind must be initial or predecessor")
        _set_fields(
            self,
            name=name,
            reference=reference,
            source_kind=source_kind,
            source_work_unit_id=source_work_unit_id,
            satisfaction_id=satisfaction_id,
            output_name=output_name,
        )

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "reference": self.reference,
            "source_kind": self.source_kind,
            "source_work_unit_id": self.source_work_unit_id,
            "satisfaction_id": self.satisfaction_id,
            "output_name": self.output_name,
        }

    def subject(self) -> tuple[str, str, str, str | None, str | None]:
        """Acceptance identity includes concrete source/reference, not a fact's record ID."""
        return (self.name, self.reference, self.source_kind, self.source_work_unit_id, self.output_name)


def _bindings(value: object, field: str) -> tuple[InputBinding, ...]:
    values = _immutable_tuple(value, field)
    if any(type(item) is not InputBinding for item in values):
        raise InvalidDomainValue(f"{field} must contain InputBinding values")
    bindings = cast(tuple[InputBinding, ...], values)
    if len({item.name for item in bindings}) != len(bindings):
        raise InvalidDomainValue(f"{field} must have unique logical names")
    return tuple(sorted(bindings, key=lambda item: item.name))


_DEFAULT_BUDGET = BudgetPolicy()
_DEFAULT_AUTHORITY = AuthorityEnvelope("owner")


@dataclass(frozen=True, slots=True, init=False)
class WorkUnit:
    """Program-scoped obligation with explicit acceptance intent and declarations."""

    work_unit_id: str = dataclass_field(init=False)
    obligation: str = dataclass_field(init=False)
    dependencies: tuple[str, ...] = dataclass_field(init=False)
    required_inputs: frozenset[str] = dataclass_field(init=False)
    outputs: frozenset[str] = dataclass_field(init=False)
    acceptance_criteria: tuple[str, ...] = dataclass_field(init=False)
    acceptance_policy_reference: PolicyReference | None = dataclass_field(init=False)

    def __init__(
        self,
        work_unit_id: str,
        obligation: str,
        dependencies: tuple[str, ...] = (),
        required_inputs: frozenset[str] = frozenset(),
        outputs: frozenset[str] = frozenset(),
        acceptance_criteria: tuple[str, ...] = (),
        acceptance_policy_reference: PolicyReference | None = None,
    ) -> None:
        _begin_init(self, WorkUnit, "work_unit_id")
        _nonempty(work_unit_id, "work_unit_id")
        _nonempty(obligation, "obligation")
        deps = _immutable_tuple(dependencies, "dependencies")
        if any(type(item) is not str or not item.strip() for item in deps) or len(set(deps)) != len(deps):
            raise InvalidDomainValue("dependencies must be distinct nonblank WorkUnit IDs")
        inputs = _immutable_strings(required_inputs, "required_inputs")
        declared = _immutable_strings(outputs, "outputs")
        criteria = _criteria(acceptance_criteria, "WorkUnit acceptance_criteria")
        if acceptance_policy_reference is not None and type(acceptance_policy_reference) is not PolicyReference:
            raise InvalidDomainValue("acceptance_policy_reference must be a PolicyReference")
        if not criteria and acceptance_policy_reference is None:
            raise InvalidDomainValue("WorkUnit requires acceptance criteria or an acceptance policy reference")
        if inputs & declared:
            raise InvalidDomainValue("a WorkUnit cannot consume and produce the same logical name")
        _set_fields(
            self,
            work_unit_id=work_unit_id,
            obligation=obligation,
            dependencies=tuple(sorted(cast(tuple[str, ...], deps))),
            required_inputs=inputs,
            outputs=declared,
            acceptance_criteria=criteria,
            acceptance_policy_reference=acceptance_policy_reference,
        )

    @property
    def id(self) -> str:
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
            "obligation": self.obligation,
            "dependencies": self.dependencies,
            "required_inputs": sorted(self.required_inputs),
            "outputs": sorted(self.outputs),
            "acceptance_criteria": self.acceptance_criteria,
            "acceptance_policy_reference": (
                None if self.acceptance_policy_reference is None else self.acceptance_policy_reference.payload()
            ),
        }

    @property
    def digest(self) -> str:
        return _digest(self.payload())


def _topological_order(units: dict[str, WorkUnit]) -> tuple[str, ...]:
    remaining = {name: set(unit.dependencies) for name, unit in units.items()}
    ordered: list[str] = []
    while remaining:
        ready = sorted(name for name, dependencies in remaining.items() if not dependencies)
        if not ready:
            raise CycleDetected(f"WorkUnit dependency cycle involves: {', '.join(sorted(remaining))}")
        ordered.extend(ready)
        for name in ready:
            del remaining[name]
        for dependencies in remaining.values():
            dependencies.difference_update(ready)
    return tuple(ordered)


@dataclass(frozen=True, slots=True, init=False)
class ProgramSpec:
    """One immutable owner-approved snapshot, including all operational intent."""

    program_id: str = dataclass_field(init=False)
    objective: str = dataclass_field(init=False)
    work_units: tuple[WorkUnit, ...] = dataclass_field(init=False)
    initial_inputs: tuple[InputBinding, ...] = dataclass_field(init=False)
    budget: BudgetPolicy = dataclass_field(init=False)
    authority: AuthorityEnvelope = dataclass_field(init=False)
    revision: int = dataclass_field(init=False)
    parent_digest: str | None = dataclass_field(init=False)
    acceptance_criteria: tuple[str, ...] = dataclass_field(init=False)
    policy_references: tuple[PolicyReference, ...] = dataclass_field(init=False)

    def __init__(
        self,
        program_id: str,
        objective: str,
        work_units: tuple[WorkUnit, ...],
        initial_inputs: tuple[InputBinding, ...] = (),
        budget: BudgetPolicy = _DEFAULT_BUDGET,
        authority: AuthorityEnvelope = _DEFAULT_AUTHORITY,
        revision: int = 1,
        parent_digest: str | None = None,
        acceptance_criteria: tuple[str, ...] = (),
        policy_references: tuple[PolicyReference, ...] = (),
    ) -> None:
        _begin_init(self, ProgramSpec, "program_id")
        _nonempty(program_id, "program_id")
        _nonempty(objective, "objective")
        units = _immutable_tuple(work_units, "work_units")
        if not units or any(type(item) is not WorkUnit for item in units):
            raise InvalidDomainValue("work_units must be a nonempty tuple of WorkUnit values")
        typed_units = cast(tuple[WorkUnit, ...], units)
        if len({unit.id for unit in typed_units}) != len(typed_units):
            raise InvalidGraph("WorkUnit IDs must be unique within a Program")
        inputs = _bindings(initial_inputs, "initial_inputs")
        if any(item.source_kind != "initial" for item in inputs):
            raise InvalidDomainValue("approved initial inputs cannot cite a predecessor")
        criteria = _criteria(acceptance_criteria, "Program acceptance_criteria")
        policies = _policies(policy_references, "policy_references")
        if not criteria or not policies:
            raise InvalidDomainValue("Program acceptance criteria and pinned policy references are required")
        if type(budget) is not BudgetPolicy or type(authority) is not AuthorityEnvelope:
            raise InvalidDomainValue("budget and authority must be immutable domain values")
        _revision(revision, "revision")
        _optional_nonempty(parent_digest, "parent_digest")
        by_id = {unit.id: unit for unit in typed_units}
        producers: dict[str, str | None] = {item.name: None for item in inputs}
        for unit in typed_units:
            for predecessor in unit.dependencies:
                if predecessor not in by_id:
                    raise MissingReference(f"WorkUnit {unit.id!r} depends on missing WorkUnit {predecessor!r}")
            for output in unit.outputs:
                if output in producers:
                    raise InvalidGraph(f"logical input {output!r} has ambiguous producers")
                producers[output] = unit.id
        _topological_order(by_id)
        for unit in typed_units:
            for name in unit.required_inputs:
                source = producers.get(name)
                if name not in producers or (source is not None and source not in unit.dependencies):
                    raise MissingInput(f"WorkUnit {unit.id!r} has no direct approved source for {name!r}")
        _set_fields(
            self,
            program_id=program_id,
            objective=objective,
            work_units=tuple(sorted(typed_units, key=lambda item: item.id)),
            initial_inputs=inputs,
            budget=budget,
            authority=authority,
            revision=revision,
            parent_digest=parent_digest,
            acceptance_criteria=criteria,
            policy_references=policies,
        )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "program_id": self.program_id,
            "objective": self.objective,
            "acceptance_criteria": self.acceptance_criteria,
            "policy_references": [ref.payload() for ref in self.policy_references],
            "work_units": [unit.payload() for unit in self.work_units],
            "initial_inputs": [item.payload() for item in self.initial_inputs],
            "budget": {
                "max_attempts": self.budget.max_attempts,
                "max_active_attempts": self.budget.max_active_attempts,
            },
            "authority": self.authority.payload(),
        }

    def payload(self) -> dict[str, object]:
        return {**self.semantic_payload(), "revision": self.revision, "parent_digest": self.parent_digest}

    @property
    def digest(self) -> str:
        return _digest(self.payload())

    @property
    def topological_order(self) -> tuple[str, ...]:
        return _topological_order({unit.id: unit for unit in self.work_units})

    def work_unit(self, work_unit_id: str) -> WorkUnit:
        for unit in self.work_units:
            if unit.id == work_unit_id:
                return unit
        raise MissingReference(f"unknown WorkUnit {work_unit_id!r}")

    def work_unit_applicability_fingerprint(self, work_unit_id: str) -> str:
        target = self.work_unit(work_unit_id)
        by_id = {unit.id: unit for unit in self.work_units}
        closure = {target.id}
        pending = list(target.dependencies)
        while pending:
            predecessor = pending.pop()
            if predecessor not in closure:
                closure.add(predecessor)
                pending.extend(by_id[predecessor].dependencies)
        consumed = {name for unit_id in closure for name in by_id[unit_id].required_inputs}
        return _digest(
            {
                "encoding": "creatidy-k1-work-unit-intent-v1",
                "program_id": self.program_id,
                "objective": self.objective,
                "acceptance_criteria": self.acceptance_criteria,
                "policy_references": [ref.payload() for ref in self.policy_references],
                "closure": [by_id[unit_id].payload() for unit_id in sorted(closure)],
                "initial_inputs": [item.payload() for item in self.initial_inputs if item.name in consumed],
            }
        )

    def amend(self, amendment: SpecAmendment) -> ProgramSpec:
        if amendment.expected_revision != self.revision:
            raise StaleRevision("amendment expected an older ProgramSpec revision")
        proposed = ProgramSpec(
            program_id=self.program_id,
            objective=self.objective if amendment.objective is None else amendment.objective,
            work_units=self.work_units if amendment.work_units is None else amendment.work_units,
            initial_inputs=self.initial_inputs if amendment.initial_inputs is None else amendment.initial_inputs,
            budget=self.budget if amendment.budget is None else amendment.budget,
            authority=self.authority if amendment.authority is None else amendment.authority,
            revision=self.revision + 1,
            parent_digest=self.digest,
            acceptance_criteria=(
                self.acceptance_criteria if amendment.acceptance_criteria is None else amendment.acceptance_criteria
            ),
            policy_references=(
                self.policy_references if amendment.policy_references is None else amendment.policy_references
            ),
        )
        return self if proposed.semantic_payload() == self.semantic_payload() else proposed


@dataclass(frozen=True, slots=True, init=False)
class AttemptSpec:
    """An immutable execution snapshot with approved concrete input provenance."""

    attempt_id: str = dataclass_field(init=False)
    program_id: str = dataclass_field(init=False)
    work_unit_id: str = dataclass_field(init=False)
    spec_revision: int = dataclass_field(init=False)
    spec_digest: str = dataclass_field(init=False)
    effective_inputs: tuple[InputBinding, ...] = dataclass_field(init=False)
    allocation_reference: str | None = dataclass_field(init=False)
    agent_definition_reference: str | None = dataclass_field(init=False)
    workspace_reference: str | None = dataclass_field(init=False)
    context_reference: str | None = dataclass_field(init=False)

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
        for field, value in (
            ("attempt_id", attempt_id),
            ("program_id", program_id),
            ("work_unit_id", work_unit_id),
            ("spec_digest", spec_digest),
        ):
            _nonempty(value, field)
        _revision(spec_revision, "spec_revision")
        for field, value in (
            ("allocation_reference", allocation_reference),
            ("agent_definition_reference", agent_definition_reference),
            ("workspace_reference", workspace_reference),
            ("context_reference", context_reference),
        ):
            _optional_nonempty(value, field)
        _set_fields(
            self,
            attempt_id=attempt_id,
            program_id=program_id,
            work_unit_id=work_unit_id,
            spec_revision=spec_revision,
            spec_digest=spec_digest,
            effective_inputs=_bindings(effective_inputs, "effective_inputs"),
            allocation_reference=allocation_reference,
            agent_definition_reference=agent_definition_reference,
            workspace_reference=workspace_reference,
            context_reference=context_reference,
        )

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
                "effective_inputs": [item.payload() for item in self.effective_inputs],
                "allocation_reference": self.allocation_reference,
                "agent_definition_reference": self.agent_definition_reference,
                "workspace_reference": self.workspace_reference,
                "context_reference": self.context_reference,
            }
        )


@dataclass(frozen=True, slots=True, init=False)
class Attempt:
    """Attempt lifecycle record; its effective specification never changes."""

    spec: AttemptSpec = dataclass_field(init=False)
    status: AttemptStatus = dataclass_field(init=False)
    actor_id: str = dataclass_field(init=False)

    def __init__(
        self, spec: AttemptSpec, status: AttemptStatus = AttemptStatus.PREPARED, actor_id: str = "owner"
    ) -> None:
        _begin_init(self, Attempt, "spec")
        if type(spec) is not AttemptSpec or type(status) is not AttemptStatus:
            raise InvalidDomainValue("Attempt requires an AttemptSpec and AttemptStatus")
        _nonempty(actor_id, "Attempt actor_id")
        _set_fields(self, spec=spec, status=status, actor_id=actor_id)

    @property
    def attempt_id(self) -> str:
        return self.spec.attempt_id


@dataclass(frozen=True, slots=True, init=False)
class TrustedSatisfaction:
    """Trusted independent acceptance fact, recorded only after a finished Attempt."""

    reference_id: str = dataclass_field(init=False)
    program_id: str = dataclass_field(init=False)
    work_unit_id: str = dataclass_field(init=False)
    spec_revision: int = dataclass_field(init=False)
    spec_digest: str = dataclass_field(init=False)
    work_unit_fingerprint: str = dataclass_field(init=False)
    issuer_id: str = dataclass_field(init=False)
    source_attempt_id: str = dataclass_field(init=False)
    output_bindings: tuple[InputBinding, ...] = dataclass_field(init=False)
    record_order: int = dataclass_field(init=False)

    def __init__(
        self,
        reference_id: str,
        program_id: str,
        work_unit_id: str,
        spec_revision: int,
        spec_digest: str,
        work_unit_fingerprint: str,
        issuer_id: str,
        source_attempt_id: str,
        output_bindings: tuple[InputBinding, ...] = (),
        record_order: int = 0,
    ) -> None:
        _begin_init(self, TrustedSatisfaction, "reference_id")
        for field, value in (
            ("reference_id", reference_id),
            ("program_id", program_id),
            ("work_unit_id", work_unit_id),
            ("spec_digest", spec_digest),
            ("work_unit_fingerprint", work_unit_fingerprint),
            ("issuer_id", issuer_id),
            ("source_attempt_id", source_attempt_id),
        ):
            _nonempty(value, field)
        _revision(spec_revision, "spec_revision")
        _revision(record_order, "record_order", allow_zero=True)
        outputs = _bindings(output_bindings, "output_bindings")
        if any(item.source_kind != "initial" for item in outputs):
            raise InvalidDomainValue("declared outputs must contain only opaque name/reference pairs")
        _set_fields(
            self,
            reference_id=reference_id,
            program_id=program_id,
            work_unit_id=work_unit_id,
            spec_revision=spec_revision,
            spec_digest=spec_digest,
            work_unit_fingerprint=work_unit_fingerprint,
            issuer_id=issuer_id,
            source_attempt_id=source_attempt_id,
            output_bindings=outputs,
            record_order=record_order,
        )


@dataclass(frozen=True, slots=True, init=False)
class WorkUnitCancellation:
    """Explicit owner abandonment of exactly one historical WorkUnit intent."""

    work_unit_id: str = dataclass_field(init=False)
    work_unit_fingerprint: str = dataclass_field(init=False)
    spec_revision: int = dataclass_field(init=False)
    spec_digest: str = dataclass_field(init=False)
    actor_id: str = dataclass_field(init=False)
    reason: str = dataclass_field(init=False)
    record_order: int = dataclass_field(init=False)

    def __init__(
        self,
        work_unit_id: str,
        work_unit_fingerprint: str,
        spec_revision: int,
        spec_digest: str,
        actor_id: str,
        reason: str,
        record_order: int,
    ) -> None:
        _begin_init(self, WorkUnitCancellation, "work_unit_id")
        for field, value in (
            ("work_unit_id", work_unit_id),
            ("work_unit_fingerprint", work_unit_fingerprint),
            ("spec_digest", spec_digest),
            ("actor_id", actor_id),
            ("reason", reason),
        ):
            _nonempty(value, field)
        _revision(spec_revision, "spec_revision")
        _revision(record_order, "record_order")
        _set_fields(
            self,
            work_unit_id=work_unit_id,
            work_unit_fingerprint=work_unit_fingerprint,
            spec_revision=spec_revision,
            spec_digest=spec_digest,
            actor_id=actor_id,
            reason=reason,
            record_order=record_order,
        )


@dataclass(frozen=True, slots=True, init=False)
class AttemptCancellation:
    """A domain cancellation decision, never proof of remote runtime termination."""

    attempt_id: str = dataclass_field(init=False)
    actor_id: str = dataclass_field(init=False)
    reason: str = dataclass_field(init=False)
    record_order: int = dataclass_field(init=False)

    def __init__(self, attempt_id: str, actor_id: str, reason: str, record_order: int) -> None:
        _begin_init(self, AttemptCancellation, "attempt_id")
        for field, value in (("attempt_id", attempt_id), ("actor_id", actor_id), ("reason", reason)):
            _nonempty(value, field)
        _revision(record_order, "record_order")
        _set_fields(self, attempt_id=attempt_id, actor_id=actor_id, reason=reason, record_order=record_order)


@dataclass(frozen=True, slots=True, init=False)
class ProgramConclusion:
    """Historical terminal Program observation retained across later amendments."""

    status: ProgramStatus = dataclass_field(init=False)
    spec_revision: int = dataclass_field(init=False)
    spec_digest: str = dataclass_field(init=False)
    record_order: int = dataclass_field(init=False)

    def __init__(self, status: ProgramStatus, spec_revision: int, spec_digest: str, record_order: int) -> None:
        _begin_init(self, ProgramConclusion, "status")
        if status not in (ProgramStatus.COMPLETED, ProgramStatus.CANCELLED):
            raise InvalidDomainValue("a Program conclusion must be terminal")
        _revision(spec_revision, "spec_revision")
        _nonempty(spec_digest, "spec_digest")
        _revision(record_order, "record_order")
        _set_fields(
            self, status=status, spec_revision=spec_revision, spec_digest=spec_digest, record_order=record_order
        )


@dataclass(frozen=True, slots=True, init=False)
class WorkUnitState:
    """Rebuildable current projection, with its chosen satisfaction/Attempt."""

    work_unit_id: str = dataclass_field(init=False)
    status: WorkUnitStatus = dataclass_field(init=False)
    active_attempt_id: str | None = dataclass_field(init=False)
    satisfaction_id: str | None = dataclass_field(init=False)

    def __init__(
        self,
        work_unit_id: str,
        status: WorkUnitStatus = WorkUnitStatus.PENDING,
        active_attempt_id: str | None = None,
        satisfaction_id: str | None = None,
    ) -> None:
        _begin_init(self, WorkUnitState, "work_unit_id")
        _nonempty(work_unit_id, "work_unit_id")
        if type(status) is not WorkUnitStatus:
            raise InvalidDomainValue("status must be a WorkUnitStatus")
        _optional_nonempty(active_attempt_id, "active_attempt_id")
        _optional_nonempty(satisfaction_id, "satisfaction_id")
        if (active_attempt_id is not None) != (status is WorkUnitStatus.ACTIVE):
            raise InvalidDomainValue("exactly an active WorkUnit must have an active Attempt")
        if (satisfaction_id is not None) != (status is WorkUnitStatus.SATISFIED):
            raise InvalidDomainValue("exactly a satisfied WorkUnit must cite its satisfaction")
        _set_fields(
            self,
            work_unit_id=work_unit_id,
            status=status,
            active_attempt_id=active_attempt_id,
            satisfaction_id=satisfaction_id,
        )


@dataclass(frozen=True, slots=True, init=False)
class SpecAmendment:
    """Owner-authored candidate replacements; no-op amendments are legal."""

    expected_revision: int = dataclass_field(init=False)
    objective: str | None = dataclass_field(init=False)
    work_units: tuple[WorkUnit, ...] | None = dataclass_field(init=False)
    initial_inputs: tuple[InputBinding, ...] | None = dataclass_field(init=False)
    budget: BudgetPolicy | None = dataclass_field(init=False)
    authority: AuthorityEnvelope | None = dataclass_field(init=False)
    reason: str = dataclass_field(init=False)
    acceptance_criteria: tuple[str, ...] | None = dataclass_field(init=False)
    policy_references: tuple[PolicyReference, ...] | None = dataclass_field(init=False)

    def __init__(
        self,
        expected_revision: int,
        objective: str | None = None,
        work_units: tuple[WorkUnit, ...] | None = None,
        initial_inputs: tuple[InputBinding, ...] | None = None,
        budget: BudgetPolicy | None = None,
        authority: AuthorityEnvelope | None = None,
        reason: str = "owner amendment",
        acceptance_criteria: tuple[str, ...] | None = None,
        policy_references: tuple[PolicyReference, ...] | None = None,
    ) -> None:
        _begin_init(self, SpecAmendment, "expected_revision")
        _revision(expected_revision, "expected_revision")
        _nonempty(reason, "reason")
        if all(
            value is None
            for value in (
                objective,
                work_units,
                initial_inputs,
                budget,
                authority,
                acceptance_criteria,
                policy_references,
            )
        ):
            raise InvalidDomainValue("an amendment must specify at least one replacement")
        if objective is not None:
            _nonempty(objective, "objective")
        if work_units is not None:
            _immutable_tuple(work_units, "work_units")
        if initial_inputs is not None:
            _bindings(initial_inputs, "initial_inputs")
        if budget is not None and type(budget) is not BudgetPolicy:
            raise InvalidDomainValue("budget must be a BudgetPolicy")
        if authority is not None and type(authority) is not AuthorityEnvelope:
            raise InvalidDomainValue("authority must be an AuthorityEnvelope")
        if acceptance_criteria is not None:
            _criteria(acceptance_criteria, "acceptance_criteria")
        if policy_references is not None:
            _policies(policy_references, "policy_references")
        _set_fields(
            self,
            expected_revision=expected_revision,
            objective=objective,
            work_units=work_units,
            initial_inputs=initial_inputs,
            budget=budget,
            authority=authority,
            reason=reason,
            acceptance_criteria=acceptance_criteria,
            policy_references=policy_references,
        )


@dataclass(frozen=True, slots=True)
class DomainCommand:
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
    reason: str = "owner abandonment"

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        _nonempty(self.work_unit_id, "work_unit_id")
        _nonempty(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class PrepareAttempt(DomainCommand):
    attempt: AttemptSpec

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        if type(self.attempt) is not AttemptSpec:
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
        if type(self.satisfaction) is not TrustedSatisfaction:
            raise InvalidDomainValue("satisfaction must be a TrustedSatisfaction")


@dataclass(frozen=True, slots=True)
class AmendProgramSpec(DomainCommand):
    amendment: SpecAmendment

    def __post_init__(self) -> None:
        DomainCommand.__post_init__(self)
        if type(self.amendment) is not SpecAmendment:
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


def _resolved_inputs(
    spec: ProgramSpec,
    unit: WorkUnit,
    projected: dict[str, WorkUnitState],
    satisfactions: dict[str, TrustedSatisfaction],
) -> tuple[InputBinding, ...] | None:
    if any(projected[predecessor].status is not WorkUnitStatus.SATISFIED for predecessor in unit.dependencies):
        return None
    resolved = {item.name: item for item in spec.initial_inputs if item.name in unit.required_inputs}
    for predecessor in unit.dependencies:
        state = projected[predecessor]
        source = satisfactions[cast(str, state.satisfaction_id)]
        for output in source.output_bindings:
            if output.name in unit.required_inputs:
                resolved[output.name] = InputBinding(
                    output.name,
                    output.reference,
                    "predecessor",
                    predecessor,
                    source.reference_id,
                    output.name,
                )
    if set(resolved) != set(unit.required_inputs):
        return None
    return tuple(resolved[name] for name in sorted(resolved))


def _same_subject(left: tuple[InputBinding, ...], right: tuple[InputBinding, ...]) -> bool:
    return tuple(item.subject() for item in left) == tuple(item.subject() for item in right)


def _project(
    spec: ProgramSpec,
    attempts: tuple[Attempt, ...],
    satisfactions: tuple[TrustedSatisfaction, ...],
    cancellations: tuple[WorkUnitCancellation, ...],
) -> tuple[WorkUnitState, ...]:
    chosen: dict[str, WorkUnitState] = {}
    by_satisfaction = {item.reference_id: item for item in satisfactions}
    by_attempt = {item.attempt_id: item for item in attempts}
    for unit_id in spec.topological_order:
        unit = spec.work_unit(unit_id)
        fingerprint = spec.work_unit_applicability_fingerprint(unit_id)
        if any(chosen[predecessor].status is WorkUnitStatus.CANCELLED for predecessor in unit.dependencies) or any(
            item.work_unit_id == unit_id and item.work_unit_fingerprint == fingerprint for item in cancellations
        ):
            chosen[unit_id] = WorkUnitState(unit_id, WorkUnitStatus.CANCELLED)
            continue
        inputs = _resolved_inputs(spec, unit, chosen, by_satisfaction)
        matching = (
            item
            for item in satisfactions
            if item.work_unit_id == unit_id
            and item.work_unit_fingerprint == fingerprint
            and {output.name for output in item.output_bindings} == set(unit.outputs)
            and inputs is not None
            and _same_subject(by_attempt[item.source_attempt_id].spec.effective_inputs, inputs)
        )
        latest = max(matching, key=lambda item: item.record_order, default=None)
        if latest is not None:
            chosen[unit_id] = WorkUnitState(unit_id, WorkUnitStatus.SATISFIED, satisfaction_id=latest.reference_id)
            continue
        live = next(
            (
                attempt
                for attempt in attempts
                if attempt.spec.work_unit_id == unit_id
                and attempt.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
            ),
            None,
        )
        if live is not None and inputs is not None:
            if _same_subject(live.spec.effective_inputs, inputs):
                chosen[unit_id] = WorkUnitState(unit_id, WorkUnitStatus.ACTIVE, live.attempt_id)
                continue
        chosen[unit_id] = WorkUnitState(unit_id, WorkUnitStatus.READY if inputs is not None else WorkUnitStatus.PENDING)
    return tuple(chosen[unit.id] for unit in spec.work_units)


def _program_status(previous: ProgramStatus, states: tuple[WorkUnitState, ...]) -> ProgramStatus:
    if previous is ProgramStatus.CANCELLED:
        return previous
    if previous is ProgramStatus.DRAFT:
        return previous
    if all(item.status is WorkUnitStatus.SATISFIED for item in states):
        return ProgramStatus.COMPLETED
    # Abandoning an obligation does not abandon the Program; only CancelProgram does.
    return ProgramStatus.PAUSED if previous is ProgramStatus.PAUSED else ProgramStatus.ACTIVE


@dataclass(frozen=True, slots=True, init=False)
class Program:
    """Immutable aggregate; projections derive from complete facts, not prior statuses."""

    spec: ProgramSpec = dataclass_field(init=False)
    status: ProgramStatus = dataclass_field(init=False)
    revision: int = dataclass_field(init=False)
    work_unit_states: tuple[WorkUnitState, ...] = dataclass_field(init=False)
    attempts: tuple[Attempt, ...] = dataclass_field(init=False)
    satisfactions: tuple[TrustedSatisfaction, ...] = dataclass_field(init=False)
    cancellations: tuple[WorkUnitCancellation, ...] = dataclass_field(init=False)
    attempt_cancellations: tuple[AttemptCancellation, ...] = dataclass_field(init=False)
    conclusions: tuple[ProgramConclusion, ...] = dataclass_field(init=False)
    spec_history: tuple[ProgramSpec, ...] = dataclass_field(init=False)

    def __init__(
        self,
        spec: ProgramSpec,
        status: ProgramStatus = ProgramStatus.DRAFT,
        revision: int = 0,
        work_unit_states: tuple[WorkUnitState, ...] = (),
        attempts: tuple[Attempt, ...] = (),
        satisfactions: tuple[TrustedSatisfaction, ...] = (),
        spec_history: tuple[ProgramSpec, ...] = (),
        cancellations: tuple[WorkUnitCancellation, ...] = (),
        attempt_cancellations: tuple[AttemptCancellation, ...] = (),
        conclusions: tuple[ProgramConclusion, ...] = (),
    ) -> None:
        _begin_init(self, Program, "spec")
        if (
            type(spec) is not ProgramSpec
            or status is not ProgramStatus.DRAFT
            or revision != 0
            or any(
                (
                    work_unit_states,
                    attempts,
                    satisfactions,
                    spec_history,
                    cancellations,
                    attempt_cancellations,
                    conclusions,
                )
            )
        ):
            raise InvalidDomainValue("use Program.create and domain commands to construct Program state")
        if spec.revision != 1 or spec.parent_digest is not None:
            raise InvalidDomainValue("a new Program must begin with the first approved ProgramSpec revision")
        _set_fields(
            self,
            spec=spec,
            status=status,
            revision=revision,
            work_unit_states=tuple(WorkUnitState(unit.id) for unit in spec.work_units),
            attempts=(),
            satisfactions=(),
            cancellations=(),
            attempt_cancellations=(),
            conclusions=(),
            spec_history=(spec,),
        )

    @classmethod
    def create(cls, spec: ProgramSpec) -> Program:
        return cls(spec)

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
        return tuple(item.work_unit_id for item in self.work_unit_states if item.status is WorkUnitStatus.READY)

    @property
    def ready_work_units(self) -> tuple[WorkUnit, ...]:
        return tuple(self.spec.work_unit(unit_id) for unit_id in self.ready_work_unit_ids)

    def state(self, work_unit_id: str) -> WorkUnitState:
        for state in self.work_unit_states:
            if state.work_unit_id == work_unit_id:
                return state
        raise MissingReference(f"unknown WorkUnit {work_unit_id!r}")

    def attempt(self, attempt_id: str) -> Attempt:
        for attempt in self.attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
        raise MissingReference(f"unknown Attempt {attempt_id!r}")

    def resolved_inputs(self, work_unit_id: str) -> tuple[InputBinding, ...]:
        unit = self.spec.work_unit(work_unit_id)
        inputs = _resolved_inputs(
            self.spec,
            unit,
            {item.work_unit_id: item for item in self.work_unit_states},
            {item.reference_id: item for item in self.satisfactions},
        )
        if inputs is None:
            raise MissingInput(f"WorkUnit {work_unit_id!r} has unavailable approved inputs")
        return inputs

    def _authorize(self, action: DomainAction, actor_id: str) -> None:
        envelope = self.spec.authority
        if action in (DomainAction.AMEND_SPEC, DomainAction.CANCEL_PROGRAM, DomainAction.CANCEL_WORK_UNIT):
            if actor_id != envelope.owner_id:
                raise AuthorityViolation("an owner decision is required")
        elif action is DomainAction.SATISFY_WORK_UNIT:
            if actor_id not in envelope.trusted_satisfaction_issuers:
                raise AuthorityViolation("satisfaction requires a trusted producer")
        elif action not in envelope.allowed_actions or (
            actor_id != envelope.owner_id and actor_id not in envelope.delegated_actor_ids
        ):
            raise AuthorityViolation(f"actor {actor_id!r} cannot perform {action.value!r}")

    def _assemble(
        self,
        spec: ProgramSpec,
        status: ProgramStatus,
        attempts: tuple[Attempt, ...],
        satisfactions: tuple[TrustedSatisfaction, ...],
        cancellations: tuple[WorkUnitCancellation, ...],
        attempt_cancellations: tuple[AttemptCancellation, ...],
        spec_history: tuple[ProgramSpec, ...],
    ) -> Program:
        states = (
            tuple(WorkUnitState(unit.id) for unit in spec.work_units)
            if status is ProgramStatus.DRAFT
            else _project(spec, attempts, satisfactions, cancellations)
        )
        if status not in (ProgramStatus.DRAFT, ProgramStatus.CANCELLED):
            status = _program_status(status, states)
        order = self.revision + 1
        conclusions = self.conclusions
        if status in (ProgramStatus.COMPLETED, ProgramStatus.CANCELLED) and (
            self.status != status or spec is not self.spec
        ):
            conclusions = (*conclusions, ProgramConclusion(status, spec.revision, spec.digest, order))
        instance = object.__new__(Program)
        _set_fields(
            instance,
            spec=spec,
            status=status,
            revision=order,
            work_unit_states=states,
            attempts=attempts,
            satisfactions=satisfactions,
            cancellations=cancellations,
            attempt_cancellations=attempt_cancellations,
            conclusions=conclusions,
            spec_history=spec_history,
        )
        instance._validate()
        return instance

    def _validate(self) -> None:
        if type(self) is not Program or self.spec_history[-1] != self.spec:
            raise InvalidDomainValue("aggregate and spec history must be consistent")
        if len({item.revision for item in self.spec_history}) != len(self.spec_history):
            raise InvalidDomainValue("historical ProgramSpec revisions must be unique")
        if any(
            later.revision != earlier.revision + 1
            or later.parent_digest != earlier.digest
            or later.program_id != earlier.program_id
            for earlier, later in zip(self.spec_history, self.spec_history[1:], strict=False)
        ):
            raise InvalidDomainValue("ProgramSpec history must form an immutable approved revision chain")
        if len({item.attempt_id for item in self.attempts}) != len(self.attempts):
            raise DuplicateAttempt("Attempt IDs must be unique")
        if len({item.reference_id for item in self.satisfactions}) != len(self.satisfactions):
            raise InvalidDomainValue("satisfaction IDs must be unique")
        history = {(item.revision, item.digest): item for item in self.spec_history}
        attempts = {item.attempt_id: item for item in self.attempts}
        for attempt in self.attempts:
            historical = history.get((attempt.spec.spec_revision, attempt.spec.spec_digest))
            if historical is None or attempt.spec.program_id != self.program_id:
                raise InvalidDomainValue("Attempt must cite its historical ProgramSpec")
            try:
                historical.work_unit(attempt.spec.work_unit_id)
            except MissingReference as error:
                raise InvalidDomainValue("Attempt cites a foreign WorkUnit") from error
        for satisfaction in self.satisfactions:
            historical = history.get((satisfaction.spec_revision, satisfaction.spec_digest))
            if historical is None or satisfaction.program_id != self.program_id:
                raise InvalidDomainValue("satisfaction must cite its historical ProgramSpec")
            if satisfaction.work_unit_fingerprint != historical.work_unit_applicability_fingerprint(
                satisfaction.work_unit_id
            ):
                raise InvalidDomainValue("satisfaction must pin its historical acceptance subject")
            source = attempts.get(satisfaction.source_attempt_id)
            if (
                source is None
                or source.status is not AttemptStatus.FINISHED
                or (source.spec.work_unit_id, source.spec.spec_revision, source.spec.spec_digest)
                != (satisfaction.work_unit_id, satisfaction.spec_revision, satisfaction.spec_digest)
            ):
                raise InvalidDomainValue("satisfaction must cite a matching finished Attempt")
            if {item.name for item in satisfaction.output_bindings} != set(
                historical.work_unit(satisfaction.work_unit_id).outputs
            ):
                raise InvalidDomainValue("satisfaction outputs must match historical declarations")
            if not 0 < satisfaction.record_order <= self.revision:
                raise InvalidDomainValue("satisfaction has no authoritative aggregate record order")
        for cancellation in self.cancellations:
            historical = history.get((cancellation.spec_revision, cancellation.spec_digest))
            if (
                historical is None
                or (
                    cancellation.work_unit_fingerprint
                    != historical.work_unit_applicability_fingerprint(cancellation.work_unit_id)
                )
                or cancellation.record_order > self.revision
            ):
                raise InvalidDomainValue("cancellation must pin a historical WorkUnit intent")
        if self.status is not ProgramStatus.DRAFT and self.work_unit_states != _project(
            self.spec, self.attempts, self.satisfactions, self.cancellations
        ):
            raise InvalidDomainValue("WorkUnit projection differs from immutable history")
        states = {item.work_unit_id: item for item in self.work_unit_states}
        live_by_unit: set[str] = set()
        for attempt in self.attempts:
            if attempt.status not in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING):
                continue
            unit_id = attempt.spec.work_unit_id
            if unit_id not in states or unit_id in live_by_unit or states[unit_id].status is WorkUnitStatus.CANCELLED:
                raise InvalidDomainValue("each live Attempt must retain one current, non-cancelled WorkUnit subject")
            live_by_unit.add(unit_id)
            if (
                states[unit_id].status is WorkUnitStatus.ACTIVE
                and states[unit_id].active_attempt_id != attempt.attempt_id
            ):
                raise InvalidDomainValue("an active WorkUnit must identify its one live Attempt")
        if self.status is ProgramStatus.COMPLETED and any(
            state.status is not WorkUnitStatus.SATISFIED for state in self.work_unit_states
        ):
            raise InvalidDomainValue("completion requires satisfaction of every current obligation")
        if self.status is ProgramStatus.CANCELLED and any(
            item.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING) for item in self.attempts
        ):
            raise InvalidDomainValue("a cancelled Program cannot retain live Attempts")

    def apply(self, command: DomainCommandType) -> Program:
        if type(self) is not Program:
            raise InvalidDomainValue("Program values cannot be subclassed")
        actions = {
            ActivateProgram: DomainAction.ACTIVATE_PROGRAM,
            PauseProgram: DomainAction.PAUSE_PROGRAM,
            ResumeProgram: DomainAction.RESUME_PROGRAM,
            CancelProgram: DomainAction.CANCEL_PROGRAM,
            CancelWorkUnit: DomainAction.CANCEL_WORK_UNIT,
            PrepareAttempt: DomainAction.PREPARE_ATTEMPT,
            StartAttempt: DomainAction.START_ATTEMPT,
            FinishAttempt: DomainAction.FINISH_ATTEMPT,
            FailAttempt: DomainAction.FAIL_ATTEMPT,
            CancelAttempt: DomainAction.CANCEL_ATTEMPT,
            SatisfyWorkUnit: DomainAction.SATISFY_WORK_UNIT,
            AmendProgramSpec: DomainAction.AMEND_SPEC,
        }
        action = actions.get(type(command))
        if action is None:
            raise InvalidDomainValue(f"unsupported domain command: {type(command).__name__}")
        if command.expected_revision != self.revision:
            raise StaleRevision("expected aggregate revision does not match current revision")
        self._authorize(action, command.actor_id)
        order = self.revision + 1
        attempts = self.attempts
        satisfactions = self.satisfactions
        cancellations = self.cancellations
        attempt_cancellations = self.attempt_cancellations
        spec = self.spec
        spec_history = self.spec_history
        status = self.status

        def cancel_live(ids: frozenset[str], reason: str) -> None:
            nonlocal attempts, attempt_cancellations
            cancelled_ids = {
                item.attempt_id
                for item in attempts
                if item.attempt_id in ids and item.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
            }
            attempts = tuple(
                Attempt(item.spec, AttemptStatus.CANCELLED, item.actor_id) if item.attempt_id in cancelled_ids else item
                for item in attempts
            )
            attempt_cancellations = (
                *attempt_cancellations,
                *(
                    AttemptCancellation(attempt_id, command.actor_id, reason, order)
                    for attempt_id in sorted(cancelled_ids)
                ),
            )

        if type(command) is ActivateProgram:
            if status is not ProgramStatus.DRAFT:
                raise IllegalTransition("only a draft Program may activate")
            status = ProgramStatus.ACTIVE
        elif type(command) is PauseProgram:
            if status is not ProgramStatus.ACTIVE:
                raise IllegalTransition("only an active Program may pause")
            status = ProgramStatus.PAUSED
        elif type(command) is ResumeProgram:
            if status is not ProgramStatus.PAUSED:
                raise IllegalTransition("only a paused Program may resume")
            status = ProgramStatus.ACTIVE
        elif type(command) is CancelProgram:
            if status not in (ProgramStatus.DRAFT, ProgramStatus.ACTIVE, ProgramStatus.PAUSED):
                raise IllegalTransition("Program cannot be abandoned from this state")
            cancel_live(frozenset(item.attempt_id for item in attempts), "Program abandonment")
            status = ProgramStatus.CANCELLED
        elif type(command) is CancelWorkUnit:
            if status not in (ProgramStatus.ACTIVE, ProgramStatus.PAUSED):
                raise IllegalTransition("WorkUnit abandonment requires an active or paused Program")
            unit = spec.work_unit(command.work_unit_id)
            affected = {unit.id}
            for descendant_id in spec.topological_order:
                if any(dep in affected for dep in spec.work_unit(descendant_id).dependencies):
                    affected.add(descendant_id)
            if self.state(unit.id).status is WorkUnitStatus.CANCELLED:
                raise IllegalTransition("cannot abandon an already cancelled obligation")
            cancellations = (
                *cancellations,
                WorkUnitCancellation(
                    unit.id,
                    spec.work_unit_applicability_fingerprint(unit.id),
                    spec.revision,
                    spec.digest,
                    command.actor_id,
                    command.reason,
                    order,
                ),
            )
            cancel_live(
                frozenset(item.attempt_id for item in attempts if item.spec.work_unit_id in affected),
                "WorkUnit abandonment",
            )
        elif type(command) is PrepareAttempt:
            requested = command.attempt
            if requested.program_id != self.program_id:
                raise AuthorityViolation("Attempt belongs to a foreign Program")
            if requested.spec_revision != spec.revision or requested.spec_digest != spec.digest:
                raise StaleRevision("Attempt must pin the current ProgramSpec")
            unit = spec.work_unit(requested.work_unit_id)
            if status is not ProgramStatus.ACTIVE or self.state(unit.id).status is not WorkUnitStatus.READY:
                raise IllegalTransition("Attempt preparation requires an active, ready WorkUnit")
            if any(item.attempt_id == requested.attempt_id for item in attempts):
                raise DuplicateAttempt(f"Attempt {requested.attempt_id!r} already exists")
            if any(
                item.spec.work_unit_id == unit.id and item.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING)
                for item in attempts
            ):
                raise IllegalTransition("a WorkUnit already has an outstanding Attempt")
            if requested.effective_inputs != self.resolved_inputs(unit.id):
                raise MissingInput("Attempt bindings must exactly match current approved references and provenance")
            if len(attempts) >= min(spec.budget.max_attempts, spec.authority.max_attempts):
                raise BudgetExceeded("attempt admission limit exceeded")
            if sum(item.status in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING) for item in attempts) >= (
                spec.budget.max_active_attempts
            ):
                raise BudgetExceeded("active Attempt admission limit exceeded")
            attempts = (*attempts, Attempt(requested, actor_id=command.actor_id))
        elif isinstance(command, (StartAttempt, FinishAttempt, FailAttempt, CancelAttempt)):
            current = self.attempt(command.attempt_id)
            if type(command) is StartAttempt:
                work_state = self.state(current.spec.work_unit_id)
                if (
                    status is not ProgramStatus.ACTIVE
                    or current.status is not AttemptStatus.PREPARED
                    or work_state.active_attempt_id != command.attempt_id
                ):
                    raise IllegalTransition("only a prepared Attempt in an active Program may start")
                replacement = AttemptStatus.EXECUTING
            elif type(command) is FinishAttempt:
                if status not in (ProgramStatus.ACTIVE, ProgramStatus.PAUSED, ProgramStatus.COMPLETED) or (
                    current.status is not AttemptStatus.EXECUTING
                ):
                    raise IllegalTransition("only an executing Attempt may finish")
                replacement = AttemptStatus.FINISHED
            else:
                if status not in (
                    ProgramStatus.ACTIVE,
                    ProgramStatus.PAUSED,
                    ProgramStatus.COMPLETED,
                ) or current.status not in (
                    AttemptStatus.PREPARED,
                    AttemptStatus.EXECUTING,
                ):
                    raise IllegalTransition("only a live Attempt may fail or cancel")
                replacement = AttemptStatus.FAILED if type(command) is FailAttempt else AttemptStatus.CANCELLED
            if replacement is AttemptStatus.CANCELLED:
                cancel_live(frozenset({command.attempt_id}), "Attempt cancellation")
            else:
                attempts = tuple(
                    Attempt(item.spec, replacement, item.actor_id) if item.attempt_id == command.attempt_id else item
                    for item in attempts
                )
        elif type(command) is SatisfyWorkUnit:
            fact = command.satisfaction
            if status not in (ProgramStatus.ACTIVE, ProgramStatus.PAUSED, ProgramStatus.COMPLETED):
                raise IllegalTransition("satisfaction requires an activated, non-abandoned Program")
            if fact.issuer_id != command.actor_id:
                raise AuthorityViolation("satisfaction issuer and trusted command actor must match")
            if fact.program_id != self.program_id:
                raise AuthorityViolation("satisfaction belongs to a foreign Program")
            historical_spec = next(
                (
                    item
                    for item in spec_history
                    if (item.revision, item.digest) == (fact.spec_revision, fact.spec_digest)
                ),
                None,
            )
            if historical_spec is None:
                raise StaleRevision("satisfaction must cite an approved historical ProgramSpec")
            historical_unit = historical_spec.work_unit(fact.work_unit_id)
            if fact.work_unit_fingerprint != historical_spec.work_unit_applicability_fingerprint(historical_unit.id):
                raise StaleRevision("satisfaction does not pin its historical WorkUnit intent")
            if fact.record_order != 0 or any(item.reference_id == fact.reference_id for item in satisfactions):
                raise IllegalTransition("satisfaction has already been recorded")
            source = self.attempt(fact.source_attempt_id)
            if source.status is not AttemptStatus.FINISHED:
                raise IllegalTransition("satisfaction requires a finished source Attempt")
            if (source.spec.program_id, source.spec.work_unit_id) != (self.program_id, historical_unit.id):
                raise AuthorityViolation("satisfaction source Attempt has a foreign subject")
            if (source.spec.spec_revision, source.spec.spec_digest) != (fact.spec_revision, fact.spec_digest):
                raise StaleRevision("satisfaction must cite its source Attempt's historical spec")
            if {item.name for item in fact.output_bindings} != set(historical_unit.outputs):
                raise MissingInput("satisfaction must bind exactly the declared outputs")
            satisfactions = (
                *satisfactions,
                TrustedSatisfaction(
                    fact.reference_id,
                    fact.program_id,
                    fact.work_unit_id,
                    fact.spec_revision,
                    fact.spec_digest,
                    fact.work_unit_fingerprint,
                    fact.issuer_id,
                    fact.source_attempt_id,
                    fact.output_bindings,
                    order,
                ),
            )
        elif type(command) is AmendProgramSpec:
            if status is ProgramStatus.CANCELLED:
                raise IllegalTransition("an abandoned Program cannot be amended")
            spec = spec.amend(command.amendment)
            if spec is self.spec:
                return self
            spec_history = (*spec_history, spec)
            # Repeatedly withdraw live subjects invalidated by intent, inputs, graph or operational authority.
            # A predecessor's withdrawal may in turn invalidate a dependent Attempt's resolved bindings.
            while True:
                projected = _project(spec, attempts, satisfactions, cancellations)
                by_state = {item.work_unit_id: item for item in projected}
                by_fact = {item.reference_id: item for item in satisfactions}
                invalid: set[str] = set()
                for attempt in attempts:
                    if attempt.status not in (AttemptStatus.PREPARED, AttemptStatus.EXECUTING):
                        continue
                    unit_id = attempt.spec.work_unit_id
                    try:
                        unit = spec.work_unit(unit_id)
                        source_spec = next(
                            item
                            for item in spec_history
                            if (item.revision, item.digest) == (attempt.spec.spec_revision, attempt.spec.spec_digest)
                        )
                    except (MissingReference, StopIteration):
                        invalid.add(attempt.attempt_id)
                        continue
                    inputs = _resolved_inputs(spec, unit, by_state, by_fact)
                    required = (
                        DomainAction.START_ATTEMPT
                        if attempt.status is AttemptStatus.PREPARED
                        else DomainAction.FINISH_ATTEMPT
                    )
                    if (
                        source_spec.work_unit_applicability_fingerprint(unit_id)
                        != spec.work_unit_applicability_fingerprint(unit_id)
                        or inputs is None
                        or not _same_subject(attempt.spec.effective_inputs, inputs)
                        or required not in spec.authority.allowed_actions
                        or (
                            attempt.actor_id != spec.authority.owner_id
                            and attempt.actor_id not in spec.authority.delegated_actor_ids
                        )
                        or by_state[unit_id].status is WorkUnitStatus.CANCELLED
                    ):
                        invalid.add(attempt.attempt_id)
                if not invalid:
                    break
                cancel_live(frozenset(invalid), "amendment withdrew Attempt subject or operational authority")
        else:
            raise InvalidDomainValue("unsupported domain command")

        return self._assemble(spec, status, attempts, satisfactions, cancellations, attempt_cancellations, spec_history)

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


for _sealed_type in (
    BudgetPolicy,
    AuthorityEnvelope,
    PolicyReference,
    InputBinding,
    WorkUnit,
    ProgramSpec,
    AttemptSpec,
    Attempt,
    TrustedSatisfaction,
    WorkUnitCancellation,
    AttemptCancellation,
    ProgramConclusion,
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
    "AttemptCancellation",
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
    "PolicyReference",
    "PrepareAttempt",
    "Program",
    "ProgramConclusion",
    "ProgramSpec",
    "ProgramStatus",
    "ResumeProgram",
    "SatisfyWorkUnit",
    "SpecAmendment",
    "StaleRevision",
    "StartAttempt",
    "TrustedSatisfaction",
    "WorkUnit",
    "WorkUnitCancellation",
    "WorkUnitState",
    "WorkUnitStatus",
]
