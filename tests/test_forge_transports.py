# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: S603 - fixed Git executable and synthetic temporary repositories only.
"""Production transport boundaries, without a live forge or remote Git push."""

import os
import shutil
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport, run_git_bounded
from creatidy_kernel.core.forge import EffectStatus, ForgeConflict, Reference

REPO = Reference("forgejo:team/project")
SHA = "a" * 40
OLD = "b" * 40


def bare(tmp_path: Path) -> Path:
    source = tmp_path / "source.git"
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q", "--bare", str(source)], check=True)
    return source


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
    with patch.object(transport.opener, "open") as opened:
        with pytest.raises(ValueError, match="request exceeds"):
            transport.request("POST", "/repos/team/project/pulls", {"body": "x" * 101})
        opened.assert_not_called()


def test_https_total_deadline_interrupts_trickling_response() -> None:
    class Trickling(Response):
        def read(self, _size: int | None = -1) -> bytes:
            while True:
                time.sleep(0.1)

    transport = HTTPSForgejoTransport("https://forge.invalid/api/v1", lambda: "scoped", timeout=1)
    start = time.monotonic()
    with patch.object(transport.opener, "open", return_value=Trickling()):
        with pytest.raises(OSError, match="outcome unknown"):
            transport.request("GET", "/repos/team/project")
    assert time.monotonic() - start < 3


