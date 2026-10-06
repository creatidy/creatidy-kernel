# SPDX-License-Identifier: Apache-2.0
"""Synthetic app-server fixtures; never starts the real Codex executable."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from creatidy_kernel.adapters.codex_stdio import CodexRPCError, CodexStdio

FAKE = f"""#!{sys.executable}
import json
import os
import signal
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
status = 'inProgress'
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
    elif message['method'] == 'malformed/notification':
        print(json.dumps({{'method': 'item/agentMessage/delta', 'params': None}}), flush=True)
    elif message['method'] in {{'progress', 'progress/flood'}}:
        for _ in range(20 if message['method'] == 'progress/flood' else 2):
            params = {{'threadId': 't', 'turnId': 'u', 'itemId': 'i', 'delta': 'x' * 64}}
            print(json.dumps({{'method': 'item/agentMessage/delta', 'params': params}}))
        result = {{'turn': {{'id': 'u', 'status': 'inProgress'}}}}
        print(json.dumps({{'id': message['id'], 'result': result}}), flush=True)
    elif message['method'] == 'spawn/stubborn' or (
        os.environ.get('KERNEL_LIFECYCLE') == '1' and message['method'] == 'turn/start'
    ):
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.write(write_fd, b'ready')
            os.close(write_fd)
            while True:
                signal.pause()
        os.close(write_fd)
        assert os.read(read_fd, 5) == b'ready'
        os.close(read_fd)
        result = {{'child': child, 'group': os.getpgrp(), 'turn': {{'id': 'turn-1'}}}}
        print(json.dumps({{'id': message['id'], 'result': result}}), flush=True)
    elif os.environ.get('KERNEL_LIFECYCLE') == '1':
        method = message['method']
        if method == 'thread/start':
            result = {{'thread': {{'id': 'thread-1'}}, 'model': 'glm-5.3', 'modelProvider': 'zai'}}
        elif method == 'turn/interrupt':
            assert message['params'] == {{'threadId': 'thread-1', 'turnId': 'turn-1'}}
            status = 'interrupted'
            result = {{}}
        else:
            assert method == 'thread/read'
            for _ in range(2):
                params = {{'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'i', 'delta': 'x' * 64}}
                print(json.dumps({{'method': 'item/agentMessage/delta', 'params': params}}))
            result = {{'thread': {{'id': 'thread-1', 'turns': [{{'id': 'turn-1', 'status': status}}]}}}}
        print(json.dumps({{'id': message['id'], 'result': result}}), flush=True)
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


def test_progress_discard_does_not_accumulate_across_healthy_exchanges(command: tuple[str, ...]) -> None:
    transport = CodexStdio(command, "0.99.1", max_bytes=512, retain_notifications=False)
    try:
        for _ in range(30):
            assert transport.request("progress", {}) == {"turn": {"id": "u", "status": "inProgress"}}
        assert transport.notifications() == ()
        assert transport.request("thread/read", {}) == {"ok": {}}
    finally:
        transport.close()


def test_owned_cleanup_escalates_stubborn_child_without_signalling_bystander(
    command: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    transport = CodexStdio(command, "0.99.1", timeout=1, environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    spawned = transport.request("spawn/stubborn", {})
    child = int(str(spawned["child"]))
    owned_group = int(str(spawned["group"]))
    original_killpg = os.killpg
    signals: list[tuple[int, int]] = []

    def capture(group: int, sig: int) -> None:
        assert group == owned_group
        signals.append((group, sig))
        original_killpg(group, sig)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", capture)
            transport.close()
        assert (owned_group, signal.SIGTERM) in signals
        assert (owned_group, signal.SIGKILL) in signals
        assert bystander.poll() is None
        # A residual group (including unreaped zombies) is not reported settled.
        try:
            original_killpg(owned_group, 0)
        except ProcessLookupError:
            assert transport.owned_group_settled is True
        else:
            assert transport.owned_group_settled is False
        state = Path(f"/proc/{child}/stat")
        deadline = time.monotonic() + 5
        while state.exists() and state.read_text().split(")", 1)[1].split()[0] != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not state.exists() or state.read_text().split(")", 1)[1].split()[0] == "Z"
    finally:
        transport.close()
        original_killpg(bystander.pid, signal.SIGKILL)
        bystander.wait(timeout=5)
        try:
            original_killpg(owned_group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_cleanup_refuses_shared_process_group(command: tuple[str, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    class Inspected(CodexStdio):
        def refuse_shared(self, process: subprocess.Popen[bytes]) -> bool:
            return self._stop(process)

    transport = Inspected(command, "0.99.1")
    try:
        with monkeypatch.context() as patch:

            def forbidden(_group: int, _sig: int) -> None:
                raise AssertionError("shared group must never be signalled")

            patch.setattr(os, "killpg", forbidden)
            assert transport.refuse_shared(bystander) is False
        assert bystander.poll() is None
    finally:
        transport.close()
        bystander.kill()
        bystander.wait(timeout=5)


@pytest.mark.parametrize(
    "method", ["malformed/notification", "server/request", "too/large", "progress/flood", "malformed", "wrong/id"]
)
def test_progress_discard_still_refuses_invalid_individual_traffic(command: tuple[str, ...], method: str) -> None:
    transport = CodexStdio(command, "0.99.1", max_bytes=512, retain_notifications=False)
    try:
        with pytest.raises((OSError, OverflowError, ValueError)):
            transport.request(method, {})
        with pytest.raises(OSError, match="closed"):
            transport.request("thread/read", {})
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
SENTINEL_NAMES = ("CREATIDY_KERNEL_ROUTER_KEY", "CREATIDY_KERNEL_FORGE_TOKEN", "SOMEONE_ELSES_API_KEY")
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
    monkeypatch.setenv("CREATIDY_KERNEL_ROUTER_KEY", "synthetic-router-sentinel")
    transport = CodexStdio((env_command, "app-server"), "0.99.1")
    try:
        # Preserves the pre-existing implicit-inheritance behavior for non-opted callers.
        assert _dumped(env_command, "version")["CREATIDY_KERNEL_ROUTER_KEY"] == "synthetic-router-sentinel"
        assert _dumped(env_command, "server")["CREATIDY_KERNEL_ROUTER_KEY"] == "synthetic-router-sentinel"
    finally:
        transport.close()


def test_invalid_explicit_environment_refused(env_command: str) -> None:
    with pytest.raises(ValueError, match="environment"):
        CodexStdio((env_command, "app-server"), "0.99.1", environment={"": "x"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="environment"):
        CodexStdio((env_command, "app-server"), "0.99.1", environment={"BAD": object()})  # type: ignore[dict-item]
