# SPDX-License-Identifier: Apache-2.0
"""Application-facing contract for durable K1 Program state."""

from typing import Protocol

from creatidy_kernel.core.domain import DomainCommandType, Program, ProgramSpec


class ProgramStore(Protocol):
    """Create, recover, and atomically admit commands for a Program aggregate."""

    def create(self, spec: ProgramSpec, command_key: str) -> Program: ...

    def load(self, program_id: str) -> Program: ...

    def admit(self, program_id: str, command_key: str, command: DomainCommandType) -> Program: ...
