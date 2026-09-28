# SPDX-License-Identifier: Apache-2.0
"""D1-02 dogfood composition: one frozen owner-approved real task, end to end.

This module composes already-proved K1-K8 mechanisms into a single supported path:
Program/Attempt durability, ResourceAllocator, CodexRuntime, controller-owned Git
candidates, bounded controller-owned verification, and the existing Forgejo AGit
delivery. It is not a workflow engine, an issue parser, or a generic task runner.

Trust boundary: trusted development only. The coding Runtime edits a disposable
controller-owned workspace; isolation beyond Codex workspace-write is not attested.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.codex_runtime import CodexConnection, CodexInputs, CodexRuntime
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
from creatidy_kernel.adapters.forge_refs import oid
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport
from creatidy_kernel.adapters.reference import reference_git, reference_git_bytes
from creatidy_kernel.adapters.reference_forge import deliver_reference_pr
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator
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
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionRequest,
    TrustMode,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.core.verification import Evidence, EvidenceSubject, VerificationPolicy
from creatidy_kernel.ports.allocation import load_allocation
from creatidy_kernel.ports.application import Collection, advance_work_unit, manifest_bytes
from creatidy_kernel.ports.forge import Forge
from creatidy_kernel.ports.resources import ResourceAllocator

PROGRAM_ID = "dogfood"
UNIT_ID = "change"
ATTEMPT_ID = f"{PROGRAM_ID}:{UNIT_ID}"
OPERATION_ID = f"runtime:{ATTEMPT_ID}"
CANDIDATE_OPERATION = f"candidate:{ATTEMPT_ID}"
PR_OPERATION = "dogfood:pr"
WORKSPACE_KEY = "dogfood:workspace"
CANDIDATE_PATH = "candidate.patch"
POLICY = VerificationPolicy(
    PolicyReference("dogfood", "1", "dogfood-checks:v1"),
    ("changed-paths", "verification-commands", "structural"),
    False,
)
CANDIDATE_MESSAGE = "creatidy-kernel dogfood candidate"
CONTROLLER_IDENTITY = ("Creatidy Kernel", "kernel@creatidy.invalid")
MAX_PATCH_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 300
EVIDENCE_FRESHNESS_SECONDS = 900
COMMAND_ENV_KEYS = ("PATH", "LANG")
DEFERRED_TO_REVIEW = (
    "semantic correctness of the de-flake mechanism (the race is actually removed)",
    "absence of novel weakening forms beyond the deterministic pattern checks",
    "equivalence of scenario coverage and assertion strength",
)


class DogfoodInterrupted(RuntimeError):
    """A requested crash boundary was reached after durable state was written."""


def _nonempty(value: object, field: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"dogfood task {field} must be a nonempty string")


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """Trusted controller-supplied argv; worker output never selects commands."""

    argv: tuple[str, ...]
    timeout_seconds: int
    repeats: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(type(item) is not str or not item for item in self.argv)
        ):
            raise ValueError("verification argv must be a nonempty tuple of nonempty strings")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 24 * 3600:
            raise ValueError("verification timeout must be a bounded positive integer")
        if type(self.repeats) is not int or not 1 <= self.repeats <= 64:
            raise ValueError("verification repeats must be bounded")


@dataclass(frozen=True, slots=True)
class StructuralPolicy:
    """Deterministic anti-weakening constraints for one frozen task; not a DSL."""

    required_names: tuple[str, ...]
    required_tokens: tuple[str, ...]
    forbidden_added_patterns: tuple[str, ...]
    forbidden_sleep_seconds: float

    def __post_init__(self) -> None:
        for field, values in (("required_names", self.required_names), ("required_tokens", self.required_tokens)):
            if type(values) is not tuple or any(type(item) is not str or not item.strip() for item in values):
                raise ValueError(f"structural {field} must be nonempty strings")
        if type(self.forbidden_added_patterns) is not tuple or not self.forbidden_added_patterns:
            raise ValueError("structural policy requires forbidden added-line patterns")
        for pattern in self.forbidden_added_patterns:
            if type(pattern) is not str or not pattern:
                raise ValueError("structural patterns must be nonempty strings")
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid structural pattern: {pattern}") from error
        if type(self.forbidden_sleep_seconds) not in (int, float) or self.forbidden_sleep_seconds <= 0:
            raise ValueError("structural sleep threshold must be positive")


@dataclass(frozen=True, slots=True)
class DogfoodTaskSpec:
    """Controller-owned frozen task contract; never parsed from issue prose."""

    task_id: str
    forge_repository: str
    repository_url: str
    base_branch: str
    instruction: str
    repository_instructions: str
    allowed_paths: frozenset[str]
    verification: tuple[VerificationCommand, ...]
    structural: StructuralPolicy
    expected_base_sha: str | None = None
    max_attempts: int = 1
    pr_title: str = "Creatidy Kernel dogfood candidate"
    pr_body: str = "Controller-verified dogfood candidate. No merge or deployment authorized."

    def __post_init__(self) -> None:
        for field in ("task_id", "forge_repository", "repository_url", "base_branch", "instruction"):
            _nonempty(getattr(self, field), field)
        if not self.forge_repository.startswith("forgejo:"):
            raise ValueError("dogfood forge repository must be forgejo-qualified")
        if (
            type(self.allowed_paths) is not frozenset
            or not self.allowed_paths
            or any(
                type(item) is not str or not item or item.startswith("/") or ".." in item for item in self.allowed_paths
            )
        ):
            raise ValueError("allowed paths must be relative nonempty repository paths")
        if (
            type(self.verification) is not tuple
            or not self.verification
            or any(type(item) is not VerificationCommand for item in self.verification)
        ):
            raise ValueError("verification plan must be nonempty trusted commands")
        if type(self.structural) is not StructuralPolicy:
            raise ValueError("structural policy required")
        if self.expected_base_sha is not None:
            oid(self.expected_base_sha)
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("attempt budget must be between 1 and 3")
        _nonempty(self.pr_title, "pr_title")
        _nonempty(self.pr_body, "pr_body")

    @property
    def objective(self) -> str:
        return f"dogfood task {self.task_id}: {self.repository_url} ({self.base_branch})"

    def payload(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "forge_repository": self.forge_repository,
            "repository_url": self.repository_url,
            "base_branch": self.base_branch,
            "expected_base_sha": self.expected_base_sha,
            "instruction": self.instruction,
            "allowed_paths": sorted(self.allowed_paths),
            "verification": [
                {"argv": list(command.argv), "timeout_seconds": command.timeout_seconds, "repeats": command.repeats}
                for command in self.verification
            ],
            "structural": {
                "required_names": list(self.structural.required_names),
                "required_tokens": list(self.structural.required_tokens),
                "forbidden_added_patterns": list(self.structural.forbidden_added_patterns),
                "forbidden_sleep_seconds": self.structural.forbidden_sleep_seconds,
            },
            "max_attempts": self.max_attempts,
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(manifest_bytes(self.payload())).hexdigest()


def scarcity_router_143_task(expected_base_sha: str | None = None) -> DogfoodTaskSpec:
    """The frozen D1-01 dogfood contract for BioMedical-IT/scarcity-router#143.

    The D1-01 reference SHA 781bace66cec4f91b952cbe1993a3524be90fb63 is historical
    evidence. By default the base is frozen from the then-current source `develop`
    when the task is first admitted, so a later live run does not silently force
    the old SHA; pass expected_base_sha explicitly to pin a reproduction base.
    """
    targeted = (
        "uv",
        "run",
        "python",
        "-m",
        "unittest",
        "tests.test_e2e_execution.CancellationTests",
        "-v",
    )
    instruction = (
        "Fix the cancellation race in tests/test_e2e_execution.py, class CancellationTests, "
        "scenario-11 tests test_scenario_11_client_disconnect_propagates_cancel and "
        "test_scenario_11_cancel_reaches_exactly_the_owning_worker.\n"
        "Both tests currently drain the response socket after SHUT_WR and break on EOF, so the "
        "final assertion can run before the worker reader thread records the cancel. Replace the "
        "racy EOF/drain observation order with deterministic bounded synchronization around the "
        "cancel-delivery event (for example waiting on the worker reader's cancel-received event, "
        "or a bounded post-EOF grace poll of worker.cancels).\n"
        "Constraints: edit only tests/test_e2e_execution.py; do not change production code; keep "
        "both scenario tests and the worker-ownership requirement (bystander.cancels stays empty); "
        "do not add skip/xfail, catch-and-ignore of the failure, arbitrary large sleeps, or "
        "reduced repeat counts."
    )
    repository_instructions = (
        "The repository uses uv and unittest. The targeted class is "
        "tests.test_e2e_execution.CancellationTests. Tests are synthetic and offline; no network "
        "or credentials are required or permitted."
    )
    return DogfoodTaskSpec(
        task_id="scarcity-router-143",
        forge_repository="forgejo:BioMedical-IT/scarcity-router",
        repository_url="https://forgejo.creatidy.com/BioMedical-IT/scarcity-router",
        base_branch="develop",
        instruction=instruction,
        repository_instructions=repository_instructions,
        allowed_paths=frozenset({"tests/test_e2e_execution.py"}),
        verification=(
            VerificationCommand(targeted, 900),
            VerificationCommand(targeted, 900, repeats=8),
            VerificationCommand(("make", "check"), 3600),
        ),
        structural=StructuralPolicy(
            required_names=(
                "test_scenario_11_client_disconnect_propagates_cancel",
                "test_scenario_11_cancel_reaches_exactly_the_owning_worker",
            ),
            # Assertion-bearing tokens, not bare attribute mentions: the D1-03 spec
            # constructor must re-verify these exact forms against the actual base file.
            # The bystander token matches the argument order in the real source form
            # self.assertEqual([], bystander.cancels) (D1-03 preflight, SR 59538e9).
            required_tokens=("assertTrue(worker.cancels", "assertEqual([], bystander.cancels)"),
            forbidden_added_patterns=(
                r"@\s*(?:unittest\.)?skip(?:If|Unless)?\b",
                r"@pytest\.mark\.(?:skip|xfail)",
                r"\bxfail\b",
                r"except\s+(?:AssertionError|Exception|BaseException)",
            ),
            forbidden_sleep_seconds=5.0,
        ),
        expected_base_sha=expected_base_sha,
        max_attempts=1,
        pr_title="Fix cancellation race in scenario-11 CancellationTests (dogfood #143)",
    )


DOGFOOD_TASKS = {"143": scarcity_router_143_task}


def dogfood_spec(task: DogfoodTaskSpec) -> ProgramSpec:
    criteria = POLICY.checks
    return ProgramSpec(
        PROGRAM_ID,
        task.objective,
        (
            WorkUnit(
                UNIT_ID,
                f"execute frozen task {task.task_id}",
                required_inputs=frozenset({"task"}),
                outputs=frozenset({"change"}),
                acceptance_criteria=criteria,
            ),
        ),
        initial_inputs=(InputBinding("task", "dogfood:task:" + task.digest),),
        budget=BudgetPolicy(max_attempts=task.max_attempts),
        acceptance_criteria=criteria,
        policy_references=(POLICY.reference,),
        authority=AuthorityEnvelope(
            "owner",
            max_attempts=task.max_attempts,
            trusted_satisfaction_issuers=frozenset({"verifier"}),
            delegated_actor_ids=frozenset({"worker"}),
        ),
    )


def _request(store: SQLiteProgramStore, operation: str) -> dict[str, object]:
    raw = cast(dict[str, object], json.loads(store.operation(operation).request_json))
    return cast(dict[str, object], raw["request"])


def _git_failure(arguments: tuple[str, ...], error: subprocess.CalledProcessError) -> ValueError:
    detail = error.stderr.decode("utf-8", "replace").strip() if error.stderr else ""
    return ValueError(f"git {' '.join(arguments[:3])} failed: {detail}")


def git_text(repository: Path, *arguments: str, timeout: int = 60, index_file: str | None = None) -> str:
    try:
        return reference_git(repository, *arguments, author=CONTROLLER_IDENTITY, index_file=index_file, timeout=timeout)
    except subprocess.CalledProcessError as error:
        raise _git_failure(arguments, error) from None


def git_bytes(repository: Path, *arguments: str, timeout: int = 60) -> bytes:
    try:
        return reference_git_bytes(repository, *arguments, author=CONTROLLER_IDENTITY, timeout=timeout)
    except subprocess.CalledProcessError as error:
        raise _git_failure(arguments, error) from None


def git_data(repository: Path, *arguments: str, data: bytes, timeout: int = 60) -> str:
    try:
        return reference_git(repository, *arguments, data=data, author=CONTROLLER_IDENTITY, timeout=timeout)
    except subprocess.CalledProcessError as error:
        raise _git_failure(arguments, error) from None


def _resolve_base(source: Path, task: DogfoodTaskSpec) -> tuple[str, str]:
    if task.expected_base_sha is not None:
        resolved = git_text(source, "rev-parse", "--verify", f"{task.expected_base_sha}^{{commit}}")
        if resolved != task.expected_base_sha:
            raise ValueError("expected base SHA is not the canonical commit identity")
        return resolved, "pinned"
    for ref in (f"refs/remotes/origin/{task.base_branch}", f"refs/heads/{task.base_branch}"):
        try:
            return git_text(source, "rev-parse", "--verify", f"{ref}^{{commit}}"), ref
        except ValueError:
            continue  # an unresolvable candidate ref is an ordinary control outcome
    raise ValueError(f"source repository cannot resolve base branch {task.base_branch!r}")


def _porcelain(repository: Path) -> list[tuple[str, str]]:
    raw = git_bytes(repository, "status", "--porcelain", "-z")
    fields = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        status, path = entry[:2], entry[3:]
        index += 1
        if status[0] in "RC" and index < len(fields):
            index += 1  # renames carry the original path as a separate NUL field
        entries.append((status, path))
    return entries


def prepare_workspace(
    store: SQLiteProgramStore,
    directory: Path,
    source: Path,
    task: DogfoodTaskSpec,
    lifecycle: list[str],
    *,
    attempt_exists: bool,
) -> tuple[str, Path, Path]:
    """Establish and durably freeze the exact base; prepare the disposable workspace.

    The owner's checkout is only read (clone source, ref resolution). The workspace
    and bare object source are controller-owned clones; no canonical branch moves.
    """
    source = source.resolve()
    if source.is_symlink() or not source.is_dir() or not (source / ".git").exists():
        raise ValueError("source repository must be an existing non-symlink Git checkout")
    directory.mkdir(parents=True, exist_ok=True)
    if directory == source or directory.is_relative_to(source) or source.is_relative_to(directory):
        raise ValueError("control directory and source repository must be separate trees")
    try:
        recorded = _request(store, "dogfood:base")
    except OperationConflict as error:
        if str(error) != "unknown operation":
            raise
        recorded = None
    if recorded is None:
        base, ref = _resolve_base(source, task)
        store.intent("dogfood:base", "dogfood:base", {"base": base, "ref": ref, "task": task.task_id})
        lifecycle.append(f"exact base established: {base} ({ref})")
    else:
        base = str(recorded["base"])
        oid(base)
        if str(recorded["task"]) != task.task_id:
            raise ValueError("durable base belongs to a different dogfood task")
        if task.expected_base_sha is not None and task.expected_base_sha != base:
            raise ValueError("durable frozen base differs from the pinned expected base")
        lifecycle.append(f"exact base recovered: {base}")
    objects = directory / "objects.git"
    workspace = directory / "workspace"
    if workspace.is_symlink() or objects.is_symlink():
        raise ValueError("dogfood workspace/object paths must not be symlinks")
    if not objects.exists():
        objects.mkdir()
        git_text(directory, "clone", "--bare", "--template=", str(source), str(objects), timeout=GIT_TIMEOUT_SECONDS)
    if git_text(objects, "rev-parse", "--is-bare-repository") != "true":
        raise ValueError("controller object source must be bare")
    if git_text(objects, "cat-file", "-t", base) != "commit":
        raise ValueError("controller object source lacks the exact authorized base")
    if not workspace.exists():
        workspace.mkdir()
        git_text(
            directory,
            "clone",
            "--no-checkout",
            "--template=",
            str(objects),
            str(workspace),
            timeout=GIT_TIMEOUT_SECONDS,
        )
        git_text(workspace, "checkout", "--detach", base)
        lifecycle.append("workspace prepared: disposable controller-owned clone at the exact base")
    else:
        head = git_text(workspace, "rev-parse", "HEAD")
        if head == base:
            lifecycle.append("workspace recovered: detached at the exact base")
        else:
            parent = git_text(workspace, "rev-parse", f"{head}^")
            message = git_text(workspace, "log", "-1", "--format=%s", head)
            if parent != base or message != CANDIDATE_MESSAGE:
                raise ValueError("workspace HEAD is neither the base nor this controller's candidate")
            lifecycle.append(f"workspace recovered: at collected candidate {head}")
    if not attempt_exists and _porcelain(workspace):
        raise ValueError("dogfood workspace is unexpectedly dirty before any dispatch")
    return base, objects, workspace


def _derive_candidate(workspace: Path, base: str) -> tuple[str, str, bytes, tuple[str, ...]]:
    """Controller-owned candidate subject from the current workspace tree."""
    index = workspace / ".git" / "dogfood-index"
    if index.exists():
        index.unlink()
    try:
        git_text(workspace, "read-tree", base, index_file=str(index))
        git_text(workspace, "add", "-A", "--", index_file=str(index))
        tree = git_text(workspace, "write-tree", index_file=str(index))
        head = git_data(workspace, "commit-tree", tree, "-p", base, data=CANDIDATE_MESSAGE.encode())
    finally:
        index.unlink(missing_ok=True)
    patch = git_bytes(workspace, "diff", "--binary", base, head)
    if len(patch) > MAX_PATCH_BYTES:
        raise ValueError("candidate patch exceeds controller limit")
    return tree, head, patch, _changed_paths(workspace, base, head)


def _changed_paths(workspace: Path, base: str, head: str) -> tuple[str, ...]:
    raw = git_bytes(workspace, "diff", "--name-status", "-z", "-M", base, head)
    fields = [chunk.decode("utf-8", "replace") for chunk in raw.split(b"\0") if chunk]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            paths.extend(fields[index : index + 2])
            index += 2
        else:
            paths.append(fields[index])
            index += 1
    return tuple(path for path in paths if path)


def _install_candidate(workspace: Path, objects: Path, head: str) -> None:
    git_text(workspace, "reset", "--hard", head)
    git_text(workspace, "clean", "-fd")
    git_text(objects, "fetch", "--no-tags", str(workspace), "HEAD", timeout=GIT_TIMEOUT_SECONDS)


def _run_command(workspace: Path, command: VerificationCommand, repeat: int) -> dict[str, object]:
    environment = {"HOME": str(workspace), "LANG": "C.UTF-8"}
    for key in COMMAND_ENV_KEYS:
        if os.environ.get(key):
            environment[key] = os.environ[key]
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - trusted task-spec argv, no shell, pinned cwd.
            list(command.argv),
            cwd=workspace,
            env=environment,
            capture_output=True,  # noqa: S603
            timeout=command.timeout_seconds,
        )
        exit_code: int | None = completed.returncode
        timed_out = False
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as expired:
        exit_code, timed_out = None, True
        stdout = expired.stdout or b""
        stderr = expired.stderr or b""
    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "argv": list(command.argv),
        "repeat": repeat,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout_tail": stdout[-MAX_CAPTURE_BYTES:].decode("utf-8", "replace"),
        "stderr_tail": stderr[-MAX_CAPTURE_BYTES:].decode("utf-8", "replace"),
    }


class DogfoodChecks:
    """Deterministic controller-owned checks; semantic residue is explicitly deferred."""

    def __init__(
        self,
        store: SQLiteProgramStore,
        task: DogfoodTaskSpec,
        workspace: Path,
        base: str,
        head: str,
        paths: tuple[str, ...],
        lifecycle: list[str],
        fault: Callable[[str], None] | None = None,
        captured: dict[str, dict[str, object]] | None = None,
        evidence_now: int = 0,
    ) -> None:
        self.store = store
        self.task = task
        self.workspace = workspace
        self.base = base
        self.head = head
        self.paths = paths
        self.lifecycle = lifecycle
        self.fault = fault
        self.captured = captured if captured is not None else {}
        self.evidence_now = evidence_now
        self._last_payload: dict[str, object] = {}

    def _evidence(self, name: str, subject: EvidenceSubject, passed: bool, payload: dict[str, object]) -> Evidence:
        self._last_payload = payload
        data = manifest_bytes(payload)
        # Re-executed verification (deterministic local re-run after a crash) produces
        # fresh evidence under a new content-addressed name; it never overwrites the old.
        # Evidence binds to the collection timestamp with a bounded freshness window:
        # command execution runs after that timestamp and the decision follows within it.
        reference = self.store.finalize_artifact(
            OPERATION_ID, f"dogfood:{name}:{hashlib.sha256(data).hexdigest()}", data
        )
        return Evidence(
            f"{subject.candidate_digest}:{name}:{self.evidence_now}",
            name,
            subject,
            "controller-checker",
            "dogfood:v1",
            reference,
            self.evidence_now,
            self.evidence_now + EVIDENCE_FRESHNESS_SECONDS,
            passed,
        )

    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        if subject.head_revision != self.head:
            return None
        evidence = self._check(name, subject)
        if evidence is not None:
            self.captured.setdefault(name, self._last_payload)
        return evidence

    def _check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        if name == "changed-paths":
            allowed = set(self.task.allowed_paths)
            passed = bool(self.paths) and set(self.paths) <= allowed
            if passed:
                self.lifecycle.append("changed paths accepted: " + ", ".join(sorted(self.paths)))
            else:
                offending = sorted(set(self.paths) - allowed) or ["(no changes produced)"]
                self.lifecycle.append("changed paths rejected: " + ", ".join(offending))
            return self._evidence(
                name, subject, passed, {"changed_paths": list(self.paths), "allowed_paths": sorted(allowed)}
            )
        if name == "verification-commands":
            self.lifecycle.append(
                f"verification started: {sum(c.repeats for c in self.task.verification)} run(s) of "
                f"{len(self.task.verification)} trusted command(s)"
            )
            runs: list[dict[str, object]] = []
            for command in self.task.verification:
                for repeat in range(1, command.repeats + 1):
                    runs.append(_run_command(self.workspace, command, repeat))
                    if self.fault is not None:
                        self.fault("verification")
            entries = _porcelain(self.workspace)
            mutations = [{"status": status, "path": path} for status, path in entries if status != "??"]
            untracked = sorted(path for status, path in entries if status == "??")
            # Every non-ignored Git-visible change after verification fails: the
            # workspace must be Git-clean with respect to all non-ignored state, so
            # the recorded evidence depends on the candidate subject alone. Ignored
            # cache artifacts stay ignored by Git and are not candidate content.
            passed = (
                all(
                    isinstance(run["exit_code"], int) and run["exit_code"] == 0 and run["timed_out"] is False
                    for run in runs
                )
                and not entries
            )
            self.lifecycle.append("verification passed" if passed else "verification failed")
            return self._evidence(
                name,
                subject,
                passed,
                {
                    "runs": runs,
                    "tracked_mutations_after_verification": mutations,
                    "untracked_paths_after_verification": untracked,
                    "subject_note": "acceptance binds to the exact candidate Git subject; the workspace "
                    "must be Git-clean of all non-ignored state after verification",
                },
            )
        if name == "structural":
            payload, passed = self._structural_payload()
            self.lifecycle.append(
                "structural checks passed (deterministic subset)" if passed else "structural checks rejected"
            )
            return self._evidence(name, subject, passed, payload)
        return None

    def _structural_payload(self) -> tuple[dict[str, object], bool]:
        findings: list[str] = []
        content_parts: list[str] = []
        for path in sorted(self.task.allowed_paths):
            try:
                content_parts.append(
                    git_bytes(self.workspace, "cat-file", "blob", f"{self.head}:{path}").decode("utf-8", "replace")
                )
            except ValueError:
                findings.append(f"allowed path disappeared from candidate: {path}")
        content = "\n".join(content_parts)
        for required in self.task.structural.required_names:
            if required not in content:
                findings.append(f"required scenario removed: {required}")
        for token in self.task.structural.required_tokens:
            if token not in content:
                findings.append(f"required assertion content removed: {token}")
        added: list[str] = []
        for path in sorted(self.task.allowed_paths):
            raw = git_bytes(self.workspace, "diff", "--no-color", self.base, self.head, "--", path)
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    added.append(line[1:])
        for pattern in self.task.structural.forbidden_added_patterns:
            if any(re.search(pattern, line) for line in added):
                findings.append(f"forbidden added content matches pattern: {pattern}")
        for line in added:
            match = re.search(r"time\.sleep\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)", line)
            if match and float(match.group(1)) >= self.task.structural.forbidden_sleep_seconds:
                findings.append("arbitrary large sleep substitution added")
                break
        payload: dict[str, object] = {
            "deterministic_findings": findings,
            "deferred_to_independent_review": list(DEFERRED_TO_REVIEW),
            "scope_note": "deterministic structural subset only; semantic properties require independent review",
        }
        return payload, not findings


class DogfoodCollector:
    """Controller-owned candidate collection from the disposable workspace."""

    def __init__(
        self,
        store: SQLiteProgramStore,
        task: DogfoodTaskSpec,
        workspace: Path,
        objects: Path,
        base: str,
        lifecycle: list[str],
        fault: Callable[[str], None] | None = None,
        captured: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.store = store
        self.task = task
        self.workspace = workspace
        self.objects = objects
        self.base = base
        self.lifecycle = lifecycle
        self.fault = fault
        self.captured = captured if captured is not None else {}

    def runtime_candidate(self, request: ExecutionRequest) -> Candidate | None:
        try:
            recorded = _request(self.store, CANDIDATE_OPERATION)
        except OperationConflict as error:
            if str(error) != "unknown operation":
                raise
            recorded = None
        if recorded is None:
            _tree, _head, patch, _paths = _derive_candidate(self.workspace, self.base)
        else:
            # A durable candidate exists: restore the workspace to that exact subject so
            # verification debris can never alter re-derivation.
            head = str(recorded["head"])
            _install_candidate(self.workspace, self.objects, head)
            patch = git_bytes(self.workspace, "diff", "--binary", self.base, head)
        digest = "sha256:" + hashlib.sha256(patch).hexdigest()
        return Candidate(
            request.attempt.attempt_id,
            request.attempt.digest,
            ArtifactManifest(request.workspace.key, (Artifact(CANDIDATE_PATH, digest),)),
        )

    def collect(self, request: ExecutionRequest, candidate: Candidate, *, now: int) -> Collection:
        try:
            recorded = _request(self.store, CANDIDATE_OPERATION)
        except OperationConflict as error:
            if str(error) != "unknown operation":
                raise
            recorded = None
        if recorded is None:
            tree, head, patch, paths = _derive_candidate(self.workspace, self.base)
            self.store.intent(
                CANDIDATE_OPERATION,
                CANDIDATE_OPERATION,
                {"task": self.task.task_id, "base": self.base, "tree": tree, "head": head},
            )
            _install_candidate(self.workspace, self.objects, head)
        else:
            head = str(recorded["head"])
            if str(recorded["base"]) != self.base or str(recorded["task"]) != self.task.task_id:
                raise ValueError("durable candidate differs from the current task subject")
            parent = git_text(self.workspace, "rev-parse", f"{head}^")
            message = git_text(self.workspace, "log", "-1", "--format=%s", head)
            if parent != self.base or message != CANDIDATE_MESSAGE:
                raise ValueError("durable candidate head is not this controller's commit")
            _install_candidate(self.workspace, self.objects, head)
            patch = git_bytes(self.workspace, "diff", "--binary", self.base, head)
            tree = git_text(self.workspace, "show", "-s", "--format=%T", head)
            paths = _changed_paths(self.workspace, self.base, head)
        if len(patch) > MAX_PATCH_BYTES:
            raise ValueError("candidate patch exceeds controller limit")
        digest = self.store.finalize_artifact(request.operation.operation_id, "output", patch)
        manifest = ArtifactManifest(request.workspace.key, (Artifact(CANDIDATE_PATH, digest),))
        if manifest != candidate.artifacts:
            raise ValueError("worker candidate digest differs from trusted collection")
        self.lifecycle.append(f"candidate collected: {head} ({len(paths)} changed path(s))")
        return Collection(
            manifest,
            str(self.workspace),
            self.base,
            head,
            DogfoodChecks(
                self.store,
                self.task,
                self.workspace,
                self.base,
                head,
                paths,
                self.lifecycle,
                fault=self.fault,
                captured=self.captured,
                evidence_now=now,
            ),
        )


def export_dogfood_store(store: SQLiteProgramStore) -> dict[str, object]:
    program = store.load(PROGRAM_ID)
    mode = _request(store, "dogfood:mode")
    attempts: list[dict[str, object]] = []
    for attempt in program.attempts:
        operation = store.operation(f"runtime:{attempt.spec.attempt_id}")
        allocation = load_allocation(store, attempt.spec)
        attempts.append(
            {
                "attempt": {"attempt_id": attempt.spec.attempt_id, "status": attempt.status.value},
                "operation": {"status": operation.status, "reference": operation.accepted_reference},
                "allocation": {
                    "runtime_id": allocation.runtime_id,
                    "provider_id": allocation.provider_id,
                    "model_id": allocation.model_id,
                    "reasoning_effort": allocation.reasoning_effort,
                },
            }
        )
    result: dict[str, object] = {
        "schema": 1,
        "program": PROGRAM_ID,
        "status": program.status.value,
        "mode": mode.get("mode"),
        "task": mode.get("task"),
        "base": _request(store, "dogfood:base").get("base"),
        "attempts": attempts,
        "trust_mode": "trusted_development",
        "isolation": "not attested",
        "automatic_merge": False,
        "automatic_deploy": False,
    }
    for key, operation in (
        ("candidate", CANDIDATE_OPERATION),
        ("acceptance", f"acceptance:{ATTEMPT_ID}"),
    ):
        try:
            result[key] = _request(store, operation)
        except OperationConflict:
            pass
    try:
        result["pr"] = json.loads(store.artifact(PR_OPERATION, "export"))
    except OperationConflict:
        pass
    return result


def export_dogfood(directory: Path) -> dict[str, object]:
    if not (directory / "kernel.sqlite3").is_file():
        raise FileNotFoundError("dogfood database does not exist")
    with SQLiteProgramStore(directory / "kernel.sqlite3") as store:
        return export_dogfood_store(store)


def dogfood_status(directory: Path) -> dict[str, object]:
    """Concise operator status without running work."""
    result = export_dogfood(directory)
    candidate = cast("dict[str, object] | None", result.get("candidate"))
    acceptance = cast("dict[str, object] | None", result.get("acceptance"))
    pr = cast("dict[str, object] | None", result.get("pr"))
    return {
        "program": result["program"],
        "status": result["status"],
        "task": result["task"],
        "base": result["base"],
        "workspace_state": "candidate collected" if candidate else "prepared or untouched",
        "candidate_head": candidate.get("head") if candidate else None,
        "accepted": acceptance is not None,
        "accepted_head": acceptance.get("head") if acceptance else None,
        "pr": pr,
        "automatic_merge": False,
        "automatic_deploy": False,
    }


def run_dogfood(
    directory: Path,
    *,
    task: DogfoodTaskSpec,
    source_repository: Path,
    owner_approved: bool,
    trusted_development_acknowledged: bool,
    connection: CodexConnection,
    version: str,
    deadline: int,
    allocation: Allocation | None = None,
    allocator: ResourceAllocator | None = None,
    forge_factory: Callable[[Path], Forge] | None = None,
    supported_efforts: frozenset[tuple[str, str, str]] = frozenset(),
    fault: str | None = None,
) -> dict[str, object]:
    """Run or recover one frozen dogfood task through the bounded K1-K8 composition.

    At most one dispatch per invocation; uncertain external effects are reconciled,
    never retried blindly. Supply exactly one fixed allocation or allocator. The
    deadline bounds dispatch authorization, not the controller-owned verification
    commands, which carry their own per-command timeouts. The forge is constructed
    lazily with the control directory so its Git transport can bind the controller
    object source prepared during this run.
    """
    now = int(time.time())
    if (allocation is None) == (allocator is None):
        raise ValueError("supply exactly one allocation or allocator")
    resource_allocator = FixedAllocator(allocation) if allocation is not None else allocator
    assert resource_allocator is not None  # noqa: S101 - established by the exclusive input check.
    if owner_approved is not True or trusted_development_acknowledged is not True:
        raise ValueError("explicit owner approval and trusted-development acknowledgment required")
    if type(deadline) is not int or deadline > now + 3600 or deadline <= now:
        raise ValueError("owner deadline must be within one hour ahead")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if not source_repository.is_absolute():
        raise ValueError("source repository must be an absolute path")
    lifecycle: list[str] = []
    captured: dict[str, dict[str, object]] = {}

    def crash(boundary: str) -> None:
        if fault == boundary:
            lifecycle.append(f"interrupted after {boundary}; rerun to reconcile")
            raise DogfoodInterrupted(f"interrupted after {boundary}; rerun to reconcile")

    with SQLiteProgramStore(directory / "kernel.sqlite3") as store:
        try:
            mode_recorded = _request(store, "dogfood:mode")
        except OperationConflict as error:
            if str(error) != "unknown operation":
                raise
            mode_recorded = None
        if mode_recorded is None:
            store.intent(
                "dogfood:mode",
                "dogfood:mode",
                {
                    "mode": "live" if forge_factory is not None else "synthetic",
                    "task": task.task_id,
                    "task_digest": task.digest,
                    "deadline": deadline,
                    "version": version,
                    "repository": task.forge_repository,
                    "source": str(source_repository),
                    **(
                        {"model": allocation.model_id, "provider": allocation.provider_id}
                        if allocation is not None
                        else {"allocator": "external"}
                    ),
                    **({"supported_efforts": sorted(supported_efforts)} if supported_efforts else {}),
                },
            )
        elif str(mode_recorded["task_digest"]) != task.digest:
            raise ValueError("durable dogfood mode belongs to a different frozen task")
        try:
            program = store.load(PROGRAM_ID)
        except ProgramNotFound:
            program = store.create(dogfood_spec(task), "create")
        if program.spec != dogfood_spec(task):
            raise ValueError("durable dogfood intent differs from the frozen task specification")
        if program.status is ProgramStatus.DRAFT:
            program = store.admit(PROGRAM_ID, "activate", ActivateProgram(program.revision, "owner"))
            lifecycle.append("task admitted: owner-approved frozen dogfood task")
            crash("task-admitted")
        attempt_exists = any(item.spec.attempt_id == ATTEMPT_ID for item in program.attempts)
        base, objects, workspace = prepare_workspace(
            store, directory, source_repository, task, lifecycle, attempt_exists=attempt_exists
        )
        crash("workspace-prepared")
        status = "accepted"
        if program.state(UNIT_ID).status is not WorkUnitStatus.SATISFIED:
            lifecycle.append("allocation recovered from durable Attempt" if attempt_exists else "allocation selected")
            workspace_handle = WorkspaceHandle(
                WORKSPACE_KEY,
                WorkspaceSpec(
                    str(workspace), base, (), "dogfood:v1", "codex-workspace-write", TrustMode.TRUSTED_DEVELOPMENT
                ),
            )
            collector = DogfoodCollector(
                store, task, workspace, objects, base, lifecycle, fault=crash, captured=captured
            )

            def resolve(request: ExecutionRequest) -> CodexInputs:
                selected = request.allocation
                if selected is None:
                    raise ValueError("dogfood execution requires the durable allocation")
                prompt = (
                    f"{task.instruction}\n\nRepository guidance: {task.repository_instructions}\n\n"
                    "Work only inside the current working directory. Do not run Git commands, do not "
                    "access parent directories, credentials, or the network, and do not change Git "
                    "configuration."
                )
                return CodexInputs(
                    request.workspace.key,
                    request.context_reference,
                    request.allocation_reference,
                    request.capability_reference,
                    str(workspace),
                    prompt,
                    selected.model_id,
                    selected.provider_id,
                    reasoning_effort=selected.reasoning_effort,
                )

            def authorize(request: ExecutionRequest) -> bool:
                operation = store.operation(request.operation.operation_id)
                return (
                    int(time.time()) < deadline
                    and operation.status == "dispatched"
                    and operation.fence == request.fence
                    and operation.request_digest == request.operation.request_digest
                )

            runtime = CodexRuntime(
                connection,
                version=version,
                resolve=resolve,
                authorize=authorize,
                collect=collector.runtime_candidate,
                supported_efforts=supported_efforts,
            )

            def restore(request: ExecutionRequest, handle: str) -> None:
                runtime.restore(request, handle)

            status = advance_work_unit(
                store,
                program,
                UNIT_ID,
                allocator=resource_allocator,
                runtime=runtime,
                workspace=workspace_handle,
                collector=collector,
                policy=POLICY,
                now=int(time.time()),
                fault=crash,
                restore=restore,
                artifact_path=CANDIDATE_PATH,
            )
            if status == "identity_unavailable":
                lifecycle.append("runtime identity evidence missing or mismatched; no acceptance")
            elif status in {"unknown", "waiting", "running"}:
                lifecycle.append("runtime dispatched or reconciling; rerun this command to advance")
            program = store.load(PROGRAM_ID)
        if program.state(UNIT_ID).status is WorkUnitStatus.SATISFIED:
            accepted = _request(store, f"acceptance:{ATTEMPT_ID}")
            head = str(accepted["head"])
            lifecycle.append(f"candidate accepted: {head}")
            forge = forge_factory(directory) if forge_factory is not None else None
            receipt = deliver_reference_pr(
                store,
                repository_path=workspace,
                head=head,
                acceptance_reference=str(accepted["manifest"]),
                fault=fault,
                forge=forge,
                repository=Reference(task.forge_repository),
                base_branch=task.base_branch,
                operation_id=PR_OPERATION,
                schema="dogfood-pr-v1",
                title=task.pr_title,
                body=f"{task.pr_body}\n\nTask: {task.task_id}. Accepted result: {accepted['manifest']}.",
            )
            if receipt.get("interrupted"):
                raise DogfoodInterrupted(f"interrupted after {receipt['interrupted']}; rerun to reconcile")
            if receipt.get("status") == "accepted":
                lifecycle.append(f"PR created: {receipt.get('reference')}")
                try:
                    store.artifact(PR_OPERATION, "export")
                except OperationConflict:
                    store.finalize_artifact(PR_OPERATION, "export", manifest_bytes(receipt))
            elif receipt.get("status") == "unknown":
                lifecycle.append("PR delivery unknown; rerun this command to reconcile")
            else:
                lifecycle.append(f"PR delivery rejected: {receipt.get('status')}")
            lifecycle.append("NO MERGE / NO DEPLOY")
            result = export_dogfood_store(store)
            result.update(
                condition="accepted",
                head=head,
                pr=receipt,
                verification=captured,
                lifecycle=lifecycle,
                isolation="not attested; trusted development only",
            )
            return result
        result = export_dogfood_store(store)
        result.update(
            condition=status,
            verification=captured,
            lifecycle=lifecycle,
            isolation="not attested; trusted development only",
        )
        return result


LIVE_ENV = {
    "router": "CREATIDY_DOGFOOD_ROUTER_URL",
    "router_key": "CREATIDY_DOGFOOD_ROUTER_KEY",
    "binding": "CREATIDY_DOGFOOD_RUNTIME_BINDING",
    "codex_bin": "CREATIDY_DOGFOOD_CODEX_BIN",
    "codex_version": "CREATIDY_DOGFOOD_CODEX_VERSION",
    "forge_api": "CREATIDY_DOGFOOD_FORGE_API",
    "forge_remote": "CREATIDY_DOGFOOD_FORGE_REMOTE",
    "forge_token": "CREATIDY_DOGFOOD_FORGE_TOKEN",
    "forge_askpass": "CREATIDY_DOGFOOD_FORGE_ASKPASS",
}
CODEX_METHODS = frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"})
# The closed set of operational environment entries the Codex app-server may receive.
# HOME carries Codex's own authenticated configuration directory (~/.codex), which is
# its existing credential mechanism — no secret is carried in the environment itself.
# PATH, LANG/LC_ALL and TMPDIR are the operational tool-resolution, locale and temp
# entries. Everything else in the controller environment, including every dogfood
# controller credential and unrelated owner secret, is dropped rather than forwarded.
CODEX_ENVIRONMENT_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")


def codex_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Build the closed Codex subprocess environment; never an inheritance pass-through."""
    environment = {"PATH": environ.get("PATH") or os.defpath}
    for name in CODEX_ENVIRONMENT_KEYS:
        if name != "PATH" and environ.get(name):
            environment[name] = environ[name]
    return environment


