# SPDX-License-Identifier: Apache-2.0
"""Bounded task execution for frozen owner-approved real tasks.

This module composes already-proved K1-K8 mechanisms into a single supported path:
Program/Attempt durability, ResourceAllocator, CodexRuntime, controller-owned Git
candidates, bounded controller-owned verification, and the existing Forgejo AGit
delivery. It is not a workflow engine, an issue parser, or a generic task runner.

Trust boundary: trusted development only. The coding Runtime edits a disposable
controller-owned workspace; isolation beyond Codex workspace-write is not attested.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from creatidy_kernel.adapters.codex_discovery import (
    CODEX_VERSION_PATTERN,
    CodexDiscoveryFailure,
    resolve_codex,
)
from creatidy_kernel.adapters.codex_runtime import CodexConnection, CodexInputs, CodexRuntime
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
from creatidy_kernel.adapters.forge_refs import AGitPush, ForgeBinding, https_origin, oid, repository_path, valid_branch
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport
from creatidy_kernel.adapters.reference import reference_git, reference_git_bytes
from creatidy_kernel.adapters.reference_forge import deliver_reference_pr
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator, ScarcityRouterUnavailable
from creatidy_kernel.adapters.source_cache import SourceAcquisitionError, acquire_source, default_cache_root
from creatidy_kernel.adapters.sqlite_store import (
    OperationConflict,
    ProgramNotFound,
    SQLiteProgramStore,
    validate_local_storage,
)
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptStatus,
    AuthorityEnvelope,
    BudgetPolicy,
    FinishAttempt,
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
    ExecutionConflict,
    ExecutionRequest,
    OperationKey,
    RuntimeIdentity,
    TrustMode,
    UnsupportedExecution,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.forge import EffectStatus, Presence, Reference, UnsupportedForge
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.core.verification import Evidence, EvidenceSubject, VerificationPolicy
from creatidy_kernel.ports.allocation import (
    decode_runtime_receipt,
    encode_allocation,
    identity_matches,
    load_allocation,
)
from creatidy_kernel.ports.application import Collection, advance_work_unit, manifest_bytes
from creatidy_kernel.ports.forge import Forge
from creatidy_kernel.ports.program_store import OperationRecord
from creatidy_kernel.ports.resources import ResourceAllocator

PROGRAM_ID = "task_execution"
UNIT_ID = "change"
ATTEMPT_ID = f"{PROGRAM_ID}:{UNIT_ID}"
OPERATION_ID = f"runtime:{ATTEMPT_ID}"
CANDIDATE_OPERATION = f"candidate:{ATTEMPT_ID}"
PR_OPERATION = "task_execution:pr"
WORKSPACE_KEY = "task_execution:workspace"
CANDIDATE_PATH = "candidate.patch"
POLICY = VerificationPolicy(
    PolicyReference("task_execution", "1", "task-checks:v1"),
    ("changed-paths", "verification-commands", "structural"),
    False,
)
CANDIDATE_MESSAGE = "creatidy-kernel task candidate"
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


class TaskInterrupted(RuntimeError):
    """A requested crash boundary was reached after durable state was written."""


def _nonempty(value: object, field: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"task {field} must be a nonempty string")


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


class TaskStructure(StrEnum):
    """Closed controller-owned task validators, not programmable expressions."""

    CANCELLATION_143 = "scarcity-router-143"
    FEEDER_166 = "scarcity-router-166"


@dataclass(frozen=True, slots=True)
class StructuralPolicy:
    """Deterministic anti-weakening constraints for one frozen task; not a DSL."""

    required_names: tuple[str, ...]
    required_tokens: tuple[str, ...]
    forbidden_added_patterns: tuple[str, ...]
    forbidden_sleep_seconds: float
    task_structure: TaskStructure = TaskStructure.CANCELLATION_143

    def __post_init__(self) -> None:
        if type(self.task_structure) is not TaskStructure:
            raise ValueError("structural validator must be a supported typed task structure")
        for name, values in (("required_names", self.required_names), ("required_tokens", self.required_tokens)):
            if (
                type(values) is not tuple
                or not values
                or any(type(item) is not str or not item.strip() for item in values)
            ):
                raise ValueError(f"structural {name} must be nonempty strings")
        if type(self.forbidden_added_patterns) is not tuple or not self.forbidden_added_patterns:
            raise ValueError("structural policy requires forbidden added-line patterns")
        for pattern in self.forbidden_added_patterns:
            if type(pattern) is not str or not pattern:
                raise ValueError("structural patterns must be nonempty strings")
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid structural pattern: {pattern}") from error
        if (
            type(self.forbidden_sleep_seconds) not in (int, float)
            or not math.isfinite(self.forbidden_sleep_seconds)
            or self.forbidden_sleep_seconds <= 0
        ):
            raise ValueError("structural sleep threshold must be positive")

    def validate_baseline(self, content: str) -> None:
        tree = ast.parse(content)
        if self.task_structure is TaskStructure.CANCELLATION_143:
            for name in self.required_names:
                scenarios = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name]
                if len(scenarios) != 1:
                    raise ValueError("frozen scenario is absent or ambiguous")
                scenario = ast.get_source_segment(content, scenarios[0]) or ""
                if ".recv(" not in scenario or re.search(r"\bif\s+not\s+\w+\s*:\s*break\b", scenario) is None:
                    raise ValueError("frozen cancellation race baseline is absent or already changed")
        else:
            _feeder_structure(tree, baseline=True)
            classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SafeTermination"]
            if len(classes) != 1:
                raise ValueError("frozen SafeTermination class is absent or ambiguous")
            for name in self.required_names:
                if sum(isinstance(node, ast.FunctionDef) and node.name == name for node in classes[0].body) != 1:
                    raise ValueError("frozen SafeTermination scenario is absent or ambiguous")

    @property
    def deferred_to_review(self) -> tuple[str, ...]:
        if self.task_structure is TaskStructure.FEEDER_166:
            return (
                *DEFERRED_TO_REVIEW,
                "forced interleaving proves unsafe baseline and corrected descriptor ownership",
            )
        return DEFERRED_TO_REVIEW


def _feeder_structure(tree: ast.Module, *, baseline: bool) -> None:
    """Fixed #166 fixture shape; never execute or interpret repository code."""
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_FakeAppServer"]
    if len(classes) != 1:
        raise ValueError("real feeder fixture is absent or ambiguous")
    methods = {node.name: node for node in classes[0].body if isinstance(node, ast.FunctionDef)}
    if len(methods) != sum(isinstance(node, ast.FunctionDef) for node in classes[0].body):
        raise ValueError("feeder fixture methods are ambiguous")
    for name, required in (
        ("__init__", {"os.pipe", "threading.Thread", "self._feeder.start"}),
        ("_feed", {"os.set_blocking", "os.write"}),
    ):
        method = methods.get(name)
        method_calls: set[str] = (
            {ast.unparse(node.func) for node in ast.walk(method) if isinstance(node, ast.Call)} if method else set()
        )
        if not required <= method_calls:
            raise ValueError("real pipe/non-blocking feeder structure is missing")
    if not any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) == "threading.Thread"
        and any(keyword.arg == "target" and ast.unparse(keyword.value) == "self._feed" for keyword in node.keywords)
        for node in ast.walk(methods["__init__"])
    ):
        raise ValueError("real feeder thread target must be preserved")
    feed_calls = [node for node in ast.walk(methods["_feed"]) if isinstance(node, ast.Call)]
    if not any(
        ast.unparse(node.func) == "os.set_blocking"
        and len(node.args) == 2
        and ast.unparse(node.args[0]) == "self._write_fd"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value is False
        for node in feed_calls
    ):
        raise ValueError("feeder write descriptor must remain non-blocking")
    if baseline:
        closer = methods.get("_close_write")
        calls = [node for node in ast.walk(closer) if isinstance(node, ast.Call)] if closer else []
        names = {ast.unparse(node.func) for node in calls}
        timed_join = any(
            ast.unparse(node.func) == "self._feeder.join"
            and any(keyword.arg == "timeout" and ast.unparse(keyword.value) == "5.0" for keyword in node.keywords)
            for node in calls
        )
        if (
            not timed_join
            or not {"self._stop.set", "os.close"} <= names
            or "self._feeder.is_alive" in names
            or not any(ast.unparse(node.func) == "self._stop.is_set" for node in feed_calls)
        ):
            raise ValueError("frozen timed-join feeder race baseline is absent or already changed")


