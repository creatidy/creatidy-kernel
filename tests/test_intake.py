# SPDX-License-Identifier: Apache-2.0
"""Offline all-six-AC intake reception, producer fixtures and authority races."""

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from test_domain import active, finished, satisfaction, satisfied

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
    ActivateProgram,
    AmendProgramSpec,
    AuthorityEnvelope,
    BudgetPolicy,
    CancelProgram,
    FinishAttempt,
    PauseProgram,
    PolicyReference,
    PrepareAttempt,
    Program,
    ProgramSpec,
    ProgramStatus,
    SatisfyWorkUnit,
    SpecAmendment,
    StartAttempt,
    WorkUnitStatus,
)
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.intake import (
    BaselineSnapshot,
    ContentCriterion,
    Declaration,
    Disposition,
    Draft,
    IntakeRefused,
    IssueSubject,
    OwnerPolicy,
    TranslationRefusal,
    encode,
)
from creatidy_kernel.ports.intake import OrdinaryIntake, draft_from
from creatidy_kernel.ports.program_store import ProgramReadChanged

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
        self.branch_status = 200
        self.scan_status = 200
        self.pull_status: dict[str, int] = {}
        self.reads: list[str] = []
        self.callback: Callable[[str], None] | None = None

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        assert method == "GET" and body is None
        self.reads.append(path)
        if self.callback is not None:
            self.callback(path)
        if "/issues/" in path:
            return self.issue_status, dict(self.issue)
        if "/branches/" in path and self.branch_status != 200:
            return self.branch_status, {}
        if "/pulls?" in path and self.scan_status != 200:
            return self.scan_status, {}
        if "/pulls/" in path:
            status = self.pull_status.get(path.rsplit("/", 1)[1], 200)
            if status != 200:
                return status, {}
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
                "base": {"sha": SUBJECT.base, "ref": SUBJECT.branch, "repo": {"full_name": "team/project"}},
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
        self.recipe_digest: str | None = None
        self.unknown = False

    def read_baseline(self, subject: IssueSubject, recipe_digest: str) -> BaselineSnapshot | None:
        if self.unknown:
            return None
        return BaselineSnapshot(
            self.subject,
            recipe_digest if self.recipe_digest is None else self.recipe_digest,
            self.producer,
            self.reference,
            self.result,
            self.observed_at,
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
        assert isinstance(refusal, TranslationRefusal)
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


@pytest.mark.parametrize("phase", ["approve", "handoff"])
@pytest.mark.parametrize(
    "movement",
    [
        "reference",
        "subject",
        "recipe",
        "producer",
        "result",
        "disappeared",
        "future-time",
        "invalid-time",
        "blank-reference",
        "unknown-result",
    ],
)
def test_closing_baseline_change_blocks_approval_and_real_translator(
    sqlite_tmp_path: Path, phase: str, movement: str
) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = None
        if phase == "handoff":
            approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=200)
        reads = 0

        def move_at_closing_issue(path: str) -> None:
            nonlocal reads
            if "/issues/" not in path:
                return
            reads += 1
            if reads != 2:
                return
            if movement == "reference":
                scenario.baseline.reference = "fixture:baseline:changed"
            elif movement == "subject":
                scenario.baseline.subject = replace(SUBJECT, base="c" * 40)
            elif movement == "recipe":
                scenario.baseline.recipe_digest = "changed-recipe"
            elif movement == "producer":
                scenario.baseline.producer = "changed-producer"
            elif movement == "result":
                scenario.baseline.result = "failed"
            elif movement == "disappeared":
                scenario.baseline.unknown = True
            elif movement == "future-time":
                scenario.baseline.observed_at = 300
            elif movement == "invalid-time":
                scenario.baseline.observed_at = 0
            elif movement == "blank-reference":
                scenario.baseline.reference = ""
            else:
                scenario.baseline.result = "unknown"

        scenario.transport.callback = move_at_closing_issue
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused):
                if phase == "approve":
                    intake.approve(OWNER, draft, policy(), decision_id="changed", expires_at=200)
                else:
                    assert approved is not None
                    intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        if phase == "approve":
            assert store.find_program("ordinary:task") is None
        assert reads == 2


@pytest.mark.parametrize(
    "target,expected",
    [
        ("release", Disposition.REMAINS),
        (None, Disposition.INCOMPLETE),
        ("", Disposition.INCOMPLETE),
        ("bad..branch", Disposition.INCOMPLETE),
        (7, Disposition.INCOMPLETE),
    ],
)
def test_same_sha_pr_requires_known_selected_target_branch(target: object, expected: Disposition) -> None:
    scenario = Scenario()
    scenario.transport.add_pr()
    base = cast(dict[str, object], scenario.transport.pulls[0]["base"])
    if target is None:
        base.pop("ref")
    else:
        base["ref"] = target
    observed = scenario.reader.read(SUBJECT, declaration())
    assert observed.disposition is expected
    if target == "release":
        assert json.loads(observed.relevance_proof)[1]["target_branch"] == "release"


def test_pr_target_movement_is_incomplete_with_same_sha_and_update_time() -> None:
    scenario = Scenario()
    scenario.transport.add_pr()
    original = scenario.forge.change_snapshot(REPOSITORY, Reference("forgejo:team/project#1"))
    reads = 0

    def move_target(path: str) -> None:
        nonlocal reads
        if path.endswith("/pulls/1"):
            reads += 1
            if reads == 2:
                cast(dict[str, object], scenario.transport.pulls[0]["base"])["ref"] = "release"

    scenario.transport.callback = move_target
    observed = scenario.reader.read(SUBJECT, declaration())
    current = scenario.forge.change_snapshot(REPOSITORY, Reference("forgejo:team/project#1"))
    assert current.observation == original.observation and current.updated_at == original.updated_at
    assert observed.disposition is Disposition.INCOMPLETE


