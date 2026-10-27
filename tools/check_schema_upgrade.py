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
    assert store.startup_evidence.schema_version == 5
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
    assert bundle.schema_version == 5
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
    assert store.startup_evidence.schema_version == 5
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
                    or current.execute("PRAGMA user_version").fetchone()[0] != 5
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
                    if tuple(current.iterdump()) != before:
                        raise RuntimeError(
                            "historical explicit-request replay rewrote original claim/history/clock rows"
                        )
        print(
            f"Installed schema upgrade: actual readers {PINNED_BASE_ID}, {PINNED_PRECLOCK_ID}, "
            f"{PINNED_PREOWNERSHIP_ID} refuse schema 5; original bytes, v2/v3/v4/v5 backups and artifacts preserved; "
            f"original schema-5 claims from {PINNED_INITIAL_OWNERSHIP_ID} replay unchanged "
            "and conflicting work stays fenced."
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
