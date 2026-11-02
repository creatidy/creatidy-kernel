# SPDX-License-Identifier: Apache-2.0
"""Producer-scoped bounded progress, separate from durable work facts."""

import hashlib
import re
from collections import deque
from dataclasses import dataclass


class ProgressRefused(ValueError):
    """A progress position or bounded event is malformed."""


def command_prefix(scope: str) -> str:
    """Stable per-Program namespace; it carries no caller authority."""
    if type(scope) is not str or not scope.strip():
        raise ProgressRefused("invalid served command scope")
    return "served-command:" + hashlib.sha256(scope.encode()).hexdigest() + ":"


@dataclass(frozen=True, slots=True)
class ProgressPosition:
    producer: str
    scope: str
    sequence: int

    def __post_init__(self) -> None:
        if (
            type(self.producer) is not str
            or re.fullmatch(r"[0-9a-f]{32}", self.producer) is None
            or type(self.scope) is not str
            or not self.scope.strip()
            or type(self.sequence) is not int
            or self.sequence < 0
            or self.sequence > 2**63 - 1
        ):
            raise ProgressRefused("invalid producer-scoped progress position")


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    position: ProgressPosition
    kind: str
    revision: int

    def __post_init__(self) -> None:
        if (
            type(self.position) is not ProgressPosition
            or self.position.sequence < 1
            or type(self.kind) is not str
            or self.kind not in {"snapshot-changed", "decision-required", "resync-required"}
            or type(self.revision) is not int
            or self.revision < 0
        ):
            raise ProgressRefused("invalid bounded progress event")


@dataclass(frozen=True, slots=True)
class ProgressBatch:
    events: tuple[ProgressEvent, ...]
    position: ProgressPosition
    gap: bool


class ProgressBuffer:
    """In-memory hints only; a gap requires a fresh authoritative snapshot."""

    def __init__(self, producer: str, scope: str, *, capacity: int = 256) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ProgressRefused("progress capacity outside supported bound")
        self._position = ProgressPosition(producer, scope, 0)
        self._events: deque[ProgressEvent] = deque(maxlen=capacity)

    @property
    def position(self) -> ProgressPosition:
        return self._position

    def append(self, kind: str, revision: int) -> ProgressEvent:
        position = ProgressPosition(self._position.producer, self._position.scope, self._position.sequence + 1)
        event = ProgressEvent(position, kind, revision)
        self._events.append(event)
        self._position = position
        return event

    def read(self, after: ProgressPosition, *, limit: int = 256) -> ProgressBatch:
        if type(after) is not ProgressPosition or type(limit) is not int or not 1 <= limit <= 4096:
            raise ProgressRefused("invalid progress read")
        if (after.producer, after.scope) != (
            self._position.producer,
            self._position.scope,
        ) or after.sequence > self._position.sequence:
            return ProgressBatch((), self._position, True)
        if self._events and after.sequence < self._events[0].position.sequence - 1:
            return ProgressBatch((), self._position, True)
        events = tuple(item for item in self._events if item.position.sequence > after.sequence)[:limit]
        if any(item.kind == "resync-required" for item in events):
            return ProgressBatch(tuple(item for item in events if item.kind == "resync-required"), self._position, True)
        return ProgressBatch(events, events[-1].position if events else after, False)
