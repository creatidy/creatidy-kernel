# SPDX-License-Identifier: Apache-2.0
"""Owner-thread state projections; no remote authentication or transport is implied."""

from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

from creatidy_kernel.core.domain import Program
from creatidy_kernel.core.intake import digest, encode, object_from
from creatidy_kernel.core.served import ProgressBatch, ProgressBuffer, ProgressPosition, command_prefix
from creatidy_kernel.ports.program_store import OperationRecord


class ServedStore(Protocol):
    def load(self, program_id: str) -> Program: ...
    def find_program(self, program_id: str) -> Program | None: ...
    def has_work_budget(self, program_id: str) -> bool: ...
    def work_budget_status(self, program_id: str) -> dict[str, object]: ...
    def work_budget_revoked(self, program_id: str) -> bool | None: ...
    def find_operation(self, operation_id: str) -> OperationRecord | None: ...
    def served_operations(self, program_id: str) -> tuple[OperationRecord, ...]: ...
    def served_operation_count(self, program_id: str) -> int: ...
    def served_authority_horizon(self, program_id: str) -> dict[str, object]: ...
    def served_claims(self, program_id: str) -> tuple[dict[str, object], ...]: ...
    def find_artifact(self, operation_id: str, name: str) -> bytes | None: ...


@dataclass(frozen=True, slots=True)
class ServedSnapshot:
    version: int
    producer: str
    scope: str
    program_revision: int | None
    spec_digest: str | None
    status: str
    work_units: tuple[tuple[str, str, str | None, str | None], ...]
    attempts: tuple[tuple[str, str, str, str], ...]
    reservations: tuple[tuple[str, str, str, str | None], ...]
    commands: tuple[tuple[str, str, str, str, str], ...]
    command_coverage: tuple[int, int, bool]
    claims: tuple[dict[str, object], ...]
    correlations: tuple[dict[str, object], ...]
    decisions: tuple[dict[str, object], ...]
    usage_subtotals: tuple[tuple[str, str, str, str, int, int, str], ...]
    budget_configured: bool
    budget_revoked: bool | None
    authority: dict[str, object]
    missing_usage: str
    route_identity: None
    physical_settlement: None
    position: ProgressPosition


