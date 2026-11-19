# SPDX-License-Identifier: Apache-2.0
"""Native-shaped read-only role conformance, never a live Actions acceptance claim."""

import base64
import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, replace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from test_forgejo_roles import NativePeer

from creatidy_kernel.adapters.actions_roles import ActionsRecipe, ForgejoActionsCI, GitHubActionsCI
from creatidy_kernel.adapters.driver_http import DriverHTTP
from creatidy_kernel.adapters.github_work import GitHubIssues
from creatidy_kernel.adapters.project_drivers import TrustedSCMRemote
from creatidy_kernel.core.drivers import DriverBinding, DriverSubject, EdgeObligation, driver_digest
from creatidy_kernel.core.forge import Presence, UnsupportedForge
from creatidy_kernel.core.intake import digest, encode
from creatidy_kernel.core.ordinary_qualified import QualifiedDependencyCut
from creatidy_kernel.ports.drivers import ProjectDrivers
from tools.check_drivers import HEAD

WORKFLOW = (
    b"name: trusted\non: [push]\njobs:\n  gate:\n    name: trusted gate\n    runs-on: fixture\n    steps:\n"
    b"      - name: Checkout\n        run: trusted-checkout\n      - name: Gate\n        run: trusted-check\n"
)
GITHUB = "https://github-roles.example.test:8443"
STAMP = "1970-01-01T00:01:35Z"


