# SPDX-License-Identifier: Apache-2.0
"""Trusted local ordinary intake composition; no remote approval channel."""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, cast

from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.domain import (
    AmendProgramSpec,
    InputBinding,
    Program,
    ProgramSpec,
    SpecAmendment,
    WorkUnit,
)
from creatidy_kernel.core.forge import Observation, Page, Reference
from creatidy_kernel.core.intake import (
    BaselineSnapshot,
    ChangeSnapshot,
    ContentCriterion,
    Declaration,
    Disposition,
    Draft,
    IntakeEvidence,
    IntakeRefused,
    IssueSnapshot,
    IssueSubject,
    OwnerPolicy,
    RequirementsHandoff,
    TranslationRefusal,
    digest,
    encode,
    object_from,
    strings,
)
from creatidy_kernel.ports.program_store import ApplicationStore, OperationRecord


class EvidenceReader(Protocol):
    def read(self, subject: IssueSubject, declaration: Declaration) -> IntakeEvidence: ...


class IntakeForge(Protocol):
    """Read-only addition; legacy effect/receipt contracts remain unchanged."""

    def issue_snapshot(self, repository: Reference, number: int) -> IssueSnapshot: ...
    def change_snapshot(self, repository: Reference, change: Reference) -> ChangeSnapshot: ...
    def branch(self, repository: Reference, branch: str) -> Observation: ...
    def intake_changes(self, repository: Reference, cursor: str | None = None) -> Page: ...


class ContentSource(Protocol):
    def matches(self, subject: IssueSubject, revision: str, criteria: tuple[ContentCriterion, ...]) -> bool | None: ...


class BaselineReader(Protocol):
    def read_baseline(self, subject: IssueSubject, recipe_digest: str) -> BaselineSnapshot | None: ...


class RequirementsTranslator(Protocol):
    def translate_ordinary(self, handoff: RequirementsHandoff) -> TranslationRefusal: ...


class IntakeStore(ApplicationStore, Protocol):
    def operations(self, prefix: str = "") -> tuple[OperationRecord, ...]: ...
    def find_program(self, program_id: str) -> Program | None: ...


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise IntakeRefused("invalid intake text")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 1:
        raise IntakeRefused("positive intake integer required")
    return value


def _subject_from(value: object) -> IssueSubject:
    subject = object_from(encode(value))
    if set(subject) != {"origin", "repository", "issue", "branch", "base"}:
        raise IntakeRefused("invalid issue subject fields")
    return IssueSubject(
        _text(subject["origin"]),
        _text(subject["repository"]),
        _integer(subject["issue"]),
        _text(subject["branch"]),
        _text(subject["base"]),
    )


def draft_from(raw: bytes) -> Draft:
    """Decode only inert records, never principals, policies, or approved Programs."""
    value = object_from(raw)
    if set(value) != {
        "kind",
        "version",
        "task_id",
        "revision",
        "parent_digest",
        "declaration",
        "evidence",
        "observed_at",
    }:
        raise IntakeRefused("invalid draft fields")
    if value["kind"] != "ordinary-draft" or type(value["version"]) is not int or value["version"] != 1:
        raise IntakeRefused("unsupported draft version")
    evidence = object_from(encode(value["evidence"]))
    selected = _subject_from(evidence.pop("subject", None))
    fields = {
        "issue_digest",
        "issue_updated",
        "issue_state",
        "source",
        "recipe_digest",
        "baseline_producer",
        "baseline_reference",
        "baseline_result",
        "relevance_producer",
        "relevance_reference",
        "relevance_proof",
        "disposition",
    }
    if set(evidence) != fields:
        raise IntakeRefused("invalid evidence fields")
    observed = IntakeEvidence(
        selected,
        _text(evidence["issue_digest"]),
        _text(evidence["issue_updated"]),
        _text(evidence["issue_state"]),
        _text(evidence["source"]),
        _text(evidence["recipe_digest"]),
        _text(evidence["baseline_producer"]),
        _text(evidence["baseline_reference"]),
        _text(evidence["baseline_result"]),
        _text(evidence["relevance_producer"]),
        _text(evidence["relevance_reference"]),
        _text(evidence["relevance_proof"]).encode(),
        Disposition(_text(evidence["disposition"])),
        _integer(value["observed_at"]),
    )
    parent = value["parent_digest"]
    if parent is not None:
        parent = _text(parent)
    return Draft(
        _text(value["task_id"]),
        _integer(value["revision"]),
        parent,
        Declaration(_text(value["declaration"]).encode()),
        observed,
    )


