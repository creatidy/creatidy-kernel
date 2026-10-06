# SPDX-License-Identifier: Apache-2.0
"""Bounded task composition proof: real Git fixtures, native-shaped fakes, no paid calls."""

import inspect
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

from creatidy_kernel.adapters.codex_runtime import CodexRejected
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import (
    CODEX_METHODS,
    TASKS,
    LiveTaskComponents,
    TaskChecks,
    TaskInterrupted,
    TaskRuntimeConfig,
    TaskSpec,
    VerificationCommand,
    export_task,
    run_task,
    scarcity_router_143_task,
    task_status,
)
from creatidy_kernel.adapters.task_execution import (
    compose_task_live as _compose_task_live,
)
from creatidy_kernel.adapters.task_execution import git_text as task_git
from creatidy_kernel.core.domain import DomainCommandType, FinishAttempt, Program
from creatidy_kernel.core.execution import UnsupportedExecution
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.core.verification import Evidence, EvidenceSubject
from creatidy_kernel.ports.forge import Forge
from creatidy_kernel.ports.program_store import OperationRecord
from creatidy_kernel.ports.resources import ResourceAllocator

pytest_plugins = ["test_sqlite_store", "test_codex_stdio"]


def compose_task_live(environ: Mapping[str, str], task: TaskSpec) -> LiveTaskComponents:
    return _compose_task_live(TaskRuntimeConfig.parse(environ), task)


REPOSITORY = Reference("forgejo:BioMedical-IT/scarcity-router")
ALLOCATION = Allocation("codex", "zai", "glm-5.3", frozenset({"reference"}), 128, "fixture selection")
EFFORT_ALLOCATION = Allocation("codex", "zai", "glm-5.3", frozenset({"reference"}), 128, "fixture selection", "low")

# The frozen assertions mirror the real Scarcity Router source forms at the D1-03
# verified base (59538e9): the bystander ownership assertion is the reversed-argument
# form self.assertEqual([], bystander.cancels) (tests/test_e2e_execution.py:908).
BASE_TEST_FILE = """import time
import unittest


class _Worker:
    def __init__(self):
        self.cancels = []


class _Socket:
    def __init__(self, worker):
        self._worker = worker

    def shutdown(self, how):
        self._worker.cancels.append(1)

    def recv(self, size):
        return b""


class CancellationTests(unittest.TestCase):
    def _scenario(self):
        worker = _Worker()
        bystander = _Worker()
        raw = _Socket(worker)
        raw.shutdown(1)
        return worker, bystander, raw

    def test_scenario_11_client_disconnect_propagates_cancel(self):
        worker, bystander, raw = self._scenario()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not worker.cancels:
            try:
                piece = raw.recv(4096)
                if not piece:
                    break
            except (TimeoutError, OSError):
                continue
        self.assertTrue(worker.cancels, "cancel never reached the worker")
        self.assertEqual([], bystander.cancels)

    def test_scenario_11_cancel_reaches_exactly_the_owning_worker(self):
        worker, bystander, raw = self._scenario()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not worker.cancels:
            try:
                piece = raw.recv(4096)
                if not piece:
                    break
            except (TimeoutError, OSError):
                continue
        self.assertTrue(worker.cancels, "cancel never reached the worker")
        self.assertEqual([], bystander.cancels)


class StableTests(unittest.TestCase):
    def test_unrelated_behavior(self):
        self.assertTrue(True)
"""

RACY_BLOCK = """        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not worker.cancels:
            try:
                piece = raw.recv(4096)
                if not piece:
                    break
            except (TimeoutError, OSError):
                continue
"""

DEFLAKED_BLOCK = """        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not worker.cancels:
            time.sleep(0.01)
"""


def make_source(directory: Path) -> Path:
    source = directory / "source"
    source.mkdir(parents=True)
    reference_git(source, "init", "--initial-branch=develop")
    (source / "tests").mkdir()
    (source / "tests" / "__init__.py").write_text("")
    (source / "tests" / "test_e2e_execution.py").write_text(BASE_TEST_FILE)
    (source / "README.md").write_text("fixture repository\n")
    (source / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "fixture base")
    return source


