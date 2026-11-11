# SPDX-License-Identifier: Apache-2.0
"""Trusted supervisor launch of one independent local owner-capability consumer."""

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import cast

from creatidy_kernel.adapters.served_local import (
    LocalChannelRefused,
    LocalOwnerChannel,
    LocalOwnerSession,
    read_frame,
    unique_object,
    write_frame,
)

PROVISIONING = "creatidy-kernel-owner-descriptor-v1"


def _valid_binding(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None


class LaunchedOwner:
    """The supervisor owns the process and the owner-thread channel separately."""

    def __init__(
        self, session: LocalOwnerSession, channel: LocalOwnerChannel, process: subprocess.Popen[bytes]
    ) -> None:
        self.session, self.channel, self.process = session, channel, process
        self._closed = False

    def close(self) -> None:
        """Revoke this channel; process termination and work cancellation are separate."""
        if not self._closed:
            self.session.detach(self.channel)
            self._closed = True

    def serve_once(self) -> None:
        """Service on the owner thread, detaching a failed or disconnected channel."""
        try:
            self.channel.serve_once()
        except BaseException:
            self.close()
            raise


def launch_owner(
    session: LocalOwnerSession,
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    scope: str,
    producer: str,
    recipient: str,
    version: int = 1,
) -> LaunchedOwner:
    """Launch the trusted backend, appending its descriptor as the final argv element.

    The caller is responsible for authenticating the executable, binding the session and
    supplying an environment containing only values appropriate for the child.
    """
    if (
        sys.platform != "linux"
        or type(version) is not int
        or version != 1
        or not argv
        or not os.path.isabs(argv[0])
        or not scope
        or scope != session.scope
        or not _valid_binding(producer)
        or not _valid_binding(recipient)
    ):
        raise LocalChannelRefused("unsupported host provisioning or scope")
    server, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    channel: LocalOwnerChannel | None = None
    try:
        channel = session.attach(server)
        greeting = {
            "contract": PROVISIONING,
            "wire_version": 1,
            "role": "owner",
            "scope": scope,
            "producer": producer,
            "recipient": recipient,
        }
        write_frame(server, json.dumps(greeting, sort_keys=True).encode(), bound=65536)
        process = subprocess.Popen(  # noqa: S603 - Trusted supervisor selects the exact executable and arguments.
            [*argv, str(peer.fileno())],
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(peer.fileno(),),
        )
        return LaunchedOwner(session, channel, process)
    except BaseException:
        if channel is not None:
            session.detach(channel)
        else:
            server.close()
        raise
    finally:
        peer.close()


def receive_owner(
    fd: int, *, expected_scope: str, expected_producer: str, expected_recipient: str, version: int = 1
) -> socket.socket:
    """Take ownership of integer fd >= 3, including on refusal; never consume standard fds."""
    if type(fd) is not int or fd < 3:
        raise LocalChannelRefused("invalid inherited descriptor")
    try:
        if sys.platform != "linux":
            raise LocalChannelRefused("unsupported host provisioning platform")
        channel = socket.socket(fileno=fd)
    except (OSError, ValueError, OverflowError) as error:
        try:
            os.close(fd)
        except (OSError, OverflowError):
            pass
        raise LocalChannelRefused("invalid inherited descriptor") from error
    try:
        if (
            type(version) is not int
            or version != 1
            or not expected_scope
            or not _valid_binding(expected_producer)
            or not _valid_binding(expected_recipient)
        ):
            raise LocalChannelRefused("unsupported provisioning version or scope")
        if channel.family != socket.AF_UNIX or channel.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
            raise LocalChannelRefused("inherited descriptor is not a local stream")
        channel.getpeername()
        value: object = json.loads(read_frame(channel, bound=65536), object_pairs_hook=unique_object)
        if (
            type(value) is not dict
            or type(cast(dict[str, object], value).get("wire_version")) is not int
            or value
            != {
                "contract": PROVISIONING,
                "wire_version": 1,
                "role": "owner",
                "scope": expected_scope,
                "producer": expected_producer,
                "recipient": expected_recipient,
            }
        ):
            raise LocalChannelRefused("incompatible owner capability")
        channel.settimeout(None)
        channel.set_inheritable(False)
        return channel
    except BaseException:
        channel.close()
        raise
