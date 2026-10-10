# SPDX-License-Identifier: Apache-2.0
"""Durable ownership observations over exact Podman-shaped synthetic results."""

import json
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from subprocess import CompletedProcess, TimeoutExpired

import pytest
from test_runtime_authority import Prepared
from test_worker_profile import profile

from creatidy_kernel.adapters.oci_lifecycle import LABEL, OwnedOCI
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.execution import ExecutionConflict, Overlay, Presence, TrustMode

pytest_plugins = ["test_runtime_authority"]


class Peer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.owner: OwnedOCI | None = None
        self.code = 0
        self.stdout = b""
        self.stderr = b""
        self.foreign = False
        self.failure: Exception | None = None
        self.identifier = "a" * 64

    def invoke(self, argv: tuple[str, ...], environment: Mapping[str, str]) -> CompletedProcess[bytes]:
        assert self.owner is not None
        assert self.owner.store.find_artifact(self.owner.operation_id, "oci-owner") == self.owner.frame
        assert environment == self.owner.environment
        self.calls.append(argv)
        if self.failure is not None:
            raise self.failure
        data = self.stdout
        if argv[1:3] == ("container", "inspect") and self.code == 0 and not data:
            data = json.dumps(
                [
                    {
                        "Id": self.identifier,
                        "Image": self.owner.image,
                        "Name": self.owner.name,
                        "Config": {"Labels": {LABEL: "foreign-owner" if self.foreign else self.owner.token}},
                        "State": {"Running": True},
                    }
                ]
            ).encode()
        return CompletedProcess(argv, self.code, data, self.stderr)


def owned(prepared: Prepared, peer: Peer) -> OwnedOCI:
    root = prepared.database.parent / "synthetic-engine"
    environment = {
        "PATH": str(root / "bin"),
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "LC_ALL": "C",
        "USER": "nobody",
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "CONTAINERS_CONF": str(root / "containers.conf"),
        "CONTAINERS_STORAGE_CONF": str(root / "storage.conf"),
    }
    owner = OwnedOCI(
        prepared.store,
        prepared.request,
        root / "podman",
        replace(profile(), attempt=prepared.request.attempt.attempt_id),
        "owned-attempt",
        "sha256:" + "b" * 64,
        environment,
        peer.invoke,
    )
    peer.owner = owner
    return owner


def test_reservation_is_durable_not_a_launch_permission(prepared: Prepared) -> None:
    peer = Peer()
    owner = owned(prepared, peer)
    with pytest.raises(ExecutionConflict, match="unclaimed"):
        owner.bind_before_send(now=1)
    prepared.claim()
    assert owner.bind_before_send(now=1) == ("--label", f"{LABEL}={owner.token}")
    assert peer.calls == []
    assert owner.state() is Presence.UNKNOWN  # Marker/name cannot establish launch or absence.
    assert peer.calls == []
    with pytest.raises(ExecutionConflict, match="uncertain"):
        owner.bind_before_send(now=1)
    prepared.store.close()
    with SQLiteProgramStore(prepared.database, resolve_authority_intent=prepared.decoder) as store:
        prepared.store = store
        recovered = owned(prepared, peer)
        assert recovered.frame == owner.frame
        with pytest.raises(ExecutionConflict, match="uncertain"):
            recovered.bind_before_send(now=2)
        assert recovered.state() is Presence.UNKNOWN


def test_captured_id_survives_reopen_and_mutations_never_use_name(prepared: Prepared) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    assert owner.capture() == peer.identifier
    assert peer.calls[-1][-1] == owner.name  # Inspection only.
    prepared.store.close()
    with SQLiteProgramStore(prepared.database, resolve_authority_intent=prepared.decoder) as store:
        prepared.store = store
        recovered = owned(prepared, peer)
        assert recovered.capture() == peer.identifier
        recovered.stop()
        assert peer.calls[-1][1:] == ("stop", "-t", "5", peer.identifier)
        assert all(call[-1] == peer.identifier for call in peer.calls[1:])
        assert recovered.state() is Presence.FOUND  # Stop ack is not settlement.
        peer.code = 1
        recovered.require_absent()


def test_foreign_label_or_changed_id_never_authorizes_stop(prepared: Prepared) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    peer.foreign = True
    with pytest.raises(ExecutionConflict, match="foreign"):
        owner.stop()
    assert all(call[1] != "stop" for call in peer.calls)
    peer.foreign = False
    owner.capture()
    peer.identifier = "c" * 64
    with pytest.raises(ExecutionConflict, match="foreign"):
        owner.stop()
    assert all(call[1] != "stop" for call in peer.calls)


