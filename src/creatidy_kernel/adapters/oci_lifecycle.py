# SPDX-License-Identifier: Apache-2.0
"""Durable owned-container reconciliation, not a launcher or isolation attestation.

Podman 6.1.3 exposes Id/Image/Name/Config.Labels in its container-inspect array.
Container-exists returns 0/1 for present/absent; other outcomes remain unknown.
Its CgroupPath is cross-checked against the authenticated init's host cgroup membership.
Trusted composition supplies verified resources and a bounded native CLI executor.
"""

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from subprocess import CompletedProcess, TimeoutExpired
from types import MappingProxyType
from typing import cast, override

from creatidy_kernel.adapters.bubblewrap_verification import resource_digest
from creatidy_kernel.adapters.codex_runtime import CodexInputs, CodexRuntime
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.git_workspace import verify_generation
from creatidy_kernel.adapters.oci_lifetime import LifetimeChannel
from creatidy_kernel.adapters.source_cache import SourceAcquisitionError
from creatidy_kernel.adapters.worker_collection import CollectedFile, collect_directory_fd
from creatidy_kernel.adapters.worker_profile import WorkerProfile, attached_run_argv, owned_run_prefix
from creatidy_kernel.core.execution import (
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    Presence,
    UnsupportedExecution,
    validate_relative_path,
)
from creatidy_kernel.ports.allocation import encode_allocation
from creatidy_kernel.ports.program_store import ApplicationStore, RevocationPhase

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
WorkerAuthorizer = Callable[[ExecutionRequest, str, bytes], bool]
WorkerDeadline = Callable[[ExecutionRequest, str], int]
MAX_NATIVE_PIN_BYTES = 320 * 1024 * 1024


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in cast(dict[object, object], value)):
        raise ExecutionConflict("invalid OCI observation")
    return cast(dict[str, object], value)


def _digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _mountinfo_unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _cgroup2_mount() -> tuple[int, str, str]:
    matches: list[tuple[int, str, str]] = []
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines()
        for line in lines:
            before, separator, after = line.partition(" - ")
            fields = before.split()
            filesystem = after.split()
            if separator and len(fields) >= 5 and filesystem and filesystem[0] == "cgroup2":
                matches.append((int(fields[0]), _mountinfo_unescape(fields[3]), _mountinfo_unescape(fields[4])))
    except (OSError, UnicodeError, ValueError) as error:
        raise ExecutionConflict("host cgroup2 mount identity unavailable") from error
    if len(matches) != 1:
        raise ExecutionConflict("one host cgroup2 mount required")
    mount_id, mount_root, mount_point = matches[0]
    if not PurePosixPath(mount_root).is_absolute() or not Path(mount_point).is_absolute():
        raise ExecutionConflict("invalid host cgroup2 mount identity")
    return mount_id, mount_root, mount_point


def _proc_cgroup_path(pid: int) -> str:
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise ExecutionConflict("authenticated init cgroup membership unavailable") from error
    paths = [line.partition("::")[2] for line in lines if line.startswith("0::")]
    if len(paths) != 1 or not paths[0].startswith("/") or ".." in PurePosixPath(paths[0]).parts:
        raise ExecutionConflict("authenticated init is not in one cgroup2 subtree")
    return paths[0]


