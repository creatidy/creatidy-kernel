# SPDX-License-Identifier: Apache-2.0
"""Bounded #166 support with synthetic Git/source fixtures, never live task execution."""

import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from test_task_execution import make_source

from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import (
    TASKS,
    TaskChecks,
    TaskStructure,
    preflight_task,
    scarcity_router_143_task,
    scarcity_router_166_task,
)
from creatidy_kernel.adapters.task_execution import (
    _baseline as baseline,  # pyright: ignore[reportPrivateUsage] - focused controller seam test.
)
from creatidy_kernel.adapters.task_execution import (
    _resolve_base as resolve_base,  # pyright: ignore[reportPrivateUsage] - exact Git subject resolution.
)

pytest_plugins = ["test_task_preflight"]

TARGET = "tests/test_openai_codex_acquisition.py"
SAFE_TERMINATION_NAMES = (
    "test_success_path_terminates_without_kill",
    "test_failure_paths_terminate_without_kill",
    "test_timeout_path_terminates_and_never_leaks",
    "test_stubborn_child_is_killed_after_bounded_wait",
    "test_reader_startup_failure_terminates_child",
    "test_reader_startup_failure_kills_stubborn_child",
    "test_shutdown_never_raises_through_collect",
)

# Producer-shaped source, authored locally. It is parsed, not imported or executed.
FEEDER_SOURCE = """import io
import os
import threading
import time
import unittest


class _FakeAppServer:
    def __init__(self, before_write=None):
        self._read_fd, self._write_fd = os.pipe()
        self.events = []
        self.stdin = io.StringIO()
        self._stop = threading.Event()
        self._before_write = before_write
        self._feeder = threading.Thread(target=self._feed)
        self._feeder.start()

    def _feed(self):
        os.set_blocking(self._write_fd, False)
        while not self._stop.is_set():
            if self._before_write is not None:
                self._before_write()
            try:
                os.write(self._write_fd, b"synthetic response")
                break
            except BlockingIOError:
                time.sleep(0.002)

    def terminate(self):
        self.events.append("terminate")
        self.stdin.close()

    def kill(self):
        self.events.append("kill")

    def _close_write(self):
        self._stop.set()
        self._feeder.join(timeout=5.0)
        os.close(self._write_fd)


class SafeTermination(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()

    def _assert_no_output(self):
        self.assertEqual(self.stderr.getvalue(), "")
"""


def feeder_source() -> str:
    content = FEEDER_SOURCE
    for name in scarcity_router_166_task().structural.required_names:
        killed = "killed" in name or "kills" in name
        events = '["terminate", "kill"]' if killed else '["terminate"]'
        content += f"""
    def {name}(self):
        fake = _FakeAppServer()
        fake.terminate()
        {"fake.kill()" if killed else "pass"}
        fake._close_write()
        os.close(fake._read_fd)
        self.assertEqual(fake.events, {events})
        self.assertTrue(fake.stdin.closed)
        self.assertFalse(fake._feeder.is_alive())
        self._assert_no_output()
"""
    return content


REGRESSION_SOURCE = """
    def test_forced_interleaving_preserves_writer_ownership(self):
        paused = threading.Event()
        release = threading.Event()
        shutdown_started = threading.Event()

        def before_write():
            paused.set()
            release.wait()

        server = _FakeAppServer(before_write)
        self.assertTrue(paused.wait(timeout=1.0))

        def shutdown():
            shutdown_started.set()
            server._close_write()

        closer = threading.Thread(target=shutdown)
        closer.start()
        self.assertTrue(shutdown_started.wait(timeout=1.0))
        release.set()
        closer.join(timeout=1.0)
        self.assertFalse(closer.is_alive())
        self.assertFalse(server._feeder.is_alive())
        self.assertEqual(os.read(server._read_fd, 64), b"synthetic response")
        os.close(server._read_fd)
        self._assert_no_output()
"""


def candidate_source() -> str:
    return feeder_source().replace("self._feeder.join(timeout=5.0)", "self._feeder.join()") + REGRESSION_SOURCE


def commit_source(source: Path, content: str) -> str:
    (source / TARGET).write_text(content)
    reference_git(source, "add", "--", TARGET)
    reference_git(source, "commit", "-m", "synthetic feeder source")
    return reference_git(source, "rev-parse", "HEAD")


