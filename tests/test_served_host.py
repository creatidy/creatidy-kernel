# SPDX-License-Identifier: Apache-2.0
import json
import os
import select
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path

from creatidy_kernel.adapters.ordinary_managed import OrdinaryManagedRun
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.served_host import PROVISIONING, LaunchedOwner, launch_owner, receive_owner
from creatidy_kernel.adapters.served_local import (
    LocalChannelRefused,
    LocalOwnerChannel,
    LocalOwnerSession,
    read_frame,
    write_frame,
)
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode
from creatidy_kernel.ports.ordinary import OrdinaryApproval, OrdinaryHandoff, OrdinaryProgram
from creatidy_kernel.ports.ownership import OwnedWorkspace, WorkOwnership
from creatidy_kernel.ports.served import OwnerProjection


def test_launched_consumer_receives_and_uses_owner_descriptor(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "provision.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        script = (
            "import json,sys; from creatidy_kernel.adapters.served_host import receive_owner; "
            "from creatidy_kernel.adapters.served_local import write_frame,read_frame; "
            "s=receive_owner(int(sys.argv[-1]),expected_scope=sys.argv[1],"
            "expected_producer='producer-a',expected_recipient='backend-a'); "
            'write_frame(s,b\'{"version":1,"action":"snapshot"}\',bound=65536); '
            "r=json.loads(read_frame(s,bound=1048576)); "
            "assert r['result']['scope']==sys.argv[1]; s.close()"
        )
        launched = launch_owner(
            session,
            [sys.executable, "-I", "-c", script, program.program_id],
            environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8"},
            scope=program.program_id,
            producer="producer-a",
            recipient="backend-a",
        )
        try:
            assert select.select([launched.channel.fileno()], [], [], 10)[0]
            launched.channel.serve_once()
            assert launched.process.wait(timeout=10) == 0
        finally:
            launched.close()
            if launched.process.poll() is None:
                launched.process.kill()
                launched.process.wait(timeout=10)
        assert (store.history(program.program_id), store.operations()) == before


