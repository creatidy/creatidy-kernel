# SPDX-License-Identifier: Apache-2.0
"""Host-qualified acquisition and finite current-use leases.

Unknown entries and interrupted staging trees are preserved, never deleted or
adopted. A Path alone is not a lease. Hold source_use through the validated
controller object copy, not through the duration of a model session. This is a
trusted local controller protocol, not protection against hostile same-UID code.
"""

from __future__ import annotations

import configparser
import ctypes
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from creatidy_kernel.adapters.forge_refs import https_origin, oid, repository_path
from creatidy_kernel.adapters.forgejo_transport import GIT_LEASE_FDS, GIT_OUTPUT_ERRORS, run_git_bounded
from creatidy_kernel.adapters.sqlite_store import UnsupportedSQLiteConfiguration, validate_local_storage
from creatidy_kernel.core.forge import Reference

CACHE_NAMESPACE = ("creatidy-kernel", "source")
GIT_TIMEOUT_SECONDS = 300
GIT_VERIFY_TIMEOUT_SECONDS = 60
MAX_GIT_OUTPUT_BYTES = 65536
ACQUIRED_MARKER = "kernel-source-cache-v2"
LOCK_TIMEOUT_SECONDS = 1.0


class SourceCategory(StrEnum):
    """Closed diagnostics; Git output and remote values are never messages."""

    CLONE_FAILED = "clone_failed"
    FETCH_FAILED = "fetch_failed"
    REMOTE_MISMATCH = "remote_mismatch"
    UNSUITABLE_CACHE = "unsuitable_cache"
    OWNERSHIP_UNPROVEN = "ownership_unproven"
    LOCK_BUSY = "source_busy"
    CHECKOUT_UNSUITABLE = "checkout_unsuitable"
    CHECKOUT_DIRTY = "checkout_dirty"
    BASE_UNAVAILABLE = "base_unavailable"
    BASELINE_INVALID = "task_baseline_invalid"


class SourceAcquisitionError(RuntimeError):
    def __init__(self, category: SourceCategory, message: str) -> None:
        self.category = category
        super().__init__(message)


def _refuse(category: SourceCategory = SourceCategory.UNSUITABLE_CACHE) -> SourceAcquisitionError:
    return SourceAcquisitionError(
        category, f"source refused: {category.value}; preserve data and inspect controller placement"
    )


def default_cache_root(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    override = values.get("XDG_CACHE_HOME", "")
    base = Path(override) if override and Path(override).is_absolute() else Path.home() / ".cache"
    return base.joinpath(*CACHE_NAMESPACE)


def canonical_repository(repository_url: str) -> str:
    """Reuse Forge origin/repository syntax without changing TaskSpec identity."""
    try:
        if any(ord(char) < 33 or ord(char) == 127 for char in repository_url) or urlsplit(
            repository_url
        ).netloc.endswith(":"):
            raise ValueError("ambiguous repository URL")
        origin = https_origin(repository_url)
        path = urlsplit(repository_url).path
        if not path.startswith("/") or path.endswith("/"):
            raise ValueError("ambiguous repository path")
        path = path[1:].removesuffix(".git")
        return origin + "/" + repository_path(Reference("forgejo:" + path))
    except (ValueError, RuntimeError):
        raise _refuse(SourceCategory.REMOTE_MISMATCH) from None


def source_cache_key(repository_url: str) -> tuple[str, ...]:
    canonical = canonical_repository(repository_url)
    origin = https_origin(canonical)
    return ("v2", hashlib.sha256(origin.encode("ascii")).hexdigest(), *urlsplit(canonical).path[1:].split("/"))


def _path(path: Path) -> None:
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise _refuse()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise _refuse()


def _identity(path: Path, *, directory: bool = True) -> list[int]:
    _path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
            or (not directory and info.st_nlink != 1)
            or (info.st_dev, info.st_ino) != (path.lstat().st_dev, path.lstat().st_ino)
        ):
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        return [info.st_dev, info.st_ino, info.st_uid]
    finally:
        os.close(descriptor)


