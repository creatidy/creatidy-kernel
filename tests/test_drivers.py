# SPDX-License-Identifier: Apache-2.0
"""Provider-shaped transport and shared application conformance, no live accounts."""

import copy
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit, urlunsplit

import native_kilo_fixture
import pytest
from native_kilo_fixture import review_peer
from ordinary_flow_fixture import CODEX_PEER
from test_intake import SUBJECT, Scenario, Transport, declaration
from test_intake import policy as intake_policy
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path

import creatidy_kernel.adapters.ordinary_workspace as ordinary_workspace_module
import creatidy_kernel.adapters.task_execution as task_execution_module
from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.driver_http import object_value
from creatidy_kernel.adapters.forge_refs import ForgeBinding
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_discovery import ForgejoDiscovery
from creatidy_kernel.adapters.forgejo_drivers import ForgejoCI
from creatidy_kernel.adapters.intake import GitContentSource
from creatidy_kernel.adapters.kilo_review import NativeKiloReview
from creatidy_kernel.adapters.ordinary_managed import OrdinaryManagedRun
from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.project_drivers import (
    GitHubSCM,
    JenkinsCI,
    JiraWorkManagement,
    TrustedSCMRemote,
    binding_from_mapping,
)
from creatidy_kernel.adapters.qualified_baseline import JenkinsQualifiedBaseline
from creatidy_kernel.adapters.qualified_readiness import GitHubJiraLiteralReadiness
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.source_cache import source_use
from creatidy_kernel.adapters.sqlite_store import (
    IdempotencyConflict,
    OperationConflict,
    SQLiteProgramStore,
    validate_local_storage,
)
from creatidy_kernel.adapters.task_execution import VerificationCommand
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.domain import AuthorityEnvelope, BudgetPolicy, CancelProgram, PauseProgram, ResumeProgram
from creatidy_kernel.core.drivers import (
    DriverDelivery,
    DriverFact,
    DriverMutation,
    DriverSubject,
    EdgeObligation,
    WorkObservation,
    driver_bytes,
    driver_digest,
)
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import EffectStatus, Presence, UnsupportedForge
from creatidy_kernel.core.intake import ContentCriterion, Declaration, IntakeRefused, digest, encode
from creatidy_kernel.core.ordinary_qualified import (
    QualifiedDependencyCut,
    QualifiedDraft,
)
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, WorkClaim
from creatidy_kernel.core.verification import VerificationPolicy
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.drivers import DriverApplication, DriverDraftGrant, DriverEffectGrant
from creatidy_kernel.ports.intake import OrdinaryIntake
from creatidy_kernel.ports.ordinary import DependencyReader, OrdinaryProgram, OrdinaryReviewer, OrdinaryStore
from creatidy_kernel.ports.ownership import WorkOwnership
from creatidy_kernel.ports.program_store import OperationRecord
from tools.check_drivers import (
    BASE,
    HEAD,
    REMOTE,
    Clock,
    SyntheticServices,
    accepted,
    binding,
    configured,
    conformance,
    effect_grant,
    settlement_recovery,
)


@pytest.fixture
def services() -> Iterator[SyntheticServices]:
    service = SyntheticServices()
    try:
        yield service
    finally:
        service.close()


def test_qualified_cut_owner_approval_and_recovery(services: SyntheticServices, sqlite_tmp_path: Path) -> None:
    clock = Clock()
    project, _, _, _ = configured(services, clock)
    work = project.work.work("ENG-42")
    classifications = tuple(sorted((edge.identity, False) for edge in work.dependencies))
    obligation = EdgeObligation(
        work.fact.subject, driver_digest([asdict(edge) for edge in work.dependencies]), "decision", 120
    )
    cut = QualifiedDependencyCut.capture(work, classifications, 100, obligation=obligation)
    budget = WorkBudgetPolicy.basic(2, 1, verifications=2, retained_bytes=1024)
    policy = replace(intake_policy(), work_budget=budget, project_budget=budget, project_id="project")
    scenario = Scenario()

    class DependenciesTransport(Transport):
        def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
            assert method == "GET" and "/issues/49/dependencies?" in path and body is None
            return 200, []

    dependencies = ForgejoDiscovery(DependenciesTransport(), clock.wall, max_pages=2, freshness_seconds=30)

    def application(store: SQLiteProgramStore, *, configured_work: bool = True) -> OrdinaryProgram:
        return OrdinaryProgram(
            store,
            OrdinaryIntake(store, scenario.reader, clock.wall),
            dependencies,
            Principal("owner", "owner"),
            policy,
            VerificationPolicy(policy.reference, ("paths", "structural", "commands")),
            clock.wall,
            execution_profile=encode(
                {
                    "harness": "kilo-pull",
                    "provider": None,
                    "model": None,
                    "effort": None,
                    "trust": "trusted_development",
                }
            ),
            qualified_work=(project.work,) if configured_work else (),
        )

    path = sqlite_tmp_path / "qualified.db"
    with SQLiteProgramStore(path, boot_clock=clock.boot) as store:
        app = application(store)
        unresolved = QualifiedDependencyCut.capture(
            work, tuple(sorted((edge.identity, True) for edge in work.dependencies)), 100, obligation=obligation
        )
        with pytest.raises(IntakeRefused, match="cannot yet be mapped"):
            app.propose("qualified", "deliver", (("one", SUBJECT, declaration()),), qualified_cuts=(unresolved,))
        proposal = app.propose("qualified", "deliver", (("one", SUBJECT, declaration()),), qualified_cuts=(cut,))
        assert proposal.payload["version"] == 2
        assert app.approval_scope(proposal)["proposal"] == proposal.payload
        assert app.approval_scope(proposal)["qualified_work_bindings"] == [asdict(project.work.binding)]
        with pytest.raises(IntakeRefused, match="exact owner decision"):
            app.approve(proposal, decision_id="different", expires_at=120)
        guards_rejected: list[int] = []

        def reject_inside_transaction() -> None:
            guards_rejected.append(1)
            if len(guards_rejected) == 2:
                raise IntakeRefused("reviewed scope changed")

        with pytest.raises(IntakeRefused, match="reviewed scope changed"):
            app.approve(proposal, decision_id="decision", expires_at=120, final_guard=reject_inside_transaction)
        assert store.find_program("qualified") is None
        guards: list[bool] = []
        approved = app.approve(
            proposal, decision_id="decision", expires_at=120, final_guard=lambda: guards.append(True)
        )
        assert guards and approved.proposal == proposal
        assert app.recover("decision").proposal == proposal
        app.check(approved)

    with SQLiteProgramStore(path, boot_clock=clock.boot) as store:
        app = application(store)
        recovered = app.recover("decision")
        assert recovered.proposal == proposal
        app.check(recovered)
        services.filter = lambda _method, _path, _body: (403, {})
        with pytest.raises(ValueError):
            app.check(recovered)
        services.filter = None
        unavailable = application(store, configured_work=False)
        assert unavailable.recover("decision").proposal == proposal
        with pytest.raises(IntakeRefused, match="driver unavailable"):
            unavailable.check(recovered)
        app.stop(recovered, "owner revoked")
        with pytest.raises(ValueError):
            app.check(recovered)


def test_jenkins_qualified_baseline_requires_exact_stable_native_evidence(services: SyntheticServices) -> None:
    clock = Clock()
    project, _, _, _ = configured(services, clock)
    source = project.scm.ref("feature").subject
    recipe = digest(encode(("controller:exact-content:v1",)))
    reader = JenkinsQualifiedBaseline(
        project.ci, DriverSubject(project.ci.binding, "build", "12", None), ("controller:exact-content:v1",), clock.wall
    )
    first = reader.read_baseline(source, recipe)
    assert first is not None and first.result == "passed" and first.observed_at == 1
    proof = json.loads(first.proof)
    assert DriverSubject.parse(proof["build"]) == DriverSubject(project.ci.binding, "build", "12", None)
    assert DriverSubject.parse(proof["source"]).revision == source.revision
    assert proof["recipes"] == ["controller:exact-content:v1"]
    assert json.loads(bytes.fromhex(proof["native"]))["result"] == "SUCCESS"
    with pytest.raises(ValueError):
        replace(first, proof=b"altered")
    clock.now = 105
    assert reader.read_baseline(source, recipe) == first
    assert reader.read_baseline(source, digest(encode(("other",)))) is None
    other_source = replace(source, revision="f" * 40)
    assert reader.read_baseline(other_source, recipe) is None

    current: dict[str, object] = {"mode": "failure", "reads": 0}

    def response(method: str, path: str, body: object) -> tuple[int, object] | None:
        if not path.startswith("/job/service/job/feature/12/api/json"):
            return None
        status, value = services.route(method, path, body)
        data = cast(dict[str, object], value)
        mode = current["mode"]
        if mode == "failure":
            data["result"] = "FAILURE"
        elif mode == "pending":
            data["building"], data["result"] = True, None
        elif mode == "missing-age":
            data.pop("timestamp")
        elif mode == "future-age":
            data["timestamp"] = 200_000
        elif mode == "moving":
            current["reads"] = cast(int, current["reads"]) + 1
            data["timestamp"] = 1000 + current["reads"] * 1000
        return status, data

    services.filter = response
    failed = reader.read_baseline(source, recipe)
    assert failed is not None and failed.result == "failed" and failed.observed_at == 1
    for state in ("pending", "missing-age", "future-age", "moving"):
        current["mode"] = state
        assert reader.read_baseline(source, recipe) is None


