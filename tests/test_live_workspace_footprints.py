# SPDX-License-Identifier: Apache-2.0
"""Live invalid subjects retain their footprint without revoking independent work."""

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_clock_transaction_boundaries import connection
from test_codex_stdio import FAKE
from test_ownership_acceptance import acceptance, finish_generic
from test_ownership_composition import finish_and_accept
from test_sqlite_store import OWNER, WORKER, spec
from test_work_budget import Clock, initialize, policy
from test_work_ownership import claim, client, prepare_generic, workspace

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.sqlite_store import CorruptHistory, ProgramReadChanged, SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    FinishAttempt,
    PrepareAttempt,
    ProgramSpec,
    StartAttempt,
)
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict, ResourceScope, WorkClaim
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.ownership import WorkOwnership
from tools.check_schema_upgrade import (
    CREATE_LOGICAL_GENERIC,
    CREATE_ORIGINAL_CLAIM,
    PINNED_PRE_WORKSPACE_ID,
    install_pinned_reader,
)

pytest_plugins = ["test_sqlite_store", "test_clock_transaction_boundaries"]


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("producer", ["generic", "owned"])
@pytest.mark.parametrize("entry", ["new-owned", "prepared-owned", "new-generic", "prepared-generic"])
def test_live_generic_alias_retains_a_and_r_without_globally_blocking_b(
    sqlite_tmp_path: Path,
    entry: str,
    reopen: bool,
    producer: str,
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
        if producer == "owned":
            claim(store, client(store, old), old, attempt_id="live-old")
        else:
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
@pytest.mark.parametrize("producer", ["generic", "owned"])
@pytest.mark.parametrize("entry", ["new-owned", "prepared-owned", "new-generic", "prepared-generic"])
@pytest.mark.parametrize("change", ["different", "missing", "cycle", "canonical-replaced", "moved-into-b", "custody"])
def test_unknown_original_identity_never_adopts_current_alias_or_parent_only_hold(
    sqlite_tmp_path: Path,
    entry: str,
    reopen: bool,
    change: str,
    producer: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    (old.path / "manual").write_bytes(b"unknown is not released or adopted")
    store = SQLiteProgramStore(path, boot_clock=clock)
    try:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        if producer == "owned":
            claim(store, client(store, old), old, attempt_id="old")
        else:
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
@pytest.mark.parametrize("producer", ["generic", "owned"])
def test_inode_and_ancestor_facts_detect_nonlexical_conflicts_without_mount_privileges(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relation: str,
    producer: str,
) -> None:
    """Synthetic bind-shaped metadata; ordinary aliases are exercised with real symlinks above."""
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        if producer == "owned":
            claim(store, client(store, old), old, attempt_id="old")
        else:
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
@pytest.mark.parametrize("producer", ["generic", "owned"])
def test_identity_preflight_finishes_before_durable_clock_and_rejects_changed_cut(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    producer: str,
) -> None:
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        if producer == "owned":
            claim(store, client(store, old), old, attempt_id="old")
        else:
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


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("state", ["live", "expired", "revoked", "quarantined", "settled"])
@pytest.mark.parametrize("entry", ["generic-root", "generic-child", "owned-child", "typed-root"])
def test_unreleased_owned_alias_holds_original_inode_for_every_competing_entrance(
    sqlite_tmp_path: Path,
    reopen: bool,
    state: str,
    entry: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    old = workspace(sqlite_tmp_path, "old")
    (old.path / "manual").write_bytes(b"owned original bytes remain reserved")
    store = SQLiteProgramStore(path, boot_clock=clock)
    try:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        app = client(store, old)
        original = claim(store, app, old, attempt_id="old", lease=1)
        if state == "settled":
            program = store.load("program-1")
            store.admit("program-1", "start:old", StartAttempt(program.revision, WORKER, "old"))
            app.quiesce(original, "client:exact-owned-scope-quiescent")
            app.submit(original)
            finish_and_accept(store, "program-1", "old")
        elif state == "quarantined":
            client(store, old, owner=True).quarantine(original, evidence="owner:scope-uncertain")
        elif state == "revoked":
            store.revoke_work_budget("program-1", "owner:revoked")
        elif state == "expired":
            clock.reading = BootClockReading(102, 3_000_000_000, "synthetic-boot-a")
        held = store.load("program-1"), store.work_budget_status("program-1"), store.operation(original.operation_id)
        retained = old.path.with_name("retained")
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        if reopen:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=clock)
        nested = LocalOwnedWorkspace(retained, "nested", create=True) if entry == "owned-child" else None
        before = store.operations(), store.load("program-2"), store.work_budget_status("program-2")
        with pytest.raises(OwnershipConflict):
            if nested is not None:
                claim(store, client(store, nested), nested, "program-2", "competing")
            elif entry == "typed-root":
                program = store.load("program-2")
                attempt = AttemptSpec(
                    "competing",
                    "program-2",
                    "first",
                    program.spec.revision,
                    program.spec.digest,
                    program.resolved_inputs("first"),
                    workspace_reference=str(retained),
                )
                snapshot = b"trusted store caller's exact canonical target, not a LocalOwnedWorkspace adoption"
                store.claim_work(
                    "claim:competing",
                    WORKER,
                    program.revision,
                    attempt,
                    HandoffMode.PULL,
                    COOPERATIVE_PROFILE,
                    (ResourceScope("workspace", str(retained)),),
                    hashlib.sha256(snapshot).hexdigest(),
                    snapshot,
                    5,
                )
            else:
                target = retained / "child" if entry == "generic-child" else retained
                prepare_generic(store, "program-2", "competing", str(target))
        assert (store.operations(), store.load("program-2"), store.work_budget_status("program-2")) == before
        assert (
            store.load("program-1"),
            store.work_budget_status("program-1"),
            store.operation(original.operation_id),
        ) == held
        assert (retained / "manual").read_bytes() == b"owned original bytes remain reserved"
        assert not store.operations("work-budget:v1:ownership-release:")
    finally:
        store.close()


@pytest.mark.parametrize("entry", ["renew", "reserve", "candidate", "accept", "publication", "dispatch"])
def test_prepared_owned_b_cannot_progress_when_original_a_is_moved_into_its_tree(
    sqlite_tmp_path: Path,
    entry: str,
) -> None:
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        original = claim(store, client(store, old), old, attempt_id="old")
        app = client(store, independent)
        claimed = claim(store, app, independent, "program-2", "independent")
        program = store.load("program-2")
        store.admit("program-2", "start:independent", StartAttempt(program.revision, WORKER, "independent"))
        app.quiesce(claimed, "client:original-b-scope-quiescent")
        subject = app.submit(claimed)
        program = store.load("program-2")
        store.admit("program-2", "finish:independent", FinishAttempt(program.revision, WORKER, "independent"))
        if entry in {"publication", "dispatch"}:
            store.admit("program-2", "accept:independent", acceptance(store, "independent", program_id="program-2"))
        held = store.operations(), store.load("program-1"), store.load("program-2")
        retained = independent.path / "moved-original"
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        with pytest.raises(OwnershipConflict, match="identity/custody|footprint|inode"):
            if entry == "renew":
                store.renew_work_claim("renew:independent", claimed, 7)
            elif entry == "reserve":
                store.reserve_work("program-2", "independent", "verification:independent", WorkKind.VERIFICATION)
            elif entry == "candidate":
                store.check_claim_candidate(claimed, subject)
            elif entry == "accept":
                store.admit("program-2", "accept:independent", acceptance(store, "independent", program_id="program-2"))
            elif entry == "publication":
                store.authorize_claim_publication(claimed, OWNER, subject, "publish", "publish", {})
            else:
                store.check_work_dispatch(claimed.operation_id)
        assert (store.operations(), store.load("program-1"), store.load("program-2")) == held
        assert not store.operations("work-budget:v1:ownership-release:")
        assert store.operation(original.operation_id).attempts == 0


@pytest.mark.parametrize("evidence", ["original-capture", "unavailable", "opaque"])
def test_actual_prior_owned_capture_is_original_evidence_not_a_current_path_rebinding(
    sqlite_tmp_path: Path,
    prior_identity_reader: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
) -> None:
    subprocess.run(  # noqa: S603 - actual pinned schema-5 producer with synthetic capture.
        [str(prior_identity_reader), "-I", "-c", CREATE_ORIGINAL_CLAIM, str(sqlite_tmp_path), "edited"],
        check=True,
    )
    path = sqlite_tmp_path / "kernel.sqlite3"
    old = sqlite_tmp_path / "workspaces" / "one"
    retained = old.with_name("retained")
    old.rename(retained)
    old.symlink_to(retained, target_is_directory=True)
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        initialize(store, "program-2", project=policy(active=4))
        independent = workspace(sqlite_tmp_path, "independent")
        held = store.load("reference"), store.work_budget_status("reference"), store.operations()
        baseline = store.artifact("runtime:original", "ownership-baseline")
        read = store.find_artifact
        events: list[str] = []

        def read_original(operation_id: str, name: str) -> bytes | None:
            assert not connection(store).in_transaction
            events.append("artifact")
            if operation_id == "runtime:original" and name == "ownership-baseline":
                return (
                    None
                    if evidence == "unavailable"
                    else b"not original capture"
                    if evidence == "opaque"
                    else read(operation_id, name)
                )
            return read(operation_id, name)

        def sample() -> BootClockReading:
            events.append("clock")
            return BootClockReading(100, 1_000_000_000, "synthetic-boot-a")

        monkeypatch.setattr(store, "find_artifact", read_original)
        monkeypatch.setattr(store, "_boot_clock", sample)
        if evidence == "original-capture":
            prepare_generic(store, "program-2", "independent", str(independent.path))
            assert store.claim("runtime:independent", now=100, lease_seconds=1) == 1
            with pytest.raises(OwnershipConflict, match="original-inode"):
                prepare_generic(store, "program-2", "unsafe", str(retained))
        else:
            with pytest.raises(OwnershipConflict, match="identity/custody unknown"):
                prepare_generic(store, "program-2", "independent", str(independent.path))
            assert store.operations() == held[2]
        assert events[-1] == "clock" and "artifact" in events
        assert (store.load("reference"), store.work_budget_status("reference")) == held[:2]
        for operation in held[2]:
            assert store.operation(operation.operation_id) == operation
        assert not any(
            json.loads(item.request_json)["request"]["program"] == "reference"
            for item in store.operations("work-budget:v1:ownership-workspace:")
        )
        assert read("runtime:original", "ownership-baseline") == baseline
        assert (retained / "manual").read_bytes() == b"synthetic manually retained bytes"


@pytest.mark.parametrize("cold", [False, True])
def test_original_native_group_hold_survives_alias_without_a_live_profile_reproducer(
    sqlite_tmp_path: Path,
    cold: bool,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    binary = sqlite_tmp_path / "synthetic-app-server"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    native = CodexStdio(
        (str(binary), "app-server"), "0.99.1", environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath}
    )
    store = SQLiteProgramStore(path, boot_clock=clock)
    try:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        witness = CodexOwnedSessionWitness(native)
        effect = ResourceScope("effect", "original-effect")
        app = WorkOwnership(
            store,
            Principal(WORKER, "worker"),
            old,
            mode=HandoffMode.MANAGED,
            profile=witness.profile,
            resources=(witness.resource, effect),
            witness=witness,
        )
        original = claim(store, app, old, attempt_id="old")
        app.witness(original, "start")
        if cold:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=clock)
            app = WorkOwnership(
                store,
                Principal(WORKER, "worker"),
                old,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource, effect),
            )
        retained = old.path.with_name("retained")
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        held = store.operations(), store.work_budget_status("program-1")
        for resource in (witness.resource, effect):
            with pytest.raises(OwnershipConflict, match="workspace/session/effect"):
                claim(store, client(store, independent, resources=(resource,)), independent, "program-2", "unsafe")
        with pytest.raises(OwnershipConflict):
            app.witness(original, "complete")
        with pytest.raises(OwnershipConflict, match="original-inode"):
            prepare_generic(store, "program-2", "unsafe", str(retained))
        assert (store.operations(), store.work_budget_status("program-1")) == held
        assert app.claim("claim:old", 1, original.attempt, lease_seconds=5) == original
        independent_claim = claim(store, client(store, independent), independent, "program-2", "independent")
        assert client(store, independent).renew("renew:independent", independent_claim, lease_seconds=7) == 107
        assert not store.operations("work-budget:v1:ownership-release:")
    finally:
        store.close()
        native.close()


