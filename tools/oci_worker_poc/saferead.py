# SPDX-License-Identifier: Apache-2.0
"""Fail-closed reads of worker-controlled files: the shared bounded-regular primitive.

Any file under a disposable worker workspace — synthetic (#78) or real-harness (#53) — can
be replaced or symlinked by the worker at any time, including to host paths invisible
inside the boundary. This module is the one shared primitive for reading such paths from
the trusted controller: symbolic links are refused (``lstat`` classification plus an
``O_NOFOLLOW`` open), the opened object is validated with ``fstat`` on the very descriptor
that is read, ``O_NONBLOCK`` prevents a swapped-in FIFO from blocking the open, and reads
are bounded against both the declared size and the actual bytes (a concurrent-growth race
cannot over-read). It lives in this package so the #78 harness and the #53 proof share one
implementation without a reverse dependency.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_bounded_fd(fd: int, declared_size: int, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes from an already-pinned descriptor.

    The pre-check bounds the declared (fstat) size and the loop bounds the actual read,
    so a file that grows after ``fstat`` — the concurrent-growth race on a live worker's
    file — is still refused instead of over-reading. ``None`` means the bound was hit.
    """
    if declared_size > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def read_bounded_regular(path: Path, limit: int = 1024 * 1024) -> bytes:
    """Read one worker-controlled file safely: no symlink, regular file only, bounded.

    Used for controller-designated paths inside the writable workspace (whose content
    the worker may replace at any time, including with a symlink). Tampering raises;
    absence is the caller's decision to handle via ``FileNotFoundError``.
    """
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"refusing symlinked worker-controlled path: {path}")
    # O_NONBLOCK: a swapped-in FIFO cannot block the open; the fstat S_ISREG check below
    # refuses non-regular objects regardless, and O_NONBLOCK is a no-op for regular reads.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"worker-controlled path is not a regular file: {path}")
        data = read_bounded_fd(fd, opened.st_size, limit)
    finally:
        os.close(fd)
    if data is None:
        raise ValueError(f"worker-controlled path exceeds or grew beyond the read bound: {path}")
    return data


__all__ = ["read_bounded_fd", "read_bounded_regular"]
