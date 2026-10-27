# SPDX-License-Identifier: Apache-2.0
"""Logical completion, resource proof and count-only recovery compose independently."""

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_codex_stdio import FAKE
from test_sqlite_store import OWNER, VERIFIER, WORKER, spec
from test_work_budget import Clock, initialize, policy
from test_work_ownership import claim, client, prepare_generic, workspace

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
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
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict
from creatidy_kernel.core.verification import Evidence, EvidenceSubject
from creatidy_kernel.core.work_budget import BudgetRefused, WorkKind
from creatidy_kernel.ports.application import BudgetedChecks
from creatidy_kernel.ports.ownership import WorkOwnership
from tools.check_schema_upgrade import CREATE_LOGICAL_GENERIC, PINNED_PREQUALIFICATION_ID, install_pinned_reader

pytest_plugins = ["test_sqlite_store"]


def finish_and_accept(store: SQLiteProgramStore, program_id: str, attempt_id: str) -> None:
    program = store.load(program_id)
    if program.attempt(attempt_id).status.value == "prepared":
        program = store.admit(program_id, f"start:{attempt_id}", StartAttempt(program.revision, WORKER, attempt_id))
    program = store.admit(program_id, f"finish:{attempt_id}", FinishAttempt(program.revision, WORKER, attempt_id))
    unit = program.spec.work_unit("first")
    satisfaction = TrustedSatisfaction(
        f"logical:owned-group-settled:{attempt_id}",
        program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.spec.work_unit_applicability_fingerprint("first"),
        VERIFIER,
        attempt_id,
        tuple(InputBinding(name, f"artifact:{attempt_id}:{name}") for name in sorted(unit.outputs)),
    )
    store.admit(program_id, f"accept:{attempt_id}", SatisfyWorkUnit(program.revision, VERIFIER, satisfaction))


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("successor", ["claim", "generic"])
def test_logically_accepted_generic_does_not_prove_workspace_settlement(
    sqlite_tmp_path: Path,
    reopen: bool,
    successor: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    tree = workspace(sqlite_tmp_path, "one")
    store = SQLiteProgramStore(path, boot_clock=clock)
    try:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-1", "generic", str(tree.path))
        finish_and_accept(store, "program-1", "generic")
        capacity = store.operations("work-budget:v1:settlement:")
        assert len(capacity) == 1
        if reopen:
            store.close()
            store = SQLiteProgramStore(path, boot_clock=clock)
        before = store.load("program-2"), store.work_budget_status("program-2"), store.operations()
        with pytest.raises(OwnershipConflict, match="settlement|proof"):
            if successor == "claim":
                claim(store, client(store, tree), tree, "program-2", "successor")
            else:
                prepare_generic(store, "program-2", "successor", str(tree.path))
        assert (store.load("program-2"), store.work_budget_status("program-2"), store.operations()) == before
        store.record_work_settlement("runtime:generic", "trusted:exact-task-scope-quiescent")
        assert store.operations("work-budget:v1:settlement:") == capacity
        store.close()
        store = SQLiteProgramStore(path, boot_clock=clock)
        assert store.operations("work-budget:v1:settlement:") == capacity
        if successor == "claim":
            assert claim(store, client(store, tree), tree, "program-2", "successor").generation == 1
        else:
            prepare_generic(store, "program-2", "successor", str(tree.path))
    finally:
        store.close()


def test_retried_managed_claim_completes_without_receipts_for_retry_counters(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    tree = workspace(sqlite_tmp_path, "one")
    binary = sqlite_tmp_path / "synthetic-app-server"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
        initialize(store)
        connection = CodexStdio(
            (str(binary), "app-server"), "0.99.1", environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath}
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
            control = WorkOwnership(
                store,
                Principal(OWNER, "owner"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            original = claim(store, app, tree)
            app.witness(original, "start")
            program = store.load("program-1")
            store.admit("program-1", "start:a1", StartAttempt(program.revision, WORKER, "a1"))
            first = control.dispatch(original, original.operation_id, lease_seconds=1)
            store.record_transport(original.operation_id, first, False)
            store.observe(original.operation_id, first, "first:unknown", "unknown")
            clock.reading = BootClockReading(102, 3_000_000_000, "synthetic-boot-a")
            operation = store.operation(original.operation_id)
            store.reconcile(
                original.operation_id,
                first,
                "idempotent",
                now=102,
                evidence="trusted:original-key-retry",
                matched_digest=operation.request_digest,
            )
            assert control.dispatch(original, original.operation_id, lease_seconds=1) == 2
            connection.close()
            app.witness(original, "complete")
            assert app.submit(original) == tree.capture()[0]
            finish_and_accept(store, "program-1", "a1")
            control.release(original, evidence="owner:exact-settled-scope-retained")
            reservations = cast(list[dict[str, object]], store.work_budget_status("program-1")["reservations"])
            counters = [item for item in reservations if item["category"] == WorkKind.RETRY.value]
            assert len(counters) == 1
            with pytest.raises(OperationConflict):
                store.operation(cast(str, counters[0]["operation"]))
            assert not any(
                json.loads(item.request_json)["request"].get("operation") == counters[0]["operation"]
                for item in store.operations("work-budget:v1:settlement:")
            )
            with pytest.raises(BudgetRefused, match="count-only"):
                store.record_work_settlement(cast(str, counters[0]["operation"]), "not-an-effect-receipt")
        finally:
            connection.close()


def test_claimed_remediation_completes_without_receipt_for_remediation_counter(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        app, control = client(store, tree), client(store, tree, owner=True)
        first = claim(store, app, tree)
        program = store.load("program-1")
        program = store.admit("program-1", "start:a1", StartAttempt(program.revision, WORKER, "a1"))
        app.quiesce(first, "client:original-task-quiescent")
        program = store.admit("program-1", "finish:a1", FinishAttempt(program.revision, WORKER, "a1"))
        control.release(first, evidence="owner:original-settled-resource-retained")
        remedy = AttemptSpec(
            "remedy",
            "program-1",
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            workspace_reference=str(tree.path),
            remediation_of="a1",
        )
        remediated = app.claim("claim:remedy", program.revision, remedy, lease_seconds=5)
        program = store.load("program-1")
        store.admit("program-1", "start:remedy", StartAttempt(program.revision, WORKER, "remedy"))
        app.quiesce(remediated, "client:remediation-task-quiescent")
        assert app.submit(remediated) == tree.capture()[0]
        finish_and_accept(store, "program-1", "remedy")
        control.release(remediated, evidence="owner:remediation-settled-resource-retained")
        reservations = cast(list[dict[str, object]], store.work_budget_status("program-1")["reservations"])
        counters = [item for item in reservations if item["category"] == WorkKind.REMEDIATION.value]
        assert len(counters) == 1
        with pytest.raises(OperationConflict):
            store.operation(cast(str, counters[0]["operation"]))


class InterruptedChecks:
    def __init__(self, *, interrupt: bool) -> None:
        self.calls = 0
        self.interrupt = interrupt

    def check(self, name: str, subject: EvidenceSubject) -> Evidence:
        self.calls += 1
        if self.interrupt:
            raise RuntimeError("synthetic interruption before durable verification result")
        return Evidence(
            "observed:check", name, subject, "trusted:deterministic", "v1", "artifact:check", 100, 110, True
        )


@pytest.mark.parametrize("late", ["fresh", "expiry", "rollback"])
def test_actual_budgeted_checks_recovers_interrupted_physical_workspace_after_reopen(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    late: str,
) -> None:
    path, clock = sqlite_tmp_path / "kernel.sqlite3", Clock()
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        initialize(store)
        attempt = prepare_generic(store, "program-1", "generic", str(tree.path))
        program = store.load("program-1")
        program = store.admit("program-1", "start:generic", StartAttempt(program.revision, WORKER, "generic"))
        store.admit("program-1", "finish:generic", FinishAttempt(program.revision, WORKER, "generic"))
        subject = EvidenceSubject(
            "candidate:exact",
            attempt.digest,
            attempt.spec_digest,
            "policy:exact",
            "repo:synthetic",
            "base:exact",
            "head:exact",
        )
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            BudgetedChecks(store, InterruptedChecks(interrupt=True), "program-1", "generic", lambda: 100).check(
                "tests", subject
            )
        operation = store.operations("verification:generic:")[0]
        assert operation.status == "dispatched" and operation.attempts == 1
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        checks = InterruptedChecks(interrupt=False)
        actual_retry, actual_resolve = SQLiteProgramStore.retry_work, Path.resolve
        retrying = False
        advanced = False

        def retry(owner: SQLiteProgramStore, operation_id: str) -> None:
            nonlocal retrying
            retrying = True
            try:
                actual_retry(owner, operation_id)
            finally:
                retrying = False

        def resolve(directory: Path, strict: bool = False) -> Path:
            nonlocal advanced
            result = actual_resolve(directory, strict=strict)
            if retrying and directory == tree.path and not advanced:
                advanced = True
                clock.reading = BootClockReading(
                    100,
                    {"fresh": 1_000_000_000, "expiry": 21_000_000_000, "rollback": 500_000_000}[late],
                    "synthetic-boot-a",
                )
            return result

        monkeypatch.setattr(SQLiteProgramStore, "retry_work", retry)
        monkeypatch.setattr(Path, "resolve", resolve)
        recovery = BudgetedChecks(store, checks, "program-1", "generic", lambda: 100)
        before = store.work_budget_status("program-1")["reservations"]
        if late == "fresh":
            assert recovery.check("tests", subject) is not None
            assert checks.calls == 1
            assert recovery.check("tests", subject) is not None
            assert checks.calls == 1
            counters = [
                item
                for item in cast(list[dict[str, object]], store.work_budget_status("program-1")["reservations"])
                if item["category"] == WorkKind.RETRY.value
            ]
            assert len(counters) == 1
        else:
            with pytest.raises(BudgetRefused):
                recovery.check("tests", subject)
            assert checks.calls == 0
            assert store.work_budget_status("program-1")["reservations"] == before
        assert advanced
        assert store.operation(operation.operation_id).attempts == 1


@pytest.mark.parametrize("kind", [WorkKind.TOOL, WorkKind.AUXILIARY])
def test_logical_effect_completion_cannot_release_unknown_real_effects(sqlite_tmp_path: Path, kind: WorkKind) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        basic = policy()
        counts = tuple(
            (item, 2 if item in {WorkKind.TOOL, WorkKind.AUXILIARY} else count) for item, count in basic.counts
        )
        configured = replace(basic, counts=counts)
        program = store.create(spec(), "create")
        store.bind_work_budget("program-1", "project", configured, configured, 110)
        store.admit("program-1", "activate", ActivateProgram(program.revision, OWNER))
        app, control = client(store, tree), client(store, tree, owner=True)
        original = claim(store, app, tree)
        store.budgeted_intent("program-1", "a1", kind, "real-effect", "real-effect", {"kind": kind.value})
        app.quiesce(original, "client:task-scope-quiescent")
        store.record_work_completion("real-effect", "logical:accepted")
        for action in (
            lambda: app.submit(original),
            lambda: control.release(original, evidence="not-physical-proof"),
            lambda: control.cleanup(original, evidence="not-physical-proof"),
        ):
            with pytest.raises(OwnershipConflict, match="unreconciled effect"):
                action()
        store.record_work_settlement("real-effect", "trusted:exact-effect-reconciled", expected_fence=0)
        assert app.submit(original) == tree.capture()[0]
        control.release(original, evidence="owner:all-real-effects-reconciled")


def test_qualified_settlement_does_not_authorize_different_scope_or_stale_fence(sqlite_tmp_path: Path) -> None:
    tree, other = workspace(sqlite_tmp_path, "one"), workspace(sqlite_tmp_path, "other")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        initialize(store, "program-3", project=policy(active=4))
        prepare_generic(store, "program-1", "generic", str(tree.path))
        prepare_generic(store, "program-2", "other-generic", str(other.path))
        finish_and_accept(store, "program-1", "generic")
        store.record_work_settlement("runtime:other-generic", "trusted:other-scope-closed", expected_fence=0)
        with pytest.raises(OwnershipConflict, match="settlement|proof"):
            claim(store, client(store, tree), tree, "program-3", "successor")
        with pytest.raises(OwnershipConflict, match="fence"):
            store.record_work_settlement("runtime:generic", "trusted:stale-closure", expected_fence=1)
        assert not any(
            json.loads(item.request_json)["request"]["operation"] == "runtime:generic"
            and json.loads(item.request_json)["request"]["basis"] == "trusted-profile"
            for item in store.operations("work-budget:v1:ownership-settlement:")
        )
        store.record_work_settlement("runtime:generic", "trusted:exact-scope-closed", expected_fence=0)
        assert claim(store, client(store, tree), tree, "program-3", "successor").generation == 1


def test_qualified_absence_cannot_be_reused_for_later_dispatch(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store)
        prepare_generic(store, "program-1", "generic", str(tree.path))
        store.record_work_settlement("runtime:generic", "trusted:scope-closed-before-dispatch", expected_fence=0)
        with pytest.raises(OwnershipConflict, match="closes this operation"):
            store.claim("runtime:generic", now=100, lease_seconds=1)
        assert store.operation("runtime:generic").attempts == 0


@pytest.fixture(scope="module")
def unqualified_reader(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return install_pinned_reader(
        tmp_path_factory.mktemp("actual-unqualified-reader"), commit_id=PINNED_PREQUALIFICATION_ID
    )


def test_actual_prior_untyped_logical_completion_is_not_promoted_on_reopen(
    sqlite_tmp_path: Path,
    unqualified_reader: Path,
) -> None:
    subprocess.run(  # noqa: S603 - exact public producer, synthetic closed fixture.
        [str(unqualified_reader), "-I", "-c", CREATE_LOGICAL_GENERIC, str(sqlite_tmp_path)],
        check=True,
    )
    path = sqlite_tmp_path / "kernel.sqlite3"
    with closing(sqlite3.connect(path)) as old:
        before = tuple(old.iterdump())
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        tree = LocalOwnedWorkspace(sqlite_tmp_path / "workspaces", "one")
        app = WorkOwnership(
            store, Principal("worker", "worker"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        program = store.load("successor")
        attempt = AttemptSpec(
            "next",
            "successor",
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            workspace_reference=str(tree.path),
        )
        with pytest.raises(OwnershipConflict, match="settlement|proof"):
            app.claim("claim:next", program.revision, attempt, lease_seconds=5)
        assert not store.operations("work-budget:v1:ownership-settlement:")
    with closing(sqlite3.connect(path)) as current:
        assert tuple(current.iterdump()) == before
