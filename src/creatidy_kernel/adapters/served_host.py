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
    APPROVAL_DISPOSITION,
    APPROVAL_OPERATIONS,
    OWNER_READS,
    PROPOSAL_READS,
    LocalChannelRefused,
    LocalOwnerChannel,
    LocalOwnerSession,
    read_frame,
    unique_object,
    write_frame,
)

PROVISIONING = "creatidy-kernel-owner-descriptor-v1"
PROVISIONING_V2 = "creatidy-kernel-owner-descriptor-v2"
_OWNER_OPERATIONS = OWNER_READS | PROPOSAL_READS | APPROVAL_OPERATIONS | {APPROVAL_DISPOSITION}


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
    verified_approval_history: bool = False,
) -> LaunchedOwner:
    """Launch the trusted backend, appending its descriptor as the final argv element.

    The caller is responsible for authenticating the executable, binding the session and
    supplying an environment containing only values appropriate for the child.
    Opt in to approval disposition only for a compatible v2 backend when the host has
    verified custody of the original authoritative journal for this producer binding.
    """
    if (
        sys.platform != "linux"
        or type(version) is not int
        or version not in (1, 2)
        or type(verified_approval_history) is not bool
        or (verified_approval_history and version != 2)
        or not argv
        or not os.path.isabs(argv[0])
        or not scope
        or (version == 2 and type(scope) is not str)
        or scope != session.scope
        or not _valid_binding(producer)
        or not _valid_binding(recipient)
    ):
        raise LocalChannelRefused("unsupported host provisioning or scope")
    server, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    channel: LocalOwnerChannel | None = None
    try:
        channel = session.attach(
            server, approval_history_binding=(producer, recipient) if verified_approval_history else None
        )
        greeting: dict[str, object] = {
            "contract": PROVISIONING if version == 1 else PROVISIONING_V2,
            "wire_version": 1,  # Only the provisioning greeting changes.
            "role": "owner",
            "scope": scope,
            "producer": producer,
            "recipient": recipient,
        }
        if version == 2:
            greeting["operations"] = sorted(channel.supported_operations())
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
    """Receive the strict v1 greeting; own fd >= 3 even on refusal."""
    channel, _ = _receive_owner(fd, expected_scope, expected_producer, expected_recipient, version, allow_v2=False)
    return channel


def receive_owner_operations(
    fd: int, *, expected_scope: str, expected_producer: str, expected_recipient: str
) -> tuple[socket.socket, frozenset[str] | None]:
    """Receive v2 operations, or None on a verified legacy v1 descriptor, without a request."""
    return _receive_owner(fd, expected_scope, expected_producer, expected_recipient, 1, allow_v2=True)


def _receive_owner(
    fd: int, expected_scope: str, expected_producer: str, expected_recipient: str, version: int, *, allow_v2: bool
) -> tuple[socket.socket, frozenset[str] | None]:
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
        if channel.family != socket.AF_UNIX or channel.type != socket.SOCK_STREAM:
            raise LocalChannelRefused("inherited descriptor is not a local stream")
        channel.getpeername()
        raw = read_frame(channel, bound=65536)
        legacy_encoding = False
        try:
            try:
                value: object = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
            except (UnicodeError, json.JSONDecodeError):
                # Only the original v1 decoder accepted JSON encoding auto-detection.
                value = json.loads(raw, object_pairs_hook=unique_object)
                legacy_encoding = True
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise LocalChannelRefused("malformed owner greeting") from error
        if type(value) is not dict:
            raise LocalChannelRefused("incompatible owner capability")
        fields = cast(dict[str, object], value)
        expected = {
            "contract": PROVISIONING,
            "wire_version": 1,
            "role": "owner",
            "scope": expected_scope,
            "producer": expected_producer,
            "recipient": expected_recipient,
        }
        operations = None
        if allow_v2 and not legacy_encoding and fields.get("contract") == PROVISIONING_V2:
            if type(fields.get("scope")) is not str:
                raise LocalChannelRefused("invalid owner scope")
            expected["contract"] = PROVISIONING_V2
            operations = _parse_operations(fields.pop("operations", None))
        if type(fields.get("wire_version")) is not int or fields != expected:
            raise LocalChannelRefused("incompatible owner capability")
        channel.settimeout(None)
        channel.set_inheritable(False)
        return channel, operations
    except BaseException:
        channel.close()
        raise


def _parse_operations(value: object) -> frozenset[str]:
    if type(value) is not list:
        raise LocalChannelRefused("invalid owner operation declaration")
    entries = cast(list[object], value)
    if len(entries) > len(_OWNER_OPERATIONS) or any(
        type(entry) is not str or entry not in _OWNER_OPERATIONS for entry in entries
    ):
        raise LocalChannelRefused("unknown owner operation")
    operations = frozenset(cast(list[str], entries))
    if (
        len(operations) != len(entries)
        or not OWNER_READS <= operations
        or (operations & PROPOSAL_READS and not PROPOSAL_READS <= operations)
        or (operations & APPROVAL_OPERATIONS and not (PROPOSAL_READS | APPROVAL_OPERATIONS) <= operations)
    ):
        raise LocalChannelRefused("inconsistent owner operation declaration")
    return operations