@pytest.mark.parametrize("closing_time", [50, 150])
def test_same_baseline_receipt_retains_earliest_age_without_revision_or_expiry_renewal(
    sqlite_tmp_path: Path, closing_time: int
) -> None:
    scenario = Scenario()
    scenario.time = 150
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=200)
        original_decision = intake.historical_decision("original")
        reads = 0

        def update_timestamp(path: str) -> None:
            nonlocal reads
            if "/issues/" in path:
                reads += 1
                if reads == 2:
                    scenario.baseline.observed_at = closing_time

        scenario.transport.callback = update_timestamp
        evidence = scenario.reader.read(SUBJECT, declaration())
        assert evidence.observed_at == min(100, closing_time)
        assert evidence.digest == draft.evidence.digest
        scenario.transport.callback = None
        assert intake.prepare("task", SUBJECT, declaration(), expected_parent=draft.digest) == draft
        assert len(intake.history("task")) == 1
        assert intake.historical_decision("original") == original_decision
        scenario.baseline.observed_at = min(100, closing_time)
        scenario.time = scenario.baseline.observed_at + policy().freshness_seconds + 1
        with pytest.raises(IntakeRefused):
            intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)


def test_same_baseline_receipt_timestamp_refresh_cannot_revive_original_age(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        decision = intake.historical_decision("original")
        scenario.time = scenario.baseline.observed_at = 201
        assert intake.prepare("task", SUBJECT, declaration(), expected_parent=draft.digest) == draft
        with pytest.raises(IntakeRefused, match="expired"):
            intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
        with pytest.raises(IntakeRefused, match="expired"):
            intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        assert intake.historical_decision("original") == decision


@pytest.mark.parametrize("amendment", ["context", "issue", "provenance"])
def test_amended_draft_retains_same_baseline_receipt_durable_age(sqlite_tmp_path: Path, amendment: str) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = scenario.baseline.observed_at = 201
        meaning = declaration()
        if amendment == "context":
            meaning = declaration(context=["new context does not create baseline evidence"])
        elif amendment == "issue":
            scenario.transport.issue["body"] = "Issue edit does not create baseline evidence"
        else:
            meaning = declaration(provenance=["different proposal provenance, unchanged evidence producer"])
        changed = intake.prepare("task", SUBJECT, meaning, expected_parent=original.digest)
        assert changed.revision == 2
        assert changed.evidence.digest != original.evidence.digest or changed.declaration != original.declaration
        assert changed.evidence.observed_at == 100


@pytest.mark.parametrize("amendment", ["context", "issue", "alias"])
def test_old_baseline_cannot_approve_amendment_after_reopen(sqlite_tmp_path: Path, amendment: str) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        decision = intake.historical_decision("original")
        scenario.time = scenario.baseline.observed_at = 201
        if amendment == "issue":
            scenario.transport.issue["body"] = "changed issue, same baseline receipt"
        task_id = "alias" if amendment == "alias" else "task"
        changed = intake.prepare(
            task_id,
            SUBJECT,
            declaration(context=["amended context"]) if amendment == "context" else declaration(),
            expected_parent=None if amendment == "alias" else original.digest,
        )
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        recovered = intake.history(task_id)[-1]
        assert recovered == changed
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="expired"):
                intake.approve(OWNER, recovered, policy(), decision_id="stale-amendment", expires_at=400)
            with pytest.raises(IntakeRefused):
                intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert store.find_program("ordinary:task") == approved.program
        if amendment == "alias":
            assert store.find_program("ordinary:alias") is None
        assert intake.historical_decision("original") == decision


@pytest.mark.parametrize("amendment", ["context", "issue", "alias"])
def test_original_baseline_age_blocks_amended_approval_handoff_after_reopen(
    sqlite_tmp_path: Path, amendment: str
) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = scenario.baseline.observed_at = 150
        if amendment == "issue":
            scenario.transport.issue["body"] = "changed issue, same baseline receipt"
        task_id = "alias" if amendment == "alias" else "task"
        changed = intake.prepare(
            task_id,
            SUBJECT,
            declaration(context=["new meaning"]) if amendment == "context" else declaration(),
            expected_parent=None if amendment == "alias" else original.digest,
        )
        approved = intake.approve(OWNER, changed, policy(), decision_id="amended", expires_at=400)
        decision = intake.historical_decision("amended")
    scenario.time = scenario.baseline.observed_at = 201
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="expired"):
                intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            with pytest.raises(IntakeRefused, match="expired"):
                intake.approve(OWNER, intake.history(task_id)[-1], policy(), decision_id="amended", expires_at=400)
            translator.assert_not_called()
        assert intake.historical_decision("amended") == decision


