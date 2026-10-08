# SPDX-License-Identifier: Apache-2.0
"""Synthetic worker executed INSIDE the rootless OCI boundary (no model, no real secrets).

Approximates native coding-agent behavior for the bounded proof: reading approved read-only
sources, modifying a disposable workspace, running shell/Python subprocesses, spawning child
and detached descendants, calling the controller-owned external-effect relay, and honoring
cancellation (SIGTERM) and its recorded deadline. Every probe result is recorded as an
observed outcome; the worker never claims enforcement on the controller's behalf.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import signal
import socket
import subprocess  # noqa: S404 - the worker executes the Attempt's own synthetic argv.
import sys
import time
from pathlib import Path
from typing import Any

WORKSPACE = Path("/workspace")
STATUS_PATH = WORKSPACE / "worker-status.json"
RELAY_SOCKET = Path("/relay/effects.sock")


def _errno_label(error: OSError) -> str:
    code = error.errno or 0
    return errno.errorcode.get(code, f"errno-{code}")


def _record(status: dict[str, Any]) -> None:
    """Persist incremental worker status so an observer sees partial facts, never invented ones."""
    STATUS_PATH.write_text(json.dumps(status, indent=1, sort_keys=True))


def _note(status: dict[str, Any], step: str, observation: dict[str, Any]) -> None:
    status.setdefault("steps", {})[step] = observation
    _record(status)


def _self_observations() -> dict[str, Any]:
    status_text = Path("/proc/self/status").read_text()
    fields: dict[str, str] = {}
    for line in status_text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()

    def cgroup_value(name: str) -> str:
        try:
            return (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            return "unavailable"

    return {
        "uid_map": Path("/proc/self/uid_map").read_text().strip(),
        "gid_map": Path("/proc/self/gid_map").read_text().strip(),
        "CapEff": fields.get("CapEff", "unknown"),
        "NoNewPrivs": fields.get("NoNewPrivs", "unknown"),
        "Seccomp": fields.get("Seccomp", "unknown"),
        "Seccomp_filters": fields.get("Seccomp_filters", "unknown"),
        "NSpid": fields.get("NSpid", "unknown"),
        "env_names": sorted(os.environ.keys()),
        "mounts": Path("/proc/self/mounts").read_text().splitlines(),
        "network_interfaces": sorted(name for _index, name in socket.if_nameindex()),
        "ipv4_routes": Path("/proc/net/route").read_text().splitlines(),
        "cgroup_pids_max": cgroup_value("pids.max"),
        "cgroup_memory_max": cgroup_value("memory.max"),
    }


def _read_approved(step: dict[str, Any], status: dict[str, Any]) -> None:
    path = Path(step["path"])
    data = path.read_bytes()
    label = str(step.get("name", f"read:{path.name}"))
    _note(status, label, {"outcome": "read", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})


def _write_workspace(step: dict[str, Any], status: dict[str, Any]) -> None:
    path = WORKSPACE / step["name"].removeprefix("write:") if "name" in step else WORKSPACE / str(step["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    data = str(step.get("content", "")).encode()
    path.write_bytes(data)
    _note(status, f"write:{path.name}", {"outcome": "written", "size": len(data)})


def _run_command(step: dict[str, Any], status: dict[str, Any]) -> None:
    argv = [str(arg) for arg in step["argv"]]
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=float(step.get("timeout", 30)),
        check=False,
    )  # noqa: S603 - synthetic Attempt argv executed inside the disposable boundary.
    _note(
        status,
        f"command:{argv[0]}",
        {"rc": result.returncode, "stdout": result.stdout[:2000], "stderr": result.stderr[:2000]},
    )


def _spawn_descendant(step: dict[str, Any], status: dict[str, Any]) -> None:
    marker = str(step["marker"])
    seconds = int(step.get("seconds", 120))
    # The attempt-unique marker is carried in the cmdlines of BOTH the setsid shell and its
    # long-running grandchild, so the controller can prove descendant settlement by scanning
    # host /proc for the exact worker tree, not by assuming namespace teardown covers it.
    # The script must not exec: the surviving shell keeps its marker argv.
    script = WORKSPACE / f"desc-{marker}.sh"
    script.write_text(f'/toolchain/bin/python3.12 -c "import time; time.sleep({seconds})  # desc-{marker}"\n')
    result = subprocess.run(
        ["/bin/busybox", "setsid", "/bin/busybox", "sh", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        start_new_session=True,
    )  # noqa: S603 - owned synthetic descendant inside the Attempt boundary.
    _note(
        status,
        f"descendant:{marker}",
        {"rc": result.returncode, "marker_argv": str(script), "stderr": result.stderr[:500]},
    )


def _probe_open(path: str, status: dict[str, Any], step: str, write: bool = False) -> None:
    try:
        if write:
            Path(path).write_bytes(b"kernel78-probe-write-attempt")
            outcome = "write-ok"
        else:
            data = Path(path).read_bytes()[:256]
            outcome = f"read-ok:{len(data)}"
    except OSError as error:
        outcome = f"denied:{_errno_label(error)}"
    _note(status, step, {"outcome": outcome})


def _probe_env(name: str, status: dict[str, Any]) -> None:
    present = name in os.environ
    _note(status, f"env:{name}", {"present": present})  # Name only; values are never recorded.


def _probe_network(host: str, port: int, status: dict[str, Any], kind: str) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect((host, port))
        outcome = "connected"
    except OSError as error:
        outcome = f"denied:{_errno_label(error)}"
    _note(status, f"network:{kind}", {"outcome": outcome})


def _probe_dns(name: str, status: dict[str, Any]) -> None:
    try:
        socket.getaddrinfo(name, 443)
        outcome = "resolved"
    except OSError as error:
        outcome = f"denied:{error.__class__.__name__}"
    _note(status, f"dns:{name}", {"outcome": outcome})


def _probe_unix_socket(path: str, status: dict[str, Any], abstract: bool = False) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            address = f"\0{path}" if abstract else path
            sock.connect(address)
        outcome = "connected"
    except OSError as error:
        outcome = f"denied:{_errno_label(error)}"
    label = "abstract-socket" if abstract else "unix-socket"
    _note(status, label, {"outcome": outcome})


def _probe_unshare(status: dict[str, Any]) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    rc = libc.unshare(0x10000000)  # CLONE_NEWUSER
    detail = f"rc={rc} errno={ctypes.get_errno()}"
    _note(status, "unshare-newuser", {"outcome": "allowed" if rc == 0 else f"denied:{detail}"})


def _probe_symlink_escape(status: dict[str, Any]) -> None:
    link = WORKSPACE / "escape-link"
    if not link.exists():
        link.symlink_to("/toolchain")
    try:
        (link / "bin" / "probe.txt").write_bytes(b"escape")
        outcome = "write-ok"
    except OSError as error:
        outcome = f"denied:{_errno_label(error)}"
    _note(status, "symlink-escape", {"outcome": outcome})


def _hold_fd(step: dict[str, Any], status: dict[str, Any]) -> None:
    """Repeatedly append to a workspace file while holding the descriptor open."""
    path = WORKSPACE / str(step.get("file", "held-fd.bin"))
    seconds = float(step.get("seconds", 8))
    chunk = b"x" * 4096
    written = 0
    deadline = time.monotonic() + seconds
    with path.open("ab") as handle:
        handle.write(b"start\n")
        handle.flush()
        while time.monotonic() < deadline:
            handle.write(chunk)
            written += len(chunk)
            handle.flush()
            time.sleep(0.2)
    _note(status, "hold-fd", {"written_bytes": written, "final_size": path.stat().st_size})


def _call_relay(step: dict[str, Any], status: dict[str, Any]) -> None:
    request = {
        "request_id": str(step.get("request_id", f"req-{int(time.time() * 1000)}")),
        "grant": str(step["grant"]),
        "op": str(step["op"]),
    }
    outcome: dict[str, Any]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(float(step.get("timeout", 5)))
            sock.connect(str(RELAY_SOCKET))
            sock.sendall(json.dumps(request).encode() + b"\n")
            try:
                reply = sock.recv(4096)
            except TimeoutError:
                reply = b""
        if reply:
            outcome = json.loads(reply.decode())
        else:
            outcome = {"result": "reply_lost"}
    except OSError as error:
        outcome = {"result": f"connect_failed:{_errno_label(error)}"}
    _note(status, f"relay:{step['op']}:{request['request_id']}", outcome)


def _flood_fork(status: dict[str, Any]) -> None:
    children: list[int] = []
    try:
        for _ in range(48):
            pid = os.fork()
            if pid == 0:
                time.sleep(60)
                os._exit(0)
            children.append(pid)
        outcome = f"allowed:{len(children)}"
    except OSError as error:
        outcome = f"denied:{_errno_label(error)}:{len(children)}"
    finally:
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for pid in children:
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
    _note(status, "flood-fork", {"outcome": outcome})


_STEP_HANDLERS = {
    "read_approved": _read_approved,
    "run_command": _run_command,
    "spawn_descendant": _spawn_descendant,
    "hold_fd": _hold_fd,
    "call_relay": _call_relay,
}


def _handle_step(step: dict[str, Any], status: dict[str, Any]) -> None:
    kind = str(step.get("kind", ""))
    if kind == "write_workspace":
        _write_workspace(step, status)
    elif kind == "probe_open":
        _probe_open(str(step["path"]), status, str(step["name"]), write=bool(step.get("write", False)))
    elif kind == "probe_env":
        _probe_env(str(step["name"]), status)
    elif kind == "probe_network":
        _probe_network(str(step["host"]), int(step["port"]), status, str(step.get("label", str(step["host"]))))
    elif kind == "probe_dns":
        _probe_dns(str(step["name"]), status)
    elif kind == "probe_unix_socket":
        _probe_unix_socket(str(step["path"]), status, abstract=bool(step.get("abstract", False)))
    elif kind == "probe_unshare":
        _probe_unshare(status)
    elif kind == "probe_symlink_escape":
        _probe_symlink_escape(status)
    elif kind == "flood_fork":
        _flood_fork(status)
    elif kind in _STEP_HANDLERS:
        _STEP_HANDLERS[kind](step, status)  # type: ignore[operator]
    elif kind == "sleep":
        time.sleep(min(float(step.get("seconds", 1)), 300))
        _note(status, f"sleep:{step.get('seconds', 1)}", {"outcome": "slept"})
    else:
        _note(status, f"unknown:{kind}", {"outcome": "skipped:unknown-step-kind"})


def main() -> int:
    spec_path = WORKSPACE / "attempt.json"
    spec: dict[str, Any] = json.loads(spec_path.read_text())
    deadline = time.monotonic() + float(spec.get("deadline_seconds", 60))
    status: dict[str, Any] = {"attempt": spec.get("attempt", "unknown"), "started": True, "steps": {}}
    _record(status)

    state = {"terminal": ""}

    def on_term(signum: int, _frame: Any) -> None:
        state["terminal"] = "cancelled"
        status["terminal"] = "cancelled"
        status["terminal_signal"] = int(signum)
        _record(status)
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_term)

    for step in spec.get("steps", []):
        if time.monotonic() >= deadline:
            status["terminal"] = "expired"
            _record(status)
            return 124
        try:
            _handle_step(step, status)
        except Exception as error:  # Record per-step failure and continue; never fake success.
            status.setdefault("steps", {})[str(step.get("kind", "unknown"))] = {
                "outcome": "error",
                "detail": f"{type(error).__name__}:{error}"[:500],
            }
            _record(status)
    status["terminal"] = state["terminal"] or "completed"
    status["self_observations"] = _self_observations()
    _record(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