def test_qualified_readiness_rejects_incomplete_moving_or_equivalent_inventory(
    services: SyntheticServices, tmp_path: Path
) -> None:
    clock = Clock()
    project, scm, _, _ = configured(services, clock)
    repository = tmp_path / "readiness-source"
    repository.mkdir()
    reference_git(repository, "init", "--initial-branch=feature")
    (repository / "result.txt").write_text("original\n")
    reference_git(repository, "add", "result.txt")
    reference_git(repository, "commit", "-m", "Synthetic original")
    original = reference_git(repository, "rev-parse", "HEAD").strip()
    reference_git(repository, "checkout", "-b", "candidate")
    (repository / "result.txt").write_text("target\n")
    reference_git(repository, "add", "result.txt")
    reference_git(repository, "commit", "-m", "Synthetic active change")
    target = reference_git(repository, "rev-parse", "HEAD").strip()
    reference_git(repository, "checkout", "feature")
    reference_git(repository, "remote", "add", "origin", scm.binding.origin + "/" + scm.binding.namespace + ".git")
    services.head = original
    work = project.work.work("ENG-42")
    obligation = EdgeObligation(
        work.fact.subject, driver_digest([asdict(edge) for edge in work.dependencies]), "owner:scope", 120
    )
    cut = QualifiedDependencyCut.capture(
        work, tuple(sorted((edge.identity, False) for edge in work.dependencies)), 100, obligation=obligation
    )
    source = scm.ref("feature").subject
    criterion = ContentCriterion("result.txt", b"target\n")
    declared = declaration(criteria=[criterion.criterion])
    state: dict[str, object] = {"mode": "empty", "scans": 0}

    def response(method: str, path: str, body: object) -> tuple[int, object] | None:
        name, query = urlsplit(path).path, parse_qs(urlsplit(path).query)
        if state["mode"] == "moving-ref" and name == "/repos/acme/service/git/ref/heads/feature":
            status, value = services.route(method, path, body)
            data = cast(dict[str, object], value)
            state["refs"] = cast(int, state.get("refs", 0)) + 1
            if state["refs"] == 2:
                cast(dict[str, object], data["object"])["sha"] = "f" * 40
            return status, data
        if state["mode"] == "moving-work" and name == "/rest/api/3/issue/ENG-42":
            status, value = services.route(method, path, body)
            data = cast(dict[str, object], value)
            state["work_reads"] = cast(int, state.get("work_reads", 0)) + 1
            if state["work_reads"] == 2:
                cast(dict[str, object], data["fields"])["updated"] = "2026-11-01T00:00:00.000+0000"
            return status, data
        if name not in {"/repos/acme/service/pulls", "/repos/acme/service/pulls/7"}:
            return None
        mode = state["mode"]
        head = target if mode in {"active", "out-of-base-active"} else "f" * 40 if mode == "missing-head" else original
        pull = services.pull()
        cast(dict[str, object], pull["head"])["sha"] = head
        cast(dict[str, object], pull["base"])["sha"] = "e" * 40 if mode == "different-base" else original
        cast(dict[str, object], pull["base"])["ref"] = "develop" if mode == "out-of-base-active" else "feature"
        if mode == "foreign":
            cast(dict[str, object], cast(dict[str, object], pull["head"])["repo"])["full_name"] = "other/repo"
        if name.endswith("/7"):
            return 200, pull
        if mode == "denied":
            return 403, {"message": "denied"}
        if mode == "malformed":
            return 200, {"items": []}
        if query["page"] != ["1"]:
            return 200, []
        state["scans"] = cast(int, state["scans"]) + 1
        if mode == "empty" or (mode == "moving" and state["scans"] == 1):
            return 200, []
        return 200, [pull, pull] if mode == "duplicate" else [pull]

    services.filter = response

    def reader(max_pages: int = 3) -> GitHubJiraLiteralReadiness:
        return GitHubJiraLiteralReadiness(
            project.work,
            scm,
            repository,
            ((work.fact.subject, (criterion,)),),
            frozenset({"3"}),
            (),
            Principal("owner", "owner"),
            "owner:fixture-complete-repository-visibility",
            clock.wall,
            max_pages=max_pages,
        )

    positive = reader().read_readiness(cut, source, declared)
    assert positive is not None
    positive_proof = json.loads(positive.proof)
    assert positive_proof["inventory_assurance"] == "two-stable-bounded-scans-not-atomic-snapshot"
    assert positive_proof["visibility_attestation"] == "owner:fixture-complete-repository-visibility"
    assert positive_proof["inventory_scope"]["scope"] == "all-open-changes-in-repository"
    assert positive_proof["inventory_scope"]["equivalence"] == "host-configured-exact-file-content-only"
    for mode in (
        "denied",
        "malformed",
        "duplicate",
        "moving",
        "moving-ref",
        "moving-work",
        "foreign",
        "missing-head",
        "active",
        "out-of-base-active",
        "different-base",
    ):
        state.update(mode=mode, scans=0)
        assert reader().read_readiness(cut, source, declared) is None
    state.update(mode="non-equivalent", scans=0)
    assert reader().read_readiness(cut, source, declared) is not None
    assert reader(max_pages=1).read_readiness(cut, source, declared) is None
    already = ContentCriterion("result.txt", b"original\n")
    already_reader = GitHubJiraLiteralReadiness(
        project.work,
        scm,
        repository,
        ((work.fact.subject, (already,)),),
        frozenset({"3"}),
        (),
        Principal("owner", "owner"),
        "owner:fixture-complete-repository-visibility",
        clock.wall,
        max_pages=3,
    )
    assert already_reader.read_readiness(cut, source, declaration(criteria=[already.criterion])) is None
    with pytest.raises(ValueError, match="host readiness configuration"):
        GitHubJiraLiteralReadiness(
            project.work,
            scm,
            repository,
            ((work.fact.subject, (criterion,)),),
            frozenset({"3"}),
            (),
            Principal("owner", "owner"),
            "",
            clock.wall,
            max_pages=3,
        )
    state.update(mode="empty", scans=0)
    scm.http.headers["X-GitHub-Api-Version"] = "2022-11-28"
    assert reader().read_readiness(cut, source, declared) is None
    scm.http.headers["X-GitHub-Api-Version"] = "2026-03-10"
    original_request = scm.http.request

    def contradictory_page(
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> tuple[int, object, dict[str, str]]:
        if urlsplit(path).path == "/repos/acme/service/pulls":
            return 200, [], {"link": '<https://github.example.test/next>; rel="next"'}
        return original_request(method, path, body, before_send=before_send)

    with patch.object(scm.http, "request", contradictory_page):
        assert reader().read_readiness(cut, source, declared) is None


@pytest.mark.parametrize(
    "case",
    ["advance-jira", "advance-scm", "advance-context", "expire-cut", "expire-work", "expire-source", "expire-context"],
)
def test_qualified_readiness_uses_current_time_without_renewing_evidence(
    services: SyntheticServices, tmp_path: Path, case: str
) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    jira = cast(JiraWorkManagement, project.work)
    jira.ttl = 200
    scm.ttl = 2 if case == "expire-source" else 30 if case.startswith("advance-") else 200
    knowledge.ttl = 2 if case == "expire-context" else 30 if case.startswith("advance-") else 200
    repository = tmp_path / "timed-source"
    repository.mkdir()
    reference_git(repository, "init", "--initial-branch=feature")
    (repository / "result.txt").write_text("original\n")
    reference_git(repository, "add", "result.txt")
    reference_git(repository, "commit", "-m", "Synthetic original")
    services.head = reference_git(repository, "rev-parse", "HEAD").strip()
    reference_git(repository, "remote", "add", "origin", scm.binding.origin + "/" + scm.binding.namespace + ".git")
    services.pages[18] = services.page(18, "Earlier", "<p>earlier</p>", "fixture")

    work = project.work.work("ENG-42")
    cut_deadline = 102 if case == "expire-cut" else 200
    obligation = EdgeObligation(
        work.fact.subject, driver_digest([asdict(edge) for edge in work.dependencies]), "owner:scope", cut_deadline
    )
    cut = QualifiedDependencyCut.capture(
        work, tuple(sorted((edge.identity, False) for edge in work.dependencies)), 100, obligation=obligation
    )
    source = scm.ref("feature").subject
    if case == "expire-work":
        jira.ttl = 2
    criterion = ContentCriterion("result.txt", b"target\n")
    declared = declaration(criteria=[criterion.criterion])
    readiness = GitHubJiraLiteralReadiness(
        project.work,
        scm,
        repository,
        ((work.fact.subject, (criterion,)),),
        frozenset({"3"}),
        ((knowledge, "900", "19"), (knowledge, "900", "18")),
        Principal("owner", "owner"),
        "owner:fixture-complete-repository-visibility",
        clock.wall,
        max_pages=3,
    )

    def inventory(_method: str, path: str, _body: object) -> tuple[int, object] | None:
        return (200, []) if urlsplit(path).path == "/repos/acme/service/pulls" else None

    services.filter = inventory
    advanced = [False]
    trigger = {
        "advance-jira": "/rest/api/3/issue/ENG-42",
        "advance-scm": "/repos/acme/service/git/ref/heads/feature",
        "advance-context": "/wiki/api/v2/pages/900",
    }.get(case, "/repos/acme/service/pulls")

    def advance(_method: str, path: str) -> None:
        if not advanced[0] and path == trigger:
            advanced[0] = True
            clock.now = 101 if case.startswith("advance-") else 103

    services.client_after = advance
    work_reads: list[WorkObservation] = []
    source_reads: list[DriverFact] = []
    context_reads: list[DriverFact] = []
    original_work, original_ref, original_content = project.work.work, scm.ref, knowledge.content

    def read_work(key: str) -> WorkObservation:
        result = original_work(key)
        work_reads.append(result)
        return result

    def read_ref(key: str) -> DriverFact:
        result = original_ref(key)
        source_reads.append(result)
        return result

    def read_content(key: str, revision: str) -> DriverFact:
        result = original_content(key, revision)
        context_reads.append(result)
        return result

    with (
        patch.object(project.work, "work", side_effect=read_work),
        patch.object(scm, "ref", side_effect=read_ref),
        patch.object(knowledge, "content", side_effect=read_content),
    ):
        result = readiness.read_readiness(cut, source, declared)

    assert advanced[0] and len(work_reads) == len(source_reads) == 2 and len(context_reads) == 4
    if case.startswith("advance-"):
        assert result is not None and result.observed_at == 100 and cut.fresh_until == 200
        if case == "advance-jira":
            assert source_reads[0].observed_at == 101 and source_reads[0].fresh_until == result.fresh_until == 131
        elif case == "advance-scm":
            assert source_reads[0].observed_at == 100 and context_reads[0].observed_at == 101
            assert context_reads[0].fresh_until == 131 and result.fresh_until == 130
        else:
            assert context_reads[0].observed_at == 100 and context_reads[1].observed_at == 101
            assert context_reads[1].fresh_until == 131 and result.fresh_until == 130
    else:
        assert result is None and clock.now == 103
        if case == "expire-cut":
            assert cut.fresh_until == 102 and work_reads[0].fact.fresh_until > clock.now
        elif case == "expire-work":
            assert work_reads[0].fact.fresh_until == 102 and cut.fresh_until == 200
        elif case == "expire-source":
            assert source_reads[0].fresh_until == 102 and source_reads[1].observed_at == 103
        else:
            assert context_reads[0].fresh_until == 102 and context_reads[2].observed_at == 103


@pytest.mark.parametrize(
    "mode",
    [
        "completion",
        "path-gate",
        "semantic-gate",
        "stop",
        "managed-uncertain",
        "preparation-race",
        "baseline-failed",
        "baseline-unavailable",
        "owner-mismatch",
    ],
)
def test_qualified_work_dag_owner_approval_and_reopen(
    services: SyntheticServices, sqlite_tmp_path: Path, tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    now = clock.wall()
    deterministic_time = SimpleNamespace(time=clock.wall, monotonic=time.monotonic, sleep=time.sleep)
    monkeypatch.setattr(ordinary_workspace_module, "time", deterministic_time)
    monkeypatch.setattr(task_execution_module, "time", deterministic_time)
    monkeypatch.setattr(native_kilo_fixture, "time", deterministic_time)
    project, scm, knowledge, transports = configured(services, clock)
    tokens = {
        name: {"read": f"Bearer {name}-fixture-read", "write": f"Bearer {name}-fixture-write"}
        for name in ("jira", "github", "jenkins", "confluence")
    }

    def credential(value: str) -> Callable[[], str]:
        return lambda: value

    for transport, name in zip(transports, ("jira", "github", "jenkins", "confluence"), strict=True):
        transport.read_authorization = credential(tokens[name]["read"])
        transport.write_authorization = credential(tokens[name]["write"])
    github_request = transports[1].request

    def versioned_github_request(
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> tuple[int, object, dict[str, str]]:
        if transports[1].headers.get("X-GitHub-Api-Version") != "2026-03-10":
            return 403, {"error": "synthetic API version refused"}, {}
        return github_request(method, path, body, before_send=before_send)

    monkeypatch.setattr(transports[1], "request", versioned_github_request)
    transports[1].headers["X-GitHub-Api-Version"] = "2022-11-28"
    assert transports[1].request("GET", "/repos/acme/service/pulls/7")[0] == 403
    transports[1].headers["X-GitHub-Api-Version"] = "2026-03-10"
    repository = sqlite_tmp_path / "source"
    repository.mkdir()
    assert repository.resolve().is_relative_to(sqlite_tmp_path.resolve())
    validate_local_storage(repository)
    reference_git(repository, "init", "--initial-branch=feature")
    (repository / "result.txt").write_text("broken\n" if mode == "baseline-failed" else "original\n")
    reference_git(repository, "add", "result.txt")
    reference_git(repository, "commit", "-m", "Synthetic baseline")
    source_revision = reference_git(repository, "rev-parse", "HEAD").strip()
    repository_url = project.scm.binding.origin + "/" + project.scm.binding.namespace + ".git"
    reference_git(repository, "remote", "add", "origin", repository_url)
    services.head = source_revision
    active_workspace: list[OrdinaryGitWorkspace] = []
    context_drift = [False]
    output_pages = {19: deepcopy(services.page(19, "Output", "<p>unmodified</p>", "fixture original"))}
    output_pages[19]["id"] = "901"
    output_version = [19]

    def open_predecessor(method: str, path: str, body: object) -> tuple[int, object] | None:
        provider = next(
            (
                name
                for prefix, name in (
                    ("/rest/api/3/", "jira"),
                    ("/repos/", "github"),
                    ("/job/", "jenkins"),
                    ("/wiki/", "confluence"),
                )
                if path.startswith(prefix)
            ),
            None,
        )
        if provider is None or services.requests[-1][3] != tokens[provider]["read" if method == "GET" else "write"]:
            return 403, {"error": "synthetic credential refused"}
        if context_drift[0] and path.startswith("/wiki/api/v2/pages/900"):
            status, value = services.route(method, path, body)
            data = deepcopy(cast(dict[str, object], value))
            cast(dict[str, object], cast(dict[str, object], data["body"])["storage"])["value"] = "changed"
            return status, data
        if urlsplit(path).path == "/wiki/api/v2/pages/901":
            if method == "PUT":
                data = cast(dict[str, object], body)
                version = cast(dict[str, object], data["version"])
                if version["number"] != output_version[0] + 1:
                    return 409, {"message": "version conflict"}
                output_version[0] += 1
                page = services.page(
                    output_version[0],
                    cast(str, data["title"]),
                    cast(str, cast(dict[str, object], data["body"])["value"]),
                    cast(str, version["message"]),
                )
                page["id"] = "901"
                output_pages[output_version[0]] = page
                return 200, page
            version = int(parse_qs(urlsplit(path).query).get("version", [str(output_version[0])])[0])
            return (200, output_pages[version]) if version in output_pages else (404, {})
        if urlsplit(path).path == "/repos/acme/service/pulls":
            query = parse_qs(urlsplit(path).query)
            assert query["state"] == ["open"] and query["per_page"] == ["100"]
            pull = services.pull()
            cast(dict[str, object], pull["base"])["sha"] = source_revision
            return 200, [pull] if query["page"] == ["1"] else []
        if urlsplit(path).path == "/repos/acme/service/pulls/7":
            status, value = services.route(method, path, body)
            if status == 200:
                data = cast(dict[str, object], value)
                cast(dict[str, object], data["base"])["sha"] = source_revision
            return status, value
        if path.startswith(tuple(f"/job/service/job/feature/{item}/api/json" for item in (11, 12, 13, 14))):
            if "/11/" in path and mode == "baseline-unavailable":
                return 503, {"error": "baseline unavailable"}
            status, value = services.route(
                method, path.replace("/11/", "/12/").replace("/13/", "/12/").replace("/14/", "/12/"), body
            )
            data = cast(dict[str, object], value)
            number = 11 if "/11/" in path else 13 if "/13/" in path else 14 if "/14/" in path else 12
            if number == 11:
                head = source_revision
                data["timestamp"] = 98_000
                with source_use(repository, repository_url) as leased:
                    actual = GitContentSource(
                        leased, ForgeBinding(project.scm.binding.origin, project.scm.binding.namespace)
                    ).read(source_revision, "result.txt")
                data["result"] = "SUCCESS" if actual == b"original\n" else "FAILURE"
            else:
                collected = active_workspace[0].collected
                assert collected is not None
                head = collected[1].head
            data["number"] = number
            action = cast(dict[str, object], cast(list[object], data["actions"])[0])
            action["lastBuiltRevision"] = {"SHA1": head}
            action["buildsByBranchName"] = {
                "refs/remotes/origin/main": {"buildNumber": number, "revision": {"SHA1": head}}
            }
            return status, data
        if path.startswith("/rest/api/3/issue/ENG-41"):
            status, value = services.route(method, path, body)
            data = cast(dict[str, object], value)
            fields = cast(dict[str, object], data["fields"])
            fields["status"] = {"id": "3", "name": "In Progress"}
            return status, data
        return None

    services.filter = open_predecessor
    before_reviews = deepcopy(services.reviews)
    before_pages, before_draft = deepcopy(services.pages), services.draft
    paths = (
        "/rest/api/3/issue/ENG-42",
        "/repos/acme/service/pulls/7/reviews",
        "/job/service/job/feature/12/api/json",
        "/wiki/api/v2/pages/900",
    )
    names = ("jira", "github", "jenkins", "confluence")
    for index, (transport, name, path) in enumerate(zip(transports, names, paths, strict=True)):
        other = names[(index + 1) % len(names)]
        transport.read_authorization = None
        assert transport.request("GET", path)[0] == 403
        transport.read_authorization = credential(tokens[other]["read"])
        assert transport.request("GET", path)[0] == 403
        transport.read_authorization = credential(tokens[name]["read"])
        transport.write_authorization = credential(tokens[name]["read"])
        assert transport.request("POST", path, {"event": "COMMENT"}, before_send=lambda: None)[0] == 403
        transport.write_authorization = credential(tokens[other]["write"])
        assert transport.request("POST", path, {"event": "COMMENT"}, before_send=lambda: None)[0] == 403
        transport.write_authorization = credential(tokens[name]["write"])
    assert services.reviews == before_reviews and services.pages == before_pages and services.draft == before_draft
    denied_puts = sum(
        method == "PUT" and path.startswith("/wiki/api/v2/pages/901") for method, path, _, _ in services.requests
    )
    selected = project.work.work("ENG-42")
    predecessor = project.work.work("ENG-41")
    source = project.scm.ref("feature").subject
    decision = "qualified-decision"

    def capture(work: WorkObservation, required: bool) -> QualifiedDependencyCut:
        obligation = EdgeObligation(
            work.fact.subject, driver_digest([asdict(edge) for edge in work.dependencies]), decision, now + 28
        )
        return QualifiedDependencyCut.capture(
            work,
            tuple(sorted((edge.identity, required) for edge in work.dependencies)),
            work.fact.observed_at,
            obligation=obligation,
        )

    recipe = "fixture:baseline-has-readable-original:v1"

    def work_declaration(**changes: object) -> Declaration:
        return declaration(recipes=[recipe], **changes)

    baselines = JenkinsQualifiedBaseline(
        project.ci, DriverSubject(project.ci.binding, "build", "11", None), (recipe,), clock.wall
    )
    baseline = baselines.read_baseline(source, digest(encode(work_declaration().value["recipes"])))
    if mode != "baseline-unavailable":
        assert baseline is not None, project.ci.build(
            "11", DriverSubject(source.binding, "commit", source.revision or "", source.revision)
        )

    first_criterion = ContentCriterion("result.txt", b"approved exact result\nedge-case addressed\n")
    second_criterion = ContentCriterion("result.txt", b"second result\n")
    readiness = GitHubJiraLiteralReadiness(
        project.work,
        scm,
        repository,
        ((predecessor.fact.subject, (first_criterion,)), (selected.fact.subject, (second_criterion,))),
        frozenset({"3"}),
        ((knowledge, "900", "19"),),
        Principal("other-owner" if mode == "owner-mismatch" else "owner", "owner"),
        "owner:fixture-complete-repository-visibility",
        clock.wall,
        max_pages=3,
    )

    budget = WorkBudgetPolicy(tuple((kind, 20) for kind in WorkKind), 3, 4_000_000)
    policy = replace(
        intake_policy(),
        budget=BudgetPolicy(4, 1),
        work_budget=budget,
        project_budget=budget,
        project_id="qualified-project",
        recipes=intake_policy().recipes | {recipe},
        evidence_producers=intake_policy().evidence_producers
        | {"jenkins-qualified-baseline:v1", "github-jira-literal-readiness:v1"},
        authority=AuthorityEnvelope(
            "owner",
            max_attempts=4,
            trusted_satisfaction_issuers=frozenset({"verifier"}),
            delegated_actor_ids=frozenset({"worker"}),
        ),
    )
    with ExitStack() as stores:
        store = stores.enter_context(SQLiteProgramStore(sqlite_tmp_path / "qualified-dag.db", boot_clock=clock.boot))
        legacy = Scenario()
        if baseline is not None:
            legacy.baseline.reference = baseline.reference
            legacy.baseline.producer = baseline.producer
        legacy.baseline.observed_at = 95
        ordinary_intake = OrdinaryIntake(store, legacy.reader, clock.wall)
        ordinary_intake.prepare("legacy-alias", SUBJECT, declaration(), expected_parent=None)
        legacy_dependencies = ForgejoDiscovery(legacy.transport, clock.wall, max_pages=2, freshness_seconds=30)
        app = OrdinaryProgram(
            store,
            ordinary_intake,
            legacy_dependencies,
            Principal("owner", "owner"),
            policy,
            VerificationPolicy(policy.reference, ("paths", "structural", "commands", "ci")),
            clock.wall,
            execution_profile=encode(
                {
                    "harness": "codex" if mode == "managed-uncertain" else "kilo-pull",
                    "provider": "fixture-provider" if mode == "managed-uncertain" else None,
                    "model": "fixture-model" if mode == "managed-uncertain" else None,
                    "effort": None,
                    "trust": "trusted_development",
                }
            ),
            qualified_work=(project.work,),
            qualified_scm=(project.scm,),
            qualified_baselines=baselines,
            qualified_readiness=readiness,
        )
        if mode == "baseline-unavailable":
            with pytest.raises(IntakeRefused, match="baseline unavailable"):
                app.prepare_qualified(
                    "first",
                    work_declaration(criteria=[first_criterion.criterion]),
                    capture(predecessor, False),
                    source,
                    relevance_reason="work remains",
                )
            assert store.find_program("qualified-dag") is None
            assert not (sqlite_tmp_path / "owned").exists()
            assert not any(
                method == "GET"
                and any(f"/{item}/api/json" in path for item in (12, 13, 14))
                and auth == tokens["jenkins"]["read"]
                for method, path, _, auth in services.requests
            )
            return
        first = app.prepare_qualified(
            "first",
            work_declaration(criteria=[first_criterion.criterion]),
            capture(predecessor, False),
            source,
            relevance_reason="work remains",
            ci_subjects=(
                DriverSubject(project.ci.binding, "build", "12", None),
                DriverSubject(project.ci.binding, "build", "14", None),
            ),
        )
        assert first.baseline.observed_at == 95
        second = app.prepare_qualified(
            "second",
            work_declaration(criteria=[second_criterion.criterion]),
            capture(selected, True),
            source,
            relevance_reason="work remains",
            ci_subjects=(DriverSubject(project.ci.binding, "build", "13", None),),
        )
        assert QualifiedDraft.parse(encode(second.payload())) == second
        context_subject = DriverSubject.parse(json.loads(first.readiness.proof)["contexts"][0]["subject"])
        assert context_subject == DriverSubject(knowledge.binding, "page", "900", "19")
        with pytest.raises(IntakeRefused, match="outside the exact approved Program"):
            app.propose("missing-predecessor", "deliver", (), qualified_drafts=(second,))
        other_binding = replace(predecessor.fact.subject.binding, origin="https://other-jira.invalid:8443")
        other_work = replace(
            predecessor,
            fact=replace(predecessor.fact, subject=replace(predecessor.fact.subject, binding=other_binding)),
        )
        other_cut = capture(other_work, False)
        other_first = replace(
            first,
            cut=other_cut,
            readiness=replace(first.readiness, work=other_cut.subject, work_digest=other_cut.observation_digest),
        )
        with pytest.raises(IntakeRefused, match="outside the exact approved Program"):
            app.propose("collision", "deliver", (), qualified_drafts=(other_first, second))
        different_source = replace(source, revision="f" * 40)
        assert app._qualified_baseline_age(replace(first.baseline, observed_at=90)) == 90  # pyright: ignore[reportPrivateUsage]
        assert app._qualified_baseline_age(replace(first.baseline, source=different_source, observed_at=100)) == 90  # pyright: ignore[reportPrivateUsage]
        later_legacy = Scenario()
        later_legacy.baseline.reference = first.baseline.reference
        later_legacy.baseline.producer = first.baseline.producer
        later_legacy.baseline.observed_at = 100
        later_draft = OrdinaryIntake(store, later_legacy.reader, clock.wall).prepare(
            "legacy-after-qualified", SUBJECT, declaration(), expected_parent=None
        )
        assert later_draft.evidence.observed_at == 90
        mismatched = replace(
            second,
            baseline=replace(second.baseline, source=different_source),
            readiness=replace(second.readiness, source=different_source),
        )
        with pytest.raises(IntakeRefused, match="source repository or base differs"):
            app.propose("wrong-base", "deliver", (), qualified_drafts=(first, mismatched))
        proposal = app.propose("qualified-dag", "deliver", (), qualified_drafts=(first, second))
        opaque_baseline = replace(first.baseline, reference="legacy:opaque-baseline", proof=b"")
        historical_draft = replace(first, baseline=opaque_baseline)
        assert QualifiedDraft.parse(encode(historical_draft.payload())) == historical_draft
        historical_proposal = app.propose("opaque-baseline", "deliver", (), qualified_drafts=(historical_draft, second))
        with pytest.raises(IntakeRefused, match="baseline identity changed"):
            app.approve(historical_proposal, decision_id=decision, expires_at=now + 28)
        assert store.find_program("opaque-baseline") is None
        if mode in {"baseline-failed", "owner-mismatch"}:
            if mode == "baseline-failed":
                assert first.baseline.result == "failed"
            expected_refusal = (
                "baseline unavailable or unrelated failure"
                if mode == "baseline-failed"
                else "authenticated owner required"
            )
            with pytest.raises(IntakeRefused, match=expected_refusal):
                app.approve(proposal, decision_id=decision, expires_at=now + 28)
            assert store.find_program("qualified-dag") is None
            assert not (sqlite_tmp_path / "owned").exists()
            assert not any(
                method == "GET"
                and any(f"/{item}/api/json" in path for item in (12, 13, 14))
                and auth == tokens["jenkins"]["read"]
                for method, path, _, auth in services.requests
            )
            return
        original_verification = app.verification
        app.verification = VerificationPolicy(policy.reference, ("commands", "paths", "structural", "ci"))
        with pytest.raises(IntakeRefused, match="path and structural checks first"):
            app.propose("misordered", "deliver", (), qualified_drafts=(first, second))
        app.verification = original_verification
        assert proposal.payload["version"] == 3
        spec = app._spec(proposal, None)  # pyright: ignore[reportPrivateUsage]
        assert spec.work_units[1].dependencies == ("first",)
        assert "meaning:first" in spec.work_unit("first").required_inputs
        changed_proof = encode({"context": "different-version-or-content"})
        changed_readiness = replace(first.readiness, proof=changed_proof, reference="sha256:" + digest(changed_proof))
        changed_proposal = replace(proposal, qualified_drafts=(replace(first, readiness=changed_readiness), second))
        changed_spec = app._spec(changed_proposal, None)  # pyright: ignore[reportPrivateUsage]
        assert changed_spec.work_unit_applicability_fingerprint("first") != spec.work_unit_applicability_fingerprint(
            "first"
        )
        approved = app.approve(proposal, decision_id=decision, expires_at=now + 28)
        app.activate(approved)
        assert tuple(item.work_unit_id for item in store.load("qualified-dag").ready_work_units) == ("first",)
        assert app.recover(decision).proposal == proposal
        store.close()
        store = stores.enter_context(SQLiteProgramStore(sqlite_tmp_path / "qualified-dag.db", boot_clock=clock.boot))
        app = OrdinaryProgram(
            store,
            OrdinaryIntake(store, legacy.reader, clock.wall),
            legacy_dependencies,
            Principal("owner", "owner"),
            policy,
            VerificationPolicy(policy.reference, ("paths", "structural", "commands", "ci")),
            clock.wall,
            execution_profile=app.execution_profile,
            qualified_work=(project.work,),
            qualified_scm=(project.scm,),
            qualified_baselines=baselines,
            qualified_readiness=readiness,
        )
        approved = app.recover(decision)
        assert approved.proposal == proposal
        assert app._qualified_baseline_age(replace(first.baseline, observed_at=100)) == 90  # pyright: ignore[reportPrivateUsage]
        assert (
            OrdinaryIntake(store, later_legacy.reader, clock.wall).baseline_age(
                later_legacy.reader.read(SUBJECT, declaration())
            )
            == 90
        )
        app.check(approved)
        context_drift[0] = True
        with pytest.raises(ValueError):
            app.check(approved)
        context_drift[0] = False
        protected = sqlite_tmp_path / "owned"
        protected.mkdir(mode=0o700)
        tree = LocalOwnedWorkspace(protected, "first", create=True)
        assert tree.path.resolve().is_relative_to(sqlite_tmp_path.resolve())
        validate_local_storage(tree.path)
        command_marker = tmp_path / "candidate-command-ran"
        workspace = OrdinaryGitWorkspace(
            store,
            tree,
            repository,
            repository_url,
            proposal,
            (
                VerificationCommand(
                    (
                        sys.executable,
                        "-I",
                        "-c",
                        f"from pathlib import Path; Path({str(command_marker)!r}).write_text('ran'); "
                        "assert Path('result.txt').read_text().startswith('approved exact result\\n')"
                        + (
                            "; assert 'edge-case addressed' in Path('result.txt').read_text()"
                            if mode == "semantic-gate"
                            else ""
                        ),
                    ),
                    10,
                    1,
                ),
            ),
            ("Fixture", "fixture@example.invalid"),
            authority_deadline=approved.expires_at,
            qualified_ci=project.ci,
        )
        assert workspace.source.resolve().is_relative_to(sqlite_tmp_path.resolve())
        assert workspace.objects.resolve().is_relative_to(sqlite_tmp_path.resolve())
        connection: CodexStdio | None = None
        if mode == "managed-uncertain":
            peer = tmp_path / "codex-peer.py"
            peer.write_text(
                "#!" + sys.executable + "\n" + CODEX_PEER.replace('"status": "completed"', '"status": "inProgress"')
            )
            peer.chmod(0o700)
            connection = CodexStdio(
                (str(peer), "app-server"),
                "0.99.1",
                environment={"HOME": str(tmp_path), "PATH": os.defpath},
                schema_methods=frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"}),
                schema_version="0.99.1",
            )
            stores.callback(connection.close)
            witness = CodexOwnedSessionWitness(connection)
            worker = WorkOwnership(
                store,
                Principal("worker", "worker"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
            control = WorkOwnership(
                store,
                Principal("owner", "owner"),
                tree,
                mode=HandoffMode.MANAGED,
                profile=witness.profile,
                resources=(witness.resource,),
                witness=witness,
            )
        else:
            worker = WorkOwnership(
                store, Principal("worker", "worker"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            control = WorkOwnership(
                store, Principal("owner", "owner"), tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
        workspace.control = control
        active_workspace.append(workspace)
        reviewer = cast(OrdinaryReviewer, MagicMock(authority_deadline=approved.expires_at))
        client = app.client(approved, Principal("worker", "worker"), workspace, worker, control, reviewer)
        if mode == "preparation-race":
            caller_revision = store.load("qualified-dag").revision
            original_observe = WorkOwnership.observe
            original_capture = LocalOwnedWorkspace.capture
            changed = [False]

            def capture_race(target: LocalOwnedWorkspace) -> tuple[str, bytes]:
                result = original_capture(target)
                if not changed[0]:
                    changed[0] = True
                    current = store.load("qualified-dag")
                    paused = store.admit(
                        current.program_id, "qualified-pause-race", PauseProgram(current.revision, "owner")
                    )
                    store.admit(paused.program_id, "qualified-resume-race", ResumeProgram(paused.revision, "owner"))
                return result

            def observe_race(target: WorkOwnership, claim: WorkClaim) -> str:
                if target is control:
                    with patch.object(LocalOwnedWorkspace, "capture", capture_race):
                        return original_observe(target, claim)
                return original_observe(target, claim)

            with patch.object(WorkOwnership, "observe", observe_race):
                with pytest.raises(ValueError):
                    client.request("first-request", lease_seconds=10, expected_revision=caller_revision)
            assert changed[0] and workspace.objects.exists()
            attempt = store.load("qualified-dag").attempts[-1].spec
            original_claim = worker.claim("first-request", caller_revision, attempt, lease_seconds=10)
            assert not store.find_ordinary_preparation(original_claim)
            return
        handoff = client.request("first-request", lease_seconds=10)
        assert handoff.attempt.work_unit_id == "first"
        validate_local_storage(workspace.objects)
        assert workspace.base == source_revision
        assert (tree.path / "result.txt").read_text() == "original\n"
        if mode == "managed-uncertain":
            assert connection is not None
            managed = OrdinaryManagedRun(client, workspace, connection)
            assert managed.start(handoff)
            with patch.object(WorkOwnership, "witness", side_effect=ValueError("synthetic unavailable physical proof")):
                assert managed.request_stop("owner stops qualified managed work") == "cancel_uncertain"
            assert tree.path.exists() and not store.load("qualified-dag").satisfactions
            assert store.find_artifact("ordinary-cancel:" + handoff.attempt.attempt_id, "settlement") is None
            with pytest.raises(ValueError):
                client.context(handoff)
            return
        assert client.start(handoff) is None
        handoff_context = json.loads(client.context(handoff))
        assert handoff_context["approved"]["version"] == 3
        approved_draft = handoff_context["approved"]["qualified_drafts"][0]
        handed_proof = json.loads(bytes.fromhex(approved_draft["readiness"]["proof"]))
        assert (
            json.loads(bytes.fromhex(handed_proof["contexts"][0]["native"]))["body"]["storage"]["value"]
            == "<p>synthetic context</p>"
        )
        if mode == "stop":
            app.stop(approved, "owner revoked the qualified Program")
            assert tree.path.exists() and not store.load("qualified-dag").satisfactions
            with pytest.raises(ValueError):
                client.context(handoff)
            assert not any(
                method == "GET"
                and any(f"/{item}/api/json" in path for item in (12, 13, 14))
                and auth == tokens["jenkins"]["read"]
                for method, path, _, auth in services.requests
            )
            return
        (tree.path / "result.txt").write_text("approved exact result\n")
        if mode == "path-gate":
            (tree.path / "forbidden.txt").write_text("not approved\n")
        client.quiesce(handoff, "fixture-worker-finished")
        control.reconcile(handoff.claim, control.observe(handoff.claim), evidence="owner receives exact fixture bytes")
        if mode == "path-gate":
            assert client.report(handoff) == "rejected"
            assert not command_marker.exists()
            assert not any(
                method == "GET"
                and any(f"/{item}/api/json" in path for item in (12, 13, 14))
                and auth == tokens["jenkins"]["read"]
                for method, path, _, auth in services.requests
            )
            return
        if mode == "semantic-gate":
            with review_peer(tmp_path / "review", None) as (origin, calls):
                approving_peer = NativeKiloReview(
                    store,
                    origin,
                    tmp_path,
                    "product-review",
                    lambda _: workspace.review_input(),
                    clock.wall,
                    freshness_seconds=20,
                    timeout_seconds=10,
                    authority_deadline=approved.expires_at,
                )
                checked_client = app.client(
                    approved, Principal("worker", "worker"), workspace, worker, control, approving_peer
                )
                assert checked_client.report(handoff) == "rejected"
                assert command_marker.exists() and not calls
                rejected = json.loads(store.artifact("rejection:" + handoff.attempt.attempt_id, "rejected-result"))
                assert any("command" in item["fingerprint"] for item in rejected["findings"]), rejected["findings"]
            return
        with review_peer(tmp_path / "review", None, reject_first=True) as (origin, calls):
            reviewer = NativeKiloReview(
                store,
                origin,
                tmp_path,
                "product-review",
                lambda _: workspace.review_input(),
                clock.wall,
                freshness_seconds=20,
                timeout_seconds=10,
                authority_deadline=approved.expires_at,
            )
            reviewed_client = app.client(approved, Principal("worker", "worker"), workspace, worker, control, reviewer)
            outcome = reviewed_client.report(handoff)
            assert outcome == "rejected"
            assert calls
            reviewed_proof = json.loads(
                bytes.fromhex(json.loads(workspace.review_input())["approved"]["readiness"]["proof"])
            )
            assert (
                json.loads(bytes.fromhex(reviewed_proof["contexts"][0]["native"]))["body"]["storage"]["value"]
                == "<p>synthetic context</p>"
            )
            control.release(handoff.claim, evidence="owner retains rejected workspace and admits remediation")
            remediation_tree = LocalOwnedWorkspace(protected, "remediation", create=True)
            remediation_workspace = OrdinaryGitWorkspace(
                store,
                remediation_tree,
                repository,
                repository_url,
                proposal,
                workspace.commands,
                ("Fixture", "fixture@example.invalid"),
                authority_deadline=approved.expires_at,
                qualified_ci=project.ci,
            )
            remediation_worker = WorkOwnership(
                store,
                Principal("worker", "worker"),
                remediation_tree,
                mode=HandoffMode.PULL,
                profile=COOPERATIVE_PROFILE,
            )
            remediation_control = WorkOwnership(
                store, Principal("owner", "owner"), remediation_tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            remediation_workspace.control = remediation_control
            active_workspace[0] = remediation_workspace
            remediation_reviewer = NativeKiloReview(
                store,
                origin,
                tmp_path,
                "product-review",
                lambda _: remediation_workspace.review_input(),
                clock.wall,
                freshness_seconds=20,
                timeout_seconds=10,
                authority_deadline=approved.expires_at,
            )
            remediation_client = app.client(
                approved,
                Principal("worker", "worker"),
                remediation_workspace,
                remediation_worker,
                remediation_control,
                remediation_reviewer,
            )
            remediation = remediation_client.request(
                "first-remediation", lease_seconds=10, remediation_of=handoff.attempt.attempt_id
            )
            assert remediation.attempt.remediation_of == handoff.attempt.attempt_id
            remediation_client.start(remediation)
            remediation_context = json.loads(remediation_client.context(remediation))["remediation"]
            assert remediation_context["attempt"] == handoff.attempt.attempt_id and remediation_context["findings"]
            (remediation_tree.path / "result.txt").write_text("approved exact result\nedge-case addressed\n")
            remediation_client.quiesce(remediation, "fixture-remediation-finished")
            remediation_control.reconcile(
                remediation.claim,
                remediation_control.observe(remediation.claim),
                evidence="owner receives exact remediation bytes",
            )
            assert remediation_client.report(remediation) == "accepted"
            assert remediation_workspace.collected is not None
            candidate_head = remediation_workspace.collected[1].head
            assert workspace.collected is not None and workspace.collected[1].head != candidate_head
            ci_operations = [
                operation
                for operation in store.operations("verification:" + remediation.attempt.attempt_id + ":")
                if json.loads(operation.request_json)["request"]["check"] == "ci"
            ]
            assert len(ci_operations) == 1
            evidence = json.loads(store.artifact(ci_operations[0].operation_id, "result"))
            assert evidence["passed"] and evidence["subject"]["head_revision"] == candidate_head
            ci_reference = evidence["artifact_reference"].removeprefix("sha256:")
            ci_payload = json.loads(
                store.artifact("runtime:" + remediation.attempt.attempt_id, "qualified-ci:" + ci_reference)
            )
            assert DriverSubject.parse(ci_payload["source"]).revision == candidate_head
            assert DriverSubject.parse(ci_payload["subject"]).key == "14"
            wrong_source = DriverSubject(scm.binding, "commit", "f" * 40, "f" * 40)
            assert project.ci.build("14", wrong_source).presence is not Presence.FOUND
            mutation = DriverMutation(
                DriverSubject(knowledge.binding, "page", "901", "19"),
                "replace_page",
                (
                    ("actor", "synthetic-confluence-writer"),
                    ("body", "Accepted exact candidate " + candidate_head),
                    ("title", "Qualified execution receipt"),
                ),
                "owner:synthetic-effect-policy",
            )

            def authorize_sync(claim: WorkClaim, requested: DriverMutation) -> DriverEffectGrant:
                app.check(approved)
                assert claim == remediation.claim and requested == mutation
                return DriverEffectGrant(claim, requested, 1, approved.expires_at, Event())

            def authorize_draft(claim: WorkClaim, requested: DriverMutation, key: OperationKey) -> DriverDraftGrant:
                app.check(approved)
                assert claim == remediation.claim and requested == mutation
                return DriverDraftGrant(claim, requested, key, 1, approved.expires_at, Event())

            synchronization = DriverApplication(
                store,
                remediation_control,
                project,
                (scm, knowledge),
                clock.wall,
                authorize_sync,
                lease_seconds=5,
                authorize_draft=authorize_draft,
            )
            services.lost.add("PUT")
            unknown = synchronization.synchronize(remediation.claim, "qualified-sync", "qualified-sync", mutation)
            assert unknown.status is EffectStatus.UNKNOWN
            pending = store.operation("qualified-sync")
            assert pending.status == "unknown" and pending.attempts == 1
            assert store.find_artifact("qualified-sync", "driver-receipt") is None
            assert (
                synchronization.synchronize(remediation.claim, "qualified-sync", "qualified-sync", mutation).status
                is EffectStatus.UNKNOWN
            )
            assert store.operation("qualified-sync") == pending
            clock.now += 6
            receipt = synchronization.synchronize(remediation.claim, "qualified-sync", "qualified-sync", mutation)
            assert receipt.status is EffectStatus.ACCEPTED
            assert (
                synchronization.synchronize(remediation.claim, "qualified-sync", "qualified-sync", mutation) == receipt
            )
            assert store.find_artifact("qualified-sync", "driver-receipt") is not None
            assert (
                sum(
                    method == "PUT" and path.startswith("/wiki/api/v2/pages/901")
                    for method, path, _, _ in services.requests
                )
                == denied_puts + 1
            )
            assert services.pages == before_pages
            assert output_version[0] == 20
            assert tuple(item.work_unit_id for item in store.load("qualified-dag").ready_work_units) == ("second",)
            services.head = "f" * 40
            with pytest.raises(ValueError):
                app.check(approved)
            services.head = source_revision
            app.check(approved)

            second_tree = LocalOwnedWorkspace(protected, "second", create=True)
            second_workspace = OrdinaryGitWorkspace(
                store,
                second_tree,
                repository,
                repository_url,
                proposal,
                (
                    VerificationCommand(
                        (
                            sys.executable,
                            "-I",
                            "-c",
                            "from pathlib import Path; assert Path('result.txt').read_text() == 'second result\\n'",
                        ),
                        10,
                        1,
                    ),
                ),
                ("Fixture", "fixture@example.invalid"),
                authority_deadline=approved.expires_at,
                qualified_ci=project.ci,
            )
            second_worker = WorkOwnership(
                store, Principal("worker", "worker"), second_tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            second_control = WorkOwnership(
                store, Principal("owner", "owner"), second_tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            second_workspace.control = second_control
            active_workspace[0] = second_workspace
            second_reviewer = NativeKiloReview(
                store,
                origin,
                tmp_path,
                "product-review",
                lambda _: second_workspace.review_input(),
                clock.wall,
                freshness_seconds=20,
                timeout_seconds=10,
                authority_deadline=approved.expires_at,
            )
            second_client = app.client(
                approved,
                Principal("worker", "worker"),
                second_workspace,
                second_worker,
                second_control,
                second_reviewer,
            )
            second_handoff = second_client.request("second-request", lease_seconds=10)
            assert second_handoff.attempt.work_unit_id == "second"
            assert (second_tree.path / "result.txt").read_text() == "approved exact result\nedge-case addressed\n"
            second_client.start(second_handoff)
            (second_tree.path / "result.txt").write_text("second result\n")
            second_client.quiesce(second_handoff, "fixture-worker-finished-2")
            second_control.reconcile(
                second_handoff.claim,
                second_control.observe(second_handoff.claim),
                evidence="owner receives exact fixture bytes",
            )
            assert second_client.report(second_handoff) == "accepted"
            assert len(calls) >= 3
        assert store.load("qualified-dag").status.value == "completed"
        assert len(store.load("qualified-dag").satisfactions) == 2


def test_qualified_dependency_cut_preserves_jira_visible_scope(services: SyntheticServices) -> None:
    project, _, _, _ = configured(services, Clock())
    work = project.work.work("ENG-42")
    classifications = tuple(sorted((edge.identity, True) for edge in work.dependencies))
    with pytest.raises(ValueError, match="incomplete native enumeration"):
        QualifiedDependencyCut.capture(work, classifications, 100)
    obligation = EdgeObligation(
        work.fact.subject, driver_digest([asdict(edge) for edge in work.dependencies]), "owner:decision", 120
    )
    cut = QualifiedDependencyCut.capture(work, classifications, 100, obligation=obligation)
    assert cut.assurance == "visible-scope-obligation"
    assert cut.required == tuple(edge.target for edge in work.dependencies)
    assert QualifiedDependencyCut.parse(cut.encoded) == cut
    cut.recheck(project.work.work("ENG-42"), 100)
    with pytest.raises(ValueError):
        cut.recheck(replace(work, dependencies=()), 100)
    changed_edge = replace(work.dependencies[0], target=replace(work.dependencies[0].target, revision="changed"))
    with pytest.raises(ValueError):
        cut.recheck(replace(work, dependencies=(changed_edge, *work.dependencies[1:])), 100)
    with pytest.raises(ValueError, match="changed"):
        cut.recheck(replace(work, fact=replace(work.fact, native=b"changed")), 100)
    with pytest.raises(ValueError):
        cut.recheck(replace(work, fact=replace(work.fact, presence=Presence.INACCESSIBLE)), 100)
    with pytest.raises(ValueError):
        cut.recheck(work, 121)
    with pytest.raises(ValueError):
        QualifiedDependencyCut.capture(work, (), 100, obligation=obligation)
    with pytest.raises(ValueError):
        QualifiedDependencyCut.parse(cut.encoded + b" ")
    with pytest.raises(ValueError):
        QualifiedDependencyCut.parse(cut.encoded.replace(b'"version":1', b'"version":2'))


def test_qualified_dependency_cut_distinct_instances(services: SyntheticServices) -> None:
    project, _, _, _ = configured(services, Clock())
    work = project.work.work("ENG-42")
    classifications = tuple(sorted((edge.identity, True) for edge in work.dependencies))
    obligation = EdgeObligation(
        work.fact.subject, driver_digest([asdict(edge) for edge in work.dependencies]), "owner:decision", 120
    )
    cut = QualifiedDependencyCut.capture(work, classifications, 100, obligation=obligation)
    assert QualifiedDependencyCut.parse(cut.encoded) == cut
    other_binding = replace(work.fact.subject.binding, origin="https://other.invalid:8443")
    other_fact = replace(work.fact, subject=replace(work.fact.subject, binding=other_binding))
    with pytest.raises(ValueError):
        cut.recheck(replace(work, fact=other_fact), 100)
    with pytest.raises(ValueError, match="classification"):
        QualifiedDependencyCut.capture(
            work, tuple(sorted((*classifications, ("extra", True)))), 100, obligation=obligation
        )


def test_ordinary_qualified_observer_uses_only_explicit_binding(services: SyntheticServices) -> None:
    clock = Clock()
    project, _, _, _ = configured(services, clock)
    work = project.work.work("ENG-42")
    budget = WorkBudgetPolicy.basic(2, 1, verifications=2, retained_bytes=1024)
    policy = replace(intake_policy(), work_budget=budget, project_budget=budget, project_id="project")
    app = OrdinaryProgram(
        cast(OrdinaryStore, MagicMock()),
        cast(OrdinaryIntake, None),
        cast(DependencyReader, None),
        Principal("owner", "owner"),
        policy,
        VerificationPolicy(policy.reference, ("paths", "structural", "commands")),
        clock.wall,
        execution_profile=encode(
            {"harness": "kilo-pull", "provider": None, "model": None, "effort": None, "trust": "trusted_development"}
        ),
    )
    obligation = EdgeObligation(
        work.fact.subject, driver_digest([asdict(edge) for edge in work.dependencies]), "owner:decision", 120
    )
    classifications = tuple(sorted((edge.identity, True) for edge in work.dependencies))
    cut = app.observe_qualified_dependencies(project.work, work.fact.subject, classifications, obligation=obligation)
    assert cut.subject == work.fact.subject and cut.edges == work.dependencies
    app.recheck_qualified_dependencies(project.work, QualifiedDependencyCut.parse(cut.encoded))
    other = replace(work.fact.subject, binding=replace(work.fact.subject.binding, origin="https://other.invalid:8443"))
    with pytest.raises(IntakeRefused, match="independently bound"):
        app.observe_qualified_dependencies(project.work, other, classifications, obligation=obligation)
    with pytest.raises(IntakeRefused, match="changed"):
        app.observe_qualified_dependencies(
            project.work, replace(work.fact.subject, revision="stale"), classifications, obligation=obligation
        )


def review_mutation(scm: GitHubSCM) -> DriverMutation:
    return DriverMutation(
        DriverSubject(scm.binding, "change", "7", HEAD),
        "append_review",
        (("actor", "synthetic-kernel"), ("body", "Exact candidate receipt")),
        "owner:synthetic-effect-policy",
    )


def draft_grant(claim: WorkClaim, mutation: DriverMutation, key: OperationKey) -> DriverDraftGrant:
    return DriverDraftGrant(claim, mutation, key, 1, 1000, Event())


@pytest.mark.parametrize(
    "mode",
    [
        "no-callback",
        "missing",
        "wrong-operation",
        "wrong-effect",
        "wrong-digest",
        "wrong-claim",
        "wrong-mutation",
        "revoked",
        "expired",
    ],
)
def test_confluence_requires_separate_exact_draft_authority(services: SyntheticServices, mode: str) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = DriverMutation(
            DriverSubject(knowledge.binding, "page", "900", "19"),
            "replace_page",
            (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
            "owner:policy",
        )
        services.draft = "another user's draft"

        def grant_for_draft(original: WorkClaim, value: DriverMutation, key: OperationKey) -> DriverDraftGrant | None:
            if mode == "missing":
                return None
            grant = draft_grant(original, value, key)
            if mode == "wrong-operation":
                return replace(grant, operation=replace(key, operation_id="different"))
            if mode == "wrong-effect":
                return replace(grant, operation=replace(key, effect_key="different"))
            if mode == "wrong-digest":
                return replace(grant, operation=replace(key, request_digest="different"))
            if mode == "wrong-claim":
                return replace(grant, claim=replace(original, generation=original.generation + 1))
            if mode == "wrong-mutation":
                return replace(grant, mutation=replace(value, authority_reference="different"))
            if mode == "revoked":
                grant.invalidated.set()
            if mode == "expired":
                return replace(grant, expires_at=100)
            return grant

        app = DriverApplication(
            store,
            owner,
            project,
            (scm, knowledge),
            clock.wall,
            effect_grant,
            authorize_draft=None if mode == "no-callback" else grant_for_draft,
        )
        with pytest.raises(ValueError, match="draft"):
            app.synchronize(claim, "page:operation", "page:effect", mutation)
        assert not any(method == "PUT" for method, _, _, _ in services.requests)
        assert services.draft == "another user's draft"


@pytest.mark.parametrize("change", ["revoke", "expire"])
def test_draft_grant_rechecked_after_provider_preflight(services: SyntheticServices, change: str) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = DriverMutation(
            DriverSubject(knowledge.binding, "page", "900", "19"),
            "replace_page",
            (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
            "owner:policy",
        )
        grants: list[DriverDraftGrant] = []

        def authorize_draft(original: WorkClaim, value: DriverMutation, key: OperationKey) -> DriverDraftGrant:
            grant = replace(draft_grant(original, value, key), expires_at=150)
            grants.append(grant)
            return grant

        def credential() -> str:
            if change == "revoke":
                grants[0].invalidated.set()
            else:
                clock.now = 150
            return "Bearer synthetic-write"

        transports[3].write_authorization = credential
        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, effect_grant, authorize_draft=authorize_draft
        )
        with pytest.raises(ValueError, match="draft"):
            app.synchronize(claim, "page:operation", "page:effect", mutation)
        assert len(grants) == 1
        assert not any(method == "PUT" for method, _, _, _ in services.requests)


@pytest.mark.parametrize("draft_timing", ["existing", "concurrent"])
def test_explicit_draft_grant_covers_native_reconciliation(services: SyntheticServices, draft_timing: str) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = DriverMutation(
            DriverSubject(knowledge.binding, "page", "900", "19"),
            "replace_page",
            (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
            "owner:policy",
        )
        if draft_timing == "existing":
            services.draft = "another user's draft"
        else:

            def credential() -> str:
                services.draft = "concurrent draft"
                return "Bearer synthetic-write"

            transports[3].write_authorization = credential
        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, effect_grant, authorize_draft=draft_grant
        )
        assert app.synchronize(claim, "page:operation", "page:effect", mutation).status is EffectStatus.ACCEPTED
        assert services.draft == "<p>new</p>"


def test_draft_authorizer_reentry_cannot_bypass_work_authority(services: SyntheticServices) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = DriverMutation(
            DriverSubject(knowledge.binding, "page", "900", "19"),
            "replace_page",
            (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
            "owner:policy",
        )

        def cancel(original: WorkClaim, value: DriverMutation, key: OperationKey) -> DriverDraftGrant:
            program = store.load(original.attempt.program_id)
            store.admit(program.program_id, "draft-policy:cancel", CancelProgram(program.revision, "owner"))
            return draft_grant(original, value, key)

        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, effect_grant, authorize_draft=cancel
        )
        with pytest.raises((ValueError, BudgetRefused, OperationConflict)):
            app.synchronize(claim, "page:operation", "page:effect", mutation)
        assert not services.requests


def delivery(mutation: DriverMutation, *, attempts: int = 1) -> DriverDelivery:
    return DriverDelivery(OperationKey("operation", "effect", driver_digest(mutation.request())), mutation, 1, attempts)


@pytest.mark.parametrize(
    "field,value",
    [
        ("family", "knowledge"),
        ("implementation", "other"),
        ("origin", "https://github.example.test:9443"),
        ("tenant", "other-tenant"),
        ("namespace", "other/service"),
        ("api_version", "2022-11-28"),
        ("rights", "other-rights"),
    ],
)
def test_subject_identity_qualifies_every_binding_dimension(field: str, value: str) -> None:
    original = binding("scm", "github", "acme/service", "2026-03-10")
    left = DriverSubject(original, "issue", "ENG-42/uuid", "jira-native-updated")
    right = replace(left, binding=replace(original, **{field: value}))
    assert left != right and left.reference != right.reference
    assert left != replace(left, kind="commit") and left != replace(left, revision=HEAD)
    assert left.key == "ENG-42/uuid"
    assert DriverSubject.parse(left.reference) == left
    with pytest.raises(ValueError):
        DriverSubject.parse(left.reference.replace(":v1:", ":v2:"))
    with pytest.raises(ValueError):
        DriverSubject.parse("forgejo:acme/service#42")


def test_subject_reference_maximum_unicode_roundtrip_and_excess_refusal() -> None:
    value = "\U0001f600" * 4096
    original = binding("knowledge", "x", "x", "x")
    maximum = replace(
        original,
        implementation=value,
        origin="https://" + "a" * (4096 - len("https://")),
        tenant=value,
        namespace=value,
        api_version=value,
        rights=value,
    )
    subject = DriverSubject(maximum, value, value, value)
    assert DriverSubject.parse(subject.reference) == subject
    with pytest.raises(ValueError, match="unsupported external subject codec"):
        DriverSubject.parse(subject.reference + "00")


@pytest.mark.parametrize(
    "change", [{"version": "2"}, {"grant": "owner"}, {"trust": "sandbox"}, {"mutation_token": "forbidden"}]
)
def test_configuration_unknown_versions_and_authority_fields_refuse(change: dict[str, str]) -> None:
    value = {**asdict(binding("work", "jira-cloud", "ENG", "3")), **change}
    with pytest.raises(ValueError):
        binding_from_mapping(value)


@pytest.mark.parametrize(
    "origin",
    [
        "http://host.test",
        urlunsplit(("https", "synthetic-user:synthetic-password@host.test", "", "", "")),
        "https://host.test/path",
        "https://host.test?x=y",
    ],
)
def test_binding_never_infers_credential_or_origin_authority(origin: str) -> None:
    with pytest.raises(ValueError):
        replace(binding("work", "jira-cloud", "ENG", "3"), origin=origin)


def test_read_only_driver_never_resolves_mutation_credentials(services: SyntheticServices) -> None:
    project, scm, _, transports = configured(services, Clock())

    def forbidden() -> str:
        raise AssertionError("write credential resolved by read")

    for transport in transports:
        transport.write_authorization = forbidden
    assert project.work.work("uuid-42").fact.subject.key == "ENG-42"
    assert scm.repository().presence is Presence.FOUND
    assert scm.ref("feature").subject.revision == HEAD
    assert scm.commit(HEAD).subject.revision == HEAD
    assert scm.change("7").related[-1].revision == BASE
    assert scm.reviews("7").complete
    review = scm.reviews("7").items[0]
    assert review.subject.revision is None and review.related == (DriverSubject(scm.binding, "commit", HEAD, HEAD),)
    assert len(review.observation_digest) == 64
    assert all(item[0] == "GET" and item[3] == "Bearer synthetic-read" for item in services.requests)
    transports[1].read_authorization = None
    assert scm.repository().presence is Presence.FOUND and services.requests[-1][3] is None


@pytest.mark.parametrize(
    "path",
    [
        "https://other.test/repos/acme/service",
        "//other.test/repos/acme/service",
        "/repos/acme/serviceevil",
        "/repos/acme/service/../secret",
        "/repos/acme/service/%2e%2e/secret",
        "/repos/acme/service/a\\secret",
        "/repos/acme/service#fragment",
        "/repos/acme/service/with space",
    ],
)
def test_namespace_and_url_refusals_precede_authentication(services: SyntheticServices, path: str) -> None:
    _, _, _, transports = configured(services, Clock())
    transport = transports[1]

    def forbidden() -> str:
        raise AssertionError("credential resolved for untrusted target")

    transport.read_authorization = forbidden
    with pytest.raises(ValueError):
        transport.request("GET", path)
    assert not services.requests


def test_redirect_is_not_followed_and_oversized_native_response_is_unknown(services: SyntheticServices) -> None:
    _, scm, _, transports = configured(services, Clock())
    services.filter = lambda method, path, body: (302, {})
    assert scm.repository().presence is Presence.UNKNOWN
    assert len(services.requests) == 1
    services.filter = lambda method, path, body: (200, {"full_name": "acme/service", "oversized": "x" * 300})
    transports[1].max_bytes = 100
    assert scm.repository().presence is Presence.UNKNOWN


def test_http_refuses_unsupported_total_deadline_thread_before_credentials(services: SyntheticServices) -> None:
    _, _, _, transports = configured(services, Clock())
    transports[1].read_authorization = lambda: pytest.fail("unsupported execution thread resolved credential")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(UnsupportedForge):
            executor.submit(transports[1].request, "GET", "/repos/acme/service").result()
    assert not services.requests


def test_jira_native_dependencies_are_not_a_complete_or_accepted_kernel_graph(services: SyntheticServices) -> None:
    project, _, _, _ = configured(services, Clock())
    observed = project.work.work("ENG-42")
    assert observed.fact.presence is Presence.FOUND and observed.fact.complete
    assert not observed.dependencies_complete
    edge = observed.dependencies[0]
    assert (edge.identity, edge.type_id, edge.native_type, edge.direction, edge.target.key) == (
        "link-9",
        "10000",
        "Blocks",
        "is blocked by",
        "ENG-41",
    )
    assert b"Closed" not in observed.fact.native  # It is attributed target data, not acceptance.
    obligation = EdgeObligation(
        observed.fact.subject,
        driver_digest([asdict(item) for item in observed.dependencies]),
        "owner:approved-explicit-edge-cut",
        130,
    )
    obligation.require(observed, 100)
    with pytest.raises(ValueError):
        replace(obligation, edges_digest="different").require(observed, 100)
    with pytest.raises(ValueError):
        obligation.require(observed, 131)


@pytest.mark.parametrize("mode", ["permission404", "missinglinks", "moving", "missingpage", "capped"])
def test_jira_incomplete_changed_or_hidden_edges_are_not_absence(services: SyntheticServices, mode: str) -> None:
    project, _, _, transports = configured(services, Clock())
    reads = 0

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        nonlocal reads
        if "/issue/ENG-42?" not in path:
            if mode == "missingpage" and "/issue/uuid-41?" in path:
                return 404, {}
            return None
        if mode == "permission404":
            return 404, {}
        status, raw = services.route(method, path, body)
        data = copy.deepcopy(object_value(raw))
        fields = object_value(data["fields"])
        if mode == "missinglinks":
            fields.pop("issuelinks")
        if mode == "moving":
            reads += 1
            fields["updated"] = f"native-revision-{reads}"
        if mode == "capped":
            fields["issuelinks"] = [*cast_list(fields["issuelinks"]), *cast_list(fields["issuelinks"])]
        return status, data

    services.filter = changed
    work = JiraWorkManagement(transports[0], Clock().wall, max_edges=1) if mode == "capped" else project.work
    result = work.work("ENG-42")
    assert result.fact.presence is not Presence.FOUND and not result.dependencies_complete
    assert not result.fact.complete


@pytest.mark.parametrize("mode", ["cap", "page404", "duplicate", "movinghead", "permission", "movingreview"])
def test_github_pages_and_repeated_moving_scans_do_not_prove_absence(services: SyntheticServices, mode: str) -> None:
    _, scm, _, transports = configured(services, Clock())
    if mode == "cap":
        scm = GitHubSCM(transports[1], Clock().wall, max_pages=1)
    count = 0

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        nonlocal count
        if "/reviews?" not in path:
            return None
        count += 1
        if mode == "permission":
            return 403, {}
        if "page=2" in path and mode == "page404":
            return 404, {}
        if "page=2" in path and mode == "duplicate":
            return 200, services.reviews
        if mode == "movinghead":
            services.head = "c" * 40
        if mode == "movingreview" and count >= 3:
            return 200, [{**services.reviews[0], "state": "DISMISSED"}] if "page=1" in path else []
        return None

    services.filter = changed
    scan = scm.reviews("7")
    assert not scan.complete and not scan.stable


def test_github_append_is_exact_commit_creation_not_claimed_cas(services: SyntheticServices) -> None:
    _, scm, _, _ = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    before: list[str] = []
    result = scm.apply(intent, lambda: before.append("fresh-authority"))
    assert result.status is EffectStatus.ACCEPTED and before == ["fresh-authority"]
    native = object_value(json.loads(next(item[2] for item in services.requests if item[0] == "POST")))
    assert native["commit_id"] == HEAD and native["event"] == "COMMENT" and "forge-effect:" in str(native["body"])
    assert "not-current-head-CAS" in result.reason
    calls = sum(item[0] == "POST" for item in services.requests)
    services.head = "c" * 40
    historical = scm.apply(replace(intent, attempts=2), lambda: pytest.fail("duplicate mutation"))
    assert historical.status is EffectStatus.ACCEPTED
    assert sum(item[0] == "POST" for item in services.requests) == calls


def test_github_append_head_race_still_targets_only_explicit_approved_commit(services: SyntheticServices) -> None:
    _, scm, _, transports = configured(services, Clock())

    def credential() -> str:
        services.head = "c" * 40
        return "Bearer synthetic-write"

    transports[1].write_authorization = credential
    receipt = scm.apply(delivery(review_mutation(scm)), lambda: None)
    assert receipt.status is EffectStatus.ACCEPTED and services.head != HEAD
    assert services.reviews[-1]["commit_id"] == HEAD  # Append receipt does NOT assert unchanged current PR head.


@pytest.mark.parametrize("action", ["create_pr", "update_pr", "merge", "append_review_if_head_unchanged"])
def test_unsupported_github_conditional_mutation_refuses_before_write_credentials(
    services: SyntheticServices, action: str
) -> None:
    _, scm, _, transports = configured(services, Clock())
    transports[1].write_authorization = lambda: pytest.fail("unsupported mutation resolved credentials")
    with pytest.raises(UnsupportedForge):
        scm.apply(delivery(replace(review_mutation(scm), action=action)), lambda: None)
    assert not services.requests


def test_native_head_drift_refuses_send_and_lost_post_is_not_retried(services: SyntheticServices) -> None:
    _, scm, _, _ = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    services.head = "c" * 40
    assert scm.apply(intent, lambda: None).status is EffectStatus.STALE
    assert all(item[0] == "GET" for item in services.requests)
    services.head = HEAD
    services.lost.add("POST")
    assert scm.apply(intent, lambda: None).status is EffectStatus.UNKNOWN
    assert (
        scm.apply(replace(intent, attempts=2), lambda: pytest.fail("lost reply retried")).status
        is EffectStatus.ACCEPTED
    )
    assert sum(item[0] == "POST" for item in services.requests) == 1


def test_empty_review_search_and_duplicate_marker_are_unknown_not_retry_permission(services: SyntheticServices) -> None:
    _, scm, _, _ = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    assert scm.reconcile(intent).status is EffectStatus.UNKNOWN
    assert scm.apply(intent, lambda: None).status is EffectStatus.ACCEPTED
    services.reviews.append({**services.reviews[-1], "id": 99})
    assert scm.reconcile(intent).status is EffectStatus.UNKNOWN


def test_native_marker_from_another_principal_is_not_kernel_receipt(services: SyntheticServices) -> None:
    _, scm, knowledge, transports = configured(services, Clock())
    intent = delivery(review_mutation(scm))
    assert scm.apply(intent, lambda: None).status is EffectStatus.ACCEPTED
    services.reviews[-1]["user"] = {"login": "untrusted-reviewer"}
    assert scm.reconcile(intent).status is EffectStatus.UNKNOWN
    transports[1].write_authorization = None
    services.reviews[-1]["user"] = {"login": "synthetic-kernel"}
    assert scm.reconcile(intent).status is EffectStatus.ACCEPTED  # Reconciliation needs no write secret.
    mutation = DriverMutation(
        DriverSubject(knowledge.binding, "page", "900", "19"),
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
        "owner:policy",
    )
    page = delivery(mutation)
    assert knowledge.apply(page, lambda: None).status is EffectStatus.ACCEPTED
    object_value(services.pages[20]["version"])["authorId"] = "untrusted-user"
    assert knowledge.reconcile(page).status is EffectStatus.UNKNOWN


@pytest.mark.parametrize("mode", ["wrongsha", "wrongremote", "duplicate", "parameters", "unknownresult", "inherited"])
def test_jenkins_requires_actual_checkout_evidence_not_parameters_or_job_name(
    services: SyntheticServices, mode: str
) -> None:
    project, scm, _, _ = configured(services, Clock())

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        if "/api/json?" not in path:
            return None
        status, raw = services.route(method, path, body)
        data = copy.deepcopy(object_value(raw))
        actions: list[dict[str, object]] = [object_value(value) for value in cast_list(data["actions"])]
        if mode == "wrongsha":
            actions[0]["lastBuiltRevision"] = {"SHA1": "c" * 40}
        if mode == "wrongremote":
            actions[0]["remoteUrls"] = ["https://other.test/acme/service.git"]
        if mode == "duplicate":
            actions.append(copy.deepcopy(actions[0]))
        if mode == "parameters":
            actions = [
                {"_class": "hudson.model.ParametersAction", "parameters": [{"name": "GIT_COMMIT", "value": HEAD}]}
            ]
        if mode == "unknownresult":
            data["result"] = "CUSTOM_PASS"
        if mode == "inherited":
            actions[0]["buildsByBranchName"] = {"main": {"buildNumber": 11, "revision": {"SHA1": HEAD}}}
        data["actions"] = actions
        return status, data

    services.filter = changed
    source = DriverSubject(scm.binding, "commit", HEAD, HEAD)
    assert project.ci.build("12", source).presence is not Presence.FOUND
    with pytest.raises(ValueError):
        project.ci.build("12", replace(source, binding=replace(scm.binding, tenant="other")))


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "https://other.example.test"),
        ("namespace", "other/repo"),
        ("implementation", "github"),
        ("tenant", "other"),
    ],
)
def test_forgejo_ci_refuses_different_source_repository(field: str, value: str) -> None:
    ci = binding("ci", "forgejo-facade", "acme/service", "1")
    source = replace(ci, family="scm")
    forge = cast(ForgejoForge, SimpleNamespace(binding=ForgeBinding(ci.origin, ci.namespace)))
    with pytest.raises(ValueError):
        ForgejoCI(forge, ci, replace(source, **{field: value}), Clock().wall)


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "https://other.example.test"),
        ("tenant", "other"),
        ("namespace", "other/repo"),
        ("implementation", "other"),
        ("api_version", "other"),
        ("rights", "other"),
    ],
)
def test_jenkins_rejects_mapping_for_different_scm_binding(services: SyntheticServices, field: str, value: str) -> None:
    _, scm, _, transports = configured(services, Clock())
    mapping = TrustedSCMRemote(REMOTE, replace(scm.binding, **{field: value}), "owner:mapping")
    with pytest.raises(ValueError, match="mapping"):
        JenkinsCI(transports[2], Clock().wall, remote_mapping=mapping, source_binding=scm.binding)
    assert not services.requests


@pytest.mark.parametrize(
    "remote",
    [
        "https://synthetic" + ":" + "synthetic" + "@example.test/repo",
        "https://example.test/repo?x=1",
        "https://example.test/repo#x",
        "http://example.test/repo",
        "https://example.test/",
    ],
)
def test_jenkins_remote_mapping_rejects_unqualified_urls(remote: str) -> None:
    with pytest.raises(ValueError):
        TrustedSCMRemote(remote, binding("scm", "github", "acme/service", "2026-03-10"), "owner:mapping")


def cast_list(value: object) -> list[object]:
    from creatidy_kernel.adapters.driver_http import list_value

    return list_value(value)


def test_confluence_pinned_content_and_server_version_conflict(services: SyntheticServices) -> None:
    _, _, knowledge, _ = configured(services, Clock())
    before = knowledge.content("900", "19")
    assert before.presence is Presence.FOUND and before.subject.revision == "19"
    assert any("version=19" in item[1] for item in services.requests)
    mutation = DriverMutation(
        before.subject,
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
        "owner:policy",
    )
    intent = delivery(mutation)
    assert knowledge.apply(intent, lambda: None).status is EffectStatus.ACCEPTED
    other = delivery(
        replace(
            mutation, fields=(("actor", "synthetic-confluence-writer"), ("body", "<p>other</p>"), ("title", "Guide"))
        )
    )
    assert knowledge.apply(other, lambda: None).status is EffectStatus.STALE
    assert services.version == 20
    assert knowledge.reconcile(intent).status is EffectStatus.ACCEPTED
    assert knowledge.content("900", "19").native == before.native


def test_confluence_native_condition_handles_race_after_local_preflight(services: SyntheticServices) -> None:
    _, _, knowledge, transports = configured(services, Clock())
    mutation = DriverMutation(
        DriverSubject(knowledge.binding, "page", "900", "19"),
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>ours</p>"), ("title", "Guide")),
        "owner:policy",
    )

    def credential() -> str:
        services.version = 20
        services.pages[20] = services.page(20, "Another owner", "<p>theirs</p>", "independent")
        return "Bearer synthetic-write"

    transports[3].write_authorization = credential
    assert knowledge.apply(delivery(mutation), lambda: None).status is EffectStatus.STALE
    assert services.version == 20 and services.pages[20]["title"] == "Another owner"


def test_confluence_valid_response_without_version_visibility_is_unknown(services: SyntheticServices) -> None:
    _, _, knowledge, _ = configured(services, Clock())
    mutation = DriverMutation(
        DriverSubject(knowledge.binding, "page", "900", "19"),
        "replace_page",
        (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
        "owner:policy",
    )

    def hidden(method: str, path: str, body: object) -> tuple[int, object] | None:
        return (404, {}) if "version=20" in path else None

    services.filter = hidden
    intent = delivery(mutation)
    assert knowledge.apply(intent, lambda: None).status is EffectStatus.UNKNOWN and services.version == 20
    assert (
        knowledge.apply(replace(intent, attempts=2), lambda: pytest.fail("PUT retried")).status is EffectStatus.UNKNOWN
    )
    assert sum(item[0] == "PUT" for item in services.requests) == 1


@pytest.mark.parametrize("mode", ["withdrawn", "space", "version", "body", "moving"])
def test_confluence_permission_version_and_freshness_refusals(services: SyntheticServices, mode: str) -> None:
    _, _, knowledge, _ = configured(services, Clock())
    reads = 0

    def changed(method: str, path: str, body: object) -> tuple[int, object] | None:
        nonlocal reads
        if not path.startswith("/wiki/"):
            return None
        reads += 1
        status, raw = services.route(method, path, body)
        data = copy.deepcopy(object_value(raw))
        if mode == "withdrawn":
            data["status"] = "trashed"
        if mode == "space":
            data["spaceId"] = "other"
        if mode == "version" and "version=" in path:
            data["version"] = {"number": 99}
        if mode == "body":
            data.pop("body")
        if mode == "moving" and reads >= 3:
            data["title"] = "changed"
        return status, data

    services.filter = changed
    assert knowledge.content("900", "19").presence is not Presence.FOUND


def state_directory() -> tempfile.TemporaryDirectory[str]:
    import os

    return tempfile.TemporaryDirectory(dir=os.environ.get("CREATIDY_TEST_STORAGE_DIR"))


@pytest.mark.parametrize(
    "cut", ["credential-stop", "read-stop", "credential-expiry", "credential-lease", "policy-revoke"]
)
def test_application_rechecks_real_claim_authority_after_reads_and_credentials(
    services: SyntheticServices, cut: str
) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        invalidated = Event()
        mutation = review_mutation(scm)
        grant = DriverEffectGrant(claim, mutation, 1, 1000, invalidated)
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, lambda original, mutation: grant)

        def revoke() -> str:
            if cut in {"credential-stop", "read-stop"}:
                program = store.load(claim.attempt.program_id)
                store.admit(program.program_id, "cancel", CancelProgram(program.revision, "owner"))
            elif cut == "credential-expiry":
                clock.now = 1001
            elif cut == "credential-lease":
                clock.now = 161
            else:
                invalidated.set()
            return "Bearer synthetic-write"

        if cut == "read-stop":

            def after(method: str, path: str) -> None:
                if path.endswith("/pulls/7"):
                    services.client_after = None
                    revoke()

            services.client_after = after
        else:
            transports[1].write_authorization = revoke
        with pytest.raises((ValueError, BudgetRefused, OperationConflict)):
            app.synchronize(claim, "review", "effect", mutation)
        assert not any(item[0] == "POST" for item in services.requests)
        assert store.operation("review").status == "unknown"


def test_actual_outbox_fence_is_not_a_worker_supplied_integer(services: SyntheticServices) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        request = {
            **mutation.request(),
            "attempt": claim.attempt.digest,
            "claim": claim.command_key,
            "generation": claim.generation,
        }
        owner.authorize_publication(claim, "review", "effect", request)
        with pytest.raises(OperationConflict):
            store.check_driver_delivery("review", 1, "forged")
        assert not services.requests
        app = DriverApplication(
            store,
            owner,
            project,
            (scm, knowledge),
            clock.wall,
            lambda original, value: effect_grant(original, value) if value == mutation else None,
        )
        assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.ACCEPTED
        with pytest.raises((ValueError, OperationConflict, IdempotencyConflict)):
            app.synchronize(claim, "review", "effect", replace(mutation, authority_reference="worker:forged"))


def test_exact_intent_and_actual_delivery_claim_are_durable_before_native_send(services: SyntheticServices) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        observed: list[int] = []

        def credential() -> str:
            operation = store.operation("review")
            request = object_value(object_value(json.loads(operation.request_json))["request"])
            assert operation.status == "dispatched" and operation.attempts == 1 and operation.fence > 0
            assert request["schema"] == "kernel-driver-effect-v1" and request["target"] == asdict(mutation.target)
            assert request["attempt"] == claim.attempt.digest and request["generation"] == claim.generation
            assert request["authority_reference"] == mutation.authority_reference
            assert not any(item[0] == "POST" for item in services.requests)
            observed.append(operation.fence)
            return "Bearer synthetic-write"

        transports[1].write_authorization = credential
        app = DriverApplication(
            store,
            owner,
            project,
            (scm, knowledge),
            clock.wall,
            lambda original, value: effect_grant(original, value) if value == mutation else None,
        )
        assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.ACCEPTED
        assert observed == [store.operation("review").fence]


def test_original_receipt_replay_is_history_not_fresh_authority(services: SyntheticServices) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        app = DriverApplication(
            store,
            owner,
            project,
            (scm, knowledge),
            clock.wall,
            lambda original, value: effect_grant(original, value) if value == mutation else None,
        )
        receipt = app.synchronize(claim, "review", "effect", mutation)
        calls = len(services.requests)
        clock.now = 1001
        for transport in transports:
            transport.read_authorization = transport.write_authorization = lambda: pytest.fail(
                "history resolved a credential"
            )
        assert app.synchronize(claim, "review", "effect", mutation) == receipt
        assert len(services.requests) == calls and store.operation("review").attempts == 1
        with pytest.raises((BudgetRefused, ValueError)):
            app.synchronize(claim, "new:review", "new:effect", mutation)


def test_installed_recipe_runs_same_real_transports_and_shared_handlers() -> None:
    with state_directory() as directory:
        conformance(Path(directory))


@pytest.mark.parametrize("provider", ["github", "confluence"])
@pytest.mark.parametrize("cut", ["owner-capture", "store-cut"])
@pytest.mark.parametrize("change", ["revoke", "expire"])
def test_effect_grant_is_rechecked_after_final_workspace_capture(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    cut: str,
    change: str,
) -> None:
    from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace

    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = (
            review_mutation(scm)
            if provider == "github"
            else DriverMutation(
                DriverSubject(knowledge.binding, "page", "900", "19"),
                "replace_page",
                (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
                "owner:policy",
            )
        )
        invalidated = Event()
        grant = DriverEffectGrant(claim, mutation, 1, 150, invalidated)
        captures: list[int] = []
        observations = 0
        observing_final = False
        checking_delivery = False
        capture = LocalOwnedWorkspace.capture
        observe = owner.observe
        delivery_cut = store.check_driver_delivery

        def observed(original: WorkClaim) -> str:
            nonlocal observations, observing_final
            observations += 1
            observing_final = observations == 3
            try:
                return observe(original)
            finally:
                observing_final = False

        def captured(workspace: LocalOwnedWorkspace) -> tuple[str, bytes]:
            result = capture(workspace)
            if (observing_final and cut == "owner-capture") or (checking_delivery and cut == "store-cut"):
                captures.append(observations)
                if change == "revoke":
                    invalidated.set()
                else:
                    clock.now = 150
            return result

        decisions: list[int] = []

        def checked(operation_id: str, fence: int, request_digest: str) -> int:
            nonlocal checking_delivery
            checking_delivery = True
            try:
                now = delivery_cut(operation_id, fence, request_digest)
                decisions.append(now)
                return now
            finally:
                checking_delivery = False

        monkeypatch.setattr(owner, "observe", observed)
        monkeypatch.setattr(LocalOwnedWorkspace, "capture", captured)
        monkeypatch.setattr(store, "check_driver_delivery", checked)
        app = DriverApplication(
            store,
            owner,
            project,
            (scm, knowledge),
            clock.wall,
            lambda original, value: grant,
            authorize_draft=draft_grant,
        )
        with pytest.raises(ValueError, match="grant"):
            app.synchronize(claim, "review", "effect", mutation)
        assert observations == 3 and captures
        assert decisions == [150 if change == "expire" else 100]
        assert not any(method in {"POST", "PUT"} for method, _, _, _ in services.requests)
        original = store.operation("review")
        assert original.status == "unknown" and original.attempts == 1
        assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.UNKNOWN
        assert store.operation("review") == original
        assert store.find_artifact("review", "driver-receipt") is None


@pytest.mark.parametrize("provider", ["github", "confluence"])
@pytest.mark.parametrize("change", ["cancel", "expire"])
def test_pregrant_authorizer_reentry_is_followed_by_fresh_work_authority(
    services: SyntheticServices,
    provider: str,
    change: str,
) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = (
            review_mutation(scm)
            if provider == "github"
            else DriverMutation(
                DriverSubject(knowledge.binding, "page", "900", "19"),
                "replace_page",
                (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
                "owner:policy",
            )
        )
        calls: list[str] = []

        def authorize(original: WorkClaim, value: DriverMutation) -> DriverEffectGrant:
            calls.append(change)
            if change == "cancel":
                program = store.load(claim.attempt.program_id)
                store.admit(program.program_id, "policy:cancel", CancelProgram(program.revision, "owner"))
            else:
                clock.now = 1001
            return DriverEffectGrant(original, value, 1, 2000, Event())

        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, authorize, authorize_draft=draft_grant
        )
        with pytest.raises((ValueError, BudgetRefused, OperationConflict)):
            app.synchronize(claim, "review", "effect", mutation)
        assert calls == [change] and not services.requests
        operation = store.operation("review")
        assert operation.attempts == 0 and operation.status not in {"accepted", "rejected"}
        assert store.find_artifact("review", "driver-receipt") is None


@pytest.mark.parametrize("provider", ["github", "confluence"])
@pytest.mark.parametrize("change", ["cancel", "expire"])
def test_authorizer_never_runs_after_final_delivery_cut(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    change: str,
) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = (
            review_mutation(scm)
            if provider == "github"
            else DriverMutation(
                DriverSubject(knowledge.binding, "page", "900", "19"),
                "replace_page",
                (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
                "owner:policy",
            )
        )
        calls: list[bool] = []
        cut_done = False
        original_cut = store.check_driver_delivery

        def checked(operation_id: str, fence: int, request_digest: str) -> int:
            nonlocal cut_done
            now = original_cut(operation_id, fence, request_digest)
            cut_done = True
            return now

        def authorize(original: WorkClaim, value: DriverMutation) -> DriverEffectGrant:
            calls.append(cut_done)
            if cut_done:
                if change == "cancel":
                    program = store.load(claim.attempt.program_id)
                    store.admit(program.program_id, "policy:cancel", CancelProgram(program.revision, "owner"))
                else:
                    clock.now = 1001
            return DriverEffectGrant(original, value, 1, 2000, Event())

        monkeypatch.setattr(store, "check_driver_delivery", checked)
        app = DriverApplication(
            store, owner, project, (scm, knowledge), clock.wall, authorize, authorize_draft=draft_grant
        )
        assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.ACCEPTED
        assert calls == [False] and clock.now == 100
        assert sum(method in {"POST", "PUT"} for method, _, _, _ in services.requests) == 1


@pytest.mark.parametrize("scope", ["claim", "mutation"])
def test_grant_for_different_exact_scope_refuses_before_dispatch(services: SyntheticServices, scope: str) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        grant = DriverEffectGrant(
            replace(claim, generation=claim.generation + 1) if scope == "claim" else claim,
            replace(mutation, authority_reference="owner:different-grant") if scope == "mutation" else mutation,
            1,
            1000,
            Event(),
        )
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, lambda original, value: grant)
        with pytest.raises(ValueError, match="grant"):
            app.synchronize(claim, "review", "effect", mutation)
        assert not services.requests and store.operation("review").attempts == 0


@pytest.mark.parametrize(
    "revision,expires_at",
    [
        (0, 1000),
        (True, 1000),
        (2**63, 1000),
        (1, 0),
        (1, True),
        (1, 2**63),
    ],
)
def test_effect_grant_validates_revision_and_absolute_expiry(
    services: SyntheticServices,
    revision: int,
    expires_at: int,
) -> None:
    clock = Clock()
    _, scm, _, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        _, claim = accepted(store, Path(directory))
        with pytest.raises(ValueError, match="ephemeral"):
            DriverEffectGrant(claim, review_mutation(scm), revision, expires_at, Event())


def test_effect_grant_rejects_event_subclass_with_arbitrary_flag_callback(services: SyntheticServices) -> None:
    class CallbackEvent(Event):
        def is_set(self) -> bool:
            pytest.fail("unbounded callback used as a final grant flag")

    clock = Clock()
    _, scm, _, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        _, claim = accepted(store, Path(directory))
        with pytest.raises(ValueError, match="ephemeral"):
            DriverEffectGrant(claim, review_mutation(scm), 1, 1000, CallbackEvent())


@pytest.mark.parametrize("revoked", [False, True])
def test_effect_grant_uses_builtin_event_flag_not_instance_callback(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
    revoked: bool,
) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        event = Event()
        if revoked:
            event.set()

        def forbidden() -> bool:
            pytest.fail("instance callback invoked instead of bounded stdlib Event flag read")

        monkeypatch.setattr(event, "is_set", forbidden)
        grant = DriverEffectGrant(claim, mutation, 1, 1000, event)
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, lambda original, value: grant)
        if revoked:
            with pytest.raises(ValueError, match="grant"):
                app.synchronize(claim, "review", "effect", mutation)
            assert not services.requests
        else:
            assert app.synchronize(claim, "review", "effect", mutation).status is EffectStatus.ACCEPTED


@pytest.mark.parametrize("provider", ["github", "confluence"])
def test_denied_effect_grant_never_dispatches_or_resolves_provider_credentials(
    services: SyntheticServices,
    provider: str,
) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = (
            review_mutation(scm)
            if provider == "github"
            else DriverMutation(
                DriverSubject(knowledge.binding, "page", "900", "19"),
                "replace_page",
                (("actor", "synthetic-confluence-writer"), ("body", "<p>new</p>"), ("title", "Guide")),
                "owner:policy",
            )
        )
        for transport in transports:
            transport.read_authorization = transport.write_authorization = lambda: pytest.fail(
                "denied grant resolved credentials"
            )
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, lambda original, value: None)
        with pytest.raises(ValueError, match="grant"):
            app.synchronize(claim, "review", "effect", mutation)
        assert not services.requests and store.operation("review").attempts == 0
        assert store.find_artifact("review", "driver-receipt") is None


@pytest.mark.parametrize("rejected", [False, True], ids=["github-accepted", "confluence-409-rejected"])
def test_terminal_receipt_repairs_exact_settlement_after_crash_reopen(rejected: bool) -> None:
    with state_directory() as directory:
        settlement_recovery(Path(directory), rejected=rejected)


@pytest.mark.parametrize(
    "mismatch", ["unknown", "stale", "rejected", "key", "reference", "noncanonical", "extra-field", "cancelled"]
)
def test_cached_terminal_receipt_must_match_exact_operation_and_canonical_outcome(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    clock = Clock()
    project, scm, knowledge, _ = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, effect_grant)
        receipt = app.synchronize(claim, "review", "effect", mutation)
        value = asdict(receipt)
        if mismatch in {"unknown", "stale", "rejected"}:
            value["status"] = mismatch
        elif mismatch == "key":
            value["operation"] = {**asdict(receipt.operation), "request_digest": "different-exact-input"}
        elif mismatch == "reference":
            value["reference"] = "different-native-review"
        elif mismatch == "extra-field":
            value["grant"] = "not-a-receipt-field"
        cached = json.dumps(value, indent=2).encode() if mismatch == "noncanonical" else driver_bytes(value)
        calls = len(services.requests)
        operation = store.operation("review")
        native_operation = store.operation

        if mismatch == "cancelled":

            def cancelled_operation(operation_id: str) -> OperationRecord:
                return replace(native_operation(operation_id), status="cancelled")

            monkeypatch.setattr(store, "operation", cancelled_operation)

        def cached_artifact(operation_id: str, name: str) -> bytes:
            return cached

        def no_settlement(operation_id: str, evidence: str, *, expected_fence: int | None = None) -> None:
            pytest.fail("mismatched receipt settled")

        monkeypatch.setattr(store, "find_artifact", cached_artifact)
        monkeypatch.setattr(store, "record_work_settlement", no_settlement)
        if mismatch == "cancelled":
            assert app.synchronize(claim, "review", "effect", mutation) == receipt
        else:
            with pytest.raises(ValueError, match="receipt"):
                app.synchronize(claim, "review", "effect", mutation)
        assert len(services.requests) == calls and native_operation("review") == operation


def test_nonterminal_cached_receipt_still_finishes_original_outcome_without_resend(
    services: SyntheticServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    project, scm, knowledge, transports = configured(services, clock)
    with (
        state_directory() as directory,
        SQLiteProgramStore(Path(directory) / "state.sqlite3", boot_clock=clock.boot) as store,
    ):
        owner, claim = accepted(store, Path(directory))
        mutation = review_mutation(scm)
        app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, effect_grant)

        def crash(*args: object, **kwargs: object) -> None:
            raise RuntimeError("crash after artifact before terminal observation")

        with monkeypatch.context() as patch:
            patch.setattr(store, "observe", crash)
            with pytest.raises(RuntimeError, match="before terminal observation"):
                app.synchronize(claim, "review", "effect", mutation)
        operation = store.operation("review")
        cached = store.find_artifact("review", "driver-receipt")
        assert operation.status == "dispatched" and cached is not None
        calls = len(services.requests)
        clock.now = 1001
        for transport in transports:
            transport.read_authorization = transport.write_authorization = lambda: pytest.fail(
                "cache replay contacted provider"
            )

        def no_new_grant(original: WorkClaim, value: DriverMutation) -> DriverEffectGrant | None:
            pytest.fail("cache replay requested new grant")

        app.authorize = no_new_grant
        receipt = app.synchronize(claim, "review", "effect", mutation)
        assert receipt.status is EffectStatus.ACCEPTED and driver_bytes(asdict(receipt)) == cached
        recovered = store.operation("review")
        assert recovered.status == "accepted" and (recovered.fence, recovered.attempts) == (
            operation.fence,
            operation.attempts,
        )
        owner.release(claim, evidence="original-nonterminal-receipt-recovery")
        assert len(services.requests) == calls