def _feeder_candidate_findings(base: str, content: str) -> list[str]:
    try:
        original, candidate = ast.parse(base), ast.parse(content)
        _feeder_structure(candidate, baseline=False)
    except (SyntaxError, ValueError) as error:
        return [str(error)]
    # Preserve existing test identities and assertion expressions, including the
    # shared stderr assertion. New regressions remain subject to semantic review.
    findings: list[str] = []
    original_tests: set[tuple[str, str]] = set()
    candidate_tests: set[tuple[str, str]] = set()
    candidate_methods = {
        (cls.name, method.name): method
        for cls in candidate.body
        if isinstance(cls, ast.ClassDef)
        for method in cls.body
        if isinstance(method, ast.FunctionDef)
    }
    for cls in original.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef) or not (
                method.name.startswith("test_") or method.name == "_assert_no_output"
            ):
                continue
            key = (cls.name, method.name)
            if method.name.startswith("test_"):
                original_tests.add(key)
            replacement = candidate_methods.get(key)
            assertions = [
                ast.dump(node)
                for node in ast.walk(method)
                if isinstance(node, ast.Assert)
                or (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and (node.func.attr.startswith("assert") or node.func.attr == "_assert_no_output")
                )
            ]
            remaining = [ast.dump(node) for node in ast.walk(replacement)] if replacement else []
            for assertion in assertions:
                if assertion in remaining:
                    remaining.remove(assertion)
                else:
                    findings.append(f"existing assertion removed or changed: {cls.name}.{method.name}")
            if replacement is None:
                findings.append(f"existing scenario removed: {cls.name}.{method.name}")
    candidate_tests.update(key for key in candidate_methods if key[1].startswith("test_"))
    regressions = candidate_tests - original_tests
    if not regressions or not any(
        isinstance(node, ast.Assert)
        or (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert"))
        for key in regressions
        for node in ast.walk(candidate_methods[key])
    ):
        findings.append("forced-interleaving regression coverage must be added")
    sleeps = [
        ast.dump(node)
        for node in ast.walk(original)
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {"time.sleep", "sleep"}
    ]
    for node in ast.walk(candidate):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {"time.sleep", "sleep"}:
            if ast.dump(node) in sleeps:
                sleeps.remove(ast.dump(node))
            else:
                findings.append("new or changed sleep cannot establish feeder correctness")
        if not isinstance(node, ast.Call) or ast.unparse(node.func) != "self._feeder.join":
            continue
        timeouts = [keyword.value for keyword in node.keywords if keyword.arg == "timeout"]
        timeouts.extend(node.args[:1])
        if any(
            isinstance(value, ast.Constant) and isinstance(value.value, (int, float)) and value.value > 5
            for value in timeouts
        ):
            findings.append("feeder join timeout increase is not an ownership fix")
    return findings


@dataclass(frozen=True, slots=True)
class TaskSpec:
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
    pr_title: str = "Creatidy Kernel task candidate"
    pr_body: str = "Controller-verified task candidate. No merge or deployment authorized."
    forge_issue: int = 143

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "forge_repository",
            "repository_url",
            "base_branch",
            "instruction",
            "repository_instructions",
        ):
            _nonempty(getattr(self, name), name)
        if not self.forge_repository.startswith("forgejo:"):
            raise ValueError("task forge repository must be forgejo-qualified")
        path = repository_path(Reference(self.forge_repository))
        https_origin(self.repository_url)
        if urlsplit(self.repository_url).path != f"/{path}":
            raise ValueError("task repository URL must bind the exact repository")
        valid_branch(self.base_branch)
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
        if type(self.forge_issue) is not int or not 1 <= self.forge_issue < 10**18:
            raise ValueError("frozen Forge issue must be a positive bounded integer")

    @property
    def objective(self) -> str:
        return f"task {self.task_id}: {self.repository_url} ({self.base_branch})"

    def payload(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "forge_repository": self.forge_repository,
            "forge_issue": self.forge_issue,
            "repository_url": self.repository_url,
            "base_branch": self.base_branch,
            "expected_base_sha": self.expected_base_sha,
            "instruction": self.instruction,
            "repository_instructions": self.repository_instructions,
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
                # Preserve the shipped #143 payload/digest for durable recovery.
                **(
                    {"task_structure": self.structural.task_structure.value}
                    if self.structural.task_structure is not TaskStructure.CANCELLATION_143
                    else {}
                ),
            },
            "max_attempts": self.max_attempts,
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
        }

    @property
    def digest(self) -> str:
        payload = self.payload()
        # The optional pin constrains base resolution, not logical task identity.
        # Exact base authority is durably frozen and checked independently.
        payload.pop("expected_base_sha")
        return hashlib.sha256(manifest_bytes(payload)).hexdigest()


