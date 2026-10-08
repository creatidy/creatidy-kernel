# SPDX-License-Identifier: Apache-2.0
"""Issue #51 reception through real intake/HTTP and immutable Attempt recovery.

Synthetic examples follow Router 5c48d51f1eb1f11424a5100eb2ccf20a6cba4581
kernel_requirements.py, selection_types.py and routing_core.RequestBinding.
No producer package, live Router, inference or ordinary dispatcher is used.
"""

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

import pytest
from test_allocation_recovery import WORKSPACE, NoCollection, RecordingRuntime
from test_intake import OWNER, SUBJECT, Scenario, declaration, policy
from test_scarcity_router import BINDING, decision, fixture, selected, server

from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
from creatidy_kernel.adapters.reference import POLICY, ReferenceInterrupted
from creatidy_kernel.adapters.scarcity_router import (
    INTERFACE_PREFIX,
    PRODUCER_REVISION,
    QUALITY_PREFIX,
    RouterFailureCategory,
    RuntimeSupport,
    ScarcityRouterAllocator,
    ScarcityRouterUnavailable,
    requirement_markers,
)
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AuthorityEnvelope,
    CancelProgram,
    PrepareAttempt,
    ProgramStatus,
)
from creatidy_kernel.core.execution import Lookup, OperationKey, Presence
from creatidy_kernel.core.intake import IntakeRefused, RequirementsHandoff, TranslationRefusal, encode
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.ports.allocation import decode_allocation, encode_allocation, load_allocation
from creatidy_kernel.ports.application import advance_work_unit, manifest_bytes
from creatidy_kernel.ports.intake import OrdinaryIntake

pytest_plugins = ["test_sqlite_store"]
SUPPORT = RuntimeSupport(
    "codex",
    "synthetic-version-1",
    frozenset({"tool_calls", "reasoning_mode", "reasoning_controls", "structured_output", "streaming"}),
    8192,
    "fixture:independent-harness-evidence",
)
CODING: dict[str, object] = {
    "task_level": "L3",
    "capability_minima": {"coding": 4, "reasoning": 3, "tool_use": 3},
    "hard_constraints": {
        "requires_tool_use": True,
        "minimum_input_context_tokens": 2048,
        "minimum_output_tokens": 1024,
    },
}
EDITORIAL: dict[str, object] = {
    "task_level": "L2",
    "capability_minima": {"writing_editorial": 4, "translation_multilingual": 3},
    "hard_constraints": {"minimum_input_context_tokens": 1000},
}


def handoff(approved_requirement: dict[str, object] = CODING, /, **changes: object) -> RequirementsHandoff:
    scenario = Scenario()
    values: dict[str, object] = requirement_markers(approved_requirement) | {"context": list[str]()} | changes
    meaning = declaration(**values)
    from creatidy_kernel.core.intake import Draft

    draft = Draft("projection", 1, None, meaning, scenario.reader.read(SUBJECT, meaning))
    return RequirementsHandoff(draft, "inert-subject", b'{"synthetic":"inert"}', "synthetic-program")


def translated(subject: RequirementsHandoff) -> ResourceRequest:
    result = ScarcityRouterAllocator.translate_ordinary(subject)
    assert isinstance(result, ResourceRequest)
    assert result.requirements_handoff is subject
    return result


def response(requirement: dict[str, object], *, profile: str | None = None, version: int = 7) -> dict[str, object]:
    result = fixture()
    decision(result)["requirement"] = requirement
    if profile is not None:
        decision(result).update(profile_id=profile, profile_policy_version=version)
    return result