def test_nonadjacent_old_receipt_cannot_borrow_new_receipt_age(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = scenario.baseline.observed_at = 150
        scenario.baseline.reference = "fixture:baseline:new"
        newer = intake.prepare("task", SUBJECT, declaration(), expected_parent=original.digest)
        intake.approve(OWNER, newer, policy(), decision_id="newer", expires_at=400)
        scenario.time = scenario.baseline.observed_at = 201
        scenario.baseline.reference = "fixture:baseline:1"
        restored = intake.prepare(
            "task", SUBJECT, declaration(context=["restored old evidence"]), expected_parent=newer.digest
        )
        assert restored.evidence.observed_at == 100
        with pytest.raises(IntakeRefused, match="expired"):
            intake.approve(OWNER, restored, policy(), decision_id="restored", expires_at=400)


@pytest.mark.parametrize("task_id", ["task", "alias"])
def test_genuinely_new_identified_baseline_receipt_permits_fresh_age_after_reopen(
    sqlite_tmp_path: Path, task_id: str
) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
    scenario.time = scenario.baseline.observed_at = 201
    scenario.baseline.reference = "fixture:baseline:genuinely-new"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        fresh = intake.prepare(
            task_id,
            SUBJECT,
            declaration(context=["explicit amendment with new evidence"]),
            expected_parent=original.digest if task_id == "task" else None,
        )
        assert fresh.evidence.observed_at == 201
        approved = intake.approve(OWNER, fresh, policy(), decision_id="fresh", expires_at=400)
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            refusal = intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_called_once()
        assert isinstance(refusal, TranslationRefusal)
        assert refusal.handoff.draft.evidence.baseline_reference == "fixture:baseline:genuinely-new"
        assert refusal.handoff.draft.evidence.observed_at == 201
        assert refusal.reason == "ordinary_requirement_mapping_unavailable"
        assert intake.history("task")[0] == original


def test_existing_inflated_age_record_is_checked_against_older_history_on_reopen(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = scenario.baseline.observed_at = 150
        # Synthetic persisted shape from the former task-alias age-renewal bug.
        legacy = Draft(
            "legacy-alias",
            1,
            None,
            declaration(context=["amended legacy context"]),
            replace(original.evidence, observed_at=150),
        )
        raw = encode(legacy.payload())
        key = "ordinary:legacy-alias:draft:1"
        store.intent(key, key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()})
        store.finalize_artifact(key, "record", raw)
        approved = intake.approve(OWNER, legacy, policy(), decision_id="legacy", expires_at=400)
    scenario.time = scenario.baseline.observed_at = 201
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.history("legacy-alias") == (legacy,)
        assert intake.history("task")[0] == original
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="expired"):
                intake.approve(OWNER, legacy, policy(), decision_id="legacy", expires_at=400)
            with pytest.raises(IntakeRefused, match="expired"):
                intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert store.artifact(key, "record") == raw


@pytest.mark.parametrize("binding", ["source", "recipe"])
def test_old_receipt_cannot_claim_a_new_unverified_source_or_recipe(sqlite_tmp_path: Path, binding: str) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = scenario.baseline.observed_at = 201
        selected = SUBJECT
        meaning = declaration(context=["proposal claims fresh evidence"])
        current_policy = policy()
        if binding == "source":
            selected = replace(SUBJECT, base="c" * 40)
            scenario.transport.branches["develop"] = selected.base
            scenario.content.results[selected.base] = False
            # The identified baseline producer still binds the original source.
        else:
            meaning = declaration(recipes=["controller:new-recipe:v2"])
            current_policy = replace(policy(), recipes=frozenset({"controller:new-recipe:v2"}))
            scenario.baseline.recipe_digest = original.evidence.recipe_digest
        changed = intake.prepare("task", selected, meaning, expected_parent=original.digest)
        assert changed.evidence.disposition is Disposition.BASELINE
        assert changed.evidence.baseline_reference == "unknown"
        with pytest.raises(IntakeRefused):
            intake.approve(OWNER, changed, current_policy, decision_id="counterfeit", expires_at=400)
        assert not store.operations("ordinary:decision:counterfeit")


@pytest.mark.parametrize("field", ["baseline_reference", "baseline_producer", "baseline_result", "observed_at"])
def test_proposal_cannot_mint_new_baseline_identity_or_age(field: str) -> None:
    with pytest.raises(IntakeRefused):
        declaration(**{field: "new-evidence-claim"})


@pytest.mark.parametrize("observe_via", ["prepare", "approve", "handoff"])
def test_unchanged_lower_age_observation_survives_reopen_without_rewriting_draft(
    sqlite_tmp_path: Path, observe_via: str
) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        raw = store.artifact("ordinary:task:draft:1", "record")
        decision = intake.historical_decision("original")
        scenario.time = 150
        scenario.baseline.observed_at = 50
        if observe_via == "prepare":
            assert intake.prepare("task", SUBJECT, declaration(), expected_parent=original.digest) == original
        elif observe_via == "approve":
            assert intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400) == approved
        else:
            refusal = intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            assert isinstance(refusal, TranslationRefusal)
            assert refusal.handoff.draft == original
        assert intake.history("task") == (original,)
        assert store.artifact("ordinary:task:draft:1", "record") == raw
        assert intake.historical_decision("original") == decision
    scenario.time = scenario.baseline.observed_at = 151
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.history("task") == (original,)
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="expired"):
                intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            with pytest.raises(IntakeRefused, match="expired"):
                intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
            translator.assert_not_called()
        assert store.artifact("ordinary:task:draft:1", "record") == raw
        assert intake.historical_decision("original") == decision


@pytest.mark.parametrize("task_id", ["draft", "consumed", "decision", "baseline-age", "record"])
def test_reserved_looking_task_ids_do_not_poison_other_history_or_reopen(sqlite_tmp_path: Path, task_id: str) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare(task_id, SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="selected", expires_at=400)
        other = intake.prepare("unrelated", SUBJECT, declaration(), expected_parent=None)
        other_approved = intake.approve(OWNER, other, policy(), decision_id="unrelated", expires_at=400)
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.history(task_id) == (draft,)
        assert intake.history("unrelated") == (other,)
        assert intake.approve(OWNER, draft, policy(), decision_id="selected", expires_at=400) == approved
        assert intake.approve(OWNER, other, policy(), decision_id="unrelated", expires_at=400) == other_approved


