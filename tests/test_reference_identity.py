# SPDX-License-Identifier: Apache-2.0
"""Resolved identity is durable evidence, never a pre-dispatch assertion."""

from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from test_reference_live import Connection

from creatidy_kernel.adapters.codex_runtime import CodexRuntime
from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference import (
    ReferenceInterrupted,
    export_reference,
    reference_commit,
    reference_git,
    run_reference,
)
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.execution import ExecutionRequest
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.ports.program_store import OperationRecord

pytest_plugins = ["test_sqlite_store"]


def live_scenario(directory: Path) -> tuple[Connection, Callable[[], dict[str, object]]]:
    connection = Connection(directory)
    repository = Reference("forgejo:synthetic/reference")
    transport = SyntheticForgeTransport(repository)
    forge = ForgejoForge(transport, transport, lambda _: True)
    objects = directory / "objects.git"
    objects.mkdir()
    reference_git(objects, "init", "--bare")
    transport.branches["develop"] = reference_commit(objects, b"")

    def run() -> dict[str, object]:
        return run_live_reference(
            directory,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            allocation=Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "owner selection"),
            forge=forge,
            repository=repository,
            object_source=objects,
            deadline=100,
            max_observations=4,
        )

    return connection, run


def test_codex_receipt_crash_recovers_original_resolution_before_identity_publication(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: 10)
    connection, run = live_scenario(sqlite_tmp_path)
    observe = SQLiteProgramStore.observe
    start = CodexRuntime.start

    def unresolved_start(runtime: CodexRuntime, request: ExecutionRequest) -> str:
        assert request.identity.requested == "model"
        assert request.identity.resolved is None and request.identity.observed is None
        return start(runtime, request)

    def crash_after_receipt(
        store: SQLiteProgramStore,
        operation_id: str,
        fence: int,
        observation_id: str,
        kind: str,
        *,
        reference: str | None = None,
    ) -> OperationRecord:
        result = observe(store, operation_id, fence, observation_id, kind, reference=reference)
        if operation_id.startswith("runtime:") and kind == "accepted":
            raise ReferenceInterrupted("receipt committed before identity evidence")
        return result

    with (
        patch.object(SQLiteProgramStore, "observe", crash_after_receipt),
        patch.object(CodexRuntime, "start", unresolved_start),
    ):
        with pytest.raises(ReferenceInterrupted):
            run()
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        assert store.operation("runtime:reference:first").accepted_reference == "codex:thread-1:turn-1"
        assert store.find_artifact("runtime:reference:first", "identity") is None
        assert store.find_artifact("runtime:reference:first", "runtime-receipt") is not None
    result = run()
    assert result["status"] == "completed"
    assert connection.starts == 2
    identity = cast(dict[str, object], cast(list[dict[str, object]], result["attempts"])[0]["identity"])
    assert identity["resolved"] == "model" and identity["observed"] is None


def test_legacy_native_receipt_without_resolution_evidence_still_fails_closed(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: 10)
    connection, run = live_scenario(sqlite_tmp_path)
    observe = SQLiteProgramStore.observe

    def crash_after_receipt(
        store: SQLiteProgramStore,
        operation_id: str,
        fence: int,
        observation_id: str,
        kind: str,
        *,
        reference: str | None = None,
    ) -> OperationRecord:
        result = observe(store, operation_id, fence, observation_id, kind, reference=reference)
        if kind == "accepted":
            raise ReferenceInterrupted("legacy receipt without configuration")
        return result

    with patch.object(SQLiteProgramStore, "observe", crash_after_receipt):
        with pytest.raises(ReferenceInterrupted):
            run()
    find = SQLiteProgramStore.find_artifact

    def legacy_artifacts(store: SQLiteProgramStore, operation_id: str, name: str) -> bytes | None:
        return None if name == "runtime-receipt" else find(store, operation_id, name)

    with patch.object(SQLiteProgramStore, "find_artifact", legacy_artifacts):
        result = run()
    assert result["condition"] == "identity_unavailable" and result["accepted"] == []
    assert connection.starts == 1


def test_codex_restart_uses_original_durable_resolution_not_a_guessed_model(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.reference_live.time.time", lambda: 10)
    connection, run = live_scenario(sqlite_tmp_path)
    finalize = SQLiteProgramStore.finalize_artifact

    def crash_after_identity(store: SQLiteProgramStore, operation_id: str, name: str, data: bytes) -> str:
        result = finalize(store, operation_id, name, data)
        if name == "identity":
            raise ReferenceInterrupted("identity evidence committed")
        return result

    with patch.object(SQLiteProgramStore, "finalize_artifact", crash_after_identity):
        with pytest.raises(ReferenceInterrupted):
            run()
    result = run()
    assert result["status"] == "completed" and connection.starts == 2
    for attempt in cast(list[dict[str, object]], result["attempts"]):
        identity = cast(dict[str, object], attempt["identity"])
        assert identity["requested"] == identity["resolved"] == "model"
        assert identity["observed"] is None


def test_offline_receipt_restart_reconstructs_durable_identity(sqlite_tmp_path: Path) -> None:
    with pytest.raises(ReferenceInterrupted):
        run_reference(sqlite_tmp_path, owner_approved=True, fault="receipt")
    result = run_reference(sqlite_tmp_path, owner_approved=True)
    assert result["status"] == "completed"
    exported = export_reference(sqlite_tmp_path)
    for attempt in cast(list[dict[str, object]], exported["attempts"]):
        identity = cast(dict[str, object], attempt["identity"])
        assert identity["resolved"] == "deterministic"


def test_legacy_acceptance_without_identity_cannot_publish_or_export_success(sqlite_tmp_path: Path) -> None:
    run_reference(sqlite_tmp_path, owner_approved=True, fault="pr-commit")
    find = SQLiteProgramStore.find_artifact

    def missing_identity(store: SQLiteProgramStore, operation: str, name: str) -> bytes | None:
        return None if name == "identity" else find(store, operation, name)

    with (
        patch.object(SQLiteProgramStore, "find_artifact", missing_identity),
        patch("creatidy_kernel.adapters.reference.deliver_reference_pr") as deliver,
    ):
        with pytest.raises(ValueError, match="runtime identity"):
            run_reference(sqlite_tmp_path, owner_approved=True)
        with pytest.raises(ValueError, match="runtime identity"):
            export_reference(sqlite_tmp_path)
        deliver.assert_not_called()
