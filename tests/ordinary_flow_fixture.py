# SPDX-License-Identifier: Apache-2.0
"""Deterministic native peers for the installed ordinary application reception.

Kernel application, Git/HTTP clients, source collection, command owners, Codex
Runtime/Stdio and native Kilo review are production implementations. Only external
Forgejo/Codex/model peers are synthetic; Kilo sessions are real native sessions.
"""

import base64
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from native_kilo_fixture import review_peer

from creatidy_kernel.adapters.codex_runtime import CodexOwnedSessionWitness
from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_baseline import ForgejoBaseline
from creatidy_kernel.adapters.forgejo_discovery import ForgejoDiscovery, ScanStatus
from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport
from creatidy_kernel.adapters.intake import GitContentSource
from creatidy_kernel.adapters.kilo_review import NativeKiloReview
from creatidy_kernel.adapters.ordinary_intake import OwnerReviewedForgejoEvidence
from creatidy_kernel.adapters.ordinary_managed import OrdinaryManagedRun
from creatidy_kernel.adapters.ordinary_sync import OrdinaryForgejoSync
from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import VerificationCommand
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import AuthorityEnvelope, BudgetPolicy, DomainAction, PolicyReference, ProgramStatus
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.intake import Declaration, Disposition, IntakeRefused, IssueSubject, OwnerPolicy, encode
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode
from creatidy_kernel.core.verification import VerificationPolicy
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.intake import OrdinaryIntake
from creatidy_kernel.ports.ordinary import OrdinaryHandoff, OrdinaryProgram
from creatidy_kernel.ports.ownership import WorkOwnership

CODEX_PEER = """import json, pathlib, sys
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.99.1"); raise SystemExit
cwd = None
for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    if method == "initialized": continue
    if method == "initialize": result = {"userAgent": "deterministic-owned-peer"}
    elif method == "thread/start":
        cwd = pathlib.Path(message["params"]["cwd"])
        result = {"thread": {"id": "fixture-thread"}, "model": "fixture-model", "modelProvider": "fixture-provider"}
    elif method == "turn/start":
        context = json.loads(message["params"]["input"][0]["text"])
        unit = context["work_unit"]
        (cwd / (unit + ".py")).write_text("VALUE = " + ("1" if unit == "alpha" else "2") + "\\n")
        result = {"turn": {"id": "fixture-turn"}}
    elif method == "thread/read":
        result = {"thread": {"id": "fixture-thread", "turns": [{"id": "fixture-turn", "status": "completed"}]}}
    elif method == "turn/interrupt": result = {}
    else: raise RuntimeError(method)
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
"""


