# SPDX-License-Identifier: Apache-2.0
"""Live invalid subjects retain their footprint without revoking independent work."""

import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_clock_transaction_boundaries import connection
from test_ownership_acceptance import acceptance, finish_generic
from test_sqlite_store import OWNER, WORKER, spec
from test_work_budget import Clock, initialize, policy
from test_work_ownership import claim, client, prepare_generic, workspace

from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.sqlite_store import CorruptHistory, ProgramReadChanged, SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    FinishAttempt,
    PrepareAttempt,
    ProgramSpec,
    StartAttempt,
)
from creatidy_kernel.core.ownership import OwnershipConflict, WorkClaim
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from tools.check_schema_upgrade import CREATE_LOGICAL_GENERIC, PINNED_PRE_WORKSPACE_ID, install_pinned_reader

pytest_plugins = ["test_sqlite_store", "test_clock_transaction_boundaries"]


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("entry", ["new-owned", "prepared-owned", "new-generic", "prepared-generic"])
def test_live_generic_alias_retains_a_and_r_without_globally_blocking_b(
    sqlite_tmp_path: Path,
    entry: str,
    reopen: bool,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    (old.path / "manual-data").write_bytes(b"original bytes and history must survive")
    store = SQLiteProgramStore(path, boot_clock=clock)
    try:
        configured = WorkBudgetPolicy.basic(3, 4, verifications=6, retained_bytes=4096, auxiliary=1)
        for name in ("program-1", "program-2"):
            base = spec()
            scoped = ProgramSpec(
                name,
                base.objective,
                base.work_units,
                base.initial_inputs,
                base.budget,
                base.authority,
                acceptance_criteria=base.acceptance_criteria,
                policy_references=base.policy_references,
            )
            program = store.create(scoped, f"create:{name}")
            store.bind_work_budget(name, "project", configured, configured, 110)
            store.admit(name, f"activate:{name}", ActivateProgram(program.revision, OWNER))
        prepare_generic(store, "program-1", "live-old", str(old.path))
        owned: WorkClaim | None = None
        if entry == "prepared-owned":
            owned = claim(store, client(store, independent), independent, "program-2", "independent")
            program = store.load("program-2")
            store.admit("program-2", "start:independent", StartAttempt(program.revision, WORKER, "independent"))
        elif entry == "prepared-generic":
            prepare_generic(store, "program-2", "independent", str(independent.path))
        original = store.load("program-1"), store.work_budget_status("program-1"), store.operation("runtime:live-old")
        retained = old.path.with_name("retained-old")
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        if reopen:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=clock)
            independent = LocalOwnedWorkspace(independent.path.parent, "independent")
        if entry == "new-owned":
            owned = claim(store, client(store, independent), independent, "program-2", "independent")
            program = store.load("program-2")
            store.admit("program-2", "start:independent", StartAttempt(program.revision, WORKER, "independent"))
        elif entry == "new-generic":
            prepare_generic(store, "program-2", "independent", str(independent.path))
        if owned is not None:
            app = client(store, independent)
            assert app.renew("renew:independent", owned, lease_seconds=7) == 107
            app.quiesce(owned, "client:independent-task-quiescent")
            app.submit(owned)
            program = store.load("program-2")
            store.admit("program-2", "finish:independent", FinishAttempt(program.revision, WORKER, "independent"))
        else:
            store.reserve_work("program-2", "independent", "verification:independent", WorkKind.VERIFICATION)
            store.check_work_dispatch("runtime:independent")
            assert store.claim("runtime:independent", now=100, lease_seconds=1) == 1
            finish_generic(store, "independent", program_id="program-2")
            store.record_work_settlement("runtime:independent", "trusted:independent-runtime-closed", expected_fence=1)
            store.record_work_settlement("verification:independent", "trusted:independent-check-complete")
        store.admit("program-2", "accept:independent", acceptance(store, "independent", program_id="program-2"))
        if owned is not None:
            owner = client(store, independent, owner=True)
            owner.authorize_publication(owned, "publication", "publication", {"accepted": "independent"})
            assert owner.dispatch(owned, "publication", lease_seconds=1) == 1
        else:
            store.budgeted_intent("program-2", "independent", WorkKind.AUXILIARY, "publication", "publication", {})
            assert store.claim("publication", now=100, lease_seconds=1) == 1
        assert (
            store.load("program-1"),
            store.work_budget_status("program-1"),
            store.operation("runtime:live-old"),
        ) == original
        for target in (old.path, retained, retained / "child", old.path.parent):
            before = store.operations()
            with pytest.raises(OwnershipConflict):
                prepare_generic(store, "program-2", "unsafe", str(target))
            assert store.operations() == before
        assert old.path.is_symlink()
        assert (retained / "manual-data").read_bytes() == b"original bytes and history must survive"
        reservations = cast(list[dict[str, object]], store.work_budget_status("program-1")["reservations"])
        assert len(reservations) == 1
    finally:
        store.close()


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("entry", ["new-owned", "prepared-owned", "new-generic", "prepared-generic"])
@pytest.mark.parametrize("change", ["different", "missing", "cycle", "canonical-replaced", "moved-into-b", "custody"])
def test_unknown_original_identity_never_adopts_current_alias_or_parent_only_hold(
    sqlite_tmp_path: Path,
    entry: str,
    reopen: bool,
    change: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    (old.path / "manual").write_bytes(b"unknown is not released or adopted")
    store = SQLiteProgramStore(path, boot_clock=clock)
    try:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-1", "old", str(old.path))
        owned = None
        if entry == "prepared-owned":
            owned = claim(store, client(store, independent), independent, "program-2", "independent")
        elif entry == "prepared-generic":
            prepare_generic(store, "program-2", "independent", str(independent.path))
        retained = independent.path / "moved-original" if change == "moved-into-b" else old.path.with_name("retained")
        old.path.rename(retained)
        if change == "canonical-replaced":
            old.path.mkdir()
        elif change == "cycle":
            old.path.symlink_to(old.path, target_is_directory=True)
        elif change == "missing":
            old.path.symlink_to(old.path.with_name("missing"), target_is_directory=True)
        elif change in {"different", "moved-into-b"}:
            unrelated = old.path.with_name("unrelated")
            unrelated.mkdir()
            old.path.symlink_to(unrelated, target_is_directory=True)
        else:
            other_parent = sqlite_tmp_path / "other-parent"
            other_parent.mkdir(mode=0o700)
            moved = other_parent / "original"
            retained.rename(moved)
            retained = moved
            old.path.symlink_to(retained, target_is_directory=True)
        before = store.operations(), store.load("program-1"), store.work_budget_status("program-1")
        if reopen:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=clock)
        with pytest.raises(OwnershipConflict, match="identity|custody|canonical"):
            if entry == "new-owned":
                claim(store, client(store, independent), independent, "program-2", "independent")
            elif entry == "new-generic":
                prepare_generic(store, "program-2", "independent", str(independent.path))
            elif owned is not None:
                client(store, independent).renew("renew", owned, lease_seconds=7)
            else:
                store.reserve_work("program-2", "independent", "verify", WorkKind.VERIFICATION)
        assert (store.operations(), store.load("program-1"), store.work_budget_status("program-1")) == before
        assert (retained / "manual").read_bytes() == b"unknown is not released or adopted"
        assert not store.operations("work-budget:v1:ownership-settlement:")
        assert not store.operations("work-budget:v1:settlement:")
    finally:
        store.close()


