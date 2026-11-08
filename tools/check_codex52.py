# SPDX-License-Identifier: Apache-2.0
"""Opt-in #52 native negative receipt; no inference, gateway or product adapter.

Runs the installed Codex against an actual pinned Router parser on loopback.
Only synthetic homes/workspaces are used. Source identity is not binary attestation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.codex_stdio import CodexStdio

CODEX_VERSION = "0.159.3"
CODEX_REVISION = "01fc69f4026735edfdf6789820549727a4867b11"  # pragma: allowlist secret
CODEX_SHA256 = "8bf204b36a2f6dd0dab73aa2f639892e67ef9ac8befccb4a05b1496ebf25c479"  # pragma: allowlist secret
ROUTER_REVISION = "925ec06681c7fa085edd5015f57663e85d735768"  # pragma: allowlist secret
PIN = "sr-pin:synthetic-source/openai/synthetic-model/opaque-not-effort~high@synthetic-decision"

# This process imports the real public producer, never a reimplemented allowlist.
PARSER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from scarcity_router.executable_contract import ExecutableRequirements
from scarcity_router.gateway_contracts import GatewayError
from scarcity_router.gateway_responses import parse_responses_request
body = json.load(sys.stdin)
try:
    parsed = parse_responses_request(body)
    if 'execution_requirements' in body:
        context = ExecutableRequirements.from_dict(body['execution_requirements'])
        assert context.to_dict() == body['execution_requirements']
except GatewayError as error:
    print(json.dumps({'status': 'refused', 'code': error.code, 'param': error.param}))
else:
    print(json.dumps({'status': 'parsed'}))
"""


def requirements() -> dict[str, object]:
    """Synthetic producer-shaped demands, not a real selection or owner grant."""
    return {
        "schema_version": 2,
        "requirement": {"task_level": "L1", "capability_minima": {}, "hard_constraints": {}},
        "binding": {
            "pinned_target": {
                "resource_id": "synthetic-source",
                "model": {"provider": "openai", "model": "synthetic-model", "variant": "opaque-not-effort"},
                "decision_id": "synthetic-decision",
                "reasoning_effort": "high",
            },
        },
        "profile_policy_version": None,
        "profile_expansion": None,
        "reasoning_effort": "high",
    }


def control() -> dict[str, object]:
    return {
        "model": PIN,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "synthetic control"}]}],
        "store": False,
        "stream": True,
        "reasoning": {"effort": "high"},
        "execution_requirements": requirements(),
    }


def environment(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "CODEX_HOME": str(home / "codex"),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def verify_source(source: Path, revision: str, env: dict[str, str]) -> None:
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], env=env, timeout=30, text=True)
    status = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"], env=env, timeout=30
    )
    if head.strip() != revision or status:
        raise ValueError("proof requires unchanged exact public source")


def parse(router: Path, body: dict[str, object], env: dict[str, str]) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-I", "-c", PARSER, str(router)],
        input=json.dumps(body, allow_nan=False),
        env=env,
        timeout=15,
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, object], json.loads(result.stdout))


