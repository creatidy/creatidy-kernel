# SPDX-License-Identifier: Apache-2.0
"""Synthetic Codex model backend executed INSIDE the disposable boundary (no paid inference).

Implements exactly the OpenAI Responses wire shape the pinned app-server consumes: one
``POST /v1/responses`` SSE stream per model request, with ``function_call`` items answered by
the app-server's native tool execution and a terminal ``message`` item. The scenario is
controller-authored and written into the disposable writable workspace before launch
(the read-only mount covers only this module's source); the mock never invents network
destinations and
binds only the container-internal loopback. Its records (native tool inventory, request
trail, scenario progress) are evidence, written into the disposable workspace.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

WORKSPACE = Path("/workspace")
RECORD_DIR = WORKSPACE / "mock"
INSTRUCTIONS_KEEP = 400  # Codex-authored prompt text is recorded by length plus a head, never in full.


def _load_scenario(path: Path) -> dict[str, Any]:
    scenario = cast(dict[str, Any], json.loads(path.read_text()))
    if not isinstance(scenario.get("steps"), list):
        raise ValueError("scenario must carry a steps list")
    return scenario


def _response_payload(items: list[dict[str, Any]], response_id: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "status": "completed",
        "output": items,
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def _sse_events(items: list[dict[str, Any]], response_id: str) -> list[tuple[str, dict[str, Any]]]:
    response = _response_payload(items, response_id)
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"type": "response.created", "response": response}),
    ]
    for index, item in enumerate(items):
        events.append(
            ("response.output_item.done", {"type": "response.output_item.done", "output_index": index, "item": item})
        )
    events.append(("response.completed", {"type": "response.completed", "response": response}))
    return events


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in (cast(list[object], value)) if isinstance(entry, dict)]


class MockModel:
    """Scenario-driven synthetic model with persisted, bounded request records."""

    def __init__(self, scenario_path: Path) -> None:
        self.scenario = _load_scenario(scenario_path)
        self.index = 0
        self.lock = threading.Lock()
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        self.request_log = RECORD_DIR / "requests-summary.jsonl"
        self.inventory_path = RECORD_DIR / "tool-inventory.json"
        self.first_request_path = RECORD_DIR / "first-request.json"

    def _next_step(self) -> dict[str, Any]:
        with self.lock:
            steps = cast(list[dict[str, Any]], self.scenario["steps"])
            step: dict[str, Any] = (
                steps[self.index] if self.index < len(steps) else {"kind": "final", "text": "scenario exhausted"}
            )
            self.index += 1
            state = {"index": self.index, "last_step_kind": step.get("kind")}
            (RECORD_DIR / "state.json").write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
            return step

    def _record_tools(self, body: dict[str, Any]) -> None:
        if self.inventory_path.exists():
            return
        inventory = [{"type": tool.get("type"), "name": tool.get("name")} for tool in _dicts(body.get("tools"))]
        self.inventory_path.write_text(json.dumps(inventory, indent=1, sort_keys=True) + "\n")

    def _record_request(self, body: dict[str, Any]) -> None:
        tools = _dicts(body.get("tools"))
        inputs = _dicts(body.get("input"))
        record = {
            "model": body.get("model"),
            "stream": body.get("stream"),
            "store": body.get("store"),
            "tool_names": [tool.get("name") for tool in tools],
            "input_item_types": [item.get("type") for item in inputs],
            "has_function_call_output": any(item.get("type") == "function_call_output" for item in inputs),
            "instructions_chars": len(str(body.get("instructions", "") or "")),
        }
        with self.request_log.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        if not self.first_request_path.exists():
            trimmed = dict(body)
            trimmed["instructions"] = str(body.get("instructions", "") or "")[:INSTRUCTIONS_KEEP]
            self.first_request_path.write_text(json.dumps(trimmed, indent=1, sort_keys=True) + "\n")

    def items_for(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        self._record_tools(body)
        self._record_request(body)
        step = self._next_step()
        if step.get("kind") == "exec":
            arguments = {
                "cmd": str(step["cmd"]),
                "login": False,
                "max_output_tokens": 4000,
            }
            return [
                {
                    "type": "function_call",
                    "id": f"fc_{self.index}",
                    "call_id": f"call_{self.index}",
                    "name": "exec_command",
                    "arguments": json.dumps(arguments),
                }
            ]
        return [
            {
                "type": "message",
                "id": f"msg_{self.index}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": str(step.get("text", "done"))}],
            }
        ]


class _ModelServer(ThreadingHTTPServer):
    """Loopback-only server carrying its scenario-driven model as typed server state."""

    def __init__(self, model: MockModel) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.model = model


class _Handler(BaseHTTPRequestHandler):
    server_version = "synthetic-responses/1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server signature.
        return

    def do_POST(self) -> None:  # noqa: N802 - http.server interface name.
        if not self.path.rstrip("/").endswith("/responses"):
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        body = cast(dict[str, Any], json.loads(raw))
        server = cast(_ModelServer, self.server)
        items = server.model.items_for(body)
        payload = "".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n"
            for name, data in _sse_events(items, f"resp_{server.model.index}")
        )
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload.encode())


def make_server(scenario_path: Path) -> _ModelServer:
    return _ModelServer(MockModel(scenario_path))


def main() -> int:
    scenario_path = Path(sys.argv[1])
    port_file = Path(sys.argv[2])
    server = make_server(scenario_path)
    port = server.server_address[1]
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(f"{port}\n")
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    (RECORD_DIR / "server.json").write_text(
        json.dumps({"bound": "127.0.0.1", "port": port}, indent=1, sort_keys=True) + "\n"
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