@pytest.mark.parametrize("relation", ["same-inode", "candidate-descendant", "candidate-ancestor"])
def test_inode_and_ancestor_facts_detect_nonlexical_conflicts_without_mount_privileges(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relation: str,
) -> None:
    """Synthetic bind-shaped metadata; ordinary aliases are exercised with real symlinks above."""
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-1", "old", str(old.path))
        capture = SQLiteProgramStore._capture_workspace_footprint  # pyright: ignore[reportPrivateUsage]
        original = capture(str(old.path))
        assert original.root is not None

        def alias(reference: str):
            observed = capture(reference)
            if reference == str(independent.path):
                if relation == "same-inode":
                    return replace(observed, root=original.root)
                if relation == "candidate-descendant":
                    return replace(observed, ancestors=(*observed.ancestors, original.root))
            if reference == str(old.path) and relation == "candidate-ancestor":
                candidate = capture(str(independent.path))
                assert candidate.root is not None
                return replace(observed, ancestors=(*observed.ancestors, candidate.root))
            return observed

        monkeypatch.setattr(SQLiteProgramStore, "_capture_workspace_footprint", staticmethod(alias))
        before = store.operations()
        with pytest.raises(OwnershipConflict, match="inode/ancestor"):
            prepare_generic(store, "program-2", "independent", str(independent.path))
        assert store.operations() == before


