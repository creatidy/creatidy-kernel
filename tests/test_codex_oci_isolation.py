# SPDX-License-Identifier: Apache-2.0
"""Native #53 reception: the real Codex app-server inside the #78 rootless OCI boundary.

Set CREATIDY_TEST_OCI_TOOLROOT to the #78 toolroot to run native reception; a real Codex
binary must resolve (CREATIDY_TEST_CODEX_BIN or PATH) at the pinned version. Missing setup is
explicitly UNPROVED, never a passing receipt: every skip below means the real-harness boundary
was not exercised, not that isolation holds. Payloads are synthetic; no owner Codex state, no
credentials, no live model and no paid inference participate. Each Attempt records observed
outcomes into an evidence file beside its disposable workspace.
"""

import json
import os
import signal
import subprocess  # noqa: S603 - every call below uses fixed synthetic argv, no shell.
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest

from creatidy_kernel.adapters.codex_stdio import CodexRPCError
from tools.codex_oci_proof.codexbin import (
    PINNED_CODEX_VERSION,
    protocol_inventory,
    resolve_codex_binary,
)
from tools.codex_oci_proof.driver import AttemptExpired, CodexAttempt, exec_step, final_step
from tools.codex_oci_proof.ocimage import build_codex_image_archive
from tools.oci_worker_poc import harness
from tools.oci_worker_poc.toolchain import Toolchain, read_provenance

TOOLROOT_ENV = "CREATIDY_TEST_OCI_TOOLROOT"
PINNED_PODMAN_VERSION = "podman version 6.1.3"
PINNED_CRUN_VERSION = "crun version 1.30.1"
PINNED_CONMON_VERSION = "conmon version 2.2.1"
BYSTANDER_MARKER = "kernel53-bystander-marker"


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    configured = os.environ.get(TOOLROOT_ENV)
    if configured is None:
        pytest.skip(f"real-Codex worker isolation UNPROVED: explicit pinned toolroot required ({TOOLROOT_ENV})")
    resolved = harness.resolve_or_unproved()
    if resolved is None:
        pytest.skip(f"real-Codex worker isolation UNPROVED: toolroot incomplete at {configured}")
    return resolved


@pytest.fixture(scope="session")
def codex_binary(tmp_path_factory: pytest.TempPathFactory) -> Any:
    probe_root = tmp_path_factory.mktemp("codex-probe")
    try:
        return resolve_codex_binary(probe_root)
    except (FileNotFoundError, RuntimeError) as error:
        pytest.skip(f"real-Codex worker isolation UNPROVED: {error}")


