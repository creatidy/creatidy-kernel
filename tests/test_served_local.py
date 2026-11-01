# SPDX-License-Identifier: Apache-2.0
import base64
import json
import os
import select
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path
from test_work_budget import Clock, initialize, policy, prepare
from test_work_ownership import claim, client, workspace

from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.served_local import (
    LocalChannelRefused,
    LocalDecisionNotifier,
    LocalReadChannel,
    LocalWorkerChannel,
    read_frame,
    write_frame,
)
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore, WrongWriterThread
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AuthorityEnvelope,
    BudgetPolicy,
    CancelProgram,
    ProgramSpec,
    WorkUnit,
)
from creatidy_kernel.core.intake import encode
from creatidy_kernel.core.ownership import WorkClaim
from creatidy_kernel.core.served import command_prefix
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.ordinary import OrdinaryWorkClient
from creatidy_kernel.ports.served import OwnerProjection


def test_preconnected_descriptor_serves_without_second_reader_or_mutation(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "local.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        owner, observer = socket.socketpair()
        with owner, observer:
            server = LocalReadChannel(OwnerProjection(store, program.program_id), owner)
            write_frame(observer, b'{"version":1,"action":"snapshot"}', bound=65536)
            server.serve_once()
            reply = json.loads(read_frame(observer, bound=1024 * 1024))
            assert reply["ok"] is True
            assert reply["result"]["scope"] == program.program_id
            assert reply["result"]["physical_settlement"] is None
            assert (store.history(program.program_id), store.operations()) == before
            store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
            request = {"version": 1, "action": "events", "after": reply["result"]["position"], "limit": 1}
            write_frame(observer, json.dumps(request).encode(), bound=65536)
            server.serve_once()
            batch = json.loads(read_frame(observer, bound=1024 * 1024))["result"]
            assert batch["gap"] is True
            assert batch["events"][0]["kind"] == "resync-required"


def test_payload_cannot_claim_owner_identity_or_submit_command(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "local.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        owner, observer = socket.socketpair()
        with owner, observer:
            server = LocalReadChannel(OwnerProjection(store, program.program_id), owner)
            write_frame(observer, b'{"version":1,"action":"snapshot","role":"owner"}', bound=65536)
            with pytest.raises(LocalChannelRefused):
                server.serve_once()
            assert (store.history(program.program_id), store.operations()) == before


@pytest.mark.parametrize(
    "wire_request",
    [
        b'{"version":true,"action":"snapshot"}',
        b'{"version":1,"action":"snapshot","action":"stop"}',
        b'{"version":1,"action":"stop"}',
        b'{"version":1,"action":"claim","command_id":"x","expected_revision":0,"lease_seconds":1,"remediation_of":null}',
    ],
)
def test_ambiguous_or_mutating_request_is_rejected(sqlite_tmp_path: Path, wire_request: bytes) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "local.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        owner, observer = socket.socketpair()
        with owner, observer:
            server = LocalReadChannel(OwnerProjection(store, program.program_id), owner)
            write_frame(observer, wire_request, bound=65536)
            with pytest.raises(LocalChannelRefused):
                server.serve_once()
        assert (store.history(program.program_id), store.operations()) == before


def test_separate_client_uses_only_inherited_read_capability(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "local.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        owner, observer = socket.socketpair()
        with owner, observer:
            server = LocalReadChannel(OwnerProjection(store, program.program_id), owner)
            environment = {"HOME": "/tmp", "PATH": os.defpath, "LANG": "C.UTF-8"}  # noqa: S108 - Synthetic fixture.
            process = subprocess.Popen(  # noqa: S603 - Fixed interpreter/module and synthetic descriptor capability.
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "creatidy_kernel.adapters.served_client",
                    "--fd",
                    str(observer.fileno()),
                    "snapshot",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(observer.fileno(),),
                env=environment,
            )
            try:
                assert select.select([owner], [], [], 10)[0]
                server.serve_once()
                stdout, stderr = process.communicate(timeout=10)
                assert process.returncode == 0, stderr.decode(errors="replace")
                assert json.loads(stdout)["result"]["scope"] == program.program_id
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        assert (store.history(program.program_id), store.operations()) == before


def test_capability_cannot_read_store_from_non_owner_thread(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "local.db") as store:
        program = store.create(reference_spec(), "create")
        owner, observer = socket.socketpair()
        with owner, observer, ThreadPoolExecutor(max_workers=1) as pool:
            server = LocalReadChannel(OwnerProjection(store, program.program_id), owner)
            write_frame(observer, b'{"version":1,"action":"snapshot"}', bound=65536)
            with pytest.raises(WrongWriterThread):
                pool.submit(server.serve_once).result(timeout=5)
            assert observer.recv(1) == b""


def test_host_can_revoke_read_capability_without_mutating_program(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "local.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        owner, observer = socket.socketpair()
        with owner, observer:
            server = LocalReadChannel(OwnerProjection(store, program.program_id), owner)
            server.revoke()
            assert observer.recv(1) == b""
        assert (store.history(program.program_id), store.operations()) == before


def test_decision_notification_is_bounded_deduplicated_and_best_effort(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "notification.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        snapshot = replace(
            OwnerProjection(store, program.program_id).snapshot(),
            decisions=(
                {
                    "source": "product-review",
                    "attempt": "a",
                    "status": "owner_decision_needed",
                    "applicability": "pending",
                },
            ),
        )
        owner, observer = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with owner, observer:
            notifier = LocalDecisionNotifier(owner)
            notifier.publish(snapshot)
            notification = json.loads(observer.recv(65536))
            assert notification["kind"] == "decision-required" and notification["scope"] == program.program_id
            notifier.publish(snapshot)
            assert not select.select([observer], [], [], 0)[0]
            observer.close()
            notifier.publish(
                replace(snapshot, decisions=({"status": "comment", "attempt": "b", "applicability": "pending"},))
            )
            assert notifier.dropped is True
        assert (store.history(program.program_id), store.operations()) == before


def test_claim_command_identity_is_scoped_to_program_and_actor(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "claim-scopes.db", boot_clock=Clock()) as store:
        first = initialize(store, project=policy(active=2))
        second = initialize(store, "program-2", project=policy(active=2))
        qualified: list[str] = []
        for program, actor in ((first, "worker-a"), (second, "worker-a"), (first, "worker-b")):
            handoff = SimpleNamespace(
                attempt=SimpleNamespace(program_id=program.program_id, attempt_id=f"attempt-{actor}"),
                claim=SimpleNamespace(generation=1),
            )
            request_claim = Mock(return_value=handoff)
            worker = cast(
                OrdinaryWorkClient,
                SimpleNamespace(
                    approved=SimpleNamespace(program=program),
                    principal=SimpleNamespace(actor_id=actor),
                    application=SimpleNamespace(store=store, check=Mock()),
                    request=request_claim,
                ),
            )
            owner, peer = socket.socketpair()
            with owner, peer:
                channel = LocalWorkerChannel(OwnerProjection(store, program.program_id), owner, worker)
                wire = encode(
                    {
                        "version": 1,
                        "action": "claim",
                        "command_id": "shared-id",
                        "expected_revision": program.revision,
                        "lease_seconds": 30,
                        "remediation_of": None,
                    }
                )
                write_frame(peer, wire, bound=65536)
                channel.serve_once()
                assert json.loads(read_frame(peer, bound=1024 * 1024))["result"]["status"] == "claimed"
            qualified.append(request_claim.call_args.args[0])
        assert len(set(qualified)) == 3
        assert len(store.operations(command_prefix(first.program_id))) == 2
        assert len(store.operations(command_prefix(second.program_id))) == 1


def test_reconnected_local_observer_receives_expired_authority(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "observer-horizon.db", boot_clock=clock) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "observer-horizon")
        claim(store, client(store, tree), tree)
        projection = OwnerProjection(store, program.program_id)
        observations: list[dict[str, object]] = []
        for reading in (
            BootClockReading(100, 1_000_000_000, "synthetic-boot-a"),
            BootClockReading(110, 11_000_000_000, "synthetic-boot-a"),
        ):
            clock.reading = reading
            owner, observer = socket.socketpair()
            with owner, observer:
                channel = LocalReadChannel(projection, owner)
                write_frame(observer, encode({"version": 1, "action": "snapshot"}), bound=65536)
                channel.serve_once()
                observations.append(json.loads(read_frame(observer, bound=1024 * 1024))["result"])
        first = cast(dict[str, object], observations[0]["authority"])
        last = cast(dict[str, object], observations[1]["authority"])
        assert first["cutoff"] == "not-expired-observed"
        assert last["cutoff"] == "expired"
        assert last["safe_next_action"] == "reconcile-or-stop"
        assert observations[1]["physical_settlement"] is None


def test_large_claim_projection_is_paged_and_oversized_record_chunked(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "large-paged.db", boot_clock=clock) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "large-paged")
        claim(store, client(store, tree), tree)
        original = store.served_claims(program.program_id)[0]
        records = tuple(
            {**original, "claim": f"synthetic-{index:04d}-" + "x" * (12000 if index == 0 else 600)}
            for index in range(2048)
        )
        assert sum(len(encode(item)) for item in records) > 1024 * 1024

        def sample(
            _store: SQLiteProgramStore, _program: str, *, offset: int = 0, limit: int = 8
        ) -> tuple[dict[str, object], ...]:
            return records[offset : offset + limit]

        with (
            patch.object(SQLiteProgramStore, "served_claims", sample),
            patch.object(SQLiteProgramStore, "served_claim_count", return_value=len(records)),
        ):
            projection = OwnerProjection(store, program.program_id)
            owner, peer = socket.socketpair()
            with owner, peer:
                channel = LocalReadChannel(projection, owner)
                write_frame(peer, encode({"version": 1, "action": "snapshot"}), bound=65536)
                channel.serve_once()
                first = json.loads(read_frame(peer, bound=1024 * 1024))["result"]
                assert first["coverage"]["claims"] == [0, 2048, False]
                assert first["claims"][0]["oversized"] is True
                position = first["position"]
                fragments = bytearray()
                offset = 0
                while True:
                    request = {
                        "version": 1,
                        "action": "record",
                        "collection": "claims",
                        "index": 0,
                        "offset": offset,
                        "after": position,
                    }
                    write_frame(peer, encode(request), bound=65536)
                    channel.serve_once()
                    part = json.loads(read_frame(peer, bound=1024 * 1024))["result"]
                    fragments.extend(base64.b64decode(part["data_base64"]))
                    offset = len(fragments)
                    if part["complete"]:
                        break
                assert json.loads(fragments)["claim"] == records[0]["claim"]
                write_frame(peer, encode({"version": 1, "action": "snapshot", "page": 255}), bound=65536)
                channel.serve_once()
                final = json.loads(read_frame(peer, bound=1024 * 1024))["result"]
                assert final["coverage"]["claims"] == [2040, 2048, True]
                store.admit(
                    program.program_id,
                    "cancel-for-pagination",
                    CancelProgram(store.load(program.program_id).revision, "owner"),
                )
                write_frame(
                    peer,
                    encode(
                        {
                            "version": 1,
                            "action": "record",
                            "collection": "claims",
                            "index": 0,
                            "offset": 0,
                            "after": position,
                        }
                    ),
                    bound=65536,
                )
                with pytest.raises(LocalChannelRefused, match="local read refused"):
                    channel.serve_once()


def test_real_claim_history_exceeding_frame_remains_readable(sqlite_tmp_path: Path) -> None:
    clock = Clock()
    with SQLiteProgramStore(sqlite_tmp_path / "real-large-history.db", boot_clock=clock) as store:
        base = reference_spec()
        spec = ProgramSpec(
            "large-real",
            "synthetic large history",
            tuple(WorkUnit(f"unit-{index}", "bounded work", acceptance_criteria=("complete",)) for index in range(9)),
            budget=BudgetPolicy(9, 9),
            authority=AuthorityEnvelope("owner", max_attempts=9, delegated_actor_ids=frozenset({"worker-7"})),
            acceptance_criteria=base.acceptance_criteria,
            policy_references=base.policy_references,
        )
        program = store.create(spec, "create-large")
        budget = WorkBudgetPolicy.basic(9, 9, verifications=9, retained_bytes=1_000_000)
        store.bind_work_budget(program.program_id, "large-project", budget, budget, 110)
        store.admit(program.program_id, "activate-large", ActivateProgram(program.revision, "owner"))
        granted_claims: list[WorkClaim] = []
        for index in range(9):
            tree = workspace(sqlite_tmp_path, f"large-real-{index}")
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
            granted_claims.append(
                client(store, tree).claim(f"claim-{index}-" + "x" * 125000, current.revision, attempt, lease_seconds=5)
            )
        projection = OwnerProjection(store, program.program_id)
        owner, observer = socket.socketpair()
        with owner, observer:
            channel = LocalReadChannel(projection, owner)
            write_frame(observer, encode({"version": 1, "action": "snapshot"}), bound=65536)
            channel.serve_once()
            first = json.loads(read_frame(observer, bound=1024 * 1024))["result"]
            assert first["coverage"]["claims"] == [0, 9, False]
            assert all(item["oversized"] for item in first["claims"])
            write_frame(observer, encode({"version": 1, "action": "snapshot", "page": 1}), bound=65536)
            channel.serve_once()
            final = json.loads(read_frame(observer, bound=1024 * 1024))["result"]
            assert final["coverage"]["claims"] == [8, 9, True]
            assert first["position"] == final["position"]
            later_id = store.served_claims(program.program_id, offset=8)[0]["claim"]
            later = next(item for item in granted_claims if item.command_key == later_id)
            store.quarantine_work_claim(later, "owner", "synthetic later-page uncertainty")
            write_frame(observer, encode({"version": 1, "action": "snapshot", "page": 1}), bound=65536)
            channel.serve_once()
            changed = json.loads(read_frame(observer, bound=1024 * 1024))["result"]
            assert changed["position"] != final["position"]
            stale_owner, stale_peer = socket.socketpair()
            with stale_owner, stale_peer:
                stale_channel = LocalReadChannel(projection, stale_owner)
                write_frame(
                    stale_peer,
                    encode(
                        {
                            "version": 1,
                            "action": "record",
                            "collection": "claims",
                            "index": 8,
                            "offset": 0,
                            "after": final["position"],
                        }
                    ),
                    bound=65536,
                )
                with pytest.raises(LocalChannelRefused):
                    stale_channel.serve_once()
            current = store.load(program.program_id)
            store.admit(program.program_id, "cancel-large", CancelProgram(current.revision, "owner"))
            write_frame(observer, encode({"version": 1, "action": "snapshot", "page": 1}), bound=65536)
            channel.serve_once()
            cancelled = json.loads(read_frame(observer, bound=1024 * 1024))["result"]
            assert cancelled["position"] != changed["position"]


def test_separate_client_reads_position_qualified_record(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "record-client.db") as store:
        program = store.create(reference_spec(), "create")
        projection = OwnerProjection(store, program.program_id)
        cursor = projection.snapshot().position
        owner, peer = socket.socketpair()
        with owner, peer:
            channel = LocalReadChannel(projection, owner)
            process = subprocess.Popen(  # noqa: S603 - Local module and synthetic inherited descriptor.
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "creatidy_kernel.adapters.served_client",
                    "--fd",
                    str(peer.fileno()),
                    "record",
                    "--collection",
                    "work_units",
                    "--index",
                    "0",
                    "--offset",
                    "0",
                    "--producer",
                    cursor.producer,
                    "--scope",
                    cursor.scope,
                    "--sequence",
                    str(cursor.sequence),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(peer.fileno(),),
                env={"HOME": str(sqlite_tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8"},
            )
            try:
                assert select.select([owner], [], [], 10)[0]
                channel.serve_once()
                output, error = process.communicate(timeout=10)
                assert process.returncode == 0, error.decode(errors="replace")
                record = json.loads(output)["result"]
                assert json.loads(base64.b64decode(record["data_base64"]))[0] == "first"
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)


def test_real_long_attempt_and_runtime_handle_are_chunk_readable(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "long-identities.db", boot_clock=Clock()) as store:
        program = initialize(store)
        attempt = "a" * 5000
        prepare(store, program, attempt)
        operation = "runtime:" + attempt
        store.reserve_work(program.program_id, attempt, operation, WorkKind.ATTEMPT)
        store.finalize_artifact(operation, "runtime-receipt", encode({"version": 2, "handle": "h" * 5000}))
        projection = OwnerProjection(store, program.program_id)
        owner, peer = socket.socketpair()
        with owner, peer:
            channel = LocalReadChannel(projection, owner)
            write_frame(peer, encode({"version": 1, "action": "snapshot"}), bound=65536)
            channel.serve_once()
            snapshot = json.loads(read_frame(peer, bound=1024 * 1024))["result"]
            assert snapshot["reservations"][0]["oversized"] is True
            assert snapshot["correlations"][0]["oversized"] is True
            for collection in ("reservations", "correlations"):
                offset, collected = 0, bytearray()
                while True:
                    write_frame(
                        peer,
                        encode(
                            {
                                "version": 1,
                                "action": "record",
                                "collection": collection,
                                "index": 0,
                                "offset": offset,
                                "after": snapshot["position"],
                            }
                        ),
                        bound=65536,
                    )
                    channel.serve_once()
                    result = json.loads(read_frame(peer, bound=1024 * 1024))["result"]
                    collected.extend(base64.b64decode(result["data_base64"]))
                    offset = len(collected)
                    if result["complete"]:
                        break
                decoded = json.loads(collected)
                assert (
                    decoded[1] == operation if collection == "reservations" else decoded["runtime_handle"] == "h" * 5000
                )
