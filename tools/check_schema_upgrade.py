# SPDX-License-Identifier: Apache-2.0
"""Installed real pinned-base reader versus the new authority schema, offline only."""

import io
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

PINNED_BASE_ID = "e2348656238a2ff9a3244868e2beb543d86f15a5"  # Exact public Git object ID, not a credential.
PINNED_PRECLOCK_ID = "23a28e31c17018acb9900efeed2da1b9b03330cf"  # Published schema-3 public Git object ID.
PINNED_PREOWNERSHIP_ID = "b11aae420517b14bbb4d85c84f9a0d82c14446c6"  # Published schema-4 public Git object ID.
PINNED_INITIAL_OWNERSHIP_ID = "b9aae79d36e1a1ece046dd565a07be028c8f865a"  # Original PR110 schema-5 record producer.
PINNED_PREQUALIFICATION_ID = "3aa2b8f0232892ddfc61545bff7c646176253628"  # Exact schema-5 prequalification producer.
PINNED_PRE_ACCEPTANCE_ID = "88d59fcd594cbf7fd529971a12139628bef9b6a5"  # Exact public Git object ID.
PINNED_PRE_PUBLICATION_ID = "3ebd84a28c6c3bfcf6ed7f4b1937fc425ae05b13"  # Exact public Git object ID.
PINNED_PRE_WORKSPACE_ID = "397ee0cb6a11f0638b9b19f48076f34d1d1c69b1"  # Exact public schema-5 pathname-only producer.
PINNED_PRE_ORDINARY_ID = "b1c9ffca16f06596e5c3afdca3b9893cf7422d8d"  # Exact integrated pre-ordinary reader.
ROOT = Path(__file__).resolve().parents[1]


def install_pinned_reader(directory: Path, *, commit_id: str = PINNED_BASE_ID) -> Path:
    """Build the actual public base, never a simulated legacy validator or another checkout."""
    resolved = subprocess.run(
        ["git", "rev-parse", f"{commit_id}^{{commit}}"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    if resolved.stdout.strip() != commit_id:
        raise RuntimeError("pinned public base object unavailable")
    archive = subprocess.run(["git", "archive", "--format=tar", commit_id], cwd=ROOT, check=True, capture_output=True)
    source = directory / "source"
    source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as contents:
        contents.extractall(source, filter="data")
    dist = directory / "dist"
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-sources",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--out-dir",
            str(dist),
        ],
        cwd=source,
        check=True,
    )
    environment = directory / "legacy-venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
    python = environment / "bin/python"
    wheel = next(dist.glob("*.whl"))
    subprocess.run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)], check=True)
    return python


