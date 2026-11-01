# SPDX-License-Identifier: Apache-2.0
"""Exercise the installed local observer without a second SQLite reader."""

import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path
from typing import cast

import creatidy_kernel
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.served_local import LocalReadChannel
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import ActivateProgram
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
print("Installed host-bound local observer reads owner projection without a competing SQLite reader.")