@pytest.mark.parametrize("requirement", [CODING, EDITORIAL])
def test_intake_http_original_subject_and_durable_unknown_recovery(
    sqlite_tmp_path: Path, requirement: dict[str, object]
) -> None:
    scenario = Scenario()
    trusted_policy = replace(
        policy(),
        authority=AuthorityEnvelope(
            "owner", trusted_satisfaction_issuers=frozenset({"verifier"}), delegated_actor_ids=frozenset({"worker"})
        ),
    )
    meaning = declaration(**requirement_markers(requirement), context=[])
    path = sqlite_tmp_path / "ordinary.db"
    document = response(requirement)
    with server(encode(document)) as (origin, requests), SQLiteProgramStore(path) as store:
        allocator = ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,))
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("issue51", SUBJECT, meaning, expected_parent=None)
        approved = intake.approve(OWNER, draft, trusted_policy, decision_id="decision51", expires_at=200)
        request = intake.handoff(OWNER, approved, trusted_policy, allocator)
        assert isinstance(request, ResourceRequest)
        assert requests == []  # Projection is inert.
        allocation = intake.select(OWNER, approved, trusted_policy, allocator)
        assert isinstance(allocation, Allocation)
        assert requests[0]["body"] == {"requirement": requirement}
        assert requests[0]["path"] == "/v1/select"
        assert allocation.decision_provenance == encode(document).decode()
        assert allocation.requirements_provenance is not None
        evidence = cast(dict[str, object], json.loads(allocation.requirements_provenance))
        assert evidence["draft"] == draft.payload()
        assert cast(dict[str, object], evidence["draft"])["declaration"] == meaning.raw.decode()
        assert evidence["decision_bytes"] == approved.decision_bytes.decode()
        assert evidence["program_digest"] == approved.program.spec.digest
        assert evidence["sent_request"] == requests[0]["body"]
        assert evidence["producer_revision"] == PRODUCER_REVISION
        assert cast(dict[str, object], evidence["runtime_support"])["evidence_reference"] == SUPPORT.evidence_reference
        assert approved.program.status is ProgramStatus.DRAFT and not approved.program.attempts
        assert scenario.transport.pushes == scenario.transport.posts == 0
        # Explicit synthetic controller composes existing core/journal primitives,
        # not a newly introduced ordinary native dispatcher or inferred grant.
        program = store.admit(approved.program.program_id, "activate", ActivateProgram(0, "owner"))
        data = encode_allocation(allocation)
        context = manifest_bytes([asdict(item) for item in program.resolved_inputs("ordinary")])
        attempt = AttemptSpec(
            "ordinary:issue51:ordinary",
            program.program_id,
            "ordinary",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("ordinary"),
            "sha256:" + hashlib.sha256(data).hexdigest(),
            "reference:v1",
            WORKSPACE.key,
            "sha256:" + hashlib.sha256(context).hexdigest(),
        )
        operation = "runtime:" + attempt.attempt_id
        store.admit_with_intent(
            program.program_id,
            "prepare",
            PrepareAttempt(program.revision, "worker", attempt),
            operation,
            operation,
            {
                "version": 1,
                "attempt": attempt.digest,
                "workspace": WORKSPACE.key,
                "allocation": data.decode(),
                "context": context.decode(),
            },
        )
        original_operation = store.operation(operation)
        assert decode_allocation(data) == allocation and json.loads(data)["version"] == 2

    class UnknownRuntime(RecordingRuntime):
        def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup:
            return Lookup(Presence.UNKNOWN)

    runtime = UnknownRuntime()

    class ForbiddenAllocator:
        def select(self, request: ResourceRequest) -> Allocation:
            pytest.fail("existing Attempt must never select again")

    with SQLiteProgramStore(path) as store:
        assert OrdinaryIntake(store, scenario.reader, lambda: 100).history("issue51") == (draft,)

        def lost_receipt(point: str) -> None:
            if point == "send":
                raise ReferenceInterrupted(point)

        with pytest.raises(ReferenceInterrupted, match="send"):
            advance_work_unit(
                store,
                store.load(program.program_id),
                "ordinary",
                now=100,
                fault=lost_receipt,
                allocator=ForbiddenAllocator(),
                runtime=runtime,
                workspace=WORKSPACE,
                collector=NoCollection(),
                policy=POLICY,
            )
        assert load_allocation(store, attempt) == allocation
        # Actual crash-after-send boundary: no receipt, unavailable reconciliation.
        assert (
            advance_work_unit(
                store,
                store.load(program.program_id),
                "ordinary",
                now=102,
                allocator=ForbiddenAllocator(),
                runtime=runtime,
                workspace=WORKSPACE,
                collector=NoCollection(),
                policy=POLICY,
            )
            == "unknown"
        )
        after = store.operation(operation)
        assert after.request_json == original_operation.request_json
        assert after.request_digest == original_operation.request_digest
        assert len(runtime.requests) == 1 and load_allocation(store, attempt) == allocation


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"quality": []}, "quality.unsupported"),
        ({"quality": ["excellent coding"]}, "quality.unsupported"),
        ({"quality": [QUALITY_PREFIX + '{"requirement":{},"allowed_models":[]}']}, "quality.invalid_requirement"),
        ({"quality": [QUALITY_PREFIX + '{"requirement":null,"requirement":null}']}, "quality.invalid_requirement"),
        ({"context": ["private local context; no egress"]}, "context.unsupported"),
        ({"unknowns": ["context unknown"]}, "unknowns.unresolved"),
        ({"interface": ["UTF-8"]}, "interface.unsupported"),
        ({"interface": [INTERFACE_PREFIX + '{"profile_alias":"any"}']}, "interface.recommendation_unsupported"),
        ({"interface": [INTERFACE_PREFIX + '{"pinned_target":{}}']}, "interface.recommendation_unsupported"),
        ({"interface": [INTERFACE_PREFIX + '{"maximum_output_tokens":100}']}, "interface.recommendation_unsupported"),
        ({"interface": [INTERFACE_PREFIX + '{"access_mode":"subscription"}']}, "interface.recommendation_unsupported"),
        ({"interface": [INTERFACE_PREFIX + '{"channel":"native"}']}, "interface.recommendation_unsupported"),
        (
            {"interface": [INTERFACE_PREFIX + '{"requires_tool_calls":null}']},
            "interface.invalid_or_contradictory_binding",
        ),
    ],
)
def test_unsupported_meaning_keeps_all_original_fields(changes: dict[str, object], code: str) -> None:
    subject = handoff(**changes)
    result = ScarcityRouterAllocator.translate_ordinary(subject)
    assert isinstance(result, TranslationRefusal)
    assert result.handoff is subject and result.problems == (code,)


