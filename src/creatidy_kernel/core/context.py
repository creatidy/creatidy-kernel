# SPDX-License-Identifier: Apache-2.0
"""Bounded, source-pinned context manifest; compilation and storage are external."""

import hashlib
import json
from dataclasses import dataclass

from creatidy_kernel.core.domain import AttemptSpec


def _text(value: str, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")


@dataclass(frozen=True, slots=True)
class ContextSource:
    source_id: str
    revision: str
    digest: str
    selected_content: str

    def __post_init__(self) -> None:
        for name in ("source_id", "revision", "digest"):
            _text(getattr(self, name), name)
        if type(self.selected_content) is not str:
            raise ValueError("selected_content must be text")

    def payload(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "revision": self.revision,
            "digest": self.digest,
            "selected_content": self.selected_content,
        }


@dataclass(frozen=True, slots=True)
class ContextPackage:
    attempt_id: str
    program_spec_digest: str
    sources: tuple[ContextSource, ...]
    selection_policy_digest: str
    redaction_policy_digest: str
    compiler_version: str
    max_bytes: int
    max_tokens: int

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "program_spec_digest",
            "selection_policy_digest",
            "redaction_policy_digest",
            "compiler_version",
        ):
            _text(getattr(self, name), name)
        if type(self.sources) is not tuple or any(type(source) is not ContextSource for source in self.sources):
            raise ValueError("sources must be an immutable tuple of ContextSource values")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("source IDs must be unique")
        for name in ("max_bytes", "max_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if sum(len(source.selected_content.encode("utf-8")) for source in self.sources) > self.max_bytes:
            raise ValueError("selected content exceeds byte budget")

    def matches(self, attempt: AttemptSpec) -> bool:
        """The Attempt's context_reference can pin this manifest digest."""
        return (
            self.attempt_id == attempt.attempt_id
            and self.program_spec_digest == attempt.spec_digest
            and attempt.context_reference == self.digest
        )

    def payload(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "program_spec_digest": self.program_spec_digest,
            "sources": [source.payload() for source in self.sources],
            "selection_policy_digest": self.selection_policy_digest,
            "redaction_policy_digest": self.redaction_policy_digest,
            "compiler_version": self.compiler_version,
            "max_bytes": self.max_bytes,
            "max_tokens": self.max_tokens,
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
