# SPDX-License-Identifier: Apache-2.0
"""Retained MI knowledge-utilization references (issue #55).

Producer-shaped fixtures follow machine_api.selection_envelope carrying the
mi_utilization reference emitted by the accepted Router #176 consumer at
4162f6fa9f774beaf6e92d86b7ebc92f306da4bd, consuming the MI publication at
d65891668b7e79f7b666c021f02a1b4bf8c4ca9c (model_intelligence.utilization).
All digests, sources and versions are synthetic; no MI package, Router
implementation or live service is used.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast

import pytest

from creatidy_kernel.core.knowledge import (
    UTILIZATION_SCHEMA,
    UTILIZATION_SOURCE,
    KnowledgeState,
    UtilizedKnowledge,
    export_document,
)
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.ports.allocation import decode_allocation, encode_allocation

DIGEST = "a1b2c3d4" * 8
IDENTIFIER = f"urn:mi:utilization:sha256:{DIGEST}"
BASE = Allocation(
    "codex",
    "openai",
    "fixture-model",
    frozenset({"reference"}),
    4096,
    "controller binding",
    "high",
)


def reference(
    *,
    version: str = UTILIZATION_SCHEMA,
    source: str = UTILIZATION_SOURCE,
    identifier: str = IDENTIFIER,
) -> dict[str, object]:
    return {"source": source, "identifier": identifier, "version": version}


def utilized() -> UtilizedKnowledge:
    return UtilizedKnowledge(KnowledgeState.UTILIZED, UTILIZATION_SOURCE, IDENTIFIER, UTILIZATION_SCHEMA)


@pytest.mark.parametrize(
    ("document", "state"),
    [
        (reference(), KnowledgeState.UTILIZED),
        (reference(version="mi.utilization-working/2"), KnowledgeState.UNSUPPORTED),
        (reference(version="mi.publication/3"), KnowledgeState.UNSUPPORTED),
        (reference(source="other_intelligence"), KnowledgeState.UNSUPPORTED),
        (reference(identifier=f"urn:mi:utilization:sha256:{'z' * 64}"), KnowledgeState.UNSUPPORTED),
        (reference(identifier=f"urn:mi:utilization:sha256:{DIGEST[:-1]}"), KnowledgeState.UNSUPPORTED),
        (reference(identifier=f"urn:mi:other:sha256:{DIGEST}"), KnowledgeState.UNSUPPORTED),
        (reference(identifier=DIGEST), KnowledgeState.UNSUPPORTED),
    ],
)
def test_reference_states_use_producer_semantics(document: dict[str, object], state: KnowledgeState) -> None:
    knowledge = UtilizedKnowledge.from_reference(document)
    assert knowledge.state is state
    assert knowledge.to_document() == {"state": state.value, **document}


@pytest.mark.parametrize(
    "document",
    [
        None,
        "model_intelligence",
        [],
        {},
        {"source": UTILIZATION_SOURCE, "identifier": IDENTIFIER},
        {"source": UTILIZATION_SOURCE, "identifier": IDENTIFIER, "version": UTILIZATION_SCHEMA, "date": "2026-01-01"},
        reference(source="Model_Intelligence"),
        reference(source="bad source"),
        reference(identifier=" " * 8),
        reference(identifier="x" * 513),
        reference(identifier=""),
        reference(version="x" * 129),
        {"source": 1, "identifier": IDENTIFIER, "version": UTILIZATION_SCHEMA},
        {"source": UTILIZATION_SOURCE, "identifier": 2, "version": UTILIZATION_SCHEMA},
    ],
)
def test_corrupt_reference_grammar_is_refused(document: object) -> None:
    with pytest.raises(ValueError):
        UtilizedKnowledge.from_reference(document)


def test_retained_state_must_match_producer_contract() -> None:
    with pytest.raises(ValueError, match="contradicts"):
        UtilizedKnowledge(KnowledgeState.UTILIZED, UTILIZATION_SOURCE, "urn:other:reference", UTILIZATION_SCHEMA)
    with pytest.raises(ValueError, match="contradicts"):
        UtilizedKnowledge(KnowledgeState.UNSUPPORTED, UTILIZATION_SOURCE, IDENTIFIER, UTILIZATION_SCHEMA)
    with pytest.raises(ValueError):
        UtilizedKnowledge(cast(KnowledgeState, "bogus"), UTILIZATION_SOURCE, IDENTIFIER, UTILIZATION_SCHEMA)


def test_corrupt_retained_document_is_refused() -> None:
    with pytest.raises(ValueError):
        UtilizedKnowledge.from_document({"state": "utilized", "source": UTILIZATION_SOURCE, "identifier": IDENTIFIER})
    with pytest.raises(ValueError):
        UtilizedKnowledge.from_document(
            {"state": "future", "source": UTILIZATION_SOURCE, "identifier": IDENTIFIER, "version": UTILIZATION_SCHEMA}
        )


def test_export_states_are_explicit_and_restricted() -> None:
    assert export_document(None) == {"state": "not_retained"}
    exported = export_document(utilized())
    assert exported == {
        **utilized().to_document(),
        "authority": "provenance-only",
        "distribution": "local-only",
    }
    assert (
        export_document(UtilizedKnowledge.from_reference(reference(version="mi.utilization-working/2")))["state"]
        == "unsupported_version"
    )


def test_allocation_codec_retains_knowledge_as_version_3() -> None:
    allocation = replace(BASE, decision_provenance="{}", knowledge_utilization=utilized())
    data = encode_allocation(allocation)
    document = cast(dict[str, object], json.loads(data))
    assert document["version"] == 3
    inner = cast(dict[str, object], document["allocation"])
    assert inner["knowledge_utilization"] == utilized().to_document()
    assert decode_allocation(data) == allocation
    assert encode_allocation(decode_allocation(data)) == data


def test_knowledge_reference_needs_no_requirements_evidence() -> None:
    knowledge_only = replace(BASE, knowledge_utilization=utilized())
    document = cast(dict[str, object], json.loads(encode_allocation(knowledge_only)))
    inner = cast(dict[str, object], document["allocation"])
    assert document["version"] == 3
    assert inner["requirements_provenance"] is None
    assert decode_allocation(encode_allocation(knowledge_only)) == knowledge_only
    both = replace(knowledge_only, requirements_provenance='{"ordinary":true}')
    assert cast(dict[str, object], json.loads(encode_allocation(both)))["version"] == 3
    assert decode_allocation(encode_allocation(both)) == both


def test_older_document_versions_stay_decodable_without_knowledge() -> None:
    assert cast(dict[str, object], json.loads(encode_allocation(BASE)))["version"] == 1
    ordinary = replace(BASE, requirements_provenance='{"ordinary":true}')
    assert cast(dict[str, object], json.loads(encode_allocation(ordinary)))["version"] == 2
    assert decode_allocation(encode_allocation(ordinary)) == ordinary
    legacy = {
        "runtime_id": "codex",
        "provider_id": "openai",
        "model_id": "fixture-model",
        "capabilities": ["reference"],
        "context_tokens": 4096,
        "rationale": "controller binding",
        "reasoning_effort": None,
    }
    assert decode_allocation(json.dumps(legacy).encode()) == replace(BASE, reasoning_effort=None)
    poisoned = cast(dict[str, object], json.loads(encode_allocation(ordinary)))
    cast(dict[str, object], poisoned["allocation"])["knowledge_utilization"] = utilized().to_document()
    with pytest.raises(ValueError, match="invalid allocation fields"):
        decode_allocation(json.dumps(poisoned).encode())


@pytest.mark.parametrize(
    "tampering",
    [
        "mislabel",
        "wrong-digest",
        "drop-key",
        "non-object",
    ],
)
def test_tampered_retained_knowledge_is_refused_on_decode(tampering: str) -> None:
    document = cast(dict[str, object], json.loads(encode_allocation(replace(BASE, knowledge_utilization=utilized()))))
    inner = cast(dict[str, object], document["allocation"])
    retained = cast(dict[str, object], inner["knowledge_utilization"])
    if tampering == "mislabel":
        retained["state"] = "unsupported_version"
    elif tampering == "wrong-digest":
        retained["identifier"] = f"urn:mi:utilization:sha256:{'c' * 63}"
    elif tampering == "drop-key":
        del retained["version"]
    else:
        inner["knowledge_utilization"] = "utilized"
    with pytest.raises(ValueError):
        decode_allocation(json.dumps(document).encode())


def test_unsupported_retained_reference_survives_restart_decode() -> None:
    unsupported = UtilizedKnowledge.from_reference(reference(version="mi.utilization-working/2"))
    allocation = replace(BASE, knowledge_utilization=unsupported)
    assert decode_allocation(encode_allocation(allocation)) == allocation
    restarted = decode_allocation(encode_allocation(allocation))
    assert restarted is not allocation
    assert restarted.knowledge_utilization == unsupported
    assert restarted.knowledge_utilization is not None
    assert restarted.knowledge_utilization.state is KnowledgeState.UNSUPPORTED
