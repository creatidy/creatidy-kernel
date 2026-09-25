# SPDX-License-Identifier: Apache-2.0
"""Replaceable runtime and workspace seams, without worker authority over control state."""

from typing import Protocol

from creatidy_kernel.core.execution import (
    ArtifactManifest,
    Candidate,
    ExecutionRequest,
    Lookup,
    OperationKey,
    RuntimeObservation,
    WorkspaceHandle,
    WorkspaceSpec,
)


class Runtime(Protocol):
    def start(self, request: ExecutionRequest) -> str: ...

    def observe(self, handle: str, *, now: int) -> RuntimeObservation: ...

    def candidate(self, handle: str) -> Candidate | None: ...

    def cancel(self, handle: str) -> RuntimeObservation: ...

    def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup: ...


class Workspace(Protocol):
    def materialize(self, key: str, spec: WorkspaceSpec) -> WorkspaceHandle: ...

    def collect(self, handle: WorkspaceHandle) -> ArtifactManifest: ...

    def reconcile(self, key: str, handle: str | None = None) -> Lookup: ...

    def cleanup(self, handle: WorkspaceHandle, *, worker_settled: bool) -> None: ...
