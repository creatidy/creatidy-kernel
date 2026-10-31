# SPDX-License-Identifier: Apache-2.0
"""One trusted local application entrance, reusable by future transports/dispatchers.

The host binds principal, profile, resource configuration and witness producer before
exposing this object. None is accepted in a worker request body. This is not remote
authentication or hostile same-UID isolation.
"""

from collections.abc import Callable
from typing import Protocol

from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.ownership import (
    COOPERATIVE_PROFILE,
    HandoffMode,
    HandoffProof,
    OwnershipConflict,
    ResourceScope,
    WorkClaim,
)
from creatidy_kernel.ports.program_store import OperationRecord


class OwnershipStore(Protocol):
    @property
    def ownership_authority(self) -> str: ...

    def find_work_claim(
        self,
        command_key: str,
        claimant: str,
        expected_revision: int,
        attempt: AttemptSpec,
        mode: HandoffMode,
        profile: str,
        resources: tuple[ResourceScope, ...],
        lease_seconds: int,
    ) -> WorkClaim | None: ...

    def claim_work(
        self,
        command_key: str,
        claimant: str,
        expected_revision: int,
        attempt: AttemptSpec,
        mode: HandoffMode,
        profile: str,
        resources: tuple[ResourceScope, ...],
        workspace_subject: str,
        snapshot: bytes,
        lease_seconds: int,
    ) -> WorkClaim: ...

    def check_work_claim(self, claim: WorkClaim, *, settled: bool = False) -> None: ...

    def renew_work_claim(self, command_key: str, claim: WorkClaim, lease_seconds: int) -> int: ...

    def claim_owned_operation(self, claim: WorkClaim, operation_id: str, subject: str, lease_seconds: int) -> int: ...

    def check_claim_candidate(self, claim: WorkClaim, subject: str) -> None: ...

    def record_claim_proof(self, claim: WorkClaim, proof: HandoffProof, *, worker_report: bool = False) -> None: ...

    def observe_claim_workspace(
        self,
        claim: WorkClaim,
        subject: str,
        snapshot: bytes,
        *,
        owner: str | None = None,
    ) -> None: ...

    def reconcile_claim_workspace(self, claim: WorkClaim, owner: str, subject: str, evidence: str) -> None: ...

    def submit_claim_candidate(self, claim: WorkClaim, subject: str, snapshot: bytes) -> str: ...

    def cleanup_work_claim(
        self,
        claim: WorkClaim,
        owner: str,
        subject: str,
        snapshot: bytes,
        evidence: str,
        retire: Callable[[], None],
    ) -> None: ...

    def authorize_claim_publication(
        self,
        claim: WorkClaim,
        owner: str,
        subject: str,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
    ) -> OperationRecord: ...

    def find_claim_publication(
        self,
        claim: WorkClaim,
        owner: str,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
    ) -> OperationRecord | None: ...

    def release_claim_resources(
        self,
        claim: WorkClaim,
        owner: str,
        subject: str,
        snapshot: bytes,
        evidence: str,
    ) -> None: ...

    def quarantine_work_claim(self, claim: WorkClaim, owner: str | None, evidence: str) -> None: ...


class OwnedWorkspace(Protocol):
    @property
    def resource(self) -> ResourceScope: ...

    def bind_authority(self, authority: str) -> None: ...

    def capture(self) -> tuple[str, bytes]: ...

    def retirement(self) -> tuple[str, bytes] | None: ...

    def retire_empty(self, subject: str) -> None: ...


class WitnessProducer(Protocol):
    @property
    def profile(self) -> str: ...

    @property
    def resource(self) -> ResourceScope: ...

    @property
    def scope_unknown(self) -> bool: ...

    def witness(self, claim: WorkClaim, stage: str) -> HandoffProof | None: ...


