# SPDX-License-Identifier: Apache-2.0
"""Synthetic external-effect relay: the controller-owned authority broker.

Demonstrates the required architecture boundary on a disposable loopback channel: the
isolated worker has no general network and reaches the relay only through one explicitly
mounted Unix socket. The relay — not the worker — owns authorization: every external
operation must carry a grant bound to the Attempt and a currently valid, non-revoked,
non-expired authority. Lost replies are recorded as uncertain effects, never as
cancellations or rollbacks. This is a PoC seam, not a permanent service.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Grant:
    grant_id: str
    attempt: str
    op: str
    expires_at: float
    revoked: bool = False
    single_use: bool = True
    used: bool = False


@dataclass(slots=True)
class RelayEvent:
    kind: str  # request / effect / refusal / lost
    request_id: str
    grant_id: str
    op: str
    detail: str
    at: float = field(default_factory=time.time)


class EffectsRelay:
    """Unix-socket effect broker with controller-owned authority state."""

    def __init__(self, socket_path: Path, attempt: str) -> None:
        self.socket_path = socket_path
        self.attempt = attempt
        self._grants: dict[str, Grant] = {}
        self._events: list[RelayEvent] = []
        self._drop_next_reply = False
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        self._listener.bind(str(socket_path))
        self._listener.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    # -- controller-side authority operations -------------------------------------------
    def grant(self, grant_id: str, op: str, ttl_seconds: float = 60.0, single_use: bool = True) -> Grant:
        record = Grant(
            grant_id=grant_id,
            attempt=self.attempt,
            op=op,
            expires_at=time.time() + ttl_seconds,
            single_use=single_use,
        )
        with self._lock:
            self._grants[grant_id] = record
        return record

    def revoke(self, grant_id: str) -> None:
        with self._lock:
            if grant_id in self._grants:
                self._grants[grant_id].revoked = True

    def expire(self, grant_id: str) -> None:
        with self._lock:
            if grant_id in self._grants:
                self._grants[grant_id].expires_at = time.time() - 1.0

    def drop_next_reply(self) -> None:
        with self._lock:
            self._drop_next_reply = True

    def events(self) -> tuple[RelayEvent, ...]:
        with self._lock:
            return tuple(self._events)

    # -- serving --------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(10)
                buf = b""
                while b"\n" not in buf and not self._stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except (TimeoutError, OSError):
                        chunk = b""
                    if not chunk:
                        break
                    buf += chunk
                if not buf.strip():
                    continue
                try:
                    request = json.loads(buf.split(b"\n", 1)[0].decode())
                except json.JSONDecodeError:
                    continue
                reply = self._authorize(request)
                if self._consume_drop():
                    with self._lock:
                        self._events.append(
                            RelayEvent(
                                kind="lost",
                                request_id=str(request.get("request_id", "?")),
                                grant_id=str(request.get("grant", "?")),
                                op=str(request.get("op", "?")),
                                detail="reply deliberately dropped; effect state uncertain to worker",
                            )
                        )
                    continue
                try:
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    with self._lock:
                        self._events.append(
                            RelayEvent(
                                kind="lost",
                                request_id=str(request.get("request_id", "?")),
                                grant_id=str(request.get("grant", "?")),
                                op=str(request.get("op", "?")),
                                detail="reply could not be delivered; effect state uncertain to worker",
                            )
                        )

    def _consume_drop(self) -> bool:
        with self._lock:
            if self._drop_next_reply:
                self._drop_next_reply = False
                return True
            return False

    def _authorize(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str(request.get("request_id", "?"))
        grant_id = str(request.get("grant", ""))
        op = str(request.get("op", ""))
        with self._lock:
            self._events.append(
                RelayEvent(kind="request", request_id=request_id, grant_id=grant_id, op=op, detail="received")
            )
            record = self._grants.get(grant_id)
            now = time.time()
            if record is None:
                reason = "unknown_grant"
            elif record.attempt != self.attempt:
                reason = "grant_not_bound_to_attempt"
            elif record.op != op:
                reason = "grant_op_mismatch"
            elif record.revoked:
                reason = "grant_revoked"
            elif now >= record.expires_at:
                reason = "grant_expired"
            elif record.single_use and record.used:
                reason = "grant_already_consumed"
            else:
                record.used = True
                self._events.append(
                    RelayEvent(kind="effect", request_id=request_id, grant_id=grant_id, op=op, detail="executed")
                )
                return {"result": "executed", "request_id": request_id, "receipt": f"effect:{grant_id}"}
            self._events.append(
                RelayEvent(kind="refusal", request_id=request_id, grant_id=grant_id, op=op, detail=reason)
            )
            return {"result": "refused", "reason": reason, "request_id": request_id}
