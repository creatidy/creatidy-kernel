# SPDX-License-Identifier: Apache-2.0
"""Installed-capable native role conformance, using only synthetic HTTPS peers."""

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from creatidy_kernel.adapters.forgejo import AGitPush, ForgeBinding, ForgejoForge
from creatidy_kernel.adapters.forgejo_drivers import ForgejoCI, ForgejoSCM, ForgejoWorkManagement
from creatidy_kernel.adapters.forgejo_transport import HTTPSForgejoTransport
from creatidy_kernel.adapters.forgejo_verification import ForgejoStatusVerification
from creatidy_kernel.adapters.project_drivers import TrustedSCMRemote
from creatidy_kernel.adapters.qualified_baseline import ConfiguredQualifiedBaseline, QualifiedCIProfile
from creatidy_kernel.core.drivers import DriverBinding, DriverFact, DriverSubject
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import Effect, EffectStatus, Presence, Reference, UnsupportedForge
from creatidy_kernel.core.intake import digest, encode
from creatidy_kernel.ports.drivers import ProjectDrivers
from tools.check_drivers import BASE, HEAD, Clock, SyntheticServices, configured

ORIGIN = "https://forgejo-roles.example.test:8443"
REPOSITORY = "own/project"


@dataclass
class ReadOnlyGit:
    binding: ForgeBinding
    supports_conditional_push: bool = False
    supports_agit: bool = False

    def compare_and_push(
        self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
    ) -> EffectStatus:
        raise UnsupportedForge("synthetic read-only native Git")

    def create_agit_pr(
        self, repository: Reference, base: str, topic: str, revision: Reference, title: str, description: str
    ) -> AGitPush:
        raise UnsupportedForge("synthetic read-only native AGit")


