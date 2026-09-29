# SPDX-License-Identifier: Apache-2.0
"""The command interface delegates lifecycle decisions to the application."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.cli import main

pytest_plugins = ["test_sqlite_store"]


def test_reference_delegates_explicit_approval_and_fault(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("creatidy_kernel.adapters.cli.run_reference", return_value={"status": "paused"}) as run:
        assert main(["reference", "--data-dir", "state", "--approve", "--fault", "send"]) == 0
        run.assert_called_once_with(Path("state"), owner_approved=True, fault="send")
    assert json.loads(capsys.readouterr().out) == {"status": "paused"}


def test_cli_does_not_invent_approval(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("creatidy_kernel.adapters.cli.run_reference", side_effect=ValueError("approval required")) as run:
        assert main(["reference", "--data-dir", "state"]) == 1
        run.assert_called_once_with(Path("state"), owner_approved=False, fault=None)
    output = capsys.readouterr()
    assert output.out == ""
    assert "approval required" in output.err


def test_export_never_dispatches(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("creatidy_kernel.adapters.cli.export_reference", return_value={"attempts": []}) as export,
        patch("creatidy_kernel.adapters.cli.run_reference") as run,
    ):
        assert main(["export", "--data-dir", "state"]) == 0
        export.assert_called_once_with(Path("state"))
        run.assert_not_called()
    result: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert result == {"attempts": []}


@pytest.mark.parametrize("args", [[], ["merge"], ["reference"], ["reference", "--data-dir", "state", "--live"]])
def test_unsupported_commands_fail_closed(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def test_cli_offline_reference_and_export(sqlite_tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = sqlite_tmp_path / "reference"
    arguments = ["reference", "--data-dir", str(directory), "--approve"]
    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "completed"
    assert len(first["attempts"]) == len(first["accepted"]) == 2
    assert first["pr"]["status"] == "accepted"
    assert first["pr"]["mode"] == "synthetic"
    assert not first["automatic_merge"] and not first["automatic_deploy"]
    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["attempts"] == first["attempts"]
    assert second["pr"]["reference"] == first["pr"]["reference"]
    assert main(["export", "--data-dir", str(directory)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["attempts"] == first["attempts"]


def test_cli_unapproved_reference_creates_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    directory = tmp_path / "unapproved"
    assert main(["reference", "--data-dir", str(directory)]) == 1
    assert not directory.exists()
    assert "approval" in capsys.readouterr().err


@pytest.mark.parametrize("boundary", ["commit", "send", "receipt"])
def test_cli_resumes_after_runtime_fault(
    sqlite_tmp_path: Path, capsys: pytest.CaptureFixture[str], boundary: str
) -> None:
    arguments = ["reference", "--data-dir", str(sqlite_tmp_path / "reference"), "--approve"]
    with patch("creatidy_kernel.adapters.reference.time.time", return_value=100):
        assert main([*arguments, "--fault", boundary]) == 1
    assert "interrupted" in capsys.readouterr().err
    with patch("creatidy_kernel.adapters.reference.time.time", return_value=200):
        assert main(arguments) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["status"] == "completed"
    assert len(recovered["attempts"]) == 2
    assert recovered["interruptions"][0]["boundary"] == boundary


def test_cli_dogfood_requires_complete_live_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert (
            main(
                [
                    "dogfood",
                    "run",
                    "--data-dir",
                    "state",
                    "--repo",
                    "/var/empty/repo",
                    "--approve",
                    "--trusted-development",
                ]
            )
            == 1
        )
    output = capsys.readouterr()
    assert output.out == ""
    assert "missing: CREATIDY_DOGFOOD" in output.err


def test_cli_dogfood_does_not_invent_approval(capsys: pytest.CaptureFixture[str]) -> None:
    environment = {
        "CREATIDY_DOGFOOD_ROUTER_URL": "https://router.invalid",
        "CREATIDY_DOGFOOD_RUNTIME_BINDING": "zai/glm-5.3",
        "CREATIDY_DOGFOOD_CODEX_BIN": "/usr/local/bin/codex",
        "CREATIDY_DOGFOOD_CODEX_VERSION": "0.45.0",
        "CREATIDY_DOGFOOD_FORGE_API": "https://forge.invalid/api/v1",
        "CREATIDY_DOGFOOD_FORGE_REMOTE": "https://forge.invalid/BioMedical-IT/scarcity-router.git",
        "CREATIDY_DOGFOOD_FORGE_TOKEN": "synthetic-token",
    }
    with patch.dict("os.environ", environment, clear=True):
        assert main(["dogfood", "run", "--data-dir", "state", "--repo", "/var/empty/repo"]) == 1
    output = capsys.readouterr()
    assert "--approve" in output.err


def test_cli_dogfood_status_reports_missing_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["dogfood", "status", "--data-dir", str(tmp_path / "none")]) == 1
    assert "dogfood database does not exist" in capsys.readouterr().err


def test_cli_dogfood_run_prints_lifecycle(sqlite_tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from test_dogfood import (
        EFFORT_ALLOCATION,
        FixtureConnection,
        deflake_edit,
        fixture_task,
        make_forge,
        make_source,
    )

    from creatidy_kernel.adapters.dogfood import DogfoodLiveComponents, DogfoodTaskSpec
    from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
    from creatidy_kernel.adapters.reference import reference_git

    source = make_source(sqlite_tmp_path)
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    forge, _transport = make_forge(base)
    connection = FixtureConnection(deflake_edit, effort="low")
    # The controller-owned support evidence reaches run_dogfood through the CLI
    # composition; the explicit-effort allocation is executable only with it.
    components = DogfoodLiveComponents(
        allocator=FixedAllocator(EFFORT_ALLOCATION),
        connection_factory=lambda: connection,
        forge_factory=lambda _directory: forge,
        supported_efforts=frozenset({("zai", "glm-5.3", "low")}),
    )

    def task(expected_base_sha: str | None = None) -> DogfoodTaskSpec:
        return fixture_task(expected_base_sha=expected_base_sha)

    tasks = {"143": task}
    control = sqlite_tmp_path / "control"
    with (
        patch("creatidy_kernel.adapters.cli.compose_dogfood_live", return_value=components),
        patch("creatidy_kernel.adapters.cli.DOGFOOD_TASKS", tasks),
    ):
        assert (
            main(
                [
                    "dogfood",
                    "run",
                    "--data-dir",
                    str(control),
                    "--repo",
                    str(source),
                    "--task",
                    "143",
                    "--approve",
                    "--trusted-development",
                ]
            )
            == 0
        )
    output = capsys.readouterr().out
    assert "task admitted: owner-approved frozen dogfood task" in output
    assert "NO MERGE / NO DEPLOY" in output
    assert "condition=accepted" in output
