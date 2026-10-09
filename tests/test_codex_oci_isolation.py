# SPDX-License-Identifier: Apache-2.0
"""Native #53 reception: the real Codex app-server inside the #78 rootless OCI boundary.

Set CREATIDY_TEST_OCI_TOOLROOT to the #78 toolroot to run native reception; a real Codex
binary must resolve (CREATIDY_TEST_CODEX_BIN or PATH) at the pinned version. Missing setup is
explicitly UNPROVED, never a passing receipt: every skip below means the real-harness boundary
was not exercised, not that isolation holds. Payloads are synthetic; no owner Codex state, no
credentials, no live model and no paid inference participate. Each Attempt records observed
outcomes into an evidence file beside its disposable workspace.
"""

import hashlib
import json
import os
import signal
import stat
import subprocess  # noqa: S603 - every call below uses fixed synthetic argv, no shell.
import sys
import time
from pathlib import Path, PurePosixPath
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
from tools.oci_worker_poc.profile import FORBIDDEN_BIND_PREFIXES
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


def _read_worker_file(attempt: CodexAttempt, relative: str) -> bytes:
    """Fail-closed read of one worker-controlled workspace file (test evidence plumbing).

    The worker controls every entry of its writable workspace, so test-side reads use the
    same trust boundary as collection: no symlink following, regular files only, bounded.
    """
    from tools.codex_oci_proof.collect import read_bounded_regular

    return read_bounded_regular(attempt.resources.workspace / relative)


def _worker_file_text(attempt: CodexAttempt, relative: str) -> str:
    return _read_worker_file(attempt, relative).decode()


