# SPDX-License-Identifier: Apache-2.0
"""Proof-runner safety tests; real native/producer proof is explicitly opt-in."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.check_codex52 import PIN, control, environment, parser_matrix, requirements, verify_source


def test_synthetic_demands_keep_pin_effort_and_explicit_nulls() -> None:
    first = requirements()
    assert first["schema_version"] == 2
    assert first["reasoning_effort"] == "high"
    assert first["profile_policy_version"] is None
    assert first["profile_expansion"] is None
    assert "opaque-not-effort" in PIN
    first.clear()
    assert requirements()["reasoning_effort"] == "high"
    assert control()["execution_requirements"] == requirements()


def test_proof_child_environment_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_SECRET_SENTINEL", "synthetic-do-not-inherit")
    monkeypatch.setenv("CODEX_HOME", "/synthetic/owner-home")
    env = environment(tmp_path)
    assert "OWNER_SECRET_SENTINEL" not in env
    assert env["CODEX_HOME"] == str(tmp_path / "codex")
    assert env["HOME"] == str(tmp_path)
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


@pytest.mark.parametrize("head,status", [("other", b""), ("pinned", b" M source.py"), ("pinned", b"?? extra.py")])
def test_source_guard_refuses_changed_inputs(tmp_path: Path, head: str, status: bytes) -> None:
    with patch("tools.check_codex52.subprocess.check_output", side_effect=[head, status]):
        with pytest.raises(ValueError, match="unchanged exact public source"):
            verify_source(tmp_path, "pinned", environment(tmp_path))


def test_source_guard_requires_successful_git_reads(tmp_path: Path) -> None:
    with patch("tools.check_codex52.subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "git")):
        with pytest.raises(subprocess.CalledProcessError):
            verify_source(tmp_path, "pinned", environment(tmp_path))


@pytest.mark.parametrize("unexpected", ["positive-refused", "negative-parsed"])
def test_parser_matrix_never_accepts_changed_oracle(tmp_path: Path, unexpected: str) -> None:
    # Test the runner's failure policy, not a substitute producer parser.
    outcomes = (
        [{"status": "refused"}]
        if unexpected == "positive-refused"
        else [{"status": "parsed"}, {"status": "parsed"}, {"status": "parsed"}]
    )
    with patch("tools.check_codex52.parse", side_effect=outcomes):
        with pytest.raises(ValueError, match="producer"):
            parser_matrix(tmp_path, environment(tmp_path))
