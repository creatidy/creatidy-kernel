# SPDX-License-Identifier: Apache-2.0
"""Deterministic single-candidate Codex discovery; a version probe only."""

import os
import sys
from pathlib import Path

import pytest

from creatidy_kernel.adapters.codex_discovery import (
    CodexDiscovery,
    CodexDiscoveryCategory,
    CodexDiscoveryFailure,
    probe_environment,
    resolve_codex,
)

FAKE_CODEX = f"""#!{sys.executable}
import os
import sys

assert sys.argv[1:] == ["--version"], "discovery must never start a thread or turn"
assert os.environ.get("HOME") == "__HOME__", "the probe uses the supplied closed environment"
print("codex-cli __VERSION__")
"""


def write_codex(directory: Path, version: str, *, home: str, name: str = "codex") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / name
    binary.write_text(FAKE_CODEX.replace("__VERSION__", version).replace("__HOME__", home))
    binary.chmod(0o700)
    return binary


def test_probe_environment_is_the_closed_operational_key_set() -> None:
    supplied = {"PATH": "/opt/tools", "HOME": "/home/operator", "LANG": "C.UTF-8", "AMBIENT": "value"}
    assert probe_environment(supplied) == {"PATH": "/opt/tools", "HOME": "/home/operator", "LANG": "C.UTF-8"}
    assert probe_environment({}) == {"PATH": os.defpath}


def test_first_path_candidate_resolves_deterministically(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    first = write_codex(tmp_path / "a" / "bin", "1.2.3", home=home)
    second = write_codex(tmp_path / "z" / "bin", "9.9.9", home=home)
    environment = {
        "PATH": os.pathsep.join((str(tmp_path / "a" / "bin"), str(tmp_path / "z" / "bin"))),
        "HOME": home,
    }
    resolution = resolve_codex(environment)
    assert isinstance(resolution, CodexDiscovery)
    assert resolution.binary == first.resolve() and resolution.version == "1.2.3"
    assert second.exists()  # the other installation is never silently substituted


def test_explicit_override_binary_wins_and_is_probed(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    write_codex(tmp_path / "bin", "1.2.3", home=home)
    override = write_codex(tmp_path / "other", "4.5.6", home=home, name="codex-override")
    resolution = resolve_codex(
        {"PATH": str(tmp_path / "bin"), "HOME": home, "CREATIDY_KERNEL_CODEX_BIN": str(override)}
    )
    assert isinstance(resolution, CodexDiscovery)
    assert resolution.binary == override.resolve() and resolution.version == "4.5.6"


def test_missing_codex_is_a_closed_not_found_failure(tmp_path: Path) -> None:
    failure = resolve_codex({"PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)})
    assert isinstance(failure, CodexDiscoveryFailure)
    assert failure.category is CodexDiscoveryCategory.NOT_FOUND


@pytest.mark.parametrize("behavior", ["exit-failure", "wrong-format", "development-version"])
def test_unusable_resolved_installation_is_loudly_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    home = str(tmp_path / "home")
    directory = tmp_path / "bin"
    if behavior == "exit-failure":
        directory.mkdir(parents=True)
        binary = directory / "codex"
        binary.write_text(f"#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ['--version']\nsys.exit(3)\n")
        binary.chmod(0o700)
        expected = CodexDiscoveryCategory.PROBE_FAILED
    elif behavior == "wrong-format":
        directory.mkdir(parents=True)
        binary = directory / "codex"
        binary.write_text(f"#!{sys.executable}\nimport sys\nprint('codex unknown-shape')\n")
        binary.chmod(0o700)
        expected = CodexDiscoveryCategory.PROBE_FAILED
    else:
        write_codex(directory, "1.2", home=home)
        expected = CodexDiscoveryCategory.UNSUPPORTED_VERSION
    monkeypatch.chdir(tmp_path)
    failure = resolve_codex({"PATH": str(directory), "HOME": home})
    assert isinstance(failure, CodexDiscoveryFailure)
    assert failure.category is expected


def test_version_shape_follows_the_runtime_configuration_parser(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    directory = tmp_path / "bin"
    for version in ("0.155.0-alpha.16.3", "1.2.3+build.7"):
        write_codex(directory, version, home=home)
        resolution = resolve_codex({"PATH": str(directory), "HOME": home})
        assert isinstance(resolution, CodexDiscovery) and resolution.version == version
