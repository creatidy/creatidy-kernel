# SPDX-License-Identifier: Apache-2.0
"""Offline all-six-AC intake reception, producer fixtures and authority races."""

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from test_domain import active, finished, satisfied

from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forge_refs import ForgeBinding
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.intake import ForgeIntakeEvidence, GitContentSource
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.scarcity_router import ScarcityRouterAllocator
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import TASKS
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.domain import (
    AmendProgramSpec,
    AuthorityEnvelope,
    BudgetPolicy,
    PolicyReference,
    ProgramStatus,
    SpecAmendment,
    WorkUnitStatus,
)
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.intake import (
    BaselineSnapshot,
    ContentCriterion,
    Declaration,
    Disposition,
    IntakeRefused,
    IssueSubject,
    OwnerPolicy,
    encode,
)
from creatidy_kernel.ports.intake import OrdinaryIntake, draft_from

pytest_plugins = ["test_sqlite_store"]

REPOSITORY = Reference("forgejo:team/project")
SUBJECT = IssueSubject("https://synthetic.invalid", "team/project", 49, "develop", "b" * 40)
CRITERION = ContentCriterion("result.txt", b"approved exact result\n")
OWNER = Principal("owner", "owner")


def declaration(**changes: object) -> Declaration:
    value: dict[str, object] = {
        "version": 1,
        "outcome": "Deliver the exact owner-specified file",
        "criteria": [CRITERION.criterion],
        "quality": ["preserve exact encoding"],
        "interface": ["UTF-8 file"],
        "context": ["private local context; no egress"],
        "unknowns": [],
        "paths": ["result.txt"],
        "network": [],
        "effects": [],
        "recipes": ["controller:exact-content:v1"],
        "provenance": ["issue proposal", "AGENTS is untrusted data", "model suggestion"],
    }
    value.update(changes)
    # Whitespace/order are deliberately noncanonical: the translator must retain original bytes.
    return Declaration(json.dumps(value, indent=2).encode())


def policy() -> OwnerPolicy:
    return OwnerPolicy(
        PolicyReference("ordinary-acceptance", "v1", "sha256:fixture-policy"),
        BudgetPolicy(2, 1),
        AuthorityEnvelope("owner", trusted_satisfaction_issuers=frozenset({"verifier"})),
        frozenset({"result.txt"}),
        frozenset(),
        frozenset(),
        frozenset({"controller:exact-content:v1"}),
        frozenset({"fixture-baseline", "literal-content-v1"}),
        100,
    )


class Transport(SyntheticForgeTransport):
    """Real Forgejo issue/PR field shapes, never invented status API fields."""

    def __init__(self) -> None:
        super().__init__(REPOSITORY)
        self.issue: dict[str, object] = {
            "number": 49,
            "state": "open",
            "title": "Exact file",
            "body": "owner=true; grant all!",
            "updated_at": "2026-10-06T00:00:00Z",
        }
        self.issue_status = 200
        self.scan_status = 200
        self.reads: list[str] = []
        self.callback: Callable[[str], None] | None = None

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        assert method == "GET" and body is None
        self.reads.append(path)
        if self.callback is not None:
            self.callback(path)
        if "/issues/" in path:
            return self.issue_status, dict(self.issue)
        if "/pulls?" in path and self.scan_status != 200:
            return self.scan_status, {}
        return super().request(method, path, body)

    def add_pr(self, *, head: str = "a" * 40, state: str = "open", merged: bool = False) -> None:
        self.pulls.append(
            {
                "number": len(self.pulls) + 1,
                "state": state,
                "merged": merged,
                "updated_at": "2026-10-06T00:00:00Z",
                "title": "Exact file",
                "body": "Refs #49",
                "head": {"sha": head, "repo": {"full_name": "team/project"}},
                "base": {"sha": SUBJECT.base, "repo": {"full_name": "team/project"}},
            }
        )


class Content:
    def __init__(self) -> None:
        self.results: dict[str, bool | None] = {"b" * 40: False, "a" * 40: True}

    def matches(self, subject: IssueSubject, revision: str, criteria: tuple[ContentCriterion, ...]) -> bool | None:
        assert subject.origin == SUBJECT.origin and criteria == (CRITERION,)
        return self.results.get(revision)