def test_draft_and_consumed_task_ids_coexist_with_further_amendments(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        consumed = intake.prepare("consumed", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, consumed, policy(), decision_id="consumed", expires_at=400)
        draft = intake.prepare("draft", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="draft", expires_at=400)
        amendment = intake.prepare(
            "consumed", SUBJECT, declaration(context=["amended"]), expected_parent=consumed.digest
        )
        changed = intake.approve(OWNER, amendment, policy(), decision_id="amendment", expires_at=400)
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.history("consumed") == (consumed, amendment)
        assert intake.history("draft") == (draft,)
        assert intake.approve(OWNER, amendment, policy(), decision_id="amendment", expires_at=400) == changed
        assert intake.approve(OWNER, draft, policy(), decision_id="draft", expires_at=400) == approved


def test_lower_age_artifact_crash_recovers_from_nonexecutable_journal(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = 150
        scenario.baseline.observed_at = 50
        publish = store.finalize_artifact

        def crash_age(operation_id: str, name: str, data: bytes) -> str:
            if operation_id.startswith("ordinary:baseline-age:"):
                raise RuntimeError("age publication crash")
            return publish(operation_id, name, data)

        with patch.object(store, "finalize_artifact", side_effect=crash_age):
            with pytest.raises(RuntimeError, match="age publication"):
                intake.prepare("task", SUBJECT, declaration(), expected_parent=original.digest)
        records = store.operations("ordinary:baseline-age:")
        assert len(records) == 1 and records[0].attempts == 0
        assert store.find_artifact(records[0].operation_id, "record") is None
        with pytest.raises(OperationConflict, match="inert"):
            store.claim(records[0].operation_id, now=150, lease_seconds=5)
    scenario.time = scenario.baseline.observed_at = 151
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="expired"):
                intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert store.find_artifact(records[0].operation_id, "record") is not None
        assert intake.history("task") == (original,)


def test_retained_lower_age_is_idempotent_and_scoped_to_receipt_not_alias(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = 150
        scenario.baseline.observed_at = 50
        for _ in range(2):
            assert intake.prepare("task", SUBJECT, declaration(), expected_parent=original.digest) == original
        assert len(store.operations("ordinary:baseline-age:")) == 1
    scenario.time = scenario.baseline.observed_at = 151
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        alias = intake.prepare("draft", SUBJECT, declaration(context=["alias amendment"]), expected_parent=None)
        assert alias.evidence.observed_at == 50
        with pytest.raises(IntakeRefused, match="expired"):
            intake.approve(OWNER, alias, policy(), decision_id="alias", expires_at=400)
        scenario.baseline.reference = "fixture:genuinely-new-after-lower-age"
        fresh = intake.prepare("task", SUBJECT, declaration(), expected_parent=original.digest)
        assert fresh.evidence.observed_at == 151
        approved = intake.approve(OWNER, fresh, policy(), decision_id="fresh", expires_at=400)
        refusal = intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
        assert isinstance(refusal, TranslationRefusal)
        assert refusal.handoff.draft == fresh
        assert len(store.operations("ordinary:baseline-age:")) == 1
        assert intake.history("task")[0] == original


def test_legacy_consumption_collision_is_readable_without_rewriting_history(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "producer.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("draft", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        records = [
            (record.operation_id, store.artifact(record.operation_id, "record"))
            for record in store.operations("ordinary:")
        ]
    path = sqlite_tmp_path / "legacy.db"
    legacy_key = "ordinary:consumed:draft:1"
    legacy_raw = b""
    with SQLiteProgramStore(path) as store:
        for key, raw in records:
            if json.loads(raw)["kind"] == "ordinary-consumption":
                key = legacy_key
                legacy_raw = raw
            store.intent(key, key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()})
            store.finalize_artifact(key, "record", raw)
        store.create(approved.program.spec, "ordinary:approve:original")
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.history("draft") == (draft,)
        assert intake.history("consumed") == ()
        assert intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400) == approved
        consumed = intake.prepare("consumed", SUBJECT, declaration(), expected_parent=None)
        changed = intake.approve(OWNER, consumed, policy(), decision_id="consumed", expires_at=400)
        assert store.artifact(legacy_key, "record") == legacy_raw
        assert intake.history("consumed") == (consumed,)
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.approve(OWNER, consumed, policy(), decision_id="consumed", expires_at=400) == changed
        assert intake.history("draft") == (draft,)
        assert store.artifact(legacy_key, "record") == legacy_raw


@pytest.mark.parametrize("corruption", ["kind", "namespace", "task-subject", "extra-field"])
def test_malformed_draft_metadata_fails_without_rewriting(sqlite_tmp_path: Path, corruption: str) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "malformed.db"
    with SQLiteProgramStore(path) as store:
        evidence = scenario.reader.read(SUBJECT, declaration())
        value = Draft("task", 1, None, declaration(), evidence).payload()
        key = "ordinary:task:draft:1"
        if corruption == "kind":
            value["kind"] = "ordinary-consumption"
        elif corruption == "namespace":
            key = "ordinary:task:draft:2"
        elif corruption == "task-subject":
            value["task_id"] = "other"
        else:
            value["approval"] = "untrusted extra field"
        raw = encode(value)
        store.intent(key, key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()})
        store.finalize_artifact(key, "record", raw)
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        with pytest.raises(IntakeRefused):
            intake.history("task")
        assert store.artifact(key, "record") == raw