def _json(path: Path) -> dict[str, object]:
    before = _identity(path, directory=False)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if [info.st_dev, info.st_ino, info.st_uid] != before:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
    return cast(dict[str, object], value)


def _write_new(path: Path, value: Mapping[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def validate_checkout(source: Path, *, bare: bool = False) -> None:
    """Refuse executable local Git config BEFORE invoking Git; never write source.

    The small accepted grammar excludes includes, filters, fsmonitor, hooks,
    credentials, URL rewrites, alternate objects and linked worktrees.
    """
    _path(source)
    validate_local_storage(source)
    _identity(source)
    gitdir = source if bare else source / ".git"
    _identity(gitdir)
    validate_local_storage(gitdir)
    mounts = {
        line.split()[4].replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
    }
    if not bare and str(gitdir) in mounts:
        raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)
    pending = [gitdir]
    count = 0
    device = gitdir.stat().st_dev
    while pending:
        parent = pending.pop()
        for child in parent.iterdir():
            count += 1
            info = child.lstat()
            if (
                count > 1000000
                or child.is_symlink()
                or info.st_dev != device
                or str(child) in mounts
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or info.st_uid != os.getuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
            ):
                raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
    for relative in (
        "commondir",
        "config.worktree",
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "refs/replace",
    ):
        if (gitdir / relative).exists():
            raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)
    config_path = gitdir / "config"
    config_identity = _identity(config_path, directory=False)
    descriptor = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if [info.st_dev, info.st_ino, info.st_uid] != config_identity:
            raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)
        raw = stream.read(MAX_GIT_OUTPUT_BYTES + 1)
    if len(raw) > MAX_GIT_OUTPUT_BYTES:
        raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)
    config = configparser.RawConfigParser(strict=True)
    try:
        config.read_string(raw.decode("utf-8"))
    except (configparser.Error, UnicodeError):
        raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE) from None
    allowed = {
        "core": {"repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase", "precomposeunicode"},
        'remote "origin"': {"url", "fetch"},
        "extensions": {"objectformat"},
    }
    for section in config.sections():
        keys = {"remote", "merge"} if section.startswith('branch "') and section.endswith('"') else allowed.get(section)
        if keys is None or set(config[section]) - keys:
            raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)


@contextmanager
def _lock(root: Path, repository_url: str, *, existing_only: bool = False) -> Generator[None]:
    canonical = canonical_repository(repository_url)
    _path(root)
    parent = root
    while not parent.exists():
        parent = parent.parent
    validate_local_storage(parent)
    import fcntl  # Linux-only, after the existing native filesystem/platform gate.

    if (root / ".git").exists():
        raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
    if not existing_only:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_identity = _identity(root)
    locks = root / ".locks-v2"
    if not existing_only:
        locks.mkdir(exist_ok=True, mode=0o700)
    _identity(locks)
    validate_local_storage(locks)
    path = locks / hashlib.sha256(canonical.encode("ascii")).hexdigest()
    created = False
    if existing_only:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    else:
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        identity = _identity(path, directory=False)
        info = os.fstat(descriptor)
        if identity != [info.st_dev, info.st_ino, info.st_uid]:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        validate_local_storage(locks, database_path=path)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise _refuse(SourceCategory.LOCK_BUSY) from error
                time.sleep(0.01)
        expected = {"schema": 2, "repository": canonical, "root": root_identity, "lock": identity}
        if created:
            if root.joinpath(*source_cache_key(canonical)).exists():
                raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
            os.write(descriptor, json.dumps({**expected, "acquisition": None}, sort_keys=True).encode("ascii"))
            os.fsync(descriptor)
        else:
            recorded = _json(path)
            if set(recorded) != {*expected, "acquisition"} or any(
                recorded.get(key) != value for key, value in expected.items()
            ):
                raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        if _identity(path, directory=False) != identity or _identity(root) != root_identity:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        # Native Git retains this SAME open-file-description lease if the
        # controller dies. Context-local descriptors never reach model sessions.
        lease = GIT_LEASE_FDS.set((*GIT_LEASE_FDS.get(), descriptor))
        try:
            yield
            if _identity(path, directory=False) != identity or _identity(root) != root_identity:
                raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        finally:
            GIT_LEASE_FDS.reset(lease)
    finally:
        # Never unlink a lock: waiters must always contend on the same inode.
        os.close(descriptor)


