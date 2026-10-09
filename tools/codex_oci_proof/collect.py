# SPDX-License-Identifier: Apache-2.0
"""Fail-closed collection of worker-controlled workspace content.

The isolated worker controls every byte and every directory entry of its writable
workspace, including symbolic links whose targets may be invisible inside the OCI
boundary but readable to the trusted controller on the host. Collection therefore never
trusts path resolution: each directory is pinned by file descriptor, exactly one component
is looked up at a time, symbolic links are refused at every level (``lstat``
classification plus ``O_NOFOLLOW``/``O_DIRECTORY`` opens), the opened object is validated
with ``fstat`` on the very descriptor that is read, and reads are size- and entry-bounded.
A checked path is never re-resolved and reopened, so no path-validation/open race exists.
Refusals are recorded with reasons and never silently skipped; a refusal is not a pass.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_DEPTH = 32


@dataclass(frozen=True, slots=True)
class CollectedFile:
    """One regular file's collected identity: exact bytes were read through a pinned fd."""

    path: str  # relative POSIX path inside the collected root
    size: int
    sha256: str


@dataclass(slots=True)
class SafeCollection:
    """Collected regular files plus recorded refusals; refusals are failures, not skips.

    ``consumed`` counts every entry the traversal spent budget on (files, refusals and
    descended directories), so traversal work — not only output size — is bounded.
    """

    files: dict[str, CollectedFile] = field(default_factory=dict)
    refusals: dict[str, str] = field(default_factory=dict)
    consumed: int = 0

    def digests(self) -> set[str]:
        return {record.sha256 for record in self.files.values()}


def _errno_label(error: OSError) -> str:
    code = error.errno or 0
    return errno.errorcode.get(code, f"errno-{code}")


def _walk(
    dir_fd: int,
    prefix: str,
    exclude: frozenset[str],
    depth: int,
    out: SafeCollection,
    sink: Callable[[str, bytes, CollectedFile], None] | None,
) -> None:
    if out.consumed >= MAX_ENTRIES:
        out.refusals[prefix or "."] = "entry-budget-exhausted"
        return
    if depth > MAX_DEPTH:
        out.refusals[prefix or "."] = "max-depth-exceeded"
        return
    # scandir streams entries incrementally: a multi-million-entry directory cannot
    # inflate controller memory before the entry budget stops the traversal.
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if _walk_entry(dir_fd, entry.name, prefix, exclude, depth, out, sink):
                return
            if out.consumed >= MAX_ENTRIES:
                out.refusals[prefix or "."] = "entry-budget-exhausted"
                return


def _walk_entry(
    dir_fd: int,
    name: str,
    prefix: str,
    exclude: frozenset[str],
    depth: int,
    out: SafeCollection,
    sink: Callable[[str, bytes, CollectedFile], None] | None,
) -> bool:
    """Classify and handle one directory entry; True means the budget stopped the walk."""
    if out.consumed >= MAX_ENTRIES:
        out.refusals[prefix + name] = "entry-budget-exhausted"
        return True
    relative = prefix + name
    if depth == 0 and name in exclude:
        return False  # collected separately under its own explicit root
    out.consumed += 1
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except OSError as error:
        out.refusals[relative] = f"lstat-denied:{_errno_label(error)}"
        return False
    if stat.S_ISLNK(st.st_mode):
        out.refusals[relative] = "symlink-refused"
        return False
    if stat.S_ISDIR(st.st_mode):
        out.consumed += 1  # descending into a directory spends traversal budget too
        try:
            sub_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError as error:
            # ELOOP here means a directory entry was swapped to a symlink after lstat.
            out.refusals[relative] = f"dir-open-denied:{_errno_label(error)}"
            return False
        try:
            _walk(sub_fd, relative + "/", exclude, depth + 1, out, sink)
        finally:
            os.close(sub_fd)
        return out.consumed >= MAX_ENTRIES
    if not stat.S_ISREG(st.st_mode):
        out.refusals[relative] = "not-regular-file"
        return False
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as error:
        out.refusals[relative] = f"open-denied:{_errno_label(error)}"
        return False
    try:
        opened = os.fstat(fd)  # authoritative for the object this descriptor reads
        if not stat.S_ISREG(opened.st_mode):
            out.refusals[relative] = "not-regular-file"
            return False
        data = read_bounded_fd(fd, opened.st_size, MAX_FILE_BYTES)
    finally:
        os.close(fd)
    if data is None:
        out.refusals[relative] = "file-exceeds-size-bound"
        return False
    record = CollectedFile(relative, len(data), hashlib.sha256(data).hexdigest())
    if sink is not None:
        sink(relative, data, record)  # exact bytes delivered while still trusted in memory
    out.files[relative] = record
    return False


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


def collect_directory(
    root: Path,
    *,
    exclude: frozenset[str] = frozenset(),
    sink: Callable[[str, bytes, CollectedFile], None] | None = None,
) -> SafeCollection:
    """Collect regular files under ``root`` without ever following a symbolic link.

    ``exclude`` names root-level entries collected separately under their own explicit
    roots; exclusion applies only at the top level and is not a safety mechanism. When
    ``sink`` is given it receives every file's exact bytes together with its record,
    before the record is published, so the caller can durably preserve and bind them.
    Root problems raise: a missing, non-directory or symlinked root is a caller contract
    failure, not a collectable refusal.
    """
    try:
        st = os.lstat(root)
    except FileNotFoundError as error:
        raise ValueError(f"collection root does not exist: {root}") from error
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"collection root is a symbolic link: {root}")
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"collection root is not a directory: {root}")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        out = SafeCollection()
        _walk(root_fd, "", exclude, 0, out, sink)
        return out
    finally:
        os.close(root_fd)


def read_bounded_regular(path: Path, limit: int = 1024 * 1024) -> bytes:
    """Read one worker-controlled file safely: no symlink, regular file only, bounded.

    Used for fixed controller-designated paths inside the writable workspace (whose
    content the worker may replace at any time, including with a symlink).
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


def stored_bytes_sink(stored_root: Path) -> Callable[[str, bytes, CollectedFile], None]:
    """Build a collector sink that durably preserves exact bytes bound to each record.

    The relative paths come from the collector's own directory listings (never from path
    resolution), so they contain no ``..`` components and no symlinked segments; the
    destination tree is controller-owned and outside the worker workspace. Every stored
    copy is re-hashed from disk and must match the record, closing write-time races.
    """

    def sink(relative: str, data: bytes, record: CollectedFile) -> None:
        if hashlib.sha256(data).hexdigest() != record.sha256:
            raise RuntimeError(f"collected bytes do not match the record for {relative}")
        target = stored_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if sha256_file(target) != record.sha256:
            raise RuntimeError(f"stored copy does not bind to the record for {relative}")

    return sink


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "MAX_DEPTH",
    "MAX_ENTRIES",
    "MAX_FILE_BYTES",
    "CollectedFile",
    "SafeCollection",
    "collect_directory",
    "read_bounded_fd",
    "read_bounded_regular",
    "sha256_file",
    "stored_bytes_sink",
]
