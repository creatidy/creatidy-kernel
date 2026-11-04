# SPDX-License-Identifier: Apache-2.0
"""Installed SDK conformance for private durable outcome export."""

from __future__ import annotations

import ast
import hashlib
import hmac
import itertools
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, cast
from unittest.mock import patch

import creatidy_kernel
from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.outcome_file import write_private_outcome
from creatidy_kernel.adapters.reference import POLICY as REFERENCE_POLICY
from creatidy_kernel.adapters.reference import reference_commit, reference_git, reference_spec, run_reference
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import (
    TaskCollector,
    TaskInterrupted,
    TaskSpec,
    VerificationCommand,
    run_task,
    scarcity_router_143_task,
)
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AmendProgramSpec,
    AttemptSpec,
    CancelProgram,
    CancelWorkUnit,
    PrepareAttempt,
    ProgramSpec,
    SpecAmendment,
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
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.resources import Allocation, ResourceRequest
from creatidy_kernel.core.verification import (
    CandidateResult,
    Evidence,
    EvidenceSubject,
    ProductReviewReceipt,
    ProductReviewRequest,
    ProductReviewVerdict,
    VerificationPolicy,
)
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.application import Collection, advance_work_unit
from creatidy_kernel.ports.outcome_export import PRIVATE_METADATA, export_private_outcome
from creatidy_kernel.ports.verification import ReviewLookup, ReviewLookupStatus

root = Path(__file__).resolve().parents[1]
if root in Path(creatidy_kernel.__file__).resolve().parents:
    raise SystemExit("outcome conformance must use the installed package")
if len(sys.argv) != 2:
    raise SystemExit("expected synthetic storage directory")