class NativePeer:
    def __init__(self) -> None:
        self.services, self.clock = SyntheticServices(), Clock()
        self.services.filter = self.route
        self.mode = "normal"
        self.reads: dict[str, int] = {}
        native = ForgeBinding(ORIGIN, REPOSITORY)
        http = HTTPSForgejoTransport(
            ORIGIN + "/api/v1", Reference("forgejo:" + REPOSITORY), lambda: "synthetic-read-only"
        )
        http.opener = self.services.opener(ORIGIN)
        self.forge = ForgejoForge(http, ReadOnlyGit(native), lambda _: False, page_size=1)
        self.work = ForgejoWorkManagement(self.forge, self.binding("work"), self.clock.wall, max_pages=4)
        self.scm = ForgejoSCM(self.forge, self.binding("scm"), self.clock.wall, max_pages=4)
        self.ci = ForgejoCI(self.forge, self.binding("ci"), self.scm.binding, self.clock.wall, max_pages=4)

    def binding(self, family: str) -> DriverBinding:
        return DriverBinding(
            family, "forgejo-facade", "1", ORIGIN, "read-account", REPOSITORY, "1", "owner:fixture-read-rights"
        )

    @staticmethod
    def issue(number: int, repository: str = REPOSITORY) -> dict[str, object]:
        return {
            "number": number,
            "title": "native issue",
            "body": "prose is not an edge",
            "state": "open",
            "updated_at": "2026-11-16T00:00:00Z",
            "html_url": f"{ORIGIN}/{repository}/issues/{number}",
        }

    @staticmethod
    def change() -> dict[str, object]:
        return {
            "number": 7,
            "state": "open",
            "merged": False,
            "updated_at": "2026-11-16T00:00:00Z",
            "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": REPOSITORY}},
            "base": {"sha": BASE, "ref": "develop", "repo": {"full_name": REPOSITORY}},
        }

    @staticmethod
    def review() -> dict[str, object]:
        return {
            "id": 1,
            "commit_id": HEAD,
            "state": "APPROVED",
            "body": "native, not Kernel acceptance",
            "user": {"login": "reviewer"},
            "team": None,
            "stale": False,
            "dismissed": False,
            "official": True,
            "pull_request_url": f"{ORIGIN}/{REPOSITORY}/pulls/7",
            "updated_at": "2026-11-16T00:00:00Z",
        }

    def route(self, method: str, path: str, body: object) -> tuple[int, object] | None:
        if not path.startswith("/api/v1/repos/" + REPOSITORY):
            return None
        assert method == "GET" and body is None
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        resource = parsed.path.removeprefix("/api/v1/repos/" + REPOSITORY)
        self.reads[resource] = self.reads.get(resource, 0) + 1
        self.services.response_headers = {}
        if resource == "":
            return 200, {"full_name": REPOSITORY}
        if resource == "/branches/develop":
            return 200, {"name": "develop", "commit": {"id": BASE}}
        if resource == "/actions/runs/11":
            if self.mode == "run-denied":
                return 403, {}
            return 200, {
                "id": 11,
                "index_in_repo": 2,
                "status": "success",
                "workflow_id": "verify.yml",
                "event": "push",
                "commit_sha": BASE if self.mode == "run-wrong-sha" else HEAD,
                "repository": {
                    "full_name": REPOSITORY if self.mode != "run-wrong-repository" else "other/repo",
                    "html_url": ORIGIN + "/" + REPOSITORY,
                },
            }
        if resource == "/issues/42":
            issue = self.issue(42)
            if self.mode == "issue-drift" and self.reads[resource] > 1:
                issue["updated_at"] = "changed"
            return 200, issue
        if resource == "/issues/42/dependencies":
            if self.mode in {"permission", "unsupported"}:
                return (403 if self.mode == "permission" else 404), {}
            target = self.issue(9, "other/repository")
            if self.mode == "closed":
                target["state"] = "closed"
            if self.mode == "hidden":
                target = {"title": "HIDDEN"}
            if self.mode == "moving" and self.reads[resource] > 2:
                target["state"] = "closed"
            if self.mode == "slow":
                self.clock.now += 20
            return 200, [target] if page == 1 or self.mode == "capped" else []
        if resource.startswith("/git/commits/"):
            revision = resource.rsplit("/", 1)[-1]
            if self.mode in {"commit-denied", "commit-hidden"}:
                return (403 if self.mode == "commit-denied" else 404), {}
            return 200, {
                "sha": "f" * 40 if self.mode == "wrong-sha" else revision,
                "url": f"{ORIGIN}/api/v1/repos/{REPOSITORY}/git/commits/{revision}"
                if self.mode != "wrong-url"
                else "https://other.invalid/commit",
                "commit": {"tree": {"sha": revision}},
            }
        if resource == "/pulls":
            if self.mode == "inventory-denied":
                return 403, []
            if self.mode == "inventory-slow":
                self.clock.now += 20
            if self.mode == "inventory-fork":
                value = self.change()
                value["head"] = {"sha": HEAD, "ref": "feature", "repo": {"full_name": "external/fork"}}
                return 200, [value] if page == 1 else []
            if self.mode == "inventory-cap":
                return 200, [self.change()]
            return 200, [self.change()] if page == 1 else []
        if resource == "/pulls/7":
            change = self.change()
            if self.mode == "head-drift" and self.reads[resource] > 1:
                change["head"] = {"sha": "e" * 40, "ref": "feature", "repo": {"full_name": REPOSITORY}}
            if self.mode == "inventory-moving":
                change["updated_at"] = "edited-between-list-and-detail"
            if self.mode == "inventory-fork":
                change["head"] = {"sha": HEAD, "ref": "feature", "repo": {"full_name": "external/fork"}}
            return 200, change
        if resource == "/pulls/7/reviews":
            self.services.response_headers = {"X-Total-Count": "1"}
            review = self.review()
            if self.mode == "no-total":
                self.services.response_headers = {}
            if self.mode == "review-denied":
                return 403, []
            if self.mode == "empty":
                self.services.response_headers = {"X-Total-Count": "0"}
                return 200, []
            if self.mode == "hidden-gap":
                self.services.response_headers = {"X-Total-Count": "2"}
                return 200, [] if page == 1 else [review]
            if self.mode == "review-cap":
                self.services.response_headers = {"X-Total-Count": "100"}
            if self.mode == "review-moving" and self.reads[resource] > 1:
                review["body"] = "edited"
            if self.mode in {"stale", "dismissed"}:
                review[self.mode] = True
            if self.mode == "old-commit":
                review["commit_id"] = BASE
            if self.mode == "review-foreign":
                review["pull_request_url"] = "https://other.invalid/own/project/pulls/7"
            if self.mode == "request":
                review.update({"state": "REQUEST_REVIEW", "commit_id": "", "user": None, "team": {"name": "reviewers"}})
            return 200, [review]
        if resource.startswith("/commits/") and resource.endswith("/statuses"):
            result = "success" if self.mode not in {"pending", "failure", "unknown"} else self.mode
            row: dict[str, object] = {"id": 1, "status": result, "context": "trusted-check"}
            if self.mode == "status-moving" and self.reads[resource] > 2:
                row["status"] = "failure"
            if self.mode == "wrong-context":
                row["context"] = "untrusted-check"
            if self.mode == "status-empty":
                return 200, []
            if self.mode in {"status-duplicate", "status-repeat-id"}:
                row["id"] = page if self.mode == "status-duplicate" else 1
                return 200, [row] if page < 3 else []
            return 200, [row] if page == 1 else []
        return 404, {}


