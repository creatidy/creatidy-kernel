# SPDX-License-Identifier: Apache-2.0
"""Configured work capacity, distinct from measured resource consumption."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import cast


class BudgetRefused(ValueError):
    """Capacity or evidence does not permit another admission."""


class WorkKind(StrEnum):
    ATTEMPT = "attempt"
    REVIEW = "review"
    REMEDIATION = "remediation"
    TOOL = "tool"
    RETRY = "retry"
    HELPER = "helper"
    VERIFICATION = "verification"
    AUXILIARY = "auxiliary"


@dataclass(frozen=True, slots=True)
class ResourceCeiling:
    metric: str
    unit: str
    quantity: Decimal

    def __post_init__(self) -> None:
        if (
            self.metric not in {"money", "tokens", "quota", "compute", "elapsed", "attention"}
            or type(self.unit) is not str
            or not self.unit.strip()
            or type(self.quantity) is not Decimal
            or not self.quantity.is_finite()
            or self.quantity <= 0
        ):
            raise ValueError("finite positive unitful resource ceiling required")
        if self.metric == "money" and (
            len(self.unit) != 3 or not self.unit.isascii() or not self.unit.isupper() or not self.unit.isalpha()
        ):
            raise ValueError("money requires a separate ISO currency unit")

    def payload(self) -> dict[str, object]:
        return {"metric": self.metric, "unit": self.unit, "quantity": str(self.quantity)}


@dataclass(frozen=True, slots=True)
class WorkBudgetPolicy:
    """No model prices, quota conversions or implicit numerical owner budgets.

    Resource ceilings are stop-on-observed limits, not hard inference guarantees.
    Unknown consumption holds the entire remaining ceiling until reconciled.
    """

    counts: tuple[tuple[WorkKind, int], ...]
    max_active: int
    retained_bytes: int
    resources: tuple[ResourceCeiling, ...] = ()
    strict_inference: bool = False

    def __post_init__(self) -> None:
        if type(self.counts) is not tuple or len(self.counts) != len(WorkKind):
            raise ValueError("every work category needs an explicit finite count")
        if {kind for kind, _ in self.counts} != set(WorkKind) or any(
            type(kind) is not WorkKind or type(limit) is not int or limit < 0 for kind, limit in self.counts
        ):
            raise ValueError("invalid finite work counts")
        if (
            type(self.max_active) is not int
            or self.max_active <= 0
            or type(self.retained_bytes) is not int
            or self.retained_bytes <= 0
        ):
            raise ValueError("finite positive concurrency and retained-byte limits required")
        if (
            type(self.resources) is not tuple
            or any(type(item) is not ResourceCeiling for item in self.resources)
            or len({(item.metric, item.unit) for item in self.resources}) != len(self.resources)
        ):
            raise ValueError("resource ceilings must have unique metric/unit identities")
        if type(self.strict_inference) is not bool:
            raise ValueError("strict inference profile must be explicit")

    @classmethod
    def basic(
        cls, attempts: int, active: int, *, verifications: int, retained_bytes: int, auxiliary: int = 0
    ) -> "WorkBudgetPolicy":
        """Derive only mechanical bounds from already configured finite work counts."""
        limits = {kind: 0 for kind in WorkKind}
        limits.update(
            {
                WorkKind.ATTEMPT: attempts,
                WorkKind.REMEDIATION: attempts,
                WorkKind.REVIEW: attempts,
                WorkKind.RETRY: attempts,
                WorkKind.VERIFICATION: verifications,
                WorkKind.AUXILIARY: auxiliary,
            }
        )
        return cls(tuple(limits.items()), active, retained_bytes)

    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "counts": {kind.value: limit for kind, limit in self.counts},
            "max_active": self.max_active,
            "retained_bytes": self.retained_bytes,
            "resources": [item.payload() for item in self.resources],
            "strict_inference": self.strict_inference,
        }

    @classmethod
    def decode(cls, value: object) -> "WorkBudgetPolicy":
        if type(value) is not dict:
            raise ValueError("invalid work policy")
        raw = cast(dict[str, object], value)
        if (
            set(raw) != {"version", "counts", "max_active", "retained_bytes", "resources", "strict_inference"}
            or raw["version"] != 1
            or type(raw["counts"]) is not dict
            or type(raw["resources"]) is not list
        ):
            raise ValueError("invalid work policy schema")
        resources: list[ResourceCeiling] = []
        for item in cast(list[object], raw["resources"]):
            if type(item) is not dict:
                raise ValueError("invalid resource ceiling")
            fields = cast(dict[str, object], item)
            if set(fields) != {"metric", "unit", "quantity"} or any(type(fields[name]) is not str for name in fields):
                raise ValueError("invalid resource ceiling fields")
            resources.append(
                ResourceCeiling(
                    cast(str, fields["metric"]), cast(str, fields["unit"]), Decimal(cast(str, fields["quantity"]))
                )
            )
        return cls(
            tuple((WorkKind(kind), cast(int, count)) for kind, count in cast(dict[str, object], raw["counts"]).items()),
            cast(int, raw["max_active"]),
            cast(int, raw["retained_bytes"]),
            tuple(resources),
            cast(bool, raw["strict_inference"]),
        )
