# SPDX-License-Identifier: Apache-2.0
"""Installed application conformance: real SQLite/local handlers, no harness/provider."""

import json
import os
import sys
from pathlib import Path
from typing import cast
from unittest.mock import patch

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    FinishAttempt,
    InputBinding,
    PrepareAttempt,
    ProgramSpec,
    SatisfyWorkUnit,
    StartAttempt,
    TrustedSatisfaction,
)
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.ownership import WorkOwnership


def require(condition: object) -> None:
    if not condition:
        raise AssertionError("installed ownership invariant failed")


def settlement_conformance(directory: Path, binary: Path) -> None:
    parent = directory / "proof-workspaces"
    parent.mkdir(mode=0o700)
    tree = LocalOwnedWorkspace(parent, "one", create=True)
    base = reference_spec()
    clock = lambda: BootClockReading(100, 1_000_000_000, "installed-proof-boot")  # noqa: E731
    with SQLiteProgramStore(directory / "proof.sqlite3", boot_clock=clock) as store:
        budget = WorkBudgetPolicy.basic(3, 3, verifications=3, retained_bytes=16384, auxiliary=2)
        for name in ("logical", "next"):
            spec = ProgramSpec(
                name,
                base.objective,
                base.work_units,
                base.initial_inputs,
                base.budget,
                base.authority,
                acceptance_criteria=base.acceptance_criteria,
                policy_references=base.policy_references,
            )
            program = store.create(spec, f"create:{name}")
            store.bind_work_budget(name, "proof-project", budget, budget, 110)
            store.admit(name, f"activate:{name}", ActivateProgram(program.revision, "owner"))
        connection = CodexStdio(
            (str(binary), "app-server"), "0.99.1", environment={"HOME": str(directory), "PATH": os.defpath}
        )
        try:
            program = store.load("logical")
            attempt = AttemptSpec(
                "logical-generic",
                "logical",
                "first",
                program.spec.revision,
                program.spec.digest,
                program.resolved_inputs("first"),
                workspace_reference=str(tree.path),
            )
            program = store.admit_with_intent(
                "logical",
                "prepare:logical",
                PrepareAttempt(program.revision, "worker", attempt),
                "runtime:logical-generic",
                "runtime:logical-generic",
                {"attempt": attempt.digest},
            )
            fence = store.claim("runtime:logical-generic", now=100, lease_seconds=1)
            program = store.admit(
                "logical", "start:logical", StartAttempt(program.revision, "worker", attempt.attempt_id)
            )
            program = store.admit(
                "logical", "finish:logical", FinishAttempt(program.revision, "worker", attempt.attempt_id)
            )
            accepted = TrustedSatisfaction(
                "logical:accepted",
                "logical",
                "first",
                program.spec.revision,
                program.spec.digest,
                program.spec.work_unit_applicability_fingerprint("first"),
                "verifier",
                attempt.attempt_id,
                tuple(
                    InputBinding(name, f"artifact:{name}") for name in sorted(program.spec.work_unit("first").outputs)
                ),
            )
            require(connection.owned_group_active)
            before_acceptance = store.load("logical"), store.work_budget_status("logical"), store.operations()
            try:
                store.admit("logical", "accept:logical", SatisfyWorkUnit(program.revision, "verifier", accepted))
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("active native task group was freshly accepted from logical terminal state")
            require(
                (store.load("logical"), store.work_budget_status("logical"), store.operations()) == before_acceptance
            )
            store.record_work_completion("runtime:logical-generic", "logical:terminal-only")
            program = store.load("next")
            successor = AttemptSpec(
                "next",
                "next",
                "first",
                program.spec.revision,
                program.spec.digest,
                program.resolved_inputs("first"),
                workspace_reference=str(tree.path),
            )
            app = WorkOwnership(
                store, Principal("worker", "worker"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            try:
                app.claim("claim:next", program.revision, successor, lease_seconds=5)
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("logical accepted fact released a generic physical workspace")
            original_capacity = store.operations("work-budget:v1:settlement:")
            operation = store.operation("runtime:logical-generic")
            connection.close()
            require(connection.owned_group_settled is True)
            evidence = store.finalize_artifact(
                operation.operation_id,
                "owned-stop",
                json.dumps(
                    {
                        "operation": operation.operation_id,
                        "request_digest": operation.request_digest,
                        "fence": operation.fence,
                        "attempt": attempt.digest,
                        "workspace": str(tree.path),
                        "owned_group_reference": connection.owned_group_reference,
                        "owned_group_settled": connection.owned_group_settled,
                    },
                    sort_keys=True,
                ).encode(),
            )
            store.record_work_settlement(operation.operation_id, evidence, expected_fence=operation.fence)
            require(store.operations("work-budget:v1:settlement:") == original_capacity)
            current = store.load("logical")
            store.admit("logical", "accept:logical", SatisfyWorkUnit(current.revision, "verifier", accepted))
            store.budgeted_intent(
                "logical",
                attempt.attempt_id,
                WorkKind.AUXILIARY,
                "proof-publication",
                "proof-publication",
                {"synthetic": True},
            )
            require(store.claim("proof-publication", now=100, lease_seconds=1) == 1)
            store.record_work_settlement("proof-publication", "trusted:synthetic-no-external-effect", expected_fence=1)
            require(fence == 1)
            retained = tree.path.with_name("retained-one")
            tree.path.rename(retained)
            tree.path.symlink_to(retained, target_is_directory=True)
            independent = LocalOwnedWorkspace(parent, "independent", create=True)
            next_app = WorkOwnership(
                store, Principal("worker", "worker"), independent, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            successor = AttemptSpec(
                "next",
                "next",
                "first",
                program.spec.revision,
                program.spec.digest,
                program.resolved_inputs("first"),
                workspace_reference=str(independent.path),
            )
            claimed = next_app.claim("claim:next", program.revision, successor, lease_seconds=5)
            require(claimed.generation == 1)
            next_app.quiesce(claimed, "client:independent-task-quiescent")
            require(next_app.submit(claimed) == independent.capture()[0])
            require(tree.path.is_symlink() and retained.is_dir())
        finally:
            connection.close()


def publication_replay_conformance(directory: Path, binary: Path) -> None:
    for mode in (HandoffMode.PULL, HandoffMode.MANAGED):
        parent = directory / f"publication-{mode.value}"
        parent.mkdir(mode=0o700)
        tree = LocalOwnedWorkspace(parent, "one", create=True)
        path = directory / f"publication-{mode.value}.sqlite3"
        clock = lambda: BootClockReading(100, 1_000_000_000, "installed-publication-boot")  # noqa: E731
        connection = None
        witness = None
        resources = ()
        profile = COOPERATIVE_PROFILE
        if mode is HandoffMode.MANAGED:
            connection = CodexStdio(
                (str(binary), "app-server"), "0.99.1", environment={"HOME": str(directory), "PATH": os.defpath}
            )
            witness = CodexOwnedSessionWitness(connection)
            resources = (witness.resource,)
            profile = witness.profile
        try:
            with SQLiteProgramStore(path, boot_clock=clock) as store:
                base = reference_spec()
                program = store.create(base, "create")
                budget = WorkBudgetPolicy.basic(3, 3, verifications=3, retained_bytes=16384, auxiliary=3)
                store.bind_work_budget(program.program_id, "publication-project", budget, budget, 110)
                program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
                worker = WorkOwnership(
                    store,
                    Principal("worker", "worker"),
                    tree,
                    mode=mode,
                    profile=profile,
                    resources=resources,
                    witness=witness,
                )
                owner = WorkOwnership(
                    store,
                    Principal("owner", "owner"),
                    tree,
                    mode=mode,
                    profile=profile,
                    resources=resources,
                    witness=witness,
                )
                attempt = AttemptSpec(
                    "publication",
                    program.program_id,
                    "first",
                    program.spec.revision,
                    program.spec.digest,
                    program.resolved_inputs("first"),
                    workspace_reference=str(tree.path),
                )
                claimed = worker.claim("claim:publication", program.revision, attempt, lease_seconds=5)
                if mode is HandoffMode.MANAGED:
                    worker.witness(claimed, "start")
                program = store.load(program.program_id)
                store.admit(program.program_id, "start", StartAttempt(program.revision, "worker", attempt.attempt_id))
                if connection is not None:
                    connection.close()
                    worker.witness(claimed, "complete")
                else:
                    worker.quiesce(claimed, "client:publication-task-quiescent")
                worker.submit(claimed)
                program = store.load(program.program_id)
                program = store.admit(
                    program.program_id, "finish", FinishAttempt(program.revision, "worker", attempt.attempt_id)
                )
                fact = TrustedSatisfaction(
                    "accepted:publication",
                    program.program_id,
                    "first",
                    program.spec.revision,
                    program.spec.digest,
                    program.spec.work_unit_applicability_fingerprint("first"),
                    "verifier",
                    attempt.attempt_id,
                    tuple(
                        InputBinding(name, f"artifact:{name}")
                        for name in sorted(program.spec.work_unit("first").outputs)
                    ),
                )
                store.admit(program.program_id, "accept", SatisfyWorkUnit(program.revision, "verifier", fact))
                request: dict[str, object] = {"accepted": fact.reference_id}
                authorized = owner.authorize_publication(claimed, "publication", "publication-effect", request)
                before = store.operations(), store.work_budget_status(program.program_id)
                require(
                    owner.authorize_publication(claimed, "publication", "publication-effect", request) == authorized
                )
                require((store.operations(), store.work_budget_status(program.program_id)) == before)
                fence = owner.dispatch(claimed, "publication", lease_seconds=1)
                store.record_transport("publication", fence, True)
                store.observe(
                    "publication", fence, "accepted:publication", "accepted", reference="synthetic:publication"
                )
                store.record_work_settlement("publication", "trusted:synthetic-effect-closed", expected_fence=fence)
                owner.cleanup(claimed, evidence="owner:exact-empty-retirement")
                require(
                    owner.authorize_publication(claimed, "publication", "publication-effect", request)
                    == store.operation("publication")
                )
            with SQLiteProgramStore(path, boot_clock=clock) as store:
                cold = WorkOwnership(
                    store,
                    Principal("owner", "owner"),
                    LocalOwnedWorkspace(parent, "one"),
                    mode=mode,
                    profile=profile,
                    resources=resources,
                )
                before = store.operations(), store.work_budget_status(program.program_id)
                require(
                    cold.authorize_publication(claimed, "publication", "publication-effect", request)
                    == store.operation("publication")
                )
                require((store.operations(), store.work_budget_status(program.program_id)) == before)
        finally:
            if connection is not None:
                connection.close()


def live_alias_conformance(directory: Path, *, owned: bool = False) -> None:
    """Shared handler preserves generic/owned original-inode holds across aliases."""
    label = "live-owned-alias" if owned else "live-generic-alias"
    parent = directory / f"{label}-workspaces"
    parent.mkdir(mode=0o700)
    old = LocalOwnedWorkspace(parent, "old", create=True)
    independent = LocalOwnedWorkspace(parent, "independent", create=True)
    (old.path / "manual").write_bytes(b"installed original manual bytes")
    path = directory / f"{label}.sqlite3"
    clock = lambda: BootClockReading(100, 1_000_000_000, "installed-alias-boot")  # noqa: E731
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        base = reference_spec()
        budget = WorkBudgetPolicy.basic(3, 3, verifications=3, retained_bytes=16384, auxiliary=2)
        for name in ("live-generic", "independent", "competing"):
            spec = ProgramSpec(
                name,
                base.objective,
                base.work_units,
                base.initial_inputs,
                base.budget,
                base.authority,
                acceptance_criteria=base.acceptance_criteria,
                policy_references=base.policy_references,
            )
            program = store.create(spec, f"create:{name}")
            store.bind_work_budget(name, "alias-project", budget, budget, 110)
            store.admit(name, f"activate:{name}", ActivateProgram(program.revision, "owner"))
        program = store.load("live-generic")
        original = AttemptSpec(
            "live-generic",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            workspace_reference=str(old.path),
        )
        if owned:
            app = WorkOwnership(
                store, Principal("worker", "worker"), old, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            app.claim("claim:old", program.revision, original, lease_seconds=5)
        else:
            store.admit_with_intent(
                program.program_id,
                "prepare:old",
                PrepareAttempt(program.revision, "worker", original),
                "runtime:live-generic",
                "runtime:live-generic",
                {"attempt": original.digest},
            )
        retained = old.path.with_name("retained-old")
        old.path.rename(retained)
        old.path.symlink_to(retained, target_is_directory=True)
        held = (
            store.load("live-generic"),
            store.work_budget_status("live-generic"),
            store.operation("runtime:live-generic"),
        )
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        worker = WorkOwnership(
            store, Principal("worker", "worker"), independent, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        program = store.load("independent")
        attempt = AttemptSpec(
            "independent",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            workspace_reference=str(independent.path),
        )
        claimed = worker.claim("claim:independent", program.revision, attempt, lease_seconds=5)
        require(worker.renew("renew:independent", claimed, lease_seconds=7) == 107)
        program = store.load("independent")
        program = store.admit(
            "independent", "start:independent", StartAttempt(program.revision, "worker", attempt.attempt_id)
        )
        worker.quiesce(claimed, "client:independent-task-quiescent")
        worker.submit(claimed)
        program = store.admit(
            "independent", "finish:independent", FinishAttempt(program.revision, "worker", attempt.attempt_id)
        )
        accepted = TrustedSatisfaction(
            "accepted:independent",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            program.spec.work_unit_applicability_fingerprint("first"),
            "verifier",
            attempt.attempt_id,
            tuple(InputBinding(name, "artifact:" + name) for name in sorted(program.spec.work_unit("first").outputs)),
        )
        store.admit("independent", "accept:independent", SatisfyWorkUnit(program.revision, "verifier", accepted))
        owner = WorkOwnership(
            store, Principal("owner", "owner"), independent, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        owner.authorize_publication(
            claimed, "independent-publication", "independent-publication", {"accepted": "independent"}
        )
        require(owner.dispatch(claimed, "independent-publication", lease_seconds=1) == 1)
        require(
            (
                store.load("live-generic"),
                store.work_budget_status("live-generic"),
                store.operation("runtime:live-generic"),
            )
            == held
        )
        for target in (old.path, retained, retained / "child", parent):
            program = store.load("competing")
            competing = AttemptSpec(
                "unsafe",
                program.program_id,
                "first",
                program.spec.revision,
                program.spec.digest,
                program.resolved_inputs("first"),
                workspace_reference=str(target),
            )
            before = store.operations()
            try:
                store.admit(program.program_id, "prepare:unsafe", PrepareAttempt(program.revision, "worker", competing))
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("installed live original/alias/ancestor/descendant hold was bypassed")
            require(store.operations() == before)
        require(old.path.is_symlink() and (retained / "manual").read_bytes() == b"installed original manual bytes")


def fresh_subject_conformance(directory: Path, binary: Path) -> None:
    """Installed store acceptance/effect entrances use actual post-verifier bytes."""
    for mode in (HandoffMode.PULL, HandoffMode.MANAGED):
        for sample in ("source.txt", "verification.txt", None, "configured"):
            configured = sample == "configured"
            if configured and mode is HandoffMode.MANAGED:
                continue  # The byte-limit parameter is producer I/O, not a managed-profile contract.
            filename = None if configured else sample
            label = f"fresh-{mode.value}-{sample or 'positive'}"
            parent = directory / f"{label}-workspaces"
            parent.mkdir(mode=0o700)
            tree = LocalOwnedWorkspace(parent, "one", create=True, max_bytes=(20 if configured else 16) * 1024 * 1024)
            (tree.path / "source.txt").write_bytes(
                b"x" * (17 * 1024 * 1024) if configured else b"installed original source"
            )
            (tree.path / "verification.txt").write_bytes(b"installed original verification")
            native = None
            clock = lambda: BootClockReading(100, 1_000_000_000, "installed-fresh-subject-boot")  # noqa: E731
            try:
                with SQLiteProgramStore(directory / f"{label}.sqlite3", boot_clock=clock) as store:
                    base = reference_spec()
                    program = store.create(base, "create")
                    ceiling = len(tree.capture()[1]) + 4096 if configured else 16384
                    budget = WorkBudgetPolicy.basic(3, 3, verifications=3, retained_bytes=ceiling, auxiliary=3)
                    store.bind_work_budget(base.program_id, "project", budget, budget, 110)
                    program = store.admit(base.program_id, "activate", ActivateProgram(program.revision, "owner"))
                    resources = ()
                    witness = None
                    profile = COOPERATIVE_PROFILE
                    if mode is HandoffMode.MANAGED:
                        native = CodexStdio(
                            (str(binary), "app-server"),
                            "0.99.1",
                            environment={"HOME": str(directory), "PATH": os.defpath},
                        )
                        witness = CodexOwnedSessionWitness(native)
                        resources, profile = (witness.resource,), witness.profile
                    app = WorkOwnership(
                        store,
                        Principal("worker", "worker"),
                        tree,
                        mode=mode,
                        profile=profile,
                        resources=resources,
                        witness=witness,
                    )
                    attempt = AttemptSpec(
                        "original",
                        base.program_id,
                        "first",
                        base.revision,
                        base.digest,
                        program.resolved_inputs("first"),
                        workspace_reference=str(tree.path),
                    )
                    claimed = app.claim("claim:original", program.revision, attempt, lease_seconds=5)
                    if native is not None:
                        app.witness(claimed, "start")
                    program = store.load(base.program_id)
                    store.admit(base.program_id, "start:original", StartAttempt(program.revision, "worker", "original"))
                    if native is not None:
                        native.close()
                        app.witness(claimed, "complete")
                    else:
                        app.quiesce(claimed, "client:installed-original-scope-quiescent")
                    app.submit(claimed)
                    subject = app.candidate_subject(claimed)
                    program = store.load(base.program_id)
                    program = store.admit(
                        base.program_id, "finish:original", FinishAttempt(program.revision, "worker", "original")
                    )
                    fact = TrustedSatisfaction(
                        "accepted:original",
                        base.program_id,
                        "first",
                        base.revision,
                        base.digest,
                        base.work_unit_applicability_fingerprint("first"),
                        "verifier",
                        "original",
                        tuple(
                            InputBinding(name, "artifact:" + name) for name in sorted(base.work_unit("first").outputs)
                        ),
                    )
                    command = SatisfyWorkUnit(program.revision, "verifier", fact)
                    if filename is not None:
                        (tree.path / filename).write_bytes(b"installed post-verifier manual bytes")
                        before = (
                            store.load(base.program_id),
                            store.operations(),
                            store.work_budget_status(base.program_id),
                        )
                        try:
                            store.admit(base.program_id, "accept:original", command)
                        except OwnershipConflict:
                            pass
                        else:
                            raise AssertionError("installed fresh acceptance used a stale verifier subject")
                        require(
                            (store.load(base.program_id), store.operations(), store.work_budget_status(base.program_id))
                            == before
                        )
                        require("second" not in store.load(base.program_id).ready_work_unit_ids)
                        require((tree.path / filename).read_bytes() == b"installed post-verifier manual bytes")
                    else:
                        store.admit(base.program_id, "accept:original", command)
                        require("second" in store.load(base.program_id).ready_work_unit_ids)
                        original = store.authorize_claim_publication(
                            claimed, "owner", subject, "publish", "publish", {}
                        )
                        (tree.path / "verification.txt").write_bytes(b"installed post-authorization manual bytes")
                        before = store.operations()
                        for deliver in (False, True):
                            try:
                                if deliver:
                                    store.claim_owned_operation(claimed, "publish", subject, 1)
                                else:
                                    store.check_work_dispatch("publish")
                            except OwnershipConflict:
                                pass
                            else:
                                raise AssertionError("installed delivery accepted stale authorized workspace bytes")
                        require(store.operations() == before)
                        store.record_work_settlement(
                            "publish", "trusted:installed-undelivered-scope-closed", expected_fence=0
                        )
                        before = store.operations()
                        try:
                            store.authorize_claim_publication(claimed, "owner", subject, "other", "other", {})
                        except OwnershipConflict:
                            pass
                        else:
                            raise AssertionError("installed first owner authorization used stale candidate bytes")
                        require(store.operations() == before)
                        require(store.find_claim_publication(claimed, "owner", "publish", "publish", {}) == original)
            finally:
                if native is not None:
                    native.close()


def main(directory: Path) -> None:
    parent = directory / "workspaces"
    parent.mkdir(mode=0o700)
    one = LocalOwnedWorkspace(parent, "one", create=True)
    two = LocalOwnedWorkspace(parent, "two", create=True)
    policy = WorkBudgetPolicy.basic(4, 3, verifications=3, retained_bytes=16384)
    reading = BootClockReading(100, 1_000_000_000, "installed-synthetic-boot")
    clock = lambda: reading  # noqa: E731
    path = directory / "kernel.sqlite3"
    base = reference_spec()
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        for program_id in ("reference", "independent", "managed"):
            spec = ProgramSpec(
                program_id,
                base.objective,
                base.work_units,
                base.initial_inputs,
                base.budget,
                base.authority,
                acceptance_criteria=base.acceptance_criteria,
                policy_references=base.policy_references,
            )
            program = store.create(spec, f"create:{program_id}")
            store.bind_work_budget(program_id, "installed-project", policy, policy, 110)
            store.admit(program_id, f"activate:{program_id}", ActivateProgram(program.revision, "owner"))

        def app(tree: LocalOwnedWorkspace, *, owner: bool = False) -> WorkOwnership:
            return WorkOwnership(
                store,
                Principal("owner", "owner") if owner else Principal("worker", "worker"),
                tree,
                mode=HandoffMode.PULL,
                profile=COOPERATIVE_PROFILE,
            )

        def attempt(program_id: str, attempt_id: str, tree: LocalOwnedWorkspace) -> AttemptSpec:
            program = store.load(program_id)
            return AttemptSpec(
                attempt_id,
                program_id,
                "first",
                program.spec.revision,
                program.spec.digest,
                program.resolved_inputs("first"),
                workspace_reference=str(tree.path),
            )

        program = store.load("reference")
        original = attempt("reference", "installed-pull", one)
        claimed = app(one).claim("claim:one", program.revision, original, lease_seconds=5)
        require(app(one).claim("claim:one", program.revision, original, lease_seconds=5) == claimed)
        require(app(one).renew("renew:one", claimed, lease_seconds=100) == 110)
        competing = attempt("independent", "generic-conflict", one)
        generic_before = store.load("independent")
        try:
            store.admit_with_intent(
                "independent",
                "prepare:generic-conflict",
                PrepareAttempt(generic_before.revision, "worker", competing),
                "runtime:generic-conflict",
                "runtime:generic-conflict",
                {"attempt": competing.digest},
            )
        except OwnershipConflict:
            pass
        else:
            raise AssertionError("generic typed preparation bypassed installed global workspace ownership")
        require(store.load("independent") == generic_before)
        try:
            store.operation("runtime:generic-conflict")
        except OperationConflict:
            pass
        else:
            raise AssertionError("refused generic preparation retained an executable Operation")
        try:
            app(one).claim(
                "claim:conflict",
                store.load("independent").revision,
                attempt("independent", "conflicting", one),
                lease_seconds=5,
            )
        except OwnershipConflict:
            pass
        else:
            raise AssertionError("installed handler allowed conflicting workspace mutation ownership")
        independent_revision = store.load("independent").revision
        independent_attempt = attempt("independent", "independent-pull", two)
        unrelated = app(two).claim(
            "claim:two",
            independent_revision,
            independent_attempt,
            lease_seconds=5,
        )
        require(unrelated.generation > claimed.generation)
        try:
            app(one).submit(claimed)
        except OwnershipConflict:
            pass
        else:
            raise AssertionError("candidate accepted without cooperative task-scope quiescence")
        app(one).quiesce(claimed, "installed-client:exact-task-handoff")
        original_subject = app(one).submit(claimed)
        (one.path / "unknown-user-data").write_bytes(b"synthetic manual bytes must survive")
        before_replay = store.operations()
        require(app(one).claim("claim:one", program.revision, original, lease_seconds=5) == claimed)
        require(store.operations() == before_replay)
        app(one).observe(claimed)
        try:
            app(one, owner=True).cleanup(claimed, evidence="not permission to delete unknown data")
        except OwnershipConflict:
            pass
        else:
            raise AssertionError("cleanup removed changed/unknown user data")
        require((one.path / "unknown-user-data").read_bytes() == b"synthetic manual bytes must survive")
        require(store.artifact(claimed.operation_id, "ownership-candidate"))
        require(original_subject != one.capture()[0])
        app(two).quiesce(unrelated, "installed-client:independent-handoff")
        app(two, owner=True).cleanup(unrelated, evidence="exact empty resource retired")
        require(not two.path.exists())
        before_replay = store.operations()
        require(app(two).claim("claim:two", independent_revision, independent_attempt, lease_seconds=5) == unrelated)
        require(store.operations() == before_replay)
        three = LocalOwnedWorkspace(parent, "managed", create=True)
        fixture = directory / "synthetic-app-server"
        fixture.write_text(
            f"#!{sys.executable}\n"
            + """
import json, sys
if sys.argv[1:] == ['--version']:
    print('codex-cli 0.99.1')
    sys.exit(0)
for line in sys.stdin:
    message = json.loads(line)
    if 'id' in message:
        print(json.dumps({'id': message['id'], 'result': {}}), flush=True)
"""
        )
        fixture.chmod(0o700)
        connection = CodexStdio(
            (str(fixture), "app-server"), "0.99.1", environment={"HOME": str(directory), "PATH": os.defpath}
        )
        try:
            witness = CodexOwnedSessionWitness(connection)
            managed = WorkOwnership(
                store,
                Principal("worker", "worker"),
                three,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            managed_revision = store.load("managed").revision
            managed_attempt = attempt("managed", "installed-managed", three)
            handoff = managed.claim(
                "claim:managed",
                managed_revision,
                managed_attempt,
                lease_seconds=5,
            )
            managed.witness(handoff, "start")
            current = store.load("managed")
            store.admit(
                "managed", "start:managed", StartAttempt(current.revision, "worker", handoff.attempt.attempt_id)
            )
            control = WorkOwnership(
                store,
                Principal("owner", "owner"),
                three,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            require(control.dispatch(handoff, handoff.operation_id, lease_seconds=1) == 1)
            store.record_transport(handoff.operation_id, 1, False)
            store.observe(handoff.operation_id, 1, "managed:first-unknown", "unknown")
            reading = BootClockReading(102, 3_000_000_000, "installed-synthetic-boot")
            store.reconcile(
                handoff.operation_id,
                1,
                "idempotent",
                now=102,
                evidence="trusted:original-key-retry",
                matched_digest=store.operation(handoff.operation_id).request_digest,
            )
            require(control.dispatch(handoff, handoff.operation_id, lease_seconds=1) == 2)
            try:
                managed.witness(handoff, "complete")
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("active native group became normal-completion settlement")
            connection.close()
            managed.witness(handoff, "complete")
            require(managed.submit(handoff) == three.capture()[0])
            current = store.load("managed")
            current = store.admit(
                "managed", "finish:managed", FinishAttempt(current.revision, "worker", handoff.attempt.attempt_id)
            )
            control.release(handoff, evidence="owner:managed-scope-settled-for-remediation")
            remedy = AttemptSpec(
                "installed-remedy",
                "managed",
                "first",
                current.spec.revision,
                current.spec.digest,
                current.resolved_inputs("first"),
                workspace_reference=str(three.path),
                remediation_of=handoff.attempt.attempt_id,
            )
            pull = app(three)
            repaired = pull.claim("claim:remedy", current.revision, remedy, lease_seconds=5)
            current = store.load("managed")
            store.admit("managed", "start:remedy", StartAttempt(current.revision, "worker", remedy.attempt_id))
            pull.quiesce(repaired, "client:remediation-task-quiescent")
            require(pull.submit(repaired) == three.capture()[0])
            current = store.load("managed")
            store.admit("managed", "finish:remedy", FinishAttempt(current.revision, "worker", remedy.attempt_id))
            app(three, owner=True).release(repaired, evidence="owner:remediation-scope-settled")
            reservations = cast(list[dict[str, object]], store.work_budget_status("managed")["reservations"])
            require(sum(item["category"] == WorkKind.RETRY.value for item in reservations) == 1)
            require(sum(item["category"] == WorkKind.REMEDIATION.value for item in reservations) == 1)
            witness.invalidate()
            require(managed.claim("claim:managed", managed_revision, managed_attempt, lease_seconds=5) == handoff)
            try:
                managed.submit(handoff)
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("historical claim replay renewed an invalidated managed profile")
        finally:
            connection.close()
        temporal_spec = ProgramSpec(
            "temporal",
            base.objective,
            base.work_units,
            base.initial_inputs,
            base.budget,
            base.authority,
            acceptance_criteria=base.acceptance_criteria,
            policy_references=base.policy_references,
        )
        temporal = store.create(temporal_spec, "create:temporal")
        temporal_policy = WorkBudgetPolicy.basic(1, 1, verifications=1, retained_bytes=4096)
        store.bind_work_budget("temporal", "installed-temporal-project", temporal_policy, temporal_policy, 110)
        temporal = store.admit("temporal", "activate:temporal", ActivateProgram(temporal.revision, "owner"))
        four = LocalOwnedWorkspace(parent, "temporal", create=True)
        pending = attempt("temporal", "installed-temporal", four)
        store.admit_with_intent(
            "temporal",
            "prepare:temporal",
            PrepareAttempt(temporal.revision, "worker", pending),
            "runtime:installed-temporal",
            "runtime:installed-temporal",
            {"attempt": pending.digest},
        )
        actual_resolve = Path.resolve
        expired = False

        def slow_resolve(path: Path, strict: bool = False) -> Path:
            nonlocal reading, expired
            result = actual_resolve(path, strict=strict)
            if path == four.path and not expired:
                expired = True
                reading = BootClockReading(100, 21_000_000_000, "installed-synthetic-boot")
            return result

        with patch.object(Path, "resolve", slow_resolve):
            try:
                store.claim("runtime:installed-temporal", now=100, lease_seconds=1)
            except BudgetRefused:
                pass
            else:
                raise AssertionError("installed generic delivery spent clock authority captured before filesystem I/O")
        require(expired and store.operation("runtime:installed-temporal").attempts == 0)
        require(store.original_work_deadline("temporal") == 110)
        retained = store.work_budget_status("reference")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        before_replay = store.operations()
        require(
            app(LocalOwnedWorkspace(parent, "one")).claim("claim:one", program.revision, original, lease_seconds=5)
            == claimed
        )
        require(
            app(LocalOwnedWorkspace(parent, "two")).claim(
                "claim:two", independent_revision, independent_attempt, lease_seconds=5
            )
            == unrelated
        )
        historical_managed = WorkOwnership(
            store,
            Principal("worker", "worker"),
            LocalOwnedWorkspace(parent, "managed"),
            mode=HandoffMode.MANAGED,
            profile=witness.profile,
            resources=(witness.resource,),
        )
        require(
            historical_managed.claim("claim:managed", managed_revision, managed_attempt, lease_seconds=5) == handoff
        )
        require(store.operations() == before_replay)
        require(store.work_budget_status("reference") == retained)
        require(store.operation(claimed.operation_id).attempts == 0)
        require((one.path / "unknown-user-data").read_bytes() == b"synthetic manual bytes must survive")
        require(store.startup_evidence.schema_version == 5)
    settlement_conformance(directory, fixture)
    publication_replay_conformance(directory, fixture)
    live_alias_conformance(directory)
    live_alias_conformance(directory, owned=True)
    fresh_subject_conformance(directory, fixture)
    print(
        "Installed ownership: atomic pull claims, conflicts, independent work, original renewal, quiescence, "
        "immutable capture, manual preservation, empty cleanup, explicit-request replay after edit/retirement/reopen, "
        "global generic preparation fencing, cold managed history, fresh post-filesystem boot expiry, "
        "fresh task-proof acceptance/publication, settled historical alias independence, "
        "logical/profile proof separation, counter-only retry/remediation completion, "
        "exact original publication authorization replay across retirement/reopen in both modes, "
        "generic/owned live original-inode alias holds with independent actual-handler work, "
        "actual current-subject acceptance/publication/delivery guards in both modes, "
        "and configured capture above the 16 MiB default within original finite capacity passed."
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
