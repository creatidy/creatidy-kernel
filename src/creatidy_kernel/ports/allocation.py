# SPDX-License-Identifier: Apache-2.0
"""Immutable allocation input and runtime identity evidence, including shipped K7 records."""

import hashlib
import json
from dataclasses import asdict
from typing import cast

from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.execution import RuntimeIdentity
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.ports.program_store import ApplicationStore

_LEGACY_ALLOCATION = {
    "runtime_id",
    "provider_id",
    "model_id",
    "capabilities",
    "context_tokens",
    "rationale",
    "reasoning_effort",
}
_LEGACY_IDENTITY = {"requested", "resolved", "observed", "agent_definition_version"}
_IDENTITY = _LEGACY_IDENTITY | {
    "requested_provider",
    "resolved_provider",
    "observed_provider",
    "requested_effort",
    "resolved_effort",
    "observed_effort",
}


def _object(data: bytes) -> dict[str, object]:
    raw: object = json.loads(data)
    if not isinstance(raw, dict):
        raise ValueError("durable input must be a JSON object")
    return cast(dict[str, object], raw)


def encode_allocation(allocation: Allocation) -> bytes:
    value = asdict(allocation)
    value["capabilities"] = sorted(allocation.capabilities)
    return json.dumps({"version": 1, "allocation": value}, sort_keys=True, separators=(",", ":")).encode()


def decode_allocation(data: bytes) -> Allocation:
    value = _object(data)
    if "version" in value:
        if set(value) != {"version", "allocation"} or type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("unsupported allocation document version")
        raw = value["allocation"]
        if not isinstance(raw, dict):
            raise ValueError("invalid allocation document")
        value = cast(dict[str, object], raw)
        expected = _LEGACY_ALLOCATION | {"variant", "decision_provenance"}
    else:
        expected = _LEGACY_ALLOCATION
    if set(value) != expected:
        raise ValueError("invalid allocation fields")
    capabilities = value["capabilities"]
    if not isinstance(capabilities, list):
        raise ValueError("invalid allocation capabilities")
    if any(type(item) is not str for item in cast(list[object], capabilities)):
        raise ValueError("invalid allocation capabilities")
    names = cast(list[str], capabilities)
    if names != sorted(set(names)):
        raise ValueError("allocation capabilities must be canonical")
    return Allocation(
        cast(str, value["runtime_id"]),
        cast(str, value["provider_id"]),
        cast(str, value["model_id"]),
        frozenset(names),
        cast(int, value["context_tokens"]),
        cast(str, value["rationale"]),
        cast(str | None, value["reasoning_effort"]),
        cast(str | None, value.get("variant")),
        cast(str | None, value.get("decision_provenance")),
    )


def is_legacy_allocation(data: bytes) -> bool:
    allocation = decode_allocation(data)
    return "version" not in _object(data) and all(
        value is None for value in (allocation.reasoning_effort, allocation.variant, allocation.decision_provenance)
    )


