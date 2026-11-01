# SPDX-License-Identifier: Apache-2.0
"""Bounded descriptor-passed local read channel; it does not listen on a network socket."""

import json
import re
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import cast

from creatidy_kernel.adapters.ordinary_managed import OrdinaryManagedRun
from creatidy_kernel.adapters.sqlite_store import IdempotencyConflict
from creatidy_kernel.core.domain import AttemptStatus
from creatidy_kernel.core.intake import digest, encode, object_from
from creatidy_kernel.core.ownership import HandoffMode
from creatidy_kernel.core.served import ProgressPosition, command_prefix
from creatidy_kernel.ports.ordinary import (
    OrdinaryApproval,
    OrdinaryHandoff,
    OrdinaryProgram,
    OrdinaryProposal,
    OrdinaryWorkClient,
)
from creatidy_kernel.ports.ownership import WorkOwnership
from creatidy_kernel.ports.served import OwnerProjection, ServedSnapshot

MAX_REQUEST = 65536
MAX_RESPONSE = 1024 * 1024


class LocalChannelRefused(ValueError):
    """The local capability received an invalid or incomplete frame."""


class LocalDecisionNotifier:
    """Best-effort datagrams; authoritative decisions remain in the owner snapshot."""

    def __init__(self, channel: socket.socket) -> None:
        if channel.family != socket.AF_UNIX or channel.type & socket.SOCK_DGRAM != socket.SOCK_DGRAM:
            raise LocalChannelRefused("a connected local datagram capability is required")
        channel.getpeername()
        self._channel = channel
        self._seen: set[str] = set()
        self.dropped = False

    def publish(self, snapshot: ServedSnapshot) -> None:
        for decision in snapshot.decisions:
            if decision.get("status") not in {
                "owner_decision_needed",
                "comment",
                "invalid",
                "rejected",
                "review_unknown",
            }:
                continue
            identity = digest(encode([snapshot.scope, decision]))
            if identity in self._seen:
                continue
            if len(self._seen) >= 4096:
                self.dropped = True
                return
            payload = encode({"version": 1, "kind": "decision-required", "id": identity, "scope": snapshot.scope})
            try:
                if self._channel.send(payload, socket.MSG_DONTWAIT) != len(payload):
                    self.dropped = True
                    return
            except OSError:
                self.dropped = True
                return
            self._seen.add(identity)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LocalChannelRefused("duplicate local request field")
        result[key] = value
    return result


def _receive_exact(channel: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LocalChannelRefused("local frame deadline expired")
        channel.settimeout(remaining)
        part = channel.recv(size - len(data))
        if not part:
            raise LocalChannelRefused("incomplete local frame")
        data.extend(part)
    return bytes(data)


def read_frame(channel: socket.socket, *, bound: int, timeout: float = 1.0) -> bytes:
    if not 0 < timeout <= 300:
        raise LocalChannelRefused("local frame deadline is outside its bound")
    deadline = time.monotonic() + timeout
    size = struct.unpack("!I", _receive_exact(channel, 4, deadline))[0]
    if size < 1 or size > bound:
        raise LocalChannelRefused("local frame exceeds its bound")
    return _receive_exact(channel, size, deadline)


def write_frame(channel: socket.socket, payload: bytes, *, bound: int) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= bound:
        raise LocalChannelRefused("local frame exceeds its bound")
    channel.sendall(struct.pack("!I", len(payload)) + payload)


class LocalReadChannel:
    """One host-bound read capability, serviced only by the store-owning thread."""

    def __init__(self, owner: OwnerProjection, channel: socket.socket) -> None:
        if channel.family != socket.AF_UNIX or channel.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
            raise LocalChannelRefused("a preconnected local stream capability is required")
        channel.getpeername()
        self._owner, self._channel = owner, channel

    def revoke(self) -> None:
        """The trusted host can close this capability without changing work state."""
        self._channel.close()

    def _dispatch(self, request: dict[str, object]) -> object:
        if request.get("action") == "snapshot" and set(request) == {"version", "action"}:
            return asdict(self._owner.snapshot())
        if request.get("action") == "events" and set(request) == {"version", "action", "after", "limit"}:
            raw = request["after"]
            if type(raw) is not dict:
                raise LocalChannelRefused("invalid event position")
            position = cast(dict[str, object], raw)
            if set(position) != {"producer", "scope", "sequence"}:
                raise LocalChannelRefused("invalid event position")
            cursor = ProgressPosition(
                cast(str, position["producer"]), cast(str, position["scope"]), cast(int, position["sequence"])
            )
            return asdict(self._owner.events(cursor, limit=cast(int, request["limit"])))
        raise LocalChannelRefused("unsupported local read request")

    def serve_once(self) -> None:
        self._channel.settimeout(1.0)
        try:
            payload = read_frame(self._channel, bound=MAX_REQUEST)
            value: object = json.loads(payload, object_pairs_hook=_unique_object)
            if type(value) is not dict:
                raise LocalChannelRefused("request must be an object")
            request = cast(dict[str, object], value)
            if type(request.get("version")) is not int or request.get("version") != 1:
                raise LocalChannelRefused("unsupported local contract version")
            result = self._dispatch(request)
            response = json.dumps({"version": 1, "ok": True, "result": result}, sort_keys=True).encode()
            write_frame(self._channel, response, bound=MAX_RESPONSE)
        except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError, IdempotencyConflict) as error:
            self._channel.close()
            raise LocalChannelRefused("local read refused") from error
        except BaseException:
            self._channel.close()
            raise


