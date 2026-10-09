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


@dataclass(frozen=True, slots=True)
class CollectedFile:
    """One regular file's collected identity: exact bytes were read through a pinned fd."""

    path: str  # relative POSIX path inside the collected root
    size: int
    sha256: str


@dataclass(slots=True)
class SafeCollection:
    """Collected regular files plus recorded refusals; refusals are failures, not skips."""

    files: dict[str, CollectedFile] = field(default_factory=dict)
    refusals: dict[str, str] = field(default_factory=dict)

    def digests(self) -> set[str]:
        return {record.sha256 for record in self.files.values()}


def _errno_label(error: OSError) -> str:
    code = error.errno or 0
    return errno.errorcode.get(code, f"errno-{code}")


def _walk(
    dir_fd: int,
    prefix: str,
    exclude: frozenset[str],
    out: SafeCollection,
    sink: Callable[[str, bytes, CollectedFile], None] | None,
) -> None:
    if len(out.files) + len(out.refusals) >= MAX_ENTRIES:
        out.refusals[prefix or "."] = "entry-budget-exhausted"
        return
    for name in sorted(os.listdir(dir_fd)):
        relative = prefix + name
        if len(out.files) + len(out.refusals) >= MAX_ENTRIES:
            out.refusals[relative] = "entry-budget-exhausted"
            return
        if prefix == "" and name in exclude:
            continue  # collected separately under its own explicit root
        try:
            st = os.lstat(name, dir_fd=dir_fd)
        except OSError as error:
            out.refusals[relative] = f"lstat-denied:{_errno_label(error)}"
            continue
        if stat.S_ISLNK(st.st_mode):
            out.refusals[relative] = "symlink-refused"
            continue
        if stat.S_ISDIR(st.st_mode):
            try:
                sub_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as error:
                # ELOOP here means a directory entry was swapped to a symlink after lstat.
                out.refusals[relative] = f"dir-open-denied:{_errno_label(error)}"
                continue
            try:
                _walk(sub_fd, relative + "/", exclude, out, sink)
            finally:
                os.close(sub_fd)
            continue
        if not stat.S_ISREG(st.st_mode):
            out.refusals[relative] = "not-regular-file"
            continue
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as error:
            out.refusals[relative] = f"open-denied:{_errno_label(error)}"
            continue
        try:
            opened = os.fstat(fd)  # authoritative for the object this descriptor reads
            if not stat.S_ISREG(opened.st_mode):
                out.refusals[relative] = "not-regular-file"
                continue
            data = _read_bounded(fd, opened)
        finally:
            os.close(fd)
        if data is None:
            out.refusals[relative] = "file-exceeds-size-bound"
            continue
        record = CollectedFile(relative, len(data), hashlib.sha256(data).hexdigest())
        if sink is not None:
            sink(relative, data, record)  # exact bytes delivered while still trusted in memory
        out.files[relative] = record


def _read_bounded(fd: int, opened: os.stat_result) -> bytes | None:
    if opened.st_size > MAX_FILE_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_FILE_BYTES:
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
        _walk(root_fd, "", exclude, out, sink)
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
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"worker-controlled path is not a regular file: {path}")
        if opened.st_size > limit:
            raise ValueError(f"worker-controlled path exceeds the read bound: {path}")
        data = _read_bounded(fd, opened)
    finally:
        os.close(fd)
    if data is None:
        raise ValueError(f"worker-controlled path grew beyond the read bound: {path}")
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
    "MAX_ENTRIES",
    "MAX_FILE_BYTES",
    "CollectedFile",
    "SafeCollection",
    "collect_directory",
    "read_bounded_regular",
    "sha256_file",
    "stored_bytes_sink",
]
