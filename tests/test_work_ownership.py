# SPDX-License-Identifier: Apache-2.0
"""Executable shared local handlers, exact-resource races and durable fencing."""

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_codex_stdio import FAKE
from test_sqlite_store import OWNER, VERIFIER, WORKER, spec
from test_work_budget import Clock, initialize, policy

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.sqlite_store import IdempotencyConflict, OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    FinishAttempt,
    InputBinding,
    SatisfyWorkUnit,
    StartAttempt,
    TrustedSatisfaction,
)
from creatidy_kernel.core.ownership import (
    COOPERATIVE_PROFILE,
    HandoffMode,
    HandoffProof,
    OwnershipConflict,
    ResourceScope,
    WorkClaim,
)
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.ownership import WorkOwnership

pytest_plugins = ["test_sqlite_store"]


def workspace(root: Path, name: str) -> LocalOwnedWorkspace:
    parent = root / "workspaces"
    parent.mkdir(mode=0o700, exist_ok=True)
    return LocalOwnedWorkspace(parent, name, create=True)


def client(
    store: SQLiteProgramStore,
    tree: LocalOwnedWorkspace,
    *,
    resources: tuple[ResourceScope, ...] = (),
    owner: bool = False,
) -> WorkOwnership:
    return WorkOwnership(
        store,
        Principal(OWNER, "owner") if owner else Principal(WORKER, "worker"),
        tree,
        mode=HandoffMode.PULL,
        profile=COOPERATIVE_PROFILE,
        resources=resources,
    )


def claim(
    store: SQLiteProgramStore,
    app: WorkOwnership,
    tree: LocalOwnedWorkspace,
    program_id: str = "program-1",
    attempt_id: str = "a1",
    *,
    lease: int = 5,
) -> WorkClaim:
    program = store.load(program_id)
    attempt = AttemptSpec(
        attempt_id,
        program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
        workspace_reference=str(tree.path),
    )
    return app.claim(f"claim:{attempt_id}", program.revision, attempt, lease_seconds=lease)