@pytest.mark.parametrize("state", ["expired", "revoked", "quarantined"])
def test_independent_owned_b_delivers_and_accepts_while_a_retains_an_inactive_alias_hold(
    sqlite_tmp_path: Path,
    state: str,
) -> None:
    clock = Clock()
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        original = claim(store, client(store, old), old, attempt_id="old", lease=1)
        if state == "expired":
            clock.reading = BootClockReading(102, 3_000_000_000, "synthetic-boot-a")
        elif state == "revoked":
            store.revoke_work_budget("program-1", "owner:revoked-original")
        else:
            client(store, old, owner=True).quarantine(original, evidence="owner:original-scope-unknown")
        held = store.load("program-1"), store.work_budget_status("program-1"), store.operation(original.operation_id)
        retained = old.path.with_name("retained")
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        worker = client(store, independent)
        claimed = claim(store, worker, independent, "program-2", "independent")
        assert worker.renew("renew:independent", claimed, lease_seconds=7) == (109 if state == "expired" else 107)
        program = store.load("program-2")
        store.admit("program-2", "start:independent", StartAttempt(program.revision, WORKER, "independent"))
        worker.quiesce(claimed, "client:independent-scope-quiescent")
        worker.submit(claimed)
        program = store.load("program-2")
        store.admit("program-2", "finish:independent", FinishAttempt(program.revision, WORKER, "independent"))
        store.budgeted_intent("program-2", "independent", WorkKind.VERIFICATION, "check", "check", {})
        assert client(store, independent, owner=True).dispatch(claimed, "check", lease_seconds=1) == 1
        store.record_work_settlement("check", "trusted:synthetic-independent-check-closed", expected_fence=1)
        store.admit("program-2", "accept:independent", acceptance(store, "independent", program_id="program-2"))
        assert (
            store.load("program-1"),
            store.work_budget_status("program-1"),
            store.operation(original.operation_id),
        ) == held
        assert not store.operations("work-budget:v1:ownership-release:")


