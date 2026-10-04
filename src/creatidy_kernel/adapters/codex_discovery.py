# SPDX-License-Identifier: Apache-2.0
"""Deterministic local Codex discovery: a version probe only, never a thread or turn.

Normal operation must not require ``CREATIDY_KERNEL_CODEX_BIN``/``VERSION`` when an
unambiguous supported Codex is on ``PATH``. Resolution is single-candidate and
deterministic (the explicit override, else the first ``codex`` on the supplied
``PATH``): when that installation is unusable, its closed failure category is
reported and other installations are never silently substituted.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

CODEX_PROGRAM = "codex"
CODEX_VERSION_PREFIX = "codex-cli "
# The exact released-version shape the runtime configuration parser accepts.
CODEX_VERSION_PATTERN = r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?"
PROBE_TIMEOUT_SECONDS = 15
MAX_PROBE_OUTPUT_BYTES = 4096
_PROBE_ARGUMENTS = ("--version",)


class CodexDiscoveryCategory(StrEnum):
    """Closed safe discovery vocabulary; no executable output is ever echoed."""

    NOT_FOUND = "not_found"
    PROBE_FAILED = "probe_failed"
    UNSUPPORTED_VERSION = "unsupported_version"


@dataclass(frozen=True, slots=True)
class CodexDiscovery:
    """One resolved local installation; the caller still validates schema later."""

    binary: Path
    version: str


@dataclass(frozen=True, slots=True)
class CodexDiscoveryFailure:
    category: CodexDiscoveryCategory


CodexResolution = CodexDiscovery | CodexDiscoveryFailure


class _ProbeFailure(Exception):
    """Internal probe refusal converted to a closed result category at the boundary."""

    def __init__(self, category: CodexDiscoveryCategory) -> None:
        super().__init__(category.value)
        self.category = category


def probe_codex_version(binary: Path, environment: Mapping[str, str]) -> str:
    """Run ``<binary> --version`` and return its released version string."""
    try:
        completed = subprocess.run(  # noqa: S603 - absolute resolved executable, fixed inference-free argv.
            (str(binary), *_PROBE_ARGUMENTS),
            env=dict(environment),
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise _ProbeFailure(CodexDiscoveryCategory.PROBE_FAILED) from error
    if completed.returncode != 0:
        raise _ProbeFailure(CodexDiscoveryCategory.PROBE_FAILED)
    first = completed.stdout[:MAX_PROBE_OUTPUT_BYTES].decode("utf-8", "replace").splitlines()
    if not first or not first[0].startswith(CODEX_VERSION_PREFIX):
        raise _ProbeFailure(CodexDiscoveryCategory.PROBE_FAILED)
    version = first[0].removeprefix(CODEX_VERSION_PREFIX).strip()
    if re.fullmatch(CODEX_VERSION_PATTERN, version) is None:
        raise _ProbeFailure(CodexDiscoveryCategory.UNSUPPORTED_VERSION)
    return version


def probe_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The closed operational key set the probe and the runtime child both use."""
    environment = {"PATH": environ.get("PATH") or os.defpath}
    for name in ("HOME", "LANG", "LC_ALL", "TMPDIR"):
        if environ.get(name):
            environment[name] = environ[name]
    return environment


def resolve_codex(environ: Mapping[str, str]) -> CodexResolution:
    """Resolve the explicit override binary, else the first ``codex`` on ``PATH``."""
    search_path = environ.get("PATH") or os.defpath
    override = environ.get("CREATIDY_KERNEL_CODEX_BIN", "")
    if override:
        binary = Path(override)
        executable = binary if binary.is_absolute() and binary.is_file() and os.access(binary, os.X_OK) else None
    else:
        executable = shutil.which(CODEX_PROGRAM, path=search_path)
    if executable is None:
        return CodexDiscoveryFailure(CodexDiscoveryCategory.NOT_FOUND)
    resolved = Path(executable).resolve()
    if not resolved.is_absolute():
        return CodexDiscoveryFailure(CodexDiscoveryCategory.NOT_FOUND)
    try:
        version = probe_codex_version(resolved, probe_environment(environ))
    except _ProbeFailure as failure:
        return CodexDiscoveryFailure(failure.category)
    return CodexDiscovery(resolved, version)