def test_claim_is_one_budget_domain_operation_boundary_and_idempotent(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        app = client(store, tree)
        granted = claim(store, app, tree)
        assert store.load(program.program_id).attempt("a1").spec == granted.attempt
        assert store.operation(granted.operation_id).attempts == 0
        assert len(cast(list[object], store.work_budget_status(program.program_id)["reservations"])) == 1
        assert app.claim("claim:a1", program.revision, granted.attempt, lease_seconds=5) == granted
        assert store.artifact(granted.operation_id, "ownership-baseline") == tree.capture()[1]
        with pytest.raises(IdempotencyConflict):
            app.claim("claim:a1", program.revision, granted.attempt, lease_seconds=6)
        with pytest.raises(IdempotencyConflict):
            store.claim_work(
                "claim:a1",
                OWNER,
                program.revision,
                granted.attempt,
                granted.mode,
                granted.profile,
                granted.resources,
                *tree.capture(),
                5,
            )
        with pytest.raises(OwnershipConflict, match="shared current-subject"):
            store.claim(granted.operation_id, now=100, lease_seconds=1)
        with pytest.raises(OwnershipConflict, match="pull"):
            store.claim_owned_operation(granted, granted.operation_id, tree.capture()[0], 1)


@pytest.mark.parametrize("cut", ["_insert_history", "_insert_command_result", "_finalize_artifact"])
def test_claim_failed_commit_has_no_partial_authority(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    clock = Clock()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        original = getattr(store, cut)

        def fail(*_args: object, **_kwargs: object) -> None:
            raise OSError("synthetic persistence failure")

        monkeypatch.setattr(store, cut, fail)
        with pytest.raises(OSError, match="persistence"):
            claim(store, client(store, tree), tree)
        monkeypatch.setattr(store, cut, original)
        assert store.load(program.program_id) == program
        assert store.work_budget_status(program.program_id)["reservations"] == []
        with pytest.raises(OperationConflict):
            store.operation("runtime:a1")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        granted = claim(store, client(store, tree), tree)
        assert granted.generation == 1


@pytest.mark.parametrize("shared", ["workspace", "session", "effect"])
def test_conflicting_resources_remain_reserved_after_expiry_and_reopen(
    sqlite_tmp_path: Path,
    shared: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    one, two, independent = (workspace(sqlite_tmp_path, name) for name in ("one", "two", "independent"))
    resources = () if shared == "workspace" else (ResourceScope(shared, "exact-shared-resource"),)
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        initialize(store, "program-3", project=policy(active=4))
        first = claim(store, client(store, one, resources=resources), one, lease=1)
    clock.reading = BootClockReading(102, 3_000_000_000, "synthetic-boot-a")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        with pytest.raises(OwnershipConflict, match="expired"):
            store.renew_work_claim("renew", first, 2)
        target = one if shared == "workspace" else two
        before = store.load("program-2")
        with pytest.raises(OwnershipConflict, match="conflicting"):
            claim(store, client(store, target, resources=resources), target, "program-2", "a2")
        assert store.load("program-2") == before
        unrelated = claim(store, client(store, independent), independent, "program-3", "a3")
        assert unrelated.generation > first.generation
        with pytest.raises(OwnershipConflict):
            client(store, one, resources=resources).submit(first)


@pytest.mark.parametrize(
    "reading",
    [
        None,
        BootClockReading(99, 2_000_000_000, "synthetic-boot-a"),
        BootClockReading(101, 2_000_000_000, "reboot"),
        BootClockReading(101, 12_000_000_000, "synthetic-boot-a"),
    ],
)
def test_renewal_and_reports_never_reanchor_original_wall_boot_horizon(
    sqlite_tmp_path: Path,
    reading: BootClockReading | None,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store)
        app = client(store, tree)
        granted = claim(store, app, tree)
        assert app.renew("renew", granted, lease_seconds=1000) == 110
    clock.reading = reading
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        app = client(store, tree)
        with pytest.raises(BudgetRefused):
            app.renew("later-renew", granted, lease_seconds=1000)
        with pytest.raises(BudgetRefused):
            app.observe(granted)
        assert store.original_work_deadline("program-1") == 110


def test_cooperative_quiescence_is_required_and_not_managed_assurance(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        app = client(store, tree)
        granted = claim(store, app, tree)
        with pytest.raises(OwnershipConflict, match="settlement"):
            app.submit(granted)
        with pytest.raises(OwnershipConflict):
            store.record_work_settlement(granted.operation_id, "heartbeat expired")
        with pytest.raises(OwnershipConflict, match="unknown"):
            app.witness(granted, "complete")
        app.quiesce(granted, "authenticated-client:task-quiescent")
        subject = app.submit(granted)
        assert subject == tree.capture()[0]
        with pytest.raises(OwnershipConflict, match="cooperative"):
            WorkOwnership(store, Principal(WORKER, "worker"), tree, mode=HandoffMode.PULL, profile="physical")
        with pytest.raises(OwnershipConflict, match="managed start"):
            store.record_claim_proof(
                granted,
                HandoffProof(
                    "worker",
                    granted.profile,
                    granted.mode,
                    granted.attempt.digest,
                    granted.generation,
                    granted.resources,
                    "start",
                    "fake process receipt",
                ),
            )


@pytest.mark.parametrize("change", ["tracked", "untracked", "out-of-scope", "verification", "symlink"])
def test_changed_workspace_is_retained_unattributed_and_needs_exact_owner_reconcile(
    sqlite_tmp_path: Path,
    change: str,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    (tree.path / "tracked").write_bytes(b"original")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        app = client(store, tree)
        granted = claim(store, app, tree)
        if change == "symlink":
            (tree.path / "untrusted-link").symlink_to(sqlite_tmp_path / "outside")
        else:
            (tree.path / change).write_bytes(b"manual or otherwise unattributed change")
        app.quiesce(granted, "client:quiescent")
        with pytest.raises(OwnershipConflict, match="reconciliation"):
            app.submit(granted)
        observed = tree.capture()
        assert store.artifact(granted.operation_id, f"workspace:{observed[0]}") == observed[1]
        with pytest.raises(OwnershipConflict, match="owner"):
            app.reconcile(granted, observed[0], evidence="not owner")
        control = client(store, tree, owner=True)
        control.reconcile(granted, observed[0], evidence="owner retained exact changed subject")
        assert app.submit(granted) == observed[0]
        with pytest.raises(OwnershipConflict, match="nonempty"):
            control.cleanup(granted, evidence="cleanup")
        assert tree.capture() == observed
        assert store.artifact(granted.operation_id, "ownership-baseline") != observed[1]
        assert store.artifact(granted.operation_id, "ownership-candidate") == observed[1]


def test_verification_or_manual_edit_after_candidate_fences_publication(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        app = client(store, tree)
        granted = claim(store, app, tree)
        app.quiesce(granted, "client:quiescent")
        app.submit(granted)
        (tree.path / "verification.py").write_bytes(b"changed after verification")
        with pytest.raises(OwnershipConflict, match="unchanged exact"):
            client(store, tree, owner=True).authorize_publication(granted, "publish", "publish", {"synthetic": True})
        with pytest.raises(OperationConflict):
            store.operation("publish")


def test_expired_revoked_generation_never_releases_resources_but_trusted_cleanup_can_retire_empty(
    sqlite_tmp_path: Path,
) -> None:
    clock = Clock()
    tree = workspace(sqlite_tmp_path, "one")
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        app = client(store, tree)
        granted = claim(store, app, tree)
        app.quiesce(granted, "client:quiescent")
        app.submit(granted)
        store.revoke_work_budget("program-1", "owner STOP")
        with pytest.raises(BudgetRefused, match="revoked"):
            app.submit(granted)
        client(store, tree, owner=True).cleanup(granted, evidence="exact empty leaf retired")
        assert not tree.path.exists()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        replacement = LocalOwnedWorkspace(tree.path.parent, "one-new", create=True)
        newer = claim(store, client(store, replacement), replacement, "program-2", "a2")
        assert newer.generation > granted.generation
        with pytest.raises(OwnershipConflict, match="released"):
            store.check_work_claim(granted)


def test_unsettled_effect_blocks_empty_cleanup_and_successor(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        app = client(store, tree)
        granted = claim(store, app, tree)
        store.reserve_work("program-1", "a1", "tool:a1", WorkKind.VERIFICATION)
        app.quiesce(granted, "client:quiescent")
        with pytest.raises(OwnershipConflict, match="unreconciled"):
            client(store, tree, owner=True).cleanup(granted, evidence="not safe")
        assert tree.path.exists()
        store.record_work_settlement("tool:a1", "exact-tool-effect-reconciled")
        client(store, tree, owner=True).cleanup(granted, evidence="now settled and empty")
        assert not tree.path.exists()


def test_same_workspace_identity_replacement_and_symlink_are_never_adopted(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    (tree.path / "user-data").write_bytes(b"preserve")
    old = tree.path.with_name("preserved-original")
    tree.path.rename(old)
    tree.path.symlink_to(old, target_is_directory=True)
    with pytest.raises(OSError):
        tree.capture()
    assert (old / "user-data").read_bytes() == b"preserve"
    tree.path.unlink()
    tree.path.mkdir()
    with pytest.raises(OwnershipConflict, match="identity"):
        tree.capture()
    with pytest.raises(FileExistsError):
        LocalOwnedWorkspace(tree.path.parent, "one", create=True)


def test_cleanup_race_preserves_changed_identity_and_nonempty_data(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    subject = tree.capture()[0]
    original_rename = os.rename

    def race(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        original_rename(src, "original-preserved", src_dir_fd=src_dir_fd, dst_dir_fd=src_dir_fd)
        os.mkdir(src, dir_fd=src_dir_fd)
        (tree.path / "user-data").write_bytes(b"do not delete")
        original_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", race)
    with pytest.raises(OwnershipConflict, match="race changed identity"):
        tree.retire_empty(subject)
    preserved = list(tree.path.parent.glob(".one.retired-*"))
    assert len(preserved) == 1 and (preserved[0] / "user-data").read_bytes() == b"do not delete"
    assert (tree.path.parent / "original-preserved").is_dir()


def test_managed_requires_exact_start_and_completion_witnesses_and_cold_is_unknown(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        app = WorkOwnership(
            store, Principal(WORKER, "worker"), tree, mode=HandoffMode.MANAGED, profile="synthetic:owned-group"
        )
        with pytest.raises(OwnershipConflict, match="unsupported managed"):
            claim(store, app, tree)
        assert store.load("program-1") == program
        attempt = AttemptSpec(
            "a1",
            "program-1",
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            workspace_reference=str(tree.path),
        )
        # Trusted producer fixture for the store proof path; the application
        # refused the missing managed profile before creating any authority.
        granted = store.claim_work(
            "claim:a1",
            WORKER,
            program.revision,
            attempt,
            HandoffMode.MANAGED,
            "synthetic:owned-group",
            (tree.resource,),
            *tree.capture(),
            5,
        )
        with pytest.raises(OwnershipConflict, match="start witness"):
            store.claim_owned_operation(granted, granted.operation_id, tree.capture()[0], 1)
        with pytest.raises(OwnershipConflict, match="witness"):
            app.witness(granted, "complete")
        start = HandoffProof(
            "synthetic-native-owner",
            granted.profile,
            granted.mode,
            granted.attempt.digest,
            granted.generation,
            granted.resources,
            "start",
            "opaque-original-owned-group",
        )
        for invalid in (replace(start, generation=2), replace(start, profile="other"), replace(start, resources=())):
            with pytest.raises(OwnershipConflict):
                store.record_claim_proof(granted, invalid)
        store.record_claim_proof(granted, start)
        assert store.claim_owned_operation(granted, granted.operation_id, tree.capture()[0], 1) == 1
        current = store.load(program.program_id)
        store.admit(current.program_id, "start", StartAttempt(current.revision, WORKER, "a1"))
        with pytest.raises(OwnershipConflict, match="settlement"):
            app.submit(granted)
        store.record_claim_proof(granted, replace(start, stage="complete"))
        assert app.submit(granted) == tree.capture()[0]
        with pytest.raises(OwnershipConflict):
            app.quiesce(granted, "cooperative not physical")


def test_second_process_cannot_bypass_single_writer_and_retained_claim(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    code = """
import sys
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore, ConcurrentWriter
try:
    SQLiteProgramStore(sys.argv[1], busy_timeout_ms=20)
except ConcurrentWriter:
    sys.exit(0)
sys.exit(12)
"""
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        claim(store, client(store, tree), tree)
        result = subprocess.run(  # noqa: S603 - fixed synthetic child, closed environment.
            [sys.executable, "-c", code, str(path)],
            check=False,
            timeout=10,
            env={"PATH": os.defpath, "HOME": str(sqlite_tmp_path)},
        )
        assert result.returncode == 0
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        assert store.operation("runtime:a1").attempts == 0
        assert len(store.load("program-1").attempts) == 1


def test_claim_records_are_nondispatchable_and_migration_keeps_original_rows(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        claim(store, client(store, tree), tree)
        records = store.operations("work-budget:v1:ownership-claim:")
        assert len(records) == 1
        with pytest.raises(OperationConflict, match="metadata"):
            store.claim(records[0].operation_id, now=100, lease_seconds=1)
        assert json.loads(records[0].request_json)["request"]["generation"] == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5


@pytest.mark.parametrize("stage", ["complete", "stop"])
def test_real_retained_native_owner_supplies_matching_start_and_settlement_not_cold_adoption(
    sqlite_tmp_path: Path,
    stage: str,
) -> None:
    binary = sqlite_tmp_path / "synthetic-app-server"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        tree = workspace(sqlite_tmp_path, "one")
        connection = CodexStdio(
            (str(binary), "app-server"), "0.99.1", environment={"PATH": os.defpath, "HOME": str(sqlite_tmp_path)}
        )
        try:
            witness = CodexOwnedSessionWitness(connection)
            app = WorkOwnership(
                store,
                Principal(WORKER, "worker"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            granted = claim(store, app, tree)
            app.witness(granted, "start")
            control = WorkOwnership(
                store,
                Principal(OWNER, "owner"),
                tree,
                mode=granted.mode,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            assert control.dispatch(granted, granted.operation_id, lease_seconds=1) == 1
            with pytest.raises(OwnershipConflict, match="unknown"):
                app.witness(granted, stage)
            assert CodexOwnedSessionWitness(connection).witness(granted, stage) is None
            connection.close()
            assert connection.owned_group_settled is True
            app.witness(granted, stage)
            assert app.submit(granted) == tree.capture()[0]
            assert CodexOwnedSessionWitness(connection).witness(granted, stage) is None
            witness.invalidate()
            with pytest.raises(OwnershipConflict, match="scope unknown"):
                app.submit(granted)
            with pytest.raises(OwnershipConflict, match="scope unknown"):
                store.check_work_claim(granted)
        finally:
            connection.close()


def test_exact_workspace_reuse_increases_generation_and_fences_original_claim(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        app = client(store, tree)
        original = claim(store, app, tree)
        app.quiesce(original, "client:quiescent")
        client(store, tree, owner=True).release(
            original, evidence="owner retained workspace and explicitly released settled scope"
        )
        successor = claim(store, app, tree, "program-2", "successor")
        assert successor.generation > original.generation
        assert successor.resources == original.resources
        assert tree.path.is_dir()
        with pytest.raises(OwnershipConflict, match="released"):
            app.submit(original)
        with pytest.raises(OwnershipConflict, match="released"):
            client(store, tree, owner=True).cleanup(original, evidence="stale cleanup")
        with pytest.raises(OwnershipConflict):
            store.check_work_claim(replace(successor, generation=original.generation))


def test_escaped_scope_quarantine_survives_reopen_and_never_promotes_old_witness(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        app = client(store, tree)
        original = claim(store, app, tree)
        app.quiesce(original, "client:quiescent")
        client(store, tree, owner=True).quarantine(original, evidence="task-scope effect escaped supported receipt")
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        app = client(store, tree)
        for action in (
            lambda: app.submit(original),
            lambda: client(store, tree, owner=True).cleanup(original, evidence="cannot prove safe"),
            lambda: client(store, tree, owner=True).release(original, evidence="not settled"),
        ):
            with pytest.raises(OwnershipConflict, match="scope unknown"):
                action()
        assert tree.path.is_dir()


def test_concurrent_leaf_write_during_retirement_is_retained(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    subject = tree.capture()[0]
    original = os.rmdir

    def race(path: str, *, dir_fd: int) -> None:
        (tree.path.parent / path / "late-user-data").write_bytes(b"racing bytes preserved")
        original(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "rmdir", race)
    with pytest.raises(OSError):
        tree.retire_empty(subject)
    retained = list(tree.path.parent.glob(".one.retired-*"))
    assert len(retained) == 1 and (retained[0] / "late-user-data").read_bytes() == b"racing bytes preserved"


def test_worker_cannot_supply_owner_principal_or_restart_released_native_capability(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        app = client(store, tree)
        granted = claim(store, app, tree)
        for action in (
            lambda: app.cleanup(granted, evidence="body claims owner"),
            lambda: app.release(granted, evidence="body claims owner"),
            lambda: app.quarantine(granted, evidence="body claims owner"),
            lambda: app.reconcile(granted, granted.workspace_subject, evidence="body claims owner"),
            lambda: app.authorize_publication(granted, "publish", "publish", {"principal": {"role": "owner"}}),
        ):
            with pytest.raises(OwnershipConflict, match="owner authenticated outside"):
                action()
        app.quiesce(granted, "worker:task-scope-quiescent")
        current = store.load("program-1")
        with pytest.raises(OwnershipConflict, match="cannot be restarted"):
            store.admit("program-1", "late-start", StartAttempt(current.revision, WORKER, "a1"))


def test_workspace_cannot_be_shared_by_two_capacity_authorities(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "one.sqlite3", boot_clock=Clock()) as first:
        initialize(first)
        claim(first, client(first, tree), tree)
        with SQLiteProgramStore(sqlite_tmp_path / "two.sqlite3", boot_clock=Clock()) as second:
            initialize(second)
            with pytest.raises(OwnershipConflict, match="another capacity authority"):
                client(second, LocalOwnedWorkspace(tree.path.parent, "one"))
            assert not second.load("program-1").attempts


def test_publication_uses_existing_auxiliary_budget_and_accepted_exact_subject(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        original = spec()
        program = store.create(original, "create")
        budget = WorkBudgetPolicy.basic(3, 3, verifications=6, retained_bytes=4096, auxiliary=2)
        store.bind_work_budget(program.program_id, "project", budget, budget, 110)
        program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, OWNER))
        app = client(store, tree)
        granted = claim(store, app, tree)
        current = store.load("program-1")
        current = store.admit("program-1", "start", StartAttempt(current.revision, WORKER, "a1"))
        app.quiesce(granted, "client:quiescent")
        app.submit(granted)
        current = store.admit("program-1", "finish", FinishAttempt(current.revision, WORKER, "a1"))
        assert app.candidate_subject(granted) == tree.capture()[0]
        satisfaction = TrustedSatisfaction(
            "accepted-artifact",
            "program-1",
            "first",
            original.revision,
            original.digest,
            original.work_unit_applicability_fingerprint("first"),
            VERIFIER,
            "a1",
            (InputBinding("artifact", "artifact:exact"),),
        )
        store.admit("program-1", "accept", SatisfyWorkUnit(current.revision, VERIFIER, satisfaction))
        control = client(store, tree, owner=True)
        publication = control.authorize_publication(granted, "publish", "publish", {"accepted": "accepted-artifact"})
        assert publication.operation_id == "publish" and publication.attempts == 0
        assert control.dispatch(granted, "publish", lease_seconds=1) == 1
        (tree.path / "late-manual").write_bytes(b"do not publish a stale workspace subject")
        with pytest.raises(OwnershipConflict, match="unchanged exact"):
            control.dispatch(granted, "publish", lease_seconds=1)
        assert store.operation("publish").attempts == 1
        store.revoke_work_budget("program-1", "owner STOP")
        with pytest.raises(BudgetRefused, match="revoked"):
            store.check_work_dispatch("publish")
        with pytest.raises(OwnershipConflict, match="unreconciled"):
            control.cleanup(granted, evidence="publication effect still unknown")


def test_abrupt_process_exit_retains_original_handoff_and_missing_settlement(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    path = sqlite_tmp_path / "kernel.sqlite3"
    code = """
import os, sys
from pathlib import Path
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode
from creatidy_kernel.core.work_budget import WorkBudgetPolicy
from creatidy_kernel.ports.ownership import WorkOwnership
store = SQLiteProgramStore(sys.argv[1], boot_clock=lambda: BootClockReading(100,1000000000,'synthetic-boot-a'))
program = store.create(reference_spec(), 'create')
policy = WorkBudgetPolicy.basic(3,3,verifications=3,retained_bytes=4096)
store.bind_work_budget('reference','project',policy,policy,110)
program = store.admit('reference','activate',ActivateProgram(program.revision,'owner'))
tree = LocalOwnedWorkspace(Path(sys.argv[2]), 'one')
app = WorkOwnership(store,Principal('worker','worker'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
attempt = AttemptSpec('abrupt','reference','first',program.spec.revision,program.spec.digest,
                      program.resolved_inputs('first'),workspace_reference=str(tree.path))
app.claim('claim:abrupt',program.revision,attempt,lease_seconds=5)
os._exit(0)
"""
    result = subprocess.run(  # noqa: S603 - fixed synthetic abrupt owner, closed environment.
        [sys.executable, "-c", code, str(path), str(tree.path.parent)],
        check=False,
        timeout=15,
        env={"PATH": os.defpath, "HOME": str(sqlite_tmp_path)},
    )
    assert result.returncode == 0
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        program = store.load("reference")
        assert len(program.attempts) == 1
        app = WorkOwnership(
            store, Principal("worker", "worker"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        granted = app.claim("claim:abrupt", 1, program.attempt("abrupt").spec, lease_seconds=5)
        assert store.operation(granted.operation_id).attempts == 0
        with pytest.raises(OwnershipConflict, match="settlement"):
            app.submit(granted)
        assert tree.path.is_dir()


def test_cleanup_release_failure_replays_exact_durable_filesystem_receipt_after_reopen(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        app = client(store, tree)
        granted = claim(store, app, tree)
        app.quiesce(granted, "client:quiescent")
        original = cast(
            Callable[[SQLiteProgramStore, str, str, dict[str, object]], None],
            inspect.getattr_static(SQLiteProgramStore, "_write_work_record"),
        )

        def fail(kind: str, identity: str, fields: dict[str, object]) -> None:
            if kind == "ownership-release":
                raise OSError("synthetic release persistence failure after physical retirement")
            original(store, kind, identity, fields)

        monkeypatch.setattr(store, "_write_work_record", fail)
        with pytest.raises(OSError, match="after physical retirement"):
            client(store, tree, owner=True).cleanup(granted, evidence="exact empty resource retired")
        assert not tree.path.exists() and tree.retirement() is not None
        store.check_work_claim(granted, settled=True)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        reopened = LocalOwnedWorkspace(tree.path.parent, "one")
        client(store, reopened, owner=True).cleanup(granted, evidence="replayed exact retained retirement receipt")
        with pytest.raises(OwnershipConflict, match="released"):
            store.check_work_claim(granted)
        assert not tree.path.exists()


def test_absent_path_without_owned_retirement_receipt_is_never_cleanup_proof(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    tree.path.rmdir()
    assert tree.retirement() is None
    with pytest.raises(FileNotFoundError):
        tree.retire_empty("unknown")


def test_capture_never_follows_symlinks_or_copies_multiply_linked_outside_files(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    outside = sqlite_tmp_path / "outside-synthetic"
    outside.write_bytes(b"outside bytes must not be captured")
    (tree.path / "link").symlink_to(outside)
    assert b"outside bytes must not be captured" not in tree.capture()[1]
    (tree.path / "hardlink").hardlink_to(outside)
    with pytest.raises(OwnershipConflict, match="external ownership"):
        tree.capture()
    assert outside.read_bytes() == b"outside bytes must not be captured"


def test_workspace_capture_bytes_share_original_budget_without_partial_claim_or_silent_omission(
    sqlite_tmp_path: Path,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        original = initialize(store)
        (tree.path / "large-user-data").write_bytes(b"x" * 4096)
        app = client(store, tree)
        with pytest.raises(BudgetRefused, match="retained"):
            claim(store, app, tree)
        assert store.load("program-1") == original
        assert store.work_budget_status("program-1")["reservations"] == []
        assert (tree.path / "large-user-data").read_bytes() == b"x" * 4096
        (tree.path / "large-user-data").unlink()
        granted = claim(store, app, tree)
        (tree.path / "large-user-data").write_bytes(b"y" * 4096)
        with pytest.raises(BudgetRefused, match="workspace artifact"):
            app.observe(granted)
        assert (tree.path / "large-user-data").read_bytes() == b"y" * 4096
