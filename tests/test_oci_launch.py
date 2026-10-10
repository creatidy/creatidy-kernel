# SPDX-License-Identifier: Apache-2.0
"""Consume the installed launch gate with synthetic resources and native stdio peers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from subprocess import CompletedProcess, TimeoutExpired
from typing import cast

import pytest
from test_codex_stdio import FAKE
from test_runtime_authority import Prepared

from creatidy_kernel.adapters import oci_bootstrap
from creatidy_kernel.adapters.bubblewrap_verification import resource_digest
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.oci_lifecycle import LABEL, LifetimeSpec, OCIResources, OwnedOCI
from creatidy_kernel.adapters.oci_lifetime import SO_PASSPIDFD, LifetimeChannel
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.worker_profile import WorkerMount, WorkerProfile
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.execution import ExecutionConflict

pytest_plugins = ["test_runtime_authority"]


@pytest.fixture
def grant_operations(request: pytest.FixtureRequest) -> frozenset[str]:
    return cast(frozenset[str], getattr(request, "param", frozenset({"read", "write", "execute"})))


@dataclass
class Launch:
    prepared: Prepared
    owner: OwnedOCI
    resources: OCIResources
    codex: Path
    time: list[int]
    calls: list[tuple[str, ...]]
    spawns: list[tuple[str, ...]]

    def connect(self) -> CodexStdio:
        return self.owner.connect(
            self.resources,
            self.codex,
            "0.99.1",
            ("/explicit/entrypoint",),
            {"HOME": str(self.resources.private_parent / "probe-home"), "PATH": "/usr/bin:/bin"},
            frozenset({"thread/read"}),
            self.prepared.store.worker_authorizers(lambda: self.time[0]),
            lambda: self.time[0],
        )


@pytest.fixture
def launch(prepared: Prepared, monkeypatch: pytest.MonkeyPatch) -> Launch:
    prepared.claim()
    root = prepared.database.parent
    engine = root / "engine"
    engine.mkdir(mode=0o700)
    podman, codex = engine / "podman", engine / "codex"
    podman.write_text(FAKE.replace("assert sys.argv[1:] == ['app-server']", "assert sys.argv[1] == 'run'"))
    codex.write_text(FAKE)
    podman.chmod(0o700)
    codex.chmod(0o700)
    seccomp, config, storage = (engine / name for name in ("seccomp.json", "containers.conf", "storage.conf"))
    for path in (seccomp, config, storage):
        path.write_bytes(b"controller-fixed synthetic policy/config")
        path.chmod(0o600)
    readonly = engine / "readonly"
    readonly.mkdir(mode=0o700)
    (readonly / "input").write_bytes(b"controller-selected immutable input")
    environment = {
        "PATH": str(engine),
        "HOME": str(engine / "home"),
        "TMPDIR": str(engine / "tmp"),
        "LC_ALL": "C",
        "USER": "synthetic",
        "XDG_CACHE_HOME": str(engine / "cache"),
        "XDG_CONFIG_HOME": str(engine / "config"),
        "XDG_DATA_HOME": str(engine / "data"),
        "XDG_RUNTIME_DIR": str(engine / "runtime"),
        "CONTAINERS_CONF": str(config),
        "CONTAINERS_STORAGE_CONF": str(storage),
    }
    profile = WorkerProfile(
        prepared.request.attempt.attempt_id,
        (
            WorkerMount(prepared.root, PurePosixPath("/workspace"), True),
            WorkerMount(readonly, PurePosixPath("/input"), False),
        ),
        (("PATH", "/usr/bin:/bin"),),
        seccomp,
        16,
        1024 * 1024,
        0.5,
        10,
    )
    calls: list[tuple[str, ...]] = []
    spawns: list[tuple[str, ...]] = []
    owner: OwnedOCI

    def invoke(argv: tuple[str, ...], supplied: Mapping[str, str]) -> CompletedProcess[bytes]:
        assert supplied == environment
        calls.append(argv)
        if argv[1:3] == ("image", "inspect"):
            assert prepared.store.find_artifact(owner.operation_id, "oci-owner") is None
            return CompletedProcess(argv, 0, ("sha256:" + owner.image + "\n").encode(), b"")
        assert argv[1:3] == ("container", "inspect")
        assert prepared.store.find_artifact(owner.operation_id, "runtime-dispatch:1:worker") is not None
        return CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {
                        "Id": "a" * 64,
                        "Image": owner.image,
                        "Name": owner.name,
                        "Config": {"Labels": {LABEL: owner.token}},
                    }
                ]
            ).encode(),
            b"",
        )

    owner = OwnedOCI(prepared.store, prepared.request, podman, profile, "owned-launch", "b" * 64, environment, invoke)
    resources = OCIResources(
        prepared.root,
        root,
        (prepared.database,),
        tuple(
            (path, hashlib.sha256(path.read_bytes()).hexdigest()) for path in (podman, codex, seccomp, config, storage)
        ),
        ((readonly, resource_digest(readonly)),),
    )
    original = subprocess.Popen

    def spawn(argv: tuple[str, ...], **kwargs: object) -> subprocess.Popen[bytes]:
        spawns.append(argv)
        if argv[0] == str(podman):
            assert prepared.store.find_artifact(owner.operation_id, "runtime-dispatch:1:worker") is not None
        return original(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", spawn)
    return Launch(prepared, owner, resources, codex, [1], calls, spawns)


def test_exact_resources_are_consumed_before_one_durable_worker_send(launch: Launch) -> None:
    connection = launch.connect()
    try:
        assert connection.request("thread/read", {"proof": True}) == {"ok": {"proof": True}}
        assert launch.spawns[0] == (str(launch.codex), "--version")
        argv = launch.spawns[1]
        assert argv[-2:] == (launch.owner.image, "/explicit/entrypoint")
        assert argv[argv.index("--label") + 1] == f"{LABEL}={launch.owner.token}"
        assert len(launch.spawns) == 2
        marker = launch.prepared.store.artifact(launch.owner.operation_id, "runtime-dispatch:1:worker")
        binding = json.loads(json.loads(marker)["launch"])
        assert binding["command"] == list(argv)
        assert launch.prepared.store.find_artifact(launch.owner.operation_id, "runtime-dispatch:1:thread") is None
        assert launch.prepared.store.find_artifact(launch.owner.operation_id, "oci-container") is not None
        with pytest.raises(ExecutionConflict, match="authority"):
            launch.connect()
        assert len(launch.spawns) == 2
    finally:
        connection.close()


@pytest.mark.parametrize("which", range(5))
def test_wrong_native_config_or_policy_pin_has_zero_native_calls(launch: Launch, which: int) -> None:
    pins = list(launch.resources.pins)
    path, _ = pins[which]
    pins[which] = (path, "c" * 64)
    launch.resources = replace(launch.resources, pins=tuple(pins))
    with pytest.raises(ExecutionConflict, match="pin"):
        launch.connect()
    assert launch.calls == launch.spawns == []


@pytest.mark.parametrize("mutation", ["writable", "extra", "nested", "reserved", "protected", "readonly", "parent"])
def test_mount_and_private_resource_refusals_prevent_execution(launch: Launch, mutation: str) -> None:
    profile = launch.owner.profile
    if mutation == "writable":
        profile = replace(profile, mounts=(*profile.mounts, WorkerMount(launch.codex, PurePosixPath("/more"), True)))
    elif mutation == "extra":
        profile = replace(profile, mounts=(*profile.mounts, WorkerMount(launch.codex, PurePosixPath("/more"), False)))
    elif mutation == "nested":
        profile = replace(
            profile,
            mounts=(profile.mounts[0], replace(profile.mounts[1], destination=PurePosixPath("/workspace/input"))),
        )
    elif mutation == "reserved":
        profile = replace(
            profile, mounts=(profile.mounts[0], replace(profile.mounts[1], destination=PurePosixPath("/proc/input")))
        )
    elif mutation == "protected":
        launch.resources = replace(launch.resources, protected=(launch.prepared.database.parent,))
    elif mutation == "readonly":
        (launch.resources.read_only[0][0] / "input").write_bytes(b"changed")
    else:
        launch.resources.private_parent.chmod(0o755)
    launch.owner = OwnedOCI(
        launch.prepared.store,
        launch.prepared.request,
        launch.owner.podman,
        profile,
        launch.owner.name,
        launch.owner.image,
        launch.owner.environment,
        launch.owner.invoke,
    )
    with pytest.raises((ExecutionConflict, ValueError)):
        launch.connect()
    assert launch.calls == launch.spawns == []


def test_readonly_hardlink_to_writable_workspace_is_refused(launch: Launch) -> None:
    os.link(launch.resources.read_only[0][0] / "input", launch.prepared.root / "mutable-alias")
    with pytest.raises(ExecutionConflict, match="hardlink"):
        launch.connect()
    assert launch.calls == launch.spawns == []


@pytest.mark.parametrize("failure", ["wrong", "multiple", "stderr", "error", "timeout"])
def test_ambiguous_image_identity_is_not_launch_permission(launch: Launch, failure: str) -> None:
    def invoke(argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        del environment
        launch.calls.append(argv)
        if failure == "timeout":
            raise TimeoutExpired("synthetic engine", 1)
        output = ("c" * 64 if failure == "wrong" else launch.owner.image).encode()
        return CompletedProcess(
            argv,
            125 if failure == "error" else 0,
            output + (b"\nextra" if failure == "multiple" else b""),
            b"diagnostic" if failure == "stderr" else b"",
        )

    owner = launch.owner
    launch.owner = OwnedOCI(
        owner.store, owner.request, owner.podman, owner.profile, owner.name, owner.image, owner.environment, invoke
    )
    with pytest.raises(ExecutionConflict, match="image identity"):
        launch.connect()
    assert len(launch.calls) == 1 and launch.spawns == []


@pytest.mark.parametrize("change", ["lease", "revoked", "resource", "identity"])
def test_change_during_version_probe_prevents_worker_send(
    launch: Launch, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    original = subprocess.Popen
    changed: list[str] = []

    def spawn(argv: tuple[str, ...], **kwargs: object) -> subprocess.Popen[bytes]:
        result = original(argv, **kwargs)  # type: ignore[arg-type]
        if argv[1] == "--version":
            if change == "lease":
                launch.time[0] = 11
            elif change == "revoked":
                launch.prepared.store.revoke(
                    Principal(launch.prepared.grant.issuer, "owner"), launch.prepared.grant.grant_id
                )
            elif change == "resource":
                launch.resources.pins[2][0].write_bytes(b"replaced policy")
            else:
                path = launch.resources.pins[2][0]
                replacement = path.with_suffix(".replacement")
                replacement.write_bytes(path.read_bytes())
                replacement.chmod(path.stat().st_mode & 0o777)
                replacement.replace(path)
            changed.append(change)
        return result

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises((ExecutionConflict, ValueError)):
        launch.connect()
    assert launch.spawns == [(str(launch.codex), "--version")]
    assert changed == [change]
    assert launch.prepared.store.find_artifact(launch.owner.operation_id, "runtime-dispatch:1:worker") is None
    assert launch.prepared.store.find_artifact(launch.owner.operation_id, "oci-owner") is not None


def test_reopened_controller_cannot_adopt_worker_launch_lease(launch: Launch) -> None:
    launch.prepared.store.close()
    launch.prepared.store = SQLiteProgramStore(launch.prepared.database)
    owner = launch.owner
    launch.owner = OwnedOCI(
        launch.prepared.store,
        owner.request,
        owner.podman,
        owner.profile,
        owner.name,
        owner.image,
        owner.environment,
        owner.invoke,
    )
    with pytest.raises(ExecutionConflict, match="authority"):
        launch.connect()
    assert launch.calls == launch.spawns == []


@pytest.mark.parametrize("topology", ["symlink", "fifo", "hardlink"])
def test_native_resource_topology_cannot_bypass_pins(launch: Launch, topology: str) -> None:
    path = launch.resources.pins[2][0]
    if topology == "hardlink":
        os.link(path, launch.prepared.root / "alias")
    else:
        retained = path.with_suffix(".original")
        path.rename(retained)
        if topology == "symlink":
            path.symlink_to(retained)
        else:
            os.mkfifo(path)
    with pytest.raises((ExecutionConflict, ValueError)):
        launch.connect()
    assert launch.calls == launch.spawns == []


def test_foreign_workspace_owner_is_not_a_private_resource(launch: Launch, monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.fstat
    inode = launch.prepared.root.stat().st_ino

    def fstat(descriptor: int) -> os.stat_result:
        result = original(descriptor)
        if result.st_ino == inode:
            fields = list(result)
            fields[4] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(ExecutionConflict, match="ownership"):
        launch.connect()
    assert launch.calls == launch.spawns == []


def test_expiry_after_reservation_is_retained_not_retried(launch: Launch, monkeypatch: pytest.MonkeyPatch) -> None:
    original = launch.prepared.store.finalize_artifact

    def reserve(operation: str, name: str, data: bytes) -> str:
        result = original(operation, name, data)
        if name == "runtime-dispatch:1:worker":
            launch.time[0] = 11
        return result

    monkeypatch.setattr(launch.prepared.store, "finalize_artifact", reserve)
    with pytest.raises(ValueError, match="authorization"):
        launch.connect()
    assert launch.spawns == [(str(launch.codex), "--version")]
    assert launch.prepared.store.find_artifact(launch.owner.operation_id, "runtime-dispatch:1:worker") is not None
    launch.time[0] = 1
    with pytest.raises(ExecutionConflict, match="authority"):
        launch.connect()
    assert len(launch.spawns) == 1


def test_native_start_failure_never_reauthorizes_send(launch: Launch, monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen

    def spawn(argv: tuple[str, ...], **kwargs: object) -> subprocess.Popen[bytes]:
        if argv[0] == str(launch.owner.podman):
            assert (
                launch.prepared.store.find_artifact(launch.owner.operation_id, "runtime-dispatch:1:worker") is not None
            )
            raise OSError("synthetic uncertain worker startup")
        return original(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(OSError, match="startup"):
        launch.connect()
    assert launch.spawns == [(str(launch.codex), "--version")]
    with pytest.raises(ExecutionConflict, match="authority"):
        launch.connect()
    assert len(launch.spawns) == 1


def test_failed_owned_receipt_closes_transport_without_another_launch(
    launch: Launch, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = launch.owner.invoke

    def invoke(argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        if argv[1:3] == ("container", "inspect"):
            raise TimeoutExpired("synthetic receipt", 1)
        return original(argv, environment)

    owner = launch.owner
    launch.owner = OwnedOCI(
        owner.store, owner.request, owner.podman, owner.profile, owner.name, owner.image, owner.environment, invoke
    )
    closed: list[bool] = []
    original_close = CodexStdio.close

    def close(connection: CodexStdio) -> None:
        original_close(connection)
        closed.append(True)

    monkeypatch.setattr(CodexStdio, "close", close)
    with pytest.raises(ExecutionConflict, match="unavailable"):
        launch.connect()
    assert closed and len(launch.spawns) == 2
    assert launch.prepared.store.find_artifact(launch.owner.operation_id, "oci-container") is None
    with pytest.raises(ExecutionConflict, match="authority"):
        launch.connect()
    assert len(launch.spawns) == 2


def test_revoked_original_grant_prevents_even_resource_or_native_preflight(launch: Launch) -> None:
    launch.prepared.store.revoke(Principal(launch.prepared.grant.issuer, "owner"), launch.prepared.grant.grant_id)
    launch.resources = replace(launch.resources, workspace=Path("/does/not/exist"))
    with pytest.raises(ExecutionConflict, match="authority"):
        launch.connect()
    assert launch.calls == launch.spawns == []


def test_uncertain_existing_native_thread_cannot_gain_another_worker(launch: Launch) -> None:
    dispatch, _ = launch.prepared.store.runtime_authorizers(lambda: 1)
    assert dispatch(launch.prepared.request, str(launch.prepared.root), "thread/start")
    with pytest.raises(ExecutionConflict, match="authority"):
        launch.connect()
    assert launch.calls == launch.spawns == []


@pytest.mark.parametrize("grant_operations", [frozenset({"execute"}), frozenset({"execute", "read"})], indirect=True)
def test_execute_permission_alone_does_not_authorize_writable_project(launch: Launch) -> None:
    with pytest.raises(ExecutionConflict, match="authority"):
        launch.connect()
    assert launch.calls == launch.spawns == []


def test_entry_gate_requires_exact_prior_worker_and_has_no_replay(launch: Launch) -> None:
    request, cwd = launch.prepared.request, str(launch.prepared.root)
    enter = launch.prepared.store.worker_entry_authorizer(lambda: 1)
    binding = b"exact-controller-verified-binding"
    assert not enter(request, cwd, binding)
    _, dispatch = launch.prepared.store.worker_authorizers(lambda: 1)
    assert dispatch(request, cwd, binding)
    assert not enter(request, cwd, b"different")
    assert enter(request, cwd, binding)
    assert not enter(request, cwd, binding)
    assert launch.calls == launch.spawns == []


def test_worker_deadline_is_original_upper_bound_not_lease_renewal(launch: Launch) -> None:
    assert launch.prepared.store.worker_deadline(launch.prepared.request, str(launch.prepared.root)) == 11
    assert launch.prepared.store.operation(launch.owner.operation_id).lease_until == 11


def _retired(launch: Launch) -> OwnedOCI:
    """Simulated durable controller retirement; NOT a native namespace receipt."""
    launch.connect().close()
    owner = launch.owner
    stage = json.loads(owner.store.artifact(owner.operation_id, "runtime-dispatch:1:worker"))
    namespace = json.dumps(
        {
            "version": 1,
            "owner": owner.token,
            "container": "a" * 64,
            "identity": {"simulated": True},
            "binding_reference": hashlib.sha256(stage["launch"].encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    owner.store.finalize_artifact(owner.operation_id, "oci-init", namespace)
    owner.store.finalize_artifact(
        owner.operation_id,
        "oci-namespace-settled",
        json.dumps(
            {
                "version": 1,
                "owner": owner.token,
                "container": "a" * 64,
                "namespace_reference": hashlib.sha256(namespace).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )

    def invoke(argv: tuple[str, ...], _environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        assert argv[1:] == ("container", "exists", "a" * 64)
        return CompletedProcess(argv, 1, b"", b"")

    return OwnedOCI(
        owner.store, owner.request, owner.podman, owner.profile, owner.name, owner.image, owner.environment, invoke
    )


def test_retirement_gates_exact_controller_candidate_bytes_and_excludes_private_root(launch: Launch) -> None:
    owner = _retired(launch)
    candidate_root = launch.prepared.root / "candidate"
    candidate_root.mkdir()
    (candidate_root / "result.txt").write_bytes(b"exact candidate bytes")
    (launch.prepared.root / "private-runtime.json").write_bytes(b"must not be published")
    candidate = owner.collect_candidate(launch.resources)
    assert candidate.attempt_id == launch.prepared.request.attempt.attempt_id
    assert [item.path for item in candidate.artifacts.artifacts] == ["result.txt"]
    name = "oci-candidate-file:" + hashlib.sha256(b"result.txt").hexdigest()
    assert owner.store.artifact(owner.operation_id, name) == b"exact candidate bytes"
    assert owner.store.find_artifact(owner.operation_id, "oci-candidate") is not None
    assert (launch.prepared.root / "private-runtime.json").exists()


def test_absence_without_exact_namespace_receipt_never_authorizes_candidate(launch: Launch) -> None:
    launch.connect().close()
    owner = launch.owner

    def invoke(argv: tuple[str, ...], _environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        return CompletedProcess(argv, 1, b"", b"")

    absent = OwnedOCI(
        owner.store, owner.request, owner.podman, owner.profile, owner.name, owner.image, owner.environment, invoke
    )
    with pytest.raises(ExecutionConflict, match="settlement"):
        absent.collect_candidate(launch.resources)
    assert owner.store.find_artifact(owner.operation_id, "oci-candidate") is None
    assert launch.prepared.root.exists()


@pytest.mark.parametrize("mutation", ["root", "symlink", "inode"])
def test_candidate_root_changes_refuse_without_manifest(launch: Launch, mutation: str) -> None:
    owner = _retired(launch)
    root = launch.prepared.root / "candidate"
    root.mkdir()
    (root / "result").write_bytes(b"data")
    if mutation == "root":
        launch.resources = replace(launch.resources, candidate_root=PurePosixPath("private-runtime"))
    elif mutation == "symlink":
        (root / "malicious").symlink_to(launch.prepared.database)
    else:
        old = launch.prepared.root.with_name("retained-original")
        launch.prepared.root.rename(old)
        launch.prepared.root.mkdir()
    with pytest.raises(ExecutionConflict):
        owner.collect_candidate(launch.resources)
    assert owner.store.find_artifact(owner.operation_id, "oci-candidate") is None


@pytest.mark.parametrize("revoked", [False, True])
def test_real_composition_consumes_entry_gate_before_release(
    launch: Launch, monkeypatch: pytest.MonkeyPatch, revoked: bool
) -> None:
    """Mock only kernel ownership in this unit cell; native bootstrap has its own receipt."""
    bootstrap = Path(oci_bootstrap.__file__).resolve()
    profile = replace(
        launch.owner.profile,
        mounts=(*launch.owner.profile.mounts, WorkerMount(bootstrap, PurePosixPath("/worker/lifetime.py"), False)),
    )
    launch.resources = replace(
        launch.resources, read_only=(*launch.resources.read_only, (bootstrap, resource_digest(bootstrap)))
    )
    identity: dict[str, object] = {"pid": 1234567, "simulated": True}
    reader, writer = os.pipe()
    released: list[bool] = []
    original_option = socket.socket.setsockopt

    def option(stream: socket.socket, level: int, name: int, value: int) -> None:
        if name != SO_PASSPIDFD:
            original_option(stream, level, name, value)

    monkeypatch.setattr(socket.socket, "setsockopt", option)

    def receive(channel: LifetimeChannel, *, timeout: float) -> dict[str, object]:
        assert timeout == 15
        channel.pidfd = reader
        channel.identity = identity
        if revoked:
            launch.prepared.store.revoke(
                Principal(launch.prepared.grant.issuer, "owner"), launch.prepared.grant.grant_id
            )
        return identity

    monkeypatch.setattr(LifetimeChannel, "receive", receive)
    original_release = LifetimeChannel.release

    def release(channel: LifetimeChannel) -> None:
        assert (
            launch.prepared.store.find_artifact(launch.owner.operation_id, "runtime-dispatch:1:worker-enter")
            is not None
        )
        released.append(True)
        original_release(channel)

    monkeypatch.setattr(LifetimeChannel, "release", release)

    def invoke(argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        del environment
        owner = launch.owner
        if argv[1:3] == ("image", "inspect"):
            return CompletedProcess(argv, 0, owner.image.encode(), b"")
        assert argv[1:3] == ("container", "inspect")
        return CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {
                        "Id": "a" * 64,
                        "Image": owner.image,
                        "Name": owner.name,
                        "Config": {"Labels": {LABEL: owner.token}},
                        "State": {"Pid": 1234567, "Running": True},
                    }
                ]
            ).encode(),
            b"",
        )

    owner = launch.owner
    launch.owner = OwnedOCI(
        owner.store, owner.request, owner.podman, profile, owner.name, owner.image, owner.environment, invoke
    )
    connection: CodexStdio | None = None
    try:

        def connect() -> CodexStdio:
            return launch.owner.connect(
                launch.resources,
                launch.codex,
                "0.99.1",
                ("/explicit/entrypoint",),
                {"HOME": str(launch.resources.private_parent / "probe-home")},
                frozenset({"thread/read"}),
                launch.prepared.store.worker_authorizers(lambda: 1),
                lambda: 1,
                lifetime=LifetimeSpec(PurePosixPath("/toolchain/bin/python"), PurePosixPath("/worker/lifetime.py")),
                enter_authorizer=launch.prepared.store.worker_entry_authorizer(lambda: 1),
                deadline=launch.prepared.store.worker_deadline,
            )

        if revoked:
            with pytest.raises(ValueError, match="startup gate"):
                connect()
            assert released == []
        else:
            connection = connect()
            assert released == [True]
            assert any(part.startswith("--preserve-fd=") for part in launch.spawns[1])
        assert launch.prepared.store.find_artifact(launch.owner.operation_id, "oci-init") is not None
    finally:
        if connection is not None:
            connection.close()
        lifetime = launch.owner._lifetime  # pyright: ignore[reportPrivateUsage] - exact fixture-owned descriptors.
        if lifetime is not None:
            lifetime.close()
        os.close(writer)