def test_duplicated_bearer_works_until_session_revocation(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "stolen-bearer.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        server, peer = socket.socketpair()
        with peer:
            channel = session.attach(server)
            write_frame(
                server,
                json.dumps(
                    {
                        "contract": PROVISIONING,
                        "wire_version": 1,
                        "role": "owner",
                        "scope": program.program_id,
                        "producer": "producer-a",
                        "recipient": "backend-a",
                    }
                ).encode(),
                bound=65536,
            )
            with receive_owner(
                peer.detach(),
                expected_scope=program.program_id,
                expected_producer="producer-a",
                expected_recipient="backend-a",
            ) as original:
                with original.dup() as stolen:
                    write_frame(stolen, b'{"version":1,"action":"snapshot"}', bound=65536)
                    channel.serve_once()
                    assert json.loads(read_frame(stolen, bound=1048576))["result"]["scope"] == program.program_id
                    session.revoke()
                    for descriptor in (original, stolen):
                        descriptor.settimeout(5)
                        try:
                            assert descriptor.recv(1) == b""
                        except ConnectionResetError:
                            pass
        assert (store.history(program.program_id), store.operations()) == before


@pytest.mark.parametrize("mismatch", ["projection", "control", "worker", "managed-control", "workspace"])
def test_owner_refuses_same_scope_cross_store_compositions(sqlite_tmp_path: Path, mismatch: str) -> None:
    with (
        SQLiteProgramStore(sqlite_tmp_path / "one.db") as store,
        SQLiteProgramStore(sqlite_tmp_path / "two.db") as other,
    ):
        program = store.create(reference_spec(), "create")
        other.create(reference_spec(), "create")
        before = (
            store.history(program.program_id),
            other.history(program.program_id),
            store.operations(),
            other.operations(),
        )
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))

        def bind_authority(_: str) -> None:
            pass

        workspace = cast(OwnedWorkspace, SimpleNamespace(resource="synthetic", bind_authority=bind_authority))
        owner_control = WorkOwnership(
            store, Principal("owner", "owner"), workspace, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        wrong_control = WorkOwnership(
            other, Principal("owner", "owner"), workspace, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
        )
        handoff = cast(OrdinaryHandoff, SimpleNamespace(attempt=SimpleNamespace(program_id=program.program_id)))
        control = wrong_control if mismatch == "control" else owner_control
        managed = cast(
            OrdinaryManagedRun,
            SimpleNamespace(
                handoff=None,
                client=SimpleNamespace(
                    application=app,
                    worker=wrong_control if mismatch == "worker" else owner_control,
                    control=wrong_control if mismatch == "managed-control" else owner_control,
                ),
                workspace=SimpleNamespace(store=other if mismatch == "workspace" else store),
            ),
        )
        projection = OwnerProjection(other if mismatch == "projection" else store, program.program_id)
        with pytest.raises(LocalChannelRefused):
            LocalOwnerSession(projection, app, approval, control=control, handoff=handoff, managed=managed)
        left, right = socket.socketpair()
        with left, right, pytest.raises(LocalChannelRefused):
            LocalOwnerChannel(projection, left, app, approval, control=control, handoff=handoff, managed=managed)
        assert (
            store.history(program.program_id),
            other.history(program.program_id),
            store.operations(),
            other.operations(),
        ) == before


def test_host_and_backend_do_not_inherit_unrelated_authority(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREATIDY_SYNTHETIC_AMBIENT_SENTINEL", "synthetic-only")
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    try:
        with SQLiteProgramStore(sqlite_tmp_path / "inheritance.db") as store:
            program = store.create(reference_spec(), "create")
            app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
            approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
            session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
            script = textwrap.dedent(
                """\
                import os, subprocess, sys
                from creatidy_kernel.adapters.served_host import receive_owner
                assert 'CREATIDY_SYNTHETIC_AMBIENT_SENTINEL' not in os.environ
                try:
                    os.fstat(int(sys.argv[2]))
                except OSError:
                    pass
                else:
                    raise AssertionError('unrelated descriptor inherited')
                channel = receive_owner(int(sys.argv[-1]), expected_scope=sys.argv[1],
                    expected_producer='producer-a', expected_recipient='backend-a')
                assert not channel.get_inheritable()
                stat = os.fstat(channel.fileno())
                worker = """
                + repr("""\
import os, sys
try:
    stat = os.fstat(int(sys.argv[1]))
except OSError:
    pass
else:
    assert (stat.st_dev, stat.st_ino) != (int(sys.argv[2]), int(sys.argv[3]))
""")
                + """
                subprocess.run([sys.executable, '-I', '-c', worker, str(channel.fileno()),
                    str(stat.st_dev), str(stat.st_ino)], close_fds=False, check=True, timeout=10)
                channel.close()
            """
            )
            launched = launch_owner(
                session,
                [sys.executable, "-I", "-c", script, program.program_id, str(read_fd)],
                environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath},
                scope=program.program_id,
                producer="producer-a",
                recipient="backend-a",
            )
            try:
                assert launched.process.wait(timeout=20) == 0
            finally:
                launched.close()
                if launched.process.poll() is None:
                    launched.process.kill()
                launched.process.wait(timeout=10)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"contract":"wrong","wire_version":1,"role":"owner","scope":"p",'
        b'"producer":"producer-a","recipient":"backend-a"}',
        b'{"contract":"creatidy-kernel-owner-descriptor-v1","wire_version":true,"role":"owner","scope":"p",'
        b'"producer":"producer-a","recipient":"backend-a"}',
        b'{"contract":"creatidy-kernel-owner-descriptor-v1","wire_version":1,"role":"worker","scope":"p",'
        b'"producer":"producer-a","recipient":"backend-a"}',
        b'{"contract":"creatidy-kernel-owner-descriptor-v1","wire_version":1,"role":"owner","scope":"other",'
        b'"producer":"producer-a","recipient":"backend-a"}',
        b'{"contract":"creatidy-kernel-owner-descriptor-v1","wire_version":1,"role":"owner","scope":"p",'
        b'"scope":"p","producer":"producer-a","recipient":"backend-a"}',
    ],
)
def test_consumer_refuses_incompatible_greetings(payload: bytes) -> None:
    sender, receiver = socket.socketpair()
    with sender:
        write_frame(sender, payload, bound=65536)
        with pytest.raises(LocalChannelRefused):
            receive_owner(
                receiver.detach(), expected_scope="p", expected_producer="producer-a", expected_recipient="backend-a"
            )
        try:
            assert sender.recv(1) == b""
        except ConnectionResetError:
            pass


