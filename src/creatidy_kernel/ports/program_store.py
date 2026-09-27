# SPDX-License-Identifier: Apache-2.0
"""Application-facing contract for durable K1 Program state."""

from dataclasses import dataclass
from typing import Protocol

from creatidy_kernel.core.domain import DomainCommandType, Program, ProgramSpec


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