@pytest.fixture
def peer() -> Iterator[NativePeer]:
    value = NativePeer()
    try:
        yield value
    finally:
        value.services.close()


@pytest.mark.parametrize(
    "mode", ["normal", "closed", "permission", "unsupported", "hidden", "moving", "slow", "capped", "issue-drift"]
)
def test_native_work_dependencies_keep_scope_and_uncertainty(peer: NativePeer, mode: str) -> None:
    peer.mode = mode
    work = peer.work.work("42")
    assert work.fact.presence is Presence.FOUND
    assert work.dependencies_complete is (mode in {"normal", "closed"})
    if mode in {"normal", "closed"}:
        target = work.dependencies[0].target
        assert target.binding.namespace == "other/repository" and target.binding.origin == ORIGIN
        assert target.key == "9" and target.revision == "2026-11-16T00:00:00Z"
        assert not json.loads(work.fact.native)["dependency_snapshot"]
        assert json.loads(work.fact.native)["dependencies"][0]["state"] == ("closed" if mode == "closed" else "open")


@pytest.mark.parametrize("mode", ["normal", "wrong-sha", "wrong-url", "commit-denied", "commit-hidden"])
def test_native_commit_is_exact_not_branch_or_tree_attestation(peer: NativePeer, mode: str) -> None:
    peer.mode = mode
    fact = peer.scm.commit(HEAD)
    assert fact.complete is (mode == "normal")
    if fact.complete:
        assert fact.subject == DriverSubject(peer.scm.binding, "commit", HEAD, HEAD)
        assert "tree-identity-unproved" in fact.qualification
    if mode == "commit-hidden":
        assert fact.presence is Presence.INACCESSIBLE and not fact.complete
        assert fact.qualification == "permission-or-absence-unknown"
    with pytest.raises(ValueError):
        peer.scm.commit("feature")


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "empty",
        "stale",
        "dismissed",
        "request",
        "old-commit",
        "hidden-gap",
        "no-total",
        "review-denied",
        "review-foreign",
        "review-cap",
        "review-moving",
        "head-drift",
    ],
)
def test_native_reviews_do_not_conflate_visibility_or_acceptance(peer: NativePeer, mode: str) -> None:
    peer.mode = mode
    scan = peer.scm.reviews("7")
    assert scan.complete is (mode in {"normal", "empty", "stale", "dismissed", "request", "old-commit"})
    if mode in {"stale", "dismissed"}:
        assert json.loads(scan.items[0].native)[mode] is True
        assert json.loads(scan.items[0].native)["state"] == "APPROVED"
    if mode == "request":
        assert not scan.items[0].related
    if mode == "old-commit":
        assert scan.items[0].related[0].revision == BASE
    if mode == "hidden-gap":
        assert peer.reads["/pulls/7/reviews"] == 2
    assert all(request[0] == "GET" for request in peer.services.requests)


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "pending",
        "failure",
        "unknown",
        "wrong-context",
        "status-moving",
        "status-empty",
        "status-duplicate",
        "status-repeat-id",
    ],
)
def test_status_profile_receives_exact_native_strength(peer: NativePeer, mode: str) -> None:
    peer.mode = mode
    producer = ForgejoStatusVerification(
        peer.ci, ("trusted-check",), ("trusted:recipe:v1",), "owner:recipe-status-binding"
    )
    source = DriverSubject(peer.scm.binding, "ref", "develop", BASE)
    received = producer.baseline().read_baseline(source, digest(encode(producer.recipes)))
    if mode in {"normal", "failure"}:
        assert received is not None and received.result == ("passed" if mode == "normal" else "failed")
        assert "not-Actions" in json.loads(received.proof)["qualification"]
    else:
        assert received is None
    producer.contexts = ("changed",)
    assert producer.baseline().read_baseline(source, digest(encode(producer.recipes))) is None


