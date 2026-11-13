# SPDX-License-Identifier: Apache-2.0
"""Inert, versioned dependency cuts for provider-qualified ordinary work.

These records are not approvals or admission authority. The trusted application
must bind their bytes to an owner decision and recheck them at each authority cut.
"""

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import cast

from creatidy_kernel.core.drivers import (
    DriverSubject,
    EdgeObligation,
    NativeDependency,
    WorkObservation,
    driver_digest,
    opaque,
)
from creatidy_kernel.core.intake import Declaration, digest, encode, object_from


@dataclass(frozen=True, slots=True)
class QualifiedDependencyCut:
    """Exact observed work and explicitly classified native edges, not a grant."""

    subject: DriverSubject
    observation_digest: str
    provenance: str
    qualification: str
    edges_digest: str
    native: bytes
    edges: tuple[NativeDependency, ...]
    required: tuple[DriverSubject, ...]
    classifications: tuple[tuple[str, bool], ...]
    observed_at: int
    fresh_until: int
    assurance: str
    obligation: EdgeObligation | None

    def __post_init__(self) -> None:
        if (
            type(self.subject) is not DriverSubject
            or self.subject.binding.family != "work"
            or self.subject.revision is None
            or any(
                type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in (self.observation_digest, self.edges_digest)
            )
            or type(self.provenance) is not str
            or not self.provenance
            or type(self.qualification) is not str
            or not self.qualification
            or type(self.native) is not bytes
            or hashlib.sha256(self.native).hexdigest() != self.observation_digest
            or type(self.edges) is not tuple
            or any(type(edge) is not NativeDependency for edge in self.edges)
            or any(
                type(value) is not str or not value
                for edge in self.edges
                for value in (edge.identity, edge.type_id, edge.native_type, edge.direction)
            )
            or any(
                type(edge.target) is not DriverSubject
                or edge.target.binding.family != "work"
                or edge.target.revision is None
                for edge in self.edges
            )
            or driver_digest([asdict(edge) for edge in self.edges]) != self.edges_digest
            or len({edge.identity for edge in self.edges}) != len(self.edges)
            or type(self.required) is not tuple
            or any(
                type(item) is not DriverSubject or item.binding.family != "work" or item.revision is None
                for item in self.required
            )
            or tuple(sorted(self.required, key=lambda item: item.reference)) != self.required
            or len(set(self.required)) != len(self.required)
            or type(self.classifications) is not tuple
            or any(type(pair) is not tuple or len(pair) != 2 for pair in self.classifications)
            or any(
                type(key) is not str or not key or type(required) is not bool for key, required in self.classifications
            )
            or tuple(sorted(self.classifications)) != self.classifications
            or len({key for key, _ in self.classifications}) != len(self.classifications)
            or {edge.identity for edge in self.edges} != {key for key, _ in self.classifications}
            or tuple(
                sorted(
                    (edge.target for edge in self.edges if dict(self.classifications)[edge.identity]),
                    key=lambda item: item.reference,
                )
            )
            != self.required
            or type(self.observed_at) is not int
            or self.observed_at < 0
            or type(self.fresh_until) is not int
            or self.fresh_until < self.observed_at
            or self.assurance not in {"native-complete", "visible-scope-obligation"}
            or (self.assurance == "native-complete" and self.obligation is not None)
            or (self.assurance == "visible-scope-obligation" and type(self.obligation) is not EdgeObligation)
            or (
                self.obligation is not None
                and (
                    self.obligation.subject != self.subject
                    or self.obligation.edges_digest != self.edges_digest
                    or type(self.obligation.decision_reference) is not str
                    or not self.obligation.decision_reference
                    or type(self.obligation.fresh_until) is not int
                    or self.fresh_until > self.obligation.fresh_until
                )
            )
        ):
            raise ValueError("invalid qualified dependency cut")

    @property
    def payload(self) -> dict[str, object]:
        return {
            "kind": "ordinary-qualified-dependencies",
            "version": 1,
            "subject": self.subject.reference,
            "observation_digest": self.observation_digest,
            "provenance": self.provenance,
            "qualification": self.qualification,
            "edges_digest": self.edges_digest,
            "native": self.native.hex(),
            "edges": [
                {
                    "identity": edge.identity,
                    "type_id": edge.type_id,
                    "native_type": edge.native_type,
                    "direction": edge.direction,
                    "target": edge.target.reference,
                }
                for edge in self.edges
            ],
            "required": [item.reference for item in self.required],
            "classifications": [[key, required] for key, required in self.classifications],
            "observed_at": self.observed_at,
            "fresh_until": self.fresh_until,
            "assurance": self.assurance,
            "obligation": None
            if self.obligation is None
            else {
                "subject": self.obligation.subject.reference,
                "edges_digest": self.obligation.edges_digest,
                "decision_reference": self.obligation.decision_reference,
                "fresh_until": self.obligation.fresh_until,
            },
        }

    @property
    def encoded(self) -> bytes:
        return encode(self.payload)

    @classmethod
    def capture(
        cls,
        work: WorkObservation,
        classifications: tuple[tuple[str, bool], ...],
        now: int,
        *,
        obligation: EdgeObligation | None = None,
    ) -> "QualifiedDependencyCut":
        work.fact.require_current(now)
        subject = work.fact.subject
        if (
            subject.binding.family != "work"
            or subject.revision is None
            or work.fact.complete is not True
            or type(work.dependencies_complete) is not bool
            or type(now) is not int
            or not work.fact.qualification
            or type(classifications) is not tuple
            or any(type(pair) is not tuple or len(pair) != 2 for pair in classifications)
            or any(type(key) is not str or not key or type(required) is not bool for key, required in classifications)
            or tuple(sorted(classifications)) != classifications
            or len({key for key, _ in classifications}) != len(classifications)
        ):
            raise ValueError("qualified current work and canonical edge classifications required")
        edges = work.dependencies
        if (
            len({edge.identity for edge in edges}) != len(edges)
            or {edge.identity for edge in edges} != {key for key, _ in classifications}
            or any(edge.target.binding.family != "work" or edge.target.revision is None for edge in edges)
        ):
            raise ValueError("every visible edge needs one exact classification and target")
        for edge in edges:
            for value in (edge.identity, edge.type_id, edge.native_type, edge.direction):
                opaque(value)
        if work.dependencies_complete:
            if obligation is not None:
                raise ValueError("complete native observation must not be relabelled as owner scope")
            assurance = "native-complete"
        else:
            if obligation is None:
                raise ValueError("incomplete native enumeration requires an explicit visible-scope obligation")
            obligation.require(work, now)
            assurance = "visible-scope-obligation"
        selected = dict(classifications)
        required = tuple(sorted((edge.target for edge in edges if selected[edge.identity]), key=lambda s: s.reference))
        if len(set(required)) != len(required):
            raise ValueError("duplicate required predecessor")
        return cls(
            subject,
            work.fact.observation_digest,
            work.fact.provenance,
            work.fact.qualification,
            driver_digest([asdict(edge) for edge in edges]),
            work.fact.native,
            edges,
            required,
            classifications,
            work.fact.observed_at,
            min(work.fact.fresh_until, obligation.fresh_until) if obligation else work.fact.fresh_until,
            assurance,
            obligation,
        )

    def recheck(self, work: WorkObservation, now: int) -> None:
        """Compare current meaning while retaining the original, nonrenewable deadline."""
        current = self.capture(work, self.classifications, now, obligation=self.obligation)
        if now < self.observed_at or now > self.fresh_until:
            raise ValueError("original dependency cut expired")
        if (
            current.subject != self.subject
            or current.observation_digest != self.observation_digest
            or current.provenance != self.provenance
            or current.qualification != self.qualification
            or current.edges_digest != self.edges_digest
            or current.native != self.native
            or current.edges != self.edges
            or current.required != self.required
            or current.assurance != self.assurance
        ):
            raise ValueError("dependency cut changed")

    @classmethod
    def parse(cls, raw: bytes) -> "QualifiedDependencyCut":
        value = object_from(raw)
        expected = {
            "kind",
            "version",
            "subject",
            "observation_digest",
            "provenance",
            "qualification",
            "edges_digest",
            "native",
            "edges",
            "required",
            "classifications",
            "observed_at",
            "fresh_until",
            "assurance",
            "obligation",
        }
        if (
            set(value) != expected
            or value["kind"] != "ordinary-qualified-dependencies"
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("unsupported qualified dependency codec")
        try:
            obligation_value = value["obligation"]
            obligation = None
            if obligation_value is not None:
                if type(obligation_value) is not dict:
                    raise ValueError("invalid obligation")
                data = cast(dict[str, object], obligation_value)
                if set(data) != {"subject", "edges_digest", "decision_reference", "fresh_until"}:
                    raise ValueError("invalid obligation fields")
                obligation = EdgeObligation(
                    DriverSubject.parse(cast(str, data["subject"])),
                    cast(str, data["edges_digest"]),
                    cast(str, data["decision_reference"]),
                    cast(int, data["fresh_until"]),
                )
            classifications = cast(list[list[object]], value["classifications"])
            edge_values = cast(list[dict[str, object]], value["edges"])
            if any(set(edge) != {"identity", "type_id", "native_type", "direction", "target"} for edge in edge_values):
                raise ValueError("invalid native edge fields")
            edges = tuple(
                NativeDependency(
                    cast(str, edge["identity"]),
                    cast(str, edge["type_id"]),
                    cast(str, edge["native_type"]),
                    cast(str, edge["direction"]),
                    DriverSubject.parse(cast(str, edge["target"])),
                )
                for edge in edge_values
            )
            cut = cls(
                DriverSubject.parse(cast(str, value["subject"])),
                cast(str, value["observation_digest"]),
                cast(str, value["provenance"]),
                cast(str, value["qualification"]),
                cast(str, value["edges_digest"]),
                bytes.fromhex(cast(str, value["native"])),
                edges,
                tuple(DriverSubject.parse(item) for item in cast(list[str], value["required"])),
                tuple((cast(str, pair[0]), cast(bool, pair[1])) for pair in classifications),
                cast(int, value["observed_at"]),
                cast(int, value["fresh_until"]),
                cast(str, value["assurance"]),
                obligation,
            )
            if cut.encoded != raw:
                raise ValueError("noncanonical qualified dependency record")
            return cut
        except (KeyError, TypeError, IndexError, AttributeError):
            raise ValueError("invalid qualified dependency record") from None


@dataclass(frozen=True, slots=True)
class QualifiedBaseline:
    """An already-produced baseline for an exact SCM ref and recipe, not permission to run it."""

    source: DriverSubject
    recipe_digest: str
    producer: str
    reference: str
    result: str
    observed_at: int
    proof: bytes = b""

    def __post_init__(self) -> None:
        if (
            type(self.source) is not DriverSubject
            or self.source.binding.family != "scm"
            or self.source.kind != "ref"
            or self.source.revision is None
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.source.revision) is None
            or type(self.recipe_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.recipe_digest) is None
            or type(self.producer) is not str
            or not self.producer
            or self.producer == "unknown"
            or type(self.reference) is not str
            or not self.reference
            or self.reference == "unknown"
            or self.result not in {"passed", "failed"}
            or type(self.observed_at) is not int
            or self.observed_at < 0
            or type(self.proof) is not bytes
            or (self.proof and self.reference != "sha256:" + digest(self.proof))
        ):
            raise ValueError("exact qualified baseline evidence required")

    @property
    def meaning(self) -> dict[str, object]:
        meaning: dict[str, object] = {
            "source": self.source.reference,
            "recipe_digest": self.recipe_digest,
            "producer": self.producer,
            "reference": self.reference,
            "result": self.result,
        }
        if self.proof:
            meaning["proof"] = self.proof.hex()
        return meaning

    @property
    def payload(self) -> dict[str, object]:
        return {**self.meaning, "observed_at": self.observed_at}