class LocalWorkerChannel(LocalReadChannel):
    """A separate host-bound capability; the worker identity never comes from the frame."""

    def __init__(
        self,
        owner: OwnerProjection,
        channel: socket.socket,
        worker: OrdinaryWorkClient,
        *,
        handoffs: dict[str, OrdinaryHandoff] | None = None,
        notifier: LocalDecisionNotifier | None = None,
    ) -> None:
        if worker.approved.program.program_id != owner.scope:
            raise LocalChannelRefused("worker and projection scopes differ")
        super().__init__(owner, channel)
        self._worker = worker
        self._handoffs = {} if handoffs is None else handoffs
        self._notifier = notifier

    def _dispatch(self, request: dict[str, object]) -> object:
        if request.get("action") == "start":
            return self._start(request)
        if request.get("action") == "quiesce":
            return self._quiesce(request)
        if request.get("action") == "renew":
            return self._renew(request)
        if request.get("action") in {"submit", "report"}:
            return self._candidate_action(request)
        if request.get("action") != "claim":
            return super()._dispatch(request)
        if set(request) != {"version", "action", "command_id", "expected_revision", "lease_seconds", "remediation_of"}:
            raise LocalChannelRefused("invalid work request")
        command_id, revision, lease, remediation = (
            request["command_id"],
            request["expected_revision"],
            request["lease_seconds"],
            request["remediation_of"],
        )
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
            or type(lease) is not int
            or lease < 1
            or (remediation is not None and (type(remediation) is not str or not remediation.strip()))
        ):
            raise LocalChannelRefused("invalid work request values")
        if command_id not in self._handoffs and len(self._handoffs) >= 4096:
            raise LocalChannelRefused("worker capability handoff limit reached")
        store = self._worker.application.store
        operation_id = command_prefix(self._owner.scope) + digest(encode([self._worker.principal.actor_id, command_id]))
        exact = {**request, "scope": self._owner.scope, "actor": self._worker.principal.actor_id}
        prior = store.find_operation(operation_id)
        receipt = None
        if prior is not None:
            if prior.effect_key != operation_id or object_from(prior.request_json.encode()).get("request") != exact:
                raise LocalChannelRefused("command identity already has different input")
            receipt = store.find_artifact(operation_id, "receipt")
            if receipt is None:
                return {"status": "unknown", "settlement": "unknown"}
        else:
            self._worker.application.check(self._worker.approved)
            if store.load(self._owner.scope).revision != revision:
                raise LocalChannelRefused("caller expected revision is stale")
            store.intent(operation_id, operation_id, exact)
        handoff = self._worker.request(
            operation_id, lease_seconds=lease, remediation_of=remediation, expected_revision=revision
        )
        result: dict[str, object] = {
            "version": 1,
            "status": "claimed",
            "program_id": handoff.attempt.program_id,
            "attempt_id": handoff.attempt.attempt_id,
            "claim_generation": handoff.claim.generation,
            "receipt": "recorded; current authority is checked at each later action",
        }
        if receipt is None:
            store.finalize_artifact(operation_id, "receipt", encode(result))
        elif object_from(receipt) != result:
            raise LocalChannelRefused("recorded claim receipt differs from original handoff")
        self._handoffs[command_id] = handoff
        return result

    def _start(self, request: dict[str, object]) -> object:
        if set(request) != {"version", "action", "command_id", "expected_revision", "claim_id"}:
            raise LocalChannelRefused("invalid start request")
        command_id, revision, claim_id = request["command_id"], request["expected_revision"], request["claim_id"]
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
            or type(claim_id) is not str
            or claim_id not in self._handoffs
        ):
            raise LocalChannelRefused("invalid start values or unbound handoff")
        handoff = self._handoffs[claim_id]

        def execute() -> dict[str, object]:
            self._worker.start(handoff, expected_revision=revision)
            return {"version": 1, "status": "accepted", "settlement": "unknown", "authority": "historical-only"}

        return self._recorded(request, handoff, execute, require_prepared=True)

    def _quiesce(self, request: dict[str, object]) -> object:
        if set(request) != {"version", "action", "command_id", "expected_revision", "claim_id", "receipt"}:
            raise LocalChannelRefused("invalid quiescence request")
        command_id, revision, claim_id, receipt = (
            request["command_id"],
            request["expected_revision"],
            request["claim_id"],
            request["receipt"],
        )
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
            or type(claim_id) is not str
            or claim_id not in self._handoffs
            or type(receipt) is not str
            or not receipt.strip()
            or len(receipt) > 256
        ):
            raise LocalChannelRefused("invalid quiescence values or unbound handoff")
        handoff = self._handoffs[claim_id]
        if handoff.claim.mode is not HandoffMode.PULL:
            raise LocalChannelRefused("a worker string cannot settle a managed runtime")

        def execute() -> dict[str, object]:
            self._worker.quiesce(handoff, receipt, expected_revision=revision)
            return {
                "version": 1,
                "status": "accepted",
                "settlement": "cooperative-task-scope",
                "authority": "historical-only",
            }

        return self._recorded(request, handoff, execute)

    def _candidate_action(self, request: dict[str, object]) -> object:
        if set(request) != {"version", "action", "command_id", "expected_revision", "claim_id"}:
            raise LocalChannelRefused("invalid candidate request")
        command_id, revision, claim_id = request["command_id"], request["expected_revision"], request["claim_id"]
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
            or type(claim_id) is not str
            or claim_id not in self._handoffs
        ):
            raise LocalChannelRefused("invalid candidate values or unbound handoff")
        handoff = self._handoffs[claim_id]

        def execute() -> dict[str, object]:
            if request["action"] == "submit":
                subject = self._worker.worker.submit(handoff.claim, expected_revision=revision)
                return {"version": 1, "status": "submitted", "subject": subject, "authority": "historical-only"}
            outcome = self._worker.report(handoff, expected_revision=revision)
            return {"version": 1, "status": outcome, "settlement": "unknown", "authority": "historical-only"}

        result = self._recorded(request, handoff, execute)
        if request["action"] == "report" and self._notifier is not None:
            self._notifier.publish(self._owner.snapshot())
        return result

    def _renew(self, request: dict[str, object]) -> object:
        if set(request) != {"version", "action", "command_id", "expected_revision", "claim_id", "lease_seconds"}:
            raise LocalChannelRefused("invalid renewal request")
        command_id, revision, claim_id, lease = (
            request["command_id"],
            request["expected_revision"],
            request["claim_id"],
            request["lease_seconds"],
        )
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
            or type(claim_id) is not str
            or claim_id not in self._handoffs
            or type(lease) is not int
            or lease < 1
        ):
            raise LocalChannelRefused("invalid renewal values or unbound handoff")
        handoff = self._handoffs[claim_id]

        def execute() -> dict[str, object]:
            qualified = command_prefix(self._owner.scope) + digest(
                encode([self._worker.principal.actor_id, command_id])
            )
            expires_at = self._worker.renew(qualified, handoff, lease_seconds=lease, expected_revision=revision)
            return {"version": 1, "status": "recorded", "expires_at": expires_at, "authority": "historical-only"}

        return self._recorded(request, handoff, execute)

    def _recorded(
        self,
        request: dict[str, object],
        handoff: OrdinaryHandoff,
        execute: Callable[[], dict[str, object]],
        *,
        require_prepared: bool = False,
    ) -> dict[str, object]:
        command_id = cast(str, request["command_id"])
        revision = cast(int, request["expected_revision"])
        store = self._worker.application.store
        operation_id = command_prefix(self._owner.scope) + digest(encode([self._worker.principal.actor_id, command_id]))
        exact = {
            **request,
            "scope": self._owner.scope,
            "actor": self._worker.principal.actor_id,
            "attempt_digest": handoff.attempt.digest,
            "generation": handoff.claim.generation,
        }
        prior = store.find_operation(operation_id)
        if prior is not None:
            if prior.effect_key != operation_id or object_from(prior.request_json.encode()).get("request") != exact:
                raise LocalChannelRefused("command identity already has different input")
            receipt = store.find_artifact(operation_id, "receipt")
            if receipt is None:
                return {"status": "unknown", "settlement": "unknown"}
            result = object_from(receipt)
            if not self._valid_receipt(cast(str, request["action"]), result):
                raise LocalChannelRefused("recorded command receipt is invalid")
            return result  # Historical receipt only; no dispatch or authority renewal.
        self._worker.application.check(self._worker.approved)
        program = store.load(self._owner.scope)
        if program.revision != revision:
            raise LocalChannelRefused("caller expected revision is stale")
        if require_prepared and program.attempt(handoff.attempt.attempt_id).status is not AttemptStatus.PREPARED:
            raise LocalChannelRefused("a new start command requires an unstarted Attempt")
        store.intent(operation_id, operation_id, exact)
        result = execute()
        if not self._valid_receipt(cast(str, request["action"]), result):
            raise LocalChannelRefused("command returned an invalid result")
        store.finalize_artifact(operation_id, "receipt", encode(result))
        return result

    @staticmethod
    def _valid_receipt(action: str, result: dict[str, object]) -> bool:
        if type(result.get("version")) is not int or result.get("version") != 1:
            return False
        if action == "start":
            return result == {
                "version": 1,
                "status": "accepted",
                "settlement": "unknown",
                "authority": "historical-only",
            }
        if action == "quiesce":
            return result == {
                "version": 1,
                "status": "accepted",
                "settlement": "cooperative-task-scope",
                "authority": "historical-only",
            }
        if action == "submit":
            subject = result.get("subject")
            return (
                set(result) == {"version", "status", "subject", "authority"}
                and result["version"] == 1
                and result["status"] == "submitted"
                and result["authority"] == "historical-only"
                and type(subject) is str
                and re.fullmatch(r"[0-9a-f]{64}", subject) is not None
            )
        if action == "report":
            return (
                set(result) == {"version", "status", "settlement", "authority"}
                and result["version"] == 1
                and result["status"]
                in {
                    "accepted",
                    "rejected",
                    "review_unknown",
                    "owner_decision_needed",
                    "comment",
                    "invalid",
                    "expired",
                    "cancel_requested",
                }
                and result["settlement"] == "unknown"
                and result["authority"] == "historical-only"
            )
        if action == "renew":
            return (
                set(result) == {"version", "status", "expires_at", "authority"}
                and result["version"] == 1
                and result["status"] == "recorded"
                and type(result["expires_at"]) is int
                and result["expires_at"] > 0
                and result["authority"] == "historical-only"
            )
        return False


