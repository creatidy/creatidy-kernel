# SPDX-License-Identifier: Apache-2.0
"""Application-facing contract for durable K1 Program state."""

from dataclasses import dataclass
from typing import Protocol

from creatidy_kernel.core.domain import DomainCommandType, Program, ProgramSpec
from creatidy_kernel.core.work_budget import WorkKind


class ProgramReadChanged(ValueError):
    """A callback invalidated a bounded single-controller store read cut."""


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    effect_key: str
    request_digest: str
    request_json: str
    fence: int
    lease_until: int | None
    status: str
    accepted_reference: str | None
    retry_proof: str | None
    attempts: int


class ProgramStore(Protocol):
    """Create, recover, and atomically admit commands for a Program aggregate."""

    def create(self, spec: ProgramSpec, command_key: str) -> Program: ...

    def load(self, program_id: str) -> Program: ...

    def admit(self, program_id: str, command_key: str, command: DomainCommandType) -> Program: ...


class ApplicationStore(ProgramStore, Protocol):
    def has_work_budget(self, program_id: str) -> bool: ...
    def check_work_authority(self, program_id: str) -> None: ...
    def check_work_dispatch(self, operation_id: str) -> None: ...
    def reserve_work(
        self, program_id: str, attempt_id: str, operation_id: str, category: WorkKind, *, retained_bytes: int = 0
    ) -> None: ...
    def budgeted_intent(
        self,
        program_id: str,
        attempt_id: str,
        category: WorkKind,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
        *,
        retained_bytes: int = 0,
    ) -> OperationRecord: ...
    def work_program_for_attempt(self, attempt_id: str) -> str: ...
    def record_work_completion(self, operation_id: str, evidence: str) -> None: ...
    def record_work_settlement(
        self, operation_id: str, evidence: str, *, expected_fence: int | None = None
    ) -> None: ...
    def retry_work(self, operation_id: str) -> None: ...
    def intent(self, operation_id: str, effect_key: str, request: dict[str, object]) -> OperationRecord: ...
    def operation(self, operation_id: str) -> OperationRecord: ...
    def claim(self, operation_id: str, *, now: int, lease_seconds: int) -> int: ...
    def admit_with_intent(
        self,
        program_id: str,
        command_key: str,
        command: DomainCommandType,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
    ) -> Program: ...
    def record_transport(self, operation_id: str, fence: int, succeeded: bool) -> None: ...
    def observe(
        self, operation_id: str, fence: int, observation_id: str, kind: str, *, reference: str | None = None
    ) -> OperationRecord: ...
    def reconcile(
        self,
        operation_id: str,
        fence: int,
        outcome: str,
        *,
        now: int,
        evidence: str,
        reference: str | None = None,
        matched_digest: str | None = None,
        authoritative_absence: bool = False,
    ) -> OperationRecord: ...
    def finalize_artifact(self, operation_id: str, name: str, data: bytes) -> str: ...
    def artifact(self, operation_id: str, name: str) -> bytes: ...
    def find_artifact(self, operation_id: str, name: str) -> bytes | None: ...