@pytest.mark.parametrize("corruption", ["version", "kind", "subject", "timestamp", "extra-field", "wrong-task"])
def test_malformed_age_and_consumption_bindings_refuse_recovery_without_rewrite(
    sqlite_tmp_path: Path, corruption: str
) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "malformed.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        original = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, original, policy(), decision_id="original", expires_at=400)
        scenario.time = 150
        scenario.baseline.observed_at = 50
        intake.prepare("task", SUBJECT, declaration(), expected_parent=original.digest)
        if corruption == "wrong-task":
            record = store.operations("ordinary:consumed:task:")[0]
            value = json.loads(store.artifact(record.operation_id, "record"))
            key = "ordinary:consumed:other:record:1"
        else:
            record = store.operations("ordinary:baseline-age:")[0]
            value = json.loads(store.artifact(record.operation_id, "record"))
            key = record.operation_id
            if corruption == "version":
                value["version"] = 2
            elif corruption == "kind":
                value["kind"] = "ordinary-draft"
            elif corruption == "subject":
                value["identity"]["subject"]["issue"] = 50
            elif corruption == "timestamp":
                value["observed_at"] = 60
            else:
                value["principal"] = "owner"
        raw = encode(value)
        # Rebuild only the synthetic journal into a fresh store; immutable rows are not patched.
        valid = [
            (item.operation_id, store.artifact(item.operation_id, "record"))
            for item in store.operations("ordinary:")
            if item.operation_id != key
        ]
    with SQLiteProgramStore(sqlite_tmp_path / "recovered.db") as store:
        for record_key, record_raw in [*valid, (key, raw)]:
            store.intent(
                record_key, record_key, {"kind": "ordinary-intake-record", "version": 1, "bytes": record_raw.decode()}
            )
            store.finalize_artifact(record_key, "record", record_raw)
        store.create(approved.program.spec, "ordinary:approve:original")
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused):
                if corruption == "wrong-task":
                    other = intake.prepare("other", SUBJECT, declaration(), expected_parent=None)
                    intake.approve(OWNER, other, policy(), decision_id="other", expires_at=400)
                else:
                    intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert store.artifact(key, "record") == raw


@pytest.mark.parametrize("action", ["approve", "handoff", "new-decision", "amendment"])
def test_owner_cancelled_program_refuses_replay_and_handoff_after_reopen(sqlite_tmp_path: Path, action: str) -> None:
    scenario = Scenario()
    path = sqlite_tmp_path / "intake.db"
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        original = {
            record.operation_id: store.artifact(record.operation_id, "record")
            for record in store.operations("ordinary:")
        }
        cancelled = store.admit(
            approved.program.program_id, "owner-cancel", CancelProgram(approved.program.revision, "owner")
        )
        assert cancelled.status is ProgramStatus.CANCELLED
        assert cancelled.spec == approved.program.spec
    with SQLiteProgramStore(path) as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        assert intake.history("task") == (draft,)
        assert intake.historical_decision("original") == approved.decision_bytes
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="cancelled"):
                if action == "handoff":
                    intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
                elif action == "amendment":
                    changed = intake.prepare(
                        "task", SUBJECT, declaration(context=["new inert proposal"]), expected_parent=draft.digest
                    )
                    intake.approve(OWNER, changed, policy(), decision_id="amended", expires_at=400)
                else:
                    intake.approve(
                        OWNER,
                        draft,
                        policy(),
                        decision_id="original" if action == "approve" else "new-decision",
                        expires_at=400,
                    )
            translator.assert_not_called()
        assert store.load(approved.program.program_id) == cancelled
        assert all(store.artifact(key, "record") == raw for key, raw in original.items())
        assert not store.operations("ordinary:decision:new-decision")
        assert not store.operations("ordinary:decision:amended")


@pytest.mark.parametrize("action", ["approve", "handoff"])
@pytest.mark.parametrize("cut", ["closing-evidence", "history-read", "final-clock"])
def test_cancellation_during_current_checks_refuses_stale_approved_copy(
    sqlite_tmp_path: Path, action: str, cut: str
) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        decision = intake.historical_decision("original")
        fired = False

        def cancel() -> None:
            nonlocal fired
            if not fired:
                program = store.load(approved.program.program_id)
                store.admit(program.program_id, "owner-cancel", CancelProgram(program.revision, "owner"))
                fired = True

        if cut == "closing-evidence":
            reads = 0

            def closing_issue(path: str) -> None:
                nonlocal reads
                if "/issues/" in path:
                    reads += 1
                    if reads == 2:
                        cancel()

            scenario.transport.callback = closing_issue
        elif cut == "history-read":
            operations = store.operations

            def history_read(prefix: str = ""):
                result = operations(prefix)
                if prefix == "ordinary:":
                    cancel()
                return result

            store.operations = history_read
        else:
            clocks = 0

            def final_clock() -> int:
                nonlocal clocks
                clocks += 1
                if clocks == (3 if action == "approve" else 2):
                    cancel()
                return scenario.time

            intake.clock = final_clock
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="cancelled"):
                if action == "approve":
                    intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
                else:
                    intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert fired
        assert approved.program.status is ProgramStatus.DRAFT
        assert store.load(approved.program.program_id).status is ProgramStatus.CANCELLED
        assert intake.historical_decision("original") == decision
        assert intake.history("task") == (draft,)


