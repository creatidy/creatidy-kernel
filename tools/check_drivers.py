# SPDX-License-Identifier: Apache-2.0
"""Installed mixed-provider conformance over real bounded transports and local synthetic services."""

import base64
import http.client
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from creatidy_kernel.adapters.driver_http import DriverHTTP, object_value
from creatidy_kernel.adapters.forge_refs import AGitPush, ForgeBinding
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_drivers import ForgejoCI, ForgejoSCM, ForgejoWorkManagement
from creatidy_kernel.adapters.forgejo_transport import HTTPSForgejoTransport, NoRedirectHTTPHandler
from creatidy_kernel.adapters.intake import GitContentSource
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.project_drivers import (
    ConfluenceKnowledge,
    GitHubSCM,
    JenkinsCI,
    JiraWorkManagement,
    RepositoryDocumentation,
)
from creatidy_kernel.adapters.reference import reference_spec
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    FinishAttempt,
    InputBinding,
    SatisfyWorkUnit,
    StartAttempt,
    TrustedSatisfaction,
)
from creatidy_kernel.core.drivers import (
    DriverBinding,
    DriverMutation,
    DriverSubject,
    EdgeObligation,
    driver_bytes,
    driver_digest,
)
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import Effect, EffectStatus, Presence, Reference
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, OwnershipConflict, WorkClaim
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.drivers import DriverApplication, DriverEffectGrant, ProjectDrivers
from creatidy_kernel.ports.ownership import WorkOwnership

HEAD = "a" * 40
BASE = "b" * 40
REMOTE = "https://code.example.test/acme/service.git"


def require(value: object) -> None:
    if not value:
        raise AssertionError("installed driver invariant failed")


def binding(family: str, implementation: str, namespace: str, api: str) -> DriverBinding:
    return DriverBinding(
        family,
        implementation,
        "1",
        f"https://{implementation}.example.test:8443",
        "synthetic-tenant",
        namespace,
        api,
        "owner:synthetic-data-rights-and-native-profile",
    )


class Clock:
    def __init__(self) -> None:
        self.now = 100

    def wall(self) -> int:
        return self.now

    def boot(self) -> BootClockReading:
        return BootClockReading(self.now, self.now * 1_000_000_000, "synthetic-driver-boot")