class OwnerProjection:
    """One scope on the already-owning store thread; progress is an in-memory hint."""

    def __init__(self, store: ServedStore, program_id: str, *, capacity: int = 256) -> None:
        self._store = store
        self._scope = program_id
        self._buffer = ProgressBuffer(uuid4().hex, program_id, capacity=capacity)
        self._last_digest: str | None = None

    @property
    def scope(self) -> str:
        return self._scope

    def snapshot(self) -> ServedSnapshot:
        program = self._store.find_program(self._scope)
        configured = program is not None and self._store.has_work_budget(self._scope)
        budget = self._store.work_budget_status(self._scope) if configured else {}
        revoked = self._store.work_budget_revoked(self._scope) if program is not None else None
        if (configured and type(revoked) is not bool) or (not configured and revoked is not None):
            raise ValueError("producer returned inconsistent budget revocation")
        missing = budget.get("missing_usage", "unknown")
        if type(missing) is not str:
            raise ValueError("producer returned invalid usage coverage")
        work_units = tuple(
            (item.work_unit_id, item.status.value, item.active_attempt_id, item.satisfaction_id)
            for item in (() if program is None else program.work_unit_states)
        )
        attempts = tuple(
            (item.attempt_id, item.spec.work_unit_id, item.status.value, item.actor_id)
            for item in (() if program is None else program.attempts)
        )
        raw_reservations = budget.get("reservations", [])
        if type(raw_reservations) is not list or len(cast(list[object], raw_reservations)) > 4096:
            raise ValueError("producer reservation projection is unavailable or exceeds its bound")
        reservations: list[tuple[str, str, str, str | None]] = []
        for raw in cast(list[object], raw_reservations):
            if type(raw) is not dict:
                raise ValueError("producer returned an invalid reservation")
            row = cast(dict[str, object], raw)
            attempt, operation, category = row.get("attempt"), row.get("operation"), row.get("category")
            if any(type(item) is not str or not item or len(item) > 512 for item in (attempt, operation, category)):
                raise ValueError("producer returned an invalid reservation identity")
            operation_id = cast(str, operation)
            found = self._store.find_operation(operation_id)
            reservations.append(
                (cast(str, attempt), operation_id, cast(str, category), None if found is None else found.status)
            )
        retained = tuple(sorted(reservations))
        commands: list[tuple[str, str, str, str, str]] = []
        decisions: list[dict[str, object]] = []
        for operation in self._store.served_operations(self._scope):
            outer = object_from(operation.request_json.encode())
            raw_request = outer.get("request")
            if type(raw_request) is not dict:
                raise ValueError("producer returned an invalid served command")
            request = cast(dict[str, object], raw_request)
            action, actor, key = request.get("action"), request.get("actor"), request.get("command_id")
            if (
                request.get("scope") != self._scope
                or any(type(item) is not str or not item or len(item) > 512 for item in (action, actor, key))
                or operation.operation_id != command_prefix(self._scope) + digest(encode([actor, key]))
            ):
                raise ValueError("producer returned an inconsistent served command identity")
            receipt = self._store.find_artifact(operation.operation_id, "receipt")
            if receipt is None:
                recorded = "unknown"
            else:
                value = object_from(receipt)
                if (
                    type(value.get("version")) is not int
                    or value.get("version") != 1
                    or type(value.get("status")) is not str
                ):
                    raise ValueError("producer returned an invalid served command receipt")
                recorded = "recorded"
                if action == "report":
                    attempt_digest = request.get("attempt_digest")
                    attempt_id = next(
                        (
                            item.attempt_id
                            for item in (() if program is None else program.attempts)
                            if item.spec.digest == attempt_digest
                        ),
                        None,
                    )
                    decisions.append(
                        {
                            "source": "served-command",
                            "command_id": key,
                            "attempt": attempt_id,
                            "status": value["status"],
                        }
                    )
            commands.append((cast(str, key), cast(str, action), cast(str, actor), operation.status, recorded))
        command_facts = tuple(sorted(commands))
        command_total = self._store.served_operation_count(self._scope)
        if type(command_total) is not int or command_total < len(command_facts):
            raise ValueError("producer command coverage is inconsistent")
        command_coverage = (len(command_facts), command_total, command_total == len(command_facts))
        claims = self._store.served_claims(self._scope) if program is not None else ()
        if type(claims) is not tuple or len(claims) > 4096:
            raise ValueError("producer claim projection is unavailable or exceeds its bound")
        horizon = (
            self._store.served_authority_horizon(self._scope)
            if configured
            else {"deadline": None, "observed_at": None, "clock": "unknown"}
        )
        deadline, observed, clock = horizon.get("deadline"), horizon.get("observed_at"), horizon.get("clock")
        if (
            (deadline is not None and type(deadline) is not int)
            or (observed is not None and type(observed) is not int)
            or clock not in {"unknown", "uncommitted-display-sample"}
            or ((observed is None) != (clock == "unknown"))
        ):
            raise ValueError("producer returned invalid authority horizon")
        cutoff = (
            "revoked"
            if revoked
            else "unknown"
            if observed is None or deadline is None
            else "expired"
            if observed >= deadline
            else "not-expired-observed"
        )
        authority: dict[str, object] = {
            "task_deadline": deadline,
            "observed_at": observed,
            "clock": clock,
            "cutoff": cutoff,
            "safe_next_action": "reconcile-or-stop"
            if cutoff in {"expired", "revoked"}
            else "check-authority-before-dispatch",
        }
        qualified_claims: list[dict[str, object]] = []
        for claim in claims:
            expiry = claim.get("recorded_expires_at")
            if type(expiry) is not int:
                raise ValueError("producer returned invalid claim horizon")
            claim_cutoff = (
                "revoked"
                if revoked
                else "unknown"
                if observed is None
                else "expired"
                if observed >= expiry
                else "not-expired-observed"
            )
            qualified_claims.append({**claim, "cutoff": claim_cutoff})
        claims = tuple(qualified_claims)
        correlations: list[dict[str, object]] = []
        for attempt in () if program is None else program.attempts:
            operation_id = f"runtime:{attempt.attempt_id}"
            candidate_raw = self._store.find_artifact(operation_id, "ordinary-candidate")
            runtime_raw = self._store.find_artifact(operation_id, "runtime-receipt")
            candidate = None if candidate_raw is None else object_from(candidate_raw)
            runtime = None if runtime_raw is None else object_from(runtime_raw)
            head = None if candidate is None else candidate.get("head")
            subject = None if candidate is None else candidate.get("scope")
            handle = None if runtime is None else runtime.get("handle")
            if any(item is not None and (type(item) is not str or len(item) > 512) for item in (head, subject, handle)):
                raise ValueError("producer returned invalid candidate or session identity")
            correlations.append(
                {
                    "attempt": attempt.attempt_id,
                    "workspace_reference": attempt.spec.workspace_reference,
                    "candidate_head": head,
                    "candidate_scope": subject,
                    "runtime_handle": handle,
                }
            )
            rejection_id = f"rejection:{attempt.attempt_id}"
            if self._store.find_artifact(rejection_id, "rejected-result") is not None:
                rejection = self._store.find_operation(rejection_id)
                if rejection is None:
                    raise ValueError("rejection artifact has no originating operation")
                raw = object_from(rejection.request_json.encode()).get("request")
                if type(raw) is not dict:
                    raise ValueError("producer returned an invalid rejection")
                status = cast(dict[str, object], raw).get("product_review_status")
                if status not in {None, "changes_requested", "owner_decision_needed", "comment", "invalid"}:
                    raise ValueError("producer returned an invalid review disposition")
                decisions.append(
                    {"source": "product-review", "attempt": attempt.attempt_id, "status": status or "rejected"}
                )
        correlation_facts = tuple(correlations)
        decision_facts = tuple(decisions)
        raw_totals = budget.get("resource_totals", [])
        if type(raw_totals) is not list or len(cast(list[object], raw_totals)) > 1024:
            raise ValueError("producer usage projection is unavailable or exceeds its bound")
        totals: list[tuple[str, str, str, str, int, int, str]] = []
        for raw in cast(list[object], raw_totals):
            if type(raw) is not dict:
                raise ValueError("producer returned an invalid usage subtotal")
            row = cast(dict[str, object], raw)
            metric, unit, provenance, subtotal, coverage = (
                row.get("metric"),
                row.get("unit"),
                row.get("provenance"),
                row.get("known_subtotal"),
                row.get("coverage"),
            )
            unknown, unreported = row.get("unknown_receipts"), row.get("unreported_operations")
            if (
                any(
                    type(item) is not str or not item or len(item) > 512
                    for item in (metric, unit, provenance, subtotal, coverage)
                )
                or type(unknown) is not int
                or unknown < 0
                or type(unreported) is not int
                or unreported < 0
                or row.get("quantity") is not None
            ):
                raise ValueError("producer returned inconsistent usage coverage")
            totals.append(
                (
                    cast(str, metric),
                    cast(str, unit),
                    cast(str, provenance),
                    cast(str, subtotal),
                    unknown,
                    unreported,
                    cast(str, coverage),
                )
            )
        usage_subtotals = tuple(sorted(totals))
        facts = {
            "program_revision": None if program is None else program.revision,
            "spec_digest": None if program is None else program.spec.digest,
            "status": "absent" if program is None else program.status.value,
            "work_units": work_units,
            "attempts": attempts,
            "reservations": retained,
            "commands": command_facts,
            "command_coverage": command_coverage,
            "claims": claims,
            "correlations": correlation_facts,
            "decisions": decision_facts,
            "usage_subtotals": usage_subtotals,
            "budget_configured": configured,
            "budget_revoked": revoked,
            "authority": {**authority, "observed_at": None},
            "missing_usage": missing,
        }
        fingerprint = digest(encode(facts))
        if self._last_digest is not None and fingerprint != self._last_digest:
            # Polling a snapshot cannot prove that intermediate durable facts were observed.
            self._buffer.append("resync-required", 0 if program is None else program.revision)
        self._last_digest = fingerprint
        return ServedSnapshot(
            1,
            self._buffer.position.producer,
            self._scope,
            None if program is None else program.revision,
            None if program is None else program.spec.digest,
            "absent" if program is None else program.status.value,
            work_units,
            attempts,
            retained,
            command_facts,
            command_coverage,
            claims,
            correlation_facts,
            decision_facts,
            usage_subtotals,
            configured,
            revoked,
            authority,
            missing,
            None,
            None,
            self._buffer.position,
        )

    def events(self, after: ProgressPosition, *, limit: int = 256) -> ProgressBatch:
        """Read retained hints without invoking a command, dispatcher or clock renewal."""
        snapshot = self.snapshot()
        result = self._buffer.read(after, limit=limit)
        if not snapshot.command_coverage[2]:
            return ProgressBatch((), snapshot.position, True)
        return result