class RolePeer:
    def __init__(self, provider: str, mode: str = "success") -> None:
        self.native = NativePeer()
        self.services, self.clock = self.native.services, self.native.clock
        self.services.filter = self.route
        self.provider, self.mode = provider, mode
        self.head, self.run_id = HEAD, 11
        self.result_stamp = STAMP
        self.reads: dict[str, int] = {}
        self.source = (
            self.native.scm.binding
            if provider == "forgejo"
            else replace(self.native.scm.binding, origin=GITHUB, implementation="github", api_version="2026-03-10")
        )
        self.binding = replace(
            self.source,
            family="ci",
            implementation="forgejo-actions" if provider == "forgejo" else "github-actions",
            api_version="16.0.0" if provider == "forgejo" else "2026-03-10",
        )
        self.prefix = "/repos/" + self.binding.namespace
        path = (".forgejo" if provider == "forgejo" else ".github") + "/workflows/verify.yml"
        self.recipe = ActionsRecipe(
            71,
            "verify.yml" if provider == "forgejo" else "19",
            path,
            digest(WORKFLOW),
            "refs/heads/verify",
            ("trusted:recipe:v1",),
            ("trusted gate",),
            () if provider == "forgejo" else (("trusted gate", ("Checkout", "Gate")),),
            "owner:exact-workflow-recipe",
            "owner:complete-applicable-CI-visibility",
        )
        self.http = DriverHTTP(
            self.binding,
            self.prefix,
            read_authorization=lambda: "synthetic-role-read",
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"},
        )
        self.http.opener = self.services.opener(self.binding.origin)

    def ci(self) -> GitHubActionsCI | ForgejoActionsCI:
        if self.provider == "github":
            return GitHubActionsCI(self.http, self.source, self.recipe, self.clock.wall, max_pages=4)
        return ForgejoActionsCI(self.native.forge, self.binding, self.source, self.recipe, self.clock.wall, max_pages=4)

    def repository(self) -> dict[str, object]:
        return {
            "id": 71,
            "full_name": self.binding.namespace,
            "url": self.binding.origin + self.prefix,
            "html_url": self.binding.origin + "/" + self.binding.namespace,
            "clone_url": self.binding.origin + "/" + self.binding.namespace + ".git",
            "owner": {"id": 8},
        }

    def issue(self, number: int, repository: str | None = None) -> dict[str, object]:
        namespace = repository or self.source.namespace
        root = GITHUB + "/repos/" + namespace
        return {
            "id": number * 10,
            "node_id": "native-node:" + str(number),
            "number": number,
            "repository_url": root,
            "url": root + "/issues/" + str(number),
            "html_url": "https://github-public.example/" + namespace + "/issues/" + str(number),
            "state": "open",
            "title": "not authority",
            "body": "prose is not an edge",
            "updated_at": "2026-11-16T00:00:00Z",
        }

    def run(self) -> dict[str, object]:
        sha = "f" * 40 if self.mode == "wrong-sha" else self.head
        if self.provider == "github":
            value: dict[str, object] = {
                "id": self.run_id,
                "run_attempt": 1,
                "workflow_id": 20 if self.mode == "wrong-workflow" else 19,
                "path": self.recipe.workflow_path,
                "head_sha": sha,
                "head_branch": "verify",
                "event": "push",
                "status": "in_progress" if self.mode == "pending" else "completed",
                "conclusion": "failure" if self.mode == "failure" else "success",
                "repository": self.repository(),
                "head_repository": self.repository(),
                "updated_at": self.result_stamp,
            }
            if self.mode == "missing-attempt":
                value.pop("run_attempt")
            if self.mode == "fork":
                value["head_repository"] = {"id": 72, "full_name": "other/fork", "url": GITHUB + "/repos/other/fork"}
            if self.mode == "reusable-workflow":
                value["referenced_workflows"] = [{"path": "other/workflows/build.yml", "sha": HEAD}]
            if self.mode == "qualified-path":
                value["path"] = self.binding.namespace + "/" + self.recipe.workflow_path + "@verify"
            return value
        payload = {"after": sha, "ref": self.recipe.ref, "repository": {"id": 71}}
        if self.mode == "payload-mismatch":
            payload["after"] = "f" * 40
        return {
            "id": self.run_id,
            "workflow_id": "foreign.yml" if self.mode == "wrong-workflow" else "verify.yml",
            "commit_sha": sha,
            "event": "push",
            "trigger_event": "push",
            "event_payload": json.dumps(payload),
            "repository": self.repository(),
            "is_fork_pull_request": self.mode == "fork",
            "need_approval": False,
            "status": "running" if self.mode == "pending" else "failure" if self.mode == "failure" else "success",
            "started": "1970-01-01T00:01:30Z",
            "stopped": "1970-01-01T00:02:00Z" if self.mode == "future-result" else self.result_stamp,
        }

    def jobs(self, resource: str) -> list[dict[str, object]]:
        if self.mode == "missing-job":
            return []
        changed = self.mode == "changed-job" and self.reads[resource] > 1
        if self.provider == "github":
            steps = [
                {"name": name, "number": index, "status": "completed", "conclusion": "success"}
                for index, name in enumerate(("Checkout", "Gate"), 1)
            ]
            if self.mode == "step-unknown":
                steps[1]["conclusion"] = "skipped"
            return [
                {
                    "id": 100 + self.run_id,
                    "run_id": self.run_id + 1 if self.mode == "other-run" else self.run_id,
                    "run_attempt": 2 if changed or self.mode == "wrong-attempt" else 1,
                    "head_sha": self.head,
                    "name": "trusted gate",
                    "url": self.binding.origin + self.prefix + f"/actions/jobs/{100 + self.run_id}",
                    "run_url": self.binding.origin + self.prefix + f"/actions/runs/{self.run_id}",
                    "status": "completed",
                    "conclusion": "skipped"
                    if self.mode == "skipped-job"
                    else "failure"
                    if self.mode == "failure"
                    else "success",
                    "completed_at": "1970-01-01T00:02:00Z" if self.mode == "future-result" else self.result_stamp,
                    "steps": steps,
                }
            ]
        return [
            {
                "id": 100 + self.run_id,
                "run_id": self.run_id + 1 if self.mode == "other-run" else self.run_id,
                "repo_id": 71,
                "owner_id": 8,
                "attempt": 2 if changed else 1,
                "handle": "native-handle-2" if changed else "native-handle-1",
                "task_id": 200 + self.run_id + (1 if changed else 0),
                "name": "trusted gate",
                "status": "skipped"
                if self.mode == "skipped-job"
                else "failure"
                if self.mode == "failure"
                else "success",
                "needs": [],
                "runs_on": ["fixture"],
            }
        ]

    def route(self, method: str, path: str, body: object) -> tuple[int, object] | None:
        if self.provider == "github" and path.startswith("/api/v1"):
            return self.native.route(method, path, body)
        prefix = ("/api/v1" if path.startswith("/api/v1") else "") + self.prefix
        if not path.startswith(prefix):
            return self.native.route(method, path, body)
        assert method == "GET" and body is None
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        resource = parsed.path.removeprefix(prefix)
        self.reads[resource] = self.reads.get(resource, 0) + 1
        self.services.response_headers = {}
        if self.mode == "permission":
            return 403, {}
        if self.mode == "expired":
            self.clock.now += 10
        if resource == "":
            return 200, self.repository()
        if resource == "/issues/42":
            value = self.issue(42)
            if self.mode == "issue-moving" and self.reads[resource] > 1:
                value["body"] = "changed"
            return 200, value
        if resource == "/issues/42/dependencies/blocked_by":
            if self.mode == "dependency-permission":
                return 403, {}
            if self.mode == "dependency-empty":
                return 200, []
            value = self.issue(9 + page - 1 if self.mode == "dependency-capped" else 9, "other/project")
            if self.mode == "dependency-closed":
                value["state"] = "closed"
            if self.mode == "dependency-moving" and self.reads[resource] > 2:
                value["state"] = "closed"
            if self.mode == "dependency-foreign-origin":
                value["repository_url"] = "https://another.example/repos/other/project"
            return 200, [value] if page == 1 or self.mode == "dependency-capped" else []
        if resource == "/actions/runs":
            rows = [self.run()]
            if self.mode == "ambiguous":
                rows.append({**self.run(), "id": 12})
            if self.provider == "forgejo":
                return 200, {"workflow_runs": rows[page - 1 : page], "total_count": len(rows)}
            return 200, {
                "workflow_runs": rows,
                "total_count": len(rows) + (1 if self.mode == "incomplete-count" else 0),
            }
        if resource in {f"/actions/runs/{self.run_id}", f"/actions/runs/{self.run_id}/attempts/1"}:
            value = self.run()
            if self.mode == "future-result" and self.provider == "forgejo":
                value["stopped"] = "1970-01-01T00:02:00Z"
            return 200, value
        if resource in {f"/actions/runs/{self.run_id}/jobs", f"/actions/runs/{self.run_id}/attempts/1/jobs"}:
            rows = self.jobs(resource)
            if self.provider == "forgejo":
                return 200, rows
            return 200, {"jobs": rows, "total_count": len(rows)}
        if resource == "/contents":
            return 200, [
                {"name": ".forgejo", "path": ".forgejo", "type": "dir"},
                {"name": ".github", "path": ".github", "type": "dir"},
            ]
        if resource in {"/contents/.forgejo", "/contents/.github"}:
            directory = resource.removeprefix("/contents/")
            return 200, [{"name": "workflows", "path": directory + "/workflows", "type": "dir"}]
        if resource.startswith("/contents/"):
            assert query.get("ref") == [self.head]
            raw = b"changed workflow" if self.mode == "workflow-changed" else WORKFLOW
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
            return 200, {
                "type": "file",
                "path": resource.removeprefix("/contents/"),
                "encoding": "base64",
                "size": len(raw),
                "content": base64.b64encode(raw).decode(),
                "sha": "f" * 40 if self.mode == "blob-wrong" else blob,
            }
        return 404, {}


