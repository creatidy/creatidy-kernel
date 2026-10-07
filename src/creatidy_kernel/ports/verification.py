# SPDX-License-Identifier: Apache-2.0
"""Verification-only trusted caller seam, not worker or remote authentication.

The controller supplies the recipe, resources and authorizer outside candidate
data. A snapshot contains only explicitly collected file bytes, never a checkout
directory (which could expose .git, untracked secrets, mounts or sockets).
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from creatidy_kernel.core.verification import EvidenceSubject


class VerificationProfile(StrEnum):
    TRUSTED_DEVELOPMENT = "trusted_development"  # Non-isolated; never auto-upgraded.
    LINUX_BUBBLEWRAP = "linux_bubblewrap_verification_v1"


@dataclass(frozen=True, slots=True)
class VerificationResource:
    source: Path
    destination: PurePosixPath
    sha256: str


@dataclass(frozen=True, slots=True)
class VerificationSnapshot:
    files: tuple[tuple[str, bytes], ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.files) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not bytes
            for item in self.files
        ):
            raise ValueError("immutable snapshot file bytes required")

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for path, content in self.files:
            digest.update(path.encode() + b"\0" + hashlib.sha256(content).digest())
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationInvocation:
    subject: EvidenceSubject
    recipe: tuple[str, ...]
    resources: tuple[VerificationResource, ...]
    snapshot_digest: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        if (
            type(self.subject) is not EvidenceSubject
            or type(self.recipe) is not tuple
            or any(type(arg) is not str for arg in self.recipe)
            or type(self.resources) is not tuple
            or any(type(resource) is not VerificationResource for resource in self.resources)
            or type(self.snapshot_digest) is not str
            or type(self.timeout_seconds) is not int
        ):
            raise ValueError("immutable verification invocation required")


# Implemented by an authenticated trusted controller, not a caller-name check.
# Must check exact subject/spec/Attempt/repo/base/head, recipe/resources,
# expiry and revocation at every call. No grant can add mounts or network.
VerificationAuthorizer = Callable[[VerificationInvocation, int], bool]
