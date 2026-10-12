# SPDX-License-Identifier: Apache-2.0
"""Consume the installed launch gate with synthetic resources and native stdio peers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from subprocess import CompletedProcess, TimeoutExpired
from typing import cast

import pytest
from test_codex_runtime import NativeConnection
from test_codex_stdio import FAKE
from test_runtime_authority import Prepared

from creatidy_kernel.adapters import oci_bootstrap
from creatidy_kernel.adapters.bubblewrap_verification import resource_digest
from creatidy_kernel.adapters.codex_runtime import CodexInputs, CodexRuntime
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.git_workspace import GitWorkspace
from creatidy_kernel.adapters.oci_lifecycle import LABEL, MAX_NATIVE_PIN_BYTES, LifetimeSpec, OCIResources, OwnedOCI
from creatidy_kernel.adapters.oci_lifetime import SO_PASSPIDFD, LifetimeChannel
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.worker_profile import WorkerMount, WorkerProfile
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.execution import Activity, ExecutionConflict, ExecutionRequest, Presence, UnsupportedExecution

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


def test_active_worker_check_requires_exact_entry_and_survives_receipt_without_new_send(launch: Launch) -> None:
    prepared = launch.prepared
    request, cwd, binding = prepared.request, str(prepared.root), b"exact bounded active binding"
    active = prepared.store.worker_active_authorizer(lambda: launch.time[0])
    _, dispatch = prepared.store.worker_authorizers(lambda: 1)
    assert not active(request, cwd, binding)
    assert dispatch(request, cwd, binding)
    assert not active(request, cwd, binding)
    assert prepared.store.worker_entry_authorizer(lambda: 1)(request, cwd, binding)
    assert not active(request, cwd, b"different")
    assert active(request, cwd, binding)
    assert active(request, cwd, binding)
    runtime, _ = prepared.runtime()
    handle = runtime.start(request)
    prepared.receipt(runtime, handle)
    assert prepared.store.operation(launch.owner.operation_id).status == "accepted"
    assert active(request, cwd, binding)
    assert prepared.store.find_artifact(launch.owner.operation_id, "runtime-dispatch:1:worker-active") is None
    launch.time[0] = 11
    assert not active(request, cwd, binding)
    assert prepared.store.operation(launch.owner.operation_id).lease_until == 11
    assert launch.calls == launch.spawns == []


def test_reopened_controller_cannot_adopt_active_worker_authority(launch: Launch) -> None:
    prepared = launch.prepared
    binding = b"original bounded binding"
    _, dispatch = prepared.store.worker_authorizers(lambda: 1)
    assert dispatch(prepared.request, str(prepared.root), binding)
    assert prepared.store.worker_entry_authorizer(lambda: 1)(prepared.request, str(prepared.root), binding)
    prepared.store.close()
    with SQLiteProgramStore(prepared.database) as restarted:
        assert not restarted.worker_active_authorizer(lambda: 1)(prepared.request, str(prepared.root), binding)
        assert restarted.find_artifact(launch.owner.operation_id, "runtime-dispatch:1:worker-active") is None
    assert launch.calls == launch.spawns == []


def _retired(launch: Launch) -> OwnedOCI:
    """Simulated durable controller retirement; NOT a native namespace receipt."""
    launch.connect().close()
    owner = launch.owner
    stage = json.loads(owner.store.artifact(owner.operation_id, "runtime-dispatch:1:worker"))
    assert launch.prepared.store.worker_entry_authorizer(lambda: launch.time[0])(
        owner.request, str(launch.prepared.root), stage["launch"].encode()
    )
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
    candidate = owner.collect_candidate(
        launch.resources,
        codex=launch.codex,
        authorizer=launch.prepared.store.worker_active_authorizer(lambda: launch.time[0]),
    )
    assert candidate.attempt_id == launch.prepared.request.attempt.attempt_id
    assert candidate.spec_digest == launch.prepared.request.attempt.digest
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
        owner.collect_candidate(
            launch.resources,
            codex=launch.codex,
            authorizer=launch.prepared.store.worker_active_authorizer(lambda: launch.time[0]),
        )
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
        channel.namespace_fd = os.open(launch.prepared.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
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


@dataclass
class RuntimeLaunch:
    launch: Launch
    runtime: CodexRuntime
    connection: CodexStdio
    exited: list[bool]
    absent: list[bool]
    calls: list[str]


@pytest.fixture
def runtime_launch(launch: Launch, monkeypatch: pytest.MonkeyPatch) -> Iterator[RuntimeLaunch]:
    """Real stdio and journal admission; kernel lifetime/engine observations are simulated."""
    script = FAKE.replace("0.99.1", "0.159.3").replace(
        "assert sys.argv[1:] == ['app-server']", "assert sys.argv[1] == 'run'"
    )
    script = script.replace(
        "print(json.dumps({'id': message['id'], 'result': {'ok': message['params']}}), flush=True)",
        """method = message['method']
        if method == 'thread/start':
            assert message['params']['cwd'] == '/workspace'
            assert message['params']['sandbox'] == 'danger-full-access'
            assert message['params']['approvalPolicy'] == 'never'
            result = {'thread': {'id': 'thread-one'}, 'model': 'model-one', 'modelProvider': 'provider-one'}
        elif method == 'turn/start':
            result = {'turn': {'id': 'turn-one'}}
        elif method == 'turn/interrupt':
            result = {}
        else:
            assert method == 'thread/read'
            result = {'thread': {'id': 'thread-one', 'turns': [{'id': 'turn-one', 'status': 'completed'}]}}
        print(json.dumps({'id': message['id'], 'result': result}), flush=True)""",
    )
    launch.owner.podman.write_text(script)
    launch.codex.write_text(FAKE.replace("0.99.1", "0.159.3"))
    launch.resources = replace(
        launch.resources,
        pins=tuple((path, hashlib.sha256(path.read_bytes()).hexdigest()) for path, _ in launch.resources.pins),
    )
    connection = launch.owner.connect(
        launch.resources,
        launch.codex,
        "0.159.3",
        ("/explicit/entrypoint",),
        {"HOME": str(launch.resources.private_parent / "probe-home")},
        frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"}),
        launch.prepared.store.worker_authorizers(lambda: launch.time[0]),
        lambda: launch.time[0],
    )
    original_option = socket.socket.setsockopt

    def option(stream: socket.socket, level: int, name: int, value: int) -> None:
        if name != SO_PASSPIDFD:
            original_option(stream, level, name, value)

    monkeypatch.setattr(socket.socket, "setsockopt", option)
    channel = LifetimeChannel(launch.owner.token)
    channel.identity = {"simulated": True}
    reader, writer = os.pipe()
    channel.pidfd = reader
    channel.namespace_fd = os.open(launch.prepared.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    exited, absent = [False], [False]

    def namespace_exited(_channel: LifetimeChannel) -> bool:
        return exited[0]

    monkeypatch.setattr(LifetimeChannel, "exited", namespace_exited)

    def namespace_live(value: LifetimeChannel) -> bool:
        return value.pidfd is not None and value.namespace_fd is not None and not exited[0]

    monkeypatch.setattr(LifetimeChannel, "live", namespace_live)
    object.__setattr__(launch.owner, "_lifetime", channel)
    connection.before_close = channel.close_liveness
    stage = json.loads(launch.owner.store.artifact(launch.owner.operation_id, "runtime-dispatch:1:worker"))
    assert launch.prepared.store.worker_entry_authorizer(lambda: 1)(
        launch.prepared.request, str(launch.prepared.root), stage["launch"].encode()
    )
    launch.owner.store.finalize_artifact(
        launch.owner.operation_id,
        "oci-init",
        json.dumps(
            {
                "owner": launch.owner.token,
                "container": "a" * 64,
                "identity": channel.identity,
                "binding_reference": hashlib.sha256(stage["launch"].encode()).hexdigest(),
                "version": 1,
            }
        ).encode(),
    )
    original_invoke = launch.owner.invoke

    def invoke(argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        if argv[1:3] == ("container", "exists"):
            assert argv[-1] == "a" * 64
            return CompletedProcess(argv, 1 if absent[0] else 0, b"", b"")
        if argv[1] == "stop":
            assert argv[-1] == "a" * 64
            return CompletedProcess(argv, 0, b"", b"")
        return original_invoke(argv, environment)

    object.__setattr__(launch.owner, "invoke", invoke)
    original_request = connection.request
    calls: list[str] = []

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append(method)
        return original_request(method, params)

    monkeypatch.setattr(connection, "request", request)
    original, _ = launch.prepared.runtime()
    runtime = launch.owner.runtime(
        launch.resources,
        launch.codex,
        resolve=original.resolve,
        authorizers=launch.prepared.store.runtime_authorizers(lambda: launch.time[0]),
        active_authorizer=launch.prepared.store.worker_active_authorizer(lambda: launch.time[0]),
        clock=lambda: launch.time[0],
    )
    try:
        yield RuntimeLaunch(launch, runtime, connection, exited, absent, calls)
    finally:
        connection.close()
        channel.close()
        os.close(writer)


def test_owned_runtime_terminal_and_candidate_wait_for_both_retirement_witnesses(runtime_launch: RuntimeLaunch) -> None:
    cell = runtime_launch
    root = cell.launch.prepared.root / "candidate"
    root.mkdir()
    (root / "result").write_bytes(b"exact bounded result")
    handle = cell.runtime.start(cell.launch.prepared.request)
    assert cell.runtime.start(cell.launch.prepared.request) == handle
    cell.launch.prepared.receipt(cell.runtime, handle)
    assert cell.runtime.observe(handle, now=2).activity is Activity.UNKNOWN
    assert cell.runtime.candidate(handle) is None
    assert cell.calls.count("thread/start") == cell.calls.count("turn/start") == 1
    cell.absent[0] = True
    assert cell.runtime.observe(handle, now=3).activity is Activity.UNKNOWN
    assert cell.runtime.candidate(handle) is None
    cell.exited[0] = True
    observation = cell.runtime.observe(handle, now=4)
    assert observation.activity is Activity.TERMINAL
    assert not observation.cancellation_requested
    candidate = cell.runtime.candidate(handle)
    assert candidate is not None and candidate.spec_digest == cell.launch.prepared.request.attempt.digest
    assert cell.runtime.candidate(handle) == candidate
    assert cell.launch.prepared.root.exists()
    assert cell.runtime.reconcile(cell.launch.prepared.request.operation, handle).presence is Presence.UNKNOWN


def test_owned_permission_exception_is_journaled_against_original_launch(runtime_launch: RuntimeLaunch) -> None:
    cell = runtime_launch
    cell.runtime.start(cell.launch.prepared.request)
    permission = json.loads(
        cell.launch.prepared.store.artifact(cell.launch.owner.operation_id, "oci-runtime-permissions")
    )
    launch = json.loads(
        cell.launch.prepared.store.artifact(cell.launch.owner.operation_id, "runtime-dispatch:1:worker")
    )
    assert permission == {
        "version": 1,
        "owner": cell.launch.owner.token,
        "container": "a" * 64,
        "launch_reference": hashlib.sha256(launch["launch"].encode()).hexdigest(),
        "codex_version": "0.159.3",
        "sandbox": "danger-full-access",
        "approvalPolicy": "never",
        "owner_decision": "2026-10-11T13:09:41Z:Kernel53:PR85",
    }


@pytest.mark.parametrize("retarget", ["connection", "cwd"])
def test_owned_permission_exception_cannot_retarget_host_execution(
    runtime_launch: RuntimeLaunch, retarget: str
) -> None:
    cell = runtime_launch
    host = NativeConnection()
    if retarget == "connection":
        cell.runtime.connection = host
    else:
        cell.runtime.native_cwd = "/unverified-host-workspace"
    with pytest.raises(ExecutionConflict, match="cannot retarget"):
        cell.runtime.start(cell.launch.prepared.request)
    assert host.calls == [] and cell.calls == []
    assert cell.launch.prepared.store.find_artifact(cell.launch.owner.operation_id, "oci-runtime-permissions") is None
    with pytest.raises(OSError, match="transport closed"):
        cell.connection.request("thread/start", {})


def test_unapproved_owned_codex_version_retires_without_fallback(
    runtime_launch: RuntimeLaunch, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = runtime_launch
    monkeypatch.setattr(CodexStdio, "version", property(lambda self: "0.99.1"))
    with pytest.raises(UnsupportedExecution, match="sandbox exception requires Codex 0.159.3"):
        cell.launch.owner.runtime(
            cell.launch.resources,
            cell.launch.codex,
            resolve=cell.runtime.resolve,
            authorizers=cell.launch.prepared.store.runtime_authorizers(lambda: cell.launch.time[0]),
            active_authorizer=cell.launch.prepared.store.worker_active_authorizer(lambda: cell.launch.time[0]),
            clock=lambda: cell.launch.time[0],
        )
    assert cell.calls == []
    with pytest.raises(OSError, match="transport closed"):
        cell.connection.request("thread/start", {})


@pytest.mark.parametrize("initial", ["observe", "candidate"])
@pytest.mark.parametrize("failure_at", ["inspect", "stop"])
def test_completed_retirement_failure_preserves_candidate_until_exact_settlement(
    runtime_launch: RuntimeLaunch, monkeypatch: pytest.MonkeyPatch, initial: str, failure_at: str
) -> None:
    cell = runtime_launch
    root = cell.launch.prepared.root / "candidate"
    root.mkdir()
    (root / "result").write_bytes(b"completed result survives retirement retry")
    handle = cell.runtime.start(cell.launch.prepared.request)
    native_request = cell.connection.request

    def pending(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "thread/read":
            return {"thread": {"id": "thread-one", "turns": [{"id": "turn-one", "status": "inProgress"}]}}
        return native_request(method, params)

    monkeypatch.setattr(cell.connection, "request", pending)
    cell.launch.prepared.receipt(cell.runtime, handle)
    monkeypatch.setattr(cell.connection, "request", native_request)
    original = cell.launch.owner.invoke
    failed = False

    def invoke(argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        nonlocal failed
        if not failed and (argv[1:3] == ("container", "inspect") and failure_at == "inspect" or argv[1] == "stop"):
            failed = True
            return CompletedProcess(argv, 125, b"", b"synthetic transient retirement observation")
        return original(argv, environment)

    object.__setattr__(cell.launch.owner, "invoke", invoke)
    with pytest.raises(ExecutionConflict):
        if initial == "observe":
            cell.runtime.observe(handle, now=2)
        else:
            cell.runtime.candidate(handle)
    assert failed
    observation = cell.runtime.observe(handle, now=3)
    assert observation.activity is Activity.UNKNOWN and not observation.cancellation_requested
    assert cell.runtime.candidate(handle) is None
    assert cell.calls.count("thread/read") == 1
    cell.exited[0] = cell.absent[0] = True
    candidate = cell.runtime.candidate(handle)
    assert candidate is not None and candidate.spec_digest == cell.launch.prepared.request.attempt.digest
    assert cell.runtime.candidate(handle) == candidate
    observation = cell.runtime.observe(handle, now=4)
    assert observation.activity is Activity.TERMINAL and not observation.cancellation_requested
    assert cell.calls.count("thread/start") == cell.calls.count("turn/start") == cell.calls.count("thread/read") == 1
    assert cell.launch.prepared.root.exists()


@pytest.mark.parametrize("mutation", ["expired", "revoked", "resource"])
def test_owned_runtime_current_authority_refusal_retires_without_read_or_candidate(
    runtime_launch: RuntimeLaunch, mutation: str
) -> None:
    cell = runtime_launch
    handle = cell.runtime.start(cell.launch.prepared.request)
    if mutation == "expired":
        cell.launch.time[0] = 11
    elif mutation == "revoked":
        prepared = cell.launch.prepared
        prepared.store.revoke(Principal(prepared.grant.issuer, "owner"), prepared.grant.grant_id)
    else:
        cell.launch.resources.pins[2][0].write_bytes(b"changed")
    count = len(cell.calls)
    observation = cell.runtime.observe(handle, now=2)
    assert observation.activity is Activity.UNKNOWN and observation.cancellation_requested
    assert len(cell.calls) == count
    assert cell.runtime.candidate(handle) is None
    assert cell.launch.prepared.root.exists()
    cell.absent[0] = cell.exited[0] = True
    assert cell.runtime.observe(handle, now=3).activity is Activity.TERMINAL
    assert cell.runtime.candidate(handle) is None


@pytest.mark.parametrize("lost_at", ["thread/start", "turn/start", "turn/interrupt"])
def test_owned_runtime_uncertain_rpc_retires_only_its_boundary_without_retry(
    runtime_launch: RuntimeLaunch, monkeypatch: pytest.MonkeyPatch, lost_at: str
) -> None:
    cell = runtime_launch
    original = cell.connection.request

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == lost_at:
            raise OSError("synthetic lost response")
        return original(method, params)

    monkeypatch.setattr(cell.connection, "request", request)
    if lost_at == "turn/interrupt":
        handle = cell.runtime.start(cell.launch.prepared.request)
        assert cell.runtime.cancel(handle).activity is Activity.UNKNOWN
        assert cell.runtime.candidate(handle) is None
        cell.exited[0] = cell.absent[0] = True
        assert cell.runtime.cancel(handle).activity is Activity.TERMINAL
    else:
        with pytest.raises(OSError, match="lost response"):
            cell.runtime.start(cell.launch.prepared.request)
        with pytest.raises(ExecutionConflict, match="uncertain"):
            cell.runtime.start(cell.launch.prepared.request)
    assert cell.runtime.reconcile(cell.launch.prepared.request.operation).presence is Presence.UNKNOWN
    assert cell.launch.prepared.root.exists()


@pytest.mark.parametrize("mutation", ["expired", "revoked", "error"])
def test_final_thread_admission_refusal_retires_prelaunched_owned_worker(
    runtime_launch: RuntimeLaunch, mutation: str
) -> None:
    cell = runtime_launch
    prepared = cell.launch.prepared
    dispatch, recovery = prepared.store.runtime_authorizers(lambda: cell.launch.time[0])

    def guarded(request: ExecutionRequest, cwd: str, method: str) -> bool:
        assert method == "thread/start"
        if mutation == "expired":
            cell.launch.time[0] = 11
        elif mutation == "revoked":
            prepared.store.revoke(Principal("owner", "owner"), prepared.grant.grant_id)
        else:
            raise ExecutionConflict("synthetic final admission failure")
        return dispatch(request, cwd, method)

    runtime = cell.launch.owner.runtime(
        cell.launch.resources,
        cell.launch.codex,
        resolve=cell.runtime.resolve,
        authorizers=(guarded, recovery),
        active_authorizer=prepared.store.worker_active_authorizer(lambda: cell.launch.time[0]),
        clock=lambda: cell.launch.time[0],
    )
    lifetime = cell.launch.owner._lifetime  # pyright: ignore[reportPrivateUsage] - fixture-owned lifetime.
    assert lifetime is not None and lifetime.controller.fileno() >= 0
    with pytest.raises(ExecutionConflict):
        runtime.start(prepared.request)
    assert cell.calls == []
    assert lifetime.controller.fileno() == -1
    with pytest.raises(OSError, match="transport closed"):
        cell.connection.request("thread/start", {})
    assert runtime.reconcile(prepared.request.operation).presence is Presence.UNKNOWN
    assert prepared.store.find_artifact(cell.launch.owner.operation_id, "oci-candidate") is None
    assert prepared.root.exists()


@pytest.mark.parametrize("failure", ["prompt", "cwd", "sandbox", "resolver", "retirement"])
def test_initial_input_refusal_retires_only_its_bound_worker(runtime_launch: RuntimeLaunch, failure: str) -> None:
    cell = runtime_launch
    original = cell.runtime.resolve

    def invalid(request: ExecutionRequest) -> CodexInputs:
        if failure == "resolver":
            raise OSError("synthetic input resolution failure")
        value = original(request)
        if failure == "cwd":
            return replace(value, cwd="/different-host-workspace")
        if failure == "sandbox":
            return replace(value, sandbox="danger-full-access")
        return replace(value, prompt="")

    cell.runtime.resolve = invalid
    if failure == "retirement":
        original_invoke = cell.launch.owner.invoke

        def uncertain(argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
            if argv[1] == "stop":
                return CompletedProcess(argv, 125, b"", b"synthetic uncertain retirement")
            return original_invoke(argv, environment)

        object.__setattr__(cell.launch.owner, "invoke", uncertain)
    lifetime = cell.launch.owner._lifetime  # pyright: ignore[reportPrivateUsage] - fixture-owned lifetime.
    assert lifetime is not None
    expected = OSError if failure == "resolver" else UnsupportedExecution if failure == "sandbox" else ExecutionConflict
    with pytest.raises(expected) as raised:
        cell.runtime.start(cell.launch.prepared.request)
    if failure == "retirement":
        assert "resolved Codex inputs" in str(raised.value)
        assert isinstance(raised.value.__cause__, ExecutionConflict)
    assert cell.calls == [] and lifetime.controller.fileno() == -1
    with pytest.raises(OSError, match="transport closed"):
        cell.connection.request("thread/start", {})
    assert cell.runtime.reconcile(cell.launch.prepared.request.operation).presence is Presence.UNKNOWN
    assert cell.launch.prepared.store.find_artifact(cell.launch.owner.operation_id, "runtime-dispatch:1:thread") is None
    assert cell.launch.prepared.root.exists()


def test_different_request_input_refusal_does_not_retire_bound_worker(runtime_launch: RuntimeLaunch) -> None:
    cell = runtime_launch
    lifetime = cell.launch.owner._lifetime  # pyright: ignore[reportPrivateUsage] - fixture-owned lifetime.
    assert lifetime is not None
    with pytest.raises(ExecutionConflict, match="differs from owned Attempt"):
        cell.runtime.start(replace(cell.launch.prepared.request, fence=2))
    assert cell.calls == [] and lifetime.controller.fileno() >= 0
    assert cell.runtime.start(cell.launch.prepared.request) == "codex:thread-one:turn-one"


@pytest.mark.parametrize("method", ["observe", "candidate", "reconcile"])
@pytest.mark.parametrize("uncertain_stop", [False, True])
def test_active_journal_failure_retires_before_preserving_validation_error(
    runtime_launch: RuntimeLaunch, monkeypatch: pytest.MonkeyPatch, method: str, uncertain_stop: bool
) -> None:
    cell = runtime_launch
    handle = cell.runtime.start(cell.launch.prepared.request)
    lifetime = cell.launch.owner._lifetime  # pyright: ignore[reportPrivateUsage] - fixture-owned lifetime.
    assert lifetime is not None
    original_invoke = cell.launch.owner.invoke
    if uncertain_stop:

        def uncertain(argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
            if argv[1] == "stop":
                return CompletedProcess(argv, 125, b"", b"synthetic uncertain retirement")
            return original_invoke(argv, environment)

        object.__setattr__(cell.launch.owner, "invoke", uncertain)

    def unavailable(*args: object, **kwargs: object) -> bool:
        raise sqlite3.OperationalError("synthetic current authority unavailable")

    with monkeypatch.context() as failed_journal:
        failed_journal.setattr(cell.launch.prepared.store, "check", unavailable)
        with pytest.raises(sqlite3.OperationalError, match="current authority unavailable") as raised:
            if method == "observe":
                cell.runtime.observe(handle, now=2)
            elif method == "candidate":
                cell.runtime.candidate(handle)
            else:
                cell.runtime.reconcile(cell.launch.prepared.request.operation, handle)
        if uncertain_stop:
            assert isinstance(raised.value.__cause__, ExecutionConflict)
    assert lifetime.controller.fileno() == -1 and cell.calls == ["thread/start", "turn/start"]
    with pytest.raises(OSError, match="transport closed"):
        cell.connection.request("thread/read", {})
    object.__setattr__(cell.launch.owner, "invoke", original_invoke)
    cell.exited[0] = cell.absent[0] = True
    observation = cell.runtime.observe(handle, now=3)
    assert observation.activity is Activity.TERMINAL and observation.cancellation_requested
    assert cell.runtime.candidate(handle) is None
    assert cell.launch.prepared.root.exists()


def test_non_gated_factory_connection_cannot_be_presented_as_owned_runtime(launch: Launch) -> None:
    connection = launch.connect()
    original, _ = launch.prepared.runtime()
    try:
        with pytest.raises(ExecutionConflict, match="gated OCI connection"):
            launch.owner.runtime(
                launch.resources,
                launch.codex,
                resolve=original.resolve,
                authorizers=launch.prepared.store.runtime_authorizers(lambda: 1),
                active_authorizer=launch.prepared.store.worker_active_authorizer(lambda: 1),
                clock=lambda: 1,
            )
    finally:
        connection.close()


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_failed_native_candidate_still_retires_owned_namespace(
    runtime_launch: RuntimeLaunch, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    cell = runtime_launch
    original = cell.connection.request

    def request(method: str, params: dict[str, object]) -> dict[str, object]:
        result = original(method, params)
        if method == "thread/read":
            return {"thread": {"id": "thread-one", "turns": [{"id": "turn-one", "status": status}]}}
        return result

    monkeypatch.setattr(cell.connection, "request", request)
    handle = cell.runtime.start(cell.launch.prepared.request)
    assert cell.runtime.candidate(handle) is None

    with pytest.raises(OSError, match="transport closed"):
        cell.connection.request("thread/read", {"threadId": "thread-one"})
    assert cell.launch.owner.store.find_artifact(cell.launch.owner.operation_id, "oci-candidate") is None
    assert cell.runtime.observe(handle, now=2).activity is Activity.UNKNOWN
    cell.exited[0] = cell.absent[0] = True
    assert cell.runtime.observe(handle, now=3).activity is Activity.TERMINAL
    assert cell.runtime.candidate(handle) is None


@pytest.fixture
def source_bound_launch(launch: Launch, sqlite_tmp_path: Path, request: pytest.FixtureRequest) -> GitWorkspace:
    source = Path(tempfile.mkdtemp(prefix="bound-source-", dir=sqlite_tmp_path.parent))

    def cleanup_source() -> None:
        shutil.rmtree(source)

    request.addfinalizer(cleanup_source)
    assert source.parent == sqlite_tmp_path.parent
    assert source.stat().st_dev == launch.prepared.database.stat().st_dev
    reference_git(source, "init", "--template=")
    (source / "original.txt").write_bytes(b"exact materialized input")
    reference_git(source, "add", "-A")
    reference_git(source, "commit", "-m", "synthetic source")
    reference_git(source, "remote", "add", "origin", "https://forge.invalid/Owner/repo")
    original_request = launch.prepared.request
    spec = replace(original_request.workspace.spec, base_revision=reference_git(source, "rev-parse", "HEAD").strip())
    root = launch.prepared.root
    root.rmdir()  # This generated fixture root is empty and has never been dispatched.
    workspace = GitWorkspace(
        launch.prepared.store,
        source,
        "https://forge.invalid/Owner/repo",
        spec.repository,
        root,
        root.parent / "objects.git",
    )
    handle = workspace.materialize(original_request.workspace.key, spec)
    launch.prepared.request = replace(original_request, workspace=handle)
    prior = launch.owner
    owner: OwnedOCI

    def invoke(argv: tuple[str, ...], _environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        launch.calls.append(argv)
        if argv[1:3] == ("image", "inspect"):
            return CompletedProcess(argv, 0, ("sha256:" + owner.image + "\n").encode(), b"")
        if argv[1:3] == ("container", "inspect"):
            record = {
                "Id": "a" * 64,
                "Name": owner.name,
                "Image": owner.image,
                "Config": {"Labels": {LABEL: owner.token}},
            }
            return CompletedProcess(argv, 0, json.dumps([record]).encode(), b"")
        assert argv[1:3] == ("container", "exists")
        return CompletedProcess(argv, 1, b"", b"")

    owner = OwnedOCI(
        prior.store,
        launch.prepared.request,
        prior.podman,
        prior.profile,
        prior.name,
        prior.image,
        prior.environment,
        invoke,
    )
    launch.owner = owner
    resources = replace(launch.resources, protected=(*launch.resources.protected, workspace.objects))
    launch.resources = workspace.bind(
        owner,
        resources,
        codex=launch.codex,
        collection_authorizer=launch.prepared.store.worker_active_authorizer(lambda: launch.time[0]),
    )
    return workspace


def test_real_factory_consumes_source_generation_and_settlement_collection(
    source_bound_launch: GitWorkspace, launch: Launch
) -> None:
    workspace = source_bound_launch
    _retired(launch)
    manifest = workspace.collect(launch.prepared.request.workspace)
    assert {artifact.path for artifact in manifest.artifacts} == {"original.txt"}
    assert all(not artifact.path.startswith(".git") for artifact in manifest.artifacts)
    assert workspace.reconcile(launch.prepared.request.workspace.key).presence is Presence.FOUND
    receipt = launch.prepared.store.artifact(launch.owner.operation_id, "oci-candidate")
    assert receipt
    assert workspace.objects.exists()


@pytest.mark.parametrize("mutation", ["objects", "candidate", "unprotected", "mapping"])
def test_source_generation_refusal_occurs_before_native_probe_or_worker(
    source_bound_launch: GitWorkspace, launch: Launch, mutation: str
) -> None:
    workspace = source_bound_launch
    if mutation == "objects":
        (workspace.objects / "config").write_bytes(b"changed protected original")
    elif mutation == "candidate":
        candidate = launch.prepared.root / "candidate"
        candidate.rename(candidate.with_name("retained-candidate"))
        candidate.mkdir()
    elif mutation == "unprotected":
        launch.resources = replace(launch.resources, protected=(launch.prepared.database,))
    else:
        launch.resources = replace(launch.resources, candidate_root=PurePosixPath("different"))
    with pytest.raises((ExecutionConflict, OSError, ValueError, RuntimeError)):
        launch.connect()
    assert launch.calls == []
    assert not any(argv and argv[0] in {str(launch.codex), str(launch.owner.podman)} for argv in launch.spawns)


def test_native_raw_pin_and_readonly_closure_are_independently_verified(launch: Launch) -> None:
    prior = launch.owner
    profile = replace(
        prior.profile,
        mounts=(*prior.profile.mounts, WorkerMount(launch.codex, PurePosixPath("/toolchain/codex/codex"), False)),
    )
    owner = OwnedOCI(
        prior.store, prior.request, prior.podman, profile, prior.name, prior.image, prior.environment, prior.invoke
    )
    resources = replace(
        launch.resources, read_only=(*launch.resources.read_only, (launch.codex, resource_digest(launch.codex)))
    )
    assert resources.verify(owner, launch.codex)
    changed = replace(
        resources,
        read_only=tuple((path, "f" * 64 if path == launch.codex else digest) for path, digest in resources.read_only),
    )
    with pytest.raises(ExecutionConflict, match="content differs"):
        changed.verify(owner, launch.codex)
    changed = replace(
        resources, pins=tuple((path, "f" * 64 if path == launch.codex else digest) for path, digest in resources.pins)
    )
    with pytest.raises(ExecutionConflict, match="content differs"):
        changed.verify(owner, launch.codex)
    assert launch.calls == launch.spawns == []


def test_pinned_native_byte_bound_refuses_before_probe_or_worker(
    launch: Launch, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.lstat

    def lstat(path: Path) -> os.stat_result:
        info = original(path)
        if path == launch.codex:
            return os.stat_result((*info[:6], MAX_NATIVE_PIN_BYTES + 1, *info[7:]))
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ExecutionConflict, match="native/config resource"):
        launch.connect()
    assert launch.calls == launch.spawns == []


@pytest.mark.parametrize("late", ["expired", "revoked", "resource"])
def test_new_collection_checks_authority_after_confirmed_terminal_retirement(
    runtime_launch: RuntimeLaunch, late: str
) -> None:
    cell = runtime_launch
    candidate = cell.launch.prepared.root / "candidate"
    candidate.mkdir()
    (candidate / "result").write_bytes(b"retained but not authorized for new collection")
    handle = cell.runtime.start(cell.launch.prepared.request)
    cell.runtime.observe(handle, now=2)
    cell.exited[0] = cell.absent[0] = True
    assert cell.runtime.observe(handle, now=3).activity is Activity.TERMINAL
    before = tuple(cell.calls)
    if late == "expired":
        cell.launch.time[0] = 11
    elif late == "revoked":
        cell.launch.prepared.store.revoke(Principal("owner", "owner"), cell.launch.prepared.grant.grant_id)
    else:
        cell.launch.resources.pins[2][0].write_bytes(b"changed after retirement")
    with pytest.raises(ExecutionConflict):
        cell.runtime.candidate(handle)
    assert tuple(cell.calls) == before
    assert cell.launch.prepared.store.find_artifact(cell.launch.owner.operation_id, "oci-candidate") is None
    assert (candidate / "result").read_bytes() == b"retained but not authorized for new collection"


@pytest.mark.parametrize("mutation", ["unknown", "missing", "malformed"])
def test_unsupported_init_shape_cannot_establish_settlement_or_collect(
    launch: Launch, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    _retired(launch)
    original = launch.prepared.store.find_artifact
    namespace = json.loads(launch.prepared.store.artifact(launch.owner.operation_id, "oci-init"))
    if mutation == "unknown":
        namespace["version"] = 2
    elif mutation == "missing":
        del namespace["binding_reference"]
    else:
        namespace["binding_reference"] = True

    def artifact(operation_id: str, name: str) -> bytes | None:
        return json.dumps(namespace).encode() if name == "oci-init" else original(operation_id, name)

    monkeypatch.setattr(launch.prepared.store, "find_artifact", artifact)
    with pytest.raises(ExecutionConflict, match="namespace receipt"):
        launch.owner.settled()
    assert launch.prepared.root.exists()


@pytest.mark.parametrize("late", ["expired", "revoked", "resource", "reopened"])
def test_direct_workspace_collection_cannot_bypass_current_authority(
    source_bound_launch: GitWorkspace, launch: Launch, late: str
) -> None:
    workspace = source_bound_launch
    owner = _retired(launch)
    if late == "expired":
        launch.time[0] = 11
    elif late == "revoked":
        launch.prepared.store.revoke(Principal("owner", "owner"), launch.prepared.grant.grant_id)
    elif late == "resource":
        launch.resources.pins[2][0].write_bytes(b"changed after namespace retirement")
    else:
        launch.prepared.store.close()
        reopened = SQLiteProgramStore(launch.prepared.database)
        try:
            owner = OwnedOCI(
                reopened,
                owner.request,
                owner.podman,
                owner.profile,
                owner.name,
                owner.image,
                owner.environment,
                owner.invoke,
            )
            with pytest.raises(ExecutionConflict):
                owner.collect_candidate(
                    launch.resources, codex=launch.codex, authorizer=reopened.worker_active_authorizer(lambda: 1)
                )
            assert reopened.find_artifact(owner.operation_id, "oci-candidate") is None
        finally:
            reopened.close()
        assert (workspace.workspace / "candidate/original.txt").read_bytes() == b"exact materialized input"
        return
    with pytest.raises(ExecutionConflict):
        workspace.collect(launch.prepared.request.workspace)
    with pytest.raises(ExecutionConflict):
        owner.collect_candidate(
            launch.resources,
            codex=launch.codex,
            authorizer=launch.prepared.store.worker_active_authorizer(lambda: launch.time[0]),
        )
    assert launch.prepared.store.find_artifact(owner.operation_id, "oci-candidate") is None
    assert (workspace.workspace / "candidate/original.txt").read_bytes() == b"exact materialized input"


def test_retirement_without_collection_authorizer_is_not_new_read_permission(launch: Launch) -> None:
    owner = _retired(launch)
    with pytest.raises(ExecutionConflict, match="authority unavailable"):
        owner.collect_candidate(launch.resources)
    assert launch.prepared.store.find_artifact(owner.operation_id, "oci-candidate") is None