def test_independent_work_source_and_ci_bindings_require_explicit_mapping(peer: NativePeer) -> None:
    project, scm, _, _ = configured(peer.services, peer.clock)
    composed = ProjectDrivers(peer.work, scm, project.ci, ())
    assert composed.work.binding.origin != composed.scm.binding.origin
    assert composed.work.work("42").dependencies_complete
    source_fact = scm.commit(HEAD)
    jenkins_fact = composed.ci.build("12", source_fact.subject)
    assert source_fact.complete and jenkins_fact.complete and jenkins_fact.related == (source_fact.subject,)
    with pytest.raises(ValueError, match="exact SCM"):
        ForgejoCI(peer.forge, peer.binding("ci"), scm.binding, peer.clock.wall)
    mapping = TrustedSCMRemote(ORIGIN + "/" + REPOSITORY + ".git", scm.binding, "owner:exact-mirrored-source")
    mapped = ForgejoCI(peer.forge, peer.binding("ci"), scm.binding, peer.clock.wall, remote_mapping=mapping)
    source = DriverSubject(scm.binding, "commit", HEAD, HEAD)
    fact = mapped.build(HEAD, source)
    assert fact.complete and fact.related == (source,)
    assert "trusted-remote-mapping-sha256:" in fact.qualification
    before_mismatch = len(peer.services.requests)
    with pytest.raises(ValueError, match="exact SCM commit"):
        mapped.build(HEAD, replace(source, binding=replace(source.binding, tenant="different-account")))
    with pytest.raises(ValueError, match="unsupported driver implementation major"):
        ForgejoSCM(peer.forge, replace(peer.binding("scm"), version="unknown"), peer.clock.wall)
    assert len(peer.services.requests) == before_mismatch
    peer.forge.http.binding = ForgeBinding(ORIGIN, "changed/repository")
    with pytest.raises(ValueError, match="transport binding changed"):
        mapped.build(HEAD, source)


@pytest.mark.parametrize(
    "mode", ["normal", "inventory-denied", "inventory-cap", "inventory-fork", "inventory-moving", "inventory-slow"]
)
def test_native_inventory_is_exact_bounded_and_not_snapshot(peer: NativePeer, mode: str) -> None:
    peer.mode = mode
    scan = peer.scm.open_changes()
    assert scan.complete is (mode == "normal")
    if scan.complete:
        assert scan.scope == "all-open-changes-in-repository:" + peer.scm.binding.digest
        assert scan.items[0].related == (
            DriverSubject(peer.scm.binding, "commit", HEAD, HEAD),
            DriverSubject(peer.scm.binding, "commit", BASE, BASE),
        )
        assert "not-snapshot" in scan.qualification


def test_native_roles_share_existing_project_observation_without_new_authority(peer: NativePeer) -> None:
    selected = ProjectDrivers(peer.work, peer.scm, peer.ci, ())
    work, facts, reviews = selected.observe("42", "develop", "7", HEAD, ())
    assert work.dependencies_complete and reviews.complete and len(facts) == 5
    for fact in facts:
        fact.require_current(peer.clock.wall())
        assert fact.complete
    assert not peer.forge.capabilities() & {"pr", "conditional_push", "branch_create"}
    assert all(request[0] == "GET" for request in peer.services.requests)
    before_refusal = len(peer.services.requests)

    def authorized(_: Effect) -> bool:
        return True

    peer.forge.authorize = authorized
    conditional = Effect(
        OperationKey("fixture:operation", "fixture:effect", "fixture:request"),
        Reference("forgejo:" + REPOSITORY),
        "branch",
        "conditional-topic",
        revision=Reference("forgejo:" + HEAD),
        base_branch="develop",
        base_revision=Reference("forgejo:" + BASE),
        fence=1,
        delivery_attempts=1,
    )
    with pytest.raises(UnsupportedForge, match="branch_create unsupported"):
        peer.scm.apply_legacy(conditional)
    assert len(peer.services.requests) == before_refusal


