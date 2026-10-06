# SPDX-License-Identifier: Apache-2.0
"""Qualified Forge reads and bounded literal-content relevance; no recipe execution."""

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from creatidy_kernel.adapters.forge_refs import ForgeBinding, oid
from creatidy_kernel.core.forge import Presence, Reference
from creatidy_kernel.core.intake import (
    ContentCriterion,
    Declaration,
    Disposition,
    IntakeEvidence,
    IntakeRefused,
    IssueSubject,
    digest,
    encode,
    strings,
)
from creatidy_kernel.ports.intake import BaselineReader, ContentSource, IntakeForge


class GitContentSource:
    """Read exact Git objects only. Trusted policy supplies literal file obligations.

    No working-tree tooling, hooks, tests, model inference, or semantic classifier.
    Missing objects/non-blob paths are unknown; never infer semantic absence.
    """

    def __init__(self, repository: Path, binding: ForgeBinding) -> None:
        self.repository = repository
        self.binding = binding

    def matches(self, subject: IssueSubject, revision: str, criteria: tuple[ContentCriterion, ...]) -> bool | None:
        if self.binding != ForgeBinding(subject.origin, subject.repository):
            raise IntakeRefused("source binding differs")
        oid(revision)
        git = shutil.which("git", path=os.defpath)
        if git is None:
            return None
        result = True
        for criterion in criteria:
            path = PurePosixPath(criterion.path)
            if path.is_absolute() or ".." in path.parts or criterion.path in {"", "."}:
                raise IntakeRefused("invalid content criterion path")
            try:
                # Use bounded cat-file -s before reading; no repository executable is invoked.
                environment = {
                    "HOME": "/tmp",  # noqa: S108 - Operational value only, no files/authority stored here.
                    "PATH": os.defpath,
                    "LANG": "C.UTF-8",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_NO_LAZY_FETCH": "1",
                }
                argv = [git, "--no-replace-objects", "-C", str(self.repository), "cat-file"]
                object_name = f"{revision}:{criterion.path}"
                entry = subprocess.run(  # noqa: S603 - Resolved system Git; literal read, no hooks or shell.
                    [
                        git,
                        "--no-replace-objects",
                        "--literal-pathspecs",
                        "-C",
                        str(self.repository),
                        "ls-tree",
                        "-z",
                        revision,
                        "--",
                        criterion.path,
                    ],
                    env=environment,
                    capture_output=True,
                    timeout=5,
                    check=True,
                ).stdout
                metadata, separator, name = entry.partition(b"\t")
                fields = metadata.split()
                if (
                    not separator
                    or len(fields) != 3
                    or fields[0] not in {b"100644", b"100755"}
                    or fields[1] != b"blob"
                    or name != criterion.path.encode() + b"\x00"
                ):
                    return None
                size = subprocess.run(  # noqa: S603 - Fixed Git argv; exact OID and relative trusted path, no shell.
                    [*argv, "-s", object_name], env=environment, capture_output=True, timeout=5, check=True
                ).stdout
                if not 0 <= int(size) <= 262144:
                    return None
                observed = subprocess.run(  # noqa: S603 - Reads only a bounded blob; never runs repository code.
                    [*argv, "blob", object_name], env=environment, capture_output=True, timeout=5, check=True
                ).stdout
            except (OSError, ValueError, subprocess.SubprocessError):
                return None
            result = result and observed == criterion.expected
        return result if criteria else None