@dataclass(frozen=True, slots=True)
class QualifiedReadiness:
    """Trusted producer's exact-subject open-work and relevance observation."""

    work: DriverSubject
    source: DriverSubject
    work_digest: str
    declaration_digest: str
    owner_actor: str
    producer: str
    reference: str
    proof: bytes
    disposition: str
    issue_state: str
    observed_at: int
    fresh_until: int

    def __post_init__(self) -> None:
        if (
            type(self.work) is not DriverSubject
            or self.work.binding.family != "work"
            or self.work.revision is None
            or type(self.source) is not DriverSubject
            or self.source.binding.family != "scm"
            or self.source.revision is None
            or any(
                type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                for value in (self.work_digest, self.declaration_digest)
            )
            or type(self.owner_actor) is not str
            or not self.owner_actor.strip()
            or type(self.producer) is not str
            or not self.producer
            or self.producer == "unknown"
            or type(self.reference) is not str
            or not self.reference
            or self.reference == "unknown"
            or type(self.proof) is not bytes
            or not self.proof
            or self.reference != "sha256:" + digest(self.proof)
            or self.disposition != "work_remains"
            or self.issue_state != "open"
            or type(self.observed_at) is not int
            or self.observed_at < 0
            or type(self.fresh_until) is not int
            or self.fresh_until < self.observed_at
        ):
            raise ValueError("exact open qualified-work relevance required")

    @property
    def meaning(self) -> dict[str, object]:
        return {
            "work": self.work.reference,
            "source": self.source.reference,
            "work_digest": self.work_digest,
            "declaration_digest": self.declaration_digest,
            "owner_actor": self.owner_actor,
            "producer": self.producer,
            "reference": self.reference,
            "proof": self.proof.hex(),
            "disposition": self.disposition,
            "issue_state": self.issue_state,
        }

    @property
    def payload(self) -> dict[str, object]:
        return {**self.meaning, "observed_at": self.observed_at, "fresh_until": self.fresh_until}