@pytest.mark.parametrize("action", ["replay", "amendment"])
def test_cancellation_during_decision_recording_prevents_return_or_amendment(
    sqlite_tmp_path: Path, action: str
) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        decision_id = "original"
        if action == "amendment":
            draft = intake.prepare("task", SUBJECT, declaration(context=["amended"]), expected_parent=draft.digest)
            decision_id = "amended"
        intent = store.intent
        fired = False

        def cancel_after_record(operation_id: str, effect_key: str, request: dict[str, object]):
            nonlocal fired
            result = intent(operation_id, effect_key, request)
            if operation_id == f"ordinary:decision:{decision_id}" and not fired:
                program = store.load(approved.program.program_id)
                store.admit(program.program_id, "owner-cancel", CancelProgram(program.revision, "owner"))
                fired = True
            return result

        with patch.object(store, "intent", side_effect=cancel_after_record):
            with pytest.raises(IntakeRefused, match="cancelled"):
                intake.approve(OWNER, draft, policy(), decision_id=decision_id, expires_at=400)
        assert fired
        program = store.load(approved.program.program_id)
        assert program.status is ProgramStatus.CANCELLED and program.spec == approved.program.spec
        assert intake.historical_decision("original") == approved.decision_bytes


@pytest.mark.parametrize(
    "status", [ProgramStatus.DRAFT, ProgramStatus.ACTIVE, ProgramStatus.PAUSED, ProgramStatus.COMPLETED]
)
def test_nonabandoned_core_states_retain_replay_and_explicit_amendment_compatibility(
    sqlite_tmp_path: Path, status: ProgramStatus
) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        program = approved.program
        if status is not ProgramStatus.DRAFT:
            program = store.admit(program.program_id, "owner-activate", ActivateProgram(program.revision, "owner"))
        if status is ProgramStatus.PAUSED:
            program = store.admit(program.program_id, "owner-pause", PauseProgram(program.revision, "owner"))
        elif status is ProgramStatus.COMPLETED:
            fixture = finished(program, "ordinary")
            attempt = fixture.attempts[0]
            program = store.admit(
                program.program_id, "fixture-prepare", PrepareAttempt(program.revision, "owner", attempt.spec)
            )
            program = store.admit(
                program.program_id, "fixture-start", StartAttempt(program.revision, "owner", attempt.attempt_id)
            )
            program = store.admit(
                program.program_id, "fixture-finish", FinishAttempt(program.revision, "owner", attempt.attempt_id)
            )
            fact = satisfaction(program, "ordinary", "s1", attempt.attempt_id)
            program = store.admit(
                program.program_id, "fixture-satisfy", SatisfyWorkUnit(program.revision, fact.issuer_id, fact)
            )
        assert program.status is status
        replay = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        assert replay.program == program
        refusal = intake.handoff(OWNER, replay, policy(), ScarcityRouterAllocator)
        assert isinstance(refusal, TranslationRefusal)
        assert refusal.handoff.draft == draft
        amended = intake.prepare(
            "task", SUBJECT, declaration(context=["explicit owner amendment"]), expected_parent=draft.digest
        )
        current = intake.approve(OWNER, amended, policy(), decision_id="amended", expires_at=400)
        assert current.program.spec.revision == 2
        assert current.program.status is (ProgramStatus.ACTIVE if status is ProgramStatus.COMPLETED else status)


def test_cancellation_after_core_create_is_not_returned_as_new_approval(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        create = store.create

        def cancel_after_create(spec: ProgramSpec, command_key: str):
            program = create(spec, command_key)
            store.admit(program.program_id, "owner-cancel", CancelProgram(program.revision, "owner"))
            return program

        with patch.object(store, "create", side_effect=cancel_after_create):
            with pytest.raises(IntakeRefused, match="cancelled"):
                intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        program = store.load("ordinary:task")
        assert program.status is ProgramStatus.CANCELLED
        assert program.spec.revision == 1 and not program.attempts
        assert intake.historical_decision("original")
        with pytest.raises(IntakeRefused, match="cancelled"):
            intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("failed_read", ["closing-issue", "closing-branch", "second-scan"])
def test_late_permission_loss_retains_failed_presence_and_refuses_actual_handoff(
    sqlite_tmp_path: Path, status: int, failed_read: str
) -> None:
    scenario = Scenario()
    scenario.transport.add_pr(head="c" * 40)
    scenario.content.results["c" * 40] = False
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)

        def install_loss() -> None:
            scenario.transport.issue_status = scenario.transport.branch_status = scenario.transport.scan_status = 200
            counts = {"issue": 0, "branch": 0, "scan": 0}

            def lose_permission(path: str) -> None:
                if "/issues/" in path:
                    counts["issue"] += 1
                    if failed_read == "closing-issue" and counts["issue"] == 2:
                        scenario.transport.issue_status = status
                elif "/branches/" in path:
                    counts["branch"] += 1
                    if failed_read == "closing-branch" and counts["branch"] == 2:
                        scenario.transport.branch_status = status
                elif "/pulls?" in path and "page=1" in path:
                    counts["scan"] += 1
                    if failed_read == "second-scan" and counts["scan"] == 2:
                        scenario.transport.scan_status = status

            scenario.transport.callback = lose_permission

        install_loss()
        observed = scenario.reader.read(SUBJECT, declaration())
        assert observed.disposition is Disposition.PERMISSION
        proof = json.loads(observed.relevance_proof)
        if failed_read == "second-scan":
            failed = next(item for item in proof if item.get("scan") == "closing")
            assert failed["failure"] == Disposition.PERMISSION.value and failed["presence"] == "inaccessible"
        else:
            reads = next(item["reads"] for item in proof if "reads" in item)
            assert reads["issue" if failed_read == "closing-issue" else "branch"]["closing"] == "inaccessible"
        cut = next(item for item in proof if "reads" in item)
        assert cut["issue_changed"] is False and cut["branch_changed"] is False
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            install_loss()
            with pytest.raises(IntakeRefused):
                intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
            install_loss()
            with pytest.raises(IntakeRefused):
                intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert scenario.transport.pushes == scenario.transport.posts == 0


