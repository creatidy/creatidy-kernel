# SPDX-License-Identifier: Apache-2.0
"""Generic operator profile support: dotenv files and optional SOPS decryption.

The profile carries the same neutral runtime-environment contract as the process
environment; environment-based configuration remains fully supported. This is not
a secrets manager: there is no key generation, storage or recipient configuration
here, profile values are never logged, printed or serialized, and the SOPS path is
an optional generic adapter over a user-supplied profile and the user's own SOPS
key environment. Deployment-specific policy (allowed hosts, age recipients, file
locations) stays with the deployment, not in this public product.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.task_execution import LIVE_ENV, ConfigurationError, TaskRuntimeConfig

MAX_PROFILE_BYTES = 256 * 1024
MAX_DECRYPTED_BYTES = 4 * 1024 * 1024
SOPS_TIMEOUT_SECONDS = 60
_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
OPERATIONAL_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
KNOWN_KEYS = frozenset(LIVE_ENV.values()) | set(OPERATIONAL_KEYS)
_SOPS_ENVIRONMENT_PREFIX = "SOPS_"


class ProfileError(ValueError):
    """Safe fixed profile refusal; supplied values never appear in messages."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Bounded literal KEY=VALUE parsing; values are never shell syntax or eval input."""
    if len(text.encode()) > MAX_PROFILE_BYTES:
        raise ProfileError("profile exceeds size limit")
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw = stripped.partition("=")
        key = key.strip()
        if not separator or _KEY.fullmatch(key) is None or key in values:
            raise ProfileError("profile must contain unique KEY=VALUE entries with plain uppercase names")
        values[key] = raw
    return values


def parse_json_profile(text: str) -> dict[str, str]:
    """Bounded JSON-object profile (the SOPS output shape) with string fields only."""
    if len(text.encode()) > MAX_DECRYPTED_BYTES:
        raise ProfileError("profile exceeds size limit")
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise ProfileError("profile must be a JSON object of string fields") from None
    if not isinstance(value, dict):
        raise ProfileError("profile must be a JSON object of string fields")
    values: dict[str, str] = {}
    for key, item in cast("dict[object, object]", value).items():
        if not isinstance(key, str) or _KEY.fullmatch(key) is None or not isinstance(item, str):
            raise ProfileError("profile must be a JSON object of string fields")
        values[key] = item
    return values


def decrypt_profile(
    path: Path,
    *,
    environment: Mapping[str, str] | None = None,
    timeout: int = SOPS_TIMEOUT_SECONDS,
) -> str:
    """Decrypt a user-supplied SOPS profile with the user's own SOPS key environment."""
    values = os.environ if environment is None else environment
    search_path = values.get("PATH") or os.defpath
    executable = shutil.which("sops", path=search_path)
    if executable is None:
        raise ProfileError("sops executable unavailable; install sops or load a plaintext profile")
    child = {"PATH": search_path}
    for name in ("HOME", "LANG", "LC_ALL", "TMPDIR"):
        if values.get(name):
            child[name] = values[name]
    for name, value in values.items():
        if name.startswith(_SOPS_ENVIRONMENT_PREFIX):
            child[name] = value
    try:
        completed = subprocess.run(  # noqa: S603 - resolved executable, fixed read-only argv.
            (executable, "--decrypt", "--output-type", "json", str(path)),
            env=child,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise ProfileError("profile decryption failed") from error
    if completed.returncode != 0 or len(completed.stdout) > MAX_DECRYPTED_BYTES:
        raise ProfileError("profile decryption failed")
    return completed.stdout.decode("utf-8", "replace")


def load_profile(
    path: Path,
    *,
    sops: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load one user-supplied operator profile; plaintext dotenv or SOPS-decrypted JSON."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ProfileError("operator profile must be an existing plain file")
    try:
        if path.stat().st_size > MAX_PROFILE_BYTES:
            raise ProfileError("profile exceeds size limit")
        if sops:
            return parse_json_profile(decrypt_profile(path, environment=environment))
        text = path.read_text(encoding="utf-8")
    except ProfileError:
        raise
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ProfileError("operator profile is unreadable") from error
    return parse_dotenv(text)


def unknown_fields(values: Mapping[str, str]) -> tuple[str, ...]:
    """Profile field names outside the neutral runtime contract and operational keys."""
    return tuple(sorted(name for name in values if name not in KNOWN_KEYS))


def configuration_fields(values: Mapping[str, str]) -> tuple[str, ...]:
    """Field names the runtime parser would reject; empty when the values are usable."""
    try:
        TaskRuntimeConfig.parse(dict(values))
    except ConfigurationError as error:
        return error.fields
    return ()


def default_profile_path(environ: Mapping[str, str] | None = None) -> Path:
    """Conventional user-local profile location; XDG-compatible on Linux."""
    values = os.environ if environ is None else environ
    override = values.get("XDG_CONFIG_HOME", "")
    base = Path(override) if override and Path(override).is_absolute() else Path.home() / ".config"
    return base / "creatidy-kernel" / "kernel.env"


def profile_template() -> str:
    """Generic operator profile template; placeholders only, no deployment specifics."""
    return f"""# Creatidy Kernel operator profile (literal KEY=VALUE values; never shell syntax).
# Validate with: creatidy-kernel config validate --profile {default_profile_path()}
# Secrets stay in this user-local file or in the process environment; they are never
# printed, logged or exported into evidence by Kernel.
CREATIDY_KERNEL_ROUTER_URL=REPLACE_ROUTER_ORIGIN
CREATIDY_KERNEL_ROUTER_KEY=
CREATIDY_KERNEL_RUNTIME_BINDING=REPLACE_PROVIDER/MODEL[/EFFORT]
# Codex entries are optional when a supported `codex` is available on PATH.
CREATIDY_KERNEL_CODEX_BIN=
CREATIDY_KERNEL_CODEX_VERSION=
CREATIDY_KERNEL_FORGE_API=REPLACE_FORGE_API_V1_URL
CREATIDY_KERNEL_FORGE_REMOTE=REPLACE_TASK_REPOSITORY_GIT_URL
CREATIDY_KERNEL_FORGE_TOKEN=REPLACE_SCOPED_FORGE_CREDENTIAL
CREATIDY_KERNEL_FORGE_ASKPASS=
# Source and state entries are optional: Kernel acquires its own source cache and
# selects user-local state defaults when they are unset.
CREATIDY_KERNEL_SOURCE_REPOSITORY=
CREATIDY_KERNEL_STATE_DIRECTORY=
# Operational entries supplied to the Codex child (closed environment).
HOME={Path.home()}
PATH={os.environ.get("PATH", os.defpath)}
LANG=C.UTF-8
LC_ALL=
# Leave TMPDIR empty to use the platform default temporary directory.
TMPDIR={os.environ.get("TMPDIR", "")}
"""
