# SPDX-License-Identifier: Apache-2.0
"""Actual factory/kernel/Codex reception with synthetic source and local model only.

No profile adoption, owner state or live inference. Evidence and uncertain roots
are retained in the approved temporary proof root. Missing setup is UNPROVED.
"""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from creatidy_kernel.adapters import oci_bootstrap
from creatidy_kernel.adapters.bubblewrap_verification import resource_digest
from creatidy_kernel.adapters.codex_runtime import CodexInputs, CodexRuntime
from creatidy_kernel.adapters.codex_stdio import CodexRPCError, CodexStdio
from creatidy_kernel.adapters.forgejo_transport import run_git_bounded
from creatidy_kernel.adapters.git_workspace import GitWorkspace
from creatidy_kernel.adapters.oci_lifecycle import LifetimeSpec, OCIResources, OwnedOCI
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.worker_collection import read_bounded_regular
from creatidy_kernel.adapters.worker_profile import WorkerMount, WorkerProfile
from creatidy_kernel.core.authority import AuthorityGrant, OperationIntent, Principal
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AuthorityEnvelope,
    PolicyReference,
    PrepareAttempt,
    ProgramSpec,
    WorkUnit,
)
from creatidy_kernel.core.execution import (
    Activity,
    ExecutionConflict,
    ExecutionRequest,
    OperationKey,
    Presence,
    RuntimeIdentity,
    TrustMode,
    WorkspaceSpec,
)
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.ports.allocation import encode_allocation
from creatidy_kernel.ports.program_store import OperationRecord
from tools.codex_oci_proof.boundary import write_seccomp_profile
from tools.codex_oci_proof.codexbin import CodexBinary, ProtocolInventory
from tools.codex_oci_proof.driver import exec_step, final_step, write_scenario
from tools.codex_oci_proof.ocimage import CodexImageRecord
from tools.oci_worker_poc.toolchain import Toolchain, host_python_closure

pytest_plugins = ["test_codex_oci_isolation"]


@dataclass
class NativeRuntime:
    root: Path
    store: SQLiteProgramStore
    request: ExecutionRequest
    owner: OwnedOCI
    resources: OCIResources
    runtime: CodexRuntime
    connection: CodexStdio
    scenario: str
    codex: CodexBinary
    store_closed: bool = False


SOURCE_EDIT_PROBE = """import ctypes, json, os, socket, subprocess, sys
from pathlib import Path
scope = json.loads(sys.argv[1])
namespaces = {key: os.readlink('/proc/self/ns/' + key) for key in scope['host_namespaces']}
assert all(value != scope['host_namespaces'][key] for key, value in namespaces.items())
denied = []
for name in scope['forbidden']:
    try:
        with open(name, 'rb') as stream:
            stream.read(1)
    except OSError:
        denied.append(name)
    else:
        raise AssertionError('unauthorized path became readable')
    try:
        with open(name, 'ab') as stream:
            stream.write(b'forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('unauthorized path became writable')
try:
    descriptors = os.listdir('/proc/1/fd')
except OSError:
    descriptors = []
init_fd_refusals = []
for descriptor in descriptors:
    for mode in [os.O_RDONLY, os.O_WRONLY, os.O_RDWR]:
        try:
            inherited = os.open('/proc/1/fd/' + descriptor, mode | os.O_NONBLOCK)
        except OSError as error:
            assert error.errno in [1, 13], 'descriptor refusal was not an access denial'
            init_fd_refusals.append(error.errno)
            continue
        os.close(inherited)
        raise AssertionError('private init descriptor could be reopened')
link = Path('/workspace/candidate/escape-control')
link.symlink_to(scope['forbidden'][0])
try:
    link.read_bytes()
except OSError:
    pass
else:
    raise AssertionError('symlink escaped authorized workspace')
finally:
    link.unlink()
for name in ['/bin/forbidden-root-write', '/toolchain/codex/codex']:
    try:
        with open(name, 'ab') as stream:
            stream.write(b'forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('immutable mount became writable')
with socket.socket() as stream:
    stream.settimeout(0.2)
    try:
        stream.connect(('192.0.2.1', 443))
    except OSError:
        pass
    else:
        raise AssertionError('unauthorized external network became reachable')
libc = ctypes.CDLL(None, use_errno=True)
assert libc.unshare(0x10000000) == -1 and ctypes.get_errno() == 1
child_script = ("import json,os; print(json.dumps({key: os.readlink('/proc/self/ns/'+key) "
    "for key in ['pid','mnt','net','user']}))")
child = subprocess.run([sys.executable, '-S', '-c', child_script],
    env=dict(os.environ), start_new_session=True, capture_output=True, text=True, check=True)
assert json.loads(child.stdout) == namespaces
version = subprocess.run(['/toolchain/codex/codex', '--version'], env=dict(os.environ),
    capture_output=True, text=True, check=True)
assert version.stdout.strip() == 'codex-cli 0.159.3'
Path('/workspace/candidate/original.txt').write_bytes(b'native updated source\\n')
Path('/workspace/candidate/result.txt').write_bytes(b'native source edit\\n')
Path('/workspace/candidate/security-receipt.json').write_text(json.dumps({'namespaces': namespaces,
    'denied_paths': denied, 'version': version.stdout.strip(),
    'subprocess_namespaces': json.loads(child.stdout), 'init_fd_refusals': init_fd_refusals},
    sort_keys=True))
"""


