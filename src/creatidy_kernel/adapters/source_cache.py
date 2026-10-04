# SPDX-License-Identifier: Apache-2.0
"""Controller-owned read-only source cache acquired from the canonical task remote.

TaskSpec owns canonical repository identity, so normal operation never requires a
user checkout path: the controller acquires its own disposable cache checkout from
the exact canonical HTTPS remote, re-verifies that identity before every use, and
only reads from the remote. This is not a repository discovery framework: there is
no sibling-directory, home-relative, or workspace-layout search anywhere. The cache
namespace is controller-owned; a foreign or corrupt entry there is replaced, never
adopted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from creatidy_kernel.adapters.forge_refs import https_origin
from creatidy_kernel.adapters.forgejo_transport import run_git_bounded

CACHE_NAMESPACE = ("creatidy-kernel", "source")
GIT_TIMEOUT_SECONDS = 300
GIT_VERIFY_TIMEOUT_SECONDS = 60
MAX_GIT_OUTPUT_BYTES = 65536
ACQUIRED_MARKER = "kernel-source-cache-v1"
_REPOSITORY_KEY = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-")


class SourceCategory(StrEnum):
    """Closed safe acquisition vocabulary; never remote output or free text."""

    CLONE_FAILED = "clone_failed"
    FETCH_FAILED = "fetch_failed"
    REMOTE_MISMATCH = "remote_mismatch"
    UNSUITABLE_CACHE = "unsuitable_cache"


class SourceAcquisitionError(RuntimeError):
    """Safe acquisition refusal carrying its closed category."""

    def __init__(self, category: SourceCategory, message: str) -> None:
        self.category = category
        super().__init__(message)


def default_cache_root(environ: Mapping[str, str] | None = None) -> Path:
    """Platform-appropriate user-local cache root; XDG-compatible on Linux."""
    values = os.environ if environ is None else environ
    override = values.get("XDG_CACHE_HOME", "")
    base = Path(override) if override and Path(override).is_absolute() else Path.home() / ".cache"
    return base.joinpath(*CACHE_NAMESPACE)


def source_cache_key(repository_url: str) -> tuple[str, ...]:
    """Deterministic cache location for one canonical repository URL."""
    path = urlsplit(repository_url).path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = path.split("/") if path else []
    if len(parts) < 2 or any(not part or part in {".", ".."} or set(part) - _REPOSITORY_KEY for part in parts):
        raise SourceAcquisitionError(
            SourceCategory.REMOTE_MISMATCH,
            "canonical repository URL must be an HTTPS origin with a plain multi-segment path",
        )
    return tuple(parts)


def _refuse_symlink_components(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise SourceAcquisitionError(
            SourceCategory.UNSUITABLE_CACHE,
            "cache location must not contain symlinked path components",
        )


def _discard(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _git(
    binary: str,
    working_directory: Path,
    environment: Mapping[str, str],
    arguments: tuple[str, ...],
    *,
    failure: SourceCategory,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_git_bounded(
            [binary, *arguments], working_directory, dict(environment), timeout, MAX_GIT_OUTPUT_BYTES
        )
    except (TimeoutError, OverflowError, OSError) as error:
        raise SourceAcquisitionError(
            failure, "Git source acquisition did not complete within its bounded window"
        ) from error


def _mandatory_git(
    binary: str,
    working_directory: Path,
    environment: Mapping[str, str],
    arguments: tuple[str, ...],
    *,
    failure: SourceCategory,
    message: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    completed = _git(binary, working_directory, environment, arguments, failure=failure, timeout=timeout)
    if completed.returncode != 0:
        raise SourceAcquisitionError(failure, message)
    return completed


def _git_environment(root: Path, search_path: str, askpass: Path | None) -> dict[str, str]:
    # Closed transport environment mirroring the conditional Git push transport:
    # no ambient Git configuration and no ambient credential helpers ever apply.
    environment = {
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root),
        "PATH": search_path,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if askpass is not None:
        environment["GIT_ASKPASS"] = str(askpass)
    return environment


def _canonical_origin_verified(cache: Path, binary: str, environment: Mapping[str, str], repository_url: str) -> None:
    completed = _git(
        binary,
        cache,
        environment,
        ("config", "--get", "remote.origin.url"),
        failure=SourceCategory.UNSUITABLE_CACHE,
        timeout=GIT_VERIFY_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0 or completed.stdout.strip() not in {repository_url, repository_url + ".git"}:
        raise SourceAcquisitionError(
            SourceCategory.REMOTE_MISMATCH,
            "cached source origin differs from the exact canonical task repository",
        )


def _fresh_clone(
    binary: str,
    root: Path,
    environment: Mapping[str, str],
    repository_url: str,
    transport: str,
    target: Path,
    *,
    timeout: int,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{target.name}.staging-{os.getpid()}"
    _discard(staging)
    completed = _git(
        binary,
        root.parent,
        environment,
        (
            "clone",
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "--template=",
            transport,
            str(staging),
        ),
        failure=SourceCategory.CLONE_FAILED,
        timeout=timeout,
    )
    if completed.returncode != 0:
        _discard(staging)
        raise SourceAcquisitionError(
            SourceCategory.CLONE_FAILED,
            "source acquisition from the canonical repository failed; check remote reachability and credentials",
        )
    try:
        if transport != repository_url:
            _mandatory_git(
                binary,
                staging,
                environment,
                ("remote", "set-url", "origin", repository_url),
                failure=SourceCategory.REMOTE_MISMATCH,
                message="cached source origin could not be bound to the canonical task repository",
                timeout=GIT_VERIFY_TIMEOUT_SECONDS,
            )
        _canonical_origin_verified(staging, binary, environment, repository_url)
        (staging / ".git" / ACQUIRED_MARKER).write_text("1\n", encoding="ascii")
        _discard(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(target)
    except SourceAcquisitionError:
        _discard(staging)
        raise
    except OSError as error:
        _discard(staging)
        raise SourceAcquisitionError(SourceCategory.UNSUITABLE_CACHE, "cache location could not be prepared") from error
    return target


def acquire_source(
    repository_url: str,
    *,
    cache_root: Path | None = None,
    clone_from: str | None = None,
    askpass: Path | None = None,
    environment: Mapping[str, str] | None = None,
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> Path:
    """Return the controller-owned read-only cache checkout of the canonical remote.

    ``clone_from`` is an explicit advanced mirror transport (used by tests and by
    explicit operator mirrors); the acquired cache origin is always rebound and
    verified against the exact canonical URL. Acquisition never writes to the
    remote and never searches the filesystem for candidate checkouts. Callers
    treat the returned checkout as read-only; the disposable candidate workspace
    remains a separate controller-owned tree.
    """
    https_origin(repository_url)
    values = os.environ if environment is None else environment
    root = cache_root if cache_root is not None else default_cache_root(values)
    target = root.joinpath(*source_cache_key(repository_url))
    _refuse_symlink_components(root)
    _refuse_symlink_components(target)
    binary = shutil.which("git", path=values.get("PATH") or os.defpath)
    if binary is None or not Path(binary).is_absolute():
        raise SourceAcquisitionError(SourceCategory.UNSUITABLE_CACHE, "absolute Git executable required")
    search_path = values.get("PATH") or os.defpath
    git_environment = _git_environment(root.parent, search_path, askpass)
    transport = clone_from if clone_from else repository_url

    usable = (
        target.is_dir()
        and not target.is_symlink()
        and (target / ".git").is_dir()
        and not (target / ".git").is_symlink()
    )
    if usable:
        try:
            _canonical_origin_verified(target, binary, git_environment, repository_url)
        except SourceAcquisitionError as error:
            if error.category is not SourceCategory.REMOTE_MISMATCH:
                raise
            usable = False
    if usable:
        status = _git(
            binary,
            target,
            git_environment,
            ("status", "--porcelain"),
            failure=SourceCategory.UNSUITABLE_CACHE,
            timeout=GIT_VERIFY_TIMEOUT_SECONDS,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise SourceAcquisitionError(
                SourceCategory.UNSUITABLE_CACHE,
                "cached source is not a clean controller-owned checkout; delete the cache directory to repair",
            )
        # Refreshing against the exact canonical remote keeps the cache faithful to
        # TaskSpec authority; a failed refresh must never pass as readiness.
        _mandatory_git(
            binary,
            target,
            git_environment,
            ("fetch", "--prune", "origin"),
            failure=SourceCategory.FETCH_FAILED,
            message="refreshing the controller source cache failed; check canonical remote reachability",
            timeout=timeout,
        )
        return target
    if target.exists():
        _discard(target)
    return _fresh_clone(binary, root, git_environment, repository_url, transport, target, timeout=timeout)