def test_conditional_git_push_uses_single_expected_old_ref(tmp_path: Path) -> None:
    source = bare(tmp_path)
    transport = ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", source)
    with patch("creatidy_kernel.adapters.forgejo_transport.run_git_bounded") as run:
        run.side_effect = [
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "commit\n", ""),
            CompletedProcess(
                [],
                0,
                f"To https://forge.invalid/team/project.git\n+\t{SHA}:refs/heads/feature\t"
                f"{OLD[:7]}..{SHA[:7]} (forced update)\nDone\n",
                "",
            ),
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
        assert run.call_args.args[2]["GIT_TERMINAL_PROMPT"] == "0"
        assert run.call_args.args[2]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert run.call_args.args[2]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert run.call_args.args[2]["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == str(source / "objects")


def test_conditional_git_creation_stale_and_uncertainty(tmp_path: Path) -> None:
    transport = ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", bare(tmp_path))
    with patch("creatidy_kernel.adapters.forgejo_transport.run_git_bounded") as run:
        setup = [
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "commit\n", ""),
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
        for stdout in (
            "ok",
            "",
            f"To https://forge.invalid/team/project.git\n*\t{SHA}:refs/heads/other\t[new branch]\nDone\n",
            f"To https://forge.invalid/team/project.git\n*\t{SHA}:refs/heads/new\t[new branch]\n"
            f"*\t{SHA}:refs/heads/other\t[new branch]\nDone\n",
        ):
            run.side_effect = [*setup, CompletedProcess([], 0, stdout, "")]
            assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.UNKNOWN
        run.side_effect = [
            *setup,
            CompletedProcess(
                [], 0, f"To https://forge.invalid/team/project.git\n*\t{SHA}:refs/heads/new\t[new branch]\nDone\n", ""
            ),
        ]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.ACCEPTED
    with pytest.raises(ForgeConflict):
        transport.compare_and_push(REPO, "--delete", None, Reference(f"forgejo:{SHA}"))
    with pytest.raises(ForgeConflict):
        ConditionalGitTransport(REPO, "https://forge.invalid/other/repo.git", transport.object_source)


def test_success_receipt_matches_real_git_porcelain(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run([git, "init", "-q", str(work)], check=True)
    subprocess.run(
        [
            git,
            "-C",
            str(work),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
    )
    source = tmp_path / "controller.git"
    subprocess.run([git, "clone", "-q", "--bare", str(work), str(source)], check=True)
    sha = subprocess.run(
        [git, "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    remote = tmp_path / "remote.git"
    subprocess.run([git, "init", "-q", "--bare", str(remote)], check=True)
    url = "https://forge.invalid/team/project.git"
    transport = ConditionalGitTransport(REPO, url, source)
    original = run_git_bounded

    def local_push(
        argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int
    ) -> CompletedProcess[str]:
        if "push" in argv:
            result = original([str(remote) if arg == url else arg for arg in argv], cwd, env, timeout, max_bytes)
            return CompletedProcess(
                argv, result.returncode, result.stdout.replace(f"To {remote}", f"To {url}"), result.stderr
            )
        return original(argv, cwd, env, timeout, max_bytes)

    with patch("creatidy_kernel.adapters.forgejo_transport.run_git_bounded", side_effect=local_push):
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{sha}")) is EffectStatus.ACCEPTED


def test_bare_source_rejects_indirection_and_unverified_objects(tmp_path: Path) -> None:
    source = bare(tmp_path)
    url = "https://forge.invalid/team/project.git"
    with pytest.raises(ValueError, match="bare"):
        ConditionalGitTransport(REPO, url, tmp_path)
    linked = tmp_path / "linked.git"
    linked.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="bare"):
        ConditionalGitTransport(REPO, url, linked)
    alternate = source / "objects" / "info" / "alternates"
    alternate.write_text(str(tmp_path / "other"), encoding="utf-8")
    with pytest.raises(ValueError, match="indirection"):
        ConditionalGitTransport(REPO, url, source)
    alternate.unlink()
    commondir = source / "commondir"
    commondir.write_text("../elsewhere", encoding="utf-8")
    with pytest.raises(ValueError, match="indirection"):
        ConditionalGitTransport(REPO, url, source)
    commondir.unlink()
    (source / "objects" / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ConditionalGitTransport(REPO, url, source)
    (source / "objects" / "escape").unlink()
    transport = ConditionalGitTransport(REPO, url, source)
    assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.UNKNOWN
    git = shutil.which("git")
    assert git is not None
    blob = subprocess.run(
        [git, "--git-dir", str(source), "hash-object", "-w", "--stdin"],
        input="blob",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{blob}")) is EffectStatus.UNKNOWN


def test_git_capture_flood_and_timeout_kill_process(tmp_path: Path) -> None:
    env = {"PATH": os.environ["PATH"]}
    with pytest.raises(OverflowError):
        run_git_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*1000000); sys.stderr.write('y'*1000000)"],
            tmp_path,
            env,
            2,
            100,
        )
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        run_git_bounded([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, env, 1, 100)


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
    source = bare(tmp_path)
    transport = ConditionalGitTransport(REPO, "https://127.0.0.1:1/team/project.git", source, timeout=5)
    assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{sha}")) is EffectStatus.UNKNOWN
    assert not marker.exists()
    assert (
        subprocess.run([git, "--git-dir", str(redirected), "show-ref"], capture_output=True, check=False).returncode
        == 1
    )


def test_conditional_git_requires_explicit_trusted_askpass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = bare(tmp_path)
    with pytest.raises(ValueError, match="askpass"):
        ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", source, askpass=Path("relative"))
    askpass = source / "askpass"
    askpass.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    askpass.chmod(0o755)
    with pytest.raises(ValueError, match="object source"):
        ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", source, askpass=askpass)
    monkeypatch.setenv("GIT_ASKPASS", "/untrusted/helper")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    trusted = tmp_path / "trusted.git"
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "-q", "--bare", str(trusted)], check=True)
    transport = ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", trusted, askpass=askpass)
    with patch("creatidy_kernel.adapters.forgejo_transport.run_git_bounded") as run:
        run.side_effect = [
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "commit\n", ""),
            CompletedProcess(
                [], 0, f"To https://forge.invalid/team/project.git\n*\t{SHA}:refs/heads/new\t[new branch]\nDone\n", ""
            ),
        ]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.ACCEPTED
        assert run.call_args.args[2]["GIT_ASKPASS"] == str(askpass)
