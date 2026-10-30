# SPDX-License-Identifier: Apache-2.0
import os
from contextlib import closing
from pathlib import Path

import pytest
from ordinary_flow_fixture import run_ordinary_flow

from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.intake import IntakeRefused
from creatidy_kernel.core.ownership import OwnershipConflict
from creatidy_kernel.ports.ownership import WorkOwnership


@pytest.mark.parametrize("mode", ["pull", "managed", "mixed"])
@pytest.mark.parametrize("exercise", ["normal", "reopen", "remediation", "lost-reply"])
def test_ordinary_installed_handlers_with_synthetic_external_peers(tmp_path: Path, mode: str, exercise: str) -> None:
    result = run_ordinary_flow(tmp_path / mode, None, mode=mode, exercise=exercise)
    assert result["status"] == "completed"
    assert result["satisfactions"] == 2
    assert result["native_sends"] == 4
    assert result["native_kilo"] is False


def test_materialization_cannot_precede_atomic_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    initialize = OrdinaryGitWorkspace.initialize

    def guarded(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec) -> str:
        program = workspace.store.load(attempt.program_id)
        assert any(item.attempt_id == attempt.attempt_id for item in program.attempts), (
            "workspace mutation began before the Attempt and resource claim were admitted"
        )
        return initialize(workspace, attempt)

    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", guarded)
    run_ordinary_flow(tmp_path / "claimed-before-materialization", None, mode="pull")


def test_changed_preparation_is_preserved_and_not_auto_attributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize = OrdinaryGitWorkspace.initialize
    touched: list[Path] = []

    def edit_after_preparation(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec) -> str:
        subject = initialize(workspace, attempt)
        target = workspace.workspace.path / "alpha.py"
        target.write_bytes(b"manual change must be retained\n")
        touched.append(target)
        return subject

    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", edit_after_preparation)
    with pytest.raises(IntakeRefused, match="prepared workspace changed"):
        run_ordinary_flow(tmp_path / "changed-preparation", None, mode="pull")
    assert touched[0].read_bytes() == b"manual change must be retained\n"


def test_refused_claim_never_materializes_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OwnershipConflict("synthetic resource conflict")

    def forbidden(*_args: object, **_kwargs: object) -> str:
        pytest.fail("controller wrote the workspace after a refused claim")

    monkeypatch.setattr(WorkOwnership, "claim", refuse)
    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", forbidden)
    with pytest.raises(OwnershipConflict, match="resource conflict"):
        run_ordinary_flow(tmp_path / "refused-claim", None, mode="pull")


def test_interrupted_preparation_retains_claim_and_unknown_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[Path] = []
    attempts: list[str] = []

    def interrupted(workspace: OrdinaryGitWorkspace, attempt: AttemptSpec) -> str:
        target = workspace.workspace.path / "interrupted.txt"
        target.write_bytes(b"preserve interrupted controller data\n")
        touched.append(target)
        attempts.append(attempt.attempt_id)
        raise RuntimeError("synthetic preparation interruption")

    monkeypatch.setattr(OrdinaryGitWorkspace, "initialize", interrupted)
    root = tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="preparation interruption"):
        run_ordinary_flow(root, None, mode="pull")
    assert touched[0].read_bytes() == b"preserve interrupted controller data\n"
    with closing(SQLiteProgramStore(root / "state.db")) as store:
        assert store.operations("work-budget:v1:ownership-claim:")
        assert not store.operations("work-budget:v1:ownership-release:")
        assert store.operation(f"runtime:{attempts[0]}").attempts == 0
        assert store.find_artifact(f"ordinary-workspace:{attempts[0]}", "prepared") is None


@pytest.mark.parametrize("mode", ["pull", "managed", "mixed"])
def test_ordinary_two_obligation_program_with_real_native_cold_review(tmp_path: Path, mode: str) -> None:
    binary = os.environ.get("CREATIDY_KILO_NATIVE_TEST_BINARY")
    if not binary:
        pytest.skip("explicit pinned native binary required; no live inference or automatic dependency download")
    result = run_ordinary_flow(tmp_path / mode, Path(binary), mode=mode)
    assert result["status"] == "completed"
    assert result["satisfactions"] == 2
    assert result["native_sends"] == 4


@pytest.mark.parametrize("mode", ["pull", "managed"])
def test_ordinary_stop_revokes_authority_without_accepting_or_publishing(tmp_path: Path, mode: str) -> None:
    result = run_ordinary_flow(tmp_path / mode, None, mode=mode, exercise="stop")
    assert result["status"] == "stopped"
    assert result["native_sends"] == 0
    if mode == "pull":
        assert result["settled"] is False
    else:
        assert result["settled"] == "cancelled"