@pytest.fixture
def native_runtime(
    request: pytest.FixtureRequest,
    toolchain: Toolchain,
    codex_binary: CodexBinary,
    codex_inventory: ProtocolInventory,
    image: CodexImageRecord,
) -> Iterator[NativeRuntime]:
    root = Path(tempfile.mkdtemp(prefix="runtime-conformance-", dir="/tmp/kilo")).resolve()  # noqa: S108 - approved retained proof root.
    source, control, closure = root / "source", root / "control", root / "closure"
    for path in (source, control, closure):
        path.mkdir(mode=0o700)
    reference_git(source, "init", "--template=")
    (source / "original.txt").write_bytes(b"exact bounded source\n")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "synthetic source")
    reference_git(source, "remote", "add", "origin", "https://forge.invalid/Owner/repo")
    base = reference_git(source, "rev-parse", "HEAD").strip()
    python, (stdlib, loader, *libs) = host_python_closure()
    approved_stdlib = closure / "python3.12"
    shutil.copytree(
        stdlib,
        approved_stdlib,
        ignore=shutil.ignore_patterns("config-*", "__pycache__", "dist-packages", "site-packages"),
    )
    proof_source = Path(__file__).resolve().parent.parent / "tools/codex_oci_proof"
    bootstrap = Path(oci_bootstrap.__file__).resolve()
    attempt_id = uuid.uuid4().hex
    workspace_path = control / "workspace"
    mounts = [
        WorkerMount(python.resolve(), PurePosixPath("/toolchain/bin/python3.12"), False),
        WorkerMount(approved_stdlib, PurePosixPath("/toolchain/lib/python3.12"), False),
        WorkerMount(loader.resolve(), PurePosixPath("/lib64/ld-linux-x86-64.so.2"), False),
        WorkerMount(Path("/usr/bin/bash").resolve(), PurePosixPath("/bin/bash"), False),
        WorkerMount(
            Path("/usr/lib/x86_64-linux-gnu/libtinfo.so.6").resolve(),
            PurePosixPath("/toolchain/lib/libtinfo.so.6"),
            False,
        ),
        WorkerMount(codex_binary.path, PurePosixPath("/toolchain/codex/codex"), False),
        WorkerMount(proof_source / "worker_entry.py", PurePosixPath("/worker/main.py"), False),
        WorkerMount(proof_source / "mockmodel.py", PurePosixPath("/worker/mockmodel.py"), False),
        WorkerMount(bootstrap, PurePosixPath("/worker/lifetime.py"), False),
        WorkerMount(workspace_path, PurePosixPath("/workspace"), True),
    ]
    if codex_binary.code_mode_host is not None:
        mounts.append(
            WorkerMount(codex_binary.code_mode_host, PurePosixPath("/toolchain/codex/codex-code-mode-host"), False)
        )
    configured_bwrap = os.environ.get("CREATIDY_TEST_BWRAP_BIN")
    if configured_bwrap is None and str(request.param).startswith("edit"):
        pytest.skip("workspace-write tool conformance UNPROVED: explicit pinned bwrap closure required")
    if configured_bwrap is not None:
        bwrap = Path(configured_bwrap).resolve(strict=True)
        assert (
            hashlib.sha256(bwrap.read_bytes()).hexdigest()
            == "7c0da12cc27c4de4bac5bc1dcf50d19c62a445bdb5f30cb8c9740800b0b8b237"  # pragma: allowlist secret
        )
        mounts.extend(
            (
                WorkerMount(bwrap, PurePosixPath("/toolchain/bin/bwrap"), False),
                WorkerMount(
                    Path("/usr/lib/x86_64-linux-gnu/libcap.so.2").resolve(),
                    PurePosixPath("/toolchain/lib/libcap.so.2"),
                    False,
                ),
            )
        )
    mounts.extend(WorkerMount(lib.resolve(), PurePosixPath("/toolchain/lib") / lib.name, False) for lib in libs)
    seccomp = write_seccomp_profile(control / "seccomp.json")
    if request.param == "edit-diagnostic":
        policy = json.loads(seccomp.read_text())
        for rule in policy["syscalls"]:
            if set(rule["names"]) & {"clone", "unshare", "setns", "mount"}:
                rule["errnoRet"] = 33
        seccomp.write_text(json.dumps(policy, sort_keys=True))
    profile = WorkerProfile(
        attempt_id,
        tuple(mounts),
        (
            ("PATH", "/bin:/toolchain/bin"),
            ("PYTHONHOME", "/toolchain"),
            ("LD_LIBRARY_PATH", "/toolchain/lib"),
            ("HOME", "/home/worker"),
            ("TMPDIR", "/tmp"),  # noqa: S108 - private in-container tmpfs, not host state.
            ("LANG", "C.UTF-8"),
            ("LC_ALL", "C"),
            ("ATTEMPT_ID", attempt_id),
            ("SHELL", "/bin/sh"),
            ("CODEX_HOME", "/workspace/codex-home"),
        ),
        seccomp,
        256,
        1024 * 1024 * 1024,
        1.0,
        120,
    )
    readonly = tuple((mount.source, resource_digest(mount.source)) for mount in mounts if not mount.writable)
    expected: dict[str, object] = {}

    def decode(operation: OperationRecord) -> OperationIntent:
        if json.loads(operation.request_json) != {"version": 1, "request": expected}:
            raise ValueError("native controller request differs from original producer")
        return OperationIntent(
            operation.operation_id,
            operation.effect_key,
            operation.request_digest,
            "execute",
            "synthetic-repository",
            Path("."),
        )

    connection: CodexStdio | None = None
    cell: NativeRuntime | None = None
    with SQLiteProgramStore(control / "kernel.db", resolve_authority_intent=decode) as store:
        workspace = GitWorkspace(
            store,
            source,
            "https://forge.invalid/Owner/repo",
            "synthetic-repository",
            workspace_path,
            control / "objects.git",
        )
        handle = workspace.materialize(
            "workspace-" + attempt_id,
            WorkspaceSpec(
                "synthetic-repository", base, (), "explicit-synthetic-closure", "trusted", TrustMode.TRUSTED_DEVELOPMENT
            ),
        )
        (workspace_path / "attempt.json").write_text(
            json.dumps(
                {
                    "attempt": attempt_id,
                    "deadline_seconds": 120,
                    "sandbox_mode": "workspace-write",
                    "detached_mock": True,
                }
            )
        )
        steps = [final_step("bounded synthetic completion")]
        if request.param in {
            "cancel",
            "revoke",
            "close",
            "lost-thread",
            "lost-turn",
            "cold",
            "reopen",
            "object-drift",
            "candidate-drift",
            "observer",
        }:
            steps[0]["delay_seconds"] = 30
        if str(request.param).startswith("edit"):
            credentials = control / "credentials"
            credentials.mkdir(mode=0o700)
            for name in ("forge", "model", "host-only"):
                (credentials / name).write_bytes(b"synthetic private canary")
            (workspace_path / "candidate/security-probe.py").write_text(SOURCE_EDIT_PROBE)
            scope = {
                "forbidden": [str(credentials / name) for name in ("forge", "model", "host-only")]
                + [str(control / "kernel.db"), str(control / "objects.git/HEAD"), "/run/podman/podman.sock"],
                "host_namespaces": {key: os.readlink("/proc/self/ns/" + key) for key in ("pid", "mnt", "net", "user")},
            }
            steps.insert(
                0,
                exec_step(
                    "/toolchain/bin/python3.12 -S /workspace/candidate/security-probe.py "
                    + shlex.quote(json.dumps(scope, sort_keys=True))
                ),
            )
        if request.param in {"cancel", "revoke", "close"}:
            child = (
                "import json,os,time; from pathlib import Path; "
                "Path('/workspace/candidate/.subprocess.tmp').write_text(json.dumps("
                "{'pid':os.getpid(),'session':os.getsid(0),'namespace':os.readlink('/proc/self/ns/pid')})); "
                "os.rename('/workspace/candidate/.subprocess.tmp','/workspace/candidate/subprocess.json'); "
                "time.sleep(45)"
            )
            command = (
                "import os,subprocess,sys; from pathlib import Path; "
                f"child=subprocess.Popen([sys.executable,'-S','-c',{child!r}],"
                "env=dict(os.environ),start_new_session=True); "
                "Path('/workspace/candidate/in-flight.txt').write_bytes(b'native authorized work'); child.wait()"
            )
            steps.insert(0, exec_step("/toolchain/bin/python3.12 -S -c " + shlex.quote(command)))
        write_scenario(workspace_path, steps, attempt_id)
        program = store.create(
            ProgramSpec(
                "program-" + attempt_id,
                "native factory fixture",
                (WorkUnit("unit", "bounded source edit", acceptance_criteria=("exact bytes",)),),
                acceptance_criteria=("received boundary",),
                authority=AuthorityEnvelope("owner"),
                policy_references=(PolicyReference("acceptance", "v1", "sha256:synthetic-policy"),),
            ),
            "create",
        )
        program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
        allocation = Allocation(
            "codex", "synthetic", "synthetic-model", frozenset({"reference"}), 128, "local synthetic backend"
        )
        allocation_bytes, context = encode_allocation(allocation), b"[]"
        attempt = AttemptSpec(
            attempt_id,
            program.program_id,
            "unit",
            program.spec.revision,
            program.spec.digest,
            allocation_reference="sha256:" + hashlib.sha256(allocation_bytes).hexdigest(),
            context_reference="sha256:" + hashlib.sha256(context).hexdigest(),
            workspace_reference=handle.key,
            agent_definition_reference="synthetic-native-agent",
        )
        expected.update(
            {
                "version": 1,
                "attempt": attempt.digest,
                "workspace": handle.key,
                "allocation": allocation_bytes.decode(),
                "context": context.decode(),
            }
        )
        operation_id = "runtime:" + attempt_id
        store.admit_with_intent(
            program.program_id,
            "prepare",
            PrepareAttempt(program.revision, "owner", attempt),
            operation_id,
            operation_id,
            expected,
        )
        now = int(time.time())
        grant = AuthorityGrant(
            "grant-" + attempt_id,
            "owner",
            attempt.digest,
            program.program_id,
            program.spec.digest,
            handle.spec.repository,
            workspace_path,
            frozenset({workspace_path}),
            frozenset(),
            frozenset({"read", "write", "execute"}),
            now + 120,
            True,
        )
        store.issue(Principal("owner", "owner"), grant)
        operation = store.operation(operation_id)
        store.check(Principal(attempt_id, "worker"), grant.grant_id, attempt, decode(operation), now)
        fence = store.claim(operation_id, now=now, lease_seconds=120)
        execution = ExecutionRequest(
            OperationKey(operation_id, operation.effect_key, operation.request_digest),
            attempt,
            handle,
            cast(str, attempt.context_reference),
            cast(str, attempt.allocation_reference),
            grant.grant_id,
            RuntimeIdentity("synthetic-model", None, None, "synthetic-native-agent", requested_provider="synthetic"),
            fence,
            allocation,
        )

        def invoke(argv: tuple[str, ...], environment: Mapping[str, str]) -> subprocess.CompletedProcess[bytes]:
            result = run_git_bounded(list(argv), control, dict(environment), 15, 1024 * 1024)
            return subprocess.CompletedProcess(argv, result.returncode, result.stdout.encode(), result.stderr.encode())

        owner = OwnedOCI(
            store,
            execution,
            toolchain.root / "bin/podman",
            profile,
            "kernel53-runtime-" + attempt_id,
            image.config_digest.removeprefix("sha256:"),
            toolchain.env(),
            invoke,
        )
        pins = (owner.podman, codex_binary.path, seccomp, toolchain.containers_conf, toolchain.storage_conf)
        resources = workspace.bind(
            owner,
            OCIResources(
                workspace_path,
                control,
                (control / "kernel.db", workspace.objects),
                tuple((path, hashlib.sha256(path.read_bytes()).hexdigest()) for path in pins),
                readonly,
            ),
            codex=codex_binary.path,
            collection_authorizer=store.worker_active_authorizer(lambda: int(time.time())),
        )
        probe = control / "probe"
        probe.mkdir(mode=0o700)
        try:
            connection = owner.connect(
                resources,
                codex_binary.path,
                codex_binary.version,
                ("/toolchain/bin/python3.12", "-S", "/worker/main.py"),
                {
                    "HOME": str(probe),
                    "CODEX_HOME": str(probe / "codex-home"),
                    "TMPDIR": str(probe),
                    "PATH": "/usr/bin:/bin",
                    "LC_ALL": "C",
                },
                codex_inventory.required_methods,
                store.worker_authorizers(lambda: int(time.time())),
                lambda: int(time.time()),
                lifetime=LifetimeSpec(PurePosixPath("/toolchain/bin/python3.12"), PurePosixPath("/worker/lifetime.py")),
                enter_authorizer=store.worker_entry_authorizer(lambda: int(time.time())),
                deadline=store.worker_deadline,
            )

            def resolve(item: ExecutionRequest) -> CodexInputs:
                return CodexInputs(
                    item.workspace.key,
                    item.context_reference,
                    item.allocation_reference,
                    item.capability_reference,
                    str(workspace_path),
                    "Perform only the bounded synthetic source task.",
                    "synthetic-model",
                    "synthetic",
                )

            runtime = owner.runtime(
                resources,
                codex_binary.path,
                resolve=resolve,
                authorizers=store.runtime_authorizers(lambda: int(time.time())),
                active_authorizer=store.worker_active_authorizer(lambda: int(time.time())),
                clock=lambda: int(time.time()),
            )
            if request.param in {"edit-legacy", "edit-diagnostic"}:
                # Historical comparison only: host-safe adapter keeps the former native settings.
                runtime = CodexRuntime(
                    connection,
                    version=connection.version,
                    resolve=runtime.resolve,
                    authorize=runtime.authorize,
                    authorize_resolved=runtime.authorize_resolved,
                    authorize_recovery=runtime.authorize_recovery,
                    collect=runtime.collect,
                    clock=runtime.clock,
                    native_cwd=runtime.native_cwd,
                    authorize_active=runtime.authorize_active,
                    retire=runtime.retire,
                    settled=runtime.settled,
                )
            cell = NativeRuntime(
                root, store, execution, owner, resources, runtime, connection, str(request.param), codex_binary
            )
            yield cell
        finally:
            if connection is not None:
                connection.close()
            if (
                not (cell is not None and cell.store_closed)
                and store.find_artifact(operation_id, "oci-container") is not None
            ):
                owner.stop()
                deadline = time.monotonic() + 10
                while not owner.settled() and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert owner.settled(), "owned native namespace retirement remains unknown"


