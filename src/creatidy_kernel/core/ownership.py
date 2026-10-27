# SPDX-License-Identifier: Apache-2.0
"""Exact local handoff subjects; cooperative assurance never implies process ownership."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from creatidy_kernel.core.domain import AttemptSpec


class OwnershipConflict(ValueError):
    """A resource, generation, subject or required witness is unavailable."""


class HandoffMode(StrEnum):
    PULL = "pull"
    MANAGED = "managed"


COOPERATIVE_PROFILE = "kernel:cooperative-task-scope:v1"


@dataclass(frozen=True, slots=True)
class ResourceScope:
    kind: str
    key: str

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not str
            or type(self.key) is not str
            or self.kind not in {"workspace", "session", "effect"}
            or not self.key.strip()
        ):
            raise OwnershipConflict("invalid resource scope")
        if self.kind == "workspace" and (
            not self.key.startswith("/") or any(part in {".", "..", ""} for part in self.key.split("/")[1:])
        ):
            raise OwnershipConflict("workspace resource must be an exact canonical absolute path")

    def conflicts(self, other: ResourceScope) -> bool:
        if self.kind != other.kind:
            return False
        if self.kind == "workspace":
            return self.key == other.key or self.key.startswith(other.key + "/") or other.key.startswith(self.key + "/")
        return self.key == other.key


@dataclass(frozen=True, slots=True)
class WorkClaim:
    command_key: str
    claimant: str
    attempt: AttemptSpec
    generation: int
    mode: HandoffMode
    profile: str
    resources: tuple[ResourceScope, ...]
    workspace_subject: str
    expires_at: int

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation <= 0
            or type(self.expires_at) is not int
            or self.expires_at <= 0
            or type(self.mode) is not HandoffMode
            or type(self.attempt) is not AttemptSpec
            or type(self.resources) is not tuple
            or not self.resources
            or any(type(item) is not ResourceScope for item in self.resources)
            or any(
                type(item) is not str or not item.strip()
                for item in (
                    self.command_key,
                    self.claimant,
                    self.profile,
                    self.workspace_subject,
                )
            )
        ):
            raise OwnershipConflict("invalid immutable work claim")

    @property
    def operation_id(self) -> str:
        return f"runtime:{self.attempt.attempt_id}"


@dataclass(frozen=True, slots=True)
class HandoffProof:
    """Supplied by a trusted configured producer, not decoded from a worker report."""

    source: str
    profile: str
    mode: HandoffMode
    attempt_digest: str
    generation: int
    resources: tuple[ResourceScope, ...]
    stage: str
    reference: str

    def check(self, claim: WorkClaim, stage: str) -> None:
        if (
            type(self.mode) is not HandoffMode
            or type(self.generation) is not int
            or type(self.resources) is not tuple
            or any(type(item) is not ResourceScope for item in self.resources)
            or not self.source.strip()
            or not self.reference.strip()
            or stage not in {"start", "complete", "stop"}
            or (self.profile, self.mode, self.attempt_digest, self.generation, self.resources, self.stage)
            != (claim.profile, claim.mode, claim.attempt.digest, claim.generation, claim.resources, stage)
        ):
            raise OwnershipConflict("missing or mismatched exact profile/task-scope witness")
        if self.mode is HandoffMode.PULL and stage == "start":
            raise OwnershipConflict("cooperative receipt cannot certify managed start")

    def payload(self) -> dict[str, object]:
        return asdict(self)