def _wait_for_worker_file(attempt: CodexAttempt, relative: str, timeout: float = 30.0) -> bool:
    """Wait until the entry exists as a regular file (lstat: symlinks never count)."""
    deadline = time.monotonic() + timeout
    target = attempt.resources.workspace / relative
    while time.monotonic() < deadline:
        try:
            if stat.S_ISREG(os.lstat(target).st_mode):
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def _worker_file_size(attempt: CodexAttempt, relative: str) -> int | None:
    try:
        st = os.lstat(attempt.resources.workspace / relative)
    except OSError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def _rollout_texts(attempt: CodexAttempt) -> list[str]:
    """Fail-closed native-session-rollout reads under the worker-controlled codex-home.

    The bytes are captured through the collector's pinned descriptors via its sink —
    enumerated and read in one fd-pinned pass, with no path re-resolution afterward —
    so no worker-controlled path component is ever resolved by the kernel a second time.
    """
    from tools.codex_oci_proof.collect import CollectedFile, collect_directory

    texts: list[str] = []

    def sink(relative: str, data: bytes, record: CollectedFile) -> None:
        if relative.startswith("sessions/") and "rollout-" in relative and relative.endswith(".jsonl"):
            texts.append(data.decode())

    collect_directory(attempt.resources.codex_home, sink=sink)
    return texts


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
        # Closed environment: exactly the controller allowlist plus the runtime-injected
        # container basics. A new name means the boundary env closure changed; update the
        # pin deliberately with evidence, never by widening silently.
        expected_env = {
            "PATH",
            "PYTHONHOME",
            "LD_LIBRARY_PATH",
            "LANG",
            "LC_ALL",
            "ATTEMPT_ID",
            "HOME",
            "TMPDIR",
            "SHELL",
            "CODEX_HOME",
        }
        runtime_injected = {"HOSTNAME", "PWD", "SHLVL", "container"}
        observed_env = set(self_observations["env_names"])
        assert observed_env == expected_env | runtime_injected, (
            observed_env - (expected_env | runtime_injected),
            (expected_env | runtime_injected) - observed_env,
        )
        # The cgroup bounds travel with the attempt (default budget 256 pids / 1 GiB).
        assert self_observations["cgroup_pids_max"] == "256", self_observations["cgroup_pids_max"]
        assert self_observations["cgroup_memory_max"] == "1073741824", self_observations["cgroup_memory_max"]
        # Single-UID mapping and the mount allowlist are the properties the decision record
        # cites: container root maps 1:1 onto the invoking host uid, and no mount point lies
        # at or under a forbidden host prefix — except the profile's own synthetic HOME
        # tmpfs at /home/worker (controller-issued, host-independent), the only intended
        # mount under a forbidden prefix.
        uid_map = self_observations["uid_map"].split()
        assert len(uid_map) == 3 and uid_map[2] == "1", self_observations["uid_map"]
        synthetic_home = PurePosixPath("/home/worker")
        for line in self_observations["mounts"]:
            fields = line.split()
            if len(fields) < 2:
                continue
            point = PurePosixPath(fields[1])
            if point == synthetic_home or synthetic_home in point.parents:
                continue
            for prefix in FORBIDDEN_BIND_PREFIXES:
                forbidden = PurePosixPath(prefix)
                assert point != forbidden and forbidden not in point.parents, f"forbidden mount leaked: {line}"
        # Native thread/turn lifecycle is durable in the synthetic CODEX_HOME rollout record.
        rollouts = _rollout_texts(attempt)
        assert rollouts, "native session rollout missing from synthetic CODEX_HOME"
        assert thread_id in rollouts[0]
        assert _worker_file_text(attempt, "exec-marker.txt") == "written-in-boundary"
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
    return json.loads(_read_worker_file(attempt, name))


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
        assert _worker_file_text(attempt, "candidate-copy.txt") == "seeded-synthetic-source\ntool-appended"
        assert _worker_file_text(attempt, "source-input.txt") == "seeded-synthetic-source\n"
        # The read-only rootfs refused the native write outside the candidate workspace:
        # the observed command output carries the EROFS failure text, not a bare nonzero rc
        # (the container is gone after the run, so /bin itself can only be asserted inside).
        denial_text = " ".join(str(item.get("aggregatedOutput", "")) for item in _command_execution_items(attempt))
        assert "forbidden-rootfs-write" in denial_text, denial_text
        assert "Read-only file system" in denial_text, denial_text
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
            assert _wait_for_worker_file(attempt, "in-flight.txt"), "exec never became in-flight"
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
                    "ls /home/unmounted-probe > /workspace/fs-probe-home.txt 2>&1;"
                    " echo rc=$? >> /workspace/fs-probe-home.txt"
                ),
                exec_step("/toolchain/bin/python3.12 /workspace/network-probe.py"),
                final_step("denial turn complete"),
            ],
            seed_files={"network-probe.py": _network_probe_script()},
        )
        attempt.thread_start()
        turn_id = attempt.start_turn("probe forbidden host surfaces")
        assert attempt.wait_turn(turn_id) == "completed", attempt.turns[turn_id].statuses_seen

        etcssh = _worker_file_text(attempt, "fs-probe-etcssh.txt")
        assert "rc=1" in etcssh, etcssh  # No such file or directory: /etc/ssh is not mounted.
        canary_probe = _worker_file_text(attempt, "fs-probe-canary.txt")
        assert "rc=1" in canary_probe, canary_probe
        home_probe = _worker_file_text(attempt, "fs-probe-home.txt")
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
        assert _wait_for_worker_file(attempt, "in-flight.txt")
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
        # Either observed pre-stop state is legitimate (the container may or may not still
        # be running when the monitor dies); only the post-reconciliation state asserts.
        state = attempt.podman_state()
        if state == "exists":
            attempt.stop(grace_seconds=2)
        assert attempt.podman_state() == "absent"
        assert _worker_file_size(attempt, "in-flight.txt") is not None, "workspace not retained across recovery"
        attempt.terminal = "reconciled-after-monitor-loss"
        attempt.close()