def scarcity_router_143_task(expected_base_sha: str | None = None) -> TaskSpec:
    """The frozen task contract for BioMedical-IT/scarcity-router#143.

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
    return TaskSpec(
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
        pr_title="Fix cancellation race in scenario-11 CancellationTests (#143)",
    )


def scarcity_router_166_task(expected_base_sha: str | None = None) -> TaskSpec:
    """Bounded support only; registration does not authorize #166 execution.

    Selection observed 5792d2dfa1d76fbd973baf2ef82b5fcf236342e9. Like #143,
    admission freezes current develop unless the owner supplies an explicit pin.
    """
    module = ("uv", "run", "python", "-m", "unittest", "tests.test_openai_codex_acquisition", "-v")
    return TaskSpec(
        task_id="scarcity-router-166",
        forge_repository="forgejo:BioMedical-IT/scarcity-router",
        forge_issue=166,
        repository_url="https://forgejo.creatidy.com/BioMedical-IT/scarcity-router",
        base_branch="develop",
        instruction=(
            "Fix #166 only in tests/test_openai_codex_acquisition.py. _FakeAppServer._close_write() "
            "currently joins _feed with timeout=5.0 then closes the real pipe write descriptor even "
            "if the feeder is still alive. A feeder paused after its stop check can resume os.write "
            "after close (including descriptor reuse). Establish safe feeder/write-descriptor "
            "ownership or shutdown ordering without leaked thread tracebacks. Preserve the real "
            "pipe, non-blocking writer, concurrency, all existing tests and assertion strength. "
            "Add deterministic forced-interleaving regression coverage that proves the unsafe "
            "baseline and the corrected ownership invariant, not merely module-isolated loops. "
            "Do not increase timeouts arbitrarily, use sleep-based correctness, skip/xfail, swallow "
            "assertions/exceptions, reduce coverage or the eight-repeat contract, or replace real "
            "concurrency with an inert mock. Do not change production code."
        ),
        repository_instructions=(
            "The repository uses uv and unittest. The targeted class is "
            "tests.test_openai_codex_acquisition.SafeTermination. Fixtures are synthetic and "
            "offline with real local pipes and threads; no network or credentials are permitted."
        ),
        allowed_paths=frozenset({"tests/test_openai_codex_acquisition.py"}),
        verification=(
            VerificationCommand((*module[:-2], "tests.test_openai_codex_acquisition.SafeTermination", "-v"), 900),
            VerificationCommand(module, 900),
            VerificationCommand(module, 900, repeats=8),
            VerificationCommand(("make", "check"), 3600),
        ),
        structural=StructuralPolicy(
            required_names=(
                "test_success_path_terminates_without_kill",
                "test_failure_paths_terminate_without_kill",
                "test_timeout_path_terminates_and_never_leaks",
                "test_stubborn_child_is_killed_after_bounded_wait",
                "test_reader_startup_failure_terminates_child",
                "test_reader_startup_failure_kills_stubborn_child",
                "test_shutdown_never_raises_through_collect",
            ),
            required_tokens=(
                "os.pipe()",
                "os.set_blocking(self._write_fd, False)",
                "os.write(",
                "threading.Thread(",
                "self._feeder.start()",
                'self.assertEqual(fake.events, ["terminate"])',
                'self.assertEqual(fake.events, ["terminate", "kill"])',
                "self.assertTrue(fake.stdin.closed)",
                'self.assertEqual(self.stderr.getvalue(), "")',
            ),
            forbidden_added_patterns=(
                r"@\s*(?:unittest\.)?skip(?:If|Unless)?\b",
                r"@pytest\.mark\.(?:skip|xfail)",
                r"\bxfail\b",
                r"except\s+.*\b(?:AssertionError|Exception|BaseException|OSError)\b",
                r"except\s*:",
                r"\b(?:skipTest|expectedFailure)\b",
            ),
            forbidden_sleep_seconds=5.0,
            task_structure=TaskStructure.FEEDER_166,
        ),
        expected_base_sha=expected_base_sha,
        max_attempts=1,
        pr_title="Fix fake-process feeder descriptor ownership (#166)",
    )


TASKS = {"143": scarcity_router_143_task, "166": scarcity_router_166_task}


def task_program_spec(task: TaskSpec) -> ProgramSpec:
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
        initial_inputs=(InputBinding("task", "task_execution:task:" + task.digest),),
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


def _recover_task_pin(task: TaskSpec, mode: Mapping[str, object]) -> TaskSpec:
    recorded = mode.get("expected_base_sha")
    if recorded is None:
        return task
    pin = oid(str(recorded))
    if task.expected_base_sha is not None and task.expected_base_sha != pin:
        raise ValueError("requested base differs from the original approved pin")
    return replace(task, expected_base_sha=pin)


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


def _resolve_base(source: Path, task: TaskSpec) -> tuple[str, str]:
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
    task: TaskSpec,
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
        recorded = _request(store, "task_execution:base")
    except OperationConflict as error:
        if str(error) != "unknown operation":
            raise
        recorded = None
    if recorded is None:
        base, ref = _resolve_base(source, task)
        store.intent("task_execution:base", "task_execution:base", {"base": base, "ref": ref, "task": task.task_id})
        lifecycle.append(f"exact base established: {base} ({ref})")
    else:
        base = str(recorded["base"])
        oid(base)
        if str(recorded["task"]) != task.task_id:
            raise ValueError("durable base belongs to a different task")
        if task.expected_base_sha is not None and task.expected_base_sha != base:
            raise ValueError("durable frozen base differs from the pinned expected base")
        lifecycle.append(f"exact base recovered: {base}")
    objects = directory / "objects.git"
    workspace = directory / "workspace"
    if workspace.is_symlink() or objects.is_symlink():
        raise ValueError("task workspace/object paths must not be symlinks")
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
        raise ValueError("task workspace is unexpectedly dirty before any dispatch")
    return base, objects, workspace


def _derive_candidate(workspace: Path, base: str) -> tuple[str, str, bytes, tuple[str, ...]]:
    """Controller-owned candidate subject from the current workspace tree."""
    index = workspace / ".git" / "task-index"
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


def _run_command(
    workspace: Path,
    command: VerificationCommand,
    repeat: int,
    supplied: Mapping[str, str],
) -> dict[str, object]:
    environment = {"HOME": str(workspace), "PATH": os.defpath, "LANG": "C.UTF-8"}
    for key in COMMAND_ENV_KEYS:
        if supplied.get(key):
            environment[key] = supplied[key]
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


class TaskChecks:
    """Deterministic controller-owned checks; semantic residue is explicitly deferred."""

    def __init__(
        self,
        store: SQLiteProgramStore,
        task: TaskSpec,
        workspace: Path,
        base: str,
        head: str,
        paths: tuple[str, ...],
        lifecycle: list[str],
        fault: Callable[[str], None] | None = None,
        captured: dict[str, dict[str, object]] | None = None,
        command_environment: Mapping[str, str] | None = None,
        authorized: Callable[[], bool] = lambda: True,
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
        self.command_environment = dict(command_environment or {})
        self.authorized = authorized
        self._last_payload: dict[str, object] = {}

    def _evidence(self, name: str, subject: EvidenceSubject, passed: bool, payload: dict[str, object]) -> Evidence:
        self._last_payload = payload
        data = manifest_bytes(payload)
        # Re-executed verification (deterministic local re-run after a crash) produces
        # fresh evidence under a new content-addressed name; it never overwrites the old.
        # Timestamp the actual completed check, never its pre-command collection time.
        observed_at = int(time.time())
        reference = self.store.finalize_artifact(
            OPERATION_ID, f"task_execution:{name}:{hashlib.sha256(data).hexdigest()}", data
        )
        return Evidence(
            f"{subject.candidate_digest}:{name}:{observed_at}",
            name,
            subject,
            "controller-checker",
            "task_execution:v1",
            reference,
            observed_at,
            observed_at + EVIDENCE_FRESHNESS_SECONDS,
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
                    if not self.authorized():
                        return self._evidence(name, subject, False, {"runs": runs, "reason": "authority unavailable"})
                    runs.append(_run_command(self.workspace, command, repeat, self.command_environment))
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
        if self.task.structural.task_structure is TaskStructure.FEEDER_166:
            base_content = "\n".join(
                git_bytes(self.workspace, "cat-file", "blob", f"{self.base}:{path}").decode("utf-8")
                for path in sorted(self.task.allowed_paths)
            )
            findings.extend(_feeder_candidate_findings(base_content, content))
        payload: dict[str, object] = {
            "deterministic_findings": findings,
            "deferred_to_independent_review": list(self.task.structural.deferred_to_review),
            "scope_note": "deterministic structural subset only; semantic properties require independent review",
        }
        return payload, not findings


class TaskCollector:
    """Controller-owned candidate collection from the disposable workspace."""

    def __init__(
        self,
        store: SQLiteProgramStore,
        task: TaskSpec,
        workspace: Path,
        objects: Path,
        base: str,
        lifecycle: list[str],
        fault: Callable[[str], None] | None = None,
        captured: dict[str, dict[str, object]] | None = None,
        command_environment: Mapping[str, str] | None = None,
        authorized: Callable[[], bool] = lambda: True,
    ) -> None:
        self.store = store
        self.task = task
        self.workspace = workspace
        self.objects = objects
        self.base = base
        self.lifecycle = lifecycle
        self.fault = fault
        self.captured = captured if captured is not None else {}
        self.command_environment = dict(command_environment or {})
        self.authorized = authorized

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
            TaskChecks(
                self.store,
                self.task,
                self.workspace,
                self.base,
                head,
                paths,
                self.lifecycle,
                fault=self.fault,
                captured=self.captured,
                command_environment=self.command_environment,
                authorized=self.authorized,
            ),
        )


def export_task_store(store: SQLiteProgramStore) -> dict[str, object]:
    program = store.load(PROGRAM_ID)
    mode = _request(store, "task_execution:mode")
    try:
        base = _request(store, "task_execution:base").get("base")
    except OperationConflict as error:
        if str(error) != "unknown operation":
            raise
        base = None
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
        "deadline": mode.get("deadline"),
        "base": base,
        "attempts": attempts,
        "trust_mode": "trusted_development",
        "isolation": "not attested",
        "automatic_merge": False,
        "automatic_deploy": False,
    }
    try:
        store.operation(OPERATION_ID)
    except OperationConflict as error:
        if str(error) != "unknown operation":
            raise
        result["runtime_operation_absent"] = True
    else:
        result["runtime_operation_absent"] = False
    for key, operation in (
        ("candidate", CANDIDATE_OPERATION),
        ("acceptance", f"acceptance:{ATTEMPT_ID}"),
        ("cancellation", f"cancel:{ATTEMPT_ID}"),
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


def export_task(directory: Path) -> dict[str, object]:
    if not (directory / "kernel.sqlite3").is_file():
        raise FileNotFoundError("task database does not exist")
    with SQLiteProgramStore(directory / "kernel.sqlite3") as store:
        return export_task_store(store)


def task_status(directory: Path) -> dict[str, object]:
    """Concise operator status without running work."""
    result = export_task(directory)
    candidate = cast("dict[str, object] | None", result.get("candidate"))
    acceptance = cast("dict[str, object] | None", result.get("acceptance"))
    pr = cast("dict[str, object] | None", result.get("pr"))
    attempts = cast("list[dict[str, object]]", result["attempts"])
    operations = [cast("dict[str, object]", item["operation"]) for item in attempts]
    expired = type(result["deadline"]) is int and int(time.time()) >= result["deadline"]
    terminal = bool(operations) and all(item["status"] == "terminal" for item in operations)
    condition = (
        "accepted"
        if acceptance is not None
        else "cancelled_no_dispatch"
        if result.get("cancellation") is not None and not operations and result["runtime_operation_absent"] is True
        else "cancelled"
        if result.get("cancellation") is not None and terminal
        else "cancel_uncertain"
        if result.get("cancellation") is not None
        else "expired"
        if expired
        else "terminal_no_candidate"
        if terminal and candidate is None
        else "terminal"
        if terminal
        else "reconciling"
        if any(item["status"] in {"unknown", "dispatched"} for item in operations)
        else "active_receipt"
        if operations
        else "prepared"
    )
    return {
        "program": result["program"],
        "status": result["status"],
        "task": result["task"],
        "base": result["base"],
        "deadline": result["deadline"],
        "expired": expired,
        "condition": condition,
        "next_action": (
            "Cancelled before dispatch; new execution requires separate approval."
            if condition == "cancelled_no_dispatch"
            else "Inspect cancellation evidence; retain workspace until owned descendants are proved settled."
            if result.get("cancellation") is not None
            else "Inspect terminal evidence; new execution requires separate approval."
            if terminal or expired
            else "Observe/reconcile the original receipt and deadline; never blindly resubmit."
        ),
        "runtime_state": "status is recorded evidence, not a fresh native observation",
        "cancellation": result.get("cancellation"),
        "attempts": result["attempts"],
        "workspace_state": "candidate collected" if candidate else "prepared or untouched",
        "candidate_head": candidate.get("head") if candidate else None,
        "accepted": acceptance is not None,
        "accepted_head": acceptance.get("head") if acceptance else None,
        "pr": pr,
        "automatic_merge": False,
        "automatic_deploy": False,
    }


def run_task(
    directory: Path,
    *,
    task: TaskSpec,
    source_repository: Path,
    owner_approved: bool,
    trusted_development_acknowledged: bool,
    connection: CodexConnection,
    version: str,
    deadline: int | None,
    allocation: Allocation | None = None,
    allocator: ResourceAllocator | None = None,
    forge_factory: Callable[[Path], Forge] | None = None,
    supported_efforts: frozenset[tuple[str, str, str]] = frozenset(),
    command_environment: Mapping[str, str] | None = None,
    fault: str | None = None,
    cancel_requested: bool = False,
    cancellation_requested: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Run or recover one frozen task through the bounded K1-K8 composition.

    At most one dispatch per invocation; uncertain external effects are reconciled,
    never retried blindly. Supply exactly one fixed allocation or allocator. The
    original deadline bounds dispatch and acceptance; expired work remains observable.
    Verification commands retain finite timeouts but cannot extend authority. The forge is constructed
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
    if deadline is not None and (type(deadline) is not int or deadline > now + 3600):
        raise ValueError("owner deadline must be within one hour ahead")
    _suitable_path(directory)
    _suitable_path(source_repository)
    if (
        directory == source_repository
        or directory.is_relative_to(source_repository)
        or source_repository.is_relative_to(directory)
    ):
        raise ValueError("control directory and source repository must be separate trees")
    existing_database = (directory / "kernel.sqlite3").exists()
    if directory.exists() and not existing_database and any(directory.iterdir()):
        raise ValueError("unjournaled task control directory must be empty")
    directory.mkdir(parents=True, exist_ok=True)
    lifecycle: list[str] = []
    captured: dict[str, dict[str, object]] = {}

    def crash(boundary: str) -> None:
        if fault == boundary:
            lifecycle.append(f"interrupted after {boundary}; rerun to reconcile")
            raise TaskInterrupted(f"interrupted after {boundary}; rerun to reconcile")

    with SQLiteProgramStore(directory / "kernel.sqlite3") as store, ExitStack() as cleanup:

        def record_cancellation(attempt_id: str, operation_id: str) -> OperationRecord:
            key = f"cancel:{attempt_id}"
            try:
                recorded = store.operation(key)
            except OperationConflict as error:
                if str(error) != "unknown operation":
                    raise
                return store.intent(key, key, {"operation": operation_id, "descendants": "unproven"})
            if _request(store, key).get("operation") != operation_id:
                raise ValueError("cancellation intent differs from original execution")
            return recorded

        def cancellation_requested_now() -> bool:
            requested = cancel_requested or (cancellation_requested is not None and cancellation_requested())
            if requested:
                record_cancellation(ATTEMPT_ID, OPERATION_ID)
            return requested

        def finish_cancelled_attempt(attempt_id: str) -> None:
            current = store.load(PROGRAM_ID)
            if current.attempt(attempt_id).status is AttemptStatus.EXECUTING:
                store.admit(PROGRAM_ID, f"finish:{attempt_id}", FinishAttempt(current.revision, "worker", attempt_id))

        def cancelled_without_attempt() -> dict[str, object]:
            result = export_task_store(store)
            if result["runtime_operation_absent"] is not True:
                raise ValueError("orphaned runtime Operation requires explicit reconciliation")
            result.update(condition="cancelled_no_dispatch", lifecycle=lifecycle)
            return result

        try:
            mode_recorded = _request(store, "task_execution:mode")
        except OperationConflict as error:
            if str(error) != "unknown operation":
                raise
            mode_recorded = None
        if mode_recorded is None:
            if existing_database:
                raise ValueError("existing database is not this neutral task")
            deadline = deadline if deadline is not None else now + 3300
            if deadline <= now:
                raise ValueError("owner deadline must be within one hour ahead")
            store.intent(
                "task_execution:mode",
                "task_execution:mode",
                {
                    "mode": "live" if forge_factory is not None else "synthetic",
                    "task": task.task_id,
                    "task_digest": task.digest,
                    "expected_base_sha": task.expected_base_sha,
                    "deadline": deadline,
                    "version": version,
                    "repository": task.forge_repository,
                    "source": str(source_repository),
                    **(
                        {
                            "model": allocation.model_id,
                            "provider": allocation.provider_id,
                            "fixed_allocation": encode_allocation(allocation).decode(),
                        }
                        if allocation is not None
                        else {"allocator": "external"}
                    ),
                    **({"supported_efforts": sorted(supported_efforts)} if supported_efforts else {}),
                },
            )
        elif str(mode_recorded["task_digest"]) != task.digest:
            raise ValueError("durable task mode belongs to a different frozen task")
        else:
            task = _recover_task_pin(task, mode_recorded)
            if allocation is not None:
                fixed_binding = mode_recorded.get("fixed_allocation")
                if fixed_binding is None:
                    historical = store.load(PROGRAM_ID)
                    if not any(item.spec.attempt_id == ATTEMPT_ID for item in historical.attempts):
                        raise ValueError("original fixed allocation unavailable in legacy envelope")
                    fixed_binding = encode_allocation(
                        load_allocation(store, historical.attempt(ATTEMPT_ID).spec)
                    ).decode()
                if fixed_binding != encode_allocation(allocation).decode():
                    raise ValueError("recovery differs from original fixed allocation envelope")
            recorded_deadline = mode_recorded.get("deadline")
            if (
                type(recorded_deadline) is not int
                or any(
                    mode_recorded.get(name) != value
                    for name, value in {
                        "version": version,
                        "mode": "live" if forge_factory is not None else "synthetic",
                        "source": str(source_repository),
                        "repository": task.forge_repository,
                        **(
                            {"model": allocation.model_id, "provider": allocation.provider_id}
                            if allocation is not None
                            else {"allocator": "external"}
                        ),
                    }.items()
                )
                or (deadline is not None and deadline != recorded_deadline)
            ):
                raise ValueError("recovery differs from recorded execution envelope")
            if mode_recorded.get("supported_efforts", []) != [list(item) for item in sorted(supported_efforts)]:
                raise UnsupportedExecution("recovery requires original trusted support evidence")
            deadline = recorded_deadline
        assert deadline is not None  # noqa: S101 - established by durable envelope validation.
        if isinstance(connection, CodexStdio):
            previous_before_close = connection.before_close

            def before_owned_close() -> None:
                if cancellation_requested_now() or int(time.time()) >= deadline:
                    record_cancellation(ATTEMPT_ID, OPERATION_ID)
                if previous_before_close is not None:
                    previous_before_close()

            connection.before_close = before_owned_close
            cleanup.callback(setattr, connection, "before_close", previous_before_close)
        cancellation_id = f"cancel:{ATTEMPT_ID}"
        cancellation_pending = cancellation_requested_now()
        try:
            store.operation(cancellation_id)
            cancellation_pending = True
        except OperationConflict as error:
            if str(error) != "unknown operation":
                raise
        try:
            program = store.load(PROGRAM_ID)
        except ProgramNotFound:
            program = store.create(task_program_spec(task), "create")
        if program.spec != task_program_spec(task):
            raise ValueError("durable task intent differs from the frozen task specification")
        if program.status is ProgramStatus.DRAFT:
            program = store.admit(PROGRAM_ID, "activate", ActivateProgram(program.revision, "owner"))
            lifecycle.append("task admitted: owner-approved frozen task")
            crash("task-admitted")
        attempt_exists = any(item.spec.attempt_id == ATTEMPT_ID for item in program.attempts)
        if cancellation_pending and not attempt_exists:
            return cancelled_without_attempt()
        base, objects, workspace = prepare_workspace(
            store, directory, source_repository, task, lifecycle, attempt_exists=attempt_exists
        )
        crash("workspace-prepared")
        if cancellation_requested_now():
            cancellation_pending = True
        if cancellation_pending:
            # Runtime terminality is not task acceptance. Intent must survive even
            # when no interrupt is needed, including cancellation during verification.
            for attempt in program.attempts:
                operation = store.operation(f"runtime:{attempt.spec.attempt_id}")
                record_cancellation(attempt.spec.attempt_id, operation.operation_id)
        status = "accepted"
        if program.state(UNIT_ID).status is not WorkUnitStatus.SATISFIED:
            lifecycle.append("allocation recovered from durable Attempt" if attempt_exists else "allocation selected")
            workspace_handle = WorkspaceHandle(
                WORKSPACE_KEY,
                WorkspaceSpec(
                    str(workspace),
                    base,
                    (),
                    "task_execution:v1",
                    "codex-workspace-write",
                    TrustMode.TRUSTED_DEVELOPMENT,
                ),
            )
            collector = TaskCollector(
                store,
                task,
                workspace,
                objects,
                base,
                lifecycle,
                fault=crash,
                captured=captured,
                command_environment=command_environment,
                authorized=lambda: int(time.time()) < deadline and not cancellation_requested_now(),
            )

            def resolve(request: ExecutionRequest) -> CodexInputs:
                selected = request.allocation
                if selected is None:
                    raise ValueError("task execution requires the durable allocation")
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
                    and not cancellation_requested_now()
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
                clock=lambda: int(time.time()),
            )

            def restore(request: ExecutionRequest, handle: str) -> None:
                runtime.restore(request, handle)

            try:
                status = (
                    "cancel_uncertain"
                    if cancellation_pending
                    else advance_work_unit(
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
                        clock=lambda: int(time.time()),
                        deadline=deadline,
                        cancel_requested=cancellation_requested_now,
                    )
                )
            except (ExecutionConflict, OSError, RuntimeError, ValueError):
                if not cancellation_requested_now() and int(time.time()) < deadline:
                    raise
                status = "cancel_uncertain"
            cancellation_pending = cancellation_pending or cancellation_requested_now()
            if cancellation_pending or (int(time.time()) >= deadline and status != "accepted"):
                status = "expired" if int(time.time()) >= deadline else "cancel_uncertain"
                program = store.load(PROGRAM_ID)
                for attempt in program.attempts:
                    operation = store.operation(f"runtime:{attempt.spec.attempt_id}")
                    cancellation_id = f"cancel:{attempt.spec.attempt_id}"
                    cancellation = record_cancellation(attempt.spec.attempt_id, operation.operation_id)
                    if operation.status == "terminal":
                        finish_cancelled_attempt(attempt.spec.attempt_id)
                        if int(time.time()) < deadline:
                            status = "cancelled"
                        continue
                    selected = load_allocation(store, attempt.spec)
                    handle = operation.accepted_reference
                    receipt_data = store.find_artifact(operation.operation_id, "runtime-receipt")
                    if receipt_data is not None:
                        receipt_handle, receipt_fence, receipt_identity = decode_runtime_receipt(receipt_data)
                        if (
                            receipt_fence != operation.fence
                            or handle not in {None, receipt_handle}
                            or not identity_matches(
                                receipt_identity,
                                selected,
                                str(attempt.spec.agent_definition_reference),
                                require_resolved=False,
                            )
                        ):
                            raise ValueError("cancellation receipt differs from original execution")
                        if handle is None:
                            if operation.status not in {"dispatched", "unknown"} or operation.retry_proof is not None:
                                raise ValueError("cancellation receipt has no original delivery")
                            store.record_transport(operation.operation_id, receipt_fence, True)
                            operation = store.observe(
                                operation.operation_id,
                                receipt_fence,
                                f"accepted:{receipt_handle}",
                                "accepted",
                                reference=receipt_handle,
                            )
                        handle = receipt_handle
                        store.finalize_artifact(cancellation_id, "target", receipt_data)
                    if handle is None:
                        lifecycle.append("cancellation target unknown; no interrupt issued")
                        continue
                    request = ExecutionRequest(
                        OperationKey(operation.operation_id, operation.effect_key, operation.request_digest),
                        attempt.spec,
                        workspace_handle,
                        str(attempt.spec.context_reference),
                        str(attempt.spec.allocation_reference),
                        "owner-approved-reference",
                        RuntimeIdentity(
                            selected.model_id,
                            None,
                            None,
                            str(attempt.spec.agent_definition_reference),
                            requested_provider=selected.provider_id,
                            requested_effort=selected.reasoning_effort,
                        ),
                        operation.fence,
                        allocation=selected,
                    )
                    runtime.restore(request, handle)
                    if cancellation.attempts == 0:
                        fence = store.claim(cancellation_id, now=int(time.time()), lease_seconds=1)
                        crash("cancel-claim")
                        store.observe(cancellation_id, fence, f"uncertain:{cancellation_id}", "unknown")
                        receipt = runtime.cancel(handle)
                        crash("cancel-send")
                        store.finalize_artifact(cancellation_id, "receipt", manifest_bytes(asdict(receipt)))
                    observation = runtime.observe(handle, now=int(time.time()))
                    data = manifest_bytes(asdict(observation))
                    store.finalize_artifact(cancellation_id, f"observation:{hashlib.sha256(data).hexdigest()}", data)
                    crash("cancel-observation")
                    if observation.activity is Activity.TERMINAL:
                        store.observe(
                            operation.operation_id,
                            operation.fence,
                            f"terminal:{operation.operation_id}",
                            "terminal",
                            reference=handle,
                        )
                        crash("cancel-terminal")
                        finish_cancelled_attempt(attempt.spec.attempt_id)
                        if int(time.time()) < deadline:
                            status = "cancelled"
                    lifecycle.append(
                        "cancel delivery uncertain; descendant settlement unproven; cancellation is not rollback"
                    )
            if status == "identity_unavailable":
                lifecycle.append("runtime identity evidence missing or mismatched; no acceptance")
            elif status in {"unknown", "waiting", "running"}:
                lifecycle.append("runtime active or reconciling; preserve owned connection and original envelope")
            program = store.load(PROGRAM_ID)
            if cancellation_pending and not program.attempts:
                return cancelled_without_attempt()
        if cancellation_requested_now() and not cancellation_pending:
            cancellation_pending = True
            for attempt in program.attempts:
                operation = store.operation(f"runtime:{attempt.spec.attempt_id}")
                record_cancellation(attempt.spec.attempt_id, operation.operation_id)
        if program.state(UNIT_ID).status is WorkUnitStatus.SATISFIED:
            if cancellation_pending or int(time.time()) >= deadline:
                result = export_task_store(store)
                result.update(condition="expired" if int(time.time()) >= deadline else "cancelled", lifecycle=lifecycle)
                return result
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
                schema="task-pr-v1",
                title=task.pr_title,
                body=f"{task.pr_body}\n\nTask: {task.task_id}. Accepted result: {accepted['manifest']}.",
            )
            if receipt.get("interrupted"):
                raise TaskInterrupted(f"interrupted after {receipt['interrupted']}; rerun to reconcile")
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
            result = export_task_store(store)
            result.update(
                condition="accepted",
                head=head,
                pr=receipt,
                verification=captured,
                lifecycle=lifecycle,
                isolation="not attested; trusted development only",
            )
            return result
        result = export_task_store(store)
        result.update(
            condition=status,
            verification=captured,
            lifecycle=lifecycle,
            isolation="not attested; trusted development only",
        )
        return result


LIVE_ENV = {
    "router": "CREATIDY_KERNEL_ROUTER_URL",
    "router_key": "CREATIDY_KERNEL_ROUTER_KEY",
    "binding": "CREATIDY_KERNEL_RUNTIME_BINDING",
    "codex_bin": "CREATIDY_KERNEL_CODEX_BIN",
    "codex_version": "CREATIDY_KERNEL_CODEX_VERSION",
    "forge_api": "CREATIDY_KERNEL_FORGE_API",
    "forge_remote": "CREATIDY_KERNEL_FORGE_REMOTE",
    "forge_token": "CREATIDY_KERNEL_FORGE_TOKEN",
    "forge_askpass": "CREATIDY_KERNEL_FORGE_ASKPASS",
    "source": "CREATIDY_KERNEL_SOURCE_REPOSITORY",
    "state": "CREATIDY_KERNEL_STATE_DIRECTORY",
}
CODEX_METHODS = frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"})
# The closed set of operational environment entries the Codex app-server may receive.
# HOME carries Codex's own authenticated configuration directory (~/.codex), which is
# its existing credential mechanism — no secret is carried in the environment itself.
# PATH, LANG/LC_ALL and TMPDIR are the operational tool-resolution, locale and temp
# entries. Everything else in the controller environment, including every task
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
class LiveTaskComponents:
    """Owner-configured live bindings; secrets stay in the environment, never evidence."""

    allocator: ResourceAllocator
    connection_factory: Callable[[], CodexConnection]
    forge_factory: Callable[[Path], Forge]
    # Trusted Runtime effort-support evidence derived from the same controller-owned
    # runtime binding that constrains Router allocation; never Router-derived.
    supported_efforts: frozenset[tuple[str, str, str]]
    forge_read_factory: Callable[[], ForgejoForge] | None = None


def _runtime_binding(raw: str) -> Allocation:
    parts = raw.split("/")
    if len(parts) not in (2, 3) or any(re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,63}", part) is None for part in parts):
        raise ValueError("CREATIDY_KERNEL_RUNTIME_BINDING must be exact provider/model[/effort]")
    provider, model, *effort = parts
    return Allocation(
        "codex",
        provider,
        model,
        frozenset({"reference"}),
        128,
        "controller-configured task runtime binding",
        reasoning_effort=effort[0] if effort else None,
    )


class ConfigurationError(ValueError):
    """Safe diagnostics expose field names, never supplied values."""

    def __init__(self, reason: str, fields: tuple[str, ...]) -> None:
        self.fields = fields
        super().__init__(f"task configuration {reason}: " + ", ".join(fields))


@dataclass(frozen=True, slots=True)
class TaskRuntimeConfig:
    """Immutable infrastructure configuration, not task or execution authority."""

    router_url: str
    binding: Allocation
    codex_binary: Path
    codex_version: str
    forge_api: str
    forge_remote: str
    forge_token: str = field(repr=False)
    router_key: str | None = field(default=None, repr=False)
    forge_askpass: Path | None = None
    source_repository: Path | None = None
    state_directory: Path | None = None
    child_environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def supported_efforts(self) -> frozenset[tuple[str, str, str]]:
        binding = self.binding
        return (
            frozenset({(binding.provider_id, binding.model_id, binding.reasoning_effort)})
            if binding.reasoning_effort is not None
            else frozenset()
        )

    @classmethod
    def parse(cls, environ: Mapping[str, str]) -> TaskRuntimeConfig:
        # Negative compatibility guard: experimental environment names are rejected,
        # even if a complete neutral configuration is also present. No fallback.
        legacy = tuple(sorted(name for name in environ if name.startswith("CREATIDY_" + "DOGFOOD_")))
        if legacy:
            raise ConfigurationError("unsupported legacy fields", legacy)
        optional = {"router_key", "forge_askpass", "source", "state"}
        missing = tuple(sorted(name for key, name in LIVE_ENV.items() if key not in optional and not environ.get(name)))
        if missing:
            raise ConfigurationError("missing", missing)
        values = {key: environ.get(name, "") for key, name in LIVE_ENV.items()}

        def invalid(key: str) -> ConfigurationError:
            return ConfigurationError("invalid", (LIVE_ENV[key],))

        try:
            binding = _runtime_binding(values["binding"])
        except ValueError:
            raise invalid("binding") from None
        try:
            ScarcityRouterAllocator(values["router"], (binding,), api_key=values["router_key"] or None)
        except (ValueError, AllocationUnavailable):
            raise ConfigurationError("invalid", (LIVE_ENV["router"], LIVE_ENV["router_key"])) from None
        for key in ("codex_bin", "forge_askpass", "source", "state"):
            value = values[key]
            if value and (not Path(value).is_absolute() or any(ord(c) < 32 for c in value)):
                raise invalid(key)
        if re.fullmatch(CODEX_VERSION_PATTERN, values["codex_version"]) is None:
            raise invalid("codex_version")
        for key in ("forge_api", "forge_remote"):
            try:
                https_origin(values[key])
                if any(ord(c) <= 32 for c in values[key]):
                    raise ValueError("invalid URL")
            except ValueError:
                raise invalid(key) from None
        if urlsplit(values["forge_api"]).path != "/api/v1":
            raise invalid("forge_api")
        if https_origin(values["forge_api"]) != https_origin(values["forge_remote"]):
            raise ConfigurationError("inconsistent origins", (LIVE_ENV["forge_api"], LIVE_ENV["forge_remote"]))
        if not values["forge_token"] or any(not 33 <= ord(c) <= 126 for c in values["forge_token"]):
            raise invalid("forge_token")
        child = codex_environment(environ)
        for name, value in child.items():
            if any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ConfigurationError("invalid operational field", (name,))
            if name in {"HOME", "TMPDIR"} and not Path(value).is_absolute():
                raise ConfigurationError("invalid operational path", (name,))
            if name == "PATH" and any(not part or not Path(part).is_absolute() for part in value.split(os.pathsep)):
                raise ConfigurationError("invalid operational path", (name,))
        return cls(
            values["router"],
            binding,
            Path(values["codex_bin"]),
            values["codex_version"],
            values["forge_api"],
            values["forge_remote"],
            values["forge_token"],
            values["router_key"] or None,
            Path(values["forge_askpass"]) if values["forge_askpass"] else None,
            Path(values["source"]) if values["source"] else None,
            Path(values["state"]) if values["state"] else None,
            tuple(sorted(child.items())),
        )


def _codex_schema(config: TaskRuntimeConfig) -> frozenset[str]:
    """Generate this exact binary's native schema without creating a thread or turn."""
    methods: set[str] = set()

    def inspect(value: object, depth: int = 0) -> None:
        if depth > 64:
            raise ValueError("Codex schema exceeds nesting limit")
        if isinstance(value, dict):
            obj = cast(dict[str, object], value)
            properties = obj.get("properties")
            if isinstance(properties, dict):
                method = cast(dict[str, object], properties).get("method")
                if isinstance(method, dict):
                    definition = cast(dict[str, object], method)
                    constant = definition.get("const")
                    enum = definition.get("enum")
                    if isinstance(constant, str):
                        methods.add(constant)
                    if isinstance(enum, list):
                        methods.update(item for item in cast(list[object], enum) if isinstance(item, str))
            for child in obj.values():
                inspect(child, depth + 1)
        elif isinstance(value, list):
            for child in cast(list[object], value):
                inspect(child, depth + 1)

    with tempfile.TemporaryDirectory(
        prefix="kernel-codex-schema-",
        dir=dict(config.child_environment).get("TMPDIR", "/tmp"),  # noqa: S108 - private randomized TemporaryDirectory.
    ) as directory:
        completed = subprocess.run(  # noqa: S603 - absolute configured executable, fixed inference-free argv.
            (str(config.codex_binary), "app-server", "generate-json-schema", "--out", directory),
            env=dict(config.child_environment),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("Codex schema generation failed")
        files = list(Path(directory).rglob("*.json"))
        if not files or len(files) > 512:
            raise ValueError("Codex schema inventory is missing or exceeds limit")
        total = 0
        for path in files:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Codex schema file is unsuitable")
            total += path.stat().st_size
            if total > 8 * 1024 * 1024:
                raise ValueError("Codex schema exceeds size limit")
            inspect(cast(object, json.loads(path.read_bytes())))
    if not CODEX_METHODS <= methods:
        raise ValueError("Codex schema lacks required native methods")
    return frozenset(methods)


def compose_task_live(config: TaskRuntimeConfig, task: TaskSpec) -> LiveTaskComponents:
    """Compose only parsed infrastructure; TaskSpec alone supplies task authority."""
    repository = Reference(task.forge_repository)
    if urlsplit(config.forge_remote).path != f"/{repository_path(repository)}.git" or https_origin(
        config.forge_api
    ) != https_origin(task.repository_url):
        raise ConfigurationError("outside TaskSpec repository", (LIVE_ENV["forge_api"], LIVE_ENV["forge_remote"]))
    allocator = ScarcityRouterAllocator(
        config.router_url,
        (config.binding,),
        api_key=config.router_key,
    )

    def connection_factory() -> CodexConnection:
        _executable(config.codex_binary)
        methods = _codex_schema(config)
        return CodexStdio(
            (str(config.codex_binary), "app-server"),
            config.codex_version,
            schema_methods=methods,
            schema_version=config.codex_version,
            environment=dict(config.child_environment),
        )

    def forge_factory(directory: Path) -> Forge:
        transport = ConditionalGitTransport(
            repository,
            config.forge_remote,
            directory / "objects.git",
            askpass=config.forge_askpass,
        )
        return ForgejoForge(
            HTTPSForgejoTransport(config.forge_api, repository, lambda: config.forge_token),
            transport,
            lambda proposed: proposed.repository == repository,
        )

    def forge_read_factory() -> ForgejoForge:
        # The preflight has no object source and must not construct a push transport.
        return ForgejoForge(
            HTTPSForgejoTransport(config.forge_api, repository, lambda: config.forge_token),
            ReadOnlyGitBinding(https_origin(config.forge_api), repository_path(repository)),
            lambda _: False,
        )

    return LiveTaskComponents(
        allocator, connection_factory, forge_factory, config.supported_efforts, forge_read_factory
    )


class ReadOnlyGitBinding:
    """Forge read composition cannot deliver any Git effect."""

    def __init__(self, origin: str, repository: str) -> None:
        self.binding = ForgeBinding(origin, repository)
        self.supports_conditional_push = False
        self.supports_agit = False

    def compare_and_push(
        self,
        repository: Reference,
        branch: str,
        expected: Reference | None,
        revision: Reference,
    ) -> EffectStatus:
        raise UnsupportedForge("preflight is read-only")

    def create_agit_pr(
        self,
        repository: Reference,
        base: str,
        topic: str,
        revision: Reference,
        title: str,
        description: str,
    ) -> AGitPush:
        raise UnsupportedForge("preflight is read-only")


class PreflightReason(StrEnum):
    CONFIGURATION = "configuration_invalid"
    SOURCE = "source_not_ready"
    STATE = "state_not_ready"
    TASK = "task_baseline_invalid"
    CODEX = "codex_not_ready"
    ROUTER = "router_not_ready"
    FORGE = "forge_not_ready"


@dataclass(frozen=True, slots=True)
class PreflightBlocker:
    reason: PreflightReason
    fields: tuple[str, ...] = ()
    category: str | None = None

    def __post_init__(self) -> None:
        if self.category is not None and re.fullmatch(r"[a-z][a-z_]{0,63}", self.category) is None:
            raise ValueError("blocker category must be a safe closed-vocabulary identifier")

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"reason": self.reason.value, "fields": list(self.fields)}
        if self.category is not None:
            payload["category"] = self.category
        return payload