def _owned(target: Path, root: Path, repository_url: str) -> None:
    marker = _json(target / ".git" / ACQUIRED_MARKER)
    expected = {
        "schema": 2,
        "repository": canonical_repository(repository_url),
        "location": str(root.joinpath(*source_cache_key(repository_url))),
        "root": _identity(root),
        "checkout": _identity(target),
        "gitdir": _identity(target / ".git"),
        "lock": _identity(
            root / ".locks-v2" / hashlib.sha256(canonical_repository(repository_url).encode("ascii")).hexdigest(),
            directory=False,
        ),
    }
    token = marker.get("token")
    if (
        any(marker.get(key) != value for key, value in expected.items())
        or not isinstance(token, str)
        or len(token) != 64
    ):
        raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
    lock_path = root / ".locks-v2" / hashlib.sha256(canonical_repository(repository_url).encode("ascii")).hexdigest()
    if _json(lock_path).get("acquisition") != marker:
        raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)


def _record_acquisition(root: Path, repository_url: str, marker: dict[str, object]) -> None:
    """Bind the marker token to controller metadata on the SAME locked inode.

    A crash during this finite write makes metadata unproven, not a usable cache.
    Retain bytes and refuse in that case; no ownership is inferred from checkout.
    """
    lock_path = root / ".locks-v2" / hashlib.sha256(canonical_repository(repository_url).encode("ascii")).hexdigest()
    recorded = _json(lock_path)
    if recorded.get("lock") != marker["lock"] or recorded.get("root") != marker["root"]:
        raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if [info.st_dev, info.st_ino, info.st_uid] != marker["lock"]:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        raw = json.dumps({**recorded, "acquisition": marker}, sort_keys=True).encode("ascii")
        if len(raw) > 8192:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        if os.write(descriptor, raw) != len(raw):
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        os.ftruncate(descriptor, len(raw))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def source_use(source: Path, repository_url: str) -> Generator[Path]:
    """Lease an owned qualified cache; explicit/historical paths remain literal."""
    key = source_cache_key(repository_url)
    if tuple(source.parts[-len(key) :]) == key:
        root = source.parents[len(key) - 1]
        try:
            with _lock(root, repository_url, existing_only=True):
                _owned(source, root, repository_url)
                before = (_identity(source), _identity(source / ".git"))
                yield source
                _owned(source, root, repository_url)
                if before != (_identity(source), _identity(source / ".git")):
                    raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        except (OSError, ValueError, configparser.Error, UnsupportedSQLiteConfiguration):
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN) from None
    else:
        yield source


def _git_environment(root: Path, search_path: str, askpass: Path | None) -> dict[str, str]:
    environment = {
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root),
        "PATH": search_path,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "6",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "http.followRedirects",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "http.sslVerify",
        "GIT_CONFIG_VALUE_2": "true",
        "GIT_CONFIG_KEY_3": "core.hooksPath",
        "GIT_CONFIG_VALUE_3": os.devnull,
        "GIT_CONFIG_KEY_4": "core.fsmonitor",
        "GIT_CONFIG_VALUE_4": "false",
        "GIT_CONFIG_KEY_5": "gc.auto",
        "GIT_CONFIG_VALUE_5": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    if askpass is not None:
        _path(askpass)
        if not askpass.is_file() or not os.access(askpass, os.X_OK) or askpass.is_relative_to(root):
            raise _refuse()
        environment["GIT_ASKPASS"] = str(askpass)
    return environment


