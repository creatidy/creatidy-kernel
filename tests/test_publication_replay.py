# SPDX-License-Identifier: Apache-2.0
"""Original owner authorization is evidence, not repeatable live authority."""

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_clock_transaction_boundaries import CommitFailure, connection
from test_codex_stdio import FAKE
from test_ownership_composition import finish_and_accept
from test_sqlite_store import OWNER, WORKER, spec
from test_work_budget import Clock
from test_work_ownership import claim, client, workspace

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.sqlite_store import IdempotencyConflict, SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, StartAttempt
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict, ResourceScope, WorkClaim
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.ownership import WorkOwnership

pytest_plugins = ["test_sqlite_store", "test_clock_transaction_boundaries"]


def accepted(store: SQLiteProgramStore, tree: LocalOwnedWorkspace) -> tuple[WorkClaim, WorkOwnership]:
    program = store.create(spec(), "create")
    basic = WorkBudgetPolicy.basic(3, 3, verifications=6, retained_bytes=16384, auxiliary=3)
    budget = replace(basic, counts=tuple((kind, 2 if kind is WorkKind.TOOL else limit) for kind, limit in basic.counts))
    store.bind_work_budget(program.program_id, "project", budget, budget, 110)
    store.admit(program.program_id, "activate", ActivateProgram(program.revision, OWNER))
    worker = client(store, tree)
    original = claim(store, worker, tree)
    program = store.load("program-1")
    store.admit("program-1", "start:a1", StartAttempt(program.revision, WORKER, "a1"))
    worker.quiesce(original, "client:exact-task-quiescent")
    worker.submit(original)
    finish_and_accept(store, "program-1", "a1")
    return original, client(store, tree, owner=True)


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize(
    "state", ["pending", "accepted", "edited", "alias", "expired", "revoked", "released", "retired", "other-effect"]
)
def test_actual_owner_authorization_replay_precedes_capture_and_live_authority(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reopen: bool,
    state: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    samples: list[BootClockReading | None] = []

    def read_clock() -> BootClockReading | None:
        samples.append(clock.reading)
        return clock()

    tree = workspace(sqlite_tmp_path, "one")
    store = SQLiteProgramStore(path, boot_clock=read_clock)
    try:
        original, owner = accepted(store, tree)
        request: dict[str, object] = {"accepted": "accepted:a1"}
        authorized = owner.authorize_publication(original, "publish", "publish-effect", request)
        if state in {"accepted", "released", "retired", "other-effect"}:
            fence = owner.dispatch(original, "publish", lease_seconds=1)
            store.record_transport("publish", fence, True)
            store.observe("publish", fence, "publish:accepted", "accepted", reference="synthetic:publication")
        if state in {"released", "retired"}:
            store.record_work_settlement("publish", "trusted:publication-scope-closed", expected_fence=1)
            if state == "retired":
                owner.cleanup(original, evidence="owner:exact-empty-resource-retired")
            else:
                owner.release(original, evidence="owner:exact-scope-released")
        elif state == "edited":
            (tree.path / "manual-data").write_bytes(b"keep these changed bytes")
        elif state == "expired":
            clock.reading = BootClockReading(112, 13_000_000_000, "synthetic-boot-a")
        elif state == "revoked":
            store.revoke_work_budget("program-1", "owner:revoked")
        elif state == "other-effect":
            store.record_work_settlement("publish", "trusted:first-publication-closed", expected_fence=1)
            owner.authorize_publication(original, "other-effect", "other-effect", {"accepted": "another-effect"})
        if reopen:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=read_clock)
            tree = LocalOwnedWorkspace(tree.path.parent, "one")
            owner = client(store, tree, owner=True)
        if state == "alias":
            retained = tree.path.with_name("retained-original")
            tree.path.rename(retained)
            tree.path.symlink_to(retained, target_is_directory=True)

        def no_capture() -> tuple[str, bytes]:
            raise AssertionError("historical publication authorization must not capture current bytes")

        monkeypatch.setattr(tree, "capture", no_capture)
        before = store.load("program-1"), store.work_budget_status("program-1"), store.operations(), len(samples)
        result = owner.authorize_publication(original, "publish", "publish-effect", request)
        assert result == store.operation("publish")
        assert result.operation_id == authorized.operation_id
        assert (
            store.load("program-1"),
            store.work_budget_status("program-1"),
            store.operations(),
            len(samples),
        ) == before
        if state in {"alias", "expired", "revoked", "released", "retired", "other-effect"}:
            with pytest.raises((OwnershipConflict, BudgetRefused)):
                store.check_work_dispatch("publish")
    finally:
        store.close()