@dataclass(frozen=True, slots=True)
class PreflightResult:
    task_id: str
    task_digest: str
    repository: str
    exact_base_sha: str | None
    base_ref: str | None
    blockers: tuple[PreflightBlocker, ...]
    evidence: Mapping[str, object] = field(repr=False)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def payload(self) -> dict[str, object]:
        return {
            "schema": 1,
            "status": "READY_FOR_LIVE_TASK" if self.ready else "BLOCKED",
            "ready": self.ready,
            "task_id": self.task_id,
            "task_digest": self.task_digest,
            "repository": self.repository,
            "exact_base_sha": self.exact_base_sha,
            "base_ref": self.base_ref,
            "blockers": [blocker.payload() for blocker in self.blockers],
            "evidence": dict(self.evidence),
            "inference_performed": False,
            "forge_writes_performed": False,
            "execution_authorized": False,
        }


def _suitable_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("absolute non-symlink path required")


def _executable(path: Path) -> None:
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("absolute executable file required")


def default_state_directory(environ: Mapping[str, str] | None = None) -> Path:
    """Platform-appropriate user-local task state root; XDG-compatible on Linux."""
    values = os.environ if environ is None else environ
    override = values.get("XDG_STATE_HOME", "")
    base = Path(override) if override and Path(override).is_absolute() else Path.home() / ".local" / "state"
    return base / "creatidy-kernel" / "task"


