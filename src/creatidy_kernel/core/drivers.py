# SPDX-License-Identifier: Apache-2.0
"""Versioned external subjects, not internal Task identities or provider authority."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import cast
from urllib.parse import urlsplit

from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import EffectStatus, Presence


def driver_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def driver_digest(value: object) -> str:
    return hashlib.sha256(driver_bytes(value)).hexdigest()


def opaque(value: str) -> str:
    if not value or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid external identity")
    return value


@dataclass(frozen=True, slots=True)
class DriverBinding:
    family: str
    implementation: str
    version: str
    origin: str
    tenant: str
    namespace: str
    api_version: str
    rights: str

    def __post_init__(self) -> None:
        if self.family not in {"work", "scm", "ci", "knowledge"}:
            raise ValueError("unsupported driver family")
        for value in asdict(self).values():
            opaque(value)
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
            or self.origin != self.origin.lower()
            or re.fullmatch(r"[a-z0-9.-]+|[0-9a-f:]+", parsed.hostname) is None
        ):
            raise ValueError("explicit canonical HTTPS instance origin required")
        if self.version != "1":
            raise ValueError("unsupported driver implementation major")

    @property
    def digest(self) -> str:
        return driver_digest(asdict(self))


@dataclass(frozen=True, slots=True)
class DriverSubject:
    binding: DriverBinding
    kind: str
    key: str
    revision: str | None

    def __post_init__(self) -> None:
        opaque(self.kind)
        opaque(self.key)
        if self.revision is not None:
            opaque(self.revision)

    @property
    def reference(self) -> str:
        # New codec only. Never reinterpret legacy Reference/IssueSubject bytes.
        return "kernel-driver-subject:v1:" + driver_bytes(asdict(self)).hex()

    @classmethod
    def parse(cls, reference: str) -> "DriverSubject":
        prefix = "kernel-driver-subject:v1:"
        # Eight opaque fields can each expand to 12 ASCII JSON bytes per codepoint;
        # the canonical HTTPS origin is ASCII, and knowledge is the longest family.
        envelope = (
            b'{"binding":{"api_version":"","family":"knowledge","implementation":"",'
            b'"namespace":"","origin":"","rights":"","tenant":"","version":"1"},'
            b'"key":"","kind":"","revision":""}'
        )
        max_length = len(prefix) + 2 * (len(envelope) + 4096 + 8 * 4096 * 12)
        if not reference.startswith(prefix) or len(reference) > max_length:
            raise ValueError("unsupported external subject codec")
        try:
            raw: object = json.loads(bytes.fromhex(reference[len(prefix) :]))
            if not isinstance(raw, dict):
                raise ValueError("invalid external subject")
            data = cast(dict[str, object], raw)
            if set(data) != {"binding", "kind", "key", "revision"} or not isinstance(data["binding"], dict):
                raise ValueError("unsupported external subject fields")
            binding = cast(dict[str, object], data["binding"])
            expected = {"family", "implementation", "version", "origin", "tenant", "namespace", "api_version", "rights"}
            if set(binding) != expected or any(type(value) is not str for value in binding.values()):
                raise ValueError("unsupported external binding fields")
            if (
                type(data["kind"]) is not str
                or type(data["key"]) is not str
                or (data["revision"] is not None and type(data["revision"]) is not str)
            ):
                raise ValueError("invalid external subject fields")
            subject = cls(
                DriverBinding(**cast(dict[str, str], binding)),
                data["kind"],
                data["key"],
                data["revision"],
            )
            if subject.reference != reference:
                raise ValueError("noncanonical external subject")
            return subject
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError, RecursionError):
            raise ValueError("invalid external subject encoding") from None


@dataclass(frozen=True, slots=True)
class DriverFact:
    subject: DriverSubject
    presence: Presence
    observed_at: int
    fresh_until: int
    provenance: str
    native: bytes = b"null"
    related: tuple[DriverSubject, ...] = ()
    complete: bool = False
    qualification: str = "unavailable"

    def __post_init__(self) -> None:
        if self.observed_at < 0 or self.fresh_until < self.observed_at:
            raise ValueError("invalid observation interval")
        opaque(self.provenance)

    def require_current(self, now: int) -> None:
        if self.presence is not Presence.FOUND or not self.observed_at <= now <= self.fresh_until:
            raise ValueError("external fact unavailable or stale")

    @property
    def observation_digest(self) -> str:
        """Integrity of observed bytes, never a manufactured native revision/CAS token."""
        return hashlib.sha256(self.native).hexdigest()


@dataclass(frozen=True, slots=True)
class DriverScan:
    items: tuple[DriverFact, ...]
    complete: bool
    stable: bool
    scope: str
    observed_at: int
    fresh_until: int
    qualification: str


@dataclass(frozen=True, slots=True)
class NativeDependency:
    identity: str
    type_id: str
    native_type: str
    direction: str
    target: DriverSubject


@dataclass(frozen=True, slots=True)
class WorkObservation:
    fact: DriverFact
    dependencies: tuple[NativeDependency, ...]
    dependencies_complete: bool


@dataclass(frozen=True, slots=True)
class EdgeObligation:
    """Trusted owner-policy input, never a value read from issue prose or workers."""

    subject: DriverSubject
    edges_digest: str
    decision_reference: str
    fresh_until: int

    def require(self, work: WorkObservation, now: int) -> None:
        work.fact.require_current(now)
        if (
            self.subject != work.fact.subject
            or self.edges_digest != driver_digest([asdict(edge) for edge in work.dependencies])
            or not self.decision_reference
            or now > self.fresh_until
            or not work.fact.complete
        ):
            raise ValueError("owner-approved exact dependency obligation unavailable or stale")


@dataclass(frozen=True, slots=True)
class DriverMutation:
    target: DriverSubject
    action: str
    fields: tuple[tuple[str, str], ...]
    authority_reference: str

    def __post_init__(self) -> None:
        opaque(self.action)
        opaque(self.authority_reference)
        if len(dict(self.fields)) != len(self.fields) or tuple(sorted(self.fields)) != self.fields:
            raise ValueError("mutation fields must be unique and canonical")
        for key, value in self.fields:
            opaque(key)
            if len(value) > 65536:
                raise ValueError("mutation input exceeds bound")

    def request(self) -> dict[str, object]:
        return {"schema": "kernel-driver-effect-v1", **asdict(self)}


@dataclass(frozen=True, slots=True)
class DriverDelivery:
    operation: OperationKey
    mutation: DriverMutation
    fence: int
    attempts: int

    def __post_init__(self) -> None:
        if self.fence <= 0 or self.attempts <= 0:
            raise ValueError("committed outbox delivery claim required")


@dataclass(frozen=True, slots=True)
class DriverReceipt:
    status: EffectStatus
    operation: OperationKey
    reference: str | None = None
    reason: str = "unavailable"