def test_exact_retirement_read_receipt_does_not_restore_worker_capability_or_release_original_path(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        worker = client(store, old)
        original = claim(store, worker, old, attempt_id="old")
        worker.quiesce(original, "client:exact-original-scope-quiescent")
        write = SQLiteProgramStore._write_work_record  # pyright: ignore[reportPrivateUsage]

        def fail(owner: SQLiteProgramStore, kind: str, identity: str, fields: dict[str, object]) -> None:
            if kind == "ownership-release":
                raise OSError("synthetic release failure")
            write(owner, kind, identity, fields)

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteProgramStore, "_write_work_record", fail)
            with pytest.raises(OSError, match="release failure"):
                client(store, old, owner=True).cleanup(original, evidence="owner:exact-original-empty-retirement")
        assert old.retirement() is not None and not old.path.exists()
        held = store.operations(), store.work_budget_status("program-1")
        store.check_work_claim(original, settled=True)
        with pytest.raises(OwnershipConflict, match="identity/custody"):
            store.renew_work_claim("renew:old", original, 7)
        with pytest.raises(OwnershipConflict, match="identity/custody"):
            store.reserve_work("program-1", "old", "check:old", WorkKind.VERIFICATION)
        with pytest.raises(OwnershipConflict, match="unreleased"):
            prepare_generic(store, "program-2", "unsafe", str(old.path))
        assert (store.operations(), store.work_budget_status("program-1")) == held
        claim(store, client(store, independent), independent, "program-2", "independent")
        assert not store.operations("work-budget:v1:ownership-release:")


