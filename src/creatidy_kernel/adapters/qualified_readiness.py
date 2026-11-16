# SPDX-License-Identifier: Apache-2.0
"""Bounded GitHub/Jira literal-content readiness; no model or repository code execution.

GitHub REST 2026-03-10 documents GET /repos/{owner}/{repo}/pulls with state,
per_page and page filters. Two bounded scans plus exact change rereads
detect observed movement; they do not establish a remote atomic snapshot.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import quote, urlencode

from creatidy_kernel.adapters.driver_http import native_number, object_value
from creatidy_kernel.adapters.forge_refs import ForgeBinding
from creatidy_kernel.adapters.intake import GitContentSource
from creatidy_kernel.adapters.project_drivers import GitHubSCM
from creatidy_kernel.adapters.source_cache import source_use
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.drivers import DriverFact, DriverScan, DriverSubject, WorkObservation
from creatidy_kernel.core.forge import Presence
from creatidy_kernel.core.intake import ContentCriterion, Declaration, digest, encode, object_from, strings
from creatidy_kernel.core.ordinary_qualified import QualifiedDependencyCut, QualifiedReadiness
from creatidy_kernel.ports.drivers import Knowledge, SourceManagement, WorkManagement


class LiteralReadiness:
    """Controller-bound exact-content criteria and bounded independent native reads."""

    def __init__(
        self,
        work: WorkManagement,
        source: SourceManagement,
        repository: Path,
        criteria: tuple[tuple[DriverSubject, tuple[ContentCriterion, ...]], ...],
        open_status_ids: frozenset[str],
        contexts: tuple[tuple[Knowledge, str, str], ...],
        owner: Principal,
        visibility_reference: str,
        clock: Callable[[], int],
        *,
        max_pages: int,
        inventory: Callable[[], DriverScan],
        work_is_open: Callable[[WorkObservation], bool],
        producer: str,
        context_subjects: tuple[DriverSubject, ...],
    ) -> None:
        if (
            work.binding.family != "work"
            or source.binding.family != "scm"
            or type(criteria) is not tuple
            or not criteria
            or any(type(pair) is not tuple or len(pair) != 2 for pair in criteria)
            or any(
                type(subject) is not DriverSubject
                or subject.binding != work.binding
                or subject.revision is None
                or type(items) is not tuple
                or not items
                or any(type(item) is not ContentCriterion for item in items)
                for subject, items in criteria
            )
            or len({subject for subject, _ in criteria}) != len(criteria)
            or type(open_status_ids) is not frozenset
            or not open_status_ids
            or any(type(item) is not str or not item for item in open_status_ids)
            or type(contexts) is not tuple
            or any(
                type(key) is not str
                or not key
                or type(revision) is not str
                or not revision
                or driver.binding.family != "knowledge"
                for driver, key, revision in contexts
            )
            or len({(driver.binding, key, revision) for driver, key, revision in contexts}) != len(contexts)
            or type(owner) is not Principal
            or owner.role != "owner"
            or not owner.actor_id.strip()
            or type(visibility_reference) is not str
            or not visibility_reference.strip()
            or type(max_pages) is not int
            or not 0 < max_pages <= 1000
            or not producer.strip()
            or producer == "unknown"
            or len(context_subjects) != len(contexts)
            or any(
                subject.binding != driver.binding or subject.key != key or subject.revision != revision
                for subject, (driver, key, revision) in zip(context_subjects, contexts, strict=True)
            )
        ):
            raise ValueError("finite exact host readiness configuration required")
        self.work, self.source, self.repository = work, source, repository
        self.criteria, self.open_status_ids, self.contexts = dict(criteria), open_status_ids, contexts
        self.context_bindings = tuple(driver.binding for driver, _, _ in contexts)
        self.clock, self.max_pages = clock, max_pages
        self.work_binding, self.source_binding = work.binding, source.binding
        self.owner, self.visibility_reference = owner, visibility_reference
        self.inventory, self.work_is_open, self.producer = inventory, work_is_open, producer
        self.context_subjects = context_subjects
        self._scans: list[DriverScan] = []

    def _configuration_current(self) -> bool:
        return self.source.binding == self.source_binding and self.work.binding == self.work_binding

    def _inventory(self) -> tuple[DriverFact, ...] | None:
        scan = self.inventory()
        if (
            not self._configuration_current()
            or not scan.complete
            or not scan.stable
            or scan.scope != "all-open-changes-in-repository:" + self.source_binding.digest
            or scan.qualification in {"", "unavailable"}
            or not scan.observed_at <= self.clock() <= scan.fresh_until
            or len(scan.items) > self.max_pages * 100
            or len({fact.subject.key for fact in scan.items}) != len(scan.items)
        ):
            return None
        for fact in scan.items:
            fact.require_current(self.clock())
            if (
                fact.subject.binding != self.source_binding
                or fact.subject.kind != "change"
                or not fact.complete
                or not fact.qualification
            ):
                return None
        self._scans.append(scan)
        return scan.items

    def read_readiness(
        self, cut: QualifiedDependencyCut, source: DriverSubject, declaration: Declaration
    ) -> QualifiedReadiness | None:
        items = self.criteria.get(cut.subject)
        if (
            items is None
            or source.binding != self.source_binding
            or source.kind != "ref"
            or source.revision is None
            or self.work.binding != self.work_binding
            or self.source.binding != self.source_binding
            or tuple(item.criterion for item in items) != strings(declaration.value["criteria"])
        ):
            return None
        try:
            self._scans = []
            started = self.clock()
            work = self.work.work(cut.subject.key)
            cut.recheck(work, self.clock())
            if not self.work_is_open(work):
                return None
            initial_source = self.source.ref(source.key)
            initial_source.require_current(self.clock())
            if initial_source.subject != source:
                return None
            initial_contexts = tuple(driver.content(key, revision) for driver, key, revision in self.contexts)
            for fact, (driver, _, _), expected, binding in zip(
                initial_contexts, self.contexts, self.context_subjects, self.context_bindings, strict=True
            ):
                fact.require_current(self.clock())
                if driver.binding != binding or fact.subject != expected or not fact.complete or not fact.qualification:
                    return None
            inventory = self._inventory()
            if inventory is None:
                return None
            repository_url = source.binding.origin + "/" + source.binding.namespace + ".git"
            with source_use(self.repository, repository_url) as leased:
                git = GitContentSource(leased, ForgeBinding(source.binding.origin, source.binding.namespace))
                base = tuple(git.read(source.revision, item.path) for item in items)
                if any(value is None for value in base) or all(
                    value == item.expected for value, item in zip(base, items, strict=True)
                ):
                    return None
                for change in inventory:
                    if (
                        change.subject.revision is None
                        or len(change.related) != 2
                        or change.related[0]
                        != DriverSubject(source.binding, "commit", change.subject.revision, change.subject.revision)
                        or change.related[1]
                        != DriverSubject(source.binding, "commit", source.revision, source.revision)
                    ):
                        return None
                    contents = tuple(git.read(change.subject.revision, item.path) for item in items)
                    if any(value is None for value in contents) or all(
                        value == item.expected for value, item in zip(contents, items, strict=True)
                    ):
                        return None
            closing_inventory = self._inventory()
            closing_source = self.source.ref(source.key)
            closing_work = self.work.work(cut.subject.key)
            closing_contexts = tuple(driver.content(key, revision) for driver, key, revision in self.contexts)
            now = self.clock()
            cut.recheck(work, now)
            cut.recheck(closing_work, now)
            if (
                closing_inventory is None
                or [
                    (fact.subject, fact.native, fact.provenance, fact.qualification, fact.complete)
                    for fact in inventory
                ]
                != [
                    (fact.subject, fact.native, fact.provenance, fact.qualification, fact.complete)
                    for fact in closing_inventory
                ]
                or (
                    initial_source.subject,
                    initial_source.native,
                    initial_source.provenance,
                    initial_source.qualification,
                    initial_source.complete,
                )
                != (
                    closing_source.subject,
                    closing_source.native,
                    closing_source.provenance,
                    closing_source.qualification,
                    closing_source.complete,
                )
                or [
                    (fact.subject, fact.native, fact.provenance, fact.qualification, fact.complete)
                    for fact in initial_contexts
                ]
                != [
                    (fact.subject, fact.native, fact.provenance, fact.qualification, fact.complete)
                    for fact in closing_contexts
                ]
                or self.work.binding != self.work_binding
                or self.source.binding != self.source_binding
                or not self._configuration_current()
                or tuple(driver.binding for driver, _, _ in self.contexts) != self.context_bindings
                or any(not scan.observed_at <= now <= scan.fresh_until for scan in self._scans)
            ):
                return None
            for fact in (
                initial_source,
                closing_source,
                *initial_contexts,
                *closing_contexts,
                *inventory,
                *closing_inventory,
            ):
                fact.require_current(now)
            proof_value: dict[str, object] = {
                "work": cut.subject.reference,
                "source": source.reference,
                "declaration": declaration.digest,
                "judgment": "work_remains",
                "native": cut.observation_digest,
                "open_status_ids": sorted(self.open_status_ids),
                "criteria": [
                    {
                        "criterion": item.criterion,
                        "path": item.path,
                        "expected_digest": digest(item.expected),
                        "source_content": cast(bytes, value).hex(),
                    }
                    for item, value in zip(items, base, strict=True)
                ],
                "inventory": [{"subject": fact.subject.reference, "native": fact.native.hex()} for fact in inventory],
                "inventory_assurance": "two-stable-bounded-scans-not-atomic-snapshot",
                "visibility_attestation": self.visibility_reference,
                "owner_actor": self.owner.actor_id,
                "inventory_scope": {
                    "repository": self.source_binding.digest,
                    "scope": "all-open-changes-in-repository",
                    "selected_base": source.reference,
                    "state": "open",
                    "max_pages": self.max_pages,
                    "equivalence": "host-configured-exact-file-content-only",
                },
                "contexts": [
                    {
                        "subject": fact.subject.reference,
                        "digest": fact.observation_digest,
                        "native": fact.native.hex(),
                    }
                    for fact in initial_contexts
                ],
            }
            if self._scans:
                proof_value["scan_evidence"] = [
                    {
                        "scope": scan.scope,
                        "qualification": scan.qualification,
                        "complete": scan.complete,
                        "stable": scan.stable,
                    }
                    for scan in self._scans
                ]
                proof_value["role_evidence"] = [
                    {
                        "subject": fact.subject.reference,
                        "digest": fact.observation_digest,
                        "provenance": fact.provenance,
                        "qualification": fact.qualification,
                        "complete": fact.complete,
                    }
                    for fact in (initial_source, *initial_contexts, *inventory)
                ]
            proof = encode(proof_value)
            fresh_until = min(
                cut.fresh_until,
                initial_source.fresh_until,
                closing_source.fresh_until,
                *(scan.fresh_until for scan in self._scans),
                *(fact.fresh_until for fact in (*initial_contexts, *closing_contexts, *inventory, *closing_inventory)),
            )
            return QualifiedReadiness(
                cut.subject,
                source,
                cut.observation_digest,
                declaration.digest,
                self.owner.actor_id,
                self.producer,
                "sha256:" + digest(proof),
                proof,
                "work_remains",
                "open",
                started,
                fresh_until,
            )
        except (OSError, ValueError, TypeError, OverflowError):
            return None


class GitHubJiraLiteralReadiness(LiteralReadiness):
    """Historical Jira/GitHub profile, preserving its original proof bytes."""

    def __init__(
        self,
        work: WorkManagement,
        source: GitHubSCM,
        repository: Path,
        criteria: tuple[tuple[DriverSubject, tuple[ContentCriterion, ...]], ...],
        open_status_ids: frozenset[str],
        contexts: tuple[tuple[Knowledge, str, str], ...],
        owner: Principal,
        visibility_reference: str,
        clock: Callable[[], int],
        *,
        max_pages: int,
    ) -> None:
        if (
            work.binding.implementation != "jira-cloud"
            or source.binding.implementation != "github"
            or source.binding.api_version != "2026-03-10"
        ):
            raise ValueError("finite exact host readiness configuration required")
        super().__init__(
            work,
            source,
            repository,
            criteria,
            open_status_ids,
            contexts,
            owner,
            visibility_reference,
            clock,
            max_pages=max_pages,
            inventory=lambda: self._unused_inventory(),
            work_is_open=lambda observed: (
                object_value(object_value(object_from(observed.fact.native).get("fields")).get("status")).get("id")
                in open_status_ids
            ),
            producer="github-jira-literal-readiness:v1",
            context_subjects=tuple(
                DriverSubject(driver.binding, "page", key, revision) for driver, key, revision in contexts
            ),
        )
        self.github = source
        self.source_prefix = "/repos/" + "/".join(quote(part, safe="") for part in source.binding.namespace.split("/"))

    def _unused_inventory(self) -> DriverScan:
        raise ValueError("historical native inventory uses its exact bounded reader")

    def _configuration_current(self) -> bool:
        return (
            super()._configuration_current()
            and self.github.http.binding == self.source_binding
            and self.github.prefix == self.source_prefix
            and self.github.http.prefix == self.source_prefix
            and self.github.http.headers.get("X-GitHub-Api-Version") == self.source_binding.api_version
        )

    def _inventory(self) -> tuple[DriverFact, ...] | None:
        if not self._configuration_current():
            return None
        result: list[DriverFact] = []
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            query = urlencode({"state": "open", "per_page": 100, "page": page})
            status, payload, headers = self.github.http.request("GET", self.github.prefix + "/pulls?" + query)
            if status != 200 or type(payload) is not list:
                return None
            entries = cast(list[object], payload)
            if len(entries) > 100:
                return None
            if not entries:
                if re.search(r"\brel\s*=\s*(?:\"next\"|next)(?:\s|,|;|$)", headers.get("link", ""), re.I):
                    return None
                return tuple(result)
            for entry in entries:
                row = object_value(entry)
                key = native_number(row.get("number"))
                if key in seen:
                    return None
                seen.add(key)
                detail = self.github.change(key)
                if detail.presence is not Presence.FOUND:
                    return None
                actual = object_from(detail.native)
                row_head, row_base = object_value(row.get("head")), object_value(row.get("base"))
                head, base = object_value(actual.get("head")), object_value(actual.get("base"))
                if (
                    row.get("state") != "open"
                    or actual.get("state") != "open"
                    or row.get("updated_at") != actual.get("updated_at")
                    or row_base.get("ref") != base.get("ref")
                    or (row_head.get("sha"), row_head.get("ref"), object_value(row_head.get("repo")).get("full_name"))
                    != (head.get("sha"), head.get("ref"), object_value(head.get("repo")).get("full_name"))
                    or (row_base.get("sha"), object_value(row_base.get("repo")).get("full_name"))
                    != (base.get("sha"), object_value(base.get("repo")).get("full_name"))
                ):
                    return None
                result.append(detail)
        return None
