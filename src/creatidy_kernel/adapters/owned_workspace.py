# SPDX-License-Identifier: Apache-2.0
"""Descriptor-anchored local workspace ownership, capture and non-destructive retirement.

The controller alone may mutate the protected parent. Workers may mutate the leaf.
This is cooperative same-principal ownership, not a hostile-UID security boundary.
"""

import base64
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import cast

from creatidy_kernel.core.ownership import OwnershipConflict, ResourceScope


def _directory(path: Path) -> int:
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise OwnershipConflict("workspace parent must be canonical and absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class LocalOwnedWorkspace:
    """Retain ownership records outside the worker leaf, never adopt existing data."""

    def __init__(self, parent: Path, name: str, *, create: bool = False, max_bytes: int = 16 * 1024 * 1024) -> None:
        if not name or name in {".", ".."} or "/" in name or "\\" in name or max_bytes <= 0:
            raise OwnershipConflict("invalid owned workspace configuration")
        self._parent, self._name, self._max_bytes = parent, name, max_bytes
        parent_fd = _directory(parent)
        try:
            info = os.fstat(parent_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OwnershipConflict("workspace parent must be controller-owned and private")
            marker = f".{name}.ownership-v1"
            if create:
                # A partial mkdir/marker failure is retained, not adopted on retry.
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                root = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                record: dict[str, object] = {
                    "version": 1,
                    "parent": [info.st_dev, info.st_ino],
                    "root": [root.st_dev, root.st_ino],
                    "uid": os.getuid(),
                    "token": uuid.uuid4().hex,
                }
                raw = json.dumps(record, sort_keys=True).encode()
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                try:
                    with os.fdopen(fd, "wb", closefd=False) as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(parent_fd)
            fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                marker_info = os.fstat(fd)
                if (
                    not stat.S_ISREG(marker_info.st_mode)
                    or marker_info.st_uid != os.getuid()
                    or marker_info.st_size > 4096
                ):
                    raise OwnershipConflict("workspace ownership record unavailable")
                record = cast(dict[str, object], json.loads(os.read(fd, 4097)))
            finally:
                os.close(fd)
            if (
                set(record) != {"version", "parent", "root", "uid", "token"}
                or record["version"] != 1
                or record["uid"] != os.getuid()
                or record["parent"] != [info.st_dev, info.st_ino]
                or type(record["token"]) is not str
                or len(record["token"]) != 32
            ):
                raise OwnershipConflict("foreign or changed workspace ownership record")
            self._record = record
            self._retired = f".{name}.retired-{record['token']}"
        finally:
            os.close(parent_fd)

    @property
    def path(self) -> Path:
        return self._parent / self._name

    @property
    def resource(self) -> ResourceScope:
        return ResourceScope("workspace", str(self.path))

    def bind_authority(self, authority: str) -> None:
        """One durable capacity owner, including across separate SQLite files/processes."""
        retired = self.retirement()
        parent, root = (_directory(self._parent), None) if retired is not None else self._open()
        try:
            name = f".{self._name}.authority-v1"
            raw = json.dumps({"authority": authority, "owner": self._record}, sort_keys=True).encode()
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            except FileExistsError:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    if os.read(fd, len(raw) + 1) != raw:
                        raise OwnershipConflict(
                            "workspace belongs to another capacity authority; preserve original binding"
                        )
                finally:
                    os.close(fd)
            else:
                try:
                    with os.fdopen(fd, "wb", closefd=False) as output:
                        output.write(raw)
                        output.flush()
                        os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(parent)
        finally:
            if root is not None:
                os.close(root)
            os.close(parent)

    def retirement(self) -> tuple[str, bytes] | None:
        """Replay only an exact durable post-rmdir witness, never pathname absence."""
        parent = _directory(self._parent)
        try:
            info = os.fstat(parent)
            if self._record["parent"] != [info.st_dev, info.st_ino] or info.st_mode & 0o077:
                raise OwnershipConflict("workspace parent custody changed")
            try:
                fd = os.open(f".{self._name}.retirement-v1", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 65536:
                    raise OwnershipConflict("retirement witness unavailable")
                value = cast(dict[str, object], json.loads(os.read(fd, 65537)))
            finally:
                os.close(fd)
            if set(value) != {"version", "snapshot"} or value["version"] != 1 or type(value["snapshot"]) is not str:
                raise OwnershipConflict("retirement witness version/fields unavailable")
            raw = base64.b64decode(value["snapshot"], validate=True)
            snapshot = cast(dict[str, object], json.loads(raw))
            if snapshot.get("owner") != self._record or snapshot.get("entries") != []:
                raise OwnershipConflict("retirement witness does not cover exact empty owned resource")
            if os.path.lexists(self.path) or os.path.lexists(self._parent / self._retired):
                raise OwnershipConflict("retired resource identity changed or data reappeared; preserve it")
            return hashlib.sha256(raw).hexdigest(), raw
        finally:
            os.close(parent)

    def _open(self) -> tuple[int, int]:
        parent = _directory(self._parent)
        try:
            info = os.fstat(parent)
            if self._record["parent"] != [info.st_dev, info.st_ino] or info.st_mode & 0o077:
                raise OwnershipConflict("workspace parent custody changed")
            root = os.open(self._name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            info = os.fstat(root)
            if self._record["root"] != [info.st_dev, info.st_ino]:
                os.close(root)
                raise OwnershipConflict("workspace identity changed; explicit reconciliation required")
            return parent, root
        except BaseException:
            os.close(parent)
            raise

    def capture(self) -> tuple[str, bytes]:
        parent, root = self._open()
        try:

            def scan() -> bytes:
                entries: list[dict[str, object]] = []
                retained = 0

                def stamp(info: os.stat_result) -> tuple[int, ...]:
                    # Reading may update atime. That is not an edit; inode,
                    # type/permissions, bytes and change/modify times are.
                    return (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                        info.st_nlink,
                        info.st_uid,
                        info.st_gid,
                    )

                def visit(fd: int, prefix: str) -> None:
                    nonlocal retained
                    before = os.fstat(fd)
                    for name in sorted(os.listdir(fd)):
                        if len(entries) >= 10_000:
                            raise OwnershipConflict("workspace capture entry bound exceeded")
                        path = f"{prefix}{name}"
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        entry: dict[str, object] = {
                            "path": path,
                            "mode": info.st_mode,
                            "inode": [info.st_dev, info.st_ino],
                            "mtime": info.st_mtime_ns,
                            "ctime": info.st_ctime_ns,
                        }
                        entries.append(entry)
                        if stat.S_ISLNK(info.st_mode):
                            entry["symlink"] = os.readlink(name, dir_fd=fd)
                        elif stat.S_ISDIR(info.st_mode):
                            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            try:
                                if stamp(os.fstat(child)) != stamp(info):
                                    raise OwnershipConflict("workspace changed during capture")
                                visit(child, path + "/")
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(info.st_mode):
                            if info.st_nlink != 1:
                                raise OwnershipConflict(
                                    "multiply linked file has unknown external ownership; preserve it"
                                )
                            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                            try:
                                if stamp(os.fstat(child)) != stamp(info) or retained + info.st_size > self._max_bytes:
                                    raise OwnershipConflict("workspace changed or capture byte bound exceeded")
                                chunks: list[bytes] = []
                                while chunk := os.read(child, min(65536, self._max_bytes - retained + 1)):
                                    retained += len(chunk)
                                    if retained > self._max_bytes:
                                        raise OwnershipConflict("workspace capture byte bound exceeded")
                                    chunks.append(chunk)
                                if stamp(os.fstat(child)) != stamp(info):
                                    raise OwnershipConflict("workspace changed during capture")
                                entry["bytes"] = base64.b64encode(b"".join(chunks)).decode("ascii")
                            finally:
                                os.close(child)
                        else:
                            raise OwnershipConflict("unsupported workspace entry; preserve unknown data")
                    after = os.fstat(fd)
                    if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                        raise OwnershipConflict("workspace changed during capture")

                visit(root, "")
                info = os.fstat(root)
                return json.dumps(
                    {
                        "version": 1,
                        "owner": self._record,
                        "entries": entries,
                        "root": {"mode": info.st_mode, "mtime": info.st_mtime_ns, "ctime": info.st_ctime_ns},
                    },
                    sort_keys=True,
                ).encode()

            first, second = scan(), scan()
            current = os.stat(self._name, dir_fd=parent, follow_symlinks=False)
            if first != second or self._record["root"] != [current.st_dev, current.st_ino]:
                raise OwnershipConflict("workspace changed during capture")
            return hashlib.sha256(first).hexdigest(), first
        finally:
            os.close(root)
            os.close(parent)

    def retire_empty(self, subject: str) -> None:
        retired = self.retirement()
        if retired is not None:
            if retired[0] != subject:
                raise OwnershipConflict("retirement replay subject differs")
            return
        captured_subject, captured = self.capture()
        if captured_subject != subject:
            raise OwnershipConflict("cleanup subject changed")
        parent, root = self._open()
        try:
            if os.listdir(root):
                raise OwnershipConflict("nonempty workspace retained, including artifacts and unknown user data")
            # The protected parent is not worker-writable. Move into controller
            # custody before checking identity and using nonrecursive rmdir. A
            # concurrent leaf write makes rmdir fail; changed identities are kept.
            if os.path.lexists(self._parent / self._retired):
                raise OwnershipConflict("interrupted retirement retained; explicit reconciliation required")
            os.rename(self._name, self._retired, src_dir_fd=parent, dst_dir_fd=parent)
            moved = os.stat(self._retired, dir_fd=parent, follow_symlinks=False)
            if self._record["root"] != [moved.st_dev, moved.st_ino]:
                raise OwnershipConflict("cleanup race changed identity; quarantined data retained")
            os.rmdir(self._retired, dir_fd=parent)
            os.fsync(parent)
            fd = os.open(
                f".{self._name}.retirement-v1",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                raw = json.dumps({"version": 1, "snapshot": base64.b64encode(captured).decode("ascii")}).encode()
                with os.fdopen(fd, "wb", closefd=False) as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent)
        finally:
            os.close(root)
            os.close(parent)
