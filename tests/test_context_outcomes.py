# SPDX-License-Identifier: Apache-2.0
"""Offline contracts for pinned context and non-authoritative local outcome export."""

from dataclasses import replace
from decimal import Decimal

import pytest

from creatidy_kernel.core.context import ContextPackage, ContextSource
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.outcomes import (
    AttemptOutcome,
    AttributedIdentity,
    Identity,
    Provenance,
    QuotaSnapshot,
    UsageObservation,
    export_outcomes,
)
from creatidy_kernel.core.resources import Allocation


def context() -> ContextPackage:
    return ContextPackage(
        "try-1",
        "spec-sha",
        (ContextSource("source", "rev-1", "sha-1", "selected text"),),
        "selection-sha",
        "redaction-sha",
        "compiler-1",
        100,
        20,
    )


def attempt(attempt_id: str, context_reference: str | None = None) -> AttemptSpec:
    return AttemptSpec(
        attempt_id,
        "program",
        "unit",
        1,
        "spec-sha",
        allocation_reference="allocation-1",
        context_reference=context_reference,
    )


def outcome(attempt_id: str, *, remediation_of: str | None = None, acceptance_id: str | None = None) -> AttemptOutcome:
    allocation = Allocation("harness", "provider", "model", frozenset(), 100, "fixed", "high")
    return AttemptOutcome(
        attempt(attempt_id),
        "code",
        Identity.from_allocation(allocation),
        resolved=AttributedIdentity(Identity("harness-v2", "provider", "model", "high"), "allocator", 10, 20),
        observed=AttributedIdentity(Identity("harness-v2", None, None, None), "runtime receipt", 11, 21),
        quota=QuotaSnapshot("quota-1", "provider", 9, 12),
        validation="rejected" if remediation_of is None else "passed",
        finding_ids=("finding-1",) if remediation_of is None else (),
        remediation_of=remediation_of,
        acceptance_id=acceptance_id,
        intervention_ids=("owner-1",),
    )


def usage(
    observation_id: str, attempt_id: str, receipt_id: str, quantity: Decimal | None, supersedes_id: str | None = None
) -> UsageObservation:
    return UsageObservation(
        observation_id,
        attempt_id,
        receipt_id,
        "input",
        "tokens",
        quantity,
        Provenance.PROVIDER_REPORT,
        "runtime",
        15,
        supersedes_id,
    )


def test_context_pins_selected_sources_policies_and_attempt_without_compiling() -> None:
    manifest = context()
    assert manifest.matches(attempt("try-1", manifest.digest))
    assert not manifest.matches(attempt("try-2", manifest.digest))
    assert not manifest.matches(attempt("try-1", "different"))
    changed = replace(manifest, sources=(ContextSource("source", "rev-2", "sha-2", "selected text"),))
    assert manifest.digest != changed.digest
    assert manifest.digest != replace(manifest, redaction_policy_digest="new-policy").digest
    assert manifest.payload()["max_tokens"] == 20


def test_context_rejects_unbounded_or_ambiguous_manifest() -> None:
    with pytest.raises(ValueError, match="byte budget"):
        replace(context(), max_bytes=1)
    with pytest.raises(ValueError, match="unique"):
        replace(context(), sources=(context().sources[0], context().sources[0]))
    with pytest.raises(ValueError, match="nonnegative"):
        replace(context(), max_tokens=-1)
    with pytest.raises(ValueError, match="nonnegative"):
        replace(context(), max_tokens=True)
    with pytest.raises(ValueError, match="sources"):
        replace(context(), sources=[context().sources[0]])  # type: ignore[arg-type]


def test_export_preserves_unknown_provenance_freshness_and_remediation_cost() -> None:
    first = outcome("try-1")
    second = outcome("try-2", remediation_of="try-1", acceptance_id="accepted-1")
    facts = (
        usage("obs-1", "try-1", "receipt-1", None),
        usage("obs-2", "try-1", "receipt-1", Decimal("12.50"), "obs-1"),
        usage("obs-3", "try-2", "receipt-2", None),
    )
    exported = export_outcomes((first, second), facts)
    assert exported["schema"] == "creatidy-local-outcomes-v1"
    assert exported["observations"] == [fact.payload() for fact in facts]
    attempts = exported["attempts"]
    assert isinstance(attempts, list)
    assert attempts[0]["effective_usage"] == [facts[1].payload()]
    assert attempts[1]["effective_usage"] == [facts[2].payload()]
    assert attempts[1]["cost_attempt_ids"] == ["try-1", "try-2"]
    assert attempts[0]["requested"]["reasoning_effort"] == "high"
    assert attempts[0]["observed"]["identity"]["provider_id"] is None
    assert attempts[0]["quota"]["fresh_until"] == 12
    assert attempts[0]["finding_ids"] == ["finding-1"]


def test_duplicate_receipts_and_invalid_corrections_fail_closed() -> None:
    original = usage("obs-1", "try-1", "receipt", Decimal(4))
    for next_fact in (
        usage("obs-2", "try-1", "receipt", Decimal(4)),
        usage("obs-2", "try-1", "receipt", Decimal(5), "wrong"),
        usage("obs-1", "try-1", "other", Decimal(5)),
        usage("obs-2", "try-2", "receipt", Decimal(5), "obs-1"),
    ):
        with pytest.raises(ValueError):
            export_outcomes((outcome("try-1"), outcome("try-2")), (original, next_fact))
    with pytest.raises(ValueError, match="earlier receipt"):
        export_outcomes((outcome("try-1"),), (usage("obs-2", "try-1", "receipt", None, "obs-1"),))


def test_outcome_rejects_invalid_identity_freshness_quantities_and_cross_unit_links() -> None:
    with pytest.raises(ValueError, match="freshness"):
        AttributedIdentity(Identity(None, None, None, None), "runtime", 20, 10)
    with pytest.raises(ValueError, match="quantity"):
        usage("obs", "try-1", "receipt", Decimal("NaN"))
    with pytest.raises(ValueError, match="quantity"):
        usage("obs", "try-1", "receipt", Decimal(-1))
    with pytest.raises(ValueError, match="same WorkUnit"):
        other = replace(
            outcome("try-2", remediation_of="try-1"), attempt=AttemptSpec("try-2", "program", "other", 1, "spec-sha")
        )
        export_outcomes((outcome("try-1"), other), ())