class Baseline:
    def __init__(self) -> None:
        self.result = "passed"
        self.reference = "fixture:baseline:1"
        self.observed_at = 100
        self.subject = SUBJECT
        self.producer = "fixture-baseline"
        self.unknown = False

    def read_baseline(self, subject: IssueSubject, recipe_digest: str) -> BaselineSnapshot | None:
        if self.unknown:
            return None
        return BaselineSnapshot(
            self.subject, recipe_digest, self.producer, self.reference, self.result, self.observed_at
        )


class Scenario:
    def __init__(self) -> None:
        self.time = 100
        self.transport = Transport()
        self.forge = ForgejoForge(self.transport, self.transport, lambda _: False, page_size=2)
        self.content = Content()
        self.baseline = Baseline()
        self.reader = ForgeIntakeEvidence(
            self.forge,
            self.transport.binding,
            self.content,
            self.baseline,
            (CRITERION,),
            "literal-content-v1",
            lambda: self.time,
            max_pages=3,
        )


def test_inert_draft_approved_intact_real_translation_and_reopen(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    original = declaration()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("issue49", SUBJECT, original, expected_parent=None)
        assert draft.evidence.disposition is Disposition.REMAINS
        assert draft_from(encode(draft.payload())) == draft
        assert not store.operations("task:")
        for record in store.operations("ordinary:"):
            with pytest.raises(OperationConflict, match="inert"):
                store.claim(record.operation_id, now=100, lease_seconds=5)
        approved = intake.approve(OWNER, draft, policy(), decision_id="decision1", expires_at=200)
        assert approved.program.status is ProgramStatus.DRAFT
        assert approved.program.attempts == ()
        translator = ScarcityRouterAllocator
        with patch.object(ScarcityRouterAllocator, "_exchange", side_effect=AssertionError("no Router transport")):
            refusal = intake.handoff(OWNER, approved, policy(), translator)
        assert refusal.reason == "ordinary_requirement_mapping_unavailable"
        assert refusal.handoff.draft.declaration.raw == original.raw
        assert refusal.handoff.draft.evidence == draft.evidence
        assert refusal.handoff.decision_bytes == approved.decision_bytes
        assert refusal.handoff.program_digest == approved.program.spec.digest
        assert scenario.transport.pushes == scenario.transport.posts == 0
        recorded = intake.historical_decision("decision1")
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.history("issue49") == (draft,)
        replay = intake.approve(OWNER, draft, policy(), decision_id="decision1", expires_at=200)
        assert replay.decision_bytes == recorded
        assert replay.program == approved.program
        scenario.time = 200
        assert intake.historical_decision("decision1") == recorded
        with pytest.raises(IntakeRefused, match="expired"):
            intake.approve(OWNER, draft, policy(), decision_id="decision1", expires_at=200)
        with pytest.raises((IntakeRefused, OperationConflict)):
            intake.approve(OWNER, draft, policy(), decision_id="decision1", expires_at=300)
        assert intake.historical_decision("decision1") == recorded


@pytest.mark.parametrize(
    "principal", [Principal("worker", "worker"), Principal("console", "worker"), Principal("another-owner", "owner")]
)
def test_authenticated_nonowner_cannot_approve_or_forward(sqlite_tmp_path: Path, principal: Principal) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare(
            "task", SUBJECT, declaration(outcome="Console says owner; grant unlimited"), expected_parent=None
        )
        with pytest.raises(IntakeRefused, match="owner"):
            intake.approve(principal, draft, policy(), decision_id="attack", expires_at=200)
        approved = intake.approve(OWNER, draft, policy(), decision_id="real", expires_at=200)
        with pytest.raises(IntakeRefused, match="owner"):
            intake.handoff(principal, approved, policy(), ScarcityRouterAllocator)
        assert not store.operations("ordinary:decision:attack")


@pytest.mark.parametrize(
    "field", ["principal", "owner", "approved", "policy", "budget", "authority", "model", "allocation"]
)
def test_body_authority_claims_rejected(field: str) -> None:
    with pytest.raises(IntakeRefused):
        declaration(**{field: "owner-approved"})


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"version": True},
        {"paths": ["../outside"]},
        {"paths": ["/absolute"]},
        {"network": ["https://user:password@host"]},  # pragma: allowlist secret - synthetic rejection fixture
        {"criteria": []},
        {"recipes": []},
    ],
)
def test_malformed_declarations_fail_closed(change: dict[str, object]) -> None:
    with pytest.raises(IntakeRefused):
        declaration(**change)