def _git(
    binary: str,
    cwd: Path,
    environment: Mapping[str, str],
    arguments: tuple[str, ...],
    *,
    failure: SourceCategory,
    timeout: int,
    max_bytes: int = MAX_GIT_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    decoding = GIT_OUTPUT_ERRORS.set("strict")
    try:
        completed = run_git_bounded([binary, *arguments], cwd, dict(environment), timeout, max_bytes)
    except (TimeoutError, OverflowError, OSError, subprocess.TimeoutExpired, UnicodeError):
        raise _refuse(failure) from None
    finally:
        GIT_OUTPUT_ERRORS.reset(decoding)
    if completed.returncode != 0:
        raise _refuse(failure)
    return completed


def _origin(target: Path, binary: str, environment: Mapping[str, str], repository_url: str) -> None:
    origin = _git(
        binary,
        target,
        environment,
        ("config", "--local", "--no-includes", "--get", "remote.origin.url"),
        failure=SourceCategory.REMOTE_MISMATCH,
        timeout=GIT_VERIFY_TIMEOUT_SECONDS,
    ).stdout.strip()
    if canonical_repository(origin) != canonical_repository(repository_url):
        raise _refuse(SourceCategory.REMOTE_MISMATCH)


def source_git(source: Path, *arguments: str, max_bytes: int = MAX_GIT_OUTPUT_BYTES) -> str:
    """Bounded read-only Git after validate_checkout, with no ambient config."""
    binary = shutil.which("git", path=os.defpath)
    if binary is None:
        raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)
    return _git(
        binary,
        source,
        _git_environment(Path("/nonexistent"), os.defpath, None),
        arguments,
        failure=SourceCategory.BASE_UNAVAILABLE,
        timeout=GIT_VERIFY_TIMEOUT_SECONDS,
        max_bytes=max_bytes,
    ).stdout


