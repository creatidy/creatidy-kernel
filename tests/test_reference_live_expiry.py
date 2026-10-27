# SPDX-License-Identifier: Apache-2.0
"""Crossing an authority boundary inside native observation/collection retires owned targets."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from test_reference_live import Connection

from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference import ReferenceCollector, reference_commit, reference_git
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.execution import Candidate, ExecutionRequest
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.core.work_budget import BudgetRefused
from creatidy_kernel.ports.application import Collection

pytest_plugins = ["test_sqlite_store"]


class Clock:
    reading = BootClockReading(100, 1_000_000_000, "synthetic-live-boot")

    def __call__(self) -> BootClockReading:
        return self.reading

    def expire(self) -> None:
        self.reading = BootClockReading(111, 2_000_000_000, "synthetic-live-boot")


class OwnedConnection(Connection):
    def __init__(self, directory: Path, clock: Clock, crossing: str, settlement: bool | None | str) -> None:
        super().__init__(directory)
        self.clock = clock
        self.crossing = crossing
        self.crossed = False
        self.settlement = settlement
        self.close_calls = 0
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )

    @property
    def owned_group_reference(self) -> str | None:
        return None if self.settlement == "missing" else "synthetic-original-owned-group"

    @property
    def owned_group_settled(self) -> bool | None:
        if self.settlement == "error" and self.close_calls:
            raise OSError("synthetic settlement probe unavailable after retirement")
        if self.settlement is None or self.settlement == "missing":
            return None
        return self.settlement is True and self.close_calls > 0 and self.process.poll() is not None

    def close(self) -> None:
        self.close_calls += 1
        if self.settlement is not False:
            self.retire_fixture()

    def retire_fixture(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        result = super().request(method, params)
        if method == "turn/start":
            (self.directory / "repository/protected-worker-output").write_text("retain")
        if method == "thread/read" and self.crossing == "observation" and not self.crossed:
            self.crossed = True
            self.clock.expire()
        return result


@pytest.mark.parametrize("crossing", ["observation", "collection", "capacity"])
@pytest.mark.parametrize("settlement", [True, False, None, "missing", "error"])
def test_refusal_inside_admission_uses_durable_stop_and_qualified_cleanup(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crossing: str, settlement: bool | None | str
) -> None:
    clock = Clock()
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: clock.reading.wall)
    connection = OwnedConnection(sqlite_tmp_path, clock, crossing, settlement)
    collect = ReferenceCollector.collect

    def crossing_collection(
        collector: ReferenceCollector, request: ExecutionRequest, candidate: Candidate, *, now: int
    ) -> Collection:
        result = collect(collector, request, candidate, now=now)
        if not connection.crossed:
            connection.crossed = True
            if crossing == "capacity":
                raise BudgetRefused("synthetic capacity refusal inside collection")
            clock.expire()
        return result

    if crossing != "observation":
        monkeypatch.setattr(ReferenceCollector, "collect", crossing_collection)
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
            allocation=Allocation(
                "codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner selection"
            ),
            forge=forge,
            repository=repository,
            object_source=objects,
            deadline=110,
            max_observations=3,
            boot_clock=clock,
        )

    try:
        result = run()
        assert result["accepted"] == [] and transport.pulls == [] and connection.starts == 1
        assert (sqlite_tmp_path / "repository/protected-worker-output").read_text() == "retain"
        assert connection.close_calls == (0 if settlement == "missing" else 1)
        assert result["condition"] == ("budget_exhausted" if settlement is True else "cancel_uncertain")
        cleanup = cast(list[dict[str, object]], result["owned_cleanup"])
        assert cleanup and cleanup[0]["owned_group_settled"] is (
            True if settlement is True else False if settlement is False else None
        )
        with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=clock) as store:
            assert store.operations("interruption:live-authority")
            operation = store.operation("runtime:reference:first")
            assert operation.status == "terminal"  # This alone must not skip cleanup.
            cancellation = store.operation("cancel:reference:first")
            assert cancellation.attempts == (0 if settlement == "missing" else 1)
            proof = store.find_artifact(cancellation.operation_id, "owned-group-settlement")
            assert (proof is not None) is (settlement is True)
            if proof is not None:
                witness = json.loads(proof)
                assert (
                    witness["request_digest"] == operation.request_digest
                    and witness["runtime_fence"] == operation.fence
                )
                assert witness["result_reconciled"] is True and witness["descendants"] == "unproven"
            assert not store.operations("acceptance:")
        replay = run()
        assert replay["accepted"] == [] and transport.pulls == [] and connection.starts == 1
        assert replay["condition"] == result["condition"]
        # A matching, still-unsettled original owner gets one bounded retirement
        # phase per invocation. An interrupt claim alone never suppresses it.
        assert connection.close_calls == (0 if settlement == "missing" else 1 if settlement is True else 2)
        assert (sqlite_tmp_path / "repository/protected-worker-output").read_text() == "retain"
    finally:
        connection.retire_fixture()
