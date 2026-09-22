# SPDX-License-Identifier: Apache-2.0
"""Neutral port for applying pure domain commands."""

from typing import Protocol

from creatidy_kernel.core.domain import DomainCommandType, Program


class ProgramTransitions(Protocol):
    """An application layer can depend on this port without adding an adapter to core."""

    def apply(self, command: DomainCommandType) -> Program: ...