@pytest.mark.parametrize("before_claim", [False, True])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("entry", ["dispatch", "reserve", "start"])
def test_prepared_future_name_cannot_dispatch_into_later_observed_owned_alias(
    sqlite_tmp_path: Path,
    before_claim: bool,
    reopen: bool,
    entry: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    old = workspace(sqlite_tmp_path, "old")
    retained = old.path.with_name("future")
    store = SQLiteProgramStore(path, boot_clock=clock)
    try:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        if before_claim:
            prepare_generic(store, "program-2", "future", str(retained))
        original = claim(store, client(store, old), old, attempt_id="old")
        if not before_claim:
            prepare_generic(store, "program-2", "future", str(retained))
        held = store.work_budget_status("program-1"), store.operation(original.operation_id)
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        if reopen:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=clock)
        before = store.operations(), store.load("program-2"), store.work_budget_status("program-2")
        with pytest.raises(OwnershipConflict):
            if entry == "dispatch":
                store.check_work_dispatch("runtime:future")
            elif entry == "reserve":
                store.reserve_work("program-2", "future", "verification:future", WorkKind.VERIFICATION)
            else:
                program = store.load("program-2")
                store.admit("program-2", "start:future", StartAttempt(program.revision, WORKER, "future"))
        with pytest.raises(OwnershipConflict):
            store.claim("runtime:future", now=100, lease_seconds=1)
        assert (store.operations(), store.load("program-2"), store.work_budget_status("program-2")) == before
        assert (store.work_budget_status("program-1"), store.operation(original.operation_id)) == held
    finally:
        store.close()
