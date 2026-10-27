# SPDX-License-Identifier: Apache-2.0
"""Original, suspend-inclusive task envelopes; no clock acquisition in the core."""

from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class BootClockReading:
    wall: int
    boot_ns: int
    boot_id: str
    clock: str = "linux:CLOCK_BOOTTIME"

    def __post_init__(self) -> None:
        if (
            type(self.wall) is not int
            or self.wall < 0
            or type(self.boot_ns) is not int
            or self.boot_ns < 0
            or type(self.boot_id) is not str
            or not self.boot_id.strip()
            or self.clock != "linux:CLOCK_BOOTTIME"
        ):
            raise ValueError("unverified suspend-inclusive clock reading")


@dataclass(frozen=True, slots=True)
class DeadlineProjection:
    wall_deadline: int
    wall_anchor: int
    boot_anchor_ns: int
    boot_deadline_ns: int
    boot_id: str
    clock: str = "linux:CLOCK_BOOTTIME"

    def __post_init__(self) -> None:
        BootClockReading(self.wall_anchor, self.boot_anchor_ns, self.boot_id, self.clock)
        if (
            type(self.wall_deadline) is not int
            or self.wall_deadline <= self.wall_anchor
            or type(self.boot_deadline_ns) is not int
            or self.boot_deadline_ns != self.boot_anchor_ns + (self.wall_deadline - self.wall_anchor) * 1_000_000_000
        ):
            raise ValueError("invalid original deadline projection")

    @classmethod
    def bind(cls, deadline: int, reading: BootClockReading) -> "DeadlineProjection":
        return cls(
            deadline,
            reading.wall,
            reading.boot_ns,
            reading.boot_ns + (deadline - reading.wall) * 1_000_000_000,
            reading.boot_id,
            reading.clock,
        )

    def decision_time(self, reading: BootClockReading | None) -> int:
        """Unverifiable recovery fails closed, never reprojects from recovery wall time."""
        if not self.verifiable(reading):
            return self.wall_deadline
        assert reading is not None  # noqa: S101 - verified above.
        return max(reading.wall, self.wall_anchor + (reading.boot_ns - self.boot_anchor_ns) // 1_000_000_000)

    def verifiable(self, reading: BootClockReading | None) -> bool:
        return not (
            reading is None
            or reading.boot_id != self.boot_id
            or reading.clock != self.clock
            or reading.boot_ns < self.boot_anchor_ns
            or reading.wall < self.wall_anchor
        )

    @classmethod
    def decode(cls, value: object) -> "DeadlineProjection":
        if type(value) is not dict:
            raise ValueError("missing original deadline projection")
        raw = cast(dict[str, object], value)
        if set(raw) != {"wall_deadline", "wall_anchor", "boot_anchor_ns", "boot_deadline_ns", "boot_id", "clock"}:
            raise ValueError("invalid deadline projection fields")
        return cls(
            cast(int, raw["wall_deadline"]),
            cast(int, raw["wall_anchor"]),
            cast(int, raw["boot_anchor_ns"]),
            cast(int, raw["boot_deadline_ns"]),
            cast(str, raw["boot_id"]),
            cast(str, raw["clock"]),
        )