@pytest.mark.parametrize(
    "hard,code",
    [
        ({"requires_vision": True}, "quality.vision_channel_unsupported"),
        ({"privacy_constraint": "local-only"}, "quality.privacy_unsupported"),
        (
            {"required_provider": "openai", "required_model": {"provider": "zai", "model": "fixture"}},
            "quality.invalid_requirement",
        ),
        ({"minimum_input_context_tokens": True}, "quality.invalid_requirement"),
        ({"requires_tool_use": None}, "quality.invalid_requirement"),
        ({"requires_tool_use": 1}, "quality.invalid_requirement"),
        ({"resource_id": "fixture"}, "quality.invalid_requirement"),
    ],
)
def test_unsupported_invalid_or_contradictory_hard_requirements(hard: dict[str, object], code: str) -> None:
    subject = handoff(quality=[QUALITY_PREFIX + json.dumps({"requirement": CODING | {"hard_constraints": hard}})])
    result = ScarcityRouterAllocator.translate_ordinary(subject)
    assert isinstance(result, TranslationRefusal) and result.problems == (code,)


@pytest.mark.parametrize("minimum", [0, True, 6, 1.5, "4", {}, []])
def test_invalid_quality_scale_is_not_inferred(minimum: object) -> None:
    subject = handoff(
        quality=[QUALITY_PREFIX + json.dumps({"requirement": CODING | {"capability_minima": {"coding": minimum}}})]
    )
    assert isinstance(ScarcityRouterAllocator.translate_ordinary(subject), TranslationRefusal)


def test_structural_demands_and_exact_pin_are_preserved_separately() -> None:
    interface: dict[str, object] = {
        "explicit_model": {"provider": "openai", "model": BINDING.model_id},
        "explicit_variant": "opaque-configuration",
        "requires_tool_calls": True,
        "requires_reasoning_controls": True,
        "requires_structured_output": True,
        "requires_streaming": True,
        "minimum_input_context_tokens": 3000,
    }
    subject = handoff(**requirement_markers(CODING, interface=interface))
    request = translated(subject)
    expected = CODING | {
        "hard_constraints": cast(dict[str, object], CODING["hard_constraints"])
        | {
            "minimum_input_context_tokens": 3000,
            "required_model": interface["explicit_model"],
            "required_variant": "opaque-configuration",
            "requires_reasoning_mode": True,
        }
    }
    assert request.context_tokens == 3000
    with server(encode(response(expected))) as (origin, requests):
        allocation = ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,)).select(request)
    assert requests[0]["body"] == {"requirement": expected}  # Never RequestBinding on select wire.
    assert allocation.requirements_provenance is not None
    assert json.loads(allocation.requirements_provenance)["interface"] == interface
    for requirement in [
        CODING | {"hard_constraints": {"required_provider": "zai"}},
        CODING | {"hard_constraints": {"required_variant": "other"}},
    ]:
        bad = ScarcityRouterAllocator.translate_ordinary(
            handoff(**requirement_markers(requirement, interface=interface))
        )
        assert isinstance(bad, TranslationRefusal)


