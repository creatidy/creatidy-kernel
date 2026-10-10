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
import os
import stat
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from subprocess import CompletedProcess, TimeoutExpired
from types import MappingProxyType
from typing import cast

from creatidy_kernel.adapters.bubblewrap_verification import resource_digest
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.oci_lifetime import LifetimeChannel
from creatidy_kernel.adapters.worker_collection import CollectedFile, collect_directory_fd
from creatidy_kernel.adapters.worker_profile import WorkerProfile, attached_run_argv, rootless_run_prefix
from creatidy_kernel.core.execution import (
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    Presence,
    validate_relative_path,
)
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
WorkerAuthorizer = Callable[[ExecutionRequest, str, bytes], bool]
WorkerDeadline = Callable[[ExecutionRequest, str], int]


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in cast(dict[object, object], value)):
        raise ExecutionConflict("invalid OCI observation")
    return cast(dict[str, object], value)


def _digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


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
        for path, expected in (*pins.items(), *read_only.items()):
            if path.is_relative_to(self.workspace) or self.workspace.is_relative_to(path):
                raise ExecutionConflict("OCI writable workspace overlaps immutable resources")
            info = path.lstat()
            if path in pins:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024 * 1024:
                    raise ExecutionConflict("invalid OCI native/config resource")
                expected = hashlib.sha256(b".\0" + bytes.fromhex(expected)).hexdigest()
            actual = resource_digest(path)
            if path in read_only and any(
                item.is_file() and item.lstat().st_nlink != 1
                for item in ((path, *path.rglob("*")) if path.is_dir() else (path,))
            ):
                raise ExecutionConflict("OCI read-only resources have mutable hardlink aliases")
            if actual != expected:
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
        return _bytes(
            {
                "version": 1,
                "identities": identities,
                "pins": {str(path): digest for path, digest in pins.items()},
                "read_only": {str(path): digest for path, digest in read_only.items()},
                "protected": [str(path) for path in self.protected],
                "candidate_root": str(self.candidate_root),
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
            ("profile", profile),
            ("environment", MappingProxyType(supplied)),
            ("name", name),
            ("image", image),
            ("operation_id", request.operation.operation_id),
            ("frame", frame),
            ("token", hashlib.sha256(frame).hexdigest()),
            ("_lifetime", None),
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

    def settled(self) -> bool:
        """Require exact retained kernel init exit AND owned container absence, never cold PID guesses."""
        identifier = self._receipt()
        namespace = self.store.find_artifact(self.operation_id, "oci-init")
        if identifier is None or namespace is None:
            return False
        record = _object(json.loads(namespace))
        if record.get("owner") != self.token or record.get("container") != identifier:
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
            return self.state() is Presence.ABSENT
        lifetime = self._lifetime
        if (
            lifetime is None
            or lifetime.identity != record.get("identity")
            or not lifetime.exited()
            or self.state() is not Presence.ABSENT
        ):
            return False
        self.store.finalize_artifact(self.operation_id, "oci-namespace-settled", witness)
        lifetime.close()
        object.__setattr__(self, "_lifetime", None)
        return True

    def require_settled(self) -> None:
        if not self.settled():
            raise ExecutionConflict("owned OCI namespace settlement unproved")

    def collect_candidate(self, resources: OCIResources) -> Candidate:
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
        if namespace.get("binding_reference") != hashlib.sha256(binding.encode()).hexdigest():
            raise ExecutionConflict("OCI namespace differs from original launch")
        retained = _object(json.loads(cast(str, launch.get("resources"))))
        if retained.get("candidate_root") != str(resources.candidate_root):
            raise ExecutionConflict("OCI candidate root differs from original launch")
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
            collected = collect_directory_fd(descriptor, sink=sink)
            if collected.refusals:
                raise ExecutionConflict("OCI candidate collection refused unsafe entries")
        self.require_settled()
        manifest = ArtifactManifest(self.request.workspace.key, tuple(sorted(artifacts, key=lambda item: item.path)))
        candidate = Candidate(self.request.attempt.attempt_id, self.request.attempt.spec_digest, manifest)
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
        if "HOME" not in probe or any(
            not Path(probe[key]).is_relative_to(resources.private_parent)
            or any(
                Path(probe[key]).is_relative_to(root) or root.is_relative_to(Path(probe[key]))
                for root in (resources.workspace, *resources.protected)
            )
            for key in ("HOME", "CODEX_HOME", "TMPDIR")
            if key in probe
        ):
            raise ExecutionConflict("OCI probe state must be private and separate")
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
                self.store.finalize_artifact(
                    self.operation_id,
                    "oci-init",
                    _bytes(
                        {
                            "version": 1,
                            "owner": self.token,
                            "container": identifier,
                            "identity": identity,
                            "binding_reference": hashlib.sha256(binding).hexdigest(),
                        }
                    ),
                )
                object.__setattr__(self, "_lifetime", channel)
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
        return connection