@dataclass(frozen=True, slots=True)
class DogfoodLiveComponents:
    """Owner-configured live bindings; secrets stay in the environment, never evidence."""

    allocator: ResourceAllocator
    connection_factory: Callable[[], CodexConnection]
    forge_factory: Callable[[Path], Forge]
    # Trusted Runtime effort-support evidence derived from the same controller-owned
    # runtime binding that constrains Router allocation; never Router-derived.
    supported_efforts: frozenset[tuple[str, str, str]]


def _live_binding(raw: str) -> Allocation:
    parts = raw.split("/")
    if len(parts) not in (2, 3) or any(not part.strip() for part in parts):
        raise ValueError("CREATIDY_DOGFOOD_RUNTIME_BINDING must be provider/model[/effort]")
    provider, model, *effort = parts
    return Allocation(
        "codex",
        provider,
        model,
        frozenset({"reference"}),
        128,
        "owner-configured live dogfood runtime binding",
        reasoning_effort=effort[0] if effort else None,
    )


def compose_dogfood_live(environ: Mapping[str, str], task: DogfoodTaskSpec) -> DogfoodLiveComponents:
    """Build the live Router/Codex/Forge composition from documented environment only.

    Credentials are read from the environment when the adapter strictly requires
    them; they are never task data, never Program evidence, never logged, and never
    embedded in URLs or command-line arguments. The Codex app-server subprocesses
    receive a closed controller-built operational environment (HOME, PATH, locale,
    temp) instead of inheriting the controller environment, so dogfood controller
    credentials and unrelated owner secrets never reach the coding runtime. Codex
    authentication uses its own HOME-based authenticated configuration, not an
    environment-carried secret.
    """
    required = [name for key, name in LIVE_ENV.items() if key not in ("router_key", "forge_askpass")]
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise ValueError("dogfood live configuration is incomplete; missing: " + ", ".join(sorted(missing)))
    binding = _live_binding(environ[LIVE_ENV["binding"]])
    allocator = ScarcityRouterAllocator(
        environ[LIVE_ENV["router"]],
        (binding,),
        api_key=environ.get(LIVE_ENV["router_key"]) or None,
    )
    codex_bin = environ[LIVE_ENV["codex_bin"]]
    codex_version = environ[LIVE_ENV["codex_version"]]
    repository = Reference(task.forge_repository)
    forge_api = environ[LIVE_ENV["forge_api"]]
    forge_remote = environ[LIVE_ENV["forge_remote"]]
    forge_token = environ[LIVE_ENV["forge_token"]]
    askpass_raw = environ.get(LIVE_ENV["forge_askpass"])

    def connection_factory() -> CodexConnection:
        if not Path(codex_bin).is_absolute():
            raise ValueError("CREATIDY_DOGFOOD_CODEX_BIN must be an absolute executable path")
        return CodexStdio(
            (codex_bin, "app-server"),
            codex_version,
            schema_methods=CODEX_METHODS,
            schema_version=codex_version,
            environment=codex_environment(os.environ),
        )

    def forge_factory(directory: Path) -> Forge:
        askpass = Path(askpass_raw) if askpass_raw else None
        transport = ConditionalGitTransport(repository, forge_remote, directory / "objects.git", askpass=askpass)
        return ForgejoForge(
            HTTPSForgejoTransport(forge_api, repository, lambda: forge_token),
            transport,
            lambda proposed: proposed.repository == repository,
        )

    return DogfoodLiveComponents(
        allocator,
        connection_factory,
        forge_factory,
        # One controller-side authority: the parsed runtime binding both constrains
        # Router allocation and proves Runtime effort support for its exact tuple.
        # A binding without an explicit effort manufactures no evidence; null stays
        # null and is never coerced to the literal effort string "none".
        (
            frozenset({(binding.provider_id, binding.model_id, binding.reasoning_effort)})
            if binding.reasoning_effort is not None
            else frozenset()
        ),
    )