def test_status_profile_reconstruction_preserves_proof_not_native_build_age(peer: NativePeer) -> None:
    source = DriverSubject(peer.scm.binding, "ref", "develop", BASE)
    first = ForgejoStatusVerification(peer.ci, ("trusted-check",), ("trusted:recipe:v1",), "owner:profile")
    recipe_digest = digest(encode(first.recipes))
    initial = first.baseline().read_baseline(source, recipe_digest)
    assert initial is not None and initial.observed_at == 100
    peer.clock.now = 110
    reconstructed = ForgejoStatusVerification(peer.ci, first.contexts, first.recipes, "owner:profile")
    closing = reconstructed.baseline().read_baseline(source, recipe_digest)
    assert closing is not None and closing.proof == initial.proof and closing.reference == initial.reference
    assert closing.observed_at == 110
    assert reconstructed.baseline().read_baseline(source, "different-recipe") is None


@pytest.mark.parametrize("mode", ["stable", "changed", "expired", "native-timestamp"])
def test_status_baseline_latency_preserves_earliest_age_and_exact_evidence(peer: NativePeer, mode: str) -> None:
    producer = ForgejoStatusVerification(peer.ci, ("trusted-check",), ("trusted:recipe:v1",), "owner:profile")
    original = producer.build
    starts: list[int] = []

    def delayed(key: str, source: DriverSubject) -> DriverFact:
        fact = original(key, source)
        starts.append(fact.observed_at)
        peer.clock.now += 30 if mode == "expired" and len(starts) == 2 else 1
        if mode == "changed" and len(starts) == 1:
            peer.mode = "failure"
        return fact

    profile = producer.profile()
    if mode == "native-timestamp":
        profile = QualifiedCIProfile(producer, producer.binding, producer.producer, profile.interpret)
    reader = ConfiguredQualifiedBaseline(
        profile, DriverSubject(producer.binding, "build", producer.key, None), producer.recipes, peer.clock.wall
    )
    with patch.object(producer, "build", side_effect=delayed):
        result = reader.read_baseline(
            DriverSubject(peer.scm.binding, "ref", "develop", BASE), digest(encode(producer.recipes))
        )
    assert starts == [100, 101]
    if mode == "stable":
        assert result is not None and result.result == "passed" and result.observed_at == 100
        assert json.loads(result.proof)["timestamp_kind"] == "observation"
        assert profile.matches_approval(result.proof)
        native = QualifiedCIProfile(producer, producer.binding, producer.producer, profile.interpret)
        assert not native.matches_approval(result.proof)
    else:
        assert result is None


@pytest.mark.parametrize("mode", ["normal", "run-denied", "run-wrong-sha", "run-wrong-repository"])
def test_actions_aggregate_is_not_checkout_attempt_or_step_proof(peer: NativePeer, mode: str) -> None:
    peer.mode = mode
    source = DriverSubject(peer.scm.binding, "commit", HEAD, HEAD)
    run = peer.ci.run("11", source)
    assert (run.presence is Presence.FOUND) == (mode == "normal")
    assert not run.complete
    if mode == "normal":
        assert json.loads(run.native)["status"] == "success" and run.related == (source,)
        assert "actual-checkout-steps-and-job-attempts-unavailable" in run.qualification

        def no_interpretation(_: DriverFact, now: int) -> tuple[str, int]:
            raise AssertionError("incomplete metadata must never reach a passing interpreter")

        profile = QualifiedCIProfile(peer.ci, peer.ci.binding, "fixture:mandatory-native-Actions", no_interpretation)
        selected = DriverSubject(peer.ci.binding, "build", "11", None)
        with patch.object(peer.ci, "build", return_value=replace(run, subject=selected)):
            assert (
                ConfiguredQualifiedBaseline(profile, selected, ("recipe",), peer.clock.wall).read_baseline(
                    DriverSubject(peer.scm.binding, "ref", "feature", HEAD), digest(encode(("recipe",)))
                )
                is None
            )
