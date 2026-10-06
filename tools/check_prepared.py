# SPDX-License-Identifier: Apache-2.0
"""Installed ordinary preparation/approval/refusal smoke with synthetic local evidence."""

import sys
from collections.abc import Mapping
from pathlib import Path

from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.intake import ForgeIntakeEvidence, GitContentSource
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.domain import AuthorityEnvelope, BudgetPolicy, PolicyReference, ProgramStatus
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.intake import (
    BaselineSnapshot,
    ContentCriterion,
    Declaration,
    IssueSubject,
    OwnerPolicy,
    encode,
)
from creatidy_kernel.ports.intake import OrdinaryIntake


class SyntheticIssue(SyntheticForgeTransport):
    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        if "/issues/" in path:
            return 200, {
                "number": 49,
                "title": "Synthetic content task",
                "body": "untrusted proposal",
                "state": "open",
                "updated_at": "2026-10-06T00:00:00Z",
            }
        return super().request(method, path, body)


class SyntheticBaseline:
    def read_baseline(self, subject: IssueSubject, recipe_digest: str) -> BaselineSnapshot:
        return BaselineSnapshot(subject, recipe_digest, "smoke-baseline", "fixture:exact-baseline", "passed", 100)


state = Path(sys.argv[1])
repository = state / "source"
repository.mkdir()
reference_git(repository, "init", "--initial-branch=develop")
(repository / "result.txt").write_bytes(b"old fixture bytes\n")
reference_git(repository, "add", "result.txt")
reference_git(
    repository,
    "-c",
    "user.name=Synthetic",
    "-c",
    "user.email=synthetic@example.invalid",
    "commit",
    "-m",
    "synthetic fixture",
)
base = reference_git(repository, "rev-parse", "HEAD")
transport = SyntheticIssue(Reference("forgejo:team/project"))
transport.branches["develop"] = base
subject = IssueSubject(transport.binding.origin, transport.binding.repository, 49, "develop", base)
criterion = ContentCriterion("result.txt", b"new owner-specified exact bytes\n")
meaning = Declaration(
    encode(
        {
            "version": 1,
            "outcome": "Exact file content",
            "criteria": [criterion.criterion],
            "quality": ["exact encoding"],
            "interface": ["UTF-8"],
            "context": ["local only"],
            "unknowns": [],
            "paths": ["result.txt"],
            "network": [],
            "effects": [],
            "recipes": ["fixture:recipe:v1"],
            "provenance": ["synthetic issue proposal"],
        }
    )
)
reader = ForgeIntakeEvidence(
    ForgejoForge(transport, transport, lambda _: False),
    transport.binding,
    GitContentSource(repository, transport.binding),
    SyntheticBaseline(),
    (criterion,),
    "smoke-content",
    lambda: 100,
    max_pages=2,
)
policy = OwnerPolicy(
    PolicyReference("smoke-policy", "v1", "fixture:policy"),
    BudgetPolicy(1, 1),
    AuthorityEnvelope("synthetic-owner"),
    frozenset({"result.txt"}),
    frozenset(),
    frozenset(),
    frozenset({"fixture:recipe:v1"}),
    frozenset({"smoke-baseline", "smoke-content"}),
    10,
)
# This Principal is ONLY the deliberate synthetic fixture; production callers authenticate externally.
principal = Principal("synthetic-owner", "owner")
with SQLiteProgramStore(state / "prepared.db") as store:
    intake = OrdinaryIntake(store, reader, lambda: 100)
    draft = intake.prepare("installed-smoke", subject, meaning, expected_parent=None)
    approved = intake.approve(principal, draft, policy, decision_id="smoke-decision", expires_at=110)
    refusal = intake.handoff(principal, approved, policy, ScarcityRouterAllocator)
    if (
        refusal.handoff.draft.declaration.raw != meaning.raw
        or approved.program.status is not ProgramStatus.DRAFT
        or approved.program.attempts
        or refusal.reason != "ordinary_requirement_mapping_unavailable"
    ):
        raise SystemExit("Installed ordinary intake contract failed")
with SQLiteProgramStore(state / "prepared.db") as store:
    intake = OrdinaryIntake(store, reader, lambda: 100)
    recovered = intake.approve(principal, draft, policy, decision_id="smoke-decision", expires_at=110)
    if recovered.program != approved.program or recovered.decision_bytes != approved.decision_bytes:
        raise SystemExit("Installed ordinary approval recovery differs")
if transport.pushes or transport.posts:
    raise SystemExit("Ordinary preparation performed a Forge effect")
print("Installed ordinary intake: exact Git/Forge-shaped evidence, inert approval, intact refusal and reopen passed.")