@pytest.mark.parametrize(
    "support,binding",
    [
        (None, BINDING),
        (replace(SUPPORT, features=frozenset()), BINDING),
        (replace(SUPPORT, output_tokens=1023), BINDING),
        (SUPPORT, replace(BINDING, context_tokens=2047)),
        (SUPPORT, replace(BINDING, provider_id="zai")),
    ],
)
def test_no_compatible_harness_or_pin_refuses_before_transport(
    support: RuntimeSupport | None, binding: Allocation
) -> None:
    requirement = CODING | {
        "hard_constraints": cast(dict[str, object], CODING["hard_constraints"])
        | {"required_model": {"provider": "openai", "model": BINDING.model_id}}
    }
    with server(b"unreachable content") as (origin, requests):
        allocator = ScarcityRouterAllocator(origin, (binding,), runtime_support=() if support is None else (support,))
        with pytest.raises(ScarcityRouterUnavailable) as caught:
            allocator.select(translated(handoff(requirement)))
        assert caught.value.category is RouterFailureCategory.SELECTION_INCOMPATIBLE
        assert requests == []


@pytest.mark.parametrize("change", ["model", "effort", "variant", "requirement", "catalog", "policy"])
def test_selection_cannot_expand_approved_pin_or_configured_harness(change: str) -> None:
    requirement = CODING | {
        "hard_constraints": cast(dict[str, object], CODING["hard_constraints"])
        | {"required_variant": "opaque-configuration"}
    }
    document = response(requirement)
    if change == "model":
        selected(document)["identity"] = {"provider": "openai", "model": "wrong", "variant": "opaque-configuration"}
    elif change == "effort":
        selected(document)["reasoning_effort"] = "low"
    elif change == "variant":
        selected(document)["identity"] = {"provider": "openai", "model": BINDING.model_id, "variant": "other"}
    elif change == "requirement":
        decision(document)["requirement"] = EDITORIAL
    else:
        decision(document)["catalog_version" if change == "catalog" else "resource_policy_version"] = None
    with server(encode(document)) as (origin, requests):
        with pytest.raises(ScarcityRouterUnavailable):
            ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,)).select(
                translated(handoff(requirement))
            )
        assert len(requests) == 1


@pytest.mark.parametrize("change", [None, "profile", "version", "floor", "missing"])
def test_profile_xor_monotone_expansion_and_version_echo(change: str | None) -> None:
    requirement = CODING
    document = response(requirement, profile="synthetic-coding")
    if change == "profile":
        decision(document)["profile_id"] = "other"
    elif change == "version":
        decision(document)["profile_policy_version"] = 8
    elif change == "missing":
        decision(document).pop("profile_policy_version")
    elif change == "floor":
        decision(document)["requirement"] = CODING | {"capability_minima": {"coding": 5}}
    subject = handoff(**requirement_markers(requirement, profile_id="synthetic-coding"))
    with server(encode(document)) as (origin, requests):
        allocator = ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,), profile_policy_version=7)
        if change is None:
            assert allocator.select(translated(subject)).requirements_provenance is not None
        else:
            with pytest.raises(ScarcityRouterUnavailable) as caught:
                allocator.select(translated(subject))
            assert caught.value.category is RouterFailureCategory.INVALID_RESPONSE
        assert requests[0]["body"] == {"profile_id": "synthetic-coding", "tightening": requirement}
        assert "requirement" not in cast(dict[str, object], requests[0]["body"])