@pytest.fixture
def role_peer(request: pytest.FixtureRequest) -> Iterator[RolePeer]:
    value = RolePeer(str(request.param))
    try:
        yield value
    finally:
        value.services.close()


@pytest.mark.parametrize("role_peer", ["github", "forgejo"], indirect=True)
def test_native_execution_identity_excludes_access_metadata(role_peer: RolePeer) -> None:
    ci = role_peer.ci()
    source = DriverSubject(role_peer.source, "commit", HEAD, HEAD)
    fact = ci.build(ci.key, source)
    profile = ci.profile()
    assert profile.execution_identity is not None
    identity = profile.execution_identity(fact)
    subject = fact.subject
    alternate = replace(subject, binding=replace(subject.binding, tenant="other-tenant", rights="other-rights"))
    assert profile.execution_identity(replace(fact, subject=alternate)) == identity
    role_peer.run_id = 12
    distinct = ci.build(ci.key, source)
    assert profile.execution_identity(distinct) != identity


@pytest.mark.parametrize("role_peer", ["github", "forgejo"], indirect=True)
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "failure",
        "pending",
        "missing-job",
        "skipped-job",
        "wrong-sha",
        "other-run",
        "changed-job",
        "fork",
        "ambiguous",
        "permission",
        "workflow-changed",
        "blob-wrong",
        "future-result",
        "expired",
    ],
)
def test_native_actions_role_binds_profile_source_workflow_and_job_results(role_peer: RolePeer, mode: str) -> None:
    role_peer.mode = mode
    ci = role_peer.ci()
    source = DriverSubject(role_peer.source, "ref", "develop", HEAD)
    result = ci.baseline().read_baseline(source, digest(encode(role_peer.recipe.recipes)))
    if mode in {"success", "failure"}:
        assert result is not None and result.result == ("passed" if mode == "success" else "failed")
        assert result.observed_at == 95
        proof = json.loads(result.proof)
        assert "actual-worktree" in proof["qualification"]
        native = json.loads(bytes.fromhex(proof["native"]))
        assert native["jobs"][0]["run_id"] == 11 and native["workflow"]["sha256"] == digest(WORKFLOW)
    else:
        assert result is None
        if mode == "permission":
            fact = ci.build(ci.key, DriverSubject(role_peer.source, "commit", HEAD, HEAD))
            assert fact.presence is Presence.INACCESSIBLE
    assert all(request[0] == "GET" for request in role_peer.services.requests)


