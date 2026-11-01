# SPDX-License-Identifier: Apache-2.0
"""Actual pinned installed legacy producer and interruption-safe authority migration."""

import inspect
import json
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from creatidy_kernel.adapters import sqlite_store
from creatidy_kernel.adapters.sqlite_store import CorruptHistory, SQLiteProgramStore
from tools.check_schema_upgrade import CREATE_LEGACY, PINNED_PRE_ORDINARY_ID, REFUSE_DOWNGRADE, install_pinned_reader

pytest_plugins = ["test_sqlite_store"]


@pytest.fixture(scope="module")
def pinned_reader(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return install_pinned_reader(tmp_path_factory.mktemp("actual-pinned-reader"))


@pytest.fixture
def legacy_database(sqlite_tmp_path: Path, pinned_reader: Path) -> Path:
    path = sqlite_tmp_path / "legacy.sqlite3"
    subprocess.run([str(pinned_reader), "-I", "-c", CREATE_LEGACY, str(path)], check=True)  # noqa: S603 - exact pinned own source, synthetic fixture.
    return path


@pytest.mark.parametrize("migration_step", ["_migrate_2_to_3", "_migrate_3_to_4", "_migrate_4_to_5", "_migrate_5_to_6"])
def test_migration_interruption_rolls_back_then_reuses_atomic_original_backup(
    legacy_database: Path, monkeypatch: pytest.MonkeyPatch, migration_step: str
) -> None:
    path = legacy_database
    with closing(sqlite3.connect(path)) as raw:
        original = tuple(raw.iterdump())
    migrate = cast(Callable[[SQLiteProgramStore], None], inspect.getattr_static(SQLiteProgramStore, migration_step))

    def interrupted(store: SQLiteProgramStore) -> None:
        migrate(store)
        raise RuntimeError("synthetic interruption before schema transaction commit")

    with monkeypatch.context() as failure:
        failure.setattr(SQLiteProgramStore, migration_step, interrupted)
        with pytest.raises(RuntimeError, match="interruption"):
            SQLiteProgramStore(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
        assert tuple(raw.iterdump()) == original
    backups = list(path.parent.glob(path.name + ".pre-schema-4-*"))
    assert len(backups) == 1
    manifest = json.loads((backups[0] / "manifest.json").read_text())
    assert manifest["schema_version"] == 2 and manifest["artifacts"]
    with SQLiteProgramStore(path) as upgraded:
        assert upgraded.startup_evidence.schema_version == 6
        assert upgraded.artifact("legacy-evidence", "original") == b"synthetic retained original bytes"
    assert list(path.parent.glob(path.name + ".pre-schema-4-*")) == backups


def test_actual_old_reader_refuses_new_schema_without_dispatch_or_record_mutation(
    legacy_database: Path, pinned_reader: Path
) -> None:
    path = legacy_database
    with SQLiteProgramStore(path) as store:
        store.revoke_work_budget("reference", "synthetic:owner-stop")
    with closing(sqlite3.connect(path)) as raw:
        original = tuple(raw.iterdump())
    marker = path.parent / "forbidden-dispatch"
    subprocess.run([str(pinned_reader), "-I", "-c", REFUSE_DOWNGRADE, str(path), str(marker)], check=True)  # noqa: S603 - actual isolated installed base reader, no external effects.
    with closing(sqlite3.connect(path)) as raw:
        assert tuple(raw.iterdump()) == original
    assert not marker.exists()


def test_failed_backup_never_installs_the_new_authority_contract(
    legacy_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = legacy_database
    sync = cast(Callable[[Path], None], inspect.getattr_static(sqlite_store, "_fsync_directory"))

    def fail_publication(directory: Path) -> None:
        if ".pre-schema-4-" in directory.name:
            raise OSError("synthetic backup publication failure")
        sync(directory)

    with monkeypatch.context() as failure:
        failure.setattr(sqlite_store, "_fsync_directory", fail_publication)
        with pytest.raises(OSError, match="backup publication"):
            SQLiteProgramStore(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
        assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'store_contract'").fetchone() is None
        assert raw.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
    assert not list(path.parent.glob(path.name + ".pre-schema-4-*"))
    assert not list(path.parent.glob("." + path.name + ".pre-schema-4-*"))
    with SQLiteProgramStore(path) as recovered:
        assert recovered.startup_evidence.schema_version == 6


def test_schema5_interruption_retains_original_backup_and_rows(
    sqlite_tmp_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = install_pinned_reader(tmp_path, commit_id=PINNED_PRE_ORDINARY_ID)
    path = sqlite_tmp_path / "schema5.sqlite3"
    subprocess.run([str(reader), "-I", "-c", CREATE_LEGACY, str(path), "5"], check=True)  # noqa: S603 - Pinned synthetic reader.
    with closing(sqlite3.connect(path)) as raw:
        original = tuple(raw.iterdump())
    migrate = cast(Callable[[SQLiteProgramStore], None], inspect.getattr_static(SQLiteProgramStore, "_migrate_5_to_6"))

    def interrupted(store: SQLiteProgramStore) -> None:
        migrate(store)
        raise RuntimeError("synthetic schema-6 interruption")

    with monkeypatch.context() as failure:
        failure.setattr(SQLiteProgramStore, "_migrate_5_to_6", interrupted)
        with pytest.raises(RuntimeError, match="schema-6 interruption"):
            SQLiteProgramStore(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
        assert tuple(raw.iterdump()) == original
    backups = list(path.parent.glob(path.name + ".pre-schema-6-*"))
    assert len(backups) == 1
    with SQLiteProgramStore(path) as store:
        assert store.startup_evidence.schema_version == 6
        assert store.artifact("legacy-evidence", "original") == b"synthetic retained original bytes"
    assert list(path.parent.glob(path.name + ".pre-schema-6-*")) == backups


@pytest.mark.parametrize("corruption", ["schema", "omitted-artifact", "artifact-bytes"])
def test_current_schema_backup_validates_version_and_complete_artifact_inventory(
    legacy_database: Path, corruption: str
) -> None:
    path = legacy_database
    with SQLiteProgramStore(path) as store:
        bundle = store.backup(path.parent / "backup-three")
    document = json.loads(bundle.manifest.read_text())
    if corruption == "schema":
        document["schema_version"] = 2
        bundle.manifest.write_text(json.dumps(document))
    elif corruption == "omitted-artifact":
        document["artifacts"] = []
        bundle.manifest.write_text(json.dumps(document))
    else:
        (bundle.directory / "kernel.sqlite3.artifacts" / document["artifacts"][0]["digest"]).write_bytes(b"corrupted")
    target = path.parent / "must-not-restore.sqlite3"
    with pytest.raises(CorruptHistory):
        SQLiteProgramStore.restore_backup(bundle, target)
    assert not target.exists()