def load_attempt_inputs(store: ApplicationStore, attempt: AttemptSpec) -> tuple[bytes, bytes]:
    """Read original journal bytes before artifact publication, or pinned legacy artifacts.

    Missing published blobs remain corruption, not an invitation to repair or reselect.
    Only an unpublished artifact reference can be finalized from the original intent.
    """
    operation_id = f"runtime:{attempt.attempt_id}"
    operation = store.operation(operation_id)
    if hashlib.sha256(operation.request_json.encode()).hexdigest() != operation.request_digest:
        raise ValueError("runtime request digest mismatch")
    envelope = _object(operation.request_json.encode())
    if set(envelope) != {"version", "request"} or type(envelope["version"]) is not int or envelope["version"] != 1:
        raise ValueError("unsupported runtime request envelope")
    raw = envelope["request"]
    if not isinstance(raw, dict):
        raise ValueError("invalid original runtime request")
    request = cast(dict[str, object], raw)
    legacy = set(request) == {"attempt", "workspace"}
    if not legacy and (
        set(request) != {"version", "attempt", "workspace", "allocation", "context"}
        or type(request["version"]) is not int
        or request["version"] != 1
    ):
        raise ValueError("unsupported original runtime request")
    if request["attempt"] != attempt.digest or request["workspace"] != attempt.workspace_reference:
        raise ValueError("original runtime request differs from Attempt")
    inputs: list[bytes] = []
    for name, reference in (("allocation", attempt.allocation_reference), ("context", attempt.context_reference)):
        published = store.find_artifact(operation_id, name)
        if legacy:
            if published is None:
                raise ValueError("original legacy Attempt input is missing")
            data = published
        else:
            original = request[name]
            if not isinstance(original, str):
                raise ValueError("original Attempt input bytes are missing")
            data = original.encode()
            if published is not None and published != data:
                raise ValueError("artifact differs from original Attempt input")
        if "sha256:" + hashlib.sha256(data).hexdigest() != reference:
            raise ValueError("original Attempt input reference mismatch")
        inputs.append(data)
    allocation, context = inputs
    decode_allocation(allocation)
    expected_context = json.dumps(
        [asdict(item) for item in attempt.effective_inputs], sort_keys=True, separators=(",", ":")
    ).encode()
    if context != expected_context:
        raise ValueError("original context differs from Attempt inputs")
    return allocation, context


def load_allocation(store: ApplicationStore, attempt: AttemptSpec) -> Allocation:
    """Restore exactly the allocation bound to an Attempt without contacting any allocator."""
    allocation, _ = load_attempt_inputs(store, attempt)
    return decode_allocation(allocation)


def decode_identity(data: bytes) -> RuntimeIdentity:
    value = _object(data)
    if set(value) not in (_LEGACY_IDENTITY, _IDENTITY):
        raise ValueError("invalid runtime identity fields")
    for name, item in value.items():
        if item is None and name not in {"requested", "agent_definition_version"}:
            continue
        if type(item) is not str or not item.strip():
            raise ValueError("invalid runtime identity value")
    return RuntimeIdentity(
        cast(str, value["requested"]),
        cast(str | None, value["resolved"]),
        cast(str | None, value["observed"]),
        cast(str, value["agent_definition_version"]),
        requested_provider=cast(str | None, value.get("requested_provider")),
        resolved_provider=cast(str | None, value.get("resolved_provider")),
        observed_provider=cast(str | None, value.get("observed_provider")),
        requested_effort=cast(str | None, value.get("requested_effort")),
        resolved_effort=cast(str | None, value.get("resolved_effort")),
        observed_effort=cast(str | None, value.get("observed_effort")),
    )


def identity_matches(
    identity: RuntimeIdentity,
    allocation: Allocation,
    agent_definition: str,
    *,
    require_resolved: bool,
    legacy: bool = False,
) -> bool:
    """Unknown observations stay unknown; selected config requires actual resolution evidence."""
    legacy = legacy and all(
        value is None for value in (allocation.reasoning_effort, allocation.variant, allocation.decision_provenance)
    )
    if (
        identity.requested != allocation.model_id
        or identity.agent_definition_version != agent_definition
        or identity.resolved not in (None, allocation.model_id)
        or identity.observed not in (None, allocation.model_id)
        or (require_resolved and identity.resolved != allocation.model_id)
        or identity.requested_provider not in ((None, allocation.provider_id) if legacy else (allocation.provider_id,))
        or identity.resolved_provider not in (None, allocation.provider_id)
        or identity.observed_provider not in (None, allocation.provider_id)
        or (require_resolved and not legacy and identity.resolved_provider != allocation.provider_id)
        or identity.requested_effort != allocation.reasoning_effort
    ):
        return False
    effort = allocation.reasoning_effort
    return effort is None or (
        identity.resolved_effort in (None, effort)
        and identity.observed_effort in (None, effort)
        and (not require_resolved or identity.resolved_effort == effort)
    )
