# SPDX-License-Identifier: Apache-2.0
"""Retained MI knowledge-utilization references: provenance only, never authority.

The public Router selection envelope may carry ``mi_utilization``, the
producer-admitted reference to the Model Intelligence evidence actually used
on that decision path (Router #176 consuming the MI publication contract).
This module validates that reference against the exact published producer
grammar and keeps its retention state explicit. A retained reference explains
a decision; it never grants local execution, permission, freshness or
physical identity, and Kernel invents no TTL, refresh or replay against
today's mutable evidence. Absence of a reference is recorded as such, never
fabricated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

UTILIZATION_SOURCE = "model_intelligence"
UTILIZATION_SCHEMA = "mi.utilization-working/1"
UTILIZATION_IDENTIFIER_PREFIX = "urn:mi:utilization:sha256:"
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE_KEYS = frozenset({"source", "identifier", "version"})
_DOCUMENT_KEYS = frozenset({"state", "source", "identifier", "version"})


class KnowledgeState(StrEnum):
    """Closed retention vocabulary; a missing reference exports as ``not_retained``."""

    UTILIZED = "utilized"
    UNSUPPORTED = "unsupported_version"


def _bounded(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("invalid knowledge utilization value")
    return value


def _contract(source: str, identifier: str, version: str) -> bool:
    digest = (
        identifier[len(UTILIZATION_IDENTIFIER_PREFIX) :]
        if identifier.startswith(UTILIZATION_IDENTIFIER_PREFIX)
        else None
    )
    return (
        source == UTILIZATION_SOURCE
        and version == UTILIZATION_SCHEMA
        and digest is not None
        and _DIGEST.fullmatch(digest) is not None
    )


@dataclass(frozen=True, slots=True)
class UtilizedKnowledge:
    """One retained producer reference with its explicit support state."""

    state: KnowledgeState
    source: str
    identifier: str
    version: str

    def __post_init__(self) -> None:
        if type(self.state) is not KnowledgeState:
            raise ValueError("invalid knowledge utilization state")
        if type(self.source) is not str or not _SAFE_ID.fullmatch(self.source):
            raise ValueError("invalid knowledge utilization source")
        _bounded(self.identifier, 512)
        _bounded(self.version, 128)
        if (self.state is KnowledgeState.UTILIZED) != _contract(self.source, self.identifier, self.version):
            raise ValueError("knowledge utilization state contradicts producer contract")

    @classmethod
    def from_reference(cls, document: object) -> UtilizedKnowledge:
        """Parse the exact published ``mi_utilization`` reference grammar.

        A grammar violation is corrupt producer output and refuses the whole
        envelope at the adapter; a grammar-valid reference that no longer
        matches the producer contract is retained as explicitly unsupported.
        """
        if not isinstance(document, dict):
            raise ValueError("invalid utilization reference fields")
        value = cast(dict[str, object], document)
        if set(value) != set(_REFERENCE_KEYS):
            raise ValueError("invalid utilization reference fields")
        source = value["source"]
        if type(source) is not str or not _SAFE_ID.fullmatch(source):
            raise ValueError("invalid utilization reference source")
        identifier = _bounded(value["identifier"], 512)
        version = _bounded(value["version"], 128)
        state = KnowledgeState.UTILIZED if _contract(source, identifier, version) else KnowledgeState.UNSUPPORTED
        return cls(state, source, identifier, version)

    def to_document(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "source": self.source,
            "identifier": self.identifier,
            "version": self.version,
        }

    @classmethod
    def from_document(cls, document: object) -> UtilizedKnowledge:
        """Decode the durable retention document; the state must still hold its contract."""
        if not isinstance(document, dict):
            raise ValueError("invalid retained knowledge document")
        value = cast(dict[str, object], document)
        if set(value) != set(_DOCUMENT_KEYS):
            raise ValueError("invalid retained knowledge document")
        state = value["state"]
        if type(state) is not str or state not in {item.value for item in KnowledgeState}:
            raise ValueError("invalid retained knowledge state")
        source = value["source"]
        if type(source) is not str or not _SAFE_ID.fullmatch(source):
            raise ValueError("invalid retained knowledge source")
        return cls(
            KnowledgeState(state),
            source,
            _bounded(value["identifier"], 512),
            _bounded(value["version"], 128),
        )


def export_document(knowledge: UtilizedKnowledge | None) -> dict[str, object]:
    """Local export projection: explicit retention state with local-only restrictions.

    The reference is provenance for explaining a decision; the export never
    claims capability, freshness, distribution rights or physical identity.
    """
    if knowledge is None:
        return {"state": "not_retained"}
    return {
        **knowledge.to_document(),
        "authority": "provenance-only",
        "distribution": "local-only",
    }