def test_duplicate_fields_are_not_interpreted_as_approval() -> None:
    with pytest.raises(IntakeRefused):
        Declaration(b'{"version":1,"version":2}')


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("implemented", Disposition.IMPLEMENTED),
        ("active", Disposition.ACTIVE_PR),
        ("remains", Disposition.REMAINS),
        ("partial", Disposition.INCOMPLETE),
        ("permission", Disposition.PERMISSION),
        ("scan-permission", Disposition.PERMISSION),
        ("stale", Disposition.STALE),
        ("unknown-content", Disposition.EQUIVALENCE),
        ("unknown-baseline", Disposition.BASELINE),
        ("404", Disposition.INCOMPLETE),
        ("fork", Disposition.INCOMPLETE),
        ("empty-checks", Disposition.BASELINE),
        ("merged", Disposition.REMAINS),
        ("closed", Disposition.REMAINS),
        ("missing-pr-object", Disposition.EQUIVALENCE),
        ("moving-source", Disposition.STALE),
        ("moving-scan", Disposition.INCOMPLETE),
    ],
)
def test_producer_dispositions_never_use_open_title_or_link_as_absence(mode: str, expected: Disposition) -> None:
    scenario = Scenario()
    if mode == "implemented":
        scenario.content.results[SUBJECT.base] = True
    elif mode in {"active", "fork", "merged", "closed", "missing-pr-object", "moving-scan"}:
        scenario.transport.add_pr(state="closed" if mode in {"merged", "closed"} else "open", merged=mode == "merged")
        if mode == "fork":
            cast(dict[str, object], scenario.transport.pulls[0]["head"])["repo"] = {"full_name": "fork/project"}
        elif mode == "missing-pr-object":
            scenario.content.results["a" * 40] = None
        elif mode == "moving-scan":
            count = 0

            def move(path: str) -> None:
                nonlocal count
                if "/pulls?" in path:
                    count += 1
                    if count == 3:
                        scenario.transport.add_pr(head="c" * 40)

            scenario.transport.callback = move
    elif mode == "partial":
        for _ in range(6):
            scenario.transport.add_pr(state="closed")
    elif mode == "permission":
        scenario.transport.issue_status = 403
    elif mode == "scan-permission":
        scenario.transport.scan_status = 403
    elif mode == "stale":
        scenario.transport.branches["develop"] = "c" * 40
    elif mode == "unknown-content":
        scenario.content.results[SUBJECT.base] = None
    elif mode in {"unknown-baseline", "empty-checks"}:
        scenario.baseline.unknown = True
    elif mode == "404":
        scenario.transport.issue_status = 404
    elif mode == "moving-source":
        count = 0

        def move_source(path: str) -> None:
            nonlocal count
            if "/branches/" in path:
                count += 1
                if count == 2:
                    scenario.transport.branches["develop"] = "c" * 40

        scenario.transport.callback = move_source
    observed = scenario.reader.read(SUBJECT, declaration())
    assert observed.disposition is expected
    assert scenario.transport.pushes == scenario.transport.posts == 0


def test_general_semantic_criterion_does_not_become_a_byte_hash_claim() -> None:
    scenario = Scenario()
    scenario.content.results[SUBJECT.base] = True
    evidence = scenario.reader.read(SUBJECT, declaration(criteria=["Fix all cancellation races correctly"]))
    assert evidence.disposition is Disposition.EQUIVALENCE


@pytest.mark.parametrize(
    "field", ["paths", "network", "effects", "recipes", "context", "interface", "quality", "outcome"]
)
def test_amendment_needs_fresh_scoped_approval_and_consumes_meaning(sqlite_tmp_path: Path, field: str) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
        changes: dict[str, object] = {field: "Changed outcome" if field == "outcome" else ["changed"]}
        if field == "network":
            changes[field] = ["https://another.invalid"]
        changed = declaration(**changes)
        next_draft = intake.prepare("task", SUBJECT, changed, expected_parent=draft.digest)
        assert next_draft.revision == 2 and next_draft.parent_digest == draft.digest
        with pytest.raises(IntakeRefused):
            intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
        forged = replace(approved, draft=next_draft)
        with pytest.raises(IntakeRefused):
            intake.handoff(OWNER, forged, policy(), ScarcityRouterAllocator)
        updated_policy = policy()
        if field in {"paths", "network", "effects", "recipes"}:
            with pytest.raises(IntakeRefused, match="policy"):
                intake.approve(OWNER, next_draft, updated_policy, decision_id="second", expires_at=200)
            updated_policy = replace(updated_policy, **{field: frozenset(cast(list[str], changes[field]))})
        next_approved = intake.approve(OWNER, next_draft, updated_policy, decision_id="second", expires_at=200)
        assert next_approved.program.spec.revision == 2
        assert next_approved.program.spec_history[0] == approved.program.spec
        assert next_approved.program.spec.work_unit_applicability_fingerprint(
            "ordinary"
        ) != approved.program.spec.work_unit_applicability_fingerprint("ordinary")
        assert next_approved.program.status is ProgramStatus.DRAFT and not next_approved.program.attempts