@pytest.mark.parametrize(
    "status,category",
    [
        (400, RouterFailureCategory.REQUEST_INVALID),
        (503, RouterFailureCategory.HTTP_REJECTED),
        (200, RouterFailureCategory.NO_ELIGIBLE_SELECTION),
    ],
)
def test_requirement_no_solution_and_infrastructure_diagnostics(status: int, category: RouterFailureCategory) -> None:
    document = response(CODING)
    decision(document).update(selected=None, degraded=False, reason_codes=["no_eligible_candidate"])
    with server(encode(document), status=status) as (origin, _):
        with pytest.raises(ScarcityRouterUnavailable) as caught:
            ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,)).select(translated(handoff()))
    assert caught.value.category is category
    assert (caught.value.decision_provenance is not None) == (status == 200)


def test_unapproved_changed_request_and_fixed_fallback_refuse_before_transport() -> None:
    request = translated(handoff())
    with server(encode(response(CODING))) as (origin, requests):
        allocator = ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,))
        for changed in [
            replace(request, context_tokens=0),
            replace(request, required_capabilities=frozenset()),
            replace(request, work_unit_id="different"),
        ]:
            with pytest.raises(ScarcityRouterUnavailable):
                allocator.select(changed)
        assert not requests
        with pytest.raises(AllocationUnavailable):
            FixedAllocator(BINDING).select(request)


@pytest.mark.parametrize("change", ["issue", "revision", "cancel", "expiry"])
def test_current_authority_and_subject_rechecked_after_selection(sqlite_tmp_path: Path, change: str) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "authority.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare(
            "authority", SUBJECT, declaration(**requirement_markers(CODING), context=[]), expected_parent=None
        )
        approved = intake.approve(OWNER, draft, policy(), decision_id="authority", expires_at=200)

        class ChangedAllocator:
            translate_ordinary = staticmethod(ScarcityRouterAllocator.translate_ordinary)

            def select(self, request: ResourceRequest) -> Allocation:
                if change == "issue":
                    scenario.transport.issue["body"] = "changed"
                elif change == "revision":
                    intake.prepare(
                        "authority",
                        SUBJECT,
                        declaration(**requirement_markers(EDITORIAL), context=[]),
                        expected_parent=draft.digest,
                    )
                elif change == "cancel":
                    store.admit(approved.program.program_id, "cancel", CancelProgram(0, "owner"))
                else:
                    scenario.time = 200
                return BINDING

        with pytest.raises(IntakeRefused):
            intake.select(OWNER, approved, policy(), ChangedAllocator())
        assert not store.load(approved.program.program_id).attempts


@pytest.mark.parametrize("where", ["declaration", "response", "support"])
def test_credentials_never_become_ordinary_evidence(where: str) -> None:
    token = "synthetic-bearer-credential"  # noqa: S105 - deliberately synthetic.
    subject = handoff(provenance=[token] if where == "declaration" else ["safe"])
    document = response(CODING)
    if where == "response":
        decision(document)["extra"] = [token]
    support = replace(SUPPORT, evidence_reference=token) if where == "support" else SUPPORT
    with server(encode(document)) as (origin, requests):
        with pytest.raises(ScarcityRouterUnavailable) as caught:
            ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(support,), api_key=token).select(
                translated(subject)
            )
        assert caught.value.decision_provenance is None
        assert token not in str(caught.value)
        assert len(requests) == (0 if where == "declaration" else 1)


def test_escaped_declaration_credential_refuses_before_transport() -> None:
    from creatidy_kernel.core.intake import Declaration

    token = "synthetic-bearer-credential"  # noqa: S105 - deliberately synthetic.
    subject = handoff(provenance=[token])
    escaped = "".join("\\u" + format(ord(character), "04x") for character in token)
    raw = subject.draft.declaration.raw.replace(token.encode(), escaped.encode())
    subject = replace(subject, draft=replace(subject.draft, declaration=Declaration(raw)))
    assert subject.draft.declaration.value["provenance"] == [token] and token.encode() not in raw
    with server(encode(response(CODING))) as (origin, requests):
        with pytest.raises(ScarcityRouterUnavailable) as caught:
            ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,), api_key=token).select(
                translated(subject)
            )
        assert caught.value.category is RouterFailureCategory.REQUEST_UNSUPPORTED
        assert caught.value.decision_provenance is None and requests == []
        assert subject.draft.declaration.raw == raw


