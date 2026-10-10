# SPDX-License-Identifier: Apache-2.0
"""In-boundary launcher for the real Codex app-server (executes inside the OCI worker).

Runs as the container entrypoint: records observed runtime facts (namespace identity,
capabilities, seccomp, mounts, network, cgroup limits) into the disposable workspace, starts
the controller-authored synthetic model backend on container-internal loopback, writes a
fresh synthetic ``CODEX_HOME`` config pointing at that backend, and then ``exec``s the
version-pinned Codex binary's ``app-server``. No host environment, credential or network
path participates: the closed environment below is the entire worker world.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess  # noqa: S404 - the entry executes the controller-pinned argv inside the boundary.
import sys
import time
from pathlib import Path
from typing import Any

WORKSPACE = Path("/workspace")
ATTEMPT_SPEC = WORKSPACE / "attempt.json"
OBSERVATIONS = WORKSPACE / "worker-observations.json"
PORT_FILE = WORKSPACE / "mock" / "port.txt"
CODEX_HOME = WORKSPACE / "codex-home"


def _record(payload: dict[str, Any]) -> None:
    OBSERVATIONS.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


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


def _write_codex_config(port: int) -> None:
    """Fresh synthetic provider config: loopback-only synthetic Responses backend."""
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    config = (
        'model = "synthetic-model"\n'
        'model_provider = "synthetic"\n'
        'approval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n'
        "\n"
        "[model_providers.synthetic]\n"
        'name = "Synthetic (controller-authored; in-boundary loopback)"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\n'
    )
    (CODEX_HOME / "config.toml").write_text(config)


def _start_mock(python: str, mock_source: str, scenario: str) -> int:
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if PORT_FILE.exists():
        PORT_FILE.unlink()
    process = subprocess.Popen(  # noqa: S603 - controller-mounted fixed argv inside the boundary.
        [python, "-S", mock_source, scenario, str(PORT_FILE)],
        env={
            "PATH": "/bin:/toolchain/bin",
            "PYTHONHOME": "/toolchain",
            "LD_LIBRARY_PATH": "/toolchain/lib",
            "LANG": "C.UTF-8",
            "LC_ALL": "C",
        },
    )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if PORT_FILE.exists():
            if process.poll() is not None:
                raise RuntimeError("mock model backend exited before publishing its port")
            return int(PORT_FILE.read_text().strip())
        if process.poll() is not None:
            raise RuntimeError("mock model backend exited before publishing its port")
        time.sleep(0.1)
    raise TimeoutError("mock model backend never published its port")


def main() -> int:
    spec: dict[str, Any] = json.loads(ATTEMPT_SPEC.read_text())
    deadline = time.monotonic() + float(spec.get("deadline_seconds", 180))
    observations: dict[str, Any] = {
        "attempt": spec.get("attempt", "unknown"),
        "started": True,
        "self_observations": _self_observations(),
    }
    _record(observations)

    def on_term(signum: int, _frame: Any) -> None:
        observations["terminal"] = "cancelled"
        observations["terminal_signal"] = int(signum)
        _record(observations)
        sys.exit(143)

    signal.signal(signal.SIGTERM, on_term)
    if time.monotonic() >= deadline:
        observations["terminal"] = "expired"
        _record(observations)
        return 124

    mock_port = _start_mock("/toolchain/bin/python3.12", "/worker/mockmodel.py", "/workspace/mock/scenario.json")
    _write_codex_config(mock_port)
    observations["mock_port"] = mock_port
    observations["codex_home"] = str(CODEX_HOME)
    observations["config_written"] = (CODEX_HOME / "config.toml").is_file()
    observations["launching"] = "codex app-server"
    _record(observations)

    codex = "/toolchain/codex/codex"
    environment = {
        "PATH": "/bin:/toolchain/bin",
        "HOME": "/home/worker",
        "TMPDIR": "/tmp",  # noqa: S108 - container-internal tmpfs path, not a host temp file.
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "ATTEMPT_ID": str(spec.get("attempt", "unknown")),
        "SHELL": "/bin/sh",
        "CODEX_HOME": str(CODEX_HOME),
        "LD_LIBRARY_PATH": "/toolchain/lib",
    }
    os.execve(codex, [codex, "app-server"], environment)  # noqa: S606 - pinned binary replaces this entry process.


if __name__ == "__main__":
    raise SystemExit(main())