class WorkOwnership:
    """Trusted host composition, not a worker-accessible store or Principal factory."""

    def __init__(
        self,
        store: OwnershipStore,
        principal: Principal,
        workspace: OwnedWorkspace,
        *,
        mode: HandoffMode,
        profile: str,
        resources: tuple[ResourceScope, ...] = (),
        witness: WitnessProducer | None = None,
    ) -> None:
        if not profile.strip() or type(mode) is not HandoffMode:
            raise OwnershipConflict("authenticated principal and explicit configured profile required")
        if mode is HandoffMode.PULL and profile != COOPERATIVE_PROFILE:
            raise OwnershipConflict("unsupported cooperative profile; no managed/physical assurance")
        self._store, self._principal, self._workspace = store, principal, workspace
        self._mode, self._profile, self._witness = mode, profile, witness
        self._resources = (workspace.resource, *resources)
        workspace.bind_authority(store.ownership_authority)

    def _bound_caller(self, claim: WorkClaim) -> None:
        if (claim.mode, claim.profile, claim.resources) != (
            self._mode,
            self._profile,
            self._resources,
        ) or (self._principal.role == "worker" and claim.claimant != self._principal.actor_id):
            raise OwnershipConflict("claim does not belong to authenticated client/configuration")

    def _caller(self, claim: WorkClaim) -> None:
        self._bound_caller(claim)
        if self._witness is not None and self._witness.scope_unknown:
            self._store.quarantine_work_claim(claim, None, "configured profile producer reports unknown/escaped scope")
            raise OwnershipConflict("configured profile scope unknown; retained durable quarantine")

    def _owner(self) -> str:
        if self._principal.role != "owner":
            raise OwnershipConflict("operation requires an owner authenticated outside request data")
        return self._principal.actor_id

    def claim(self, command_key: str, expected_revision: int, attempt: AttemptSpec, *, lease_seconds: int) -> WorkClaim:
        if self._principal.role != "worker":
            raise OwnershipConflict("handoff must bind an authenticated worker claimant")
        historical = self._store.find_work_claim(
            command_key,
            self._principal.actor_id,
            expected_revision,
            attempt,
            self._mode,
            self._profile,
            self._resources,
            lease_seconds,
        )
        if historical is not None:
            return historical  # Recorded outcome only, never a new live grant.
        if self._mode is HandoffMode.MANAGED and (
            self._witness is None
            or self._witness.profile != self._profile
            or self._witness.resource not in self._resources
        ):
            raise OwnershipConflict("unsupported managed profile/resource producer before handoff")
        if self._witness is not None and self._witness.scope_unknown:
            raise OwnershipConflict("configured profile scope unknown before handoff")
        subject, snapshot = self._workspace.capture()
        return self._store.claim_work(
            command_key,
            self._principal.actor_id,
            expected_revision,
            attempt,
            self._mode,
            self._profile,
            self._resources,
            subject,
            snapshot,
            lease_seconds,
        )

    def renew(self, command_key: str, claim: WorkClaim, *, lease_seconds: int) -> int:
        self._caller(claim)
        if self._principal.role != "worker":
            raise OwnershipConflict("only original authenticated claimant can renew")
        self.observe(claim)
        return self._store.renew_work_claim(command_key, claim, lease_seconds)

    def check_current(self, claim: WorkClaim) -> None:
        """Validate the exact retained generation without renewing its authority."""
        self._caller(claim)
        if self._principal.role != "worker":
            raise OwnershipConflict("current work authority requires the original claimant")
        self._store.check_work_claim(claim)

    def observe(self, claim: WorkClaim) -> str:
        self._caller(claim)
        subject, snapshot = self._workspace.capture()
        self._store.observe_claim_workspace(
            claim,
            subject,
            snapshot,
            owner=self._principal.actor_id if self._principal.role == "owner" else None,
        )
        return subject

    def witness(self, claim: WorkClaim, stage: str) -> None:
        self._caller(claim)
        proof = None if self._witness is None else self._witness.witness(claim, stage)
        if proof is None:
            raise OwnershipConflict("task-scope settlement/profile witness unknown; preserve resources")
        self._store.record_claim_proof(claim, proof)

    def quiesce(self, claim: WorkClaim, receipt_id: str) -> None:
        """Authenticated cooperative task-scope promise, not physical process settlement."""
        self._caller(claim)
        if self._mode is not HandoffMode.PULL or self._principal.role != "worker":
            raise OwnershipConflict("managed work requires its configured native witness")
        self.observe(claim)
        self._store.record_claim_proof(
            claim,
            HandoffProof(
                self._principal.actor_id,
                self._profile,
                self._mode,
                claim.attempt.digest,
                claim.generation,
                claim.resources,
                "complete",
                receipt_id,
            ),
            worker_report=True,
        )

    def reconcile(self, claim: WorkClaim, subject: str, *, evidence: str) -> None:
        """Owner-only trusted call; never silently attribute changed bytes to a harness."""
        self._caller(claim)
        owner = self._owner()
        if self.observe(claim) != subject:
            raise OwnershipConflict("workspace changed after reconciliation subject was selected")
        self._store.reconcile_claim_workspace(claim, owner, subject, evidence)

    def submit(self, claim: WorkClaim) -> str:
        self._caller(claim)
        self._store.check_work_claim(claim, settled=True)
        subject, snapshot = self._workspace.capture()
        self._store.observe_claim_workspace(claim, subject, snapshot)
        return self._store.submit_claim_candidate(claim, subject, snapshot)

    def authorize_publication(
        self,
        claim: WorkClaim,
        operation_id: str,
        effect_key: str,
        request: dict[str, object],
    ) -> OperationRecord:
        owner = self._owner()
        self._bound_caller(claim)
        historical = self._store.find_claim_publication(claim, owner, operation_id, effect_key, request)
        if historical is not None:
            return historical  # Original authorization outcome, not current delivery authority.
        self._caller(claim)
        subject = self.observe(claim)
        return self._store.authorize_claim_publication(
            claim,
            owner,
            subject,
            operation_id,
            effect_key,
            request,
        )

    def candidate_subject(self, claim: WorkClaim) -> str:
        """Fresh exact handoff guard for the existing trusted verification/acceptance producer."""
        self._caller(claim)
        subject = self.observe(claim)
        self._store.check_claim_candidate(claim, subject)
        return subject

    def dispatch(self, claim: WorkClaim, operation_id: str, *, lease_seconds: int) -> int:
        """Return only an outbox delivery fence, not a physical execution receipt."""
        self._caller(claim)
        self._owner()
        subject = self.observe(claim)
        return self._store.claim_owned_operation(claim, operation_id, subject, lease_seconds)

    def cleanup(self, claim: WorkClaim, *, evidence: str) -> None:
        self._caller(claim)
        owner = self._owner()
        retired = self._workspace.retirement()
        subject, snapshot = self._workspace.capture() if retired is None else retired
        # Retirement never removes nonempty data; release happens only AFTER the
        # guarded filesystem operation. A crash retains conflicting ownership.
        self._store.cleanup_work_claim(
            claim,
            owner,
            subject,
            snapshot,
            evidence,
            lambda: self._workspace.retire_empty(subject),
        )

    def release(self, claim: WorkClaim, *, evidence: str) -> None:
        """Reconcile and release settled resources without removing any workspace bytes."""
        self._caller(claim)
        owner = self._owner()
        subject, snapshot = self._workspace.capture()
        self._store.release_claim_resources(claim, owner, subject, snapshot, evidence)

    def quarantine(self, claim: WorkClaim, *, evidence: str) -> None:
        self._caller(claim)
        self._store.quarantine_work_claim(claim, self._owner(), evidence)