def test_quality_reasoning_mode_requires_independent_reasoning_controls() -> None:
    requirement = CODING | {
        "hard_constraints": cast(dict[str, object], CODING["hard_constraints"]) | {"requires_reasoning_mode": True}
    }
    request = translated(handoff(requirement))
    assert (
        request.requirements_handoff is not None
        and request.requirements_handoff.draft.declaration.value["interface"] == []
    )
    with server(encode(response(requirement))) as (origin, requests):
        with pytest.raises(ScarcityRouterUnavailable) as caught:
            ScarcityRouterAllocator(
                origin,
                (BINDING,),
                runtime_support=(replace(SUPPORT, features=SUPPORT.features - {"reasoning_controls"}),),
            ).select(request)
        assert caught.value.category is RouterFailureCategory.SELECTION_INCOMPATIBLE and requests == []


@pytest.mark.parametrize("authenticated", [False, True])
def test_near_bound_declaration_has_identical_authenticated_evidence_outcome(authenticated: bool) -> None:
    subject = handoff(provenance=["synthetic-padding-" + "x" * 261000])
    assert len(subject.draft.declaration.raw) < 262144
    token = "synthetic-bearer-credential"  # noqa: S105 - deliberately synthetic.
    with server(encode(response(CODING))) as (origin, requests):
        allocation = ScarcityRouterAllocator(
            origin, (BINDING,), runtime_support=(SUPPORT,), api_key=token if authenticated else None
        ).select(translated(subject))
        assert len(requests) == 1 and allocation.requirements_provenance is not None
        assert len(allocation.requirements_provenance.encode()) > 262144
        assert (
            json.loads(allocation.requirements_provenance)["draft"]["declaration"]
            == subject.draft.declaration.raw.decode()
        )


@pytest.mark.parametrize("authenticated,credential_in_policy", [(False, False), (True, False), (True, True)])
def test_large_real_approval_decision_uses_intake_decoder(
    sqlite_tmp_path: Path, authenticated: bool, credential_in_policy: bool
) -> None:
    token = "synthetic-bearer-credential"  # noqa: S105 - deliberately synthetic.
    scenario = Scenario()
    trusted = replace(
        policy(),
        paths=policy().paths
        | frozenset(f"src/approved_component_{index:05d}.py" for index in range(10000))
        | (frozenset({f"src/{token}.py"}) if credential_in_policy else frozenset()),
    )
    with SQLiteProgramStore(sqlite_tmp_path / "large-policy.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: 100)
        draft = intake.prepare(
            "large", SUBJECT, declaration(**requirement_markers(CODING), context=[]), expected_parent=None
        )
        approved = intake.approve(OWNER, draft, trusted, decision_id="large", expires_at=200)
        original = approved.decision_bytes
        assert len(original) > 262144
        with server(encode(response(CODING))) as (origin, requests):
            allocator = ScarcityRouterAllocator(
                origin, (BINDING,), runtime_support=(SUPPORT,), api_key=token if authenticated else None
            )
            if credential_in_policy:
                with pytest.raises(ScarcityRouterUnavailable) as caught:
                    intake.select(OWNER, approved, trusted, allocator)
                assert caught.value.category is RouterFailureCategory.REQUEST_UNSUPPORTED
                assert caught.value.decision_provenance is None and requests == []
            else:
                result = intake.select(OWNER, approved, trusted, allocator)
                assert isinstance(result, Allocation) and result.requirements_provenance is not None
                assert len(requests) == 1
                assert json.loads(result.requirements_provenance)["decision_bytes"] == original.decode()
        assert intake.historical_decision("large") == original


def test_invalid_local_approval_encoding_has_typed_safe_refusal() -> None:
    token = "synthetic-bearer-credential"  # noqa: S105 - deliberately synthetic.
    subject = replace(handoff(), decision_bytes=b"not a valid approval encoding")
    with server(encode(response(CODING))) as (origin, requests):
        with pytest.raises(ScarcityRouterUnavailable) as caught:
            ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,), api_key=token).select(
                translated(subject)
            )
        assert caught.value.category is RouterFailureCategory.REQUEST_UNSUPPORTED
        assert caught.value.decision_provenance is None and requests == []


