# SPDX-License-Identifier: Apache-2.0
"""Synthetic native Git admission, immutable copy and literal recovery proofs."""

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_task_166 import TARGET, commit_source, feeder_source
from test_task_execution import (
    ALLOCATION,
    FixtureConnection,
    deflake_edit,
    fixture_task,
    live_environment,
    make_source,
    run,
)

from creatidy_kernel.adapters import cli, task_execution
from creatidy_kernel.adapters.reference import reference_git, reference_request
from creatidy_kernel.adapters.source_cache import SourceAcquisitionError, acquire_source, source_use
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import TaskRuntimeConfig, TaskSpec, export_task, resolve_source
from creatidy_kernel.core.resources import Allocation, ResourceRequest

pytest_plugins = ["test_sqlite_store"]


@pytest.mark.parametrize("failure", ["origin", "dirty", "missing", "stale", "pin", "gitfile", "symlink"])
def test_source_refusal_precedes_all_native_and_forge_starts(sqlite_tmp_path: Path, failure: str) -> None:
    source = make_source(sqlite_tmp_path)
    task = fixture_task(repeats=1)
    if failure == "origin":
        reference_git(source, "remote", "set-url", "origin", "https://other.invalid/Owner/repo")
    elif failure == "dirty":
        (source / "untracked").write_text("unowned work\n")
    elif failure in {"missing", "stale"}:
        path = source / "tests" / "test_e2e_execution.py"
        if failure == "missing":
            path.unlink()
        else:
            path.write_text("import unittest\n# obsolete structure\n")
        reference_git(source, "add", "-A")
        reference_git(source, "commit", "-m", "synthetic incompatible baseline")
    elif failure == "pin":
        task = replace(task, expected_base_sha="1" * 40)
    else:
        gitdir = source / ".git"
        moved = sqlite_tmp_path / "original-gitdir"
        gitdir.rename(moved)
        if failure == "gitfile":
            gitdir.write_text(f"gitdir: {moved}\n")
        else:
            gitdir.symlink_to(moved, target_is_directory=True)
    connection = FixtureConnection(lambda _: pytest.fail("model must not execute"))
    effects: list[str] = []

    def forge(_directory: Path):
        effects.append("forge")
        pytest.fail("Forge must not be constructed")

    with pytest.raises((SourceAcquisitionError, ValueError)):
        task_execution.run_task(
            sqlite_tmp_path / "control",
            task=task,
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=None,
            allocation=ALLOCATION,
            forge_factory=forge,
        )
    assert connection.starts == 0 and effects == []


@pytest.mark.parametrize(
    "configuration", ["core.fsmonitor", "core.hooksPath", "filter.fixture.clean", "include.path", "credential.helper"]
)
def test_local_git_configuration_is_rejected_before_any_execution(sqlite_tmp_path: Path, configuration: str) -> None:
    source = make_source(sqlite_tmp_path)
    canary = sqlite_tmp_path / "no-execution"
    executable = sqlite_tmp_path / "hostile-helper"
    executable.write_text(f"#!/bin/sh\n: > '{canary}'\nexit 0\n")
    executable.chmod(0o700)
    reference_git(source, "config", configuration, str(executable))
    connection = FixtureConnection(lambda _: pytest.fail("model must not execute"))
    with pytest.raises(SourceAcquisitionError, match="checkout_unsuitable"):
        run(sqlite_tmp_path / "control", source, connection, task=fixture_task(repeats=1))
    assert connection.starts == 0 and not canary.exists()