def task_paths(
    config: TaskRuntimeConfig,
    directory: Path | None,
    source_repository: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path | None]:
    """CLI paths first, then configured fields, then user-local state defaults.

    Infrastructure defaults never alter TaskSpec. The returned source is ``None``
    when normal operation must acquire the controller-owned cache from the
    TaskSpec canonical repository; an explicit source remains the advanced
    override and is verified exactly as before.
    """
    state = directory if directory is not None else config.state_directory
    if state is None and environ is not None:
        state = default_state_directory(environ)
    source = source_repository if source_repository is not None else config.source_repository
    if state is None:
        raise ConfigurationError("missing path arguments or fields", (LIVE_ENV["state"],))
    return state, source


def resolve_source(
    config: TaskRuntimeConfig,
    task: TaskSpec,
    source_repository: Path | None,
    *,
    environment: Mapping[str, str],
) -> Path:
    """CLI override, then configured field, then the controller-owned canonical cache.

    Normal operation therefore needs no ``CREATIDY_KERNEL_SOURCE_REPOSITORY`` and no
    local checkout: the cache is acquired from the TaskSpec canonical repository and
    verified against that exact identity.
    """
    source = source_repository if source_repository is not None else config.source_repository
    if source is not None:
        return source
    return acquire_source(task.repository_url, askpass=config.forge_askpass, environment=environment)


