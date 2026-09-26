# SPDX-License-Identifier: Apache-2.0
"""Optional stdlib HTTPS and conditional Git transport for the Forgejo adapter.

Run only in a trusted controller with scoped credentials; never in a worker.
"""

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from creatidy_kernel.adapters.forge_refs import ForgeBinding, https_origin, oid, repository_path, valid_branch
from creatidy_kernel.core.forge import EffectStatus, ForgeConflict, Reference, UnsupportedForge


def _https(url: str) -> None:
    parsed = urlsplit(url)
    https_origin(url)
    if not parsed.path.startswith("/"):
        raise ValueError("forge transport requires an HTTPS URL without embedded credentials")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


class LocalRequestRefusal(ValueError):
    """A deterministic request-size refusal before network I/O."""


@contextmanager
def _deadline(seconds: int):
    # urllib's socket timeout is per I/O; on non-main threads there is no
    # interruptible total deadline, so never claim one there.
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        raise UnsupportedForge("total HTTP deadline unsupported in this context")
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL)[0] > 0:
        raise UnsupportedForge("total HTTP deadline unavailable with active alarm")

    def expired(_signum: int, _frame: object) -> None:
        raise TimeoutError("forge HTTP operation deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    end = time.monotonic() + seconds
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
        if time.monotonic() >= end:
            raise TimeoutError("forge HTTP operation deadline exceeded")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class HTTPSForgejoTransport:
    def __init__(
        self,
        api_url: str,
        repository: Reference,
        token: Callable[[], str],
        *,
        timeout: int = 15,
        max_bytes: int = 1_000_000,
    ) -> None:
        _https(api_url)
        origin = https_origin(api_url)
        if urlsplit(api_url).path != "/api/v1" or timeout <= 0 or max_bytes <= 0:
            raise ValueError("Forgejo API v1 URL and finite limits required")
        self.binding = ForgeBinding(origin, repository_path(repository))
        self.api_url = f"{origin}/api/v1"
        self.token = token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = build_opener(_NoRedirect())

    @property
    def supports_reads(self) -> bool:
        return self._deadline_available()

    @property
    def supports_pr(self) -> bool:
        return self._deadline_available()

    @staticmethod
    def _deadline_available() -> bool:
        return (
            callable(getattr(signal, "setitimer", None))
            and threading.current_thread() is threading.main_thread()
            and signal.getitimer(signal.ITIMER_REAL)[0] == 0
        )

    def authoritative_absence(self, path: str) -> bool:
        # Forgejo can conceal permission failures as 404; HTTP status alone is not proof.
        return False

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        prefix = f"/repos/{self.binding.repository}"
        if (
            method not in {"GET", "POST"}
            or not path.startswith(prefix)
            or path[len(prefix) : len(prefix) + 1] not in {"", "/", "?"}
            or ".." in path
            or "#" in path
            or len(path) > 1024
        ):
            raise ValueError("unsupported forge request")
        if not self._deadline_available():
            raise UnsupportedForge("total HTTP deadline unavailable")
        # PR requests are flat strings. Bound each value before json.dumps can
        # allocate a body proportional to an untrusted title or description.
        if body is not None:
            if any(
                not isinstance(value, str) or len(value[: self.max_bytes + 1].encode("utf-8")) > self.max_bytes
                for value in body.values()
            ):
                raise LocalRequestRefusal("forge HTTP request exceeds safe limit")
            data = json.dumps(dict(body)).encode("utf-8")
            if len(data) > self.max_bytes:
                raise LocalRequestRefusal("forge HTTP request exceeds safe limit")
        else:
            data = None
        try:
            with _deadline(self.timeout):
                secret = self.token()
                if not secret or "\n" in secret or "\r" in secret:
                    raise ValueError("invalid forge credential")
                request = Request(  # noqa: S310 - HTTPS only; redirects disabled.
                    f"{self.api_url}{path}",
                    data=data,
                    headers={
                        "Authorization": f"token {secret}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    method=method,
                )
                try:
                    with self.opener.open(request, timeout=self.timeout) as response:
                        status = response.status
                        content = response.read(self.max_bytes + 1)
                except HTTPError as error:
                    with error:
                        status = error.code
                        content = error.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise OSError("forge HTTP response exceeds safe limit")
                try:
                    payload: object = json.loads(content) if content else None
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                    payload = None
        except (URLError, TimeoutError) as error:
            raise OSError("forge HTTP outcome unknown") from error
        return status, payload


def run_git_bounded(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int
) -> subprocess.CompletedProcess[str]:
    with subprocess.Popen(  # noqa: S603 - caller supplies fixed binary and controlled arguments/environment.
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True
    ) as process:
        if process.stdout is None or process.stderr is None:
            raise OSError("Git output pipes unavailable")
        output = {process.stdout: bytearray(), process.stderr: bytearray()}
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for pipe in output:
                selector.register(pipe, selectors.EVENT_READ, pipe)
            try:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Git operation timed out")
                    for key, _ in selector.select(remaining):
                        pipe = cast(BinaryIO, key.data)
                        chunk = os.read(pipe.fileno(), min(8192, max_bytes + 1))
                        if not chunk:
                            selector.unregister(pipe)
                        else:
                            output[pipe].extend(chunk)
                            if sum(len(part) for part in output.values()) > max_bytes:
                                raise OverflowError("Git output limit exceeded")
                process.wait(timeout=max(0, deadline - time.monotonic()))
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        return subprocess.CompletedProcess(
            argv,
            process.returncode,
            output[process.stdout].decode("utf-8", "replace"),
            output[process.stderr].decode("utf-8", "replace"),
        )


class ConditionalGitTransport:
    """Push from an isolated bare repository, never loading workload Git configuration."""

    supports_conditional_push = True

    def __init__(
        self,
        repository: Reference,
        remote_url: str,
        object_source: Path,
        *,
        timeout: int = 60,
        askpass: Path | None = None,
        max_output_bytes: int = 65536,
    ) -> None:
        _https(remote_url)
        repo_path = repository_path(repository)
        if urlsplit(remote_url).path != f"/{repo_path}.git":
            raise ForgeConflict("remote URL must bind the exact repository")
        self.binding = ForgeBinding(https_origin(remote_url), repo_path)
        if timeout <= 0 or max_output_bytes <= 0:
            raise ValueError("finite Git limits required")
        # This must be a controller-owned, non-worker-writable bare repository.
        # Path checks reject indirection, but do not establish hostile isolation.
        if not object_source.is_absolute() or object_source.is_symlink() or not object_source.is_dir():
            raise ValueError("explicit absolute bare object source required")
        if any(parent.is_symlink() for parent in object_source.parents):
            raise ValueError("symlink in object source path forbidden")
        source = object_source.resolve(strict=True)
        if (source / ".git").exists() or not (source / "HEAD").is_file() or not (source / "refs").is_dir():
            raise ValueError("bare object source required")
        objects = source / "objects"
        if not objects.is_dir() or (objects / "info" / "alternates").exists() or (source / "commondir").exists():
            raise ValueError("object source indirection forbidden")
        if any(path.is_symlink() for path in (source / "HEAD", source / "refs", objects, objects / "info")):
            raise ValueError("symlink in object source forbidden")
        self.repository = repository
        self.remote_url = remote_url
        self.object_source = source
        self.source_identity = (
            source.stat().st_dev,
            source.stat().st_ino,
            objects.stat().st_dev,
            objects.stat().st_ino,
        )
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        # The caller supplies a trusted executable which obtains a scoped credential
        # outside the worktree. Never inherit Git's ambient credential helpers.
        if askpass is not None and (
            not askpass.is_absolute() or not askpass.is_file() or not os.access(askpass, os.X_OK)
        ):
            raise ValueError("askpass must be a trusted absolute executable")
        if askpass is not None and askpass.resolve().is_relative_to(source):
            raise ValueError("askpass cannot be controlled by the object source")
        self.askpass = askpass
        binary = shutil.which("git")
        if binary is None or not Path(binary).is_absolute():
            raise ValueError("absolute Git executable required")
        self.git_binary = binary

    def compare_and_push(
        self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
    ) -> EffectStatus:
        if repository != self.repository:
            raise ForgeConflict("repository outside configured Git remote")
        valid_branch(branch)
        sha = revision.value.removeprefix("forgejo:")
        old = expected.value.removeprefix("forgejo:") if expected else ""
        if not revision.value.startswith("forgejo:") or not _valid_oid(sha):
            raise ForgeConflict("revision must be a full Git object ID")
        if expected is not None and (not expected.value.startswith("forgejo:") or not _valid_oid(old)):
            raise ForgeConflict("expected-old must be a full Git object ID")
        ref = f"refs/heads/{branch}"
        with tempfile.TemporaryDirectory(prefix="forge-push-") as directory:
            root = Path(directory)
            env = {
                "HOME": directory,
                "XDG_CONFIG_HOME": directory,
                "PATH": "/usr/bin:/bin",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
            if self.askpass is not None:
                env["GIT_ASKPASS"] = str(self.askpass)

            def run_git(argv: list[str]) -> subprocess.CompletedProcess[str]:
                return run_git_bounded(argv, root, env, self.timeout, self.max_output_bytes)

            try:
                source = self.object_source
                objects = source / "objects"
                if (
                    source.is_symlink()
                    or any(parent.is_symlink() for parent in source.parents)
                    or (source.stat().st_dev, source.stat().st_ino, objects.stat().st_dev, objects.stat().st_ino)
                    != self.source_identity
                    or (source / "commondir").exists()
                    or (objects / "info" / "alternates").exists()
                    or any(path.is_symlink() for path in (source / "HEAD", source / "refs", objects, objects / "info"))
                ):
                    return EffectStatus.UNKNOWN
                env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(objects)
                initialized = run_git([self.git_binary, "init", "--bare", "--template=", str(root / "repo")])
                if initialized.returncode != 0:
                    return EffectStatus.UNKNOWN
                checked = run_git([self.git_binary, "check-ref-format", "--branch", branch])
                if checked.returncode != 0:
                    raise ForgeConflict("invalid Git branch")
                kind = run_git([self.git_binary, f"--git-dir={root / 'repo'}", "cat-file", "-t", sha])
                if kind.returncode != 0 or kind.stdout != "commit\n":
                    return EffectStatus.UNKNOWN
                pushed = run_git(
                    [
                        self.git_binary,
                        f"--git-dir={root / 'repo'}",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "credential.helper=",
                        "-c",
                        "http.followRedirects=false",
                        "push",
                        "--porcelain",
                        f"--force-with-lease={ref}:{old}",
                        "--",
                        self.remote_url,
                        f"{sha}:{ref}",
                    ],
                )
            except (OSError, TimeoutError, subprocess.TimeoutExpired, OverflowError):
                return EffectStatus.UNKNOWN
        success = f"{sha}:{ref}\t"
        lines = pushed.stdout.splitlines()
        if (
            pushed.returncode == 0
            and len(lines) == 3
            and lines[0] == f"To {self.remote_url}"
            and lines[2] == "Done"
            and (
                re.fullmatch(rf"\*\t{re.escape(success)}\[new branch\]", lines[1])
                or re.fullmatch(rf" \t{re.escape(success)}[0-9a-f]{{7,64}}\.\.[0-9a-f]{{7,64}}", lines[1])
                or re.fullmatch(
                    rf"\+\t{re.escape(success)}[0-9a-f]{{7,64}}\.\.[0-9a-f]{{7,64}} \(forced update\)", lines[1]
                )
            )
        ):
            return EffectStatus.ACCEPTED
        rejected = f"!\t{sha}:{ref}\t[rejected] (stale info)"
        return (
            EffectStatus.STALE
            if pushed.returncode != 0 and lines == [f"To {self.remote_url}", rejected, "Done"]
            else EffectStatus.UNKNOWN
        )


def _valid_oid(value: str) -> bool:
    try:
        oid(value)
        return True
    except ValueError:
        return False