def copy_source_objects(source: Path, target: Path, *, base: str) -> None:
    """Copy, not hardlink, source objects using the same bounded closed Git seam."""
    validate_checkout(source)
    oid(base)
    binary = shutil.which("git", path=os.defpath)
    if binary is None:
        raise _refuse(SourceCategory.CHECKOUT_UNSUITABLE)
    _git(
        binary,
        source,
        _git_environment(Path("/nonexistent"), os.defpath, None),
        ("clone", "--no-local", "--bare", "--template=", str(source), str(target)),
        failure=SourceCategory.CLONE_FAILED,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    # A bare clone omits remote-tracking-only history. Transfer the admitted
    # exact commit explicitly, and retain a local ref for the workspace clone.
    _git(
        binary,
        target,
        _git_environment(Path("/nonexistent"), os.defpath, None),
        ("fetch", "--no-tags", "--no-write-fetch-head", str(source), f"+{base}:refs/heads/kernel-source-base"),
        failure=SourceCategory.CLONE_FAILED,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if source_git(target, "rev-parse", "--verify", "refs/heads/kernel-source-base^{commit}").strip() != base:
        raise _refuse(SourceCategory.BASE_UNAVAILABLE)


def _publish(staging: Path, target: Path) -> None:
    """Linux atomic no-replace publication; no unchecked rename fallback."""
    parent_identity = _identity(target.parent)
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if [info.st_dev, info.st_ino, info.st_uid] != parent_identity or staging.parent != target.parent:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        try:
            rename = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as error:
            raise _refuse() from error
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(descriptor, os.fsencode(staging.name), descriptor, os.fsencode(target.name), 1) != 0:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
        os.fsync(descriptor)
        if _identity(target.parent) != parent_identity:
            raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
    finally:
        os.close(descriptor)


def acquire_source(
    repository_url: str,
    *,
    cache_root: Path | None = None,
    clone_from: str | None = None,
    askpass: Path | None = None,
    environment: Mapping[str, str] | None = None,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> Path:
    """Return an UNLEASED compatibility Path after locked acquisition/refresh.

    An explicit local mirror supplies transport only, never canonical identity.
    Failed/interrupted trees are retained, never recursively deleted or adopted.
    This policy needs no migration/deletion authority over historical data.
    """
    canonical = canonical_repository(repository_url)
    if type(timeout) is not int or not 0 < timeout <= GIT_TIMEOUT_SECONDS:
        raise _refuse()
    values = os.environ if environment is None else environment
    root = cache_root if cache_root is not None else default_cache_root(values)
    target = root.joinpath(*source_cache_key(canonical))
    binary = shutil.which("git", path=values.get("PATH") or os.defpath)
    if binary is None or not Path(binary).is_absolute():
        raise _refuse()
    if Path(binary).resolve().is_relative_to(root):
        raise _refuse()
    try:
        with _lock(root, canonical):
            _path(target)
            git_environment = _git_environment(root, values.get("PATH") or os.defpath, askpass)
            if target.exists():
                _owned(target, root, canonical)
                validate_checkout(target)
                _origin(target, binary, git_environment, canonical)
                if _git(
                    binary,
                    target,
                    git_environment,
                    ("status", "--porcelain"),
                    failure=SourceCategory.UNSUITABLE_CACHE,
                    timeout=GIT_VERIFY_TIMEOUT_SECONDS,
                ).stdout.strip():
                    raise _refuse(SourceCategory.UNSUITABLE_CACHE)
                _git(
                    binary,
                    target,
                    git_environment,
                    ("fetch", "--prune", "origin"),
                    failure=SourceCategory.FETCH_FAILED,
                    timeout=timeout,
                )
                _owned(target, root, canonical)
                return target
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for parent in (target.parent, *target.parents[: len(source_cache_key(canonical)) - 1]):
                _identity(parent)
                validate_local_storage(parent)
            staging = Path(tempfile.mkdtemp(prefix=".source-staging-", dir=target.parent))
            identity = _identity(staging)
            transport = clone_from if clone_from is not None else canonical
            if clone_from is not None:
                mirror = Path(clone_from)
                if not mirror.exists():
                    raise _refuse(SourceCategory.CLONE_FAILED)
                validate_checkout(mirror)
                if Path(binary).resolve().is_relative_to(mirror) or (
                    askpass is not None and askpass.resolve().is_relative_to(mirror)
                ):
                    raise _refuse()
                transport = str(mirror)
            _git(
                binary,
                root,
                git_environment,
                ("clone", "--no-local", "--template=", transport, str(staging)),
                failure=SourceCategory.CLONE_FAILED,
                timeout=timeout,
            )
            if _identity(staging) != identity:
                raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
            validate_checkout(staging)
            if transport != canonical:
                _git(
                    binary,
                    staging,
                    git_environment,
                    ("remote", "set-url", "origin", canonical),
                    failure=SourceCategory.REMOTE_MISMATCH,
                    timeout=GIT_VERIFY_TIMEOUT_SECONDS,
                )
            _origin(staging, binary, git_environment, canonical)
            _write_new(
                staging / ".git" / ACQUIRED_MARKER,
                {
                    "schema": 2,
                    "repository": canonical,
                    "location": str(target),
                    "root": _identity(root),
                    "checkout": identity,
                    "gitdir": _identity(staging / ".git"),
                    "token": secrets.token_hex(32),
                    "lock": _identity(
                        root / ".locks-v2" / hashlib.sha256(canonical.encode("ascii")).hexdigest(), directory=False
                    ),
                },
            )
            if target.exists() or target.is_symlink():
                raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN)
            _record_acquisition(root, canonical, _json(staging / ".git" / ACQUIRED_MARKER))
            _owned(staging, root, canonical)
            _publish(staging, target)
            _owned(target, root, canonical)
            return target
    except SourceAcquisitionError:
        raise
    except (OSError, ValueError, configparser.Error, UnsupportedSQLiteConfiguration):
        raise _refuse(SourceCategory.OWNERSHIP_UNPROVEN) from None
