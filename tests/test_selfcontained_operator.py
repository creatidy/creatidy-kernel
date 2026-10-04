# SPDX-License-Identifier: Apache-2.0
"""Self-contained public operator path: no private repository and no configured checkout."""

import json
import os
import sys
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import cast

import pytest
from test_task_execution import (
    fixture_task,
    live_environment,
    make_source,
    selection_document,
    write_fake_codex,
)

from creatidy_kernel.adapters import source_cache
from creatidy_kernel.adapters.cli import main
from creatidy_kernel.adapters.forgejo_transport import HTTPSForgejoTransport
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator
from creatidy_kernel.adapters.task_execution import (
    PreflightReason,
    PreflightResult,
    TaskRuntimeConfig,
    environment_with_discovered_codex,
    preflight_task,
    resolve_source,
)
from creatidy_kernel.adapters.task_execution import (
    acquire_source as _real_acquire_source,
)

pytest_plugins = ["test_sqlite_store"]

CANONICAL = "https://forge.invalid/BioMedical-IT/scarcity-router"


@pytest.fixture
def readiness_stub(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], dict[Path, str]]:
    """Router and Forge stubs so readiness is proven against local fixtures only."""
    environment = live_environment("zai/glm-5.3/low")
    environment["CREATIDY_KERNEL_ROUTER_KEY"] = "synthetic-router-secret"
    environment["HOME"] = str(sqlite_tmp_path / "synthetic-codex-home")
    Path(environment["HOME"]).mkdir()
    environment["PATH"] = os.defpath
    environment["CREATIDY_KERNEL_CODEX_BIN"] = str(provision_codex(sqlite_tmp_path / "codex-tools", "1.2.3"))
    environment["CREATIDY_KERNEL_CODEX_VERSION"] = "1.2.3"
    base_by_remote: dict[Path, str] = {}

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
        base = next(iter(base_by_remote.values()))
        if path == prefix:
            return 200, {"full_name": "BioMedical-IT/scarcity-router"}
        if path == prefix + "/branches/develop":
            return 200, {"name": "develop", "commit": {"id": base}}
        assert path == prefix + "/issues/143"
        return 200, {"number": 143, "html_url": CANONICAL + "/issues/143", "state": "open"}

    def acquire(url: str, **kwargs: object) -> Path:
        assert url == CANONICAL, "acquisition always targets the TaskSpec canonical repository"
        remote = next(iter(base_by_remote))
        return _real_acquire_source(
            url,
            cache_root=cache_root(sqlite_tmp_path),
            clone_from=str(remote),
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", router)
    monkeypatch.setattr(HTTPSForgejoTransport, "request", forge)
    monkeypatch.setattr("creatidy_kernel.adapters.task_execution.acquire_source", acquire)
    return environment, base_by_remote  # type: ignore[return-value]


def cache_root(root: Path) -> Path:
    return root / "xdg-cache"


def provision_codex(directory: Path, version: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return write_fake_codex(directory, version)


def provision_path_codex(directory: Path, version: str) -> Path:
    """A fake codex executable discoverable under its own name on PATH."""
    fake = provision_codex(directory, version)
    on_path = directory / "codex"
    on_path.write_text(fake.read_text())
    on_path.chmod(0o700)
    return on_path


def install_remote(remote_location: Path, bases: dict[Path, str]) -> Path:
    remote = make_source(remote_location)
    reference_git(remote, "remote", "add", "origin", CANONICAL + ".git")
    bases[remote] = reference_git(remote, "rev-parse", "refs/heads/develop")
    return remote


@pytest.mark.parametrize(
    ("remote_location", "state_location"),
    [
        ("one/deep/remote-nest/checkout", "elsewhere/state-a"),
        ("B/completely/different/much/deeper/place/repo", "z/other/state-b"),
    ],
)
def test_preflight_requires_no_checkout_configuration_across_unrelated_layouts(
    sqlite_tmp_path: Path,
    readiness_stub: tuple[dict[str, str], dict[Path, str]],
    remote_location: str,
    state_location: str,
) -> None:
    environment, bases = readiness_stub
    remote = install_remote(sqlite_tmp_path / remote_location, bases)
    state = sqlite_tmp_path / state_location

    result = preflight_task(environment, task=fixture_task(), directory=state)

    assert result.ready and result.blockers == ()
    assert result.exact_base_sha == bases[remote]
    evidence = cast("dict[str, object]", result.evidence["source"])
    assert evidence["acquired"] is True
    assert evidence["directory"] == str(cache_root(sqlite_tmp_path) / "BioMedical-IT" / "scarcity-router")
    assert evidence["read_only"] is True
    assert not state.exists()


def test_preflight_acquisition_ignores_a_canonical_sibling_checkout(
    sqlite_tmp_path: Path,
    readiness_stub: tuple[dict[str, str], dict[Path, str]],
) -> None:
    environment, bases = readiness_stub
    remote = install_remote(sqlite_tmp_path / "remote-anywhere" / "checkout", bases)
    sibling = make_source(sqlite_tmp_path / "neighbor" / "scarcity-router")
    reference_git(sibling, "remote", "add", "origin", CANONICAL)
    (sibling / "operator-file.txt").write_text("operator-owned\n")
    sibling_head = reference_git(sibling, "rev-parse", "HEAD")

    result = preflight_task(environment, task=fixture_task(), directory=sqlite_tmp_path / "state")

    assert result.ready and result.exact_base_sha == bases[remote]
    evidence = cast("dict[str, object]", result.evidence["source"])
    assert evidence["directory"] == str(cache_root(sqlite_tmp_path) / "BioMedical-IT" / "scarcity-router")
    assert reference_git(sibling, "rev-parse", "HEAD") == sibling_head
    assert (sibling / "operator-file.txt").read_text() == "operator-owned\n"


def test_preflight_codex_comes_from_path_discovery_without_manual_fields(
    sqlite_tmp_path: Path,
    readiness_stub: tuple[dict[str, str], dict[Path, str]],
) -> None:
    environment, bases = readiness_stub
    remote = install_remote(sqlite_tmp_path / "one" / "checkout", bases)
    on_path = provision_path_codex(sqlite_tmp_path / "tools", "1.2.3")
    environment["PATH"] = os.pathsep.join((str(on_path.parent), os.defpath))
    del environment["CREATIDY_KERNEL_CODEX_BIN"]
    del environment["CREATIDY_KERNEL_CODEX_VERSION"]

    result = preflight_task(environment, task=fixture_task(), directory=sqlite_tmp_path / "state")

    assert result.ready and result.exact_base_sha == bases[remote]
    codex = cast("dict[str, object]", result.evidence["codex"])
    assert codex["version"] == "1.2.3" and codex["initialized"] is True
    enriched, discovery_failure = environment_with_discovered_codex(environment)
    assert discovery_failure is None
    parsed = TaskRuntimeConfig.parse(enriched)
    assert str(parsed.codex_binary) == str(on_path.resolve())
    assert parsed.codex_version == "1.2.3"


@pytest.mark.parametrize(
    ("behavior", "expected_category"),
    [
        ("absent", "not_found"),
        ("broken", "probe_failed"),
        ("development-version", "unsupported_version"),
    ],
)
def test_preflight_reports_closed_discovery_categories(
    sqlite_tmp_path: Path,
    readiness_stub: tuple[dict[str, str], dict[Path, str]],
    behavior: str,
    expected_category: str,
) -> None:
    environment, _bases = readiness_stub
    tools = sqlite_tmp_path / "tools"
    if behavior == "absent":
        tools.mkdir()
    elif behavior == "broken":
        tools.mkdir()
        binary = tools / "codex"
        binary.write_text(f"#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ['--version']\nsys.exit(4)\n")
        binary.chmod(0o700)
    else:
        provision_path_codex(tools, "1.2")
    environment["PATH"] = os.pathsep.join((str(tools), os.defpath))
    del environment["CREATIDY_KERNEL_CODEX_BIN"]
    del environment["CREATIDY_KERNEL_CODEX_VERSION"]

    result = preflight_task(environment, task=fixture_task())

    assert not result.ready and result.exact_base_sha is None
    codex = next(blocker for blocker in result.blockers if blocker.reason is PreflightReason.CODEX)
    assert codex.category == expected_category
    assert "CREATIDY_KERNEL_CODEX_BIN" in codex.fields
    payload = result.payload()
    assert payload["status"] == "BLOCKED"
    assert "synthetic-router-secret" not in json.dumps(payload)


def _router_readiness(sqlite_tmp_path: Path, origin: str) -> PreflightResult:
    environment = live_environment("zai/glm-5.3/low")
    environment["CREATIDY_KERNEL_ROUTER_URL"] = origin
    environment["HOME"] = str(sqlite_tmp_path / "synthetic-codex-home")
    Path(environment["HOME"]).mkdir()
    environment["PATH"] = os.defpath
    environment["CREATIDY_KERNEL_CODEX_BIN"] = str(provision_codex(sqlite_tmp_path / "codex-tools", "1.2.3"))
    environment["CREATIDY_KERNEL_CODEX_VERSION"] = "1.2.3"
    return preflight_task(environment, task=fixture_task())


@pytest.mark.parametrize(
    ("mode", "expected_category"),
    [
        ("closed-port", "endpoint_unreachable"),
        ("http-500", "http_rejected"),
        ("garbage", "invalid_response"),
        ("no-eligible", "no_eligible_selection"),
        ("other-model", "selection_incompatible"),
    ],
)
def test_preflight_router_blockers_carry_safe_categories(
    sqlite_tmp_path: Path,
    mode: str,
    expected_category: str,
) -> None:
    """Real loopback HTTP outcomes classify into the closed safe Router vocabulary."""
    no_eligible = {
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
            "selected": None,
            "alternatives": [],
            "excluded": [],
            "closest_candidates": [],
            "recoverable_candidates": [],
            "degraded": False,
            "reason_codes": ["no_eligible_candidate"],
            "preference_order": [],
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(500 if mode == "http-500" else 200)
            self.send_header("Content-Type", "application/json")
            if mode == "http-500":
                body = b'{"message": "synthetic-secret refusal"}'
            elif mode == "garbage":
                body = b"<not json>"
            elif mode == "other-model":
                body = selection_document("zai", "other-model", "low")
            else:
                body = json.dumps(no_eligible).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{httpd.server_port}"
    live = mode != "closed-port"
    try:
        if not live:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=2)
        readiness = _router_readiness(sqlite_tmp_path, origin)
    finally:
        if live:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=2)

    assert not readiness.ready
    router = next(blocker for blocker in readiness.blockers if blocker.reason is PreflightReason.ROUTER)
    assert router.category == expected_category
    assert "synthetic-secret" not in json.dumps(readiness.payload())


def test_resolve_source_prefers_override_then_config_then_canonical_cache(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = live_environment("zai/glm-5.3/low")
    environment["CREATIDY_KERNEL_SOURCE_REPOSITORY"] = str(sqlite_tmp_path / "configured")
    config = TaskRuntimeConfig.parse(environment)
    override = sqlite_tmp_path / "override"
    assert resolve_source(config, fixture_task(), override, environment=environment) == override
    assert resolve_source(config, fixture_task(), None, environment=environment) == sqlite_tmp_path / "configured"

    plain = TaskRuntimeConfig.parse(live_environment("zai/glm-5.3/low"))
    acquired: list[str] = []

    def acquire(url: str, **kwargs: object) -> Path:
        acquired.append(url)
        return sqlite_tmp_path / "cache-result"

    monkeypatch.setattr("creatidy_kernel.adapters.task_execution.acquire_source", acquire)
    resolved = resolve_source(plain, fixture_task(), None, environment=environment)
    assert resolved == sqlite_tmp_path / "cache-result"
    assert acquired == [CANONICAL]


def test_doctor_reports_blocked_categories_and_hints_without_configuration(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.cli.os.environ", {})
    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["readiness"]["status"] == "BLOCKED"
    assert payload["doctor"] == "scarcity-router-143"
    reasons = {blocker["reason"] for blocker in payload["readiness"]["blockers"]}
    assert "configuration_invalid" in reasons and "codex_not_ready" in reasons
    assert main(["doctor"]) == 1
    human = capsys.readouterr().out
    assert "doctor: blocked configuration_invalid" in human
    assert "doctor: blocked codex_not_ready (not_found)" in human
    assert "doctor: BLOCKED" in human
    assert "doctor: hint for codex_not_ready (not_found)" in human
    assert "no inference" in human


def test_doctor_and_preflight_run_without_private_operator_or_checkout_configuration(
    sqlite_tmp_path: Path,
    readiness_stub: tuple[dict[str, str], dict[Path, str]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, bases = readiness_stub
    remote = install_remote(sqlite_tmp_path / "one" / "checkout", bases)
    on_path = provision_path_codex(sqlite_tmp_path / "tools", "1.2.3")
    environment["PATH"] = os.pathsep.join((str(on_path.parent), os.defpath))
    del environment["CREATIDY_KERNEL_CODEX_BIN"]
    del environment["CREATIDY_KERNEL_CODEX_VERSION"]
    environment["XDG_STATE_HOME"] = str(sqlite_tmp_path / "xdg-state")
    monkeypatch.setattr("creatidy_kernel.adapters.cli.os.environ", environment)

    def task(expected_base_sha: str | None = None):
        return fixture_task(expected_base_sha=expected_base_sha)

    monkeypatch.setattr("creatidy_kernel.adapters.cli.TASKS", {"143": task})
    # The fake canonical origin is a local mirror at the transport level; cache
    # reuse must fetch from origin, so serve that fetch from the mirror refspec.
    real_run = source_cache.run_git_bounded
    mirror = str(remote)

    def fetch_from_mirror(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int):
        if argv[1:2] == ["fetch"]:
            return real_run(
                [argv[0], "fetch", "--prune", mirror, "+refs/heads/*:refs/remotes/origin/*"],
                cwd,
                env,
                timeout,
                max_bytes,
            )
        return real_run(argv, cwd, env, timeout, max_bytes)

    monkeypatch.setattr(source_cache, "run_git_bounded", fetch_from_mirror)

    assert main(["doctor", "--task", "143"]) == 0
    human = capsys.readouterr().out
    assert "doctor: READY_FOR_LIVE_TASK" in human
    assert "doctor: source:" in human and "acquired cache" in human
    assert "doctor: codex: 1.2.3" in human

    assert main(["task", "preflight", "--task", "143", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "READY_FOR_LIVE_TASK"
    assert payload["exact_base_sha"] == bases[remote]
    assert payload["inference_performed"] is False and payload["forge_writes_performed"] is False
    evidence = cast("dict[str, object]", payload["evidence"]["source"])
    assert evidence["acquired"] is True
    assert evidence["directory"] == str(cache_root(sqlite_tmp_path) / "BioMedical-IT" / "scarcity-router")
    assert not (sqlite_tmp_path / "xdg-state").exists()


def test_config_validate_and_template_report_names_only(
    sqlite_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("creatidy_kernel.adapters.cli.os.environ", {"PATH": os.defpath, "HOME": str(sqlite_tmp_path)})
    assert main(["config", "template"]) == 0
    template = capsys.readouterr().out
    assert "CREATIDY_KERNEL_ROUTER_URL=REPLACE_ROUTER_ORIGIN" in template

    profile = sqlite_tmp_path / "kernel.env"
    profile.write_text("CREATIDY_KERNEL_UNKNOWN_FIELD=x\n")
    assert main(["config", "validate", "--profile", str(profile), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["unknown_fields"] == ["CREATIDY_KERNEL_UNKNOWN_FIELD"]
    assert "CREATIDY_KERNEL_FORGE_TOKEN" in payload["fields"]

    complete = live_environment("zai/glm-5.3/low")
    complete["CREATIDY_KERNEL_CODEX_BIN"] = str(provision_codex(sqlite_tmp_path / "tools", "1.2.3"))
    complete["CREATIDY_KERNEL_CODEX_VERSION"] = "1.2.3"
    profile.write_text("".join(f"{key}={value}\n" for key, value in complete.items() if value))
    assert main(["config", "validate", "--profile", str(profile), "--json"]) == 0
    valid = json.loads(capsys.readouterr().out)
    assert valid["valid"] is True and valid["fields"] == [] and valid["codex"] == {"status": "configured"}


def test_status_and_export_default_to_the_user_local_state_root(
    sqlite_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {"XDG_STATE_HOME": str(sqlite_tmp_path / "xdg-state"), "PATH": os.defpath}
    monkeypatch.setattr("creatidy_kernel.adapters.cli.os.environ", environment)
    assert main(["task", "status"]) == 1
    assert "task database does not exist" in capsys.readouterr().err
    assert not (sqlite_tmp_path / "xdg-state" / "creatidy-kernel" / "task").exists()
