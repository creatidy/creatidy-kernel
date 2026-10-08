# SPDX-License-Identifier: Apache-2.0
"""Installed ordinary preparation/approval/refusal smoke with synthetic local evidence."""

import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.intake import ForgeIntakeEvidence, GitContentSource
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.scarcity_router import RuntimeSupport, ScarcityRouterAllocator, requirement_markers
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
    TranslationRefusal,
    encode,
)
from creatidy_kernel.core.resources import Allocation, ResourceRequest
from creatidy_kernel.ports.allocation import decode_allocation, encode_allocation
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
        not isinstance(refusal, TranslationRefusal)
        or refusal.handoff.draft.declaration.raw != meaning.raw
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

requirement: dict[str, object] = {
    "task_level": "L2",
    "capability_minima": {"writing_editorial": 3},
    "hard_constraints": {"minimum_input_context_tokens": 512},
}
typed = Declaration(encode(meaning.value | requirement_markers(requirement) | {"context": list[str]()}))
sent: list[object] = []


class Recommendation(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        sent.append(json.loads(raw))
        body = encode(
            {
                "schema_version": 1,
                "decision": {
                    "requirement": requirement,
                    "catalog_version": 1,
                    "catalog_updated_on": "2026-10-08",
                    "resource_policy_version": 1,
                    "evaluated_at": "2026-10-08T00:00:00Z",
                    "selector_mode": "balanced",
                    "degraded": False,
                    "reason_codes": ["selected_balanced"],
                    "preference_order": [],
                    "alternatives": [],
                    "excluded": [],
                    "closest_candidates": [],
                    "recoverable_candidates": [],
                    "selected": {
                        "identity": {"provider": "openai", "model": "synthetic-model", "variant": "opaque"},
                        "display_name": "Synthetic",
                        "reasoning_effort": None,
                        "eligible": True,
                        "degraded": False,
                        "capability_margin": 0,
                    },
                },
            }
        )
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


httpd = HTTPServer(("127.0.0.1", 0), Recommendation)
worker = Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
worker.start()
try:
    allocator = ScarcityRouterAllocator(
        f"http://127.0.0.1:{httpd.server_port}",
        (Allocation("fixture-harness", "openai", "synthetic-model", frozenset(), 2048, "synthetic binding"),),
        runtime_support=(RuntimeSupport("fixture-harness", "synthetic-v1", frozenset(), 0, "fixture:harness-proof"),),
    )
    with SQLiteProgramStore(state / "prepared.db") as store:
        intake = OrdinaryIntake(store, reader, lambda: 100)
        positive = intake.prepare("installed-smoke", subject, typed, expected_parent=draft.digest)
        approved_positive = intake.approve(principal, positive, policy, decision_id="typed-decision", expires_at=110)
        projection = intake.handoff(principal, approved_positive, policy, allocator)
        if not isinstance(projection, ResourceRequest) or sent:
            raise SystemExit("Installed typed handoff is not inert")
        allocation = intake.select(principal, approved_positive, policy, allocator)
        if not isinstance(allocation, Allocation) or sent != [{"requirement": requirement}]:
            raise SystemExit("Installed typed public selection differs")
        data = encode_allocation(allocation)
        store.intent("fixture:selection", "fixture:selection", {"allocation": data.decode()})
        store.finalize_artifact("fixture:selection", "allocation", data)
    with SQLiteProgramStore(state / "prepared.db") as store:
        if decode_allocation(store.artifact("fixture:selection", "allocation")) != allocation:
            raise SystemExit("Installed typed allocation reopen differs")
        # A changed recommendation is never needed to recover immutable bytes.
        if replace(allocation, requirements_provenance=None) == allocation:
            raise SystemExit("Installed typed allocation lacks original subject evidence")
finally:
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=2)
if transport.pushes or transport.posts:
    raise SystemExit("Ordinary preparation performed a Forge effect")
print("Installed ordinary intake: inert approval/refusal, typed HTTP selection and original evidence reopen passed.")
