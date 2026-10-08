# SPDX-License-Identifier: Apache-2.0
"""Native whole-worker isolation reception over the user-owned pinned rootless OCI toolroot.

Set CREATIDY_TEST_OCI_TOOLROOT to the provisioned toolroot to run native reception. Missing
setup is explicitly UNPROVED, never a passing denial receipt: every skip below means the
whole-worker boundary was not exercised, not that isolation holds. Payloads are synthetic;
no real credentials, private state, live model or remote effect is used. Each attempt
records observed outcomes into an evidence file under its temporary directory.
"""

import dataclasses
import json
import os
import signal
import socket
import subprocess  # noqa: S603 - every call below uses fixed synthetic argv, no shell.
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from tools.oci_worker_poc import harness
from tools.oci_worker_poc.evidence import ProbeRecord, RunEvidence
from tools.oci_worker_poc.profile import WorkerProfile, build_profile, build_run_argv
from tools.oci_worker_poc.relay import EffectsRelay
from tools.oci_worker_poc.toolchain import Toolchain, read_provenance

TOOLROOT_ENV = "CREATIDY_TEST_OCI_TOOLROOT"
PINNED_PODMAN_VERSION = "podman version 6.1.3"
PINNED_CRUN_VERSION = "crun version 1.30.1"
PINNED_CONMON_VERSION = "conmon version 2.2.1"
BYSTANDER_MARKER = "kernel78-bystander-marker"


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    configured = os.environ.get(TOOLROOT_ENV)
    if configured is None:
        pytest.skip(f"whole-worker isolation UNPROVED: explicit pinned toolroot required ({TOOLROOT_ENV})")
    resolved = harness.resolve_or_unproved()
    if resolved is None:
        pytest.skip(f"whole-worker isolation UNPROVED: toolroot incomplete at {configured}")
    return resolved


@pytest.fixture(scope="session")
def provenance(toolchain: Toolchain) -> dict[str, Any]:
    return read_provenance(toolchain)


class AttemptResult:
    """One synthetic Attempt: resources, relay, launch record and observed outcomes."""

    def __init__(
        self,
        attempt: str,
        sandbox: harness.Sandbox,
        relay: EffectsRelay,
        profile: WorkerProfile,
        argv: list[str],
        evidence: RunEvidence,
        process: subprocess.Popen[bytes],
    ) -> None:
        self.attempt = attempt
        self.sandbox = sandbox
        self.relay = relay
        self.profile = profile
        self.argv = argv
        self.evidence = evidence
        self.process = process
        self.returncode: int | None = None
        self.stderr_tail = ""
        self.status: dict[str, Any] = {}

    @property
    def workspace(self) -> Path:
        return self.sandbox.resources.workspace

    @property
    def steps(self) -> dict[str, Any]:
        return dict(self.status.get("steps", {}))

    def step(self, key: str) -> dict[str, Any]:
        return dict(self.steps.get(key, {}))

    @property
    def observations(self) -> dict[str, Any]:
        return dict(self.status.get("self_observations", {}))

    def finish(self, returncode: int | None, stderr: str) -> None:
        self.returncode = returncode
        self.stderr_tail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
        self.status = harness.worker_status(self.sandbox)
        for probe, observation in self.steps.items():
            self.evidence.probes.append(
                ProbeRecord(
                    probe=probe,
                    expectation="recorded observed outcome",
                    outcome="recorded",
                    observation=json.dumps(observation, default=str)[:400],
                )
            )
        self.evidence.dump(self.sandbox.root / "evidence.json")

    def container_name(self) -> str:
        return f"kernel78-worker-{self.attempt}"


