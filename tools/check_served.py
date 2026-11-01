# SPDX-License-Identifier: Apache-2.0
"""Exercise the installed local observer without a second SQLite reader."""

import base64
import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path
from typing import cast
from unittest.mock import patch

import creatidy_kernel
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.served_local import LocalChannelRefused, LocalReadChannel
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AuthorityEnvelope,
    BudgetPolicy,
    PrepareAttempt,
    ProgramSpec,
    WorkUnit,
)
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, WorkClaim
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.ownership import WorkOwnership
from creatidy_kernel.ports.served import OwnerProjection

ROOT = Path(__file__).resolve().parents[1]
if Path(creatidy_kernel.__file__).resolve().is_relative_to(ROOT):
    raise SystemExit("served conformance must import the installed package")
storage = Path(sys.argv[1])


def request(projection: OwnerProjection, arguments: list[str]) -> dict[str, object]:
    owner, observer = socket.socketpair()
    with owner, observer:
        server = LocalReadChannel(projection, owner)
        process = subprocess.Popen(  # noqa: S603 - Installed CLI and synthetic descriptor only.
            [
                str(Path(sys.executable).with_name("creatidy-kernel-client")),
                "--fd",
                str(observer.fileno()),
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(observer.fileno(),),
            env={"HOME": str(storage), "PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        try:
            if not select.select([owner], [], [], 10)[0]:
                raise RuntimeError("installed observer did not send its request")
            server.serve_once()
            stdout, _stderr = process.communicate(timeout=10)
            if process.returncode != 0:
                raise RuntimeError("installed observer did not receive a valid response")
            return cast(dict[str, object], json.loads(stdout)["result"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


with SQLiteProgramStore(storage / "served.db") as store:
    program = store.create(reference_spec(), "create")
    projection = OwnerProjection(store, program.program_id)
    before = (store.history(program.program_id), store.operations())
    snapshot = request(projection, ["snapshot"])
    if snapshot["scope"] != program.program_id or (store.history(program.program_id), store.operations()) != before:
        raise RuntimeError("installed observer mutated durable authority")
    store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
    after_change = (store.history(program.program_id), store.operations())
    position = cast(dict[str, object], snapshot["position"])
    batch = request(
        projection,
        [
            "events",
            "--producer",
            str(position["producer"]),
            "--scope",
            program.program_id,
            "--sequence",
            str(position["sequence"]),
        ],
    )
    if batch["gap"] is not True or (store.history(program.program_id), store.operations()) != after_change:
        raise RuntimeError("installed observer hid a gap or mutated durable state")
reading = [BootClockReading(100, 1_000_000_000, "installed-synthetic-boot")]
with SQLiteProgramStore(storage / "served-horizon.db", boot_clock=lambda: reading[0]) as store:
    program = store.create(reference_spec(), "create")
    policy = WorkBudgetPolicy.basic(3, 1, verifications=6, retained_bytes=4096)
    store.bind_work_budget(program.program_id, "synthetic-project", policy, policy, 110)
    store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
    projection = OwnerProjection(store, program.program_id)
    original = (store.history(program.program_id), store.operations())
    before = cast(dict[str, object], request(projection, ["snapshot"])["authority"])
    reading[0] = BootClockReading(110, 11_000_000_000, "installed-synthetic-boot")
    after = cast(dict[str, object], request(projection, ["snapshot"])["authority"])
    if (
        before["cutoff"] != "not-expired-observed"
        or after["cutoff"] != "expired"
        or after["safe_next_action"] != "reconcile-or-stop"
        or (store.history(program.program_id), store.operations()) != original
    ):
        raise RuntimeError("installed observer failed to show a display-only expired horizon")
    records: tuple[dict[str, object], ...] = tuple(
        {"claim": f"synthetic-{index:04d}-" + "x" * (12000 if index == 0 else 600), "recorded_expires_at": 105}
        for index in range(2048)
    )

    def sample(
        _store: SQLiteProgramStore, _program: str, *, offset: int = 0, limit: int = 8
    ) -> tuple[dict[str, object], ...]:
        return records[offset : offset + limit]

    with (
        patch.object(SQLiteProgramStore, "served_claims", sample),
        patch.object(SQLiteProgramStore, "served_claim_count", return_value=len(records)),
    ):
        first = request(projection, ["snapshot"])
        coverage = cast(dict[str, object], first["coverage"])
        claims = cast(list[dict[str, object]], first["claims"])
        if coverage["claims"] != [0, 2048, False] or claims[0].get("oversized") is not True:
            raise RuntimeError("installed observer did not bound oversized claim history")
        position = cast(dict[str, object], first["position"])
        recovered = bytearray()
        while True:
            part = request(
                projection,
                [
                    "record",
                    "--collection",
                    "claims",
                    "--index",
                    "0",
                    "--offset",
                    str(len(recovered)),
                    "--producer",
                    str(position["producer"]),
                    "--scope",
                    str(position["scope"]),
                    "--sequence",
                    str(position["sequence"]),
                ],
            )
            recovered.extend(base64.b64decode(cast(str, part["data_base64"])))
            if part["complete"] is True:
                break
        if json.loads(recovered)["claim"] != records[0]["claim"]:
            raise RuntimeError("installed observer could not recover a chunked claim")
        last = request(projection, ["snapshot", "--page", "255"])
        if cast(dict[str, object], last["coverage"])["claims"] != [2040, 2048, True]:
            raise RuntimeError("installed observer did not reach the final claim page")
parent = storage / "real-claim-workspaces"
parent.mkdir(mode=0o700)
with SQLiteProgramStore(storage / "served-real-history.db", boot_clock=lambda: reading[0]) as store:
    reading[0] = BootClockReading(100, 1_000_000_000, "installed-synthetic-boot")
    base = reference_spec()
    spec = ProgramSpec(
        "served-real-history",
        "synthetic large history",
        tuple(WorkUnit(f"unit-{index}", "bounded work", acceptance_criteria=("complete",)) for index in range(9)),
        budget=BudgetPolicy(9, 9),
        authority=AuthorityEnvelope("owner", max_attempts=9, delegated_actor_ids=frozenset({"worker"})),
        acceptance_criteria=base.acceptance_criteria,
        policy_references=base.policy_references,
    )
    program = store.create(spec, "create-real-history")
    budget = WorkBudgetPolicy.basic(9, 9, verifications=9, retained_bytes=1_000_000)
    store.bind_work_budget(program.program_id, "real-history-project", budget, budget, 110)
    store.admit(program.program_id, "activate-real-history", ActivateProgram(program.revision, "owner"))
    actual_claims: list[WorkClaim] = []
    for index in range(9):
        tree = LocalOwnedWorkspace(parent, f"tree-{index}", create=True)
        worker = WorkOwnership(
            store, Principal("worker", "worker"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        current = store.load(program.program_id)
        attempt = AttemptSpec(
            f"attempt-{index}",
            program.program_id,
            f"unit-{index}",
            current.spec.revision,
            current.spec.digest,
            current.resolved_inputs(f"unit-{index}"),
            workspace_reference=str(tree.path),
        )
        actual_claims.append(worker.claim(f"claim-{index}-" + "x" * 125000, current.revision, attempt, lease_seconds=5))
    projection = OwnerProjection(store, program.program_id)
    before = (store.history(program.program_id), store.operations())
    first = request(projection, ["snapshot"])
    last = request(projection, ["snapshot", "--page", "1"])
    if (
        cast(dict[str, object], first["coverage"])["claims"] != [0, 9, False]
        or cast(dict[str, object], last["coverage"])["claims"] != [8, 9, True]
        or first["position"] != last["position"]
        or (store.history(program.program_id), store.operations()) != before
    ):
        raise RuntimeError("installed observer failed to resync real large durable claims")
    position = cast(dict[str, object], first["position"])
    events = request(
        projection,
        [
            "events",
            "--producer",
            str(position["producer"]),
            "--scope",
            str(position["scope"]),
            "--sequence",
            str(position["sequence"]),
        ],
    )
    if events["gap"] is not True:
        raise RuntimeError("installed observer hid incomplete real claim history")
    recovered = bytearray()
    while True:
        part = request(
            projection,
            [
                "record",
                "--collection",
                "claims",
                "--index",
                "0",
                "--offset",
                str(len(recovered)),
                "--producer",
                str(position["producer"]),
                "--scope",
                str(position["scope"]),
                "--sequence",
                str(position["sequence"]),
            ],
        )
        recovered.extend(base64.b64decode(cast(str, part["data_base64"])))
        if part["complete"] is True:
            break
    if not cast(str, json.loads(recovered)["claim"]).startswith("claim-"):
        raise RuntimeError("installed observer lost the original real claim identity")
    if (store.history(program.program_id), store.operations()) != before:
        raise RuntimeError("installed resync or detail reads mutated durable authority")
    later_id = store.served_claims(program.program_id, offset=8)[0]["claim"]
    later = next(item for item in actual_claims if item.command_key == later_id)
    store.quarantine_work_claim(later, "owner", "synthetic later-page uncertainty")
    changed = request(projection, ["snapshot", "--page", "1"])
    if changed["position"] == last["position"]:
        raise RuntimeError("installed observer failed to detect later-page claim mutation")
    try:
        request(
            projection,
            [
                "record",
                "--collection",
                "claims",
                "--index",
                "8",
                "--offset",
                "0",
                "--producer",
                str(position["producer"]),
                "--scope",
                str(position["scope"]),
                "--sequence",
                str(position["sequence"]),
            ],
        )
    except LocalChannelRefused:
        pass
    else:
        raise RuntimeError("installed observer accepted a stale later-page detail cursor")
with SQLiteProgramStore(storage / "served-long-identities.db", boot_clock=lambda: reading[0]) as store:
    program = store.create(reference_spec(), "create-long")
    budget = WorkBudgetPolicy.basic(3, 1, verifications=6, retained_bytes=16384)
    store.bind_work_budget(program.program_id, "long-project", budget, budget, 110)
    program = store.admit(program.program_id, "activate-long", ActivateProgram(program.revision, "owner"))
    attempt_id = "a" * 5000
    attempt = AttemptSpec(
        attempt_id,
        program.program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
    )
    operation = "runtime:" + attempt_id
    store.admit_with_intent(
        program.program_id,
        "prepare-long",
        PrepareAttempt(program.revision, "worker", attempt),
        operation,
        operation,
        {"attempt": attempt.digest},
    )
    store.reserve_work(program.program_id, attempt_id, operation, WorkKind.ATTEMPT)
    store.finalize_artifact(operation, "runtime-receipt", json.dumps({"version": 2, "handle": "h" * 5000}).encode())
    projection = OwnerProjection(store, program.program_id)
    first = request(projection, ["snapshot"])
    position = cast(dict[str, object], first["position"])
    for collection in ("reservations", "correlations"):
        collection_records = cast(list[dict[str, object]], first[collection])
        if collection_records[0].get("oversized") is not True:
            raise RuntimeError("installed observer rejected or omitted a long producer identity")
        recovered = bytearray()
        while True:
            part = request(
                projection,
                [
                    "record",
                    "--collection",
                    collection,
                    "--index",
                    "0",
                    "--offset",
                    str(len(recovered)),
                    "--producer",
                    str(position["producer"]),
                    "--scope",
                    str(position["scope"]),
                    "--sequence",
                    str(position["sequence"]),
                ],
            )
            recovered.extend(base64.b64decode(cast(str, part["data_base64"])))
            if part["complete"] is True:
                break
        record = json.loads(recovered)
        if (record[1] if collection == "reservations" else record["runtime_handle"]) != (
            operation if collection == "reservations" else "h" * 5000
        ):
            raise RuntimeError("installed observer changed a long producer identity")
with SQLiteProgramStore(storage / "served-long-scopes.db") as store:
    base = reference_spec()
    for length in (300, 10000):
        program_id = "program-" + "p" * length
        spec = ProgramSpec(
            program_id,
            base.objective,
            base.work_units,
            base.initial_inputs,
            base.budget,
            base.authority,
            acceptance_criteria=base.acceptance_criteria,
            policy_references=base.policy_references,
        )
        store.create(spec, "create-" + str(length))
        projection = OwnerProjection(store, program_id)
        snapshot = request(projection, ["snapshot"])
        position = cast(dict[str, object], snapshot["position"])
        if length == 300:
            if snapshot["scope"] != program_id or position["scope"] != program_id:
                raise RuntimeError("installed observer changed a valid long Program identity")
        else:
            if cast(dict[str, object], snapshot["scope"]).get("oversized") is not True:
                raise RuntimeError("installed observer did not bound an oversized Program identity")
            recovered = bytearray()
            while True:
                part = request(
                    projection,
                    [
                        "record",
                        "--collection",
                        "scope",
                        "--index",
                        "0",
                        "--offset",
                        str(len(recovered)),
                        "--producer",
                        str(position["producer"]),
                        "--scope",
                        str(position["scope"]),
                        "--sequence",
                        str(position["sequence"]),
                    ],
                )
                recovered.extend(base64.b64decode(cast(str, part["data_base64"])))
                if part["complete"] is True:
                    break
            if json.loads(recovered) != program_id:
                raise RuntimeError("installed observer failed to recover exact Program identity")
        events = request(
            projection,
            [
                "events",
                "--producer",
                str(position["producer"]),
                "--scope",
                str(position["scope"]),
                "--sequence",
                str(position["sequence"]),
            ],
        )
        if events["gap"] is not False:
            raise RuntimeError("installed observer failed to reconnect the original Program scope")
print("Installed host-bound local observer reads owner projection without a competing SQLite reader.")