def _preflight_base(source: Path, directory: Path, task: TaskSpec) -> tuple[str, str]:
    database = directory / "kernel.sqlite3"
    if database.exists():
        # Do not open the writable ProgramStore (startup can migrate/checkpoint).
        # The state suitability gate refuses live/recovery sidecars first. Read the
        # checkpointed intent immutably, without creating WAL/SHM files or locks.
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
            mode = connection.execute(
                "SELECT request_json FROM operations WHERE operation_id = ?",
                ("task_execution:mode",),
            ).fetchone()
            base = connection.execute(
                "SELECT request_json FROM operations WHERE operation_id = ?",
                ("task_execution:base",),
            ).fetchone()
        if mode is None:
            raise ValueError("existing state is not this neutral task")
        mode_request = cast(dict[str, object], json.loads(str(mode[0])))["request"]
        if (
            not isinstance(mode_request, dict)
            or cast(dict[str, object], mode_request).get("task_digest") != task.digest
        ):
            raise ValueError("existing state differs from frozen TaskSpec")
        task = _recover_task_pin(task, cast(dict[str, object], mode_request))
        if base is not None:
            request = cast(dict[str, object], cast(dict[str, object], json.loads(str(base[0])))["request"])
            sha = oid(str(request["base"]))
            if request.get("task") != task.task_id or (
                task.expected_base_sha is not None and sha != task.expected_base_sha
            ):
                raise ValueError("existing frozen base differs from task")
            if git_text(source, "rev-parse", "--verify", f"{sha}^{{commit}}") != sha:
                raise ValueError("frozen base unavailable")
            return sha, "durable:" + str(request["ref"])
    return _resolve_base(source, task)


