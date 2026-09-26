# SPDX-License-Identifier: Apache-2.0
"""Synthetic authority cases; no claim of authentication or hostile-code isolation."""

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from creatidy_kernel.adapters.fake_authority import FakeAuthorityBroker
from creatidy_kernel.core.authority import AuthorityDenied, AuthorityGrant, OperationIntent, Principal
from creatidy_kernel.core.domain import AttemptSpec


def attempt() -> AttemptSpec:
    return AttemptSpec("attempt-1", "program-1", "unit-1", 1, "approved-spec")


def grant(root: Path, spec: AttemptSpec, *, single_use: bool = False) -> AuthorityGrant:
    return AuthorityGrant(
        "grant-1",
        "owner-1",
        spec.digest,
        spec.program_id,
        spec.spec_digest,
        "repo-1",
        root,
        frozenset({root / "work"}),
        frozenset({"api.example.test"}),
        frozenset({"read", "write", "publish_candidate"}),
        100,
        single_use,
    )


def intent(**changes: object) -> OperationIntent:
    return replace(
        OperationIntent("op-1", "effect-1", "input-digest-1", "write", "repo-1", Path("work/output")),
        **changes,
    )


@pytest.fixture
def setup(tmp_path: Path) -> tuple[FakeAuthorityBroker, AttemptSpec, AuthorityGrant]:
    (tmp_path / "work").mkdir()
    spec = attempt()
    broker = FakeAuthorityBroker()
    bounded = broker.issue(Principal("owner-1", "owner"), grant(tmp_path, spec, single_use=True))
    return broker, spec, bounded


def test_single_use_exact_intent_replay_and_changed_input(
    setup: tuple[FakeAuthorityBroker, AttemptSpec, AuthorityGrant],
) -> None:
    broker, spec, bounded = setup
    worker = Principal(spec.attempt_id, "worker")
    original = intent()
    assert broker.check(worker, bounded.grant_id, spec, original, 10) == original
    assert broker.check(worker, bounded.grant_id, spec, original, 11) == original
    for changed in (
        intent(request_digest="different"),
        intent(path=Path("work/other")),
        intent(effect_key="other-effect"),
        intent(repository="other-repo"),
    ):
        with pytest.raises(AuthorityDenied):
            broker.check(worker, bounded.grant_id, spec, changed, 12)
    with pytest.raises(AuthorityDenied, match="consumed"):
        broker.check(worker, bounded.grant_id, spec, intent(operation_id="op-2", effect_key="effect-2"), 12)


def test_owner_and_worker_authority_separated(setup: tuple[FakeAuthorityBroker, AttemptSpec, AuthorityGrant]) -> None:
    broker, spec, bounded = setup
    worker = Principal(spec.attempt_id, "worker")
    owner = Principal("owner-1", "owner")
    with pytest.raises(AuthorityDenied):
        broker.issue(worker, replace(bounded, grant_id="second"))
    with pytest.raises(AuthorityDenied):
        broker.revoke(worker, bounded.grant_id)
    with pytest.raises(AuthorityDenied):
        broker.check(owner, bounded.grant_id, spec, intent(), 1)
    with pytest.raises(AuthorityDenied):
        broker.check(Principal("other-attempt", "worker"), bounded.grant_id, spec, intent(), 1)
    for forbidden in ("grant", "accept", "change_gate", "control_state", "credential"):
        with pytest.raises(AuthorityDenied):
            replace(bounded, operations=frozenset({forbidden}))
    with pytest.raises(FrozenInstanceError):
        bounded.repository = "other"  # type: ignore[misc]


def test_expiry_revocation_and_changed_spec(setup: tuple[FakeAuthorityBroker, AttemptSpec, AuthorityGrant]) -> None:
    broker, spec, bounded = setup
    worker = Principal(spec.attempt_id, "worker")
    with pytest.raises(AuthorityDenied, match="expired"):
        broker.check(worker, bounded.grant_id, spec, intent(), 100)
    with pytest.raises(AuthorityDenied, match="spec changed"):
        broker.check(
            worker, bounded.grant_id, AttemptSpec("attempt-1", "program-1", "unit-1", 2, "new-spec"), intent(), 1
        )
    broker.check(worker, bounded.grant_id, spec, intent(), 1)
    assert broker.revoke(Principal("owner-1", "owner"), bounded.grant_id).revoked
    with pytest.raises(AuthorityDenied, match="revoked"):
        broker.check(worker, bounded.grant_id, spec, intent(), 2)


def test_scope_path_symlink_and_network(
    setup: tuple[FakeAuthorityBroker, AttemptSpec, AuthorityGrant], tmp_path: Path
) -> None:
    broker, spec, bounded = setup
    worker = Principal(spec.attempt_id, "worker")
    for changed in (
        intent(path=Path("../outside")),
        intent(path=tmp_path.parent / "outside"),
        intent(path=Path("other/output")),
        intent(path=None),
        intent(destination="other.example.test"),
        intent(operation="execute"),
        intent(repository="different"),
    ):
        with pytest.raises(AuthorityDenied):
            broker.check(worker, bounded.grant_id, spec, changed, 1)
    (tmp_path / "work" / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(AuthorityDenied, match="path"):
        broker.check(worker, bounded.grant_id, spec, intent(path=Path("work/escape/file")), 1)
    assert broker.check(worker, bounded.grant_id, spec, intent(destination="api.example.test"), 1)


def test_effect_replay_conflict_and_invalid_grant(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    spec = attempt()
    broker = FakeAuthorityBroker()
    bounded = broker.issue(Principal("owner-1", "owner"), grant(tmp_path, spec))
    worker = Principal(spec.attempt_id, "worker")
    broker.check(worker, bounded.grant_id, spec, intent(), 1)
    with pytest.raises(AuthorityDenied, match="effect key"):
        broker.check(worker, bounded.grant_id, spec, intent(operation_id="other-op"), 2)
    with pytest.raises(AuthorityDenied, match="already exists"):
        broker.issue(Principal("owner-1", "owner"), bounded)
    with pytest.raises(AuthorityDenied):
        replace(bounded, paths=frozenset({tmp_path.parent / "outside"}))


def test_symlinked_allowed_path_cannot_escape_grant_root(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-grant"
    outside.mkdir()
    (tmp_path / "work" / "link").symlink_to(outside, target_is_directory=True)
    spec = attempt()
    broker = FakeAuthorityBroker()
    bounded = replace(grant(tmp_path, spec), paths=frozenset({tmp_path / "work" / "link"}))
    broker.issue(Principal("owner-1", "owner"), bounded)
    with pytest.raises(AuthorityDenied, match="path"):
        broker.check(
            Principal(spec.attempt_id, "worker"),
            bounded.grant_id,
            spec,
            intent(path=Path("work/link/file")),
            1,
        )
