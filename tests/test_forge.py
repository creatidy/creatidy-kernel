# SPDX-License-Identifier: Apache-2.0
"""Offline conformance for the fake and Forgejo reference adapter."""

from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.fake_forge import FakeForge, SyntheticForgeTransport
from creatidy_kernel.adapters.forge_refs import pr_payload
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_transport import LocalRequestRefusal
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import (
    CheckResult,
    Effect,
    EffectStatus,
    ForgeConflict,
    Presence,
    Reference,
    UnsupportedForge,
    effect_marker,
)
from creatidy_kernel.ports.forge import Forge

REPO = Reference("forgejo:team/project")
BASE = Reference("forgejo:" + "b" * 40)
HEAD = Reference("forgejo:" + "a" * 40)
NEXT_HEAD = Reference("forgejo:" + "c" * 40)
MOVED_BASE = "d" * 40


@pytest.fixture(params=["fake", "forgejo"])
def boundary(request: pytest.FixtureRequest) -> tuple[Forge, SyntheticForgeTransport, list[Effect]]:
    transport = SyntheticForgeTransport(REPO)
    permitted: list[Effect] = []

    def authorize(effect: Effect) -> bool:
        return effect in permitted

    forge: Forge = (
        FakeForge(transport, authorize)
        if request.param == "fake"
        else ForgejoForge(transport, transport, authorize, page_size=2, isolation=transport)
    )
    return forge, transport, permitted


def operation(action: str, *, attempts: int = 1, key: str | None = None) -> Effect:
    return Effect(
        OperationKey(f"op-{action}", key or f"effect-{action}", f"digest-{action}"),
        REPO,
        action,
        "feature",
        expected=HEAD if action == "push" else None,
        revision=NEXT_HEAD if action == "push" else HEAD,
        base_branch="develop" if action in {"pr", "branch"} else None,
        base_revision=BASE if action == "branch" else None,
        title="Patch" if action == "pr" else None,
        fence=1,
        delivery_attempts=attempts,
    )


def test_observations_and_pagination(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, _ = boundary
    assert {"identity", "branch", "checks", "pr", "conditional_push"} <= forge.capabilities()
    assert forge.identity(REPO).presence is Presence.FOUND
    assert forge.branch(REPO, "feature").revision == HEAD
    assert forge.branch(REPO, "missing").presence is Presence.UNKNOWN
    transport.statuses.extend(
        [
            {"id": 1, "context": "unit", "status": "success"},
            {"id": 2, "context": "lint", "status": "failure"},
            {"id": 3, "context": "review", "status": "pending"},
        ]
    )
    first = forge.checks(REPO, HEAD)
    assert len(first.items) == 2 and not first.complete and first.next_cursor == "2"
    assert first.items[0].revision == HEAD
    assert first.items[0].check_context == "unit" and first.items[0].check_result is CheckResult.PASSED
    assert first.items[1].check_result is CheckResult.FAILED
    second = forge.checks(REPO, HEAD, first.next_cursor)
    assert len(second.items) == 1 and not second.complete
    assert second.items[0].check_result is CheckResult.PENDING
    assert forge.checks(REPO, HEAD, second.next_cursor).complete
    assert forge.checks(REPO, Reference("forgejo:" + "d" * 40)).items == ()
    transport.forbidden = True
    assert forge.identity(REPO).presence is Presence.INACCESSIBLE
    assert not forge.changes(REPO).complete


@pytest.mark.parametrize("cursor", ["invalid", "0", "01", "-1", "١", "9" * 5000])
def test_invalid_pagination_cursor_is_incomplete(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], cursor: str
) -> None:
    forge, _, _ = boundary
    for page in (forge.changes(REPO, cursor), forge.checks(REPO, HEAD, cursor)):
        assert page.items == () and page.next_cursor is None and not page.complete


