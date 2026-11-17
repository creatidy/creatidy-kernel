# SPDX-License-Identifier: Apache-2.0
"""Authorized bounded file snapshots, not a document store or remote fallback."""

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from creatidy_kernel.adapters.project_drivers import native_fact, unavailable_fact
from creatidy_kernel.core.drivers import DriverBinding, DriverFact, DriverSubject
from creatidy_kernel.core.forge import Presence


class LocalDocumentKnowledge:
    def __init__(
        self,
        binding: DriverBinding,
        root: Path,
        clock: Callable[[], int],
        authorize: Callable[[DriverSubject, int], bool],
        *,
        max_bytes: int = 1_000_000,
        ttl: int = 30,
    ) -> None:
        if (binding.family, binding.implementation, binding.api_version) != (
            "knowledge",
            "local-documents",
            "file-snapshot-v1",
        ):
            raise ValueError("explicit local document source contract required")
        if (
            not root.is_absolute()
            or ".." in root.parts
            or len(str(root)) > 4096
            or len(root.parts) > 128
            or type(max_bytes) is not int
            or not 0 < max_bytes <= 16_000_000
            or type(ttl) is not int
            or not 0 < ttl <= 3600
            or not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
        ):
            raise ValueError("bounded no-follow local file profile required")
        self.binding, self.root, self.clock, self.authorize = binding, root, clock, authorize
        self.max_bytes, self.ttl = max_bytes, ttl
        self._configuration = (binding, root, max_bytes, ttl, authorize)
        descriptors: list[int] = []
        try:
            descriptor = self._root(descriptors)
            info = os.fstat(descriptor)
            self.root_identity = (info.st_dev, info.st_ino)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _root(self, descriptors: list[int]) -> int:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in self.root.parts[1:]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        return descriptor

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)

    def snapshot(self, key: str) -> DriverFact:
        return self._read(key, None)

    def content(self, key: str, revision: str) -> DriverFact:
        if (
            not revision.startswith("sha256:")
            or len(revision) != 71
            or any(value not in "0123456789abcdef" for value in revision[7:])
        ):
            raise ValueError("exact local content snapshot digest required")
        return self._read(key, revision)

    def _read(self, key: str, revision: str | None) -> DriverFact:
        path = PurePosixPath(key)
        if (
            not path.parts
            or path.is_absolute()
            or path.as_posix() != key
            or len(key) > 512
            or len(path.parts) > 32
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in key
            or "\x00" in key
        ):
            raise ValueError("bounded relative document key required")
        selected = DriverSubject(self.binding, "document", key, revision)
        now = self.clock()
        descriptors: list[int] = []
        try:
            if (self.binding, self.root, self.max_bytes, self.ttl, self.authorize) != self._configuration:
                raise ValueError("local source configuration changed")
            if self.authorize(selected, now) is not True:
                raise PermissionError("local context permission unavailable")
            directory = self._root(descriptors)
            root = os.fstat(directory)
            if (root.st_dev, root.st_ino) != self.root_identity:
                raise ValueError("local source root changed")
            for part in path.parts[:-1]:
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                descriptors.append(directory)
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > self.max_bytes:
                raise ValueError("unsupported local context file")
            if not before.st_mode & 0o444:
                raise PermissionError("local context file read permission unavailable")
            chunks: list[bytes] = []
            remaining = self.max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            actual = "sha256:" + hashlib.sha256(data).hexdigest()
            if len(data) > self.max_bytes or len(data) != before.st_size or revision is not None and actual != revision:
                raise ValueError("local content bytes differ from the selected snapshot")
            if self.authorize(DriverSubject(self.binding, "document", key, actual), self.clock()) is not True:
                raise PermissionError("local context permission withdrawn")
            current_directory = self._root(descriptors)
            current_root = os.fstat(current_directory)
            if (current_root.st_dev, current_root.st_ino) != self.root_identity:
                raise ValueError("local source root changed during read")
            for part in path.parts[:-1]:
                current_directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current_directory
                )
                descriptors.append(current_directory)
            closing = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current_directory)
            descriptors.append(closing)
            identity = self._identity(before)
            if (
                identity != self._identity(os.fstat(descriptor))
                or identity != self._identity(os.fstat(closing))
                or not now <= self.clock() <= now + self.ttl
                or (self.binding, self.root, self.max_bytes, self.ttl, self.authorize) != self._configuration
            ):
                raise ValueError("local context changed, withdrawn or expired")
            return native_fact(
                self.binding,
                "document",
                key,
                actual,
                now,
                self.ttl,
                {
                    "content_hex": data.hex(),
                    "sha256": actual[7:],
                    "root_identity": list(self.root_identity),
                    "file_identity": list(identity),
                },
                qualification="authorized-local-file-snapshot;presence-and-rights-rechecked;not-a-document-version-service",
            )
        except PermissionError:
            return native_fact(
                self.binding,
                "document",
                key,
                None,
                now,
                0,
                presence=Presence.INACCESSIBLE,
                complete=False,
                qualification="local-read-permission-unavailable",
            )
        except (OSError, ValueError, OverflowError):
            return unavailable_fact(self.binding, "document", key, now, 0)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
