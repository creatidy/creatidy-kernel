# SPDX-License-Identifier: Apache-2.0
"""Unresolved TOOL ownership blocks workspace reuse, not exact runtime STOP cleanup."""

import json
import os
import selectors
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest
from test_task_execution import ALLOCATION, FixtureConnection, deflake_edit, fixture_task, make_source
from test_work_budget import Clock

from creatidy_kernel.adapters import task_execution
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading

pytest_plugins = ["test_sqlite_store"]


class OwnedRuntime(FixtureConnection):
    """A terminal turn may still own a live process group; only its original owner retires it."""

    def __init__(self) -> None:
        super().__init__(deflake_edit)
        self.close_calls = 0
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )

    def close(self) -> None:
        self.close_calls += 1
        self.retire_fixture()
        self.closed = True
        self.owned_group_settled = self.process.poll() is not None

    def retire_fixture(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)


@pytest.mark.parametrize("trigger", ["stop", "boot-expiry"])
@pytest.mark.parametrize("stage", ["reopen", "same-invocation"])
@pytest.mark.parametrize("runtime_running", [False, True])
def test_unknown_command_does_not_preempt_original_runtime_cleanup(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str, stage: str, runtime_running: bool
) -> None:
    monkeypatch.setattr(task_execution.time, "time", lambda: 100)
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    clock = Clock()
    runtime = OwnedRuntime()
    owners: dict[str, task_execution.OwnedVerificationCommand] = {}
    stop = False
    command_calls = 0

    def forbid_reuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unresolved command must forbid workspace preparation/restoration/collection")

    def stop_or_expire() -> None:
        nonlocal stop
        if runtime_running:
            runtime.status = "inProgress"
        if trigger == "stop":
            stop = True
        else:
            # Wall time remains valid; suspend-inclusive original boot expiry alone refuses authority.
            clock.reading = BootClockReading(100, 12_000_000_000, "synthetic-boot-a")

    def abandoned(
        workspace: Path,
        command: task_execution.VerificationCommand,
        repeat: int,
        supplied: Mapping[str, str],
        authorized: Callable[[], bool],
        max_bytes: int,
        *,
        owner: task_execution.OwnedVerificationCommand | None = None,
    ) -> dict[str, object]:
        nonlocal command_calls
        command_calls += 1
        assert owner is not None and runtime.process.poll() is None
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib,time; pathlib.Path('protected-command-output').write_bytes(b'retain unknown tool'); "
                "print('ready', flush=True); time.sleep(60)",
            ],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        owner.attach(process)
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(5) and process.stdout.readline() == b"ready\n"
        monkeypatch.setattr(task_execution, "prepare_workspace", forbid_reuse)
        monkeypatch.setattr(task_execution, "_install_candidate", forbid_reuse)
        monkeypatch.setattr(task_execution.TaskCollector, "runtime_candidate", forbid_reuse)
        monkeypatch.setattr(task_execution.TaskCollector, "collect", forbid_reuse)
        if stage == "reopen":
            raise task_execution.TaskInterrupted("synthetic controller loss after command dispatch")
        stop_or_expire()
        return {
            "argv": list(command.argv),
            "repeat": repeat,
            "exit_code": None,
            "timed_out": True,
            "duration_ms": 1,
            "observed_at": 100,
            "stdout_tail": "",
            "stderr_tail": "",
            "retained_bytes": 0,
            "output_exhausted": False,
            "authority_lost": False,
            "owned_group_settled": False,
        }

    def run(client: OwnedRuntime, *, retain_command_owner: bool = False) -> dict[str, object]:
        return task_execution.run_task(
            control,
            task=fixture_task(repeats=1),
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=client,
            version=client.version,
            allocation=ALLOCATION,
            deadline=110,
            boot_clock=clock,
            cancellation_requested=lambda: stop,
            command_owners=owners if retain_command_owner else None,
        )

    monkeypatch.setattr(task_execution, "_run_command", abandoned)
    try:
        if stage == "reopen":
            with pytest.raises(task_execution.TaskInterrupted, match="controller loss"):
                run(runtime, retain_command_owner=True)
            assert runtime.process.poll() is None and runtime.close_calls == 0
            stop_or_expire()
            result = run(runtime)
        else:
            result = run(runtime, retain_command_owner=True)
        key, owner = next(iter(owners.items()))
        assert owner.process is not None and owner.process.poll() is None
        assert result["condition"] == "cancel_uncertain" and result["unresolved_commands"] == [key]
        assert "acceptance" not in result and "pr" not in result and command_calls == runtime.starts == 1
        assert runtime.close_calls == 1 and runtime.process.poll() is not None
        # A fresh runtime recovers the handle, not the prior in-memory terminal flag.
        assert runtime.interrupts == int(stage == "reopen")
        marker = control / "workspace/protected-command-output"
        assert marker.read_bytes() == b"retain unknown tool"
        cancellation_id = f"cancel:{task_execution.ATTEMPT_ID}"
        with SQLiteProgramStore(control / "kernel.sqlite3", boot_clock=clock) as store:
            operation = store.operation(task_execution.OPERATION_ID)
            cancellation = store.operation(cancellation_id)
            receipt = store.artifact(task_execution.OPERATION_ID, "runtime-receipt")
            assert store.artifact(cancellation_id, "target") == receipt
            settlement = json.loads(store.artifact(cancellation_id, "owned-group-settlement"))
            assert settlement["operation_id"] == operation.operation_id
            assert settlement["runtime_fence"] == operation.fence
            assert settlement["cancellation_fence"] == cancellation.fence == 1
            assert (
                settlement["owned_group_reference"]
                == settlement["expected_group_reference"]
                == runtime.owned_group_reference
            )
            assert settlement["owned_group_settled"] is settlement["result_reconciled"] is True
            assert cancellation.status == "accepted" and operation.status == "terminal"
            original_binding = store.artifact(task_execution.OPERATION_ID, f"command-owner:{key}")
            original_result = store.find_artifact(task_execution.OPERATION_ID, key)
            assert not store.operations("acceptance:") and not store.operations(task_execution.PR_OPERATION)
            assert not any(
                json.loads(record.request_json)["request"].get("operation") == key
                for record in store.operations("work-budget:v1:settlement:")
            )
        for _ in range(2):
            repeated = run(runtime)
            assert repeated["condition"] == "cancel_uncertain" and repeated["unresolved_commands"] == [key]
            assert "acceptance" not in repeated and "pr" not in repeated
        replacement = OwnedRuntime()
        try:
            repeated = run(replacement)
            assert repeated["condition"] == "cancel_uncertain" and replacement.close_calls == replacement.starts == 0
            assert replacement.process.poll() is None
        finally:
            replacement.retire_fixture()
        assert runtime.close_calls == command_calls == runtime.starts == 1
        assert runtime.interrupts == int(stage == "reopen")
        assert owner.process.poll() is None and marker.read_bytes() == b"retain unknown tool"
        with SQLiteProgramStore(control / "kernel.sqlite3", boot_clock=clock) as store:
            assert store.artifact(task_execution.OPERATION_ID, f"command-owner:{key}") == original_binding
            assert store.find_artifact(task_execution.OPERATION_ID, key) == original_result
            assert store.operation(cancellation_id).attempts == 1
            assert (
                cast(list[object], store.work_budget_status(task_execution.PROGRAM_ID)["usage_history"])
                == cast(dict[str, object], result["work_budget"])["usage_history"]
            )
    finally:
        runtime.retire_fixture()
        for owner in owners.values():
            owner.retire()
            if owner.process is not None:
                if owner.process.stdout is not None:
                    owner.process.stdout.close()
                if owner.process.stderr is not None:
                    owner.process.stderr.close()
