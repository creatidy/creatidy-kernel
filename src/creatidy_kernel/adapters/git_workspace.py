# SPDX-License-Identifier: Apache-2.0
"""Journaled source-backed generations, not isolated workers or implicit Git authority.

Materialization precedes grants and worker exposure. Interrupted generations are
retained, never adopted or refilled. Recovery reads only protected original Git
objects; worker-controlled Git configuration is never executed by the controller.
"""

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from creatidy_kernel.adapters.bubblewrap_verification import resource_digest
from creatidy_kernel.adapters.forge_refs import oid
from creatidy_kernel.adapters.source_cache import (
    SourceAcquisitionError,
    canonical_repository,
    copy_source_objects,
    directory_identity,
    publish_directory,
    source_git,
    source_use,
    validate_checkout,
    validate_source_repository,
)
from creatidy_kernel.adapters.sqlite_store import OperationConflict
from creatidy_kernel.adapters.worker_collection import MAX_DEPTH, MAX_ENTRIES, MAX_FILE_BYTES
from creatidy_kernel.core.execution import (
    ArtifactManifest,
    ExecutionConflict,
    ExecutionRequest,
    Lookup,
    Presence,
    TrustMode,
    UnsupportedExecution,
    WorkspaceHandle,
    WorkspaceSpec,
    validate_relative_path,
)
from creatidy_kernel.ports.execution import Workspace
from creatidy_kernel.ports.program_store import ApplicationStore

if TYPE_CHECKING:
    from creatidy_kernel.adapters.oci_lifecycle import OCIResources, OwnedOCI

DEFAULT_CANDIDATE_ROOT = PurePosixPath("candidate")


def _bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in cast(dict[object, object], value)):
        raise ExecutionConflict("invalid Workspace generation record")
    return cast(dict[str, object], value)


def _spec(spec: WorkspaceSpec) -> dict[str, object]:
    return {
        "repository": spec.repository,
        "base_revision": spec.base_revision,
        "overlays": [asdict(item) for item in spec.overlays],
        "toolchain_digest": spec.toolchain_digest,
        "enforcement_profile": spec.enforcement_profile,
        "trust_mode": spec.trust_mode.value,
        "network_destinations": sorted(spec.network_destinations),
        "mounts": sorted(spec.mounts),
    }


def verify_generation(
    store: ApplicationStore,
    operation_id: str,
    handle: WorkspaceHandle,
    workspace: Path,
    private_parent: Path,
    candidate_root: PurePosixPath,
    protected: tuple[Path, ...],
) -> bytes:
    """Verify original ownership without consulting mutable worker Git state."""
    if operation_id != "workspace:" + hashlib.sha256(handle.key.encode()).hexdigest():
        raise ExecutionConflict("Workspace operation identity differs from original key")
    record_bytes = store.find_artifact(operation_id, "workspace-created")
    if record_bytes is None:
        raise ExecutionConflict("Workspace completed generation unavailable")
    record = _object(json.loads(record_bytes))
    plan = _object(json.loads(store.operation(operation_id).request_json)).get("request")
    plan = _object(plan)
    if (
        set(record) != {"version", "plan_reference", "identities", "objects_digest"}
        or type(record.get("version")) is not int
        or record.get("version") != 1
        or type(plan.get("version")) is not int
        or plan.get("version") != 1
        or record.get("plan_reference") != hashlib.sha256(_bytes(plan)).hexdigest()
        or plan.get("key") != handle.key
        or plan.get("spec") != _spec(handle.spec)
        or plan.get("workspace") != str(workspace)
        or plan.get("parent") != str(private_parent)
        or plan.get("candidate_root") != str(candidate_root)
    ):
        raise ExecutionConflict("Workspace generation differs from original binding")
    original_objects = plan.get("objects")
    if type(original_objects) is not str:
        raise ExecutionConflict("Workspace original object binding unavailable")
    objects = Path(original_objects)
    if not any(objects.is_relative_to(root) for root in protected):
        raise ExecutionConflict("Workspace original objects are not protected")
    identities = _object(record.get("identities"))
    for path in (private_parent, workspace, objects, workspace / candidate_root):
        if identities.get(str(path)) != list(directory_identity(path)):
            raise ExecutionConflict("Workspace original generation identity changed")
    if private_parent.stat().st_mode & 0o077:
        raise ExecutionConflict("Workspace parent is not private")
    validate_checkout(objects, bare=True)
    if any(path.is_file() and path.lstat().st_nlink != 1 for path in objects.rglob("*")):
        raise ExecutionConflict("Workspace original objects have mutable hardlink aliases")
    if resource_digest(objects) != record.get("objects_digest"):
        raise ExecutionConflict("Workspace original objects changed or unavailable")
    if (
        source_git(objects, "rev-parse", "--verify", f"{handle.spec.base_revision}^{{commit}}").strip()
        != handle.spec.base_revision
    ):
        raise ExecutionConflict("Workspace original base unavailable")
    return record_bytes