def test_receiver_closes_revoked_channel_and_rejects_version() -> None:
    sender, receiver = socket.socketpair()
    with sender:
        write_frame(
            sender,
            json.dumps(
                {
                    "contract": PROVISIONING,
                    "wire_version": 1,
                    "role": "owner",
                    "scope": "p",
                    "producer": "producer-a",
                    "recipient": "backend-a",
                }
            ).encode(),
            bound=65536,
        )
        with pytest.raises(LocalChannelRefused):
            receive_owner(
                receiver.detach(),
                expected_scope="p",
                expected_producer="producer-a",
                expected_recipient="backend-a",
                version=2,
            )
        try:
            assert sender.recv(1) == b""
        except ConnectionResetError:
            pass


@pytest.mark.parametrize("unsupported", [False, True])
def test_receive_consumes_non_socket_descriptor_on_refusal(unsupported: bool) -> None:
    read_fd, write_fd = os.pipe()
    try:
        with patch("creatidy_kernel.adapters.served_host.sys.platform", "darwin" if unsupported else sys.platform):
            with pytest.raises(LocalChannelRefused):
                receive_owner(
                    read_fd, expected_scope="p", expected_producer="producer-a", expected_recipient="backend-a"
                )
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)


def test_receive_refuses_invalid_descriptors_without_consuming_standard_fds() -> None:
    before: dict[int, os.stat_result | None] = {}
    for fd in range(3):
        try:
            before[fd] = os.fstat(fd)
        except OSError:
            before[fd] = None
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        for fd in (0, 1, 2, -1, True, read_fd, 10**100):
            with pytest.raises(LocalChannelRefused):
                receive_owner(fd, expected_scope="p", expected_producer="producer-a", expected_recipient="backend-a")
        for fd, expected in before.items():
            if expected is None:
                with pytest.raises(OSError):
                    os.fstat(fd)
            else:
                assert os.fstat(fd) == expected
    finally:
        os.close(write_fd)


def test_launch_refuses_scope_and_failed_exec_cleans_up(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "launch-failure.db") as store:
        program = store.create(reference_spec(), "create")
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        for _ in range(65):
            with pytest.raises(FileNotFoundError):
                launch_owner(
                    session,
                    ["/nonexistent-synthetic-executable"],
                    environment={},
                    scope=program.program_id,
                    producer="producer-a",
                    recipient="backend-a",
                )
        with pytest.raises(LocalChannelRefused):
            launch_owner(
                session,
                [sys.executable],
                environment={},
                scope="different",
                producer="producer-a",
                recipient="backend-a",
            )
        session.revoke()
        with pytest.raises(LocalChannelRefused):
            launch_owner(
                session,
                [sys.executable],
                environment={},
                scope=program.program_id,
                producer="producer-a",
                recipient="backend-a",
            )


def test_revoke_active_launched_clients_without_revoking_work(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "active.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        script = (
            "import sys; from creatidy_kernel.adapters.served_host import receive_owner; "
            "from creatidy_kernel.adapters.served_local import write_frame; "
            "s=receive_owner(int(sys.argv[-1]),expected_scope=sys.argv[1],"
            "expected_producer='producer-a',expected_recipient='backend-a'); "
            'write_frame(s,b\'{"version":1,"action":"snapshot"}\',bound=65536); '
            "\ntry:\n assert s.recv(1)==b''\nexcept ConnectionResetError:\n pass\n"
        )
        launches: list[LaunchedOwner] = []
        try:
            for _ in range(2):
                launches.append(
                    launch_owner(
                        session,
                        [sys.executable, "-I", "-c", script, program.program_id],
                        environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8"},
                        scope=program.program_id,
                        producer="producer-a",
                        recipient="backend-a",
                    )
                )
            for launched in launches:
                assert select.select([launched.channel.fileno()], [], [], 10)[0]
            session.revoke()
            for launched in launches:
                assert launched.process.wait(timeout=10) == 0
                launched.close()
                launched.close()
            assert (store.history(program.program_id), store.operations()) == before
            with pytest.raises(LocalChannelRefused):
                launch_owner(
                    session,
                    [sys.executable],
                    environment={},
                    scope=program.program_id,
                    producer="producer-a",
                    recipient="backend-a",
                )
        finally:
            for launched in launches:
                launched.close()
                if launched.process.poll() is None:
                    launched.process.kill()
                    launched.process.wait(timeout=10)