def test_indented_include_cannot_hide_executable_configuration(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    (source / ".gitattributes").write_text("README.md filter=probe\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "synthetic filtered source")
    marker = sqlite_tmp_path / "no-execution"
    helper = sqlite_tmp_path / "filter"
    helper.write_text(f"#!/bin/sh\n/usr/bin/touch '{marker}'\n/bin/cat\n")
    helper.chmod(0o700)
    included = sqlite_tmp_path / "included-config"
    included.write_text(f'[filter "probe"]\n clean = {helper}\n')
    config = source / ".git/config"
    config.write_text(config.read_text() + f'\n[branch "benign"]\n remote = origin\n  [include]\n  path = {included}\n')
    tracked = source / "README.md"
    stamp = tracked.stat()
    os.utime(tracked, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2000000000))
    with pytest.raises(SourceAcquisitionError, match="checkout_unsuitable"):
        task_execution.validate_task_source(source, fixture_task(repeats=1), {"PATH": os.defpath})
    assert not marker.exists()


def test_initialized_gitlink_cannot_execute_hidden_child_configuration(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    child = make_source(sqlite_tmp_path / "child")
    (child / ".gitattributes").write_text("README.md filter=probe\n")
    reference_git(child, "add", "-A")
    reference_git(child, "commit", "-m", "synthetic filtered child")
    reference_git(source, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "nested")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "synthetic gitlink")
    reference_git(source, "config", "--remove-section", "submodule.nested")
    marker = sqlite_tmp_path / "no-child-execution"
    helper = sqlite_tmp_path / "filter"
    helper.write_text(f"#!/bin/sh\n/usr/bin/touch '{marker}'\n/bin/cat\n")
    helper.chmod(0o700)
    reference_git(source / "nested", "config", "filter.probe.clean", str(helper))
    tracked = source / "nested/README.md"
    stamp = tracked.stat()
    os.utime(tracked, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 2000000000))
    connection = FixtureConnection(lambda _: pytest.fail("gitlinks cannot start a model"))
    with pytest.raises(SourceAcquisitionError, match="checkout_unsuitable"):
        run(sqlite_tmp_path / "control", source, connection, task=fixture_task(repeats=1))
    assert not marker.exists() and connection.starts == 0


