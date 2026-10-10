# SPDX-License-Identifier: Apache-2.0
"""Durable owned-container reconciliation, not a launcher or isolation attestation.

Podman 6.1.3 exposes Id/Image/Name/Config.Labels in its container-inspect array.
Container-exists returns 0/1 for present/absent; other outcomes remain unknown.
Its CgroupPath is a hierarchy path, not an absolute filesystem resource: this
adapter deliberately does not infer descendant settlement or cgroup enforcement.
Trusted composition supplies verified resources and a bounded native CLI executor.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from types import MappingProxyType
from typing import cast

from creatidy_kernel.adapters.worker_profile import WorkerProfile, rootless_run_prefix
from creatidy_kernel.core.execution import ExecutionConflict, ExecutionRequest, Presence
from creatidy_kernel.ports.allocation import encode_allocation
from creatidy_kernel.ports.program_store import ApplicationStore

Invoke = Callable[[tuple[str, ...], Mapping[str, str]], CompletedProcess[bytes]]
LABEL = "com.creatidy.kernel.owner"
ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "LC_ALL",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "CONTAINERS_CONF",
        "CONTAINERS_STORAGE_CONF",
    }
)


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in cast(dict[object, object], value)):
        raise ExecutionConflict("invalid OCI observation")
    return cast(dict[str, object], value)


def _digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


@dataclass(frozen=True, slots=True, init=False)
class OwnedOCI:
    """One immutable Operation/Attempt generation, never ownership inferred from a name."""

    store: ApplicationStore
    request: ExecutionRequest
    podman: Path
    environment: Mapping[str, str]
    name: str
    image: str
    invoke: Invoke
    operation_id: str
    frame: bytes
    token: str

    def __init__(
        self,
        store: ApplicationStore,
        request: ExecutionRequest,
        podman: Path,
        profile: WorkerProfile,
        name: str,
        image_config_digest: str,
        environment: Mapping[str, str],
        invoke: Invoke,
    ) -> None:
        image = image_config_digest.removeprefix("sha256:")
        if not _digest(image):
            raise ValueError("exact image configuration digest required")
        if profile.attempt != request.attempt.attempt_id:
            raise ExecutionConflict("OCI profile Attempt differs")
        supplied = dict(environment)
        if frozenset(supplied) != ENVIRONMENT_KEYS or any(
            type(value) is not str or not value or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in supplied.values()
        ):
            raise ValueError("explicit closed engine environment required")
        workspace = request.workspace.spec
        frame = _bytes(
            {
                "version": 1,
                "operation": request.operation.operation_id,
                "effect": request.operation.effect_key,
                "request": request.operation.request_digest,
                "attempt": request.attempt.digest,
                "workspace": {
                    "key": request.workspace.key,
                    "repository": workspace.repository,
                    "base_revision": workspace.base_revision,
                    "overlays": [asdict(item) for item in workspace.overlays],
                    "toolchain_digest": workspace.toolchain_digest,
                    "enforcement_profile": workspace.enforcement_profile,
                    "trust_mode": workspace.trust_mode.value,
                    "network_destinations": sorted(workspace.network_destinations),
                    "mounts": sorted(workspace.mounts),
                },
                "context": request.context_reference,
                "allocation_reference": request.allocation_reference,
                "allocation": encode_allocation(request.allocation).decode()
                if request.allocation is not None
                else None,
                "grant": request.capability_reference,
                "identity": asdict(request.identity),
                "fence": request.fence,
                "prefix": rootless_run_prefix(podman, profile, name),
                "image": image,
                "environment": supplied,
                "deadline_seconds": profile.deadline_seconds,
            }
        )
        for key, value in (
            ("store", store),
            ("request", request),
            ("invoke", invoke),
            ("podman", podman),
            ("environment", MappingProxyType(supplied)),
            ("name", name),
            ("image", image),
            ("operation_id", request.operation.operation_id),
            ("frame", frame),
            ("token", hashlib.sha256(frame).hexdigest()),
        ):
            object.__setattr__(self, key, value)

    def _call(self, *arguments: str) -> CompletedProcess[bytes]:
        result = self.invoke((str(self.podman), *arguments), dict(self.environment))
        if (
            type(result.returncode) is not int
            or type(result.stdout) is not bytes
            or type(result.stderr) is not bytes
            or len(result.stdout) + len(result.stderr) > 1024 * 1024
        ):
            raise ExecutionConflict("invalid bounded OCI CLI result")
        return result

    def bind_before_send(self, *, now: int) -> tuple[str, str]:
        """Irreversible reservation only; independent current authority still gates launch."""
        operation = self.store.operation(self.operation_id)
        if (
            operation.effect_key != self.request.operation.effect_key
            or operation.request_digest != self.request.operation.request_digest
            or operation.fence != self.request.fence
            or operation.status != "dispatched"
            or operation.attempts != 1
            or operation.lease_until is None
            or type(now) is not int
            or now < 0
            or now >= operation.lease_until
            or self.store.find_artifact(self.operation_id, "oci-owner") is not None
        ):
            raise ExecutionConflict("OCI generation unclaimed, expired or already uncertain")
        self.store.finalize_artifact(self.operation_id, "oci-owner", self.frame)
        return "--label", f"{LABEL}={self.token}"

    def _receipt(self) -> str | None:
        if self.store.find_artifact(self.operation_id, "oci-owner") != self.frame:
            raise ExecutionConflict("OCI generation differs from durable binding")
        data = self.store.find_artifact(self.operation_id, "oci-container")
        if data is None:
            return None
        try:
            receipt = _object(json.loads(data))
        except ValueError as error:
            raise ExecutionConflict("invalid durable OCI receipt") from error
        identifier = receipt.get("id")
        if set(receipt) != {"id", "owner"} or receipt["owner"] != self.token or not _digest(identifier):
            raise ExecutionConflict("invalid durable OCI receipt")
        return cast(str, identifier)

    def capture(self) -> str:
        """Name lookup is inspection only; mutations use the durably captured full ID."""
        identifier = self._receipt()
        try:
            result = self._call("container", "inspect", identifier if identifier is not None else self.name)
        except (OSError, TimeoutError, TimeoutExpired) as error:
            raise ExecutionConflict("OCI ownership observation unavailable") from error
        if result.returncode != 0 or result.stderr:
            raise ExecutionConflict("OCI ownership observation unavailable")
        try:
            observed: object = json.loads(result.stdout)
        except ValueError as error:
            raise ExecutionConflict("invalid OCI observation") from error
        if not isinstance(observed, list) or len(cast(list[object], observed)) != 1:
            raise ExecutionConflict("OCI inspection is not one container")
        item = _object(cast(list[object], observed)[0])
        actual, image = item.get("Id"), item.get("Image")
        labels = _object(_object(item.get("Config")).get("Labels"))
        if (
            not _digest(actual)
            or type(image) is not str
            or image.removeprefix("sha256:") != self.image
            or item.get("Name") != self.name
            or labels.get(LABEL) != self.token
            or (identifier is not None and identifier != actual)
        ):
            raise ExecutionConflict("foreign OCI container; no mutation authorized")
        self.store.finalize_artifact(self.operation_id, "oci-container", _bytes({"id": actual, "owner": self.token}))
        return cast(str, actual)

    def state(self) -> Presence:
        identifier = self._receipt()
        if identifier is None:
            return Presence.UNKNOWN
        try:
            result = self._call("container", "exists", identifier)
        except (OSError, TimeoutError, TimeoutExpired, ExecutionConflict):
            return Presence.UNKNOWN
        if result.stdout or result.stderr:
            return Presence.UNKNOWN
        return {0: Presence.FOUND, 1: Presence.ABSENT}.get(result.returncode, Presence.UNKNOWN)

    def stop(self) -> None:
        """Stop delivery/acknowledgement is not settlement or cleanup permission."""
        if self.state() is Presence.ABSENT:
            return
        identifier = self.capture()
        try:
            result = self._call("stop", "-t", "5", identifier)
        except (OSError, TimeoutError, TimeoutExpired) as error:
            raise ExecutionConflict("OCI stop delivery uncertain") from error
        if result.returncode != 0 or result.stderr:
            raise ExecutionConflict("OCI stop delivery uncertain")

    def require_absent(self) -> None:
        """Prove only this owned container is absent, not independent descendant settlement."""
        if self.state() is not Presence.ABSENT:
            raise ExecutionConflict("owned OCI container absence unproved")
