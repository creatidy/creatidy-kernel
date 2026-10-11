# SPDX-License-Identifier: Apache-2.0
"""Synthetic native Git generations; no harness, model or external Forge operation."""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from creatidy_kernel.adapters import git_workspace, worker_collection
from creatidy_kernel.adapters.git_workspace import GitWorkspace
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.source_cache import SourceAcquisitionError
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.execution import ExecutionConflict, Presence, TrustMode, UnsupportedExecution, WorkspaceSpec
from creatidy_kernel.ports.execution import Workspace

pytest_plugins = ["test_sqlite_store"]


@dataclass
class Materialization:
    workspace: GitWorkspace
    spec: WorkspaceSpec
    store: SQLiteProgramStore
    source: Path


@pytest.fixture
def materialization(sqlite_tmp_path: Path) -> Iterator[Materialization]:
    source = sqlite_tmp_path / "source"
    source.mkdir(mode=0o700)
    reference_git(source, "init", "--template=")
    (source / "hello.txt").write_bytes(b"original exact source\x00binary bytes\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "synthetic source")
    reference_git(source, "remote", "add", "origin", "https://forge.invalid/Owner/repo")
    base = reference_git(source, "rev-parse", "HEAD").strip()
    control = sqlite_tmp_path / "control"
    control.mkdir(mode=0o700)
    with SQLiteProgramStore(control / "kernel.db") as store:
        workspace = GitWorkspace(
            store,
            source,
            "https://forge.invalid/Owner/repo",
            "logical-repository",
            control / "workspace",
            control / "objects.git",
        )
        spec = WorkspaceSpec(
            "logical-repository", base, (), "opaque-toolchain", "trusted", TrustMode.TRUSTED_DEVELOPMENT
        )
        yield Materialization(workspace, spec, store, source)


def test_exact_source_generation_is_durable_and_never_hardlinks_originals(materialization: Materialization) -> None:
    cell = materialization
    port: Workspace = cell.workspace
    handle = port.materialize("workspace-one", cell.spec)
    root, objects = cell.workspace.workspace, cell.workspace.objects
    assert (root / "candidate/hello.txt").read_bytes() == b"original exact source\x00binary bytes\n"
    assert reference_git(root / "candidate", "rev-parse", "HEAD").strip() == cell.spec.base_revision
    assert reference_git(root / "candidate", "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert all(
        path.stat().st_nlink == 1
        for path in (*objects.rglob("*"), *(root / "candidate/.git").rglob("*"))
        if path.is_file()
    )
    assert port.reconcile(handle.key).presence is Presence.FOUND
    assert port.materialize(handle.key, cell.spec) == handle
    operation_id = "workspace:" + hashlib.sha256(handle.key.encode()).hexdigest()
    assert cell.store.operation(operation_id).attempts == 1
    assert cell.store.operation(operation_id).status == "accepted"
    before = (root / "candidate/hello.txt").read_bytes()
    (root / "candidate/hello.txt").write_bytes(b"worker change")
    assert port.materialize(handle.key, cell.spec) == handle
    assert (root / "candidate/hello.txt").read_bytes() != before
    with pytest.raises(UnsupportedExecution, match="preserve"):
        port.cleanup(handle, worker_settled=True)
    assert root.exists() and objects.exists()


def test_reopen_reads_original_objects_without_reacquiring_changed_or_missing_source(
    materialization: Materialization,
) -> None:
    cell = materialization
    handle = cell.workspace.materialize("workspace-one", cell.spec)
    (cell.source / "hello.txt").write_bytes(b"source moved after admission")
    reference_git(cell.source, "add", "-A")
    reference_git(cell.source, "commit", "-m", "later source")
    cell.source.rename(cell.source.with_name("preserved-source"))
    reopened = GitWorkspace(
        cell.store,
        cell.source,
        "https://forge.invalid/Owner/repo",
        cell.spec.repository,
        cell.workspace.workspace,
        cell.workspace.objects,
    )
    assert reopened.reconcile(handle.key).presence is Presence.FOUND
    assert reopened.materialize(handle.key, cell.spec) == handle
    assert (reopened.workspace / "candidate/hello.txt").read_bytes() == b"original exact source\x00binary bytes\n"


@pytest.mark.parametrize("target", ["workspace", "objects", "candidate", "config"])
def test_changed_original_generation_stays_unknown_without_replacement(
    materialization: Materialization, target: str
) -> None:
    cell = materialization
    handle = cell.workspace.materialize("workspace-one", cell.spec)
    if target == "config":
        (cell.workspace.objects / "config").write_bytes(b"invalid altered original configuration")
    else:
        path = {
            "workspace": cell.workspace.workspace,
            "objects": cell.workspace.objects,
            "candidate": cell.workspace.workspace / "candidate",
        }[target]
        path.rename(path.with_name("retained-" + path.name))
        path.mkdir()
    assert cell.workspace.reconcile(handle.key).presence is Presence.UNKNOWN
    with pytest.raises((ExecutionConflict, SourceAcquisitionError, ValueError)):
        cell.workspace.materialize(handle.key, cell.spec)


@pytest.mark.parametrize("when", [1, 2])
def test_partial_publication_is_preserved_and_never_retried(
    materialization: Materialization, monkeypatch: pytest.MonkeyPatch, when: int
) -> None:
    cell = materialization
    original = git_workspace.publish_directory
    calls: list[Path] = []

    def publish(staging: Path, target: Path) -> None:
        calls.append(staging)
        if len(calls) == when:
            raise OSError("synthetic publication failure")
        original(staging, target)

    monkeypatch.setattr(git_workspace, "publish_directory", publish)
    with pytest.raises(OSError, match="publication failure"):
        cell.workspace.materialize("workspace-one", cell.spec)
    retained = {path: path.stat().st_ino for path in cell.workspace.workspace.parent.glob(".workspace-*")}
    assert retained
    with pytest.raises(ExecutionConflict, match="preserved"):
        cell.workspace.materialize("workspace-one", cell.spec)
    assert retained == {path: path.stat().st_ino for path in retained}
    assert cell.workspace.reconcile("workspace-one").presence is Presence.UNKNOWN
    assert len(calls) == when


def test_foreign_root_is_not_adopted_or_cleaned(materialization: Materialization) -> None:
    cell = materialization
    cell.workspace.workspace.mkdir()
    canary = cell.workspace.workspace / "unowned"
    canary.write_bytes(b"retain")
    with pytest.raises(ExecutionConflict, match="unowned"):
        cell.workspace.materialize("workspace-one", cell.spec)
    assert canary.read_bytes() == b"retain"


def test_unsupported_symlink_tree_is_not_published(materialization: Materialization) -> None:
    cell = materialization
    (cell.source / "unsafe").symlink_to(cell.workspace.workspace.parent / "kernel.db")
    reference_git(cell.source, "add", "unsafe")
    reference_git(cell.source, "commit", "-m", "synthetic unsafe source")
    spec = replace(cell.spec, base_revision=reference_git(cell.source, "rev-parse", "HEAD").strip())
    with pytest.raises(UnsupportedExecution, match="unsupported entries"):
        cell.workspace.materialize("workspace-one", spec)
    assert not cell.workspace.workspace.exists()
    assert not cell.workspace.objects.exists()


def test_changed_spec_cannot_replace_materialized_generation(materialization: Materialization) -> None:
    cell = materialization
    cell.workspace.materialize("workspace-one", cell.spec)
    reopened = GitWorkspace(
        cell.store,
        cell.source,
        "https://forge.invalid/Owner/repo",
        cell.spec.repository,
        cell.workspace.workspace,
        cell.workspace.objects,
    )
    with pytest.raises(OperationConflict, match="different request"):
        reopened.materialize("workspace-one", replace(cell.spec, toolchain_digest="changed"))
    assert (cell.workspace.workspace / "candidate/hello.txt").exists()


def test_recorded_unknown_schema_cannot_recover_original_generation(
    materialization: Materialization, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = materialization
    handle = cell.workspace.materialize("workspace-one", cell.spec)
    operation = "workspace:" + hashlib.sha256(handle.key.encode()).hexdigest()
    receipt = json.loads(cell.store.artifact(operation, "workspace-created"))
    assert receipt["version"] == 1
    original = cell.store.find_artifact

    def artifact(operation_id: str, name: str) -> bytes | None:
        if name == "workspace-created":
            return json.dumps({**receipt, "version": 2}).encode()
        return original(operation_id, name)

    monkeypatch.setattr(cell.store, "find_artifact", artifact)
    assert cell.workspace.reconcile(handle.key).presence is Presence.UNKNOWN
    assert cell.workspace.reconcile("different").presence is Presence.UNKNOWN
    assert cell.workspace.reconcile(handle.key, "foreign").presence is Presence.UNKNOWN


@pytest.mark.parametrize("configuration", ["remote", "hook"])
def test_source_identity_and_executable_config_refuse_without_running_source(
    materialization: Materialization, configuration: str
) -> None:
    cell = materialization
    canary = cell.source.parent / "no-source-execution"
    if configuration == "remote":
        reference_git(cell.source, "remote", "set-url", "origin", "https://other.invalid/Owner/repo")
    else:
        helper = cell.source.parent / "helper"
        helper.write_text(f"#!/bin/sh\n/usr/bin/touch '{canary}'\n")
        helper.chmod(0o700)
        reference_git(cell.source, "config", "core.hooksPath", str(helper))
    with pytest.raises(SourceAcquisitionError):
        cell.workspace.materialize("workspace-one", cell.spec)
    assert not canary.exists()
    assert not cell.workspace.workspace.exists()


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_ambiguous_or_future_generation_version_preserves_original_evidence(
    materialization: Materialization, monkeypatch: pytest.MonkeyPatch, version: object
) -> None:
    cell = materialization
    handle = cell.workspace.materialize("workspace-one", cell.spec)
    operation = "workspace:" + hashlib.sha256(handle.key.encode()).hexdigest()
    original = cell.store.find_artifact
    received = cell.store.artifact(operation, "workspace-created")
    receipt = json.loads(received)

    def artifact(operation_id: str, name: str) -> bytes | None:
        return (
            json.dumps({**receipt, "version": version}).encode()
            if name == "workspace-created"
            else original(operation_id, name)
        )

    monkeypatch.setattr(cell.store, "find_artifact", artifact)
    assert cell.workspace.reconcile(handle.key).presence is Presence.UNKNOWN
    assert original(operation, "workspace-created") == received
    assert (cell.workspace.workspace / "candidate/hello.txt").exists()


@pytest.mark.parametrize(
    "layout,count", [("flat", 6), ("flat", 7), ("shared", 4), ("shared", 5), ("nested", 2), ("nested", 3)]
)
def test_source_preflight_matches_directory_cost_and_strict_collection_boundary(
    materialization: Materialization, monkeypatch: pytest.MonkeyPatch, layout: str, count: int
) -> None:
    cell = materialization
    prefix = {"flat": "", "shared": "shared/", "nested": "shared/nested/"}[layout]
    for index in range(count):
        path = cell.source / f"{prefix}file-{index}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    reference_git(cell.source, "add", "-A")
    reference_git(cell.source, "commit", "-m", "synthetic bounded tree")
    spec = replace(cell.spec, base_revision=reference_git(cell.source, "rev-parse", "HEAD").strip())
    monkeypatch.setattr(git_workspace, "MAX_ENTRIES", 8)
    monkeypatch.setattr(worker_collection, "MAX_ENTRIES", 8)
    cost = 1 + count + 2 * {"flat": 0, "shared": 1, "nested": 2}[layout]
    if cost >= 8:
        with pytest.raises(UnsupportedExecution, match="collection bounds"):
            cell.workspace.materialize("workspace-one", spec)
        assert not cell.workspace.workspace.exists() and not cell.workspace.objects.exists()
    else:
        cell.workspace.materialize("workspace-one", spec)
        collected = worker_collection.collect_directory(
            cell.workspace.workspace / "candidate", exclude=frozenset({".git"})
        )
        assert collected.consumed == cost and not collected.refusals
        assert len(collected.files) == count + 1


def test_many_distinct_directories_cannot_publish_an_uncollectable_source(materialization: Materialization) -> None:
    cell = materialization
    for index in range(900):
        parent = cell.source / str(index) / "n"
        parent.mkdir(parents=True)
        (parent / "file").write_bytes(b"")
    reference_git(cell.source, "add", "-A")
    reference_git(cell.source, "commit", "-m", "synthetic directory-heavy tree")
    spec = replace(cell.spec, base_revision=reference_git(cell.source, "rev-parse", "HEAD").strip())
    with pytest.raises(UnsupportedExecution, match="collection bounds"):
        cell.workspace.materialize("workspace-one", spec)
    assert not cell.workspace.workspace.exists() and not cell.workspace.objects.exists()
    assert (cell.source / "hello.txt").exists()
