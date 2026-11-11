# SPDX-License-Identifier: Apache-2.0
"""Executable shared local handlers, exact-resource races and durable fencing."""

import inspect
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_clock_transaction_boundaries import CommitFailure, connection
from test_codex_stdio import FAKE
from test_sqlite_store import OWNER, VERIFIER, WORKER, spec
from test_work_budget import Clock, initialize, policy

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.sqlite_store import (
    IdempotencyConflict,
    OperationConflict,
    ProgramReadChanged,
    SQLiteProgramStore,
)
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    CancelAttempt,
    CancelProgram,
    FinishAttempt,
    InputBinding,
    PrepareAttempt,
    ProgramSpec,
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
from tools.check_schema_upgrade import CREATE_ORIGINAL_CLAIM, PINNED_INITIAL_OWNERSHIP_ID, install_pinned_reader

pytest_plugins = ["test_sqlite_store", "test_clock_transaction_boundaries"]


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
        record = json.loads(records[0].request_json)["request"]
        assert record["generation"] == 1
        assert "workspace_subject" not in json.loads(record["input"])
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


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


def prepare_generic(
    store: SQLiteProgramStore,
    program_id: str,
    attempt_id: str,
    reference: str,
    *,
    intent: bool = True,
) -> AttemptSpec:
    program = store.load(program_id)
    attempt = AttemptSpec(
        attempt_id,
        program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
        workspace_reference=reference,
    )
    command = PrepareAttempt(program.revision, WORKER, attempt)
    if intent:
        store.admit_with_intent(
            program_id,
            f"prepare:{attempt_id}",
            command,
            f"runtime:{attempt_id}",
            f"runtime:{attempt_id}",
            {"attempt": attempt.digest},
        )
    else:
        store.admit(program_id, f"prepare:{attempt_id}", command)
    return attempt