@pytest.fixture
def feeder_repository(tmp_path: Path) -> tuple[Path, str]:
    source = make_source(tmp_path)
    return source, commit_source(source, feeder_source())


@pytest.fixture
def closed_tools(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []
    original_which = shutil.which

    def which(command: str, *, path: str | None = None) -> str | None:
        if command == "git":
            return original_which(command, path=path)
        assert command in {"uv", "make"}
        calls.append((command, path))
        return "/synthetic-tools/" + command

    def no_verification(*_args: object, **_kwargs: object) -> None:
        pytest.fail("support tests must not execute verification")

    monkeypatch.setattr("creatidy_kernel.adapters.task_execution.shutil.which", which)
    monkeypatch.setattr("creatidy_kernel.adapters.task_execution._run_command", no_verification)
    return calls


def structural_payload(source: Path, base: str, head: str) -> tuple[dict[str, object], bool]:
    # This pure payload seam must neither open a store nor publish check evidence.
    with patch("creatidy_kernel.adapters.task_execution.SQLiteProgramStore", side_effect=AssertionError("no store")):
        checks = TaskChecks(
            cast(SQLiteProgramStore, None),
            scarcity_router_166_task(),
            source,
            base,
            head,
            (TARGET,),
            [],
        )
        return checks._structural_payload()  # pyright: ignore[reportPrivateUsage] - no execution/evidence writes.


def test_exact_166_task_contract_and_legacy_143_identity() -> None:
    task = scarcity_router_166_task()
    module = ("uv", "run", "python", "-m", "unittest", "tests.test_openai_codex_acquisition", "-v")
    assert TASKS["166"] is scarcity_router_166_task
    assert TASKS["143"] is scarcity_router_143_task
    assert task.task_id == "scarcity-router-166" and task.forge_issue == 166
    assert task.forge_repository == "forgejo:BioMedical-IT/scarcity-router"
    assert task.repository_url == "https://forgejo.creatidy.com/BioMedical-IT/scarcity-router"
    assert task.allowed_paths == frozenset({TARGET})
    assert task.base_branch == "develop" and task.expected_base_sha is None
    assert task.max_attempts == 1
    assert [(command.argv, command.timeout_seconds, command.repeats) for command in task.verification] == [
        ((*module[:-2], "tests.test_openai_codex_acquisition.SafeTermination", "-v"), 900, 1),
        (module, 900, 1),
        (module, 900, 8),
        (("make", "check"), 3600, 1),
    ]
    assert task.structural.task_structure is TaskStructure.FEEDER_166
    assert task.structural.required_names == SAFE_TERMINATION_NAMES
    assert cast(dict[str, object], task.payload()["structural"])["task_structure"] == "scarcity-router-166"
    pinned = scarcity_router_166_task("1" * 40)
    assert pinned.expected_base_sha == "1" * 40
    assert pinned.digest == task.digest
    legacy = scarcity_router_143_task()
    assert legacy.structural.task_structure is TaskStructure.CANCELLATION_143
    assert "task_structure" not in cast(dict[str, object], legacy.payload()["structural"])
    # Public pre-change TaskSpec identity, not a credential.
    assert legacy.digest == (
        "9e71b426d9d8dc9f9f1d9d011fa2cda836a514ca1a9e4f48a36c91948d6ea06e"  # pragma: allowlist secret
    )


@pytest.mark.parametrize("invalid", ["scarcity-router-166", "unknown", len])
def test_structural_policy_requires_closed_typed_discriminator(invalid: object) -> None:
    assert set(TaskStructure) == {TaskStructure.CANCELLATION_143, TaskStructure.FEEDER_166}
    with pytest.raises(ValueError, match="supported typed task structure"):
        replace(scarcity_router_166_task().structural, task_structure=cast(TaskStructure, invalid))


def test_baseline_resolves_current_develop_and_checks_closed_tools_without_execution(
    feeder_repository: tuple[Path, str], closed_tools: list[tuple[str, str | None]]
) -> None:
    source, initial = feeder_repository
    current = commit_source(source, feeder_source() + "\n# develop advanced after task selection\n")
    task = scarcity_router_166_task()
    assert current != initial
    assert resolve_base(source, task) == (current, "refs/heads/develop")
    assert resolve_base(source, replace(task, expected_base_sha=initial))[0] == initial
    before = reference_git(source, "status", "--porcelain")
    payload = baseline(source, current, task, {"PATH": "/closed-tools"})
    assert payload["structural_baseline"] == "present"
    assert payload["allowed_paths"] == [TARGET]
    assert payload["verification_executed"] is False
    assert closed_tools == [("uv", "/closed-tools")] * 3 + [("make", "/closed-tools")]
    assert reference_git(source, "status", "--porcelain") == before == ""
    assert ".recv(" not in feeder_source()
    assert "if not piece:" not in feeder_source()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-file",
        "inert-feed",
        "inert-target",
        "blocking-writer",
        "missing-fixture",
        "stale-join",
        "already-fixed",
        "guarded-close",
        "missing-stop-check",
        "syntax",
        "duplicate-fixture",
        "duplicate-method",
        "duplicate-class",
        "duplicate-scenario",
        "missing-scenario",
    ],
)
def test_baseline_rejects_missing_inert_stale_or_ambiguous_source(
    feeder_repository: tuple[Path, str], closed_tools: list[tuple[str, str | None]], mutation: str
) -> None:
    source, _base = feeder_repository
    content = feeder_source()
    if mutation == "missing-file":
        reference_git(source, "rm", "--", TARGET)
        reference_git(source, "commit", "-m", "synthetic removed fixture")
        head = reference_git(source, "rev-parse", "HEAD")
    else:
        if mutation == "inert-feed":
            content = content.replace('os.write(self._write_fd, b"synthetic response")', "pass  # os.write(")
        elif mutation == "inert-target":
            content = content.replace("threading.Thread(target=self._feed)", "threading.Thread(target=lambda: None)")
        elif mutation == "blocking-writer":
            content = content.replace(
                "os.set_blocking(self._write_fd, False)",
                "os.set_blocking(self._write_fd, True)  # os.set_blocking(self._write_fd, False)",
            )
        elif mutation == "missing-fixture":
            content = content.replace("class _FakeAppServer:", "class InertMock:")
        elif mutation == "stale-join":
            content = content.replace("join(timeout=5.0)", "join(timeout=6.0)")
        elif mutation == "already-fixed":
            content = content.replace("join(timeout=5.0)", "join()")
        elif mutation == "guarded-close":
            content = content.replace(
                "        os.close(self._write_fd)",
                "        if not self._feeder.is_alive():\n            os.close(self._write_fd)",
            )
        elif mutation == "missing-stop-check":
            content = content.replace("while not self._stop.is_set():", "while True:")
        elif mutation == "syntax":
            content += "\ninvalid syntax here\n"
        elif mutation == "duplicate-fixture":
            content += "\nclass _FakeAppServer:\n    pass\n"
        elif mutation == "duplicate-method":
            content = content.replace(
                "    def _feed(self):", "    def _feed(self):\n        pass\n\n    def _feed(self):"
            )
        elif mutation == "duplicate-class":
            content += "\nclass SafeTermination:\n    pass\n"
        elif mutation == "duplicate-scenario":
            content += f"\n    def {SAFE_TERMINATION_NAMES[0]}(self):\n        pass\n"
        else:
            content = content.replace(
                f"def {SAFE_TERMINATION_NAMES[0]}(self):", f"def renamed(self):  # {SAFE_TERMINATION_NAMES[0]}"
            )
        head = commit_source(source, content)
    with pytest.raises((ValueError, SyntaxError)):
        baseline(source, head, scarcity_router_166_task(), {"PATH": "/closed-tools"})
    assert closed_tools == []