@pytest.mark.parametrize("changed", [False, True])
def test_cli_cancelled_pre_attempt_recovery_needs_no_fresh_source(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], changed: bool
) -> None:
    from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
    from creatidy_kernel.adapters.task_execution import LiveTaskComponents

    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(lambda _: pytest.fail("cancelled work cannot start a model"))
    task = fixture_task(repeats=1)
    assert (
        run(
            control,
            source,
            connection,
            task=task,
            allocation=None,
            allocator=FixedAllocator(ALLOCATION),
            cancel_requested=True,
        )["condition"]
        == "cancelled_no_dispatch"
    )
    assert not (control / "objects.git").exists()
    before = export_task(control)
    if changed:
        reference_git(source, "remote", "set-url", "origin", "https://other.invalid/Owner/repo")
    else:
        source.rename(sqlite_tmp_path / "retained-historical-source")
    environment = live_environment("zai/glm-5.3")
    environment.update(HOME=str(sqlite_tmp_path), PATH=os.defpath)

    def effective(_args: object) -> dict[str, str]:
        return environment

    def selected(expected_base_sha: str | None) -> TaskSpec:
        return replace(task, expected_base_sha=expected_base_sha)

    monkeypatch.setattr(cli, "_effective_environment", effective)
    monkeypatch.setitem(cli.TASKS, "143", selected)

    def forge(_directory: Path):
        pytest.fail("cancelled work cannot construct Forge")

    def components(_config: TaskRuntimeConfig, _task: TaskSpec) -> LiveTaskComponents:
        return LiveTaskComponents(FixedAllocator(ALLOCATION), lambda: connection, forge, frozenset())

    monkeypatch.setattr(cli, "compose_task_live", components)
    assert (
        cli.main(
            ["task", "run", "--task", "143", "--data-dir", str(control), "--approve", "--trusted-development", "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["condition"] == "cancelled_no_dispatch"
    assert connection.starts == 0 and export_task(control) == before


@pytest.mark.parametrize("configured", [False, True])
def test_cli_source_override_fails_before_runtime_factory(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    source = make_source(sqlite_tmp_path)
    reference_git(source, "remote", "set-url", "origin", "https://other.invalid/Owner/repo")
    environment = live_environment("zai/glm-5.3")
    environment["HOME"] = str(sqlite_tmp_path)
    environment["PATH"] = os.defpath
    if configured:
        environment["CREATIDY_KERNEL_SOURCE_REPOSITORY"] = str(source)

    def effective(_args: object) -> dict[str, str]:
        return environment

    def selected(expected_base_sha: str | None) -> TaskSpec:
        return fixture_task(expected_base_sha=expected_base_sha)

    monkeypatch.setattr(cli, "_effective_environment", effective)
    monkeypatch.setitem(task_execution.TASKS, "143", selected)

    def forbidden(*_args: object):
        pytest.fail("composition/native/Forge factory must not run for a wrong source")

    monkeypatch.setattr(cli, "compose_task_live", forbidden)
    argv = [
        "task",
        "run",
        "--task",
        "143",
        "--data-dir",
        str(sqlite_tmp_path / "control"),
        "--approve",
        "--trusted-development",
    ]
    if not configured:
        argv.extend(["--repo", str(source)])
    assert cli.main(argv) == 1


def test_166_admission_checks_real_structural_baseline_and_exact_origin(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    commit_source(source, feeder_source())
    task = task_execution.scarcity_router_166_task()
    reference_git(source, "remote", "set-url", "origin", task.repository_url)

    # Executable discovery only, no verification or native connection.
    def which(name: str, *, path: str) -> str:
        return "/usr/bin/" + name

    monkeypatch.setattr(task_execution.shutil, "which", which)
    base, _, _ = task_execution.validate_task_source(source, task, {"PATH": os.defpath})
    assert base == reference_git(source, "rev-parse", "HEAD")
    path = source / TARGET
    path.write_text(
        path.read_text().replace("test_reader_startup_failure_terminates_child", "obsolete_reader_scenario")
    )
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "synthetic weakened feeder baseline")
    with pytest.raises(SourceAcquisitionError, match="task_baseline_invalid"):
        task_execution.validate_task_source(source, task, {"PATH": os.defpath})


def test_snapshot_validation_crossing_deadline_never_dispatches_turn(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    clock = [1000]
    armed = [False]
    methods: list[str] = []
    monkeypatch.setattr(time, "time", lambda: clock[0])
    validate = task_execution.validate_task_source

    class Connection(FixtureConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            methods.append(method)
            result = super().request(method, params)
            if method == "thread/start":
                armed[0] = True
            return result

    def checked(
        source_path: Path,
        task: TaskSpec,
        environment: Mapping[str, str],
        *,
        base: str | None = None,
        base_resolver: Callable[[], tuple[str, str]] | None = None,
        snapshot: bool = False,
    ) -> tuple[str, str, dict[str, object]]:
        result = validate(source_path, task, environment, base=base, base_resolver=base_resolver, snapshot=snapshot)
        if armed[0] and snapshot:
            clock[0] = 4301
        return result

    monkeypatch.setattr(task_execution, "validate_task_source", checked)
    result = run(control, source, Connection(lambda _: None), task=fixture_task(repeats=1))
    assert result["deadline"] == 4300
    assert "turn/start" not in methods
    assert "acceptance" not in result and "pr" not in result


def test_snapshot_revalidated_after_allocator_callback_before_native_start(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(lambda _: pytest.fail("model must not execute"))

    class MutatingAllocator:
        def select(self, request: ResourceRequest) -> Allocation:
            reference_git(control / "objects.git", "config", "core.fsmonitor", "/synthetic/untrusted")
            return ALLOCATION

    with pytest.raises(SourceAcquisitionError, match="checkout_unsuitable"):
        run(control, source, connection, task=fixture_task(repeats=1), allocation=None, allocator=MutatingAllocator())
    assert connection.starts == 0


def test_validated_snapshot_keeps_original_subject_when_source_moves(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    original = reference_git(source, "rev-parse", "HEAD")
    connection = FixtureConnection(deflake_edit)

    class SourceMoves:
        def select(self, request: ResourceRequest) -> Allocation:
            (source / "README.md").write_text("changed after the immutable copy cut\n")
            return ALLOCATION

    control = sqlite_tmp_path / "control"
    result = run(control, source, connection, task=fixture_task(repeats=1), allocation=None, allocator=SourceMoves())
    assert connection.starts == 1 and result["condition"] == "accepted"
    exported = export_task(control)
    assert cast(dict[str, object], exported["candidate"])["base"] == original
    assert (source / "README.md").read_text() == "changed after the immutable copy cut\n"


@pytest.mark.parametrize("placement", ["refreshed", "nondefault", "remote-only-pin"])
def test_dispatch_copies_exact_base_reachable_only_from_remote_refs(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    from creatidy_kernel.adapters import source_cache

    remote = make_source(sqlite_tmp_path)
    root = sqlite_tmp_path / "cache"
    task = fixture_task(repeats=1)
    if placement == "nondefault":
        reference_git(remote, "branch", "default-other")
        reference_git(remote, "commit", "--allow-empty", "-m", "new develop base")
        reference_git(remote, "checkout", "default-other")
    source = acquire_source(task.repository_url, cache_root=root, clone_from=str(remote))
    if placement != "nondefault":
        reference_git(remote, "commit", "--allow-empty", "-m", "refreshed develop base")
        real_run = source_cache.run_git_bounded

        def fetch(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int):
            if argv[1] == "fetch":
                argv = [argv[0], "fetch", "--prune", str(remote), "+refs/heads/*:refs/remotes/origin/*"]
            return real_run(argv, cwd, env, timeout, max_bytes)

        with monkeypatch.context() as patch:
            patch.setattr(source_cache, "run_git_bounded", fetch)
            assert acquire_source(task.repository_url, cache_root=root) == source
    base = reference_git(remote, "rev-parse", "refs/heads/develop")
    assert reference_git(source, "rev-parse", "HEAD") != base
    if placement == "remote-only-pin":
        task = replace(task, expected_base_sha=base)
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    control = sqlite_tmp_path / "control"
    connection = FixtureConnection(deflake_edit)
    result = run(control, source, connection, task=task)
    assert result["condition"] == "accepted" and connection.starts == 1
    assert cast(dict[str, object], export_task(control)["candidate"])["base"] == base
    assert reference_git(control / "objects.git", "cat-file", "-t", base) == "commit"
    assert before == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_source_lease_covers_copy_but_not_live_session(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote = make_source(sqlite_tmp_path)
    root = sqlite_tmp_path / "cache"
    source = acquire_source(fixture_task().repository_url, cache_root=root, clone_from=str(remote))
    original_copy = task_execution.copy_source_objects
    leased: list[bool] = []

    def copy(source_path: Path, target: Path, *, base: str) -> None:
        with pytest.raises(SourceAcquisitionError, match="source_busy"):
            acquire_source(fixture_task().repository_url, cache_root=root, clone_from=str(remote))
        leased.append(True)
        original_copy(source_path, target, base=base)

    monkeypatch.setattr(task_execution, "copy_source_objects", copy)
    connection = FixtureConnection(deflake_edit)
    connection.status = "inProgress"
    result = run(sqlite_tmp_path / "control", source, connection, task=fixture_task(repeats=1))
    assert result["condition"] in {"running", "waiting"} and leased == [True]
    with source_use(source, fixture_task().repository_url):
        assert source.exists()  # No healthy-session serialization after the copy.


def test_original_source_loader_never_reacquires_or_renews_terminal_work(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source(sqlite_tmp_path)
    control = sqlite_tmp_path / "control"
    task = fixture_task(repeats=1)
    accepted = run(control, source, FixtureConnection(deflake_edit), task=task)
    assert accepted["condition"] == "accepted"
    before = export_task(control)
    source.rename(sqlite_tmp_path / "retained-historical-source")

    def forbidden(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("original envelope must not select a fresh default cache")

    monkeypatch.setattr(task_execution, "acquire_source", forbidden)
    resolved = resolve_source(
        TaskRuntimeConfig.parse(live_environment("zai/glm-5.3")),
        task,
        None,
        environment={"PATH": os.defpath},
        directory=control,
    )
    assert resolved == source and not source.exists()
    connection = FixtureConnection(lambda _: pytest.fail("terminal recovery must not execute"))
    result = run(control, resolved, connection, task=task)
    assert result["condition"] == "accepted" and connection.starts == 0
    assert export_task(control) == before


@pytest.mark.parametrize("task_id", ["143", "166"])
def test_historical_hostless_source_and_frozen_identity_survive_expired_recovery(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_id: str,
) -> None:
    source = make_source(sqlite_tmp_path)
    if task_id == "166":
        commit_source(source, feeder_source())
    factory = task_execution.TASKS[task_id]
    original = factory()
    reference_git(source, "remote", "set-url", "origin", original.repository_url)
    base = reference_git(source, "rev-parse", "HEAD")
    task = factory(expected_base_sha=base)
    legacy = sqlite_tmp_path / "hostless-cache" / "BioMedical-IT" / "scarcity-router"
    legacy.parent.mkdir(parents=True)
    source.rename(legacy)
    (legacy / ".git" / "kernel-source-cache-v1").write_text("1\n")
    source_bytes = {str(p.relative_to(legacy)): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    control = sqlite_tmp_path / "control"
    environment = {"PATH": os.environ.get("PATH", os.defpath)}

    class InterruptedConnection(FixtureConnection):
        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "turn/interrupt":
                self.status = "interrupted"
                return {}
            return super().request(method, params)

    first = InterruptedConnection(lambda _: None)
    first.status = "inProgress"

    def advance(connection: FixtureConnection) -> dict[str, object]:
        return task_execution.run_task(
            control,
            task=task,
            source_repository=legacy,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=None,
            allocation=ALLOCATION,
            command_environment=environment,
        )

    active = advance(first)
    assert first.starts == 1 and active["condition"] in {"running", "waiting"}
    assert source_bytes == {str(p.relative_to(legacy)): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
    operations = ("task_execution:mode", "task_execution:base", task_execution.OPERATION_ID)
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        requests = {key: store.operation(key).request_json for key in operations}
        attempt = store.load(task_execution.PROGRAM_ID).attempt(task_execution.ATTEMPT_ID).spec
        mode = reference_request(store, "task_execution:mode")
        assert mode["source"] == str(legacy) and mode["expected_base_sha"] == base
        assert mode["task_digest"] == original.digest == task.digest
        deadline = cast(int, mode["deadline"])
    legacy.rename(legacy.with_name("retained-original"))

    def forbidden(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("historical recovery cannot reacquire a new namespace")

    monkeypatch.setattr(task_execution, "acquire_source", forbidden)
    resolved = resolve_source(
        TaskRuntimeConfig.parse(live_environment("zai/glm-5.3")), task, None, environment=environment, directory=control
    )
    assert resolved == legacy and not resolved.exists()
    monkeypatch.setattr(task_execution.time, "time", lambda: deadline + 1)
    recovered = InterruptedConnection(lambda _: pytest.fail("expired work cannot execute"))
    assert advance(recovered)["condition"] == "expired" and recovered.starts == 0
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        assert requests == {key: store.operation(key).request_json for key in operations}
        assert store.load(task_execution.PROGRAM_ID).attempt(task_execution.ATTEMPT_ID).spec == attempt


def test_invalid_utf8_baseline_is_not_replacement_decoded_into_admission(sqlite_tmp_path: Path) -> None:
    source = make_source(sqlite_tmp_path)
    path = source / "tests" / "test_e2e_execution.py"
    path.write_bytes(path.read_bytes() + b"# invalid source encoding: \xff\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "synthetic invalid source encoding")
    connection = FixtureConnection(lambda _: pytest.fail("invalid baseline cannot start a model"))
    with pytest.raises(SourceAcquisitionError, match="task_baseline_invalid"):
        run(sqlite_tmp_path / "control", source, connection, task=fixture_task(repeats=1))
    assert connection.starts == 0