@pytest.mark.parametrize(
    "code,stdout,stderr", [(125, b"", b""), (1, b"diagnostic", b""), (1, b"", b"diagnostic"), (0, b"noise", b"")]
)
def test_cli_failures_or_diagnostics_are_unknown_not_absence(
    prepared: Prepared, code: int, stdout: bytes, stderr: bytes
) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    owner.capture()
    peer.code, peer.stdout, peer.stderr = code, stdout, stderr
    assert owner.state() is Presence.UNKNOWN
    with pytest.raises(ExecutionConflict, match="unproved"):
        owner.require_absent()


def test_oversize_timeout_and_malformed_inspection_fail_closed(prepared: Prepared) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    peer.stdout = b"not JSON"
    with pytest.raises(ExecutionConflict, match="invalid"):
        owner.capture()
    peer.stdout = b""
    owner.capture()
    peer.failure = TimeoutError("synthetic bounded observation timeout")
    assert owner.state() is Presence.UNKNOWN
    peer.failure = None
    peer.stdout = b"a" * (1024 * 1024 + 1)
    assert owner.state() is Presence.UNKNOWN


def test_generation_and_environment_are_immutable(prepared: Prepared) -> None:
    peer = Peer()
    owner = owned(prepared, peer)
    with pytest.raises(FrozenInstanceError):
        owner.name = "foreign"  # type: ignore[misc] - deliberate attack on a frozen generation.
    with pytest.raises(TypeError):
        owner.environment["HOME"] = "/other"  # type: ignore[index] - deliberate immutable mapping attack.


def test_changed_fence_cannot_reuse_binding(prepared: Prepared) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    prepared.request = replace(prepared.request, fence=2)
    changed = owned(prepared, peer)
    with pytest.raises(ExecutionConflict, match="differs"):
        changed.state()
    assert peer.calls == []


@pytest.mark.parametrize("failure", [OSError("synthetic engine error"), TimeoutExpired("podman", 1)])
def test_native_observation_failures_never_prove_absence(prepared: Prepared, failure: Exception) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    owner.capture()
    peer.failure = failure
    assert owner.state() is Presence.UNKNOWN
    with pytest.raises(ExecutionConflict, match="unproved"):
        owner.require_absent()
    with pytest.raises(ExecutionConflict, match="unavailable"):
        owner.stop()
    assert all(call[1] != "stop" for call in peer.calls)


@pytest.mark.parametrize("field", ["base", "scope", "overlay", "mode", "grant", "identity"])
def test_changed_request_cannot_adopt_owned_container(prepared: Prepared, field: str) -> None:
    prepared.claim()
    peer = Peer()
    original = owned(prepared, peer)
    original.bind_before_send(now=1)
    original.capture()
    request = prepared.request
    spec = request.workspace.spec
    if field == "base":
        spec = replace(spec, base_revision="different")
    elif field == "scope":
        spec = replace(spec, network_destinations=frozenset({"different"}), mounts=frozenset({"different"}))
    elif field == "overlay":
        spec = replace(spec, overlays=(Overlay("changed", "different"),))
    elif field == "mode":
        spec = replace(spec, trust_mode=TrustMode.ISOLATED)
    elif field == "grant":
        request = replace(request, capability_reference="different")
    else:
        request = replace(request, identity=replace(request.identity, observed="different"))
    prepared.request = replace(request, workspace=replace(request.workspace, spec=spec))
    changed = owned(prepared, peer)
    calls = len(peer.calls)
    with pytest.raises(ExecutionConflict, match="differs"):
        changed.stop()
    assert len(peer.calls) == calls


def test_uncaptured_or_malformed_receipt_is_not_absence(prepared: Prepared) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    with pytest.raises(ExecutionConflict, match="unproved"):
        owner.require_absent()
    prepared.store.finalize_artifact(owner.operation_id, "oci-container", b'{"id":"short","owner":"foreign"}')
    with pytest.raises(ExecutionConflict, match="invalid durable"):
        owner.state()
    assert peer.calls == []


def test_inspection_diagnostic_and_conflicting_receipt_refuse_mutation(prepared: Prepared) -> None:
    prepared.claim()
    peer = Peer()
    owner = owned(prepared, peer)
    owner.bind_before_send(now=1)
    peer.stderr = b"engine diagnostic"
    with pytest.raises(ExecutionConflict, match="unavailable"):
        owner.capture()
    assert prepared.store.find_artifact(owner.operation_id, "oci-container") is None
    peer.stderr = b""
    owner.capture()
    with pytest.raises(OperationConflict, match="immutable"):
        prepared.store.finalize_artifact(owner.operation_id, "oci-container", b"different")
    assert all(call[1] != "stop" for call in peer.calls)
