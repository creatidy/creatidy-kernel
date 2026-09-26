# SPDX-License-Identifier: Apache-2.0
"""Optional stdlib HTTPS and conditional Git transport for the Forgejo adapter.

Run only in a trusted controller with scoped credentials; never in a worker.
"""

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from creatidy_kernel.core.forge import EffectStatus, ForgeConflict, Reference


def _https(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("forge transport requires an HTTPS URL without embedded credentials")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str) -> None:
        return None


class HTTPSForgejoTransport:
    def __init__(
        self, api_url: str, token: Callable[[], str], *, timeout: int = 15, max_bytes: int = 1_000_000
    ) -> None:
        _https(api_url)
        if not api_url.rstrip("/").endswith("/api/v1") or timeout <= 0 or max_bytes <= 0:
            raise ValueError("Forgejo API v1 URL and finite limits required")
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = build_opener(_NoRedirect())

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        if method not in {"GET", "POST"} or not path.startswith("/repos/") or ".." in path or "#" in path:
            raise ValueError("unsupported forge request")
        secret = self.token()
        if not secret or "\n" in secret or "\r" in secret:
            raise ValueError("invalid forge credential")
        data = json.dumps(dict(body)).encode("utf-8") if body is not None else None
        request = Request(  # noqa: S310 - URL is constrained to HTTPS above and redirects are disabled.
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
        except URLError as error:
            raise OSError("forge HTTP outcome unknown") from error
        if len(content) > self.max_bytes:
            raise OSError("forge HTTP response exceeds safe limit")
        try:
            payload: object = json.loads(content) if content else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return status, payload


class ConditionalGitTransport:
    """One-ref expected-old push; failure without a stale lease marker is uncertain."""

    def __init__(self, repository: Reference, remote_url: str, worktree: Path, *, timeout: int = 60) -> None:
        _https(remote_url)
        repo_path = repository.value.removeprefix("forgejo:")
        if (
            not repository.value.startswith("forgejo:")
            or remote_url.rstrip("/").split("/", 3)[-1].removesuffix(".git") != repo_path
        ):
            raise ForgeConflict("remote URL must bind the exact repository")
        if not worktree.is_dir() or timeout <= 0:
            raise ValueError("existing local Git repository and finite timeout required")
        self.repository = repository
        self.remote_url = remote_url
        self.worktree = worktree
        self.timeout = timeout
        binary = shutil.which("git")
        if binary is None or not Path(binary).is_absolute():
            raise ValueError("absolute Git executable required")
        self.git_binary = binary

    def compare_and_push(
        self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
    ) -> EffectStatus:
        if repository != self.repository:
            raise ForgeConflict("repository outside configured Git remote")
        if branch.startswith("-"):
            raise ForgeConflict("invalid Git branch")
        result = subprocess.run(  # noqa: S603 - fixed executable/argv; branch is checked as a ref before push.
            [self.git_binary, "check-ref-format", "--branch", branch],
            cwd=self.worktree,
            capture_output=True,
            check=False,
            timeout=self.timeout,
        )
        if result.returncode != 0:
            raise ForgeConflict("invalid Git branch")
        sha = revision.value.removeprefix("forgejo:")
        old = expected.value.removeprefix("forgejo:") if expected else ""
        if not revision.value.startswith("forgejo:") or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
            raise ForgeConflict("revision must be a full Git object ID")
        if expected is not None and (
            not expected.value.startswith("forgejo:") or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", old)
        ):
            raise ForgeConflict("expected-old must be a full Git object ID")
        ref = f"refs/heads/{branch}"
        argv = [
            self.git_binary,
            "push",
            "--porcelain",
            f"--force-with-lease={ref}:{old}",
            "--",
            self.remote_url,
            f"{sha}:{ref}",
        ]
        try:
            pushed = subprocess.run(  # noqa: S603 - one exact ref and expected-old CAS; no shell.
                argv,
                cwd=self.worktree,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return EffectStatus.UNKNOWN
        if pushed.returncode == 0:
            return EffectStatus.ACCEPTED
        output = pushed.stdout + pushed.stderr
        return EffectStatus.STALE if "stale info" in output else EffectStatus.UNKNOWN