def test_restarted_owner_requires_resync_for_launched_backend(sqlite_tmp_path: Path) -> None:
    database = sqlite_tmp_path / "restart.db"
    script = (
        "import json,pathlib,sys; from creatidy_kernel.adapters.served_host import receive_owner; "
        "from creatidy_kernel.adapters.served_local import write_frame,read_frame; "
        "s=receive_owner(int(sys.argv[-1]),expected_scope=sys.argv[1],"
        "expected_producer='producer-a',expected_recipient='backend-a'); "
        "write_frame(s,sys.argv[2].encode(),bound=65536); "
        "pathlib.Path(sys.argv[3]).write_bytes(read_frame(s,bound=1048576)); s.close()"
    )

    def request(session: LocalOwnerSession, payload: dict[str, object], path: Path) -> dict[str, object]:
        launched = launch_owner(
            session,
            [sys.executable, "-I", "-c", script, session.scope, json.dumps(payload), str(path)],
            environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8"},
            scope=session.scope,
            producer="producer-a",
            recipient="backend-a",
        )
        try:
            assert select.select([launched.channel.fileno()], [], [], 10)[0]
            launched.channel.serve_once()
            assert launched.process.wait(timeout=10) == 0
            return cast(dict[str, object], json.loads(path.read_text())["result"])
        finally:
            launched.close()
            if launched.process.poll() is None:
                launched.process.kill()
            launched.process.wait(timeout=10)

    with SQLiteProgramStore(database) as store:
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        program = store.create(reference_spec(), "create")
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        first = request(session, {"version": 1, "action": "snapshot"}, sqlite_tmp_path / "first.json")
        session.revoke()
    with SQLiteProgramStore(database) as store:
        app.store = store
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        events = request(
            session,
            {"version": 1, "action": "events", "after": first["position"], "limit": 1},
            sqlite_tmp_path / "events.json",
        )
        assert events["gap"] is True
        snapshot = request(session, {"version": 1, "action": "snapshot"}, sqlite_tmp_path / "second.json")
        assert snapshot["scope"] == first["scope"] and snapshot["position"] != first["position"]
        session.revoke()


@pytest.mark.parametrize("scope,version", [("wrong-recipient", "1"), ("correct", "2")])
def test_launched_backend_refuses_wrong_scope_or_version(sqlite_tmp_path: Path, scope: str, version: str) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "wrong-recipient.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        script = (
            "import sys; from creatidy_kernel.adapters.served_host import receive_owner; "
            "from creatidy_kernel.adapters.served_local import LocalChannelRefused; "
            "\ntry:\n receive_owner(int(sys.argv[-1]),expected_scope=sys.argv[1],"
            "expected_producer='producer-a',expected_recipient='backend-a',version=int(sys.argv[2]))"
            "\nexcept LocalChannelRefused:\n sys.exit(0)\nraise SystemExit(1)\n"
        )
        launched = launch_owner(
            session,
            [sys.executable, "-I", "-c", script, program.program_id if scope == "correct" else scope, version],
            environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8"},
            scope=program.program_id,
            producer="producer-a",
            recipient="backend-a",
        )
        try:
            assert launched.process.wait(timeout=10) == 0
            assert (store.history(program.program_id), store.operations()) == before
        finally:
            launched.close()
            if launched.process.poll() is None:
                launched.process.kill()
                launched.process.wait(timeout=10)


@pytest.mark.parametrize("producer,recipient", [("producer-b", "backend-a"), ("producer-a", "backend-b")])
def test_same_scope_misdirection_is_refused(sqlite_tmp_path: Path, producer: str, recipient: str) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "misdirected.db") as store:
        program = store.create(reference_spec(), "create")
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        script = (
            "import sys; from creatidy_kernel.adapters.served_host import receive_owner; "
            "from creatidy_kernel.adapters.served_local import LocalChannelRefused; "
            "\ntry:\n receive_owner(int(sys.argv[-1]),expected_scope=sys.argv[1],"
            "expected_producer='producer-a',expected_recipient='backend-a')"
            "\nexcept LocalChannelRefused:\n sys.exit(0)\nraise SystemExit(1)\n"
        )
        launched = launch_owner(
            session,
            [sys.executable, "-I", "-c", script, program.program_id],
            environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath},
            scope=program.program_id,
            producer=producer,
            recipient=recipient,
        )
        try:
            assert launched.process.wait(timeout=10) == 0
            assert select.select([launched.channel.fileno()], [], [], 10)[0]
            with pytest.raises(LocalChannelRefused):
                launched.serve_once()
            assert launched.channel.fileno() == -1
        finally:
            launched.close()
            if launched.process.poll() is None:
                launched.process.kill()
            launched.process.wait(timeout=10)