@contextmanager
def forge_service(
    base: str,
    *,
    lose_comment_reply: bool = False,
    unrelated_branch: bool = False,
    lose_pr_reply: bool = False,
    copied_marker: bool = False,
    diamond: bool = False,
) -> Generator[tuple[str, dict[str, object]]]:
    created = datetime.now(UTC).isoformat()
    prs: list[dict[str, object]] = []
    if unrelated_branch:
        prs.append(
            {
                "number": 1,
                "state": "open",
                "merged": False,
                "updated_at": created,
                "title": "Unrelated branch change",
                "body": "unrelated work",
                "head": {"sha": base, "ref": "feature/unrelated", "repo": {"full_name": "team/project"}},
                "base": {"sha": base, "ref": "develop", "repo": {"full_name": "team/project"}},
            }
        )
    state: dict[str, object] = {
        "prs": prs,
        "comments": [],
        "sends": [],
        "base": base,
        "lost_replies": 0,
        "lost_pr_replies": 0,
    }

    def issue(number: int) -> dict[str, object]:
        return {
            "number": number,
            "title": f"Everyday obligation {number}",
            "body": "Owner-selected ordinary work.",
            "state": "open",
            "updated_at": created,
            "html_url": f"https://forge.invalid/team/project/issues/{number}",
        }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, value: object, status: int = 200) -> None:
            raw = encode(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path.removeprefix("/api/v1/repos/team/project")
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            if not path:
                self.reply({"full_name": "team/project", "permissions": {"admin": True, "push": True, "pull": True}})
            elif path == "/issues":
                self.reply([issue(number) for number in range(731, 735 if diamond else 733)] if page == 1 else [])
            elif re.fullmatch(r"/issues/73[1234]", path):
                self.reply(issue(int(path.rsplit("/", 1)[1])))
            elif path.endswith("/dependencies"):
                dependencies = {732: [731], 733: [731], 734: [732, 733]} if diamond else {732: [731]}
                number = int(path.split("/")[2])
                self.reply([issue(item) for item in dependencies.get(number, [])] if page == 1 else [])
            elif path.endswith("/comments"):
                number = int(path.split("/")[2])
                self.reply(
                    [
                        item
                        for item in cast(list[dict[str, object]], state["comments"])
                        if item["issue_url"] == f"https://forge.invalid/api/v1/repos/team/project/issues/{number}"
                    ]
                    if page == 1
                    else []
                )
            elif path.startswith("/commits/") and path.endswith("/statuses"):
                self.reply(
                    [{"id": 1, "context": "ci/baseline", "status": "success", "created_at": created}]
                    if page == 1
                    else []
                )
            elif path == "/branches/develop":
                self.reply({"name": "develop", "commit": {"id": state["base"]}})
            elif path == "/pulls":
                self.reply(state["prs"] if page == 1 else [])
            elif path.startswith("/pulls/"):
                number = int(path.rsplit("/", 1)[1])
                self.reply(cast(list[dict[str, object]], state["prs"])[number - 1])
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            value = cast(dict[str, object], json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            cast(list[object], state["sends"]).append({"path": self.path, "body": value})
            if self.path == "/fixture/agit":
                prs = cast(list[dict[str, object]], state["prs"])
                number = len(prs) + 1
                description = cast(str, value["description"])
                prs.append(
                    {
                        "number": number,
                        "state": "open",
                        "title": value["title"],
                        "body": base64.b64decode(description.removeprefix("{base64}")).decode(),
                        "head": {
                            "sha": value["head"],
                            "ref": f"refs/pull/{number}/head",
                            "repo": {"full_name": "team/project"},
                        },
                        "base": {"sha": state["base"], "ref": "develop", "repo": {"full_name": "team/project"}},
                    }
                )
                if copied_marker and number == 2:
                    prs[0]["body"] = prs[-1]["body"]
                if lose_pr_reply and state["lost_pr_replies"] == 0:
                    state["lost_pr_replies"] = 1
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply({"number": number}, 201)
            elif self.path.endswith("/comments"):
                number = int(self.path.split("/")[-2])
                comments = cast(list[dict[str, object]], state["comments"])
                row = {
                    "id": len(comments) + 1,
                    "body": value["body"],
                    "user": {"login": "fixture-owner"},
                    "issue_url": f"https://forge.invalid/api/v1/repos/team/project/issues/{number}",
                }
                comments.append(row)
                if lose_comment_reply and state["lost_replies"] == 0:
                    state["lost_replies"] = 1
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(row, 201)
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_ordinary_flow(
    root: Path, native_binary: Path | None, *, mode: str, exercise: str = "normal"
) -> dict[str, object]:
    root.mkdir(mode=0o700)
    source = root / "source"
    source.mkdir(mode=0o700)
    reference_git(source, "init", "--initial-branch=develop")
    (source / "base.py").write_text("BASELINE = True\n")
    units = ("alpha", "beta", "gamma", "delta") if exercise in {"diamond", "diamond-conflict"} else ("alpha", "beta")
    for unit in units:
        (source / (unit + ".py")).write_text("VALUE = 0\n")
    reference_git(source, "add", "base.py", *(unit + ".py" for unit in units))
    reference_git(source, "commit", "-m", "Synthetic received baseline")
    repository_url = "https://forge.invalid/team/project.git"
    reference_git(source, "remote", "add", "origin", repository_url)
    base = reference_git(source, "rev-parse", "HEAD").strip()
    protected = root / "owned"
    protected.mkdir(mode=0o700)
    with (
        forge_service(
            base,
            lose_comment_reply=exercise == "lost-reply",
            unrelated_branch=exercise in {"branch-pr", "ambiguous-pr"},
            lose_pr_reply=exercise in {"branch-pr", "ambiguous-pr"},
            copied_marker=exercise == "ambiguous-pr",
            diamond=exercise in {"diamond", "diamond-conflict"},
        ) as (endpoint, native_state),
        review_peer(
            root / "native", native_binary, lose_first_reply=exercise in {"review-lost", "review-lost-reopen"}
        ) as (review_origin, calls),
        ExitStack() as owners,
    ):

        class FixtureTLS(HTTPSHandler):
            def https_open(self, req: Request):
                rewritten = Request(  # noqa: S310 - Endpoint is the synthetic loopback fixture.
                    endpoint
                    + urlsplit(req.full_url).path
                    + ("?" + urlsplit(req.full_url).query if urlsplit(req.full_url).query else ""),
                    data=req.data,
                    headers=dict(req.headers),
                    method=req.method,
                )
                return build_opener(ProxyHandler({})).open(rewritten, timeout=15)

        def http() -> HTTPSForgejoTransport:
            transport = HTTPSForgejoTransport(
                "https://forge.invalid/api/v1", Reference("forgejo:team/project"), lambda: "fixture"
            )
            transport.opener = build_opener(ProxyHandler({}), FixtureTLS())
            return transport

        git_peer = root / "git-peer"
        git_peer.write_text(f"""#!{sys.executable}
import json, subprocess, sys, urllib.request
args = sys.argv[1:]
if "push" not in args:
    raise SystemExit(subprocess.run([{shutil.which("git")!r}, *args]).returncode)
options = {{args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1] for i, arg in enumerate(args) if arg == "-o"}}
sha = args[-1].split(":", 1)[0]
payload = json.dumps({{"title": options["title"], "description": options["description"], "head": sha}}).encode()
request = urllib.request.Request({(endpoint + "/fixture/agit")!r}, data=payload,
                                 headers={{"Content-Type": "application/json"}})
with urllib.request.urlopen(request) as response:
    number = json.load(response)["number"]
print("To " + {repository_url!r})
print("*\\t" + sha + ":refs/pull/" + str(number) + "/head\\t[new reference]")
print("Done")
""")
        git_peer.chmod(0o700)
        read_http = http()
        read_git = ConditionalGitTransport(
            repository=Reference("forgejo:team/project"), remote_url=repository_url, object_source=source / ".git"
        )
        read_git.git_binary = str(git_peer)
        forge = ForgejoForge(read_http, read_git, lambda _: False)
        discovery = ForgejoDiscovery(read_http, lambda: int(time.time()), max_pages=4, freshness_seconds=900)
        assert discovery.discover().status is ScanStatus.OBSERVED
        recipes = ("ordinary:clean-baseline:v1",)
        baseline = ForgejoBaseline(read_http, recipes, frozenset({"ci/baseline"}))
        criteria = tuple(f"{unit}.VALUE is {index + 1}; preserve unrelated modules" for index, unit in enumerate(units))
        subjects = tuple(
            IssueSubject("https://forge.invalid", "team/project", number, "develop", base)
            for number in range(731, 731 + len(units))
        )
        time_offset = [0]
        boot_clock = lambda: BootClockReading(  # noqa: E731 - Controlled fixture clock.
            int(time.time()) + time_offset[0], time.monotonic_ns() + time_offset[0] * 10**9, "ordinary-fixture-boot"
        )
        with SQLiteProgramStore(root / "state.db", boot_clock=boot_clock) as store:
            reader = OwnerReviewedForgejoEvidence(
                store,
                Principal("owner", "owner"),
                forge,
                read_http.binding,
                GitContentSource(source, read_http.binding),
                baseline,
                lambda: int(time.time()),
                max_pages=4,
            )
            budget = WorkBudgetPolicy(
                tuple((kind, 8 if kind is WorkKind.TOOL else 24) for kind in WorkKind), 16, 4_000_000
            )
            policy_ref = PolicyReference("ordinary-received", "v1", "sha256:ordinary-received-v1")
            authority = AuthorityEnvelope(
                "owner", frozenset(DomainAction), 6, frozenset({"verifier"}), frozenset({"worker"})
            )
            owner_policy = OwnerPolicy(
                policy_ref,
                BudgetPolicy(6, 1),
                authority,
                paths=frozenset(unit + ".py" for unit in units),
                network=frozenset(),
                effects=frozenset({"forgejo:pr", "forgejo:receipt"}),
                recipes=frozenset(recipes),
                evidence_producers=frozenset({"forgejo-status-baseline:v13.0.1", reader.producer}),
                freshness_seconds=900,
                work_budget=budget,
                project_id="ordinary-project",
                project_budget=budget,
            )
            decision_clock = lambda: int(time.time()) + time_offset[0]  # noqa: E731 - Controlled fixture clock.
            app = OrdinaryProgram(
                store,
                OrdinaryIntake(store, reader, lambda: int(time.time())),
                discovery,
                Principal("owner", "owner"),
                owner_policy,
                VerificationPolicy(policy_ref, ("paths", "structural", "commands")),
                decision_clock,
                execution_profile=encode(
                    {
                        "harness": "kilo-pull+codex",
                        "provider": "fixture-provider",
                        "model": "fixture-model",
                        "effort": None,
                        "trust": "trusted_development",
                    }
                ),
            )
            selections = tuple(
                (
                    unit,
                    subject,
                    Declaration(
                        encode(
                            {
                                "version": 1,
                                "outcome": "Receive " + unit,
                                "criteria": [criterion],
                                "quality": [],
                                "interface": ["Python module"],
                                "context": [],
                                "unknowns": [],
                                "paths": [unit + ".py", "beta.py"]
                                if exercise == "diamond-conflict" and unit == "gamma"
                                else [unit + ".py"],
                                "effects": ["forgejo:pr", "forgejo:receipt"],
                                "network": [],
                                "recipes": list(recipes),
                                "provenance": ["owner-selected ordinary obligation"],
                            }
                        )
                    ),
                )
                for unit, subject, criterion in zip(units, subjects, criteria, strict=True)
            )
            proposal = app.propose("everyday-program", "Receive two native-dependent obligations", selections)
            assert all(item.evidence.disposition is Disposition.EQUIVALENCE for item in proposal.drafts)
            try:
                app.approve(proposal, decision_id="unresolved-must-not-activate", expires_at=int(time.time()) + 900)
            except IntakeRefused:
                pass
            else:
                raise AssertionError("unresolved source semantics were promoted to execution")
            assert store.find_program("everyday-program") is None
            for unit, subject, declaration in selections:
                snapshot = reader.snapshot(subject, declaration)
                assert (source / (unit + ".py")).read_text() == "VALUE = 0\n"
                reader.review(
                    subject,
                    declaration,
                    expected_snapshot=reader.snapshot_digest(snapshot, declaration),
                    disposition=Disposition.REMAINS,
                    decision_id="owner-source-" + unit,
                    reason=(
                        "Owner inspected pinned existing zero value and complete native PR inventory; "
                        "requested behavior is absent"
                    ),
                )
            if exercise == "owner-binding":
                subject = selections[0][1]
                original = selections[0][2]
                changed_value = dict(original.value)
                changed_value["criteria"] = ["a different condition must hold"]
                changed = Declaration(encode(changed_value))
                assert reader.snapshot(subject, original).digest == reader.snapshot(subject, changed).digest
                assert reader.read(subject, changed).disposition is Disposition.EQUIVALENCE
                return {"status": "unassessed"}
            proposal = app.propose("everyday-program", "Receive two native-dependent obligations", selections)
            approved = app.approve(proposal, decision_id="owner-program-v1", expires_at=int(time.time()) + 900)
            assert approved.program.status is ProgramStatus.DRAFT
            assert app.activate(approved).ready_work_unit_ids == ("alpha",)
            publications: list[tuple[OrdinaryForgejoSync, OrdinaryHandoff]] = []
            sessions: list[str] = []
            review_operations: set[str] = set()
            rejected_attempt: str | None = None
            for position, unit in enumerate(("alpha", "alpha", "beta") if exercise == "remediation" else units):
                ordinal = units.index(unit)
                bad_candidate = exercise == "remediation" and position == 0
                leaf = unit + "-" + str(position)
                tree = LocalOwnedWorkspace(protected, leaf, create=True)
                selected = (
                    HandoffMode.MANAGED if mode == "managed" or (mode == "mixed" and ordinal == 1) else HandoffMode.PULL
                )
                command = VerificationCommand(
                    (
                        sys.executable,
                        "-B",
                        "-c",
                        "from pathlib import Path; assert Path("
                        + repr(unit + ".py")
                        + ").read_text() == "
                        + repr("VALUE = " + str(ordinal + 1) + "\n"),
                    ),
                    10,
                )
                workspace = OrdinaryGitWorkspace(
                    store,
                    tree,
                    source,
                    repository_url,
                    proposal,
                    (command,),
                    ("Fixture Owner", "owner@example.invalid"),
                    authority_deadline=approved.expires_at,
                )
                connection: CodexStdio | None = None
                witness = None
                if selected is HandoffMode.MANAGED:
                    peer = root / (unit + "-codex.py")
                    peer.write_text(
                        "#!"
                        + sys.executable
                        + "\n"
                        + (
                            CODEX_PEER.replace('("1" if unit == "alpha" else "2")', '"-1"')
                            if bad_candidate
                            else CODEX_PEER
                        )
                    )
                    peer.chmod(0o700)
                    connection = CodexStdio(
                        (str(peer), "app-server"),
                        "0.99.1",
                        environment={"HOME": str(root), "PATH": os.defpath},
                        schema_methods=frozenset({"thread/start", "turn/start", "thread/read", "turn/interrupt"}),
                        schema_version="0.99.1",
                    )
                    owners.callback(connection.close)
                    witness = CodexOwnedSessionWitness(connection)

                def bind(
                    principal: Principal,
                    current_store: SQLiteProgramStore,
                    current_tree: LocalOwnedWorkspace,
                    current_mode: HandoffMode,
                    current_witness: CodexOwnedSessionWitness | None,
                ) -> WorkOwnership:
                    if current_witness is None:
                        return WorkOwnership(
                            current_store, principal, current_tree, mode=current_mode, profile=COOPERATIVE_PROFILE
                        )
                    return WorkOwnership(
                        current_store,
                        principal,
                        current_tree,
                        mode=current_mode,
                        profile=current_witness.profile,
                        resources=(current_witness.resource,),
                        witness=current_witness,
                    )

                worker = bind(Principal("worker", "worker"), store, tree, selected, witness)
                control = bind(Principal("owner", "owner"), store, tree, selected, witness)
                workspace.control = control
                reviewer = NativeKiloReview(
                    store,
                    review_origin,
                    root,
                    "product-review",
                    lambda _, ws=workspace: ws.review_input(),
                    lambda: int(time.time()),
                    freshness_seconds=900,
                    timeout_seconds=30,
                    authority_deadline=approved.expires_at,
                )
                client = app.client(approved, Principal("worker", "worker"), workspace, worker, control, reviewer)
                command_key = "owner-assignment-" + leaf
                handoff = client.request(
                    command_key, lease_seconds=600, remediation_of=rejected_attempt if unit == "alpha" else None
                )
                assert handoff.attempt.work_unit_id == unit
                assert (
                    client.request(
                        command_key, lease_seconds=600, remediation_of=rejected_attempt if unit == "alpha" else None
                    )
                    == handoff
                )
                if exercise == "reopen" and ordinal == 0:
                    store.close()
                    store = owners.enter_context(SQLiteProgramStore(root / "state.db", boot_clock=boot_clock))
                    reader = OwnerReviewedForgejoEvidence(
                        store,
                        Principal("owner", "owner"),
                        forge,
                        read_http.binding,
                        GitContentSource(source, read_http.binding),
                        baseline,
                        lambda: int(time.time()),
                        max_pages=4,
                    )
                    app = OrdinaryProgram(
                        store,
                        OrdinaryIntake(store, reader, lambda: int(time.time())),
                        discovery,
                        Principal("owner", "owner"),
                        owner_policy,
                        VerificationPolicy(policy_ref, ("paths", "structural", "commands")),
                        lambda: int(time.time()),
                        execution_profile=app.execution_profile,
                    )
                    approved = app.recover("owner-program-v1")
                    assert approved.proposal == proposal
                    tree = LocalOwnedWorkspace(protected, leaf)
                    workspace = OrdinaryGitWorkspace(
                        store,
                        tree,
                        source,
                        repository_url,
                        proposal,
                        (command,),
                        ("Fixture Owner", "owner@example.invalid"),
                        authority_deadline=approved.expires_at,
                    )
                    worker = bind(Principal("worker", "worker"), store, tree, selected, witness)
                    control = bind(Principal("owner", "owner"), store, tree, selected, witness)
                    workspace.control = control
                    reviewer = NativeKiloReview(
                        store,
                        review_origin,
                        root,
                        "product-review",
                        lambda _, ws=workspace: ws.review_input(),
                        lambda: int(time.time()),
                        freshness_seconds=900,
                        timeout_seconds=30,
                        authority_deadline=approved.expires_at,
                    )
                    client = app.client(approved, Principal("worker", "worker"), workspace, worker, control, reviewer)
                    assert client.request(command_key, lease_seconds=600) == handoff
                if connection is not None:
                    managed = OrdinaryManagedRun(client, workspace, connection)
                    managed.start(handoff)
                    if exercise == "stop":
                        settled = managed.request_stop("owner requested STOP")
                        assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                        assert not store.load("everyday-program").satisfactions
                        assert not native_state["sends"]
                        try:
                            client.report(handoff)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("revoked managed authority accepted a later report")
                        return {"status": "stopped", "mode": mode, "settled": settled, "native_sends": 0}
                    assert managed.finish() == "quiescent"
                else:
                    assert client.start(handoff) is None
                    if exercise == "stop":
                        app.stop(approved, "owner requested STOP")
                        settled = False  # Cooperative authority revocation does not prove physical quiescence.
                        assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                        assert not store.load("everyday-program").satisfactions
                        assert not native_state["sends"]
                        try:
                            client.report(handoff)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("revoked pull authority accepted a later report")
                        return {"status": "stopped", "mode": mode, "settled": settled, "native_sends": 0}
                    (tree.path / (unit + ".py")).write_text(
                        "VALUE = " + str(-1 if bad_candidate else ordinal + 1) + "\n"
                    )
                    if exercise == "diamond-conflict" and unit == "gamma":
                        (tree.path / "beta.py").write_text("VALUE = 999\n")
                    client.quiesce(handoff, "authenticated-kilo-sdk-task-scope-closed")
                control.reconcile(
                    handoff.claim,
                    control.observe(handoff.claim),
                    evidence="owner receives exact task-owned bytes after qualified task-scope quiescence",
                )
                outcome = client.report(handoff)
                if exercise in {"review-lost", "review-lost-reopen"} and position == 0:
                    assert outcome == "review_unknown"
                    assert len(calls) == 1
                    time_offset[0] = 61
                    if exercise == "review-lost-reopen":
                        store.close()
                        store = owners.enter_context(SQLiteProgramStore(root / "state.db", boot_clock=boot_clock))
                        reader = OwnerReviewedForgejoEvidence(
                            store,
                            Principal("owner", "owner"),
                            forge,
                            read_http.binding,
                            GitContentSource(source, read_http.binding),
                            baseline,
                            lambda: int(time.time()),
                            max_pages=4,
                        )
                        app = OrdinaryProgram(
                            store,
                            OrdinaryIntake(store, reader, lambda: int(time.time())),
                            discovery,
                            Principal("owner", "owner"),
                            owner_policy,
                            VerificationPolicy(policy_ref, ("paths", "structural", "commands")),
                            decision_clock,
                            execution_profile=encode(
                                {
                                    "harness": "kilo-pull+codex",
                                    "provider": "fixture-provider",
                                    "model": "fixture-model",
                                    "effort": None,
                                    "trust": "trusted_development",
                                }
                            ),
                        )
                        workspace = OrdinaryGitWorkspace(
                            store,
                            tree,
                            source,
                            repository_url,
                            proposal,
                            (command,),
                            ("Fixture Owner", "owner@example.invalid"),
                            authority_deadline=approved.expires_at,
                        )
                        worker = bind(Principal("worker", "worker"), store, tree, selected, witness)
                        control = bind(Principal("owner", "owner"), store, tree, selected, witness)
                        workspace.control = control
                        reviewer = NativeKiloReview(
                            store,
                            review_origin,
                            root,
                            "product-review",
                            lambda _, ws=workspace: ws.review_input(),
                            lambda: int(time.time()),
                            freshness_seconds=900,
                            timeout_seconds=30,
                            authority_deadline=approved.expires_at,
                        )
                        client = app.client(
                            approved, Principal("worker", "worker"), workspace, worker, control, reviewer
                        )
                    outcome = client.report(handoff)
                    assert len(calls) == 1  # Reconciliation must not issue a second native prompt.
                if bad_candidate:
                    assert outcome == "rejected", outcome
                    assert not store.load("everyday-program").satisfactions
                    assert not native_state["sends"]
                    control.release(
                        handoff.claim, evidence="owner retained rejected workspace and reconciled settled effects"
                    )
                    rejected_attempt = handoff.attempt.attempt_id
                    continue
                assert outcome == "accepted", outcome
                assert client.report(handoff) == "accepted"
                review_operation = next(
                    record.operation_id
                    for record in store.operations("product-review:")
                    if record.operation_id not in review_operations
                )
                review_operations.add(review_operation)
                native_session = json.loads(store.artifact(review_operation, "kilo-review-session"))
                sessions.append(cast(str, native_session["session"]))
                pub_git = ConditionalGitTransport(
                    repository=Reference("forgejo:team/project"),
                    remote_url=repository_url,
                    object_source=workspace.objects,
                )
                pub_git.git_binary = str(git_peer)
                publications.append(
                    (OrdinaryForgejoSync(app, approved, control, workspace, http(), pub_git, "fixture-owner"), handoff)
                )
            assert store.load("everyday-program").status is ProgramStatus.COMPLETED
            for sync, raw_handoff in publications:
                handoff = raw_handoff
                first = sync.publish(handoff)
                if exercise == "ambiguous-pr":
                    assert first == ("unknown", "held")
                    assert sync.publish(handoff) == first
                    assert len(cast(list[object], native_state["sends"])) == 1
                    return {"status": "unknown", "native_sends": 1}
                assert all(item.startswith("forgejo:") for item in first), first
                repeated = sync.publish(handoff)
                assert repeated == first, (first, repeated)
            assert len(cast(list[object], native_state["prs"])) == len(units) + (1 if exercise == "branch-pr" else 0)
            assert len(cast(list[object], native_state["comments"])) == len(units)
            assert native_state["lost_replies"] == (1 if exercise == "lost-reply" else 0)
            assert native_state["lost_pr_replies"] == (1 if exercise == "branch-pr" else 0)
            assert len(set(sessions)) == len(units) and all(
                item.startswith("ses_") and item != "ses_fixture" for item in sessions
            )
            assert calls
            return {
                "status": "completed",
                "mode": mode,
                "sessions": sessions,
                "native_kilo": native_binary is not None,
                "review_calls": len(calls),
                "satisfactions": len(store.load("everyday-program").satisfactions),
                "native_sends": len(cast(list[object], native_state["sends"])),
            }