@pytest.mark.parametrize("change", ["aba", "alias-target", "cancel", "expired"])
def test_identity_preflight_finishes_before_durable_clock_and_rejects_changed_cut(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-1", "old", str(old.path))
        retained = old.path.with_name("retained")
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        actual = SQLiteProgramStore._capture_workspace_footprint  # pyright: ignore[reportPrivateUsage]
        changed = False
        events: list[str] = []

        def capture(reference: str):
            nonlocal changed
            assert not connection(store).in_transaction
            events.append("identity")
            result = actual(reference)
            if reference == str(old.path) and not changed:
                changed = True
                if change == "aba":
                    temporary = retained.with_name("temporarily-moved")
                    retained.rename(temporary)
                    temporary.rename(retained)
                elif change == "alias-target":
                    old.path.unlink()
                    old.path.symlink_to(independent.path, target_is_directory=True)
                elif change == "cancel":
                    store.revoke_work_budget("program-2", "owner:cancel-during-probe")
                clock.reading = BootClockReading(
                    100, 21_000_000_000 if change == "expired" else 5_000_000_000, "synthetic-boot-a"
                )
            return result

        def sample() -> BootClockReading | None:
            events.append("clock")
            return clock()

        monkeypatch.setattr(SQLiteProgramStore, "_capture_workspace_footprint", staticmethod(capture))
        monkeypatch.setattr(store, "_boot_clock", sample)
        with pytest.raises(BudgetRefused if change == "expired" else ProgramReadChanged):
            prepare_generic(store, "program-2", "independent", str(independent.path))
        assert changed and events[-1] == "clock"
        assert clock.reading is not None
        assert (
            connection(store).execute("SELECT MAX(boot_ns) FROM work_clock_state").fetchone()[0]
            == clock.reading.boot_ns
        )
        if change == "cancel":
            with pytest.raises(BudgetRefused, match="revoked"):
                store.check_work_authority("program-2")
        assert store.load("program-2").attempts == ()


@pytest.fixture(scope="module")
def prior_identity_reader(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return install_pinned_reader(tmp_path_factory.mktemp("prior-workspace-identity"), commit_id=PINNED_PRE_WORKSPACE_ID)


def test_actual_unbound_prior_generic_is_not_retroactively_tagged_or_adopted(
    sqlite_tmp_path: Path,
    prior_identity_reader: Path,
) -> None:
    subprocess.run(  # noqa: S603 - exact pinned public producer with synthetic offline data.
        [str(prior_identity_reader), "-I", "-c", CREATE_LOGICAL_GENERIC, str(sqlite_tmp_path), "logical-only"],
        check=True,
    )
    path = sqlite_tmp_path / "kernel.sqlite3"
    old = sqlite_tmp_path / "workspaces" / "one"
    retained = old.with_name("retained")
    old.rename(retained)
    old.symlink_to(retained, target_is_directory=True)
    with closing(sqlite3.connect(path)) as raw:
        before = tuple(raw.iterdump())
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        independent = workspace(sqlite_tmp_path, "independent")
        program = store.load("successor")
        attempt = AttemptSpec(
            "independent",
            "successor",
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            workspace_reference=str(independent.path),
        )
        with pytest.raises(OwnershipConflict, match="identity/custody unknown"):
            store.admit("successor", "prepare:independent", PrepareAttempt(program.revision, "worker", attempt))
        assert not store.operations("work-budget:v1:ownership-workspace:")
    with closing(sqlite3.connect(path)) as raw:
        assert tuple(raw.iterdump()) == before


def test_actual_prior_reader_refuses_new_identity_category_without_changing_bytes(
    sqlite_tmp_path: Path,
    prior_identity_reader: Path,
) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    tree = workspace(sqlite_tmp_path, "generic")
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        prepare_generic(store, "program-1", "generic", str(tree.path))
        records = store.operations("work-budget:v1:ownership-")
        assert len(records) == 1 and json.loads(records[0].request_json)["request"]["type"] == "ownership-workspace"
    with closing(sqlite3.connect(path)) as raw:
        before = tuple(raw.iterdump())
    subprocess.run(  # noqa: S603 - actual prior installed reader, no effects.
        [
            str(prior_identity_reader),
            "-I",
            "-c",
            """
import sys
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore, CorruptHistory
try:
    SQLiteProgramStore(sys.argv[1])
except CorruptHistory as error:
    assert 'unsupported ownership record' in str(error)
else:
    raise AssertionError('old reader ignored the unknown identity category')
""",
            str(path),
        ],
        check=True,
    )
    with closing(sqlite3.connect(path)) as raw:
        assert tuple(raw.iterdump()) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("root", [1, True, 1, 0o40700]),
        ("parent", None),
        ("attempt_digest", "different"),
        ("command", "different"),
        ("operation", "different"),
        ("reservation", None),
        ("unexpected", 1),
    ],
)
def test_startup_strictly_validates_original_admission_identity(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    tree = workspace(sqlite_tmp_path, "generic")
    write = SQLiteProgramStore._write_work_record  # pyright: ignore[reportPrivateUsage]

    def corrupt(store: SQLiteProgramStore, kind: str, identity: str, fields: dict[str, object]) -> None:
        write(store, kind, identity, {**fields, field: value} if kind == "ownership-workspace" else fields)

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteProgramStore, "_write_work_record", corrupt)
        with SQLiteProgramStore(path, boot_clock=Clock()) as store:
            initialize(store)
            prepare_generic(store, "program-1", "generic", str(tree.path))
    with pytest.raises(CorruptHistory):
        SQLiteProgramStore(path, boot_clock=Clock())