def test_failed_channels_are_detached_across_more_than_64_replacements(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "replacement.db") as store:
        program = store.create(reference_spec(), "create")
        before = (store.history(program.program_id), store.operations())
        app = cast(OrdinaryProgram, SimpleNamespace(owner=SimpleNamespace(role="owner"), store=store))
        approval = cast(OrdinaryApproval, SimpleNamespace(program=program))
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        script = (
            "import sys; from creatidy_kernel.adapters.served_host import receive_owner; "
            "from creatidy_kernel.adapters.served_local import write_frame; "
            "s=receive_owner(int(sys.argv[-1]),expected_scope=sys.argv[1],"
            "expected_producer='producer-a',expected_recipient='backend-a'); "
            '\nif sys.argv[2]=="refused":\n'
            ' write_frame(s,b\'{\\"version\\":1,\\"action\\":\\"invalid\\"}\',bound=65536)'
            "\ns.close()"
        )
        for index in range(66):
            launched = launch_owner(
                session,
                [sys.executable, "-I", "-c", script, program.program_id, "refused" if index % 2 else "closed"],
                environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath},
                scope=program.program_id,
                producer="producer-a",
                recipient="backend-a",
            )
            try:
                assert launched.process.wait(timeout=10) == 0
                assert select.select([launched.channel.fileno()], [], [], 10)[0]
                with pytest.raises(LocalChannelRefused):
                    launched.serve_once()
            finally:
                launched.close()
                if launched.process.poll() is None:
                    launched.process.kill()
                launched.process.wait(timeout=10)
        assert (store.history(program.program_id), store.operations()) == before


def test_owner_process_death_and_fresh_process_require_resync(sqlite_tmp_path: Path) -> None:
    script = textwrap.dedent("""\
        import json, os, select, sys
        from pathlib import Path
        from types import SimpleNamespace
        from creatidy_kernel.adapters.reference import reference_spec
        from creatidy_kernel.adapters.served_host import launch_owner
        from creatidy_kernel.adapters.served_local import LocalOwnerSession
        from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
        from creatidy_kernel.ports.served import OwnerProjection

        root, phase = Path(sys.argv[1]), sys.argv[2]
        store = SQLiteProgramStore(root / 'process.db')
        program = store.create(reference_spec(), 'create') if phase == 'first' else store.load('reference')
        app = SimpleNamespace(owner=SimpleNamespace(role='owner'), store=store)
        approval = SimpleNamespace(program=program)
        session = LocalOwnerSession(OwnerProjection(store, program.program_id), app, approval)
        request = {'version': 1, 'action': 'snapshot'} if phase == 'first' else {
            'version': 1, 'action': 'events',
            'after': json.loads((root / 'first.json').read_text())['position'], 'limit': 1}
        child = (
            "import json,pathlib,sys; from creatidy_kernel.adapters.served_host import receive_owner; "
            "from creatidy_kernel.adapters.served_local import write_frame,read_frame; "
            "s=receive_owner(int(sys.argv[-1]),expected_scope=sys.argv[1],"
            "expected_producer='producer-a',expected_recipient='backend-a'); "
            "write_frame(s,sys.argv[2].encode(),bound=65536); "
            "pathlib.Path(sys.argv[3]).write_text(json.dumps(json.loads(read_frame(s,bound=1048576))['result'])); "
            "s.close()"
        )
        launched = launch_owner(session, [sys.executable, '-I', '-c', child, program.program_id,
            json.dumps(request), str(root / (phase + '.json'))],
            environment={'HOME': str(root), 'PATH': os.defpath}, scope=program.program_id,
            producer='producer-a', recipient='backend-a')
        try:
            assert select.select([launched.channel.fileno()], [], [], 10)[0]
            launched.serve_once()
            assert launched.process.wait(timeout=10) == 0
        finally:
            launched.close()
            if launched.process.poll() is None:
                launched.process.kill()
            launched.process.wait(timeout=10)
        (root / (phase + '.pid')).write_text(str(os.getpid()))
        if phase == 'first':
            os._exit(0)
        store.close()
    """)
    env = {"HOME": str(sqlite_tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8"}
    for phase in ("first", "second"):
        subprocess.run(  # noqa: S603 - Fixed synthetic producer program in a closed environment.
            [sys.executable, "-I", "-c", script, str(sqlite_tmp_path), phase], env=env, check=True, timeout=30
        )
    assert (sqlite_tmp_path / "first.pid").read_text() != (sqlite_tmp_path / "second.pid").read_text()
    assert json.loads((sqlite_tmp_path / "second.json").read_text())["gap"] is True
