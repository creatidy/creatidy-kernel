# SPDX-License-Identifier: Apache-2.0
"""Synthetic, bounded authority values; not an OS or credential boundary."""

from dataclasses import dataclass
from pathlib import Path

from creatidy_kernel.core.domain import AttemptSpec


class AuthorityDenied(ValueError):
    """The requested synthetic capability is not authorized."""


WORKER_OPERATIONS = frozenset({"read", "write", "execute", "publish_candidate"})


@dataclass(frozen=True, slots=True)
class Principal:
    actor_id: str
    role: str

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or self.role not in {"owner", "worker"}:
            raise AuthorityDenied("invalid principal")


@dataclass(frozen=True, slots=True)
class AuthorityGrant:
    grant_id: str
    issuer: str
    subject: str
    program_id: str
    spec_digest: str
    repository: str
    root: Path
    paths: frozenset[Path]
    network: frozenset[str]
    operations: frozenset[str]
    expires_at: int
    single_use: bool = False
    revoked: bool = False

    def __post_init__(self) -> None:
        if not all((self.grant_id, self.issuer, self.subject, self.program_id, self.spec_digest, self.repository)):
            raise AuthorityDenied("grant identity is incomplete")
        if not self.root.is_absolute() or not self.root.is_dir():
            raise AuthorityDenied("grant root must be an existing absolute directory")
        if not self.paths or any(not path.is_relative_to(self.root) for path in self.paths):
            raise AuthorityDenied("paths must be within the repository root")
        if not self.operations or not self.operations <= WORKER_OPERATIONS:
            raise AuthorityDenied("worker grants cannot confer control authority")
        if self.expires_at <= 0 or any(
            not destination or destination.strip() != destination for destination in self.network
        ):
            raise AuthorityDenied("invalid expiry or network destination")


@dataclass(frozen=True, slots=True)
class OperationIntent:
    operation_id: str
    effect_key: str
    request_digest: str
    operation: str
    repository: str
    path: Path | None = None
    destination: str | None = None

    def __post_init__(self) -> None:
        if not all((self.operation_id, self.effect_key, self.request_digest, self.operation, self.repository)):
            raise AuthorityDenied("operation intent is incomplete")


def check_scope(grant: AuthorityGrant, attempt: AttemptSpec, intent: OperationIntent, now: int) -> None:
    if grant.revoked or now >= grant.expires_at:
        raise AuthorityDenied("grant revoked or expired")
    if (grant.subject, grant.program_id, grant.spec_digest) != (
        attempt.digest,
        attempt.program_id,
        attempt.spec_digest,
    ):
        raise AuthorityDenied("attempt or approved spec changed")
    if intent.repository != grant.repository or intent.operation not in grant.operations:
        raise AuthorityDenied("repository or operation exceeds grant")
    if intent.destination is not None and intent.destination not in grant.network:
        raise AuthorityDenied("network destination exceeds grant")
    if intent.operation in {"read", "write"} and intent.path is None:
        raise AuthorityDenied("file operation requires an explicit path")
    if intent.path is not None:
        if intent.path.is_absolute() or ".." in intent.path.parts:
            raise AuthorityDenied("absolute or traversing path")
        root = grant.root.resolve()
        target = (grant.root / intent.path).resolve()
        if not target.is_relative_to(root) or not any(
            allowed.resolve().is_relative_to(root) and target.is_relative_to(allowed.resolve())
            for allowed in grant.paths
        ):
            raise AuthorityDenied("path exceeds grant or follows escaping symlink")