class AttemptRunner:
    """Prepares, launches and disposes synthetic Attempts against the pinned boundary."""

    def __init__(self, toolchain: Toolchain, tmp_path: Path) -> None:
        self.toolchain = toolchain
        self.tmp_path = tmp_path
        self.results: list[AttemptResult] = []

    def start(
        self,
        spec: dict[str, Any],
        *,
        deadline_seconds: int = 90,
        pids_limit: int | None = None,
        source_ro: tuple[Path, ...] = (),
        canary_env: dict[str, str] | None = None,
        grants: tuple[tuple[str, str], ...] = (),
    ) -> AttemptResult:
        attempt = uuid.uuid4().hex[:12]
        sandbox = harness.prepare_sandbox(self.tmp_path, attempt)
        resources = sandbox.resources
        if pids_limit is not None or source_ro:
            resources = dataclasses.replace(
                resources, pids_limit=pids_limit or resources.pids_limit, source_ro=source_ro
            )
        image = harness.ensure_image(self.toolchain, self.tmp_path / "worker-image.tar")
        relay = EffectsRelay(resources.relay_socket, attempt)
        relay.start()
        for grant_id, op in grants:
            relay.grant(grant_id, op)
        profile = build_profile(self.toolchain, resources, sandbox.worker_entry, deadline_seconds)
        argv = build_run_argv(self.toolchain, profile, image.reference, f"kernel78-worker-{attempt}")
        harness.write_attempt_spec(sandbox, spec, deadline_seconds)
        env = dict(self.toolchain.env())
        env.update(canary_env or {})
        evidence = harness.new_evidence(attempt, self.toolchain, image, argv)
        process = harness.launch(argv, env)
        result = AttemptResult(attempt, sandbox, relay, profile, argv, evidence, process)
        self.results.append(result)
        return result

    def wait(self, result: AttemptResult, timeout: float = 180.0) -> AttemptResult:
        returncode, _stdout, stderr = harness.wait_container_exit(result.process, timeout=timeout)
        if "Falling back to --cgroup-manager=cgroupfs" in stderr:
            raise RuntimeError(
                "podman fell back to the cgroupfs manager: memory/pids limits would be silently unenforced; "
                "the whole-worker boundary must stay fail-closed (systemd user session required)"
            )
        result.finish(returncode, stderr)
        result.relay.stop()
        return result

    def run(self, spec: dict[str, Any], **options: Any) -> AttemptResult:
        return self.wait(self.start(spec, **options))

    def state(self, result: AttemptResult) -> str:
        return harness.container_state(self.toolchain, result.container_name())

    def stop(self, result: AttemptResult, grace_seconds: int = 5) -> str:
        return harness.stop_container(self.toolchain, result.container_name(), grace_seconds=grace_seconds)

    def dispose_all(self) -> None:
        for result in self.results:
            try:
                if self.state(result) == "running":
                    self.stop(result, grace_seconds=2)
                result.relay.stop()
            except Exception:  # noqa: S110 - bounded best-effort disposal; native assertions already ran.
                pass