class SyntheticServices:
    """ONLY the trusted test HTTPS opener rewrites fixed synthetic origins to loopback."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes, str | None]] = []
        self.head = HEAD
        self.reviews: list[dict[str, object]] = [
            {
                "id": 1,
                "commit_id": HEAD,
                "state": "APPROVED",
                "body": "native review",
                "user": {"login": "synthetic-reviewer"},
            }
        ]
        self.version = 19
        self.pages: dict[int, dict[str, object]] = {19: self.page(19, "Guide", "<p>synthetic context</p>", "initial")}
        self.lost: set[str] = set()
        self.filter: Callable[[str, str, object], tuple[int, object] | None] | None = None
        self.after: Callable[[str, str], None] | None = None
        self.client_after: Callable[[str, str], None] | None = None
        self.forge_pr: dict[str, object] | None = None
        services = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                self.serve()

            def do_POST(self) -> None:
                self.serve()

            def do_PUT(self) -> None:
                self.serve()

            def serve(self) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 1_000_000:
                    self.send_error(413)
                    return
                body = self.rfile.read(size)
                services.requests.append((self.command, self.path, body, self.headers.get("Authorization")))
                raw: object = json.loads(body) if body else None
                override = None if services.filter is None else services.filter(self.command, self.path, raw)
                status, data = override if override is not None else services.route(self.command, self.path, raw)
                if services.after is not None:
                    services.after(self.command, self.path)
                if self.command in services.lost:
                    services.lost.remove(self.command)
                    self.close_connection = True
                    return
                content = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("X-Jenkins", "2.479.3")
                if status == 302:
                    self.send_header("Location", "https://unconfigured.example.invalid/stolen")
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        require(not self.thread.is_alive())

    def opener(self, origin: str):
        port = self.server.server_address[1]
        services = self

        class LocalFixtureHTTPS(HTTPSHandler):
            def https_open(self, req: Request) -> http.client.HTTPResponse:
                parsed = urlsplit(req.full_url)
                require(f"https://{parsed.netloc}" == origin)
                request = Request(  # noqa: S310 - Deliberate synthetic loopback service, never product configuration.
                    f"http://127.0.0.1:{port}{parsed.path}{'?' + parsed.query if parsed.query else ''}",
                    data=req.data,
                    headers=dict(req.header_items()),
                    method=req.get_method(),
                )
                request.timeout = getattr(req, "timeout", 15)
                response = self.do_open(http.client.HTTPConnection, request)
                if services.client_after is not None:
                    services.client_after(req.get_method(), parsed.path)
                return response

        return build_opener(LocalFixtureHTTPS(), NoRedirectHTTPHandler())

    @staticmethod
    def page(version: int, title: str, body: str, message: str) -> dict[str, object]:
        return {
            "id": "900",
            "spaceId": "17",
            "status": "current",
            "title": title,
            "version": {
                "number": version,
                "message": message,
                "createdAt": "2026-10-28T00:00:00Z",
                "authorId": "synthetic-confluence-writer",
            },
            "body": {"storage": {"representation": "storage", "value": body}},
        }

    def pull(self) -> dict[str, object]:
        return {
            "number": 7,
            "state": "open",
            "merged": False,
            "updated_at": "2026-10-28T00:00:00Z",
            "head": {"sha": self.head, "ref": "feature", "repo": {"full_name": "acme/service"}},
            "base": {"sha": BASE, "ref": "develop", "repo": {"full_name": "acme/service"}},
        }

    def route(self, method: str, path: str, raw: object) -> tuple[int, object]:
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        name = parsed.path
        if name.startswith("/rest/api/3/issue/"):
            key = name.rsplit("/", 1)[1]
            if key not in {"ENG-42", "uuid-42", "ENG-41", "uuid-41"}:
                return 404, {}
            selected = key in {"ENG-42", "uuid-42"}
            return 200, {
                "id": "uuid-42" if selected else "uuid-41",
                "key": "ENG-42" if selected else "ENG-41",
                "fields": {
                    "summary": "Selected work" if selected else "Native predecessor",
                    "description": {"type": "doc", "version": 1, "content": []},
                    "updated": "2026-10-28T00:00:00.000+0000",
                    "status": {"id": "3" if selected else "6", "name": "In Progress" if selected else "Closed"},
                    "project": {"key": "ENG"},
                    "issuelinks": [
                        {
                            "id": "link-9",
                            "type": {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                            "inwardIssue": {"id": "uuid-41", "key": "ENG-41"},
                        }
                    ]
                    if selected
                    else [],
                },
            }
        if name == "/repos/acme/service":
            return 200, {"full_name": "acme/service"}
        if name == "/repos/acme/service/git/ref/heads/feature":
            return 200, {"ref": "refs/heads/feature", "object": {"type": "commit", "sha": self.head}}
        if name.startswith("/repos/acme/service/git/commits/"):
            return 200, {"sha": name.rsplit("/", 1)[1], "tree": {"sha": BASE}}
        if name == "/repos/acme/service/pulls/7":
            return 200, self.pull()
        if name == "/repos/acme/service/pulls/7/reviews":
            if method == "POST":
                data = object_value(raw)
                require(set(data) == {"commit_id", "event", "body"} and data["event"] == "COMMENT")
                review = {
                    "id": len(self.reviews) + 1,
                    "commit_id": data["commit_id"],
                    "state": "COMMENTED",
                    "body": data["body"],
                    "user": {"login": "synthetic-kernel"},
                }
                self.reviews.append(review)
                return 200, review
            index = int(query.get("page", ["1"])[0]) - 1
            return 200, self.reviews[index : index + 1]  # Server-capped below per_page; must seek empty page.
        if name == "/job/service/job/feature/12/api/json":
            return 200, {
                "number": 12,
                "building": False,
                "result": "SUCCESS",
                "timestamp": 1000,
                "duration": 10,
                "actions": [
                    {
                        "_class": "hudson.plugins.git.util.BuildData",
                        "remoteUrls": [REMOTE],
                        "lastBuiltRevision": {"SHA1": HEAD},
                    }
                ],
                "artifacts": [],
            }
        if name == "/wiki/api/v2/pages/900":
            if method == "PUT":
                data = object_value(raw)
                version = object_value(data["version"])
                if version["number"] != self.version + 1:
                    return 409, {"message": "version conflict"}
                self.version += 1
                self.pages[self.version] = self.page(
                    self.version,
                    cast(str, data["title"]),
                    cast(str, object_value(data["body"])["value"]),
                    cast(str, version["message"]),
                )
                return 200, self.pages[self.version]
            version = int(query.get("version", [str(self.version)])[0])
            return (200, self.pages[version]) if version in self.pages else (404, {})
        prefix = "/api/v1/repos/acme/reference"
        if name == prefix:
            return 200, {"full_name": "acme/reference"}
        if name == prefix + "/branches/develop":
            return 200, {"name": "develop", "commit": {"id": BASE}}
        if name == prefix + "/issues/42":
            return 200, {
                "number": 42,
                "title": "Legacy issue",
                "body": "original bytes",
                "state": "open",
                "updated_at": "2026-10-28T00:00:00Z",
            }
        if name == prefix + "/pulls/7":
            return (200, self.forge_pr) if self.forge_pr is not None else (404, {})
        if name == prefix + f"/commits/{HEAD}/statuses":
            return 200, [{"id": 1, "status": "success", "context": "synthetic-check"}] if query.get("page") == [
                "1"
            ] else []
        return 404, {}


def configured(services: SyntheticServices, clock: Clock, docs: RepositoryDocumentation | None = None):
    bindings = (
        binding("work", "jira-cloud", "ENG", "3"),
        binding("scm", "github", "acme/service", "2026-03-10"),
        binding("ci", "jenkins", "service/feature", JenkinsCI.API_VERSION),
        binding("knowledge", "confluence-cloud", "17", "2"),
    )
    prefixes = ("/rest/api/3/issue", "/repos/acme/service", "/job/service/job/feature", "/wiki/api/v2/pages")
    transports: list[DriverHTTP] = []
    for item, prefix in zip(bindings, prefixes, strict=True):
        headers = (
            {"X-GitHub-Api-Version": item.api_version, "Accept": "application/vnd.github+json"}
            if item.family == "scm"
            else {}
        )
        transport = DriverHTTP(
            item,
            prefix,
            read_authorization=lambda: "Bearer synthetic-read",
            write_authorization=lambda: "Bearer synthetic-write",
            headers=headers,
        )
        transport.opener = services.opener(item.origin)
        transports.append(transport)
    work = JiraWorkManagement(transports[0], clock.wall)
    scm = GitHubSCM(transports[1], clock.wall, writer_login="synthetic-kernel")
    ci = JenkinsCI(transports[2], clock.wall, remote=REMOTE, source_binding=scm.binding)
    knowledge = ConfluenceKnowledge(transports[3], clock.wall, writer_account_id="synthetic-confluence-writer")
    project = ProjectDrivers(work, scm, ci, (knowledge,) if docs is None else (knowledge, docs))
    return project, scm, knowledge, transports


def accepted(store: SQLiteProgramStore, directory: Path) -> tuple[WorkOwnership, WorkClaim]:
    workspace_parent = directory / "workspaces"
    workspace_parent.mkdir(mode=0o700)
    workspace = LocalOwnedWorkspace(workspace_parent, "candidate", create=True)
    program = store.create(reference_spec(), "create")
    budget = WorkBudgetPolicy.basic(10, 10, verifications=5, retained_bytes=1_000_000, auxiliary=10)
    store.bind_work_budget(program.program_id, "synthetic-project", budget, budget, 1000)
    program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
    worker = WorkOwnership(
        store, Principal("worker", "worker"), workspace, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
    )
    owner = WorkOwnership(
        store, Principal("owner", "owner"), workspace, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
    )
    attempt = AttemptSpec(
        "drivers",
        program.program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.resolved_inputs("first"),
        workspace_reference=str(workspace.path),
    )
    claim = worker.claim("claim:drivers", program.revision, attempt, lease_seconds=800)
    program = store.load(program.program_id)
    store.admit(program.program_id, "start", StartAttempt(program.revision, "worker", attempt.attempt_id))
    worker.quiesce(claim, "client:synthetic-task-quiescence")
    worker.submit(claim)
    program = store.load(program.program_id)
    program = store.admit(program.program_id, "finish", FinishAttempt(program.revision, "worker", attempt.attempt_id))
    fact = TrustedSatisfaction(
        "accepted:drivers",
        program.program_id,
        "first",
        program.spec.revision,
        program.spec.digest,
        program.spec.work_unit_applicability_fingerprint("first"),
        "verifier",
        attempt.attempt_id,
        tuple(InputBinding(name, f"artifact:{name}") for name in sorted(program.spec.work_unit("first").outputs)),
    )
    store.admit(program.program_id, "accept", SatisfyWorkUnit(program.revision, "verifier", fact))
    return owner, claim


def effect_grant(claim: WorkClaim, mutation: DriverMutation) -> DriverEffectGrant:
    """Synthetic trusted-host policy, separate from durable Kernel work authority."""
    return DriverEffectGrant(claim, mutation, 1, 1000, threading.Event())


def legacy_conformance(services: SyntheticServices, clock: Clock) -> None:
    repository = Reference("forgejo:acme/reference")
    native_binding = ForgeBinding("https://forgejo.example.test", "acme/reference")

    class AGit:
        binding = native_binding
        supports_conditional_push = True
        supports_agit = True

        def compare_and_push(
            self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
        ) -> EffectStatus:
            return EffectStatus.UNKNOWN

        def create_agit_pr(
            self, repository: Reference, base: str, topic: str, revision: Reference, title: str, description: str
        ) -> AGitPush:
            require(
                repository == Reference("forgejo:acme/reference")
                and base == "develop"
                and revision == Reference("forgejo:" + HEAD)
            )
            services.forge_pr = {
                "number": 7,
                "title": title,
                "body": base64.b64decode(description.removeprefix("{base64}"), validate=True).decode(),
                "head": {"sha": HEAD, "ref": "refs/pull/7/head", "repo": {"full_name": "acme/reference"}},
                "base": {"sha": BASE, "ref": "develop", "repo": {"full_name": "acme/reference"}},
            }
            return AGitPush(EffectStatus.ACCEPTED, Reference("forgejo:acme/reference#7"))

    http = HTTPSForgejoTransport(native_binding.origin + "/api/v1", repository, lambda: "synthetic-forge-read")
    http.opener = services.opener(native_binding.origin)
    forge = ForgejoForge(http, AGit(), lambda effect: effect.operation.operation_id == "legacy:operation")

    def bound(family: str) -> DriverBinding:
        return DriverBinding(
            family,
            "forgejo-facade",
            "1",
            native_binding.origin,
            "synthetic-tenant",
            "acme/reference",
            "1",
            "synthetic-native-rights",
        )

    work = ForgejoWorkManagement(forge, bound("work"), clock.wall)
    scm = ForgejoSCM(forge, bound("scm"), clock.wall)
    ci = ForgejoCI(forge, bound("ci"), scm.binding, clock.wall)
    require(work.work("42").fact.presence is Presence.FOUND)
    require(scm.repository().presence is Presence.FOUND and scm.ref("develop").subject.revision == BASE)
    source = DriverSubject(scm.binding, "commit", HEAD, HEAD)
    require(ci.build(HEAD, source).complete)
    from creatidy_kernel.core.execution import OperationKey

    operation = OperationKey("legacy:operation", "legacy:effect", "legacy:request")
    effect = Effect(
        operation,
        repository,
        "pr",
        "kernel-pr-" + "1" * 32,
        revision=Reference("forgejo:" + HEAD),
        base_branch="develop",
        title="Synthetic legacy PR",
        body="Legacy effect bytes",
        fence=1,
        delivery_attempts=1,
    )
    result = scm.apply_legacy(effect)
    require(result.status is EffectStatus.ACCEPTED and result.reference == Reference("forgejo:acme/reference#7"))
    require(scm.reconcile_legacy(effect, result.reference).status is EffectStatus.ACCEPTED)


def settlement_recovery(directory: Path, *, rejected: bool) -> None:
    """Crash after the real native outcome commit; reopen and repair only its exact settlement."""
    clock = Clock()
    services = SyntheticServices()
    try:
        project, scm, knowledge, transports = configured(services, clock)
        mutation = DriverMutation(
            DriverSubject(knowledge.binding, "page", "900", "19")
            if rejected
            else DriverSubject(scm.binding, "change", "7", HEAD),
            "replace_page" if rejected else "append_review",
            (("actor", "synthetic-confluence-writer"), ("body", "<p>ours</p>"), ("title", "Guide"))
            if rejected
            else (("actor", "synthetic-kernel"), ("body", "Exact settlement recovery")),
            "owner:exact-effect-policy",
        )
        if rejected:

            def competing_update() -> str:
                services.version = 20
                services.pages[20] = services.page(20, "Another owner", "<p>theirs</p>", "independent")
                return "Bearer synthetic-write"

            transports[3].write_authorization = competing_update

        class SettlementCrash(RuntimeError):
            pass

        with SQLiteProgramStore(directory / "recovery.sqlite3", boot_clock=clock.boot) as store:
            owner, claim = accepted(store, directory)
            app = DriverApplication(
                store,
                owner,
                project,
                (scm, knowledge),
                clock.wall,
                lambda original, value: (
                    effect_grant(original, value) if original == claim and value == mutation else None
                ),
            )
            crashed: list[str] = []

            def crash(operation_id: str, evidence: str, *, expected_fence: int | None = None) -> None:
                operation = store.operation(operation_id)
                require(operation_id == "sync:crash" and operation.status == ("rejected" if rejected else "accepted"))
                cached = store.find_artifact(operation_id, "driver-receipt")
                require(cached is not None and expected_fence == operation.fence and operation.attempts == 1)
                native = object_value(json.loads(cached or b"null"))
                require(evidence == "sha256:" + driver_digest(native))
                require(native["status"] == ("stale" if rejected else "accepted"))
                crashed.append(evidence)
                raise SettlementCrash("simulated crash after committed native outcome before resource settlement")

            with patch.object(store, "record_work_settlement", side_effect=crash):
                try:
                    app.synchronize(claim, "sync:crash", "effect:crash", mutation)
                except SettlementCrash:
                    pass
                else:
                    raise AssertionError("original publication did not propagate the settlement crash")
            original = store.operation("sync:crash")
            cached = store.find_artifact("sync:crash", "driver-receipt")
            require(len(crashed) == 1 and cached is not None)
            require(sum(method in {"POST", "PUT"} for method, _, _, _ in services.requests) == 1)
            try:
                owner.release(claim, evidence="must-not-release-missing-settlement")
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("unsettled native result released resources before recovery")
            store.reserve_work(
                claim.attempt.program_id, claim.attempt.attempt_id, "other:unsettled", WorkKind.AUXILIARY
            )

        with SQLiteProgramStore(directory / "recovery.sqlite3", boot_clock=clock.boot) as store:
            workspace = LocalOwnedWorkspace(directory / "workspaces", "candidate")
            owner = WorkOwnership(
                store, Principal("owner", "owner"), workspace, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
            )
            project, scm, knowledge, transports = configured(services, clock)

            def forbidden() -> str:
                raise AssertionError("cached recovery attempted another provider request")

            def no_new_grant(original: WorkClaim, value: DriverMutation) -> DriverEffectGrant | None:
                raise AssertionError("cached recovery requested new publication authority")

            for transport in transports:
                transport.read_authorization = transport.write_authorization = forbidden
            app = DriverApplication(store, owner, project, (scm, knowledge), clock.wall, no_new_grant)
            calls = len(services.requests)
            receipt = app.synchronize(claim, "sync:crash", "effect:crash", mutation)
            require(
                receipt.operation == OperationKey(original.operation_id, original.effect_key, original.request_digest)
            )
            require(receipt.status is (EffectStatus.STALE if rejected else EffectStatus.ACCEPTED))
            require(
                driver_bytes(asdict(receipt)) == cached
                and store.find_artifact("sync:crash", "driver-receipt") == cached
            )
            require(store.operation("sync:crash") == original and len(services.requests) == calls)
            records = store.operations()
            require(app.synchronize(claim, "sync:crash", "effect:crash", mutation) == receipt)
            require(store.operations() == records and len(services.requests) == calls)
            try:
                store.record_work_settlement("sync:crash", crashed[0], expected_fence=original.fence + 1)
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("recovered settlement accepted a different fence")
            try:
                owner.release(claim, evidence="other-operation-is-still-unsettled")
            except OwnershipConflict:
                pass
            else:
                raise AssertionError("receipt recovery silently settled another operation")
            store.record_work_settlement("other:unsettled", "trusted:fixture-other-never-dispatched")
            owner.release(claim, evidence="exact-native-receipt-recovered-and-other-scope-settled")
            require(store.operation("sync:crash") == original and len(services.requests) == calls)
    finally:
        services.close()


def conformance(directory: Path) -> None:
    clock = Clock()
    services = SyntheticServices()
    try:
        source = directory / "source"
        source.mkdir()
        environment = {
            "HOME": str(directory),
            "PATH": os.defpath,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        subprocess.run(["git", "init", "--template=", str(source)], env=environment, check=True, capture_output=True)
        (source / "guide.txt").write_text("Independent repository documentation\n")
        subprocess.run(["git", "-C", str(source), "add", "guide.txt"], env=environment, check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "user.name=Synthetic Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "fixture",
            ],
            env=environment,
            check=True,
            capture_output=True,
        )
        revision = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], env=environment, check=True, capture_output=True, text=True
        ).stdout.strip()
        doc_binding = binding("knowledge", "repository-docs", "acme/service", "git-object-v1")
        docs = RepositoryDocumentation(
            doc_binding, GitContentSource(source, ForgeBinding(doc_binding.origin, doc_binding.namespace)), clock.wall
        )
        project, scm, knowledge, _ = configured(services, clock, docs)
        work, facts, reviews = project.observe(
            "ENG-42",
            "feature",
            "7",
            "12",
            ((knowledge.binding.digest, "900", "19"), (doc_binding.digest, "guide.txt", revision)),
        )
        require(work.fact.presence is Presence.FOUND and len(work.dependencies) == 1 and not work.dependencies_complete)
        require(work.dependencies[0].target.key == "ENG-41" and work.dependencies[0].direction == "is blocked by")
        obligation = EdgeObligation(
            work.fact.subject,
            driver_digest([asdict(edge) for edge in work.dependencies]),
            "owner:exact-edge-policy",
            130,
        )
        obligation.require(work, clock.wall())
        require(all(fact.presence is Presence.FOUND for fact in facts) and reviews.complete and reviews.stable)
        require(facts[4].subject.revision is None and facts[4].related == (facts[3].subject,))
        require(all(item.subject.revision is None and item.related[0].revision == HEAD for item in reviews.items))
        require(b"Independent".hex().encode() in facts[-1].native)
        with SQLiteProgramStore(directory / "drivers.sqlite3", boot_clock=clock.boot) as store:
            owner, claim = accepted(store, directory)
            review = DriverMutation(
                DriverSubject(scm.binding, "change", "7", HEAD),
                "append_review",
                (("actor", "synthetic-kernel"), ("body", "Accepted exact candidate")),
                "owner:effect-policy",
            )
            page = DriverMutation(
                DriverSubject(knowledge.binding, "page", "900", "19"),
                "replace_page",
                (
                    ("actor", "synthetic-confluence-writer"),
                    ("body", "<p>Accepted result context</p>"),
                    ("title", "Guide"),
                ),
                "owner:effect-policy",
            )
            app = DriverApplication(
                store,
                owner,
                project,
                (scm, knowledge),
                clock.wall,
                lambda original, mutation: (
                    effect_grant(original, mutation) if original == claim and mutation in {review, page} else None
                ),
            )
            before = store.load(claim.attempt.program_id)
            receipt = app.synchronize(claim, "sync:review", "effect:review", review)
            require(receipt.status is EffectStatus.ACCEPTED)
            services.lost.add("PUT")
            uncertain = app.synchronize(claim, "sync:page", "effect:page", page)
            require(uncertain.status is EffectStatus.UNKNOWN and store.operation("sync:page").status == "unknown")
            require(store.load(claim.attempt.program_id) == before and services.version == 20)
            calls = len(services.requests)
            require(
                app.synchronize(claim, "sync:review", "effect:review", review) == receipt
                and len(services.requests) == calls
            )
            clock.now += 61
            require(app.synchronize(claim, "sync:page", "effect:page", page).status is EffectStatus.ACCEPTED)
            require(services.version == 20 and len([item for item in services.requests if item[0] == "PUT"]) == 1)
            require(store.operation("sync:page").attempts == 1 and store.load(claim.attempt.program_id) == before)
            original_request = store.operation("sync:review").request_json
            grant = effect_grant(claim, review)

            def revoked_credential() -> str:
                grant.invalidated.set()
                return "Bearer synthetic-write"

            scm.http.write_authorization = revoked_credential
            guarded = DriverApplication(
                store, owner, project, (scm, knowledge), clock.wall, lambda original, value: grant
            )
            mutations = sum(method in {"POST", "PUT"} for method, _, _, _ in services.requests)
            try:
                guarded.synchronize(claim, "sync:revoked-grant", "effect:revoked-grant", review)
            except ValueError:
                pass
            else:
                raise AssertionError("revoked ephemeral grant allowed native send")
            require(store.operation("sync:revoked-grant").status == "unknown")
            require(store.operation("sync:revoked-grant").attempts == 1)
            require(store.find_artifact("sync:revoked-grant", "driver-receipt") is None)
            require(sum(method in {"POST", "PUT"} for method, _, _, _ in services.requests) == mutations)
        with SQLiteProgramStore(directory / "drivers.sqlite3", boot_clock=clock.boot) as reopened:
            require(reopened.operation("sync:review").request_json == original_request)
            require(reopened.operation("sync:page").status == "accepted")
        legacy_conformance(services, clock)
        for rejected in (False, True):
            recovery = directory / ("rejected-settlement" if rejected else "accepted-settlement")
            recovery.mkdir(mode=0o700)
            settlement_recovery(recovery, rejected=rejected)
        require(any(item[3] == "Bearer synthetic-write" for item in services.requests))
    finally:
        services.close()


if __name__ == "__main__":
    conformance(Path(sys.argv[1]))
    print(
        "Installed mixed drivers: native HTTP reads/effects, exact outbox recovery "
        "retained Forgejo facade, ephemeral grant revocation and terminal receipt settlement "
        "crash/reopen recovery passed (synthetic only)."
    )
