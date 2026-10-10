# SPDX-License-Identifier: Apache-2.0
"""Native whole-worker isolation reception over the user-owned pinned rootless OCI toolroot.

Set CREATIDY_TEST_OCI_TOOLROOT to the provisioned toolroot to run native reception. Missing
setup is explicitly UNPROVED, never a passing denial receipt: every skip below means the
whole-worker boundary was not exercised, not that isolation holds. Payloads are synthetic;
no real credentials, private state, live model or remote effect is used. Each attempt
records observed outcomes into an evidence file under its temporary directory.
"""

import copy
import dataclasses
import json
import os
import platform
import select
import signal
import socket
import struct
import subprocess  # noqa: S603 - every call below uses fixed synthetic argv, no shell.
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from creatidy_kernel.adapters import oci_bootstrap
from creatidy_kernel.adapters.oci_lifetime import LifetimeChannel
from creatidy_kernel.adapters.worker_profile import WorkerMount, rootless_run_prefix
from tools.codex_oci_proof.boundary import write_seccomp_profile
from tools.oci_worker_poc import harness
from tools.oci_worker_poc.evidence import ProbeRecord, RunEvidence, sha256_path
from tools.oci_worker_poc.profile import WorkerProfile, build_profile, build_run_argv
from tools.oci_worker_poc.relay import EffectsRelay
from tools.oci_worker_poc.toolchain import Toolchain, host_python_closure, read_provenance

TOOLROOT_ENV = "CREATIDY_TEST_OCI_TOOLROOT"
PINNED_PODMAN_VERSION = "podman version 6.1.3"
PINNED_CRUN_VERSION = "crun version 1.30.1"
PINNED_CONMON_VERSION = "conmon version 2.2.1"
BYSTANDER_MARKER = "kernel78-bystander-marker"


