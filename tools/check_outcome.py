# SPDX-License-Identifier: Apache-2.0
"""Installed SDK conformance for private durable outcome export."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

import creatidy_kernel
from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.outcome_file import write_private_outcome
from creatidy_kernel.adapters.reference import reference_commit, reference_git, reference_spec, run_reference
from creatidy_kernel.adapters.reference_live import run_live_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import TaskSpec, VerificationCommand, run_task, scarcity_router_143_task
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt, ProgramSpec
from creatidy_kernel.core.execution import RuntimeIdentity
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.outcome_export import PRIVATE_METADATA, export_private_outcome

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
baseline = next(
    (
        ast.literal_eval(item.value)
        for item in task_source_module.body
        if isinstance(item, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "BASE_TEST_FILE" for target in item.targets)
    ),
    None,
)
if type(baseline) is not str:
    raise SystemExit("synthetic bounded-task baseline is unavailable")
for qualified in (False, True):
    root = storage / f"actual-task-{qualified}"
    root.mkdir(mode=0o700)
    source = root / "source"
    source.mkdir(mode=0o700)
    reference_git(source, "init", "--initial-branch=develop")
    reference_git(source, "remote", "add", "origin", "https://forge.invalid/synthetic/task")
    (source / "tests").mkdir()
    (source / "tests" / "__init__.py").write_text("")
    (source / "tests" / "test_e2e_execution.py").write_text(baseline)
    reference_git(source, "add", "tests")
    reference_git(
        source, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-m", "fixture"
    )
    task = TaskSpec(
        task_id="synthetic-task",
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
print(
    "Installed private outcome SDK: correction, unknown coverage, private custody and interrupted publication passed."
)