@pytest.fixture
def runner(toolchain: Toolchain, tmp_path: Path) -> Any:
    instance = AttemptRunner(toolchain, tmp_path)
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
        [sys.executable, "-c", f"import time; time.sleep(90)  # {BYSTANDER_MARKER}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )  # noqa: S603 - fixed synthetic bystander argv, never workload input.


class TestProvenance:
    def test_pinned_toolchain_versions_and_image_load(
        self, toolchain: Toolchain, provenance: dict[str, Any], runner: AttemptRunner
    ) -> None:
        assert toolchain.podman.version == PINNED_PODMAN_VERSION, "podman pin changed; update the decision record"
        assert toolchain.crun.version == PINNED_CRUN_VERSION, "crun pin changed; update the decision record"
        assert toolchain.conmon.version == PINNED_CONMON_VERSION, "conmon pin changed; update the decision record"
        # Provenance values are typed Any (external JSON record); presence is the assertion.
        assert {"podman", "crun", "conmon"} <= set(provenance["binaries"])
        result = runner.run({"steps": [{"kind": "write_workspace", "path": "ok.txt", "content": "ok"}]})
        assert result.returncode == 0, result.stderr_tail
        assert result.status.get("terminal") == "completed"
        assert (result.workspace / "ok.txt").read_text() == "ok"


class TestApprovedWork:
    def test_reads_approved_sources_runs_shell_and_python_writes_artifacts(
        self, runner: AttemptRunner, tmp_path: Path
    ) -> None:
        source = tmp_path / "approved-source"
        source.mkdir()
        (source / "module.py").write_text("APPROVED_CONTENT = 'kernel78'\n")
        result = runner.run(
            {
                "steps": [
                    {"kind": "read_approved", "path": "/source/approved-source/module.py"},
                    {"kind": "run_command", "argv": ["/bin/busybox", "sh", "-c", "echo shell-ok"]},
                    {
                        "kind": "run_command",
                        "argv": [
                            "/toolchain/bin/python3.12",
                            "-c",
                            "import sys; print('python', sys.version.split()[0])",
                        ],
                    },
                    {"kind": "write_workspace", "path": "artifact.json", "content": '{"synthetic": true}'},
                ]
            },
            source_ro=(source,),
        )
        assert result.returncode == 0, result.stderr_tail
        read_step = next(value for key, value in result.steps.items() if key.startswith("read:"))
        assert read_step["outcome"] == "read" and read_step["size"] > 0
        assert any("shell-ok" in str(value.get("stdout", "")) for value in result.steps.values())
        assert any("python 3.12" in str(value.get("stdout", "")) for value in result.steps.values())
        assert json.loads((result.workspace / "artifact.json").read_text())["synthetic"] is True


class TestCredentialAndHostDenial:
    def test_environment_closure_excludes_host_secrets(self, runner: AttemptRunner) -> None:
        canaries = {
            "KERNEL78_SYNTHETIC_CONTROLLER_TOKEN": "synthetic-controller-value",
            "KERNEL78_SYNTHETIC_PROVIDER_KEY": "synthetic-provider-value",
            "KERNEL78_SYNTHETIC_FORGE_TOKEN": "synthetic-forge-value",
        }
        result = runner.run(
            {
                "steps": [{"kind": "probe_env", "name": name} for name in canaries]
                + [{"kind": "probe_env", "name": "HOME"}]
            },
            canary_env=canaries,
        )
        assert result.returncode == 0, result.stderr_tail
        for name in canaries:
            assert result.step(f"env:{name}") == {"present": False}, f"host canary {name} leaked into the worker"
        observed_names = result.observations["env_names"]
        assert set(canaries).isdisjoint(observed_names), "canary names leaked into the worker environment"
        assert "HOME" in observed_names
        assert "synthetic-" not in json.dumps(result.status), "synthetic secret text leaked into worker records"

    def test_host_filesystem_paths_denied_and_unmodified(self, runner: AttemptRunner, tmp_path: Path) -> None:
        host_only = tmp_path / "host-only"
        host_only.mkdir()
        canary = host_only / "canary.txt"
        canary.write_text("host-synthetic-canary")
        result = runner.run(
            {
                "steps": [
                    {"kind": "probe_open", "path": "/host-only/canary.txt", "name": "host:tmp-canary"},
                    {"kind": "probe_open", "path": str(Path.home()), "name": "host:user-home"},
                    {"kind": "probe_open", "path": "/etc/ssh/sshd_config", "name": "host:etc-ssh"},
                    {"kind": "probe_open", "path": "/root", "name": "host:root-home"},
                ]
            }
        )
        assert result.returncode == 0, result.stderr_tail
        for key in ("host:tmp-canary", "host:user-home", "host:etc-ssh", "host:root-home"):
            outcome = result.step(key).get("outcome", "")
            assert outcome.startswith("denied:"), f"{key} was reachable inside the boundary: {outcome}"
        assert canary.read_text() == "host-synthetic-canary", "host marker was modified"

    def test_control_sockets_and_unmounted_unix_sockets_denied(self, runner: AttemptRunner, tmp_path: Path) -> None:
        outside = tmp_path / "outside-socket.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(outside))
        server.listen(1)
        try:
            result = runner.run(
                {
                    "steps": [
                        {"kind": "probe_unix_socket", "path": "/var/run/docker.sock", "abstract": False},
                        {"kind": "probe_unix_socket", "path": "/run/containerd/containerd.sock", "abstract": False},
                        {"kind": "probe_unix_socket", "path": str(outside), "abstract": False},
                        {"kind": "probe_unix_socket", "path": "kernel78-abstract-probe", "abstract": True},
                    ]
                }
            )
        finally:
            server.close()
            outside.unlink(missing_ok=True)
        assert result.returncode == 0, result.stderr_tail
        outcomes = [result.step(key).get("outcome", "") for key in result.steps]
        assert all(outcome.startswith("denied:") for outcome in outcomes), outcomes


class TestNetworkDenial:
    @pytest.mark.parametrize(
        ("host", "label"),
        [("127.0.0.1", "ipv4-loopback"), ("10.240.0.1", "ipv4-private"), ("::1", "ipv6-loopback")],
    )
    def test_network_families_denied(self, runner: AttemptRunner, host: str, label: str) -> None:
        result = runner.run({"steps": [{"kind": "probe_network", "host": host, "port": 59999, "label": label}]})
        assert result.returncode == 0, result.stderr_tail
        outcome = result.step(f"network:{label}").get("outcome", "")
        assert outcome.startswith("denied:"), f"{label} connectivity unexpectedly available: {outcome}"

    def test_dns_resolution_denied(self, runner: AttemptRunner) -> None:
        result = runner.run({"steps": [{"kind": "probe_dns", "name": "forgejo.creatidy.com"}]})
        assert result.returncode == 0, result.stderr_tail
        outcome = result.step("dns:forgejo.creatidy.com").get("outcome", "")
        assert outcome.startswith("denied:"), f"DNS unexpectedly resolved inside the boundary: {outcome}"


class TestRuntimePrivileges:
    def test_privilege_profile_observed_and_namespace_creation_denied(self, runner: AttemptRunner) -> None:
        result = runner.run({"steps": [{"kind": "probe_unshare"}]})
        assert result.returncode == 0, result.stderr_tail
        assert result.step("unshare-newuser").get("outcome") == "denied:rc=-1 errno=1"
        observations = result.observations
        assert observations["CapEff"] == "0000000000000000", "worker retains effective capabilities"
        assert observations["NoNewPrivs"] == "1"
        assert observations["Seccomp"] == "2"
        uid_map = observations["uid_map"].split()
        assert len(uid_map) == 3 and uid_map[2] == "1", f"expected single-UID mapping, saw {observations['uid_map']}"
        mounts = "\n".join(observations["mounts"])
        assert " /workspace " in mounts + "\n"
        assert any(" /toolchain/" in line for line in observations["mounts"]), "toolchain closure not mounted"
        for forbidden in (str(Path.home()), "/etc/ssh", "docker.sock", "containerd"):
            assert forbidden not in mounts, f"forbidden mount leaked: {forbidden}"

    def test_readonly_root_and_symlink_escape_denied(self, runner: AttemptRunner) -> None:
        result = runner.run(
            {
                "steps": [
                    {"kind": "probe_open", "path": "/bin/cannot-write", "name": "rootfs-write", "write": True},
                    {"kind": "probe_symlink_escape"},
                ]
            }
        )
        assert result.returncode == 0, result.stderr_tail
        assert result.step("rootfs-write").get("outcome", "").startswith("denied:")
        assert result.step("symlink-escape").get("outcome", "").startswith("denied:")

    def test_pids_limit_blocks_fork_flood(self, runner: AttemptRunner) -> None:
        result = runner.run({"steps": [{"kind": "flood_fork"}]}, pids_limit=8)
        assert result.returncode == 0, result.stderr_tail
        outcome = result.step("flood-fork").get("outcome", "")
        assert outcome.startswith("denied:"), f"fork flood succeeded under pids limit: {outcome}"
        assert result.observations["cgroup_pids_max"] == "8", "container cgroup did not carry the pids limit"


class TestLifecycleAndAuthority:
    def test_descendants_settle_after_stop_and_bystander_survives(self, runner: AttemptRunner) -> None:
        bystander = _spawn_bystander()
        try:
            result = runner.start(
                {
                    "steps": [
                        {"kind": "spawn_descendant", "marker": "settle-probe", "seconds": 120},
                        {"kind": "sleep", "seconds": 120},
                    ]
                }
            )
            # One marker substring covers both the setsid shell and its python grandchild.
            marker = "desc-settle-probe"
            assert _wait_for_marker(marker), "synthetic descendant never became visible on the host"
            assert harness.scan_host_for_marker(BYSTANDER_MARKER), "bystander missing before stop"
            runner.stop(result, grace_seconds=5)
            settled, stragglers = harness.wait_settled(marker)
            assert settled, f"descendants survived container stop: {stragglers}"
            assert harness.scan_host_for_marker(BYSTANDER_MARKER), "unrelated bystander was killed"
            returncode, _stdout, stderr = harness.wait_container_exit(result.process, timeout=60)
            result.finish(returncode, stderr)
            result.relay.stop()
            assert result.status.get("terminal") == "cancelled"
        finally:
            bystander.terminate()
            try:
                bystander.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bystander.kill()

    def test_relay_refuses_revoked_and_unknown_authority_during_execution(self, runner: AttemptRunner) -> None:
        result = runner.start(
            {
                "steps": [
                    {"kind": "call_relay", "grant": "g-valid", "op": "synthetic:effect", "request_id": "r1"},
                    {"kind": "call_relay", "grant": "g-valid", "op": "synthetic:effect", "request_id": "r2"},
                    {"kind": "sleep", "seconds": 3},
                    {"kind": "call_relay", "grant": "g-revoked", "op": "synthetic:effect", "request_id": "r3"},
                    {"kind": "call_relay", "grant": "g-unknown", "op": "synthetic:effect", "request_id": "r4"},
                ]
            },
            grants=(("g-valid", "synthetic:effect"), ("g-revoked", "synthetic:effect")),
        )
        time.sleep(1.0)
        result.relay.revoke("g-revoked")
        runner.wait(result)
        assert result.returncode == 0, result.stderr_tail
        assert result.step("relay:synthetic:effect:r1").get("result") == "executed"
        assert result.step("relay:synthetic:effect:r2") == {
            "result": "refused",
            "reason": "grant_already_consumed",
            "request_id": "r2",
        }
        assert result.step("relay:synthetic:effect:r3") == {
            "result": "refused",
            "reason": "grant_revoked",
            "request_id": "r3",
        }
        assert result.step("relay:synthetic:effect:r4")["reason"] == "unknown_grant"

    def test_lost_reply_effect_stays_uncertain(self, runner: AttemptRunner) -> None:
        result = runner.start(
            {"steps": [{"kind": "call_relay", "grant": "g1", "op": "synthetic:effect", "request_id": "r1"}]},
            grants=(("g1", "synthetic:effect"),),
        )
        result.relay.drop_next_reply()
        runner.wait(result)
        assert result.returncode == 0, result.stderr_tail
        assert result.step("relay:synthetic:effect:r1") == {"result": "reply_lost"}
        lost = [event for event in result.relay.events() if event.kind == "lost"]
        assert lost, "relay did not record the dropped reply"
        executed = [event for event in result.relay.events() if event.kind == "effect"]
        assert executed, "authorized effect should still have been recorded as executed"
        assert "uncertain" in lost[0].detail

    def test_hold_fd_effects_persist_and_are_not_revoked(self, runner: AttemptRunner) -> None:
        result = runner.start({"steps": [{"kind": "hold_fd", "file": "held.bin", "seconds": 20}]})
        held = result.workspace / "held.bin"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if held.exists() and held.stat().st_size >= 8192:
                break
            time.sleep(0.2)
        assert held.exists() and held.stat().st_size >= 8192, "worker never streamed to the workspace"
        size_at_revoke = held.stat().st_size
        runner.stop(result, grace_seconds=0)
        returncode, _stdout, stderr = harness.wait_container_exit(result.process, timeout=60)
        result.finish(returncode, stderr)
        result.relay.stop()
        final_size = held.stat().st_size
        assert final_size >= size_at_revoke, "workspace bytes were rolled back after termination"
        assert final_size < 20 * 4096, "unexpectedly complete stream; check timing"
        # The precise final byte count is an observed, non-revocable effect of the static
        # writable bind: termination bounds the future, it does not retract past writes.
        assert result.status.get("terminal") != "cancelled", "SIGKILL stop must not be recorded as a graceful cancel"

    def test_deadline_expiry_refuses_further_steps(self, runner: AttemptRunner) -> None:
        result = runner.run(
            {
                "steps": [
                    {"kind": "sleep", "seconds": 30},
                    {"kind": "write_workspace", "path": "late.txt", "content": "late"},
                ]
            },
            deadline_seconds=2,
        )
        assert result.status.get("terminal") == "expired"
        assert not (result.workspace / "late.txt").exists()

    def test_monitor_death_leaves_state_unknown_until_observed(self, runner: AttemptRunner) -> None:
        result = runner.start({"steps": [{"kind": "sleep", "seconds": 60}]})
        time.sleep(2)
        os.kill(result.process.pid, signal.SIGKILL)
        result.process.wait(timeout=30)
        # After monitor death the Attempt terminality is unknown until reconciled by
        # observation; the next read must produce a concrete observed state, never a guess.
        state = runner.state(result)
        assert state in {"running", "absent"}, state
        if state == "running":
            runner.stop(result, grace_seconds=2)
        assert runner.state(result) == "absent"
        result.status = harness.worker_status(result.sandbox)
        result.relay.stop()
