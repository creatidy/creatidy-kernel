# SPDX-License-Identifier: Apache-2.0
"""Exclusive owner-local file publication for private outcome exports."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat

from creatidy_kernel.ports.outcome_export import OutcomeExportStore, export_private_outcome


def write_private_outcome(
    store: OutcomeExportStore,
    program_id: str,
    *,
    owner_policy: str,
    directory_fd: int,
    name: str,
    pseudonym_key: bytes | None = None,
) -> None:
    """Publish one new 0600 file in an already-open owner-private directory.

    Existing names are never replaced. A crash before publication can leave a
    private temporary file; an error after the link is an uncertain outcome,
    and the caller must inspect the destination rather than blindly retry.
    """
    if type(directory_fd) is not int or directory_fd < 0:
        raise ValueError("an open private directory descriptor is required")
    if type(name) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is None:
        raise ValueError("invalid private export filename")
    directory = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or directory.st_mode & 0o077:
        raise PermissionError("export directory must be owned and private")
    document = export_private_outcome(store, program_id, owner_policy=owner_policy, pseudonym_key=pseudonym_key)
    data = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    temporary = ".outcome-" + secrets.token_hex(16)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd
    )
    original = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
    finally:
        try:
            current = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
                os.unlink(temporary, dir_fd=directory_fd)
                os.fsync(directory_fd)
        except FileNotFoundError:
            pass
