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
    assert store.startup_evidence.schema_version == 4
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
    assert bundle.schema_version == 4
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
backups = list(path.parent.glob(path.name + '.pre-schema-4-*'))
assert len(backups) == 1
import json
manifest = json.loads((backups[0] / 'manifest.json').read_text())
old_schema = int(sys.argv[2]) if len(sys.argv) > 2 else 2
assert manifest['schema_version'] == old_schema
old_bundle = BackupBundle(backups[0], backups[0] / 'kernel.sqlite3', backups[0] / 'manifest.json',
                          manifest['database_sha256'], manifest['history_record_count'], old_schema)
restored = SQLiteProgramStore.restore_backup(old_bundle, path.parent / 'restored-original.sqlite3')
with SQLiteProgramStore(restored, boot_clock=clock) as store:
    assert store.startup_evidence.schema_version == 4
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


def main(new_python: Path, storage_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="installed-schema-upgrade-", dir=storage_root) as value:
        directory = Path(value)
        for schema, commit_id in ((2, PINNED_BASE_ID), (3, PINNED_PRECLOCK_ID)):
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
                    or current.execute("PRAGMA user_version").fetchone()[0] != 4
                    or current.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0] != 0
                ):
                    raise RuntimeError("downgraded reader changed protected records or dispatched")
            if marker.exists():
                raise RuntimeError("downgraded reader performed an effect")
        print(
            f"Installed schema upgrade: actual readers {PINNED_BASE_ID} and {PINNED_PRECLOCK_ID} refuse schema 4; "
            "original bytes, v2/v3/v4 backups and artifacts preserved."
        )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
