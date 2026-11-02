# SPDX-License-Identifier: Apache-2.0
"""Installed SDK conformance for private durable outcome export."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

import creatidy_kernel
from creatidy_kernel.adapters.outcome_file import write_private_outcome
from creatidy_kernel.adapters.reference import reference_spec, run_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, PrepareAttempt
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
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
    if len(cast(list[object], result["satisfactions"])) != 2 or result["reviews"] != []:
        raise SystemExit("installed reference outcome misrepresented independent review evidence")
    native = cast(list[dict[str, object]], result["native_effects"])
    if len(native) != 1 or native[0]["driver"] != "reference-pr-v1" or native[0]["current_remote_state"] is not None:
        raise SystemExit("installed reference outcome lost historical native publication lineage")
print(
    "Installed private outcome SDK: correction, unknown coverage, private custody and interrupted publication passed."
)
