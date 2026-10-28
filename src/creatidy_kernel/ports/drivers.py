# SPDX-License-Identifier: Apache-2.0
"""Independent project bindings and one trusted application entrance for journaled effects."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol, cast

from creatidy_kernel.core.drivers import (
    DriverBinding,
    DriverDelivery,
    DriverFact,
    DriverMutation,
    DriverReceipt,
    DriverScan,
    DriverSubject,
    WorkObservation,
    driver_bytes,
    driver_digest,
)
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import EffectStatus
from creatidy_kernel.core.ownership import WorkClaim
from creatidy_kernel.ports.ownership import WorkOwnership
from creatidy_kernel.ports.program_store import ApplicationStore


class WorkManagement(Protocol):
    @property
    def binding(self) -> DriverBinding: ...
    def work(self, key: str) -> WorkObservation: ...


class SourceManagement(Protocol):
    @property
    def binding(self) -> DriverBinding: ...
    def repository(self) -> DriverFact: ...
    def ref(self, branch: str) -> DriverFact: ...
    def commit(self, revision: str) -> DriverFact: ...
    def change(self, key: str) -> DriverFact: ...
    def reviews(self, key: str) -> DriverScan: ...


class ContinuousIntegration(Protocol):
    @property
    def binding(self) -> DriverBinding: ...
    def build(self, key: str, source: DriverSubject) -> DriverFact: ...


class Knowledge(Protocol):
    @property
    def binding(self) -> DriverBinding: ...
    def content(self, key: str, revision: str) -> DriverFact: ...


class DriverEffects(Protocol):
    @property
    def binding(self) -> DriverBinding: ...
    def validate_mutation(self, mutation: DriverMutation) -> None: ...
    def apply(self, delivery: DriverDelivery, before_send: Callable[[], None]) -> DriverReceipt: ...
    def reconcile(self, delivery: DriverDelivery) -> DriverReceipt: ...


class DriverStore(ApplicationStore, Protocol):
    def check_driver_delivery(self, operation_id: str, fence: int, request_digest: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ProjectDrivers:
    work: WorkManagement
    scm: SourceManagement
    ci: ContinuousIntegration
    knowledge: tuple[Knowledge, ...]

    def __post_init__(self) -> None:
        if (self.work.binding.family, self.scm.binding.family, self.ci.binding.family) != ("work", "scm", "ci"):
            raise ValueError("independent project family bindings required")
        if not self.knowledge or any(item.binding.family != "knowledge" for item in self.knowledge):
            raise ValueError("explicit knowledge source bindings required")
        if len({item.binding.digest for item in self.knowledge}) != len(self.knowledge):
            raise ValueError("duplicate knowledge bindings")

    def observe(
        self,
        work_key: str,
        branch: str,
        change_key: str,
        build_key: str,
        context: tuple[tuple[str, str, str], ...],
    ) -> tuple[WorkObservation, tuple[DriverFact, ...], DriverScan]:
        """Read native evidence only. No work selection, acceptance or execution grant."""
        sources = {item.binding.digest: item for item in self.knowledge}
        if any(binding not in sources for binding, _, _ in context):
            raise ValueError("context outside configured sources")
        work = self.work.work(work_key)
        repository = self.scm.repository()
        ref = self.scm.ref(branch)
        change = self.scm.change(change_key)
        if change.subject.revision is None:
            raise ValueError("exact change head unavailable")
        commit = self.scm.commit(change.subject.revision)
        checks = self.ci.build(build_key, commit.subject)
        reviews = self.scm.reviews(change_key)
        facts = (
            repository,
            ref,
            change,
            commit,
            checks,
            *(sources[binding].content(key, revision) for binding, key, revision in context),
        )
        return work, facts, reviews


class DriverApplication:
    """Trusted controller API. Workers receive neither this object nor driver credentials.

    Existing WorkOwnership owns intent admission and the REAL delivery claim. All
    callbacks and read waits precede a fresh outbox/authority check immediately at send.
    """

    def __init__(
        self,
        store: DriverStore,
        ownership: WorkOwnership,
        project: ProjectDrivers,
        effects: tuple[DriverEffects, ...],
        clock: Callable[[], int],
        authorize: Callable[[WorkClaim, DriverMutation], bool],
        *,
        lease_seconds: int = 60,
    ) -> None:
        configured = {
            project.work.binding,
            project.scm.binding,
            project.ci.binding,
            *(item.binding for item in project.knowledge),
        }
        if any(item.binding not in configured for item in effects):
            raise ValueError("effect driver outside explicit project bindings")
        if len({item.binding for item in effects}) != len(effects):
            raise ValueError("duplicate effect drivers")
        self.store, self.ownership, self.project = store, ownership, project
        self.effects = {item.binding: item for item in effects}
        self.clock, self.authorize = clock, authorize
        if not 0 < lease_seconds <= 300:
            raise ValueError("finite driver delivery lease required")
        self.lease_seconds = lease_seconds

    def synchronize(
        self,
        claim: WorkClaim,
        operation_id: str,
        effect_key: str,
        mutation: DriverMutation,
    ) -> DriverReceipt:
        driver = self.effects.get(mutation.target.binding)
        if driver is None:
            raise ValueError("effect driver not configured")
        driver.validate_mutation(mutation)  # Unsupported native conditions refuse before credentials/intent/send.
        request = {
            **mutation.request(),
            "attempt": claim.attempt.digest,
            "claim": claim.command_key,
            "generation": claim.generation,
        }
        operation = self.ownership.authorize_publication(claim, operation_id, effect_key, request)
        key = OperationKey(operation_id, operation.effect_key, operation.request_digest)
        cached = self.store.find_artifact(operation_id, "driver-receipt")
        if cached is not None:
            raw: object = json.loads(cached)
            value = cast(dict[str, object], raw)
            if not isinstance(raw, dict) or value.get("operation") != asdict(key):
                raise ValueError("driver receipt differs from immutable Operation")
            receipt = DriverReceipt(
                EffectStatus(cast(str, value["status"])),
                key,
                cast(str | None, value["reference"]),
                cast(str, value["reason"]),
            )
            if cached != driver_bytes(asdict(receipt)):
                raise ValueError("driver receipt differs from its canonical committed content")
            if operation.status == "cancelled":
                return receipt  # Retain the earlier receipt as history; cancellation grants no settlement.
            if operation.status in {"accepted", "rejected"}:
                expected = (
                    "accepted"
                    if receipt.status is EffectStatus.ACCEPTED
                    else "rejected"
                    if receipt.status in {EffectStatus.REJECTED, EffectStatus.STALE}
                    else None
                )
                if operation.status != expected or (
                    expected == "accepted" and receipt.reference != operation.accepted_reference
                ):
                    raise ValueError("driver receipt differs from terminal Operation outcome")
                # Outcome and settlement are separate commits; replay repairs only this exact witness.
                self.store.record_work_settlement(
                    operation_id, "sha256:" + driver_digest(asdict(receipt)), expected_fence=operation.fence
                )
            else:
                self._record(operation_id, operation.fence, receipt)
            return receipt  # Historical receipt is not current remote state or renewed authority.
        if operation.attempts:
            if operation.lease_until is not None and operation.lease_until > self.clock():
                return DriverReceipt(EffectStatus.UNKNOWN, key, reason="original-delivery-lease-held")
            delivery = DriverDelivery(key, mutation, operation.fence, operation.attempts)
            receipt = driver.reconcile(delivery)
            if receipt.operation != key:
                raise ValueError("driver reconciliation differs from original Operation")
            if receipt.status is EffectStatus.ACCEPTED:
                reference = self.store.finalize_artifact(operation_id, "driver-receipt", driver_bytes(asdict(receipt)))
                self.store.reconcile(
                    operation_id,
                    operation.fence,
                    "found",
                    now=self.clock(),
                    evidence=reference,
                    reference=receipt.reference,
                    matched_digest=operation.request_digest,
                )
                self.store.record_work_settlement(operation_id, reference, expected_fence=operation.fence)
            else:
                self.store.reconcile(
                    operation_id,
                    operation.fence,
                    "unknown",
                    now=self.clock(),
                    evidence="driver:lookup:" + driver_digest(asdict(receipt)),
                )
            return receipt  # Never automatic retry, even after a complete empty scan.

        if not self.authorize(claim, mutation):
            raise ValueError("exact driver effect not authorized by current host policy")
        fence = self.ownership.dispatch(claim, operation_id, lease_seconds=self.lease_seconds)
        operation = self.store.operation(operation_id)
        delivery = DriverDelivery(key, mutation, fence, operation.attempts)

        def before_send() -> None:
            self.ownership.observe(claim)
            self.store.check_driver_delivery(operation_id, delivery.fence, key.request_digest)
            # Blocking physical/clock preflight may revoke the separate host effect grant.
            if not self.authorize(claim, mutation):
                raise ValueError("driver effect authority revoked during observation/credential resolution")

        try:
            receipt = driver.apply(delivery, before_send)
        except Exception:
            self.store.record_transport(operation_id, fence, False)
            self.store.observe(
                operation_id, fence, "driver:unknown:" + str(fence) + ":" + driver_digest(operation_id), "unknown"
            )
            raise
        if receipt.operation != key:
            raise ValueError("driver reply differs from original Operation")
        self.store.record_transport(operation_id, fence, receipt.status is not EffectStatus.UNKNOWN)
        self._record(operation_id, fence, receipt)
        return receipt

    def _record(self, operation_id: str, fence: int, receipt: DriverReceipt) -> None:
        if receipt.status is EffectStatus.UNKNOWN:
            self.store.observe(
                operation_id, fence, "driver:unknown:" + str(fence) + ":" + driver_digest(operation_id), "unknown"
            )
            return
        reference = self.store.finalize_artifact(operation_id, "driver-receipt", driver_bytes(asdict(receipt)))
        kind = "accepted" if receipt.status is EffectStatus.ACCEPTED else "rejected"
        self.store.observe(
            operation_id,
            fence,
            "driver:receipt:" + driver_digest(asdict(receipt)),
            kind,
            reference=receipt.reference if kind == "accepted" else None,
        )
        self.store.record_work_settlement(operation_id, reference, expected_fence=fence)
