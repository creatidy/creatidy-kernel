# SPDX-License-Identifier: Apache-2.0
"""Private, versioned codecs for durable K1 facts and command inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import cast

from creatidy_kernel.core.domain import (
    ActivateProgram,
    AmendProgramSpec,
    Attempt,
    AttemptCancellation,
    AttemptSpec,
    AttemptStatus,
    AuthorityEnvelope,
    BudgetPolicy,
    CancelAttempt,
    CancelProgram,
    CancelWorkUnit,
    DomainAction,
    DomainCommandType,
    DomainError,
    FailAttempt,
    FinishAttempt,
    InputBinding,
    PauseProgram,
    PolicyReference,
    PrepareAttempt,
    Program,
    ProgramConclusion,
    ProgramSpec,
    ProgramStatus,
    ResumeProgram,
    SatisfyWorkUnit,
    SpecAmendment,
    StartAttempt,
    TrustedSatisfaction,
    WorkUnit,
    WorkUnitCancellation,
    WorkUnitState,
    WorkUnitStatus,
)

CODEC_VERSION = 1


class RecordCodecError(ValueError):
    """A durable record is malformed, unsupported, or violates K1 invariants."""


def canonical_json(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RecordCodecError("value cannot be represented as canonical JSON") from error


def digest_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    result = _object(value, label)
    if set(result) != fields:
        raise RecordCodecError(f"{label} has missing or unknown fields")
    return result


def _object(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise RecordCodecError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise RecordCodecError(f"{label} must be an array")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if type(value) is not str:
        raise RecordCodecError(f"{label} must be text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise RecordCodecError(f"{label} must be an integer")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    return tuple(_text(item, label) for item in _array(value, label))


def _versioned(raw: str, label: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RecordCodecError(f"{label} is not valid JSON") from error
    value = _mapping(decoded, {"schema_version", "value"}, label)
    if _integer(value["schema_version"], f"{label}.schema_version") != CODEC_VERSION:
        raise RecordCodecError(f"unsupported {label} schema version")
    return _object(value["value"], f"{label}.value")


def _policy_data(policy: PolicyReference) -> dict[str, str]:
    return policy.payload()


def _policy_from(data: object) -> PolicyReference:
    value = _mapping(data, {"policy_id", "version", "digest"}, "policy reference")
    return PolicyReference(
        _text(value["policy_id"], "policy_id"),
        _text(value["version"], "version"),
        _text(value["digest"], "digest"),
    )


def _binding_from(data: object) -> InputBinding:
    value = _mapping(
        data,
        {"name", "reference", "source_kind", "source_work_unit_id", "satisfaction_id", "output_name"},
        "input binding",
    )
    return InputBinding(
        name=_text(value["name"], "input name"),
        reference=_text(value["reference"], "input reference"),
        source_kind=_text(value["source_kind"], "input source_kind"),
        source_work_unit_id=_optional_text(value["source_work_unit_id"], "source_work_unit_id"),
        satisfaction_id=_optional_text(value["satisfaction_id"], "satisfaction_id"),
        output_name=_optional_text(value["output_name"], "output_name"),
    )


def _work_unit_from(data: object) -> WorkUnit:
    value = _mapping(
        data,
        {
            "work_unit_id",
            "obligation",
            "dependencies",
            "required_inputs",
            "outputs",
            "acceptance_criteria",
            "acceptance_policy_reference",
        },
        "WorkUnit",
    )
    policy = value["acceptance_policy_reference"]
    return WorkUnit(
        work_unit_id=_text(value["work_unit_id"], "work_unit_id"),
        obligation=_text(value["obligation"], "obligation"),
        dependencies=_strings(value["dependencies"], "dependencies"),
        required_inputs=frozenset(_strings(value["required_inputs"], "required_inputs")),
        outputs=frozenset(_strings(value["outputs"], "outputs")),
        acceptance_criteria=_strings(value["acceptance_criteria"], "acceptance_criteria"),
        acceptance_policy_reference=None if policy is None else _policy_from(policy),
    )


def _authority_from(data: object) -> AuthorityEnvelope:
    value = _mapping(
        data,
        {"owner_id", "allowed_actions", "max_attempts", "trusted_satisfaction_issuers", "delegated_actor_ids"},
        "authority envelope",
    )
    try:
        actions = frozenset(DomainAction(item) for item in _strings(value["allowed_actions"], "allowed_actions"))
    except ValueError as error:
        raise RecordCodecError("authority envelope contains an unknown action") from error
    return AuthorityEnvelope(
        owner_id=_text(value["owner_id"], "owner_id"),
        allowed_actions=actions,
        max_attempts=_integer(value["max_attempts"], "authority.max_attempts"),
        trusted_satisfaction_issuers=frozenset(
            _strings(value["trusted_satisfaction_issuers"], "trusted_satisfaction_issuers")
        ),
        delegated_actor_ids=frozenset(_strings(value["delegated_actor_ids"], "delegated_actor_ids")),
    )


def _spec_from(data: object) -> ProgramSpec:
    value = _mapping(
        data,
        {
            "program_id",
            "objective",
            "acceptance_criteria",
            "policy_references",
            "work_units",
            "initial_inputs",
            "budget",
            "authority",
            "revision",
            "parent_digest",
        },
        "ProgramSpec",
    )
    budget_data = _mapping(value["budget"], {"max_attempts", "max_active_attempts"}, "budget")
    return ProgramSpec(
        program_id=_text(value["program_id"], "program_id"),
        objective=_text(value["objective"], "objective"),
        acceptance_criteria=_strings(value["acceptance_criteria"], "acceptance_criteria"),
        policy_references=tuple(_policy_from(item) for item in _array(value["policy_references"], "policy_references")),
        work_units=tuple(_work_unit_from(item) for item in _array(value["work_units"], "work_units")),
        initial_inputs=tuple(_binding_from(item) for item in _array(value["initial_inputs"], "initial_inputs")),
        budget=BudgetPolicy(
            max_attempts=_integer(budget_data["max_attempts"], "budget.max_attempts"),
            max_active_attempts=_integer(budget_data["max_active_attempts"], "budget.max_active_attempts"),
        ),
        authority=_authority_from(value["authority"]),
        revision=_integer(value["revision"], "spec revision"),
        parent_digest=_optional_text(value["parent_digest"], "parent_digest"),
    )


def _attempt_spec_data(spec: AttemptSpec) -> dict[str, object]:
    return {
        "attempt_id": spec.attempt_id,
        "program_id": spec.program_id,
        "work_unit_id": spec.work_unit_id,
        "spec_revision": spec.spec_revision,
        "spec_digest": spec.spec_digest,
        "effective_inputs": [item.payload() for item in spec.effective_inputs],
        "allocation_reference": spec.allocation_reference,
        "agent_definition_reference": spec.agent_definition_reference,
        "workspace_reference": spec.workspace_reference,
        "context_reference": spec.context_reference,
    }


def _attempt_spec_from(data: object) -> AttemptSpec:
    value = _mapping(
        data,
        {
            "attempt_id",
            "program_id",
            "work_unit_id",
            "spec_revision",
            "spec_digest",
            "effective_inputs",
            "allocation_reference",
            "agent_definition_reference",
            "workspace_reference",
            "context_reference",
        },
        "AttemptSpec",
    )
    return AttemptSpec(
        attempt_id=_text(value["attempt_id"], "attempt_id"),
        program_id=_text(value["program_id"], "program_id"),
        work_unit_id=_text(value["work_unit_id"], "work_unit_id"),
        spec_revision=_integer(value["spec_revision"], "Attempt spec_revision"),
        spec_digest=_text(value["spec_digest"], "Attempt spec_digest"),
        effective_inputs=tuple(
            _binding_from(item) for item in _array(value["effective_inputs"], "Attempt effective_inputs")
        ),
        allocation_reference=_optional_text(value["allocation_reference"], "allocation_reference"),
        agent_definition_reference=_optional_text(value["agent_definition_reference"], "agent_definition_reference"),
        workspace_reference=_optional_text(value["workspace_reference"], "workspace_reference"),
        context_reference=_optional_text(value["context_reference"], "context_reference"),
    )


def attempt_projection_json(attempt: Attempt) -> str:
    return canonical_json(
        {
            "schema_version": CODEC_VERSION,
            "value": {
                "spec": _attempt_spec_data(attempt.spec),
                "status": attempt.status.value,
                "actor_id": attempt.actor_id,
            },
        }
    )


def _satisfaction_data(fact: TrustedSatisfaction) -> dict[str, object]:
    return {
        "reference_id": fact.reference_id,
        "program_id": fact.program_id,
        "work_unit_id": fact.work_unit_id,
        "spec_revision": fact.spec_revision,
        "spec_digest": fact.spec_digest,
        "work_unit_fingerprint": fact.work_unit_fingerprint,
        "issuer_id": fact.issuer_id,
        "source_attempt_id": fact.source_attempt_id,
        "output_bindings": [item.payload() for item in fact.output_bindings],
        "record_order": fact.record_order,
    }


def _satisfaction_from(data: object) -> TrustedSatisfaction:
    value = _mapping(
        data,
        {
            "reference_id",
            "program_id",
            "work_unit_id",
            "spec_revision",
            "spec_digest",
            "work_unit_fingerprint",
            "issuer_id",
            "source_attempt_id",
            "output_bindings",
            "record_order",
        },
        "TrustedSatisfaction",
    )
    return TrustedSatisfaction(
        reference_id=_text(value["reference_id"], "satisfaction reference_id"),
        program_id=_text(value["program_id"], "satisfaction program_id"),
        work_unit_id=_text(value["work_unit_id"], "satisfaction work_unit_id"),
        spec_revision=_integer(value["spec_revision"], "satisfaction spec_revision"),
        spec_digest=_text(value["spec_digest"], "satisfaction spec_digest"),
        work_unit_fingerprint=_text(value["work_unit_fingerprint"], "satisfaction work_unit_fingerprint"),
        issuer_id=_text(value["issuer_id"], "satisfaction issuer_id"),
        source_attempt_id=_text(value["source_attempt_id"], "satisfaction source_attempt_id"),
        output_bindings=tuple(
            _binding_from(item) for item in _array(value["output_bindings"], "satisfaction output_bindings")
        ),
        record_order=_integer(value["record_order"], "satisfaction record_order"),
    )


def _cancellation_from(data: object) -> WorkUnitCancellation:
    value = _mapping(
        data,
        {
            "work_unit_id",
            "work_unit_fingerprint",
            "spec_revision",
            "spec_digest",
            "actor_id",
            "reason",
            "record_order",
        },
        "WorkUnitCancellation",
    )
    return WorkUnitCancellation(
        work_unit_id=_text(value["work_unit_id"], "cancellation work_unit_id"),
        work_unit_fingerprint=_text(value["work_unit_fingerprint"], "cancellation work_unit_fingerprint"),
        spec_revision=_integer(value["spec_revision"], "cancellation spec_revision"),
        spec_digest=_text(value["spec_digest"], "cancellation spec_digest"),
        actor_id=_text(value["actor_id"], "cancellation actor_id"),
        reason=_text(value["reason"], "cancellation reason"),
        record_order=_integer(value["record_order"], "cancellation record_order"),
    )


def _attempt_cancellation_from(data: object) -> AttemptCancellation:
    value = _mapping(data, {"attempt_id", "actor_id", "reason", "record_order"}, "AttemptCancellation")
    return AttemptCancellation(
        attempt_id=_text(value["attempt_id"], "AttemptCancellation attempt_id"),
        actor_id=_text(value["actor_id"], "AttemptCancellation actor_id"),
        reason=_text(value["reason"], "AttemptCancellation reason"),
        record_order=_integer(value["record_order"], "AttemptCancellation record_order"),
    )


def _conclusion_from(data: object) -> ProgramConclusion:
    value = _mapping(data, {"status", "spec_revision", "spec_digest", "record_order"}, "ProgramConclusion")
    try:
        status = ProgramStatus(_text(value["status"], "conclusion status"))
    except ValueError as error:
        raise RecordCodecError("ProgramConclusion has an unknown status") from error
    return ProgramConclusion(
        status=status,
        spec_revision=_integer(value["spec_revision"], "conclusion spec_revision"),
        spec_digest=_text(value["spec_digest"], "conclusion spec_digest"),
        record_order=_integer(value["record_order"], "conclusion record_order"),
    )


def _work_unit_state_from(data: object) -> WorkUnitState:
    value = _mapping(data, {"work_unit_id", "status", "active_attempt_id", "satisfaction_id"}, "WorkUnitState")
    try:
        status = WorkUnitStatus(_text(value["status"], "WorkUnitState status"))
    except ValueError as error:
        raise RecordCodecError("WorkUnitState has an unknown status") from error
    return WorkUnitState(
        work_unit_id=_text(value["work_unit_id"], "WorkUnitState work_unit_id"),
        status=status,
        active_attempt_id=_optional_text(value["active_attempt_id"], "active_attempt_id"),
        satisfaction_id=_optional_text(value["satisfaction_id"], "satisfaction_id"),
    )


def _program_value(program: Program) -> dict[str, object]:
    if type(program) is not Program:
        raise RecordCodecError("only exact K1 Program values can be persisted")
    _validate_program(program)
    return {
        "program_id": program.program_id,
        "status": program.status.value,
        "revision": program.revision,
        "spec": program.spec.payload(),
        "work_unit_states": [
            {
                "work_unit_id": state.work_unit_id,
                "status": state.status.value,
                "active_attempt_id": state.active_attempt_id,
                "satisfaction_id": state.satisfaction_id,
            }
            for state in program.work_unit_states
        ],
        "attempts": [
            {"spec": _attempt_spec_data(attempt.spec), "status": attempt.status.value, "actor_id": attempt.actor_id}
            for attempt in program.attempts
        ],
        "satisfactions": [_satisfaction_data(item) for item in program.satisfactions],
        "cancellations": [
            {
                "work_unit_id": item.work_unit_id,
                "work_unit_fingerprint": item.work_unit_fingerprint,
                "spec_revision": item.spec_revision,
                "spec_digest": item.spec_digest,
                "actor_id": item.actor_id,
                "reason": item.reason,
                "record_order": item.record_order,
            }
            for item in program.cancellations
        ],
        "attempt_cancellations": [
            {
                "attempt_id": item.attempt_id,
                "actor_id": item.actor_id,
                "reason": item.reason,
                "record_order": item.record_order,
            }
            for item in program.attempt_cancellations
        ],
        "conclusions": [
            {
                "status": item.status.value,
                "spec_revision": item.spec_revision,
                "spec_digest": item.spec_digest,
                "record_order": item.record_order,
            }
            for item in program.conclusions
        ],
        "spec_history": [item.payload() for item in program.spec_history],
    }


def program_json(program: Program) -> str:
    return canonical_json({"schema_version": CODEC_VERSION, "value": _program_value(program)})


def program_from_json(raw: str) -> Program:
    value = _versioned(raw, "Program record")
    if set(value) != {
        "program_id",
        "status",
        "revision",
        "spec",
        "work_unit_states",
        "attempts",
        "satisfactions",
        "cancellations",
        "attempt_cancellations",
        "conclusions",
        "spec_history",
    }:
        raise RecordCodecError("Program record has missing or unknown fields")
    try:
        try:
            status = ProgramStatus(_text(value["status"], "Program status"))
        except ValueError as error:
            raise RecordCodecError("Program has an unknown status") from error
        attempts: list[Attempt] = []
        for item in _array(value["attempts"], "attempts"):
            attempt_value = _mapping(item, {"spec", "status", "actor_id"}, "Attempt")
            try:
                attempt_status = AttemptStatus(_text(attempt_value["status"], "Attempt status"))
            except ValueError as error:
                raise RecordCodecError("Attempt has an unknown status") from error
            attempts.append(
                Attempt(
                    _attempt_spec_from(attempt_value["spec"]),
                    attempt_status,
                    _text(attempt_value["actor_id"], "Attempt actor_id"),
                )
            )
        program = object.__new__(Program)
        fields: dict[str, object] = {
            "spec": _spec_from(value["spec"]),
            "status": status,
            "revision": _integer(value["revision"], "Program revision"),
            "work_unit_states": tuple(
                _work_unit_state_from(item) for item in _array(value["work_unit_states"], "work_unit_states")
            ),
            "attempts": tuple(attempts),
            "satisfactions": tuple(
                _satisfaction_from(item) for item in _array(value["satisfactions"], "satisfactions")
            ),
            "cancellations": tuple(
                _cancellation_from(item) for item in _array(value["cancellations"], "cancellations")
            ),
            "attempt_cancellations": tuple(
                _attempt_cancellation_from(item)
                for item in _array(value["attempt_cancellations"], "attempt_cancellations")
            ),
            "conclusions": tuple(_conclusion_from(item) for item in _array(value["conclusions"], "conclusions")),
            "spec_history": tuple(_spec_from(item) for item in _array(value["spec_history"], "spec_history")),
        }
        for name, item in fields.items():
            object.__setattr__(program, name, item)
        if program.program_id != _text(value["program_id"], "Program program_id"):
            raise RecordCodecError("Program ID differs from its current ProgramSpec")
        _validate_program(program)
        return program
    except RecordCodecError:
        raise
    except (DomainError, TypeError, ValueError, KeyError, IndexError) as error:
        raise RecordCodecError("Program record violates K1 invariants") from error


def _validate_program(program: Program) -> None:
    validator_name = "_validate"
    validator = cast(Callable[[], None], getattr(program, validator_name))
    validator()


def create_input_json(spec: ProgramSpec) -> str:
    return canonical_json(
        {
            "schema_version": CODEC_VERSION,
            "command_type": "create_program",
            "program_id": spec.program_id,
            "actor_id": spec.authority.owner_id,
            "spec": spec.payload(),
        }
    )


def command_input_json(program_id: str, command: DomainCommandType) -> str:
    common: dict[str, object] = {
        "schema_version": CODEC_VERSION,
        "program_id": program_id,
        "expected_revision": command.expected_revision,
        "actor_id": command.actor_id,
    }
    if type(command) is ActivateProgram:
        command_type, parameters = "activate_program", {}
    elif type(command) is PauseProgram:
        command_type, parameters = "pause_program", {}
    elif type(command) is ResumeProgram:
        command_type, parameters = "resume_program", {}
    elif type(command) is CancelProgram:
        command_type, parameters = "cancel_program", {}
    elif type(command) is CancelWorkUnit:
        command_type, parameters = (
            "cancel_work_unit",
            {
                "work_unit_id": command.work_unit_id,
                "reason": command.reason,
            },
        )
    elif type(command) is PrepareAttempt:
        command_type, parameters = "prepare_attempt", {"attempt": _attempt_spec_data(command.attempt)}
    elif type(command) is StartAttempt:
        command_type, parameters = "start_attempt", {"attempt_id": command.attempt_id}
    elif type(command) is FinishAttempt:
        command_type, parameters = "finish_attempt", {"attempt_id": command.attempt_id}
    elif type(command) is FailAttempt:
        command_type, parameters = "fail_attempt", {"attempt_id": command.attempt_id}
    elif type(command) is CancelAttempt:
        command_type, parameters = "cancel_attempt", {"attempt_id": command.attempt_id}
    elif type(command) is SatisfyWorkUnit:
        command_type, parameters = "satisfy_work_unit", {"satisfaction": _satisfaction_data(command.satisfaction)}
    elif type(command) is AmendProgramSpec:
        command_type, parameters = "amend_spec", {"amendment": _amendment_data(command.amendment)}
    else:
        raise RecordCodecError("unsupported K1 domain command type")
    return canonical_json({**common, "command_type": command_type, "parameters": parameters})


def _amendment_data(amendment: SpecAmendment) -> dict[str, object]:
    return {
        "expected_revision": amendment.expected_revision,
        "objective": amendment.objective,
        "work_units": None if amendment.work_units is None else [item.payload() for item in amendment.work_units],
        "initial_inputs": None
        if amendment.initial_inputs is None
        else [item.payload() for item in amendment.initial_inputs],
        "budget": None
        if amendment.budget is None
        else {
            "max_attempts": amendment.budget.max_attempts,
            "max_active_attempts": amendment.budget.max_active_attempts,
        },
        "authority": None if amendment.authority is None else amendment.authority.payload(),
        "reason": amendment.reason,
        "acceptance_criteria": amendment.acceptance_criteria,
        "policy_references": None
        if amendment.policy_references is None
        else [_policy_data(item) for item in amendment.policy_references],
    }


def _amendment_from(data: object) -> SpecAmendment:
    value = _mapping(
        data,
        {
            "expected_revision",
            "objective",
            "work_units",
            "initial_inputs",
            "budget",
            "authority",
            "reason",
            "acceptance_criteria",
            "policy_references",
        },
        "SpecAmendment",
    )
    work_units = value["work_units"]
    initial_inputs = value["initial_inputs"]
    budget = value["budget"]
    authority = value["authority"]
    acceptance_criteria = value["acceptance_criteria"]
    policies = value["policy_references"]
    budget_value = None
    if budget is not None:
        budget_fields = _mapping(budget, {"max_attempts", "max_active_attempts"}, "amended budget")
        budget_value = BudgetPolicy(
            max_attempts=_integer(budget_fields["max_attempts"], "amended max_attempts"),
            max_active_attempts=_integer(budget_fields["max_active_attempts"], "amended max_active_attempts"),
        )
    return SpecAmendment(
        expected_revision=_integer(value["expected_revision"], "amendment expected_revision"),
        objective=_optional_text(value["objective"], "amendment objective"),
        work_units=None
        if work_units is None
        else tuple(_work_unit_from(item) for item in _array(work_units, "amendment work_units")),
        initial_inputs=None
        if initial_inputs is None
        else tuple(_binding_from(item) for item in _array(initial_inputs, "amendment initial_inputs")),
        budget=budget_value,
        authority=None if authority is None else _authority_from(authority),
        reason=_text(value["reason"], "amendment reason"),
        acceptance_criteria=None
        if acceptance_criteria is None
        else _strings(acceptance_criteria, "amendment acceptance_criteria"),
        policy_references=None
        if policies is None
        else tuple(_policy_from(item) for item in _array(policies, "amendment policy_references")),
    )


def _command_from(value: dict[str, object]) -> DomainCommandType:
    command_type = _text(value["command_type"], "command_type")
    program_id = _text(value["program_id"], "program_id")
    if not program_id.strip():
        raise RecordCodecError("command program_id must be nonempty")
    expected_revision = _integer(value["expected_revision"], "command expected_revision")
    actor_id = _text(value["actor_id"], "command actor_id")
    parameters = _object(value["parameters"], "command parameters")

    if command_type == "activate_program":
        _mapping(parameters, set(), "activate_program parameters")
        return ActivateProgram(expected_revision, actor_id)
    if command_type == "pause_program":
        _mapping(parameters, set(), "pause_program parameters")
        return PauseProgram(expected_revision, actor_id)
    if command_type == "resume_program":
        _mapping(parameters, set(), "resume_program parameters")
        return ResumeProgram(expected_revision, actor_id)
    if command_type == "cancel_program":
        _mapping(parameters, set(), "cancel_program parameters")
        return CancelProgram(expected_revision, actor_id)
    if command_type == "cancel_work_unit":
        params = _mapping(parameters, {"work_unit_id", "reason"}, "cancel_work_unit parameters")
        return CancelWorkUnit(
            expected_revision,
            actor_id,
            _text(params["work_unit_id"], "work_unit_id"),
            _text(params["reason"], "cancellation reason"),
        )
    if command_type == "prepare_attempt":
        params = _mapping(parameters, {"attempt"}, "prepare_attempt parameters")
        return PrepareAttempt(expected_revision, actor_id, _attempt_spec_from(params["attempt"]))
    if command_type in {"start_attempt", "finish_attempt", "fail_attempt", "cancel_attempt"}:
        params = _mapping(parameters, {"attempt_id"}, f"{command_type} parameters")
        attempt_id = _text(params["attempt_id"], "attempt_id")
        if command_type == "start_attempt":
            return StartAttempt(expected_revision, actor_id, attempt_id)
        if command_type == "finish_attempt":
            return FinishAttempt(expected_revision, actor_id, attempt_id)
        if command_type == "fail_attempt":
            return FailAttempt(expected_revision, actor_id, attempt_id)
        return CancelAttempt(expected_revision, actor_id, attempt_id)
    if command_type == "satisfy_work_unit":
        params = _mapping(parameters, {"satisfaction"}, "satisfy_work_unit parameters")
        return SatisfyWorkUnit(expected_revision, actor_id, _satisfaction_from(params["satisfaction"]))
    if command_type == "amend_spec":
        params = _mapping(parameters, {"amendment"}, "amend_spec parameters")
        return AmendProgramSpec(expected_revision, actor_id, _amendment_from(params["amendment"]))
    raise RecordCodecError("history has an unknown K1 command type")


def validate_history_input(raw: str, digest: str, program_id: str, command_type: str, actor_id: str) -> None:
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RecordCodecError("history command input is not valid JSON") from error
    value = _mapping(
        decoded,
        {
            "schema_version",
            "program_id",
            "expected_revision",
            "actor_id",
            "command_type",
            "parameters",
        },
        "history command input",
    )
    if _integer(value.get("schema_version"), "command schema_version") != CODEC_VERSION:
        raise RecordCodecError("unsupported history command schema version")
    if (
        value.get("program_id") != program_id
        or value.get("command_type") != command_type
        or value.get("actor_id") != actor_id
    ):
        raise RecordCodecError("history command metadata does not match its record envelope")
    _integer(value["expected_revision"], "command expected_revision")
    _object(value["parameters"], "command parameters")
    if command_type not in {
        "activate_program",
        "pause_program",
        "resume_program",
        "cancel_program",
        "cancel_work_unit",
        "prepare_attempt",
        "start_attempt",
        "finish_attempt",
        "fail_attempt",
        "cancel_attempt",
        "satisfy_work_unit",
        "amend_spec",
    }:
        raise RecordCodecError("history has an unknown K1 command type")
    if digest_json(value) != digest:
        raise RecordCodecError("history command digest does not match its canonical input")
    if raw != canonical_json(value):
        raise RecordCodecError("history command input is not canonically encoded")
    try:
        _command_from(value)
    except RecordCodecError:
        raise
    except (DomainError, TypeError, ValueError) as error:
        raise RecordCodecError("history command input violates K1 value constraints") from error


def validate_create_input(raw: str, digest: str, program_id: str, actor_id: str) -> None:
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RecordCodecError("history creation input is not valid JSON") from error
    value = _mapping(
        decoded,
        {"schema_version", "command_type", "program_id", "actor_id", "spec"},
        "history creation input",
    )
    if (
        _integer(value["schema_version"], "create schema_version") != CODEC_VERSION
        or value["command_type"] != "create_program"
        or value["program_id"] != program_id
        or value["actor_id"] != actor_id
        or digest_json(value) != digest
    ):
        raise RecordCodecError("history creation input does not match its record envelope")
    if raw != canonical_json(value):
        raise RecordCodecError("history creation input is not canonically encoded")
    spec = _spec_from(value["spec"])
    if spec.program_id != program_id or spec.authority.owner_id != actor_id:
        raise RecordCodecError("Program creation input has inconsistent identity or owner authority")