class TestCollection:
    def test_candidate_snapshot_private_state_and_independent_verification(
        self, runner: AttemptRunner, tmp_path: Path
    ) -> None:
        attempt = runner.start(
            [
                exec_step(
                    "printf 'synthetic-candidate-bytes' > /workspace/candidate/candidate.txt"
                    " && /toolchain/bin/python3.12 -c"
                    ' "import hashlib;'
                    "print(hashlib.sha256(open('/workspace/candidate/candidate.txt','rb').read()).hexdigest())\""
                    " > /workspace/candidate/candidate.sha256 2>&1"
                ),
                final_step("collection turn complete"),
            ]
        )
        attempt.thread_start()
        turn_id = attempt.start_turn("produce the synthetic candidate")
        assert attempt.wait_turn(turn_id) == "completed", attempt.turns[turn_id].statuses_seen
        # Collection refuses to race a live worker: settlement is a precondition.
        with pytest.raises(RuntimeError, match="settlement"):
            attempt.collect()
        attempt.stop(grace_seconds=5)
        assert attempt.podman_state() == "absent"
        attempt.close()
        collection = attempt.collect()

        # Publishable = exactly the controller-declared candidate root, with exact bytes
        # preserved under the controller-owned collected/ tree and digest-bound.
        assert set(collection.publishable.files) == {"candidate.txt", "candidate.sha256"}
        in_container_digest = _worker_file_text(attempt, "candidate/candidate.sha256").strip()
        record = collection.publishable.files["candidate.txt"]
        assert record.sha256 == in_container_digest, "independent digests disagree"
        stored = attempt.collected_root / "candidate.txt"
        assert stored.read_bytes() == b"synthetic-candidate-bytes"
        from tools.codex_oci_proof.collect import sha256_file

        assert sha256_file(stored) == record.sha256, "stored copy is not bound to the record"

        # Private runtime/evidence state is inventoried separately and never published.
        assert "codex-home/config.toml" in collection.private.files, sorted(collection.private.files)[:12]
        assert any(path.startswith("codex-home/sessions/") for path in collection.private.files)
        assert "mock/state.json" in collection.private.files
        assert "attempt.json" in collection.private.files and "worker-observations.json" in collection.private.files
        assert "candidate.txt" not in collection.private.files, "candidate leaked into private inventory"

        rollouts = _rollout_texts(attempt)
        assert rollouts
        assert turn_id in rollouts[0] and "candidate.txt" in rollouts[0]
        attempt.dump_native_evidence(tmp_path / "evidence-collection.json")
        evidence = json.loads((tmp_path / "evidence-collection.json").read_text())
        assert evidence["attempt"] == attempt.attempt
        assert any(record_["name"] == "codex" for record_ in evidence["tools"])

    def test_worker_created_symlinks_refused_and_host_target_never_collected(
        self, runner: AttemptRunner, tmp_path: Path
    ) -> None:
        secret_dir = tmp_path / "host-only-secret"
        secret_dir.mkdir()
        secret = secret_dir / "controller-only.txt"
        secret_bytes = b"SYNTHETIC-CONTROLLER-SECRET-9f2c7"
        secret.write_bytes(secret_bytes)
        secret_digest = hashlib.sha256(secret_bytes).hexdigest()
        attempt = runner.start(
            [
                exec_step("printf 'legitimate-candidate-bytes' > /workspace/candidate/keep.txt"),
                exec_step(
                    f"ln -s {secret_dir} /workspace/candidate/leak-dir-link"
                    f" && ln -s {secret} /workspace/candidate/leak-file-link"
                    " && ln -s /workspace/mock /workspace/candidate/runtime-link"
                    " && ln -s /workspace/candidate/keep.txt /workspace/candidate/self-link"
                    " && ln -s /nonexistent-target /workspace/candidate/dangling-link"
                    " && mkdir -p /workspace/candidate/inner && ln -s /etc /workspace/candidate/inner/escape"
                ),
                final_step("symlink turn complete"),
            ]
        )
        attempt.thread_start()
        turn_id = attempt.start_turn("create worker-side symlinks then finish")
        assert attempt.wait_turn(turn_id) == "completed", attempt.turns[turn_id].statuses_seen
        attempt.stop(grace_seconds=5)
        assert attempt.podman_state() == "absent"
        attempt.close()

        collection = attempt.collect()
        # Only the regular candidate file survives; every worker-created symlink is
        # refused by name with an explicit reason.
        assert set(collection.publishable.files) == {"keep.txt"}, sorted(collection.publishable.files)
        refusals = collection.publishable.refusals
        for name in ("leak-dir-link", "leak-file-link", "runtime-link", "self-link", "dangling-link"):
            assert refusals.get(name) == "symlink-refused", (name, refusals.get(name))
        assert "escape" not in collection.publishable.files
        assert refusals.get("inner/escape") == "symlink-refused"
        # The prohibited host target was never opened: its digest appears nowhere in any
        # collected record or preserved byte, and the preserved tree holds no link entry.
        assert secret_digest not in collection.publishable.digests()
        assert secret_digest not in collection.private.digests()
        assert not (attempt.collected_root / "leak-file-link").exists()
        assert not (attempt.collected_root / "leak-dir-link").exists()
        assert secret.read_bytes() == secret_bytes, "host target changed"
        attempt.dump_native_evidence(tmp_path / "evidence-symlink-collection.json")


