# SPDX-License-Identifier: Apache-2.0
"""Disposable, deterministic reference composition, explicitly not an OS sandbox."""

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
from creatidy_kernel.adapters.reference_forge import deliver_reference_pr
from creatidy_kernel.adapters.sqlite_store import OperationConflict, ProgramNotFound, SQLiteProgramStore
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AuthorityEnvelope,
    BudgetPolicy,
    InputBinding,
    PolicyReference,
    ProgramSpec,
    ProgramStatus,
    WorkUnit,
    WorkUnitStatus,
)
from creatidy_kernel.core.execution import (
    Activity,
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionRequest,
    Lookup,
    OperationKey,
    Presence,
    RuntimeIdentity,
    RuntimeObservation,
    TrustMode,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.core.verification import Evidence, EvidenceSubject, VerificationPolicy
from creatidy_kernel.ports.application import Collection, advance_work_unit, manifest_bytes

POLICY = VerificationPolicy(PolicyReference("reference", "1", "reference-checks:v1"), ("exact-content",), False)
EXPECTED = {"first": b"first\n", "second": b"first\nsecond\n"}


class ReferenceInterrupted(RuntimeError):
    """A requested crash boundary was reached after durable state was written."""


def reference_request(store: SQLiteProgramStore, operation: str) -> dict[str, object]:
    raw = cast(dict[str, object], json.loads(store.operation(operation).request_json))
    return cast(dict[str, object], raw["request"])


def reference_git(repository: Path, *arguments: str, data: bytes | None = None) -> str:
    environment = {
        "PATH": os.defpath,
        "HOME": str(repository),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Reference",
        "GIT_AUTHOR_EMAIL": "reference@example.invalid",
        "GIT_COMMITTER_NAME": "Reference",
        "GIT_COMMITTER_EMAIL": "reference@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise RuntimeError("Git is required for the disposable reference repository")
    return (
        subprocess.run(  # noqa: S603 - fixed Git executable and controller-built arguments; no shell.
            [executable, "-C", str(repository), *arguments],
            input=data,
            capture_output=True,  # noqa: S603
            check=True,
            timeout=10,
            env=environment,
        )
        .stdout.decode()
        .strip()
    )


def reference_commit(repository: Path, content: bytes, parent: str | None = None) -> str:
    blob = reference_git(repository, "hash-object", "-w", "--stdin", data=content)
    tree = reference_git(repository, "mktree", data=f"100644 blob {blob}\tresult.txt\n".encode())
    return reference_git(
        repository, "commit-tree", tree, *(("-p", parent) if parent else ()), data=b"Reference output\n"
    )


def prepare_reference_directory(directory: Path) -> Path:
    """Refuse to commandeer an existing checkout or a symlinked workspace."""
    repository = directory / "repository"
    if repository.is_symlink() or (repository.exists() and not (directory / "kernel.sqlite3").is_file()):
        raise ValueError("reference requires a new disposable repository or its existing durable journal")
    directory.mkdir(parents=True, exist_ok=True)
    repository.mkdir(exist_ok=True)
    if not (repository / ".git").exists():
        reference_git(repository, "init", "--initial-branch=develop")
    return repository


def reference_spec() -> ProgramSpec:
    return ProgramSpec(
        "reference",
        "owner-approved finite two-node reference",
        (
            WorkUnit(
                "first",
                "write first",
                required_inputs=frozenset({"seed"}),
                outputs=frozenset({"product"}),
                acceptance_criteria=("exact-content",),
            ),
            WorkUnit(
                "second",
                "extend accepted first",
                dependencies=("first",),
                required_inputs=frozenset({"product"}),
                outputs=frozenset({"final"}),
                acceptance_criteria=("exact-content",),
            ),
        ),
        initial_inputs=(InputBinding("seed", "reference:empty"),),
        budget=BudgetPolicy(max_attempts=2),
        acceptance_criteria=("exact-content",),
        policy_references=(POLICY.reference,),
        authority=AuthorityEnvelope(
            "owner",
            max_attempts=2,
            trusted_satisfaction_issuers=frozenset({"verifier"}),
            delegated_actor_ids=frozenset({"worker"}),
        ),
    )


class DurableReferenceRuntime:
    """Synthetic external reality survives controller and adapter reconstruction.

    This deterministic fake deliberately has no credentials or isolation claims.
    Losing its journal is unknown, not proof that a dispatched effect was absent.
    """

    def __init__(self, store: SQLiteProgramStore, *, lost: bool = False) -> None:
        self.store = store
        self.lost = lost

    def start(self, request: ExecutionRequest) -> str:
        if self.lost:
            raise RuntimeError("runtime context unavailable")
        unit = request.attempt.work_unit_id
        content = b"first\n"
        if unit == "second":
            predecessor = self.store.artifact("runtime:reference:first", "output")
            expected_ref = "artifact:sha256:" + hashlib.sha256(predecessor).hexdigest()
            if (
                len(request.attempt.effective_inputs) != 1
                or request.attempt.effective_inputs[0].reference != expected_ref
            ):
                raise ValueError("second input is not exact accepted predecessor bytes")
            content = predecessor + b"second\n"
        self.store.intent(
            request.operation.operation_id,
            request.operation.effect_key,
            {
                "digest": request.operation.request_digest,
                "attempt": request.attempt.attempt_id,
                "spec": request.attempt.digest,
                "workspace": request.workspace.key,
            },
        )
        self.store.finalize_artifact(request.operation.operation_id, "output", content)
        return request.operation.operation_id

    def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup:
        if self.lost:
            return Lookup(Presence.UNKNOWN)
        try:
            record = reference_request(self.store, operation.operation_id)
        except OperationConflict:
            return Lookup(Presence.ABSENT)
        if record["digest"] != operation.request_digest or handle not in (None, operation.operation_id):
            return Lookup(Presence.UNKNOWN)
        return Lookup(Presence.FOUND, operation.operation_id)

    def observe(self, handle: str, *, now: int) -> RuntimeObservation:
        return RuntimeObservation(
            handle,
            Activity.UNKNOWN if self.lost else Activity.TERMINAL,
            now,
            now,
            RuntimeIdentity("deterministic", "deterministic", "deterministic", "reference:v1"),
            False,
        )

    def candidate(self, handle: str) -> Candidate | None:
        if self.lost:
            return None
        record = reference_request(self.store, handle)
        digest = hashlib.sha256(self.store.artifact(handle, "output")).hexdigest()
        return Candidate(
            str(record["attempt"]),
            str(record["spec"]),
            ArtifactManifest(str(record["workspace"]), (Artifact("result.txt", "sha256:" + digest),)),
        )

    def cancel(self, handle: str) -> RuntimeObservation:
        return self.observe(handle, now=int(time.time()))


class ReferenceChecks:
    def __init__(self, content: bytes, expected: bytes, repository: Path, head: str, now: int) -> None:
        self.content, self.expected, self.repository, self.head, self.now = content, expected, repository, head, now

    def check(self, name: str, subject: EvidenceSubject) -> Evidence:
        committed = reference_git(self.repository, "rev-parse", f"{self.head}:result.txt")
        expected_blob = reference_git(self.repository, "hash-object", "--stdin", data=self.expected)
        passed = (
            name == "exact-content"
            and self.content == self.expected
            and committed == expected_blob
            and subject.head_revision == self.head
        )
        return Evidence(
            f"{subject.candidate_digest}:{name}:{self.now}",
            name,
            subject,
            "controller-checker",
            "reference-checks:v1",
            "git:" + self.head,
            self.now,
            self.now,
            passed,
        )


class ReferenceCollector:
    def __init__(self, external: SQLiteProgramStore, control: SQLiteProgramStore, repository: Path, base: str) -> None:
        self.external, self.control, self.repository, self.base = external, control, repository, base

    def collect(self, request: ExecutionRequest, candidate: Candidate, *, now: int) -> Collection:
        content = self.external.artifact(request.operation.operation_id, "output")
        digest = self.control.finalize_artifact(request.operation.operation_id, "output", content)
        manifest = ArtifactManifest(request.workspace.key, (Artifact("result.txt", digest),))
        if manifest != candidate.artifacts:
            raise ValueError("worker artifact digest differs from trusted collection")
        parent = self.base
        if request.attempt.work_unit_id == "second":
            parent = str(reference_request(self.control, "acceptance:reference:first")["head"])
        head = reference_commit(self.repository, content, parent)
        return Collection(
            manifest,
            str(self.repository),
            parent,
            head,
            ReferenceChecks(content, EXPECTED[request.attempt.work_unit_id], self.repository, head, now),
        )


def reference_export(store: SQLiteProgramStore) -> dict[str, object]:
    program = store.load("reference")
    mode = reference_request(store, "reference:mode")["mode"]
    attempts: list[dict[str, object]] = []
    for attempt in program.attempts:
        operation = store.operation(f"runtime:{attempt.spec.attempt_id}")
        entry: dict[str, object] = {
            "attempt": asdict(attempt),
            "operation": asdict(operation),
            "resources": {
                "value": None,
                "unit": "tokens",
                "source": "synthetic-not-metered" if mode == "offline" else "codex-usage-unavailable",
            },
        }
        try:
            entry["identity"] = json.loads(store.artifact(operation.operation_id, "identity"))
        except OperationConflict:
            entry["identity"] = None
        if program.state(attempt.spec.work_unit_id).status is WorkUnitStatus.SATISFIED:
            accepted = reference_request(store, f"acceptance:{attempt.spec.attempt_id}")
            entry["verification"] = json.loads(store.artifact(str(accepted["operation"]), str(accepted["name"])))
        rejected: list[object] = []
        for record in store.operations(f"rejection:{attempt.spec.attempt_id}:"):
            rejection = reference_request(store, record.operation_id)
            digest = str(rejection["manifest"]).removeprefix("sha256:")
            rejected.append(json.loads(store.artifact(operation.operation_id, f"verification:{digest}")))
        entry["rejections"] = rejected
        attempts.append(entry)
    interruptions: list[dict[str, object]] = []
    for boundary in ("commit", "send", "receipt", "live-budget"):
        try:
            interruptions.append(reference_request(store, f"interruption:{boundary}"))
        except OperationConflict:
            pass
    result: dict[str, object] = {
        "schema": 1,
        "mode": mode,
        "program": program.program_id,
        "status": program.status.value,
        "trust_mode": "trusted_development",
        "isolation": "none; deterministic synthetic worker" if mode == "offline" else "not attested",
        "attempts": attempts,
        "accepted": [asdict(item) for item in program.satisfactions],
        "remediation": {"budget": 0, "policy": "pause on failed independent check"},
        "interruptions": interruptions,
        "unknowns": ["runtime usage is unavailable"],
        "automatic_merge": False,
        "automatic_deploy": False,
    }
    try:
        result["pr"] = json.loads(store.artifact("reference:pr", "export"))
        result["pr_observation"] = "historical; export does not contact the forge"
    except OperationConflict:
        pass
    return result


def export_reference(directory: Path) -> dict[str, object]:
    if not (directory / "kernel.sqlite3").is_file():
        raise FileNotFoundError("reference database does not exist")
    with SQLiteProgramStore(directory / "kernel.sqlite3") as store:
        return reference_export(store)


def run_reference(directory: Path, *, owner_approved: bool, fault: str | None = None) -> dict[str, object]:
    if owner_approved is not True:
        raise ValueError("explicit owner approval is required")
    if fault not in {None, "commit", "send", "receipt", "lost-context", "pr-commit", "pr-send", "pr-receipt"}:
        raise ValueError("unsupported reference fault")
    directory = directory.resolve()
    if (directory / "kernel.sqlite3").exists() and not (directory / "runtime.sqlite3").exists():
        raise RuntimeError("external runtime journal missing; effect presence is unknown")
    repository = prepare_reference_directory(directory)
    base = reference_commit(repository, b"")
    allocator = FixedAllocator(
        Allocation(
            "fake", "local", "deterministic", frozenset({"reference"}), 128, "owner-configured offline reference"
        )
    )
    with (
        SQLiteProgramStore(directory / "kernel.sqlite3") as store,
        SQLiteProgramStore(directory / "runtime.sqlite3") as external,
    ):
        store.intent("reference:mode", "reference:mode", {"mode": "offline"})
        try:
            program = store.load("reference")
        except ProgramNotFound:
            program = store.create(reference_spec(), "create")
        if program.spec != reference_spec():
            raise ValueError("reference intent differs from durable owner-approved spec")
        if program.status is ProgramStatus.DRAFT:
            program = store.admit("reference", "activate", ActivateProgram(program.revision, "owner"))

        def crash(boundary: str) -> None:
            if fault == boundary:
                store.intent(
                    f"interruption:{boundary}",
                    f"interruption:{boundary}",
                    {"boundary": boundary, "source": "owner-requested synthetic fault"},
                )
                raise ReferenceInterrupted(f"interrupted after {boundary}; rerun to reconcile")

        runtime = DurableReferenceRuntime(external, lost=fault == "lost-context")
        for unit in ("first", "second"):
            program = store.load("reference")
            if program.state(unit).status is WorkUnitStatus.SATISFIED:
                continue
            workspace = WorkspaceHandle(
                f"workspace:{unit}",
                WorkspaceSpec(str(repository), base, (), "reference:v1", "synthetic", TrustMode.TRUSTED_DEVELOPMENT),
            )
            status = advance_work_unit(
                store,
                program,
                unit,
                allocator=allocator,
                runtime=runtime,
                workspace=workspace,
                collector=ReferenceCollector(external, store, repository, base),
                policy=POLICY,
                now=int(time.time()),
                fault=crash,
            )
            if status != "accepted":
                result = reference_export(store)
                result["condition"] = status
                return result
        accepted = reference_request(store, "acceptance:reference:second")
        receipt = deliver_reference_pr(
            store,
            repository_path=repository,
            head=str(accepted["head"]),
            acceptance_reference=str(accepted["manifest"]),
            fault=fault,
        )
        if receipt.get("status") == "accepted":
            try:
                store.artifact("reference:pr", "export")
            except OperationConflict:
                store.finalize_artifact("reference:pr", "export", manifest_bytes(receipt))
        result = reference_export(store)
        result["pr"] = receipt
        result["pr_observation"] = "current adapter readback"
        result["head"] = accepted["head"]
        return result
