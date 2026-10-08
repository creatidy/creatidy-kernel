# SPDX-License-Identifier: Apache-2.0
"""Closed-environment tests for the PoC authority seam and isolation profile builder.

These run everywhere (no native toolchain required): the relay authority state machine and
the controller-owned profile construction are exercised with synthetic fixtures only.
Native enforcement itself is covered separately by tests/test_oci_worker_isolation.py and
stays UNPROVED without the explicit pinned toolroot.
"""

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from tools.oci_worker_poc.ociimage import SECCOMP_DENY_NAMES, seccomp_profile_json
from tools.oci_worker_poc.profile import (
    FORBIDDEN_BIND_PREFIXES,
    AttemptResources,
    WorkerProfile,
    build_profile,
    build_run_argv,
    validate_resource_paths,
)
from tools.oci_worker_poc.relay import EffectsRelay
from tools.oci_worker_poc.toolchain import Toolchain, ToolRecord, host_python_closure


@pytest.fixture
def relay(tmp_path: Path) -> Iterator[EffectsRelay]:
    broker = EffectsRelay(tmp_path / "effects.sock", "attempt-a")
    broker.start()
    yield broker
    broker.stop()


def _request(broker: EffectsRelay, request_id: str, grant: str, op: str) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(broker.socket_path))
        sock.sendall(json.dumps({"request_id": request_id, "grant": grant, "op": op}).encode() + b"\n")
        reply = b""
        while b"\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
    assert reply, "relay closed without reply"
    result: dict[str, object] = json.loads(reply.split(b"\n", 1)[0].decode())
    return result


class TestRelayAuthority:
    def test_valid_single_use_grant_executes_then_replays_refuse(self, relay: EffectsRelay) -> None:
        relay.grant("g1", "synthetic:effect")
        first = _request(relay, "r1", "g1", "synthetic:effect")
        assert first["result"] == "executed"
        replay = _request(relay, "r2", "g1", "synthetic:effect")
        assert replay == {"result": "refused", "reason": "grant_already_consumed", "request_id": "r2"}

    def test_unknown_mismatched_revoked_and_expired_grants_refuse(self, relay: EffectsRelay) -> None:
        relay.grant("g-revoke", "synthetic:effect")
        relay.grant("g-expire", "synthetic:effect")
        relay.revoke("g-revoke")
        relay.expire("g-expire")
        assert _request(relay, "r1", "missing", "synthetic:effect")["reason"] == "unknown_grant"
        assert _request(relay, "r2", "g-revoke", "synthetic:effect")["reason"] == "grant_revoked"
        assert _request(relay, "r3", "g-expire", "synthetic:effect") == {
            "result": "refused",
            "reason": "grant_expired",
            "request_id": "r3",
        }
        relay.grant("g-op", "synthetic:effect")
        assert _request(relay, "r4", "g-op", "synthetic:other")["reason"] == "grant_op_mismatch"

    def test_dropped_reply_records_uncertain_effect_not_cancellation(self, relay: EffectsRelay) -> None:
        relay.grant("g1", "synthetic:effect")
        relay.drop_next_reply()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(str(relay.socket_path))
            sock.sendall(json.dumps({"request_id": "r1", "grant": "g1", "op": "synthetic:effect"}).encode() + b"\n")
            assert sock.recv(4096) == b"", "dropped reply must not be delivered"
        kinds = [event.kind for event in relay.events()]
        assert "lost" in kinds
        lost = next(event for event in relay.events() if event.kind == "lost")
        assert "uncertain" in lost.detail
        # The executed effect stays distinct from any cancel/rollback claim.
        assert "effect" in kinds
        assert not any("cancel" in event.detail or "rollback" in event.detail for event in relay.events())

    def test_grants_are_bound_to_the_relay_attempt(self, tmp_path: Path) -> None:
        broker_a = EffectsRelay(tmp_path / "a.sock", "attempt-a")
        broker_a.start()
        try:
            broker_a.grant("g1", "synthetic:effect")
            assert _request(broker_a, "r1", "g1", "synthetic:effect")["result"] == "executed"
        finally:
            broker_a.stop()
        broker_b = EffectsRelay(tmp_path / "b.sock", "attempt-b")
        broker_b.start()
        try:
            broker_b.grant("g2", "synthetic:effect")
            # A grant id copied across attempts is not authority for the other attempt.
            assert _request(broker_b, "r2", "g1", "synthetic:effect")["reason"] == "unknown_grant"
        finally:
            broker_b.stop()