@pytest.mark.parametrize("changed", ["owner", "claimant", "generation", "resources", "operation", "effect", "request"])
def test_publication_replay_rejects_changed_explicit_identity_without_mutation(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        original, owner = accepted(store, tree)
        owner.authorize_publication(original, "publish", "publish-effect", {"accepted": "a1"})
        if changed == "owner":
            owner = WorkOwnership(
                store, Principal("different-owner", "owner"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
        supplied = original
        if changed == "claimant":
            supplied = replace(original, claimant="different-worker")
        elif changed == "generation":
            supplied = replace(original, generation=original.generation + 1)
        elif changed == "resources":
            supplied = replace(original, resources=(*original.resources, ResourceScope("session", "different-session")))
        before = store.operations(), store.work_budget_status("program-1")
        with pytest.raises((IdempotencyConflict, OwnershipConflict)):
            owner.authorize_publication(
                supplied,
                "different-operation" if changed == "operation" else "publish",
                "changed-effect" if changed == "effect" else "publish-effect",
                {"accepted": "changed" if changed == "request" else "a1"},
            )
        assert (store.operations(), store.work_budget_status("program-1")) == before


def test_generic_aux_intent_is_not_original_owner_authorization(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        original, owner = accepted(store, tree)
        store.budgeted_intent("program-1", "a1", WorkKind.AUXILIARY, "publish", "publish-effect", {"accepted": "a1"})
        before = store.operations(), store.work_budget_status("program-1")
        with pytest.raises((IdempotencyConflict, OwnershipConflict)):
            owner.authorize_publication(original, "publish", "publish-effect", {"accepted": "a1"})
        assert (store.operations(), store.work_budget_status("program-1")) == before


def test_lost_authorization_reply_replays_before_reinvoking_store_command(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        original, owner = accepted(store, tree)
        authorize = SQLiteProgramStore.authorize_claim_publication

        def lost_reply(
            current: SQLiteProgramStore,
            claim: WorkClaim,
            actor: str,
            subject: str,
            operation_id: str,
            effect_key: str,
            request: dict[str, object],
        ) -> object:
            authorize(current, claim, actor, subject, operation_id, effect_key, request)
            raise OSError("synthetic lost reply after committed authorization")

        monkeypatch.setattr(SQLiteProgramStore, "authorize_claim_publication", lost_reply)
        with pytest.raises(OSError, match="lost reply"):
            owner.authorize_publication(original, "publish", "publish-effect", {"accepted": "a1"})
        before = store.operations(), store.work_budget_status("program-1")
        assert owner.authorize_publication(
            original, "publish", "publish-effect", {"accepted": "a1"}
        ) == store.operation("publish")
        assert (store.operations(), store.work_budget_status("program-1")) == before


@pytest.mark.usefixtures("failing_connection")
def test_publication_receipt_and_intent_rollback_together_but_clock_floor_survives(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        original, owner = accepted(store, tree)
        raw = cast(CommitFailure, connection(store))
        authorize = SQLiteProgramStore.authorize_claim_publication

        def fail_commit(
            current: SQLiteProgramStore,
            claim: WorkClaim,
            actor: str,
            subject: str,
            operation_id: str,
            effect_key: str,
            request: dict[str, object],
        ) -> object:
            clock.reading = BootClockReading(100, 5_000_000_000, "synthetic-boot-a")
            raw.remaining = 3  # Pending marker, durable floor, then failed business COMMIT.
            return authorize(current, claim, actor, subject, operation_id, effect_key, request)

        monkeypatch.setattr(SQLiteProgramStore, "authorize_claim_publication", fail_commit)
        before = store.operations(), store.work_budget_status("program-1")
        with pytest.raises(sqlite3.OperationalError):
            owner.authorize_publication(original, "publish", "publish-effect", {"accepted": "a1"})
        assert (store.operations(), store.work_budget_status("program-1")) == before
        assert not store.operations("work-budget:v1:ownership-publication:")
        assert raw.execute("SELECT boot_ns FROM work_clock_state").fetchone()[0] == 5_000_000_000
        monkeypatch.undo()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        owner = client(store, tree, owner=True)
        assert owner.authorize_publication(original, "publish", "publish-effect", {"accepted": "a1"}).attempts == 0


def test_managed_authorization_replay_with_invalidated_and_cold_profile(sqlite_tmp_path: Path) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    tree = workspace(sqlite_tmp_path, "one")
    binary = sqlite_tmp_path / "synthetic-app-server"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        program = store.create(spec(), "create")
        budget = WorkBudgetPolicy.basic(3, 3, verifications=6, retained_bytes=16384, auxiliary=3)
        store.bind_work_budget("program-1", "project", budget, budget, 110)
        store.admit("program-1", "activate", ActivateProgram(program.revision, OWNER))
        session = CodexStdio(
            (str(binary), "app-server"), "0.99.1", environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath}
        )
        try:
            witness = CodexOwnedSessionWitness(session)
            worker = WorkOwnership(
                store,
                Principal(WORKER, "worker"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            owner = WorkOwnership(
                store,
                Principal(OWNER, "owner"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            original = claim(store, worker, tree)
            worker.witness(original, "start")
            current = store.load("program-1")
            store.admit("program-1", "start:a1", StartAttempt(current.revision, WORKER, "a1"))
            session.close()
            worker.witness(original, "complete")
            worker.submit(original)
            finish_and_accept(store, "program-1", "a1")
            authorized = owner.authorize_publication(original, "publish", "publish-effect", {"accepted": "a1"})
            witness.invalidate()
            before = store.operations()
            assert owner.authorize_publication(original, "publish", "publish-effect", {"accepted": "a1"}) == authorized
            assert store.operations() == before  # No live-profile quarantine as a side effect of reading history.
        finally:
            session.close()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        owner = WorkOwnership(
            store,
            Principal(OWNER, "owner"),
            tree,
            mode=HandoffMode.MANAGED,
            profile=witness.profile,
            resources=(witness.resource,),
        )
        assert owner.authorize_publication(
            original, "publish", "publish-effect", {"accepted": "a1"}
        ) == store.operation("publish")
