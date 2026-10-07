# SPDX-License-Identifier: Apache-2.0
"""Actual candidate execution no-effect proof, using controller-owned Git fixtures."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_task_execution import FixtureConnection, deflake_edit, fixture_task, make_source, run

from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.task_execution import TaskChecks, VerificationCommand
from creatidy_kernel.core.verification import Evidence, EvidenceSubject

pytest_plugins = ["test_sqlite_store"]


def test_actual_changed_make_recipe_has_no_effect(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    (source / "Makefile").write_text("check:\n\t@true\n")
    reference_git(source, "add", "Makefile")
    reference_git(source, "commit", "-m", "synthetic trusted check recipe")
    marker = sqlite_tmp_path / "rejected-make-effect"
    task = replace(fixture_task(repeats=1), verification=(VerificationCommand(("/usr/bin/make", "check"), 30),))

    def edit(workspace: Path) -> None:
        deflake_edit(workspace)
        (workspace / "Makefile").write_text(f"check:\n\t@touch {marker}\n")

    result = run(sqlite_tmp_path / "control", source, FixtureConnection(edit), task=task)
    assert result["condition"] == "rejected"
    assert not marker.exists()
    assert "verification-commands" not in cast(dict[str, object], result["verification"])


@pytest.mark.parametrize("forbidden", ("Makefile", "pyproject.toml", "tools/check.py", "AGENTS.md"))
def test_forbidden_verification_tooling_cannot_run(sqlite_tmp_path: Path, forbidden: str) -> None:
    marker = sqlite_tmp_path / "unauthorized-effect"
    task = fixture_task(repeats=1)
    command = replace(
        task.verification[0], argv=(task.verification[0].argv[0], "-c", f"open({str(marker)!r},'w').close()")
    )
    task = replace(task, verification=(command,))
    source = make_source(sqlite_tmp_path)

    def edit(workspace: Path) -> None:
        deflake_edit(workspace)
        target = workspace / forbidden
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("candidate-controlled gate replacement\n")

    result = run(sqlite_tmp_path / "control", source, FixtureConnection(edit), task=task)
    assert result["condition"] == "rejected"
    assert not marker.exists()
    assert "verification-commands" not in cast(dict[str, object], result["verification"])


def test_direct_command_call_cannot_bypass_structural_gate(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = sqlite_tmp_path / "unauthorized-effect"
    source = make_source(sqlite_tmp_path)
    task = fixture_task(repeats=1)
    command = replace(
        task.verification[0], argv=(task.verification[0].argv[0], "-c", f"open({str(marker)!r},'w').close()")
    )
    task = replace(task, verification=(command,))
    called: list[bool] = []
    original = TaskChecks.check

    def direct(checks: TaskChecks, name: str, subject: EvidenceSubject) -> Evidence | None:
        if name == "changed-paths":
            # Even a direct request cannot run the rejected test code.
            result = original(checks, "verification-commands", subject)
            assert result is not None and not result.passed
            called.append(True)
        return original(checks, name, subject)

    monkeypatch.setattr(TaskChecks, "check", direct)

    def edit(workspace: Path) -> None:
        target = workspace / "tests/test_e2e_execution.py"
        target.write_text("print('removed required scenarios')\n")

    result = run(sqlite_tmp_path / "control", source, FixtureConnection(edit), task=task)
    assert result["condition"] == "rejected" and called
    assert not marker.exists()
