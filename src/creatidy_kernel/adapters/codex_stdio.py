# SPDX-License-Identifier: Apache-2.0
"""Opt-in Codex app-server stdio transport for a trusted controller only.

The caller owns durable operation identity and reconciliation. A failed request
can have taken effect remotely; this transport never retries it.
"""

import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast


class CodexRPCError(RuntimeError):
    """A correlated native JSON-RPC domain error."""


class CodexStdio:
    # The active controller can journal pending cancellation before pipe failure
    # causes owned process cleanup, without reopening its exclusively held store.
    before_close: Callable[[], None] | None = None

    def __init__(
        self,
        command: tuple[str, ...],
        expected_version: str,
        timeout: int = 15,
        max_bytes: int = 1_000_000,
        schema_methods: frozenset[str] = frozenset(),
        schema_version: str | None = None,
        environment: Mapping[str, str] | None = None,
        retain_notifications: bool = True,
        *,
        worker_command: tuple[str, ...] | None = None,
        worker_environment: Mapping[str, str] | None = None,
        before_worker: Callable[[tuple[str, ...], Mapping[str, str]], bool] | None = None,
        worker_fds: tuple[int, ...] = (),
        after_worker: Callable[[subprocess.Popen[bytes]], bool] | None = None,
    ) -> None:
        """Probe pinned Codex, then initialize either it or an explicitly composed worker.

        Worker composition requires closed environments for BOTH processes. The
        trusted caller must authorize and verify resources BEFORE this constructor:
        it launches immediately, before Runtime thread/turn admission. This is not
        an isolation attestation, container receipt or descendant-settlement gate.
        Controlled composition also uses before_worker to recheck current authority
        and resources after the probe, immediately before the worker send.
        """
        if (
            len(command) != 2
            or not Path(command[0]).is_absolute()
            or command[1] != "app-server"
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?", expected_version) is None
            or type(timeout) is not int
            or timeout <= 0
            or type(max_bytes) is not int
            or max_bytes <= 0
            or type(schema_methods) is not frozenset
            or any(type(method) is not str or not method for method in schema_methods)
            or (schema_methods and schema_version != expected_version)
            or type(retain_notifications) is not bool
        ):
            raise ValueError("absolute Codex app-server command, pinned version and finite limits required")
        if worker_command is not None and (
            type(worker_command) is not tuple
            or not worker_command
            or any(type(part) is not str or "\x00" in part for part in worker_command)
            or not Path(worker_command[0]).is_absolute()
            or environment is None
            or worker_environment is None
        ):
            raise ValueError("explicit absolute worker argv and closed probe/worker environments required")
        if worker_command is None and worker_environment is not None:
            raise ValueError("worker environment requires an explicit worker command")
        if worker_command is None and before_worker is not None:
            raise ValueError("worker authorization requires an explicit worker command")
        if (
            type(worker_fds) is not tuple
            or any(type(fd) is not int or fd < 3 for fd in worker_fds)
            or len(set(worker_fds)) != len(worker_fds)
            or (worker_command is None and (worker_fds or after_worker is not None))
        ):
            raise ValueError("explicit worker descriptor and startup gate required")
        launch_environment = None if worker_environment is None else dict(worker_environment)
        if launch_environment is not None and any(
            type(name) is not str
            or not name
            or "=" in name
            or "\x00" in name
            or type(value) is not str
            or "\x00" in value
            for name, value in launch_environment.items()
        ):
            raise ValueError("explicit worker environment must map valid names to string values")
        if environment is not None:
            # Explicit mappings replace inheritance, never merge with ambient state.
            # The direct-Codex path uses this environment for both subprocesses.
            supplied = dict(environment)
            if any(type(name) is not str or not name or type(value) is not str for name, value in supplied.items()):
                raise ValueError("explicit Codex environment must map nonempty names to string values")
            self._environment: dict[str, str] | None = supplied
        else:
            # Preserves the pre-existing implicit-inheritance behavior for callers
            # that have not opted into an explicit subprocess environment.
            self._environment = None
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._retain_notifications = retain_notifications
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._notifications: list[dict[str, object]] = []
        self._notification_bytes = 0
        self._next_id = 1
        self._process: subprocess.Popen[bytes] | None = None
        self.owned_group_settled: bool | None = None
        self._version = expected_version
        # initialize supplies no native method inventory. A trusted caller must
        # supply the method inventory from the schema generated for this binary.
        self._methods = schema_methods

        version_process = subprocess.Popen(  # noqa: S603 - explicit trusted absolute executable, no shell.
            (command[0], "--version"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=self._environment,
        )
        try:
            output = self._version_output(version_process)
        finally:
            self._stop(version_process)
        if output != f"codex-cli {expected_version}" and output != f"codex {expected_version}":
            raise ValueError("Codex executable version does not match expected_version")

        try:
            if before_worker is not None and (
                worker_command is None
                or launch_environment is None
                or before_worker(worker_command, dict(launch_environment)) is not True
            ):
                raise ValueError("worker launch authorization unavailable")
            self._process = subprocess.Popen(  # noqa: S603 - explicit trusted absolute executable, no shell.
                command if worker_command is None else worker_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=self._environment if worker_command is None else launch_environment,
                pass_fds=worker_fds,
            )
            if after_worker is not None and after_worker(self._process) is not True:
                raise ValueError("worker startup gate refused")
            self._exchange(
                "initialize",
                {"clientInfo": {"name": "creatidy_kernel", "title": "Creatidy Kernel", "version": "0.0.1"}},
            )
            self._send({"method": "initialized", "params": {}}, time.monotonic() + self.timeout)
        except BaseException:
            self.close()
            raise

    @property
    def version(self) -> str:
        return self._version

    @property
    def methods(self) -> frozenset[str]:
        return self._methods

    def _stop(self, process: subprocess.Popen[bytes]) -> bool:
        settled = False
        if process.poll() is None:
            try:
                if os.getpgid(process.pid) != process.pid:
                    return False  # Never signal or adopt a shared/foreign process group.
            except ProcessLookupError:
                return False
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                pass
            # A reaped leader does not establish descendant settlement. Its owned
            # group may still contain children that ignored graceful termination.
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                settled = True
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    settled = True
                process.wait(timeout=self.timeout)
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    settled = True
        else:
            process.wait()
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        return settled

    def _version_output(self, process: subprocess.Popen[bytes]) -> str:
        if process.stdout is None:
            raise OSError("Codex version output unavailable")
        deadline = time.monotonic() + self.timeout
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Codex version check timed out")
                chunk = os.read(process.stdout.fileno(), min(8192, self.max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > self.max_bytes:
                    raise OverflowError("Codex version output exceeds limit")
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode != 0:
            raise OSError("Codex version check failed")
        return data.decode("utf-8").strip()

    def _send(self, message: dict[str, object], deadline: float) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise OSError("Codex transport closed")
        data = json.dumps(message, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > self.max_bytes:
            raise ValueError("Codex request exceeds limit")
        fd = process.stdin.fileno()
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE)
            view = memoryview(data)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Codex request write timed out")
                try:
                    count = os.write(fd, view)
                except BlockingIOError:
                    continue
                view = view[count:]

    def _read_line(self, deadline: float) -> bytes:
        process = self._process
        if process is None or process.stdout is None:
            raise OSError("Codex transport closed")
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"\n" not in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Codex response timed out")
                chunk = os.read(process.stdout.fileno(), min(8192, self.max_bytes + 1 - len(self._pending)))
                if not chunk:
                    raise OSError("Codex stdout closed before response")
                self._pending.extend(chunk)
                if len(self._pending) > self.max_bytes:
                    raise OverflowError("Codex response exceeds limit")
            line, _, rest = self._pending.partition(b"\n")
            self._pending = bytearray(rest)
            return bytes(line)

    def _decode(self, line: bytes) -> dict[str, object]:
        message: object = json.loads(line)
        if not isinstance(message, dict):
            raise OSError("invalid Codex JSON-RPC message")
        obj = cast(dict[object, object], message)
        if not all(isinstance(key, str) for key in obj):
            raise OSError("invalid Codex JSON-RPC message")
        obj = cast(dict[str, object], message)
        if "jsonrpc" in obj and obj["jsonrpc"] != "2.0":
            raise OSError("invalid Codex JSON-RPC version")
        return obj

    def _buffer_notification(self, obj: dict[str, object], size: int) -> None:
        if not isinstance(obj.get("method"), str) or not obj["method"] or "id" in obj:
            raise OSError("unsupported Codex server request")
        if "result" in obj or "error" in obj or ("params" in obj and not isinstance(obj["params"], dict)):
            raise OSError("invalid Codex notification envelope")
        # The task controller uses correlated receipts and thread/read as durable
        # evidence, not progress events. Validation and exchange bounds still apply.
        if not self._retain_notifications:
            return
        self._notification_bytes += size
        if self._notification_bytes > self.max_bytes:
            raise OverflowError("Codex notification buffer exceeds limit")
        self._notifications.append(obj)

    def _exchange(self, method: str, params: dict[str, object]) -> dict[str, object]:
        identifier = self._next_id
        self._next_id += 1
        deadline = time.monotonic() + self.timeout
        self._send({"method": method, "id": identifier, "params": params}, deadline)
        consumed = 0
        while True:
            line = self._read_line(deadline)
            consumed += len(line) + 1
            if consumed > self.max_bytes:
                raise OverflowError("Codex exchange exceeds limit")
            obj = self._decode(line)
            if "method" in obj:
                self._buffer_notification(obj, len(line) + 1)
                continue
            if type(obj.get("id")) is not int or obj["id"] != identifier:
                raise OSError("uncorrelated Codex response")
            if "error" in obj:
                error = obj["error"]
                if not isinstance(error, dict):
                    raise OSError("invalid Codex RPC error")
                error_obj = cast(dict[str, object], error)
                if type(error_obj.get("code")) is not int or not isinstance(error_obj.get("message"), str):
                    raise OSError("invalid Codex RPC error")
                raise CodexRPCError(f"Codex RPC error {error_obj['code']}: {error_obj['message']}")
            result = obj.get("result")
            if not isinstance(result, dict):
                raise OSError("invalid Codex RPC result")
            return cast(dict[str, object], result)

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if not method or method in {"initialize", "initialized"}:
            raise ValueError("invalid Codex request")
        with self._lock:
            if self._process is None:
                raise OSError("Codex transport closed; reconcile before another request")
            try:
                return self._exchange(method, params)
            except CodexRPCError:
                raise
            except (ValueError, TypeError) as error:
                if isinstance(error, (json.JSONDecodeError, UnicodeError)):
                    self._close_unlocked()
                # Local encoding/size refusal occurs before writing any bytes.
                raise
            except BaseException:
                self._close_unlocked()
                raise

    def notifications(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            process = self._process
            if process is not None and process.stdout is not None:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        deadline = time.monotonic() + self.timeout
                        received = 0
                        while selector.select(0):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Codex notification drain timed out")
                            chunk = os.read(
                                process.stdout.fileno(), min(8192, self.max_bytes + 1 - received - len(self._pending))
                            )
                            if not chunk:
                                raise OSError("Codex stdout closed")
                            received += len(chunk)
                            self._pending.extend(chunk)
                            if received + len(self._pending) > self.max_bytes:
                                raise OverflowError("Codex response exceeds limit")
                    while b"\n" in self._pending:
                        line, _, rest = self._pending.partition(b"\n")
                        self._pending = bytearray(rest)
                        self._buffer_notification(self._decode(bytes(line)), len(line) + 1)
                except BaseException:
                    self._close_unlocked()
                    raise
            messages = tuple(self._notifications)
            self._notifications.clear()
            self._notification_bytes = 0
            return messages

    def _close_unlocked(self) -> None:
        try:
            if self.before_close is not None:
                self.before_close()
        finally:
            process, self._process = self._process, None
            if process is not None:
                self.owned_group_settled = self._stop(process)

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()