def test_check_observations_fail_closed(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, _ = boundary
    transport.statuses.append({"id": 1, "context": "scan", "status": "error"})
    assert forge.checks(REPO, HEAD).items[0].check_result is CheckResult.ERROR
    transport.statuses[0]["status"] = "unexpected"
    assert forge.checks(REPO, HEAD).items[0].check_result is CheckResult.UNKNOWN
    del transport.statuses[0]["context"]
    assert not forge.checks(REPO, HEAD).complete


def test_exact_scope_and_conditional_push(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, permitted = boundary
    effect = operation("push")
    with pytest.raises(ForgeConflict):
        forge.apply(effect)
    assert transport.pushes == 0
    permitted.append(effect)
    assert forge.apply(effect).status is EffectStatus.ACCEPTED
    assert transport.pushes == 1
    assert forge.apply(effect).status is EffectStatus.STALE
    assert transport.pushes == 1
    changed = replace(effect, revision=Reference("forgejo:other"))
    with pytest.raises(ForgeConflict):
        forge.apply(changed)


def test_branch_creation_and_reconcile_lost_push(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = replace(operation("branch"), branch="new")
    permitted.append(effect)
    transport.lost_reply = True
    assert forge.apply(effect).status is EffectStatus.UNKNOWN
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    assert transport.pushes == 1
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.pushes == 1


def test_invalid_branch_replay_rejected_before_dispatch(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    _, transport, _ = boundary
    with pytest.raises(ForgeConflict, match="expect absence"):
        replace(operation("branch", attempts=2), branch="new", expected=HEAD)
    assert transport.pushes == 0


def test_lost_branch_reply_with_moved_source_remains_unknown(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = replace(operation("branch"), branch="new")
    permitted.append(effect)
    transport.lost_reply = True
    assert forge.apply(effect).status is EffectStatus.UNKNOWN
    transport.branches["develop"] = MOVED_BASE
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.pushes == 1


@pytest.mark.parametrize("action,field", [("push", "revision"), ("push", "expected"), ("branch", "base_revision")])
def test_wrong_revision_provider_rejected_before_effect(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], action: str, field: str
) -> None:
    forge, transport, permitted = boundary
    effect = replace(operation(action), **{field: Reference("github:head1")})
    permitted.append(effect)
    with pytest.raises(ForgeConflict, match="revision provider"):
        forge.apply(effect)
    assert transport.pushes == 0
    with pytest.raises(ForgeConflict, match="revision provider"):
        forge.reconcile(effect)
    with pytest.raises(ForgeConflict, match="revision provider"):
        forge.checks(REPO, Reference("github:head1"))


@pytest.mark.parametrize("invalid", ["short", "g" * 40, "a" * 39, "a" * 41, "a" * 65, "a" * 40 + "/x"])
@pytest.mark.parametrize(
    "action,field",
    [
        ("push", "revision"),
        ("push", "expected"),
        ("branch", "base_revision"),
        ("pr", "revision"),
    ],
)
def test_malformed_same_provider_effect_rejected_before_dispatch(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], invalid: str, action: str, field: str
) -> None:
    forge, transport, permitted = boundary
    effect = replace(operation(action), **{field: Reference(f"forgejo:{invalid}")})
    permitted.append(effect)
    with pytest.raises(ForgeConflict, match="full Git object ID"):
        forge.apply(effect)
    with pytest.raises(ForgeConflict, match="full Git object ID"):
        forge.reconcile(effect)
    assert transport.pushes == transport.posts == 0
    with pytest.raises(ForgeConflict, match="full Git object ID"):
        forge.checks(REPO, Reference(f"forgejo:{invalid}"))


@pytest.mark.parametrize("length", [40, 64])
def test_uppercase_full_oid_is_not_canonical(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], length: int
) -> None:
    forge, transport, permitted = boundary
    sha = "A" * length
    transport.branches["feature"] = sha
    assert forge.branch(REPO, "feature").presence is Presence.UNKNOWN
    effect = replace(operation("pr"), revision=Reference(f"forgejo:{sha}"))
    permitted.append(effect)
    with pytest.raises(ForgeConflict, match="full Git object ID"):
        forge.apply(effect)


@pytest.mark.parametrize("side", ["head", "base"])
def test_malformed_pr_revision_is_unknown_not_accepted(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], side: str
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None
    subject = transport.pulls[0][side]
    assert isinstance(subject, dict)
    subject["sha"] = "not-an-oid"
    assert forge.change(REPO, receipt.reference).presence is Presence.UNKNOWN
    assert not forge.changes(REPO).complete
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.UNKNOWN


def test_malformed_branch_revision_never_supports_pr_receipt(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = replace(operation("pr"), revision=Reference("forgejo:bad"))
    transport.branches["feature"] = "bad"
    assert forge.branch(REPO, "feature").presence is Presence.UNKNOWN
    permitted.append(effect)
    with pytest.raises(ForgeConflict):
        forge.apply(effect)
    assert transport.posts == 0


@pytest.mark.parametrize("side", ["head", "base"])
def test_malformed_pr_post_response_never_accepted(side: str) -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True, isolation=transport)
    original = transport.request

    def corrupted(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        status, payload = original(method, path, body)
        if method == "POST" and status == 201 and isinstance(payload, dict):
            subject = cast(dict[str, object], payload).get(side)
            assert isinstance(subject, dict)
            cast(dict[str, object], subject)["sha"] = "bad"
        return status, cast(object, payload)

    with patch.object(transport, "request", side_effect=corrupted):
        assert forge.apply(operation("pr")).status is EffectStatus.UNKNOWN
    assert transport.posts == 1


@pytest.mark.parametrize("number", [0, True, -1, 10**18, "1"])
def test_invalid_pr_number_never_supplies_receipt(number: object) -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True, isolation=transport)
    original = transport.request

    def corrupted(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        status, payload = original(method, path, body)
        if method == "POST" and status == 201 and isinstance(payload, dict):
            payload["number"] = number
        return status, cast(object, payload)

    with patch.object(transport, "request", side_effect=corrupted):
        assert forge.apply(operation("pr")).status is EffectStatus.UNKNOWN
    assert transport.posts == 1


@pytest.mark.parametrize("number", [0, True, -1, 10**18, "1"])
def test_invalid_stored_pr_number_is_not_observed(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], number: object
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None
    transport.pulls[0]["number"] = number
    assert forge.change(REPO, receipt.reference).presence is Presence.UNKNOWN
    assert not forge.changes(REPO).complete
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.UNKNOWN


@pytest.mark.parametrize(
    "path", ["te%61m/project", "team/pro%2Fject", "team/pro?ject", "team/pro#ject", "team/pro:ject"]
)
def test_noncanonical_repository_rejected_at_adapter_boundary(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], path: str
) -> None:
    forge, transport, permitted = boundary
    repository = Reference(f"forgejo:{path}")
    with pytest.raises(ForgeConflict, match="repository"):
        forge.identity(repository)
    with pytest.raises(ForgeConflict, match="repository"):
        forge.branch(repository, "feature")
    effect = replace(operation("push"), repository=repository)
    permitted.append(effect)
    with pytest.raises(ForgeConflict, match="repository"):
        forge.apply(effect)
    assert transport.pushes == transport.posts == 0


@pytest.mark.parametrize("branch", ["bad..name", "bad.lock", "a//b", "a/.hidden", "a@{b", "a?b", "a\\b"])
def test_invalid_git_branches_rejected_before_observation_or_effect(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], branch: str
) -> None:
    forge, transport, permitted = boundary
    with pytest.raises(ForgeConflict, match="Git branch"):
        forge.branch(REPO, branch)
    for action in ("branch", "push", "pr"):
        effect = replace(operation(action), branch=branch)
        permitted.append(effect)
        with pytest.raises(ForgeConflict, match="Git branch"):
            forge.apply(effect)
        with pytest.raises(ForgeConflict, match="Git branch"):
            forge.reconcile(effect)
        if action in {"branch", "pr"}:
            effect = replace(operation(action), base_branch=branch)
            permitted.append(effect)
            with pytest.raises(ForgeConflict, match="Git branch"):
                forge.apply(effect)
    assert transport.pushes == transport.posts == 0


@pytest.mark.parametrize(
    "attribute,missing", [("supports_pr", {"pr"}), ("supports_conditional_push", {"branch_create", "conditional_push"})]
)
def test_unsupported_transport_capabilities_fail_without_effects(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], attribute: str, missing: set[str]
) -> None:
    forge, transport, permitted = boundary
    setattr(transport, attribute, False)
    assert not (missing & forge.capabilities())
    assert forge.identity(REPO).presence is Presence.FOUND
    assert forge.branch(REPO, "feature").revision == HEAD
    for action, capability in (("branch", "branch_create"), ("push", "conditional_push"), ("pr", "pr")):
        effect = replace(operation(action), branch="new") if action == "branch" else operation(action)
        permitted.append(effect)
        if capability in missing:
            with pytest.raises(UnsupportedForge):
                forge.apply(effect)
            with pytest.raises(UnsupportedForge):
                forge.reconcile(effect)
    assert transport.pushes == transport.posts == 0


