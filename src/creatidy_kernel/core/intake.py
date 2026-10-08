# SPDX-License-Identifier: Apache-2.0
"""Inert ordinary task meaning. No execution, inference, or principal decoding."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import urlsplit

from creatidy_kernel.core.domain import AuthorityEnvelope, BudgetPolicy, PolicyReference
from creatidy_kernel.core.forge import Observation, Presence


class IntakeRefused(ValueError):
    """A safe, content-free refusal of malformed or inapplicable intake."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def object_from(raw: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise IntakeRefused("duplicate field")
            result[name] = value
        return result

    try:
        value: object = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise IntakeRefused("invalid intake encoding") from None
    if type(value) is not dict:
        raise IntakeRefused("intake object required")
    return cast(dict[str, object], value)


def strings(value: object, *, nonempty: bool = False) -> tuple[str, ...]:
    if type(value) is not list:
        raise IntakeRefused("text array required")
    items = cast(list[object], value)
    if (nonempty and not items) or any(type(item) is not str or not item.strip() for item in items):
        raise IntakeRefused("nonblank text required")
    result = cast(tuple[str, ...], tuple(items))
    if len(set(result)) != len(result):
        raise IntakeRefused("duplicate item")
    return result


@dataclass(frozen=True, slots=True)
class IssueSubject:
    """Explicit controller selection, including origin (legacy Forge refs lack host)."""

    origin: str
    repository: str
    issue: int
    branch: str
    base: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or self.origin != self.origin.rstrip("/")
            or re.fullmatch(r"[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+", self.repository) is None
            or type(self.issue) is not int
            or self.issue <= 0
            or not self.branch
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.base) is None
        ):
            raise IntakeRefused("invalid selected issue subject")

    def payload(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "repository": self.repository,
            "issue": self.issue,
            "branch": self.branch,
            "base": self.base,
        }


DECLARATION_FIELDS = frozenset(
    {
        "version",
        "outcome",
        "criteria",
        "quality",
        "interface",
        "context",
        "unknowns",
        "paths",
        "network",
        "effects",
        "recipes",
        "provenance",
    }
)


@dataclass(frozen=True, slots=True)
class Declaration:
    """Untouched proposer bytes, strict local format, never an approved ProgramSpec."""

    raw: bytes

    def __post_init__(self) -> None:
        if type(self.raw) is not bytes or len(self.raw) > 262144:
            raise IntakeRefused("declaration bytes exceed bound")
        value = object_from(self.raw)
        if frozenset(value) != DECLARATION_FIELDS or type(value["version"]) is not int or value["version"] != 1:
            raise IntakeRefused("unsupported declaration fields or version")
        if type(value["outcome"]) is not str or not value["outcome"].strip():
            raise IntakeRefused("outcome required")
        for name in DECLARATION_FIELDS - {"version", "outcome"}:
            strings(value[name], nonempty=name in {"criteria", "recipes", "provenance"})
        for path in strings(value["paths"]):
            parsed = PurePosixPath(path)
            if parsed.is_absolute() or ".." in parsed.parts or path in {".", ""} or "\\" in path:
                raise IntakeRefused("bounded relative paths required")
        for destination in strings(value["network"]):
            parsed = urlsplit(destination)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path
            ):
                raise IntakeRefused("explicit network origins required")

    @property
    def value(self) -> dict[str, object]:
        return object_from(self.raw)

    @property
    def digest(self) -> str:
        return digest(self.raw)


class Disposition(StrEnum):
    IMPLEMENTED = "implemented_in_current_source"
    ACTIVE_PR = "active_equivalent_pr"
    REMAINS = "work_remains"
    INCOMPLETE = "incomplete_search"
    PERMISSION = "permission_limited"
    STALE = "stale_source"
    EQUIVALENCE = "unresolved_equivalence"
    BASELINE = "unknown_baseline"


@dataclass(frozen=True, slots=True)
class IntakeEvidence:
    """Trusted producer output, supplied outside proposal decoding.

    Semantic identity excludes time: rereads cannot amend meaning or renew approval.
    """

    subject: IssueSubject
    issue_digest: str
    issue_updated: str
    issue_state: str
    source: str
    recipe_digest: str
    baseline_producer: str
    baseline_reference: str
    baseline_result: str
    relevance_producer: str
    relevance_reference: str
    relevance_proof: bytes
    disposition: Disposition
    observed_at: int

    def payload(self) -> dict[str, object]:
        return {
            "subject": self.subject.payload(),
            "issue_digest": self.issue_digest,
            "issue_updated": self.issue_updated,
            "issue_state": self.issue_state,
            "source": self.source,
            "recipe_digest": self.recipe_digest,
            "baseline_producer": self.baseline_producer,
            "baseline_reference": self.baseline_reference,
            "baseline_result": self.baseline_result,
            "relevance_producer": self.relevance_producer,
            "relevance_reference": self.relevance_reference,
            "relevance_proof": self.relevance_proof.decode("utf-8"),
            "disposition": self.disposition.value,
        }

    @property
    def digest(self) -> str:
        return digest(encode(self.payload()))