@pytest.mark.parametrize("permission_read", ["issue", "scan"])
def test_confirmed_branch_change_and_permission_loss_are_both_retained(permission_read: str) -> None:
    scenario = Scenario()
    scenario.transport.add_pr(head="c" * 40)
    scenario.content.results["c" * 40] = False
    counts = {"issue": 0, "branch": 0, "scan": 0}

    def change_and_lose(path: str) -> None:
        if "/issues/" in path:
            counts["issue"] += 1
            if permission_read == "issue" and counts["issue"] == 2:
                scenario.transport.issue_status = 403
        elif "/branches/" in path:
            counts["branch"] += 1
            if counts["branch"] == 2:
                scenario.transport.branches["develop"] = "c" * 40
        elif "/pulls?" in path and "page=1" in path:
            counts["scan"] += 1
            if permission_read == "scan" and counts["scan"] == 2:
                scenario.transport.scan_status = 403

    scenario.transport.callback = change_and_lose
    observed = scenario.reader.read(SUBJECT, declaration())
    assert observed.disposition is Disposition.PERMISSION
    cut = next(item for item in json.loads(observed.relevance_proof) if "reads" in item)
    assert cut["branch_changed"] is True and cut["issue_changed"] is False
    assert cut["branch_initial_revision"] == f"forgejo:{SUBJECT.base}"
    assert cut["branch_closing_revision"] == "forgejo:" + "c" * 40


@pytest.mark.parametrize("phase", ["initial", "closing"])
def test_pr_permission_failure_is_not_masked_by_another_unknown_read(phase: str) -> None:
    scenario = Scenario()
    scenario.transport.add_pr(head="c" * 40)
    scenario.transport.add_pr(head="c" * 40)
    scenario.content.results["c" * 40] = False
    reads = 0

    def fail_pr_reads(path: str) -> None:
        nonlocal reads
        if path.endswith("/pulls/1"):
            reads += 1
            if reads == (1 if phase == "initial" else 2):
                scenario.transport.pull_status = {"1": 403, "2": 500}

    scenario.transport.callback = fail_pr_reads
    observed = scenario.reader.read(SUBJECT, declaration())
    assert observed.disposition is Disposition.PERMISSION
    proof = json.loads(observed.relevance_proof)
    failed = next(
        item
        for item in proof
        if item.get("change_read") == phase and item.get("requested_reference") == "forgejo:team/project#1"
    )
    unknown = next(
        item
        for item in proof
        if item.get("change_read") == phase and item.get("requested_reference") == "forgejo:team/project#2"
    )
    assert failed["presence"] == "inaccessible" and unknown["presence"] == "unknown"


@pytest.mark.parametrize("read", ["issue", "branch"])
def test_unknown_closing_read_is_incomplete_not_a_claimed_source_change(read: str) -> None:
    scenario = Scenario()
    reads = 0

    def fail_closing(path: str) -> None:
        nonlocal reads
        if (read == "issue" and "/issues/" in path) or (read == "branch" and "/branches/" in path):
            reads += 1
            if reads == 2:
                if read == "issue":
                    scenario.transport.issue_status = 500
                else:
                    scenario.transport.branch_status = 500

    scenario.transport.callback = fail_closing
    observed = scenario.reader.read(SUBJECT, declaration())
    assert observed.disposition is Disposition.INCOMPLETE
    cut = next(item for item in json.loads(observed.relevance_proof) if "reads" in item)
    assert cut["reads"][read]["closing"] == "unknown"
    assert cut["issue_changed"] is False and cut["branch_changed"] is False


def test_permission_provenance_contains_no_provider_response_body() -> None:
    scenario = Scenario()
    scenario.transport.issue["body"] = "synthetic private response marker, not provenance"
    scenario.transport.issue_status = 403
    observed = scenario.reader.read(SUBJECT, declaration())
    assert observed.disposition is Disposition.PERMISSION
    assert b"synthetic private response marker" not in observed.relevance_proof
    cut = next(item for item in json.loads(observed.relevance_proof) if "reads" in item)
    assert cut["reads"]["issue"] == {"initial": "inaccessible", "closing": "inaccessible"}


