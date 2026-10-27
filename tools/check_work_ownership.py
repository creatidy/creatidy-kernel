# SPDX-License-Identifier: Apache-2.0
"""Installed application conformance: real SQLite/local handlers, no harness/provider."""

import os
import sys
from pathlib import Path

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, ProgramSpec
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict
from creatidy_kernel.core.work_budget import WorkBudgetPolicy
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
    clock = lambda: BootClockReading(100, 1_000_000_000, "installed-synthetic-boot")  # noqa: E731
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
        unrelated = app(two).claim(
            "claim:two",
            store.load("independent").revision,
            attempt("independent", "independent-pull", two),
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
            handoff = managed.claim(
                "claim:managed",
                store.load("managed").revision,
                attempt("managed", "installed-managed", three),
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
        finally:
            connection.close()
        retained = store.work_budget_status("reference")
    with SQLiteProgramStore(path, boot_clock=clock) as store:
        require(store.work_budget_status("reference") == retained)
        require(store.operation(claimed.operation_id).attempts == 0)
        require((one.path / "unknown-user-data").read_bytes() == b"synthetic manual bytes must survive")
        require(store.startup_evidence.schema_version == 5)
    print(
        "Installed ownership: atomic pull claims, conflicts, independent work, original renewal, quiescence, "
        "immutable capture, manual preservation, empty cleanup and reopen passed."
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
