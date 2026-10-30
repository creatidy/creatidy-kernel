# SPDX-License-Identifier: Apache-2.0
"""Real pinned native Kilo server with an in-process deterministic local provider.

No owner HOME, project config, credential, gateway or model service is used. The
provider simulates responses, not native sessions: session/message identifiers,
permissions, conversation creation and structured output belong to real Kilo.
"""

import json
import os
import re
import socket
import subprocess
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.request import ProxyHandler, build_opener


@contextmanager
def native_kilo(root: Path, binary: Path, *, reject_first: bool = False) -> Generator[tuple[str, list[object]]]:
    calls: list[object] = []

    def texts(value: object) -> list[str]:
        if type(value) is str:
            return [value]
        if type(value) is list:
            return [text for item in cast(list[object], value) for text in texts(item)]
        if type(value) is dict:
            return [text for item in cast(dict[str, object], value).values() for text in texts(item)]
        return []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            if length > 2_000_000:
                self.send_error(413)
                return
            data = cast(dict[str, object], json.loads(self.rfile.read(length)))
            calls.append(data)
            tools = cast(list[dict[str, object]], data.get("tools", []))
            target = next(
                (
                    item
                    for item in tools
                    if cast(dict[str, object], item.get("function", {})).get("name") == "StructuredOutput"
                ),
                None,
            )
            output: dict[str, object] = {"role": "assistant", "content": "Offline native fixture"}
            finish = "stop"
            marker = "no markdown or prose:\n"
            original = next((text for text in texts(data.get("messages")) if marker in text), None)
            if target is None and original is not None:
                received, _ = json.JSONDecoder().raw_decode(original.split(marker, 1)[1].lstrip())
                schema = cast(dict[str, object], received)
                properties = cast(dict[str, object], schema["properties"])
                field = cast(dict[str, object], properties["request_digest"])
                reject = reject_first and len(calls) == 1
                output["content"] = json.dumps(
                    {
                        "request_digest": field["const"],
                        "verdict": "request_changes" if reject else "approve",
                        "findings": [
                            {"fingerprint": "missing-behavior", "reason": "Add the approved edge-case behavior."}
                        ]
                        if reject
                        else [],
                    }
                )
            if target is not None:
                function = cast(dict[str, object], target["function"])
                schema = cast(dict[str, object], function["parameters"])
                properties = cast(dict[str, object], schema["properties"])
                field = cast(dict[str, object], properties["request_digest"])
                request_digest = field.get("const")
                if request_digest is None:
                    serialized = json.dumps(data)
                    match = re.search(r'input_digest[\\"\s:]+([a-f0-9]{64})', serialized)
                    if match is None:
                        self.send_error(422)
                        return
                    request_digest = match.group(1)
                verdict = "approve"
                arguments: dict[str, object] = {"request_digest": request_digest, "verdict": verdict, "findings": []}
                output = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_fixture",
                            "type": "function",
                            "function": {
                                "name": "StructuredOutput",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            if data.get("stream") is True:
                delta = dict(output)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [
                        {"index": 0, **item} for item in cast(list[dict[str, object]], delta["tool_calls"])
                    ]
                chunks: list[dict[str, object]] = [
                    {
                        "id": "fixture-completion",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "review",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    },
                    {
                        "id": "fixture-completion",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "review",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    },
                ]
                raw = b"".join(b"data: " + json.dumps(item).encode() + b"\n\n" for item in chunks) + b"data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                raw = json.dumps(
                    {
                        "id": "fixture-completion",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "review",
                        "choices": [{"index": 0, "message": output, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    }
                ).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    root.mkdir(mode=0o700)
    config = {
        "enabled_providers": ["fixture"],
        "model": "fixture/review",
        "small_model": "fixture/review",
        "share": "disabled",
        "snapshot": False,
        "autoupdate": False,
        "plugin": [],
        "mcp": {},
        "provider": {
            "fixture": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "offline fixture",
                "options": {
                    "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                    "api" + "Key": "fixture",
                },
                "models": {
                    "review": {"name": "review", "limit": {"context": 65536, "output": 8192}, "tool_call": True}
                },
            }
        },
        "agent": {"product-review": {"mode": "primary", "model": "fixture/review", "permission": {"*": "deny"}}},
    }
    environment = {
        "HOME": str(root),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "TMPDIR": str(root),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "KILO_CONFIG_CONTENT": json.dumps(config),
        "KILO_DISABLE_PROJECT_CONFIG": "1",
        "KILO_DISABLE_DEFAULT_PLUGINS": "1",
        "KILO_DISABLE_MODELS_FETCH": "1",
        "KILO_DISABLE_AUTOUPDATE": "1",
        "KILO_DISABLE_LSP_DOWNLOAD": "1",
        "KILO_DISABLE_EXTERNAL_SKILLS": "1",
        "KILO_DISABLE_CLAUDE_CODE": "1",
        "KILO_DISABLE_CHANNEL_DB": "1",
        "KILO_DISABLE_EMBEDDED_WEB_UI": "1",
        "KILO_DISABLE_AUTOCOMPACT": "1",
        "OTEL_SDK_DISABLED": "true",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    log_path = root / "native.log"
    process: subprocess.Popen[bytes] | None = None
    try:
        with log_path.open("wb") as output:
            process = subprocess.Popen(  # noqa: S603 - Explicit pinned fixture binary and closed environment.
                [str(binary), "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )  # noqa: S603 - explicit pinned fixture executable.
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("native fixture exited: " + log_path.read_text()[-6000:])
                try:
                    with opener.open(origin + "/global/health", timeout=1) as response:
                        if json.loads(response.read()).get("healthy") is True:
                            break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("native fixture readiness failed: " + log_path.read_text()[-6000:])
            yield origin, calls
    finally:
        if process is not None and process.poll() is None:
            import signal

            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def review_peer(root: Path, binary: Path | None, *, reject_first: bool = False) -> Generator[tuple[str, list[object]]]:
    """Default protocol peer is synthetic; explicit binary exercises real Kilo."""
    if binary is not None:
        with native_kilo(root, binary, reject_first=reject_first) as peer:
            yield peer
        return
    from urllib.parse import urlsplit

    calls: list[object] = []
    sessions: dict[str, dict[str, object]] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, data: object) -> None:
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/global/health":
                self.reply({"healthy": True, "version": "7.8.25"})
                return
            parts = path.split("/")
            state = sessions[parts[2]]
            self.reply(state["messages"] if path.endswith("/message") else state["metadata"])

        def do_POST(self) -> None:
            body = cast(dict[str, object], json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            path = urlsplit(self.path).path
            if path == "/session":
                key = "ses_synthetic" + str(len(sessions) + 1)
                metadata = {**body, "id": key}
                sessions[key] = {"metadata": metadata, "messages": []}
                self.reply(metadata)
                return
            key = path.split("/")[2]
            calls.append(body)
            text = cast(str, cast(list[dict[str, object]], body["parts"])[0]["text"])
            schema = cast(dict[str, object], json.loads(text.split("no markdown or prose:\n", 1)[1]))
            properties = cast(dict[str, object], schema["properties"])
            request_digest = cast(dict[str, object], properties["request_digest"])["const"]
            reject = reject_first and len(calls) == 1
            result = {
                "info": {
                    "id": "msg_synthetic_result",
                    "sessionID": key,
                    "parentID": "msg_synthetic_prompt",
                    "role": "assistant",
                    "time": {"completed": int(time.time()) * 1000},
                },
                "parts": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "request_digest": request_digest,
                                "verdict": "request_changes" if reject else "approve",
                                "findings": [
                                    {"fingerprint": "edge-case", "reason": "Implement the approved edge case."}
                                ]
                                if reject
                                else [],
                            }
                        ),
                    }
                ],
            }
            sessions[key]["messages"] = [
                {
                    "info": {"id": "msg_synthetic_prompt", "sessionID": key, "role": "user", "agent": body["agent"]},
                    "parts": body["parts"],
                },
                result,
            ]
            self.reply(result)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