class LocalWorkerSession:
    """Trusted host binds a worker once and may pass separate one-shot client descriptors."""

    def __init__(
        self, owner: OwnerProjection, worker: OrdinaryWorkClient, *, notifier: LocalDecisionNotifier | None = None
    ) -> None:
        if worker.approved.program.program_id != owner.scope:
            raise LocalChannelRefused("worker and projection scopes differ")
        self._owner, self._worker = owner, worker
        self._notifier = notifier
        self._handoffs: dict[str, OrdinaryHandoff] = {}
        self._channels: list[LocalWorkerChannel] = []
        self._revoked = False

    def attach(self, channel: socket.socket) -> LocalWorkerChannel:
        if self._revoked:
            raise LocalChannelRefused("worker capability was revoked")
        if len(self._channels) >= 64:
            raise LocalChannelRefused("worker capability channel limit reached")
        result = LocalWorkerChannel(
            self._owner, channel, self._worker, handoffs=self._handoffs, notifier=self._notifier
        )
        self._channels.append(result)
        return result

    def detach(self, channel: LocalWorkerChannel) -> None:
        if channel not in self._channels:
            raise LocalChannelRefused("channel is not owned by this capability")
        self._channels.remove(channel)
        channel.revoke()

    def revoke(self) -> None:
        """Revoke communication only; this does not cancel or settle the work."""
        self._revoked = True
        for channel in self._channels:
            channel.revoke()
        self._channels.clear()
        self._handoffs.clear()


