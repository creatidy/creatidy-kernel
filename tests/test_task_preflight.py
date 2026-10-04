# SPDX-License-Identifier: Apache-2.0
"""Quota-free task readiness with synthetic native protocols and disposable repositories."""

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from test_task_execution import (
    ALLOCATION,
    BASE_TEST_FILE,
    FixtureConnection,
    deflake_edit,
    fixture_task,
    live_environment,
    make_source,
    run,
    selection_document,
    write_fake_codex,
)

from creatidy_kernel.adapters.cli import main
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import (
    CODEX_METHODS,
    ConfigurationError,
    PreflightReason,
    TaskInterrupted,
    TaskRuntimeConfig,
    VerificationCommand,
    compose_task_live,
    preflight_task,
    task_paths,
)

pytest_plugins = ["test_sqlite_store"]


@pytest.fixture
def ready_inputs(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], Path, Path]:
    source = make_source(sqlite_tmp_path)
    reference_git(source, "remote", "add", "origin", fixture_task().repository_url + ".git")
    base = reference_git(source, "rev-parse", "refs/heads/develop")
    environment = live_environment("zai/glm-5.3/low")
    environment["CREATIDY_KERNEL_CODEX_BIN"] = str(write_fake_codex(sqlite_tmp_path, "1.2.3"))
    environment["CREATIDY_KERNEL_CODEX_VERSION"] = "1.2.3"
    environment["CREATIDY_KERNEL_ROUTER_KEY"] = "synthetic-router-secret"
    environment["HOME"] = str(sqlite_tmp_path / "synthetic-codex-home")
    Path(environment["HOME"]).mkdir()
    environment["PATH"] = os.defpath

    def router(_self: ScarcityRouterAllocator, requirement: dict[str, object]) -> bytes:
        assert requirement["hard_constraints"] == {"requires_tool_use": True, "minimum_input_context_tokens": 128}
        return selection_document("zai", "glm-5.3", "low")

    def forge(
        _self: HTTPSForgejoTransport,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> tuple[int, object]:
        assert method == "GET" and body is None
        prefix = "/repos/BioMedical-IT/scarcity-router"
        if path == prefix:
            return 200, {"full_name": "BioMedical-IT/scarcity-router"}
        if path == prefix + "/branches/develop":
            return 200, {"name": "develop", "commit": {"id": base}}
        assert path == prefix + "/issues/143"
        return 200, {"number": 143, "html_url": fixture_task().repository_url + "/issues/143", "state": "open"}

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", router)
    monkeypatch.setattr(HTTPSForgejoTransport, "request", forge)
    return environment, source, sqlite_tmp_path / "state"


def test_ready_is_inference_free_read_only_and_runs_no_verification(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    initialize: list[str] = []
    exchange = CodexStdio._exchange  # pyright: ignore[reportPrivateUsage] - instrument native initialize only.

    def only_initialize(self: CodexStdio, method: str, params: dict[str, object]) -> dict[str, object]:
        initialize.append(method)
        assert method == "initialize"
        return exchange(self, method, params)

    before = reference_git(source, "rev-parse", "HEAD")
    with (
        patch.object(CodexStdio, "_exchange", only_initialize),
        patch.object(CodexStdio, "request", side_effect=AssertionError("preflight cannot request execution")),
        patch.object(ConditionalGitTransport, "create_agit_pr", side_effect=AssertionError("no Forge writes")),
        patch("creatidy_kernel.adapters.task_execution._run_command", side_effect=AssertionError("no verification")),
        patch(
            "creatidy_kernel.adapters.task_execution.SQLiteProgramStore", side_effect=AssertionError("no state writes")
        ),
    ):
        result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert result.ready and result.blockers == ()
    payload = result.payload()
    assert payload["status"] == "READY_FOR_LIVE_TASK" and payload["schema"] == 1
    assert payload["exact_base_sha"] == before and payload["base_ref"] == "refs/heads/develop"
    assert payload["inference_performed"] is False
    assert payload["forge_writes_performed"] is False and payload["execution_authorized"] is False
    assert initialize == ["initialize"]
    task_evidence = cast(dict[str, object], result.evidence["task"])
    assert task_evidence["verification_executed"] is False
    assert cast(list[dict[str, object]], task_evidence["verification"])[1]["repeats"] == 8
    assert not state.exists()
    assert reference_git(source, "rev-parse", "HEAD") == before
    assert reference_git(source, "status", "--porcelain") == ""
    serialized = json.dumps(payload) + repr(result) + repr(TaskRuntimeConfig.parse(environment))
    for secret in (environment["CREATIDY_KERNEL_ROUTER_KEY"], environment["CREATIDY_KERNEL_FORGE_TOKEN"]):
        assert secret not in serialized


def test_config_missing_fields_are_safe_names_and_blocker_is_machine_readable() -> None:
    result = preflight_task({}, task=fixture_task())
    assert not result.ready and result.exact_base_sha is None
    reasons = {blocker.reason for blocker in result.blockers}
    assert {PreflightReason.CONFIGURATION, PreflightReason.CODEX} <= reasons
    configuration = next(blocker for blocker in result.blockers if blocker.reason is PreflightReason.CONFIGURATION)
    assert "CREATIDY_KERNEL_FORGE_TOKEN" in configuration.fields
    codex = next(blocker for blocker in result.blockers if blocker.reason is PreflightReason.CODEX)
    assert "CREATIDY_KERNEL_CODEX_BIN" in codex.fields and codex.category == "not_found"
    assert result.payload()["status"] == "BLOCKED"


def test_forge_cannot_echo_a_credential_as_a_valid_branch_sha(
    ready_inputs: tuple[dict[str, str], Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, source, state = ready_inputs
    secret = "a" * 40
    environment["CREATIDY_KERNEL_FORGE_TOKEN"] = secret
    request = HTTPSForgejoTransport.request

    def echo(
        transport: HTTPSForgejoTransport,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> tuple[int, object]:
        if path.endswith("/branches/develop"):
            return 200, {"name": "develop", "commit": {"id": secret}}
        return request(transport, method, path, body)

    monkeypatch.setattr(HTTPSForgejoTransport, "request", echo)
    result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert not result.ready and result.blockers[0].reason is PreflightReason.FORGE
    assert "forge" not in result.evidence
    assert secret not in json.dumps(result.payload()) + repr(result)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CREATIDY_KERNEL_ROUTER_URL", "http://remote.invalid"),
        ("CREATIDY_KERNEL_ROUTER_URL", "https://synthetic-secret@router.invalid"),
        ("CREATIDY_KERNEL_ROUTER_KEY", "synthetic-secret\ninvalid"),
        ("CREATIDY_KERNEL_RUNTIME_BINDING", "zai/glm-5.3/low/extra"),
        ("CREATIDY_KERNEL_RUNTIME_BINDING", "zai/glm-5.3/ low"),
        ("CREATIDY_KERNEL_CODEX_BIN", "codex"),
        ("CREATIDY_KERNEL_CODEX_VERSION", "latest"),
        ("CREATIDY_KERNEL_FORGE_API", "https://synthetic-secret@forge.invalid/api/v1"),
        ("CREATIDY_KERNEL_FORGE_API", "https://forge.invalid/other"),
        ("CREATIDY_KERNEL_FORGE_REMOTE", "https://forge.invalid/repo.git?token=synthetic-secret"),
        ("CREATIDY_KERNEL_FORGE_REMOTE", "https://other.invalid/BioMedical-IT/scarcity-router.git"),
        ("CREATIDY_KERNEL_FORGE_TOKEN", "synthetic-secret\ninvalid"),
        ("CREATIDY_KERNEL_FORGE_ASKPASS", "relative-helper"),
        ("CREATIDY_KERNEL_SOURCE_REPOSITORY", "relative-source"),
        ("CREATIDY_KERNEL_STATE_DIRECTORY", "relative-state"),
        ("HOME", "relative-home"),
        ("PATH", "/usr/bin:relative-bin"),
        ("TMPDIR", "relative-temp"),
        ("LANG", "invalid\nlocale"),
    ],
)
def test_config_validation_redacts_invalid_values(name: str, value: str) -> None:
    environment = live_environment("zai/glm-5.3/low")
    environment[name] = value
    with pytest.raises(ConfigurationError) as error:
        TaskRuntimeConfig.parse(environment)
    assert name in error.value.fields
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("include_neutral", [False, True])
def test_legacy_environment_is_rejected_without_fallback(include_neutral: bool) -> None:
    environment = live_environment("zai/glm-5.3/low") if include_neutral else {}
    environment["CREATIDY_DOGFOOD_RUNTIME_BINDING"] = "zai/glm-5.3/low"
    with pytest.raises(ConfigurationError, match="unsupported legacy fields"):
        TaskRuntimeConfig.parse(environment)


def test_config_is_immutable_binding_is_parsed_once_and_supplied_environment_is_frozen(
    ready_inputs: tuple[dict[str, str], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _source, _state = ready_inputs
    monkeypatch.setenv("HOME", "/ambient-home-must-not-be-used")
    monkeypatch.setenv("PATH", "/ambient-path-must-not-be-used")
    with patch("creatidy_kernel.adapters.task_execution._runtime_binding", return_value=ALLOCATION) as parse:
        config = TaskRuntimeConfig.parse(environment)
        components = compose_task_live(config, fixture_task())
        assert components.supported_efforts == frozenset()
        parse.assert_called_once_with("zai/glm-5.3/low")
    assert dict(config.child_environment)["HOME"] == environment["HOME"]
    environment["HOME"] = "/later-input-home"
    assert dict(config.child_environment)["HOME"] != environment["HOME"]
    with pytest.raises(FrozenInstanceError):
        config.codex_version = "changed"  # type: ignore[misc]


def test_connection_and_schema_probes_use_supplied_not_ambient_environment(
    ready_inputs: tuple[dict[str, str], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, _source, _state = ready_inputs
    config = TaskRuntimeConfig.parse(environment)
    monkeypatch.setenv("HOME", "/ambient-home-must-not-be-used")
    with (
        patch("creatidy_kernel.adapters.task_execution._codex_schema", return_value=CODEX_METHODS),
        patch("creatidy_kernel.adapters.task_execution.CodexStdio", autospec=True) as stdio,
    ):
        compose_task_live(config, fixture_task()).connection_factory()
    assert stdio.call_args.kwargs["environment"] == dict(config.child_environment)


@pytest.mark.parametrize(
    "failure", ["missing-home", "missing-temp", "missing-binary", "wrong-version", "missing-schema"]
)
def test_codex_readiness_requires_operational_paths_exact_version_and_actual_schema(
    ready_inputs: tuple[dict[str, str], Path, Path],
    failure: str,
) -> None:
    environment, source, state = ready_inputs
    if failure == "missing-home":
        environment.pop("HOME")
    elif failure == "missing-temp":
        environment["TMPDIR"] = str(state / "missing-temp")
    elif failure == "missing-binary":
        environment["CREATIDY_KERNEL_CODEX_BIN"] = str(state / "missing-codex")
    elif failure == "wrong-version":
        environment["CREATIDY_KERNEL_CODEX_VERSION"] = "1.2.4"
    else:
        binary = Path(environment["CREATIDY_KERNEL_CODEX_BIN"])
        binary.write_text(binary.read_text().replace("'turn/interrupt'", "'unsupported/method'"))
    result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert PreflightReason.CODEX in {blocker.reason for blocker in result.blockers}
    assert not result.ready and not state.exists()


def test_path_defaults_are_infrastructure_and_cli_paths_override() -> None:
    environment = live_environment("zai/glm-5.3/none")
    environment["CREATIDY_KERNEL_SOURCE_REPOSITORY"] = "/configured/source"
    environment["CREATIDY_KERNEL_STATE_DIRECTORY"] = "/configured/state"
    config = TaskRuntimeConfig.parse(environment)
    assert task_paths(config, None, None) == (Path("/configured/state"), Path("/configured/source"))
    assert task_paths(config, Path("/explicit/state"), Path("/explicit/source")) == (
        Path("/explicit/state"),
        Path("/explicit/source"),
    )
    assert config.supported_efforts == frozenset({("zai", "glm-5.3", "none")})


@pytest.mark.parametrize(
    "mutation", ["dirty", "wrong-origin", "missing-ref", "missing-assertion", "fixed-race", "invalid-syntax"]
)
def test_preflight_rejects_unsuitable_or_stale_frozen_source(
    ready_inputs: tuple[dict[str, str], Path, Path],
    mutation: str,
) -> None:
    environment, source, state = ready_inputs
    target = source / "tests/test_e2e_execution.py"
    if mutation == "wrong-origin":
        reference_git(source, "remote", "set-url", "origin", "https://other.invalid/other/repo.git")
    elif mutation == "missing-ref":
        reference_git(source, "update-ref", "-d", "refs/heads/develop")
    else:
        if mutation == "missing-assertion":
            target.write_text(BASE_TEST_FILE.replace("assertEqual([], bystander.cancels)", "assertTrue(True)"))
        elif mutation == "fixed-race":
            deflake_edit(source)
        elif mutation == "invalid-syntax":
            target.write_text(BASE_TEST_FILE + "\ninvalid syntax here\n")
        else:
            target.write_text(BASE_TEST_FILE + "\n# uncommitted change\n")
        if mutation != "dirty":
            reference_git(source, "add", "-A")
            reference_git(source, "commit", "-m", "synthetic changed baseline")
    result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    expected = (
        PreflightReason.TASK
        if mutation in {"missing-assertion", "fixed-race", "invalid-syntax"}
        else PreflightReason.SOURCE
    )
    assert expected in {blocker.reason for blocker in result.blockers}
    assert not result.ready and not state.exists()


def test_allowed_file_and_command_shapes_checked_without_executing(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    for task in (
        replace(fixture_task(), allowed_paths=frozenset({"not-present.py"})),
        replace(
            fixture_task(), verification=(VerificationCommand((sys.executable, "-c", "raise AssertionError"), 30),)
        ),
    ):
        result = preflight_task(environment, task=task, source_repository=source, directory=state)
        assert PreflightReason.TASK in {blocker.reason for blocker in result.blockers}
    assert not state.exists()


def test_verification_tool_resolution_uses_closed_path_without_running_checks(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    environment["PATH"] = str(source.parent / "unprovisioned-tools")
    task = replace(fixture_task(), verification=(VerificationCommand(("make", "check"), 60),))
    result = preflight_task(environment, task=task, source_repository=source, directory=state)
    assert PreflightReason.TASK in {blocker.reason for blocker in result.blockers}
    assert not state.exists()


@pytest.mark.parametrize("invalid", ["overlap", "relative", "symlink", "non-native"])
def test_state_path_suitability_fails_closed_without_state_creation(
    ready_inputs: tuple[dict[str, str], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    environment, source, state = ready_inputs
    if invalid == "overlap":
        state = source / "state"
    elif invalid == "relative":
        state = Path("relative-state")
    elif invalid == "symlink":
        state.symlink_to(source, target_is_directory=True)
    else:

        def non_native(_directory: Path, *, database_path: Path | None = None) -> str:
            raise RuntimeError("synthetic non-native filesystem")

        monkeypatch.setattr("creatidy_kernel.adapters.task_execution.validate_local_storage", non_native)
    result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert PreflightReason.STATE in {blocker.reason for blocker in result.blockers}
    assert not (state / "kernel.sqlite3").exists()


def test_config_cannot_redirect_task_authority(ready_inputs: tuple[dict[str, str], Path, Path]) -> None:
    environment, source, state = ready_inputs
    environment["CREATIDY_KERNEL_FORGE_REMOTE"] = "https://forge.invalid/other/repo.git"
    result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert result.blockers[0].reason is PreflightReason.CONFIGURATION
    assert not state.exists()


@pytest.mark.parametrize("reason", [PreflightReason.CODEX, PreflightReason.ROUTER, PreflightReason.FORGE])
def test_external_failures_are_typed_and_redact_exception_bodies(
    ready_inputs: tuple[dict[str, str], Path, Path],
    reason: PreflightReason,
) -> None:
    environment, source, state = ready_inputs
    target = {
        PreflightReason.CODEX: "creatidy_kernel.adapters.task_execution._codex_schema",
        PreflightReason.ROUTER: "creatidy_kernel.adapters.scarcity_router.ScarcityRouterAllocator._exchange",
        PreflightReason.FORGE: "creatidy_kernel.adapters.forgejo_transport.HTTPSForgejoTransport.request",
    }[reason]
    with patch(target, side_effect=OSError("synthetic-secret from remote error")):
        result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert reason in {blocker.reason for blocker in result.blockers}
    assert "synthetic-secret" not in json.dumps(result.payload())
    assert not state.exists()


def test_router_mismatch_cannot_expand_runtime_authority(
    ready_inputs: tuple[dict[str, str], Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, source, state = ready_inputs

    def different_effort(_self: ScarcityRouterAllocator, _requirement: dict[str, object]) -> bytes:
        return selection_document("zai", "glm-5.3", "high")

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", different_effort)
    result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert PreflightReason.ROUTER in {blocker.reason for blocker in result.blockers}


@pytest.mark.parametrize("failure", ["wrong-repository", "wrong-base", "wrong-issue", "closed-issue", "inaccessible"])
def test_forge_exact_read_identity_and_open_issue_readiness(
    ready_inputs: tuple[dict[str, str], Path, Path],
    failure: str,
) -> None:
    environment, source, state = ready_inputs

    def wrong_read(
        _self: HTTPSForgejoTransport,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> tuple[int, object]:
        assert method == "GET" and body is None
        if failure == "inaccessible":
            return 403, {"message": "synthetic-secret inaccessible"}
        if path.endswith("/branches/develop"):
            return 200, {"name": "other" if failure == "wrong-base" else "develop", "commit": {"id": "1" * 40}}
        if path.endswith("/issues/143"):
            return 200, {
                "number": 144 if failure == "wrong-issue" else 143,
                "html_url": fixture_task().repository_url + "/issues/143",
                "state": "closed" if failure == "closed-issue" else "open",
                "pull_request": None,
            }
        return 200, {"full_name": "Other/repo" if failure == "wrong-repository" else "BioMedical-IT/scarcity-router"}

    with patch.object(HTTPSForgejoTransport, "request", wrong_read):
        result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert PreflightReason.FORGE in {blocker.reason for blocker in result.blockers}
    assert "synthetic-secret" not in json.dumps(result.payload())


def test_preflight_pin_and_default_are_same_task_and_exact_frozen_subject(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    default = fixture_task()
    initial = preflight_task(environment, task=default, source_repository=source, directory=state)
    assert initial.ready and initial.exact_base_sha is not None
    pinned = replace(default, expected_base_sha=initial.exact_base_sha)
    assert pinned.digest == default.digest
    with pytest.raises(TaskInterrupted):
        run(state, source, FixtureConnection(deflake_edit), task=pinned, fault="workspace-prepared")
    recovered = preflight_task(environment, task=default, source_repository=source, directory=state)
    assert recovered.ready and recovered.exact_base_sha == initial.exact_base_sha
    assert recovered.task_digest == initial.task_digest


def test_original_pin_survives_task_admission_crash_without_a_frozen_base(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    base = reference_git(source, "rev-parse", "HEAD")
    pinned = fixture_task(expected_base_sha=base)
    with pytest.raises(TaskInterrupted):
        run(state, source, FixtureConnection(deflake_edit), task=pinned, fault="task-admitted")
    (source / "README.md").write_text("move after pinned admission\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "move after admission")
    readiness = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert readiness.ready and readiness.exact_base_sha == base
    with pytest.raises(TaskInterrupted):
        run(state, source, FixtureConnection(deflake_edit), task=fixture_task(), fault="workspace-prepared")
    recovered = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert recovered.ready and recovered.exact_base_sha == base
    wrong_pin = fixture_task(expected_base_sha=reference_git(source, "rev-parse", "HEAD"))
    assert not preflight_task(environment, task=wrong_pin, source_repository=source, directory=state).ready


def test_preflight_recovers_exact_durable_base_without_writable_store(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    task = fixture_task()
    base = reference_git(source, "rev-parse", "HEAD")
    with pytest.raises(TaskInterrupted):
        run(state, source, FixtureConnection(deflake_edit), task=task, fault="workspace-prepared")
    (source / "README.md").write_text("branch moves independently\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "branch moves")
    state_files = {path.name: path.read_bytes() for path in state.iterdir() if path.is_file()}
    with patch("creatidy_kernel.adapters.task_execution.SQLiteProgramStore", side_effect=AssertionError("read only")):
        result = preflight_task(environment, task=task, source_repository=source, directory=state)
    assert result.ready and result.exact_base_sha == base
    assert result.base_ref == "durable:refs/heads/develop"
    assert {path.name: path.read_bytes() for path in state.iterdir() if path.is_file()} == state_files
    pinned_other = replace(task, expected_base_sha=reference_git(source, "rev-parse", "HEAD"))
    result = preflight_task(environment, task=pinned_other, source_repository=source, directory=state)
    assert PreflightReason.SOURCE in {blocker.reason for blocker in result.blockers}


def test_preflight_refuses_unreconciled_state_without_reading_or_repairing_it(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    with pytest.raises(TaskInterrupted):
        run(state, source, FixtureConnection(deflake_edit), fault="task-admitted")
    sidecar = state / "kernel.sqlite3-wal"
    sidecar.write_bytes(b"synthetic unreconciled sidecar")
    with patch("creatidy_kernel.adapters.task_execution.sqlite3.connect", side_effect=AssertionError("no state reads")):
        result = preflight_task(environment, task=fixture_task(), source_repository=source, directory=state)
    assert PreflightReason.STATE in {blocker.reason for blocker in result.blockers}
    assert sidecar.read_bytes() == b"synthetic unreconciled sidecar"


def test_run_rejects_source_state_overlap_before_any_source_write(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    _environment, source, _state = ready_inputs
    with pytest.raises(ValueError, match="separate trees"):
        run(source / "state", source, FixtureConnection(deflake_edit))
    assert not (source / "state").exists()
    assert reference_git(source, "status", "--porcelain") == ""


def test_unrelated_existing_state_is_not_migrated_or_reused(
    ready_inputs: tuple[dict[str, str], Path, Path],
) -> None:
    environment, source, state = ready_inputs
    state.mkdir()
    with SQLiteProgramStore(state / "kernel.sqlite3"):
        pass
    connection = FixtureConnection(deflake_edit)
    with pytest.raises(ValueError, match="not this neutral task"):
        run(state, source, connection)
    assert connection.starts == 0
    assert not preflight_task(environment, task=fixture_task(), source_repository=source, directory=state).ready


def test_cli_preflight_has_human_and_json_nonzero_blockers(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.cli.os.environ", {})
    assert main(["task", "preflight", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED" and payload["blockers"][0]["reason"] == "configuration_invalid"
    assert main(["task", "preflight"]) == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_cli_preflight_ready_uses_configured_paths_without_approval(
    ready_inputs: tuple[dict[str, str], Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, source, state = ready_inputs
    environment["CREATIDY_KERNEL_SOURCE_REPOSITORY"] = str(source)
    environment["CREATIDY_KERNEL_STATE_DIRECTORY"] = str(state)
    monkeypatch.setattr("creatidy_kernel.adapters.cli.os.environ", environment)

    def task(expected_base_sha: str | None = None):
        return fixture_task(expected_base_sha=expected_base_sha)

    monkeypatch.setattr("creatidy_kernel.adapters.cli.TASKS", {"143": task})
    assert main(["task", "preflight", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "READY_FOR_LIVE_TASK"
    assert main(["task", "preflight"]) == 0
    assert "READY_FOR_LIVE_TASK" in capsys.readouterr().out
    assert not state.exists()


def test_legacy_command_is_not_a_compatibility_alias() -> None:
    with pytest.raises(SystemExit) as error:
        main(["dogfood", "run"])
    assert error.value.code == 2