@pytest.mark.parametrize(
    "mode",
    ["wrong-attempt", "missing-attempt", "step-unknown", "reusable-workflow", "qualified-path", "incomplete-count"],
)
def test_github_exact_attempt_steps_and_path_forms(mode: str) -> None:
    peer = RolePeer("github", mode)
    try:
        ci = peer.ci()
        result = ci.baseline().read_baseline(
            DriverSubject(peer.source, "ref", "develop", HEAD), digest(encode(peer.recipe.recipes))
        )
        assert (result is not None) is (mode == "qualified-path")
    finally:
        peer.services.close()


def test_forgejo_job_profile_does_not_invent_native_steps_or_payload_consistency() -> None:
    peer = RolePeer("forgejo", "payload-mismatch")
    try:
        ci = peer.ci()
        assert (
            ci.baseline().read_baseline(
                DriverSubject(peer.source, "ref", "develop", HEAD), digest(encode(peer.recipe.recipes))
            )
            is None
        )
        before = len(peer.services.requests)
        with pytest.raises(UnsupportedForge, match="step-result API"):
            ForgejoActionsCI(
                peer.native.forge,
                peer.binding,
                peer.source,
                replace(peer.recipe, required_steps=(("trusted gate", ("Gate",)),)),
                peer.clock.wall,
            )
        assert len(peer.services.requests) == before
    finally:
        peer.services.close()


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "dependency-empty",
        "dependency-permission",
        "dependency-moving",
        "dependency-capped",
        "dependency-foreign-origin",
        "issue-moving",
        "expired",
    ],
)
def test_github_work_preserves_visible_scan_and_explicit_unknown_edges(mode: str) -> None:
    peer = RolePeer("github", mode)
    try:
        binding = replace(peer.source, family="work", implementation="github-issues")
        http = DriverHTTP(binding, peer.prefix, headers=peer.http.headers)
        http.opener = peer.services.opener(binding.origin)
        work = GitHubIssues(http, peer.clock.wall, max_pages=3).work("42")
        assert not work.dependencies_complete
        if mode in {
            "success",
            "dependency-empty",
            "dependency-permission",
            "dependency-moving",
            "dependency-capped",
            "issue-moving",
            "expired",
        }:
            assert work.fact.presence is Presence.FOUND
            value = json.loads(work.fact.native)
            assert value["visible_scan_complete"] is (mode in {"success", "dependency-empty"})
            if mode == "success":
                assert work.dependencies[0].target.binding.namespace == "other/project"
                assert value["dependencies"][0]["id"] == 90 and value["dependencies"][0]["number"] == 9
                assert "owner-edge-obligation-required" in work.fact.qualification
        else:
            assert not work.fact.complete
        with pytest.raises(UnsupportedForge):
            GitHubIssues(http, peer.clock.wall).validate_mutation(None)
    finally:
        peer.services.close()