@dataclass(frozen=True, slots=True)
class ApprovedMeaning:
    draft: Draft
    decision_id: str
    decision_bytes: bytes
    program: Program


class OrdinaryIntake:
    """Owner-operated local API. Callers authenticate Principals outside this API.

    Metadata uses the existing immutable operation journal and artifacts. Its
    reserved kind is never claimable, including by the SQLite delivery API.
    """

    def __init__(self, store: IntakeStore, reader: EvidenceReader, clock: Callable[[], int]) -> None:
        self.store = store
        self.reader = reader
        self.clock = clock

    @staticmethod
    def _prefix(task_id: str) -> str:
        if type(task_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", task_id) is None:
            raise IntakeRefused("invalid controller task identity")
        return f"ordinary:{task_id}:draft:"

    def _record(self, key: str, raw: bytes) -> bytes:
        self.store.intent(key, key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()})
        self.store.finalize_artifact(key, "record", raw)
        return raw

    def _recover(self, operation: OperationRecord) -> bytes:
        value = object_from(operation.request_json.encode())
        request = object_from(encode(value.get("request")))
        if (
            set(request) != {"kind", "version", "bytes"}
            or request["kind"] != "ordinary-intake-record"
            or type(request["version"]) is not int
            or request["version"] != 1
            or operation.attempts
        ):
            raise IntakeRefused("unsupported inert record")
        raw = _text(request["bytes"]).encode()
        inner = object_from(raw)
        if (
            type(inner.get("version")) is not int
            or inner["version"] != 1
            or inner.get("kind")
            not in {"ordinary-draft", "ordinary-owner-decision", "ordinary-consumption", "ordinary-baseline-age"}
        ):
            raise IntakeRefused("unsupported ordinary record version or kind")
        if operation.effect_key != operation.operation_id:
            raise IntakeRefused("inert record effect identity differs")
        if inner["kind"] == "ordinary-draft":
            draft = draft_from(raw)
            if operation.operation_id not in {
                f"{self._prefix(draft.task_id)}{draft.revision}",
                f"ordinary:record:ordinary-draft:{draft.task_id}:{draft.revision}",
            }:
                raise IntakeRefused("draft namespace differs from subject")
        elif inner["kind"] == "ordinary-owner-decision":
            if set(inner) != {
                "kind",
                "version",
                "decision_id",
                "issuer",
                "draft_digest",
                "revision",
                "evidence_digest",
                "policy_digest",
                "policy",
                "expires_at",
            }:
                raise IntakeRefused("invalid decision fields")
            decision_id = _text(inner["decision_id"])
            self._prefix(decision_id)
            if operation.operation_id != f"ordinary:decision:{decision_id}":
                raise IntakeRefused("decision namespace differs from subject")
            _text(inner["issuer"])
            _integer(inner["revision"])
            _integer(inner["expires_at"])
        elif inner["kind"] == "ordinary-consumption":
            fields = set(inner)
            if fields not in (
                {"kind", "version", "decision", "draft", "program"},
                {"kind", "version", "decision", "draft", "program", "expected_revision"},
            ):
                raise IntakeRefused("invalid consumption fields")
            if "expected_revision" in inner and (
                type(inner["expected_revision"]) is not int or inner["expected_revision"] < 0
            ):
                raise IntakeRefused("invalid consumption revision")
            _text(inner["decision"])
            _text(inner["draft"])
            _text(inner["program"])
            if (
                re.fullmatch(r"ordinary:consumed:[A-Za-z0-9_-]+:(?:record:)?[1-9][0-9]*", operation.operation_id)
                is None
            ):
                raise IntakeRefused("invalid consumption namespace")
        else:
            if set(inner) != {"kind", "version", "identity", "observed_at"}:
                raise IntakeRefused("invalid baseline age fields")
            identity = object_from(encode(inner["identity"]))
            if set(identity) != {
                "subject",
                "source",
                "recipe_digest",
                "baseline_producer",
                "baseline_reference",
                "baseline_result",
            }:
                raise IntakeRefused("invalid baseline age identity")
            subject = _subject_from(identity["subject"])
            for name in set(identity) - {"subject"}:
                _text(identity[name])
            if (
                identity["source"] != subject.base
                or identity["baseline_result"] not in {"passed", "failed"}
                or re.fullmatch(r"[0-9a-f]{64}", _text(identity["recipe_digest"])) is None
                or identity["baseline_producer"] == "unknown"
                or identity["baseline_reference"] == "unknown"
            ):
                raise IntakeRefused("unresolved baseline age subject")
            observed_at = _integer(inner["observed_at"])
            if operation.operation_id != f"ordinary:baseline-age:{digest(encode(identity))}:{observed_at}":
                raise IntakeRefused("baseline age namespace differs from subject")
        artifact = self.store.find_artifact(operation.operation_id, "record")
        if artifact is None:
            self.store.finalize_artifact(operation.operation_id, "record", raw)
        elif artifact != raw:
            raise IntakeRefused("intake artifact differs from journal")
        return raw

    def history(self, task_id: str) -> tuple[Draft, ...]:
        records = (
            *self.store.operations(self._prefix(task_id)),
            *self.store.operations(f"ordinary:record:ordinary-draft:{task_id}:"),
        )
        drafts: list[Draft] = []
        for record in records:
            raw = self._recover(record)
            if object_from(raw)["kind"] == "ordinary-draft":
                drafts.append(draft_from(raw))
        drafts.sort(key=lambda item: item.revision)
        for index, item in enumerate(drafts):
            if (
                item.task_id != task_id
                or item.revision != index + 1
                or item.parent_digest != (None if index == 0 else drafts[index - 1].digest)
            ):
                raise IntakeRefused("broken immutable draft chain")
        return tuple(drafts)

    def _baseline_age(self, evidence: IntakeEvidence) -> int:
        """Earliest durable receipt age in this store, independent of task aliases/meaning."""
        identity = {
            "subject": evidence.subject.payload(),
            "source": evidence.source,
            "recipe_digest": evidence.recipe_digest,
            "baseline_producer": evidence.baseline_producer,
            "baseline_reference": evidence.baseline_reference,
            "baseline_result": evidence.baseline_result,
        }
        task_ids: set[str] = set()
        known_age: int | None = None
        for record in self.store.operations("ordinary:"):
            inner = object_from(self._recover(record))
            if inner["kind"] == "ordinary-draft":
                task_ids.add(_text(inner["task_id"]))
            elif inner["kind"] == "ordinary-baseline-age" and inner["identity"] == identity:
                age = _integer(inner["observed_at"])
                known_age = age if known_age is None else min(known_age, age)
        for task_id in sorted(task_ids):
            for draft in self.history(task_id):
                old = draft.evidence
                if (
                    evidence.subject,
                    evidence.source,
                    evidence.recipe_digest,
                    evidence.baseline_producer,
                    evidence.baseline_reference,
                    evidence.baseline_result,
                ) == (
                    old.subject,
                    old.source,
                    old.recipe_digest,
                    old.baseline_producer,
                    old.baseline_reference,
                    old.baseline_result,
                ):
                    known_age = old.observed_at if known_age is None else min(known_age, old.observed_at)
        observed_at = _integer(evidence.observed_at)
        if (
            known_age is not None
            and observed_at < known_age
            and evidence.source == evidence.subject.base
            and evidence.baseline_result in {"passed", "failed"}
            and evidence.baseline_producer != "unknown"
            and evidence.baseline_reference != "unknown"
        ):
            # Retain newly learned conservative age even when no semantic draft changes.
            self._record(
                f"ordinary:baseline-age:{digest(encode(identity))}:{observed_at}",
                encode(
                    {
                        "kind": "ordinary-baseline-age",
                        "version": 1,
                        "identity": identity,
                        "observed_at": observed_at,
                    }
                ),
            )
        return observed_at if known_age is None else min(observed_at, known_age)

    def _consumptions(self, task_id: str) -> tuple[OperationRecord, ...]:
        history = {draft.digest: draft for draft in self.history(task_id)}
        records: list[OperationRecord] = []
        for record in self.store.operations(f"ordinary:consumed:{task_id}:"):
            inner = object_from(self._recover(record))
            if inner["kind"] != "ordinary-consumption":
                continue
            draft = history.get(_text(inner["draft"]))
            decision = object_from(self.historical_decision(_text(inner["decision"])))
            if (
                draft is None
                or decision["draft_digest"] != draft.digest
                or decision["revision"] != draft.revision
                or decision["evidence_digest"] != draft.evidence.digest
            ):
                raise IntakeRefused("consumption differs from task subject")
            records.append(record)
        return tuple(records)

    def historical_decision(self, decision_id: str) -> bytes:
        """Recover immutable historical bytes only; no present authority or expiry renewal."""
        raw = self._recover(self.store.operation(f"ordinary:decision:{decision_id}"))
        value = object_from(raw)
        if (
            set(value)
            != {
                "kind",
                "version",
                "decision_id",
                "issuer",
                "draft_digest",
                "revision",
                "evidence_digest",
                "policy_digest",
                "policy",
                "expires_at",
            }
            or value["kind"] != "ordinary-owner-decision"
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["decision_id"] != decision_id
        ):
            raise IntakeRefused("unsupported historical decision")
        _integer(value["revision"])
        _integer(value["expires_at"])
        return raw

    def prepare(
        self, task_id: str, selected: IssueSubject, declaration: Declaration, *, expected_parent: str | None
    ) -> Draft:
        history = self.history(task_id)
        previous = history[-1] if history else None
        if expected_parent != (None if previous is None else previous.digest):
            raise IntakeRefused("draft amendment expected parent differs")
        evidence = self.reader.read(selected, declaration)
        if evidence.subject != selected:
            raise IntakeRefused("evidence subject differs")
        evidence = replace(evidence, observed_at=self._baseline_age(evidence))
        if previous is not None and previous.evidence.subject != selected:
            old = previous.evidence.subject
            if (old.origin, old.repository, old.issue, old.branch) != (
                selected.origin,
                selected.repository,
                selected.issue,
                selected.branch,
            ):
                raise IntakeRefused("task selection identity changed")
        if previous is not None and previous.declaration == declaration and previous.evidence.digest == evidence.digest:
            return previous
        draft = Draft(
            task_id,
            1 if previous is None else previous.revision + 1,
            None if previous is None else previous.digest,
            declaration,
            evidence,
        )
        key = f"{self._prefix(task_id)}{draft.revision}"
        if any(record.operation_id == key for record in self.store.operations(key)):
            # A legacy consumption for task 'draft' can occupy this exact draft key.
            key = f"ordinary:record:ordinary-draft:{task_id}:{draft.revision}"
        self._record(key, encode(draft.payload()))
        return draft

    def _validate(
        self, principal: Principal, draft: Draft, policy: OwnerPolicy, current: IntakeEvidence, expires_at: int
    ) -> None:
        history = self.history(draft.task_id)
        baseline_age = self._baseline_age(current)
        now = self.clock()
        if (
            type(principal) is not Principal
            or principal.role != "owner"
            or principal.actor_id != policy.authority.owner_id
        ):
            raise IntakeRefused("authenticated owner required")
        if (
            type(now) is not int
            or type(expires_at) is not int
            or now >= expires_at
            or current.observed_at > now
            or draft.evidence.observed_at > now
            or now - min(draft.evidence.observed_at, baseline_age) > policy.freshness_seconds
        ):
            raise IntakeRefused("decision or evidence expired")
        if not history or history[-1].digest != draft.digest or current.digest != draft.evidence.digest:
            raise IntakeRefused("revision or evidence changed; explicit amendment required")
        if (
            current.subject.base != current.source
            or current.issue_state != "open"
            or current.disposition is not Disposition.REMAINS
            or current.recipe_digest != digest(encode(draft.declaration.value["recipes"]))
            or current.baseline_producer not in policy.evidence_producers
            or current.relevance_producer not in policy.evidence_producers
            or current.relevance_reference != f"sha256:{digest(current.relevance_proof)}"
        ):
            raise IntakeRefused("required current evidence unresolved")
        if current.baseline_result != "passed" and not (
            current.baseline_result == "failed" and policy.repair_baseline_reference == current.baseline_reference
        ):
            raise IntakeRefused("baseline unavailable or unrelated failure")
        data = draft.declaration.value
        if strings(data["unknowns"]):
            raise IntakeRefused("required meaning remains unknown")
        for name, allowed in (
            ("paths", policy.paths),
            ("network", policy.network),
            ("effects", policy.effects),
            ("recipes", policy.recipes),
        ):
            if not frozenset(strings(data[name])) <= allowed:
                raise IntakeRefused("proposal exceeds trusted owner policy")

    def approve(
        self, principal: Principal, draft: Draft, policy: OwnerPolicy, *, decision_id: str, expires_at: int
    ) -> ApprovedMeaning:
        self._prefix(decision_id)
        current = self.reader.read(draft.evidence.subject, draft.declaration)
        # Finish all reads, including journal recovery, before sampling authority time.
        self._validate(principal, draft, policy, current, expires_at)
        key = f"ordinary:decision:{decision_id}"
        decision = encode(
            {
                "kind": "ordinary-owner-decision",
                "version": 1,
                "decision_id": decision_id,
                "issuer": principal.actor_id,
                "draft_digest": draft.digest,
                "revision": draft.revision,
                "evidence_digest": current.digest,
                "policy_digest": policy.digest,
                "policy": policy.payload(),
                "expires_at": expires_at,
            }
        )
        data = draft.declaration.value
        inputs = (
            InputBinding("ordinary-declaration", f"sha256:{draft.declaration.digest}"),
            InputBinding("ordinary-evidence", f"sha256:{current.digest}"),
            InputBinding("ordinary-recipes", f"sha256:{current.recipe_digest}"),
        )
        unit = WorkUnit(
            "ordinary",
            cast(str, data["outcome"]),
            required_inputs=frozenset(item.name for item in inputs),
            acceptance_criteria=strings(data["criteria"]),
            acceptance_policy_reference=policy.reference,
        )
        program_id = f"ordinary:{draft.task_id}"
        prior_decisions = self._consumptions(draft.task_id)
        matching = [
            record for record in prior_decisions if object_from(self._recover(record)).get("decision") == decision_id
        ]
        if matching and len(matching) != 1:
            raise IntakeRefused("ambiguous decision consumption")
        current_program = self.store.find_program(program_id)
        if matching and current_program is not None:
            consumed = object_from(self._recover(matching[0]))
            if consumed.get("program") == current_program.spec.digest:
                self._validate(principal, draft, policy, current, expires_at)
                self._record(key, decision)
                self._validate(principal, draft, policy, current, expires_at)
                return ApprovedMeaning(draft, decision_id, decision, current_program)
            if consumed.get("expected_revision") != current_program.revision:
                raise IntakeRefused("approval recovery no longer matches Program")
        ordinal = len(prior_decisions) + 1 if not matching else int(matching[0].operation_id.rsplit(":", 1)[1])
        consumption_key = (
            matching[0].operation_id if matching else f"ordinary:consumed:{draft.task_id}:record:{ordinal}"
        )
        if current_program is None:
            spec = ProgramSpec(
                program_id,
                cast(str, data["outcome"]),
                (unit,),
                inputs,
                policy.budget,
                policy.authority,
                acceptance_criteria=strings(data["criteria"]),
                policy_references=(policy.reference,),
            )
            consumption = encode(
                {
                    "kind": "ordinary-consumption",
                    "version": 1,
                    "decision": decision_id,
                    "draft": draft.digest,
                    "program": spec.digest,
                }
            )
            self._validate(principal, draft, policy, current, expires_at)
            self._record(key, decision)
            self._record(consumption_key, consumption)
            self._validate(principal, draft, policy, current, expires_at)
            program = self.store.create(spec, f"ordinary:approve:{decision_id}")
        else:
            program = current_program
            amendment = SpecAmendment(
                program.spec.revision,
                objective=cast(str, data["outcome"]),
                work_units=(unit,),
                initial_inputs=inputs,
                budget=policy.budget,
                authority=policy.authority,
                acceptance_criteria=strings(data["criteria"]),
                policy_references=(policy.reference,),
            )
            spec = program.spec.amend(amendment)
            consumption = encode(
                {
                    "kind": "ordinary-consumption",
                    "version": 1,
                    "decision": decision_id,
                    "draft": draft.digest,
                    "program": spec.digest,
                    "expected_revision": program.revision,
                }
            )
            self._validate(principal, draft, policy, current, expires_at)
            self._record(key, decision)
            self._record(consumption_key, consumption)
            self._validate(principal, draft, policy, current, expires_at)
            program = self.store.admit(
                program_id,
                f"ordinary:approve:{decision_id}",
                AmendProgramSpec(program.revision, principal.actor_id, amendment),
            )
        if self.store.load(program_id).spec.digest != program.spec.digest:
            raise IntakeRefused("historical receipt is not current approval")
        return ApprovedMeaning(draft, decision_id, decision, program)

    def handoff(
        self, principal: Principal, approved: ApprovedMeaning, policy: OwnerPolicy, translator: RequirementsTranslator
    ) -> TranslationRefusal:
        current = self.reader.read(approved.draft.evidence.subject, approved.draft.declaration)
        decision = object_from(self.historical_decision(approved.decision_id))
        program = self.store.load(approved.program.program_id)
        self._validate(principal, approved.draft, policy, current, _integer(decision["expires_at"]))
        if (
            encode(decision) != approved.decision_bytes
            or decision["policy_digest"] != policy.digest
            or decision["policy"] != policy.payload()
            or decision["issuer"] != principal.actor_id
            or decision["draft_digest"] != approved.draft.digest
            or decision["revision"] != approved.draft.revision
            or decision["evidence_digest"] != current.digest
            or program.spec.digest != approved.program.spec.digest
        ):
            raise IntakeRefused("decision or current Program binding changed")
        consumed = self._consumptions(approved.draft.task_id)
        if not any(
            object_from(self._recover(record)).get("decision") == approved.decision_id
            and object_from(self._recover(record)).get("program") == program.spec.digest
            for record in consumed
        ):
            raise IntakeRefused("decision has no matching durable consumption")
        self._validate(principal, approved.draft, policy, current, _integer(decision["expires_at"]))
        return translator.translate_ordinary(
            RequirementsHandoff(approved.draft, approved.decision_id, approved.decision_bytes, program.spec.digest)
        )