@pytest.mark.parametrize("action", ["approve", "handoff"])
@pytest.mark.parametrize("limit", ["expiry", "freshness"])
def test_final_program_load_crossing_temporal_limit_refuses_delivery(
    sqlite_tmp_path: Path, action: str, limit: str
) -> None:
    scenario = Scenario()
    current_policy = policy() if limit == "expiry" else replace(policy(), freshness_seconds=99)
    expires_at = 200 if limit == "expiry" else 400
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, current_policy, decision_id="original", expires_at=expires_at)
        decision = intake.historical_decision("original")
        scenario.time = 199
        load = store.load
        loads = 0
        crossed = False

        def cross_at_final_load(program_id: str):
            nonlocal loads, crossed
            program = load(program_id)
            loads += 1
            if loads == (5 if action == "approve" else 4):
                scenario.time = 200
                crossed = True
            return program

        with (
            patch.object(store, "load", side_effect=cross_at_final_load),
            patch.object(
                ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
            ) as translator,
        ):
            with pytest.raises(IntakeRefused, match="expired"):
                if action == "approve":
                    intake.approve(OWNER, draft, current_policy, decision_id="original", expires_at=expires_at)
                else:
                    intake.handoff(OWNER, approved, current_policy, ScarcityRouterAllocator)
            translator.assert_not_called()
        assert crossed
        assert intake.history("task") == (draft,)
        assert intake.historical_decision("original") == decision
        assert store.load(approved.program.program_id) == approved.program


@pytest.mark.parametrize("action", ["approve", "handoff"])
@pytest.mark.parametrize("cut", ["last-load", "final-clock"])
@pytest.mark.parametrize("change", ["cancel", "spec"])
def test_mutation_during_final_load_or_clock_cannot_deliver_stale_program(
    sqlite_tmp_path: Path, action: str, cut: str, change: str
) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        load = store.load
        loads = 0
        armed = False
        fired = False

        def mutate() -> None:
            nonlocal fired
            program = load(approved.program.program_id)
            if change == "cancel":
                command = CancelProgram(program.revision, "owner")
            else:
                command = AmendProgramSpec(
                    program.revision,
                    "owner",
                    SpecAmendment(program.spec.revision, objective="changed in final callback"),
                )
            store.admit(program.program_id, "owner-final-change", command)
            fired = True

        def final_load(program_id: str):
            nonlocal loads, armed
            program = load(program_id)
            loads += 1
            if loads == (5 if action == "approve" else 4):
                if cut == "last-load":
                    mutate()
                else:
                    armed = True
            return program

        def final_clock() -> int:
            if armed and not fired:
                mutate()
            return scenario.time

        intake.clock = final_clock
        with (
            patch.object(store, "load", side_effect=final_load),
            patch.object(
                ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
            ) as translator,
        ):
            with pytest.raises(IntakeRefused):
                if action == "approve":
                    intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
                else:
                    intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert fired
        current = store.load(approved.program.program_id)
        assert current.revision > approved.program.revision
        if change == "cancel":
            assert current.status is ProgramStatus.CANCELLED and current.spec == approved.program.spec
        else:
            assert current.spec.digest != approved.program.spec.digest
        assert intake.historical_decision("original") == approved.decision_bytes


@pytest.mark.parametrize("action", ["approve", "handoff"])
def test_final_temporal_check_has_no_following_privileged_reads(sqlite_tmp_path: Path, action: str) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        trace: list[str] = []

        def clock() -> int:
            trace.append("clock")
            return scenario.time

        intake.clock = clock
        store._connection.set_trace_callback(lambda _: trace.append("sql"))  # pyright: ignore[reportPrivateUsage]
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            if action == "approve":
                assert intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400) == approved
            else:
                refusal = intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
                assert isinstance(refusal, TranslationRefusal)
                assert refusal.handoff.draft == draft
                translator.assert_called_once()
        last_clock = len(trace) - 1 - trace[::-1].index("clock")
        assert "sql" in trace[:last_clock]
        assert trace[last_clock:] == ["clock"]
        store._connection.set_trace_callback(None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("where", ["read", "clock"])
def test_local_read_cut_detects_callback_writes_without_post_clock_queries(sqlite_tmp_path: Path, where: str) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        trace: list[str] = []

        def mutate() -> None:
            # Even a journal-only write invalidates this cut; no new tracking table.
            store.intent("synthetic:callback", "synthetic:callback", {"fixture": "write"})

        def read(program: Program) -> str:
            assert program == approved.program
            if where == "read":
                mutate()
            return "received"

        def clock() -> int:
            if where == "clock":
                mutate()
            trace.append("clock-return")
            return scenario.time

        store._connection.set_trace_callback(lambda _: trace.append("sql"))  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(ProgramReadChanged, match="changed"):
            store.program_read_cut(approved.program.program_id, read, clock)
        assert trace[-1] == "clock-return"
        store._connection.set_trace_callback(None)  # pyright: ignore[reportPrivateUsage]


def test_final_cut_rechecks_latest_draft_after_earlier_clock_amendment(sqlite_tmp_path: Path) -> None:
    scenario = Scenario()
    with SQLiteProgramStore(sqlite_tmp_path / "intake.db") as store:
        intake = OrdinaryIntake(store, scenario.reader, lambda: scenario.time)
        draft = intake.prepare("task", SUBJECT, declaration(), expected_parent=None)
        approved = intake.approve(OWNER, draft, policy(), decision_id="original", expires_at=400)
        clocks = 0
        changed = False

        def clock() -> int:
            nonlocal clocks, changed
            clocks += 1
            if clocks == 2:
                intake.prepare(
                    "task", SUBJECT, declaration(context=["new proposal in callback"]), expected_parent=draft.digest
                )
                changed = True
            return scenario.time

        intake.clock = clock
        with patch.object(
            ScarcityRouterAllocator, "translate_ordinary", wraps=ScarcityRouterAllocator.translate_ordinary
        ) as translator:
            with pytest.raises(IntakeRefused, match="revision"):
                intake.handoff(OWNER, approved, policy(), ScarcityRouterAllocator)
            translator.assert_not_called()
        assert changed and len(intake.history("task")) == 2
        assert store.load(approved.program.program_id) == approved.program