def test_producer_null_false_normalization_preserves_original_bytes() -> None:
    requirement: dict[str, object] = {
        "task_level": "L2",
        "capability_minima": {"coding": 3, "reasoning": None},
        "hard_constraints": {
            "requires_tool_use": False,
            "requires_reasoning_mode": False,
            "required_provider": None,
            "required_model": None,
            "minimum_input_context_tokens": None,
        },
    }
    interface: dict[str, object] = {
        "requires_streaming": False,
        "maximum_output_tokens": None,
        "profile_alias": None,
        "pinned_target": None,
        "explicit_model": None,
        "explicit_variant": None,
    }
    subject = handoff(**requirement_markers(requirement, interface=interface))
    expected: dict[str, object] = {"task_level": "L2", "capability_minima": {"coding": 3}, "hard_constraints": {}}
    with server(encode(response(expected))) as (origin, requests):
        allocation = ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,)).select(translated(subject))
    assert requests[0]["body"] == {"requirement": expected}
    assert allocation.requirements_provenance is not None
    assert (
        json.loads(allocation.requirements_provenance)["draft"]["declaration"] == subject.draft.declaration.raw.decode()
    )


@pytest.mark.parametrize(
    "feature", ["tool_calls", "structured_output", "streaming", "reasoning_controls", "reasoning_mode"]
)
def test_structural_requirements_never_create_harness_capability(feature: str) -> None:
    interface: dict[str, object] = {
        "requires_tool_calls": True,
        "requires_structured_output": True,
        "requires_streaming": True,
        "requires_reasoning_controls": True,
    }
    subject = handoff(**requirement_markers(CODING, interface=interface))
    with server(encode(response(CODING))) as (origin, requests):
        with pytest.raises(ScarcityRouterUnavailable) as caught:
            ScarcityRouterAllocator(
                origin, (BINDING,), runtime_support=(replace(SUPPORT, features=SUPPORT.features - {feature}),)
            ).select(translated(subject))
        assert caught.value.category is RouterFailureCategory.SELECTION_INCOMPATIBLE
        assert not requests


def test_profile_quality_cannot_omit_structural_floor_or_version_before_transport() -> None:
    incomplete = handoff(
        **requirement_markers(CODING, profile_id="synthetic-coding", interface={"minimum_input_context_tokens": 3000})
    )
    result = ScarcityRouterAllocator.translate_ordinary(incomplete)
    assert isinstance(result, TranslationRefusal) and result.problems == ("quality.profile_requires_full_projection",)
    complete = handoff(**requirement_markers(CODING, profile_id="synthetic-coding"))
    with server(encode(response(CODING, profile="synthetic-coding"))) as (origin, requests):
        with pytest.raises(ScarcityRouterUnavailable, match="version"):
            ScarcityRouterAllocator(origin, (BINDING,), runtime_support=(SUPPORT,)).select(translated(complete))
        assert not requests


def test_original_v1_allocation_encoding_and_invalid_v2_evidence() -> None:
    original: dict[str, object] = asdict(BINDING)
    original.pop("requirements_provenance")
    original["capabilities"] = sorted(BINDING.capabilities)
    assert encode_allocation(BINDING) == encode({"version": 1, "allocation": original})
    invalid: tuple[object, ...] = (None, "", 1, {})
    for evidence in invalid:
        with pytest.raises(ValueError):
            decode_allocation(encode({"version": 2, "allocation": original | {"requirements_provenance": evidence}}))


def test_ordinary_program_cannot_fall_back_to_reference_dispatch(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "no-fallback.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: 100)
        draft = intake.prepare(
            "no-fallback", SUBJECT, declaration(**requirement_markers(CODING), context=[]), expected_parent=None
        )
        approved = intake.approve(OWNER, draft, policy(), decision_id="no-fallback", expires_at=200)
        program = store.admit(approved.program.program_id, "activate", ActivateProgram(0, "owner"))
        runtime = RecordingRuntime()
        with pytest.raises(ValueError, match="ordinary dispatch"):
            advance_work_unit(
                store,
                program,
                "ordinary",
                allocator=FixedAllocator(BINDING),
                runtime=runtime,
                workspace=WORKSPACE,
                collector=NoCollection(),
                policy=POLICY,
                now=100,
            )
        assert not runtime.requests and not store.load(program.program_id).attempts
