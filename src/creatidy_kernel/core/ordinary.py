# SPDX-License-Identifier: Apache-2.0
"""Received ordinary Program inputs, not another workflow state machine."""

from dataclasses import dataclass

from creatidy_kernel.core.intake import Draft, IssueSubject, digest, encode


@dataclass(frozen=True, slots=True)
class DependencyEvidence:
    subject: IssueSubject
    required: tuple[tuple[str, str, int], ...]
    reference: str
    observed_at: int
    fresh_until: int
    complete: bool

    def __post_init__(self) -> None:
        if (
            type(self.subject) is not IssueSubject
            or type(self.required) is not tuple
            or any(
                type(item) is not tuple
                or len(item) != 3
                or type(item[0]) is not str
                or type(item[1]) is not str
                or type(item[2]) is not int
                or item[2] <= 0
                for item in self.required
            )
            or len(set(self.required)) != len(self.required)
            or type(self.reference) is not str
            or not self.reference
            or type(self.observed_at) is not int
            or type(self.fresh_until) is not int
            or self.fresh_until < self.observed_at
            or type(self.complete) is not bool
        ):
            raise ValueError("immutable native dependency evidence required")

    @property
    def meaning(self) -> dict[str, object]:
        return {
            "subject": self.subject.payload(),
            "required": self.required,
            "reference": self.reference,
            "complete": self.complete,
        }

    @property
    def payload(self) -> dict[str, object]:
        return {**self.meaning, "observed_at": self.observed_at, "fresh_until": self.fresh_until}


@dataclass(frozen=True, slots=True)
class OrdinaryProposal:
    program_id: str
    objective: str
    drafts: tuple[Draft, ...]
    dependencies: tuple[DependencyEvidence, ...]

    def __post_init__(self) -> None:
        if (
            type(self.program_id) is not str
            or not self.program_id.strip()
            or type(self.objective) is not str
            or not self.objective.strip()
            or type(self.drafts) is not tuple
            or not self.drafts
            or any(type(item) is not Draft for item in self.drafts)
            or type(self.dependencies) is not tuple
            or any(type(item) is not DependencyEvidence for item in self.dependencies)
            or len(self.dependencies) != len(self.drafts)
            or len({item.task_id for item in self.drafts}) != len(self.drafts)
            or tuple(item.evidence.subject for item in self.drafts) != tuple(item.subject for item in self.dependencies)
        ):
            raise ValueError("finite ordinary proposal and received dependency subjects required")

    @property
    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "kind": "ordinary-program-proposal",
            "program": self.program_id,
            "objective": self.objective,
            "drafts": [item.payload() for item in self.drafts],
            "dependencies": [item.payload for item in self.dependencies],
        }

    @property
    def digest(self) -> str:
        return digest(encode(self.payload))
