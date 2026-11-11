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
    def served_change_token(self) -> tuple[int, int]: ...
    def served_authority_horizon(self, program_id: str) -> dict[str, object]: ...
    def served_claims(self, program_id: str, *, offset: int = 0, limit: int = 8) -> tuple[dict[str, object], ...]: ...
    def served_claim_count(self, program_id: str) -> int: ...
    def served_expired_claim_count(self, program_id: str, observed_at: int) -> int: ...
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
    page: int
    coverage: dict[str, tuple[int, int, bool]]
    position: ProgressPosition


class OwnerProjection:
    """One scope on the already-owning store thread; progress is an in-memory hint."""

    def __init__(self, store: ServedStore, program_id: str, *, capacity: int = 256) -> None:
        self._store = store
        self._scope = program_id
        cursor_scope = program_id if len(encode(program_id)) <= 4096 else "sha256:" + digest(encode(program_id))
        self._buffer = ProgressBuffer(uuid4().hex, cursor_scope, capacity=capacity)
        self._last_digest: str | None = None

    @property
    def scope(self) -> str:
        return self._scope

    def uses_store(self, store: object) -> bool:
        """Check the live host composition, not durable producer identity."""
        return self._store is store

    def snapshot(self, *, page: int = 0) -> ServedSnapshot:
        if type(page) is not int or page < 0 or page > 1_000_000:
            raise ValueError("invalid snapshot page")
        source_token = self._store.served_change_token()
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
        if type(raw_reservations) is not list:
            raise ValueError("producer reservation projection is unavailable or exceeds its bound")
        reservations: list[tuple[str, str, str, str | None]] = []
        decisions: list[dict[str, object]] = []
        reviews: dict[str, list[bool]] = {}
        review_operations: dict[str, tuple[str, bool]] = {}
        for raw in cast(list[object], raw_reservations):
            if type(raw) is not dict:
                raise ValueError("producer returned an invalid reservation")
            row = cast(dict[str, object], raw)
            attempt, operation, category = row.get("attempt"), row.get("operation"), row.get("category")
            if any(type(item) is not str or not item for item in (attempt, operation, category)):
                raise ValueError("producer returned an invalid reservation identity")
            operation_id = cast(str, operation)
            found = self._store.find_operation(operation_id)
            reservations.append(
                (cast(str, attempt), operation_id, cast(str, category), None if found is None else found.status)
            )
            if category == "review" and found is not None:
                raw_review = self._store.find_artifact(operation_id, "review-request")
                if raw_review is None and found.status not in {"unknown", "dispatched", "accepted"}:
                    reviews.setdefault(cast(str, attempt), []).append(False)
                    continue
                if raw_review is None or operation_id != "product-review:" + digest(raw_review):
                    raise ValueError("unresolved review lacks its exact request evidence")
                review = object_from(raw_review)
                expected = next(
                    (item for item in (() if program is None else program.attempts) if item.attempt_id == attempt),
                    None,
                )
                subject = review.get("subject")
                if (
                    expected is None
                    or review.get("author_attempt_id") != attempt
                    or type(subject) is not dict
                    or cast(dict[str, object], subject).get("attempt_digest") != expected.spec.digest
                ):
                    raise ValueError("unresolved review differs from its Attempt")
                receipt = self._store.find_artifact(operation_id, "review-receipt")
                resolved = found.status == "accepted" and receipt is not None
                reviews.setdefault(cast(str, attempt), []).append(resolved)
                review_operations[operation_id] = (cast(str, attempt), resolved)
                if found.status in {"unknown", "dispatched"} and receipt is None:
                    decisions.append(
                        {
                            "source": "product-review-operation",
                            "attempt": attempt,
                            "status": "review_unknown",
                            "decision_id": operation_id,
                        }
                    )
        retained = tuple(sorted(reservations))
        commands: list[tuple[str, str, str, str, str]] = []
        for operation in self._store.served_operations(self._scope):
            outer = object_from(operation.request_json.encode())
            raw_request = outer.get("request")
            if type(raw_request) is not dict:
                raise ValueError("producer returned an invalid served command")
            request = cast(dict[str, object], raw_request)
            action, actor, key = request.get("action"), request.get("actor"), request.get("command_id")
            if (
                request.get("scope") != self._scope
                or any(type(item) is not str or not item for item in (action, actor, key))
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
                    decision = {
                        "source": "served-command",
                        "command_id": key,
                        "attempt": attempt_id,
                        "status": value["status"],
                    }
                    review_operation_id = value.get("review_operation_id")
                    if review_operation_id is not None:
                        if type(review_operation_id) is not str:
                            raise ValueError("producer returned invalid original review identity")
                        decision["review_operation_id"] = review_operation_id
                    if value["status"] == "review_unknown" and type(review_operation_id) is str:
                        decision["decision_id"] = review_operation_id
                    elif value["status"] == "review_unknown" and attempt_id is not None:
                        matching_reviews = [
                            operation_id
                            for operation_id, (review_attempt, _) in review_operations.items()
                            if review_attempt == attempt_id
                        ]
                        if len(matching_reviews) == 1:
                            decision["decision_id"] = matching_reviews[0]
                    elif (
                        value["status"] in {"rejected", "owner_decision_needed", "comment", "invalid"}
                        and attempt_id is not None
                    ):
                        rejection_id = f"rejection:{attempt_id}"
                        if self._store.find_artifact(rejection_id, "rejected-result") is not None:
                            decision["decision_id"] = rejection_id
                    decisions.append(decision)
            commands.append((cast(str, key), cast(str, action), cast(str, actor), operation.status, recorded))
        command_facts = tuple(sorted(commands))
        command_total = self._store.served_operation_count(self._scope)
        if type(command_total) is not int or command_total < len(command_facts):
            raise ValueError("producer command coverage is inconsistent")
        command_coverage = (len(command_facts), command_total, command_total == len(command_facts))
        claim_count = self._store.served_claim_count(self._scope) if program is not None else 0
        claims = self._store.served_claims(self._scope, offset=page * 8) if program is not None else ()
        if type(claims) is not tuple:
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
            if any(item is not None and type(item) is not str for item in (head, subject, handle)):
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
                    {
                        "source": "product-review",
                        "attempt": attempt.attempt_id,
                        "status": status or "rejected",
                        "decision_id": rejection_id,
                    }
                )
        correlation_facts = tuple(correlations)
        decision_facts_list: list[dict[str, object]] = []
        for decision in decisions:
            attempt = next(
                (
                    item
                    for item in (() if program is None else program.attempts)
                    if item.attempt_id == decision.get("attempt")
                ),
                None,
            )
            unit = next(
                (
                    item
                    for item in (() if program is None else program.work_unit_states)
                    if attempt is not None and item.work_unit_id == attempt.spec.work_unit_id
                ),
                None,
            )
            if program is None or attempt is None or unit is None:
                applicability = "unknown"
            elif (
                program.status.value in {"completed", "cancelled"}
                or unit.status.value in {"satisfied", "cancelled"}
                or attempt.spec.spec_digest != program.spec.digest
                or (
                    decision.get("source") == "served-command"
                    and decision.get("status") == "review_unknown"
                    and (
                        review_operations.get(cast(str, decision.get("review_operation_id")))
                        == (attempt.attempt_id, True)
                        if decision.get("review_operation_id") is not None
                        else reviews.get(attempt.attempt_id) == [True]
                    )
                )
            ):
                applicability = "historical"
            else:
                applicability = "pending"
            decision_facts_list.append({**decision, "applicability": applicability})
        decision_facts = tuple(decision_facts_list)
        raw_totals = budget.get("resource_totals", [])
        if type(raw_totals) is not list:
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
                any(type(item) is not str or not item for item in (metric, unit, provenance, subtotal, coverage))
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
            "claim_count": claim_count,
            "correlations": correlation_facts,
            "decisions": decision_facts,
            "usage_subtotals": usage_subtotals,
            "budget_configured": configured,
            "budget_revoked": revoked,
            "authority": {**authority, "observed_at": None},
            "expired_claim_count": (
                None if observed is None else self._store.served_expired_claim_count(self._scope, observed)
            ),
            "missing_usage": missing,
            "source_token": source_token,
        }
        if self._store.served_change_token() != source_token:
            raise ValueError("producer changed during snapshot; resync required")
        fingerprint = digest(encode(facts))
        if self._last_digest is not None and fingerprint != self._last_digest:
            # Polling a snapshot cannot prove that intermediate durable facts were observed.
            self._buffer.append("resync-required", 0 if program is None else program.revision)
        self._last_digest = fingerprint
        decision_facts = tuple(sorted(decision_facts, key=lambda item: item.get("applicability") != "pending"))
        collections = {
            "work_units": work_units,
            "attempts": attempts,
            "reservations": retained,
            "claims": claims,
            "correlations": correlation_facts,
            "decisions": decision_facts,
            "usage_subtotals": usage_subtotals,
        }
        offset = page * 8
        coverage = {
            name: (min(offset, len(items)), len(items), offset + 8 >= len(items)) for name, items in collections.items()
        }
        coverage["claims"] = (min(offset, claim_count), claim_count, offset + 8 >= claim_count)
        return ServedSnapshot(
            1,
            self._buffer.position.producer,
            self._scope,
            None if program is None else program.revision,
            None if program is None else program.spec.digest,
            "absent" if program is None else program.status.value,
            work_units[offset : offset + 8],
            attempts[offset : offset + 8],
            retained[offset : offset + 8],
            command_facts,
            command_coverage,
            claims,
            correlation_facts[offset : offset + 8],
            decision_facts[offset : offset + 8],
            usage_subtotals[offset : offset + 8],
            configured,
            revoked,
            authority,
            missing,
            None,
            None,
            page,
            coverage,
            self._buffer.position,
        )

    def events(self, after: ProgressPosition, *, limit: int = 256) -> ProgressBatch:
        """Read retained hints without invoking a command, dispatcher or clock renewal."""
        snapshot = self.snapshot()
        result = self._buffer.read(after, limit=limit)
        if not snapshot.command_coverage[2] or any(not item[2] for item in snapshot.coverage.values()):
            return ProgressBatch((), snapshot.position, True)
        return result
