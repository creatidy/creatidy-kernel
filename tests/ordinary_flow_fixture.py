# SPDX-License-Identifier: Apache-2.0
"""Deterministic native peers for the installed ordinary application reception.

Kernel application, Git/HTTP clients, source collection, command owners, Codex
Runtime/Stdio and native Kilo review are production implementations. Only external
Forgejo/Codex/model peers are synthetic; Kilo sessions are real native sessions.
"""

import base64
import inspect
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from native_kilo_fixture import review_peer

from creatidy_kernel.adapters.codex_runtime import CodexInputs, CodexOwnedSessionWitness
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
from creatidy_kernel.adapters.served_host import LaunchedOwner, launch_owner
from creatidy_kernel.adapters.served_local import (
    LocalChannelRefused,
    LocalDecisionNotifier,
    LocalOwnerChannel,
    LocalOwnerSession,
    LocalWorkerChannel,
    LocalWorkerSession,
    read_frame,
    write_frame,
)
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import VerificationCommand
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import (
    AttemptSpec,
    AuthorityEnvelope,
    BudgetPolicy,
    DomainAction,
    PauseProgram,
    PolicyReference,
    ProgramSpec,
    ProgramStatus,
    ResumeProgram,
)
from creatidy_kernel.core.execution import Candidate, ExecutionRequest
from creatidy_kernel.core.forge import Reference
from creatidy_kernel.core.intake import (
    Declaration,
    Disposition,
    IntakeEvidence,
    IntakeRefused,
    IssueSubject,
    OwnerPolicy,
    digest,
    encode,
    object_from,
)
from creatidy_kernel.core.ordinary import OrdinaryProposal
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.ownership import COOPERATIVE_PROFILE, HandoffMode, WorkClaim
from creatidy_kernel.core.served import command_prefix
from creatidy_kernel.core.verification import Evidence, EvidenceSubject, VerificationPolicy
from creatidy_kernel.core.work_budget import WorkBudgetPolicy, WorkKind
from creatidy_kernel.ports.application import Collection
from creatidy_kernel.ports.intake import OrdinaryIntake
from creatidy_kernel.ports.ordinary import DependencyReader, OrdinaryApproval, OrdinaryHandoff, OrdinaryProgram
from creatidy_kernel.ports.outcome_export import PRIVATE_METADATA, export_private_outcome
from creatidy_kernel.ports.ownership import WorkOwnership
from creatidy_kernel.ports.program_store import OperationRecord
from creatidy_kernel.ports.served import OwnerProjection

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
    initial_comments: int = 0,
    duplicate_comment: bool = False,
    baseline_status: str = "success",
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
    comments = [
        {
            "id": item,
            "body": f"existing-{item}",
            "user": {"login": "other"},
            "issue_url": "https://forge.invalid/api/v1/repos/team/project/issues/731",
        }
        for item in range(1, initial_comments + 1)
    ]
    state: dict[str, object] = {
        "prs": prs,
        "comments": comments,
        "sends": [],
        "base": base,
        "lost_replies": 0,
        "lost_pr_replies": 0,
        "baseline_created": created,
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
                )
            elif path.startswith("/commits/") and path.endswith("/statuses"):
                self.reply(
                    [{"id": 1, "context": "ci/baseline", "status": baseline_status, "created_at": created}]
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
                if duplicate_comment and len(cast(list[object], state["sends"])) == 2:
                    comments.append({**row, "id": len(comments) + 1})
                if lose_comment_reply and state["lost_replies"] == 0:
                    state["lost_replies"] = 1
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(row, 201)
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def replay_host_after_process_restart(root: Path) -> None:
    """Reconstruct the synthetic host configuration, not the previous process objects."""
    saved = cast(dict[str, object], json.loads((root / "host-replay.json").read_text()))
    budget = WorkBudgetPolicy(tuple((kind, 8 if kind is WorkKind.TOOL else 24) for kind in WorkKind), 16, 4_000_000)
    policy_ref = PolicyReference("ordinary-received", "v1", "sha256:ordinary-received-v1")
    policy = OwnerPolicy(
        policy_ref,
        BudgetPolicy(6, 1),
        AuthorityEnvelope("owner", frozenset(DomainAction), 6, frozenset({"verifier"}), frozenset({"worker"})),
        paths=frozenset({"alpha.py", "beta.py"}),
        network=frozenset(),
        effects=frozenset({"forgejo:pr", "forgejo:receipt"}),
        recipes=frozenset({"ordinary:clean-baseline:v1"}),
        evidence_producers=frozenset({"forgejo-status-baseline:v13.0.1", cast(str, saved["reader_producer"])}),
        freshness_seconds=900,
        work_budget=budget,
        project_id="ordinary-project",
        project_budget=budget,
    )
    script = (
        "import json,pathlib,sys; from creatidy_kernel.adapters.served_host import receive_owner; "
        "from creatidy_kernel.adapters.served_local import write_frame,read_frame; "
        "s=receive_owner(int(sys.argv[-1]),expected_scope='everyday-program',"
        "expected_producer='fixture-producer',expected_recipient='fixture-backend'); "
        "write_frame(s,sys.argv[1].encode(),bound=65536); "
        "pathlib.Path(sys.argv[2]).write_bytes(read_frame(s,bound=1048576)); s.close()"
    )
    with SQLiteProgramStore(root / "state.db") as store:
        assert repr(store.history("everyday-program")) == saved["history"]
        assert repr(store.operations()) == saved["operations"]
        approval_id = command_prefix("everyday-program") + digest(encode(["owner", "host-approval"]))
        unknown_id = command_prefix("everyday-program") + digest(encode(["owner", "uncertain-approval"]))
        receipt_bytes = store.artifact(approval_id, "receipt")
        assert receipt_bytes.hex() == saved["receipt_hex"]
        receipt = object_from(receipt_bytes)
        assert receipt["status"] == "approved" and receipt["decision_id"] == "host-decision"
        assert object_from(store.operation(approval_id).request_json.encode())["request"] == {
            **cast(dict[str, object], saved["request"]),
            "scope": "everyday-program",
            "actor": "owner",
            "proposal": object_from(store.artifact("ordinary-program:approval:host-decision", "record"))["proposal"],
            "policy": policy.digest,
        }
        if saved["boundary"] == "intent":
            unknown = store.operation(unknown_id)
            assert object_from(unknown.request_json.encode())["request"] == {
                **cast(dict[str, object], saved["unknown"]),
                "scope": "everyday-program",
                "actor": "owner",
                "proposal": object_from(store.artifact("ordinary-program:approval:host-decision", "record"))[
                    "proposal"
                ],
                "policy": policy.digest,
            }
            assert store.find_artifact(unknown_id, "receipt") is None
            assert store.find_artifact("ordinary-program:approval:interrupted-decision", "record") is None
        else:
            assert store.find_operation(unknown_id) is None
        app = OrdinaryProgram(
            store,
            cast(OrdinaryIntake, None),
            cast(DependencyReader, None),
            Principal("owner", "owner"),
            policy,
            VerificationPolicy(policy_ref, ("paths", "structural", "commands")),
            lambda: int(time.time()),
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
        approved = app.recover("host-decision")
        session = LocalOwnerSession(
            OwnerProjection(store, "everyday-program"), app, approved, proposal=approved.proposal
        )
        before = (store.history("everyday-program"), store.operations())
        keys = ("request", "unknown") if saved["boundary"] == "intent" else ("request",)
        for index, key in enumerate(keys):
            path = root / f"restart-result-{index}.json"
            launched = launch_owner(
                session,
                [sys.executable, "-I", "-c", script, json.dumps(saved[key]), str(path)],
                environment={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                scope="everyday-program",
                producer="fixture-producer",
                recipient="fixture-backend",
            )
            try:
                assert select.select([launched.channel.fileno()], [], [], 10)[0]
                launched.serve_once()
                assert launched.process.wait(timeout=10) == 0
                result = json.loads(path.read_text())["result"]
                assert result == (receipt if key == "request" else {"status": "unknown", "settlement": "unknown"})
            finally:
                launched.close()
                if launched.process.poll() is None:
                    launched.process.kill()
                launched.process.wait(timeout=10)
        assert (store.history("everyday-program"), store.operations()) == before
        assert store.artifact(approval_id, "receipt") == receipt_bytes
        assert store.find_artifact(unknown_id, "receipt") is None


def run_ordinary_flow(
    root: Path,
    native_binary: Path | None,
    *,
    mode: str,
    exercise: str = "normal",
    crash_host_boundary: str | None = None,
) -> dict[str, object]:
    root.mkdir(mode=0o700)
    source = root / "source"
    source.mkdir(mode=0o700)
    reference_git(source, "init", "--initial-branch=develop")
    (source / "base.py").write_text("BASELINE = True\n")
    units = (
        ("alpha", "beta", "gamma", "delta")
        if exercise in {"diamond", "diamond-conflict", "diamond-inherited"}
        else ("alpha", "beta")
    )
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
            diamond=exercise in {"diamond", "diamond-conflict", "diamond-inherited"},
            initial_comments=31 if exercise == "many-comments" else 241 if exercise == "oversized-comments" else 0,
            duplicate_comment=exercise == "duplicate-comments",
            baseline_status=exercise.removeprefix("baseline-") if exercise.startswith("baseline-") else "success",
        ) as (endpoint, native_state),
        review_peer(
            root / "native",
            native_binary,
            reject_first=exercise
            in {
                "review-remediation",
                "review-remediation-tampered",
                "review-remediation-served",
                "served-report-decision",
                "sdk-review-notify",
                "served-unknown-rejected",
            },
            lose_first_reply=exercise in {"review-lost", "review-lost-reopen", "served-unknown-rejected"},
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
        with SQLiteProgramStore(root / "state.db", boot_clock=boot_clock) as initial_store:
            store: SQLiteProgramStore = initial_store
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
                paths=frozenset(unit + ".py" for unit in units)
                | (frozenset({"renamed.py"}) if exercise == "rename-outside" else frozenset[str]()),
                network=frozenset(),
                effects=frozenset({"forgejo:pr", "forgejo:receipt"}),
                recipes=frozenset(recipes),
                evidence_producers=frozenset({"forgejo-status-baseline:v13.0.1", reader.producer}),
                freshness_seconds=900,
                work_budget=budget,
                project_id="ordinary-project",
                project_budget=budget,
            )
            if exercise.startswith("baseline-"):
                status_row = {
                    "id": 1,
                    "context": "ci/baseline",
                    "status": exercise.removeprefix("baseline-"),
                    "created_at": native_state["baseline_created"],
                }
                owner_policy = replace(
                    owner_policy, repair_baseline_reference="forgejo-statuses:" + digest(encode([status_row]))
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
                                else [unit + ".py", "alpha.py"]
                                if exercise == "diamond-inherited" and unit == "beta"
                                else [unit + ".py", "renamed.py"]
                                if exercise == "rename-outside" and unit == "alpha"
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
                try:
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
                except IntakeRefused:
                    if exercise not in {"baseline-pending", "baseline-unsupported"}:
                        raise
                    assert store.find_program("everyday-program") is None
                    return {"status": "refused"}
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
            if exercise == "served-large-proposal":
                large = app.propose("everyday-program", ('ż"\\' * 150_000), selections)
                raw = encode(large.payload)
                full = json.dumps(
                    {
                        "version": 1,
                        "ok": True,
                        "result": {
                            "version": 1,
                            "scope": large.program_id,
                            "digest": large.digest,
                            "proposal": large.payload,
                        },
                    },
                    sort_keys=True,
                ).encode()
                assert len(full) > 1024 * 1024 and len(raw) > 1024 * 1024
                before = (store.history("everyday-program"), store.operations())
                owner_socket, owner_client = socket.socketpair()
                with owner_socket, owner_client:
                    channel = LocalOwnerChannel(
                        OwnerProjection(store, "everyday-program"), owner_socket, app, None, proposal=large
                    )
                    assembled = bytearray()
                    while len(assembled) < len(raw):
                        offset = len(assembled)
                        request = {
                            "version": 1,
                            "action": "proposal-chunk",
                            "offset": offset,
                            "proposal_digest": None if offset == 0 else large.digest,
                        }
                        write_frame(owner_client, encode(request), bound=65536)
                        channel.serve_once()
                        frame = read_frame(owner_client, bound=1024 * 1024)
                        part = json.loads(frame)["result"]
                        assert part["digest"] == large.digest and part["bytes"] == len(raw)
                        assert part["offset"] == offset
                        assembled.extend(base64.b64decode(part["data_base64"], validate=True))
                        assert part["complete"] == (len(assembled) == len(raw))
                    assert bytes(assembled) == raw and digest(bytes(assembled)) == large.digest
                    write_frame(
                        owner_client,
                        encode(
                            {
                                "version": 1,
                                "action": "proposal-chunk",
                                "offset": len(raw),
                                "proposal_digest": large.digest,
                            }
                        ),
                        bound=65536,
                    )
                    channel.serve_once()
                    assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]["complete"] is True
                assert (store.history("everyday-program"), store.operations()) == before
                for invalid in (
                    {"offset": -1, "proposal_digest": large.digest},
                    {"offset": True, "proposal_digest": large.digest},
                    {"offset": len(raw) + 1, "proposal_digest": large.digest},
                    {"offset": 8192, "proposal_digest": "0" * 64},
                    {"offset": 8192, "proposal_digest": None},
                ):
                    owner_socket, owner_client = socket.socketpair()
                    with owner_socket, owner_client:
                        channel = LocalOwnerChannel(
                            OwnerProjection(store, "everyday-program"), owner_socket, app, None, proposal=large
                        )
                        write_frame(
                            owner_client, encode({"version": 1, "action": "proposal-chunk", **invalid}), bound=65536
                        )
                        try:
                            channel.serve_once()
                        except LocalChannelRefused:
                            pass
                        else:
                            raise AssertionError("invalid proposal chunk accepted")
                owner_socket, owner_client = socket.socketpair()
                with owner_socket, owner_client:
                    changed = replace(large, objective="replacement")
                    channel = LocalOwnerChannel(
                        OwnerProjection(store, "everyday-program"), owner_socket, app, None, proposal=changed
                    )
                    write_frame(
                        owner_client,
                        encode(
                            {"version": 1, "action": "proposal-chunk", "offset": 8192, "proposal_digest": large.digest}
                        ),
                        bound=65536,
                    )
                    try:
                        channel.serve_once()
                    except LocalChannelRefused:
                        pass
                    else:
                        raise AssertionError("changed proposal allowed continuation")
                owner_socket, owner_client = socket.socketpair()
                with owner_socket, owner_client:
                    channel = LocalOwnerChannel(
                        OwnerProjection(store, "everyday-program"), owner_socket, app, None, proposal=large
                    )
                    write_frame(
                        owner_client,
                        encode(
                            {
                                "version": 1,
                                "action": "approve",
                                "command_id": "large",
                                "expected_revision": -1,
                                "decision_id": "large-decision",
                                "expires_at": int(time.time()) + 900,
                                "proposal_digest": digest(bytes(assembled)),
                            }
                        ),
                        bound=65536,
                    )
                    channel.serve_once()
                    assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]["status"] == "approved"
                return {"status": "read", "native_sends": 0}
            if exercise in {"served-host-receipts", "served-host-process-create"}:
                binding = LocalOwnerSession(OwnerProjection(store, "everyday-program"), app, None, proposal=proposal)
                script = (
                    "import json,pathlib,sys; "
                    "from creatidy_kernel.adapters.served_host import receive_owner; "
                    "from creatidy_kernel.adapters.served_local import write_frame,read_frame; "
                    "s=receive_owner(int(sys.argv[-1]),expected_scope='everyday-program',"
                    "expected_producer='fixture-producer',expected_recipient='fixture-backend'); "
                    "write_frame(s,sys.argv[1].encode(),bound=65536); "
                    "\nif sys.argv[3]=='drop':\n s.close(); sys.exit(0)"
                    "\ntry:\n r=json.loads(read_frame(s,bound=1048576,timeout=10))['result']"
                    "\nexcept (ValueError,OSError):\n r={'error':True}"
                    "\npathlib.Path(sys.argv[2]).write_text(json.dumps(r)); s.close()\n"
                )
                serial = 0

                def cleanup(launched: LaunchedOwner) -> None:
                    try:
                        launched.close()
                    finally:
                        if launched.process.poll() is None:
                            launched.process.kill()
                        launched.process.wait(timeout=10)

                def spawn(request: dict[str, object], *, drop: bool = False) -> tuple[LaunchedOwner, Path]:
                    nonlocal serial
                    serial += 1
                    result_path = root / f"host-result-{serial}.json"
                    launched = launch_owner(
                        cast(LocalOwnerSession, binding),
                        [
                            sys.executable,
                            "-I",
                            "-c",
                            script,
                            json.dumps(request),
                            str(result_path),
                            "drop" if drop else "read",
                        ],
                        environment={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                        scope="everyday-program",
                        producer="fixture-producer",
                        recipient="fixture-backend",
                    )
                    owners.callback(cleanup, launched)
                    return launched, result_path

                def finish(launched: LaunchedOwner, result_path: Path, *, refused: bool = False) -> dict[str, object]:
                    try:
                        assert select.select([launched.channel.fileno()], [], [], 10)[0]
                        if refused:
                            try:
                                launched.channel.serve_once()
                            except LocalChannelRefused:
                                pass
                            else:
                                raise AssertionError("invalid command was not refused")
                        else:
                            launched.channel.serve_once()
                        assert launched.process.wait(timeout=10) == 0
                        return cast(dict[str, object], json.loads(result_path.read_text()))
                    finally:
                        launched.close()
                        if launched.process.poll() is None:
                            launched.process.kill()
                            launched.process.wait(timeout=10)

                approval_request: dict[str, object] = {
                    "version": 1,
                    "action": "approve",
                    "command_id": "host-approval",
                    "expected_revision": -1,
                    "decision_id": "host-decision",
                    "expires_at": int(time.time()) + 900,
                    "proposal_digest": proposal.digest,
                }
                if exercise == "served-host-process-create":
                    assert crash_host_boundary in {"receipt", "intent"}
                if exercise == "served-host-process-create" and crash_host_boundary in {"receipt", "intent"}:
                    approval_id = command_prefix("everyday-program") + digest(encode(["owner", "host-approval"]))
                    unknown_id = command_prefix("everyday-program") + digest(encode(["owner", "uncertain-approval"]))
                    boundary_request: dict[str, object] | None = None
                    if crash_host_boundary == "intent":
                        first, path = spawn(approval_request)
                        assert finish(first, path)["status"] == "approved"
                        boundary_request = {
                            "version": 1,
                            "action": "approve",
                            "command_id": "uncertain-approval",
                            "expected_revision": store.load("everyday-program").revision,
                            "decision_id": "interrupted-decision",
                            "expires_at": approval_request["expires_at"],
                            "proposal_digest": proposal.digest,
                        }

                    def crash_at_boundary() -> None:
                        receipt_bytes = store.artifact(approval_id, "receipt")
                        missing = store.find_artifact(unknown_id, "receipt")
                        assert missing is None
                        (root / "host-replay.json").write_text(
                            json.dumps(
                                {
                                    "request": approval_request,
                                    "unknown": boundary_request,
                                    "reader_producer": reader.producer,
                                    "receipt_hex": receipt_bytes.hex(),
                                    "history": repr(store.history("everyday-program")),
                                    "operations": repr(store.operations()),
                                    "boundary": crash_host_boundary,
                                }
                            )
                        )
                        (root / "first-producer.pid").write_text(str(os.getpid()))
                        os._exit(0)

                    interrupted, _ = spawn(
                        approval_request if boundary_request is None else boundary_request, drop=True
                    )
                    assert interrupted.process.wait(timeout=10) == 0
                    assert select.select([interrupted.channel.fileno()], [], [], 10)[0]
                    if crash_host_boundary == "receipt":
                        finalize = SQLiteProgramStore.finalize_artifact

                        def crash_after_receipt(
                            target: SQLiteProgramStore, operation_id: str, name: str, data: bytes
                        ) -> str:
                            result = finalize(target, operation_id, name, data)
                            if operation_id == approval_id and name == "receipt":
                                crash_at_boundary()
                            return result

                        with patch.object(SQLiteProgramStore, "finalize_artifact", crash_after_receipt):
                            interrupted.channel.serve_once()
                    else:
                        intent = SQLiteProgramStore.intent

                        def crash_after_intent(
                            target: SQLiteProgramStore, operation_id: str, effect_key: str, request: dict[str, object]
                        ) -> OperationRecord:
                            result = intent(target, operation_id, effect_key, request)
                            if operation_id == unknown_id:
                                crash_at_boundary()
                            return result

                        with patch.object(SQLiteProgramStore, "intent", crash_after_intent):
                            interrupted.channel.serve_once()
                    raise AssertionError("producer did not exit at the requested durable boundary")
                lost, _ = spawn(approval_request, drop=True)
                try:
                    assert lost.process.wait(timeout=10) == 0
                    assert select.select([lost.channel.fileno()], [], [], 10)[0]
                    try:
                        lost.channel.serve_once()
                    except LocalChannelRefused:
                        pass
                    else:
                        raise AssertionError("closed consumer unexpectedly received its reply")
                finally:
                    lost.close()
                    if lost.process.poll() is None:
                        lost.process.kill()
                        lost.process.wait(timeout=10)
                first, first_path = spawn(approval_request)
                second, second_path = spawn(approval_request)
                mismatched, mismatch_path = spawn({**approval_request, "decision_id": "different"})
                for pending in (first, second, mismatched):
                    assert select.select([pending.channel.fileno()], [], [], 10)[0]
                receipt = finish(first, first_path)
                assert finish(second, second_path) == receipt and receipt["status"] == "approved"
                assert finish(mismatched, mismatch_path, refused=True) == {"error": True}
                approved = app.recover("host-decision")
                app.check(approved)
                revision = store.load("everyday-program").revision
                unknown_request: dict[str, object] = {
                    "version": 1,
                    "action": "approve",
                    "command_id": "uncertain-approval",
                    "expected_revision": revision,
                    "decision_id": "interrupted-decision",
                    "expires_at": approved.expires_at,
                    "proposal_digest": proposal.digest,
                }
                interrupted, path = spawn(unknown_request)
                with patch.object(OrdinaryProgram, "approve", side_effect=OSError("synthetic interruption")):
                    assert finish(interrupted, path, refused=True) == {"error": True}
                replay, path = spawn(unknown_request)
                assert finish(replay, path) == {"status": "unknown", "settlement": "unknown"}
                assert store.load("everyday-program").revision == revision
                binding.revoke()
                replacement = replace(proposal, objective="A different host-bound proposal")
                binding = LocalOwnerSession(
                    OwnerProjection(store, "everyday-program"), app, approved, proposal=replacement
                )
                replay, path = spawn(approval_request)
                assert finish(replay, path) == receipt
                replay, path = spawn(unknown_request)
                assert finish(replay, path) == {"status": "unknown", "settlement": "unknown"}
                changed, path = spawn({**approval_request, "decision_id": "other-decision"})
                assert finish(changed, path, refused=True) == {"error": True}
                assert store.load("everyday-program").revision == revision
                binding.revoke()
                store.close()
                store = owners.enter_context(SQLiteProgramStore(root / "state.db", boot_clock=boot_clock))
                app.store = store
                app.intake.store = store
                reader.store = store
                approved = app.recover("host-decision")
                binding = LocalOwnerSession(
                    OwnerProjection(store, "everyday-program"), app, approved, proposal=approved.proposal
                )
                reopened_history = store.history("everyday-program")
                reopened_operations = store.operations()
                replay, path = spawn(approval_request)
                assert finish(replay, path) == receipt
                replay, path = spawn(unknown_request)
                assert finish(replay, path) == {"status": "unknown", "settlement": "unknown"}
                assert (store.history("everyday-program"), store.operations()) == (
                    reopened_history,
                    reopened_operations,
                )
                final_stop: dict[str, object] = {
                    "version": 1,
                    "action": "stop",
                    "command_id": "final-stop",
                    "expected_revision": revision,
                    "reason": "owner stops work despite unknown approval",
                }
                stopped, path = spawn(final_stop)
                stop_receipt = finish(stopped, path)
                assert stop_receipt["status"] == "accepted" and stop_receipt["authority"] == "historical-only"
                try:
                    app.check(approved)
                except ValueError:
                    pass
                else:
                    raise AssertionError("STOP did not revoke current work authority")
                replay, path = spawn(final_stop)
                assert finish(replay, path) == stop_receipt
                binding.revoke()
                return {"status": "stopped", "native_sends": 0}
            if exercise == "served-owner-multi-client":
                binding = LocalOwnerSession(OwnerProjection(store, "everyday-program"), app, None, proposal=proposal)

                def invoke_owner(arguments: list[str]) -> dict[str, object]:
                    owner_socket, owner_client = socket.socketpair()
                    with owner_socket, owner_client:
                        channel = binding.attach(owner_socket)
                        process = subprocess.Popen(  # noqa: S603 - Fixed installed module, synthetic descriptor only.
                            [
                                sys.executable,
                                "-I",
                                "-m",
                                "creatidy_kernel.adapters.served_client",
                                "--fd",
                                str(owner_client.fileno()),
                                *arguments,
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            pass_fds=(owner_client.fileno(),),
                            env={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                        )
                        try:
                            assert select.select([owner_socket], [], [], 10)[0]
                            channel.serve_once()
                            stdout, stderr = process.communicate(timeout=10)
                            assert process.returncode == 0, stderr.decode(errors="replace")
                            return cast(dict[str, object], json.loads(stdout)["result"])
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.wait(timeout=10)
                            binding.detach(channel)

                inspected = invoke_owner(["proposal"])
                assert inspected["proposal"] == json.loads(encode(proposal.payload))
                assert inspected["digest"] == proposal.digest and inspected["scope"] == proposal.program_id
                first_chunk = invoke_owner(["proposal-chunk", "--offset", "0"])
                assert first_chunk["digest"] == proposal.digest
                assert base64.b64decode(cast(str, first_chunk["data_base64"])) == encode(proposal.payload)[:8192]
                first = invoke_owner(
                    [
                        "approve",
                        "--command-id",
                        "owner-approve",
                        "--expected-revision",
                        "-1",
                        "--decision-id",
                        "owner-decision",
                        "--expires-at",
                        str(int(time.time()) + 900),
                        "--proposal-digest",
                        cast(str, inspected["digest"]),
                    ]
                )
                assert first["status"] == "approved"
                revision = store.load("everyday-program").revision
                stopped = invoke_owner(
                    [
                        "stop",
                        "--command-id",
                        "owner-stop",
                        "--expected-revision",
                        str(revision),
                        "--reason",
                        "owner stopped unstarted work",
                    ]
                )
                assert stopped["status"] == "accepted" and stopped["settlement"] == "unknown"
                assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                binding.revoke()
                return {"status": "stopped", "native_sends": 0}
            if exercise == "approve-revision-race":
                current_call = OrdinaryProgram.current
                altered = [False]

                def create_during_validation(
                    application: OrdinaryProgram,
                    selected: object,
                    expires_at: int,
                    original: Callable[..., None] = current_call,
                    target_store: SQLiteProgramStore = store,
                    flag: list[bool] = altered,
                ) -> None:
                    original(application, selected, expires_at)
                    if not flag[0]:
                        flag[0] = True
                        spec_builder = cast(Callable[..., object], inspect.getattr_static(OrdinaryProgram, "_spec"))
                        target_store.create(
                            cast(ProgramSpec, spec_builder(application, proposal, None)), "concurrent-owner-create"
                        )

                with patch.object(OrdinaryProgram, "current", create_during_validation):
                    try:
                        app.approve(
                            proposal,
                            decision_id="stale-creation",
                            expires_at=int(time.time()) + 900,
                            expected_revision=-1,
                        )
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("approval admitted after another owner created the Program")
                assert altered[0] and store.find_program("everyday-program") is not None
                assert store.find_artifact("ordinary-program:approval:stale-creation", "record") is None
                return {"status": "refused", "native_sends": 0}
            if exercise in {
                "served-owner-approve",
                "served-owner-approve-stale",
                "served-proposal-mismatch",
                "served-proposal-missing",
                "served-proposal-race",
                "served-proposal-unrecorded",
            }:
                owner_socket, owner_client = socket.socketpair()
                with owner_socket, owner_client:
                    server = LocalOwnerChannel(
                        OwnerProjection(store, "everyday-program"), owner_socket, app, None, proposal=proposal
                    )
                    request = {
                        "version": 1,
                        "action": "approve",
                        "command_id": "owner-approval-command",
                        "expected_revision": 0 if exercise == "served-owner-approve-stale" else -1,
                        "decision_id": "served-owner-decision",
                        "expires_at": int(time.time()) + 900,
                        "proposal_digest": proposal.digest,
                    }
                    before_read = (store.history("everyday-program"), store.operations())
                    write_frame(owner_client, encode({"version": 1, "action": "proposal"}), bound=65536)
                    server.serve_once()
                    inspected = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                    assert inspected == {
                        "version": 1,
                        "scope": proposal.program_id,
                        "digest": proposal.digest,
                        "proposal": json.loads(encode(proposal.payload)),
                    }
                    assert (store.history("everyday-program"), store.operations()) == before_read
                    replacement = replace(proposal, objective="A different objective")
                    if exercise == "served-proposal-mismatch":
                        setattr(server, "_proposal", replacement)  # noqa: B010 - Simulate host replacement.
                    if exercise == "served-proposal-unrecorded":
                        setattr(server, "_proposal", replacement)  # noqa: B010 - Simulate host replacement.
                        write_frame(owner_client, encode({"version": 1, "action": "proposal"}), bound=65536)
                        server.serve_once()
                        inspected_replacement = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                        assert inspected_replacement["proposal"] == json.loads(encode(replacement.payload))
                        request["proposal_digest"] = inspected_replacement["digest"]
                    if exercise == "served-proposal-missing":
                        del request["proposal_digest"]
                    wire = json.dumps(request, sort_keys=True).encode()
                    write_frame(owner_client, wire, bound=65536)
                    if exercise in {
                        "served-owner-approve-stale",
                        "served-proposal-mismatch",
                        "served-proposal-missing",
                        "served-proposal-unrecorded",
                    }:
                        try:
                            server.serve_once()
                        except LocalChannelRefused:
                            pass
                        else:
                            raise AssertionError("stale expected revision approved a new Program")
                        assert store.find_program("everyday-program") is None
                        if exercise == "served-proposal-unrecorded":
                            assert (
                                store.find_artifact("ordinary-program:approval:served-owner-decision", "record") is None
                            )
                            retry_owner, retry_peer = socket.socketpair()
                            with retry_owner, retry_peer:
                                retry = LocalOwnerChannel(
                                    OwnerProjection(store, "everyday-program"),
                                    retry_owner,
                                    app,
                                    None,
                                    proposal=replacement,
                                )
                                write_frame(retry_peer, wire, bound=65536)
                                try:
                                    retry.serve_once()
                                except LocalChannelRefused:
                                    pass
                                else:
                                    raise AssertionError("unrecorded proposal retry did not refuse")
                            assert not store.operations("served-command:")
                        else:
                            assert not store.operations("served-command:")
                        return {"status": "refused", "native_sends": 0}
                    if exercise == "served-proposal-race":
                        original_current = OrdinaryProgram.current

                        def replace_during_validation(
                            application: OrdinaryProgram, selected: OrdinaryProposal, expiry: int
                        ) -> None:
                            original_current(application, selected, expiry)
                            setattr(server, "_proposal", replacement)  # noqa: B010 - Reentrant host replacement.

                        with patch.object(OrdinaryProgram, "current", replace_during_validation):
                            server.serve_once()
                    else:
                        server.serve_once()
                    result = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                    assert result["status"] == "approved"
                    if exercise == "served-proposal-race":
                        assert app.recover("served-owner-decision").proposal.digest == proposal.digest
                        assert store.load("everyday-program").spec.objective == proposal.objective
                        return {"status": "approved", "native_sends": 0}
                    approved_program = store.load("everyday-program")
                    write_frame(owner_client, wire, bound=65536)
                    server.serve_once()
                    assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"] == result
                    assert store.load("everyday-program") == approved_program
                    request["expires_at"] = cast(int, request["expires_at"]) + 1
                    write_frame(owner_client, json.dumps(request).encode(), bound=65536)
                    try:
                        server.serve_once()
                    except LocalChannelRefused:
                        pass
                    else:
                        raise AssertionError("changed approval replay was accepted")
                    return {"status": "approved", "native_sends": 0}
            if exercise == "approve-expected":
                try:
                    app.approve(
                        proposal, decision_id="wrong-expected", expires_at=int(time.time()) + 900, expected_revision=0
                    )
                except IntakeRefused:
                    pass
                else:
                    raise AssertionError("approval with stale caller expectation was accepted")
                assert store.find_program("everyday-program") is None
                app.approve(
                    proposal, decision_id="expected-absent", expires_at=int(time.time()) + 900, expected_revision=-1
                )
                return {"status": "approved", "native_sends": 0}
            approved = app.approve(proposal, decision_id="owner-program-v1", expires_at=int(time.time()) + 900)
            if exercise == "served-owner-interleaved-amend":
                amended = app.propose("everyday-program", "A revised owner-approved objective", selections)
                binding = LocalOwnerSession(OwnerProjection(store, "everyday-program"), app, approved, proposal=amended)
                first_owner, first_peer = socket.socketpair()
                second_owner, second_peer = socket.socketpair()
                with first_owner, first_peer, second_owner, second_peer:
                    first = binding.attach(first_owner)
                    second = binding.attach(second_owner)
                    write_frame(
                        second_peer,
                        encode(
                            {
                                "version": 1,
                                "action": "approve",
                                "command_id": "amend-interleaved",
                                "expected_revision": approved.program.revision,
                                "decision_id": "amended-v2",
                                "expires_at": approved.expires_at,
                                "proposal_digest": amended.digest,
                            }
                        ),
                        bound=65536,
                    )
                    second.serve_once()
                    assert json.loads(read_frame(second_peer, bound=1024 * 1024))["result"]["status"] == "approved"
                    binding.detach(second)
                    write_frame(first_peer, encode({"version": 1, "action": "snapshot"}), bound=65536)
                    first.serve_once()
                    read_frame(first_peer, bound=1024 * 1024)
                    binding.detach(first)
                final_owner, final_peer = socket.socketpair()
                with final_owner, final_peer:
                    final = binding.attach(final_owner)
                    write_frame(
                        final_peer,
                        encode(
                            {
                                "version": 1,
                                "action": "stop",
                                "command_id": "stop-interleaved",
                                "expected_revision": store.load("everyday-program").revision,
                                "reason": "owner stops amended program",
                            }
                        ),
                        bound=65536,
                    )
                    final.serve_once()
                    assert json.loads(read_frame(final_peer, bound=1024 * 1024))["result"]["status"] == "accepted"
                    binding.detach(final)
                assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                return {"status": "stopped", "native_sends": 0}
            if exercise == "served-owner-amend":
                amended = app.propose("everyday-program", "A revised owner-approved objective", selections)
                owner_socket, owner_client = socket.socketpair()
                with owner_socket, owner_client:
                    server = LocalOwnerChannel(
                        OwnerProjection(store, "everyday-program"), owner_socket, app, approved, proposal=amended
                    )
                    request = {
                        "version": 1,
                        "action": "approve",
                        "command_id": "owner-amend-command",
                        "expected_revision": approved.program.revision,
                        "decision_id": "owner-amend-v1",
                        "expires_at": approved.expires_at,
                        "proposal_digest": amended.digest,
                    }
                    wire = json.dumps(request, sort_keys=True).encode()
                    write_frame(owner_client, wire, bound=65536)
                    server.serve_once()
                    result = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                    assert result["status"] == "approved" and result["expires_at"] == approved.expires_at
                    current = store.load("everyday-program")
                    assert current.spec.digest != approved.program.spec.digest
                    write_frame(owner_client, wire, bound=65536)
                    server.serve_once()
                    assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"] == result
                    assert store.load("everyday-program") == current
                    return {"status": "amended", "native_sends": 0}
            if exercise in {"baseline-pending", "baseline-unsupported"}:
                raise AssertionError("unresolved baseline was authorized as a failing baseline")
            if exercise in {"baseline-failure", "baseline-error"}:
                return {"status": "approved"}
            assert approved.program.status is ProgramStatus.DRAFT
            assert app.activate(approved).ready_work_unit_ids == ("alpha",)
            if exercise in {"served-owner-stop", "served-owner-stale"}:
                before = store.load("everyday-program")
                owner_socket, owner_client = socket.socketpair()
                with owner_socket, owner_client:
                    server = LocalOwnerChannel(OwnerProjection(store, "everyday-program"), owner_socket, app, approved)
                    request = {
                        "version": 1,
                        "action": "stop",
                        "command_id": "owner-stop-1",
                        "expected_revision": before.revision - 1
                        if exercise == "served-owner-stale"
                        else before.revision,
                        "reason": "owner stopped the ordinary Program",
                    }
                    wire = json.dumps(request, sort_keys=True).encode()
                    write_frame(owner_client, wire, bound=65536)
                    if exercise == "served-owner-stale":
                        try:
                            server.serve_once()
                        except LocalChannelRefused:
                            pass
                        else:
                            raise AssertionError("stale owner STOP was accepted")
                        assert store.load("everyday-program") == before
                        assert not store.operations("served-command:")
                        return {"status": "refused", "native_sends": 0}
                    server.serve_once()
                    result = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                    assert result == {
                        "version": 1,
                        "status": "accepted",
                        "settlement": "unknown",
                        "authority": "historical-only",
                    }
                    stopped = store.load("everyday-program")
                    assert stopped.status is ProgramStatus.CANCELLED
                    write_frame(owner_client, wire, bound=65536)
                    server.serve_once()
                    assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"] == result
                    assert store.load("everyday-program") == stopped
                    try:
                        store.check_work_authority("everyday-program")
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("owner STOP did not revoke work authority")
                    assert not native_state["sends"]
                    return {"status": "stopped", "native_sends": 0}
            if exercise == "stop-atomic-failure":
                before = store.load("everyday-program")
                original_write = cast(
                    Callable[..., object], inspect.getattr_static(SQLiteProgramStore, "_write_work_record")
                )

                def fail_revocation(store_arg: SQLiteProgramStore, kind: str, *args: object) -> object:
                    if kind == "revoked":
                        raise RuntimeError("synthetic revocation failure")
                    return original_write(store_arg, kind, *args)

                with patch.object(SQLiteProgramStore, "_write_work_record", fail_revocation):
                    try:
                        app.stop(approved, "owner stopped work")
                    except RuntimeError as error:
                        assert str(error) == "synthetic revocation failure"
                    else:
                        raise AssertionError("revocation failure was not exercised")
                assert store.load("everyday-program") == before
                return {"status": "refused", "native_sends": 0}
            publications: list[tuple[OrdinaryForgejoSync, OrdinaryHandoff]] = []
            sessions: list[str] = []
            review_operations: set[str] = set()
            rejected_attempt: str | None = None
            for position, unit in enumerate(
                ("alpha", "alpha", "beta")
                if exercise
                in {"remediation", "review-remediation", "review-remediation-tampered", "review-remediation-served"}
                else units
            ):
                store = cast(SQLiteProgramStore, store)  # pyright: ignore[reportUnnecessaryCast]
                ordinal = units.index(unit)
                bad_candidate = (
                    exercise in {"remediation", "rejected-replay", "rejected-replay-reopen"} and position == 0
                )
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
                        "print('synthetic-private-command-output'); raise SystemExit(7)"
                        if exercise == "command-export-failed"
                        else "from pathlib import Path; assert Path("
                        + repr(unit + ".py")
                        + ").read_text() == "
                        + repr("VALUE = " + str(ordinal + 1) + "\n"),
                    ),
                    10,
                    2 if exercise == "command-evidence-partial" else 1,
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
                            CODEX_PEER.replace('"status": "completed"', '"status": "inProgress"')
                            if exercise in {"managed-program-stop", "managed-budget-revoke"}
                            else CODEX_PEER.replace('("1" if unit == "alpha" else "2")', '"-1"')
                            if bad_candidate
                            else CODEX_PEER
                        )
                    )
                    if exercise == "review-remediation" and position == 1:
                        peer.write_text(
                            peer.read_text().replace(
                                'unit = context["work_unit"]',
                                'assert context["remediation"]["review_operation_id"].startswith("product-review:")\n'
                                '        assert context["remediation"]["findings"][0]["finding_id"]'
                                ' == "msg_synthetic_result:0"\n'
                                '        assert context["remediation"]["findings"][0]["fingerprint"] == "edge-case"\n'
                                '        assert context["remediation"]["findings"][0]["reason"]'
                                ' == "Implement the approved edge case."\n'
                                '        unit = context["work_unit"]',
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
                if exercise == "served-multi-client":
                    binding = LocalWorkerSession(OwnerProjection(store, "everyday-program"), client)

                    def invoke_local(
                        arguments: list[str], current_binding: LocalWorkerSession = binding
                    ) -> dict[str, object]:
                        owner_socket, worker_socket = socket.socketpair()
                        with owner_socket, worker_socket:
                            channel = current_binding.attach(owner_socket)
                            process = subprocess.Popen(  # noqa: S603 - Fixed module and synthetic descriptor capability.
                                [
                                    sys.executable,
                                    "-I",
                                    "-m",
                                    "creatidy_kernel.adapters.served_client",
                                    "--fd",
                                    str(worker_socket.fileno()),
                                    *arguments,
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                pass_fds=(worker_socket.fileno(),),
                                env={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                            )
                            try:
                                assert select.select([owner_socket], [], [], 10)[0]
                                channel.serve_once()
                                stdout, stderr = process.communicate(timeout=10)
                                assert process.returncode == 0, stderr.decode(errors="replace")
                                return cast(dict[str, object], json.loads(stdout)["result"])
                            finally:
                                if process.poll() is None:
                                    process.kill()
                                    process.wait(timeout=10)
                                current_binding.detach(channel)

                    revision = store.load("everyday-program").revision
                    claimed = invoke_local(
                        [
                            "claim",
                            "--command-id",
                            command_key,
                            "--expected-revision",
                            str(revision),
                            "--lease-seconds",
                            "300",
                        ]
                    )
                    started_revision = store.load("everyday-program").revision
                    started = invoke_local(
                        [
                            "start",
                            "--command-id",
                            "process-start",
                            "--expected-revision",
                            str(started_revision),
                            "--claim-id",
                            command_key,
                        ]
                    )
                    assert claimed["program_id"] == "everyday-program" and started["status"] == "accepted"
                    state = store.load("everyday-program")
                    binding.revoke()
                    other_owner, other_client = socket.socketpair()
                    with other_owner, other_client:
                        try:
                            binding.attach(other_owner)
                        except LocalChannelRefused:
                            pass
                        else:
                            raise AssertionError("revoked worker capability accepted a new channel")
                    assert store.load("everyday-program") == state and state.status is ProgramStatus.ACTIVE
                    return {"status": "checked", "native_sends": 0}
                if exercise in {"served-worker-stop", "served-worker-approve"}:
                    before = store.load("everyday-program")
                    owner_socket, worker_socket = socket.socketpair()
                    with owner_socket, worker_socket:
                        server = LocalWorkerChannel(OwnerProjection(store, "everyday-program"), owner_socket, client)
                        request = {
                            "version": 1,
                            "action": "stop",
                            "command_id": "forged-owner-stop",
                            "expected_revision": before.revision,
                            "reason": "forged owner request",
                        }
                        if exercise == "served-worker-approve":
                            request = {
                                "version": 1,
                                "action": "approve",
                                "command_id": "forged-approval",
                                "expected_revision": before.revision,
                                "decision_id": "forged",
                                "expires_at": int(time.time()) + 60,
                            }
                        write_frame(worker_socket, json.dumps(request).encode(), bound=65536)
                        try:
                            server.serve_once()
                        except LocalChannelRefused:
                            pass
                        else:
                            raise AssertionError("worker capability accepted owner STOP")
                    assert store.load("everyday-program") == before and not store.operations("served-command:")
                    return {"status": "refused", "native_sends": 0}
                if exercise in {"served-stale", "served-role-injection"}:
                    revision = store.load("everyday-program").revision
                    owner_socket, worker_socket = socket.socketpair()
                    with owner_socket, worker_socket:
                        server = LocalWorkerChannel(OwnerProjection(store, "everyday-program"), owner_socket, client)
                        request = {
                            "version": 1,
                            "action": "claim",
                            "command_id": command_key,
                            "expected_revision": revision - 1 if exercise == "served-stale" else revision,
                            "lease_seconds": 300,
                            "remediation_of": None,
                        }
                        if exercise == "served-role-injection":
                            request["role"] = "owner"
                        write_frame(worker_socket, json.dumps(request).encode(), bound=65536)
                        try:
                            server.serve_once()
                        except LocalChannelRefused:
                            pass
                        else:
                            raise AssertionError("stale or role-injected claim was accepted")
                    assert not store.operations("ordinary-client:")
                    assert not store.load("everyday-program").attempts
                    return {"status": "refused", "native_sends": 0}
                if exercise in {
                    "served-claim",
                    "served-claim-process",
                    "served-cross-action-id",
                    "served-start",
                    "served-start-crash",
                    "served-start-expired",
                    "served-quiesce",
                    "served-report",
                    "served-report-decision",
                    "served-report-forged",
                    "served-report-crash",
                    "served-reconcile-race",
                    "served-renew",
                    "served-renew-expired",
                }:
                    revision = store.load("everyday-program").revision
                    owner_socket, worker_socket = socket.socketpair()
                    with owner_socket, worker_socket:
                        notification_rx = None
                        notifier = None
                        if exercise == "served-report-decision":
                            notification_tx, notification_rx = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                            owners.enter_context(notification_tx)
                            owners.enter_context(notification_rx)
                            notifier = LocalDecisionNotifier(notification_tx)
                        server = LocalWorkerChannel(
                            OwnerProjection(store, "everyday-program"), owner_socket, client, notifier=notifier
                        )
                        request = {
                            "version": 1,
                            "action": "claim",
                            "command_id": command_key,
                            "expected_revision": revision,
                            "lease_seconds": 300,
                            "remediation_of": None,
                        }
                        wire = json.dumps(request, sort_keys=True).encode()
                        if exercise == "served-claim-process":
                            process = subprocess.Popen(  # noqa: S603 - Fixed installed module and synthetic capability.
                                [
                                    sys.executable,
                                    "-I",
                                    "-m",
                                    "creatidy_kernel.adapters.served_client",
                                    "--fd",
                                    str(worker_socket.fileno()),
                                    "claim",
                                    "--command-id",
                                    command_key,
                                    "--expected-revision",
                                    str(revision),
                                    "--lease-seconds",
                                    "300",
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                pass_fds=(worker_socket.fileno(),),
                                env={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                            )
                            try:
                                assert select.select([owner_socket], [], [], 10)[0]
                                server.serve_once()
                                stdout, stderr = process.communicate(timeout=10)
                                assert process.returncode == 0, stderr.decode(errors="replace")
                                first = json.loads(stdout)["result"]
                            finally:
                                if process.poll() is None:
                                    process.kill()
                                    process.wait(timeout=10)
                        else:
                            write_frame(worker_socket, wire, bound=65536)
                            server.serve_once()
                            first = json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]
                        assert first["program_id"] == "everyday-program"
                        assert len(store.load("everyday-program").attempts) == 1
                        if exercise == "served-claim-process":
                            process = subprocess.Popen(  # noqa: S603 - Installed module with a host-bound descriptor.
                                [
                                    sys.executable,
                                    "-I",
                                    "-m",
                                    "creatidy_kernel.adapters.served_client",
                                    "--fd",
                                    str(worker_socket.fileno()),
                                    "context",
                                    "--claim-id",
                                    command_key,
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                pass_fds=(worker_socket.fileno(),),
                                env={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                            )
                            try:
                                assert select.select([owner_socket], [], [], 10)[0]
                                server.serve_once()
                                stdout, stderr = process.communicate(timeout=10)
                                assert process.returncode == 0, stderr.decode(errors="replace")
                                context = json.loads(stdout)["result"]
                                assert context["work_unit"] == unit
                                assert context["approved"] == json.loads(encode(proposal.payload))
                                assert isinstance(context["inputs"], list)
                                assert context["remediation"] is None
                            finally:
                                if process.poll() is None:
                                    process.kill()
                                    process.wait(timeout=10)
                        if exercise == "served-cross-action-id":
                            start = {
                                "version": 1,
                                "action": "start",
                                "command_id": command_key,
                                "claim_id": command_key,
                                "expected_revision": store.load("everyday-program").revision,
                            }
                            write_frame(worker_socket, json.dumps(start).encode(), bound=65536)
                            try:
                                server.serve_once()
                            except LocalChannelRefused:
                                pass
                            else:
                                raise AssertionError("claim command identity was reused for start")
                            assert store.load("everyday-program").attempts[0].status.value == "prepared"
                            return {"status": "refused", "native_sends": 0}
                        if exercise in {
                            "served-start",
                            "served-start-crash",
                            "served-start-expired",
                            "served-quiesce",
                            "served-report",
                            "served-report-decision",
                            "served-report-forged",
                            "served-report-crash",
                            "served-reconcile-race",
                            "served-renew",
                            "served-renew-expired",
                        }:
                            start = {
                                "version": 1,
                                "action": "start",
                                "command_id": "local-start-1",
                                "expected_revision": store.load("everyday-program").revision,
                                "claim_id": command_key,
                            }
                            start_wire = json.dumps(start, sort_keys=True).encode()
                            if exercise == "served-start-expired":
                                time_offset[0] = 301
                            write_frame(worker_socket, start_wire, bound=65536)
                            if exercise == "served-start-expired":
                                try:
                                    server.serve_once()
                                except LocalChannelRefused:
                                    pass
                                else:
                                    raise AssertionError("expired claim started work")
                                program = store.load("everyday-program")
                                assert program.attempt(str(first["attempt_id"])).status.value == "prepared"
                                assert store.operation(f"runtime:{first['attempt_id']}").attempts == 0
                                return {"status": "refused", "native_sends": 0}
                            if exercise == "served-start-crash":
                                finalize = SQLiteProgramStore.finalize_artifact

                                def crash_receipt(
                                    store_arg: SQLiteProgramStore,
                                    operation: str,
                                    name: str,
                                    data: bytes,
                                    original: Callable[[SQLiteProgramStore, str, str, bytes], str] = finalize,
                                ) -> str:
                                    if operation.startswith("served-command:") and name == "receipt":
                                        raise RuntimeError("synthetic lost start receipt")
                                    return original(store_arg, operation, name, data)

                                with patch.object(SQLiteProgramStore, "finalize_artifact", crash_receipt):
                                    try:
                                        server.serve_once()
                                    except RuntimeError as error:
                                        assert str(error) == "synthetic lost start receipt"
                                    else:
                                        raise AssertionError("start receipt failure was not exercised")
                                assert store.load("everyday-program").attempts[0].status.value == "executing"
                                store.close()
                                store = owners.enter_context(
                                    SQLiteProgramStore(root / "state.db", boot_clock=boot_clock)
                                )
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
                                    execution_profile=app.execution_profile,
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
                                retry_owner, retry_worker = socket.socketpair()
                                with retry_owner, retry_worker:
                                    retry = LocalWorkerChannel(
                                        OwnerProjection(store, "everyday-program"), retry_owner, client
                                    )
                                    write_frame(retry_worker, wire, bound=65536)
                                    retry.serve_once()
                                    assert json.loads(read_frame(retry_worker, bound=1024 * 1024))["result"] == first
                                    write_frame(retry_worker, start_wire, bound=65536)
                                    retry.serve_once()
                                    assert json.loads(read_frame(retry_worker, bound=1024 * 1024))["result"] == {
                                        "status": "unknown",
                                        "settlement": "unknown",
                                    }
                                assert store.operation(f"runtime:{first['attempt_id']}").attempts == 0
                                return {"status": "unknown", "native_sends": 0}
                            server.serve_once()
                            accepted = json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]
                            assert accepted == {
                                "version": 1,
                                "status": "accepted",
                                "settlement": "unknown",
                                "authority": "historical-only",
                            }
                            after_start = store.load("everyday-program")
                            write_frame(worker_socket, start_wire, bound=65536)
                            server.serve_once()
                            assert json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"] == accepted
                            assert store.load("everyday-program") == after_start
                            if exercise in {"served-renew", "served-renew-expired"}:
                                renewal = {
                                    "version": 1,
                                    "action": "renew",
                                    "command_id": "local-renew-1",
                                    "expected_revision": after_start.revision,
                                    "claim_id": command_key,
                                    "lease_seconds": 120,
                                }
                                renewal_wire = json.dumps(renewal, sort_keys=True).encode()
                                write_frame(worker_socket, renewal_wire, bound=65536)
                                server.serve_once()
                                result = json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]
                                assert result["status"] == "recorded" and result["authority"] == "historical-only"
                                assert result["expires_at"] <= approved.expires_at
                                records = store.operations("work-budget:v1:ownership-renew:")
                                assert len(records) == 1
                                write_frame(worker_socket, renewal_wire, bound=65536)
                                server.serve_once()
                                assert json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"] == result
                                assert store.operations("work-budget:v1:ownership-renew:") == records
                                if exercise == "served-renew-expired":
                                    time_offset[0] = 301
                                    write_frame(worker_socket, renewal_wire, bound=65536)
                                    server.serve_once()
                                    assert json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"] == result
                                    assert store.operations("work-budget:v1:ownership-renew:") == records
                                    renewal["command_id"] = "fresh-renew-after-expiry"
                                    write_frame(worker_socket, json.dumps(renewal).encode(), bound=65536)
                                    try:
                                        server.serve_once()
                                    except LocalChannelRefused:
                                        pass
                                    else:
                                        raise AssertionError("expired claim accepted a fresh renewal")
                                    assert store.operations("work-budget:v1:ownership-renew:") == records
                                return {"status": "checked", "native_sends": 0}
                            if exercise in {
                                "served-report",
                                "served-report-decision",
                                "served-report-forged",
                                "served-report-crash",
                                "served-reconcile-race",
                            }:
                                (tree.path / "alpha.py").write_text("VALUE = 1\n")
                                quiesce = {
                                    "version": 1,
                                    "action": "quiesce",
                                    "command_id": "local-quiesce-report",
                                    "expected_revision": after_start.revision,
                                    "claim_id": command_key,
                                    "receipt": "cooperative-task-scope-complete",
                                }
                                write_frame(worker_socket, json.dumps(quiesce).encode(), bound=65536)
                                server.serve_once()
                                assert (
                                    json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]["settlement"]
                                    == "cooperative-task-scope"
                                )
                                original_handoff = client.request(
                                    command_prefix("everyday-program")
                                    + digest(encode([client.principal.actor_id, command_key])),
                                    lease_seconds=300,
                                    expected_revision=revision,
                                )
                                changed_subject = control.observe(original_handoff.claim)
                                reconcile_owner, reconcile_client = socket.socketpair()
                                with reconcile_owner, reconcile_client:
                                    owner_channel = LocalOwnerChannel(
                                        OwnerProjection(store, "everyday-program"),
                                        reconcile_owner,
                                        app,
                                        approved,
                                        control=control,
                                        handoff=original_handoff,
                                    )
                                    reconcile_request = {
                                        "version": 1,
                                        "action": "reconcile",
                                        "command_id": "owner-reconcile-1",
                                        "expected_revision": after_start.revision,
                                        "subject": changed_subject,
                                        "evidence": "owner inspected exact changed workspace",
                                    }
                                    reconcile_wire = json.dumps(reconcile_request, sort_keys=True).encode()
                                    write_frame(reconcile_client, reconcile_wire, bound=65536)
                                    if exercise == "served-reconcile-race":
                                        observe = WorkOwnership.observe
                                        changed = [False]

                                        def pause_after_reconcile_capture(
                                            current_owner: WorkOwnership,
                                            claim: WorkClaim,
                                            original: Callable[[WorkOwnership, WorkClaim], str] = observe,
                                            target_store: SQLiteProgramStore = store,
                                            flag: list[bool] = changed,
                                        ) -> str:
                                            result = original(current_owner, claim)
                                            if not flag[0]:
                                                flag[0] = True
                                                program = target_store.load("everyday-program")
                                                target_store.admit(
                                                    program.program_id,
                                                    "owner-paused-during-reconcile",
                                                    PauseProgram(program.revision, "owner"),
                                                )
                                            return result

                                        with patch.object(WorkOwnership, "observe", pause_after_reconcile_capture):
                                            try:
                                                owner_channel.serve_once()
                                            except LocalChannelRefused:
                                                pass
                                            else:
                                                raise AssertionError("stale reconciliation was accepted")
                                        assert (
                                            changed[0] and store.load("everyday-program").status is ProgramStatus.PAUSED
                                        )
                                        assert not store.operations("work-budget:v1:ownership-reconcile:")
                                        assert (tree.path / "alpha.py").read_text() == "VALUE = 1\n"
                                        return {"status": "refused", "native_sends": 0}
                                    owner_channel.serve_once()
                                    reconciliation = json.loads(read_frame(reconcile_client, bound=1024 * 1024))[
                                        "result"
                                    ]
                                    assert reconciliation["status"] == "recorded"
                                    write_frame(reconcile_client, reconcile_wire, bound=65536)
                                    owner_channel.serve_once()
                                    assert (
                                        json.loads(read_frame(reconcile_client, bound=1024 * 1024))["result"]
                                        == reconciliation
                                    )
                                submit = {
                                    "version": 1,
                                    "action": "submit",
                                    "command_id": "local-submit-1",
                                    "expected_revision": after_start.revision,
                                    "claim_id": command_key,
                                }
                                write_frame(worker_socket, json.dumps(submit).encode(), bound=65536)
                                server.serve_once()
                                assert (
                                    json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]["status"]
                                    == "submitted"
                                )
                                report = {**submit, "action": "report", "command_id": "local-report-1"}
                                if exercise == "served-report-forged":
                                    report["verdict"] = "approve"
                                    write_frame(worker_socket, json.dumps(report).encode(), bound=65536)
                                    try:
                                        server.serve_once()
                                    except LocalChannelRefused:
                                        pass
                                    else:
                                        raise AssertionError("worker supplied its own approval verdict")
                                    assert not store.load("everyday-program").satisfactions
                                    assert not calls and not native_state["sends"]
                                    return {"status": "refused", "native_sends": 0}
                                report_wire = json.dumps(report, sort_keys=True).encode()
                                write_frame(worker_socket, report_wire, bound=65536)
                                if exercise == "served-report-decision":
                                    server.serve_once()
                                    result = json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]
                                    assert result["status"] == "rejected"
                                    assert notification_rx is not None
                                    assert select.select([notification_rx], [], [], 2)[0]
                                    notification = json.loads(notification_rx.recv(65536))
                                    assert notification["kind"] == "decision-required"
                                    decisions = OwnerProjection(store, "everyday-program").snapshot().decisions
                                    assert any(item.get("status") == "changes_requested" for item in decisions)
                                    write_frame(worker_socket, report_wire, bound=65536)
                                    server.serve_once()
                                    assert json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"] == result
                                    assert not select.select([notification_rx], [], [], 0)[0]
                                    assert len(calls) == 1
                                    return {"status": "rejected", "native_sends": 0}
                                if exercise == "served-report-crash":
                                    finalize = SQLiteProgramStore.finalize_artifact

                                    def lose_receipt(
                                        store_arg: SQLiteProgramStore,
                                        operation: str,
                                        name: str,
                                        data: bytes,
                                        original: Callable[[SQLiteProgramStore, str, str, bytes], str] = finalize,
                                    ) -> str:
                                        if operation.startswith("served-command:") and name == "receipt":
                                            raise RuntimeError("synthetic lost report receipt")
                                        return original(store_arg, operation, name, data)

                                    with patch.object(SQLiteProgramStore, "finalize_artifact", lose_receipt):
                                        try:
                                            server.serve_once()
                                        except RuntimeError as error:
                                            assert str(error) == "synthetic lost report receipt"
                                        else:
                                            raise AssertionError("report receipt failure was not exercised")
                                    assert len(calls) == 1 and len(store.load("everyday-program").satisfactions) == 1
                                    retry_owner, retry_worker = socket.socketpair()
                                    with retry_owner, retry_worker:
                                        retry = LocalWorkerChannel(
                                            OwnerProjection(store, "everyday-program"), retry_owner, client
                                        )
                                        write_frame(retry_worker, wire, bound=65536)
                                        retry.serve_once()
                                        assert (
                                            json.loads(read_frame(retry_worker, bound=1024 * 1024))["result"] == first
                                        )
                                        write_frame(retry_worker, report_wire, bound=65536)
                                        retry.serve_once()
                                        assert json.loads(read_frame(retry_worker, bound=1024 * 1024))["result"] == {
                                            "status": "unknown",
                                            "settlement": "unknown",
                                        }
                                    assert len(calls) == 1 and not native_state["sends"]
                                    return {"status": "unknown", "native_sends": 0}
                                server.serve_once()
                                assert (
                                    json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]["status"]
                                    == "accepted"
                                )
                                assert len(calls) == 1
                                write_frame(worker_socket, report_wire, bound=65536)
                                server.serve_once()
                                assert (
                                    json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]["status"]
                                    == "accepted"
                                )
                                assert len(calls) == 1 and not native_state["sends"]
                                return {"status": "accepted", "native_sends": 0}
                            if exercise == "served-quiesce":
                                quiesce = {
                                    "version": 1,
                                    "action": "quiesce",
                                    "command_id": "local-quiesce-1",
                                    "expected_revision": after_start.revision,
                                    "claim_id": command_key,
                                    "receipt": "cooperative-task-scope-complete",
                                }
                                quiesce_wire = json.dumps(quiesce, sort_keys=True).encode()
                                write_frame(worker_socket, quiesce_wire, bound=65536)
                                server.serve_once()
                                result = json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]
                                assert result == {
                                    "version": 1,
                                    "status": "accepted",
                                    "settlement": "cooperative-task-scope",
                                    "authority": "historical-only",
                                }
                                write_frame(worker_socket, quiesce_wire, bound=65536)
                                server.serve_once()
                                assert json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"] == result
                                quiesce["receipt"] = "changed-receipt"
                                write_frame(worker_socket, json.dumps(quiesce).encode(), bound=65536)
                                try:
                                    server.serve_once()
                                except LocalChannelRefused:
                                    pass
                                else:
                                    raise AssertionError("changed quiescence command was accepted")
                                assert not native_state["sends"]
                                return {"status": "checked", "native_sends": 0}
                            start["command_id"] = "different-start-command"
                            start["expected_revision"] = after_start.revision
                            write_frame(worker_socket, json.dumps(start).encode(), bound=65536)
                            try:
                                server.serve_once()
                            except LocalChannelRefused:
                                pass
                            else:
                                raise AssertionError("new command restarted an already executing Attempt")
                            assert store.load("everyday-program") == after_start
                            assert not native_state["sends"]
                            return {"status": "checked", "native_sends": 0}
                        write_frame(worker_socket, wire, bound=65536)
                        server.serve_once()
                        assert json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"] == first
                        assert len(store.load("everyday-program").attempts) == 1
                        request["lease_seconds"] = 299
                        write_frame(worker_socket, json.dumps(request).encode(), bound=65536)
                        try:
                            server.serve_once()
                        except LocalChannelRefused:
                            pass
                        else:
                            raise AssertionError("changed duplicate work request was accepted")
                    assert not native_state["sends"]
                    return {"status": "checked", "native_sends": 0}
                if exercise == "expected-revision-race":
                    current_revision = store.load("everyday-program").revision
                    original_check = OrdinaryProgram.check

                    def reentrant_check(
                        program: OrdinaryProgram,
                        approval: OrdinaryApproval,
                        original: Callable[[OrdinaryProgram, OrdinaryApproval], None] = original_check,
                        target_store: SQLiteProgramStore = store,
                    ) -> None:
                        original(program, approval)
                        current = target_store.load("everyday-program")
                        target_store.admit(
                            current.program_id, "owner-paused-during-check", PauseProgram(current.revision, "owner")
                        )

                    with patch.object(OrdinaryProgram, "check", reentrant_check):
                        try:
                            client.request(command_key, lease_seconds=300, expected_revision=current_revision)
                        except IntakeRefused:
                            pass
                        else:
                            raise AssertionError("caller revision was accepted after reentrant owner mutation")
                    assert store.load("everyday-program").status is ProgramStatus.PAUSED
                    assert not store.operations("ordinary-client:")
                    return {"status": "checked", "native_sends": 0}
                if exercise == "expected-revision":
                    current_revision = store.load("everyday-program").revision
                    try:
                        client.request(command_key, lease_seconds=300, expected_revision=current_revision - 1)
                    except IntakeRefused:
                        pass
                    else:
                        raise AssertionError("stale caller revision was admitted")
                    assert not store.operations("ordinary-client:")
                    first = client.request(command_key, lease_seconds=300, expected_revision=current_revision)
                    assert client.request(command_key, lease_seconds=300, expected_revision=current_revision) == first
                    try:
                        client.request(command_key, lease_seconds=300, expected_revision=current_revision + 1)
                    except IntakeRefused:
                        pass
                    else:
                        raise AssertionError("changed replay revision was accepted")
                    return {"status": "checked", "native_sends": 0}
                if exercise in {"injected-file", "injected-symlink", "injected-hardlink"}:
                    original_git = cast(Callable[..., bytes], inspect.getattr_static(OrdinaryGitWorkspace, "_git"))
                    external = root / "manual-external"
                    external.write_bytes(b"preserve original manual bytes\n")
                    external.chmod(0o640)
                    target = tree.path / "alpha.py"
                    inserted = [False]

                    def inject_after_tree(
                        workspace_arg: OrdinaryGitWorkspace,
                        *arguments: str,
                        index: Path | None = None,
                        source: Path | None = None,
                        original: Callable[..., bytes] = original_git,
                        flag: list[bool] = inserted,
                        destination: Path = target,
                        outside: Path = external,
                    ) -> bytes:
                        result = original(workspace_arg, *arguments, index=index, source=source)
                        if arguments[0] == "ls-tree" and not flag[0]:
                            flag[0] = True
                            if exercise == "injected-file":
                                destination.write_bytes(b"preserve original manual bytes\n")
                                destination.chmod(0o640)
                            elif exercise == "injected-symlink":
                                destination.symlink_to(outside)
                            else:
                                os.link(outside, destination)
                        return result

                    with patch.object(OrdinaryGitWorkspace, "_git", inject_after_tree):
                        try:
                            client.request(command_key, lease_seconds=300)
                        except (ValueError, OSError):
                            pass
                        else:
                            raise AssertionError("unexpected manually inserted workspace entry was accepted")
                    assert inserted[0]
                    original = target if exercise == "injected-file" else external
                    assert original.read_bytes() == b"preserve original manual bytes\n"
                    assert original.stat().st_mode & 0o777 == 0o640
                    assert not native_state["sends"]
                    assert store.operations("work-budget:v1:ownership-claim:")
                    assert not store.operations("work-budget:v1:ownership-release:")
                    return {"status": "refused", "native_sends": 0}
                if exercise == "claim-revoked-during-preparation":
                    git_call = cast(Callable[..., bytes], inspect.getattr_static(OrdinaryGitWorkspace, "_git"))
                    observed: list[str] = []

                    def revoke_after_init(
                        workspace_arg: OrdinaryGitWorkspace,
                        *arguments: str,
                        index: Path | None = None,
                        source: Path | None = None,
                        original: Callable[..., bytes] = git_call,
                        events: list[str] = observed,
                        target_store: SQLiteProgramStore = store,
                    ) -> bytes:
                        events.append(arguments[0])
                        result = original(workspace_arg, *arguments, index=index, source=source)
                        if arguments[0] == "init":
                            target_store.revoke_work_budget(
                                "everyday-program", "owner revoked during controller preparation"
                            )
                        return result

                    with patch.object(OrdinaryGitWorkspace, "_git", revoke_after_init):
                        try:
                            client.request(command_key, lease_seconds=30)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("revocation during preparation was ignored")
                    assert "init" in observed and "fetch" not in observed
                    assert json.loads(tree.capture()[1])["entries"] == []
                    assert not native_state["sends"]
                    return {"status": "refused", "native_sends": 0}
                if exercise in {
                    "claim-expired",
                    "claim-expired-reopen",
                    "claim-revoked",
                    "claim-stale",
                    "claim-generation",
                    "claim-revision-stale",
                    "claim-current",
                }:
                    caller_revision = store.load("everyday-program").revision
                    with patch.object(
                        OrdinaryGitWorkspace, "initialize", side_effect=RuntimeError("interrupted preparation")
                    ):
                        try:
                            client.request(
                                command_key,
                                lease_seconds=30,
                                expected_revision=caller_revision if exercise == "claim-revision-stale" else None,
                            )
                        except RuntimeError as error:
                            assert str(error) == "interrupted preparation"
                        else:
                            raise AssertionError("preparation interruption was not exercised")
                    original = store.load("everyday-program").attempts[-1].spec
                    revision = int(store.artifact(f"ordinary-client:{original.attempt_id}", "revision"))
                    claim = worker.claim(command_key, revision, original, lease_seconds=30)
                    if exercise == "claim-expired-reopen":
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
                            execution_profile=app.execution_profile,
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
                        client = app.client(
                            approved, Principal("worker", "worker"), workspace, worker, control, reviewer
                        )
                    if exercise in {"claim-expired", "claim-expired-reopen"}:
                        time_offset[0] = 31
                    elif exercise == "claim-revoked":
                        store.revoke_work_budget("everyday-program", "owner revoked this work")
                    elif exercise == "claim-stale":
                        store.quarantine_work_claim(claim, "owner", "unreconciled original scope")
                    elif exercise == "claim-revision-stale":
                        current = store.load("everyday-program")
                        paused = store.admit(
                            current.program_id, "pause-before-preparation", PauseProgram(current.revision, "owner")
                        )
                        store.admit(
                            paused.program_id, "resume-before-preparation", ResumeProgram(paused.revision, "owner")
                        )
                    if exercise == "claim-current":
                        recovered = client.request(command_key, lease_seconds=30)
                        assert recovered.claim == claim
                        assert client.request(command_key, lease_seconds=30) == recovered
                        original_operations = store.operations()
                        time_offset[0] = 31
                        with patch.object(
                            OrdinaryGitWorkspace, "initialize", side_effect=AssertionError("history mutated")
                        ):
                            assert client.request(command_key, lease_seconds=30) == recovered
                        assert store.operations() == original_operations
                        return {"status": "recovered", "native_sends": 0}
                    if exercise == "claim-generation":
                        with patch.object(
                            WorkOwnership, "claim", return_value=replace(claim, generation=claim.generation + 1)
                        ):
                            try:
                                client.request(command_key, lease_seconds=30)
                            except ValueError:
                                pass
                            else:
                                raise AssertionError("different claim generation authorized preparation")
                    else:
                        try:
                            client.request(
                                command_key,
                                lease_seconds=30,
                                expected_revision=caller_revision if exercise == "claim-revision-stale" else None,
                            )
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("expired or revoked historical claim authorized preparation")
                    assert not workspace.objects.exists()
                    assert json.loads(tree.capture()[1])["entries"] == []
                    assert not native_state["sends"]
                    return {"status": "refused", "native_sends": 0}
                if exercise in {"claim-final-observe-race", "claim-final-observe-replay-race"}:
                    caller_revision = store.load("everyday-program").revision
                    if exercise == "claim-final-observe-replay-race":
                        with patch.object(OrdinaryGitWorkspace, "initialize", side_effect=RuntimeError("interrupted")):
                            try:
                                client.request(command_key, lease_seconds=600, expected_revision=caller_revision)
                            except RuntimeError:
                                pass
                            else:
                                raise AssertionError("original claim was not interrupted")
                    original_observe = WorkOwnership.observe
                    original_capture = LocalOwnedWorkspace.capture
                    changed = [False]

                    def capture_race(
                        target: LocalOwnedWorkspace,
                        original: Callable[[LocalOwnedWorkspace], tuple[str, bytes]] = original_capture,
                        flag: list[bool] = changed,
                        target_store: SQLiteProgramStore = store,
                    ) -> tuple[str, bytes]:
                        result = original(target)
                        if not flag[0]:
                            flag[0] = True
                            current = target_store.load("everyday-program")
                            paused = target_store.admit(
                                current.program_id, "pause-final-observe", PauseProgram(current.revision, "owner")
                            )
                            target_store.admit(
                                paused.program_id, "resume-final-observe", ResumeProgram(paused.revision, "owner")
                            )
                        return result

                    def observe_race(
                        target: WorkOwnership,
                        claim: WorkClaim,
                        owner_control: WorkOwnership = control,
                        original: Callable[[WorkOwnership, WorkClaim], str] = original_observe,
                    ) -> str:
                        if target is owner_control:
                            with patch.object(LocalOwnedWorkspace, "capture", capture_race):
                                return original(target, claim)
                        return original(target, claim)

                    with patch.object(WorkOwnership, "observe", observe_race):
                        try:
                            client.request(command_key, lease_seconds=600, expected_revision=caller_revision)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("stale final observation accepted preparation")
                    assert changed[0]
                    attempt = store.load("everyday-program").attempts[-1].spec
                    original_claim = worker.claim(command_key, caller_revision, attempt, lease_seconds=600)
                    assert not store.find_ordinary_preparation(original_claim)
                    prepared = json.loads(store.artifact(f"ordinary-workspace:{attempt.attempt_id}", "prepared"))
                    try:
                        store.accept_ordinary_preparation(
                            original_claim, "owner", prepared["prepared_subject"], expected_revision=caller_revision + 1
                        )
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("store accepted a stale preparation revision")
                    assert not store.find_ordinary_preparation(original_claim)
                    assert workspace.objects.exists() and not native_state["sends"]
                    return {"status": "refused", "native_sends": 0}
                handoff = client.request(
                    command_key, lease_seconds=600, remediation_of=rejected_attempt if unit == "alpha" else None
                )
                if exercise == "start-revision-race":
                    revision = store.load("everyday-program").revision
                    observe = WorkOwnership.observe

                    def pause_after_observation(
                        owner: WorkOwnership,
                        claim: WorkClaim,
                        original: Callable[[WorkOwnership, WorkClaim], str] = observe,
                        target_store: SQLiteProgramStore = store,
                    ) -> str:
                        subject_value = original(owner, claim)
                        current = target_store.load("everyday-program")
                        target_store.admit(
                            current.program_id, "owner-paused-during-start", PauseProgram(current.revision, "owner")
                        )
                        return subject_value

                    with patch.object(WorkOwnership, "observe", pause_after_observation):
                        try:
                            client.start(handoff, expected_revision=revision)
                        except IntakeRefused:
                            pass
                        else:
                            raise AssertionError("stale start was accepted after owner mutation")
                    assert store.load("everyday-program").attempt(handoff.attempt.attempt_id).status.value == "prepared"
                    assert store.operation(handoff.claim.operation_id).attempts == 0
                    return {"status": "refused", "native_sends": 0}
                if exercise in {"quiesce-revision-race", "renew-revision-race"}:
                    client.start(handoff)
                    revision = store.load("everyday-program").revision
                    observe = WorkOwnership.observe

                    def pause_after_quiescence_observation(
                        owner: WorkOwnership,
                        claim: WorkClaim,
                        original: Callable[[WorkOwnership, WorkClaim], str] = observe,
                        target_store: SQLiteProgramStore = store,
                    ) -> str:
                        subject_value = original(owner, claim)
                        current = target_store.load("everyday-program")
                        target_store.admit(
                            current.program_id,
                            "owner-paused-during-quiescence",
                            PauseProgram(current.revision, "owner"),
                        )
                        return subject_value

                    with patch.object(WorkOwnership, "observe", pause_after_quiescence_observation):
                        try:
                            if exercise == "quiesce-revision-race":
                                client.quiesce(handoff, "cooperative-receipt", expected_revision=revision)
                            else:
                                client.renew("renew-stale", handoff, lease_seconds=30, expected_revision=revision)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("stale quiescence proof was accepted after owner mutation")
                    assert store.load("everyday-program").status is ProgramStatus.PAUSED
                    assert not store.operations("work-budget:v1:ownership-proof:")
                    assert not store.operations("work-budget:v1:ownership-renew:")
                    return {"status": "refused", "native_sends": 0}
                assert handoff.attempt.work_unit_id == unit
                assert (
                    client.request(
                        command_key, lease_seconds=600, remediation_of=rejected_attempt if unit == "alpha" else None
                    )
                    == handoff
                )
                if exercise == "review-remediation-tampered" and position == 1:
                    artifact = SQLiteProgramStore.artifact

                    def mismatched_receipt(
                        store_arg: SQLiteProgramStore,
                        operation: str,
                        name: str,
                        original: Callable[[SQLiteProgramStore, str, str], bytes] = artifact,
                    ) -> bytes:
                        data = original(store_arg, operation, name)
                        if operation.startswith("product-review:") and name == "review-receipt":
                            record = json.loads(data)
                            record["subject"]["candidate_digest"] = "sha256:" + "0" * 64
                            return encode(record)
                        return data

                    with patch.object(SQLiteProgramStore, "artifact", mismatched_receipt):
                        try:
                            client.context(handoff)
                        except ValueError as error:
                            assert "differs from rejected candidate subject" in str(error)
                        else:
                            raise AssertionError("mismatched original review subject was accepted")
                    assert not native_state["sends"] and len(calls) == 1
                    return {"status": "refused", "native_sends": 0}
                if exercise == "review-remediation" and position == 1 and selected is HandoffMode.PULL:
                    remediation = json.loads(client.context(handoff))["remediation"]
                    assert remediation["review_operation_id"] == store.operations("product-review:")[0].operation_id
                    assert remediation["attempt"] == rejected_attempt
                    assert remediation["findings"][0]["finding_id"] == "msg_synthetic_result:0"
                    assert remediation["findings"][0]["fingerprint"] == "edge-case"
                    assert remediation["findings"][0]["reason"] == "Implement the approved edge case."
                if exercise == "review-remediation-served" and position == 1:
                    owner_socket, worker_socket = socket.socketpair()
                    with owner_socket, worker_socket:
                        server = LocalWorkerChannel(
                            OwnerProjection(store, "everyday-program"),
                            owner_socket,
                            client,
                            handoffs={"remediation-claim": handoff},
                        )
                        process = subprocess.Popen(  # noqa: S603 - Installed client and trusted local descriptor.
                            [
                                sys.executable,
                                "-I",
                                "-m",
                                "creatidy_kernel.adapters.served_client",
                                "--fd",
                                str(worker_socket.fileno()),
                                "context",
                                "--claim-id",
                                "remediation-claim",
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            pass_fds=(worker_socket.fileno(),),
                            env={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                        )
                        try:
                            assert select.select([owner_socket], [], [], 10)[0]
                            server.serve_once()
                            stdout, stderr = process.communicate(timeout=10)
                            assert process.returncode == 0, stderr.decode(errors="replace")
                            context = json.loads(stdout)["result"]
                            assert context["approved"] == json.loads(encode(proposal.payload))
                            assert context["work_unit"] == unit and isinstance(context["inputs"], list)
                            assert context["remediation"]["attempt"] == rejected_attempt
                            assert (
                                context["remediation"]["findings"][0]["reason"] == "Implement the approved edge case."
                            )
                            unit = cast(str, context["work_unit"])
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.wait(timeout=10)
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
                    changed = [False]
                    if exercise == "served-managed-start-race":
                        original_inputs = OrdinaryManagedRun.inputs

                        def pause_after_inputs(
                            runtime: OrdinaryManagedRun,
                            request: ExecutionRequest,
                            original: Callable[[OrdinaryManagedRun, ExecutionRequest], CodexInputs] = original_inputs,
                            target_store: SQLiteProgramStore = store,
                            flag: list[bool] = changed,
                        ) -> CodexInputs:
                            result = original(runtime, request)
                            if not flag[0]:
                                flag[0] = True
                                current = target_store.load("everyday-program")
                                target_store.admit(
                                    current.program_id,
                                    "owner-paused-before-native-start",
                                    PauseProgram(current.revision, "owner"),
                                )
                            return result

                        with patch.object(OrdinaryManagedRun, "inputs", pause_after_inputs):
                            managed = OrdinaryManagedRun(client, workspace, connection)
                    else:
                        managed = OrdinaryManagedRun(client, workspace, connection)
                    if exercise == "served-managed-distinct-approval":
                        current = store.load("everyday-program")
                        approval_binding: list[OrdinaryApproval | None] = [
                            replace(approved, decision_id="different-owner-decision")
                        ]
                        owner_socket, owner_client = socket.socketpair()
                        with owner_socket, owner_client:
                            channel = LocalOwnerChannel(
                                OwnerProjection(store, "everyday-program"),
                                owner_socket,
                                app,
                                approved,
                                control=control,
                                handoff=handoff,
                                managed=managed,
                                approval_binding=approval_binding,
                            )
                            start = {
                                "version": 1,
                                "action": "start-managed",
                                "command_id": "distinct-start",
                                "expected_revision": current.revision,
                            }
                            before = (store.history("everyday-program"), store.operations())
                            with patch.object(
                                OrdinaryManagedRun, "start", side_effect=AssertionError("unexpected start")
                            ):
                                write_frame(owner_client, encode(start), bound=65536)
                                try:
                                    channel.serve_once()
                                except LocalChannelRefused:
                                    pass
                                else:
                                    raise AssertionError("different owner approval authorized managed start")
                            assert (store.history("everyday-program"), store.operations()) == before
                        approval_binding[0] = approved
                        owner_socket, owner_client = socket.socketpair()
                        with owner_socket, owner_client:
                            channel = LocalOwnerChannel(
                                OwnerProjection(store, "everyday-program"),
                                owner_socket,
                                app,
                                approved,
                                control=control,
                                handoff=handoff,
                                managed=managed,
                                approval_binding=approval_binding,
                            )
                            write_frame(owner_client, encode(start), bound=65536)
                            channel.serve_once()
                            receipt = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                            approval_binding[0] = replace(approved, decision_id="amended-owner-decision")
                            with patch.object(
                                OrdinaryManagedRun, "start", side_effect=AssertionError("unexpected start")
                            ):
                                write_frame(owner_client, encode(start), bound=65536)
                                channel.serve_once()
                                assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"] == receipt
                                before = (store.history("everyday-program"), store.operations())
                                write_frame(
                                    owner_client, encode({**start, "command_id": "new-after-amend"}), bound=65536
                                )
                                try:
                                    channel.serve_once()
                                except LocalChannelRefused:
                                    pass
                                else:
                                    raise AssertionError("amended owner approval authorized old managed binding")
                                assert (store.history("everyday-program"), store.operations()) == before
                        assert store.operation(handoff.claim.operation_id).attempts == 1
                        assert not native_state["sends"]
                        return {"status": "checked", "native_sends": 0}
                    if exercise == "served-managed-stop-unstarted":
                        before = store.load("everyday-program")
                        assert managed.handoff is None
                        owner_socket, owner_client = socket.socketpair()
                        with owner_socket, owner_client:
                            owner_channel = LocalOwnerChannel(
                                OwnerProjection(store, "everyday-program"),
                                owner_socket,
                                app,
                                approved,
                                control=control,
                                handoff=handoff,
                                managed=managed,
                            )
                            wire = encode(
                                {
                                    "version": 1,
                                    "action": "stop",
                                    "command_id": "unstarted-stop",
                                    "expected_revision": before.revision,
                                    "reason": "owner stops work",
                                }
                            )
                            write_frame(owner_client, wire, bound=65536)
                            owner_channel.serve_once()
                            receipt = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                            assert receipt["status"] == "accepted" and receipt["settlement"] == "unknown"
                            write_frame(owner_client, wire, bound=65536)
                            owner_channel.serve_once()
                            assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"] == receipt
                        assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                        assert store.operation(handoff.claim.operation_id).attempts == 0
                        assert (
                            store.find_artifact("ordinary-cancel:" + handoff.attempt.attempt_id, "settlement") is None
                        )
                        assert not native_state["sends"]
                        return {"status": "stopped-unknown", "native_sends": 0}
                    if exercise in {"served-managed-start", "served-managed-start-race", "served-managed-worker-start"}:
                        initial = store.load("everyday-program")
                        if exercise == "served-managed-worker-start":
                            before_operation = store.operation(handoff.claim.operation_id)
                            before_commands = store.operations(command_prefix("everyday-program"))
                            worker_owner, worker_peer = socket.socketpair()
                            with worker_owner, worker_peer:
                                worker_channel = LocalWorkerChannel(
                                    OwnerProjection(store, "everyday-program"),
                                    worker_owner,
                                    client,
                                    handoffs={"managed-claim": handoff},
                                )
                                write_frame(
                                    worker_peer,
                                    encode(
                                        {
                                            "version": 1,
                                            "action": "start",
                                            "command_id": "worker-managed-start",
                                            "expected_revision": initial.revision,
                                            "claim_id": "managed-claim",
                                        }
                                    ),
                                    bound=65536,
                                )
                                try:
                                    worker_channel.serve_once()
                                except LocalChannelRefused:
                                    pass
                                else:
                                    raise AssertionError("worker started a managed handoff")
                            assert store.load("everyday-program") == initial
                            assert store.operation(handoff.claim.operation_id) == before_operation
                            assert store.operations(command_prefix("everyday-program")) == before_commands
                        owner_socket, owner_client = socket.socketpair()
                        with owner_socket, owner_client:
                            owner_channel = LocalOwnerChannel(
                                OwnerProjection(store, "everyday-program"),
                                owner_socket,
                                app,
                                approved,
                                control=control,
                                handoff=handoff,
                                managed=managed,
                            )
                            start = {
                                "version": 1,
                                "action": "start-managed",
                                "command_id": "owner-managed-start",
                                "expected_revision": initial.revision,
                            }
                            wire = json.dumps(start, sort_keys=True).encode()
                            write_frame(owner_client, wire, bound=65536)
                            if exercise == "served-managed-start-race":
                                try:
                                    owner_channel.serve_once()
                                except LocalChannelRefused:
                                    pass
                                else:
                                    raise AssertionError("stale managed start reached native dispatch")
                                assert changed[0] and store.load("everyday-program").status is ProgramStatus.PAUSED
                                assert store.find_artifact(handoff.claim.operation_id, "runtime-receipt") is None
                                assert not native_state["sends"]
                                return {"status": "refused", "native_sends": 0}
                            owner_channel.serve_once()
                            receipt = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                            assert receipt["status"] == "accepted" and receipt["settlement"] == "unknown"
                            assert store.operation(handoff.claim.operation_id).attempts == 1
                            write_frame(owner_client, wire, bound=65536)
                            owner_channel.serve_once()
                            assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"] == receipt
                            assert store.operation(handoff.claim.operation_id).attempts == 1
                            current = store.load("everyday-program")
                            stop = {
                                "version": 1,
                                "action": "stop",
                                "command_id": "owner-managed-stop",
                                "expected_revision": current.revision,
                                "reason": "owner stopped original target",
                            }
                            write_frame(owner_client, json.dumps(stop).encode(), bound=65536)
                            owner_channel.serve_once()
                            assert (
                                json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]["settlement"]
                                == "bound-managed-target-settled"
                            )
                        assert not native_state["sends"]
                        return {"status": "stopped", "native_sends": 0}
                    if exercise == "review-remediation":
                        intake_observations: list[IntakeEvidence] = []
                        read_evidence = OwnerReviewedForgejoEvidence.read

                        def capture_evidence(
                            target: OwnerReviewedForgejoEvidence,
                            subject: IssueSubject,
                            declaration: Declaration,
                            original: Callable[
                                [OwnerReviewedForgejoEvidence, IssueSubject, Declaration], IntakeEvidence
                            ] = read_evidence,
                            results: list[IntakeEvidence] = intake_observations,
                        ) -> IntakeEvidence:
                            evidence = original(target, subject, declaration)
                            results.append(evidence)
                            return evidence

                        try:
                            with patch.object(OwnerReviewedForgejoEvidence, "read", capture_evidence):
                                managed.start(handoff)
                        except IntakeRefused as error:
                            proof = [item.payload() for item in intake_observations]
                            raise AssertionError(f"synthetic managed intake observations: {proof!r}") from error
                    else:
                        managed.start(handoff)
                    if exercise == "private-stop-uncertain":
                        with patch.object(
                            WorkOwnership, "witness", side_effect=ValueError("synthetic unavailable proof")
                        ):
                            assert managed.request_stop("synthetic-private-stop") == "cancel_uncertain"
                        result = export_private_outcome(
                            store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                        )
                        cancelled = cast(list[dict[str, object]], result["cancellations"])
                        assert len(cancelled) == 1 and cancelled[0]["request_recorded"] is True
                        assert (
                            cancelled[0]["settlement_recorded"] is False and cancelled[0]["terminal_observed"] is None
                        )
                        assert cancelled[0]["physical_settlement"] is None
                        assert cast(dict[str, object], result["coverage"])["work_revocation_recorded"] is True
                        assert b"synthetic-private-stop" not in encode(result)
                        assert not native_state["sends"]
                        return {"status": "stopped-unknown", "native_sends": 0}
                    if exercise == "served-managed-stop-cli":
                        before = store.load("everyday-program")
                        binding = LocalOwnerSession(
                            OwnerProjection(store, "everyday-program"),
                            app,
                            approved,
                            control=control,
                            handoff=handoff,
                            managed=managed,
                        )
                        owner_socket, owner_client = socket.socketpair()
                        with owner_socket, owner_client:
                            channel = binding.attach(owner_socket)
                            process = subprocess.Popen(  # noqa: S603 - Fixed local client and synthetic descriptor.
                                [
                                    sys.executable,
                                    "-I",
                                    "-m",
                                    "creatidy_kernel.adapters.served_client",
                                    "--fd",
                                    str(owner_client.fileno()),
                                    "stop",
                                    "--command-id",
                                    "owned-managed-stop",
                                    "--expected-revision",
                                    str(before.revision),
                                    "--reason",
                                    "owner stopped managed work",
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                pass_fds=(owner_client.fileno(),),
                                env={"HOME": str(root), "PATH": os.defpath, "LANG": "C.UTF-8"},
                            )
                            try:
                                assert select.select([owner_socket], [], [], 10)[0]
                                channel.serve_once()
                                stdout, stderr = process.communicate(timeout=10)
                                assert process.returncode == 0, stderr.decode(errors="replace")
                                assert json.loads(stdout)["result"]["settlement"] == "bound-managed-target-settled"
                            finally:
                                if process.poll() is None:
                                    process.kill()
                                    process.wait(timeout=10)
                                binding.detach(channel)
                        assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                        assert not native_state["sends"]
                        return {"status": "stopped", "native_sends": 0}
                    if exercise == "served-managed-stop":
                        before = store.load("everyday-program")
                        owner_socket, owner_client = socket.socketpair()
                        with owner_socket, owner_client:
                            owner_channel = LocalOwnerChannel(
                                OwnerProjection(store, "everyday-program"),
                                owner_socket,
                                app,
                                approved,
                                control=control,
                                handoff=handoff,
                                managed=managed,
                            )
                            command = {
                                "version": 1,
                                "action": "stop",
                                "command_id": "owned-managed-stop",
                                "expected_revision": before.revision,
                                "reason": "owner stopped managed work",
                            }
                            wire = json.dumps(command, sort_keys=True).encode()
                            write_frame(owner_client, wire, bound=65536)
                            owner_channel.serve_once()
                            receipt = json.loads(read_frame(owner_client, bound=1024 * 1024))["result"]
                            assert receipt["settlement"] == "bound-managed-target-settled"
                            write_frame(owner_client, wire, bound=65536)
                            owner_channel.serve_once()
                            assert json.loads(read_frame(owner_client, bound=1024 * 1024))["result"] == receipt
                        assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                        assert not native_state["sends"]
                        return {"status": "stopped", "native_sends": 0}
                    if exercise in {"managed-program-stop", "managed-budget-revoke"}:
                        if exercise == "managed-program-stop":
                            app.stop(approved, "owner stopped the Program")
                        else:
                            store.revoke_work_budget("everyday-program", "owner revoked remaining work")
                        assert managed.finish() == "cancelled"
                        assert not native_state["sends"]
                        assert len(store.operations("runtime:")) == 1
                        return {"status": "stopped", "native_sends": 0}
                    if exercise == "stop":
                        settled = managed.request_stop("owner requested STOP")
                        assert store.load("everyday-program").status is ProgramStatus.CANCELLED
                        outcome_export = export_private_outcome(
                            store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                        )
                        cancellation = cast(list[dict[str, object]], outcome_export["cancellations"])[0]
                        assert cancellation["request_recorded"] is True and cancellation["settlement_recorded"] is True
                        assert cancellation["settled_original_group"] is not None
                        assert cancellation["terminal_observed"] is None and cancellation["physical_settlement"] is None
                        assert b"owner requested STOP" not in encode(outcome_export)
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
                    if exercise == "diamond-inherited" and unit == "beta":
                        (tree.path / "alpha.py").write_text("VALUE = 2\n")
                    if exercise == "diamond-inherited" and unit == "delta":
                        assert (tree.path / "alpha.py").read_text() == "VALUE = 2\n"
                    if exercise == "rename-outside" and unit == "alpha":
                        (tree.path / "base.py").rename(tree.path / "renamed.py")
                    if exercise == "diamond-conflict" and unit == "gamma":
                        (tree.path / "beta.py").write_text("VALUE = 999\n")
                    client.quiesce(handoff, "authenticated-kilo-sdk-task-scope-closed")
                if exercise == "report-revision-race":
                    revision = store.load("everyday-program").revision
                    capture = LocalOwnedWorkspace.capture
                    changed = [False]

                    def pause_after_candidate_capture(
                        target: LocalOwnedWorkspace,
                        original: Callable[[LocalOwnedWorkspace], tuple[str, bytes]] = capture,
                        target_store: SQLiteProgramStore = store,
                        flag: list[bool] = changed,
                    ) -> tuple[str, bytes]:
                        result = original(target)
                        if not flag[0]:
                            flag[0] = True
                            current = target_store.load("everyday-program")
                            target_store.admit(
                                current.program_id,
                                "owner-paused-during-submit",
                                PauseProgram(current.revision, "owner"),
                            )
                        return result

                    with patch.object(LocalOwnedWorkspace, "capture", pause_after_candidate_capture):
                        try:
                            client.report(handoff, expected_revision=revision)
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("stale report submitted a candidate after owner mutation")
                    assert changed[0] and store.load("everyday-program").status is ProgramStatus.PAUSED
                    assert store.find_artifact(handoff.claim.operation_id, "ownership-candidate") is None
                    assert not calls and not native_state["sends"]
                    return {"status": "refused", "native_sends": 0}
                control.reconcile(
                    handoff.claim,
                    control.observe(handoff.claim),
                    evidence="owner receives exact task-owned bytes after qualified task-scope quiescence",
                )
                if exercise in {"outcome-candidate-cut", "outcome-check-cut", "outcome-manifest-stop-cut"}:
                    original_collect = OrdinaryGitWorkspace.collect
                    original_finalize = SQLiteProgramStore.finalize_artifact
                    hit = [False]

                    def interrupt_collect(
                        target: OrdinaryGitWorkspace,
                        original_attempt: AttemptSpec,
                        original_claim: WorkClaim,
                        original: Callable[
                            [OrdinaryGitWorkspace, AttemptSpec, WorkClaim], tuple[Candidate, Collection]
                        ] = original_collect,
                        flag: list[bool] = hit,
                    ) -> tuple[Candidate, Collection]:
                        original(target, original_attempt, original_claim)
                        flag[0] = True
                        raise RuntimeError("synthetic candidate cut")

                    def interrupt_artifact(
                        target: SQLiteProgramStore,
                        operation_id: str,
                        name: str,
                        data: bytes,
                        original: Callable[[SQLiteProgramStore, str, str, bytes], str] = original_finalize,
                        flag: list[bool] = hit,
                    ) -> str:
                        result = original(target, operation_id, name, data)
                        if (
                            exercise == "outcome-check-cut"
                            and operation_id.startswith("verification:")
                            and name == "result"
                        ) or (
                            exercise == "outcome-manifest-stop-cut"
                            and operation_id.startswith("runtime:")
                            and name.startswith("verification:")
                        ):
                            flag[0] = True
                            raise RuntimeError("synthetic verification cut")
                        return result

                    with (
                        patch.object(OrdinaryGitWorkspace, "collect", interrupt_collect)
                        if exercise == "outcome-candidate-cut"
                        else patch.object(SQLiteProgramStore, "finalize_artifact", interrupt_artifact)
                    ):
                        try:
                            client.report(handoff)
                        except RuntimeError as error:
                            assert str(error).startswith("synthetic ")
                        else:
                            raise AssertionError("requested outcome crash cut was not reached")
                    assert hit[0]
                    if exercise == "outcome-manifest-stop-cut":
                        app.stop(approved, "synthetic outcome STOP")
                    before = store.served_change_token()
                    result = export_private_outcome(
                        store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                    )
                    assert store.served_change_token() == before
                    assert len(cast(list[object], result["candidates"])) == 1
                    assert not cast(list[object], result["satisfactions"])
                    if exercise == "outcome-candidate-cut":
                        assert not cast(list[object], result["checks"])
                    elif exercise == "outcome-check-cut":
                        assert cast(list[object], result["checks"])
                        assert not cast(list[object], result["verification_manifests"])
                    else:
                        manifests = cast(list[dict[str, object]], result["verification_manifests"])
                        assert manifests and all(item["decision"] is None for item in manifests)
                        assert cast(dict[str, object], result["coverage"])["work_revocation_recorded"] is True
                    return {"status": "retained", "native_sends": 0}
                if exercise == "report-verification-revision-race":
                    revision = store.load("everyday-program").revision
                    original_check = OrdinaryGitWorkspace.check
                    changed = [False]

                    def pause_before_verification(
                        target: OrdinaryGitWorkspace,
                        name: str,
                        subject: EvidenceSubject,
                        flag: list[bool] = changed,
                        target_store: SQLiteProgramStore = store,
                        original: Callable[
                            [OrdinaryGitWorkspace, str, EvidenceSubject], Evidence | None
                        ] = original_check,
                    ) -> Evidence | None:
                        if name == "commands" and not flag[0]:
                            flag[0] = True
                            current = target_store.load("everyday-program")
                            paused = target_store.admit(
                                current.program_id, "pause-before-verification", PauseProgram(current.revision, "owner")
                            )
                            target_store.admit(
                                paused.program_id, "resume-before-verification", ResumeProgram(paused.revision, "owner")
                            )
                        return original(target, name, subject)

                    with patch.object(OrdinaryGitWorkspace, "check", pause_before_verification):
                        try:
                            client.report(handoff, expected_revision=revision)
                        except ValueError:
                            pass
                    assert changed[0]
                    assert not store.operations("ordinary-command:") and not calls
                    return {"status": "refused", "native_sends": 0}
                if exercise == "report-review-revision-race":
                    revision = store.load("everyday-program").revision
                    authorizations = [0]

                    def pause_during_review_authorization(
                        count: list[int] = authorizations, target_store: SQLiteProgramStore = store
                    ) -> str:
                        count[0] += 1
                        if count[0] == 2:
                            current = target_store.load("everyday-program")
                            paused = target_store.admit(
                                current.program_id, "pause-before-review-send", PauseProgram(current.revision, "owner")
                            )
                            target_store.admit(
                                paused.program_id, "resume-before-review-send", ResumeProgram(paused.revision, "owner")
                            )
                        return ""

                    reviewer.authorization = pause_during_review_authorization
                    result = client.report(handoff, expected_revision=revision)
                    assert authorizations[0] == 2 and result == "review_unknown"
                    assert not calls and not native_state["sends"]
                    return {"status": "unknown", "native_sends": 0}
                if (
                    exercise
                    in {
                        "command-evidence-crash",
                        "command-evidence-no-witness",
                        "command-evidence-partial",
                        "command-export-crash",
                        "command-export-failed",
                    }
                    and position == 0
                ):
                    finalize = SQLiteProgramStore.finalize_artifact

                    def fail_aggregate(
                        store_arg: SQLiteProgramStore,
                        operation: str,
                        name: str,
                        data: bytes,
                        original: Callable[[SQLiteProgramStore, str, str, bytes], str] = finalize,
                    ) -> str:
                        if name.startswith("command-evidence:"):
                            raise RuntimeError("synthetic aggregate Evidence crash")
                        return original(store_arg, operation, name, data)

                    with patch.object(SQLiteProgramStore, "finalize_artifact", fail_aggregate):
                        try:
                            client.report(handoff)
                        except RuntimeError as error:
                            assert str(error) == "synthetic aggregate Evidence crash"
                        else:
                            raise AssertionError("aggregate Evidence crash was not exercised")
                    assert not calls
                    original_command = store.operations(f"ordinary-command:{handoff.attempt.attempt_id}:")[0]
                    original_result = json.loads(store.artifact(original_command.operation_id, "result"))["result"]
                    original_observed_at = original_result["observed_at"]
                    time_offset[0] = 10
                    store.close()
                    if exercise in {"command-export-crash", "command-export-failed"}:
                        return {"status": "interrupted", "native_sends": 0}
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
                        execution_profile=app.execution_profile,
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
                    client = app.client(approved, Principal("worker", "worker"), workspace, worker, control, reviewer)
                    if exercise == "command-evidence-partial":
                        find_artifact = SQLiteProgramStore.find_artifact

                        def hide_result(
                            store_arg: SQLiteProgramStore,
                            operation: str,
                            name: str,
                            original: Callable[[SQLiteProgramStore, str, str], bytes | None] = find_artifact,
                        ) -> bytes | None:
                            if (
                                operation.startswith("ordinary-command:")
                                and operation.endswith(":0:1")
                                and name == "result"
                            ):
                                return None
                            return original(store_arg, operation, name)

                        with patch.object(SQLiteProgramStore, "find_artifact", hide_result):
                            try:
                                client.report(handoff)
                            except IntakeRefused as error:
                                assert "results are incomplete" in str(error)
                            else:
                                raise AssertionError("partial original command results were accepted")
                        assert not calls
                        assert all(
                            item.attempts == 1
                            for item in store.operations(f"ordinary-command:{handoff.attempt.attempt_id}:")
                        )
                        return {"status": "unknown", "native_sends": 0}
                    if exercise == "command-evidence-no-witness":
                        operations = SQLiteProgramStore.operations

                        def hide_witness(
                            store_arg: SQLiteProgramStore,
                            prefix: str = "",
                            original: Callable[[SQLiteProgramStore, str], tuple[OperationRecord, ...]] = operations,
                        ) -> tuple[OperationRecord, ...]:
                            return () if prefix.startswith("command-settlement:") else original(store_arg, prefix)

                        with patch.object(SQLiteProgramStore, "operations", hide_witness):
                            try:
                                client.report(handoff)
                            except IntakeRefused as error:
                                assert "settlement witness" in str(error)
                            else:
                                raise AssertionError("missing original command witness was accepted")
                        assert not calls
                        assert store.operation(original_command.operation_id).attempts == 1
                        return {"status": "unknown", "native_sends": 0}
                    outcome = client.report(handoff)
                    command_operations = store.operations(f"ordinary-command:{handoff.attempt.attempt_id}:")
                    assert len(command_operations) == 1 and command_operations[0].attempts == 1
                    acceptance = store.operation(f"acceptance:{handoff.attempt.attempt_id}")
                    acceptance_request = json.loads(acceptance.request_json)["request"]
                    manifest_name = "verification:" + acceptance_request["manifest"].removeprefix("sha256:")
                    accepted_result = json.loads(store.artifact(handoff.claim.operation_id, manifest_name))
                    command_evidence = next(item for item in accepted_result["evidence"] if item["check"] == "commands")
                    assert command_evidence["observed_at"] == original_observed_at
                    assert command_evidence["fresh_until"] == original_observed_at + 900
                elif exercise == "served-unknown-rejected" and position == 0:
                    owner_socket, worker_socket = socket.socketpair()
                    with owner_socket, worker_socket:
                        server = LocalWorkerChannel(
                            OwnerProjection(store, "everyday-program"),
                            owner_socket,
                            client,
                            handoffs={"original-claim": handoff},
                        )
                        report = {
                            "version": 1,
                            "action": "report",
                            "command_id": "lost-report",
                            "claim_id": "original-claim",
                            "expected_revision": store.load("everyday-program").revision,
                        }
                        write_frame(worker_socket, encode(report), bound=65536)
                        server.serve_once()
                        historical_receipt = json.loads(read_frame(worker_socket, bound=1024 * 1024))["result"]
                        assert historical_receipt["status"] == "review_unknown"
                        original_review = historical_receipt["review_operation_id"]
                    assert len(calls) == 1
                    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                    owners.enter_context(sender)
                    owners.enter_context(receiver)
                    notifier = LocalDecisionNotifier(sender)
                    unknown_snapshot = OwnerProjection(store, "everyday-program").snapshot()
                    notifier.publish(unknown_snapshot)
                    assert select.select([receiver], [], [], 2)[0]
                    receiver.recv(65536)
                    notifier.publish(unknown_snapshot)
                    assert not select.select([receiver], [], [], 0)[0]
                    time_offset[0] = 61
                    assert client.report(handoff) == "rejected"
                    assert len(calls) == 1
                    snapshot = OwnerProjection(store, "everyday-program").snapshot()
                    original = [item for item in snapshot.decisions if item.get("source") == "served-command"]
                    assert original and original[0]["status"] == "review_unknown"
                    assert original[0]["applicability"] == "historical"
                    assert original[0]["review_operation_id"] == original_review
                    assert any(
                        item.get("status") == "changes_requested" and item.get("applicability") == "pending"
                        for item in snapshot.decisions
                    )
                    notifier.publish(snapshot)
                    assert select.select([receiver], [], [], 2)[0]
                    receiver.recv(65536)
                    notifier.publish(snapshot)
                    assert not select.select([receiver], [], [], 0)[0]
                    return {"status": "rejected", "native_sends": 0}
                else:
                    outcome = client.report(handoff)
                if exercise == "sdk-review-notify" and position == 0:
                    assert outcome == "rejected" and not store.operations(command_prefix("everyday-program"))
                    snapshot = OwnerProjection(store, "everyday-program").snapshot()
                    assert any(item.get("status") == "changes_requested" for item in snapshot.decisions)
                    assert any(item.get("applicability") == "pending" for item in snapshot.decisions)
                    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                    with sender, receiver:
                        notifier = LocalDecisionNotifier(sender)
                        notifier.publish(snapshot)
                        assert select.select([receiver], [], [], 2)[0]
                        assert json.loads(receiver.recv(65536))["kind"] == "decision-required"
                        notifier.publish(snapshot)
                        assert not select.select([receiver], [], [], 0)[0]
                    assert len(calls) == 1
                    return {"status": "rejected", "native_sends": 0}
                if exercise == "rename-outside" and position == 0:
                    assert outcome == "rejected"
                    assert not store.load("everyday-program").satisfactions
                    assert not native_state["sends"]
                    assert not (tree.path / "base.py").exists()
                    assert (tree.path / "renamed.py").read_text() == "BASELINE = True\n"
                    return {"status": "rejected", "native_sends": 0}
                if (
                    exercise in {"review-remediation", "review-remediation-tampered", "review-remediation-served"}
                    and position == 0
                ):
                    assert outcome == "rejected"
                    assert not store.load("everyday-program").satisfactions and not native_state["sends"]
                    control.release(handoff.claim, evidence="owner reconciled the reviewed original workspace")
                    rejected_attempt = handoff.attempt.attempt_id
                    continue
                if exercise in {"review-lost", "review-lost-reopen"} and position == 0:
                    assert outcome == "review_unknown"
                    assert len(calls) == 1
                    snapshot = OwnerProjection(store, "everyday-program").snapshot()
                    assert any(
                        item.get("status") == "review_unknown" and item.get("applicability") == "pending"
                        for item in snapshot.decisions
                    )
                    notification_tx, notification_rx = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                    with notification_tx, notification_rx:
                        notifier = LocalDecisionNotifier(notification_tx)
                        notifier.publish(snapshot)
                        assert select.select([notification_rx], [], [], 2)[0]
                        notification_rx.recv(65536)
                        notifier.publish(snapshot)
                        assert not select.select([notification_rx], [], [], 0)[0]
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
                    recovered = OwnerProjection(store, "everyday-program").snapshot()
                    assert not any(item.get("status") == "review_unknown" for item in recovered.decisions)
                if bad_candidate:
                    assert outcome == "rejected", outcome
                    assert not store.load("everyday-program").satisfactions
                    assert not native_state["sends"]
                    if exercise in {"rejected-replay", "rejected-replay-reopen"}:
                        original = store.artifact(f"rejection:{handoff.attempt.attempt_id}", "rejected-result")
                        rejected_subject = EvidenceSubject(**json.loads(original)["subject"])
                        evidence_name = "command-evidence:" + digest(encode(asdict(rejected_subject)))
                        evidence = store.artifact(handoff.claim.operation_id, evidence_name)
                        review_count = len(calls)
                        original_wall = time.time()
                        if exercise == "rejected-replay-reopen":
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
                                execution_profile=app.execution_profile,
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
                        with patch(
                            "creatidy_kernel.adapters.ordinary_workspace.time.time", return_value=original_wall + 10
                        ):
                            if exercise == "rejected-replay-reopen":
                                workspace.restore(handoff.attempt)
                                workspace.claim = handoff.claim
                            retained = workspace.check("commands", rejected_subject)
                            assert retained is not None
                            assert encode(asdict(retained)) == evidence
                            assert client.report(handoff) == "rejected"
                        assert store.artifact(f"rejection:{handoff.attempt.attempt_id}", "rejected-result") == original
                        assert len(calls) == review_count
                        return {"status": "rejected", "review_calls": review_count}
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
            if exercise == "stop-completed":
                original = store.load("everyday-program")
                before_export = export_private_outcome(
                    store, original.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                )
                app.stop(approved, "owner withdrew publication")
                assert store.load("everyday-program") == original
                after_export = export_private_outcome(
                    store, original.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                )
                before_coverage = cast(dict[str, object], before_export["coverage"])
                after_coverage = cast(dict[str, object], after_export["coverage"])
                assert before_coverage["work_revocation_recorded"] is False
                assert after_coverage["work_revocation_recorded"] is True
                assert after_coverage["revocation_evidence"] is not None
                budget = cast(dict[str, object], after_export["work_budget"])
                assert cast(dict[str, object], budget["original_deadline"])["wall_deadline"] == approved.expires_at
                assert b"owner withdrew publication" not in encode(after_export)
                try:
                    publications[0][0].publish(publications[0][1])
                except ValueError:
                    pass
                else:
                    raise AssertionError("revoked completed Program published a Forgejo effect")
                assert not native_state["sends"]
                return {"status": "completed-revoked", "native_sends": 0}
            for sync, raw_handoff in publications:
                handoff = raw_handoff
                if exercise in {"topic-crash", "topic-artifact-conflict"}:
                    finalize = SQLiteProgramStore.finalize_artifact

                    def fail_topic(
                        store_arg: SQLiteProgramStore,
                        operation: str,
                        name: str,
                        data: bytes,
                        original: Callable[[SQLiteProgramStore, str, str, bytes], str] = finalize,
                    ) -> str:
                        if operation.startswith("ordinary-publication-topic:") and name == "topic":
                            raise RuntimeError("synthetic topic artifact crash")
                        return original(store_arg, operation, name, data)

                    with patch.object(SQLiteProgramStore, "finalize_artifact", fail_topic):
                        try:
                            sync.publish(handoff)
                        except RuntimeError as error:
                            assert str(error) == "synthetic topic artifact crash"
                        else:
                            raise AssertionError("topic crash was not exercised")
                    assert not native_state["sends"]
                    key = "ordinary-publication-topic:" + handoff.claim.attempt.attempt_id
                    original_request = store.operation(key).request_json
                    original_topic = json.loads(json.loads(original_request)["request"]["bytes"])["topic"]
                    if exercise == "topic-artifact-conflict":
                        store.finalize_artifact(key, "topic", b"kernel-pr-00000000000000000000000000000000")
                    old_workspace = sync.workspace
                    old_tree = old_workspace.workspace
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
                        execution_profile=app.execution_profile,
                    )
                    workspace = OrdinaryGitWorkspace(
                        store,
                        old_tree,
                        source,
                        repository_url,
                        proposal,
                        old_workspace.commands,
                        ("Fixture Owner", "owner@example.invalid"),
                        authority_deadline=approved.expires_at,
                    )
                    workspace.restore(handoff.attempt)
                    control = WorkOwnership(
                        store, Principal("owner", "owner"), old_tree, mode=HandoffMode.PULL, profile=COOPERATIVE_PROFILE
                    )
                    workspace.control = control
                    pub_git = ConditionalGitTransport(
                        repository=Reference("forgejo:team/project"),
                        remote_url=repository_url,
                        object_source=workspace.objects,
                    )
                    pub_git.git_binary = str(git_peer)
                    resumed = OrdinaryForgejoSync(app, approved, control, workspace, http(), pub_git, "fixture-owner")
                    if exercise == "topic-artifact-conflict":
                        try:
                            resumed.publish(handoff)
                        except IntakeRefused:
                            pass
                        else:
                            raise AssertionError("topic artifact inconsistent with immutable intent was accepted")
                        assert store.operation(key).request_json == original_request
                        assert not native_state["sends"]
                        return {"status": "unknown", "native_sends": 0}
                    first = resumed.publish(handoff)
                    assert all(item.startswith("forgejo:") for item in first)
                    assert resumed.publish(handoff) == first
                    assert store.operation(key).request_json == original_request
                    assert store.artifact(key, "topic").decode() == original_topic
                    assert len(cast(list[object], native_state["sends"])) == 2
                    return {"status": "topic-recovered", "native_sends": 2}
                first = sync.publish(handoff)
                if exercise in {"duplicate-comments", "oversized-comments"}:
                    assert first == ("unknown", "held")
                    sends = 2 if exercise == "duplicate-comments" else 1
                    assert sync.publish(handoff) == first
                    assert len(cast(list[object], native_state["sends"])) == sends
                    return {"status": "unknown", "native_sends": sends}
                if exercise == "ambiguous-pr":
                    assert first == ("unknown", "held")
                    assert sync.publish(handoff) == first
                    assert len(cast(list[object], native_state["sends"])) == 1
                    uncertain = export_private_outcome(
                        store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                    )
                    native = cast(list[dict[str, object]], uncertain["native_effects"])
                    assert len(native) == 1 and native[0]["action"] == "pr"
                    assert native[0]["receipt_recorded"] is False and native[0]["current_remote_state"] is None
                    assert len(cast(list[object], native_state["sends"])) == 1
                    return {"status": "unknown", "native_sends": 1}
                assert all(item.startswith("forgejo:") for item in first), first
                repeated = sync.publish(handoff)
                assert repeated == first, (first, repeated)
            assert len(cast(list[object], native_state["prs"])) == len(units) + (1 if exercise == "branch-pr" else 0)
            assert len(cast(list[object], native_state["comments"])) == len(units) + (
                31 if exercise == "many-comments" else 0
            )
            assert native_state["lost_replies"] == (1 if exercise == "lost-reply" else 0)
            assert native_state["lost_pr_replies"] == (1 if exercise == "branch-pr" else 0)
            assert len(set(sessions)) == len(units) and all(
                item.startswith("ses_") and item != "ses_fixture" for item in sessions
            )
            assert calls
            if exercise in {"private-outcome-export", "review-remediation", "lost-reply"}:
                if exercise == "review-remediation":
                    for number, original_attempt in enumerate(store.load("everyday-program").attempts):
                        operation = f"runtime:{original_attempt.attempt_id}"
                        usage = UsageObservation(
                            f"synthetic-usage-{number}",
                            original_attempt.attempt_id,
                            f"synthetic-receipt-{number}",
                            "money",
                            "USD",
                            Decimal(number + 1),
                            Provenance.OWNER_RECORD,
                            "synthetic-owner",
                            100,
                        )
                        store.record_usage(operation, usage)
                        if number == 0:
                            store.record_usage(
                                operation,
                                replace(
                                    usage,
                                    observation_id="synthetic-correction",
                                    quantity=Decimal(5),
                                    supersedes_id=usage.observation_id,
                                ),
                            )
                before = store.served_change_token()
                outcome_export = export_private_outcome(
                    store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                )
                assert store.served_change_token() == before
                assert cast(dict[str, object], outcome_export["program"])["status"] == "completed"
                projected_attempts = cast(list[dict[str, object]], outcome_export["attempts"])
                assert len(projected_attempts) == (3 if exercise == "review-remediation" else 2)
                satisfactions = cast(list[dict[str, object]], outcome_export["satisfactions"])
                assert len(satisfactions) == 2
                attempt_refs = {item["id"] for item in projected_attempts}
                satisfaction_refs = {item["id"] for item in satisfactions}
                assert all(item["attempt"] in attempt_refs for item in satisfactions)
                assert all(
                    item["attempt"] in attempt_refs
                    for item in cast(list[dict[str, object]], outcome_export["usage_history"])
                )
                if exercise == "review-remediation":
                    history = cast(list[dict[str, object]], outcome_export["usage_history"])
                    assert len(history) == 4 and all(item["attempt_binding"] == "recorded" for item in history)
                    assert {item["attempt"] for item in history} == attempt_refs
                    assert cast(dict[str, object], outcome_export["coverage"])["unattributed_usage_observations"] == 0
                    assert sorted(cast(str, item["quantity"]) for item in history if item["effective"]) == [
                        "2",
                        "3",
                        "5",
                    ]
                assert any(
                    binding["source_kind"] == "predecessor" and binding["satisfaction"] in satisfaction_refs
                    for item in projected_attempts
                    for binding in cast(list[dict[str, object]], item["inputs"])
                )
                reviews = cast(list[dict[str, object]], outcome_export["reviews"])
                assert len(reviews) == (3 if exercise == "review-remediation" else 2)
                assert sum(item["verdict"] == "approve" for item in reviews) == 2
                checks = cast(list[dict[str, object]], outcome_export["checks"])
                assert checks and all(item["attempt"] in attempt_refs for item in checks)
                candidates = cast(list[dict[str, object]], outcome_export["candidates"])
                assert len(candidates) == len(projected_attempts)
                assert all(item["candidate"] in {candidate["candidate"] for candidate in candidates} for item in checks)
                assert all(type(item["passed"]) is bool and item["freshness"] == "not-evaluated" for item in checks)
                assert len({(item["attempt"], item["evidence"]) for item in checks}) == len(checks)
                assert any(item["operations"] and item["manifests"] for item in checks)
                effects = cast(list[dict[str, object]], outcome_export["effects"])
                assert any(item["category"] == "verification" for item in effects)
                assert all(item["category"] is not None for item in effects)
                native_subjects = cast(list[dict[str, object]], outcome_export["native_subjects"])
                native_effects = cast(list[dict[str, object]], outcome_export["native_effects"])
                assert len(native_subjects) == len(projected_attempts) and len(native_effects) == 4
                assert {item["action"] for item in native_effects} == {"pr", "receipt"}
                for item in native_effects:
                    original = next(subject for subject in native_subjects if subject["attempt"] == item["attempt"])
                    assert item["issue"] == original["issue"] and item["work_unit"] == original["work_unit"]
                    assert item["receipt_recorded"] is True and item["receipt_profile"] == "forgejo-native-readback:v1"
                    assert item["current_remote_state"] is None and item["satisfaction"] in satisfaction_refs
                claims = cast(list[dict[str, object]], outcome_export["claims"])
                assert len(claims) == len(projected_attempts)
                assert all(item["resources"] and item["handoff_proofs"] for item in claims)
                for item in claims:
                    if item["mode"] == "managed":
                        stages = {proof["stage"] for proof in cast(list[dict[str, object]], item["handoff_proofs"])}
                        assert "start" in stages and "complete" in stages
                review_sessions = {
                    item["reviewer_session"] for item in reviews if item.get("reviewer_session") is not None
                }
                assert all(
                    item["reviewer_session"] in review_sessions
                    for item in checks
                    if item["reviewer_session"] is not None
                )
                assert all(item["candidate"] in {review["candidate"] for review in reviews} for item in checks)
                if exercise == "review-remediation":
                    assert any(item["verdict"] == "request_changes" for item in reviews)
                    assert any(item["remediation_of"] in attempt_refs for item in projected_attempts)
                    decisions = cast(list[dict[str, object]], outcome_export["decisions"])
                    assert decisions[0]["review_status"] == "changes_requested"
                    assert decisions[0]["review_operation"] in {item["operation"] for item in reviews}
                    review = next(item for item in reviews if item["verdict"] == "request_changes")
                    review_findings = cast(list[dict[str, object]], review["findings"])
                    assert review_findings
                    remediation = next(item for item in projected_attempts if item["remediation_of"] is not None)
                    assert remediation["remediation_of"] == review["attempt"]
                    remediation_findings = cast(list[dict[str, object]], remediation["remediation_findings"])
                    assert all(item in remediation_findings for item in review_findings)
                    findings = cast(list[dict[str, object]], outcome_export["findings"])
                    assert {item["source"] for item in findings} >= {"product-review", "verification-manifest"}
                    assert {item["id"] for item in findings if item["source"] == "product-review"} == {
                        item["id"] for item in review_findings
                    }
                    assert b"Implement the approved edge case." not in encode(outcome_export)
                assert cast(dict[str, object], outcome_export["coverage"])["complete_cost"] is None
                assert b"A tightly bounded useful edit" not in encode(outcome_export)
                assert b"team/project" not in encode(outcome_export) and b"fixture-owner" not in encode(outcome_export)
                assert b"synthetic-owner" not in encode(outcome_export)
            if exercise == "review-remediation-served":
                snapshot = OwnerProjection(store, "everyday-program").snapshot()
                prior = [item for item in snapshot.decisions if item.get("status") == "changes_requested"]
                assert prior and all(item.get("applicability") == "historical" for item in prior)
                sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                with sender, receiver:
                    LocalDecisionNotifier(sender).publish(snapshot)
                    assert not select.select([receiver], [], [], 0)[0]
            return {
                "status": "completed",
                "mode": mode,
                "sessions": sessions,
                "native_kilo": native_binary is not None,
                "review_calls": len(calls),
                "satisfactions": len(store.load("everyday-program").satisfactions),
                "native_sends": len(cast(list[object], native_state["sends"])),
            }
