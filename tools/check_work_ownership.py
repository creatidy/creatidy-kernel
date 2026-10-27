# SPDX-License-Identifier: Apache-2.0
"""Installed application conformance: real SQLite/local handlers, no harness/provider."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt, ProgramSpec
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy
from creatidy_kernel.ports.ownership import WorkOwnership


def require(condition: object) -> None:
    if not condition:
        raise AssertionError("installed ownership invariant failed")


def main(directory: Path) -> None:
    parent = directory / "workspaces"
    parent.mkdir(mode=0o700)
    one = LocalOwnedWorkspace(parent, "one", create=True)
    two = LocalOwnedWorkspace(parent, "two", create=True)
    policy = WorkBudgetPolicy.basic(3, 3, verifications=3, retained_bytes=16384)
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
            try:
                managed.witness(handoff, "complete")
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("active native group became normal-completion settlement")
            connection.close()
            managed.witness(handoff, "complete")
            require(managed.submit(handoff) == three.capture()[0])
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
    print(
        "Installed ownership: atomic pull claims, conflicts, independent work, original renewal, quiescence, "
        "immutable capture, manual preservation, empty cleanup, explicit-request replay after edit/retirement/reopen, "
        "global generic preparation fencing, cold managed history and fresh post-filesystem boot expiry passed."
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
