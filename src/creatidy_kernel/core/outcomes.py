# SPDX-License-Identifier: Apache-2.0
"""Local outcome facts and pure export; never a routing or authority decision."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.resources import Allocation


def _text(value: str, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")


def _optional_text(value: str | None, name: str) -> None:
    if value is not None:
        _text(value, name)


class Provenance(StrEnum):
    ESTIMATE = "estimate"
    PROVIDER_REPORT = "provider_report"
    MEASUREMENT = "measurement"
    OWNER_RECORD = "owner_record"


@dataclass(frozen=True, slots=True)
class Identity:
    runtime_id: str | None
    provider_id: str | None
    model_id: str | None
    reasoning_effort: str | None

    def __post_init__(self) -> None:
        for name in ("runtime_id", "provider_id", "model_id", "reasoning_effort"):
            _optional_text(getattr(self, name), name)

    @classmethod
    def from_allocation(cls, allocation: Allocation) -> "Identity":
        return cls(allocation.runtime_id, allocation.provider_id, allocation.model_id, allocation.reasoning_effort)

    def payload(self) -> dict[str, str | None]:
        return {
            "runtime_id": self.runtime_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True, slots=True)
class AttributedIdentity:
    identity: Identity
    source: str
    observed_at: int
    fresh_until: int

    def __post_init__(self) -> None:
        if type(self.identity) is not Identity:
            raise ValueError("identity must be an Identity")
        _text(self.source, "source")
        if (
            type(self.observed_at) is not int
            or type(self.fresh_until) is not int
            or self.fresh_until < self.observed_at
        ):
            raise ValueError("identity observation requires valid freshness timestamps")

    def payload(self) -> dict[str, object]:
        return {
            "identity": self.identity.payload(),
            "source": self.source,
            "observed_at": self.observed_at,
            "fresh_until": self.fresh_until,
        }


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    reference: str
    source: str
    observed_at: int
    fresh_until: int

    def __post_init__(self) -> None:
        _text(self.reference, "reference")
        _text(self.source, "source")
        if (
            type(self.observed_at) is not int
            or type(self.fresh_until) is not int
            or self.fresh_until < self.observed_at
        ):
            raise ValueError("quota snapshot requires valid freshness timestamps")


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    attempt: AttemptSpec
    work_unit_class: str
    requested: Identity
    resolved: AttributedIdentity | None = None
    observed: AttributedIdentity | None = None
    quota: QuotaSnapshot | None = None
    validation: str | None = None
    finding_ids: tuple[str, ...] = ()
    remediation_of: str | None = None
    acceptance_id: str | None = None
    intervention_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.attempt) is not AttemptSpec or type(self.requested) is not Identity:
            raise ValueError("attempt and requested identity must be core values")
        _text(self.work_unit_class, "work_unit_class")
        for name in ("resolved", "observed"):
            value = getattr(self, name)
            if value is not None and type(value) is not AttributedIdentity:
                raise ValueError(f"{name} must have attributed identity")
        if self.quota is not None and type(self.quota) is not QuotaSnapshot:
            raise ValueError("quota must be a QuotaSnapshot")
        for name in ("validation", "remediation_of", "acceptance_id"):
            _optional_text(getattr(self, name), name)
        for name, values in (("finding_ids", self.finding_ids), ("intervention_ids", self.intervention_ids)):
            if type(values) is not tuple or len(values) != len(set(values)):
                raise ValueError(f"{name} must be a tuple of unique IDs")
            for value in values:
                _text(value, name)


@dataclass(frozen=True, slots=True)
class UsageObservation:
    observation_id: str
    attempt_id: str
    receipt_id: str
    metric: str
    unit: str
    quantity: Decimal | None
    provenance: Provenance
    source: str
    observed_at: int
    supersedes_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("observation_id", "attempt_id", "receipt_id", "metric", "unit", "source"):
            _text(getattr(self, name), name)
        _optional_text(self.supersedes_id, "supersedes_id")
        if self.quantity is not None and (
            type(self.quantity) is not Decimal or not self.quantity.is_finite() or self.quantity < 0
        ):
            raise ValueError("quantity must be a nonnegative finite Decimal or unknown")
        if type(self.provenance) is not Provenance or type(self.observed_at) is not int:
            raise ValueError("observation requires provenance and an integer timestamp")

    def payload(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "attempt_id": self.attempt_id,
            "receipt_id": self.receipt_id,
            "metric": self.metric,
            "unit": self.unit,
            "quantity": None if self.quantity is None else str(self.quantity),
            "provenance": self.provenance.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "supersedes_id": self.supersedes_id,
        }


def export_outcomes(
    outcomes: tuple[AttemptOutcome, ...], observations: tuple[UsageObservation, ...]
) -> dict[str, object]:
    """Export immutable facts plus latest per-receipt quantities, without imputing unknowns."""
    if type(outcomes) is not tuple or any(type(item) is not AttemptOutcome for item in outcomes):
        raise ValueError("outcomes must be an immutable tuple")
    if type(observations) is not tuple or any(type(item) is not UsageObservation for item in observations):
        raise ValueError("observations must be an immutable tuple")
    by_attempt = {item.attempt.attempt_id: item for item in outcomes}
    if len(by_attempt) != len(outcomes) or len({item.acceptance_id for item in outcomes if item.acceptance_id}) != sum(
        item.acceptance_id is not None for item in outcomes
    ):
        raise ValueError("attempt and acceptance identities must be unique")
    for item in outcomes:
        if item.remediation_of is not None:
            parent = by_attempt.get(item.remediation_of)
            if (
                parent is None
                or parent.attempt.program_id != item.attempt.program_id
                or parent.attempt.work_unit_id != item.attempt.work_unit_id
            ):
                raise ValueError("remediation must cite an Attempt in the same WorkUnit")
            seen = {item.attempt.attempt_id}
            while parent is not None:
                if parent.attempt.attempt_id in seen:
                    raise ValueError("remediation cycle")
                seen.add(parent.attempt.attempt_id)
                parent = by_attempt.get(parent.remediation_of) if parent.remediation_of is not None else None
    latest: dict[str, UsageObservation] = {}
    ids: dict[str, UsageObservation] = {}
    for observation in observations:
        if observation.attempt_id not in by_attempt or observation.observation_id in ids:
            raise ValueError("unknown Attempt or duplicate observation ID")
        prior = latest.get(observation.receipt_id)
        if prior is None:
            if observation.supersedes_id is not None:
                raise ValueError("correction lacks an earlier receipt")
        elif (
            observation.supersedes_id != prior.observation_id
            or observation.attempt_id != prior.attempt_id
            or observation.metric != prior.metric
            or observation.unit != prior.unit
        ):
            raise ValueError("duplicate receipt or invalid correction lineage")
        ids[observation.observation_id] = observation
        latest[observation.receipt_id] = observation

    def chain(item: AttemptOutcome) -> tuple[str, ...]:
        result = [item.attempt.attempt_id]
        parent = item.remediation_of
        while parent is not None:
            result.append(parent)
            parent = by_attempt[parent].remediation_of
        return tuple(reversed(result))

    exported: list[dict[str, object]] = []
    for item in outcomes:
        attempt_id = item.attempt.attempt_id
        effective = [fact.payload() for fact in latest.values() if fact.attempt_id == attempt_id]
        exported.append(
            {
                "program_id": item.attempt.program_id,
                "work_unit_id": item.attempt.work_unit_id,
                "work_unit_class": item.work_unit_class,
                "attempt_id": attempt_id,
                "attempt_spec_revision": item.attempt.spec_revision,
                "attempt_spec_digest": item.attempt.digest,
                "program_spec_digest": item.attempt.spec_digest,
                "allocation_reference": item.attempt.allocation_reference,
                "context_reference": item.attempt.context_reference,
                "requested": item.requested.payload(),
                "resolved": None if item.resolved is None else item.resolved.payload(),
                "observed": None if item.observed is None else item.observed.payload(),
                "quota": None
                if item.quota is None
                else {
                    "reference": item.quota.reference,
                    "source": item.quota.source,
                    "observed_at": item.quota.observed_at,
                    "fresh_until": item.quota.fresh_until,
                },
                "validation": item.validation,
                "finding_ids": list(item.finding_ids),
                "remediation_of": item.remediation_of,
                "acceptance_id": item.acceptance_id,
                "intervention_ids": list(item.intervention_ids),
                "effective_usage": effective,
                "cost_attempt_ids": list(chain(item)) if item.acceptance_id is not None else None,
            }
        )
    return {
        "schema": "creatidy-local-outcomes-v1",
        "attempts": exported,
        "observations": [item.payload() for item in observations],
    }