@pytest.mark.parametrize("role_peer", ["github", "forgejo"], indirect=True)
def test_actions_mapping_and_configuration_changes_have_no_implicit_fallback(role_peer: RolePeer) -> None:
    foreign = replace(
        role_peer.native.scm.binding, origin="https://separate-source.example.test", namespace="source/project"
    )

    def factory(source: DriverBinding, mapping: TrustedSCMRemote | None) -> GitHubActionsCI | ForgejoActionsCI:
        return (
            GitHubActionsCI(role_peer.http, source, role_peer.recipe, role_peer.clock.wall, remote_mapping=mapping)
            if role_peer.provider == "github"
            else ForgejoActionsCI(
                role_peer.native.forge,
                role_peer.binding,
                source,
                role_peer.recipe,
                role_peer.clock.wall,
                remote_mapping=mapping,
            )
        )

    with pytest.raises(ValueError, match="explicit trusted remote mapping"):
        factory(foreign, None)
    mapping = TrustedSCMRemote(
        role_peer.binding.origin + "/" + role_peer.binding.namespace + ".git", foreign, "owner:exact-mirror"
    )
    ci = factory(foreign, mapping)
    source = DriverSubject(foreign, "commit", HEAD, HEAD)
    assert ci.build(ci.key, source).complete
    before = len(role_peer.services.requests)
    with pytest.raises(ValueError):
        ci.build(ci.key, DriverSubject(foreign, "commit", "verify", "verify"))
    with pytest.raises(UnsupportedForge):
        ci.validate_mutation(None)
    assert len(role_peer.services.requests) == before
    ci.recipe = replace(role_peer.recipe, decision_reference="changed-policy")
    assert not ci.build(ci.key, source).complete
    assert len(role_peer.services.requests) == before


def test_real_github_work_is_independent_from_real_forgejo_source_and_ci() -> None:
    peer = RolePeer("github")
    try:
        binding = replace(peer.source, family="work", implementation="github-issues")
        http = DriverHTTP(binding, peer.prefix, headers=peer.http.headers)
        http.opener = peer.services.opener(binding.origin)
        project = ProjectDrivers(GitHubIssues(http, peer.clock.wall), peer.native.scm, peer.native.ci, ())
        work, facts, reviews = project.observe("42", "develop", "7", HEAD, ())
        assert work.fact.subject.binding.origin == GITHUB and not work.dependencies_complete
        assert reviews.complete and work.fact.subject.binding.origin != project.scm.binding.origin
        for fact in facts:
            fact.require_current(100)
            assert fact.complete
    finally:
        peer.services.close()