def parser_matrix(router: Path, env: dict[str, str]) -> dict[str, object]:
    """Independent parser controls, never a rewrite of native history for dispatch."""
    results: dict[str, object] = {}
    function = {"type": "function", "name": "synthetic", "parameters": {"type": "object"}, "strict": False}
    history = [
        {"role": "user", "content": "synthetic control"},
        {"type": "function_call", "call_id": "call-1", "name": "synthetic", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-1", "output": "synthetic result"},
    ]
    for name, body in (
        ("valid_context_stream", control()),
        ("valid_function_history", {**control(), "input": history, "tools": [function]}),
    ):
        result = parse(router, body, env)
        if result != {"status": "parsed"}:
            raise ValueError(f"producer positive control failed: {name}")
        results[name] = result
    for name, change in (
        ("native_instructions", {"instructions": "synthetic instructions"}),
        ("native_include", {"include": ["reasoning.encrypted_content"]}),
        ("native_cache_key", {"prompt_cache_key": "synthetic"}),
        ("native_client_metadata", {"client_metadata": {"session_id": "synthetic"}}),
        (
            "multipart",
            {
                "input": [
                    {
                        "role": "developer",
                        "content": [
                            {"type": "input_text", "text": "synthetic first"},
                            {"type": "input_text", "text": "synthetic second"},
                        ],
                    }
                ]
            },
        ),
        ("reasoning_summary", {"reasoning": {"effort": "high", "summary": "none"}}),
        ("opaque_variant_as_effort", {"reasoning": {"effort": "opaque-not-effort"}}),
        ("stored_continuation", {"previous_response_id": "synthetic"}),
        ("compaction_item", {"input": [{"type": "compaction", "encrypted_content": "synthetic"}]}),
        ("hosted_tool", {"tools": [{"type": "web_search"}]}),
        ("orphan_result", {"input": [{"type": "function_call_output", "call_id": "lost", "output": "synthetic"}]}),
        (
            "missing_effort_null",
            {
                "execution_requirements": {
                    key: value for key, value in requirements().items() if key != "reasoning_effort"
                }
            },
        ),
    ):
        result = parse(router, {**control(), **change}, env)
        if result.get("status") != "refused":
            raise ValueError(f"producer negative control changed: {name}")
        results[name] = result
    return results


def probe(binary: Path, router: Path, home: Path, receipts: Path) -> dict[str, object]:
    env = environment(home)
    (home / "codex").mkdir()
    workspace = home / "workspace"
    workspace.mkdir()
    captures: list[dict[str, object]] = []
    failures: list[Exception] = []
    received = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            try:
                self.connection.settimeout(5)
                count = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1/responses" or not 0 < count <= 1_000_000:
                    raise ValueError("unexpected native request path or size")
                body = cast(dict[str, object], json.loads(self.rfile.read(count)))
                outcome = parse(router, body, env)
                captures.append({"body": body, "outcome": outcome})
                if outcome != {"status": "refused", "code": "unsupported_parameter", "param": "request"}:
                    raise ValueError("native compatibility changed; reassess rather than claim negative")
                payload = json.dumps({"error": {"type": "invalid_request_error", **outcome}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("x-should-retry", "false")
                self.end_headers()
                self.wfile.write(payload)
            except Exception as error:
                failures.append(error)
            finally:
                received.set()

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        # Select the synthetic provider before initialization, not only at thread/start.
        (home / "codex/config.toml").write_text(
            f'model_provider = "router"\nmodel = "{PIN}"\n'
            "[analytics]\nenabled = false\n[feedback]\nenabled = false\n"
            '[model_providers.router]\nname = "offline-router"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api = "responses"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
            "supports_websockets = false\n"
        )
        server.timeout = 10
        server_thread = threading.Thread(target=server.handle_request)
        server_thread.start()
        try:
            with closing(CodexStdio((str(binary), "app-server"), CODEX_VERSION, timeout=15, environment=env)) as native:
                started = native.request(
                    "thread/start",
                    {
                        "model": PIN,
                        "modelProvider": "router",
                        "cwd": str(workspace),
                        "sandbox": "workspace-write",
                        "approvalPolicy": "never",
                        "config": {
                            "model_providers.router": {
                                "name": "offline-router",
                                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                "wire_api": "responses",
                                "request_max_retries": 0,
                                "stream_max_retries": 0,
                                "supports_websockets": False,
                                "extra_body": {"execution_requirements": requirements()},
                            },
                            "execution_requirements": requirements(),
                            "model_reasoning_effort": "high",
                            "model_reasoning_summary": "none",
                            "analytics.enabled": False,
                            "features.multi_agent": False,
                            "features.memories": False,
                        },
                    },
                )
                if started["model"] != PIN or started["reasoningEffort"] != "high":
                    raise ValueError("native configured identity changed")
                thread_id = cast(dict[str, object], started["thread"])["id"]
                _ = native.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": "Synthetic offline probe."}],
                        "effort": "high",
                    },
                )
                if not received.wait(15):
                    raise TimeoutError("native request not observed")
        finally:
            server_thread.join(30)
        if server_thread.is_alive() or failures:
            raise RuntimeError("offline parser probe failed")
    if len(captures) != 1:
        raise ValueError("expected one initial refused native request")
    body = cast(dict[str, object], captures[0]["body"])
    if body.get("model") != PIN or body.get("reasoning") != {"effort": "high"}:
        raise ValueError("native pin/effort changed")
    if "execution_requirements" in body or "instructions" not in body or "include" not in body:
        raise ValueError("native envelope changed; reassess compatibility")
    (receipts / "native-request.json").write_text(json.dumps(body, indent=2) + "\n")
    return {
        "native_request_count": len(captures),
        "pin_preserved": True,
        "explicit_effort_preserved": True,
        "execution_requirements_preserved": False,
        "producer_refusal": captures[0]["outcome"],
        "native_owned_group_settled": native.owned_group_settled,
        "identity": {
            "requested_pin": PIN,
            "native_resolved_model": started["model"],
            "native_dispatched_pin": body["model"],
            "router_dispatched_target": None,
            "independently_observed_model": None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--codex-source", type=Path, required=True)
    parser.add_argument("--router-source", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    args = parser.parse_args()
    binary = cast(Path, args.codex_bin).resolve()
    codex = cast(Path, args.codex_source).resolve()
    router = cast(Path, args.router_source).resolve()
    receipts = cast(Path, args.receipts).resolve()
    if hashlib.sha256(binary.read_bytes()).hexdigest() != CODEX_SHA256:
        raise ValueError("unexpected Codex binary; a version string is not enough")
    receipts.mkdir()  # Never overwrite prior receipts.
    with tempfile.TemporaryDirectory(prefix="synthetic-", dir=receipts) as temporary:
        home = Path(temporary)
        env = environment(home)
        verify_source(codex, CODEX_REVISION, env)
        verify_source(router, ROUTER_REVISION, env)
        matrix = parser_matrix(router, env)
        report = probe(binary, router, home, receipts)
        report["parser_controls"] = matrix
        verify_source(codex, CODEX_REVISION, env)
        verify_source(router, ROUTER_REVISION, env)
    report.update(
        {
            "feasibility": "NOT_ESTABLISHED",
            "codex_version": CODEX_VERSION,
            "codex_sha256": CODEX_SHA256,
            "codex_source_revision": CODEX_REVISION,
            "binary_source_attested": False,
            "router_revision": ROUTER_REVISION,
            "python_version": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "responses_surface_version": 1,
            "live_inference": False,
            "admission_proven": False,
            "usage": None,
            "unproved": ["tools/continuation", "helpers/review/compaction", "ambiguous retry", "STOP/recovery"],
        }
    )
    (receipts / "report.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
