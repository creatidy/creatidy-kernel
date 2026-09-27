# SPDX-License-Identifier: Apache-2.0
"""Live composition wiring proved with native-shaped responses, never a paid call."""

from pathlib import Path
from typing import cast

import pytest

from creatidy_kernel.adapters.codex_runtime import CodexRuntime
from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference import export_reference, reference_commit, reference_git
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.execution import RuntimeObservation
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.resources import Allocation

pytest_plugins = ["test_sqlite_store"]


class Connection:
    version = "1.2.3"
    methods = frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"})

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.starts = 0
        self.status = "completed"
        self.interrupts = 0
        self.interrupt_failure: str | None = None
        self.read_failure = False

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "thread/start":
            self.starts += 1
            return {"thread": {"id": f"thread-{self.starts}"}, "model": "model", "modelProvider": "provider"}
        if method == "turn/start":
            (self.directory / "repository" / "result.txt").write_bytes(
                b"first\n" if self.starts == 1 else b"first\nsecond\n"
            )
            return {"turn": {"id": f"turn-{self.starts}", "status": "inProgress"}}
        if method == "thread/read":
            if self.read_failure:
                raise TimeoutError("thread observation unavailable")
            thread = str(params["threadId"])
            return {
                "thread": {"id": thread, "turns": [{"id": thread.replace("thread", "turn"), "status": self.status}]}
            }
        if method == "turn/interrupt":
            if self.interrupt_failure == "before-send":
                raise KeyboardInterrupt
            self.interrupts += 1
            if self.interrupt_failure == "lost-reply":
                raise TimeoutError("interrupt reply lost")
            if self.interrupt_failure == "after-send":
                raise KeyboardInterrupt
            return {}
        raise AssertionError(method)


def test_finite_native_composition(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: 10)
    connection = Connection(sqlite_tmp_path)
    repository = Reference("forgejo:synthetic/reference")
    transport = SyntheticForgeTransport(repository)
    forge = ForgejoForge(transport, transport, lambda _: True)
    objects = sqlite_tmp_path / "objects.git"
    objects.mkdir()
    reference_git(objects, "init", "--bare")
    transport.branches["develop"] = reference_commit(objects, b"")
    result = run_live_reference(
        sqlite_tmp_path,
        owner_approved=True,
        trusted_development_acknowledged=True,
        connection=connection,
        version=connection.version,
        allocation=Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "owner selection"),
        forge=forge,
        repository=repository,
        object_source=objects,
        deadline=100,
        max_observations=3,
    )
    assert result["status"] == "completed"
    assert result["mode"] == "live"
    assert result["isolation"] == "not attested"
    assert connection.starts == 2
    head = str(cast(dict[str, object], transport.pulls[0]["head"])["sha"])
    assert reference_git(objects, "cat-file", "-t", head) == "commit"


@pytest.mark.parametrize("interrupt_failure", [None, "before-send", "lost-reply", "after-send"])
@pytest.mark.parametrize("terminal_status", ["interrupted", "failed", "completed"])
def test_observation_budget_and_expiry_survive_restart(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_failure: str | None,
    terminal_status: str,
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: 10)
    connection = Connection(sqlite_tmp_path)
    connection.status = "inProgress"
    connection.interrupt_failure = interrupt_failure
    cancel_calls = 0
    original_cancel = CodexRuntime.cancel

    def cancel(runtime: CodexRuntime, handle: str) -> RuntimeObservation:
        nonlocal cancel_calls
        cancel_calls += 1
        return original_cancel(runtime, handle)

    monkeypatch.setattr(CodexRuntime, "cancel", cancel)
    repository = Reference("forgejo:synthetic/reference")
    transport = SyntheticForgeTransport(repository)
    forge = ForgejoForge(transport, transport, lambda _: True)
    objects = sqlite_tmp_path / "objects.git"
    objects.mkdir()
    reference_git(objects, "init", "--bare")
    transport.branches["develop"] = reference_commit(objects, b"")

    def run() -> dict[str, object]:
        return run_live_reference(
            sqlite_tmp_path,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            allocation=Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "owner selection"),
            forge=forge,
            repository=repository,
            object_source=objects,
            deadline=100,
            max_observations=1,
        )

    if interrupt_failure in {"before-send", "after-send"}:
        with pytest.raises(KeyboardInterrupt):
            run()
    else:
        assert run()["condition"] == "budget_exhausted"
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        cancellation = store.operation("cancel:reference:first")
        assert cancellation.status == "unknown"
        assert cancellation.attempts == 1
        assert cancellation.accepted_reference is None
        assert cancellation.retry_proof is None
        assert store.operation("runtime:reference:first").status != "terminal"
    connection.interrupt_failure = None
    assert run()["condition"] == "budget_exhausted"
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: 101)
    assert run()["condition"] == "budget_exhausted"
    assert connection.starts == 1
    expected_interrupts = 0 if interrupt_failure == "before-send" else 1
    assert connection.interrupts == expected_interrupts
    assert cancel_calls == 1

    connection.read_failure = True
    result = run()
    assert cast(list[dict[str, object]], result["cancellations"])[0]["target_terminal"] is False
    assert cancel_calls == 1
    connection.read_failure = False

    connection.status = terminal_status
    result = run()
    cancellations = cast(list[dict[str, object]], result["cancellations"])
    assert cancellations[0]["target_terminal"] is True
    assert cast(dict[str, object], cancellations[0]["operation"])["status"] == "unknown"
    assert result["accepted"] == []
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        assert store.operation("runtime:reference:first").status == "terminal"
        assert store.operation("cancel:reference:first").attempts == 1
    assert run()["cancellations"] == result["cancellations"]
    assert export_reference(sqlite_tmp_path)["cancellations"] == result["cancellations"]
    assert connection.interrupts == expected_interrupts
    assert connection.starts == 1
    assert cancel_calls == 1
