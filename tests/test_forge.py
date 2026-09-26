# SPDX-License-Identifier: Apache-2.0
"""Offline conformance for the fake and Forgejo reference adapter."""

from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.fake_forge import FakeForge, SyntheticForgeTransport
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
        else ForgejoForge(transport, transport, authorize, page_size=2)
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
        base_revision=BASE if action in {"pr", "branch"} else None,
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
    assert forge.checks(REPO, Reference("forgejo:other")).items == ()
    transport.forbidden = True
    assert forge.identity(REPO).presence is Presence.INACCESSIBLE
    assert not forge.changes(REPO).complete


@pytest.mark.parametrize("cursor", ["invalid", "0", "01", "-1", "١"])
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


def test_pr_artifact_not_merge_and_replay(boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]]) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    receipt = forge.apply(effect)
    assert receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None
    assert forge.change(REPO, receipt.reference).head == HEAD
    assert len(transport.pulls) == 1 and transport.posts == 1
    assert "merge" not in forge.capabilities()
    retry = replace(effect, delivery_attempts=2)
    permitted.append(retry)
    assert forge.apply(retry).status is EffectStatus.UNKNOWN
    assert forge.reconcile(effect, receipt.reference).status is EffectStatus.ACCEPTED
    assert transport.posts == 1


def test_stale_rejected_uncertain_and_inaccessible(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]],
) -> None:
    forge, transport, permitted = boundary
    effect = operation("pr")
    permitted.append(effect)
    transport.branches["develop"] = MOVED_BASE
    assert forge.apply(effect).status is EffectStatus.STALE
    assert transport.posts == 0
    transport.branches["develop"] = BASE.value.removeprefix("forgejo:")
    transport.domain_rejection = True
    assert forge.apply(effect).status is EffectStatus.REJECTED
    transport.domain_rejection = False
    transport.lost_reply = True
    assert forge.apply(effect).status is EffectStatus.UNKNOWN
    assert transport.posts == 2
    assert forge.reconcile(effect).status is EffectStatus.UNKNOWN
    replay = replace(effect, delivery_attempts=2)
    permitted.append(replay)
    assert forge.apply(replay).status is EffectStatus.UNKNOWN
    assert transport.posts == 2
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


@pytest.mark.parametrize("suffix", ["", "0", "01", "-1", "abc", "1x", "١"])
def test_malformed_change_id_rejected(
    boundary: tuple[Forge, SyntheticForgeTransport, list[Effect]], suffix: str
) -> None:
    forge, _, _ = boundary
    with pytest.raises(ForgeConflict):
        forge.change(REPO, Reference(f"{REPO.value}#{suffix}"))


@pytest.mark.parametrize(
    "reference", ["forgejo:other/project#1", "forgejo:team/project#01", "forgejo:team/project#abc"]
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
    forge = ForgejoForge(transport, transport, lambda _effect: True)
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
    assert seen == [
        "/repos/team/project/pulls/1",
        "/repos/team/project/branches/develop",
        "/repos/team/project/branches/feature",
    ]


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