@pytest.mark.parametrize("reindent_sleep", [False, True])
def test_candidate_checks_use_exact_git_subject_and_defer_semantic_proof(
    feeder_repository: tuple[Path, str], reindent_sleep: bool
) -> None:
    source, base = feeder_repository
    content = candidate_source()
    if reindent_sleep:
        content = content.replace(
            "                time.sleep(0.002)",
            "                if not self._stop.is_set():\n                    time.sleep(0.002)",
        )
    head = commit_source(source, content)
    # A misleading checkout is not the candidate subject supplied to the checker.
    (source / TARGET).write_text("invalid uncommitted content\n")
    payload, passed = structural_payload(source, base, head)
    assert passed and payload["deterministic_findings"] == []
    deferred = cast(list[str], payload["deferred_to_independent_review"])
    assert "forced interleaving proves unsafe baseline and corrected descriptor ownership" in deferred
    assert "semantic properties require independent review" in str(payload["scope_note"])


@pytest.mark.parametrize(
    ("mutation", "finding"),
    [
        ("dropped-assertion", "existing assertion removed or changed"),
        ("dropped-helper-call", "existing assertion removed or changed"),
        ("dropped-scenario", "existing scenario removed"),
        ("comment-only-stderr", "existing assertion removed or changed"),
        ("comment-only-write", "real pipe/non-blocking feeder structure is missing"),
        ("comment-only-pipe", "real pipe/non-blocking feeder structure is missing"),
        ("comment-only-nonblocking", "feeder write descriptor must remain non-blocking"),
        ("inert-fixture", "real feeder fixture is absent"),
        ("inert-target", "real feeder thread target must be preserved"),
        ("no-regression", "regression coverage must be added"),
        ("inert-regression", "regression coverage must be added"),
        ("sleep", "sleep cannot establish feeder correctness"),
        ("multiline-sleep", "sleep cannot establish feeder correctness"),
        ("changed-sleep", "sleep cannot establish feeder correctness"),
        ("skip", "forbidden added content"),
        ("xfail", "forbidden added content"),
        ("expected-failure", "forbidden added content"),
        ("skip-test", "forbidden added content"),
        ("assertion-exception", "forbidden added content"),
        ("broad-exception", "forbidden added content"),
        ("os-exception", "forbidden added content"),
        ("tuple-exception", "forbidden added content"),
        ("bare-exception", "forbidden added content"),
        ("timeout-keyword", "timeout increase is not an ownership fix"),
        ("timeout-positional", "timeout increase is not an ownership fix"),
    ],
)
def test_candidate_rejects_structural_weakening(
    feeder_repository: tuple[Path, str], mutation: str, finding: str
) -> None:
    source, base = feeder_repository
    content = candidate_source()
    if mutation == "dropped-assertion":
        content = content.replace("self.assertFalse(fake._feeder.is_alive())", "self.assertTrue(True)", 1)
    elif mutation == "dropped-helper-call":
        content = content.replace("self._assert_no_output()", "pass", 1)
    elif mutation == "dropped-scenario":
        content = content.replace(
            f"def {SAFE_TERMINATION_NAMES[0]}(self):", f"def renamed(self):  # {SAFE_TERMINATION_NAMES[0]}"
        )
    elif mutation == "comment-only-stderr":
        content = content.replace(
            'self.assertEqual(self.stderr.getvalue(), "")', 'pass  # self.assertEqual(self.stderr.getvalue(), "")'
        )
    elif mutation == "comment-only-write":
        content = content.replace('os.write(self._write_fd, b"synthetic response")', "pass  # os.write(")
    elif mutation == "comment-only-pipe":
        content = content.replace("os.pipe()", "(0, 1)  # os.pipe()")
    elif mutation == "comment-only-nonblocking":
        content = content.replace(
            "os.set_blocking(self._write_fd, False)",
            "os.set_blocking(self._write_fd, True)  # os.set_blocking(self._write_fd, False)",
        )
    elif mutation == "inert-fixture":
        content = content.replace("class _FakeAppServer:", "class InertMock:")
    elif mutation == "inert-target":
        content = content.replace("threading.Thread(target=self._feed)", "threading.Thread(target=lambda: None)")
    elif mutation == "no-regression":
        content = content.removesuffix(REGRESSION_SOURCE)
    elif mutation == "inert-regression":
        content = content.removesuffix(REGRESSION_SOURCE) + "\n    def test_inert_regression(self):\n        pass\n"
    elif mutation == "sleep":
        content += "\n        time.sleep(0.001)\n"
    elif mutation == "multiline-sleep":
        content += "\n        time.sleep(\n            0.001\n        )\n"
    elif mutation == "changed-sleep":
        content = content.replace("time.sleep(0.002)", "time.sleep(0.003)")
    elif mutation in {"skip", "xfail", "expected-failure"}:
        decorator = {
            "skip": '@unittest.skip("synthetic")',
            "xfail": "@pytest.mark.xfail",
            "expected-failure": "@unittest.expectedFailure",
        }[mutation]
        content = content.replace(
            "    def test_forced_interleaving", f"    {decorator}\n    def test_forced_interleaving"
        )
    elif mutation == "skip-test":
        content += '\n        self.skipTest("synthetic")\n'
    elif mutation.endswith("exception"):
        exception = {
            "assertion-exception": " AssertionError",
            "broad-exception": " Exception",
            "os-exception": " OSError",
            "tuple-exception": " (OSError, ValueError)",
            "bare-exception": "",
        }[mutation]
        content += f"\n        try:\n            self.assertTrue(True)\n        except{exception}:\n            pass\n"
    else:
        join = "self._feeder.join(timeout=6.0)" if mutation == "timeout-keyword" else "self._feeder.join(6.0)"
        content = content.replace("self._feeder.join()", join)
    head = commit_source(source, content)
    payload, passed = structural_payload(source, base, head)
    assert not passed
    assert any(finding in item for item in cast(list[str], payload["deterministic_findings"]))