class LocalOwnerChannel(LocalReadChannel):
    """Host-bound owner capability; a request cannot supply or elevate a principal."""

    def __init__(
        self,
        owner: OwnerProjection,
        channel: socket.socket,
        application: OrdinaryProgram,
        approved: OrdinaryApproval | None,
        *,
        proposal: OrdinaryProposal | None = None,
        control: WorkOwnership | None = None,
        handoff: OrdinaryHandoff | None = None,
        managed: OrdinaryManagedRun | None = None,
    ) -> None:
        if (
            application.owner.role != "owner"
            or (approved is None and proposal is None)
            or (approved is not None and approved.program.program_id != owner.scope)
            or (proposal is not None and proposal.program_id != owner.scope)
            or ((control is None) != (handoff is None))
            or (handoff is not None and (approved is None or handoff.attempt.program_id != owner.scope))
            or (
                managed is not None
                and (
                    handoff is None
                    or (managed.handoff is not None and managed.handoff != handoff)
                    or managed.client.application is not application
                )
            )
        ):
            raise LocalChannelRefused("owner and projection scopes differ")
        super().__init__(owner, channel)
        self._application, self._approved = application, approved
        self._proposal = proposal
        self._control, self._handoff = control, handoff
        self._managed = managed

    @property
    def approved(self) -> OrdinaryApproval | None:
        """Return the host-bound approval; it is never serialized to a client."""
        return self._approved

    def _dispatch(self, request: dict[str, object]) -> object:
        if request.get("action") == "approve":
            return self._approve(request)
        if request.get("action") == "reconcile":
            return self._reconcile(request)
        if request.get("action") == "start-managed":
            return self._start_managed(request)
        if request.get("action") != "stop":
            return super()._dispatch(request)
        if self._approved is None:
            raise LocalChannelRefused("STOP requires a host-bound original approval")
        if set(request) != {"version", "action", "command_id", "expected_revision", "reason"}:
            raise LocalChannelRefused("invalid owner STOP request")
        command_id, revision, reason = request["command_id"], request["expected_revision"], request["reason"]
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
            or type(reason) is not str
            or not reason.strip()
            or len(reason) > 512
        ):
            raise LocalChannelRefused("invalid owner STOP values")
        store = self._application.store
        operation_id = command_prefix(self._owner.scope) + digest(
            encode([self._application.owner.actor_id, command_id])
        )
        exact = {
            **request,
            "scope": self._owner.scope,
            "actor": self._application.owner.actor_id,
            "decision_id": self._approved.decision_id,
            "spec_digest": self._approved.program.spec.digest,
            "bound_attempt": None if self._handoff is None else self._handoff.attempt.digest,
        }
        prior = store.find_operation(operation_id)
        if prior is not None:
            if prior.effect_key != operation_id or object_from(prior.request_json.encode()).get("request") != exact:
                raise LocalChannelRefused("command identity already has different input")
            receipt = store.find_artifact(operation_id, "receipt")
            if receipt is None:
                return {"status": "unknown", "settlement": "unknown"}
            result = object_from(receipt)
            if (
                set(result) != {"version", "status", "settlement", "authority"}
                or type(result.get("version")) is not int
                or result.get("version") != 1
                or result.get("status") != "accepted"
                or result.get("settlement") not in {"unknown", "bound-managed-target-settled"}
                or result.get("authority") != "historical-only"
            ):
                raise LocalChannelRefused("recorded STOP receipt is invalid")
            return result  # Historical receipt, not proof of process settlement.
        if store.load(self._owner.scope).revision != revision:
            raise LocalChannelRefused("caller expected revision is stale")
        store.intent(operation_id, operation_id, exact)
        self._application.stop(self._approved, reason, expected_revision=revision)
        settled = self._managed is not None and self._managed.stop(reason) == "cancelled"
        result: dict[str, object] = {
            "version": 1,
            "status": "accepted",
            "settlement": "bound-managed-target-settled" if settled else "unknown",
            "authority": "historical-only",
        }
        store.finalize_artifact(operation_id, "receipt", encode(result))
        return result

    def _start_managed(self, request: dict[str, object]) -> object:
        if (
            self._managed is None
            or self._handoff is None
            or self._approved is None
            or set(request) != {"version", "action", "command_id", "expected_revision"}
        ):
            raise LocalChannelRefused("a host-bound managed target and exact start request are required")
        command_id, revision = request["command_id"], request["expected_revision"]
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
        ):
            raise LocalChannelRefused("invalid managed start values")
        store = self._application.store
        operation_id = command_prefix(self._owner.scope) + digest(
            encode([self._application.owner.actor_id, command_id])
        )
        exact = {
            **request,
            "scope": self._owner.scope,
            "actor": self._application.owner.actor_id,
            "attempt_digest": self._handoff.attempt.digest,
            "generation": self._handoff.claim.generation,
        }
        result: dict[str, object] = {
            "version": 1,
            "status": "accepted",
            "settlement": "unknown",
            "authority": "historical-only",
        }
        prior = store.find_operation(operation_id)
        if prior is not None:
            if prior.effect_key != operation_id or object_from(prior.request_json.encode()).get("request") != exact:
                raise LocalChannelRefused("command identity already has different input")
            receipt = store.find_artifact(operation_id, "receipt")
            if receipt is None:
                return {"status": "unknown", "settlement": "unknown"}
            if object_from(receipt) != result:
                raise LocalChannelRefused("recorded managed start receipt is invalid")
            return result
        program = store.load(self._owner.scope)
        if (
            program.revision != revision
            or program.attempt(self._handoff.attempt.attempt_id).status is not AttemptStatus.PREPARED
        ):
            raise LocalChannelRefused("managed start needs the caller's current unstarted Attempt")
        store.intent(operation_id, operation_id, exact)
        self._managed.start(self._handoff, expected_revision=revision)
        store.finalize_artifact(operation_id, "receipt", encode(result))
        return result

    def _reconcile(self, request: dict[str, object]) -> object:
        if (
            self._control is None
            or self._handoff is None
            or set(request) != {"version", "action", "command_id", "expected_revision", "subject", "evidence"}
        ):
            raise LocalChannelRefused("a host-bound exact resource and reconciliation request are required")
        command_id, revision, subject, evidence = (
            request["command_id"],
            request["expected_revision"],
            request["subject"],
            request["evidence"],
        )
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < 0
            or type(subject) is not str
            or re.fullmatch(r"[0-9a-f]{64}", subject) is None
            or type(evidence) is not str
            or not evidence.strip()
            or len(evidence) > 512
        ):
            raise LocalChannelRefused("invalid owner reconciliation values")
        store = self._application.store
        operation_id = command_prefix(self._owner.scope) + digest(
            encode([self._application.owner.actor_id, command_id])
        )
        exact = {
            **request,
            "scope": self._owner.scope,
            "actor": self._application.owner.actor_id,
            "attempt_digest": self._handoff.attempt.digest,
            "generation": self._handoff.claim.generation,
        }
        prior = store.find_operation(operation_id)
        result: dict[str, object] = {"version": 1, "status": "recorded", "authority": "historical-only"}
        if prior is not None:
            if prior.effect_key != operation_id or object_from(prior.request_json.encode()).get("request") != exact:
                raise LocalChannelRefused("command identity already has different input")
            receipt = store.find_artifact(operation_id, "receipt")
            if receipt is None:
                return {"status": "unknown", "settlement": "unknown"}
            if object_from(receipt) != result:
                raise LocalChannelRefused("recorded reconciliation receipt is invalid")
            return result
        if store.load(self._owner.scope).revision != revision:
            raise LocalChannelRefused("caller expected revision is stale")
        store.intent(operation_id, operation_id, exact)
        self._control.reconcile(self._handoff.claim, subject, evidence=evidence, expected_revision=revision)
        store.finalize_artifact(operation_id, "receipt", encode(result))
        return result

    def _approve(self, request: dict[str, object]) -> object:
        if self._proposal is None or set(request) != {
            "version",
            "action",
            "command_id",
            "expected_revision",
            "decision_id",
            "expires_at",
        }:
            raise LocalChannelRefused("a host-bound proposal and exact approval request are required")
        command_id, revision, decision_id, expires_at = (
            request["command_id"],
            request["expected_revision"],
            request["decision_id"],
            request["expires_at"],
        )
        if (
            type(command_id) is not str
            or not command_id.strip()
            or len(command_id) > 256
            or type(revision) is not int
            or revision < -1
            or type(decision_id) is not str
            or not decision_id.strip()
            or len(decision_id) > 256
            or type(expires_at) is not int
            or expires_at < 1
        ):
            raise LocalChannelRefused("invalid approval request")
        store = self._application.store
        operation_id = command_prefix(self._owner.scope) + digest(
            encode([self._application.owner.actor_id, command_id])
        )
        exact = {
            **request,
            "scope": self._owner.scope,
            "actor": self._application.owner.actor_id,
            "proposal": self._proposal.digest,
            "policy": self._application.policy.digest,
        }
        prior = store.find_operation(operation_id)
        if prior is not None:
            if prior.effect_key != operation_id or object_from(prior.request_json.encode()).get("request") != exact:
                raise LocalChannelRefused("command identity already has different input")
            receipt = store.find_artifact(operation_id, "receipt")
            if receipt is None:
                return {"status": "unknown", "settlement": "unknown"}
            result = object_from(receipt)
            if (
                set(result) != {"version", "status", "decision_id", "spec_digest", "expires_at", "authority"}
                or type(result.get("version")) is not int
                or result.get("version") != 1
                or result.get("status") != "approved"
                or result.get("decision_id") != decision_id
                or result.get("authority") != "historical-only"
            ):
                raise LocalChannelRefused("recorded approval receipt is invalid")
            return result
        current = store.find_program(self._owner.scope)
        if (-1 if current is None else current.revision) != revision:
            raise LocalChannelRefused("caller expected revision is stale")
        store.intent(operation_id, operation_id, exact)
        approved = self._application.approve(
            self._proposal, decision_id=decision_id, expires_at=expires_at, expected_revision=revision
        )
        self._approved = approved
        result: dict[str, object] = {
            "version": 1,
            "status": "approved",
            "decision_id": decision_id,
            "spec_digest": approved.program.spec.digest,
            "expires_at": approved.expires_at,
            "authority": "historical-only",
        }
        store.finalize_artifact(operation_id, "receipt", encode(result))
        return result


