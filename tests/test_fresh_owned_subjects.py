# SPDX-License-Identifier: Apache-2.0
"""Fresh subject authority must consume the current bound producer, not old observations."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_clock_transaction_boundaries import connection
from test_codex_stdio import FAKE
from test_ownership_acceptance import acceptance
from test_sqlite_store import OWNER, WORKER, spec
from test_work_budget import Clock, initialize
from test_work_ownership import claim, client, workspace

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.sqlite_store import ProgramReadChanged, SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, FinishAttempt, ProgramSpec, StartAttempt
from creatidy_kernel.core.ownership import HandoffMode, OwnershipConflict, WorkClaim
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.ownership import WorkOwnership

pytest_plugins = ["test_sqlite_store", "test_clock_transaction_boundaries"]


@contextmanager
def finished(
    directory: Path,
    mode: str,
    *,
    submit: bool = True,
) -> Generator[tuple[SQLiteProgramStore, LocalOwnedWorkspace, WorkOwnership, WorkClaim, Clock], None, None]:
    tree = workspace(directory, "one")
    (tree.path / "source.txt").write_bytes(b"original source")
    (tree.path / "verification.txt").write_bytes(b"original verification")
    clock = Clock()
    store = SQLiteProgramStore(directory / "kernel.sqlite3", boot_clock=clock)
    native = None
    try:
        program = store.create(spec(), "create")
        budget = WorkBudgetPolicy.basic(3, 3, verifications=6, retained_bytes=16384, auxiliary=3)
        store.bind_work_budget("program-1", "project", budget, budget, 110)
        store.admit("program-1", "activate", ActivateProgram(program.revision, OWNER))
        if mode == "managed":
            binary = directory / "synthetic-app-server"
            binary.write_text(FAKE)
            binary.chmod(0o700)
            native = CodexStdio(
                (str(binary), "app-server"), "0.99.1", environment={"HOME": str(directory), "PATH": os.defpath}
            )
            witness = CodexOwnedSessionWitness(native)
            app = WorkOwnership(
                store,
                Principal(WORKER, "worker"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
        else:
            app = client(store, tree)
        original = claim(store, app, tree)
        if native is not None:
            app.witness(original, "start")
        program = store.load("program-1")
        store.admit("program-1", "start:a1", StartAttempt(program.revision, WORKER, "a1"))
        if native is not None:
            native.close()
            app.witness(original, "complete")
        else:
            app.quiesce(original, "client:exact-original-task-quiescent")
        if submit:
            app.submit(original)
            assert app.candidate_subject(original) == original.workspace_subject
        program = store.load("program-1")
        store.admit("program-1", "finish:a1", FinishAttempt(program.revision, WORKER, "a1"))
        yield store, tree, app, original, clock
    finally:
        store.close()
        if native is not None:
            native.close()


@pytest.mark.parametrize("mode", ["pull", "managed"])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("filename", ["source.txt", "verification.txt"])
@pytest.mark.parametrize(
    "entry", ["accept", "candidate", "submit", "authorize", "dispatch-check", "delivery", "reconcile", "release"]
)
def test_current_bytes_are_required_by_fresh_owned_subject_consumers(
    sqlite_tmp_path: Path,
    mode: str,
    reopen: bool,
    filename: str,
    entry: str,
) -> None:
    with finished(sqlite_tmp_path, mode) as (store, tree, _app, original, clock):
        subject, snapshot = tree.capture()
        if entry in {"authorize", "dispatch-check", "delivery"}:
            store.admit("program-1", "accept:a1", acceptance(store, "a1"))
        if entry in {"dispatch-check", "delivery"}:
            store.authorize_claim_publication(original, OWNER, subject, "publish", "publish", {})
        baseline = store.artifact(original.operation_id, "ownership-baseline")
        candidate = store.artifact(original.operation_id, "ownership-candidate")
        directory_stamp = tree.path.stat()
        (tree.path / filename).write_bytes(b"keep these later manual bytes")
        assert (tree.path.stat().st_ino, tree.path.stat().st_mtime_ns) == (
            directory_stamp.st_ino,
            directory_stamp.st_mtime_ns,
        )
        if reopen:
            store.close()
            store = SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock)
        try:
            before = tuple(connection(store).iterdump())
            with pytest.raises(OwnershipConflict, match="subject|current|capture"):
                if entry == "accept":
                    store.admit("program-1", "accept:a1", acceptance(store, "a1"))
                elif entry == "candidate":
                    store.check_claim_candidate(original, subject)
                elif entry == "submit":
                    store.submit_claim_candidate(original, subject, snapshot)
                elif entry == "authorize":
                    store.authorize_claim_publication(original, OWNER, subject, "publish", "publish", {})
                elif entry == "dispatch-check":
                    store.check_work_dispatch("publish")
                elif entry == "delivery":
                    store.claim_owned_operation(original, "publish", subject, 1)
                elif entry == "reconcile":
                    store.reconcile_claim_workspace(original, OWNER, subject, "owner:selected-exact-old-subject")
                else:
                    store.release_claim_resources(
                        original, OWNER, subject, snapshot, "owner:selected-exact-old-subject"
                    )
            assert tuple(connection(store).iterdump()) == before
            assert store.artifact(original.operation_id, "ownership-baseline") == baseline
            assert store.artifact(original.operation_id, "ownership-candidate") == candidate
            assert (tree.path / filename).read_bytes() == b"keep these later manual bytes"
            if entry == "accept":
                assert not store.load("program-1").satisfactions
                assert "second" not in store.load("program-1").ready_work_unit_ids
        finally:
            if reopen:
                store.close()


@pytest.mark.parametrize("mode", ["pull", "managed"])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("reconciled", [False, True])
def test_actual_current_candidate_accepts_without_edit_or_after_explicit_first_candidate_reconciliation(
    sqlite_tmp_path: Path,
    mode: str,
    reopen: bool,
    reconciled: bool,
) -> None:
    with finished(sqlite_tmp_path, mode, submit=not reconciled) as (store, tree, app, original, clock):
        baseline = store.artifact(original.operation_id, "ownership-baseline")
        if reconciled:
            (tree.path / "source.txt").write_bytes(b"owner reviewed these changed bytes")
            with pytest.raises(OwnershipConflict, match="manual|reconciliation"):
                app.submit(original)
            subject, _ = tree.capture()
            owner = WorkOwnership(
                store,
                Principal(OWNER, "owner"),
                tree,
                mode=original.mode,
                profile=original.profile,
                resources=tuple(item for item in original.resources if item.kind != "workspace"),
            )
            owner.reconcile(original, subject, evidence="owner:exact-current-manual-subject-reviewed")
            assert app.submit(original) == subject
        subject = app.candidate_subject(original)
        if reopen:
            store.close()
            store = SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock)
        try:
            store.check_claim_candidate(original, subject)
            store.admit("program-1", "accept:a1", acceptance(store, "a1"))
            assert "second" in store.load("program-1").ready_work_unit_ids
            assert store.artifact(original.operation_id, "ownership-baseline") == baseline
        finally:
            if reopen:
                store.close()


@pytest.mark.parametrize("entry", ["accept", "candidate", "submit", "authorize", "delivery"])
@pytest.mark.parametrize("change", ["expiry", "rollback", "unavailable", "cancel", "change", "aba", "failure", "fresh"])
def test_current_subject_capture_precedes_clock_and_refuses_invalidated_decisions(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    change: str,
) -> None:
    with finished(sqlite_tmp_path, "pull") as (store, tree, _app, original, clock):
        subject, snapshot = tree.capture()
        if entry in {"authorize", "delivery"}:
            store.admit("program-1", "accept:a1", acceptance(store, "a1"))
        if entry == "delivery":
            store.authorize_claim_publication(original, OWNER, subject, "publish", "publish", {})
        before = store.load("program-1"), store.operations(), store.work_budget_status("program-1")
        actual = LocalOwnedWorkspace.capture
        events: list[str] = []
        changed = False
        raw = connection(store)

        def capture(workspace: LocalOwnedWorkspace) -> tuple[str, bytes]:
            nonlocal changed
            assert not raw.in_transaction
            events.append("capture")
            result = actual(workspace)
            if not changed:
                changed = True
                if change == "cancel":
                    store.revoke_work_budget("program-1", "owner:cancel-during-current-capture")
                elif change in {"change", "aba"}:
                    data = tree.path / "source.txt"
                    data.write_bytes(b"modified while reading the candidate")
                    if change == "aba":
                        data.write_bytes(b"original source")
                clock.reading = (
                    None
                    if change == "unavailable"
                    else BootClockReading(
                        100,
                        {"expiry": 21_000_000_000, "rollback": 500_000_000}.get(change, 5_000_000_000),
                        "synthetic-boot-a",
                    )
                )
            if change == "failure":
                raise OSError("synthetic current capture failure")
            return result

        def sample() -> BootClockReading | None:
            events.append("clock")
            return clock()

        monkeypatch.setattr(LocalOwnedWorkspace, "capture", capture)
        monkeypatch.setattr(store, "_boot_clock", sample)

        def decide() -> None:
            if entry == "accept":
                store.admit("program-1", "accept:a1", acceptance(store, "a1"))
            elif entry == "candidate":
                store.check_claim_candidate(original, subject)
            elif entry == "submit":
                store.submit_claim_candidate(original, subject, snapshot)
            elif entry == "authorize":
                store.authorize_claim_publication(original, OWNER, subject, "publish", "publish", {})
            else:
                store.claim_owned_operation(original, "publish", subject, 1)

        if change == "fresh":
            decide()
        else:
            expected = (
                BudgetRefused
                if change in {"expiry", "rollback", "unavailable"}
                else ProgramReadChanged
                if change in {"cancel", "change", "aba"}
                else OwnershipConflict
            )
            with pytest.raises(expected):
                decide()
            assert store.load("program-1") == before[0]
            if change != "cancel":
                assert (store.operations(), store.work_budget_status("program-1")) == before[1:]
            else:
                with pytest.raises(BudgetRefused, match="revoked"):
                    store.check_work_authority("program-1")
        assert changed and events[-1] == "clock"
        floor = raw.execute("SELECT boot_ns FROM work_clock_state WHERE program_id = 'program-1'").fetchone()[0]
        assert floor == (
            21_000_000_000
            if change == "expiry"
            else 1_000_000_000
            if change in {"rollback", "unavailable"}
            else 5_000_000_000
        )


def test_fresh_subject_capture_is_requested_only_for_the_target_claim(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with finished(sqlite_tmp_path, "pull") as (store, tree, _app, original, _clock):
        unrelated = workspace(sqlite_tmp_path, "unrelated")
        base = spec()
        scoped = ProgramSpec(
            "program-2",
            base.objective,
            base.work_units,
            base.initial_inputs,
            base.budget,
            base.authority,
            acceptance_criteria=base.acceptance_criteria,
            policy_references=base.policy_references,
        )
        program = store.create(scoped, "create:program-2")
        budget = WorkBudgetPolicy.basic(3, 3, verifications=6, retained_bytes=16384, auxiliary=3)
        store.bind_work_budget("program-2", "project", budget, budget, 110)
        store.admit("program-2", "activate:program-2", ActivateProgram(program.revision, OWNER))
        claim(store, client(store, unrelated), unrelated, "program-2", "unrelated")
        # An unsupported entry in another owned tree must not trigger capture.
        os.mkfifo(unrelated.path / "unsafe")
        actual = LocalOwnedWorkspace.capture
        calls: list[Path] = []

        def capture(workspace: LocalOwnedWorkspace) -> tuple[str, bytes]:
            calls.append(workspace.path)
            assert workspace.path == tree.path
            return actual(workspace)

        monkeypatch.setattr(LocalOwnedWorkspace, "capture", capture)
        command = acceptance(store, "a1")
        store.admit("program-1", "accept:a1", command)
        assert calls == [tree.path, tree.path]
        calls.clear()
        store.load("program-1")
        store.work_budget_status("program-1")
        assert (
            store.find_work_claim(
                "claim:a1", WORKER, 1, original.attempt, original.mode, original.profile, original.resources, 5
            )
            == original
        )
        store.admit("program-1", "accept:a1", command)
        assert calls == []


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("entry", ["claim", "recheck"])
def test_original_native_runtime_delivery_also_requires_actual_current_subject(
    sqlite_tmp_path: Path,
    changed: bool,
    entry: str,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    (tree.path / "source.txt").write_bytes(b"original runtime input")
    binary = sqlite_tmp_path / "synthetic-app-server"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    native = CodexStdio(
        (str(binary), "app-server"), "0.99.1", environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath}
    )
    try:
        with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
            initialize(store)
            witness = CodexOwnedSessionWitness(native)
            app = WorkOwnership(
                store,
                Principal(WORKER, "worker"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            original = claim(store, app, tree)
            app.witness(original, "start")
            subject = tree.capture()[0]
            before = tuple(connection(store).iterdump())
            if changed:
                (tree.path / "source.txt").write_bytes(b"keep later manually changed runtime input")
                with pytest.raises(OwnershipConflict, match="subject|current"):
                    if entry == "claim":
                        store.claim_owned_operation(original, original.operation_id, subject, 1)
                    else:
                        store.check_work_dispatch(original.operation_id)
                assert tuple(connection(store).iterdump()) == before
            elif entry == "claim":
                assert store.claim_owned_operation(original, original.operation_id, subject, 1) == 1
            else:
                store.check_work_dispatch(original.operation_id)
            assert native.owned_group_active
    finally:
        native.close()


@pytest.mark.parametrize("mode", ["pull", "managed"])
def test_recorded_claim_and_publication_receipts_do_not_capture_or_sample_after_edit_and_reopen(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    with finished(sqlite_tmp_path, mode) as (store, tree, _app, original, clock):
        subject = tree.capture()[0]
        store.admit("program-1", "accept:a1", acceptance(store, "a1"))
        published = store.authorize_claim_publication(original, OWNER, subject, "publish", "publish", {})
        (tree.path / "source.txt").write_bytes(b"keep changed bytes after the original authorization")
        store.close()
        with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as reopened:

            def no_capture(_workspace: LocalOwnedWorkspace) -> tuple[str, bytes]:
                raise AssertionError("historical receipt or intent replay must not capture")

            def no_clock() -> BootClockReading | None:
                raise AssertionError("historical claim/publication receipt must not sample")

            monkeypatch.setattr(LocalOwnedWorkspace, "capture", no_capture)
            before = tuple(connection(reopened).iterdump())
            with monkeypatch.context() as patch:
                patch.setattr(reopened, "_boot_clock", no_clock)
                assert (
                    reopened.find_work_claim(
                        "claim:a1", WORKER, 1, original.attempt, original.mode, original.profile, original.resources, 5
                    )
                    == original
                )
                assert reopened.find_claim_publication(original, OWNER, "publish", "publish", {}) == published
                assert (
                    reopened.authorize_claim_publication(original, OWNER, subject, "publish", "publish", {})
                    == published
                )
            assert (
                reopened.budgeted_intent("program-1", "a1", WorkKind.AUXILIARY, "publish", "publish", {}) == published
            )
            assert tuple(connection(reopened).iterdump()) == before


@pytest.mark.parametrize("growth", [False, True])
def test_configured_larger_producer_keeps_original_and_reconciled_candidate_flows(
    sqlite_tmp_path: Path,
    growth: bool,
) -> None:
    parent = sqlite_tmp_path / "workspaces"
    parent.mkdir(mode=0o700)
    tree = LocalOwnedWorkspace(parent, "one", create=True, max_bytes=20 * 1024 * 1024)
    data = tree.path / "large-source"
    data.write_bytes(b"x" * (17 * 1024 * 1024))
    subject, snapshot = tree.capture()
    assert len(snapshot) > data.stat().st_size
    ceiling = 64 * 1024 * 1024 if growth else len(snapshot) + 4096
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        budget = WorkBudgetPolicy.basic(3, 3, verifications=6, retained_bytes=ceiling)
        store.bind_work_budget("program-1", "project", budget, budget, 110)
        store.admit("program-1", "activate", ActivateProgram(program.revision, OWNER))
        worker = client(store, tree)
        original = claim(store, worker, tree)
        assert original.workspace_subject == subject
        program = store.load("program-1")
        store.admit("program-1", "start:a1", StartAttempt(program.revision, WORKER, "a1"))
        worker.quiesce(original, "client:configured-large-producer-quiescent")
        if growth:
            data.write_bytes(b"y" * (18 * 1024 * 1024))
            with pytest.raises(OwnershipConflict, match="manual|reconciliation"):
                worker.submit(original)
            current, _ = tree.capture()
            client(store, tree, owner=True).reconcile(original, current, evidence="owner:finite-large-growth-reviewed")
        candidate = worker.submit(original)
        assert worker.candidate_subject(original) == candidate
        program = store.load("program-1")
        store.admit("program-1", "finish:a1", FinishAttempt(program.revision, WORKER, "a1"))
        store.admit("program-1", "accept:a1", acceptance(store, "a1"))
        assert "second" in store.load("program-1").ready_work_unit_ids
        assert store.artifact(original.operation_id, "ownership-baseline") == snapshot
        unique = (
            connection(store)
            .execute(
                "SELECT COUNT(*), SUM(size) FROM (SELECT DISTINCT digest, size FROM artifact_references "
                "WHERE operation_id = ?)",
                (original.operation_id,),
            )
            .fetchone()
        )
        assert unique[0] == (2 if growth else 1)
        assert unique[1] <= ceiling
        if not growth:
            assert ceiling - unique[1] == 4096  # Current proof reuses retained content, not just remaining headroom.
        before = tuple(connection(store).iterdump())
        smaller = LocalOwnedWorkspace(parent, "one", max_bytes=1024 * 1024)
        with pytest.raises(OwnershipConflict, match="bound"):
            client(store, smaller).candidate_subject(original)
        assert tuple(connection(store).iterdump()) == before


@pytest.mark.parametrize("scope", ["task", "project"])
def test_store_fresh_capture_has_a_finite_original_policy_io_bound_and_keeps_encoded_retention_gate(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    data = tree.path / "source"
    data.write_bytes(b"original")
    ceiling = 128 * 1024
    task = WorkBudgetPolicy.basic(3, 3, verifications=3, retained_bytes=ceiling if scope == "task" else ceiling * 2)
    project = WorkBudgetPolicy.basic(
        3, 3, verifications=3, retained_bytes=ceiling if scope == "project" else ceiling * 2
    )
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = store.create(spec(), "create")
        store.bind_work_budget("program-1", "project", project, task, 110)
        store.admit("program-1", "activate", ActivateProgram(program.revision, OWNER))
        worker = client(store, tree)
        original = claim(store, worker, tree)
        worker.quiesce(original, "client:original-policy-scope-quiescent")
        subject = worker.submit(original)
        data.write_bytes(b"z" * (ceiling + 1))
        assert tree.capture()[0] != subject  # The app's own larger finite I/O limit permits observing these bytes.
        before = tuple(connection(store).iterdump())
        with pytest.raises(BudgetRefused, match="retained"):
            worker.observe(original)
        assert tuple(connection(store).iterdump()) == before
        actual_read = os.read
        inode = data.stat().st_ino
        read_bytes = 0

        def read(fd: int, count: int) -> bytes:
            nonlocal read_bytes
            result = actual_read(fd, count)
            if os.fstat(fd).st_ino == inode:
                read_bytes += len(result)
            return result

        monkeypatch.setattr(os, "read", read)
        with pytest.raises(OwnershipConflict, match="current|subject"):
            store.check_claim_candidate(original, subject)
        assert read_bytes == 0  # Actual descriptor capture refuses before reading an over-bound file.
        assert tuple(connection(store).iterdump()) == before