@pytest.mark.parametrize("native_runtime", ["final", "edit", "edit-legacy", "edit-diagnostic"], indirect=True)
def test_real_factory_editing_and_historical_workspace_write_gate(
    native_runtime: NativeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = native_runtime
    responses: list[dict[str, object]] = []
    original = cell.connection.request

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        result = original(method, params)
        responses.append({"method": method, "params": params, "response": result})
        return result

    monkeypatch.setattr(cell.connection, "request", request)
    handle = cell.runtime.start(cell.request)
    observed = cell.runtime.observe(handle, now=int(time.time()))
    cell.store.finalize_artifact(
        cell.owner.operation_id,
        "runtime-receipt",
        json.dumps(
            {
                "version": 1,
                "handle": handle,
                "fence": cell.request.fence,
                "identity": asdict(observed.identity),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    cell.store.observe(cell.owner.operation_id, cell.request.fence, "accepted", "accepted", reference=handle)
    deadline = time.monotonic() + 15
    notifications: list[dict[str, object]] = []
    read_error: str | None = None
    while observed.activity is not Activity.TERMINAL and time.monotonic() < deadline:
        observed = cell.runtime.observe(handle, now=int(time.time()))
        notifications.extend(cell.connection.notifications())
        time.sleep(0.1)
    if observed.activity is not Activity.TERMINAL:
        try:
            cell.connection.request("thread/read", {"threadId": handle.split(":")[1], "includeTurns": True})
        except CodexRPCError as error:
            read_error = str(error)
        notifications.extend(cell.connection.notifications())
    notifications.extend(cell.connection.notifications())
    (cell.root / "factory-receipt.json").write_text(
        json.dumps(
            {
                "handle": handle,
                "activity": observed.activity.value,
                "read_error": read_error,
                "notifications": notifications[:128],
                "responses": responses[:128],
                "workspace": cell.request.workspace.key,
                "base": cell.request.workspace.spec.base_revision,
                "scenario": cell.scenario,
            },
            sort_keys=True,
            indent=2,
        )
    )
    assert observed.activity is Activity.TERMINAL, (
        f"native Runtime terminal producer unreceived; evidence at {cell.root}"
    )
    candidate = cell.runtime.candidate(handle)
    assert candidate is not None and candidate.spec_digest == cell.request.attempt.digest
    assert cell.owner.settled()
    assert "original.txt" in {artifact.path for artifact in candidate.artifacts.artifacts}
    state = json.loads(read_bounded_regular(cell.resources.workspace / "mock/state.json"))
    assert state["last_step_kind"] == "final", (
        "hydrated terminality cannot substitute for a completed native model exchange"
    )
    native_items: list[dict[str, object]] = []
    for notification in notifications:
        if notification.get("method") == "item/completed":
            params = cast(dict[str, object], notification["params"])
            if params.get("turnId") == handle.split(":")[2]:
                item = cast(dict[str, object], params["item"])
                if item.get("type") == "commandExecution":
                    native_items.append(item)
    if cell.scenario == "edit":
        assert native_items and native_items[-1].get("exitCode") == 0
        assert (cell.resources.workspace / "candidate/result.txt").read_bytes() == b"native source edit\n"
        assert (cell.resources.workspace / "candidate/original.txt").read_bytes() == b"native updated source\n"
        proof = json.loads(read_bounded_regular(cell.resources.workspace / "candidate/security-receipt.json"))
        assert proof["version"] == "codex-cli 0.159.3"
        assert len(proof["denied_paths"]) == 6 and proof["namespaces"] == proof["subprocess_namespaces"]
        assert proof["init_fd_refusals"] and set(proof["init_fd_refusals"]) <= {1, 13}
        observed_boundary = json.loads(read_bounded_regular(cell.resources.workspace / "worker-observations.json"))[
            "self_observations"
        ]
        assert observed_boundary["CapEff"] == "0000000000000000"
        assert observed_boundary["NoNewPrivs"] == "1" and observed_boundary["Seccomp"] == "2"
        assert observed_boundary["cgroup_pids_max"] == "256"
        assert observed_boundary["cgroup_memory_max"] == "1073741824"
        assert observed_boundary["uid_map"].split() == ["0", str(os.getuid()), "1"]
        assert observed_boundary["network_interfaces"] == ["lo"] and len(observed_boundary["ipv4_routes"]) == 1
        assert all(
            path.read_bytes() == b"synthetic private canary" for path in (cell.root / "control/credentials").iterdir()
        )
        permission = json.loads(cell.store.artifact(cell.owner.operation_id, "oci-runtime-permissions"))
        assert permission["sandbox"] == "danger-full-access" and permission["approvalPolicy"] == "never"
        assert (
            permission["container"] == json.loads(cell.store.artifact(cell.owner.operation_id, "oci-container"))["id"]
        )
        thread = next(value for value in responses if value["method"] == "thread/start")
        assert cast(dict[str, object], thread["params"])["sandbox"] == "danger-full-access"
        files = {artifact.path for artifact in candidate.artifacts.artifacts}
        assert {"original.txt", "result.txt", "security-receipt.json"} <= files
        assert not any("credentials" in path or path.startswith(".git/") for path in files)
        assert (
            cell.store.artifact(
                cell.owner.operation_id, "oci-candidate-file:" + hashlib.sha256(b"original.txt").hexdigest()
            )
            == b"native updated source\n"
        )
    elif cell.scenario.startswith("edit"):
        # This is explicit negative conformance reception, not successful editing.
        assert not (cell.resources.workspace / "candidate/result.txt").exists()
        assert native_items, "missing correlated native command denial receipt"
        output = str(native_items[-1].get("aggregatedOutput"))
        expected = (
            "Numerical argument out of domain"
            if cell.scenario == "edit-diagnostic"
            else "No permissions to create a new namespace"
        )
        assert expected in output
        assert native_items[-1].get("exitCode") != 0
        (cell.root / "native-edit-gate.json").write_text(
            json.dumps(
                {
                    "conformance": "UNMET",
                    "reason": "workspace-write sandbox conflicts with unchanged outer namespace denial",
                    "scenario": cell.scenario,
                    "command_receipt": native_items[-1],
                    "policy_relaxed": False,
                },
                indent=2,
                sort_keys=True,
            )
        )


@pytest.mark.parametrize("native_runtime", ["final"], indirect=True)
def test_real_final_thread_admission_revocation_retires_without_prompt(native_runtime: NativeRuntime) -> None:
    cell = native_runtime

    def clock() -> int:
        return int(time.time())

    dispatch, recovery = cell.store.runtime_authorizers(clock)

    def revoke(request: ExecutionRequest, cwd: str, method: str) -> bool:
        assert method == "thread/start"
        cell.store.revoke(Principal("owner", "owner"), request.capability_reference)
        return dispatch(request, cwd, method)

    runtime = cell.owner.runtime(
        cell.resources,
        cell.codex.path,
        resolve=cell.runtime.resolve,
        authorizers=(revoke, recovery),
        active_authorizer=cell.store.worker_active_authorizer(clock),
        clock=clock,
    )
    with pytest.raises(ExecutionConflict, match="resolved thread inputs"):
        runtime.start(cell.request)
    with pytest.raises(OSError, match="transport closed"):
        cell.connection.request("thread/start", {})
    deadline = time.monotonic() + 10
    while not cell.owner.settled() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cell.owner.settled()
    assert runtime.reconcile(cell.request.operation).presence is Presence.UNKNOWN
    assert cell.store.find_artifact(cell.owner.operation_id, "runtime-dispatch:1:thread") is None
    assert cell.store.find_artifact(cell.owner.operation_id, "oci-candidate") is None
    assert (cell.resources.workspace / "candidate/original.txt").read_bytes() == b"exact bounded source\n"


@pytest.mark.parametrize("native_runtime", ["cancel", "revoke", "close"], indirect=True)
def test_real_factory_cancel_revocation_and_transport_loss_retire_detached_namespace(
    native_runtime: NativeRuntime,
) -> None:
    cell = native_runtime
    handle = cell.runtime.start(cell.request)
    observation = cell.runtime.observe(handle, now=int(time.time()))
    assert observation.activity is not Activity.TERMINAL
    cell.store.finalize_artifact(
        cell.owner.operation_id,
        "runtime-receipt",
        json.dumps(
            {
                "version": 1,
                "handle": handle,
                "fence": cell.request.fence,
                "identity": asdict(observation.identity),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    cell.store.observe(cell.owner.operation_id, cell.request.fence, "accepted", "accepted", reference=handle)
    descendant = json.loads(read_bounded_regular(cell.resources.workspace / "mock/process.json"))
    assert descendant["pid"] == descendant["session"], "backend did not enter an independent session"
    deadline = time.monotonic() + 15
    command_receipt = cell.resources.workspace / "candidate/subprocess.json"
    while not command_receipt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert command_receipt.exists(), "actual native detached subprocess did not start"
    command_descendant = json.loads(read_bounded_regular(command_receipt))
    assert command_descendant["pid"] == command_descendant["session"]
    assert command_descendant["namespace"] != os.readlink("/proc/self/ns/pid")
    assert (cell.resources.workspace / "candidate/in-flight.txt").read_bytes() == b"native authorized work"
    if cell.scenario == "revoke":
        cell.store.revoke(Principal("owner", "owner"), cell.request.capability_reference)
        observation = cell.runtime.observe(handle, now=int(time.time()))
    elif cell.scenario == "close":
        cell.connection.close()
        observation = cell.runtime.observe(handle, now=int(time.time()))
    else:
        observation = cell.runtime.cancel(handle)
    deadline = time.monotonic() + 10
    while observation.activity is not Activity.TERMINAL and time.monotonic() < deadline:
        observation = cell.runtime.observe(handle, now=int(time.time()))
        time.sleep(0.05)
    assert observation.activity is Activity.TERMINAL and observation.cancellation_requested
    assert cell.owner.settled()
    assert cell.runtime.candidate(handle) is None
    assert cell.store.find_artifact(cell.owner.operation_id, "oci-candidate") is None
    assert cell.resources.workspace.exists()
    (cell.root / "retirement-receipt.json").write_text(
        json.dumps(
            {
                "scenario": cell.scenario,
                "handle": handle,
                "detached_backend": descendant,
                "native_detached_subprocess": command_descendant,
                "namespace_settled": True,
                "candidate": None,
                "workspace_retained": True,
                "continuous_effect_mediation": False,
            },
            sort_keys=True,
            indent=2,
        )
    )


@pytest.mark.parametrize("native_runtime", ["lost-thread", "lost-turn"], indirect=True)
def test_real_native_lost_start_reply_cannot_authorize_another_send(
    native_runtime: NativeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = native_runtime
    original = cell.connection.request
    sent: list[str] = []
    lost = "thread/start" if cell.scenario == "lost-thread" else "turn/start"

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        result = original(method, params)
        sent.append(method)
        if method == lost:
            raise OSError("synthetic lost reply AFTER native execution")
        return result

    monkeypatch.setattr(cell.connection, "request", request)
    with pytest.raises(OSError, match="AFTER native execution"):
        cell.runtime.start(cell.request)
    before = tuple(sent)
    with pytest.raises(ExecutionConflict, match="uncertain"):
        cell.runtime.start(cell.request)
    assert tuple(sent) == before and sent.count(lost) == 1
    deadline = time.monotonic() + 10
    while not cell.owner.settled() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cell.owner.settled()
    assert cell.store.find_artifact(cell.owner.operation_id, "runtime-receipt") is None
    assert cell.store.operation(cell.owner.operation_id).attempts == 1
    assert cell.resources.workspace.exists()


@pytest.mark.parametrize("native_runtime", ["cold"], indirect=True)
def test_cold_native_ownership_never_recreates_kernel_handles_or_launches(native_runtime: NativeRuntime) -> None:
    cell = native_runtime
    warm = cell.owner
    cold = OwnedOCI(
        cell.store, cell.request, warm.podman, warm.profile, warm.name, warm.image, warm.environment, warm.invoke
    )
    assert cold.state() is Presence.FOUND and not cold.settled()
    with pytest.raises(ExecutionConflict, match="launch authority"):
        cold.connect(
            cell.resources,
            cell.codex.path,
            cell.connection.version,
            ("/toolchain/bin/python3.12", "-S", "/worker/main.py"),
            {"HOME": str(cell.resources.private_parent / "probe")},
            cell.connection.methods,
            cell.store.worker_authorizers(lambda: int(time.time())),
            lambda: int(time.time()),
        )
    cell.connection.close()
    deadline = time.monotonic() + 10
    while cold.state() is not Presence.ABSENT and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cold.state() is Presence.ABSENT and not cold.settled()
    assert cell.store.find_artifact(warm.operation_id, "oci-namespace-settled") is None
    assert warm.settled()
    assert cold.settled(), "only an already received exact retirement receipt can survive lost live handles"
    assert cell.store.operation(warm.operation_id).attempts == 1
    assert cell.resources.workspace.exists()


@pytest.mark.parametrize("native_runtime", ["object-drift", "candidate-drift"], indirect=True)
def test_active_original_generation_drift_retires_without_another_native_read(
    native_runtime: NativeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = native_runtime
    handle = cell.runtime.start(cell.request)
    calls: list[str] = []
    original = cell.connection.request

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append(method)
        return original(method, params)

    monkeypatch.setattr(cell.connection, "request", request)
    if cell.scenario == "object-drift":
        (cell.resources.protected[1] / "config").write_bytes(b"changed protected fixture original")
    else:
        candidate = cell.resources.workspace / cell.resources.candidate_root
        candidate.rename(candidate.with_name("retained-original-candidate"))
        candidate.mkdir()
    observation = cell.runtime.observe(handle, now=int(time.time()))
    deadline = time.monotonic() + 10
    while observation.activity is not Activity.TERMINAL and time.monotonic() < deadline:
        observation = cell.runtime.observe(handle, now=int(time.time()))
        time.sleep(0.05)
    assert observation.activity is Activity.TERMINAL and observation.cancellation_requested
    assert calls == [] and cell.owner.settled()
    assert cell.runtime.candidate(handle) is None
    assert cell.resources.workspace.exists()


@pytest.mark.parametrize("native_runtime", ["reopen"], indirect=True)
def test_real_sqlite_reopen_retains_witness_but_not_worker_authority(native_runtime: NativeRuntime) -> None:
    cell = native_runtime
    handle = cell.runtime.start(cell.request)
    cell.runtime.cancel(handle)
    deadline = time.monotonic() + 10
    while not cell.owner.settled() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cell.owner.settled()
    worker = cell.store.artifact(cell.owner.operation_id, f"runtime-dispatch:{cell.request.fence}:worker")
    witness = cell.store.artifact(cell.owner.operation_id, "oci-namespace-settled")
    binding = json.loads(worker)["launch"].encode()
    operation = cell.store.operation(cell.owner.operation_id)
    database = cell.resources.private_parent / "kernel.db"
    cell.store.close()
    cell.store_closed = True

    def decode(record: OperationRecord) -> OperationIntent:
        if record.request_json != operation.request_json:
            raise ValueError("reopened native intent differs")
        return OperationIntent(
            record.operation_id,
            record.effect_key,
            record.request_digest,
            "execute",
            cell.request.workspace.spec.repository,
            Path("."),
        )

    with SQLiteProgramStore(database, resolve_authority_intent=decode) as reopened:
        assert reopened.operation(operation.operation_id) == operation
        assert reopened.artifact(operation.operation_id, "oci-namespace-settled") == witness
        assert reopened.artifact(operation.operation_id, f"runtime-dispatch:{cell.request.fence}:worker") == worker
        assert not reopened.worker_active_authorizer(lambda: int(time.time()))(
            cell.request,
            str(cell.resources.workspace),
            binding,
        )
        cold = OwnedOCI(
            reopened,
            cell.request,
            cell.owner.podman,
            cell.owner.profile,
            cell.owner.name,
            cell.owner.image,
            cell.owner.environment,
            cell.owner.invoke,
        )
        assert cold.settled()
        with pytest.raises(ExecutionConflict, match="launch authority"):
            cold.connect(
                cell.resources,
                cell.codex.path,
                cell.codex.version,
                ("/toolchain/bin/python3.12", "-S", "/worker/main.py"),
                {"HOME": str(cell.resources.private_parent / "probe")},
                cell.connection.methods,
                reopened.worker_authorizers(lambda: int(time.time())),
                lambda: int(time.time()),
            )
    assert cell.resources.workspace.exists()


@pytest.mark.parametrize("native_runtime", ["observer"], indirect=True)
def test_observer_state_disappearance_and_owned_cancel_leave_bystander_alive(native_runtime: NativeRuntime) -> None:
    """Synthetic owner-served state lifetime, not a private/live IDE attachment."""
    cell = native_runtime
    python, _ = host_python_closure()
    with subprocess.Popen(  # noqa: S603 - fixed synthetic process, closed environment.
        [str(python), "-I", "-S", "-c", "import time; time.sleep(60)"],
        env={"HOME": str(cell.root), "PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        start_new_session=True,
    ) as bystander:
        try:
            handle = cell.runtime.start(cell.request)
            operation = cell.store.operation(cell.owner.operation_id)
            observer = {
                "revision": cell.store.load(cell.request.attempt.program_id).revision,
                "workspace": str(cell.resources.workspace),
                "candidate": str(cell.resources.workspace / cell.resources.candidate_root),
            }
            assert (Path(cast(str, observer["candidate"])) / "original.txt").read_bytes() == b"exact bounded source\n"
            del observer
            assert cell.store.operation(cell.owner.operation_id) == operation
            assert cell.owner.state() is Presence.FOUND and not cell.owner.settled()
            observation = cell.runtime.cancel(handle)
            deadline = time.monotonic() + 10
            while observation.activity is not Activity.TERMINAL and time.monotonic() < deadline:
                observation = cell.runtime.observe(handle, now=int(time.time()))
                time.sleep(0.05)
            assert observation.activity is Activity.TERMINAL and cell.owner.settled()
            assert bystander.poll() is None
            assert cell.resources.workspace.exists()
            assert (cell.resources.workspace / "candidate/original.txt").read_bytes() == b"exact bounded source\n"
        finally:
            bystander.terminate()
            try:
                bystander.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bystander.kill()
                bystander.wait(timeout=5)