def _baseline(source: Path, base: str, task: TaskSpec, environment: Mapping[str, str]) -> dict[str, object]:
    contents: list[str] = []
    for path in sorted(task.allowed_paths):
        mode = git_text(source, "ls-tree", base, "--", path).split(" ", 1)[0]
        if mode not in {"100644", "100755"}:
            raise ValueError("allowed path must exist as an ordinary file at the exact base")
        size = int(git_text(source, "cat-file", "-s", f"{base}:{path}"))
        if size > MAX_PATCH_BYTES:
            raise ValueError("task baseline exceeds limit")
        raw = git_bytes(source, "cat-file", "blob", f"{base}:{path}")
        contents.append(raw.decode("utf-8"))
    content = "\n".join(contents)
    for required in (*task.structural.required_names, *task.structural.required_tokens):
        if required not in content:
            raise ValueError("frozen structural baseline is missing required scenario or assertion")
    task.structural.validate_baseline(content)
    for command in task.verification:
        argv = command.argv
        executable = shutil.which(argv[0], path=environment.get("PATH", os.defpath))
        if executable is None or not Path(executable).is_absolute():
            raise ValueError("trusted verification executable is unavailable in the closed environment")
        if argv == ("make", "check"):
            continue
        if len(argv) >= 6 and argv[:5] == ("uv", "run", "python", "-m", "unittest"):
            continue
        if len(argv) >= 4 and Path(argv[0]).is_absolute() and argv[1:3] == ("-m", "unittest"):
            continue
        raise ValueError("verification command is outside the bounded task command shapes")
    return {
        "allowed_paths": sorted(task.allowed_paths),
        "structural_baseline": "present",
        "verification": [
            {"argv": list(c.argv), "timeout_seconds": c.timeout_seconds, "repeats": c.repeats}
            for c in task.verification
        ],
        "verification_executed": False,
        "deferred_to_independent_review": list(task.structural.deferred_to_review),
    }


CODEX_ENV_FIELDS = (LIVE_ENV["codex_bin"], LIVE_ENV["codex_version"])


def environment_with_discovered_codex(
    environ: Mapping[str, str],
) -> tuple[dict[str, str], CodexDiscoveryFailure | None]:
    """Fill missing Codex fields from deterministic local discovery; explicit overrides win.

    Discovery resolves exactly one candidate (the override binary or the first
    ``codex`` on the supplied ``PATH``) and only probes ``--version``: no
    app-server thread or turn is ever started here.
    """
    enriched = dict(environ)
    if all(enriched.get(name) for name in CODEX_ENV_FIELDS):
        return enriched, None
    resolution = resolve_codex(enriched)
    if isinstance(resolution, CodexDiscoveryFailure):
        return enriched, resolution
    if not enriched.get(LIVE_ENV["codex_bin"]):
        enriched[LIVE_ENV["codex_bin"]] = str(resolution.binary)
    if not enriched.get(LIVE_ENV["codex_version"]):
        enriched[LIVE_ENV["codex_version"]] = resolution.version
    return enriched, None