def _sync_tree(root: Path) -> None:
    """Flush this still-unexposed controller tree; refuse aliases or special entries."""
    entries = list(root.rglob("*"))
    if len(entries) > 20000:
        raise ExecutionConflict("Workspace object/tree entry bound exceeded")
    for path in entries:
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ExecutionConflict("Workspace source contains unsafe aliases or entries")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for path in (*reversed([path for path in entries if path.is_dir()]), root):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class GitWorkspace(Workspace):
    """One explicitly bound source generation; historical repository IDs stay opaque."""

    def __init__(
        self,
        store: ApplicationStore,
        source: Path,
        source_url: str,
        repository: str,
        workspace: Path,
        objects: Path,
        *,
        candidate_root: PurePosixPath = DEFAULT_CANDIDATE_ROOT,
    ) -> None:
        validate_relative_path(str(candidate_root))
        if (
            workspace.parent != objects.parent
            or workspace == objects
            or workspace == source
            or source.is_relative_to(workspace.parent)
            or workspace.parent.is_relative_to(source)
            or not repository
        ):
            raise ExecutionConflict("Workspace/source/controller paths must be separate")
        directory_identity(workspace.parent)
        if workspace.parent.stat().st_mode & 0o077:
            raise ExecutionConflict("Workspace parent must be private")
        self.store = store
        self.source = source
        self.source_url = canonical_repository(source_url)
        self.repository = repository
        self.workspace = workspace
        self.objects = objects
        self.candidate_root = candidate_root
        self._handle: WorkspaceHandle | None = None
        self._operation_id: str | None = None
        self._owner: OwnedOCI | None = None
        self._resources: OCIResources | None = None
        self._codex: Path | None = None
        self._collection_authorizer: Callable[[ExecutionRequest, str, bytes], bool] | None = None

    def materialize(self, key: str, spec: WorkspaceSpec) -> WorkspaceHandle:
        if (
            spec.trust_mode is not TrustMode.TRUSTED_DEVELOPMENT
            or spec.overlays
            or spec.mounts
            or spec.network_destinations
        ):
            raise UnsupportedExecution("Workspace cannot expand the admitted trusted-development subset")
        if spec.repository != self.repository or not key.strip():
            raise ExecutionConflict("Workspace source identity differs")
        oid(spec.base_revision)
        handle = WorkspaceHandle(key, spec)
        if self._handle is not None and self._handle != handle:
            raise ExecutionConflict("Workspace adapter already binds another generation")
        operation_id = "workspace:" + hashlib.sha256(key.encode()).hexdigest()
        plan: dict[str, object] = {
            "version": 1,
            "key": key,
            "spec": _spec(spec),
            "source": str(self.source),
            "source_url": self.source_url,
            "workspace": str(self.workspace),
            "objects": str(self.objects),
            "parent": str(self.workspace.parent),
            "candidate_root": str(self.candidate_root),
        }
        operation = self.store.intent(operation_id, operation_id, plan)
        self._handle, self._operation_id = handle, operation_id
        if self.store.find_artifact(operation_id, "workspace-created") is not None:
            self._verify()
            return handle
        if (
            operation.attempts
            or self.workspace.exists()
            or self.workspace.is_symlink()
            or self.objects.exists()
            or self.objects.is_symlink()
        ):
            raise ExecutionConflict("partial or unowned Workspace generation must be preserved")
        fence = self.store.claim(operation_id, now=int(time.time()), lease_seconds=300)
        parent_identity = directory_identity(self.workspace.parent)
        root = Path(tempfile.mkdtemp(prefix=".workspace-staging-", dir=self.workspace.parent))
        objects = Path(tempfile.mkdtemp(prefix=".workspace-objects-", dir=self.workspace.parent))
        self.store.finalize_artifact(
            operation_id,
            "workspace-reserved",
            _bytes(
                {
                    "stage": str(root),
                    "identity": directory_identity(root),
                    "objects": str(objects),
                    "objects_identity": directory_identity(objects),
                    "parent": parent_identity,
                }
            ),
        )
        with source_use(self.source, self.source_url):
            validate_source_repository(self.source, self.source_url)
            source_identity = (directory_identity(self.source), directory_identity(self.source / ".git"))
            copy_source_objects(self.source, objects, base=spec.base_revision)
            validate_source_repository(self.source, self.source_url)
            if source_identity != (directory_identity(self.source), directory_identity(self.source / ".git")):
                raise ExecutionConflict("Workspace source changed during exact object copy")
        validate_checkout(objects, bare=True)
        entries = source_git(
            objects, "ls-tree", "-rzl", "--full-tree", spec.base_revision, max_bytes=4 * 1024 * 1024
        ).split("\0")
        paths: list[str] = []
        hashes: list[str] = []
        total = 0
        for entry in filter(None, entries):
            metadata, separator, path = entry.partition("\t")
            fields = metadata.split()
            validate_relative_path(path)
            if not separator or len(fields) != 4 or fields[0] not in {"100644", "100755"} or fields[1] != "blob":
                raise UnsupportedExecution("Workspace source tree contains unsupported entries")
            size = int(fields[3])
            total += size
            if (
                size > MAX_FILE_BYTES
                or total > 64 * 1024 * 1024
                or len(paths) >= MAX_ENTRIES
                or len(PurePosixPath(path).parts) > MAX_DEPTH
            ):
                raise UnsupportedExecution("Workspace source tree exceeds collection bounds")
            paths.append(path)
            hashes.append(fields[2])
        checkout = root / self.candidate_root
        checkout.parent.mkdir(parents=True, exist_ok=True)
        source_git(
            self.workspace.parent, "clone", "--no-local", "--no-checkout", "--template=", str(objects), str(checkout)
        )
        validate_checkout(checkout)
        source_git(checkout, "checkout", "--detach", spec.base_revision)
        source_git(checkout, "remote", "set-url", "origin", str(self.objects))
        if (
            paths
            and source_git(
                checkout, "hash-object", "--no-filters", "--", *paths, max_bytes=4 * 1024 * 1024
            ).splitlines()
            != hashes
        ):
            raise ExecutionConflict("Workspace checkout bytes differ from exact source tree")
        _sync_tree(root)
        _sync_tree(objects)
        if directory_identity(self.workspace.parent) != parent_identity:
            raise ExecutionConflict("Workspace parent changed before publication")
        publish_directory(objects, self.objects)
        publish_directory(root, self.workspace)
        record = {
            "version": 1,
            "plan_reference": hashlib.sha256(_bytes(plan)).hexdigest(),
            "identities": {
                str(path): directory_identity(path)
                for path in (self.workspace.parent, self.workspace, self.objects, self.workspace / self.candidate_root)
            },
            "objects_digest": resource_digest(self.objects),
        }
        self.store.finalize_artifact(operation_id, "workspace-created", _bytes(record))
        self._verify()
        self.store.record_transport(operation_id, fence, True)
        self.store.observe(operation_id, fence, "workspace-created", "accepted", reference=key)
        return handle

    def _verify(self) -> bytes:
        if self._handle is None or self._operation_id is None:
            raise ExecutionConflict("Workspace original generation unavailable")
        return verify_generation(
            self.store,
            self._operation_id,
            self._handle,
            self.workspace,
            self.workspace.parent,
            self.candidate_root,
            (self.objects,),
        )

    def bind(
        self,
        owner: "OwnedOCI",
        resources: "OCIResources",
        *,
        codex: Path | None = None,
        collection_authorizer: Callable[[ExecutionRequest, str, bytes], bool] | None = None,
    ) -> "OCIResources":
        if (
            owner.store is not self.store
            or owner.request.workspace != self._handle
            or resources.workspace != self.workspace
        ):
            raise ExecutionConflict("Workspace Runtime differs from original generation")
        self._verify()
        if resources.private_parent != self.workspace.parent or resources.candidate_root != self.candidate_root:
            raise ExecutionConflict("Workspace mount or Candidate mapping differs from original generation")
        if not any(self.objects.is_relative_to(root) for root in resources.protected):
            raise ExecutionConflict("Workspace original objects must remain protected")
        selected = replace(resources, workspace_operation=self._operation_id)
        self._owner, self._resources = owner, selected
        self._codex, self._collection_authorizer = codex, collection_authorizer
        return selected

    def collect(self, handle: WorkspaceHandle) -> ArtifactManifest:
        if handle != self._handle or self._owner is None or self._resources is None:
            raise ExecutionConflict("Workspace has no matching owned Runtime")
        self._verify()
        return self._owner.collect_candidate(
            self._resources,
            codex=self._codex,
            authorizer=self._collection_authorizer,
        ).artifacts

    def reconcile(self, key: str, handle: str | None = None) -> Lookup:
        if self._handle is None:
            operation_id = "workspace:" + hashlib.sha256(key.encode()).hexdigest()
            try:
                plan = _object(_object(json.loads(self.store.operation(operation_id).request_json)).get("request"))
                spec = _object(plan.get("spec"))
                if (
                    plan.get("key") != key
                    or plan.get("source") != str(self.source)
                    or plan.get("source_url") != self.source_url
                    or plan.get("workspace") != str(self.workspace)
                    or plan.get("objects") != str(self.objects)
                    or plan.get("parent") != str(self.workspace.parent)
                    or plan.get("candidate_root") != str(self.candidate_root)
                    or spec.get("repository") != self.repository
                    or spec.get("overlays") != []
                    or spec.get("mounts") != []
                    or spec.get("network_destinations") != []
                    or spec.get("trust_mode") != TrustMode.TRUSTED_DEVELOPMENT.value
                    or any(
                        type(spec.get(name)) is not str
                        for name in ("base_revision", "toolchain_digest", "enforcement_profile")
                    )
                ):
                    return Lookup(Presence.UNKNOWN)
                recovered = WorkspaceSpec(
                    self.repository,
                    cast(str, spec["base_revision"]),
                    (),
                    cast(str, spec["toolchain_digest"]),
                    cast(str, spec["enforcement_profile"]),
                    TrustMode.TRUSTED_DEVELOPMENT,
                )
                oid(recovered.base_revision)
                self._handle, self._operation_id = WorkspaceHandle(key, recovered), operation_id
            except (ValueError, ExecutionConflict, OperationConflict):
                return Lookup(Presence.UNKNOWN)
        if key != self._handle.key or handle not in {None, key}:
            return Lookup(Presence.UNKNOWN)
        try:
            self._verify()
        except (OSError, ValueError, SourceAcquisitionError, ExecutionConflict):
            return Lookup(Presence.UNKNOWN)
        return Lookup(Presence.FOUND, key)

    def cleanup(self, handle: WorkspaceHandle, *, worker_settled: bool) -> None:
        del handle, worker_settled
        raise UnsupportedExecution("Workspace owned deletion is not received; preserve the generation")
