# SPDX-License-Identifier: Apache-2.0
"""The intentionally narrow, in-process resource contract proved in A0."""

from dataclasses import dataclass


def _validate_limits(capabilities: frozenset[str], context_tokens: int) -> None:
    if type(capabilities) is not frozenset or any(type(item) is not str or not item.strip() for item in capabilities):
        raise ValueError("capabilities must be an immutable set of nonempty identifiers")
    if type(context_tokens) is not int or context_tokens < 0:
        raise ValueError("context_tokens must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """Capability identifiers are opaque; no provider ranking is encoded here."""

    work_unit_id: str
    required_capabilities: frozenset[str]
    context_tokens: int

    def __post_init__(self) -> None:
        if type(self.work_unit_id) is not str or not self.work_unit_id.strip():
            raise ValueError("work_unit_id must be nonempty")
        _validate_limits(self.required_capabilities, self.context_tokens)


@dataclass(frozen=True, slots=True)
class Allocation:
    """Configured selection, not a reservation, runtime attestation or authority grant."""

    runtime_id: str
    provider_id: str
    model_id: str
    capabilities: frozenset[str]
    context_tokens: int
    rationale: str
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (self.runtime_id, self.provider_id, self.model_id, self.rationale)
        ):
            raise ValueError("allocation identities and rationale must be nonempty")
        if self.reasoning_effort is not None and (
            type(self.reasoning_effort) is not str or not self.reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be nonempty when specified")
        _validate_limits(self.capabilities, self.context_tokens)


class AllocationUnavailable(Exception):
    """No configured allocation satisfies the request; this is not a Human Gate."""