def test_identity_write_failure_rolls_back_preparation_intent_and_capacity(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = sqlite_tmp_path / "kernel.sqlite3"
    tree = workspace(sqlite_tmp_path, "generic")
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store)
        before = store.load("program-1"), store.operations(), store.work_budget_status("program-1")
        write = SQLiteProgramStore._write_work_record  # pyright: ignore[reportPrivateUsage]

        def fail(owner: SQLiteProgramStore, kind: str, identity: str, fields: dict[str, object]) -> None:
            if kind == "ownership-workspace":
                raise OSError("synthetic identity persistence failure")
            write(owner, kind, identity, fields)

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteProgramStore, "_write_work_record", fail)
            with pytest.raises(OSError, match="identity persistence"):
                prepare_generic(store, "program-1", "generic", str(tree.path))
        assert (store.load("program-1"), store.operations(), store.work_budget_status("program-1")) == before
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        prepare_generic(store, "program-1", "generic", str(tree.path))
        assert len(store.operations("work-budget:v1:ownership-workspace:")) == 1


def test_absent_first_admission_never_promotes_a_later_directory_into_original_identity(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "independent")
    future = tree.path.with_name("future")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-1", "future", str(future))
        original = store.operations("work-budget:v1:ownership-workspace:")
        assert json.loads(original[0].request_json)["request"]["root"] is None
        future.mkdir()
        with pytest.raises(OwnershipConflict, match="identity/custody unknown"):
            prepare_generic(store, "program-2", "independent", str(tree.path))
        assert store.operations("work-budget:v1:ownership-workspace:") == original
        store.record_work_settlement("runtime:future", "trusted:original-scope-reconciled")
        prepare_generic(store, "program-2", "independent", str(tree.path))
