# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral forge values. A receipt is not Program acceptance."""

from dataclasses import dataclass
from enum import StrEnum

from creatidy_kernel.core.execution import OperationKey


class ForgeConflict(ValueError):
    """An exact scope, immutable identity, or expected revision differs."""


class UnsupportedForge(ValueError):
    """A required conditional operation cannot be provided safely."""


class Presence(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    INACCESSIBLE = "inaccessible"
    UNKNOWN = "unknown"


class EffectStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"
    UNKNOWN = "unknown"


class CheckResult(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Reference:
    """Qualified opaque identity; only the owning adapter interprets the payload."""

    value: str

    def __post_init__(self) -> None:
        if ":" not in self.value:
            raise ValueError("reference must be provider-qualified")
        provider, payload = self.value.split(":", 1)
        if not provider or not payload or any(char.isspace() for char in self.value):
            raise ValueError("invalid qualified reference")


@dataclass(frozen=True, slots=True)
class Observation:
    presence: Presence
    reference: Reference | None = None
    revision: Reference | None = None
    base: Reference | None = None
    head: Reference | None = None
    check_context: str | None = None
    check_result: CheckResult | None = None


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Observation, ...]
    next_cursor: str | None
    complete: bool


@dataclass(frozen=True, slots=True)
class Receipt:
    status: EffectStatus
    operation: OperationKey
    reference: Reference | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Effect:
    """Exact mutation authorized by a trusted caller, not by a worker-supplied grant."""

    operation: OperationKey
    repository: Reference
    action: str
    branch: str
    expected: Reference | None = None
    revision: Reference | None = None
    base_branch: str | None = None
    base_revision: Reference | None = None
    title: str | None = None
    body: str | None = None
    fence: int = 0
    delivery_attempts: int = 0

    def __post_init__(self) -> None:
        if self.action not in {"branch", "push", "pr"} or not self.branch or self.branch.strip() != self.branch:
            raise ForgeConflict("invalid forge action or branch")
        if self.fence <= 0 or self.delivery_attempts <= 0:
            raise ForgeConflict("durably claimed delivery required")
        if self.revision is None or (self.action == "push" and self.expected is None):
            raise ForgeConflict("exact revision and push expected-old required")
        if self.action in {"branch", "pr"} and (not self.base_branch or not self.base_revision):
            raise ForgeConflict("branch or PR requires exact source/base")
        if self.action == "pr" and not self.title:
            raise ForgeConflict("PR requires title")
