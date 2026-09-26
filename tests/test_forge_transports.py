# SPDX-License-Identifier: Apache-2.0
"""Production transport boundaries, without a live forge or remote Git push."""

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
        run.side_effect = [CompletedProcess([], 0), CompletedProcess([], 0, "ok", "")]
        assert (
            transport.compare_and_push(REPO, "feature", Reference(f"forgejo:{OLD}"), Reference(f"forgejo:{SHA}"))
            is EffectStatus.ACCEPTED
        )
        argv = run.call_args.args[0]
        assert argv[1:] == [
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/feature:{OLD}",
            "--",
            "https://forge.invalid/team/project.git",
            f"{SHA}:refs/heads/feature",
        ]
        assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_conditional_git_creation_stale_and_uncertainty(tmp_path: Path) -> None:
    transport = ConditionalGitTransport(REPO, "https://forge.invalid/team/project.git", tmp_path)
    with patch("creatidy_kernel.adapters.forgejo_transport.subprocess.run") as run:
        run.side_effect = [CompletedProcess([], 0), CompletedProcess([], 1, "!\t[rejected] (stale info)", "")]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.STALE
        assert "--force-with-lease=refs/heads/new:" in run.call_args.args[0]
        run.side_effect = [CompletedProcess([], 0), CompletedProcess([], 1, "", "connection closed")]
        assert transport.compare_and_push(REPO, "new", None, Reference(f"forgejo:{SHA}")) is EffectStatus.UNKNOWN
    with pytest.raises(ForgeConflict):
        transport.compare_and_push(REPO, "--delete", None, Reference(f"forgejo:{SHA}"))
    with pytest.raises(ForgeConflict):
        ConditionalGitTransport(REPO, "https://forge.invalid/other/repo.git", tmp_path)