def _resources(tmp_path: Path, attempt: str = "attempt-a", **overrides: object) -> AttemptResources:
    root = tmp_path / attempt
    values: dict[str, object] = {
        "attempt": attempt,
        "workspace": root / "workspace",
        "home": root / "home",
        "scratch": root / "scratch",
        "relay_socket": root / "relay" / "effects.sock",
    }
    values.update(overrides)
    return AttemptResources(**values)  # type: ignore[arg-type]


def _toolchain(root: Path) -> Toolchain:
    def record(name: str) -> ToolRecord:
        return ToolRecord(
            name=name, path=str(root / "bin" / name), version=f"{name}-synthetic", sha256="0" * 64, origin="synthetic"
        )

    return Toolchain(
        root=root,
        podman=record("podman"),
        crun=record("crun"),
        conmon=record("conmon"),
        netavark=record("netavark"),
        busybox=record("busybox"),
    )


class TestProfileBuilder:
    @pytest.fixture
    def argv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        monkeypatch.setattr(
            "tools.oci_worker_poc.profile.host_python_closure",
            lambda: (Path("/usr/bin/python3.12"), (Path("/usr/lib/python3.12"), Path("/lib64/ld-linux-x86-64.so.2"))),
        )
        resources = _resources(tmp_path)
        toolchain = _toolchain(tmp_path / "toolroot")
        worker_entry = tmp_path / "worker_main.py"
        worker_entry.write_text("# synthetic entry\n")
        profile: WorkerProfile = build_profile(toolchain, resources, worker_entry, deadline_seconds=45)
        return build_run_argv(toolchain, profile, "localhost/kernel78-worker:v1", "kernel78-worker-attempt-a")

    def test_run_argv_enforces_the_promised_boundary(self, argv: list[str], tmp_path: Path) -> None:
        run = argv[argv.index("run") :]
        for required in ("--network=none", "--cap-drop=all", "--read-only", "--read-only-tmpfs"):
            assert required in run, required
        assert "--security-opt" in run and "no-new-privileges" in run
        seccomp = run[run.index("--security-opt", run.index("no-new-privileges") + 1) + 1]
        assert seccomp.startswith("seccomp=") and Path(seccomp.split("=", 1)[1]).is_file()
        env_args = [run[index + 1] for index, item in enumerate(run) if item == "--env"]
        env = dict(item.split("=", 1) for item in env_args)
        assert env["HOME"] == "/home/worker"
        assert env["TMPDIR"] == "/tmp"  # noqa: S108 - container-internal tmpfs assertion.
        assert env["PATH"] == "/bin:/toolchain/bin"
        mounts = [item for item in run if item.count(":") == 2 and item.startswith("/")]
        writable = [item for item in mounts if item.endswith(":rw")]
        assert writable == [f"{tmp_path / 'attempt-a' / 'workspace'}:/workspace:rw"], (
            "workspace is the only writable bind"
        )
        assert "--network=none" in run

    def test_forbidden_and_noncanonical_resource_paths_refuse(self, tmp_path: Path) -> None:
        for prefix in FORBIDDEN_BIND_PREFIXES:
            # Forbidden prefixes are refused; some also fail the canonical-path check first
            # (e.g. /var/run is a symlink), which is an equally valid refusal.
            with pytest.raises(ValueError, match="resource path"):
                validate_resource_paths(_resources(tmp_path, relay_socket=prefix / "sock"))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-canonical"):
            validate_resource_paths(_resources(tmp_path, workspace=tmp_path / "link" / ".." / "workspace"))  # type: ignore[arg-type]

    def test_seccomp_profile_denies_namespace_and_kernel_surfaces(self, tmp_path: Path) -> None:
        profile = json.loads(seccomp_profile_json())
        assert profile["defaultAction"] == "SCMP_ACT_ALLOW"
        deny = set(profile["syscalls"][0]["names"])
        assert profile["syscalls"][0]["action"] == "SCMP_ACT_ERRNO"
        for expected in ("unshare", "setns", "mount", "umount2", "ptrace", "keyctl", "bpf", "clone3"):
            assert expected in deny
        assert deny == set(SECCOMP_DENY_NAMES)


def test_host_python_closure_shape() -> None:
    binary, closure = host_python_closure()
    assert binary.name == "python3.12"
    names = {path.name for path in closure}
    assert "python3.12" in names
    assert "ld-linux-x86-64.so.2" in names