APPEND_LOOP = """import time
from pathlib import Path, PurePosixPath

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
        deadline = time.monotonic() + 30
        size_at_stop: int | None = None
        while time.monotonic() < deadline:
            size_at_stop = _worker_file_size(attempt, "held.bin")
            if size_at_stop is not None and size_at_stop >= 8192:
                break
            time.sleep(0.2)
        assert size_at_stop is not None and size_at_stop >= 8192, "native exec never streamed to the workspace"
        attempt.stop(grace_seconds=0)
        final_size = _worker_file_size(attempt, "held.bin")
        assert final_size is not None
        # Termination bounds the future and never retracts the past: the workspace bind is
        # static, so bytes already written (and any flush that raced the kill) persist.
        assert final_size >= size_at_stop, "workspace bytes were rolled back after termination"
        assert final_size < 100 * 4096, "unexpectedly complete stream; termination did not bound the writes"
        attempt.terminal = "terminated-with-inflight-writes"
        attempt.close()


class TestCollectionSafety:
    """Host-side fail-closed collection behavior: runs without the OCI toolroot.

    The collector treats everything under its root as worker-controlled: a synthetic
    tree exercises the same primitives the native collection uses, so symlink/refusal
    behavior is gated even where the OCI receipt cannot run.
    """

    def test_symlinks_fifo_oversize_and_budget_are_refused_with_targets_unread(self, tmp_path: Path) -> None:
        from tools.codex_oci_proof.collect import MAX_FILE_BYTES, collect_directory

        root = tmp_path / "workspace-candidate"
        secret = tmp_path / "host-secret" / "controller-only.txt"
        secret.parent.mkdir()
        secret.write_bytes(b"SYNTHETIC-CONTROLLER-SECRET-BYTES")
        root.mkdir(parents=True)
        (root / "keep.txt").write_bytes(b"legitimate")
        (root / "leak-file-link").symlink_to(secret)
        (root / "leak-dir-link").symlink_to(secret.parent)
        (root / "dangling-link").symlink_to(tmp_path / "nonexistent-target")
        (root / "inner").mkdir()
        (root / "inner" / "escape-dir-link").symlink_to("/")
        (root / "inner" / "inner.txt").write_bytes(b"behind-nothing")
        os.mkfifo(root / "named-pipe")
        (root / "oversize.bin").write_bytes(b"x" * (MAX_FILE_BYTES + 1))

        collection = collect_directory(root)
        assert set(collection.files) == {"keep.txt", "inner/inner.txt"}, sorted(collection.files)
        assert collection.refusals["leak-file-link"] == "symlink-refused"
        assert collection.refusals["leak-dir-link"] == "symlink-refused"
        assert collection.refusals["dangling-link"] == "symlink-refused"
        assert collection.refusals["inner/escape-dir-link"] == "symlink-refused"
        assert collection.refusals["named-pipe"] == "not-regular-file"
        assert collection.refusals["oversize.bin"] == "file-exceeds-size-bound"
        assert hashlib.sha256(secret.read_bytes()).hexdigest() not in collection.digests()

    def test_root_contract_failures_raise(self, tmp_path: Path) -> None:
        from tools.codex_oci_proof.collect import collect_directory

        real = tmp_path / "real-root"
        real.mkdir()
        (real / "f.txt").write_bytes(b"data")
        link = tmp_path / "link-root"
        link.symlink_to(real)
        plain_file = tmp_path / "plain-file"
        plain_file.write_bytes(b"not a dir")
        with pytest.raises(ValueError, match="symbolic link"):
            collect_directory(link)
        with pytest.raises(ValueError, match="does not exist"):
            collect_directory(tmp_path / "missing-root")
        with pytest.raises(ValueError, match="not a directory"):
            collect_directory(plain_file)

    def test_entry_budget_stops_traversal(self, tmp_path: Path) -> None:
        from tools.codex_oci_proof import collect as collect_module
        from tools.codex_oci_proof.collect import collect_directory

        root = tmp_path / "budget-root"
        root.mkdir()
        for index in range(collect_module.MAX_ENTRIES + 10):
            (root / f"f{index:05d}.txt").write_bytes(b"x")
        collection = collect_directory(root)
        assert len(collection.files) == collect_module.MAX_ENTRIES
        assert len(collection.refusals) == 1
        assert "entry-budget-exhausted" in collection.refusals.values()

    def test_read_bound_holds_when_file_grows_after_open(self, tmp_path: Path) -> None:
        from tools.codex_oci_proof import collect as collect_module

        grown = tmp_path / "grows.bin"
        grown.write_bytes(b"x" * 4096)  # fstat saw a small file; the read then finds more
        fd = os.open(grown, os.O_RDONLY)
        try:
            assert collect_module.read_bounded_fd(fd, 0, 1024) is None
            os.lseek(fd, 0, os.SEEK_SET)
            assert collect_module.read_bounded_fd(fd, 4096, 8192) == b"x" * 4096
            os.lseek(fd, 0, os.SEEK_SET)
            assert collect_module.read_bounded_fd(fd, 8193, 8192) is None
        finally:
            os.close(fd)
        from tools.codex_oci_proof.collect import read_bounded_regular

        with pytest.raises(ValueError, match="read bound"):
            read_bounded_regular(grown, limit=1024)

    def test_traversal_depth_and_directory_budget_are_bounded(self, tmp_path: Path) -> None:
        from tools.codex_oci_proof import collect as collect_module
        from tools.codex_oci_proof.collect import collect_directory

        deep = tmp_path / "deep-root"
        current = deep
        deep.mkdir(parents=True)
        for _ in range(collect_module.MAX_DEPTH + 10):
            current = current / "down"
            current.mkdir()
        (current / "bottom.txt").write_bytes(b"beyond the cap")
        collection = collect_directory(deep)
        assert "max-depth-exceeded" in collection.refusals.values()
        assert all(not path.endswith("bottom.txt") for path in collection.files)

        wide = tmp_path / "wide-root"
        wide.mkdir()
        for index in range(1500):
            directory = wide / f"d{index:04d}"
            directory.mkdir()
            (directory / "inner.txt").write_bytes(b"x")
            (wide / f"t{index:04d}.txt").write_bytes(b"x")
        collection = collect_directory(wide)
        assert collection.consumed <= collect_module.MAX_ENTRIES
        assert len(collection.files) + len(collection.refusals) <= collect_module.MAX_ENTRIES
        assert "entry-budget-exhausted" in collection.refusals.values()

    def test_bounded_regular_reader_refuses_symlink_and_oversize(self, tmp_path: Path) -> None:
        from tools.codex_oci_proof.collect import read_bounded_regular

        secret = tmp_path / "controller-only.txt"
        secret.write_bytes(b"secret")
        link = tmp_path / "worker-link"
        link.symlink_to(secret)
        with pytest.raises(ValueError, match="symlinked"):
            read_bounded_regular(link)
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 2049)
        with pytest.raises(ValueError, match="bound"):
            read_bounded_regular(big, limit=2048)
        assert (
            read_bounded_regular(
                tmp_path / "ok.bin" if (tmp_path / "ok.bin").write_bytes(b"ok") is None else tmp_path / "ok.bin"
            )
            == b"ok"
        )

    def test_stored_sink_binds_bytes_and_rejects_mismatch(self, tmp_path: Path) -> None:
        import dataclasses

        from tools.codex_oci_proof.collect import CollectedFile, stored_bytes_sink

        stored_root = tmp_path / "collected"
        sink = stored_bytes_sink(stored_root)
        record = CollectedFile(path="sub/artifact.txt", size=4, sha256=hashlib.sha256(b"data").hexdigest())
        sink("sub/artifact.txt", b"data", record)
        stored = stored_root / "sub" / "artifact.txt"
        assert stored.read_bytes() == b"data"
        with pytest.raises(RuntimeError, match="do not match the record"):
            sink("sub/artifact.txt", b"tampered", record)
        forged = dataclasses.replace(record, sha256="0" * 64)
        with pytest.raises(RuntimeError, match="do not match the record"):
            sink("sub/other.txt", b"data", forged)
