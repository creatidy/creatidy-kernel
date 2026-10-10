# SPDX-License-Identifier: Apache-2.0
"""Deterministic ancillary/ownership guards; simulated pidfds are NOT native reception."""

import os
import socket
import struct
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest

from creatidy_kernel.adapters import oci_lifetime
from creatidy_kernel.adapters.oci_lifetime import SCM_PIDFD, SO_PASSPIDFD, LifetimeChannel
from creatidy_kernel.core.execution import ExecutionConflict


class PacketSocket:
    def __init__(self, stream: socket.socket, descriptor: int) -> None:
        self.stream = stream
        self.data = b"READY:" + b"b" * 64
        self.controls = [
            (socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("iII", 1234567, os.getuid(), os.getgid())),
            (socket.SOL_SOCKET, SCM_PIDFD, struct.pack("i", descriptor)),
        ]
        self.flags = 0

    def setsockopt(self, level: int, option: int, value: int) -> None:
        if option != SO_PASSPIDFD:
            self.stream.setsockopt(level, option, value)

    def settimeout(self, value: float) -> None:
        self.stream.settimeout(value)

    def recvmsg(self, size: int, space: int, flags: int) -> tuple[bytes, list[tuple[int, int, bytes]], int, None]:
        assert size == 128 and space > 0 and flags == socket.MSG_CMSG_CLOEXEC
        return self.data, self.controls, self.flags, None

    def sendall(self, data: bytes) -> None:
        self.stream.sendall(data)

    def close(self) -> None:
        self.stream.close()


@pytest.fixture
def channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[LifetimeChannel, PacketSocket, int]]:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    reader, writer = os.pipe()
    packet = PacketSocket(left, reader)

    def pair(_family: int, _type: int) -> tuple[socket.socket, socket.socket]:
        return cast(socket.socket, packet), right

    monkeypatch.setattr(socket, "socketpair", pair)
    namespace = tmp_path / "simulated-namespace"
    namespace.write_bytes(b"not a real namespace; deterministic fixture")
    original_open = os.open

    def open_namespace(path: str | Path, flags: int, mode: int = 0o777) -> int:
        return original_open(namespace if str(path) == "/proc/1234567/ns/pid" else path, flags, mode)

    monkeypatch.setattr(os, "open", open_namespace)

    def read(path: Path, limit: int) -> bytes:
        assert limit in {4096, 65536}
        if str(path) == "/proc/self/status":
            return f"Pid:\t{os.getpid()}\nNSpid:\t{os.getpid()}\n".encode()
        if str(path) == "/proc/1234567/status":
            return (
                "Pid:\t1234567\nTgid:\t1234567\nNSpid:\t1234567 1\n"
                f"Uid:\t{os.getuid()} {os.getuid()} {os.getuid()} {os.getuid()}\n"
            ).encode()
        if str(path) == "/proc/1234567/stat":
            return ("1234567 (owned (init)) " + " ".join(["S", *["0"] * 18, "12345", "0"]) + "\n").encode()
        assert str(path) == f"/proc/self/fdinfo/{reader}"
        return b"Pid:\t1234567\nNSpid:\t1234567 1\n"

    monkeypatch.setattr(oci_lifetime, "read_bounded_regular", read)
    value = LifetimeChannel("b" * 64)
    yield value, packet, writer
    value.close()
    for descriptor in (reader, writer):
        with suppress(OSError):
            os.close(descriptor)


def test_authenticated_handle_retained_until_actual_exit_signal(
    channel: tuple[LifetimeChannel, PacketSocket, int],
) -> None:
    lifetime, _, writer = channel
    identity = lifetime.receive(timeout=1)
    assert identity["pid"] == 1234567 and identity["start_ticks"] == 12345
    assert lifetime.pidfd is not None and not os.get_inheritable(lifetime.pidfd)
    assert not lifetime.exited()
    assert lifetime.live()
    lifetime.release()
    assert lifetime.worker.recv(16) == b"GO"
    os.write(writer, b"simulated pidfd exit readiness")
    assert lifetime.exited()
    assert not lifetime.live()
    with pytest.raises(ExecutionConflict, match="not live"):
        lifetime.release()
    lifetime.close()
    assert not lifetime.exited()
    assert not lifetime.live()


@pytest.mark.parametrize("kind", ["nonce", "truncated", "credentials", "rights", "duplicate"])
def test_invalid_or_untrusted_ancillary_data_closes_received_descriptors(
    channel: tuple[LifetimeChannel, PacketSocket, int], kind: str
) -> None:
    lifetime, packet, _ = channel
    descriptor = struct.unpack("i", packet.controls[1][2])[0]
    if kind == "nonce":
        packet.data = b"READY:foreign"
    elif kind == "truncated":
        packet.flags = socket.MSG_CTRUNC
    elif kind == "credentials":
        packet.controls[0] = (
            socket.SOL_SOCKET,
            socket.SCM_CREDENTIALS,
            struct.pack("iII", 1234567, os.getuid() + 1, os.getgid()),
        )
    elif kind == "rights":
        packet.controls[1] = (socket.SOL_SOCKET, socket.SCM_RIGHTS, packet.controls[1][2])
    else:
        packet.controls.append(packet.controls[0])
    with pytest.raises(ExecutionConflict, match="unauthenticated"):
        lifetime.receive(timeout=1)
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not lifetime.exited()


def test_early_exit_does_not_bind_a_replacement_pid(channel: tuple[LifetimeChannel, PacketSocket, int]) -> None:
    lifetime, _, writer = channel
    os.write(writer, b"exited before capture")
    with pytest.raises(ExecutionConflict, match="exited before"):
        lifetime.receive(timeout=1)
    assert lifetime.identity is None


def test_descriptor_errors_are_unknown_not_exit(
    channel: tuple[LifetimeChannel, PacketSocket, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifetime, _, _ = channel
    lifetime.receive(timeout=1)

    def unknown(_fd: int) -> None:
        return None

    monkeypatch.setattr(LifetimeChannel, "_exited", staticmethod(unknown))
    assert not lifetime.exited()
    assert not lifetime.live()
    with pytest.raises(ExecutionConflict, match="not live"):
        lifetime.release()
