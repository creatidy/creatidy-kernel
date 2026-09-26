# SPDX-License-Identifier: Apache-2.0
"""Adapter-boundary syntax shared by fake and controlled Forgejo transports."""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from creatidy_kernel.core.forge import Effect, ForgeConflict, Reference, effect_marker


@dataclass(frozen=True, slots=True)
class ForgeBinding:
    origin: str
    repository: str


def https_origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except ValueError as error:
        raise ValueError("invalid HTTPS forge origin") from error
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)
    ):
        raise ValueError("invalid HTTPS forge origin")
    if port is not None and port == 0:
        raise ValueError("invalid HTTPS forge port")
    authority = f"[{host.lower()}]" if ":" in host else host.lower()
    return f"https://{authority}{f':{port}' if port not in (None, 443) else ''}"


def repository_path(reference: Reference) -> str:
    if not reference.value.startswith("forgejo:"):
        raise ForgeConflict("wrong forge provider")
    path = reference.value.removeprefix("forgejo:")
    parts = path.split("/")
    if (
        len(path) > 255
        or len(parts) != 2
        or any(
            len(part) > 128
            or part in {"", ".", ".."}
            or ".." in part
            or re.fullmatch(r"[A-Za-z0-9._~-]+", part) is None
            for part in parts
        )
    ):
        raise ForgeConflict("invalid repository handle")
    return path


def valid_branch(branch: str) -> None:
    # Mirror check-ref-format --branch at the boundary; the Git transport still
    # runs Git's own check before pushing.
    if (
        len(branch) > 255
        or not branch
        or branch == "@"
        or branch.startswith("-")
        or branch.endswith(("/", "."))
        or ".." in branch
        or "@{" in branch
        or any(char in branch for char in " ~^:?*[\\")
        or any(ord(char) < 32 or ord(char) == 127 for char in branch)
        or any(part in {"", ".", ".."} or part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise ForgeConflict("invalid Git branch")


def oid(value: str) -> str:
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ValueError("invalid Git object ID")
    return value


def positive_id(value: object) -> bool:
    return type(value) is int and 0 < value < 10**18


def number(value: str) -> bool:
    return 0 < len(value) <= 18 and value.isascii() and value.isdigit() and value[0] != "0"


def pr_payload(effect: Effect, max_bytes: int) -> tuple[str, str, str]:
    """Bound input before marker encoding, hashing or body concatenation."""
    fields = (
        effect.operation.operation_id,
        effect.operation.effect_key,
        effect.operation.request_digest,
        effect.title or "",
        effect.body or "",
    )
    if any(len(value) > max_bytes // 8 or len(value) * 4 > max_bytes for value in fields):
        raise ValueError("PR input exceeds local request limit")
    marker = effect_marker(effect.operation)
    body = f"{effect.body or ''}\n\n{marker}"
    if len(body.encode("utf-8")) + len((effect.title or "").encode("utf-8")) > max_bytes // 2:
        raise ValueError("PR body exceeds local request limit")
    snapshot = "kernel/snapshots/" + hashlib.sha256(marker.encode("ascii")).hexdigest()
    return snapshot, body, marker
