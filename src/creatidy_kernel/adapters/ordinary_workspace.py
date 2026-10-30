# SPDX-License-Identifier: Apache-2.0
"""Owned ordinary source/candidate/check adapter, independent of frozen research tasks.

Workers edit only the owned leaf. Git objects, indices, results and command-owner
witnesses remain in the protected parent/store. No task recipes come from output.
"""

import base64
import os
import shutil
import stat
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.forgejo_transport import GIT_OUTPUT_ERRORS, run_git_bounded
from creatidy_kernel.adapters.owned_workspace import LocalOwnedWorkspace
from creatidy_kernel.adapters.source_cache import source_use, validate_checkout
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.adapters.task_execution import (
    OwnedVerificationCommand,
    VerificationCommand,
    run_verification_command,
)
from creatidy_kernel.core.domain import AttemptSpec
from creatidy_kernel.core.execution import Artifact, ArtifactManifest, Candidate
from creatidy_kernel.core.intake import IntakeRefused, digest, encode, object_from, strings
from creatidy_kernel.core.ordinary import OrdinaryProposal
from creatidy_kernel.core.ownership import WorkClaim
from creatidy_kernel.core.verification import Evidence, EvidenceSubject
from creatidy_kernel.core.work_budget import WorkKind
from creatidy_kernel.ports.application import BudgetedChecks, Collection
from creatidy_kernel.ports.ownership import WorkOwnership


