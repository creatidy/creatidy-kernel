# SPDX-License-Identifier: Apache-2.0
"""Offline conformance for independent fake and Forgejo adapters."""

from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.fake_forge import FakeForge, SyntheticForgeTransport
from creatidy_kernel.adapters.forge_refs import AGitPush, fresh_agit_topic, pr_payload
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import (
    CheckResult,
    Effect,
    EffectStatus,
    ForgeConflict,
    Observation,
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
TOPIC = "kernel-pr-" + "1" * 32


@pytest.fixture(params=["fake", "forgejo"])
def boundary(request: pytest.FixtureRequest) -> tuple[Forge, SyntheticForgeTransport, list[Effect]]:
    transport = SyntheticForgeTransport(REPO)
    permitted: list[Effect] = []
    authorize = permitted.__contains__
    forge: Forge = (
        FakeForge(transport, authorize)
        if request.param == "fake"
        else ForgejoForge(transport, transport, authorize, page_size=2)
    )
    return forge, transport, permitted


def operation(action: str = "pr", *, attempts: int = 1, topic: str = TOPIC, key: str | None = None) -> Effect:
    return Effect(
        OperationKey(f"op-{action}", key or f"effect-{action}", f"digest-{action}"),
        REPO,
        action,
        topic if action == "pr" else "feature",
        expected=HEAD if action == "push" else None,
        revision=NEXT_HEAD if action == "push" else HEAD,
        base_branch="develop" if action in {"pr", "branch"} else None,
        base_revision=BASE if action == "branch" else None,
        title="Patch" if action == "pr" else None,
        body="First line\nSecond line" if action == "pr" else None,
        fence=1,
        delivery_attempts=attempts,
    )


def test_observations_and_pagination(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, _ = boundary
    assert forge.capabilities() == frozenset(
        {"identity", "branch", "change", "checks", "conditional_push", "branch_create", "pr"}
    )
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
    assert len(first.items) == 2 and first.next_cursor == "2" and not first.complete
    assert first.items[0].check_result is CheckResult.PASSED
    assert first.items[0].revision == HEAD and first.items[0].check_context == "unit"
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


def test_conditional_branch_and_push_remain_bounded(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("push")
    with pytest.raises(ForgeConflict):
        forge.apply(effect)
    permitted.append(effect)
    assert forge.apply(effect).status is EffectStatus.ACCEPTED
    assert forge.apply(effect).status is EffectStatus.STALE
    assert transport.pushes == 1
    branch = replace(operation("branch"), branch="new")
    permitted.append(branch)
    transport.branches["develop"] = "d" * 40
    assert forge.apply(branch).status is EffectStatus.STALE
    transport.branches["develop"] = "b" * 40
    transport.lost_reply = True
    assert forge.apply(branch).status is EffectStatus.UNKNOWN
    replay = replace(branch, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.pushes == 2


def test_agit_creates_exact_sha_in_bound_repo_and_base(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    transport.branches["feature"] = "c" * 40
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED
    assert receipt.reference == Reference("forgejo:team/project#1")
    assert receipt.reference is not None
    assert receipt.observed_base == BASE
    assert forge.change(REPO, receipt.reference).head == HEAD
    assert transport.posts == transport.pushes == 1
    assert set(transport.branches) == {"develop", "feature"}
    assert transport.pulls[0]["topic"] == TOPIC
    assert transport.pulls[0]["head"] == {
        "sha": "a" * 40,
        "ref": "refs/pull/1/head",
        "repo": {"full_name": "team/project"},
    }
    assert effect_marker(effect.operation) in str(transport.pulls[0]["body"])
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED
    retry = replace(effect, delivery_attempts=2)
    permitted.append(retry)
    assert forge.apply(retry).status is EffectStatus.UNKNOWN
    assert transport.pushes == 1
    assert "merge" not in forge.capabilities()


def test_base_movement_is_provenance_not_authority(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    transport.move_base_on_push = True
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED
    assert receipt.observed_base == Reference("forgejo:" + "d" * 40)
    assert receipt.reference is not None
    assert forge.reconcile(effect, receipt.reference).observed_base == receipt.observed_base


@pytest.mark.parametrize("mode", ["lost", "ambiguous", "collision", "forbidden", "rejected"])
def test_lost_rejected_or_colliding_agit_never_blind_retries(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], mode: str
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    if mode == "lost":
        transport.lost_reply = True
    elif mode == "ambiguous":
        transport.ambiguous_receipt = True
    elif mode == "collision":
        transport.contend_topic = True
    elif mode == "forbidden":
        transport.forbidden = True
    else:
        transport.domain_rejection = True
    receipt = forge.apply(effect)
    assert receipt.status is (
        EffectStatus.UNKNOWN if mode in {"lost", "ambiguous", "forbidden"} else EffectStatus.REJECTED
    )
    before = transport.pushes
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.pushes == before
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN


def test_same_topic_even_for_same_operation_is_not_new_pr(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    first = operation()
    permitted.append(first)
    assert forge.apply(first).status is EffectStatus.ACCEPTED
    second = replace(first, operation=OperationKey("other", "other", "other"))
    permitted.append(second)
    assert forge.apply(second).status is EffectStatus.REJECTED
    assert forge.apply(first).status is EffectStatus.REJECTED
    assert len(transport.pulls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", Reference("forgejo:bad")),
        ("revision", Reference("github:" + "a" * 40)),
        ("base_revision", BASE),
        ("branch", "feature"),
        ("branch", "kernel-pr-" + "z" * 32),
    ],
)
def test_invalid_agit_input_prevents_effect(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], field: str, value: object
) -> None:
    forge, transport, permitted = boundary
    if field == "base_revision":
        with pytest.raises(ForgeConflict, match="observation only"):
            replace(operation(), **{field: value})
        return
    effect = replace(operation(), **{field: value})
    permitted.append(effect)
    with pytest.raises(ForgeConflict):
        forge.apply(effect)
    assert transport.pushes == 0


@pytest.mark.parametrize("field", ["operation_id", "effect_key", "request_digest", "title", "body"])
def test_oversized_pr_refused_before_marker_or_io(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], field: str
) -> None:
    forge, transport, permitted = boundary
    transport.max_bytes = 256
    effect = operation()
    if field in {"title", "body"}:
        effect = replace(effect, **{field: "x" * 100_000})
    else:
        effect = replace(effect, operation=replace(effect.operation, **{field: "x" * 100_000}))
    permitted.append(effect)
    with patch("creatidy_kernel.adapters.forge_refs.effect_marker") as marker:
        assert forge.apply(effect).status is EffectStatus.REJECTED
        marker.assert_not_called()
    assert transport.pushes == 0


@pytest.mark.parametrize("side", ["head", "base"])
@pytest.mark.parametrize("identity", [None, "other/project"])
def test_readback_repo_identity_required(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], side: str, identity: str | None
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    original = transport.request

    def corrupted(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        status, payload = original(method, path, body)
        if method == "GET" and "/pulls/" in path and isinstance(payload, dict):
            subject = cast(dict[str, object], payload).get(side)
            assert isinstance(subject, dict)
            subject["repo"] = {"full_name": identity} if identity is not None else None
        return status, cast(object, payload)

    if isinstance(forge, ForgejoForge):
        with patch.object(transport, "request", side_effect=corrupted):
            assert forge.apply(effect).status is EffectStatus.UNKNOWN
    else:
        assert forge.apply(effect).status is EffectStatus.ACCEPTED
        subject = transport.pulls[0][side]
        assert isinstance(subject, dict)
        subject["repo"] = {"full_name": identity} if identity is not None else None
        assert forge.reconcile(effect, Reference(f"{REPO.value}#1")).status is EffectStatus.UNKNOWN


@pytest.mark.parametrize("mutation", ["head", "base", "head_ref", "base_ref", "number", "title", "body"])
def test_readback_mismatch_never_acknowledges(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], mutation: str
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    accepted = forge.apply(effect)
    assert accepted.reference is not None
    pull = transport.pulls[0]
    if mutation in {"head", "base"}:
        subject = pull[mutation]
        assert isinstance(subject, dict)
        subject["sha"] = "c" * 40 if mutation == "head" else "bad"
    elif mutation.endswith("_ref"):
        subject = pull[mutation.split("_")[0]]
        assert isinstance(subject, dict)
        subject["ref"] = "other"
    else:
        pull[mutation] = 2 if mutation == "number" else "other"
    if mutation in {"head_ref", "base_ref"}:
        with pytest.raises(ForgeConflict):
            forge.reconcile(effect, accepted.reference)
    elif mutation == "head":
        assert forge.reconcile(effect, accepted.reference).status is EffectStatus.STALE
    else:
        assert forge.reconcile(effect, accepted.reference).status is not EffectStatus.ACCEPTED


def test_later_head_movement_invalidates_exact_head_evidence(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    receipt = forge.apply(effect)
    assert receipt.reference is not None
    head = transport.pulls[0]["head"]
    assert isinstance(head, dict)
    head["sha"] = "c" * 40
    assert forge.change(REPO, receipt.reference).head == NEXT_HEAD
    stale = forge.reconcile(effect, receipt.reference)
    assert stale.status is EffectStatus.STALE and stale.reference == receipt.reference


def test_head_movement_before_creation_readback_never_acknowledges(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    transport.tamper_head_on_push = True
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.STALE and receipt.reference == Reference(f"{REPO.value}#1")
    assert transport.pushes == 1
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN


def test_creation_receipt_retains_recovery_handle_when_readback_is_unavailable(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    if isinstance(forge, ForgejoForge):
        original = transport.request

        def unavailable(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
            if method == "GET" and "/pulls/" in path:
                return 503, {}
            return original(method, path, body)

        with patch.object(transport, "request", side_effect=unavailable):
            receipt = forge.apply(effect)
    else:
        with patch.object(forge, "change", return_value=Observation(Presence.UNKNOWN)):
            receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.UNKNOWN
    assert receipt.reference == Reference(f"{REPO.value}#1")
    assert transport.pushes == 1
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED


@pytest.mark.parametrize("title", ["first\nsecond", "embedded\x00null", "first\rsecond"])
def test_bounded_push_options_and_invalid_title_fail_before_dispatch(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], title: str
) -> None:
    forge, transport, permitted = boundary
    oversized = replace(operation(), body="x" * 30_000)
    malformed = replace(operation(), title=title)
    permitted.extend((oversized, malformed))
    assert forge.apply(oversized).status is EffectStatus.REJECTED
    with pytest.raises(ForgeConflict, match="one line"):
        forge.apply(malformed)
    assert transport.pushes == 0


def test_unknown_without_durable_reference_and_known_reference_only_reads_exact_pr(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    permitted.append(effect)
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    receipt = forge.apply(effect)
    assert receipt.reference is not None
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED
    with pytest.raises(ForgeConflict):
        forge.reconcile(effect, Reference("forgejo:other/repo#1"))
    assert transport.pushes == 1


def test_binding_and_capability_fail_closed(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, permitted = boundary
    effect = operation()
    for attribute in ("supports_agit", "supports_pr", "supports_reads"):
        setattr(transport, attribute, False)
        permitted.append(effect)
        assert "pr" not in forge.capabilities()
        with pytest.raises(UnsupportedForge):
            forge.apply(effect)
        setattr(transport, attribute, True)
    with pytest.raises(ForgeConflict):
        forge.identity(Reference("forgejo:other/project"))
    assert transport.pushes == 0


def test_forgejo_rejects_unvalidated_git_receipt_even_with_matching_pr() -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True)
    effect = operation()
    with patch.object(transport, "create_agit_pr", return_value=AGitPush(EffectStatus.UNKNOWN)):
        assert forge.apply(effect).status is EffectStatus.UNKNOWN
    assert transport.pushes == 0


def test_marker_distinguishes_colon_keys() -> None:
    first = OperationKey("one", "a:b", "c")
    other = OperationKey("two", "a", "b:c")
    assert effect_marker(first) != effect_marker(other)
    assert pr_payload(replace(operation(), operation=first), 1_000_000)[0] == TOPIC


def test_new_topic_is_random_collision_resistant_and_not_reused() -> None:
    topics = {fresh_agit_topic() for _ in range(32)}
    assert len(topics) == 32
    for topic in topics:
        assert len(topic) == len(TOPIC)
        assert topic.startswith("kernel-pr-")
        int(topic.removeprefix("kernel-pr-"), 16)


def test_completed_agit_topic_is_not_reused_for_another_operation(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    first = operation()
    permitted.append(first)
    assert forge.apply(first).status is EffectStatus.ACCEPTED
    transport.pulls.clear()  # A closed PR is no longer an open-topic match at the forge.
    second = replace(first, operation=OperationKey("new-op", "new-effect", "new-digest"))
    permitted.append(second)
    assert forge.apply(second).status is EffectStatus.REJECTED
    assert transport.pushes == 1


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
    "attribute,missing",
    [
        ("supports_pr", {"pr"}),
        ("supports_conditional_push", {"branch_create", "conditional_push"}),
        ("supports_agit", {"pr"}),
    ],
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
    head = transport.pulls[0]["head"]
    assert isinstance(head, dict)
    transport.pulls.append(
        {**transport.pulls[0], "number": 2, "head": {**head, "ref": "refs/pull/2/head"}, "title": "Other"}
    )
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


def test_server_cap_does_not_hide_later_pr() -> None:
    transport = SyntheticForgeTransport(REPO, page_size=1)
    forge = ForgejoForge(transport, transport, lambda _effect: True, page_size=30)
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
    forge = ForgejoForge(transport, transport, lambda _effect: True)
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
    creator = ForgejoForge(transport, transport, lambda _effect: True)
    receipt = creator.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None
    seen: list[str] = []
    original = transport.request

    def counting(method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        seen.append(path)
        return original(method, path, body)

    with patch.object(transport, "request", side_effect=counting):
        assert creator.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED
    assert seen == ["/repos/team/project/pulls/1"]


def test_agit_never_reads_or_writes_an_ordinary_topic_branch(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    original = forge.branch

    def observed(repository: Reference, branch: str) -> Observation:
        assert branch == "develop"
        return original(repository, branch)

    with patch.object(forge, "branch", side_effect=observed):
        assert forge.apply(effect).status is EffectStatus.ACCEPTED
    assert TOPIC not in transport.branches
    assert transport.pushes == 1


def test_forgejo_read_failure_and_wrong_change_are_unknown() -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True)
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


def test_unknown_absence_never_proves_safe_pr_retry(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.posts == 0


def test_pr_base_revision_cannot_be_part_of_effect_authority() -> None:
    with pytest.raises(ForgeConflict, match="observation only"):
        replace(operation("pr"), base_revision=BASE)


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
    assert forge.apply(effect).status is EffectStatus.REJECTED
    assert transport.posts == 1
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.posts == 1
    transport.forbidden = True
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN


@pytest.mark.parametrize("side", ["head", "base"])
def test_malformed_immediate_pr_payload_is_unknown_in_both_adapters(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], side: str
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    original = transport.create_agit_pr

    def malformed(
        repository: Reference, base: str, topic: str, revision: Reference, title: str, description: str
    ) -> AGitPush:
        result = original(repository, base, topic, revision, title, description)
        subject = transport.pulls[0][side]
        assert isinstance(subject, dict)
        subject["sha"] = "invalid"
        return result

    with patch.object(transport, "create_agit_pr", side_effect=malformed):
        assert forge.apply(effect).status is EffectStatus.UNKNOWN
    assert transport.posts == 1


@pytest.mark.parametrize("number", [0, True, -1, 10**18, "1"])
def test_invalid_pr_number_never_supplies_receipt(number: object) -> None:
    transport = SyntheticForgeTransport(REPO)
    forge = ForgejoForge(transport, transport, lambda _effect: True)
    original = transport.create_agit_pr

    def invalid(
        repository: Reference, base: str, topic: str, revision: Reference, title: str, description: str
    ) -> AGitPush:
        result = original(repository, base, topic, revision, title, description)
        transport.pulls[0]["number"] = number
        return result

    with patch.object(transport, "create_agit_pr", side_effect=invalid):
        assert forge.apply(operation("pr")).status is EffectStatus.UNKNOWN
    assert transport.posts == 1
