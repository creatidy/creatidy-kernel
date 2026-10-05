# SPDX-License-Identifier: Apache-2.0
"""The command interface delegates lifecycle decisions to the application."""

import json
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any, cast
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


def test_cli_task_requires_complete_live_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert (
            main(
                [
                    "task",
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
    assert "missing: CREATIDY_KERNEL" in output.err


def test_cli_task_does_not_invent_approval(capsys: pytest.CaptureFixture[str]) -> None:
    environment = {
        "CREATIDY_KERNEL_ROUTER_URL": "https://router.invalid",
        "CREATIDY_KERNEL_RUNTIME_BINDING": "zai/glm-5.3",
        "CREATIDY_KERNEL_CODEX_BIN": "/usr/local/bin/codex",
        "CREATIDY_KERNEL_CODEX_VERSION": "0.45.0",
        "CREATIDY_KERNEL_FORGE_API": "https://forge.invalid/api/v1",
        "CREATIDY_KERNEL_FORGE_REMOTE": "https://forge.invalid/BioMedical-IT/scarcity-router.git",
        "CREATIDY_KERNEL_FORGE_TOKEN": "synthetic-token",
    }
    with patch.dict("os.environ", environment, clear=True):
        assert main(["task", "run", "--data-dir", "state", "--repo", "/var/empty/repo"]) == 1
    output = capsys.readouterr()
    assert "--approve" in output.err


def test_cli_task_status_reports_missing_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["task", "status", "--data-dir", str(tmp_path / "none")]) == 1
    assert "task database does not exist" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("initial_reads", "interrupt"), [(0, False), (5, False), (5, True), (5, "rpc"), (0, "startup")]
)
def test_cli_task_run_prints_lifecycle(
    sqlite_tmp_path: Path, capsys: pytest.CaptureFixture[str], initial_reads: int, interrupt: bool | str
) -> None:
    from test_task_execution import (
        EFFORT_ALLOCATION,
        FixtureConnection,
        deflake_edit,
        export_task,
        fixture_task,
        live_environment,
        make_forge,
        make_source,
    )

    from creatidy_kernel.adapters.codex_stdio import CodexStdio
    from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
    from creatidy_kernel.adapters.reference import reference_git
    from creatidy_kernel.adapters.task_execution import LiveTaskComponents, TaskSpec

    source = make_source(sqlite_tmp_path)
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    forge, _transport = make_forge(base)

    class OwnedConnection(FixtureConnection, CodexStdio):
        reads = 0
        closed = False
        events: list[str]

        def __init__(self) -> None:
            FixtureConnection.__init__(self, deflake_edit, effort="low")
            self.events = []
            if interrupt in {"rpc", "startup"}:
                self._lock = threading.Lock()
                self._process = cast("subprocess.Popen[bytes]", object())

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if interrupt in {"rpc", "startup"}:
                return CodexStdio.request(self, method, params)
            return self._exchange(method, params)

        def _exchange(self, method: str, params: dict[str, object]) -> dict[str, object]:
            assert not self.closed
            self.events.append(method)
            if method == "turn/interrupt":
                self.status = "interrupted"
                return {}
            if method == "thread/read":
                self.reads += 1
                if interrupt == "rpc" and self.reads == 4:
                    signal.raise_signal(signal.SIGINT)
                if self.status != "interrupted":
                    self.status = "inProgress" if self.reads <= initial_reads else "completed"
            result = FixtureConnection.request(self, method, params)
            if interrupt == "startup" and method == "thread/start":
                signal.raise_signal(signal.SIGINT)
            return result

        def _close_unlocked(self) -> None:
            assert "cancellation" in export_task(control)
            self.events.append("owned-teardown")
            self._process = None
            self.closed = True

        def close(self) -> None:
            assert self.status in {"completed", "interrupted"}
            if interrupt in {"rpc", "startup"}:
                CodexStdio.close(self)
            else:
                self.closed = True

    connection = OwnedConnection()
    original_sigint = signal.getsignal(signal.SIGINT)
    # The controller-owned support evidence reaches run_task through the CLI
    # composition; the explicit-effort allocation is executable only with it.
    components = LiveTaskComponents(
        allocator=FixedAllocator(EFFORT_ALLOCATION),
        connection_factory=lambda: connection,
        forge_factory=lambda _directory: forge,
        supported_efforts=frozenset({("zai", "glm-5.3", "low")}),
    )

    def task(expected_base_sha: str | None = None) -> TaskSpec:
        return fixture_task(expected_base_sha=expected_base_sha)

    tasks = {"143": task}
    control = sqlite_tmp_path / "control"
    with (
        patch.dict("os.environ", live_environment("zai/glm-5.3/low"), clear=True),
        patch("creatidy_kernel.adapters.cli.compose_task_live", return_value=components),
        patch("creatidy_kernel.adapters.cli.TASKS", tasks),
        patch("creatidy_kernel.adapters.cli.time.sleep", side_effect=KeyboardInterrupt if interrupt is True else None),
    ):
        assert (
            main(
                [
                    "task",
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
    assert "task admitted: owner-approved frozen task" in output
    if interrupt:
        assert ("condition=cancel_uncertain" if interrupt == "startup" else "condition=cancelled") in output
        assert "acceptance" not in export_task(control)
        assert "pr" not in export_task(control)
    else:
        assert "NO MERGE / NO DEPLOY" in output
        assert "condition=accepted" in output
    assert connection.starts == 1
    assert connection.closed
    assert signal.getsignal(signal.SIGINT) == original_sigint
    if interrupt == "rpc":
        assert connection.events.index("turn/interrupt") < connection.events.index("owned-teardown")
    if interrupt == "startup":
        assert "turn/start" not in connection.events
        assert "turn/interrupt" not in connection.events
        assert "cancellation" in export_task(control)