@dataclass(frozen=True, slots=True)
class Draft:
    task_id: str
    revision: int
    parent_digest: str | None
    declaration: Declaration
    evidence: IntakeEvidence

    def payload(self) -> dict[str, object]:
        return {
            "kind": "ordinary-draft",
            "version": 1,
            "task_id": self.task_id,
            "revision": self.revision,
            "parent_digest": self.parent_digest,
            "declaration": self.declaration.raw.decode("utf-8"),
            "evidence": self.evidence.payload(),
            "observed_at": self.evidence.observed_at,
        }

    @property
    def digest(self) -> str:
        return digest(encode(self.payload()))


@dataclass(frozen=True, slots=True)
class OwnerPolicy:
    """Trusted local policy; no values are inferred from proposal text."""

    reference: PolicyReference
    budget: BudgetPolicy
    authority: AuthorityEnvelope
    paths: frozenset[str]
    network: frozenset[str]
    effects: frozenset[str]
    recipes: frozenset[str]
    evidence_producers: frozenset[str]
    freshness_seconds: int
    repair_baseline_reference: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.reference) is not PolicyReference
            or type(self.budget) is not BudgetPolicy
            or type(self.authority) is not AuthorityEnvelope
        ):
            raise IntakeRefused("explicit trusted policy, budget and authority required")
        for values in (self.paths, self.network, self.effects, self.recipes, self.evidence_producers):
            if type(values) is not frozenset or any(type(value) is not str or not value for value in values):
                raise IntakeRefused("immutable explicit owner policy bounds required")
        if not self.recipes or not self.evidence_producers:
            raise IntakeRefused("explicit recipe and producer policy required")
        if type(self.freshness_seconds) is not int or self.freshness_seconds <= 0:
            raise IntakeRefused("explicit positive evidence freshness required")

    def payload(self) -> dict[str, object]:
        return {
            "reference": self.reference.payload(),
            "budget": {
                "max_attempts": self.budget.max_attempts,
                "max_active_attempts": self.budget.max_active_attempts,
            },
            "authority": self.authority.payload(),
            "paths": sorted(self.paths),
            "network": sorted(self.network),
            "effects": sorted(self.effects),
            "recipes": sorted(self.recipes),
            "evidence_producers": sorted(self.evidence_producers),
            "freshness_seconds": self.freshness_seconds,
            "repair_baseline_reference": self.repair_baseline_reference,
        }

    @property
    def digest(self) -> str:
        return digest(encode(self.payload()))


@dataclass(frozen=True, slots=True)
class RequirementsHandoff:
    """Full received meaning and exact approval subject, not a ResourceRequest."""

    draft: Draft = field(repr=False)
    decision_id: str
    decision_bytes: bytes = field(repr=False)
    program_digest: str


@dataclass(frozen=True, slots=True)
class TranslationRefusal:
    handoff: RequirementsHandoff = field(repr=False)
    reason: str = "ordinary_requirement_mapping_unavailable"
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    presence: Presence
    content_digest: str = "unknown"
    updated_at: str = "unknown"
    state: str = "unknown"


@dataclass(frozen=True, slots=True)
class ChangeSnapshot:
    observation: Observation
    state: str = "unknown"
    merged: bool | None = None
    updated_at: str = "unknown"
    target_branch: str | None = None


@dataclass(frozen=True, slots=True)
class BaselineSnapshot:
    """Already-produced evidence, not permission to execute a recipe."""

    subject: IssueSubject
    recipe_digest: str
    producer: str
    reference: str
    result: str
    observed_at: int


@dataclass(frozen=True, slots=True)
class ContentCriterion:
    """Supported ONLY for a trusted literal exact-file-content obligation."""

    path: str
    expected: bytes

    def __post_init__(self) -> None:
        if type(self.path) is not str or type(self.expected) is not bytes or len(self.expected) > 262144:
            raise IntakeRefused("bounded literal content criterion required")
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or self.path in {"", "."} or "\\" in self.path:
            raise IntakeRefused("relative literal content criterion path required")

    @property
    def criterion(self) -> str:
        return f"Exact file bytes at {self.path}: sha256:{digest(self.expected)}"