storage = Path(sys.argv[1])
directory = storage / "private"
directory.mkdir(mode=0o700)
with SQLiteProgramStore(storage / "outcome.sqlite3") as store:
    program = store.create(reference_spec(), "synthetic-create")
    budget = WorkBudgetPolicy.basic(3, 1, verifications=6, retained_bytes=16384)
    store.bind_work_budget(program.program_id, "synthetic-project", budget, budget, 4102444800)
    program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
    attempt = AttemptSpec(
        "synthetic-attempt",
        program.program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
    )
    operation = "runtime:synthetic-attempt"
    store.admit_with_intent(
        program.program_id,
        "prepare",
        PrepareAttempt(program.revision, "worker", attempt),
        operation,
        operation,
        {"attempt": attempt.digest},
    )
    store.reserve_work(program.program_id, attempt.attempt_id, operation, WorkKind.ATTEMPT)
    observation = UsageObservation(
        "obs",
        attempt.attempt_id,
        "receipt",
        "money",
        "USD",
        Decimal("2"),
        Provenance.PROVIDER_REPORT,
        "synthetic-secret-source",
        100,
    )
    store.record_usage(operation, observation)
    store.record_usage(
        operation, replace(observation, observation_id="correction", quantity=Decimal("4"), supersedes_id="obs")
    )
    token = store.served_change_token()
    expected = export_private_outcome(store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    if store.served_change_token() != token or "synthetic-secret-source" in json.dumps(expected):
        raise SystemExit("installed export mutated source or leaked a source identity")
    totals = cast(list[dict[str, object]], expected["usage_totals"])
    if len(totals) != 1 or totals[0]["known_subtotal"] != "4" or totals[0]["quantity"] is not None:
        raise SystemExit("installed export lost correction or unknown coverage")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        write_private_outcome(
            store,
            program.program_id,
            owner_policy=PRIVATE_METADATA,
            directory_fd=descriptor,
            name="outcome.json",
            pseudonym_key=b"k" * 32,
        )
        output = directory / "outcome.json"
        if output.stat().st_mode & 0o777 != 0o600 or json.loads(output.read_bytes()) != expected:
            raise SystemExit("installed export did not publish private exact bytes")
        try:
            write_private_outcome(
                store, program.program_id, owner_policy=PRIVATE_METADATA, directory_fd=descriptor, name="outcome.json"
            )
        except FileExistsError:
            pass
        else:
            raise SystemExit("installed export overwrote an existing file")
        with patch("os.link", side_effect=OSError("synthetic interrupted publication")):
            try:
                write_private_outcome(
                    store,
                    program.program_id,
                    owner_policy=PRIVATE_METADATA,
                    directory_fd=descriptor,
                    name="interrupted.json",
                )
            except OSError:
                pass
            else:
                raise SystemExit("installed export missed interrupted publication")
        if (directory / "interrupted.json").exists() or store.served_change_token() != token:
            raise SystemExit("installed interrupted export published partial data or mutated the source")
    finally:
        os.close(descriptor)
crash_source = storage / "crash.sqlite3"
with SQLiteProgramStore(crash_source) as store:
    program = store.create(reference_spec(), "crash-create")
    original = (store.load(program.program_id), store.history(program.program_id), store.operations())
crash_script = """
import os, sys
from creatidy_kernel.adapters.outcome_file import write_private_outcome
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.ports.outcome_export import PRIVATE_METADATA
real_link = os.link
def interrupt(*args, **kwargs):
    if sys.argv[3] == 'after':
        real_link(*args, **kwargs)
    os._exit(97)
os.link = interrupt
fd = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
with SQLiteProgramStore(sys.argv[1]) as store:
    write_private_outcome(store, 'reference', owner_policy=PRIVATE_METADATA, directory_fd=fd,
                          name='outcome.json', pseudonym_key=b'k' * 32)
"""
for phase in ("before", "after"):
    destination = storage / f"crash-{phase}"
    destination.mkdir(mode=0o700)
    result = subprocess.run(  # noqa: S603 - Installed interpreter with synthetic fixed script.
        [sys.executable, "-I", "-c", crash_script, str(crash_source), str(destination), phase],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
        env={"HOME": str(storage), "PATH": os.defpath, "LANG": "C.UTF-8"},
    )
    if result.returncode != 97:
        raise SystemExit("installed private export did not reach the process interruption cut")
    output = destination / "outcome.json"
    leftovers = tuple(item for item in destination.iterdir() if item.name.startswith(".outcome-"))
    if len(leftovers) != 1 or leftovers[0].stat().st_mode & 0o777 != 0o600 or output.exists() != (phase == "after"):
        raise SystemExit("installed crash cut exposed or partially published export bytes")
    if output.exists() and (
        json.loads(output.read_bytes())["schema"] != "creatidy.outcome.private/1"
        or output.read_bytes() != leftovers[0].read_bytes()
    ):
        raise SystemExit("installed post-publication crash left incomplete output")
    with SQLiteProgramStore(crash_source) as store:
        if (store.load("reference"), store.history("reference"), store.operations()) != original:
            raise SystemExit("installed export crash changed original Program evidence")
for version in (1, 99):
    with SQLiteProgramStore(storage / f"legacy-{version}.sqlite3") as store:
        program = store.create(reference_spec(), f"create-{version}")
        budget = WorkBudgetPolicy.basic(3, 1, verifications=6, retained_bytes=16384)
        store.bind_work_budget(program.program_id, f"project-{version}", budget, budget, 4102444800)
        program = store.admit(program.program_id, f"activate-{version}", ActivateProgram(program.revision, "owner"))
        attempt = AttemptSpec(
            f"legacy-attempt-{version}",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
        )
        operation = f"runtime:{attempt.attempt_id}"
        store.admit_with_intent(
            program.program_id,
            f"prepare-{version}",
            PrepareAttempt(program.revision, "worker", attempt),
            operation,
            operation,
            {"attempt": attempt.digest},
        )
        receipt = {
            "version": version,
            "handle": "synthetic-private-handle",
            "fence": 1,
            "identity": {
                "requested": "synthetic-model",
                "resolved": None,
                "observed": None,
                "agent_definition_version": "synthetic-agent",
            },
        }
        store.finalize_artifact(operation, "runtime-receipt", json.dumps(receipt).encode())
        if version == 1:
            result = export_private_outcome(
                store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
            )
            attempts = cast(list[dict[str, object]], result["attempts"])
            identity = cast(dict[str, object], attempts[0]["runtime_receipt_identity"])
            if identity["observed"] is not None or identity["requested_provider"] is not None:
                raise SystemExit("installed legacy export invented an observed identity")
        else:
            try:
                export_private_outcome(
                    store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                )
            except ValueError:
                pass
            else:
                raise SystemExit("installed export accepted an unsupported producer receipt")
reference_dir = storage / "reference-driver"
if run_reference(reference_dir, owner_approved=True)["status"] != "completed":
    raise SystemExit("installed reference driver did not finish")
with SQLiteProgramStore(reference_dir / "kernel.sqlite3") as store:
    result = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    attempts = cast(list[dict[str, object]], result["attempts"])
    if len(attempts) != 2 or any(item["allocation"] is None for item in attempts):
        raise SystemExit("installed reference outcome omitted original allocations")
    if any(cast(dict[str, object], item["terminal_outcome"])["candidate"] != "present" for item in attempts):
        raise SystemExit("installed reference outcome omitted original terminal candidate disposition")
    if len(cast(list[object], result["satisfactions"])) != 2 or result["reviews"] != []:
        raise SystemExit("installed reference outcome misrepresented independent review evidence")
    native = cast(list[dict[str, object]], result["native_effects"])
    if len(native) != 1 or native[0]["driver"] != "reference-pr-v1" or native[0]["current_remote_state"] is not None:
        raise SystemExit("installed reference outcome lost historical native publication lineage")
    program = store.load("reference")
    store.admit(
        "reference",
        "amend-after-completion",
        AmendProgramSpec(
            program.revision, "owner", SpecAmendment(program.spec.revision, objective="synthetic amendment")
        ),
    )
with SQLiteProgramStore(reference_dir / "kernel.sqlite3") as store:
    history = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    conclusions = cast(list[dict[str, object]], history["program_conclusions"])
    if not conclusions or conclusions[0]["spec"] == cast(dict[str, object], history["program"])["spec"]:
        raise SystemExit("installed export lost historical conclusions after amendment and reopen")
domain_path = storage / "domain-cancel.sqlite3"
with SQLiteProgramStore(domain_path) as store:
    program = store.create(reference_spec(), "domain-create")
    program = store.admit("reference", "domain-activate", ActivateProgram(program.revision, "owner"))
    attempt = AttemptSpec(
        "domain-attempt",
        "reference",
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
    )
    program = store.admit("reference", "domain-prepare", PrepareAttempt(program.revision, "worker", attempt))
    program = store.admit(
        "reference",
        "domain-cancel-work",
        CancelWorkUnit(program.revision, "owner", "first", "synthetic-private-reason"),
    )
    store.admit("reference", "domain-cancel-program", CancelProgram(program.revision, "owner"))
with SQLiteProgramStore(domain_path) as store:
    before = store.served_change_token()
    exported = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    records = cast(list[dict[str, object]], exported["domain_cancellations"])
    if (
        {item["kind"] for item in records} != {"work-unit", "attempt"}
        or exported["cancellations"]
        or any(item["physical_settlement"] != "unknown" for item in records)
        or not exported["program_conclusions"]
        or "synthetic-private-reason" in json.dumps(exported)
        or store.served_change_token() != before
    ):
        raise SystemExit("installed export lost domain cancellation history after reopen")
for driver in ("reference", "task_execution"):
    for qualified in (False, True):
        path = storage / f"{driver}-{qualified}.sqlite3"
        with SQLiteProgramStore(path) as store:
            base = reference_spec()
            spec = ProgramSpec(
                driver,
                base.objective,
                base.work_units,
                base.initial_inputs,
                base.budget,
                base.authority,
                acceptance_criteria=base.acceptance_criteria,
                policy_references=base.policy_references,
            )
            program = store.create(spec, f"create-{driver}-{qualified}")
            budget = WorkBudgetPolicy.basic(3, 1, verifications=6, retained_bytes=16384)
            store.bind_work_budget(program.program_id, f"project-{driver}", budget, budget, 4102444800)
            program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
            attempt = AttemptSpec(
                f"{driver}:first",
                program.program_id,
                "first",
                program.spec.revision,
                program.spec.digest,
                program.resolved_inputs("first"),
            )
            runtime = f"runtime:{attempt.attempt_id}"
            store.admit_with_intent(
                program.program_id,
                "prepare",
                PrepareAttempt(program.revision, "worker", attempt),
                runtime,
                runtime,
                {"attempt": attempt.digest},
            )
            store.reserve_work(program.program_id, attempt.attempt_id, runtime, WorkKind.ATTEMPT)
            runtime_fence = store.claim(runtime, now=100, lease_seconds=10)
            handle, group = "synthetic-private-handle", "synthetic-private-group"
            identity = RuntimeIdentity("synthetic-model", "synthetic-model", "synthetic-model", "synthetic-agent")
            store.finalize_artifact(
                runtime,
                "runtime-receipt",
                json.dumps(
                    {
                        "version": 2,
                        "handle": handle,
                        "fence": runtime_fence,
                        "identity": asdict(identity),
                        "owned_group_reference": group,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            )
            store.record_transport(runtime, runtime_fence, True)
            store.observe(runtime, runtime_fence, "runtime-accepted", "accepted", reference=handle)
            operation = store.operation(runtime)
            cancellation = f"cancel:{attempt.attempt_id}"
            request: dict[str, object] = (
                {"operation": runtime, "handle": handle}
                if driver == "reference"
                else {"operation": runtime, "descendants": "unproven"}
            )
            store.intent(cancellation, cancellation, request)
            fence = store.claim(cancellation, now=100, lease_seconds=10)
            store.record_transport(cancellation, fence, qualified)
            if qualified:
                if driver == "reference":
                    proof = {
                        "version": 1,
                        "operation": runtime,
                        "effect_key": operation.effect_key,
                        "request_digest": operation.request_digest,
                        "runtime_fence": runtime_fence,
                        "cancellation_fence": fence,
                        "attempt": attempt.digest,
                        "handle": handle,
                        "owned_group_reference": group,
                        "owned_group_settled": True,
                        "result_reconciled": True,
                        "descendants": "unproven",
                    }
                else:
                    proof = {
                        "version": 1,
                        "attempt_id": attempt.attempt_id,
                        "operation_id": runtime,
                        "handle": handle,
                        "runtime_fence": runtime_fence,
                        "cancellation_fence": fence,
                        "owned_group_reference": group,
                        "expected_group_reference": group,
                        "owned_group_settled": True,
                        "result_reconciled": True,
                        "transport_succeeded": True,
                        "descendants": "unproven",
                    }
                reference = store.finalize_artifact(cancellation, "owned-group-settlement", json.dumps(proof).encode())
                store.observe(cancellation, fence, "cancellation-observation", "accepted", reference=reference)
            else:
                store.observe(cancellation, fence, "cancellation-observation", "unknown")
            before = (store.history(program.program_id), store.operations())
        with SQLiteProgramStore(path) as store:
            exported = export_private_outcome(store, driver, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
            cancellations = cast(list[dict[str, object]], exported["cancellations"])
            if len(cancellations) != 1 or cancellations[0]["operation_status"] != (
                "accepted" if qualified else "unknown"
            ):
                raise SystemExit("installed reopened cancellation status was lost")
            proofs = cast(list[dict[str, object]], cancellations[0]["settlement_proofs"])
            if any(item["qualified_original_group"] is True for item in proofs) is not qualified:
                raise SystemExit("installed cancellation changed original group qualification")
            if cancellations[0]["current_activity"] is not None or "synthetic-private" in json.dumps(exported):
                raise SystemExit("installed cancellation disclosed private evidence or asserted current state")
            if (store.history(program.program_id), store.operations()) != before:
                raise SystemExit("installed cancellation export changed source evidence")


class SyntheticOriginalConnection:
    """Offline source-shaped native connection; no model or external service."""

    version = "1.2.3"
    methods = frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"})

    def __init__(self, directory: Path, *, qualified: bool) -> None:
        self.directory = directory
        self.qualified = qualified
        self.retired = False
        self.starts = 0
        self.interrupts = 0
        self.cwd: str | None = None

    @property
    def owned_group_reference(self) -> str:
        return "synthetic-private-original-group"

    @property
    def owned_group_settled(self) -> bool:
        return self.qualified and self.retired

    def close(self) -> None:
        self.retired = True

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "thread/start":
            self.starts += 1
            self.cwd = str(params["cwd"])
            return {"thread": {"id": "synthetic-thread"}, "model": "model", "modelProvider": "provider"}
        if method == "turn/start":
            if self.cwd is None:
                raise AssertionError("synthetic worker was not started")
            (Path(self.cwd) / "result.txt").write_text("synthetic candidate")
            return {"turn": {"id": "synthetic-turn", "status": "inProgress"}}
        if method == "thread/read":
            status = "interrupted" if self.qualified and self.retired else "inProgress"
            return {"thread": {"id": params["threadId"], "turns": [{"id": "synthetic-turn", "status": status}]}}
        if method == "turn/interrupt":
            self.interrupts += 1
            return {}
        raise AssertionError("unexpected synthetic method")


for qualified in (False, True):
    source = storage / f"actual-reference-{qualified}"
    source.mkdir(mode=0o700)
    objects = source / "objects.git"
    objects.mkdir()
    reference_git(objects, "init", "--bare")
    repository = Reference("forgejo:synthetic/reference")
    transport = SyntheticForgeTransport(repository)
    transport.branches["develop"] = reference_commit(objects, b"")
    forge = ForgejoForge(transport, transport, lambda _: True)
    connection = SyntheticOriginalConnection(source, qualified=qualified)
    with patch("creatidy_kernel.adapters.reference_live.time.time", return_value=100):
        run_live_reference(
            source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            allocation=Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner"),
            forge=forge,
            repository=repository,
            object_source=objects,
            deadline=110,
            max_observations=1,
            boot_clock=lambda: BootClockReading(100, 1_000_000_000, "synthetic-boot"),
        )
    before_counts = (connection.starts, connection.interrupts)
    with SQLiteProgramStore(source / "kernel.sqlite3") as store:
        before = store.operations()
        exported = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
        records = cast(list[dict[str, object]], exported["cancellations"])
        if (
            len(records) != 1
            or any(
                item["qualified_original_group"] is True
                for item in cast(list[dict[str, object]], records[0]["settlement_proofs"])
            )
            is not qualified
        ):
            raise SystemExit("installed reference producer cancellation was not preserved exactly")
        if records[0]["current_activity"] is not None or store.operations() != before:
            raise SystemExit("installed reference export inferred current activity or retried cancellation")
    if (connection.starts, connection.interrupts) != before_counts:
        raise SystemExit("installed reference export touched the original native connection")
task_source_module = ast.parse((root / "tests/test_task_execution.py").read_text())


def task_literal(name: str) -> str:
    value = next(
        (
            ast.literal_eval(item.value)
            for item in task_source_module.body
            if isinstance(item, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in item.targets)
        ),
        None,
    )
    if type(value) is not str:
        raise ValueError("synthetic bounded-task source fixture is unavailable")
    return value


baseline, racy_block, fixed_block = (task_literal(name) for name in ("BASE_TEST_FILE", "RACY_BLOCK", "DEFLAKED_BLOCK"))


def synthetic_task_source(label: str, task_id: str) -> tuple[Path, Path, TaskSpec]:
    task_root = storage / label
    task_root.mkdir(mode=0o700)
    source = task_root / "source"
    source.mkdir(mode=0o700)
    reference_git(source, "init", "--initial-branch=develop")
    reference_git(source, "remote", "add", "origin", "https://forge.invalid/synthetic/task")
    (source / "tests").mkdir()
    (source / "tests" / "__init__.py").write_text("")
    (source / "tests" / "test_e2e_execution.py").write_text(baseline)
    (source / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    reference_git(source, "add", "tests", ".gitignore")
    reference_git(
        source, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-m", "fixture"
    )
    task = TaskSpec(
        task_id=task_id,
        forge_repository="forgejo:synthetic/task",
        repository_url="https://forge.invalid/synthetic/task",
        base_branch="develop",
        instruction="synthetic local change",
        repository_instructions="synthetic only",
        allowed_paths=frozenset({"tests/test_e2e_execution.py"}),
        verification=(
            VerificationCommand((sys.executable, "-m", "unittest", "tests.test_e2e_execution.CancellationTests"), 5),
        ),
        structural=scarcity_router_143_task().structural,
    )
    return task_root, source, task


for qualified in (False, True):
    root, source, task = synthetic_task_source(f"actual-task-{qualified}", "synthetic-task")
    connection = SyntheticOriginalConnection(root, qualified=qualified)
    control = root / "control"

    def clock() -> BootClockReading:
        return BootClockReading(100, 1_000_000_000, "synthetic-task-boot")

    allocation = Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner")
    with patch("creatidy_kernel.adapters.task_execution.time.time", return_value=100):
        first = run_task(
            control,
            task=task,
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=110,
            allocation=allocation,
            boot_clock=clock,
        )
        if first["condition"] != "running":
            raise SystemExit("installed synthetic task failed to retain the original running attempt")
        run_task(
            control,
            task=task,
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=110,
            allocation=allocation,
            boot_clock=clock,
            cancel_requested=True,
        )
    calls = (connection.starts, connection.interrupts)
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        before = store.operations()
        exported = export_private_outcome(
            store, "task_execution", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        cancellations = cast(list[dict[str, object]], exported["cancellations"])
        if (
            len(cancellations) != 1
            or any(
                item["qualified_original_group"] is True
                for item in cast(list[dict[str, object]], cancellations[0]["settlement_proofs"])
            )
            is not qualified
        ):
            raise SystemExit("installed task producer cancellation did not retain its original qualification")
        if cancellations[0]["current_activity"] is not None or store.operations() != before:
            raise SystemExit("installed task outcome inferred current state or dispatched work")
    if (connection.starts, connection.interrupts) != calls:
        raise SystemExit("installed task export touched the native connection")


def pre_attempt_clock() -> BootClockReading:
    return BootClockReading(100, 1_000_000_000, "synthetic-task-boot")


for before_allocation, qualified in itertools.product((False, True), repeat=2):
    root, source, task = synthetic_task_source(
        f"actual-task-pre-attempt-{before_allocation}-{qualified}", "synthetic-pre-attempt"
    )
    requested = [before_allocation]
    connection = SyntheticOriginalConnection(root, qualified=qualified)
    control = root / "control"
    allocation = Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner")

    class StopAllocator:
        def select(
            self, request: object, flag: list[bool] = requested, selected: Allocation = allocation
        ) -> Allocation:
            del request
            flag[0] = True
            return selected

    with patch("creatidy_kernel.adapters.task_execution.time.time", return_value=100):
        stopped = run_task(
            control,
            task=task,
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=110,
            allocation=None,
            allocator=StopAllocator(),
            boot_clock=pre_attempt_clock,
            cancellation_requested=lambda flag=requested: flag[0],
        )
    if stopped["condition"] not in {"cancelled_no_dispatch", "cancel_uncertain"} or connection.starts != 0:
        raise SystemExit("installed task did not preserve pre-Attempt cancellation")
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        before = store.operations()
        exported = export_private_outcome(
            store, "task_execution", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        cancellations = cast(list[dict[str, object]], exported["cancellations"])
        if (
            exported["attempts"]
            or len(cancellations) != 1
            or cancellations[0]["attempt"] is not None
            or cancellations[0]["runtime_operation_recorded"] is not False
            or cancellations[0]["no_dispatch_observed"] is not True
            or cancellations[0]["settlement_recorded"] is not True
            or cancellations[0]["qualified_original_no_dispatch_group"] is not qualified
            or store.operations() != before
        ):
            raise SystemExit("installed reopened outcome invented an Attempt or lost pre-dispatch cancellation")

root, source, task = synthetic_task_source("actual-task-pre-attempt-crash", "synthetic-pre-attempt-crash")
connection = SyntheticOriginalConnection(root, qualified=True)
control = root / "control"
allocation = Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner")
with patch("creatidy_kernel.adapters.task_execution.time.time", return_value=100):
    try:
        run_task(
            control,
            task=task,
            source_repository=source,
            owner_approved=True,
            trusted_development_acknowledged=True,
            connection=connection,
            version=connection.version,
            deadline=110,
            allocation=allocation,
            boot_clock=pre_attempt_clock,
            cancel_requested=True,
            fault="cancel-claim",
        )
    except TaskInterrupted:
        pass
    else:
        raise SystemExit("installed pre-Attempt cancellation cut was not reached")
with SQLiteProgramStore(control / "kernel.sqlite3") as store:
    before = store.operations()
    exported = export_private_outcome(store, "task_execution", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    cancellations = cast(list[dict[str, object]], exported["cancellations"])
    if (
        exported["attempts"]
        or len(cancellations) != 1
        or cancellations[0]["operation_status"] != "dispatched"
        or cancellations[0]["settlement_recorded"] is not False
        or cancellations[0]["qualified_original_no_dispatch_group"] is not None
        or store.operations() != before
        or connection.starts != 0
    ):
        raise SystemExit("installed pre-Attempt crash changed or fabricated original cancellation evidence")


class SyntheticCompletingTaskConnection(SyntheticOriginalConnection):
    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "turn/start":
            if self.cwd is None:
                raise AssertionError("synthetic worker was not started")
            target = Path(self.cwd) / "tests/test_e2e_execution.py"
            original = target.read_text()
            if racy_block not in original:
                raise AssertionError("synthetic baseline differs from frozen task")
            target.write_text(original.replace(racy_block, fixed_block))
            return {"turn": {"id": "synthetic-turn", "status": "completed"}}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": [{"id": "synthetic-turn", "status": "completed"}]}}
        return super().request(method, params)


for fault in (None, "pr-send"):
    root, source, task = synthetic_task_source(
        "actual-task-publication" if fault is None else "actual-task-unknown-publication", "synthetic-publication"
    )
    transport = SyntheticForgeTransport(Reference(task.forge_repository))
    transport.branches["develop"] = reference_git(source, "rev-parse", "refs/heads/develop")
    forge = ForgejoForge(transport, transport, lambda _: True)
    connection = SyntheticCompletingTaskConnection(root, qualified=True)
    control = root / "control"
    allocation = Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner")

    def task_clock() -> BootClockReading:
        return BootClockReading(100, 1_000_000_000, "synthetic-task-boot")

    with patch("creatidy_kernel.adapters.task_execution.time.time", return_value=100):
        try:
            completed = run_task(
                control,
                task=task,
                source_repository=source,
                owner_approved=True,
                trusted_development_acknowledged=True,
                connection=connection,
                version=connection.version,
                deadline=110,
                allocation=allocation,
                boot_clock=task_clock,
                forge_factory=lambda _directory, original=forge: original,
                fault=fault,
            )
        except TaskInterrupted:
            if fault != "pr-send":
                raise
        else:
            if fault is not None or completed["condition"] != "accepted":
                raise SystemExit(
                    f"installed synthetic task publication condition: {completed.get('condition')}; "
                    f"synthetic verification: {completed.get('verification')!r}"
                )
    before_calls = (connection.starts, connection.interrupts, transport.pushes, transport.posts)
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        before = store.operations()
        exported = export_private_outcome(
            store, "task_execution", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        native = cast(list[dict[str, object]], exported["native_effects"])
        if len(native) != 1 or native[0]["driver"] != "task-pr-v1" or native[0]["current_remote_state"] is not None:
            raise SystemExit("installed task publication lineage was lost or treated as current remote state")
        if native[0]["receipt_recorded"] is not (fault is None) or store.operations() != before:
            raise SystemExit("installed task publication guessed a receipt or changed original evidence")
    if (connection.starts, connection.interrupts, transport.pushes, transport.posts) != before_calls:
        raise SystemExit("installed task publication export reissued a native effect")

for cut in ("before-output", "after-collection", "after-collection-stop"):
    root, source, task = synthetic_task_source(f"actual-task-candidate-{cut}", "synthetic-candidate-cut")
    connection = SyntheticCompletingTaskConnection(root, qualified=True)
    control = root / "control"
    allocation = Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner")
    original_collect = TaskCollector.collect
    original_finalize = SQLiteProgramStore.finalize_artifact
    captured_candidates: list[Candidate] = []

    def interrupt_collect(
        target: TaskCollector,
        request: ExecutionRequest,
        candidate: Candidate,
        *,
        now: int,
        original: Callable[..., Collection] = original_collect,
        captured: list[Candidate] = captured_candidates,
    ) -> NoReturn:
        original(target, request, candidate, now=now)
        captured.append(candidate)
        raise TaskInterrupted("synthetic after-collection cut")

    def interrupt_output(
        store: SQLiteProgramStore,
        operation_id: str,
        name: str,
        data: bytes,
        original: Callable[[SQLiteProgramStore, str, str, bytes], str] = original_finalize,
    ) -> str:
        if operation_id == "runtime:task_execution:change" and name == "output":
            raise TaskInterrupted("synthetic before-output cut")
        return original(store, operation_id, name, data)

    with patch("creatidy_kernel.adapters.task_execution.time.time", return_value=100):
        override = (
            patch.object(TaskCollector, "collect", interrupt_collect)
            if cut != "before-output"
            else patch.object(SQLiteProgramStore, "finalize_artifact", interrupt_output)
        )
        with override:
            try:
                run_task(
                    control,
                    task=task,
                    source_repository=source,
                    owner_approved=True,
                    trusted_development_acknowledged=True,
                    connection=connection,
                    version=connection.version,
                    deadline=110,
                    allocation=allocation,
                    boot_clock=pre_attempt_clock,
                )
            except TaskInterrupted:
                pass
            else:
                raise SystemExit("installed task candidate interruption cut was not reached")
        if cut == "after-collection-stop":
            stopped = run_task(
                control,
                task=task,
                source_repository=source,
                owner_approved=True,
                trusted_development_acknowledged=True,
                connection=connection,
                version=connection.version,
                deadline=110,
                allocation=allocation,
                boot_clock=pre_attempt_clock,
                cancel_requested=True,
            )
            if stopped["condition"] not in {"cancelled", "cancel_uncertain"} or connection.starts != 1:
                raise SystemExit("installed post-collection STOP did not retain the original target")
    calls = (connection.starts, connection.interrupts)
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        before = store.operations()
        exported = export_private_outcome(
            store, "task_execution", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        candidates = cast(list[dict[str, object]], exported["candidates"])
        if (
            len(candidates) != 1
            or candidates[0]["source"] != "task-candidate-v1"
            or candidates[0]["head"] is None
            or candidates[0]["base"] is None
            or candidates[0]["tree"] is None
            or candidates[0]["patch_recorded"] is not (cut != "before-output")
            or (candidates[0]["candidate"] is not None) is not (cut != "before-output")
            or exported["checks"]
            or exported["satisfactions"]
            or store.operations() != before
        ):
            raise SystemExit("installed reopened task outcome lost or fabricated original candidate evidence")
        if cut != "before-output":
            if len(captured_candidates) != 1:
                raise SystemExit("installed task fixture lost original producer candidate")
            expected = CandidateResult(
                "candidate:task_execution:change", captured_candidates[0], (("change", "candidate.patch"),)
            ).digest
            message = json.dumps(["candidate", expected], ensure_ascii=True, separators=(",", ":")).encode()
            expected_ref = "hmac-sha256:" + hmac.new(b"k" * 32, message, hashlib.sha256).hexdigest()
            if candidates[0]["candidate"] != expected_ref:
                raise SystemExit("installed outcome changed the original task candidate identity")
    if (connection.starts, connection.interrupts) != calls:
        raise SystemExit("installed task candidate export reissued native work")


for failure in (False, True):
    root, source, task = synthetic_task_source(f"command-outcome-{failure}", "synthetic-command-outcome")

    class CommandOutcomeConnection(SyntheticCompletingTaskConnection):
        failure: bool = False

        def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            result = super().request(method, params)
            if self.failure and method == "turn/start" and self.cwd is not None:
                target = Path(self.cwd) / "tests/test_e2e_execution.py"
                target.write_text(
                    target.read_text().replace(
                        "    def test_scenario_11_client_disconnect_propagates_cancel(self):",
                        "    def test_extra_failure(self):\n        assert False, 'synthetic-private-output'\n\n"
                        "    def test_scenario_11_client_disconnect_propagates_cancel(self):",
                    )
                )
            return result

    connection = CommandOutcomeConnection(root, qualified=True)
    connection.failure = failure
    control = root / "control"
    allocation = Allocation("codex", "provider", "model", frozenset({"reference"}), 128, "synthetic owner")
    with patch("creatidy_kernel.adapters.task_execution.time.time", return_value=100):
        try:
            run_task(
                control,
                task=task,
                source_repository=source,
                owner_approved=True,
                trusted_development_acknowledged=True,
                connection=connection,
                version=connection.version,
                deadline=110,
                allocation=allocation,
                boot_clock=pre_attempt_clock,
                fault="verification",
            )
        except TaskInterrupted:
            pass
        else:
            raise SystemExit("installed bounded command interruption not reached")
    with SQLiteProgramStore(control / "kernel.sqlite3") as store:
        before = store.served_change_token()
        exported = export_private_outcome(
            store, "task_execution", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        commands = cast(list[dict[str, object]], exported["command_results"])
        if (
            len(commands) != 1
            or (commands[0]["exit_code"] == 0) is failure
            or commands[0]["settlement"] != "witnessed"
            or commands[0]["integrity"] != "recorded"
            or exported["satisfactions"]
            or store.served_change_token() != before
            or "synthetic-private-output" in json.dumps(exported)
        ):
            raise SystemExit("installed bounded command evidence lost or disclosed")


class SyntheticReviewRuntime:
    request_value: ExecutionRequest | None = None

    def start(self, request: ExecutionRequest) -> str:
        self.request_value = request
        return "synthetic-review-runtime"

    def observe(self, handle: str, *, now: int) -> RuntimeObservation:
        if self.request_value is None:
            raise ValueError("synthetic request is missing")
        identity = replace(
            self.request_value.identity,
            resolved=self.request_value.identity.requested,
            observed=self.request_value.identity.requested,
            resolved_provider=self.request_value.identity.requested_provider,
            observed_provider=self.request_value.identity.requested_provider,
        )
        return RuntimeObservation(handle, Activity.TERMINAL, now, now + 1000, identity, False)

    def reconcile(self, operation: OperationKey, handle: str | None = None) -> Lookup:
        del operation
        return Lookup(Presence.FOUND, handle or "synthetic-review-runtime")

    def candidate(self, handle: str) -> Candidate:
        del handle
        if self.request_value is None:
            raise ValueError("synthetic request is missing")
        return Candidate(
            self.request_value.attempt.attempt_id,
            self.request_value.attempt.digest,
            ArtifactManifest(self.request_value.workspace.key, (Artifact("result.txt", "sha256:" + "0" * 64),)),
        )

    def cancel(self, handle: str) -> RuntimeObservation:
        return self.observe(handle, now=10)


class SyntheticExpiringCheck:
    def check(self, name: str, subject: EvidenceSubject) -> Evidence:
        return Evidence("synthetic-check", name, subject, "synthetic-checker", "v1", "sha256:check", 10, 100, True)


class SyntheticReviewCollector:
    def collect(self, request: ExecutionRequest, candidate: Candidate, *, now: int) -> Collection:
        del request, now
        return Collection(
            candidate.artifacts, "synthetic-repository", "synthetic-base", "synthetic-head", SyntheticExpiringCheck()
        )


class SyntheticReviewAllocator:
    def select(self, request: ResourceRequest) -> Allocation:
        del request
        return Allocation("fixture", "provider", "model", frozenset({"reference"}), 128, "synthetic")


review_clock = [10]


class SyntheticApprovingReview:
    def dispatch(self, operation: OperationKey, request: ProductReviewRequest, *, fence: int) -> ProductReviewReceipt:
        if not operation.operation_id.startswith("product-review:") or fence < 1:
            raise ValueError("invalid synthetic review operation")
        review_clock[0] = 101
        return ProductReviewReceipt(
            "synthetic-review",
            request.input_digest,
            request.subject,
            "independent-reviewer",
            "cold-synthetic-session",
            ProductReviewVerdict.APPROVE,
            (),
            10,
            1000,
        )

    def reconcile(self, operation: OperationKey, request_digest: str) -> ReviewLookup:
        del operation, request_digest
        return ReviewLookup(ReviewLookupStatus.ABSENT)


stale_review_path = storage / "approved-review-stale-check.sqlite3"
with SQLiteProgramStore(
    stale_review_path, boot_clock=lambda: BootClockReading(10, 1_000_000_000, "synthetic-review-boot")
) as store:
    program = store.create(reference_spec(), "create-stale-review")
    budget = WorkBudgetPolicy.basic(3, 1, verifications=6, retained_bytes=65536)
    store.bind_work_budget(program.program_id, "synthetic-review-project", budget, budget, 110)
    program = store.admit(program.program_id, "activate-stale-review", ActivateProgram(program.revision, "owner"))
    workspace = WorkspaceHandle(
        "workspace:first",
        WorkspaceSpec("fixture", "base-revision", (), "tools:v1", "synthetic", TrustMode.TRUSTED_DEVELOPMENT),
    )
    outcome = advance_work_unit(
        store,
        program,
        "first",
        allocator=SyntheticReviewAllocator(),
        runtime=SyntheticReviewRuntime(),
        workspace=workspace,
        collector=SyntheticReviewCollector(),
        policy=VerificationPolicy(REFERENCE_POLICY.reference, ("exact-content",), True),
        now=10,
        clock=lambda: review_clock[0],
        product_reviewer=SyntheticApprovingReview(),
    )
    if outcome != "rejected" or store.load(program.program_id).satisfactions:
        raise SystemExit("installed synthetic stale prerequisite did not reject the candidate")
with SQLiteProgramStore(
    stale_review_path, boot_clock=lambda: BootClockReading(10, 1_000_000_000, "synthetic-review-boot")
) as store:
    exported = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
    reviews = cast(list[dict[str, object]], exported["reviews"])
    decisions = cast(list[dict[str, object]], exported["decisions"])
    if (
        len(reviews) != 1
        or reviews[0]["verdict"] != "approve"
        or len(decisions) != 1
        or decisions[0]["review_status"] != "approved"
        or exported["satisfactions"]
    ):
        raise SystemExit("installed outcome conflated historical approval with rejected candidate acceptance")
print(
    "Installed private outcome SDK: correction, unknown coverage, private custody and interrupted publication passed."
)
