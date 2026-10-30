# SPDX-License-Identifier: Apache-2.0
"""Independently journalled, owner-authorized publication of accepted ordinary work.

Only exact-SHA AGit PR creation and append-only historical acceptance receipts are
supported. Native close/edit/merge is not acceptance and is not authorized here.
An unknown write can only be read back; an empty scan never authorizes replay.
"""

from dataclasses import asdict
from typing import cast

from creatidy_kernel.adapters.forge_refs import fresh_agit_topic, number
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_transport import ConditionalGitTransport, HTTPSForgejoTransport
from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import Effect, EffectStatus, ForgeConflict, Presence, Receipt, Reference, effect_marker
from creatidy_kernel.core.intake import IntakeRefused, digest, encode, strings
from creatidy_kernel.core.verification import CandidateResult, EvidenceSubject
from creatidy_kernel.ports.ordinary import OrdinaryApproval, OrdinaryHandoff, OrdinaryProgram
from creatidy_kernel.ports.ownership import WorkOwnership


class OrdinaryForgejoSync:
    def __init__(
        self,
        app: OrdinaryProgram,
        approved: OrdinaryApproval,
        control: WorkOwnership,
        workspace: OrdinaryGitWorkspace,
        http: HTTPSForgejoTransport,
        git: ConditionalGitTransport,
        expected_login: str,
        *,
        max_pages: int = 8,
    ) -> None:
        if (
            not expected_login
            or max_pages <= 0
            or http.binding != git.binding
            or git.object_source != workspace.objects
        ):
            raise IntakeRefused("exact owner-bound publication transports and object source required")
        if http.before_send is not None or git.before_send is not None:
            raise IntakeRefused("publication transports must be dedicated; do not replace another owner's guard")
        self.app, self.approved, self.control, self.workspace = app, approved, control, workspace
        self.store = cast(SQLiteProgramStore, app.store)
        self.http, self.git, self.login, self.max_pages = http, git, expected_login, max_pages
        self.active: tuple[str, int] | None = None
        self.handoff: OrdinaryHandoff | None = None
        self.forge = ForgejoForge(http, git, self.authorized, authorize_reconcile=self.authorized_readback)
        http.before_send = self.before_send
        git.before_send = self.before_send

    def accepted(self, handoff: OrdinaryHandoff) -> dict[str, object]:
        program = self.store.load(handoff.attempt.program_id)
        state = program.state(handoff.attempt.work_unit_id)
        fact = next((item for item in program.satisfactions if item.reference_id == state.satisfaction_id), None)
        if (
            program.spec.digest != self.approved.program.spec.digest
            or fact is None
            or fact.source_attempt_id != handoff.attempt.attempt_id
            or self.workspace.collected is None
        ):
            raise IntakeRefused(
                "native publication requires the selected current Kernel satisfaction, not CI or ticket state"
            )
        candidate, collection = self.workspace.collected
        if candidate.spec_digest != handoff.attempt.digest or self.workspace.workspace.capture()[0] != digest(
            self.store.artifact(handoff.claim.operation_id, "ownership-candidate")
        ):
            raise IntakeRefused("accepted candidate ownership or exact bytes changed")
        draft = next(item for item in self.approved.proposal.drafts if item.task_id == handoff.attempt.work_unit_id)
        if (draft.evidence.subject.origin, draft.evidence.subject.repository) != (
            self.http.binding.origin,
            self.http.binding.repository,
        ):
            raise IntakeRefused("publication escaped the approved native issue repository")
        if not {"forgejo:pr", "forgejo:receipt"}.issubset(strings(draft.declaration.value["effects"])):
            raise IntakeRefused("ordinary declaration did not receive both bounded publication effects")
        return {
            "program": program.program_id,
            "spec": program.spec.digest,
            "unit": fact.work_unit_id,
            "satisfaction": fact.reference_id,
            "attempt": handoff.attempt.digest,
            "candidate": CandidateResult(
                f"candidate:{handoff.attempt.attempt_id}",
                candidate,
                tuple((name, "candidate.patch") for name in sorted(program.spec.work_unit(fact.work_unit_id).outputs)),
            ).digest,
            "head": collection.head,
            "base": collection.base,
            "issue": draft.evidence.subject.issue,
            "approval": self.approved.decision_id,
        }

    def before_send(self) -> None:
        if self.active is None or self.handoff is None:
            raise IntakeRefused("no exact independently journalled native publication delivery")
        self.accepted(self.handoff)
        if self.app.policy.digest != self.approved.policy_digest:
            raise IntakeRefused("publication policy changed")
        # No network/caller callbacks after this cut. HTTP's credential resolver
        # has already returned, and its total deadline is intentionally not nested.
        self.store.check_operation_delivery(*self.active)

    def native_current(self) -> None:
        if self.handoff is None:
            raise IntakeRefused("no selected native publication subject")
        for draft, original in zip(self.approved.proposal.drafts, self.approved.proposal.dependencies, strict=True):
            repository = Reference("forgejo:" + draft.evidence.subject.repository)
            native_issue = self.forge.issue_snapshot(repository, draft.evidence.subject.issue)
            native_base = self.forge.branch(repository, draft.evidence.subject.branch)
            if (
                native_issue.presence is not Presence.FOUND
                or (native_issue.content_digest, native_issue.state)
                != (draft.evidence.issue_digest, draft.evidence.issue_state)
                or native_base.presence is not Presence.FOUND
                or native_base.revision != Reference("forgejo:" + draft.evidence.subject.base)
            ):
                raise IntakeRefused("native issue meaning or observed base changed before publication")
            edges = self.app.dependencies.read_dependencies(draft.evidence.subject)
            if not edges.complete or edges.required != original.required:
                raise IntakeRefused("native required predecessors changed or became unverified")

    def authorized(self, effect: Effect) -> bool:
        if self.active != (effect.operation.operation_id, effect.fence):
            return False
        self.native_current()
        self.before_send()
        return True

    def authorized_readback(self, effect: Effect) -> bool:
        if self.active != (effect.operation.operation_id, effect.fence) or self.handoff is None:
            return False
        original = self.store.operation(effect.operation.operation_id)
        if (original.effect_key, original.request_digest, original.fence) != (
            effect.operation.effect_key,
            effect.operation.request_digest,
            effect.fence,
        ):
            return False
        self.accepted(self.handoff)
        return True  # Read permission for the recorded effect is not a new delivery lease.

    def _comments(self, issue: int, body: str) -> str | None:
        matches: list[str] = []
        seen: set[int] = set()
        for page in range(1, self.max_pages + 1):
            status, raw = self.http.request(
                "GET", f"/repos/{self.http.binding.repository}/issues/{issue}/comments?page={page}&limit=30"
            )
            if status != 200 or type(raw) is not list:
                return None
            rows = cast(list[object], raw)
            if not rows:
                return matches[0] if len(matches) == 1 else None
            if len(rows) > 30:
                return None
            for item in rows:
                value = cast(dict[str, object], item) if type(item) is dict else {}
                key = value.get("id")
                if type(key) is not int or key <= 0 or key in seen:
                    return None
                seen.add(key)
                user = value.get("user")
                expected_issue = (
                    f"{self.http.binding.origin}/api/v1/repos/{self.http.binding.repository}/issues/{issue}"
                )
                if (
                    value.get("body") == body
                    and type(user) is dict
                    and cast(dict[str, object], user).get("login") == self.login
                    and value.get("issue_url") == expected_issue
                ):
                    matches.append(f"forgejo:{self.http.binding.repository}:comment:{key}")
        return None

    def _pr_readback(self, effect: Effect) -> Receipt:
        if self.handoff is None:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        expected_base = Reference("forgejo:" + cast(str, self.accepted(self.handoff)["base"]))
        references: list[Reference] = []
        seen: set[Reference] = set()
        cursor: str | None = None
        for _ in range(self.max_pages):
            page = self.forge.changes(effect.repository, cursor)
            if not page.complete and page.next_cursor is None:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            for item in page.items:
                if item.presence is not Presence.FOUND or item.reference is None or item.reference in seen:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                seen.add(item.reference)
                prefix = f"{effect.repository.value}#"
                suffix = item.reference.value.removeprefix(prefix)
                if not item.reference.value.startswith(prefix) or not number(suffix):
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                status, raw = self.http.request("GET", f"/repos/{self.http.binding.repository}/pulls/{suffix}")
                if status != 200 or type(raw) is not dict:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                body = cast(dict[str, object], raw).get("body")
                if body is None:
                    continue
                if type(body) is not str:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                if effect_marker(effect.operation) not in body:
                    continue
                try:
                    receipt = self.forge.reconcile(effect, item.reference)
                except ForgeConflict:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                if receipt.status is EffectStatus.ACCEPTED and receipt.observed_base == expected_base:
                    references.append(item.reference)
            if page.next_cursor is None:
                return (
                    Receipt(EffectStatus.ACCEPTED, effect.operation, references[0], observed_base=expected_base)
                    if len(references) == 1
                    else Receipt(EffectStatus.UNKNOWN, effect.operation)
                )
            if page.next_cursor == cursor:
                break
            cursor = page.next_cursor
        return Receipt(EffectStatus.UNKNOWN, effect.operation)

    def publish(self, handoff: OrdinaryHandoff) -> tuple[str, str]:
        if self.active is not None:
            raise IntakeRefused("reentrant native publication refused")
        received = self.accepted(handoff)
        self.handoff = handoff
        attempt = handoff.attempt
        topic_key = f"ordinary-publication-topic:{attempt.attempt_id}"
        saved_topic = self.store.find_artifact(topic_key, "topic")
        if saved_topic is None:
            topic = fresh_agit_topic()
            self.store.intent(
                topic_key,
                topic_key,
                {"kind": "ordinary-intake-record", "version": 1, "bytes": encode({"topic": topic}).decode()},
            )
            self.store.finalize_artifact(topic_key, "topic", topic.encode())
        else:
            topic = saved_topic.decode()
        repository = Reference("forgejo:" + self.http.binding.repository)
        draft = next(item for item in self.approved.proposal.drafts if item.task_id == attempt.work_unit_id)
        results: list[str] = []
        for action in ("pr", "receipt"):
            key = f"ordinary-publish:{attempt.attempt_id}:{action}"
            subject = EvidenceSubject(
                candidate_digest=cast(str, received["candidate"]),
                attempt_digest=attempt.digest,
                spec_digest=self.approved.program.spec.digest,
                policy_digest=self.app.verification.reference.digest,
                repository=self.workspace.repository_url,
                base_revision=cast(str, received["base"]),
                head_revision=cast(str, received["head"]),
            )
            op = self.control.authorize_publication(
                handoff.claim,
                key,
                key,
                {
                    "kind": "ordinary-publication",
                    "action": action,
                    "accepted": received,
                    "login": self.login,
                    "origin": self.http.binding.origin,
                    "subject": asdict(subject),
                    "topic": topic,
                },
            )
            identity = OperationKey(op.operation_id, op.effect_key, op.request_digest)
            saved = self.store.find_artifact(key, "native-receipt")
            fence = op.fence
            if op.attempts == 0:
                fence = self.control.dispatch(handoff.claim, key, lease_seconds=120)
                op = self.store.operation(key)
            body = (
                "Kernel accepted immutable work; this does not close, merge or release native work.\n"
                + encode(received).decode()
                + "\n"
                + effect_marker(identity)
            )
            effect = Effect(
                identity,
                repository,
                "pr",
                topic,
                revision=Reference("forgejo:" + cast(str, received["head"])),
                base_branch=draft.evidence.subject.branch,
                title=f"Kernel accepted candidate for issue #{draft.evidence.subject.issue}",
                body=body,
                fence=fence,
                delivery_attempts=op.attempts,
            )
            self.active = key, fence
            try:
                if action == "pr":
                    receipt = (
                        self._pr_readback(effect)
                        if op.attempts > 1 or saved is not None or op.status != "dispatched"
                        else None
                    )
                    # attempts==1 also covers a lost reply after the first write.
                    started = self.store.find_artifact(key, "physical-send")
                    if receipt is None and started is None:
                        self.store.finalize_artifact(key, "physical-send", encode({"operation": key, "fence": fence}))
                        receipt = self.forge.apply(effect)
                    if (
                        receipt is None
                        or receipt.status is not EffectStatus.ACCEPTED
                        or receipt.observed_base != Reference("forgejo:" + cast(str, received["base"]))
                    ):
                        receipt = self._pr_readback(effect)
                    reference = (
                        receipt.reference.value
                        if receipt.status is EffectStatus.ACCEPTED and receipt.reference
                        else None
                    )
                else:
                    reference = self._comments(draft.evidence.subject.issue, body)
                    started = self.store.find_artifact(key, "physical-send")
                    if reference is None and started is None and op.status == "dispatched":
                        self.native_current()
                        self.store.finalize_artifact(key, "physical-send", encode({"operation": key, "fence": fence}))
                        try:
                            self.http.request(
                                "POST",
                                f"/repos/{self.http.binding.repository}/issues/{draft.evidence.subject.issue}/comments",
                                {"body": body},
                            )
                        except OSError:
                            pass
                        reference = self._comments(draft.evidence.subject.issue, body)
                if reference is not None:
                    try:
                        self.native_current()
                    except (ValueError, OSError):
                        reference = None  # Observational postflight is not native CAS.
                if reference is None:
                    self.store.observe(key, fence, key + ":unknown", "unknown")
                    results.append("unknown")
                else:
                    native = encode(
                        {
                            "operation": asdict(identity),
                            "reference": reference,
                            "accepted": received,
                            "action": action,
                            "profile": "forgejo-native-readback:v1",
                        }
                    )
                    witness = self.store.finalize_artifact(key, "native-receipt", native)
                    self.store.observe(key, fence, key + ":accepted", "accepted", reference=reference)
                    self.store.record_work_settlement(key, witness, expected_fence=fence)
                    results.append(reference)
            finally:
                self.active = None
            if results[-1] == "unknown":
                return "unknown", "held"
        return results[0], results[1]