def _cgroup_directory(cgroup_path: str) -> tuple[Path, int, str, str, os.stat_result]:
    mount_id, mount_root, mount_point = _cgroup2_mount()
    try:
        relative = PurePosixPath(cgroup_path).relative_to(PurePosixPath(mount_root))
    except ValueError as error:
        raise ExecutionConflict("worker cgroup is outside the mounted cgroup2 tree") from error
    path = Path(mount_point).joinpath(*relative.parts)
    try:
        info = path.lstat()
    except OSError as error:
        raise ExecutionConflict("owned cgroup directory unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ExecutionConflict("owned cgroup path is not a real directory")
    return path, mount_id, mount_root, mount_point, info


def _cgroup_events(path: Path) -> dict[str, int]:
    try:
        with (path / "cgroup.events").open("rb") as stream:
            content = stream.read(4097)
    except OSError as error:
        raise ExecutionConflict("owned cgroup events unavailable") from error
    if len(content) > 4096:
        raise ExecutionConflict("owned cgroup events exceeded bound")
    try:
        values = {key: int(value) for key, value in (line.split() for line in content.decode("ascii").splitlines())}
    except (UnicodeError, ValueError) as error:
        raise ExecutionConflict("invalid owned cgroup events") from error
    if not {"populated", "frozen"} <= values.keys() or any(value not in {0, 1} for value in values.values()):
        raise ExecutionConflict("invalid owned cgroup event values")
    return values


def _cgroup_subtree(root: Path) -> tuple[tuple[Path, dict[str, int]], ...]:
    nodes: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        nodes.append(current)
        if len(nodes) > 256:
            raise ExecutionConflict("owned cgroup subtree exceeds bound")
        try:
            children = tuple(current.iterdir())
        except OSError as error:
            raise ExecutionConflict("owned cgroup subtree enumeration failed") from error
        for child in children:
            try:
                info = child.lstat()
            except OSError as error:
                raise ExecutionConflict("owned cgroup child changed during enumeration") from error
            if stat.S_ISLNK(info.st_mode):
                raise ExecutionConflict("symlink in owned cgroup subtree")
            if stat.S_ISDIR(info.st_mode) and (child / "cgroup.events").exists():
                pending.append(child)
    return tuple((path, _cgroup_events(path)) for path in nodes)


def _wait_cgroup_subtree(
    root: Path, *, frozen: int | None = None, populated: int | None = None, timeout: float = 5.0
) -> tuple[tuple[Path, dict[str, int]], ...]:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = _cgroup_subtree(root)
        if (
            frozen is not None
            and snapshot[0][1]["frozen"] == frozen
            and all(events["frozen"] == frozen for _, events in snapshot if events["populated"] == 1)
        ):
            return snapshot
        if populated is not None and all(events["populated"] == populated for _, events in snapshot):
            return snapshot
        if time.monotonic() >= deadline:
            raise ExecutionConflict("kernel cgroup state did not reach the required barrier")
        time.sleep(0.01)


@dataclass(frozen=True, slots=True)
class OCIResources:
    """Controller-selected pins, not inferred from opaque historical WorkspaceSpec strings.

    Read-only closures use the existing trusted resource digest; raw tool/config
    pins use SHA-256. Protected ancestors must remain under trusted host control.
    This refuses sockets/extra channels rather than adopting a relay protocol.
    """

    workspace: Path
    private_parent: Path
    protected: tuple[Path, ...]
    pins: tuple[tuple[Path, str], ...]
    read_only: tuple[tuple[Path, str], ...]
    candidate_root: PurePosixPath = PurePosixPath("candidate")
    workspace_operation: str | None = None

    def verify(self, owner: "OwnedOCI", codex: Path) -> bytes:
        if type(self.candidate_root) is not PurePosixPath:
            raise ExecutionConflict("explicit immutable OCI candidate subtree required")
        validate_relative_path(str(self.candidate_root))
        if any(type(value) is not tuple for value in (self.protected, self.pins, self.read_only)) or not self.protected:
            raise ExecutionConflict("immutable explicit OCI resources required")
        identities: dict[str, list[int]] = {}
        for path in (self.private_parent, self.workspace):
            if not path.is_absolute() or path == Path("/") or path.resolve() != path:
                raise ExecutionConflict("OCI private resource path differs")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                info = os.fstat(descriptor)
                if (
                    info.st_uid != os.getuid()
                    or not stat.S_ISDIR(info.st_mode)
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
                    or (path == self.private_parent and info.st_mode & 0o077)
                    or (info.st_dev, info.st_ino) != (path.lstat().st_dev, path.lstat().st_ino)
                ):
                    raise ExecutionConflict("OCI private resource ownership differs")
                identities[str(path)] = [info.st_dev, info.st_ino, info.st_uid]
            finally:
                os.close(descriptor)
        if self.workspace == self.private_parent or not self.workspace.is_relative_to(self.private_parent):
            raise ExecutionConflict("OCI workspace is outside its private parent")
        pins, read_only = dict(self.pins), dict(self.read_only)
        if len(pins) != len(self.pins) or len(read_only) != len(self.read_only):
            raise ExecutionConflict("duplicate OCI resource pin")
        required = {
            owner.podman,
            codex,
            owner.profile.seccomp_path,
            Path(owner.environment["CONTAINERS_CONF"]),
            Path(owner.environment["CONTAINERS_STORAGE_CONF"]),
        }
        if not required <= pins.keys() or any(not _digest(digest) for digest in (*pins.values(), *read_only.values())):
            raise ExecutionConflict("complete exact OCI resource pins required")
        mounts = owner.profile.mounts
        if [(item.source, str(item.destination)) for item in mounts if item.writable] != [
            (self.workspace, "/workspace")
        ] or {item.source for item in mounts if not item.writable} != read_only.keys():
            raise ExecutionConflict("OCI mounts differ from the explicit resource closure")
        for root in self.protected:
            if not root.is_absolute() or root.resolve() != root:
                raise ExecutionConflict("OCI protected resource path differs")
        for index, mount in enumerate(mounts):
            source, destination = mount.source, mount.destination
            if any(source.is_relative_to(root) or root.is_relative_to(source) for root in self.protected):
                raise ExecutionConflict("OCI mount exposes protected controller resources")
            for reserved in ("/", "/proc", "/sys", "/dev", "/run", "/tmp", "/home/worker"):  # noqa: S108 - OCI tmpfs target.
                if str(destination) == "/" or (
                    reserved != "/"
                    and (destination.is_relative_to(reserved) or PurePosixPath(reserved).is_relative_to(destination))
                ):
                    raise ExecutionConflict("OCI mount overlaps a runtime-managed destination")
            for previous in mounts[:index]:
                if (
                    destination.is_relative_to(previous.destination)
                    or previous.destination.is_relative_to(destination)
                    or source.is_relative_to(previous.source)
                    or previous.source.is_relative_to(source)
                ):
                    raise ExecutionConflict("OCI resources overlap")
        for path in dict.fromkeys((*pins, *read_only)):
            if path.is_relative_to(self.workspace) or self.workspace.is_relative_to(path):
                raise ExecutionConflict("OCI writable workspace overlaps immutable resources")
            info = path.lstat()
            if path in pins:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_NATIVE_PIN_BYTES:
                    raise ExecutionConflict("invalid OCI native/config resource")
            actual = resource_digest(path)
            if path in pins and actual != hashlib.sha256(b".\0" + bytes.fromhex(pins[path])).hexdigest():
                raise ExecutionConflict("OCI resource content differs from the trusted pin")
            if path in read_only and any(
                item.is_file() and item.lstat().st_nlink != 1
                for item in ((path, *path.rglob("*")) if path.is_dir() else (path,))
            ):
                raise ExecutionConflict("OCI read-only resources have mutable hardlink aliases")
            if path in read_only and actual != read_only[path]:
                raise ExecutionConflict("OCI resource content differs from the trusted pin")
            after = path.lstat()
            identity = [
                info.st_dev,
                info.st_ino,
                info.st_uid,
                info.st_mode,
                info.st_nlink,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ]
            if identity != [
                after.st_dev,
                after.st_ino,
                after.st_uid,
                after.st_mode,
                after.st_nlink,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ]:
                raise ExecutionConflict("OCI resource identity changed while reading")
            identities[str(path)] = identity
        materialization = (
            verify_generation(
                owner.store,
                self.workspace_operation,
                owner.request.workspace,
                self.workspace,
                self.private_parent,
                self.candidate_root,
                self.protected,
            ).decode()
            if self.workspace_operation is not None
            else None
        )
        return _bytes(
            {
                "version": 1,
                "identities": identities,
                "pins": {str(path): digest for path, digest in pins.items()},
                "read_only": {str(path): digest for path, digest in read_only.items()},
                "protected": [str(path) for path in self.protected],
                "candidate_root": str(self.candidate_root),
                "materialization": materialization,
            }
        )


@dataclass(frozen=True, slots=True)
class LifetimeSpec:
    """Controller-selected in-boundary Python and dedicated immutable bootstrap mount."""

    python: PurePosixPath
    script: PurePosixPath

    def validate(self, profile: WorkerProfile) -> None:
        source = Path(__file__).with_name("oci_bootstrap.py").resolve()
        if (
            not self.python.is_absolute()
            or not self.script.is_absolute()
            or not any(
                mount.source == source and mount.destination == self.script and not mount.writable
                for mount in profile.mounts
            )
        ):
            raise ExecutionConflict("immutable installed OCI lifetime bootstrap mount required")


@dataclass(frozen=True, slots=True, init=False)
class OwnedOCI:
    """One immutable Operation/Attempt generation, never ownership inferred from a name."""

    store: ApplicationStore
    request: ExecutionRequest
    podman: Path
    profile: WorkerProfile
    environment: Mapping[str, str]
    name: str
    image: str
    invoke: Invoke
    operation_id: str
    frame: bytes
    token: str
    _lifetime: LifetimeChannel | None
    _connection: CodexStdio | None
    _cgroup_identity: bytes | None

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
                "version": 2,
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
                "prefix": owned_run_prefix(podman, profile, name),
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
            ("profile", profile),
            ("environment", MappingProxyType(supplied)),
            ("name", name),
            ("image", image),
            ("operation_id", request.operation.operation_id),
            ("frame", frame),
            ("token", hashlib.sha256(frame).hexdigest()),
            ("_lifetime", None),
            ("_connection", None),
            ("_cgroup_identity", None),
        ):
            object.__setattr__(self, key, value)
        owner = store.find_artifact(request.operation.operation_id, "oci-owner")
        cgroup_identity = store.find_artifact(request.operation.operation_id, "oci-cgroup-identity")
        if owner == frame:
            if cgroup_identity is not None:
                object.__setattr__(self, "_cgroup_identity", cgroup_identity)
            if type(request.capability_reference) is str:
                store.register_revocation_handler(
                    request.capability_reference, request.operation.operation_id, self._handle_revocation
                )

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
        if type(self.request.capability_reference) is not str:
            raise ExecutionConflict("OwnedOCI requires a durable authority grant")
        self.store.register_revocation_handler(
            self.request.capability_reference, self.operation_id, self._handle_revocation
        )
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
        item = self._inspection()
        actual = cast(str, item["Id"])
        self.store.finalize_artifact(self.operation_id, "oci-container", _bytes({"id": actual, "owner": self.token}))
        return actual

    def _inspection(self) -> dict[str, object]:
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
        return item

    def _capture_cgroup_identity(
        self,
        identifier: str,
        state: dict[str, object],
        init_identity: dict[str, object],
        init_record: bytes,
        binding: bytes,
        channel: LifetimeChannel,
    ) -> bytes:
        pid = state.get("Pid")
        cgroup_path = state.get("CgroupPath")
        if (
            type(pid) is not int
            or pid <= 0
            or init_identity.get("pid") != pid
            or type(cgroup_path) is not str
            or _proc_cgroup_path(pid) != cgroup_path
            or channel.exited()
        ):
            raise ExecutionConflict("OCI init cgroup identity differs from the exact worker process")
        path, mount_id, mount_root, mount_point, info = _cgroup_directory(cgroup_path)
        if not path.is_relative_to(Path(mount_point)):
            raise ExecutionConflict("owned worker cgroup escaped the cgroup2 mount")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return _bytes(
            {
                "version": 1,
                "owner": self.token,
                "container": identifier,
                "binding_reference": hashlib.sha256(binding).hexdigest(),
                "init_reference": hashlib.sha256(init_record).hexdigest(),
                "pid": pid,
                "cgroup_path": cgroup_path,
                "host_path": str(path),
                "mount_id": mount_id,
                "mount_root": mount_root,
                "mount_point": mount_point,
                "st_dev": info.st_dev,
                "st_ino": info.st_ino,
                "boot_id": boot_id,
            }
        )

    def _load_cgroup_identity(self, *, require_path: bool = True) -> tuple[bytes, dict[str, object], Path]:
        identity_bytes = self.store.find_artifact(self.operation_id, "oci-cgroup-identity")
        namespace_bytes = self.store.find_artifact(self.operation_id, "oci-init")
        identifier = self._receipt()
        if identity_bytes is None or namespace_bytes is None or identifier is None:
            raise ExecutionConflict("durable OCI cgroup identity is unavailable")
        try:
            record = _object(json.loads(identity_bytes))
            namespace = _object(json.loads(namespace_bytes))
            identity = _object(namespace.get("identity"))
        except (ValueError, TypeError) as error:
            raise ExecutionConflict("invalid durable OCI cgroup identity") from error
        expected = {
            "version",
            "owner",
            "container",
            "binding_reference",
            "init_reference",
            "pid",
            "cgroup_path",
            "host_path",
            "mount_id",
            "mount_root",
            "mount_point",
            "st_dev",
            "st_ino",
            "boot_id",
        }
        if (
            set(record) != expected
            or record.get("version") != 1
            or record.get("owner") != self.token
            or record.get("container") != identifier
            or namespace.get("owner") != self.token
            or namespace.get("container") != identifier
            or record.get("init_reference") != hashlib.sha256(namespace_bytes).hexdigest()
            or record.get("binding_reference") != namespace.get("binding_reference")
            or record.get("pid") != identity.get("pid")
            or type(record.get("mount_id")) is not int
            or type(record.get("st_dev")) is not int
            or type(record.get("st_ino")) is not int
            or any(
                type(record.get(key)) is not str
                for key in ("cgroup_path", "host_path", "mount_root", "mount_point", "boot_id")
            )
        ):
            raise ExecutionConflict("durable OCI cgroup identity differs from its launch")
        current_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        mount_id, mount_root, mount_point = _cgroup2_mount()
        path = Path(cast(str, record["host_path"]))
        if (
            mount_id != record["mount_id"]
            or mount_root != record["mount_root"]
            or mount_point != record["mount_point"]
            or current_boot_id != record["boot_id"]
        ):
            raise ExecutionConflict("owned worker cgroup incarnation changed")
        if path.exists():
            current_path, current_id, current_root, current_mount, info = _cgroup_directory(
                cast(str, record["cgroup_path"])
            )
            if (
                str(current_path) != record["host_path"]
                or current_id != record["mount_id"]
                or current_root != record["mount_root"]
                or current_mount != record["mount_point"]
                or info.st_dev != record["st_dev"]
                or info.st_ino != record["st_ino"]
            ):
                raise ExecutionConflict("owned worker cgroup incarnation changed")
            path = current_path
        elif require_path:
            raise ExecutionConflict("owned worker cgroup directory disappeared before settlement")
        object.__setattr__(self, "_cgroup_identity", identity_bytes)
        return identity_bytes, record, path

    def _verify_live_cgroup_identity(self, record: dict[str, object]) -> dict[str, object]:
        item = self._inspection()
        state = _object(item.get("State"))
        pid = state.get("Pid")
        if (
            type(pid) is not int
            or pid != record.get("pid")
            or state.get("CgroupPath") != record.get("cgroup_path")
            or _proc_cgroup_path(pid) != record.get("cgroup_path")
        ):
            raise ExecutionConflict("live OCI process no longer matches its cgroup identity")
        namespace_bytes = self.store.find_artifact(self.operation_id, "oci-init")
        if namespace_bytes is None:
            raise ExecutionConflict("original OCI init identity is unavailable")
        namespace = _object(json.loads(namespace_bytes))
        init_identity = _object(namespace.get("identity"))
        try:
            process_stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            start_ticks = int(process_stat.rpartition(")")[2].split()[19])
            pid_namespace = os.stat(f"/proc/{pid}/ns/pid")
        except (OSError, ValueError, IndexError) as error:
            raise ExecutionConflict("live OCI init kernel identity unavailable") from error
        if start_ticks != init_identity.get("start_ticks") or [
            pid_namespace.st_dev,
            pid_namespace.st_ino,
        ] != init_identity.get("namespace"):
            raise ExecutionConflict("live OCI init differs from its recorded kernel identity")
        return state

    def _pause_for_revocation(self) -> None:
        identity_bytes, _identity, path = self._load_cgroup_identity()
        events = _cgroup_subtree(path)
        if all(item["populated"] == 0 for _, item in events):
            return
        else:
            state = self._verify_live_cgroup_identity(_object(json.loads(identity_bytes)))
            state_is_paused = state.get("Paused") is True or state.get("Status") == "paused"
            if not state_is_paused:
                identifier = self._receipt()
                if identifier is None:
                    raise ExecutionConflict("owned OCI container identity is unavailable")
                result = self._call("pause", identifier)
                if result.returncode != 0 or result.stderr:
                    raise ExecutionConflict("owned OCI pause was not acknowledged")
            events = _wait_cgroup_subtree(path, frozen=1)
            state = _object(self._inspection().get("State"))
            if state.get("Paused") is not True and state.get("Status") != "paused":
                raise ExecutionConflict("Podman did not retain the acknowledged paused state")
        frozen_record = _bytes(
            {
                "version": 1,
                "owner": self.token,
                "container": self._receipt(),
                "identity_reference": hashlib.sha256(identity_bytes).hexdigest(),
                "frozen": 1,
            }
        )
        self.store.finalize_artifact(self.operation_id, "oci-cgroup-frozen", frozen_record)

    def _validate_cgroup_empty(self, identity_bytes: bytes, empty_bytes: bytes, container_id: str) -> dict[str, object]:
        empty = _object(json.loads(empty_bytes))
        if (
            set(empty) != {"version", "owner", "container", "identity_reference", "populated", "frozen"}
            or empty.get("version") != 1
            or empty.get("owner") != self.token
            or empty.get("container") != container_id
            or empty.get("identity_reference") != hashlib.sha256(identity_bytes).hexdigest()
            or empty.get("populated") != 0
            or type(empty.get("frozen")) is not int
            or empty.get("frozen") not in {0, 1}
        ):
            raise ExecutionConflict("durable cgroup-empty receipt differs")
        return empty

    def _finalize_cgroup_settled(self, identity_bytes: bytes, empty_bytes: bytes, container_id: str) -> None:
        if self.state() is not Presence.ABSENT:
            raise ExecutionConflict("container absence required after cgroup-empty receipt")
        settled = _bytes(
            {
                "version": 1,
                "owner": self.token,
                "container": container_id,
                "identity_reference": hashlib.sha256(identity_bytes).hexdigest(),
                "empty_reference": hashlib.sha256(empty_bytes).hexdigest(),
            }
        )
        self.store.finalize_artifact(self.operation_id, "oci-cgroup-settled", settled)
        if type(self.request.capability_reference) is str:
            self.store.unregister_revocation_handler(self.request.capability_reference, self.operation_id)

    def _retire_revoked_worker(self) -> None:
        identity_bytes, _identity, path = self._load_cgroup_identity(require_path=False)
        container_id = self._receipt()
        if container_id is None:
            raise ExecutionConflict("owned OCI container identity is unavailable")
        settled = self.store.find_artifact(self.operation_id, "oci-cgroup-settled")
        if settled is not None:
            if self.state() is not Presence.ABSENT:
                raise ExecutionConflict("settled OCI container reappeared")
            self._close_owned_runtime()
            return

        empty_bytes = self.store.find_artifact(self.operation_id, "oci-cgroup-empty")
        if empty_bytes is not None:
            self._validate_cgroup_empty(identity_bytes, empty_bytes, container_id)
            presence = self.state()
            if presence is Presence.ABSENT:
                self._close_owned_runtime()
                self._finalize_cgroup_settled(identity_bytes, empty_bytes, container_id)
                return
            if presence is not Presence.FOUND:
                raise ExecutionConflict("owned container state is unknown after cgroup-empty receipt")
            if not path.exists():
                raise ExecutionConflict("owned cgroup disappeared before exact container removal")
            self._inspection()
            events = _cgroup_subtree(path)
            if any(item["populated"] != 0 for _, item in events):
                raise ExecutionConflict("worker cgroup repopulated after durable empty receipt")
        else:
            if not path.exists():
                raise ExecutionConflict("owned cgroup disappeared before empty receipt")
            self._inspection()
            events = _cgroup_subtree(path)
            if any(item["populated"] == 1 for _, item in events):
                if any(item["frozen"] != 1 for _, item in events if item["populated"] == 1):
                    raise ExecutionConflict("worker subtree thawed after revocation commit")
                state = _object(self._inspection().get("State"))
                if state.get("Paused") is not True and state.get("Status") != "paused":
                    raise ExecutionConflict("Podman no longer reports the frozen worker as paused")
                (path / "cgroup.kill").write_text("1", encoding="ascii")
                events = _wait_cgroup_subtree(path, populated=0)
            if any(item["populated"] != 0 for _, item in events):
                raise ExecutionConflict("worker cgroup did not reach populated=0")
            identity_bytes, _identity, path = self._load_cgroup_identity()
            events = _cgroup_subtree(path)
            if any(item["populated"] != 0 for _, item in events):
                raise ExecutionConflict("worker cgroup repopulated before receipt")
            empty_bytes = _bytes(
                {
                    "version": 1,
                    "owner": self.token,
                    "container": container_id,
                    "identity_reference": hashlib.sha256(identity_bytes).hexdigest(),
                    "populated": 0,
                    "frozen": events[0][1]["frozen"],
                }
            )
            self.store.finalize_artifact(self.operation_id, "oci-cgroup-empty", empty_bytes)

        # The receipt is durable before Podman removes the cgroupfs tree.
        self._close_owned_runtime()
        self._remove_exact_container(container_id)
        self._finalize_cgroup_settled(identity_bytes, empty_bytes, container_id)

    def _remove_exact_container(self, identifier: str) -> None:
        self._inspection()
        try:
            result = self._call("rm", "--force", identifier)
        except (OSError, TimeoutError, TimeoutExpired) as error:
            if self.state() is Presence.ABSENT:
                return
            raise ExecutionConflict("exact owned OCI removal uncertain") from error
        if result.returncode != 0 or result.stderr:
            if self.state() is Presence.ABSENT:
                return
            raise ExecutionConflict("exact owned OCI removal unacknowledged")
        if self.state() is not Presence.ABSENT:
            raise ExecutionConflict("exact owned OCI absence unproved after removal")

    def _close_owned_runtime(self) -> None:
        connection = self._connection
        lifetime = self._lifetime
        if lifetime is not None:
            lifetime.close_liveness()
        try:
            if connection is not None:
                connection.close()
        finally:
            if lifetime is not None:
                lifetime.close()
            object.__setattr__(self, "_lifetime", None)
            object.__setattr__(self, "_connection", None)

    def _record_no_worker_revocation(self) -> bool:
        if (
            self.store.find_artifact(self.operation_id, "oci-container") is not None
            or self.store.find_artifact(self.operation_id, "oci-cgroup-identity") is not None
        ):
            return False
        prefix = f"runtime-dispatch:{self.request.fence}:"
        if any(
            self.store.find_artifact(self.operation_id, prefix + stage) is not None
            for stage in ("worker", "worker-enter", "thread", "turn")
        ):
            return False
        receipt = _bytes(
            {
                "version": 1,
                "owner": self.token,
                "operation": self.operation_id,
                "grant": self.request.capability_reference,
                "fence": self.request.fence,
                "worker_created": False,
            }
        )
        self.store.finalize_artifact(self.operation_id, "oci-revocation-no-worker", receipt)
        return True

    def _handle_revocation(self, phase: RevocationPhase) -> None:
        if phase == "freeze":
            if self.store.find_artifact(self.operation_id, "oci-cgroup-settled") is not None:
                if self.state() is not Presence.ABSENT:
                    raise ExecutionConflict("settled OCI container reappeared")
                return
            empty_bytes = self.store.find_artifact(self.operation_id, "oci-cgroup-empty")
            if empty_bytes is not None:
                identity_bytes, _record, _path = self._load_cgroup_identity(require_path=False)
                container_id = self._receipt()
                if container_id is None:
                    raise ExecutionConflict("owned OCI container identity is unavailable")
                self._validate_cgroup_empty(identity_bytes, empty_bytes, container_id)
                if self.state() is Presence.ABSENT:
                    self._finalize_cgroup_settled(identity_bytes, empty_bytes, container_id)
                    return
            if self.store.find_artifact(self.operation_id, "oci-revocation-no-worker") is not None:
                return
            if self.store.find_artifact(self.operation_id, "oci-cgroup-identity") is None:
                if self._record_no_worker_revocation():
                    return
                raise ExecutionConflict("worker launch is uncertain without exact cgroup identity")
            self._pause_for_revocation()
            return
        if phase == "retire":
            if self.store.find_artifact(self.operation_id, "oci-cgroup-settled") is not None:
                return
            if self.store.find_artifact(self.operation_id, "oci-revocation-no-worker") is not None:
                return
            self._retire_revoked_worker()
            return
        raise ExecutionConflict("unknown OCI revocation phase")

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
        try:
            item = self._inspection()
        except ExecutionConflict:
            if self.state() is Presence.ABSENT:
                return
            raise
        state_value = item.get("State")
        state = _object(state_value) if state_value is not None else {}
        if state.get("Paused") is True or state.get("Status") == "paused":
            return
        if state and state.get("Running") is not True:
            return
        identifier = cast(str, item["Id"])
        try:
            result = self._call("stop", "-t", "5", identifier)
        except (OSError, TimeoutError, TimeoutExpired) as error:
            if self.state() is Presence.ABSENT:
                return
            raise ExecutionConflict("OCI stop delivery uncertain") from error
        if result.returncode != 0 or result.stderr:
            if self.state() is Presence.ABSENT:
                return
            current = self._inspection()
            current_state = _object(current.get("State"))
            if current_state.get("Running") is not True or current_state.get("Paused") is True:
                return
            raise ExecutionConflict("OCI stop delivery uncertain")

    def require_absent(self) -> None:
        """Prove only this owned container is absent, not independent descendant settlement."""
        if self.state() is not Presence.ABSENT:
            raise ExecutionConflict("owned OCI container absence unproved")

    def settled(self) -> bool:
        """Require a warm PIDFD witness or a cold cgroup-empty receipt plus exact absence."""
        identifier = self._receipt()
        namespace = self.store.find_artifact(self.operation_id, "oci-init")
        if identifier is None or namespace is None:
            return False
        identity_bytes = self.store.find_artifact(self.operation_id, "oci-cgroup-identity")
        empty_bytes = self.store.find_artifact(self.operation_id, "oci-cgroup-empty")
        cgroup_settled = self.store.find_artifact(self.operation_id, "oci-cgroup-settled")
        if cgroup_settled is not None and (identity_bytes is None or empty_bytes is None):
            raise ExecutionConflict("cgroup settlement is missing its durable empty witness")
        if identity_bytes is not None and empty_bytes is not None:
            self._load_cgroup_identity(require_path=False)
            self._validate_cgroup_empty(identity_bytes, empty_bytes, identifier)
            expected = _bytes(
                {
                    "version": 1,
                    "owner": self.token,
                    "container": identifier,
                    "identity_reference": hashlib.sha256(identity_bytes).hexdigest(),
                    "empty_reference": hashlib.sha256(empty_bytes).hexdigest(),
                }
            )
            if cgroup_settled is not None and cgroup_settled != expected:
                raise ExecutionConflict("cgroup settlement receipt differs")
            if self.state() is not Presence.ABSENT:
                return False
            if cgroup_settled is None:
                self.store.finalize_artifact(self.operation_id, "oci-cgroup-settled", expected)
            return True

        record = _object(json.loads(namespace))
        if (
            set(record) != {"version", "owner", "container", "identity", "binding_reference"}
            or type(record.get("version")) is not int
            or record.get("version") != 1
            or record.get("owner") != self.token
            or record.get("container") != identifier
            or not isinstance(record.get("identity"), dict)
            or not record.get("identity")
            or type(record.get("binding_reference")) is not str
            or re.fullmatch("[0-9a-f]{64}", cast(str, record["binding_reference"])) is None
        ):
            raise ExecutionConflict("OCI namespace receipt differs from ownership")
        witness = _bytes(
            {
                "version": 1,
                "owner": self.token,
                "container": identifier,
                "namespace_reference": hashlib.sha256(namespace).hexdigest(),
            }
        )
        previous = self.store.find_artifact(self.operation_id, "oci-namespace-settled")
        if previous is not None:
            if previous != witness:
                raise ExecutionConflict("OCI settlement receipt differs")
            absent = self.state() is Presence.ABSENT
            if absent and type(self.request.capability_reference) is str:
                self.store.unregister_revocation_handler(self.request.capability_reference, self.operation_id)
            return absent
        lifetime = self._lifetime
        if lifetime is None or lifetime.identity != record.get("identity") or not lifetime.exited():
            return False
        if self.state() is Presence.FOUND:
            self._remove_exact_container(identifier)
        if self.state() is not Presence.ABSENT:
            return False
        self.store.finalize_artifact(self.operation_id, "oci-namespace-settled", witness)
        lifetime.close()
        object.__setattr__(self, "_lifetime", None)
        if type(self.request.capability_reference) is str:
            self.store.unregister_revocation_handler(self.request.capability_reference, self.operation_id)
        return True

    def require_settled(self) -> None:
        if not self.settled():
            raise ExecutionConflict("owned OCI worker settlement unproved")

    def collect_candidate(
        self, resources: OCIResources, *, codex: Path | None = None, authorizer: WorkerAuthorizer | None = None
    ) -> Candidate:
        """Collect only the controller's prebound candidate subtree after exact settlement.

        Private runtime state is not an implicit candidate root. This is a retained
        proposal, not task acceptance or external publication. Workspace cleanup and
        general source materialization remain separate, unsupported lifecycle work.
        """
        self.require_settled()
        stage = _object(
            json.loads(self.store.artifact(self.operation_id, f"runtime-dispatch:{self.request.fence}:worker"))
        )
        binding = stage.get("launch")
        if type(binding) is not str:
            raise ExecutionConflict("OCI launch resources missing")
        launch = _object(json.loads(binding))
        namespace = _object(json.loads(self.store.artifact(self.operation_id, "oci-init")))
        if codex is None or authorizer is None:
            raise ExecutionConflict("current OCI collection authority unavailable")
        if (
            resources.verify(self, codex).decode() != launch.get("resources")
            or authorizer(self.request, str(resources.workspace), binding.encode()) is not True
        ):
            raise ExecutionConflict("current OCI collection authority refused")
        if namespace.get("binding_reference") != hashlib.sha256(binding.encode()).hexdigest():
            raise ExecutionConflict("OCI namespace differs from original launch")
        retained = _object(json.loads(cast(str, launch.get("resources"))))
        if retained.get("candidate_root") != str(resources.candidate_root):
            raise ExecutionConflict("OCI candidate root differs from original launch")
        materialization = retained.get("materialization")
        if resources.workspace_operation is not None:
            actual = verify_generation(
                self.store,
                resources.workspace_operation,
                self.request.workspace,
                resources.workspace,
                resources.private_parent,
                resources.candidate_root,
                resources.protected,
            ).decode()
            if actual != materialization:
                raise ExecutionConflict("OCI Workspace generation differs from original launch")
        elif materialization is not None:
            raise ExecutionConflict("OCI original Workspace generation missing")
        identities = _object(retained.get("identities"))
        artifacts: list[Artifact] = []

        def sink(relative: str, data: bytes, record: CollectedFile) -> None:
            name = "oci-candidate-file:" + hashlib.sha256(relative.encode()).hexdigest()
            digest = self.store.finalize_artifact(self.operation_id, name, data)
            if digest != f"sha256:{record.sha256}":
                raise ExecutionConflict("OCI candidate bytes differ from collected digest")
            artifacts.append(Artifact(relative, digest))

        with ExitStack() as stack:
            parent = resources.private_parent
            if not parent.is_absolute() or parent.resolve() != parent or not resources.workspace.is_relative_to(parent):
                raise ExecutionConflict("OCI private workspace ancestry differs")
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if (
                identities.get(str(parent)) != [info.st_dev, info.st_ino, info.st_uid]
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ExecutionConflict("OCI private parent differs from original launch")
            for part in resources.workspace.relative_to(parent).parts:
                descriptor = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
                )
                stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if identities.get(str(resources.workspace)) != [info.st_dev, info.st_ino, info.st_uid]:
                raise ExecutionConflict("OCI workspace differs from original launch")
            validate_relative_path(str(resources.candidate_root))
            for part in resources.candidate_root.parts:
                descriptor = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
                )
                stack.callback(os.close, descriptor)
            collected = collect_directory_fd(
                descriptor, sink=sink, exclude=frozenset({".git"}) if materialization is not None else frozenset()
            )
            if collected.refusals:
                raise ExecutionConflict("OCI candidate collection refused unsafe entries")
        self.require_settled()
        manifest = ArtifactManifest(self.request.workspace.key, tuple(sorted(artifacts, key=lambda item: item.path)))
        if (
            resources.verify(self, codex).decode() != launch.get("resources")
            or authorizer(self.request, str(resources.workspace), binding.encode()) is not True
        ):
            raise ExecutionConflict("current OCI collection authority refused")
        candidate = Candidate(self.request.attempt.attempt_id, self.request.attempt.digest, manifest)
        self.store.finalize_artifact(self.operation_id, "oci-candidate", _bytes(asdict(candidate)))
        return candidate

    def connect(
        self,
        resources: OCIResources,
        codex: Path,
        version: str,
        entrypoint: tuple[str, ...],
        probe_environment: Mapping[str, str],
        schema_methods: frozenset[str],
        authorizers: tuple[WorkerAuthorizer, WorkerAuthorizer],
        clock: Callable[[], int],
        *,
        lifetime: LifetimeSpec | None = None,
        enter_authorizer: WorkerAuthorizer | None = None,
        deadline: WorkerDeadline | None = None,
    ) -> CodexStdio:
        """Trusted-development composition with real resource checks and a durable send gate.

        This does not add an isolated Runtime, continuously revoke in-flight writes,
        attest the in-boundary executable or prove descendants settled. Missing or
        uncertain launch receipts remain retained, never a duplicate launch permit.
        """
        check, dispatch = authorizers
        cwd = str(resources.workspace)
        if check(self.request, cwd, _bytes({"version": 1, "owner": self.token})) is not True:
            raise ExecutionConflict("OCI current launch authority unavailable")
        evidence = resources.verify(self, codex)
        if lifetime is not None:
            lifetime.validate(self.profile)
            if enter_authorizer is None or deadline is None:
                raise ExecutionConflict("durable OCI entry gate and original deadline required")
        probe = dict(probe_environment)
        if not probe or not set(probe) <= {"HOME", "CODEX_HOME", "PATH", "LC_ALL", "LANG", "TMPDIR"}:
            raise ExecutionConflict("closed OCI probe environment required")
        if "HOME" not in probe:
            raise ExecutionConflict("OCI probe state must be private and separate")
        try:
            private_parent = resources.private_parent.resolve(strict=True)
            private_roots = tuple(root.resolve(strict=True) for root in (resources.workspace, *resources.protected))
        except (OSError, RuntimeError, ValueError) as error:
            raise ExecutionConflict("OCI probe state must be private and separate") from error
        for key in ("HOME", "CODEX_HOME", "TMPDIR"):
            if key not in probe:
                continue
            value = probe[key]
            if type(value) is not str:
                raise ExecutionConflict("OCI probe state must be private and separate")
            path = Path(value)
            if not path.is_absolute() or path == Path("/") or ".." in path.parts:
                raise ExecutionConflict("OCI probe state must be private and separate")
            try:
                resolved = path.resolve(strict=False)
            except (OSError, RuntimeError, ValueError) as error:
                raise ExecutionConflict("OCI probe state must be private and separate") from error
            if (
                path != resolved
                or not resolved.is_relative_to(private_parent)
                or any(resolved.is_relative_to(root) or root.is_relative_to(resolved) for root in private_roots)
            ):
                raise ExecutionConflict("OCI probe state must be private and separate")
            probe[key] = str(resolved)
        try:
            result = self._call("image", "inspect", "--format", "{{.Id}}", self.image)
            observed_image = result.stdout.decode("ascii").strip().removeprefix("sha256:")
        except (OSError, TimeoutError, TimeoutExpired, ValueError) as error:
            raise ExecutionConflict("exact loaded OCI image identity unavailable") from error
        if result.returncode != 0 or result.stderr or observed_image != self.image:
            raise ExecutionConflict("exact loaded OCI image identity unavailable")
        channel: LifetimeChannel | None = None
        attached_run_argv(self.podman, self.profile, self.name, self.image, entrypoint)
        selected_entry = entrypoint
        expiry: float | None = None
        if lifetime is not None and deadline is not None:
            expiry = min(deadline(self.request, cwd), clock() + self.profile.deadline_seconds)
            if expiry <= clock():
                raise ExecutionConflict("OCI original lifetime deadline expired")
            channel = LifetimeChannel(self.token)
            selected_entry = (
                str(lifetime.python),
                "-I",
                "-S",
                str(lifetime.script),
                str(channel.worker.fileno()),
                self.token,
                str(expiry),
                "--",
                *entrypoint,
            )
        command = attached_run_argv(self.podman, self.profile, self.name, self.image, selected_entry)
        image_index = len(command) - len(selected_entry) - 1
        label = ("--label", f"{LABEL}={self.token}")
        if channel is not None:
            label = (*label, f"--preserve-fd={channel.worker.fileno()}")
        command = (*command[:image_index], *label, *command[image_index:])
        binding = _bytes(
            {
                "version": 1,
                "owner": self.token,
                "command": command,
                "probe": str(codex),
                "probe_environment": probe,
                "resources": evidence.decode(),
                "lifetime": {"expiry": expiry, "python": str(lifetime.python), "script": str(lifetime.script)}
                if lifetime is not None
                else None,
            }
        )
        if check(self.request, cwd, binding) is not True:
            raise ExecutionConflict("OCI current launch authority unavailable")
        self.bind_before_send(now=clock())

        def before_worker(argv: tuple[str, ...], environment: Mapping[str, str]) -> bool:
            if argv != command or environment != self.environment or resources.verify(self, codex) != evidence:
                return False
            return dispatch(self.request, cwd, binding)

        def after_worker(_process: object) -> bool:
            if channel is None or enter_authorizer is None:
                return False
            try:
                channel.worker.close()
                identity = channel.receive(timeout=15)
                identifier = self.capture()
                state = _object(self._inspection().get("State"))
                if state.get("Running") is not True or state.get("Pid") != identity["pid"]:
                    raise ExecutionConflict("OCI init differs from authenticated bootstrap")
                init_record = _bytes(
                    {
                        "version": 1,
                        "owner": self.token,
                        "container": identifier,
                        "identity": identity,
                        "binding_reference": hashlib.sha256(binding).hexdigest(),
                    }
                )
                self.store.finalize_artifact(self.operation_id, "oci-init", init_record)
                cgroup_identity = self._capture_cgroup_identity(
                    identifier, state, identity, init_record, binding, channel
                )
                self.store.finalize_artifact(self.operation_id, "oci-cgroup-identity", cgroup_identity)
                object.__setattr__(self, "_lifetime", channel)
                object.__setattr__(self, "_cgroup_identity", cgroup_identity)
                if (
                    resources.verify(self, codex) != evidence
                    or enter_authorizer(self.request, cwd, binding) is not True
                ):
                    return False
                channel.release()
                return True
            except BaseException:
                channel.close_liveness()
                raise

        connection: CodexStdio | None = None
        try:
            connection = CodexStdio(
                (str(codex), "app-server"),
                version,
                schema_methods=schema_methods,
                schema_version=version,
                environment=probe,
                worker_command=command,
                worker_environment=self.environment,
                before_worker=before_worker,
                worker_fds=(channel.worker.fileno(),) if channel is not None else (),
                after_worker=after_worker if channel is not None else None,
            )
            if channel is None:
                self.capture()
            else:
                connection.before_close = channel.close_liveness
        except BaseException:
            if channel is not None:
                channel.close_liveness()
            if connection is not None:
                connection.close()
            raise
        object.__setattr__(self, "_connection", connection)
        return connection

    def runtime(
        self,
        resources: OCIResources,
        codex: Path,
        *,
        resolve: Callable[[ExecutionRequest], CodexInputs],
        authorizers: tuple[Callable[[ExecutionRequest, str, str], bool], Callable[[ExecutionRequest, str, str], bool]],
        active_authorizer: WorkerAuthorizer,
        clock: Callable[[], int],
        supported_efforts: frozenset[tuple[str, str, str]] = frozenset(),
    ) -> CodexRuntime:
        """Consume only this factory's gated connection through the existing Runtime.

        Host cwd remains the authority subject; /workspace is only the native bind
        destination. Observation-time revocation is not continuous effect mediation.
        Cold connection reconstruction and isolated product activation remain unsupported.
        """
        connection, lifetime = self._connection, self._lifetime
        if connection is None or lifetime is None or not lifetime.live():
            raise ExecutionConflict("owned live gated OCI connection required")

        def retire(request: ExecutionRequest) -> None:
            if request != self.request:
                raise ExecutionConflict("OCI retirement request differs from owned Attempt")
            lifetime.close_liveness()
            try:
                connection.close()
            finally:
                self.stop()

        if connection.version != "0.159.3":
            error = UnsupportedExecution("owned OCI sandbox exception requires Codex 0.159.3")
            try:
                retire(self.request)
            except BaseException as retirement_error:
                raise error from retirement_error
            raise error
        stage = _object(
            json.loads(self.store.artifact(self.operation_id, f"runtime-dispatch:{self.request.fence}:worker"))
        )
        binding = stage.get("launch")
        if type(binding) is not str:
            raise ExecutionConflict("OCI runtime launch binding unavailable")
        launch = _object(json.loads(binding))
        evidence = resources.verify(self, codex)
        if launch.get("resources") != evidence.decode() or launch.get("owner") != self.token:
            raise ExecutionConflict("OCI runtime resources differ from original launch")
        dispatch, recovery = authorizers

        def inputs(request: ExecutionRequest) -> CodexInputs:
            if request != self.request:
                raise ExecutionConflict("OCI Runtime request differs from owned Attempt")
            value = resolve(request)
            if value.cwd != str(resources.workspace):
                raise ExecutionConflict("OCI Runtime host cwd differs from verified workspace")
            return value

        def active(request: ExecutionRequest) -> bool:
            if request != self.request:
                return False
            try:
                return (
                    lifetime.live()
                    and resources.verify(self, codex) == evidence
                    and active_authorizer(request, str(resources.workspace), binding.encode()) is True
                )
            except (OSError, ValueError, SourceAcquisitionError, ExecutionConflict):
                return False

        def authorize(request: ExecutionRequest) -> bool:
            if active(request):
                return True
            retire(request)
            return False

        def authorize_resolved(request: ExecutionRequest, cwd: str, method: str) -> bool:
            if not authorize(request):
                return False
            permitted = False
            try:
                permitted = dispatch(request, cwd, method)
            finally:
                if not permitted:
                    retire(request)
            return permitted

        def collect(request: ExecutionRequest) -> Candidate:
            if request != self.request:
                raise ExecutionConflict("OCI collection request differs from owned Attempt")
            return self.collect_candidate(resources, codex=codex, authorizer=active_authorizer)

        class OwnedRuntime(CodexRuntime):
            @override
            def _inputs(runtime_self, request: ExecutionRequest) -> CodexInputs:
                if runtime_self.connection is not connection or runtime_self.native_cwd != "/workspace":
                    raise ExecutionConflict("owned OCI sandbox exception cannot retarget its connection")
                value = super()._inputs(request)
                identifier = self._receipt()
                if identifier is None:
                    raise ExecutionConflict("owned OCI sandbox exception lacks container identity")
                # Owner decision 2026-10-11: OCI, not nested Codex namespaces, enforces isolation.
                self.store.finalize_artifact(
                    self.operation_id,
                    "oci-runtime-permissions",
                    _bytes(
                        {
                            "version": 1,
                            "owner": self.token,
                            "container": identifier,
                            "launch_reference": hashlib.sha256(binding.encode()).hexdigest(),
                            "codex_version": connection.version,
                            "sandbox": "danger-full-access",
                            "approvalPolicy": "never",
                            "owner_decision": "2026-10-11T13:09:41Z:Kernel53:PR85",
                        }
                    ),
                )
                return replace(value, sandbox="danger-full-access")

        return OwnedRuntime(
            connection,
            version=connection.version,
            resolve=inputs,
            authorize=authorize,
            authorize_resolved=authorize_resolved,
            authorize_recovery=recovery,
            collect=collect,
            clock=clock,
            supported_efforts=supported_efforts,
            native_cwd="/workspace",
            authorize_active=active,
            retire=retire,
            settled=lambda _: self.settled(),
        )