def test_166_preflight_is_read_only_without_turns_verification_store_or_forge_writes(
    ready_inputs: tuple[dict[str, str], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    closed_tools: list[tuple[str, str | None]],
) -> None:
    environment, source, state = ready_inputs
    task = replace(scarcity_router_166_task(), repository_url="https://forge.invalid/BioMedical-IT/scarcity-router")
    base = commit_source(source, feeder_source())
    reads: list[str] = []

    def forge(
        _self: HTTPSForgejoTransport,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> tuple[int, object]:
        assert method == "GET" and body is None
        reads.append(path)
        prefix = "/repos/BioMedical-IT/scarcity-router"
        if path == prefix:
            return 200, {"full_name": "BioMedical-IT/scarcity-router"}
        if path == prefix + "/branches/develop":
            return 200, {"name": "develop", "commit": {"id": base}}
        assert path == prefix + "/issues/166"
        return 200, {"number": 166, "html_url": task.repository_url + "/issues/166", "state": "open"}

    monkeypatch.setattr(HTTPSForgejoTransport, "request", forge)
    initialize: list[str] = []
    exchange = CodexStdio._exchange  # pyright: ignore[reportPrivateUsage] - native initialization instrumentation.

    def only_initialize(self: CodexStdio, method: str, params: dict[str, object]) -> dict[str, object]:
        initialize.append(method)
        assert method == "initialize"
        return exchange(self, method, params)

    with (
        patch.object(CodexStdio, "_exchange", only_initialize),
        patch.object(CodexStdio, "request", side_effect=AssertionError("no model turns")),
        patch.object(ConditionalGitTransport, "create_agit_pr", side_effect=AssertionError("no Forge writes")),
        patch("creatidy_kernel.adapters.task_execution.SQLiteProgramStore", side_effect=AssertionError("no store")),
    ):
        default = preflight_task(environment, task=task, source_repository=source, directory=state)
        pinned = preflight_task(
            environment, task=replace(task, expected_base_sha=base), source_repository=source, directory=state
        )
    assert default.ready and pinned.ready
    assert default.exact_base_sha == pinned.exact_base_sha == base
    assert default.base_ref == "refs/heads/develop"
    assert default.task_digest == pinned.task_digest == task.digest
    assert initialize == ["initialize", "initialize"]
    assert (
        reads
        == [
            "/repos/BioMedical-IT/scarcity-router",
            "/repos/BioMedical-IT/scarcity-router/branches/develop",
            "/repos/BioMedical-IT/scarcity-router/issues/166",
        ]
        * 2
    )
    payload = default.payload()
    assert payload["inference_performed"] is False
    assert payload["forge_writes_performed"] is False
    assert payload["execution_authorized"] is False
    assert cast(dict[str, object], default.evidence["task"])["verification_executed"] is False
    assert len(closed_tools) == 8
    assert not state.exists()
    assert reference_git(source, "rev-parse", "HEAD") == base
    assert reference_git(source, "status", "--porcelain") == ""