@dataclass(frozen=True, slots=True)
class QualifiedDraft:
    """Selected work, independent source and existing-producer baseline as inert meaning."""

    task_id: str
    declaration: Declaration
    cut: QualifiedDependencyCut
    baseline: QualifiedBaseline
    readiness: QualifiedReadiness
    source_digest: str
    source_provenance: str
    source_qualification: str
    source_observed_at: int
    source_fresh_until: int
    relevance_reason: str
    ci_subjects: tuple[DriverSubject, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.task_id) is not str
            or not self.task_id.strip()
            or type(self.declaration) is not Declaration
            or type(self.cut) is not QualifiedDependencyCut
            or type(self.baseline) is not QualifiedBaseline
            or type(self.readiness) is not QualifiedReadiness
            or self.readiness.work != self.cut.subject
            or self.readiness.source != self.baseline.source
            or self.readiness.work_digest != self.cut.observation_digest
            or self.readiness.declaration_digest != self.declaration.digest
            or self.baseline.recipe_digest != digest(encode(self.declaration.value["recipes"]))
            or type(self.source_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.source_digest) is None
            or type(self.source_provenance) is not str
            or not self.source_provenance
            or type(self.source_qualification) is not str
            or not self.source_qualification
            or type(self.source_observed_at) is not int
            or self.source_observed_at < 0
            or type(self.source_fresh_until) is not int
            or self.source_fresh_until < self.source_observed_at
            or type(self.relevance_reason) is not str
            or not self.relevance_reason.strip()
            or type(self.ci_subjects) is not tuple
            or any(
                type(item) is not DriverSubject
                or item.binding.family != "ci"
                or item.kind != "build"
                or item.revision is not None
                for item in self.ci_subjects
            )
            or len(set(self.ci_subjects)) != len(self.ci_subjects)
        ):
            raise ValueError("invalid qualified draft")

    def payload(self) -> dict[str, object]:
        return {
            "kind": "ordinary-qualified-draft",
            "version": 1,
            "task_id": self.task_id,
            "declaration": self.declaration.raw.decode("utf-8"),
            "cut": self.cut.payload,
            "baseline": self.baseline.payload,
            "readiness": self.readiness.payload,
            "source_digest": self.source_digest,
            "source_provenance": self.source_provenance,
            "source_qualification": self.source_qualification,
            "source_observed_at": self.source_observed_at,
            "source_fresh_until": self.source_fresh_until,
            "relevance_reason": self.relevance_reason,
            "ci_subjects": [item.reference for item in self.ci_subjects],
        }

    @classmethod
    def parse(cls, raw: bytes) -> "QualifiedDraft":
        value = object_from(raw)
        if (
            set(value)
            != {
                "kind",
                "version",
                "task_id",
                "declaration",
                "cut",
                "baseline",
                "readiness",
                "source_digest",
                "source_provenance",
                "source_qualification",
                "source_observed_at",
                "source_fresh_until",
                "relevance_reason",
                "ci_subjects",
            }
            or value["kind"] != "ordinary-qualified-draft"
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("unsupported qualified draft")
        try:
            base = cast(dict[str, object], value["baseline"])
            readiness = cast(dict[str, object], value["readiness"])
            baseline_fields = {"source", "recipe_digest", "producer", "reference", "result", "observed_at"}
            if set(base) not in (baseline_fields, baseline_fields | {"proof"}):
                raise ValueError("invalid qualified baseline")
            if set(readiness) != {
                "work",
                "source",
                "work_digest",
                "declaration_digest",
                "owner_actor",
                "producer",
                "reference",
                "proof",
                "disposition",
                "issue_state",
                "observed_at",
                "fresh_until",
            }:
                raise ValueError("invalid qualified relevance")
            draft = cls(
                cast(str, value["task_id"]),
                Declaration(cast(str, value["declaration"]).encode("utf-8")),
                QualifiedDependencyCut.parse(encode(value["cut"])),
                QualifiedBaseline(
                    DriverSubject.parse(cast(str, base["source"])),
                    cast(str, base["recipe_digest"]),
                    cast(str, base["producer"]),
                    cast(str, base["reference"]),
                    cast(str, base["result"]),
                    cast(int, base["observed_at"]),
                    bytes.fromhex(cast(str, base.get("proof", ""))),
                ),
                QualifiedReadiness(
                    DriverSubject.parse(cast(str, readiness["work"])),
                    DriverSubject.parse(cast(str, readiness["source"])),
                    cast(str, readiness["work_digest"]),
                    cast(str, readiness["declaration_digest"]),
                    cast(str, readiness["owner_actor"]),
                    cast(str, readiness["producer"]),
                    cast(str, readiness["reference"]),
                    bytes.fromhex(cast(str, readiness["proof"])),
                    cast(str, readiness["disposition"]),
                    cast(str, readiness["issue_state"]),
                    cast(int, readiness["observed_at"]),
                    cast(int, readiness["fresh_until"]),
                ),
                cast(str, value["source_digest"]),
                cast(str, value["source_provenance"]),
                cast(str, value["source_qualification"]),
                cast(int, value["source_observed_at"]),
                cast(int, value["source_fresh_until"]),
                cast(str, value["relevance_reason"]),
                tuple(DriverSubject.parse(item) for item in cast(list[str], value["ci_subjects"])),
            )
            if encode(draft.payload()) != raw:
                raise ValueError("noncanonical qualified draft")
            return draft
        except (KeyError, TypeError, AttributeError):
            raise ValueError("invalid qualified draft") from None
