# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: S603 - fixed Git executable and synthetic temporary repositories only.
"""Production transport boundaries, without a live forge or remote Git push."""

import os
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport
from creatidy_kernel.core.forge import EffectStatus, ForgeConflict, Reference

REPO = Reference("forgejo:team/project")
SHA = "a" * 40
OLD = "b" * 40


class Response(BytesIO):
    status = 200


def test_https_transport_stays_on_configured_origin_and_bounds_payload() -> None:
    with pytest.raises(ValueError):
        HTTPSForgejoTransport("http://forge.invalid/api/v1", lambda: "token")
    transport = HTTPSForgejoTransport("https://forge.invalid/api/v1", lambda: "scoped", max_bytes=100)
    with patch.object(transport.opener, "open", return_value=Response(b'{"full_name":"team/project"}')) as opened:
        assert transport.request("GET", "/repos/team/project") == (200, {"full_name": "team/project"})
        sent = opened.call_args.args[0]
        assert sent.full_url == "https://forge.invalid/api/v1/repos/team/project"
        assert sent.get_header("Authorization") == "token scoped"
    with pytest.raises(ValueError):
        transport.request("GET", "/repos/../other")
    with patch.object(transport.opener, "open", return_value=Response(b"x" * 101)):
        with pytest.raises(OSError):
            transport.request("GET", "/repos/team/project")


def test_conditional_git_push_uses_single_expected_old_ref(tmp_path: Path) -> None:
    transport = ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", tmp_path)
    with patch("creatidy_kernel.adapters.forgejo_transport.subprocess.run") as run:
        run.side_effect = [
            CompletedProcess([], 0, str(tmp_path)),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "ok", ""),
        ]
        assert (
            transport.compare_and_push(REPO, "feature", Reference(f"forgejo:{OLD}"), Reference(f"forgejo:{SHA}"))
            is EffectStatus.ACCEPTED
        )
        argv = run.call_args.args[0]
        assert argv[2:9] == [
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "push",
        ]
        assert argv[9:] == [
            "--porcelain",
            f"--force-with-lease=refs/heads/feature:{OLD}",
            "--",
            "https://forge.invalid/team/project.git",
            f"{SHA}:refs/heads/feature",
        ]
        assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert run.call_args.kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert run.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert run.call_args.kwargs["env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == str(tmp_path)


def test_conditional_git_creation_stale_and_uncertainty(tmp_path: Path) -> None:
    transport = ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", tmp_path)
    with patch("creatidy_kernel.adapters.forgejo_transport.subprocess.run") as run:
        setup = [
            CompletedProcess([], 0, str(tmp_path)),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
        ]
        run.side_effect = [*setup, CompletedProcess([], 1, f"!\t{SHA}:refs/heads/new\t[rejected] (stale info)\n", "")]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.STALE
        assert "--force-with-lease=refs/heads/new:" in run.call_args.args[0]
        run.side_effect = [*setup, CompletedProcess([], 1, "", "connection closed")]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.UNKNOWN
        run.side_effect = [*setup, CompletedProcess([], 1, "", "remote: stale info")]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.UNKNOWN
        run.side_effect = [*setup, CompletedProcess([], 1, f"!\t{SHA}:refs/heads/other\t[rejected] (stale info)", "")]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.UNKNOWN
    with pytest.raises(ForgeConflict):
        transport.compare_and_push(REPO, "--delete", None, Reference(f"forgejo:{SHA}"))
    with pytest.raises(ForgeConflict):
        ConditionalGitTransport(REPO, "https://forge.invalid/other/repo.git", tmp_path)


def test_conditional_git_ignores_worktree_hooks_rewrites_and_ambient_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = shutil.which("git")
    assert git is not None
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run([git, "init", "-q", str(worktree)], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(worktree),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
    )
    sha = subprocess.run(
        [git, "-C", str(worktree), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    redirected = tmp_path / "redirected.git"
    subprocess.run([git, "init", "-q", "--bare", str(redirected)], check=True)
    marker = tmp_path / "hook-ran"
    hook = worktree / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(
        [
            git,
            "-C",
            str(worktree),
            "config",
            "url.file://" + str(redirected) + ".insteadOf",
            "https://127.0.0.1:1/team/project.git",
        ],
        check=True,
    )
    subprocess.run(
        [git, "-C", str(worktree), "config", "credential.helper", "!touch '" + str(marker) + "'"], check=True
    )
    hostile_global = tmp_path / "global-config"
    hostile_global.write_text(
        f'[url "file://{redirected}"]\n\tinsteadOf = https://127.0.0.1:1/team/project.git\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_global))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook.parent))
    transport = ConditionalGitTransport(REPO, "https://127.0.0.1:1/team/project.git", worktree, timeout=5)
    assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{sha}")) is EffectStatus.UNKNOWN
    assert not marker.exists()
    assert (
        subprocess.run([git, "--git-dir", str(redirected), "show-ref"], capture_output=True, check=False).returncode
        == 1
    )


def test_conditional_git_requires_explicit_trusted_askpass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="askpass"):
        ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", tmp_path, askpass=Path("relative"))
    askpass = tmp_path / "askpass"
    askpass.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    askpass.chmod(0o755)
    with pytest.raises(ValueError, match="worktree"):
        ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", tmp_path, askpass=askpass)
    monkeypatch.setenv("GIT_ASKPASS", "/untrusted/helper")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    transport = ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", worktree, askpass=askpass)
    with patch("creatidy_kernel.adapters.forgejo_transport.subprocess.run") as run:
        run.side_effect = [
            CompletedProcess([], 0, str(tmp_path)),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
        ]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.ACCEPTED
        assert run.call_args.kwargs["env"]["GIT_ASKPASS"] == str(askpass)