def test_kernel_authenticated_init_lifetime(toolchain: Toolchain, tmp_path: Path) -> None:
    """Receive production bootstrap and actual kernel sender pidfd, not marker-based settlement."""
    root = tmp_path.resolve()
    workspace = root / "workspace"
    workspace.mkdir()
    python, (stdlib, loader, *libs) = host_python_closure()
    bootstrap = Path(oci_bootstrap.__file__).resolve()
    mounts = [
        WorkerMount(python, PurePosixPath("/toolchain/bin/python3.12"), False),
        WorkerMount(stdlib, PurePosixPath("/toolchain/lib/python3.12"), False),
        WorkerMount(loader, PurePosixPath("/lib64/ld-linux-x86-64.so.2"), False),
        WorkerMount(bootstrap, PurePosixPath("/worker/lifetime.py"), False),
        WorkerMount(workspace, PurePosixPath("/workspace"), True),
    ]
    mounts.extend(WorkerMount(lib, PurePosixPath("/toolchain/lib") / lib.name, False) for lib in libs)
    attempt, nonce = uuid.uuid4().hex, uuid.uuid4().hex + uuid.uuid4().hex
    name = "kernel53-lifetime-" + attempt[:12]
    profile = WorkerProfile(
        attempt,
        tuple(mounts),
        (("PYTHONHOME", "/toolchain"), ("LD_LIBRARY_PATH", "/toolchain/lib")),
        write_seccomp_profile(root / "seccomp.json"),
        32,
        256 * 1024 * 1024,
        0.5,
        30,
    )
    image = harness.ensure_image(toolchain, root / "image.tar")
    channel = LifetimeChannel(nonce)
    descriptor = channel.worker.fileno()
    command = (
        "import subprocess,time;from pathlib import Path;"
        "subprocess.Popen(['/toolchain/bin/python3.12','-I','-S','-c','import time;time.sleep(60)'],"
        "start_new_session=True);"
        "Path('/workspace/ready').write_text('ready');time.sleep(60)"
    )
    argv = [
        *rootless_run_prefix(toolchain.root / "bin/podman", profile, name),
        f"--preserve-fd={descriptor}",
        image.config_digest.removeprefix("sha256:"),
        "/toolchain/bin/python3.12",
        "-I",
        "-S",
        "/worker/lifetime.py",
        str(descriptor),
        nonce,
        str(time.time() + 30),
        "--",
        "/toolchain/bin/python3.12",
        "-I",
        "-S",
        "-c",
        command,
    ]
    process: subprocess.Popen[bytes] | None = None
    bystander = subprocess.Popen(  # noqa: S603 - own synthetic bystander, no shared server or owner state.
        [sys.executable, "-I", "-S", "-c", "import time;time.sleep(60)"],
        env={"HOME": str(root)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed owned native namespace and inherited control descriptor.
            argv,
            env=toolchain.env(),
            pass_fds=(descriptor,),  # noqa: S603
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        channel.worker.close()
        identity = channel.receive(timeout=15)
        inspected = subprocess.run(  # noqa: S603 - fixed owned native container inspection.
            [str(toolchain.root / "bin/podman"), "container", "inspect", name],  # noqa: S603
            env=toolchain.env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        container = json.loads(inspected.stdout)[0]
        assert container["State"]["Pid"] == identity["pid"] and container["Image"].removeprefix(
            "sha256:"
        ) == image.config_digest.removeprefix("sha256:")
        assert not (workspace / "ready").exists(), "candidate ran before execution gate"
        channel.release()
        deadline = time.monotonic() + 10
        while not (workspace / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (workspace / "ready").read_text() == "ready"
        channel.close_liveness()
        assert channel.pidfd is not None
        poll = select.poll()
        poll.register(channel.pidfd, select.POLLIN)
        assert poll.poll(10000), "kernel namespace init did not complete descendant teardown"
        assert channel.exited()
        process.communicate(timeout=10)
        assert harness.container_state(toolchain, name) == "absent"
        assert bystander.poll() is None
        (root / "lifetime-receipt.json").write_text(
            json.dumps(
                {
                    "identity": identity,
                    "container": container["Id"],
                    "namespace_exit": True,
                    "container_absent": True,
                    "bystander_alive": True,
                }
            )
        )
    finally:
        channel.close_liveness()
        if harness.container_state(toolchain, name) != "absent":
            harness.stop_container(toolchain, name, grace_seconds=2)
        if process is not None:
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        channel.close()
        bystander.kill()
        bystander.wait(timeout=5)


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


@pytest.mark.parametrize("denial_errno", [1, 33], ids=["production", "diagnostic"])
def test_native_clone_masks(toolchain: Toolchain, denial_errno: int) -> None:
    assert toolchain.podman.version == PINNED_PODMAN_VERSION
    assert toolchain.crun.version == PINNED_CRUN_VERSION
    assert toolchain.conmon.version == PINNED_CONMON_VERSION
    # Public executable SHA-256 pins from the existing bounded toolroot provenance, not credentials.
    assert (
        toolchain.podman.sha256
        == "72b04715918065668d3025ba67fb87ad678d1a0fe099ebd8878f33443b281c31"  # pragma: allowlist secret
    )
    assert (
        toolchain.crun.sha256
        == "86d1e6a0e76945975d3aebfab39cbc6a26eea15f1c3fc66b6776d19e5dc346a0"  # pragma: allowlist secret
    )
    assert (
        toolchain.conmon.sha256
        == "a3baaea8adf23f94d9c2f3ee212a168876b25c335320bca23972a1bc3ed9f22e"  # pragma: allowlist secret
    )
    if sys.platform != "linux" or platform.machine() != "x86_64":
        pytest.skip("clone enforcement UNPROVED: native Linux LP64 x86-64 required")
    cc = Path("/usr/bin/cc")
    parent = Path("/tmp/kilo")  # noqa: S108 - approved synthetic proof root, never owner state.
    if not cc.is_file() or not parent.is_dir():
        pytest.skip("clone enforcement UNPROVED: explicit public build tools/temp root unavailable")
    root = Path(tempfile.mkdtemp(prefix="clone-proof-", dir=parent)).resolve()
    home, scratch = root / "home", root / "scratch"
    home.mkdir()
    scratch.mkdir()
    source = Path(__file__).parent / "fixtures" / "oci_clone_probe.c"
    probe = root / "clone-probe"
    built = subprocess.run(  # noqa: S603 - explicit public compiler and repository-authored bounded fixture.
        [
            str(cc),
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-static",
            "-fno-pie",
            "-no-pie",
            "-pthread",
            str(source),
            "-o",
            str(probe),
        ],
        cwd=root,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(scratch), "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    (root / "compile.log").write_text(built.stdout + built.stderr)
    missing = ("cannot find -lc:", "cannot find -lpthread:", "cannot find crt1.o:", "cannot find crti.o:")
    if built.returncode and any(value in built.stderr for value in missing):
        pytest.skip(f"clone enforcement UNPROVED: static build prerequisites absent; {root}")
    assert built.returncode == 0, f"probe build failed; no native receipt; {root}"
    elf = probe.read_bytes()
    assert elf[:6] == b"\x7fELF\x02\x01"
    assert struct.unpack_from("<HH", elf, 16) == (2, 62)
    phoff = struct.unpack_from("<Q", elf, 32)[0]
    phsize, phcount = struct.unpack_from("<HH", elf, 54)
    assert phsize == 56 and phcount > 0 and phoff + phsize * phcount <= len(elf)
    assert all(struct.unpack_from("<I", elf, phoff + index * phsize)[0] != 3 for index in range(phcount)), (
        "probe must be static"
    )
    seccomp = write_seccomp_profile(scratch / "seccomp.json")
    production = json.loads(seccomp.read_text())
    selected = copy.deepcopy(production)
    clones = [group for group in selected["syscalls"] if group["names"] == ["clone"]]
    assert len(clones) == 7
    for group in clones:
        assert group["action"] == "SCMP_ACT_ERRNO" and group["errnoRet"] == 1 and len(group["args"]) == 1
        group["errnoRet"] = denial_errno
    restored = copy.deepcopy(selected)
    for group in restored["syscalls"]:
        if group["names"] == ["clone"]:
            group["errnoRet"] = 1
    assert restored == production
    seccomp.write_text(json.dumps(selected))
    attempt = uuid.uuid4().hex[:12]
    name = f"kernel53-clone-{attempt}"
    profile = WorkerProfile(
        attempt,
        (WorkerMount(probe, PurePosixPath("/probe/clone"), False),),
        (("HOME", "/home/worker"), ("TMPDIR", "/tmp"), ("LC_ALL", "C")),  # noqa: S108 - private container tmpfs.
        seccomp,
        64,
        256 * 1024 * 1024,
        0.5,
        20,
    )
    image = harness.ensure_image(toolchain, root / "worker-image.tar")
    inspected = subprocess.run(  # noqa: S603 - fixed explicit tool argv for this controller-built image.
        [str(toolchain.root / "bin/podman"), "image", "inspect", "--format", "{{.Id}}", image.reference],
        env=toolchain.env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert inspected.returncode == 0 and inspected.stdout.strip() == image.config_digest.removeprefix("sha256:"), (
        "loaded image differs from fixed archive"
    )
    argv = rootless_run_prefix(toolchain.root / "bin/podman", profile, name) + [
        image.reference,
        "/probe/clone",
        str(denial_errno),
    ]
    evidence = harness.new_evidence(attempt, toolchain, image, argv)
    evidence.probes.append(
        ProbeRecord(
            "clone-inputs",
            "exact fixture/profile",
            "recorded",
            json.dumps(
                {
                    "probe": sha256_path(probe),
                    "seccomp": sha256_path(seccomp),
                    "abi": "linux-x86_64-lp64",
                }
            ),
        )
    )
    process = None
    try:
        process = harness.launch(argv, toolchain.env())
        rc, stdout, stderr = harness.wait_container_exit(process, timeout=30)
        (root / "stdout.txt").write_text(stdout)
        (root / "stderr.txt").write_text(stderr)
        assert "Falling back to --cgroup-manager=cgroupfs" not in stderr
        assert rc == 0, f"native clone proof failed; evidence at {root}"
        assert stdout.splitlines() == [
            "RAW_CLONE_REAPED",
            "PTHREAD_JOINED",
            "CLONE3_ENOSYS",
            *[f"DENIED {index} {denial_errno}" for index in range(9)],
            "NO_CHILDREN",
        ]
        evidence.probes.append(
            ProbeRecord(
                "native-clone-matrix",
                "reaped raw clone, joined pthread, clone3 ENOSYS, nine attributed namespace denials",
                "passed",
                stdout.strip(),
            )
        )
    finally:
        try:
            if harness.container_state(toolchain, name) != "absent":
                harness.stop_container(toolchain, name, grace_seconds=2)
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=10)
            state = harness.container_state(toolchain, name)
            evidence.probes.append(
                ProbeRecord("owned-container", "confirmed absent", state, f"fixture retained at {root}")
            )
            evidence.dump(root / "evidence.json")
        assert state == "absent", f"settlement UNPROVED; preserve resources at {root}"


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
                if self.state(result) != "absent":
                    # exists or unknown: bounded termination is the fail-safe action; only
                    # a confirmed absent state justifies skipping it.
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
        # Loopback refusal alone proves no listener, not isolation from the host network.
        assert result.observations["network_interfaces"] == ["lo"]
        assert len(result.observations["ipv4_routes"]) == 1, "unexpected non-loopback IPv4 route"

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
        # observation; existence is not liveness or terminality evidence, and an
        # inconclusive podman observation is reported as "unknown", never as absence.
        state = runner.state(result)
        assert state in {"exists", "absent", "unknown"}, state
        if state != "absent":
            runner.stop(result, grace_seconds=2)
        assert runner.state(result) == "absent"
        result.status = harness.worker_status(result.sandbox)
        result.relay.stop()


class TestWorkerStatusFailClosed:
    """Host-side regression for the shared worker-status reader (no podman needed).

    The worker can replace or symlink any workspace path at any time, including to host
    paths invisible inside the boundary: a planted symlink at worker-status.json must be
    refused before any open, never followed host-side. Absence stays truthful; malformed
    content and tampering raise instead of being masked.
    """

    @staticmethod
    def _sandbox(workspace: Path) -> harness.Sandbox:
        from tools.oci_worker_poc.profile import AttemptResources

        return harness.Sandbox(
            root=workspace.parent,
            resources=AttemptResources(
                attempt="probe",
                workspace=workspace,
                home=workspace.parent / "home",
                scratch=workspace.parent / "scratch",
                relay_socket=workspace.parent / "relay" / "effects.sock",
            ),
            worker_entry=workspace.parent / "worker_main.py",
        )

    def test_worker_symlink_cannot_leak_host_only_json(self, tmp_path: Path) -> None:
        secret_dir = tmp_path / "host-only"
        secret_dir.mkdir()
        secret = secret_dir / "controller-status.json"
        secret_bytes = b'{"host-only": "SYNTHETIC-SECRET-9f2c7"}'
        secret.write_bytes(secret_bytes)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "worker-status.json").symlink_to(secret)

        with pytest.raises(ValueError, match="symlinked"):
            harness.worker_status(self._sandbox(workspace))
        assert secret.read_bytes() == secret_bytes, "host target was opened or modified"

    def test_absence_is_truthful_and_content_semantics_preserved(self, tmp_path: Path) -> None:
        import json as json_module

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sandbox = self._sandbox(workspace)
        assert harness.worker_status(sandbox) == {"observed": False}
        (workspace / "worker-status.json").write_bytes(b"{not json")
        with pytest.raises(json_module.JSONDecodeError):
            harness.worker_status(sandbox)
        (workspace / "worker-status.json").write_bytes(b'{"terminal": "completed", "steps": {}}')
        assert harness.worker_status(sandbox)["terminal"] == "completed"

    def test_oversized_status_is_refused_not_read(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "worker-status.json").write_bytes(b"x" * (1024 * 1024 + 1))
        with pytest.raises(ValueError, match="read bound"):
            harness.worker_status(self._sandbox(workspace))