@pytest.mark.parametrize("scope", ["same", "ancestor", "descendant"])
@pytest.mark.parametrize("state", ["active", "expired", "revoked", "quarantined"])
@pytest.mark.parametrize("intent", [False, True])
def test_common_preparation_cannot_bypass_other_program_retained_workspace(
    sqlite_tmp_path: Path,
    scope: str,
    state: str,
    intent: bool,
) -> None:
    clock = Clock()
    tree, independent = workspace(sqlite_tmp_path, "one"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        before = initialize(store, "program-2", project=policy(active=4))
        original = claim(store, client(store, tree), tree, lease=1)
        if state == "expired":
            clock.reading = BootClockReading(102, 3_000_000_000, "synthetic-boot-a")
        elif state == "revoked":
            store.revoke_work_budget("program-1", "owner:revoked")
        elif state == "quarantined":
            client(store, tree, owner=True).quarantine(original, evidence="exact scope unknown")
        reference = {"same": tree.path, "ancestor": tree.path.parent, "descendant": tree.path / "child"}[scope]
        with pytest.raises(OwnershipConflict, match="workspace"):
            prepare_generic(store, "program-2", "generic", str(reference), intent=intent)
        assert store.load("program-2") == before
        assert store.work_budget_status("program-2")["reservations"] == []
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation("runtime:generic")
        prepare_generic(store, "program-2", "independent", str(independent.path))
        store.check_work_dispatch("runtime:independent")
        assert store.claim("runtime:independent", now=102, lease_seconds=1) == 1


@pytest.mark.parametrize("pending", ["runtime", "verification", "cancelled"])
@pytest.mark.parametrize("scope", ["same", "ancestor", "descendant"])
def test_claim_cannot_adopt_preexisting_unsettled_generic_workspace(
    sqlite_tmp_path: Path,
    pending: str,
    scope: str,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        before = initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        reference = {"same": tree.path, "ancestor": tree.path.parent, "descendant": tree.path / "child"}[scope]
        prepare_generic(store, "program-2", "earlier", str(reference))
        if pending == "verification":
            store.record_work_settlement("runtime:earlier", "trusted:runtime-scope-quiescent")
            store.reserve_work("program-2", "earlier", "verify:earlier", WorkKind.VERIFICATION)
        elif pending == "cancelled":
            program = store.load("program-2")
            store.admit("program-2", "cancel:earlier", CancelAttempt(program.revision, OWNER, "earlier"))
        with pytest.raises(OwnershipConflict, match="workspace"):
            claim(store, client(store, tree), tree)
        assert store.load("program-1") == before
        assert store.work_budget_status("program-1")["reservations"] == []
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation("runtime:a1")
        store.record_work_settlement(
            "verify:earlier" if pending == "verification" else "runtime:earlier",
            "trusted:exact-pending-scope-settled",
        )
        assert claim(store, client(store, tree), tree).generation == 1


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("state", ["active", "expired", "revoked", "quarantined"])
def test_common_delivery_and_reservation_refuse_later_workspace_owner(
    sqlite_tmp_path: Path,
    replacement: bool,
    state: str,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        initialize(store, "program-3", project=policy(active=4))
        prepare_generic(store, "program-2", "earlier", str(tree.path))
        store.record_work_settlement("runtime:earlier", "trusted:original-scope-quiescent")
        app = client(store, tree)
        first = claim(store, app, tree, lease=1)
        current_owner = first
        if replacement:
            app.quiesce(first, "client:exact-scope-quiescent")
            client(store, tree, owner=True).release(first, evidence="owner:retain-settled-tree")
            newer = claim(store, app, tree, "program-3", "newer", lease=1)
            assert newer.generation > first.generation
            current_owner = newer
        if state == "expired":
            clock.reading = BootClockReading(102, 3_000_000_000, "synthetic-boot-a")
        elif state == "revoked":
            store.revoke_work_budget(current_owner.attempt.program_id, "owner:revoked")
        elif state == "quarantined":
            client(store, tree, owner=True).quarantine(current_owner, evidence="exact scope unknown")
        before = store.work_budget_status("program-2")
        with pytest.raises(OwnershipConflict, match="workspace"):
            store.check_work_dispatch("runtime:earlier")
        with pytest.raises(OwnershipConflict, match="workspace"):
            store.claim("runtime:earlier", now=100, lease_seconds=1)
        assert store.operation("runtime:earlier").attempts == 0
        with pytest.raises(OwnershipConflict, match="workspace"):
            store.reserve_work("program-2", "earlier", "new:verify", WorkKind.VERIFICATION)
        with pytest.raises(OwnershipConflict, match="workspace"):
            store.budgeted_intent("program-2", "earlier", WorkKind.VERIFICATION, "new:verify", "new:verify", {})
        assert store.work_budget_status("program-2") == before
        with pytest.raises(OperationConflict, match="unknown operation"):
            store.operation("new:verify")


@pytest.mark.parametrize("suffix", ["/../one", "/./child", "/", "//child"])
def test_common_preparation_refuses_ambiguous_absolute_workspace_paths(sqlite_tmp_path: Path, suffix: str) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        before = initialize(store)
        with pytest.raises(OwnershipConflict, match="canonical absolute"):
            prepare_generic(store, "program-1", "ambiguous", str(tree.path) + suffix)
        assert store.load("program-1") == before


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("state", ["edited", "expired", "revoked", "retired", "quarantined", "clock-unknown"])
def test_public_claim_replays_explicit_request_before_capture_without_new_authority(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reopen: bool,
    state: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    samples: list[BootClockReading | None] = []

    def observe_clock() -> BootClockReading | None:
        samples.append(clock.reading)
        return clock()

    tree = workspace(sqlite_tmp_path, "one")
    store = SQLiteProgramStore(path, boot_clock=observe_clock)
    try:
        program = initialize(store)
        app = client(store, tree)
        original = claim(store, app, tree)
        if state == "edited":
            (tree.path / "manual-edit").write_bytes(b"preserve changed bytes")
        elif state == "expired":
            clock.reading = BootClockReading(106, 7_000_000_000, "synthetic-boot-a")
        elif state == "revoked":
            store.revoke_work_budget("program-1", "owner:revoked")
        elif state == "retired":
            app.quiesce(original, "client:exact-task-quiescent")
            client(store, tree, owner=True).cleanup(original, evidence="owner:empty-resource-retired")
        elif state == "quarantined":
            client(store, tree, owner=True).quarantine(original, evidence="exact scope unknown")
        else:
            clock.reading = None
        if reopen:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=observe_clock)
            tree = LocalOwnedWorkspace(tree.path.parent, "one")
            app = client(store, tree)

        def unexpected_capture() -> tuple[str, bytes]:
            raise AssertionError("historical explicit-request lookup must precede filesystem capture")

        monkeypatch.setattr(tree, "capture", unexpected_capture)
        before_program, before_budget, before_operations = (
            store.load("program-1"),
            store.work_budget_status("program-1"),
            store.operations(),
        )
        before_samples = len(samples)
        assert app.claim("claim:a1", program.revision, original.attempt, lease_seconds=5) == original
        assert store.load("program-1") == before_program
        assert store.work_budget_status("program-1") == before_budget
        assert store.operations() == before_operations
        assert len(samples) == before_samples
        assert store.artifact(original.operation_id, "ownership-baseline")
        if state != "edited":
            with pytest.raises((OwnershipConflict, BudgetRefused)):
                store.check_work_claim(original)
    finally:
        store.close()


@pytest.mark.parametrize("changed", ["claimant", "revision", "attempt", "mode", "resources", "lease"])
def test_retired_public_claim_replay_still_rejects_changed_explicit_input(sqlite_tmp_path: Path, changed: str) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = initialize(store)
        app = client(store, tree)
        original = claim(store, app, tree)
        app.quiesce(original, "client:exact-task-quiescent")
        client(store, tree, owner=True).cleanup(original, evidence="owner:empty-resource-retired")
        principal = Principal("different-worker" if changed == "claimant" else WORKER, "worker")
        configured = WorkOwnership(
            store,
            principal,
            LocalOwnedWorkspace(tree.path.parent, "one"),
            mode=HandoffMode.MANAGED if changed == "mode" else HandoffMode.PULL,
            profile=COOPERATIVE_PROFILE,
            resources=(ResourceScope("session", "changed-session"),) if changed == "resources" else (),
        )
        changed_attempt = AttemptSpec(
            "changed-attempt",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            workspace_reference=str(tree.path),
        )
        with pytest.raises(IdempotencyConflict):
            configured.claim(
                "claim:a1",
                program.revision + (1 if changed == "revision" else 0),
                changed_attempt if changed == "attempt" else original.attempt,
                lease_seconds=6 if changed == "lease" else 5,
            )


def test_generic_physical_preparation_cannot_compete_with_unsettled_generic_work(sqlite_tmp_path: Path) -> None:
    tree, independent = workspace(sqlite_tmp_path, "one"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-1", "first-generic", str(tree.path))
        with pytest.raises(OwnershipConflict, match="workspace"):
            prepare_generic(store, "program-2", "competing", str(tree.path))
        prepare_generic(store, "program-2", "independent", str(independent.path))
        assert store.claim("runtime:independent", now=100, lease_seconds=1) == 1


def test_full_original_authority_expiry_keeps_workspace_but_not_unrelated_work_blocked(sqlite_tmp_path: Path) -> None:
    tree, independent = workspace(sqlite_tmp_path, "one"), workspace(sqlite_tmp_path, "independent")
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        first = initialize(store, project=policy(active=4))
        original = first.spec
        second_spec = ProgramSpec(
            "program-2",
            original.objective,
            original.work_units,
            original.initial_inputs,
            original.budget,
            original.authority,
            acceptance_criteria=original.acceptance_criteria,
            policy_references=original.policy_references,
        )
        second = store.create(second_spec, "create:program-2")
        store.bind_work_budget("program-2", "synthetic-project", policy(active=4), policy(), 200)
        store.admit("program-2", "activate:program-2", ActivateProgram(second.revision, OWNER))
        claim(store, client(store, tree), tree, lease=1000)
        clock.reading = BootClockReading(112, 13_000_000_000, "synthetic-boot-a")
        with pytest.raises(OwnershipConflict, match="workspace"):
            prepare_generic(store, "program-2", "conflict", str(tree.path))
        prepare_generic(store, "program-2", "independent", str(independent.path))
        assert store.claim("runtime:independent", now=112, lease_seconds=1) == 1


def test_store_historical_lookup_compares_profile_before_any_live_validation(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = initialize(store)
        app = client(store, tree)
        original = claim(store, app, tree)
        app.quiesce(original, "client:exact-task-quiescent")
        client(store, tree, owner=True).cleanup(original, evidence="owner:empty-resource-retired")
        with pytest.raises(IdempotencyConflict):
            store.find_work_claim(
                "claim:a1",
                WORKER,
                program.revision,
                original.attempt,
                original.mode,
                "changed-profile",
                original.resources,
                5,
            )


@pytest.mark.parametrize("child", [False, True])
def test_generic_physical_alias_is_refused_not_normalized_into_an_owned_workspace(
    sqlite_tmp_path: Path, child: bool
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    alias = tree.path.parent / "alias"
    alias.symlink_to(tree.path, target_is_directory=True)
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        before = initialize(store, "program-2", project=policy(active=4))
        claim(store, client(store, tree), tree)
        with pytest.raises(OwnershipConflict, match="canonical"):
            prepare_generic(store, "program-2", "alias", str(alias / "child" if child else alias))
        assert store.load("program-2") == before
        assert store.work_budget_status("program-2")["reservations"] == []


def test_alias_introduced_after_preparation_fences_delivery_and_candidate(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    alias = tree.path.parent / "future-workspace"
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-2", "earlier", str(alias))
        app = client(store, tree)
        original = claim(store, app, tree)
        app.quiesce(original, "client:exact-task-quiescent")
        alias.symlink_to(tree.path, target_is_directory=True)
        with pytest.raises(OwnershipConflict, match="canonical"):
            store.claim("runtime:earlier", now=100, lease_seconds=1)
        with pytest.raises(OwnershipConflict, match="canonical"):
            app.submit(original)
        assert store.operation("runtime:earlier").attempts == 0


@pytest.mark.parametrize("entry", ["claim", "check_dispatch", "prepare", "reserve", "renew", "new_claim", "candidate"])
@pytest.mark.parametrize("late", ["expiry", "rollback", "fresh"])
@pytest.mark.parametrize("other_program", [False, True])
def test_physical_resolution_cannot_spend_stale_store_clock_cut(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    late: str,
    other_program: bool,
) -> None:
    events: list[str] = []

    class ObservedClock(Clock):
        def __call__(self) -> BootClockReading | None:
            events.append("boot")
            return super().__call__()

    clock = ObservedClock()
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        original: WorkClaim | None = None
        if entry in {"claim", "check_dispatch", "reserve"}:
            prepare_generic(store, "program-1", "generic", str(tree.path))
        elif entry in {"renew", "candidate"}:
            original = claim(store, client(store, tree), tree)
            if entry == "candidate":
                client(store, tree).quiesce(original, "client:exact-task-quiescent")
        resolved_path = tree.path
        if other_program:
            other = workspace(sqlite_tmp_path, "other")
            initialize(store, "program-2", project=policy(active=4))
            prepare_generic(store, "program-2", "other-generic", str(other.path))
            resolved_path = other.path
        captured = tree.capture()
        before = store.load("program-1"), store.work_budget_status("program-1"), store.operations()
        actual_resolve = Path.resolve
        advanced = False
        events.clear()

        def late_resolve(path: Path, strict: bool = False) -> Path:
            nonlocal advanced
            assert not connection(store).in_transaction
            events.append("path")
            value = actual_resolve(path, strict=strict)
            if path == resolved_path and not advanced:
                advanced = True
                clock.reading = BootClockReading(
                    100,
                    {"expiry": 21_000_000_000, "rollback": 500_000_000, "fresh": 5_000_000_000}[late],
                    "synthetic-boot-a",
                )
            return value

        monkeypatch.setattr(Path, "resolve", late_resolve)
        with nullcontext() if late == "fresh" else pytest.raises(BudgetRefused):
            if entry == "claim":
                store.claim("runtime:generic", now=100, lease_seconds=1)
            elif entry == "check_dispatch":
                store.check_work_dispatch("runtime:generic")
            elif entry == "prepare":
                prepare_generic(store, "program-1", "late-generic", str(tree.path))
            elif entry == "reserve":
                store.reserve_work("program-1", "generic", "late-verify", WorkKind.VERIFICATION)
            elif entry == "renew":
                assert original is not None
                store.renew_work_claim("late-renew", original, 7)
            elif entry == "candidate":
                assert original is not None
                store.submit_claim_candidate(original, *captured)
            else:
                claim(store, client(store, tree), tree)
        assert advanced
        assert events[-1] == "boot" and "path" in events
        assert inspect.getattr_static(store, "_workspace_scopes") is None
        assert inspect.getattr_static(store, "_workspace_artifacts") is None
        assert store.original_work_deadline("program-1") == 110
        if late != "fresh":
            assert (store.load("program-1"), store.work_budget_status("program-1"), store.operations()) == before
        if late == "expiry":
            assert (
                connection(store)
                .execute(
                    "SELECT boot_ns FROM work_clock_state WHERE program_id = 'program-1'",
                )
                .fetchone()[0]
                == 21_000_000_000
            )


@pytest.mark.parametrize("change", ["alias", "cancel"])
def test_physical_preflight_race_refuses_without_rolling_back_owner_changes(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    clock = Clock()
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store)
        prepare_generic(store, "program-1", "generic", str(tree.path))
        actual_resolve = Path.resolve
        changed = False

        def changed_resolve(path: Path, strict: bool = False) -> Path:
            nonlocal changed
            assert not connection(store).in_transaction
            result = actual_resolve(path, strict=strict)
            if path == tree.path and not changed:
                changed = True
                clock.reading = BootClockReading(100, 5_000_000_000, "synthetic-boot-a")
                if change == "alias":
                    retained = tree.path.with_name("retained-original")
                    tree.path.rename(retained)
                    tree.path.symlink_to(retained, target_is_directory=True)
                else:
                    program = store.load("program-1")
                    store.admit("program-1", "callback:cancel", CancelProgram(program.revision, OWNER))
            return result

        monkeypatch.setattr(Path, "resolve", changed_resolve)
        with pytest.raises(ProgramReadChanged):
            store.claim("runtime:generic", now=100, lease_seconds=1)
        assert changed and store.operation("runtime:generic").attempts == 0
        assert inspect.getattr_static(store, "_workspace_scopes") is None
        assert connection(store).execute("SELECT boot_ns FROM work_clock_state").fetchone()[0] == 5_000_000_000
        if change == "alias":
            assert tree.path.is_symlink() and tree.path.with_name("retained-original").is_dir()
        else:
            assert store.load("program-1").status.value == "cancelled"


@pytest.mark.usefixtures("failing_connection")
@pytest.mark.parametrize("cut", ["pending", "flush", "business"])
def test_physical_preflight_clock_evidence_survives_failed_decision_and_reopen(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store)
        prepare_generic(store, "program-1", "generic", str(tree.path))
        before = store.load("program-1"), store.work_budget_status("program-1"), store.operations()
        raw = cast(CommitFailure, connection(store))
        actual_resolve = Path.resolve
        armed = False

        def fail_after_preflight(path: Path, strict: bool = False) -> Path:
            nonlocal armed
            assert not raw.in_transaction
            result = actual_resolve(path, strict=strict)
            if path == tree.path and not armed:
                armed = True
                clock.reading = BootClockReading(100, 5_000_000_000, "synthetic-boot-a")
                raw.remaining = {"pending": 1, "flush": 2, "business": 3}[cut]
            return result

        monkeypatch.setattr(Path, "resolve", fail_after_preflight)
        with pytest.raises(sqlite3.OperationalError if cut == "business" else BudgetRefused):
            store.claim("runtime:generic", now=100, lease_seconds=1)
        assert armed and (store.load("program-1"), store.work_budget_status("program-1"), store.operations()) == before
        assert inspect.getattr_static(store, "_workspace_scopes") is None
        assert inspect.getattr_static(store, "_workspace_artifacts") is None
        row = raw.execute("SELECT boot_ns, pending FROM work_clock_state").fetchone()
        assert tuple(row) == (5_000_000_000 if cut == "business" else 1_000_000_000, int(cut == "flush"))
        monkeypatch.undo()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        assert (store.load("program-1"), store.work_budget_status("program-1"), store.operations()) == before
        assert store.original_work_deadline("program-1") == 110
        if cut == "flush":
            with pytest.raises(BudgetRefused, match="persistence unverified"):
                store.claim("runtime:generic", now=100, lease_seconds=1)
        else:
            assert store.claim("runtime:generic", now=100, lease_seconds=1) == 1


@pytest.mark.parametrize("entry", ["new_claim", "candidate"])
def test_artifact_storage_validation_cannot_outlive_ownership_clock_cut(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store)
        original: WorkClaim | None = None
        if entry == "candidate":
            original = claim(store, client(store, tree), tree)
            client(store, tree).quiesce(original, "client:exact-task-quiescent")
        captured = tree.capture()
        before = store.load("program-1"), store.work_budget_status("program-1"), store.operations()
        artifact_directory = path.parent / f"{path.name}.artifacts"
        actual_resolve = Path.resolve
        expired = False

        def slow_artifact_storage(directory: Path, strict: bool = False) -> Path:
            nonlocal expired
            result = actual_resolve(directory, strict=strict)
            if directory == artifact_directory and not expired:
                expired = True
                clock.reading = BootClockReading(100, 21_000_000_000, "synthetic-boot-a")
            return result

        monkeypatch.setattr(Path, "resolve", slow_artifact_storage)
        with pytest.raises(BudgetRefused):
            if original is None:
                claim(store, client(store, tree), tree)
            else:
                store.submit_claim_candidate(original, *captured)
        assert expired
        assert (store.load("program-1"), store.work_budget_status("program-1"), store.operations()) == before


@pytest.fixture(scope="module")
def initial_ownership_reader(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return install_pinned_reader(
        tmp_path_factory.mktemp("original-owned-claim-reader"), commit_id=PINNED_INITIAL_OWNERSHIP_ID
    )


@pytest.mark.parametrize("state", ["edited", "retired", "conflict"])
def test_actual_original_claim_records_replay_without_rewriting_or_adopting_conflicts(
    sqlite_tmp_path: Path,
    initial_ownership_reader: Path,
    state: str,
) -> None:
    subprocess.run(  # noqa: S603 - exact original public source, synthetic closed fixture.
        [str(initial_ownership_reader), "-I", "-c", CREATE_ORIGINAL_CLAIM, str(sqlite_tmp_path), state],
        check=True,
    )
    path = sqlite_tmp_path / "kernel.sqlite3"
    with closing(sqlite3.connect(path)) as original:
        original_rows = tuple(original.iterdump())
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        original_operation = store.operations("work-budget:v1:ownership-claim:")[0]
        original_record = json.loads(original_operation.request_json)["request"]
        assert json.loads(original_record["input"])["workspace_subject"] == original_record["workspace_subject"]
        tree = LocalOwnedWorkspace(sqlite_tmp_path / "workspaces", "one")
        app = WorkOwnership(
            store, Principal("worker", "worker"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        attempt = store.load("reference").attempt("original").spec
        result = app.claim("claim:original", 1, attempt, lease_seconds=5)
        assert result.generation == 1 and result.expires_at == 105
        assert result.workspace_subject == original_record["workspace_subject"]
        assert store.operation(original_operation.operation_id) == original_operation
        if state == "conflict":
            with pytest.raises(OwnershipConflict, match="workspace"):
                app.submit(result)
            with pytest.raises(OwnershipConflict, match="workspace"):
                store.claim("runtime:competing", now=100, lease_seconds=1)
            assert store.operation("runtime:competing").attempts == 0
    if state != "conflict":
        with closing(sqlite3.connect(path)) as current:
            assert (
                tuple(line for line in current.iterdump() if "ordinary_preparation_contract" not in line)
                == original_rows
            )