CREATE_LEGACY = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
with SQLiteProgramStore(Path(sys.argv[1])) as store:
    assert store.startup_evidence.schema_version == (int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    store.create(reference_spec(), 'create')
    store.intent('legacy-evidence', 'legacy-evidence', {'synthetic': True})
    store.finalize_artifact('legacy-evidence', 'original', b'synthetic retained original bytes')
"""

UPGRADE_AND_REVOKE = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore, BackupBundle, UnsupportedSQLiteConfiguration
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy
clock = lambda: BootClockReading(100, 1000000000, 'installed-synthetic-boot')
path = Path(sys.argv[1])
with SQLiteProgramStore(path, boot_clock=clock) as store:
    assert store.startup_evidence.schema_version == 6
    program = store.load('reference')
    assert store.artifact('legacy-evidence', 'original') == b'synthetic retained original bytes'
    policy = WorkBudgetPolicy.basic(1, 1, verifications=1, retained_bytes=1024)
    store.bind_work_budget('reference', 'synthetic-project', policy, policy, 110)
    program = store.admit('reference', 'activate', ActivateProgram(program.revision, 'owner'))
    attempt = AttemptSpec('installed-attempt', 'reference', 'first', program.spec.revision,
                          program.spec.digest, program.resolved_inputs('first'))
    store.admit_with_intent('reference', 'prepare', PrepareAttempt(program.revision, 'worker', attempt),
                           'runtime:installed-attempt', 'runtime:installed-attempt', {'attempt': attempt.digest})
    store.revoke_work_budget('reference', 'synthetic owner stop')
    before = store.work_budget_status('reference')
    bundle = store.backup(path.parent / 'schema-four-backup')
    assert bundle.schema_version == 6
restored = SQLiteProgramStore.restore_backup(bundle, path.parent / 'restored-four.sqlite3')
with SQLiteProgramStore(restored, boot_clock=clock) as store:
    assert store.work_budget_status('reference') == before
    assert store.artifact('legacy-evidence', 'original') == b'synthetic retained original bytes'
    try:
        store.claim('runtime:installed-attempt', now=100, lease_seconds=1)
    except BudgetRefused:
        pass
    else:
        raise AssertionError('restored revocation lost its dispatch fence')
    assert store.operation('runtime:installed-attempt').attempts == 0
old_schema = int(sys.argv[2]) if len(sys.argv) > 2 else 2
backups = list(path.parent.glob(path.name + ('.pre-schema-5-*' if old_schema == 4 else '.pre-schema-4-*')))
assert len(backups) == 1
import json
manifest = json.loads((backups[0] / 'manifest.json').read_text())
assert manifest['schema_version'] == old_schema
old_bundle = BackupBundle(backups[0], backups[0] / 'kernel.sqlite3', backups[0] / 'manifest.json',
                          manifest['database_sha256'], manifest['history_record_count'], old_schema)
restored = SQLiteProgramStore.restore_backup(old_bundle, path.parent / 'restored-original.sqlite3')
with SQLiteProgramStore(restored, boot_clock=clock) as store:
    assert store.startup_evidence.schema_version == 6
    assert not store.has_work_budget('reference')
    assert store.artifact('legacy-evidence', 'original') == b'synthetic retained original bytes'
"""

REFUSE_DOWNGRADE = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore, UnsupportedSQLiteConfiguration
try:
    with SQLiteProgramStore(Path(sys.argv[1])) as store:
        store.claim('runtime:installed-attempt', now=100, lease_seconds=1)
        Path(sys.argv[2]).write_text('forbidden synthetic dispatch')
except UnsupportedSQLiteConfiguration as error:
    assert 'newer than supported' in str(error)
else:
    raise AssertionError('actual pinned old reader failed to refuse the new authority schema')
"""

CREATE_ORIGINAL_CLAIM = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt, ProgramSpec
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode
from creatidy_kernel.core.work_budget import WorkBudgetPolicy
from creatidy_kernel.ports.ownership import WorkOwnership
directory, state = Path(sys.argv[1]), sys.argv[2]
parent = directory / 'workspaces'
parent.mkdir(mode=0o700)
tree = LocalOwnedWorkspace(parent, 'one', create=True)
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3', boot_clock=clock) as store:
    spec = reference_spec()
    program = store.create(spec, 'create')
    policy = WorkBudgetPolicy.basic(3,3,verifications=3,retained_bytes=4096)
    store.bind_work_budget('reference','project',policy,policy,110)
    program = store.admit('reference','activate',ActivateProgram(program.revision,'owner'))
    app = WorkOwnership(store,Principal('worker','worker'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    attempt = AttemptSpec('original','reference','first',program.spec.revision,program.spec.digest,
                          program.resolved_inputs('first'),workspace_reference=str(tree.path))
    original = app.claim('claim:original',program.revision,attempt,lease_seconds=5)
    app.quiesce(original,'client:exact-scope-quiescent')
    if state == 'edited':
        (tree.path / 'manual').write_bytes(b'synthetic manually retained bytes')
    elif state == 'retired':
        owner = WorkOwnership(store,Principal('owner','owner'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
        owner.cleanup(original,evidence='owner:exact-empty-retirement')
    elif state == 'conflict':
        other = ProgramSpec('generic',spec.objective,spec.work_units,spec.initial_inputs,spec.budget,spec.authority,
                            acceptance_criteria=spec.acceptance_criteria,policy_references=spec.policy_references)
        other_program = store.create(other,'create:generic')
        store.bind_work_budget('generic','project',policy,policy,110)
        other_program = store.admit('generic','activate:generic',ActivateProgram(other_program.revision,'owner'))
        competing = AttemptSpec('competing','generic','first',other.revision,other.digest,
                                other_program.resolved_inputs('first'),workspace_reference=str(tree.path))
        store.admit_with_intent('generic','prepare:competing',PrepareAttempt(other_program.revision,'worker',competing),
                               'runtime:competing','runtime:competing',{'attempt':competing.digest})
"""

REPLAY_ORIGINAL_CLAIM = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict
from creatidy_kernel.ports.ownership import WorkOwnership
directory, state = Path(sys.argv[1]), sys.argv[2]
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3', boot_clock=clock) as store:
    tree = LocalOwnedWorkspace(directory / 'workspaces','one')
    app = WorkOwnership(store,Principal('worker','worker'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    attempt = store.load('reference').attempt('original').spec
    original = app.claim('claim:original',1,attempt,lease_seconds=5)
    assert original.attempt == attempt and original.generation == 1 and original.expires_at == 105
    assert store.artifact(original.operation_id,'ownership-baseline')
    assert store.operation(original.operation_id).attempts == 0
    if state == 'conflict':
        for action in (lambda: app.submit(original), lambda: store.check_work_dispatch('runtime:competing'),
                       lambda: store.claim('runtime:competing',now=100,lease_seconds=1)):
            try:
                action()
            except OwnershipConflict:
                pass
            else:
                raise AssertionError('retained conflicting original work was treated as current authority')
        assert store.operation('runtime:competing').attempts == 0
"""


CREATE_LOGICAL_GENERIC = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (ActivateProgram, AttemptSpec, PrepareAttempt, StartAttempt, FinishAttempt,
                                        SatisfyWorkUnit, TrustedSatisfaction, InputBinding, ProgramSpec)
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
directory = Path(sys.argv[1])
parent = directory / 'workspaces'
parent.mkdir(mode=0o700)
tree = LocalOwnedWorkspace(parent,'one',create=True)
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3',boot_clock=clock) as store:
    base = reference_spec()
    budget = WorkBudgetPolicy.basic(3,3,verifications=3,retained_bytes=16384,auxiliary=2)
    for name in ('reference','successor'):
        spec = ProgramSpec(name,base.objective,base.work_units,base.initial_inputs,base.budget,base.authority,
                           acceptance_criteria=base.acceptance_criteria,policy_references=base.policy_references)
        program = store.create(spec,'create:'+name)
        store.bind_work_budget(name,'project',budget,budget,110)
        store.admit(name,'activate:'+name,ActivateProgram(program.revision,'owner'))
    program = store.load('reference')
    attempt = AttemptSpec('generic','reference','first',program.spec.revision,program.spec.digest,
                          program.resolved_inputs('first'),workspace_reference=str(tree.path))
    program = store.admit_with_intent('reference','prepare',PrepareAttempt(program.revision,'worker',attempt),
                                     'runtime:generic','runtime:generic',{'attempt':attempt.digest})
    program = store.admit('reference','start',StartAttempt(program.revision,'worker','generic'))
    program = store.admit('reference','finish',FinishAttempt(program.revision,'worker','generic'))
    accepted = TrustedSatisfaction('logical:owned-group-stopped','reference','first',program.spec.revision,
        program.spec.digest,program.spec.work_unit_applicability_fingerprint('first'),'verifier','generic',
        tuple(InputBinding(name,'artifact:'+name) for name in sorted(program.spec.work_unit('first').outputs)))
    if len(sys.argv) > 2 and sys.argv[2] == 'logical-only':
        store.record_work_completion('runtime:generic','logical:terminal')
    else:
        store.admit('reference','accept',SatisfyWorkUnit(program.revision,'verifier',accepted))
        if len(sys.argv) > 2 and sys.argv[2] == 'pending-publication':
            store.budgeted_intent('reference','generic',WorkKind.AUXILIARY,'publish','publish',{'accepted':'logical:owned-group-stopped'})
"""

REFUSE_LOGICAL_REUSE = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE,HandoffMode,OwnershipConflict
from creatidy_kernel.ports.ownership import WorkOwnership
directory = Path(sys.argv[1])
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3',boot_clock=clock) as store:
    tree = LocalOwnedWorkspace(directory / 'workspaces','one')
    app = WorkOwnership(store,Principal('worker','worker'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    program = store.load('successor')
    attempt = AttemptSpec('next','successor','first',program.spec.revision,program.spec.digest,
                          program.resolved_inputs('first'),workspace_reference=str(tree.path))
    try:
        app.claim('claim:next',program.revision,attempt,lease_seconds=5)
    except OwnershipConflict:
        pass
    else:
        raise AssertionError('logical acceptance became physical task/resource proof')
"""

REFUSE_QUALIFICATION_DOWNGRADE = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.sqlite_store import CorruptHistory,SQLiteProgramStore,UnsupportedSQLiteConfiguration
try:
    with SQLiteProgramStore(Path(sys.argv[1])):
        raise AssertionError('old reader ignored the logical/profile settlement distinction')
except (CorruptHistory, UnsupportedSQLiteConfiguration):
    pass
"""


CREATE_OWNED_PUBLICATION = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (ActivateProgram,AttemptSpec,StartAttempt,FinishAttempt,
                                        SatisfyWorkUnit,TrustedSatisfaction,InputBinding)
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE,HandoffMode
from creatidy_kernel.core.work_budget import WorkBudgetPolicy
from creatidy_kernel.ports.ownership import WorkOwnership
directory = Path(sys.argv[1])
parent = directory / 'workspaces'
parent.mkdir(mode=0o700)
tree = LocalOwnedWorkspace(parent,'one',create=True)
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3',boot_clock=clock) as store:
    program = store.create(reference_spec(),'create')
    budget = WorkBudgetPolicy.basic(3,3,verifications=3,retained_bytes=16384,auxiliary=3)
    store.bind_work_budget('reference','project',budget,budget,110)
    program = store.admit('reference','activate',ActivateProgram(program.revision,'owner'))
    worker = WorkOwnership(store,Principal('worker','worker'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    owner = WorkOwnership(store,Principal('owner','owner'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    attempt = AttemptSpec('publication-task','reference','first',program.spec.revision,program.spec.digest,
                          program.resolved_inputs('first'),workspace_reference=str(tree.path))
    claim = worker.claim('claim:publication-task',program.revision,attempt,lease_seconds=5)
    program = store.load('reference')
    program = store.admit('reference','start',StartAttempt(program.revision,'worker',attempt.attempt_id))
    worker.quiesce(claim,'client:exact-task-quiescent')
    worker.submit(claim)
    program = store.admit('reference','finish',FinishAttempt(program.revision,'worker',attempt.attempt_id))
    accepted = TrustedSatisfaction('accepted:publication','reference','first',program.spec.revision,program.spec.digest,
        program.spec.work_unit_applicability_fingerprint('first'),'verifier',attempt.attempt_id,
        tuple(InputBinding(name,'artifact:'+name) for name in sorted(program.spec.work_unit('first').outputs)))
    store.admit('reference','accept',SatisfyWorkUnit(program.revision,'verifier',accepted))
    owner.authorize_publication(claim,'owner-publication','owner-publication',{'accepted':'publication-task'})
"""

REFUSE_UNPROVEN_OWNER_AUTHORIZATION = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore,IdempotencyConflict
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE,HandoffMode
from creatidy_kernel.ports.ownership import WorkOwnership
directory = Path(sys.argv[1])
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3',boot_clock=clock) as store:
    tree = LocalOwnedWorkspace(directory / 'workspaces','one')
    owner = WorkOwnership(store,Principal('owner','owner'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    attempt = store.load('reference').attempt('publication-task').spec
    claim = store.find_work_claim('claim:publication-task','worker',1,attempt,HandoffMode.PULL,
                                  COOPERATIVE_PROFILE,(tree.resource,),5)
    assert claim is not None
    try:
        owner.authorize_publication(claim,'owner-publication','owner-publication',{'accepted':'publication-task'})
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError('old AUX state was invented into an original owner authorization')
    assert not store.operations('work-budget:v1:ownership-publication:')
"""


REFUSE_UNBOUND_WORKSPACE = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE,HandoffMode,OwnershipConflict
from creatidy_kernel.ports.ownership import WorkOwnership
directory = Path(sys.argv[1])
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3',boot_clock=clock) as store:
    tree = LocalOwnedWorkspace(directory / 'workspaces','independent',create=True)
    app = WorkOwnership(store,Principal('worker','worker'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    program = store.load('successor')
    attempt = AttemptSpec('independent','successor','first',program.spec.revision,program.spec.digest,
                          program.resolved_inputs('first'),workspace_reference=str(tree.path))
    try:
        app.claim('claim:independent',program.revision,attempt,lease_seconds=5)
    except OwnershipConflict as error:
        assert 'identity/custody unknown' in str(error)
    else:
        raise AssertionError('old generic history was retrospectively bound to a current inode')
    assert not store.operations('work-budget:v1:ownership-workspace:')
"""


VERIFY_ORIGINAL_OWNED_ALIAS = """
import sys
from pathlib import Path
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram,AttemptSpec,PrepareAttempt,ProgramSpec
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE,HandoffMode,OwnershipConflict,ResourceScope
from creatidy_kernel.core.work_budget import WorkBudgetPolicy
from creatidy_kernel.ports.ownership import WorkOwnership
directory = Path(sys.argv[1])
old, retained = directory / 'workspaces' / 'one', directory / 'workspaces' / 'retained'
clock = lambda: BootClockReading(100,1000000000,'synthetic-boot-a')
with SQLiteProgramStore(directory / 'kernel.sqlite3',boot_clock=clock) as store:
    original = store.load('reference')
    prior = store.operations()
    captured = store.artifact('runtime:original','ownership-baseline')
    recorded = store.find_work_claim('claim:original','worker',1,original.attempt('original').spec,
        HandoffMode.PULL,COOPERATIVE_PROFILE,(ResourceScope('workspace',str(old)),),5)
    assert recorded is not None
    base = reference_spec()
    policy = WorkBudgetPolicy.basic(3,3,verifications=3,retained_bytes=4096)
    for name in ('independent','competing'):
        spec = ProgramSpec(name,base.objective,base.work_units,base.initial_inputs,base.budget,base.authority,
            acceptance_criteria=base.acceptance_criteria,policy_references=base.policy_references)
        program = store.create(spec,'create:'+name)
        store.bind_work_budget(name,'project',policy,policy,110)
        store.admit(name,'activate:'+name,ActivateProgram(program.revision,'owner'))
    tree = LocalOwnedWorkspace(directory / 'workspaces','independent',create=True)
    app = WorkOwnership(store,Principal('worker','worker'),tree,mode=HandoffMode.PULL,profile=COOPERATIVE_PROFILE)
    program = store.load('independent')
    attempt = AttemptSpec('independent','independent','first',program.spec.revision,program.spec.digest,
        program.resolved_inputs('first'),workspace_reference=str(tree.path))
    claimed = app.claim('claim:independent',program.revision,attempt,lease_seconds=5)
    assert app.renew('renew:independent',claimed,lease_seconds=7) == 107
    for target in (old,retained,retained / 'child',old.parent):
        program = store.load('competing')
        attempt = AttemptSpec('unsafe','competing','first',program.spec.revision,program.spec.digest,
            program.resolved_inputs('first'),workspace_reference=str(target))
        before = store.operations()
        try:
            store.admit('competing','prepare:unsafe',PrepareAttempt(program.revision,'worker',attempt))
        except OwnershipConflict:
            pass
        else:
            raise AssertionError('prior original owned identity was ignored after aliasing')
        assert store.operations() == before
    assert store.load('reference') == original
    assert all(store.operation(item.operation_id) == item for item in prior)
    assert store.artifact('runtime:original','ownership-baseline') == captured
    assert len(store.operations('work-budget:v1:ownership-workspace:')) == 1
    assert (retained / 'manual').read_bytes() == b'synthetic manually retained bytes'
"""


def main(new_python: Path, storage_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="installed-schema-upgrade-", dir=storage_root) as value:
        directory = Path(value)
        for schema, commit_id in ((2, PINNED_BASE_ID), (3, PINNED_PRECLOCK_ID), (4, PINNED_PREOWNERSHIP_ID)):
            fixture = directory / f"schema-{schema}"
            fixture.mkdir()
            old_python = install_pinned_reader(fixture, commit_id=commit_id)
            path = fixture / "synthetic.sqlite3"
            subprocess.run(
                [str(old_python), "-I", "-c", CREATE_LEGACY, str(path), str(schema)], cwd=fixture, check=True
            )
            with closing(sqlite3.connect(path)) as original:
                history = original.execute("SELECT * FROM history").fetchall()
                admissions = original.execute("SELECT * FROM command_admissions").fetchall()
            subprocess.run(
                [str(new_python), "-I", "-c", UPGRADE_AND_REVOKE, str(path), str(schema)], cwd=fixture, check=True
            )
            with closing(sqlite3.connect(path)) as current:
                before = tuple(current.iterdump())
                if (
                    current.execute("SELECT * FROM history ORDER BY sequence LIMIT 1").fetchall() != history
                    or current.execute("SELECT * FROM command_admissions WHERE command_key = 'create'").fetchall()
                    != admissions
                ):
                    raise RuntimeError("migration rewrote original history/admission bytes")
            marker = fixture / "forbidden-effect"
            subprocess.run(
                [str(old_python), "-I", "-c", REFUSE_DOWNGRADE, str(path), str(marker)], cwd=fixture, check=True
            )
            with closing(sqlite3.connect(path)) as current:
                if (
                    tuple(current.iterdump()) != before
                    or current.execute("PRAGMA user_version").fetchone()[0] != 6
                    or current.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0] != 0
                ):
                    raise RuntimeError("downgraded reader changed protected records or dispatched")
            if marker.exists():
                raise RuntimeError("downgraded reader performed an effect")
        original = directory / "original-ownership"
        original.mkdir()
        old_python = install_pinned_reader(original, commit_id=PINNED_INITIAL_OWNERSHIP_ID)
        for state in ("edited", "retired", "conflict"):
            fixture = original / state
            fixture.mkdir()
            subprocess.run(
                [str(old_python), "-I", "-c", CREATE_ORIGINAL_CLAIM, str(fixture), state], cwd=fixture, check=True
            )
            path = fixture / "kernel.sqlite3"
            with closing(sqlite3.connect(path)) as current:
                before = tuple(current.iterdump())
            subprocess.run(
                [str(new_python), "-I", "-c", REPLAY_ORIGINAL_CLAIM, str(fixture), state], cwd=fixture, check=True
            )
            if state != "conflict":
                with closing(sqlite3.connect(path)) as current:
                    upgraded = tuple(current.iterdump())
                    if tuple(line for line in upgraded if "ordinary_preparation_contract" not in line) != before:
                        raise RuntimeError("schema-6 migration rewrote original claim/history/clock rows")
                subprocess.run(
                    [str(new_python), "-I", "-c", REPLAY_ORIGINAL_CLAIM, str(fixture), state], cwd=fixture, check=True
                )
                with closing(sqlite3.connect(path)) as current:
                    if tuple(current.iterdump()) != upgraded:
                        raise RuntimeError("historical explicit-request replay rewrote migrated original rows")
        qualification = directory / "qualification"
        qualification.mkdir()
        prior_python = install_pinned_reader(qualification, commit_id=PINNED_PREQUALIFICATION_ID)
        legacy = qualification / "untyped"
        legacy.mkdir()
        subprocess.run([str(prior_python), "-I", "-c", CREATE_LOGICAL_GENERIC, str(legacy)], cwd=legacy, check=True)
        path = legacy / "kernel.sqlite3"
        with closing(sqlite3.connect(path)) as current:
            before = tuple(current.iterdump())
        subprocess.run([str(new_python), "-I", "-c", REFUSE_LOGICAL_REUSE, str(legacy)], cwd=legacy, check=True)
        with closing(sqlite3.connect(path)) as current:
            if tuple(line for line in current.iterdump() if "ordinary_preparation_contract" not in line) != before:
                raise RuntimeError("old untyped settlement was rewritten/promoted on recovery")
        typed = qualification / "typed"
        typed.mkdir()
        subprocess.run(
            [str(new_python), "-I", "-c", CREATE_LOGICAL_GENERIC, str(typed), "logical-only"], cwd=typed, check=True
        )
        path = typed / "kernel.sqlite3"
        with closing(sqlite3.connect(path)) as current:
            before = tuple(current.iterdump())
        for reader in (old_python, prior_python):
            subprocess.run([str(reader), "-I", "-c", REFUSE_QUALIFICATION_DOWNGRADE, str(path)], cwd=typed, check=True)
        with closing(sqlite3.connect(path)) as current:
            if tuple(current.iterdump()) != before:
                raise RuntimeError("old schema5 reader changed typed settlement records")
        publication = directory / "publication"
        publication.mkdir()
        previous_python = install_pinned_reader(publication, commit_id=PINNED_PRE_PUBLICATION_ID)
        old_publication = publication / "unproven"
        old_publication.mkdir()
        subprocess.run(
            [str(previous_python), "-I", "-c", CREATE_OWNED_PUBLICATION, str(old_publication)],
            cwd=old_publication,
            check=True,
        )
        path = old_publication / "kernel.sqlite3"
        with closing(sqlite3.connect(path)) as current:
            before = tuple(current.iterdump())
        subprocess.run(
            [str(new_python), "-I", "-c", REFUSE_UNPROVEN_OWNER_AUTHORIZATION, str(old_publication)],
            cwd=old_publication,
            check=True,
        )
        with closing(sqlite3.connect(path)) as current:
            if tuple(line for line in current.iterdump() if "ordinary_preparation_contract" not in line) != before:
                raise RuntimeError("original unproven publication history was rewritten or authorized")
        new_publication = publication / "typed"
        new_publication.mkdir()
        subprocess.run(
            [str(new_python), "-I", "-c", CREATE_OWNED_PUBLICATION, str(new_publication)],
            cwd=new_publication,
            check=True,
        )
        path = new_publication / "kernel.sqlite3"
        with closing(sqlite3.connect(path)) as current:
            before = tuple(current.iterdump())
        subprocess.run(
            [str(previous_python), "-I", "-c", REFUSE_QUALIFICATION_DOWNGRADE, str(path)],
            cwd=new_publication,
            check=True,
        )
        with closing(sqlite3.connect(path)) as current:
            if tuple(current.iterdump()) != before:
                raise RuntimeError("old reader changed typed original owner authorization")
        workspace = directory / "workspace-identity"
        workspace.mkdir()
        pathname_python = install_pinned_reader(workspace, commit_id=PINNED_PRE_WORKSPACE_ID)
        for bound in (False, True):
            fixture = workspace / ("bound" if bound else "unbound")
            fixture.mkdir()
            subprocess.run(
                [
                    str(new_python if bound else pathname_python),
                    "-I",
                    "-c",
                    CREATE_LOGICAL_GENERIC,
                    str(fixture),
                    "logical-only",
                ],
                cwd=fixture,
                check=True,
            )
            path = fixture / "kernel.sqlite3"
            old = fixture / "workspaces" / "one"
            retained = old.with_name("retained")
            old.rename(retained)
            old.symlink_to(retained, target_is_directory=True)
            with closing(sqlite3.connect(path)) as current:
                before = tuple(current.iterdump())
            subprocess.run(
                [
                    str(pathname_python if bound else new_python),
                    "-I",
                    "-c",
                    REFUSE_QUALIFICATION_DOWNGRADE if bound else REFUSE_UNBOUND_WORKSPACE,
                    str(path if bound else fixture),
                ],
                cwd=fixture,
                check=True,
            )
            with closing(sqlite3.connect(path)) as current:
                after = tuple(current.iterdump())
                if (bound and after != before) or (
                    not bound and tuple(line for line in after if "ordinary_preparation_contract" not in line) != before
                ):
                    raise RuntimeError("workspace compatibility rewrote original history or identity records")
        original_owned = workspace / "original-owned-alias"
        original_owned.mkdir()
        subprocess.run(
            [str(pathname_python), "-I", "-c", CREATE_ORIGINAL_CLAIM, str(original_owned), "edited"],
            cwd=original_owned,
            check=True,
        )
        old = original_owned / "workspaces" / "one"
        retained = old.with_name("retained")
        old.rename(retained)
        old.symlink_to(retained, target_is_directory=True)
        subprocess.run(
            [str(new_python), "-I", "-c", VERIFY_ORIGINAL_OWNED_ALIAS, str(original_owned)],
            cwd=original_owned,
            check=True,
        )
        ordinary = directory / "ordinary-preparation"
        ordinary.mkdir()
        pre_ordinary_python = install_pinned_reader(ordinary, commit_id=PINNED_PRE_ORDINARY_ID)
        fixture = ordinary / "installed-flow"
        fixture.mkdir()
        subprocess.run(
            [str(new_python), "-I", str(ROOT / "tools/check_ordinary.py"), str(fixture)], cwd=fixture, check=True
        )
        path = fixture / "pull-normal" / "state.db"
        with closing(sqlite3.connect(path)) as current:
            before = tuple(current.iterdump())
        subprocess.run(
            [str(pre_ordinary_python), "-I", "-c", REFUSE_QUALIFICATION_DOWNGRADE, str(path)], cwd=fixture, check=True
        )
        with closing(sqlite3.connect(path)) as current:
            if tuple(current.iterdump()) != before:
                raise RuntimeError("pre-ordinary reader changed controller preparation records")
        print(
            f"Installed schema upgrade: actual readers {PINNED_BASE_ID}, {PINNED_PRECLOCK_ID}, "
            f"{PINNED_PREOWNERSHIP_ID} refuse schema 6; original bytes, v2/v3/v4/v5 backups and artifacts preserved; "
            f"original schema-5 claims from {PINNED_INITIAL_OWNERSHIP_ID} replay unchanged "
            "and conflicting work stays fenced."
        )
        print(
            "Installed settlement qualification: untyped logical history stays unknown; "
            "old schema5 readers refuse new proof categories."
        )
        print(
            "Installed publication authorization: original owner receipts required, old unproven operations unchanged, "
            "unsafe older reader refused."
        )
        print(
            "Installed workspace identity: unbound original bytes remain unknown/unchanged; actual prior reader "
            f"{PINNED_PRE_WORKSPACE_ID} refuses the new identity category without writes; its original owned capture "
            "retains the aliased inode while the actual shared handler admits independent work without backfill."
        )
        print(f"Installed ordinary preparation: actual prior reader {PINNED_PRE_ORDINARY_ID} refuses without writes.")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