def preflight_task(
    environ: Mapping[str, str],
    *,
    task: TaskSpec,
    directory: Path | None = None,
    source_repository: Path | None = None,
) -> PreflightResult:
    """Quota-free finite readiness; no admission, model turn, verification run or Forge write."""
    blockers: list[PreflightBlocker] = []
    evidence: dict[str, object] = {}
    base: str | None = None
    ref: str | None = None

    def result() -> PreflightResult:
        return PreflightResult(task.task_id, task.digest, task.forge_repository, base, ref, tuple(blockers), evidence)

    enriched, discovery_failure = environment_with_discovered_codex(environ)
    try:
        config = TaskRuntimeConfig.parse(enriched)
        components = compose_task_live(config, task)
        directory, source = task_paths(config, directory, source_repository, environ=enriched)
    except ConfigurationError as error:
        others = tuple(name for name in error.fields if name not in CODEX_ENV_FIELDS)
        if others:
            blockers.append(PreflightBlocker(PreflightReason.CONFIGURATION, others))
        missing_codex = tuple(name for name in error.fields if name in CODEX_ENV_FIELDS)
        if discovery_failure is not None and missing_codex:
            blockers.append(
                PreflightBlocker(
                    PreflightReason.CODEX,
                    missing_codex,
                    category=discovery_failure.category.value,
                )
            )
        if not blockers:
            blockers.append(PreflightBlocker(PreflightReason.CONFIGURATION, error.fields))
        return result()
    evidence["configuration"] = {
        "runtime_id": config.binding.runtime_id,
        "provider_id": config.binding.provider_id,
        "model_id": config.binding.model_id,
        "reasoning_effort": config.binding.reasoning_effort,
        "supported_efforts": sorted(config.supported_efforts),
        "child_environment_keys": sorted(name for name, _ in config.child_environment),
    }
    # Normal operation acquires the controller-owned read-only cache from the
    # TaskSpec canonical repository before any path-suitability check, so state
    # and source overlap rules apply to the cache as well. Acquisition is a
    # remote read; it never writes to the remote.
    acquired_cache: Path | None = None
    if source is None:
        try:
            source = acquire_source(
                task.repository_url,
                askpass=config.forge_askpass,
                environment=enriched,
            )
            acquired_cache = source
        except SourceAcquisitionError as error:
            blockers.append(PreflightBlocker(PreflightReason.SOURCE, category=error.category.value))
    try:
        _suitable_path(directory)
        if source is not None and (
            directory == source or directory.is_relative_to(source) or source.is_relative_to(directory)
        ):
            raise ValueError("source and state must be separate trees")
        parent = directory
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            raise ValueError("state directory or ancestor is not writable")
        if directory.exists() and not (directory / "kernel.sqlite3").exists() and any(directory.iterdir()):
            raise ValueError("unjournaled task state directory must be empty")
        filesystem = validate_local_storage(
            parent,
            database_path=directory / "kernel.sqlite3" if parent == directory else None,
        )
        for name in ("workspace", "objects.git", "kernel.sqlite3", "artifacts"):
            if (directory / name).is_symlink():
                raise ValueError("state child must not be a symlink")
        database = directory / "kernel.sqlite3"
        if database.exists() and not os.access(database, os.R_OK | os.W_OK):
            raise ValueError("state database must be readable and writable")
        if any((directory / f"kernel.sqlite3{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
            raise ValueError("state needs controller reconciliation/checkpoint before read-only preflight")
        if config.forge_askpass is not None:
            _suitable_path(config.forge_askpass)
            _executable(config.forge_askpass)
            if source is not None and (
                config.forge_askpass.is_relative_to(source) or config.forge_askpass.is_relative_to(directory)
            ):
                raise ValueError("askpass must be outside source/state trees")
        evidence["state"] = {"directory": str(directory), "filesystem": filesystem, "created": False}
    except (OSError, ValueError, RuntimeError):
        blockers.append(PreflightBlocker(PreflightReason.STATE))
    source_category: str | None = None
    checked_source: Path | None = None
    if source is not None:
        try:
            _suitable_path(source)
            if not source.is_dir() or not (source / ".git").is_dir() or (source / ".git").is_symlink():
                source_category = "checkout_unsuitable"
                raise ValueError("source must be an ordinary Git checkout")
            origin = git_text(source, "config", "--get", "remote.origin.url")
            if origin not in {task.repository_url, task.repository_url + ".git"}:
                source_category = "remote_mismatch"
                raise ValueError("source origin differs from the exact task repository")
            if git_text(source, "--no-optional-locks", "-c", "core.fsmonitor=false", "status", "--porcelain"):
                source_category = "checkout_dirty"
                raise ValueError("source checkout is not clean")
            try:
                base, ref = (
                    _resolve_base(source, task)
                    if any(blocker.reason is PreflightReason.STATE for blocker in blockers)
                    else _preflight_base(source, directory, task)
                )
            except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError):
                source_category = "base_unavailable"
                raise
            evidence["source"] = {
                "directory": str(source),
                "clean": True,
                "read_only": True,
                **(
                    {"acquired": True, "cache_root": str(default_cache_root(enriched))}
                    if acquired_cache is not None
                    else {}
                ),
            }
            checked_source = source
        except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError):
            blockers.append(PreflightBlocker(PreflightReason.SOURCE, category=source_category))
    if base is not None and checked_source is not None:
        try:
            evidence["task"] = _baseline(checked_source, base, task, dict(config.child_environment))
        except (OSError, ValueError, RuntimeError, SyntaxError):
            blockers.append(PreflightBlocker(PreflightReason.TASK))
    try:
        child = dict(config.child_environment)
        binary = config.codex_binary.resolve()
        if source is not None and (binary.is_relative_to(source) or binary.is_relative_to(directory)):
            raise ValueError("Codex executable must be outside source and state")
        home = Path(child.get("HOME", ""))
        if not home.is_absolute() or not home.is_dir() or not os.access(home, os.R_OK | os.X_OK):
            raise ValueError("supplied HOME must be an accessible absolute directory")
        if "TMPDIR" in child:
            temporary = Path(child["TMPDIR"])
            if not temporary.is_dir() or not os.access(temporary, os.W_OK | os.X_OK):
                raise ValueError("supplied TMPDIR must be writable")
        connection = components.connection_factory()
        try:
            if connection.version != config.codex_version or not CODEX_METHODS <= connection.methods:
                raise ValueError("native version/schema mismatch")
            evidence["codex"] = {
                "version": connection.version,
                "methods": sorted(connection.methods),
                "initialized": True,
                "authentication_attested": False,
            }
        finally:
            if isinstance(connection, CodexStdio):
                connection.close()
    except (OSError, ValueError, RuntimeError, OverflowError, RecursionError):
        blockers.append(PreflightBlocker(PreflightReason.CODEX))
    try:
        allocation = components.allocator.select(ResourceRequest(UNIT_ID, frozenset({"reference"}), 128))
        binding = config.binding
        if (
            allocation.runtime_id,
            allocation.provider_id,
            allocation.model_id,
            allocation.reasoning_effort,
        ) != (binding.runtime_id, binding.provider_id, binding.model_id, binding.reasoning_effort):
            raise AllocationUnavailable("selection outside controller runtime binding")
        evidence["router"] = {"compatible": True, "reservation_acquired": False}
    except (AllocationUnavailable, OSError, ValueError, RuntimeError) as error:
        category = (
            error.category.value
            if isinstance(error, ScarcityRouterUnavailable) and error.category is not None
            else None
        )
        blockers.append(PreflightBlocker(PreflightReason.ROUTER, category=category))
    try:
        if components.forge_read_factory is None:
            raise UnsupportedForge("read-only Forge composition unavailable")
        forge = components.forge_read_factory()
        repository = Reference(task.forge_repository)
        identity = forge.identity(repository)
        branch = forge.branch(repository, task.base_branch)
        if identity.presence is not Presence.FOUND or identity.reference != repository:
            raise UnsupportedForge("exact Forge repository unreadable")
        if branch.presence is not Presence.FOUND or branch.revision is None:
            raise UnsupportedForge("Forge base branch unreadable")
        revision = oid(branch.revision.value.removeprefix("forgejo:"))
        if config.forge_token in revision or (config.router_key is not None and config.router_key in revision):
            raise UnsupportedForge("credential echoed in Forge readiness observation")
        status, issue_raw = forge.http.request(
            "GET",
            f"/repos/{repository_path(repository)}/issues/{task.forge_issue}",
        )
        if status != 200 or not isinstance(issue_raw, dict):
            raise UnsupportedForge("frozen Forge issue unreadable")
        issue = cast(dict[str, object], issue_raw)
        if (
            type(issue.get("number")) is not int
            or issue["number"] != task.forge_issue
            or issue.get("html_url") != f"{task.repository_url}/issues/{task.forge_issue}"
            or issue.get("state") != "open"
            or issue.get("pull_request") is not None
        ):
            raise UnsupportedForge("frozen Forge issue identity or state differs")
        evidence["forge"] = {
            "repository_readable": True,
            "base_branch": task.base_branch,
            "observed_base_sha": revision,
            "issue_number": task.forge_issue,
            "issue_readable": True,
            "writes_performed": False,
            "write_permission_attested": False,
        }
    except (OSError, ValueError, RuntimeError):
        blockers.append(PreflightBlocker(PreflightReason.FORGE))
    return result()