@pytest.fixture(scope="session")
def codex_inventory(codex_binary: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    return protocol_inventory(codex_binary, tmp_path_factory.mktemp("codex-schema"))


@pytest.fixture(scope="session")
def provenance(toolchain: Toolchain) -> dict[str, Any]:
    return read_provenance(toolchain)


@pytest.fixture(scope="session")
def image(toolchain: Toolchain, tmp_path_factory: pytest.TempPathFactory) -> Any:
    archive = tmp_path_factory.mktemp("codex-image") / "codex-worker-image.tar"
    record = build_codex_image_archive(toolchain.busybox.path, archive)
    harness.load_archive([str(toolchain.root / "bin/podman")], toolchain.env(), archive)
    return record


class AttemptRunner:
    """Prepares, connects and disposes real-Codex Attempts against the pinned boundary."""

    def __init__(self, toolchain: Toolchain, codex_binary: Any, codex_inventory: Any, image: Any, tmp_path: Path):
        self.toolchain = toolchain
        self.codex_binary = codex_binary
        self.codex_inventory = codex_inventory
        self.image = image
        self.tmp_path = tmp_path
        self.attempts: list[CodexAttempt] = []

    def start(
        self,
        scenario: list[dict[str, Any]],
        *,
        deadline_seconds: int = 240,
        pids_limit: int | None = None,
        grants: tuple[tuple[str, str], ...] = (),
        seed_files: dict[str, str] | None = None,
    ) -> CodexAttempt:
        attempt = CodexAttempt(
            self.tmp_path,
            self.toolchain,
            self.codex_binary,
            self.codex_inventory,
            scenario,
            self.image,
            deadline_seconds=deadline_seconds,
            pids_limit=pids_limit,
        )
        for name, content in (seed_files or {}).items():
            path = attempt.resources.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for grant_id, op in grants:
            attempt.grant(grant_id, op)
        attempt.connect()
        self.attempts.append(attempt)
        return attempt

    def dispose_all(self) -> None:
        for attempt in self.attempts:
            attempt.dispose()


@pytest.fixture
def runner(toolchain: Toolchain, codex_binary: Any, codex_inventory: Any, image: Any, tmp_path: Path) -> Any:
    instance = AttemptRunner(toolchain, codex_binary, codex_inventory, image, tmp_path)
    yield instance
    instance.dispose_all()


def _wait_for_marker(marker: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if harness.scan_host_for_marker(marker):
            return True
        time.sleep(0.2)
    return False


def _spawn_bystander() -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed synthetic bystander argv, never workload input.
        [sys.executable, "-c", f"import time; time.sleep(120)  # {BYSTANDER_MARKER}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )  # noqa: S603 - fixed synthetic bystander argv, never workload input.


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        return ""


def _command_execution_items(attempt: CodexAttempt) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for notification in attempt.notifications:
        params = notification.get("params")
        if not isinstance(params, dict):
            continue
        item = cast(dict[object, object], params).get("item")
        if isinstance(item, dict) and cast(dict[object, object], item).get("type") == "commandExecution":
            items.append(cast(dict[str, Any], item))
    return items


def _command_execution_commands(attempt: CodexAttempt) -> list[str]:
    return [str(item.get("command", "")) for item in _command_execution_items(attempt)]


class TestProvenance:
    def test_pinned_toolchain_and_real_codex_binary(
        self,
        toolchain: Toolchain,
        provenance: dict[str, Any],
        codex_binary: Any,
        codex_inventory: Any,
    ) -> None:
        assert toolchain.podman.version == PINNED_PODMAN_VERSION, "podman pin changed; update the decision record"
        assert toolchain.crun.version == PINNED_CRUN_VERSION, "crun pin changed; update the decision record"
        assert toolchain.conmon.version == PINNED_CONMON_VERSION, "conmon pin changed; update the decision record"
        assert {"podman", "crun", "conmon"} <= set(provenance["binaries"])
        assert codex_binary.version == PINNED_CODEX_VERSION
        assert len(codex_binary.sha256) == 64
        assert codex_binary.path.is_file()
        assert codex_inventory.required_methods == frozenset(
            {"thread/start", "turn/start", "thread/read", "turn/interrupt"}
        )
        assert codex_inventory.generated_param_files > 0


class TestNativeLifecycle:
    def test_startup_handshake_thread_and_turn_inside_oci(self, runner: AttemptRunner, tmp_path: Path) -> None:
        attempt = runner.start(
            [
                exec_step("echo native-exec-ok && printf 'written-in-boundary' > /workspace/exec-marker.txt"),
                final_step("lifecycle turn complete"),
            ]
        )
        thread_id = attempt.thread_start()
        assert thread_id
        turn_id = attempt.start_turn("run the synthetic lifecycle step")
        status = attempt.wait_turn(turn_id)
        assert status == "completed", attempt.turns[turn_id].statuses_seen
        # Native tool path executed inside the boundary: commandExecution item observed.
        commands = _command_execution_commands(attempt)
        assert any("native-exec-ok" in command for command in commands), commands
        # The in-boundary launcher observed the OCI runtime context before exec'ing codex.
        observations = attempt.worker_observations()
        assert observations.get("launching") == "codex app-server"
        self_observations = observations["self_observations"]
        assert self_observations["CapEff"] == "0000000000000000"
        assert self_observations["NoNewPrivs"] == "1"
        assert self_observations["Seccomp"] == "2"
        assert len(self_observations["uid_map"].split()) == 3, self_observations["uid_map"]
        # Native thread/turn lifecycle is durable in the synthetic CODEX_HOME rollout record.
        rollouts = list((attempt.resources.codex_home / "sessions").rglob("rollout-*.jsonl"))
        assert rollouts, "native session rollout missing from synthetic CODEX_HOME"
        rollout_text = rollouts[0].read_text()
        assert thread_id in rollout_text
        assert (attempt.resources.workspace / "exec-marker.txt").read_text() == "written-in-boundary"
        attempt.dump_native_evidence(tmp_path / "evidence-lifecycle.json")


RELAY_CALL_HELPER = """import json
import socket
import sys

request_id, grant_id = sys.argv[1], sys.argv[2]
request = {"request_id": request_id, "grant": grant_id, "op": "synthetic:effect"}
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(5)
try:
    sock.connect("/relay/effects.sock")
    sock.sendall(json.dumps(request).encode() + b"\\n")
    try:
        reply = sock.recv(4096)
    except TimeoutError:
        reply = b""
    outcome = json.loads(reply.decode()) if reply else {"result": "reply_lost"}
except OSError as error:
    outcome = {"result": f"connect_failed:{error.__class__.__name__}"}
with open(f"/workspace/relay-{request_id}.json", "w") as handle:
    handle.write(json.dumps(outcome, sort_keys=True) + "\\n")
"""


def _workspace_json(attempt: CodexAttempt, name: str) -> dict[str, Any]:
    return json.loads((attempt.resources.workspace / name).read_text())


class TestWorkspaceTool:
    def test_native_tool_reads_and_modifies_only_candidate_workspace(
        self, runner: AttemptRunner, tmp_path: Path
    ) -> None:
        attempt = runner.start(
            [
                exec_step(
                    "cat /workspace/source-input.txt > /workspace/candidate-copy.txt"
                    " && printf 'tool-appended' >> /workspace/candidate-copy.txt"
                    " && cat /workspace/candidate-copy.txt"
                ),
                exec_step("touch /bin/forbidden-rootfs-write 2>&1; echo rc=$?"),
                final_step("workspace turn complete"),
            ]
        )
        (attempt.resources.workspace / "source-input.txt").write_text("seeded-synthetic-source\n")
        attempt.thread_start()
        turn_id = attempt.start_turn("perform the synthetic workspace operation")
        assert attempt.wait_turn(turn_id) == "completed", attempt.turns[turn_id].statuses_seen
        candidate = attempt.resources.workspace / "candidate-copy.txt"
        assert candidate.read_text() == "seeded-synthetic-source\ntool-appended"
        assert (attempt.resources.workspace / "source-input.txt").read_text() == "seeded-synthetic-source\n"
        # The read-only rootfs refused the native write outside the candidate workspace:
        # the observed command output carries the failed touch, and /bin is unchanged.
        denial_text = " ".join(str(item.get("aggregatedOutput", "")) for item in _command_execution_items(attempt))
        assert "forbidden-rootfs-write" in denial_text, denial_text
        assert "Read-only file system" in denial_text or "rc=1" in denial_text, denial_text
        attempt.dump_native_evidence(tmp_path / "evidence-workspace.json")


class TestNativeTerminalAndSettlement:
    def test_direct_native_terminal_without_model(self, runner: AttemptRunner, tmp_path: Path) -> None:
        attempt = runner.start([final_step("unused")])
        attempt.thread_start()
        result = attempt.command_exec(
            ["/toolchain/bin/python3.12", "-c", "print('direct-terminal-ok')"], cwd="/workspace"
        )
        assert result.get("exitCode") == 0, result
        assert "direct-terminal-ok" in str(result.get("stdout", "")), result
        denial = attempt.command_exec(["/toolchain/bin/python3.12", "-c", "open('/bin/x','w')"])
        assert denial.get("exitCode") != 0, denial
        assert "direct-terminal-ok" not in str(denial.get("stdout", ""))
        attempt.dump_native_evidence(tmp_path / "evidence-terminal.json")

    def test_descendant_survives_interrupt_then_settles_at_stop_and_bystander_survives(
        self, runner: AttemptRunner
    ) -> None:
        bystander = _spawn_bystander()
        try:
            attempt = runner.start(
                [
                    exec_step(
                        'setsid /toolchain/bin/python3.12 -c "import time; time.sleep(180)'
                        '  # KERNEL53-DESC-{attempt}" &'
                    ),
                    exec_step("touch /workspace/in-flight.txt && sleep 45"),
                ]
            )
            marker = f"KERNEL53-DESC-{attempt.attempt}"
            attempt.thread_start()
            turn_id = attempt.start_turn("spawn the descendant then hold the turn")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (attempt.resources.workspace / "in-flight.txt").exists():
                time.sleep(0.3)
            assert (attempt.resources.workspace / "in-flight.txt").exists(), "exec never became in-flight"
            assert _wait_for_marker(marker), "detached descendant never became host-visible"
            assert harness.scan_host_for_marker(BYSTANDER_MARKER), "bystander missing before stop"
            attempt.interrupt(turn_id)
            assert attempt.wait_turn(turn_id) == "interrupted", attempt.turns[turn_id].statuses_seen
            # An interrupt is not settlement: the detached descendant is still alive in the boundary.
            assert attempt.scan_marker(marker), "descendant vanished at interrupt without observed settlement"
            attempt.stop(grace_seconds=5)
            settled, stragglers = attempt.wait_settled(marker)
            assert settled, f"descendants survived container stop: {stragglers}"
            assert attempt.podman_state() == "absent"
            assert harness.scan_host_for_marker(BYSTANDER_MARKER), "unrelated bystander was killed"
            attempt.terminal = "cancelled-observed"
            attempt.close()
        finally:
            bystander.terminate()
            try:
                bystander.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bystander.kill()


def _network_probe_script() -> str:
    return """import json
import socket

results = {}

def probe(label, fn):
    try:
        fn()
        results[label] = "connected"
    except OSError as error:
        results[label] = f"denied:{error.__class__.__name__}"

def tcp(host, port, family=socket.AF_INET):
    def run():
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect((host, port))
    return run

probe("ipv4-private", tcp("10.240.0.1", 59999))
probe("ipv6-loopback", tcp("::1", 59999, socket.AF_INET6))
try:
    socket.getaddrinfo("forgejo.creatidy.com", 443)
    results["dns"] = "resolved"
except OSError as error:
    results["dns"] = f"denied:{error.__class__.__name__}"
with open("/workspace/network-probes.json", "w") as handle:
    handle.write(json.dumps(results, sort_keys=True) + "\\n")
"""


class TestHostBoundaryDenial:
    def test_forbidden_host_paths_and_network_denied_without_any_approval(
        self, runner: AttemptRunner, tmp_path: Path
    ) -> None:
        host_dir = tmp_path / "host-only"
        host_dir.mkdir()
        canary = host_dir / "canary.txt"
        canary.write_text("host-synthetic-canary")
        attempt = runner.start(
            [
                exec_step(
                    "cat /etc/ssh/sshd_config > /workspace/fs-probe-etcssh.txt 2>&1;"
                    " echo rc=$? >> /workspace/fs-probe-etcssh.txt"
                ),
                exec_step(
                    f"cat {tmp_path}/host-only/canary.txt > /workspace/fs-probe-canary.txt 2>&1;"
                    " echo rc=$? >> /workspace/fs-probe-canary.txt"
                ),
                exec_step(
                    "ls /home/adrian > /workspace/fs-probe-home.txt 2>&1; echo rc=$? >> /workspace/fs-probe-home.txt"
                ),
                exec_step("/toolchain/bin/python3.12 /workspace/network-probe.py"),
                final_step("denial turn complete"),
            ],
            seed_files={"network-probe.py": _network_probe_script()},
        )
        attempt.thread_start()
        turn_id = attempt.start_turn("probe forbidden host surfaces")
        assert attempt.wait_turn(turn_id) == "completed", attempt.turns[turn_id].statuses_seen

        etcssh = (attempt.resources.workspace / "fs-probe-etcssh.txt").read_text()
        assert "rc=1" in etcssh, etcssh  # No such file or directory: /etc/ssh is not mounted.
        canary_probe = (attempt.resources.workspace / "fs-probe-canary.txt").read_text()
        assert "rc=1" in canary_probe, canary_probe
        home_probe = (attempt.resources.workspace / "fs-probe-home.txt").read_text()
        assert "rc=1" in home_probe or "rc=2" in home_probe, home_probe
        # Host bytes are unchanged: the denials are real, not responses over a writable path.
        assert canary.read_text() == "host-synthetic-canary"

        probes = _workspace_json(attempt, "network-probes.json")
        assert probes["ipv4-private"].startswith("denied:"), probes
        assert probes["ipv6-loopback"].startswith("denied:"), probes
        assert probes["dns"].startswith("denied:"), probes
        observations = attempt.worker_observations()["self_observations"]
        assert observations["network_interfaces"] == ["lo"], observations["network_interfaces"]
        assert len(observations["ipv4_routes"]) == 1, "unexpected non-loopback IPv4 route"
        attempt.dump_native_evidence(tmp_path / "evidence-denials.json")


class TestControllerAuthority:
    def test_relay_grants_replays_revocations_expiry_then_controller_refusal(
        self, runner: AttemptRunner, tmp_path: Path
    ) -> None:
        attempt = runner.start(
            [
                exec_step("/toolchain/bin/python3.12 /workspace/relay-call.py r1 g-ok"),
                exec_step("/toolchain/bin/python3.12 /workspace/relay-call.py r2 g-ok"),
                final_step("authority turn one complete"),
                exec_step("/toolchain/bin/python3.12 /workspace/relay-call.py r6 g-lost"),
                exec_step("/toolchain/bin/python3.12 /workspace/relay-call.py r3 g-revoked"),
                exec_step("/toolchain/bin/python3.12 /workspace/relay-call.py r4 g-unknown"),
                exec_step("/toolchain/bin/python3.12 /workspace/relay-call.py r5 g-ttl"),
                final_step("authority turn two complete"),
            ],
            grants=(
                ("g-ok", "synthetic:effect"),
                ("g-revoked", "synthetic:effect"),
                ("g-ttl", "synthetic:effect"),
                ("g-lost", "synthetic:effect"),
            ),
            seed_files={"relay-call.py": RELAY_CALL_HELPER},
        )
        attempt.relay.expire("g-ttl")
        attempt.thread_start()
        turn_one = attempt.start_turn("authority turn one")
        assert attempt.wait_turn(turn_one) == "completed", attempt.turns[turn_one].statuses_seen
        assert _workspace_json(attempt, "relay-r1.json")["result"] == "executed"
        assert _workspace_json(attempt, "relay-r2.json") == {
            "result": "refused",
            "reason": "grant_already_consumed",
            "request_id": "r2",
        }
        attempt.revoke("g-revoked")
        attempt.relay.drop_next_reply()
        turn_two = attempt.start_turn("authority turn two")
        assert attempt.wait_turn(turn_two) == "completed", attempt.turns[turn_two].statuses_seen
        assert _workspace_json(attempt, "relay-r3.json") == {
            "result": "refused",
            "reason": "grant_revoked",
            "request_id": "r3",
        }
        assert _workspace_json(attempt, "relay-r4.json")["reason"] == "unknown_grant"
        assert _workspace_json(attempt, "relay-r5.json")["reason"] == "grant_expired"
        # The dropped reply leaves the effect uncertain to the worker while the relay receipt
        # records it as executed; the receipt log, not worker belief, is reconciliation truth.
        assert _workspace_json(attempt, "relay-r6.json") == {"result": "reply_lost"}
        executed = [event for event in attempt.relay_events() if event.kind == "effect" and event.grant_id == "g-lost"]
        lost = [event for event in attempt.relay_events() if event.kind == "lost" and event.grant_id == "g-lost"]
        assert executed and lost, (executed, lost)

        # After controller expiry/revocation no new external operation is dispatched at all.
        attempt.expire()
        with pytest.raises(AttemptExpired):
            attempt.start_turn("must be refused after expiry")
        with pytest.raises(AttemptExpired):
            attempt.command_exec(["/bin/true"])
        attempt.dump_native_evidence(tmp_path / "evidence-authority.json")


class TestRecovery:
    def test_monitor_death_leaves_state_unknown_until_reconciled(self, runner: AttemptRunner) -> None:
        attempt = runner.start(
            [exec_step("touch /workspace/in-flight.txt && sleep 60"), final_step("never reached")],
        )
        attempt.thread_start()
        turn_id = attempt.start_turn("hold while the monitor dies")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (attempt.resources.workspace / "in-flight.txt").exists():
            time.sleep(0.3)
        assert (attempt.resources.workspace / "in-flight.txt").exists()
        monitors = [
            pid
            for pid in attempt.scan_marker(attempt.container)
            if "podman" in _proc_cmdline(pid) and "conmon" not in _proc_cmdline(pid)
        ]
        assert monitors, "podman attach process not found for the owned container"
        for pid in monitors:
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
        # The transport is dead: the next native request fails closed instead of inventing state.
        with pytest.raises((OSError, TimeoutError, CodexRPCError)):
            attempt.turn_status(turn_id)
        # Terminality is unknown until reconciled by observation; existence is not liveness.
        state = attempt.podman_state()
        assert state in {"exists", "absent"}, state
        if state == "exists":
            attempt.stop(grace_seconds=2)
        assert attempt.podman_state() == "absent"
        assert (attempt.resources.workspace / "in-flight.txt").exists(), "workspace not retained across recovery"
        attempt.terminal = "reconciled-after-monitor-loss"
        attempt.close()


class TestCollection:
    def test_immutable_collection_and_independent_verification(self, runner: AttemptRunner, tmp_path: Path) -> None:
        attempt = runner.start(
            [
                exec_step(
                    "printf 'synthetic-candidate-bytes' > /workspace/candidate.txt"
                    " && /toolchain/bin/python3.12 -c"
                    ' "import hashlib;'
                    "print(hashlib.sha256(open('/workspace/candidate.txt','rb').read()).hexdigest())\""
                    " > /workspace/candidate.sha256 2>&1"
                ),
                final_step("collection turn complete"),
            ]
        )
        attempt.thread_start()
        turn_id = attempt.start_turn("produce the synthetic candidate")
        assert attempt.wait_turn(turn_id) == "completed", attempt.turns[turn_id].statuses_seen
        attempt.stop(grace_seconds=5)
        assert attempt.podman_state() == "absent"
        attempt.close()

        manifest = attempt.collect_manifest()
        assert "candidate.txt" in manifest and "candidate.sha256" in manifest
        assert "codex-home/config.toml" in manifest, sorted(manifest)[:10]
        in_container_digest = (attempt.resources.workspace / "candidate.sha256").read_text().strip()
        host_digest = manifest["candidate.txt"]["sha256"]
        assert in_container_digest == host_digest, "independent digests disagree"
        rollouts = list((attempt.resources.codex_home / "sessions").rglob("rollout-*.jsonl"))
        assert rollouts
        rollout_text = rollouts[0].read_text()
        assert turn_id in rollout_text and "candidate.txt" in rollout_text
        attempt.dump_native_evidence(tmp_path / "evidence-collection.json")
        evidence = json.loads((tmp_path / "evidence-collection.json").read_text())
        assert evidence["attempt"] == attempt.attempt
        assert any(record["name"] == "codex" for record in evidence["tools"])


APPEND_LOOP = """import time
from pathlib import Path

path = Path("/workspace/held.bin")
chunk = b"x" * 4096
with path.open("ab") as handle:
    handle.write(b"start\\n")
    handle.flush()
    for _ in range(100):
        handle.write(chunk)
        handle.flush()
        time.sleep(0.2)
"""


class TestInFlightEffects:
    def test_inflight_native_writes_persist_and_terminate_but_never_roll_back(self, runner: AttemptRunner) -> None:
        attempt = runner.start(
            [exec_step("/toolchain/bin/python3.12 /workspace/append-loop.py"), final_step("unreached")],
            seed_files={"append-loop.py": APPEND_LOOP},
        )
        attempt.thread_start()
        attempt.start_turn("stream synthetic bytes to the workspace")
        held = attempt.resources.workspace / "held.bin"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if held.exists() and held.stat().st_size >= 8192:
                break
            time.sleep(0.2)
        assert held.exists() and held.stat().st_size >= 8192, "native exec never streamed to the workspace"
        size_at_stop = held.stat().st_size
        attempt.stop(grace_seconds=0)
        final_size = held.stat().st_size
        # Termination bounds the future and never retracts the past: the workspace bind is
        # static, so bytes already written (and any flush that raced the kill) persist.
        assert final_size >= size_at_stop, "workspace bytes were rolled back after termination"
        assert final_size < 100 * 4096, "unexpectedly complete stream; termination did not bound the writes"
        attempt.terminal = "terminated-with-inflight-writes"
        attempt.close()
