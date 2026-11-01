# SPDX-License-Identifier: Apache-2.0
"""Ordinary managed handoff using the retained Codex Runtime and original owner.

The connection and its CodexOwnedSessionWitness are constructed by the trusted
host before binding the client. This adapter never starts a shell model launcher,
allocates a fake resource decision or adopts a PID from persisted metadata.
"""

from dataclasses import asdict
from typing import cast

from creatidy_kernel.adapters.codex_runtime import CodexInputs, CodexRuntime, owned_group_callbacks
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.core.execution import (
    Activity,
    ExecutionRequest,
    OperationKey,
    RuntimeIdentity,
    TrustMode,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.forge import Presence
from creatidy_kernel.core.intake import IntakeRefused, encode, object_from
from creatidy_kernel.ports.ordinary import OrdinaryHandoff, OrdinaryWorkClient


class OrdinaryManagedRun:
    def __init__(self, client: OrdinaryWorkClient, workspace: OrdinaryGitWorkspace, connection: CodexStdio) -> None:
        profile = object_from(client.application.execution_profile)
        if (
            profile["harness"] not in {"codex", "kilo-pull+codex"}
            or type(profile["model"]) is not str
            or type(profile["provider"]) is not str
        ):
            raise IntakeRefused("approved basic Codex profile required")
        self.client, self.workspace, self.connection, self.profile = client, workspace, connection, profile
        retire, settled, group = owned_group_callbacks(connection)
        self.runtime = CodexRuntime(
            connection,
            version=connection.version,
            resolve=self.inputs,
            collect=self.candidate,
            authorize=self.authorize,
            clock=client.application.clock,
            retire=retire,
            settled=settled,
            group_reference=group,
        )
        self.handoff: OrdinaryHandoff | None = None
        self.request: ExecutionRequest | None = None
        self.handle: str | None = None
        self.fence: int | None = None

    def inputs(self, request: ExecutionRequest) -> CodexInputs:
        if self.handoff is None or request != self.request:
            raise IntakeRefused("original managed request required")
        return CodexInputs(
            request.workspace.key,
            request.context_reference,
            request.allocation_reference,
            request.capability_reference,
            self.workspace.reference,
            self.client.context(self.handoff).decode(),
            cast(str, self.profile["model"]),
            cast(str, self.profile["provider"]),
            reasoning_effort=cast(str | None, self.profile["effort"]),
        )

    def authorize(self, request: ExecutionRequest) -> bool:
        if request != self.request or self.fence is None:
            return False
        self.client.application.check(self.client.approved)
        self.client.application.store.check_operation_delivery(request.operation.operation_id, self.fence)
        return True

    def candidate(self, request: ExecutionRequest):
        if self.handoff is None or request != self.request:
            raise IntakeRefused("original native candidate request required")
        return self.workspace.collect(request.attempt, self.handoff.claim)[0]

    def start(self, handoff: OrdinaryHandoff) -> str:
        app = self.client.application
        self.handoff = handoff
        original = app.store.operation(handoff.claim.operation_id)
        if original.attempts:
            if self.handle is not None:
                return self.handle  # Existing accepted start, never a second dispatch.
            self.fence = original.fence
            self.workspace.restore(handoff.attempt)
        else:
            self.fence = self.client.start(handoff)
        if self.fence is None:
            raise IntakeRefused("cooperative pull claim cannot dispatch managed Runtime")
        operation = app.store.operation(handoff.claim.operation_id)
        subject = next(
            item.evidence.subject
            for item in self.client.approved.proposal.drafts
            if item.task_id == handoff.attempt.work_unit_id
        )
        if self.workspace.prepared_revision is None:
            raise IntakeRefused("actual prepared source revision unavailable")
        spec = WorkspaceSpec(
            subject.origin + "/" + subject.repository + ".git",
            self.workspace.prepared_revision,
            (),
            app.execution_reference,
            "owned-local:trusted-development",
            TrustMode.TRUSTED_DEVELOPMENT,
        )
        workspace = WorkspaceHandle(self.workspace.reference, spec)
        self.request = ExecutionRequest(
            OperationKey(operation.operation_id, operation.effect_key, operation.request_digest),
            handoff.attempt,
            workspace,
            handoff.attempt.context_reference or "",
            app.execution_reference,
            app.execution_reference,
            RuntimeIdentity(
                cast(str, self.profile["model"]),
                None,
                None,
                app.execution_reference,
                requested_provider=cast(str, self.profile["provider"]),
                requested_effort=cast(str | None, self.profile["effort"]),
            ),
            self.fence,
        )
        app.store.finalize_artifact(
            operation.operation_id,
            "ordinary-execution-request",
            encode(
                {
                    "version": 1,
                    "operation": asdict(self.request.operation),
                    "attempt": handoff.attempt.digest,
                    "workspace": self.request.workspace.key,
                    "workspace_spec": {
                        "repository": spec.repository,
                        "base_revision": spec.base_revision,
                        "overlays": [asdict(item) for item in spec.overlays],
                        "toolchain_digest": spec.toolchain_digest,
                        "enforcement_profile": spec.enforcement_profile,
                        "trust_mode": spec.trust_mode.value,
                        "network_destinations": sorted(spec.network_destinations),
                        "mounts": sorted(spec.mounts),
                    },
                    "context": self.request.context_reference,
                    "basic_harness_policy": self.request.allocation_reference,
                    "agent_definition": self.request.capability_reference,
                    "requested_identity": asdict(self.request.identity),
                    "fence": self.request.fence,
                }
            ),
        )
        if original.attempts:
            raw = app.store.find_artifact(operation.operation_id, "runtime-receipt")
            if raw is None:
                raise IntakeRefused(
                    "original managed start has no accepted native identity; preserve unknown, never restart"
                )
            receipt = object_from(raw)
            identity_data = cast(dict[str, str | None], receipt["identity"])
            identity = RuntimeIdentity(**cast(dict[str, str], identity_data))
            handle = cast(str, receipt["handle"])
            restored = self.runtime.restore(
                self.request,
                handle,
                receipt_identity=identity,
                owned_group_reference=cast(str, receipt["owned_group_reference"]),
            )
            if restored.presence is not Presence.FOUND:
                raise IntakeRefused("original native owned session unavailable; no replacement or PID adoption")
            self.handle = handle
            return handle
        self.handle = self.runtime.start(self.request)
        observation = self.runtime.observe(self.handle, now=app.clock())
        app.store.observe(
            operation.operation_id,
            self.fence,
            f"ordinary-runtime:{handoff.attempt.attempt_id}:accepted",
            "accepted",
            reference=self.handle,
        )
        app.store.finalize_artifact(
            operation.operation_id,
            "runtime-receipt",
            encode(
                {
                    "version": 2,
                    "handle": self.handle,
                    "fence": self.fence,
                    "identity": asdict(observation.identity),
                    "owned_group_reference": observation.owned_group_reference,
                }
            ),
        )
        return self.handle

    def finish(self) -> str:
        if self.handoff is None or self.handle is None:
            raise IntakeRefused("managed start outcome unknown; cannot manufacture another delivery")
        try:
            self.client.application.check(self.client.approved)
        except ValueError:
            return self.stop("original ordinary work authority unavailable or revoked")
        observation = self.runtime.observe(self.handle, now=self.client.application.clock())
        if observation.activity is not Activity.TERMINAL:
            return observation.activity.value
        if self.runtime.candidate(self.handle) is None:
            raise IntakeRefused("native turn did not produce an exact completed candidate")
        self.connection.close()
        self.client.quiesce(self.handoff)
        return "quiescent"

    def stop(self, reason: str) -> str:
        if self.handoff is None or not reason.strip():
            raise IntakeRefused("original owned managed target and STOP reason required")
        app, claim = self.client.application, self.handoff.claim
        key = "ordinary-cancel:" + self.handoff.attempt.attempt_id
        app.store.intent(key, key, {"kind": "ordinary-cancellation", "claim": asdict(claim), "reason": reason})
        # Cancellation is not a renewed work dispatch. Only this retained original
        # connection can retire its own dedicated group, including an unknown start.
        if self.handle is not None:
            self.runtime.cancel(self.handle)
        else:
            self.connection.close()
        try:
            self.client.worker.witness(claim, "stop")
        except ValueError:
            return "cancel_uncertain"
        app.store.finalize_artifact(
            key,
            "settlement",
            encode({"claim": asdict(claim), "profile": claim.profile, "group": self.connection.owned_group_reference}),
        )
        return "cancelled"

    def request_stop(self, reason: str) -> str:
        """Revoke owner authority and retire only this original retained managed target."""
        self.client.application.stop(self.client.approved, reason)
        return self.stop(reason)
