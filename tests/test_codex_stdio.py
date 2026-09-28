# SPDX-License-Identifier: Apache-2.0
"""Synthetic app-server fixtures; never starts the real Codex executable."""

import json
import sys
from pathlib import Path

import pytest

from creatidy_kernel.adapters.codex_stdio import CodexRPCError, CodexStdio

FAKE = f"""#!{sys.executable}
import json
import sys
import time

if sys.argv[1:] == ['--version']:
    print('codex-cli 0.99.1')
    sys.exit(0)
assert sys.argv[1:] == ['app-server']
first = json.loads(sys.stdin.readline())
assert first['method'] == 'initialize' and first['id'] == 1
assert first['params']['clientInfo'] == {{'name': 'creatidy_kernel', 'title': 'Creatidy Kernel', 'version': '0.0.1'}}
print(json.dumps({{'id': 1, 'result': {{'userAgent': 'fake'}}}}), flush=True)
assert json.loads(sys.stdin.readline()) == {{'method': 'initialized', 'params': {{}}}}
for line in sys.stdin:
    message = json.loads(line)
    if message['method'] == 'timeout':
        time.sleep(4)
    elif message['method'] == 'too/large':
        print('x' * 2000, flush=True)
    elif message['method'] == 'malformed':
        print('not-json', flush=True)
    elif message['method'] == 'wrong/id':
        print(json.dumps({{'id': message['id'] + 1, 'result': {{}}}}), flush=True)
    elif message['method'] == 'server/request':
        print(json.dumps({{'id': 999, 'method': 'item/commandExecution/requestApproval', 'params': {{}}}}), flush=True)
    elif message['method'] == 'rpc/error':
        print(json.dumps({{'id': message['id'], 'error': {{'code': -32602, 'message': 'bad params'}}}}), flush=True)
    elif message['method'] == 'post/response':
        print(json.dumps({{'id': message['id'], 'result': {{}}}}))
        print(json.dumps({{'method': 'turn/completed', 'params': {{'turnId': 't'}}}}), flush=True)
    else:
        print(json.dumps({{'method': 'turn/started', 'params': {{'turnId': 't'}}}}), flush=True)
        print(json.dumps({{'id': message['id'], 'result': {{'ok': message['params']}}}}), flush=True)
"""


@pytest.fixture
def command(tmp_path: Path) -> tuple[str, ...]:
    binary = tmp_path / "fake-codex"
    binary.write_text(FAKE, encoding="utf-8")
    binary.chmod(0o700)
    return (str(binary), "app-server")


def test_handshake_correlation_and_notifications(command: tuple[str, ...]) -> None:
    transport = CodexStdio(command, "0.99.1")
    try:
        assert transport.version == "0.99.1"
        assert transport.methods == frozenset()
        assert transport.request("thread/start", {"model": "test"}) == {"ok": {"model": "test"}}
        assert transport.request("turn/start", {"threadId": "t"}) == {"ok": {"threadId": "t"}}
        assert transport.notifications() == (
            {"method": "turn/started", "params": {"turnId": "t"}},
            {"method": "turn/started", "params": {"turnId": "t"}},
        )
        assert transport.notifications() == ()
        with pytest.raises(CodexRPCError, match="-32602"):
            transport.request("rpc/error", {})
        assert transport.request("thread/read", {}) == {"ok": {}}
    finally:
        transport.close()
    transport.close()
    with pytest.raises(OSError, match="closed"):
        transport.request("thread/read", {})


def test_version_mismatch_does_not_launch(command: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="version"):
        CodexStdio(command, "0.99.2")
    with pytest.raises(ValueError, match="pinned version"):
        CodexStdio(command, "0.99.1", schema_methods=frozenset({"thread/start"}), schema_version="0.99.2")


@pytest.mark.parametrize("method", ["wrong/id", "too/large", "malformed", "server/request", "timeout"])
def test_uncertain_transport_closes_without_retry(command: tuple[str, ...], method: str) -> None:
    transport = CodexStdio(command, "0.99.1", timeout=1, max_bytes=512)
    try:
        with pytest.raises((OSError, OverflowError, TimeoutError, ValueError)):
            transport.request(method, {})
        with pytest.raises(OSError, match="reconcile"):
            transport.request("thread/start", {})
    finally:
        transport.close()