class OrdinaryGitWorkspace:
    def __init__(
        self,
        store: SQLiteProgramStore,
        workspace: LocalOwnedWorkspace,
        source: Path,
        repository_url: str,
        proposal: OrdinaryProposal,
        commands: tuple[VerificationCommand, ...],
        author: tuple[str, str],
        *,
        authority_deadline: int,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not commands or not all(type(item) is VerificationCommand for item in commands) or not all(author):
            raise IntakeRefused("trusted finite verification argv and explicit commit author required")
        self.store, self.workspace, self.source = store, workspace, source
        self.repository_url, self.proposal, self.commands, self.author = repository_url, proposal, commands, author
        self.max_bytes = max_bytes
        if type(authority_deadline) is not int or authority_deadline <= 0:
            raise IntakeRefused("original approved authority deadline required")
        self.authority_deadline = authority_deadline
        self.objects = workspace.path.parent / ("." + workspace.path.name + ".objects")
        self.base: str | None = None
        self.input_tree: str | None = None
        self.prepared_revision: str | None = None
        self.control: WorkOwnership | None = None
        self.claim: WorkClaim | None = None
        self._head: str | None = None
        self._paths: tuple[str, ...] = ()
        self._attempt: AttemptSpec | None = None
        self._owners: dict[str, OwnedVerificationCommand] = {}
        self.collected: tuple[Candidate, Collection] | None = None
        self.collected_scope: str | None = None

    @property
    def reference(self) -> str:
        return str(self.workspace.path)

    def _git(self, *arguments: str, index: Path | None = None, source: Path | None = None) -> bytes:
        binary = shutil.which("git", path=os.defpath)
        if binary is None:
            raise IntakeRefused("Git unavailable")
        root = self.objects if source is None else source
        environment = {
            "PATH": os.defpath,
            "HOME": str(self.objects.parent),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": self.author[0],
            "GIT_AUTHOR_EMAIL": self.author[1],
            "GIT_COMMITTER_NAME": self.author[0],
            "GIT_COMMITTER_EMAIL": self.author[1],
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
        if index is not None:
            environment["GIT_INDEX_FILE"] = str(index)
        token = GIT_OUTPUT_ERRORS.set("surrogateescape")
        try:
            result = run_git_bounded(
                [binary, "-c", "core.hooksPath=/dev/null", "-C", str(root), *arguments],
                root,
                environment,
                60,
                self.max_bytes,
            )
        finally:
            GIT_OUTPUT_ERRORS.reset(token)
        if result.returncode != 0:
            raise IntakeRefused("bounded ordinary Git operation refused")
        return result.stdout.encode("utf-8", "surrogateescape")

    def _text(self, *arguments: str, index: Path | None = None) -> str:
        return self._git(*arguments, index=index).decode().strip()

    def initialize(self, attempt: AttemptSpec) -> str:
        key = "ordinary-workspace:" + attempt.attempt_id
        previous = self.store.find_artifact(key, "prepared")
        if previous is not None:
            self.restore(attempt)
            subject = cast(str, object_from(previous)["prepared_subject"])
            if self.workspace.capture()[0] != subject:
                raise IntakeRefused("prepared workspace changed; preserve and reconcile explicitly")
            return subject
        draft = next(item for item in self.proposal.drafts if item.task_id == attempt.work_unit_id)
        empty = object_from(self.workspace.capture()[1])
        if empty["entries"]:
            raise IntakeRefused("new ordinary handoff requires a fresh owned leaf; preserve existing bytes")
        if self.objects.exists():
            raise IntakeRefused("unclaimed ordinary object directory retained, not adopted")
        self.objects.mkdir(mode=0o700)
        self._git("init", "--bare", "--template=", str(self.objects), source=self.objects.parent)
        subject = draft.evidence.subject
        with source_use(self.source, self.repository_url) as leased:
            validate_checkout(leased)
            self._git("fetch", "--no-tags", "--no-write-fetch-head", str(leased), subject.base)
        self.base = subject.base
        index = self.objects / "controller-index"
        self._git("read-tree", subject.base, index=index)
        program = self.store.load(attempt.program_id)
        for binding in attempt.effective_inputs:
            if binding.source_kind != "predecessor":
                continue
            fact = next(item for item in program.satisfactions if item.reference_id == binding.satisfaction_id)
            patch = self.store.artifact(f"runtime:{fact.source_attempt_id}", "candidate.patch")
            if binding.reference != "artifact:sha256:" + digest(patch):
                raise IntakeRefused("predecessor output bytes differ from the accepted immutable input")
            saved = self.objects / ("input-" + digest(patch) + ".patch")
            saved.write_bytes(patch)
            self._git("apply", "--cached", "--3way", "--binary", str(saved), index=index)
        self.input_tree = self._text("write-tree", index=index)
        self.prepared_revision = (
            subject.base
            if self.input_tree == self._text("rev-parse", subject.base + "^{tree}")
            else self._text(
                "commit-tree", self.input_tree, "-p", subject.base, "-m", "Prepared ordinary inputs " + attempt.digest
            )
        )
        expected_files: dict[str, tuple[bytes, bytes]] = {}
        expected_directories: set[str] = set()
        for item in self._git("ls-tree", "-r", "-z", self.input_tree).split(b"\0"):
            if not item:
                continue
            metadata, raw_path = item.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            path = raw_path.decode()
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise IntakeRefused("ordinary profile requires regular source files; no symlink/submodule adoption")
            target = self.workspace.path / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            contents = self._git("cat-file", "blob", oid.decode())
            expected_files[path] = (mode, contents)
            expected_directories.update(str(parent) for parent in Path(path).parents if str(parent) != ".")
            target.write_bytes(contents)
            target.chmod(0o755 if mode == b"100755" else 0o644)
        self._attempt = attempt
        subject, captured = self.workspace.capture()
        entries = cast(list[dict[str, object]], object_from(captured)["entries"])
        if {cast(str, item["path"]) for item in entries} != set(expected_files) | expected_directories:
            raise IntakeRefused("prepared workspace contains unexpected entries; preserve and reconcile")
        for item in entries:
            path = cast(str, item["path"])
            mode = cast(int, item["mode"])
            if path in expected_directories:
                if not stat.S_ISDIR(mode):
                    raise IntakeRefused("prepared workspace directory differs")
            else:
                expected_mode, expected_bytes = expected_files[path]
                if (
                    not stat.S_ISREG(mode)
                    or bool(mode & 0o111) != (expected_mode == b"100755")
                    or base64.b64decode(cast(str, item.get("bytes", "")), validate=True) != expected_bytes
                ):
                    raise IntakeRefused("prepared workspace bytes differ from exact approved Git tree")
        self.store.intent(
            key,
            key,
            {"kind": "ordinary-intake-record", "version": 1, "bytes": encode({"attempt": attempt.digest}).decode()},
        )
        self.store.finalize_artifact(
            key,
            "prepared",
            encode(
                {
                    "version": 1,
                    "attempt": attempt.digest,
                    "source": self.repository_url,
                    "proposal": self.proposal.digest,
                    "workspace": self.reference,
                    "base": self.base,
                    "input_tree": self.input_tree,
                    "prepared_revision": self.prepared_revision,
                    "prepared_subject": subject,
                    "commands": [asdict(command) for command in self.commands],
                    "author": self.author,
                    "deadline": self.authority_deadline,
                }
            ),
        )
        return subject

    def restore(self, attempt: AttemptSpec) -> None:
        value = object_from(self.store.artifact("ordinary-workspace:" + attempt.attempt_id, "prepared"))
        expected = {
            "version": 1,
            "attempt": attempt.digest,
            "source": self.repository_url,
            "proposal": self.proposal.digest,
            "workspace": self.reference,
            "commands": [asdict(command) for command in self.commands],
            "author": self.author,
            "deadline": self.authority_deadline,
        }
        if any(encode(value.get(key)) != encode(item) for key, item in expected.items()):
            raise IntakeRefused("original prepared workspace/profile differs; never adopt or rematerialize")
        self.base = cast(str, value["base"])
        self.input_tree = cast(str, value["input_tree"])
        self.prepared_revision = cast(str, value["prepared_revision"])
        self._attempt = attempt
        if self._text("rev-parse", self.prepared_revision + "^{tree}") != self.input_tree:
            raise IntakeRefused("original prepared Git objects unavailable")
        raw = self.store.find_artifact(f"runtime:{attempt.attempt_id}", "ordinary-candidate")
        if raw is not None:
            candidate = object_from(raw)
            self._head = cast(str, candidate["head"])
            self._paths = tuple(cast(list[str], candidate["paths"]))
            self.collected_scope = cast(str, candidate["scope"])
            if self.workspace.capture()[0] != self.collected_scope:
                raise IntakeRefused("candidate workspace changed after retained collection")
            patch = self.store.artifact(f"runtime:{attempt.attempt_id}", "candidate.patch")
            if self._git("diff", "--binary", self.base, self._head, "--") != patch:
                raise IntakeRefused("candidate Git objects differ from original immutable patch")
            manifest = ArtifactManifest(self.reference, (Artifact("candidate.patch", "sha256:" + digest(patch)),))
            self.collected = (
                Candidate(attempt.attempt_id, attempt.digest, manifest),
                Collection(manifest, self.repository_url, self.base, self._head, self),
            )

    def collect(self, attempt: AttemptSpec, claim: WorkClaim) -> tuple[Candidate, Collection]:
        if self._attempt != attempt or self.base is None or self.input_tree is None:
            raise IntakeRefused("original ordinary source binding unavailable")
        before, raw = self.workspace.capture()
        if self.collected is not None:
            if before != self.collected_scope:
                raise IntakeRefused(
                    "retained candidate differs after native completion; preserve and reconcile explicitly"
                )
            self.claim = claim
            return self.collected
        value = object_from(raw)
        index = self.objects / "candidate-index"
        if index.exists():
            raise IntakeRefused(
                "candidate index already exists; use retained result, never overwrite unknown collection"
            )
        self._git("read-tree", "--empty", index=index)
        for raw_item in cast(list[object], value["entries"]):
            item = cast(dict[str, object], raw_item)
            mode = cast(int, item["mode"])
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode) or "bytes" not in item:
                raise IntakeRefused("unsupported candidate entry; preserve it")
            data = base64.b64decode(cast(str, item["bytes"]), validate=True)
            with tempfile.NamedTemporaryFile(dir=self.objects, prefix="blob-") as blob:
                _ = blob.write(data)
                blob.flush()
                oid = self._text("hash-object", "-w", "--", blob.name)
            path = cast(str, item["path"])
            self._git(
                "update-index", "--add", "--cacheinfo", "100755" if mode & 0o111 else "100644", oid, path, index=index
            )
        tree = self._text("write-tree", index=index)
        head = self._text(
            "commit-tree", tree, "-p", self.prepared_revision or self.base, "-m", "Ordinary candidate " + attempt.digest
        )
        patch = self._git("diff", "--binary", self.base, head, "--")
        self._paths = tuple(
            path.decode() for path in self._git("diff", "--name-only", "-z", self.input_tree, head).split(b"\0") if path
        )
        if self.workspace.capture()[0] != before:
            raise IntakeRefused("candidate changed while collecting exact bytes")
        self._head, self.claim = head, claim
        manifest = ArtifactManifest(self.reference, (Artifact("candidate.patch", "sha256:" + digest(patch)),))
        candidate = Candidate(attempt.attempt_id, attempt.digest, manifest)
        self.store.finalize_artifact(f"runtime:{attempt.attempt_id}", "candidate.patch", patch)
        self.store.finalize_artifact(
            f"runtime:{attempt.attempt_id}",
            "ordinary-candidate",
            encode(
                {
                    "version": 1,
                    "head": head,
                    "paths": self._paths,
                    "scope": before,
                }
            ),
        )
        self.collected_scope = before
        self.collected = candidate, Collection(manifest, self.repository_url, self.base, head, self)
        return self.collected

    def _evidence(
        self, name: str, subject: EvidenceSubject, passed: bool, payload: object, *, observed_at: int | None = None
    ) -> Evidence:
        assert self._attempt is not None  # noqa: S101 - collection establishes original Attempt.
        raw = encode(payload)
        reference = self.store.finalize_artifact(f"runtime:{self._attempt.attempt_id}", name + ":" + digest(raw), raw)
        now = int(time.time()) if observed_at is None else observed_at
        return Evidence(
            reference + ":" + name,
            name,
            subject,
            "ordinary-controller",
            "owned-ordinary:v1",
            reference,
            now,
            now + 900,
            passed,
        )

    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        if self._attempt is None or self.claim is None or self.control is None or self._head != subject.head_revision:
            raise IntakeRefused("exact collected ordinary command owner required")
        attempt, claim, control = self._attempt, self.claim, self.control
        control.candidate_subject(claim)
        if name != "commands":
            # This producer owns only these pure byte/path checks. The existing
            # BudgetedChecks envelope does not wrap physical subprocess execution.
            return BudgetedChecks(
                self.store,
                _OrdinaryPureChecks(self),
                attempt.program_id,
                attempt.attempt_id,
                lambda: int(time.time()),
                delivery=lambda key, seconds: control.dispatch(claim, key, lease_seconds=seconds),
            ).check(name, subject)
        evidence_name = "command-evidence:" + digest(encode(asdict(subject)))
        retained_evidence = self.store.find_artifact(f"runtime:{attempt.attempt_id}", evidence_name)
        if retained_evidence is not None:
            value = object_from(retained_evidence)
            raw_subject = cast(dict[str, object], value.get("subject", {}))
            if set(raw_subject) != set(asdict(subject)):
                raise IntakeRefused("retained command Evidence subject is malformed")
            retained = Evidence(
                cast(str, value["evidence_id"]),
                cast(str, value["check"]),
                EvidenceSubject(**cast(dict[str, str], raw_subject)),
                cast(str, value["producer"]),
                cast(str, value["tool_version"]),
                cast(str, value["artifact_reference"]),
                cast(int, value["observed_at"]),
                cast(int, value["fresh_until"]),
                cast(bool, value["passed"]),
                cast(str | None, value.get("reviewer_session")),
            )
            if retained.subject != subject or retained.check != name or encode(asdict(retained)) != retained_evidence:
                raise IntakeRefused("retained command Evidence differs from the exact candidate")
            return retained
        boundary = self.pure_check("paths", subject)
        integrity = self.pure_check("structural", subject)
        if boundary is None or integrity is None or not boundary.passed or not integrity.passed:
            return None  # Never execute repository code for a known-invalid scope.
        runs: list[dict[str, object]] = []
        command_keys = [
            f"ordinary-command:{attempt.attempt_id}:{subject.candidate_digest}:{ordinal}:{repeat}"
            for ordinal, command in enumerate(self.commands)
            for repeat in range(command.repeats)
        ]
        retained_results = [self.store.find_artifact(key, "result") for key in command_keys]
        if any(item is not None for item in retained_results) and any(item is None for item in retained_results):
            raise IntakeRefused("original command results are incomplete; preserve the partial run")
        for ordinal, command in enumerate(self.commands):
            for repeat in range(command.repeats):
                key = f"ordinary-command:{attempt.attempt_id}:{subject.candidate_digest}:{ordinal}:{repeat}"
                prior = self.store.find_artifact(key, "result")
                if prior is not None:
                    # Only a result published AFTER the original owner observed
                    # group settlement and immutable candidate integrity is reusable.
                    receipt = object_from(prior)
                    if (
                        receipt.get("scope") != self.workspace.capture()[0]
                        or receipt.get("head") != subject.head_revision
                    ):
                        raise IntakeRefused("retained command integrity differs; preserve exact bytes")
                    original = self.store.operation(key)
                    binding = object_from(self.store.artifact(key, "owner"))
                    result = receipt.get("result")
                    if (
                        type(result) is not dict
                        or type(cast(dict[str, object], result).get("observed_at")) is not int
                        or binding.get("operation") != key
                        or binding.get("effect_key") != original.effect_key
                        or binding.get("request_digest") != original.request_digest
                        or binding.get("fence") != original.fence
                        or self.store.artifact(f"runtime:{attempt.attempt_id}", key)
                        != encode(cast(dict[str, object], result))
                    ):
                        raise IntakeRefused("original command result or binding differs")
                    qualified = False
                    for fact in self.store.operations(f"command-settlement:{key}:"):
                        request = object_from(fact.request_json.encode()).get("request")
                        witness = cast(dict[str, object], request).get("witness") if type(request) is dict else None
                        if (
                            type(witness) is dict
                            and all(
                                cast(dict[str, object], witness).get(name) == value for name, value in binding.items()
                            )
                            and cast(dict[str, object], witness).get("owned_group_settled") is True
                            and cast(dict[str, object], witness).get("result_reconciled") is True
                            and cast(dict[str, object], witness).get("result_digest")
                            == "sha256:" + digest(encode(cast(dict[str, object], result)))
                        ):
                            qualified = True
                    if not qualified:
                        raise IntakeRefused("original command settlement witness is unavailable")
                    self.store.record_work_settlement(key, "sha256:" + digest(prior), expected_fence=original.fence)
                    runs.append(cast(dict[str, object], result))
                    continue
                existing = self.store.find_artifact(key, "owner")
                if existing is not None:
                    raise IntakeRefused("original verification outcome/group unknown; no cold subprocess replay")
                self.store.budgeted_intent(
                    attempt.program_id,
                    attempt.attempt_id,
                    WorkKind.TOOL,
                    key,
                    key,
                    {"subject": asdict(subject), "argv": list(command.argv), "repeat": repeat},
                    retained_bytes=65536,
                )
                fence = control.dispatch(claim, key, lease_seconds=command.timeout_seconds + 1)
                owner = OwnedVerificationCommand(self.store.operation(key))
                self._owners[key] = owner
                self.store.finalize_artifact(key, "owner", encode(owner.binding))
                before = self.workspace.capture()[0]

                def authorized(key: str = key, fence: int = fence) -> bool:
                    try:
                        self.store.check_operation_delivery(key, fence, deadline=self.authority_deadline)
                    except ValueError:
                        return False
                    return True

                try:
                    owner.result = run_verification_command(
                        self.workspace.path, command, repeat, {}, authorized, max_bytes=65536, owner=owner
                    )
                finally:
                    owner.persist(self.store, evidence_operation=f"runtime:{attempt.attempt_id}")
                if owner.settled() is not True:
                    raise IntakeRefused("original verification command group remains unknown; preserve resources")
                if self.workspace.capture()[0] != before:
                    raise IntakeRefused("verification command changed exact candidate bytes")
                reference = self.store.finalize_artifact(
                    key, "result", encode({"result": owner.result, "scope": before, "head": subject.head_revision})
                )
                self.store.record_retained_bytes(key, cast(int, owner.result["retained_bytes"]))
                self.store.record_work_settlement(key, reference, expected_fence=fence)
                runs.append(owner.result)
        passed = all(
            item["exit_code"] == 0
            and not any(item.get(field) for field in ("timed_out", "authority_lost", "output_exhausted"))
            for item in runs
        )
        if not runs or any(type(item.get("observed_at")) is not int for item in runs):
            raise IntakeRefused("original command observation times are unavailable")
        observed_at = max(cast(int, item["observed_at"]) for item in runs)
        evidence = self._evidence(name, subject, passed, {"runs": runs}, observed_at=observed_at)
        self.store.finalize_artifact(f"runtime:{attempt.attempt_id}", evidence_name, encode(asdict(evidence)))
        return evidence

    def review_input(self) -> str:
        if self._attempt is None or self._head is None:
            raise IntakeRefused("no collected immutable review subject")
        draft = next(item for item in self.proposal.drafts if item.task_id == self._attempt.work_unit_id)
        return encode(
            {
                "approved": draft.payload(),
                "dependencies": [item.meaning for item in self.proposal.dependencies],
                "base": self.base,
                "head": self._head,
                "candidate_source": object_from(self.workspace.capture()[1]),
                "diff": self._git("diff", "--binary", self.base or "", self._head).decode("utf-8", "replace"),
            }
        ).decode()

    def pure_check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        assert self._attempt is not None  # noqa: S101
        draft = next(item for item in self.proposal.drafts if item.task_id == self._attempt.work_unit_id)
        if name == "paths":
            allowed = strings(draft.declaration.value["paths"])
            passed = bool(self._paths) and all(
                any(path == root or path.startswith(root.rstrip("/") + "/") for root in allowed) for path in self._paths
            )
            return self._evidence(name, subject, passed, {"paths": self._paths, "allowed": allowed})
        if name == "structural":
            # Integrity is deterministic; semantic assertions remain native review.
            submitted = self.store.artifact(f"runtime:{self._attempt.attempt_id}", "ownership-candidate")
            passed = self.workspace.capture()[0] == digest(submitted)
            return self._evidence(name, subject, passed, {"integrity": passed, "head": self._head})
        return None


class _OrdinaryPureChecks:
    def __init__(self, owner: OrdinaryGitWorkspace) -> None:
        self.owner = owner

    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        return self.owner.pure_check(name, subject)