def fixture_task(*, repeats: int = 8, expected_base_sha: str | None = None) -> TaskSpec:
    targeted = (sys.executable, "-m", "unittest", "tests.test_e2e_execution.CancellationTests")
    full = (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py")
    return TaskSpec(
        task_id="fixture-143",
        forge_repository=REPOSITORY.value,
        repository_url="https://forge.invalid/BioMedical-IT/scarcity-router",
        base_branch="develop",
        instruction="Deflake the scenario-11 cancellation tests using deterministic bounded synchronization.",
        repository_instructions="unittest-based offline fixture repository.",
        allowed_paths=frozenset({"tests/test_e2e_execution.py"}),
        verification=(
            VerificationCommand(targeted, 60),
            VerificationCommand(targeted, 60, repeats=repeats),
            VerificationCommand(full, 120),
        ),
        structural=scarcity_router_143_task().structural,
        expected_base_sha=expected_base_sha,
    )


class FixtureConnection:
    """Native-shaped app-server fake; the edit callback plays the trusted coding model."""

    version = "1.2.3"
    methods = frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"})

    def __init__(self, edit: Callable[[Path], None], *, provider: str = "zai", effort: str | None = None) -> None:
        self.edit = edit
        self.provider = provider
        self.effort = effort
        self.starts = 0
        self.status = "completed"
        self.cwd: str | None = None

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "thread/start":
            self.starts += 1
            self.cwd = str(params["cwd"])
            receipt: dict[str, object] = {
                "thread": {"id": f"thread-{self.starts}"},
                "model": "glm-5.3",
                "modelProvider": self.provider,
            }
            if self.effort is not None:
                receipt["reasoningEffort"] = self.effort
            return receipt
        if method == "turn/start":
            self.edit(Path(str(self.cwd)))
            return {"turn": {"id": f"turn-{self.starts}"}}
        if method == "thread/read":
            thread = str(params["threadId"])
            return {
                "thread": {"id": thread, "turns": [{"id": thread.replace("thread", "turn"), "status": self.status}]}
            }
        raise AssertionError(method)


def deflake_edit(path: Path) -> None:
    target = path / "tests" / "test_e2e_execution.py"
    text = target.read_text()
    assert RACY_BLOCK in text
    target.write_text(text.replace(RACY_BLOCK, DEFLAKED_BLOCK))


def make_forge(base: str) -> tuple[ForgejoForge, SyntheticForgeTransport]:
    transport = SyntheticForgeTransport(REPOSITORY)
    transport.branches["develop"] = base
    return ForgejoForge(transport, transport, lambda _: True), transport


def run(
    control: Path,
    source: Path,
    connection: FixtureConnection,
    *,
    task: TaskSpec | None = None,
    forge: Forge | None = None,
    base: str | None = None,
    allocation: Allocation | None = ALLOCATION,
    allocator: ResourceAllocator | None = None,
    supported_efforts: frozenset[tuple[str, str, str]] = frozenset(),
    fault: str | None = None,
    cancel_requested: bool = False,
    cancellation_requested: Callable[[], bool] | None = None,
) -> dict[str, object]:
    if forge is None:
        forge, _transport = make_forge(base or "0" * 40)
    return run_task(
        control,
        task=task or fixture_task(),
        source_repository=source,
        owner_approved=True,
        trusted_development_acknowledged=True,
        connection=connection,
        version=connection.version,
        deadline=None,
        allocation=allocation,
        allocator=allocator,
        forge_factory=lambda _directory: forge,
        supported_efforts=supported_efforts,
        fault=fault,
        cancel_requested=cancel_requested,
        cancellation_requested=cancellation_requested,
    )


def test_recovery_refuses_changed_runtime_version(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    connection = FixtureConnection(deflake_edit)
    connection.status = "inProgress"
    control = sqlite_tmp_path / "control"
    assert run(control, source, connection)["condition"] == "running"
    connection.version = "2.0.0"
    with pytest.raises(ValueError, match="envelope"):
        run(control, source, connection)
    assert connection.starts == 1


def test_fixed_effort_cannot_change_before_attempt_preparation(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit, effort="low")
    supports = frozenset({("zai", "glm-5.3", "low"), ("zai", "glm-5.3", "high")})
    with pytest.raises(TaskInterrupted):
        run(
            control,
            source,
            connection,
            allocation=EFFORT_ALLOCATION,
            supported_efforts=supports,
            fault="workspace-prepared",
        )
    high = Allocation(
        EFFORT_ALLOCATION.runtime_id,
        EFFORT_ALLOCATION.provider_id,
        EFFORT_ALLOCATION.model_id,
        EFFORT_ALLOCATION.capabilities,
        EFFORT_ALLOCATION.context_tokens,
        EFFORT_ALLOCATION.rationale,
        "high",
    )
    with pytest.raises(ValueError, match="fixed allocation envelope"):
        run(control, source, connection, allocation=high, supported_efforts=supports)
    assert connection.starts == 0
    assert (
        run(control, source, connection, allocation=EFFORT_ALLOCATION, supported_efforts=supports)["condition"]
        == "accepted"
    )
    assert connection.starts == 1


@pytest.mark.parametrize("fault", ["prepared", "artifacts", "started", "commit"])
def test_cancel_unclaimed_attempt_never_dispatches(sqlite_tmp_path: Path, fault: str) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    with pytest.raises(TaskInterrupted):
        run(control, source, connection, fault=fault)
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        original = store.operation("runtime:task_execution:change").request_json
    for _ in range(2):
        stopped = run(control, source, connection, cancel_requested=True)
        assert stopped["condition"] == "cancelled_no_dispatch"
        with SQLiteProgramStore(control / "kernel.sqlite3") as store:
            operation = store.operation("runtime:task_execution:change")
            assert operation.request_json == original
            assert operation.status == "intent" and operation.attempts == 0
            assert store.load("task_execution").attempt("task_execution:change").status.value == "cancelled"
    assert task_status(control)["condition"] == "cancelled_no_dispatch"
    assert connection.starts == 0


def test_failed_terminal_recovers_finish_without_native_context(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(lambda _path: None)
    connection.status = "failed"
    original = SQLiteProgramStore.admit

    class ProcessLoss(BaseException):
        pass

    def crash_finish(store: SQLiteProgramStore, program_id: str, key: str, command: DomainCommandType) -> Program:
        if isinstance(command, FinishAttempt):
            raise ProcessLoss()
        return original(store, program_id, key, command)

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteProgramStore, "admit", crash_finish)
        with pytest.raises(ProcessLoss):
            run(control, source, connection)

    class Missing(FixtureConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            raise AssertionError("terminal proof must not require another native read")

    for _ in range(2):
        restored = run(control, source, Missing(lambda _path: None))
        assert restored["condition"] == "terminal_no_candidate"
        assert cast("list[dict[str, object]]", restored["attempts"])[0]["attempt"] == {
            "attempt_id": "task_execution:change",
            "status": "finished",
        }
        assert "acceptance" not in restored


def test_lost_terminal_candidate_read_remains_recoverable(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"

    class FlakyCandidate(FixtureConnection):
        reads = 0

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "thread/read":
                self.reads += 1
                if self.reads == 4:
                    raise TimeoutError("synthetic lost candidate read")
            return super().request(method, params)

    connection = FlakyCandidate(deflake_edit)
    assert run(control, source, connection)["condition"] == "terminal_candidate_unknown"
    assert task_status(control)["condition"] == "terminal_candidate_unknown"
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        original = store.artifact("runtime:task_execution:change", "terminal-outcome")
        assert json.loads(original)["candidate"] == "unknown"
    assert run(control, source, connection)["condition"] == "accepted"
    assert connection.starts == 1
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        assert store.artifact("runtime:task_execution:change", "terminal-outcome") == original


def test_terminal_outcome_recovers_before_operation_publication(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(lambda _path: None)
    connection.status = "failed"
    original = SQLiteProgramStore.observe

    class ProcessLoss(BaseException):
        pass

    def crash_terminal(
        store: SQLiteProgramStore,
        operation_id: str,
        fence: int,
        observation_id: str,
        kind: str,
        *,
        reference: str | None = None,
    ) -> OperationRecord:
        if kind == "terminal":
            raise ProcessLoss()
        return original(store, operation_id, fence, observation_id, kind, reference=reference)

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteProgramStore, "observe", crash_terminal)
        with pytest.raises(ProcessLoss):
            run(control, source, connection)

    class Unavailable(FixtureConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            raise AssertionError("durable outcome must recover without a native read")

    recovered = run(control, source, Unavailable(lambda _path: None))
    assert recovered["condition"] == "terminal_no_candidate"
    assert "acceptance" not in recovered


def test_one_shot_cancellation_cannot_be_forgotten(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    pending = [False]

    class Pulse(FixtureConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            result = super().request(method, params)
            if method == "turn/start":
                pending[0] = True
            return result

    def consume() -> bool:
        value = pending[0]
        pending[0] = False
        return value

    result = run(control, source, Pulse(deflake_edit), cancellation_requested=consume)
    assert result["condition"] == "cancelled"
    assert "cancellation" in result
    assert "acceptance" not in result
    assert "pr" not in result


def test_partial_owned_cleanup_retains_workspace_and_unproven_stop(
    sqlite_tmp_path: Path, command: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"

    class Partial(CodexStdio):
        interrupts = 0
        starts = 0
        child = 0
        group = 0

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            result = super().request(method, params)
            if method == "turn/start":
                self.starts += 1
                self.child = int(str(result["child"]))
                self.group = int(str(result["group"]))
            if method == "turn/interrupt":
                assert params == {"threadId": "thread-1", "turnId": "turn-1"}
                self.interrupts += 1
            return result

    connection = Partial(
        command,
        "0.99.1",
        timeout=1,
        max_bytes=8192,
        schema_methods=CODEX_METHODS,
        schema_version="0.99.1",
        environment={"HOME": str(sqlite_tmp_path), "PATH": "/usr/bin:/bin", "KERNEL_LIFECYCLE": "1"},
        retain_notifications=False,
    )
    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        env={"HOME": str(sqlite_tmp_path), "PATH": "/usr/bin:/bin"},
    )
    original_killpg = os.killpg
    signals: list[int] = []

    def advance(*, cancel: bool = False) -> dict[str, object]:
        return run_task(
            control,
            task=fixture_task(),
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=None,
            allocation=ALLOCATION,
            forge_factory=lambda _path: make_forge("0" * 40)[0],
            cancel_requested=cancel,
        )

    def owned_signal(group: int, sig: int) -> None:
        assert group == connection.group != bystander.pid
        signals.append(sig)
        if sig == signal.SIGKILL:
            # The leader has exited after TERM but its stubborn child remains live.
            assert Path(f"/proc/{connection.child}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
            assert connection.owned_group_settled is not True
        original_killpg(group, sig)

    try:
        for _ in range(30):
            assert advance()["condition"] == "running"
        assert connection.starts == 1
        assert connection.notifications() == ()
        stopped = advance(cancel=True)
        assert stopped["condition"] == "cancelled"
        assert cast("dict[str, object]", stopped["cancellation"])["descendants"] == "unproven"
        assert "acceptance" not in stopped and "pr" not in stopped
        workspace = control / "workspace"
        assert workspace.exists()
        assert Path(f"/proc/{connection.child}").exists()
        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", owned_signal)
            connection.close()
        assert signal.SIGTERM in signals and signal.SIGKILL in signals
        assert bystander.poll() is None
        assert advance()["condition"] == "cancelled"
        assert workspace.exists()
        assert connection.interrupts == connection.starts == 1
        assert cast("dict[str, object]", export_task(control)["cancellation"])["descendants"] == "unproven"
    finally:
        connection.close()
        bystander.kill()
        bystander.wait(timeout=5)
        if connection.group:
            try:
                original_killpg(connection.group, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_terminal_cancel_observation_recovers_without_native_context(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"

    class Interrupted(FixtureConnection):
        interrupts = 0

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "turn/interrupt":
                self.interrupts += 1
                self.status = "interrupted"
                return {}
            return super().request(method, params)

    connection = Interrupted(deflake_edit)
    connection.status = "inProgress"
    assert run(control, source, connection)["condition"] == "running"
    with pytest.raises(TaskInterrupted):
        run(control, source, connection, cancel_requested=True, fault="cancel-observation")

    class Unavailable(FixtureConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            raise AssertionError("durable terminal cancellation must not issue another RPC")

    for _ in range(2):
        recovered = run(control, source, Unavailable(deflake_edit))
        assert recovered["condition"] == "cancelled"
        assert "acceptance" not in recovered
        assert cast("list[dict[str, object]]", recovered["attempts"])[0]["attempt"] == {
            "attempt_id": "task_execution:change",
            "status": "finished",
        }
    assert connection.interrupts == 1


@pytest.mark.parametrize("before_allocation", [False, True])
def test_cancel_before_attempt_is_stable_without_dispatch(sqlite_tmp_path: Path, before_allocation: bool) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    requested = [before_allocation]

    class StopAllocation(ResourceAllocator):
        calls = 0

        def select(self, request: ResourceRequest) -> Allocation:
            self.calls += 1
            requested[0] = True
            return ALLOCATION

    allocator = StopAllocation()
    connection = FixtureConnection(deflake_edit)
    first = run(
        control, source, connection, allocation=None, allocator=allocator, cancellation_requested=lambda: requested[0]
    )
    assert first["condition"] == "cancelled_no_dispatch"
    assert first["runtime_operation_absent"] is True
    assert first["attempts"] == []
    assert "cancellation" in first
    requested[0] = False
    for _ in range(2):
        restored = run(control, source, connection, allocation=None, allocator=allocator)
        assert restored["condition"] == "cancelled_no_dispatch"
        assert restored["deadline"] == first["deadline"]
        assert restored["cancellation"] == first["cancellation"]
        assert restored["attempts"] == []
        assert "acceptance" not in restored
    assert task_status(control)["condition"] == "cancelled_no_dispatch"
    assert allocator.calls == (0 if before_allocation else 1)
    assert connection.starts == 0


def test_cancellation_after_terminal_verification_is_durable(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    with pytest.raises(TaskInterrupted):
        run(control, source, connection, fault="verification")
    cancelled = run(control, source, connection, cancel_requested=True)
    assert cancelled["condition"] == "cancelled"
    assert "cancellation" in cancelled
    recovered = run(control, source, connection)
    assert recovered["condition"] == "cancelled"
    assert "acceptance" not in recovered
    assert "pr" not in recovered
    assert connection.starts == 1


def test_cancellation_during_verification_stops_further_commands(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from creatidy_kernel.adapters import task_execution

    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    requested = [False]
    calls: list[tuple[str, ...]] = []
    original = cast(
        "Callable[[Path, VerificationCommand, int, Mapping[str, str]], dict[str, object]]",
        inspect.getattr_static(task_execution, "_run_command"),
    )

    def interrupt_after_command(
        workspace: Path, command: VerificationCommand, repeat: int, supplied: Mapping[str, str]
    ) -> dict[str, object]:
        result = original(workspace, command, repeat, supplied)
        calls.append(command.argv)
        requested[0] = True
        return result

    monkeypatch.setattr(task_execution, "_run_command", interrupt_after_command)
    result = run(control, source, FixtureConnection(deflake_edit), cancellation_requested=lambda: requested[0])
    assert result["condition"] == "cancelled"
    assert "cancellation" in result
    assert "acceptance" not in result
    assert "pr" not in result
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["receipt-evidence", "receipt", "cancel-terminal"])
def test_cancel_recovers_original_durable_receipt(sqlite_tmp_path: Path, fault: str) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"

    class Interrupted(FixtureConnection):
        interrupts = 0

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "turn/interrupt":
                assert params == {"threadId": "thread-1", "turnId": "turn-1"}
                self.interrupts += 1
                self.status = "interrupted"
                return {}
            return super().request(method, params)

    connection = Interrupted(deflake_edit)
    connection.status = "inProgress"
    if fault == "cancel-terminal":
        assert run(control, source, connection)["condition"] == "running"
        with pytest.raises(TaskInterrupted):
            run(control, source, connection, fault=fault, cancel_requested=True)
    else:
        with pytest.raises(TaskInterrupted):
            run(control, source, connection, fault=fault)
    cancelled = run(control, source, connection, cancel_requested=True)
    assert cancelled["condition"] == "cancelled"
    assert run(control, source, connection)["condition"] == "cancelled"
    assert connection.interrupts == connection.starts == 1
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        operation = store.operation("runtime:task_execution:change")
        assert operation.accepted_reference == "codex:thread-1:turn-1"
        assert operation.status == "terminal"
        assert store.artifact("cancel:task_execution:change", "target") == store.artifact(
            operation.operation_id, "runtime-receipt"
        )
        assert store.load("task_execution").attempt("task_execution:change").status.value == "finished"


def test_observed_cancellation_survives_crash_before_advance_returns(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    requested = [False]

    class ProcessLoss(BaseException):
        pass

    class Signals(FixtureConnection):
        inject = False

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            result = super().request(method, params)
            if method == "thread/read" and self.inject:
                requested[0] = True
            return result

    connection = Signals(deflake_edit)
    connection.status = "inProgress"
    assert run(control, source, connection)["condition"] == "running"
    connection.status = "completed"
    connection.inject = True
    original_admit = SQLiteProgramStore.admit

    def crash_finish(store: SQLiteProgramStore, program_id: str, key: str, command: DomainCommandType) -> Program:
        if isinstance(command, FinishAttempt):
            raise ProcessLoss()
        return original_admit(store, program_id, key, command)

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteProgramStore, "admit", crash_finish)
        with pytest.raises(ProcessLoss):
            run(control, source, connection, cancellation_requested=lambda: requested[0])
    requested[0] = False
    connection.inject = False
    recovered = run(control, source, connection)
    assert recovered["condition"] == "cancelled"
    assert "cancellation" in recovered
    assert "acceptance" not in recovered
    assert "pr" not in recovered
    assert connection.starts == 1


def test_failed_terminal_without_candidate_finishes_attempt(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    connection = FixtureConnection(lambda _path: None)
    connection.status = "failed"
    result = run(sqlite_tmp_path / "control", source, connection)
    assert result["condition"] == "terminal_no_candidate"
    attempt = cast("list[dict[str, object]]", result["attempts"])[0]
    assert cast("dict[str, object]", attempt["attempt"])["status"] == "finished"
    assert cast("dict[str, object]", attempt["operation"])["status"] == "terminal"


def test_restart_default_keeps_deadline_and_same_running_turn(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    connection = FixtureConnection(deflake_edit)
    connection.status = "inProgress"
    control = sqlite_tmp_path / "control"
    monkeypatch.setattr(time, "time", lambda: 1000)
    first = run(control, source, connection)
    assert first["condition"] == "running"
    monkeypatch.setattr(time, "time", lambda: 2000)
    second = run(control, source, connection)
    assert second["condition"] == "running"
    assert first["deadline"] == second["deadline"] == 4300
    assert first["attempts"] == second["attempts"]
    assert connection.starts == 1
    with pytest.raises(ValueError, match="envelope"):
        run_task(
            control,
            task=fixture_task(),
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=4301,
            allocation=ALLOCATION,
            forge_factory=lambda _directory: make_forge("0" * 40)[0],
        )


@pytest.mark.parametrize("fault", [None, "cancel-claim", "cancel-send", "cancel-observation"])
def test_expiry_journals_interrupt_once_even_after_lost_reply(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    source = make_source(sqlite_tmp_path)

    class LostInterrupt(FixtureConnection):
        interrupts = 0

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "turn/interrupt":
                self.interrupts += 1
                raise TimeoutError("synthetic lost interrupt reply")
            return super().request(method, params)

    connection = LostInterrupt(deflake_edit)
    connection.status = "inProgress"
    control = sqlite_tmp_path / "control"
    monkeypatch.setattr(time, "time", lambda: 1000)
    assert run(control, source, connection)["condition"] == "running"
    monkeypatch.setattr(time, "time", lambda: 4301)
    if fault:
        with pytest.raises(TaskInterrupted):
            run(control, source, connection, fault=fault)
    else:
        assert run(control, source, connection)["condition"] == "expired"
    before = connection.interrupts
    result = run(control, source, connection)
    assert result["condition"] == "expired"
    assert connection.interrupts == before == (0 if fault == "cancel-claim" else 1)
    assert connection.starts == 1
    assert "acceptance" not in result
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        cancellation = store.operation("cancel:task_execution:change")
        assert cancellation.attempts == 1
        assert cancellation.status in {"dispatched", "unknown"}
        if fault not in {"cancel-claim", "cancel-send"}:
            assert store.find_artifact(cancellation.operation_id, "receipt") is not None


def test_long_verification_cannot_extend_original_deadline(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    clock = [1000]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    original_check = TaskChecks.check

    def delayed(checks: TaskChecks, name: str, subject: EvidenceSubject) -> Evidence | None:
        result = original_check(checks, name, subject)
        if name == "verification-commands":
            clock[0] = 5000
        return result

    monkeypatch.setattr(TaskChecks, "check", delayed)
    result = run(sqlite_tmp_path / "control", source, FixtureConnection(deflake_edit))
    assert result["condition"] == "expired"
    assert "acceptance" not in result
    assert "pr" not in result


def test_deadline_expiring_during_thread_start_never_dispatches_turn(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    clock = [1000]
    edits: list[Path] = []
    monkeypatch.setattr(time, "time", lambda: clock[0])

    class SlowThread(FixtureConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            result = super().request(method, params)
            if method == "thread/start":
                clock[0] = 4301
            return result

    connection = SlowThread(edits.append)
    control = sqlite_tmp_path / "control"
    result = run(control, source, connection)
    assert result["condition"] == "expired"
    assert edits == []
    assert connection.starts == 1
    assert "acceptance" not in result
    assert "cancellation" in result


def test_end_to_end_accepted_with_synthetic_forge(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    forge, transport = make_forge(base)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    result = run(control, source, connection, forge=forge, base=base)
    assert result["condition"] == "accepted"
    head = str(result["head"])
    lifecycle = cast(list[str], result["lifecycle"])
    assert "task admitted: owner-approved frozen task" in lifecycle
    assert any(line.startswith("exact base established: ") for line in lifecycle)
    assert "workspace prepared: disposable controller-owned clone at the exact base" in lifecycle
    assert "allocation selected" in lifecycle
    assert any(line.startswith("candidate collected: ") for line in lifecycle)
    assert "changed paths accepted: tests/test_e2e_execution.py" in lifecycle
    assert "verification passed" in lifecycle
    assert "structural checks passed (deterministic subset)" in lifecycle
    assert f"candidate accepted: {head}" in lifecycle
    assert any(line.startswith("PR created: ") for line in lifecycle)
    assert "NO MERGE / NO DEPLOY" in lifecycle
    assert result["automatic_merge"] is False and result["automatic_deploy"] is False
    assert connection.starts == 1
    # Exact-subject semantics: the PR head is the accepted candidate, parent is the base.
    assert cast(dict[str, object], transport.pulls[0]["head"])["sha"] == head
    workspace = control / "workspace"
    assert task_git(workspace, "rev-parse", "HEAD") == head
    assert task_git(workspace, "rev-parse", head + "^") == base
    assert task_git(workspace, "diff", "--name-only", base, head) == "tests/test_e2e_execution.py"
    # The owner checkout is untouched.
    assert reference_git(source, "rev-parse", "refs/heads/develop") == base
    assert reference_git(source, "status", "--porcelain") == ""
    # Verification plan: 1 + 8 targeted runs plus one full gate, all zero-exit.
    commands = cast("dict[str, dict[str, object]]", result["verification"])
    runs = cast("list[dict[str, object]]", commands["verification-commands"]["runs"])
    assert len(runs) == 10
    assert all(run["exit_code"] == 0 for run in runs)
    assert all(run["timed_out"] is False for run in runs)
    structural = commands["structural"]
    assert cast("list[str]", structural["deferred_to_independent_review"])
    evidence = export_task(control)
    assert evidence["status"] == "completed"
    assert cast(dict[str, object], evidence["pr"])["status"] == "accepted"
    assert cast(dict[str, object], evidence["candidate"])["head"] == head
    status = task_status(control)
    assert status["accepted"] is True and status["candidate_head"] == head


def test_changed_forbidden_path_rejected(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)

    def edit(path: Path) -> None:
        deflake_edit(path)
        (path / "README.md").write_text("sneaky unrelated change\n")

    result = run(sqlite_tmp_path / "control", source, FixtureConnection(edit), task=fixture_task(repeats=1))
    assert result["condition"] == "rejected"
    lifecycle = cast(list[str], result["lifecycle"])
    assert "changed paths rejected: README.md" in lifecycle
    assert not any(line.startswith("PR created") for line in lifecycle)
    evidence = export_task(sqlite_tmp_path / "control")
    assert "acceptance" not in evidence
    assert "pr" not in evidence


def test_verification_command_failure_rejected(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)

    def edit(path: Path) -> None:
        target = path / "tests" / "test_e2e_execution.py"
        target.write_text(target.read_text().replace("self.assertTrue(True)", "self.assertTrue(False)"))

    result = run(sqlite_tmp_path / "control", source, FixtureConnection(edit), task=fixture_task(repeats=1))
    assert result["condition"] == "rejected"
    lifecycle = cast(list[str], result["lifecycle"])
    assert "changed paths accepted: tests/test_e2e_execution.py" in lifecycle
    assert "verification failed" in lifecycle
    commands = cast("dict[str, dict[str, object]]", result["verification"])
    runs = cast("list[dict[str, object]]", commands["verification-commands"]["runs"])
    assert any(run["exit_code"] != 0 for run in runs)
    assert "structural checks passed (deterministic subset)" in lifecycle


def test_empty_candidate_rejected(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    result = run(
        sqlite_tmp_path / "control", source, FixtureConnection(lambda _path: None), task=fixture_task(repeats=1)
    )
    assert result["condition"] == "rejected"
    lifecycle = cast(list[str], result["lifecycle"])
    assert "changed paths rejected: (no changes produced)" in lifecycle


def _test_file(path: Path) -> Path:
    return path / "tests" / "test_e2e_execution.py"


def _rewrite(path: Path, replacement: Callable[[str], str]) -> None:
    _test_file(path).write_text(replacement(_test_file(path).read_text()))


def _edit_skip(path: Path) -> None:
    _rewrite(
        path,
        lambda text: text.replace(
            "    def test_scenario_11_client_disconnect_propagates_cancel(self):",
            '    @unittest.skip("flaky")\n    def test_scenario_11_client_disconnect_propagates_cancel(self):',
        ),
    )


def _edit_remove_assertion(path: Path) -> None:
    _rewrite(
        path, lambda text: text.replace('self.assertTrue(worker.cancels, "cancel never reached the worker")\n', "")
    )


def _edit_weaken_ownership(path: Path) -> None:
    _rewrite(
        path,
        lambda text: text.replace(
            "self.assertEqual([], bystander.cancels)", "self.assertEqual(bystander.cancels, bystander.cancels)"
        ),
    )


def _edit_remove_bystander_assertion(path: Path) -> None:
    _rewrite(path, lambda text: text.replace("        self.assertEqual([], bystander.cancels)\n", ""))


def _edit_large_sleep(path: Path) -> None:
    _rewrite(path, lambda text: text + "\n\nclass Masking:\n    def mask(self):\n        time.sleep(30)\n")


def _edit_rename_scenario(path: Path) -> None:
    _rewrite(
        path,
        lambda text: text.replace(
            "test_scenario_11_cancel_reaches_exactly_the_owning_worker",
            "test_scenario_11_cancel_reaches_something",
        ),
    )


STRUCTURAL_EDITS: list[tuple[str, Callable[[Path], None]]] = [
    ("skip-decorator", _edit_skip),
    ("assertion-removed", _edit_remove_assertion),
    ("ownership-weakened", _edit_weaken_ownership),
    ("bystander-assertion-removed", _edit_remove_bystander_assertion),
    ("large-sleep", _edit_large_sleep),
    ("scenario-renamed", _edit_rename_scenario),
]


@pytest.mark.parametrize(
    ("label", "edit"),
    STRUCTURAL_EDITS,
)
def test_structural_weakening_rejected(sqlite_tmp_path: Path, label: str, edit: Callable[[Path], None]) -> None:
    del label
    source = make_source(sqlite_tmp_path)

    def applied(path: Path) -> None:
        deflake_edit(path)
        edit(path)

    result = run(sqlite_tmp_path / "control", source, FixtureConnection(applied), task=fixture_task(repeats=1))
    assert result["condition"] == "rejected"
    lifecycle = cast(list[str], result["lifecycle"])
    assert "structural checks rejected" in lifecycle
    assert "changed paths accepted: tests/test_e2e_execution.py" in lifecycle
    structural = cast("dict[str, dict[str, object]]", result["verification"])["structural"]
    assert cast(list[str], structural["deterministic_findings"])


def test_tracked_mutation_during_verification_rejected(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    touch = (
        sys.executable,
        "-c",
        "import pathlib; p = pathlib.Path('tests/test_e2e_execution.py'); "
        "p.write_text(p.read_text() + '# verification touched\\n')",
    )
    task = TaskSpec(
        task_id="fixture-mutating",
        forge_repository=REPOSITORY.value,
        repository_url="https://forge.invalid/BioMedical-IT/scarcity-router",
        base_branch="develop",
        instruction="irrelevant",
        repository_instructions="irrelevant",
        allowed_paths=frozenset({"tests/test_e2e_execution.py"}),
        verification=(VerificationCommand(touch, 30),),
        structural=scarcity_router_143_task().structural,
    )
    result = run(sqlite_tmp_path / "control", source, FixtureConnection(deflake_edit), task=task)
    assert result["condition"] == "rejected"
    lifecycle = cast(list[str], result["lifecycle"])
    assert "verification failed" in lifecycle
    commands = cast("dict[str, dict[str, object]]", result["verification"])
    payload = commands["verification-commands"]
    mutations = cast("list[dict[str, object]]", payload["tracked_mutations_after_verification"])
    assert mutations


def test_non_ignored_untracked_litter_during_verification_rejected(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    litter = (
        sys.executable,
        "-c",
        "import pathlib; pathlib.Path('leftover-artifact.txt').write_text('verification residue')",
    )
    task = TaskSpec(
        task_id="fixture-littering",
        forge_repository=REPOSITORY.value,
        repository_url="https://forge.invalid/BioMedical-IT/scarcity-router",
        base_branch="develop",
        instruction="irrelevant",
        repository_instructions="irrelevant",
        allowed_paths=frozenset({"tests/test_e2e_execution.py"}),
        verification=(VerificationCommand(litter, 30),),
        structural=scarcity_router_143_task().structural,
    )
    control = sqlite_tmp_path / "control"
    result = run(control, source, FixtureConnection(deflake_edit), task=task)
    # The trusted command exited zero, but the workspace is no longer Git-clean.
    commands = cast("dict[str, dict[str, object]]", result["verification"])
    payload = commands["verification-commands"]
    runs = cast("list[dict[str, object]]", payload["runs"])
    assert all(run["exit_code"] == 0 for run in runs)
    assert payload["untracked_paths_after_verification"] == ["leftover-artifact.txt"]
    assert result["condition"] == "rejected"
    lifecycle = cast(list[str], result["lifecycle"])
    assert "verification failed" in lifecycle
    assert not any(line.startswith("candidate accepted") for line in lifecycle)
    evidence = export_task(control)
    assert "acceptance" not in evidence
    assert "pr" not in evidence


def test_ignored_cache_artifacts_do_not_reject_verification(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    cache_maker = (
        sys.executable,
        "-c",
        "import pathlib; pathlib.Path('__pycache__').mkdir(exist_ok=True); "
        "pathlib.Path('__pycache__/m.cpython-312.pyc').write_text('cache')",
    )
    task = TaskSpec(
        task_id="fixture-caching",
        forge_repository=REPOSITORY.value,
        repository_url="https://forge.invalid/BioMedical-IT/scarcity-router",
        base_branch="develop",
        instruction="irrelevant",
        repository_instructions="irrelevant",
        allowed_paths=frozenset({"tests/test_e2e_execution.py"}),
        verification=(
            VerificationCommand(cache_maker, 30),
            VerificationCommand((sys.executable, "-m", "unittest", "tests.test_e2e_execution.CancellationTests"), 60),
        ),
        structural=scarcity_router_143_task().structural,
    )
    result = run(sqlite_tmp_path / "control", source, FixtureConnection(deflake_edit), task=task)
    commands = cast("dict[str, dict[str, object]]", result["verification"])
    payload = commands["verification-commands"]
    assert payload["tracked_mutations_after_verification"] == []
    assert payload["untracked_paths_after_verification"] == []
    assert result["condition"] == "accepted"


def test_runtime_identity_mismatch_refuses_and_stays_uncertain(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    broken = FixtureConnection(deflake_edit, provider="wrong-provider")
    with pytest.raises(CodexRejected):
        run(control, source, broken, task=fixture_task(repeats=1))
    assert broken.starts == 1
    time.sleep(1.1)  # let the delivery claim lease expire before reconciling
    # An uncertain dispatch is never redelivered, even after a domain rejection.
    fixed = FixtureConnection(deflake_edit)
    result = run(control, source, fixed, fault=None, task=fixture_task(repeats=1))
    assert result["condition"] == "unknown"
    assert fixed.starts == 0
    assert "acceptance" not in export_task(control)
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        assert store.operation("runtime:task_execution:change").attempts == 1


def test_no_allocation_refuses_without_dispatch(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"

    class Outage:
        def select(self, request: ResourceRequest) -> Allocation:
            raise AllocationUnavailable("synthetic Router outage")

    class Counting:
        def __init__(self) -> None:
            self.calls = 0

        def select(self, request: ResourceRequest) -> Allocation:
            self.calls += 1
            assert request == ResourceRequest("change", frozenset({"reference"}), 128)
            return ALLOCATION

    connection = FixtureConnection(deflake_edit)
    with pytest.raises(AllocationUnavailable):
        run(control, source, connection, allocation=None, allocator=Outage(), task=fixture_task(repeats=1))
    assert connection.starts == 0
    counting = Counting()
    result = run(control, source, connection, allocation=None, allocator=counting, task=fixture_task(repeats=1))
    assert result["condition"] == "accepted"
    assert counting.calls == 1


@pytest.mark.parametrize("fault", ["task-admitted", "workspace-prepared", "prepared", "allocation", "receipt"])
def test_crash_recovery_completes_without_duplicate_work(sqlite_tmp_path: Path, fault: str) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    with pytest.raises(TaskInterrupted):
        run(control, source, connection, fault=fault, task=fixture_task(repeats=1))
    result = run(control, source, connection, fault=None, task=fixture_task(repeats=1))
    assert result["condition"] == "accepted"
    assert connection.starts == 1
    lifecycle = cast(list[str], result["lifecycle"])
    # The attempt either already exists (durable allocation recovered) or is selected
    # exactly once on the recovery run; neither path re-selects an existing attempt.
    expected = (
        "allocation recovered from durable Attempt"
        if fault in {"prepared", "allocation", "receipt"}
        else ("allocation selected")
    )
    assert expected in lifecycle
    if fault == "task-admitted":  # crashed before any workspace preparation existed
        assert any(line.startswith("exact base established: ") for line in lifecycle)
    else:
        assert any(line.startswith("exact base recovered: ") for line in lifecycle)
    evidence = export_task(control)
    assert len(cast(list[dict[str, object]], evidence["attempts"])) == 1


def test_uncertain_dispatch_is_not_duplicated_after_lost_context(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    first = FixtureConnection(deflake_edit)
    with pytest.raises(TaskInterrupted):
        run(control, source, first, fault="send", task=fixture_task(repeats=1))
    assert first.starts == 1
    time.sleep(1.1)  # the durable claim lease must expire before reconciliation
    lost = FixtureConnection(deflake_edit)
    result = run(control, source, lost, fault=None, task=fixture_task(repeats=1))
    assert result["condition"] == "unknown"
    assert lost.starts == 0
    # Even the retained native connection cannot resurrect a handle that was never
    # durably published: an unreferenced uncertain dispatch stays unknown by design.
    result = run(control, source, first, fault=None, task=fixture_task(repeats=1))
    assert result["condition"] == "unknown"
    assert first.starts == 1
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        assert store.operation("runtime:task_execution:change").attempts == 1


def test_verification_crash_reruns_deterministic_local_checks(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    with pytest.raises(TaskInterrupted):
        run(control, source, connection, fault="verification", task=fixture_task(repeats=1))
    result = run(control, source, connection, fault=None, task=fixture_task(repeats=1))
    assert result["condition"] == "accepted"
    assert connection.starts == 1
    head = str(result["head"])
    # Re-executed verification binds to the identical candidate Git subject.
    assert task_git(control / "workspace", "rev-parse", "HEAD") == head
    commands = cast("dict[str, dict[str, object]]", result["verification"])
    runs = cast("list[dict[str, object]]", commands["verification-commands"]["runs"])
    assert all(run["exit_code"] == 0 for run in runs)


@pytest.mark.parametrize("fault", ["pr-commit", "pr-send", "pr-receipt"])
def test_pr_delivery_recovery_never_creates_a_second_pr(sqlite_tmp_path: Path, fault: str) -> None:
    source = make_source(sqlite_tmp_path)
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    control = sqlite_tmp_path / "control"
    forge, transport = make_forge(base)
    connection = FixtureConnection(deflake_edit)
    with pytest.raises((TaskInterrupted, RuntimeError)):
        run(control, source, connection, forge=forge, base=base, fault=fault, task=fixture_task(repeats=1))
    assert connection.starts == 1
    result = run(control, source, connection, forge=forge, base=base, fault=None, task=fixture_task(repeats=1))
    assert result["condition"] == "accepted"
    assert len(transport.pulls) <= 1
    assert transport.posts <= 1
    pr = cast(dict[str, object], result["pr"])
    assert pr["status"] in {"accepted", "unknown"}
    if fault == "pr-send":
        # No durable reference was retained: delivery stays unknown and is never re-pushed.
        assert pr["status"] == "unknown"
        assert transport.pushes == 1
    evidence = export_task(control)
    assert evidence["candidate"] is not None


def test_base_frozen_when_source_develop_moves(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    with pytest.raises(TaskInterrupted):
        run(control, source, connection, fault="workspace-prepared", task=fixture_task(repeats=1))
    (source / "README.md").write_text("moved on\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "develop moves")
    assert reference_git(source, "rev-parse", "refs/heads/develop") != base
    result = run(control, source, connection, fault=None, task=fixture_task(repeats=1))
    assert result["condition"] == "accepted"
    assert cast(dict[str, object], export_task(control)["candidate"])["base"] == base


def test_pinned_expected_base_enforced(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    control = sqlite_tmp_path / "control"
    result = run(control, source, FixtureConnection(deflake_edit), task=fixture_task(repeats=1, expected_base_sha=base))
    assert result["condition"] == "accepted"
    assert cast(dict[str, object], export_task(control)["candidate"])["base"] == base
    with pytest.raises(ValueError, match="failed"):
        run(
            sqlite_tmp_path / "other",
            source,
            FixtureConnection(deflake_edit),
            task=fixture_task(repeats=1, expected_base_sha="1" * 40),
        )


def test_frozen_task_registry_and_validation() -> None:
    task = TASKS["143"]()
    assert task.allowed_paths == frozenset({"tests/test_e2e_execution.py"})
    assert task.verification[1].repeats == 8
    assert task.verification[2].argv == ("make", "check")
    assert task.expected_base_sha is None  # the live run freezes the then-current develop
    # The frozen tokens are the real assertion-bearing source forms verified by the
    # D1-03 preflight against Scarcity Router 59538e9 (test_e2e_execution.py:875,906,908).
    assert task.structural.required_tokens == (
        "assertTrue(worker.cancels",
        "assertEqual([], bystander.cancels)",
    )
    with pytest.raises(ValueError):
        VerificationCommand((), 10)
    with pytest.raises(ValueError):
        VerificationCommand(("make", "check"), 0)
    with pytest.raises(ValueError):
        TaskSpec(
            task_id="bad",
            forge_repository="not-forgejo",
            repository_url="x",
            base_branch="develop",
            instruction="x",
            repository_instructions="x",
            allowed_paths=frozenset({"a"}),
            verification=(VerificationCommand(("make", "check"), 60),),
            structural=scarcity_router_143_task().structural,
        )


def test_durable_state_survives_controller_disposal(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    forge, transport = make_forge(base)
    with pytest.raises(TaskInterrupted):
        run(control, source, FixtureConnection(deflake_edit), forge=forge, base=base, fault="verification")
    # A completely fresh controller process re-derives everything from durable state.
    result = run(control, source, FixtureConnection(deflake_edit), forge=forge, base=base, fault=None)
    assert result["condition"] == "accepted"
    assert len(transport.pulls) == 1
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        assert store.operation("task_execution:base").status == "intent"
        assert store.operation("candidate:task_execution:change").attempts == 0


def test_live_composition_requires_complete_environment() -> None:
    task = fixture_task()
    with pytest.raises(ValueError, match="missing"):
        compose_task_live({}, task)
    components = compose_task_live(live_environment("zai/glm-5.3"), task)
    assert isinstance(components.allocator, ScarcityRouterAllocator)


LIVE_ENVIRONMENT = {
    "CREATIDY_KERNEL_ROUTER_URL": "https://router.invalid",
    "CREATIDY_KERNEL_CODEX_BIN": "/usr/local/bin/codex",
    "CREATIDY_KERNEL_CODEX_VERSION": "0.45.0",
    "CREATIDY_KERNEL_FORGE_API": "https://forge.invalid/api/v1",
    "CREATIDY_KERNEL_FORGE_REMOTE": "https://forge.invalid/BioMedical-IT/scarcity-router.git",
    "CREATIDY_KERNEL_FORGE_TOKEN": "synthetic-token",
}


def live_environment(binding: str) -> dict[str, str]:
    environment = dict(LIVE_ENVIRONMENT)
    environment["CREATIDY_KERNEL_RUNTIME_BINDING"] = binding
    return environment


# Native-shaped fake Codex app-server: version probe plus initialize/initialized only.
# It never serves thread/start or turn/start, so no test can perform inference.
FAKE_CODEX = f"""#!{sys.executable}
import json
import pathlib
import sys

if sys.argv[1:] == ['--version']:
    print('codex-cli __CODEX_VERSION__')
    sys.exit(0)
if sys.argv[1:4] == ['app-server', 'generate-json-schema', '--out']:
    pathlib.Path(sys.argv[4], 'client_request.json').write_text(json.dumps({{
        'properties': {{'method': {{'enum': ['thread/start', 'turn/start', 'thread/read', 'turn/interrupt']}}}}
    }}))
    sys.exit(0)
assert sys.argv[1:] == ['app-server']
first = json.loads(sys.stdin.readline())
assert first['method'] == 'initialize' and first['id'] == 1
print(json.dumps({{'id': 1, 'result': {{'userAgent': 'fake'}}}}), flush=True)
assert json.loads(sys.stdin.readline()) == {{'method': 'initialized', 'params': {{}}}}
"""


def write_fake_codex(directory: Path, version: str) -> Path:
    binary = directory / "fake-codex"
    binary.write_text(FAKE_CODEX.replace("__CODEX_VERSION__", version))
    binary.chmod(0o700)
    return binary


def test_live_connection_factory_pins_schema_version_to_codex_version(sqlite_tmp_path: Path) -> None:
    version = "0.155.0-alpha.16.3"
    environment = live_environment("zai/glm-5.3/low")
    environment["CREATIDY_KERNEL_CODEX_BIN"] = str(write_fake_codex(sqlite_tmp_path, version))
    environment["CREATIDY_KERNEL_CODEX_VERSION"] = version
    components = compose_task_live(environment, fixture_task())
    connection = components.connection_factory()
    assert isinstance(connection, CodexStdio)
    try:
        # A non-empty schema_methods inventory constructs only when the schema pin
        # equals the expected version; the pinned pair is exactly the env version.
        assert connection.version == version
        assert connection.methods == CODEX_METHODS
    finally:
        connection.close()


def test_live_connection_factory_fails_closed_on_version_disagreement(sqlite_tmp_path: Path) -> None:
    environment = live_environment("zai/glm-5.3/low")
    environment["CREATIDY_KERNEL_CODEX_BIN"] = str(write_fake_codex(sqlite_tmp_path, "0.155.0-alpha.16.3"))
    environment["CREATIDY_KERNEL_CODEX_VERSION"] = "0.155.0-alpha.16.4"
    components = compose_task_live(environment, fixture_task())
    with pytest.raises(ValueError, match="version"):
        components.connection_factory()


EFFORT_BINDING_CASES: list[tuple[str, frozenset[tuple[str, str, str]]]] = [
    ("zai/glm-5.3/low", frozenset({("zai", "glm-5.3", "low")})),
    ("zai/glm-5.3/none", frozenset({("zai", "glm-5.3", "none")})),
    ("zai/glm-5.3", frozenset()),
]


@pytest.mark.parametrize(("binding", "expected"), EFFORT_BINDING_CASES)
def test_supported_efforts_come_only_from_the_controller_runtime_binding(
    binding: str, expected: frozenset[tuple[str, str, str]]
) -> None:
    components = compose_task_live(live_environment(binding), fixture_task())
    # "none" is a literal effort string; only an absent third component is null and
    # manufactures no evidence.
    assert components.supported_efforts == expected


def selection_document(provider: str, model: str, effort: str | None) -> bytes:
    document = {
        "schema_version": 1,
        "decision": {
            "evaluated_at": "2026-09-28T08:00:00Z",
            "requirement": {
                "task_level": "L0",
                "capability_minima": {},
                "hard_constraints": {"minimum_input_context_tokens": 128, "requires_tool_use": True},
            },
            "catalog_version": 1,
            "catalog_updated_on": "2026-09-28",
            "selector_mode": "balanced",
            "resource_policy_version": 1,
            "selected": {
                "identity": {"provider": provider, "model": model, "variant": "opaque-configuration"},
                "display_name": "Synthetic public candidate",
                "reasoning_effort": effort,
                "eligible": True,
                "degraded": False,
                "capability_margin": 0,
                "scarcity_assessment": {
                    "state": "unknown",
                    "label": "unknown",
                    "applicable_scopes": [],
                    "reason_codes": ["capacity_bindings_unknown"],
                },
            },
            "alternatives": [],
            "excluded": [],
            "closest_candidates": [],
            "recoverable_candidates": [],
            "degraded": False,
            "reason_codes": ["selected_balanced"],
            "preference_order": [],
        },
    }
    return json.dumps(document).encode()


def _stub_router_selection(monkeypatch: pytest.MonkeyPatch, provider: str, model: str, effort: str | None) -> None:
    raw = selection_document(provider, model, effort)

    def exchange(_self: ScarcityRouterAllocator, _requirement: dict[str, object]) -> bytes:
        return raw

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", exchange)


TASK_RESOURCE_REQUEST = ResourceRequest("change", frozenset({"reference"}), 128)


def test_matching_router_selection_becomes_executable_with_controller_evidence(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    components = compose_task_live(live_environment("zai/glm-5.3/low"), fixture_task())
    _stub_router_selection(monkeypatch, "zai", "glm-5.3", "low")
    allocation = components.allocator.select(TASK_RESOURCE_REQUEST)
    assert (allocation.provider_id, allocation.model_id, allocation.reasoning_effort) == ("zai", "glm-5.3", "low")
    assert ("zai", "glm-5.3", "low") in components.supported_efforts
    connection = FixtureConnection(deflake_edit, effort="low")
    result = run(
        sqlite_tmp_path / "control",
        source,
        connection,
        task=fixture_task(repeats=1),
        allocation=allocation,
        supported_efforts=components.supported_efforts,
    )
    assert result["condition"] == "accepted"
    assert connection.starts == 1


def test_router_selection_outside_the_controller_binding_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    components = compose_task_live(live_environment("zai/glm-5.3/low"), fixture_task())
    _stub_router_selection(monkeypatch, "zai", "glm-5.3", "high")
    with pytest.raises(AllocationUnavailable, match="incompatible"):
        components.allocator.select(TASK_RESOURCE_REQUEST)


def test_router_selection_cannot_manufacture_effort_support(monkeypatch: pytest.MonkeyPatch) -> None:
    # The controller binding carries no explicit effort; the Router returns one.
    components = compose_task_live(live_environment("zai/glm-5.3"), fixture_task())
    assert components.supported_efforts == frozenset()
    _stub_router_selection(monkeypatch, "zai", "glm-5.3", "low")
    with pytest.raises(AllocationUnavailable, match="incompatible"):
        components.allocator.select(TASK_RESOURCE_REQUEST)


def test_explicit_effort_without_trusted_support_fails_closed_before_dispatch(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    connection = FixtureConnection(deflake_edit, effort="low")
    with pytest.raises(UnsupportedExecution, match="trusted support"):
        run(
            sqlite_tmp_path / "control",
            source,
            connection,
            task=fixture_task(repeats=1),
            allocation=EFFORT_ALLOCATION,
        )
    assert connection.starts == 0


def test_recovery_and_router_provenance_do_not_expand_effort_trust(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    components = compose_task_live(live_environment("zai/glm-5.3/low"), fixture_task())
    _stub_router_selection(monkeypatch, "zai", "glm-5.3", "low")
    allocation = components.allocator.select(TASK_RESOURCE_REQUEST)
    assert allocation.decision_provenance is not None  # Router output alone is never evidence.
    with pytest.raises(TaskInterrupted):
        run(
            control,
            source,
            FixtureConnection(deflake_edit, effort="low"),
            task=fixture_task(repeats=1),
            allocation=allocation,
            supported_efforts=components.supported_efforts,
            fault="allocation",
        )
    # Recovery without the controller's support evidence: neither the durable mode
    # record nor the allocation's Router provenance expands trusted support.
    strict = FixtureConnection(deflake_edit, effort="low")
    with pytest.raises(UnsupportedExecution, match="trusted support"):
        run(control, source, strict, task=fixture_task(repeats=1), allocation=allocation)
    assert strict.starts == 0


def test_codex_environment_is_closed_and_secret_free(monkeypatch: pytest.MonkeyPatch) -> None:
    from creatidy_kernel.adapters.task_execution import codex_environment

    sentinels = {
        name: "synthetic-" + name.lower().replace("_", "-")
        for name in ("CREATIDY_KERNEL_ROUTER_KEY", "CREATIDY_KERNEL_FORGE_TOKEN", "UNRELATED_OWNER_API_KEY")
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)
    closed = codex_environment(os.environ)
    # Controller and unrelated owner secrets never reach the coding runtime.
    assert not any(
        name in closed
        for name in (
            "CREATIDY_KERNEL_ROUTER_KEY",
            "CREATIDY_KERNEL_FORGE_TOKEN",
            "UNRELATED_OWNER_API_KEY",
        )
    )
    assert "synthetic" not in " ".join(closed.values())
    # The closed operational set still carries the entries Codex actually needs.
    assert closed["PATH"] == (os.environ.get("PATH") or os.defpath)
    assert closed["HOME"] == os.environ["HOME"]