def test_local_refusal_keeps_connection(command: tuple[str, ...]) -> None:
    transport = CodexStdio(command, "0.99.1", max_bytes=512)
    try:
        with pytest.raises(ValueError, match="exceeds"):
            transport.request("thread/start", {"text": "x" * 512})
        assert transport.request("thread/start", {}) == {"ok": {}}
    finally:
        transport.close()


def test_notification_after_response_is_drained(command: tuple[str, ...]) -> None:
    transport = CodexStdio(command, "0.99.1")
    try:
        assert transport.request("post/response", {}) == {}
        # The synthetic server writes a notification immediately after the response.
        assert transport.notifications() == ({"method": "turn/completed", "params": {"turnId": "t"}},)
    finally:
        transport.close()


@pytest.mark.parametrize(
    "args", [("codex", "app-server"), ("/bin/true", "other"), ("/bin/true", "app-server", "--flag")]
)
def test_requires_explicit_absolute_app_server(args: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="absolute Codex"):
        CodexStdio(args, "0.99.1")


ENV_FAKE = f"""#!{sys.executable}
import json
import sys
from pathlib import Path

mode = "version" if sys.argv[1:] == ["--version"] else "server"
Path(sys.argv[0] + "." + mode + ".env").write_text(json.dumps(dict(__import__("os").environ)))
if mode == "version":
    print("codex-cli 0.99.1")
    sys.exit(0)
first = json.loads(sys.stdin.readline())
assert first["method"] == "initialize"
print(json.dumps({{"id": 1, "result": {{"userAgent": "fake"}}}}), flush=True)
assert json.loads(sys.stdin.readline()) == {{"method": "initialized", "params": {{}}}}
for line in sys.stdin:
    print(json.dumps({{"method": "turn/started", "params": {{"turnId": "t"}}}}), flush=True)
    print(json.dumps({{"id": json.loads(line)["id"], "result": {{"ok": True}}}}), flush=True)
"""

# Synthetic sentinel markers prove controller credentials never reach the child.
# Constructed programmatically so no literal credential-like pair appears in source.
SENTINEL_NAMES = ("CREATIDY_DOGFOOD_ROUTER_KEY", "CREATIDY_DOGFOOD_FORGE_TOKEN", "SOMEONE_ELSES_API_KEY")
SENTINELS = {name: "synthetic-" + name.lower().replace("_", "-") for name in SENTINEL_NAMES}


@pytest.fixture
def env_command(tmp_path: Path) -> str:
    binary = tmp_path / "env-codex"
    binary.write_text(ENV_FAKE, encoding="utf-8")
    binary.chmod(0o700)
    return str(binary)


def _dumped(binary: str, mode: str) -> dict[str, str]:
    return json.loads(Path(binary + "." + mode + ".env").read_text())


def test_explicit_environment_replaces_inheritance(env_command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    closed = {"HOME": "/home/tester", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    transport = CodexStdio((env_command, "app-server"), "0.99.1", environment=closed)
    try:
        for mode in ("version", "server"):
            dumped = _dumped(env_command, mode)
            # The controller secrets never reach either Codex subprocess.
            assert not (set(SENTINELS) & set(dumped))
            assert not (set(SENTINELS.values()) & set(dumped.values()))
            # The explicitly allowed operational entries do reach both subprocesses.
            assert dumped == closed
    finally:
        transport.close()


def test_default_environment_still_inherits(env_command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREATIDY_DOGFOOD_ROUTER_KEY", "synthetic-router-sentinel")
    transport = CodexStdio((env_command, "app-server"), "0.99.1")
    try:
        # Preserves the pre-existing implicit-inheritance behavior for non-opted callers.
        assert _dumped(env_command, "version")["CREATIDY_DOGFOOD_ROUTER_KEY"] == "synthetic-router-sentinel"
        assert _dumped(env_command, "server")["CREATIDY_DOGFOOD_ROUTER_KEY"] == "synthetic-router-sentinel"
    finally:
        transport.close()


def test_invalid_explicit_environment_refused(env_command: str) -> None:
    with pytest.raises(ValueError, match="environment"):
        CodexStdio((env_command, "app-server"), "0.99.1", environment={"": "x"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="environment"):
        CodexStdio((env_command, "app-server"), "0.99.1", environment={"BAD": object()})  # type: ignore[dict-item]
