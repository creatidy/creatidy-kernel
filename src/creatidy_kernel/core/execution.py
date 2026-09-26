# SPDX-License-Identifier: Apache-2.0
"""Values exchanged with replaceable synthetic execution boundaries.

These values describe requirements, not an OS sandbox or an acceptance verdict.
"""

from dataclasses import dataclass
from enum import StrEnum

from creatidy_kernel.core.domain import AttemptSpec


class ExecutionConflict(Exception):
    """An identity, authority, or immutable subject does not match."""


class UnsupportedExecution(Exception):
    """The requested isolation or runtime feature is not supported."""


def _text(value: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError("execution identifiers must be nonempty strings")


class TrustMode(StrEnum):
    TRUSTED_DEVELOPMENT = "trusted_development"
    ISOLATED = "isolated"


class Presence(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Activity(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OperationKey:
    operation_id: str
    effect_key: str
    request_digest: str

    def __post_init__(self) -> None:
        for value in (self.operation_id, self.effect_key, self.request_digest):
            _text(value)


@dataclass(frozen=True, slots=True)
class Overlay:
    path: str
    artifact_digest: str

    def __post_init__(self) -> None:
        validate_relative_path(self.path)
        _text(self.artifact_digest)


def validate_relative_path(path: str) -> None:
    _text(path)
    if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ExecutionConflict("path escapes or is not canonical within workspace")


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    repository: str
    base_revision: str
    overlays: tuple[Overlay, ...]
    toolchain_digest: str
    enforcement_profile: str
    trust_mode: TrustMode
    network_destinations: frozenset[str] = frozenset()
    mounts: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for value in (self.repository, self.base_revision, self.toolchain_digest, self.enforcement_profile):
            _text(value)
        if type(self.overlays) is not tuple or any(type(item) is not Overlay for item in self.overlays):
            raise ValueError("overlays must be immutable Overlay values")
        if len({item.path for item in self.overlays}) != len(self.overlays):
            raise ExecutionConflict("overlay paths must be unique")
        if type(self.trust_mode) is not TrustMode:
            raise ValueError("trust mode must be explicit")
        for values in (self.network_destinations, self.mounts):
            if type(values) is not frozenset:
                raise ValueError("workspace policy sets must be immutable")
            for value in values:
                _text(value)


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    key: str
    spec: WorkspaceSpec


@dataclass(frozen=True, slots=True)
class Artifact:
    path: str
    digest: str

    def __post_init__(self) -> None:
        validate_relative_path(self.path)
        _text(self.digest)


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    workspace_key: str
    artifacts: tuple[Artifact, ...]


@dataclass(frozen=True, slots=True)
class Lookup:
    presence: Presence
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    requested: str
    resolved: str | None
    observed: str | None
    agent_definition_version: str


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    operation: OperationKey
    attempt: AttemptSpec
    workspace: WorkspaceHandle
    context_reference: str
    allocation_reference: str
    capability_reference: str
    identity: RuntimeIdentity
    fence: int

    def __post_init__(self) -> None:
        if type(self.fence) is not int or self.fence <= 0:
            raise ValueError("a claimed operation fence is required")
        for value in (self.context_reference, self.allocation_reference, self.capability_reference):
            _text(value)
        if self.attempt.workspace_reference != self.workspace.key:
            raise ExecutionConflict("Attempt workspace reference differs")
        if self.attempt.context_reference != self.context_reference:
            raise ExecutionConflict("Attempt context reference differs")
        if self.attempt.allocation_reference != self.allocation_reference:
            raise ExecutionConflict("Attempt allocation reference differs")
        if self.attempt.agent_definition_reference != self.identity.agent_definition_version:
            raise ExecutionConflict("Attempt agent definition differs")


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    handle: str
    activity: Activity
    observed_at: int
    fresh_until: int
    identity: RuntimeIdentity
    cancellation_requested: bool


@dataclass(frozen=True, slots=True)
class Candidate:
    attempt_id: str
    spec_digest: str
    artifacts: ArtifactManifest


@dataclass(frozen=True, slots=True)
class WorkerScope:
    workspace_key: str
    attempt_id: str
    fence: int
    expires_at: int
    paths: frozenset[str]
    operations: frozenset[str]
    network_destinations: frozenset[str]
    mounts: frozenset[str]

    def __post_init__(self) -> None:
        for path in self.paths:
            validate_relative_path(path)