def test_supported_transport_capabilities(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, _, _ = boundary
    assert {"identity", "branch", "change", "checks", "branch_create", "conditional_push", "pr"} == forge.capabilities()


def test_pr_artifact_not_merge_and_replay(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None
    assert forge.change(REPO, receipt.reference).head == HEAD
    assert len(transport.pulls) == 1 and transport.posts == 1
    snapshot = pr_payload(effect, transport.max_bytes)[0]
    assert transport.pulls[0]["head"] == {"sha": "a" * 40, "ref": snapshot, "repo": {"full_name": "team/project"}}
    assert snapshot != effect.branch
    assert "merge" not in forge.capabilities()
    retry = replace(effect, delivery_attempts=2)
    permitted.append(retry)
    assert forge.apply(retry).status is EffectStatus.UNKNOWN
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED
    assert transport.posts == 1


def test_pr_mutable_branch_movement_does_not_block_exact_snapshot(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    transport.branches["feature"] = "c" * 40
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED
    assert receipt.reference is not None
    assert forge.change(REPO, receipt.reference).head == HEAD
    assert transport.branches["feature"] == "c" * 40
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED


def test_pr_snapshot_contention_fails_without_post(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    transport.contend_snapshot = True
    with pytest.raises(ForgeConflict, match="snapshot namespace"):
        forge.apply(effect)
    assert transport.posts == 0


def test_pr_base_revision_cannot_be_part_of_effect_authority() -> None:
    with pytest.raises(ForgeConflict, match="observation only"):
        replace(operation("pr"), base_revision=BASE)


def test_lost_pr_post_reply_releases_guard_but_does_not_retry(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    snapshot = pr_payload(effect, transport.max_bytes)[0]
    with transport.hold(REPO, snapshot):
        assert transport.compare_and_push(REPO, snapshot, None, HEAD) is EffectStatus.ACCEPTED
    transport.lost_reply = True
    assert forge.apply(effect).status is EffectStatus.UNKNOWN
    assert transport.posts == 1 and len(transport.pulls) == 1
    assert transport.branches[snapshot] == HEAD.value.removeprefix("forgejo:")
    with transport.hold(REPO, snapshot):
        pass
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.posts == 1 and len(transport.pulls) == 1


def test_pr_snapshot_mutation_during_post_is_authority_failure(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    transport.tamper_snapshot_on_post = True
    with pytest.raises(ForgeConflict, match="head"):
        forge.apply(effect)
    assert transport.posts == 1


def test_pr_valid_but_wrong_response_head_is_authority_failure(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    if isinstance(forge, ForgejoForge):
        original = transport.request

        def wrong_head(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
            status, payload = original(method, path, body)
            if method == "POST" and status == 201 and isinstance(payload, dict):
                head = cast(dict[str, object], payload).get("head")
                assert isinstance(head, dict)
                cast(dict[str, object], head)["sha"] = "c" * 40
            return status, cast(object, payload)

        with patch.object(transport, "request", side_effect=wrong_head):
            with pytest.raises(ForgeConflict, match="authorized head"):
                forge.apply(effect)
    else:
        transport.tamper_snapshot_on_post = True
        with pytest.raises(ForgeConflict, match="authorized head"):
            forge.apply(effect)
    assert transport.posts == 1


def test_pr_base_movement_during_post_is_provenance(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    transport.move_base_on_post = True
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED
    assert receipt.observed_base == Reference(f"forgejo:{MOVED_BASE}")
    assert receipt.reference is not None
    assert forge.reconcile(effect, receipt.reference).observed_base == receipt.observed_base


def test_pr_requires_server_enforced_isolation_before_write() -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True)
    assert "pr" not in forge.capabilities()
    with pytest.raises(UnsupportedForge, match="pr unsupported"):
        forge.apply(operation("pr"))
    assert transport.posts == transport.pushes == 0


def test_unavailable_reads_and_out_of_binding_observations(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, _ = boundary
    foreign = Reference("forgejo:other/project")
    for observe in (
        lambda: forge.identity(foreign),
        lambda: forge.branch(foreign, "feature"),
        lambda: forge.change(foreign, Reference(f"{foreign.value}#1")),
        lambda: forge.checks(foreign, HEAD),
        lambda: forge.changes(foreign),
    ):
        with pytest.raises(ForgeConflict):
            observe()
    transport.supports_reads = False
    for observe in (
        lambda: forge.identity(REPO),
        lambda: forge.branch(REPO, "feature"),
        lambda: forge.change(REPO, Reference(f"{REPO.value}#1")),
        lambda: forge.checks(REPO, HEAD),
        lambda: forge.changes(REPO),
    ):
        with pytest.raises(UnsupportedForge):
            observe()


@pytest.mark.parametrize("field", ["operation_id", "effect_key", "request_digest", "title", "body"])
def test_pr_oversized_input_rejected_before_marker_or_io(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], field: str
) -> None:
    forge, transport, permitted = boundary
    transport.max_bytes = 256
    effect = operation("pr")
    if field in {"title", "body"}:
        effect = replace(effect, **{field: "x" * 100_000})
    else:
        key = effect.operation
        effect = replace(effect, operation=replace(key, **{field: "x" * 100_000}))
    permitted.append(effect)
    with patch("creatidy_kernel.adapters.forge_refs.effect_marker") as marker:
        assert forge.apply(effect).status is EffectStatus.REJECTED
        marker.assert_not_called()
    assert transport.posts == transport.pushes == 0


def test_stale_rejected_uncertain_and_inaccessible(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    transport.branches["develop"] = MOVED_BASE
    moved = forge.apply(effect)
    assert moved.status is EffectStatus.ACCEPTED
    assert moved.observed_base == Reference(f"forgejo:{MOVED_BASE}")
    assert transport.posts == 1
    transport.branches["develop"] = BASE.value.removeprefix("forgejo:")
    transport.domain_rejection = True
    assert forge.apply(effect).status is EffectStatus.REJECTED
    transport.domain_rejection = False
    transport.lost_reply = True
    assert forge.apply(effect).status is EffectStatus.UNKNOWN
    assert transport.posts == 3
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.posts == 3
    transport.forbidden = True
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN


def test_unknown_absence_never_proves_safe_pr_retry(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.posts == 0


def test_direct_absence_is_explicit_and_never_retries_uncertain_pr(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    assert forge.change(REPO, Reference(f"{REPO.value}#99")).presence is Presence.UNKNOWN
    transport.direct_absence = True
    assert forge.branch(REPO, "missing").presence is Presence.ABSENT
    assert forge.change(REPO, Reference(f"{REPO.value}#99")).presence is Presence.ABSENT
    replay = replace(operation("pr"), delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.posts == 0
    transport.forbidden = True
    assert forge.change(REPO, Reference(f"{REPO.value}#99")).presence is Presence.INACCESSIBLE


def test_repository_identity_absence_classes(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, _ = boundary
    transport.repository_missing = True
    assert forge.identity(REPO).presence is Presence.UNKNOWN
    transport.direct_absence = True
    assert forge.identity(REPO).presence is Presence.ABSENT
    transport.forbidden = True
    assert forge.identity(REPO).presence is Presence.INACCESSIBLE


def test_forgejo_authentication_failure_is_inaccessible() -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True)
    with patch.object(transport, "request", return_value=(401, {"message": "unauthorized"})):
        assert forge.identity(REPO).presence is Presence.INACCESSIBLE
        assert forge.branch(REPO, "feature").presence is Presence.INACCESSIBLE
        assert forge.change(REPO, Reference(f"{REPO.value}#1")).presence is Presence.INACCESSIBLE


def test_missing_pr_number_and_invalid_check_ids_fail_closed(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, _ = boundary
    transport.pulls.append({"head": {}, "base": {}})
    assert forge.change(REPO, Reference(f"{REPO.value}#1")).presence is Presence.UNKNOWN
    assert not forge.changes(REPO).complete
    for invalid in (True, False, 0, -1, 10**18, "1"):
        transport.statuses[:] = [{"id": invalid, "context": "unit", "status": "success"}]
        assert forge.checks(REPO, HEAD).items == ()
        assert not forge.checks(REPO, HEAD).complete


@pytest.mark.parametrize(
    "status,expected",
    [
        (403, EffectStatus.REJECTED),
        (409, EffectStatus.REJECTED),
        (413, EffectStatus.REJECTED),
        (422, EffectStatus.REJECTED),
        (423, EffectStatus.REJECTED),
        (401, EffectStatus.UNKNOWN),
        (429, EffectStatus.UNKNOWN),
        (500, EffectStatus.UNKNOWN),
    ],
)
def test_pr_post_status_is_endpoint_specific(status: int, expected: EffectStatus) -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True, isolation=transport)
    original = transport.request

    def respond(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        if method == "POST":
            return status, {}
        return original(method, path, body)

    with patch.object(transport, "request", side_effect=respond):
        assert forge.apply(operation("pr")).status is expected
    assert transport.posts == 0


@pytest.mark.parametrize("invalid", ["team/pro..ject", "team/" + "x" * 256])
def test_finite_repository_codec_rejects_before_io(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], invalid: str
) -> None:
    forge, transport, _ = boundary
    with pytest.raises(ForgeConflict):
        forge.identity(Reference(f"forgejo:{invalid}"))
    assert transport.posts == transport.pushes == 0


@pytest.mark.parametrize("suffix", ["", "0", "01", "-1", "abc", "1x", "١", "9" * 5000])
def test_malformed_change_id_rejected(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], suffix: str
) -> None:
    forge, _, _ = boundary
    with pytest.raises(ForgeConflict):
        forge.change(REPO, Reference(f"{REPO.value}#{suffix}"))


@pytest.mark.parametrize(
    "reference",
    [
        "forgejo:other/project#1",
        "forgejo:team/project#01",
        "forgejo:team/project#abc",
        "forgejo:team/project#" + "9" * 5000,
    ],
)
def test_pr_reconciliation_rejects_foreign_or_malformed_reference(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], reference: str
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    assert forge.apply(effect).status is EffectStatus.ACCEPTED
    with pytest.raises(ForgeConflict):
        forge.reconcile(effect, Reference(reference))
    assert transport.posts == 1


def test_pr_reconciliation_does_not_adopt_wrong_known_reference(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    receipt = forge.apply(effect)
    assert receipt.reference is not None
    transport.pulls.append({**transport.pulls[0], "number": 2, "title": "Other"})
    assert forge.reconcile(effect, Reference(f"{REPO.value}#2")).status is EffectStatus.UNKNOWN
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED


def test_canonical_marker_does_not_confuse_colon_keys(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    first = replace(operation("pr"), operation=OperationKey("op-one", "a:b", "c"))
    other = replace(operation("pr"), operation=OperationKey("op-two", "a", "b:c"), delivery_attempts=2)
    assert effect_marker(first.operation) != effect_marker(other.operation)
    permitted.extend((first, other))
    assert forge.apply(first).status is EffectStatus.ACCEPTED
    assert forge.apply(other).status is EffectStatus.UNKNOWN
    assert transport.posts == 1


@pytest.mark.parametrize("side", ["head", "base"])
@pytest.mark.parametrize("identity", [None, "other/project"])
def test_pr_identity_fails_closed_on_post_and_reconcile(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], side: str, identity: str | None
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    original = transport.request

    def corrupted(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        status, payload = original(method, path, body)
        if status in {200, 201} and "/pulls" in path and isinstance(payload, dict):
            data = cast(dict[str, object], payload)
            subject = data.get(side)
            assert isinstance(subject, dict)
            cast(dict[str, object], subject)["repo"] = {"full_name": identity} if identity is not None else None
        return status, cast(object, payload)

    if isinstance(forge, ForgejoForge):
        with patch.object(transport, "request", side_effect=corrupted):
            assert forge.apply(effect).status is EffectStatus.UNKNOWN
            assert forge.reconcile(effect, Reference(f"{REPO.value}#1")).status is EffectStatus.UNKNOWN
    else:
        assert forge.apply(effect).status is EffectStatus.ACCEPTED
        subject = transport.pulls[0][side]
        assert isinstance(subject, dict)
        subject["repo"] = {"full_name": identity} if identity is not None else None
        assert forge.change(REPO, Reference(f"{REPO.value}#1")).presence is Presence.UNKNOWN
        assert forge.reconcile(effect, Reference(f"{REPO.value}#1")).status is EffectStatus.UNKNOWN


def test_branch_source_stale_and_pr_key_conflict(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    creation = replace(operation("branch"), branch="new")
    permitted.append(creation)
    transport.branches["develop"] = MOVED_BASE
    assert forge.apply(creation).status is EffectStatus.STALE
    assert "new" not in transport.branches
    transport.branches["develop"] = BASE.value.removeprefix("forgejo:")
    effect = operation("pr")
    permitted.append(effect)
    assert forge.apply(effect).status is EffectStatus.ACCEPTED
    transport.pulls[0]["title"] = "Changed remotely"
    assert forge.reconcile(effect, Reference(f"{REPO.value}#1")).status is EffectStatus.UNKNOWN


def test_known_reference_ignores_later_pages(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, permitted = boundary
    for number in (1, 2):
        transport.pulls.append(
            {
                "number": number,
                "head": {
                    "sha": HEAD.value.removeprefix("forgejo:"),
                    "ref": "feature",
                    "repo": {"full_name": "team/project"},
                },
                "base": {
                    "sha": BASE.value.removeprefix("forgejo:"),
                    "ref": "develop",
                    "repo": {"full_name": "team/project"},
                },
                "title": "Unrelated",
                "body": "not this effect",
            }
        )
    effect = operation("pr")
    permitted.append(effect)
    accepted = forge.apply(effect)
    assert accepted.status is EffectStatus.ACCEPTED and accepted.reference is not None
    assert forge.changes(REPO).next_cursor == "2"
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    receipt = forge.apply(replay)
    assert receipt.status is EffectStatus.UNKNOWN
    assert forge.reconcile(effect, accepted.reference).status is EffectStatus.ACCEPTED
    assert transport.posts == 1


@pytest.mark.parametrize("conflict_first", [False, True])
@pytest.mark.parametrize("across_page", [False, True])
def test_concurrent_marker_insertion_cannot_be_adopted_without_reference(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], conflict_first: bool, across_page: bool
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    accepted = forge.apply(effect)
    assert accepted.status is EffectStatus.ACCEPTED and accepted.reference is not None
    conflict = {
        **transport.pulls[0],
        "number": 2,
        "title": "Conflicting effect",
    }
    if across_page:
        unrelated = {**transport.pulls[0], "number": 3, "body": "unrelated"}
        transport.pulls.append(unrelated)
    if conflict_first:
        transport.pulls.insert(0, conflict)
    else:
        transport.pulls.append(conflict)
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    assert forge.reconcile(effect, accepted.reference).status is EffectStatus.ACCEPTED


def test_known_reference_does_not_depend_on_list_size() -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = FakeForge(transport, lambda _effect: True)
    effect = operation("pr")
    assert forge.apply(effect).status is EffectStatus.ACCEPTED
    transport.pulls.append({**transport.pulls[0], "number": 2})
    assert forge.reconcile(effect, Reference(f"{REPO.value}#1")).status is EffectStatus.ACCEPTED


def test_pr_local_request_refusal_is_rejected_without_post() -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True, isolation=transport)
    effect = operation("pr")
    original = transport.request

    def refused(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        if method == "POST":
            raise LocalRequestRefusal("request exceeds safe limit")
        return original(method, path, body)

    with patch.object(transport, "request", side_effect=refused):
        assert forge.apply(effect).status is EffectStatus.REJECTED
    assert transport.posts == 0


def test_server_cap_does_not_hide_later_pr() -> None:
    transport = SyntheticForgeTransport(REPO, page_size=1)
    forge = ForgejoForge(transport, transport, lambda _effect: True, page_size=30, isolation=transport)
    effect = operation("pr")
    for number in (1, 2):
        transport.pulls.append(
            {
                "number": number,
                "head": {
                    "sha": HEAD.value.removeprefix("forgejo:"),
                    "ref": "feature",
                    "repo": {"full_name": "team/project"},
                },
                "base": {
                    "sha": BASE.value.removeprefix("forgejo:"),
                    "ref": "develop",
                    "repo": {"full_name": "team/project"},
                },
                "title": "Other",
                "body": "other",
            }
        )
    first = forge.changes(REPO)
    assert len(first.items) == 1 and not first.complete and first.next_cursor == "2"
    assert forge.apply(effect).status is EffectStatus.ACCEPTED
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    assert forge.reconcile(effect, Reference(f"{REPO.value}#3")).status is EffectStatus.ACCEPTED
    assert forge.changes(REPO, "4").complete


def test_malformed_pages_do_not_supply_reconciliation_evidence() -> None:
    transport = SyntheticForgeTransport(REPO, page_size=1)
    forge = ForgejoForge(transport, transport, lambda _effect: True, isolation=transport)
    effect = operation("pr")
    transport.pulls.append(
        {
            "number": 1,
            "head": {
                "sha": HEAD.value.removeprefix("forgejo:"),
                "ref": "feature",
                "repo": {"full_name": "team/project"},
            },
            "base": {
                "sha": BASE.value.removeprefix("forgejo:"),
                "ref": "develop",
                "repo": {"full_name": "team/project"},
            },
            "title": "Other",
            "body": "other",
        }
    )
    assert forge.apply(effect).status is EffectStatus.ACCEPTED
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    assert forge.changes(REPO, "invalid").complete is False
    assert forge.changes(REPO, "0").complete is False
    assert forge.checks(REPO, HEAD, "-1").complete is False
    assert forge.reconcile(effect, Reference(f"{REPO.value}#2")).status is EffectStatus.ACCEPTED
    assert transport.posts == 1


def test_known_reference_reads_only_exact_pr_and_branches() -> None:
    transport = SyntheticForgeTransport(REPO)
    effect = operation("pr")
    creator = ForgejoForge(transport, transport, lambda _effect: True, isolation=transport)
    receipt = creator.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None
    seen: list[str] = []
    original = transport.request

    def counting(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        seen.append(path)
        return original(method, path, body)

    with patch.object(transport, "request", side_effect=counting):
        assert creator.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED
    assert seen == [
        "/repos/team/project/pulls/1",
        f"/repos/team/project/branches/{pr_payload(effect, transport.max_bytes)[0].replace('/', '%2F')}",
    ]


def test_forgejo_read_failure_and_wrong_change_are_unknown() -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True, isolation=transport)
    with patch.object(transport, "request", side_effect=OSError("lost read reply")):
        assert forge.identity(REPO).presence is Presence.UNKNOWN
        assert forge.branch(REPO, "develop").presence is Presence.UNKNOWN
        assert forge.change(REPO, Reference("forgejo:team/project#1")).presence is Presence.UNKNOWN
        assert not forge.checks(REPO, HEAD).complete
        assert not forge.changes(REPO).complete
        assert forge.reconcile(operation("pr")).status is EffectStatus.UNKNOWN
    transport.pulls.append(
        {
            "number": 2,
            "head": {"sha": HEAD.value.removeprefix("forgejo:"), "ref": "feature"},
            "base": {"sha": BASE.value.removeprefix("forgejo:"), "ref": "develop"},
        }
    )
    with patch.object(transport, "request", return_value=(200, transport.pulls[0])):
        assert forge.change(REPO, Reference("forgejo:team/project#1")).presence is Presence.UNKNOWN