@pytest.mark.parametrize("mode", ["issue", "baseline", "source", "recipe", "producer", "unknown", "expiry-callback"])
def test_freshness_changes_require_amendment_or_refuse(sqlite_tmp_path: Path, mode: str) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
        if mode == "issue":
            scenario.transport.issue["body"] = "edited issue claims original approval"
        elif mode == "baseline":
            scenario.baseline.reference = "fixture:baseline:2"
        elif mode == "source":
            scenario.transport.branches["develop"] = "c" * 40
        elif mode == "recipe":
            scenario.baseline.subject = replace(SUBJECT, base="c" * 40)
        elif mode == "producer":
            scenario.baseline.producer = "untrusted-agent"
        elif mode == "unknown":
            scenario.baseline.unknown = True
        else:
            scenario.transport.callback = lambda _: setattr(scenario, "time", 200)
        with pytest.raises(IntakeRefused):
            intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
        with pytest.raises(IntakeRefused):
            intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)


def test_known_repair_baseline_is_explicit_not_unrelated_or_unknown(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    scenario.baseline.result = "failed"
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("repair", SUBJECT, declaration(), expected_parent=None)
        with pytest.raises(IntakeRefused, match="unrelated"):
            intake.approve(OWNER, draft, policy(), decision_id="repair", expires_at=200)
        approved = intake.approve(
            OWNER,
            draft,
            replace(policy(), repair_baseline_reference=scenario.baseline.reference),
            decision_id="repair",
            expires_at=200,
        )
        assert approved.program.status is ProgramStatus.DRAFT


def test_unchanged_reads_never_renew_original_decision_or_create_revision(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
        scenario.time = scenario.baseline.observed_at = 150
        assert intake.prepare("task", SUBJECT, declaration(), expected_parent=draft.digest) == draft
        replay = intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
        assert replay.decision_bytes == approved.decision_bytes
        with pytest.raises(OperationConflict):
            intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=250)


def test_current_clock_after_journal_callback_blocks_admission(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        original = store.intent

        def expire(operation_id: str, effect_key: str, request: dict[str, object]):
            result = original(operation_id, effect_key, request)
            scenario.time = 200
            return result

        with (
            patch.object(store, "intent", side_effect=expire),
            patch.object(store, "create", side_effect=AssertionError("expired create")),
        ):
            with pytest.raises(IntakeRefused, match="expired"):
                intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)


@pytest.mark.parametrize("crash", ["artifact", "create", "amend"])
def test_crash_recovery_retains_original_records_and_consumption(sqlite_tmp_path: Path, crash: str) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        if crash == "artifact":
            with patch.object(store, "finalize_artifact", side_effect=RuntimeError("crash")):
                with pytest.raises(RuntimeError):
                    intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
            draft = intake.history("task")[-1]
        else:
            draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        if crash == "amend":
            intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
            draft = intake.prepare(
                "task", SUBJECT, declaration(context=["new local input"]), expected_parent=draft.digest
            )
        failing_method = "admit" if crash == "amend" else "create"
        with patch.object(store, failing_method, side_effect=RuntimeError("crash")):
            with pytest.raises(RuntimeError):
                intake.approve(OWNER, draft, policy(), decision_id="recover", expires_at=200)
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        approved = intake.approve(OWNER, draft, policy(), decision_id="recover", expires_at=200)
        assert approved.program.status is ProgramStatus.DRAFT and not approved.program.attempts
        assert len(store.operations("ordinary:consumed:task:")) == (2 if crash == "amend" else 1)
        assert all(record.attempts == 0 for record in store.operations("ordinary:"))


def test_real_git_exact_content_reads_only_frozen_objects(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    reference_git(repository, "init", "--initial-branch=develop")
    (repository / "result.txt").write_bytes(CRITERION.expected)
    reference_git(repository, "add", "result.txt")
    reference_git(
        repository, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-m", "fixture"
    )
    sha = reference_git(repository, "rev-parse", "HEAD")
    subject = replace(SUBJECT, base=sha)
    source = GitContentSource(repository, ForgeBinding(SUBJECT.origin, SUBJECT.repository))
    assert source.matches(subject, sha, (CRITERION,)) is True
    (repository / "result.txt").write_bytes(b"dirty untrusted workspace")
    assert source.matches(subject, sha, (CRITERION,)) is True
    assert source.matches(subject, sha, (ContentCriterion("result.txt", b"different"),)) is False
    assert source.matches(subject, "c" * 40, (CRITERION,)) is None
    assert source.matches(subject, sha, (ContentCriterion("missing.txt", b"result"),)) is None
    with pytest.raises(IntakeRefused):
        source.matches(replace(subject, origin="https://wrong.invalid"), sha, (CRITERION,))


def test_ordinary_consumption_invalidates_actual_acceptance_but_operational_budget_does_not(
    sqlite_tmp_path: Path,
) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
        # Pure domain acceptance fixture, not activation through the ordinary intake API.
        accepted = satisfied(finished(active(approved.program.spec), "ordinary"), "ordinary")
        assert accepted.state("ordinary").status is WorkUnitStatus.SATISFIED
        operational = accepted.apply(
            AmendProgramSpec(accepted.revision, "owner", SpecAmendment(1, budget=BudgetPolicy(5, 1)))
        )
        assert operational.state("ordinary").status is WorkUnitStatus.SATISFIED
        next_draft = intake.prepare(
            "task", SUBJECT, declaration(context=["privacy-critical changed input"]), expected_parent=draft.digest
        )
        next_approved = intake.approve(OWNER, next_draft, policy(), decision_id="second", expires_at=200)
        changed = accepted.apply(
            AmendProgramSpec(
                accepted.revision, "owner", SpecAmendment(1, initial_inputs=next_approved.program.spec.initial_inputs)
            )
        )
        assert changed.state("ordinary").status is WorkUnitStatus.READY
        assert changed.satisfactions == accepted.satisfactions
        assert accepted.spec == approved.program.spec


def test_historical_task_digests_registry_unchanged_and_ordinary_not_registered() -> None:
    assert set(TASKS) == {"143", "166"}
    assert {key: factory().digest for key, factory in TASKS.items()} == {
        "143": "9e71b426d9d8dc9f9f1d9d011fa2cda836a514ca1a9e4f48a36c91948d6ea06e",  # pragma: allowlist secret
        "166": "d4cd37e0bd59f6b3c13b628add646dba89858fb2a5861e951f52a528bf9ccbd8",  # pragma: allowlist secret
    }


def test_unsupported_draft_version_refuses_without_modifying_history(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        changed = draft.payload()
        changed["version"] = 2
        with pytest.raises(IntakeRefused, match="version"):
            draft_from(encode(changed))
        assert intake.history("task") == (draft,)


@pytest.mark.parametrize("mode", ["unknowns", "baseline", "future", "stale", "untrusted", "implemented", "active-pr"])
def test_required_unknown_or_nonremaining_evidence_never_constructs_approved_program(
    sqlite_tmp_path: Path, mode: str
) -> None:
    scenario = Scenario()
    meaning = declaration(unknowns=["unresolved required isolation"] if mode == "unknowns" else [])
    if mode == "baseline":
        scenario.baseline.unknown = True
    elif mode == "future":
        scenario.baseline.observed_at = 300
    elif mode == "stale":
        scenario.baseline.observed_at = 1
        scenario.time = 150
    elif mode == "untrusted":
        scenario.baseline.producer = "model-claim"
    elif mode == "implemented":
        scenario.content.results[SUBJECT.base] = True
    elif mode == "active-pr":
        scenario.transport.add_pr()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, meaning, expected_parent=None)
        with patch.object(store, "create", side_effect=AssertionError("no approved Program")):
            with pytest.raises(IntakeRefused):
                intake.approve(OWNER, draft, policy(), decision_id="blocked", expires_at=200)
        assert not store.operations("ordinary:decision:")


def test_explicit_base_amendment_is_not_silent_current_source_approval(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        old = intake.approve(OWNER, draft, policy(), decision_id="old", expires_at=200)
        subject = replace(SUBJECT, base="c" * 40)
        scenario.transport.branches["develop"] = subject.base
        scenario.content.results[subject.base] = False
        scenario.baseline.subject = subject
        with pytest.raises(IntakeRefused):
            intake.approve(OWNER, draft, policy(), decision_id="old", expires_at=200)
        amended = intake.prepare("task", subject, declaration(), expected_parent=draft.digest)
        current = intake.approve(OWNER, amended, policy(), decision_id="current", expires_at=200)
        assert current.program.spec.parent_digest == old.program.spec.digest
        assert current.program.spec.work_unit_applicability_fingerprint(
            "ordinary"
        ) != old.program.spec.work_unit_applicability_fingerprint("ordinary")
        assert intake.history("task") == (draft, amended)


def test_expired_interrupted_approval_needs_new_decision_not_receipt_revival(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        with patch.object(store, "create", side_effect=RuntimeError("crash")):
            with pytest.raises(RuntimeError):
                intake.approve(OWNER, draft, policy(), decision_id="expired", expires_at=110)
        original = intake.historical_decision("expired")
        scenario.time = 110
        with pytest.raises(IntakeRefused):
            intake.approve(OWNER, draft, policy(), decision_id="expired", expires_at=110)
        approved = intake.approve(OWNER, draft, policy(), decision_id="fresh", expires_at=200)
        assert approved.program.status is ProgramStatus.DRAFT
        assert intake.historical_decision("expired") == original
        assert len(store.operations("ordinary:consumed:task:")) == 2


def test_changed_pinned_policy_changes_applicability_while_authority_budget_do_not(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        first = intake.approve(OWNER, draft, policy(), decision_id="first", expires_at=200)
        operational_policy = replace(
            policy(),
            budget=BudgetPolicy(5, 2),
            authority=AuthorityEnvelope("owner", max_attempts=5, trusted_satisfaction_issuers=frozenset({"verifier"})),
        )
        operational = intake.approve(OWNER, draft, operational_policy, decision_id="operational", expires_at=200)
        assert first.program.spec.work_unit_applicability_fingerprint(
            "ordinary"
        ) == operational.program.spec.work_unit_applicability_fingerprint("ordinary")
        changed = replace(
            operational_policy, reference=PolicyReference("ordinary-acceptance", "v2", "fixture:changed-policy")
        )
        current = intake.approve(OWNER, draft, changed, decision_id="policy-change", expires_at=200)
        assert current.program.spec.work_unit_applicability_fingerprint(
            "ordinary"
        ) != operational.program.spec.work_unit_applicability_fingerprint("ordinary")
        assert len(current.program.spec_history) == 3


@pytest.mark.parametrize("mode", ["implemented", "active"])
def test_actual_git_producer_composed_with_forgejo_positive_paths(tmp_path: Path, mode: str) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    reference_git(repository, "init", "--initial-branch=develop")
    (repository / "result.txt").write_bytes(b"old exact file bytes\n")
    reference_git(repository, "add", "result.txt")
    reference_git(
        repository,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-m",
        "baseline fixture",
    )
    base = reference_git(repository, "rev-parse", "HEAD")
    (repository / "result.txt").write_bytes(CRITERION.expected)
    reference_git(repository, "add", "result.txt")
    reference_git(
        repository,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-m",
        "exact result fixture",
    )
    head = reference_git(repository, "rev-parse", "HEAD")
    scenario = Scenario()
    selected = replace(SUBJECT, base=head if mode == "implemented" else base)
    scenario.transport.branches["develop"] = selected.base
    scenario.baseline.subject = selected
    scenario.reader.source = GitContentSource(repository, scenario.transport.binding)
    if mode == "active":
        scenario.transport.add_pr(head=head)
        cast(dict[str, object], scenario.transport.pulls[0]["base"])["sha"] = base
    observed = scenario.reader.read(selected, declaration())
    assert observed.disposition is (Disposition.IMPLEMENTED if mode == "implemented" else Disposition.ACTIVE_PR)
    proof = json.loads(observed.relevance_proof)
    assert proof[0]["source"] == selected.base
    if mode == "active":
        assert proof[1]["head"] == f"forgejo:{head}" and proof[1]["content_result"] is True


def test_nonregular_blob_is_not_exact_file_implementation(tmp_path: Path) -> None:
    repository = tmp_path / "source"
    repository.mkdir()
    reference_git(repository, "init", "--initial-branch=develop")
    (repository / "result.txt").symlink_to("missing-target")
    reference_git(repository, "add", "result.txt")
    reference_git(
        repository,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-m",
        "symlink fixture",
    )
    sha = reference_git(repository, "rev-parse", "HEAD")
    source = GitContentSource(repository, ForgeBinding(SUBJECT.origin, SUBJECT.repository))
    assert source.matches(replace(SUBJECT, base=sha), sha, (ContentCriterion("result.txt", b"missing-target"),)) is None