class LocalOwnerSession:
    """Trusted owner binding shared by separate one-shot local client descriptors."""

    def __init__(
        self,
        owner: OwnerProjection,
        application: OrdinaryProgram,
        approved: OrdinaryApproval | None,
        *,
        proposal: OrdinaryProposal | None = None,
        control: WorkOwnership | None = None,
        handoff: OrdinaryHandoff | None = None,
        managed: OrdinaryManagedRun | None = None,
    ) -> None:
        if application.owner.role != "owner" or (approved is None and proposal is None):
            raise LocalChannelRefused("host-bound owner and approved work or proposal are required")
        self._owner, self._application, self._approved = owner, application, approved
        self._proposal, self._control, self._handoff, self._managed = proposal, control, handoff, managed
        self._channels: list[LocalOwnerChannel] = []
        self._revoked = False

    def attach(self, channel: socket.socket) -> LocalOwnerChannel:
        if self._revoked or len(self._channels) >= 64:
            raise LocalChannelRefused("owner capability revoked or channel limit reached")
        result = LocalOwnerChannel(
            self._owner,
            channel,
            self._application,
            self._approved,
            proposal=self._proposal,
            control=self._control,
            handoff=self._handoff,
            managed=self._managed,
        )
        self._channels.append(result)
        return result

    def detach(self, channel: LocalOwnerChannel) -> None:
        if channel not in self._channels:
            raise LocalChannelRefused("channel is not owned by this capability")
        self._channels.remove(channel)
        if channel.approved is not None:
            self._approved = channel.approved
        channel.revoke()

    def revoke(self) -> None:
        """Revoke communication only; this does not cancel or settle the work."""
        self._revoked = True
        for channel in self._channels:
            channel.revoke()
        self._channels.clear()
