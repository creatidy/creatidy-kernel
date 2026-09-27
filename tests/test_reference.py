# SPDX-License-Identifier: Apache-2.0
"""Offline end-to-end application recovery proof, no live service or inference."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from creatidy_kernel.adapters.reference import ReferenceChecks, ReferenceInterrupted, export_reference, run_reference
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.verification import Evidence, EvidenceSubject

pytest_plugins = ["test_sqlite_store"]


def test_two_nodes_reconstruct_and_export(sqlite_tmp_path: Path) -> None:
    result = run_reference(sqlite_tmp_path, owner_approved=True)
    assert result["status"] == "completed"
    assert len(cast(list[object], result["accepted"])) == 2
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = store.load("reference")
        first = program.satisfactions[0]
        second = program.attempt("reference:second").spec.effective_inputs[0]
        assert second.reference == first.output_bindings[0].reference
        assert second.satisfaction_id == first.reference_id
        assert store.operation("runtime:reference:first").attempts == 1
    repeated = run_reference(sqlite_tmp_path, owner_approved=True)
    assert repeated == result
    assert export_reference(sqlite_tmp_path)["accepted"] == result["accepted"]


@pytest.mark.parametrize("fault", ["commit", "send", "receipt"])
def test_restart_boundaries(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.reference.time.time", lambda: 10)
    with pytest.raises(ReferenceInterrupted):
        run_reference(sqlite_tmp_path, owner_approved=True, fault=fault)
    monkeypatch.setattr("creatidy_kernel.adapters.reference.time.time", lambda: 12)
    result = run_reference(sqlite_tmp_path, owner_approved=True)
    assert result["status"] == "completed"
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        assert store.operation("runtime:reference:first").attempts == 1


def test_lost_context_does_not_advance_or_retry(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.reference.time.time", lambda: 10)
    with pytest.raises(ReferenceInterrupted):
        run_reference(sqlite_tmp_path, owner_approved=True, fault="send")
    monkeypatch.setattr("creatidy_kernel.adapters.reference.time.time", lambda: 12)
    result = run_reference(sqlite_tmp_path, owner_approved=True, fault="lost-context")
    assert result["condition"] == "unknown"
    assert result["accepted"] == []
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        assert store.operation("runtime:reference:first").attempts == 1
    assert run_reference(sqlite_tmp_path, owner_approved=True)["status"] == "completed"


def test_no_approval_no_files(sqlite_tmp_path: Path) -> None:
    directory = sqlite_tmp_path / "denied"
    with pytest.raises(ValueError, match="approval"):
        run_reference(directory, owner_approved=False)
    assert not directory.exists()


def test_independent_failure_never_enables_successor(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = ReferenceChecks.check

    def fail(self: ReferenceChecks, name: str, subject: EvidenceSubject) -> Evidence:
        return replace(original(self, name, subject), passed=False)

    monkeypatch.setattr(ReferenceChecks, "check", fail)
    result = run_reference(sqlite_tmp_path, owner_approved=True)
    assert result["condition"] == "rejected"
    assert result["accepted"] == []
    assert len(cast(list[object], result["attempts"])) == 1


def test_stale_receipt_cannot_change_accepted_work(sqlite_tmp_path: Path) -> None:
    result = run_reference(sqlite_tmp_path, owner_approved=True)
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        with pytest.raises(OperationConflict, match="stale"):
            store.observe("runtime:reference:first", 0, "stale-worker", "terminal", reference="wrong")
    assert run_reference(sqlite_tmp_path, owner_approved=True) == result


def test_export_does_not_create_state(sqlite_tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_reference(sqlite_tmp_path)
    assert not (sqlite_tmp_path / "kernel.sqlite3").exists()
