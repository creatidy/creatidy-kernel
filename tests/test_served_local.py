# SPDX-License-Identifier: Apache-2.0
import json
import os
import select
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path

from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.served_local import LocalChannelRefused, LocalReadChannel, read_frame, write_frame
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore, WrongWriterThread
from creatidy_kernel.core.domain import ActivateProgram
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
