# SPDX-License-Identifier: Apache-2.0
"""Adapter-boundary syntax shared by fake and controlled Forgejo transports."""

import re

from creatidy_kernel.core.forge import ForgeConflict, Reference


def repository_path(reference: Reference) -> str:
    if not reference.value.startswith("forgejo:"):
        raise ForgeConflict("wrong forge provider")
    path = reference.value.removeprefix("forgejo:")
    parts = path.split("/")
    if len(parts) != 2 or any(
        part in {"", ".", ".."} or re.fullmatch(r"[A-Za-z0-9._~-]+", part) is None for part in parts
    ):
        raise ForgeConflict("invalid repository handle")
    return path


def valid_branch(branch: str) -> None:
    # Mirror check-ref-format --branch at the boundary; the Git transport still
    # runs Git's own check before pushing.
    if (
        not branch
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
