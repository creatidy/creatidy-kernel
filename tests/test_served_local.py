# SPDX-License-Identifier: Apache-2.0
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
from unittest.mock import Mock

import pytest
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path
from test_work_budget import Clock, initialize, policy
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
from creatidy_kernel.core.domain import ActivateProgram
from creatidy_kernel.core.intake import encode
from creatidy_kernel.core.served import command_prefix
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
            decisions=({"source": "product-review", "attempt": "a", "status": "owner_decision_needed"},),
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
            notifier.publish(replace(snapshot, decisions=({"status": "comment", "attempt": "b"},)))
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