@pytest.mark.parametrize("mode", ["success", "dependency-closed"])
def test_github_visible_edges_need_exact_owner_scope_and_closed_is_not_accepted(mode: str) -> None:
    peer = RolePeer("github", mode)
    try:
        binding = replace(peer.source, family="work", implementation="github-issues")
        http = DriverHTTP(binding, peer.prefix, headers=peer.http.headers)
        http.opener = peer.services.opener(binding.origin)
        work = GitHubIssues(http, peer.clock.wall).work("42")
        classification = tuple(sorted((edge.identity, True) for edge in work.dependencies))
        with pytest.raises(ValueError, match="visible-scope obligation"):
            QualifiedDependencyCut.capture(work, classification, 100)
        obligation = EdgeObligation(
            work.fact.subject,
            driver_digest([asdict(edge) for edge in work.dependencies]),
            "owner:exact-visible-native-cut",
            120,
        )
        cut = QualifiedDependencyCut.capture(work, classification, 100, obligation=obligation)
        assert cut.assurance == "visible-scope-obligation" and cut.fresh_until == 120
        assert cut.required == (work.dependencies[0].target,)
        assert cut.required[0].key == "9"  # Even a native closed target stays a Kernel predecessor.
        with pytest.raises(ValueError, match="unavailable or stale"):
            cut.recheck(work, 121)
    finally:
        peer.services.close()


def test_forgejo_workflow_directory_precedence_is_not_filename_equivalence() -> None:
    peer = RolePeer("forgejo")
    try:
        peer.recipe = replace(peer.recipe, workflow_path=".github/workflows/verify.yml")
        ci = peer.ci()
        assert not ci.build(ci.key, DriverSubject(peer.source, "commit", HEAD, HEAD)).complete
    finally:
        peer.services.close()


@pytest.mark.parametrize("role_peer", ["github", "forgejo"], indirect=True)
def test_native_attempt_units_are_preserved_not_converted(role_peer: RolePeer) -> None:
    role_peer.recipe = replace(role_peer.recipe, required_jobs=("trusted gate", "second gate"))
    original = role_peer.jobs

    def distinct_attempts(resource: str) -> list[dict[str, object]]:
        first = original(resource)[0]
        second = {**first, "id": 102, "name": "second gate"}
        if role_peer.provider == "github":
            second.update({"run_attempt": 3, "url": role_peer.binding.origin + role_peer.prefix + "/actions/jobs/102"})
        else:
            second.update({"attempt": 3, "handle": "native-handle-three", "task_id": 203})
        return [first, second]

    ci = role_peer.ci()
    with patch.object(role_peer, "jobs", side_effect=distinct_attempts):
        result = ci.baseline().read_baseline(
            DriverSubject(role_peer.source, "ref", "develop", HEAD), digest(encode(role_peer.recipe.recipes))
        )
    if role_peer.provider == "github":
        assert result is None
    else:
        assert result is not None
        native = json.loads(bytes.fromhex(json.loads(result.proof)["native"]))
        assert [job["attempt"] for job in native["jobs"]] == [1, 3]
        assert "run_attempt" not in native["run"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipes", ["mutable"]),
        ("required_jobs", "trusted gate"),
        ("required_steps", (("trusted gate", "Gate"),)),
        ("workflow_path", "../workflows/verify.yml"),
        ("workflow_sha256", "unknown"),
        ("ref", "verify"),
        ("repository_id", True),
        ("visibility_reference", ""),
    ],
)
def test_actions_configuration_is_typed_finite_and_explicit(field: str, value: object) -> None:
    valid = ActionsRecipe(
        71,
        "19",
        ".github/workflows/verify.yml",
        digest(WORKFLOW),
        "refs/heads/verify",
        ("recipe",),
        ("trusted gate",),
        (),
        "owner:recipe",
        "owner:visibility",
    )
    with pytest.raises(ValueError):
        replace(valid, **{field: value})