class ForgeIntakeEvidence:
    """Supported positive paths require controller-owned exact-content criteria.

    Other issues still produce reviewable unknown drafts. Stable repeated bounded
    scans are observations, not a global serializable remote snapshot guarantee.
    Every PR head/state and branch/issue subject is re-read before disposition.
    """

    def __init__(
        self,
        forge: IntakeForge,
        binding: ForgeBinding,
        source: ContentSource,
        baseline: BaselineReader,
        criteria: tuple[ContentCriterion, ...],
        producer: str,
        clock: Callable[[], int],
        *,
        max_pages: int,
    ) -> None:
        if max_pages <= 0 or not producer:
            raise IntakeRefused("bounded scan and identified producer required")
        self.forge = forge
        self.binding = binding
        self.source = source
        self.baseline = baseline
        self.criteria = criteria
        self.producer = producer
        self.clock = clock
        self.max_pages = max_pages

    def _scan(self, repository: Reference) -> tuple[tuple[Reference, ...], Disposition | None]:
        cursor = None
        references: list[Reference] = []
        seen: set[str] = set()
        for _ in range(self.max_pages):
            page = self.forge.intake_changes(repository, cursor)
            for observation in page.items:
                if observation.presence is Presence.INACCESSIBLE:
                    return (), Disposition.PERMISSION
                if observation.presence is not Presence.FOUND or observation.reference is None:
                    return (), Disposition.INCOMPLETE
                if observation.reference in references:
                    return (), Disposition.INCOMPLETE
                references.append(observation.reference)
            if page.complete and page.next_cursor is None:
                return tuple(references), None
            if page.next_cursor is None or page.next_cursor in seen:
                return (), Disposition.INCOMPLETE
            seen.add(page.next_cursor)
            cursor = page.next_cursor
        return (), Disposition.INCOMPLETE

    def read(self, subject: IssueSubject, declaration: Declaration) -> IntakeEvidence:
        if self.binding != ForgeBinding(subject.origin, subject.repository):
            raise IntakeRefused("forge binding differs from selected origin")
        repository = Reference(f"forgejo:{subject.repository}")
        issue = self.forge.issue_snapshot(repository, subject.issue)
        branch = self.forge.branch(repository, subject.branch)
        recipe = digest(encode(declaration.value["recipes"]))
        baseline = self.baseline.read_baseline(subject, recipe)
        baseline_valid = (
            baseline is not None
            and baseline.subject == subject
            and baseline.recipe_digest == recipe
            and type(baseline.result) is str
        )
        source_revision = "unknown" if branch.revision is None else branch.revision.value.removeprefix("forgejo:")
        disposition = Disposition.EQUIVALENCE
        proof: list[object] = []
        supported = tuple(item.criterion for item in self.criteria) == strings(declaration.value["criteria"])
        if issue.presence is Presence.INACCESSIBLE or branch.presence is Presence.INACCESSIBLE:
            disposition = Disposition.PERMISSION
        elif issue.presence is not Presence.FOUND or branch.presence is not Presence.FOUND:
            disposition = Disposition.INCOMPLETE
        elif source_revision != subject.base:
            disposition = Disposition.STALE
        elif supported:
            implemented = self.source.matches(subject, subject.base, self.criteria)
            proof.append(
                {
                    "source": subject.base,
                    "content_result": implemented,
                    "criteria": [
                        {"criterion": item.criterion, "path": item.path, "expected_digest": digest(item.expected)}
                        for item in self.criteria
                    ],
                }
            )
            refs, incomplete = self._scan(repository)
            states = tuple(self.forge.change_snapshot(repository, ref) for ref in refs)
            active = False
            unresolved = implemented is None
            for state in states:
                observation = state.observation
                matched = None
                if observation.presence is Presence.INACCESSIBLE:
                    incomplete = Disposition.PERMISSION
                elif observation.presence is not Presence.FOUND or state.target_branch is None:
                    incomplete = Disposition.INCOMPLETE
                elif state.state == "open" and state.target_branch == subject.branch:
                    if observation.head is None or observation.base != Reference(f"forgejo:{subject.base}"):
                        unresolved = True
                    else:
                        matched = self.source.matches(
                            subject, observation.head.value.removeprefix("forgejo:"), self.criteria
                        )
                        active = active or matched is True
                        unresolved = unresolved or matched is None
                proof.append(
                    {
                        "reference": None if observation.reference is None else observation.reference.value,
                        "head": None if observation.head is None else observation.head.value,
                        "base": None if observation.base is None else observation.base.value,
                        "target_branch": state.target_branch,
                        "state": state.state,
                        "merged": state.merged,
                        "updated_at": state.updated_at,
                        "content_result": matched,
                    }
                )
            again, changed = self._scan(repository)
            if refs != again or states != tuple(self.forge.change_snapshot(repository, ref) for ref in again):
                incomplete = Disposition.INCOMPLETE
            incomplete = incomplete or changed
            if incomplete is not None:
                disposition = incomplete
            elif implemented:
                disposition = Disposition.IMPLEMENTED
            elif active:
                disposition = Disposition.ACTIVE_PR
            elif unresolved:
                disposition = Disposition.EQUIVALENCE
            elif not baseline_valid or baseline is None or baseline.result not in {"passed", "failed"}:
                disposition = Disposition.BASELINE
            else:
                disposition = Disposition.REMAINS
        closing_issue = self.forge.issue_snapshot(repository, subject.issue)
        closing_branch = self.forge.branch(repository, subject.branch)
        if issue != closing_issue or branch != closing_branch:
            disposition = Disposition.STALE
        closing_baseline = self.baseline.read_baseline(subject, recipe)
        now = self.clock()
        # Close the finite evidence cut after Forge reads. Timestamps validate age,
        # not receipt identity: rereading the same receipt cannot renew its age.
        if (
            baseline is None
            or closing_baseline is None
            or (baseline.subject, baseline.recipe_digest, baseline.producer, baseline.reference, baseline.result)
            != (
                closing_baseline.subject,
                closing_baseline.recipe_digest,
                closing_baseline.producer,
                closing_baseline.reference,
                closing_baseline.result,
            )
        ):
            baseline_valid = False
        else:
            baseline_valid = all(
                snapshot.subject == subject
                and snapshot.recipe_digest == recipe
                and type(snapshot.observed_at) is int
                and 0 < snapshot.observed_at <= now
                and type(snapshot.producer) is str
                and bool(snapshot.producer.strip())
                and type(snapshot.reference) is str
                and bool(snapshot.reference.strip())
                and type(snapshot.result) is str
                and snapshot.result in {"passed", "failed"}
                for snapshot in (baseline, closing_baseline)
            )
        if not baseline_valid and disposition is Disposition.REMAINS:
            disposition = Disposition.BASELINE
        return IntakeEvidence(
            subject,
            issue.content_digest,
            issue.updated_at,
            issue.state,
            source_revision,
            recipe,
            "unknown" if not baseline_valid or baseline is None else baseline.producer,
            "unknown" if not baseline_valid or baseline is None else baseline.reference,
            "unknown" if not baseline_valid or baseline is None else baseline.result,
            self.producer,
            f"sha256:{digest(encode(proof))}",
            encode(proof),
            disposition,
            now
            if not baseline_valid or baseline is None or closing_baseline is None
            else min(baseline.observed_at, closing_baseline.observed_at),
        )
