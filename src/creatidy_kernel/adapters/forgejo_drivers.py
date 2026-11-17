# SPDX-License-Identifier: Apache-2.0
"""Version-1 family views over the unchanged Forgejo reference/effect facade."""

from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from typing import cast

from creatidy_kernel.adapters.driver_http import list_value, native_number, object_value, text
from creatidy_kernel.adapters.forge_refs import oid
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.forgejo_discovery import ForgejoDiscovery, ScanStatus
from creatidy_kernel.adapters.project_drivers import TrustedSCMRemote
from creatidy_kernel.adapters.project_drivers import native_fact as _fact
from creatidy_kernel.adapters.project_drivers import unavailable_fact as _missing
from creatidy_kernel.core.drivers import (
    DriverBinding,
    DriverFact,
    DriverScan,
    DriverSubject,
    NativeDependency,
    WorkObservation,
    driver_digest,
)
from creatidy_kernel.core.forge import Effect, Presence, Receipt, Reference, UnsupportedForge


def _binding(forge: ForgejoForge, binding: DriverBinding, family: str) -> Reference:
    if (binding.family, binding.implementation, binding.version, binding.api_version) != (
        family,
        "forgejo-facade",
        "1",
        "1",
    ):
        raise ValueError("unsupported Forgejo facade contract")
    if (binding.origin, binding.namespace) != (forge.binding.origin, forge.binding.repository):
        raise ValueError("Forgejo facade binding differs from retained transport")
    if forge.http.binding != forge.binding or forge.git.binding != forge.binding:
        raise ValueError("Forgejo facade transport binding changed")
    return Reference("forgejo:" + binding.namespace)


class ForgejoWorkManagement:
    def __init__(
        self, forge: ForgejoForge, binding: DriverBinding, clock: Callable[[], int], *, max_pages: int = 20
    ) -> None:
        self.repository = _binding(forge, binding, "work")
        self.forge, self.binding, self.clock = forge, binding, clock
        self.discovery = ForgejoDiscovery(
            forge.http, clock, max_pages=max_pages, freshness_seconds=30, page_size=forge.page_size
        )

    def work(self, key: str) -> WorkObservation:
        _binding(self.forge, self.binding, "work")
        if not key.isascii() or not key.isdigit() or key.startswith("0") or len(key) > 18:
            raise ValueError("legacy Forgejo facade requires a native positive issue number")
        snapshot = self.forge.issue_snapshot(self.repository, int(key))
        if snapshot.presence is not Presence.FOUND:
            return WorkObservation(_missing(self.binding, "issue", key, self.clock(), 0), (), False)
        dependencies = self.discovery.dependencies(int(key))
        closing = self.forge.issue_snapshot(self.repository, int(key))
        _binding(self.forge, self.binding, "work")
        stable = (
            dependencies.status is ScanStatus.OBSERVED
            and snapshot == closing
            and dependencies.observed_at <= self.clock() <= dependencies.fresh_until
        )
        native = {
            **asdict(snapshot),
            "dependencies": dependencies.issues_payload(),
            "dependency_scope": dependencies.scope,
            "dependency_status": dependencies.status.value,
            "dependency_snapshot": dependencies.snapshot,
            "dependency_source": dependencies.source,
        }
        edges = tuple(
            NativeDependency(
                "derived-native-edge:" + driver_digest([self.binding.digest, key, item.repository, item.number]),
                "forgejo-v13-dependencies",
                "depends_on",
                "outward",
                DriverSubject(
                    replace(self.binding, namespace=item.repository), "issue", str(item.number), item.updated_at
                ),
            )
            for item in dependencies.issues
        )
        fact = _fact(
            self.binding,
            "issue",
            key,
            snapshot.updated_at,
            dependencies.observed_at,
            dependencies.fresh_until - dependencies.observed_at,
            native,
            complete=snapshot == closing,
            qualification="native-issue;dependency-"
            + ("repeated-visible-not-snapshot" if stable else dependencies.status.value),
        )
        return WorkObservation(fact, edges, stable)


class ForgejoSCM:
    def __init__(
        self, forge: ForgejoForge, binding: DriverBinding, clock: Callable[[], int], *, max_pages: int = 20
    ) -> None:
        self.repository_reference = _binding(forge, binding, "scm")
        if type(max_pages) is not int or not 0 < max_pages <= 1000:
            raise ValueError("finite native SCM page bound required")
        self.forge, self.binding, self.clock = forge, binding, clock
        self.max_pages = max_pages

    def _read(self, path: str) -> tuple[int, object]:
        _binding(self.forge, self.binding, "scm")
        if not self.forge.http.supports_reads:
            raise UnsupportedForge("native Forgejo reads unavailable")
        try:
            return self.forge.http.request("GET", "/repos/" + self.binding.namespace + path)
        except OSError:
            return 0, None

    def repository(self) -> DriverFact:
        _binding(self.forge, self.binding, "scm")
        observed = self.forge.identity(self.repository_reference)
        return _fact(
            self.binding,
            "repository",
            self.binding.namespace,
            None,
            self.clock(),
            30,
            asdict(observed),
            presence=observed.presence,
            complete=observed.presence is Presence.FOUND,
        )

    def ref(self, branch: str) -> DriverFact:
        _binding(self.forge, self.binding, "scm")
        observed = self.forge.branch(self.repository_reference, branch)
        revision = None if observed.revision is None else observed.revision.value.removeprefix("forgejo:")
        return _fact(
            self.binding,
            "ref",
            branch,
            revision,
            self.clock(),
            30,
            asdict(observed),
            presence=observed.presence,
            complete=observed.presence is Presence.FOUND,
        )

    def change(self, key: str) -> DriverFact:
        _binding(self.forge, self.binding, "scm")
        observed = self.forge.change(self.repository_reference, Reference(self.repository_reference.value + "#" + key))
        revision = None if observed.head is None else observed.head.value.removeprefix("forgejo:")
        related = tuple(
            DriverSubject(
                self.binding, "commit", item.value.removeprefix("forgejo:"), item.value.removeprefix("forgejo:")
            )
            for item in (observed.head, observed.base)
            if item is not None
        )
        return _fact(
            self.binding,
            "change",
            key,
            revision,
            self.clock(),
            30,
            asdict(observed),
            presence=observed.presence,
            related=related,
            complete=observed.presence is Presence.FOUND,
        )

    def commit(self, revision: str) -> DriverFact:
        oid(revision)
        now = self.clock()
        status, raw = self._read("/git/commits/" + revision + "?stat=false&verification=false&files=false")
        try:
            data = object_value(raw)
            expected = self.binding.origin + "/api/v1/repos/" + self.binding.namespace + "/git/commits/" + revision
            if status != 200 or data.get("sha") != revision or data.get("url") != expected:
                raise ValueError("native exact commit unavailable")
            # v13.0.1 reports commit.tree.sha as the commit ID, not a tree proof.
            return _fact(
                self.binding,
                "commit",
                revision,
                revision,
                now,
                30,
                data,
                qualification="forgejo-v13-exact-commit;native-tree-identity-unproved",
            )
        except ValueError:
            return _missing(self.binding, "commit", revision, now, status)

    def reviews(self, key: str) -> DriverScan:
        if not key.isascii() or not key.isdigit() or len(key) > 18 or key.startswith("0"):
            raise ValueError("canonical native change number required")
        native_number(int(key))
        if str(int(key)) != key:
            raise ValueError("canonical native change number required")
        now, scope = self.clock(), self.binding.digest + ":reviews:" + key
        page_reader = cast(
            Callable[[str, str, Mapping[str, object] | None], tuple[int, object, dict[str, str]]] | None,
            getattr(self.forge.http, "request_with_headers", None),
        )
        if page_reader is None:
            return DriverScan((), False, False, scope, now, now, "native-pagination-metadata-unavailable")
        scans: list[tuple[DriverFact, ...]] = []
        try:
            _binding(self.forge, self.binding, "scm")
            selected = Reference(self.repository_reference.value + "#" + key)
            before = self.forge.change_snapshot(self.repository_reference, selected)
            if before.observation.presence is not Presence.FOUND:
                raise ValueError("native change snapshot unavailable")
            totals: list[int] = []
            for _ in range(2):
                items: list[DriverFact] = []
                seen: set[str] = set()
                total: int | None = None
                # Pending reviews are filtered AFTER pagination. Empty pages are
                # not terminal: inspect the native total and scan the bounded range.
                for page in range(1, self.max_pages + 1):
                    _binding(self.forge, self.binding, "scm")
                    if not self.forge.http.supports_reads:
                        raise ValueError("native reviews unavailable")
                    status, raw, headers = page_reader(
                        "GET", "/repos/" + self.binding.namespace + f"/pulls/{key}/reviews?page={page}&limit=1", None
                    )
                    value = headers.get("x-total-count", "")
                    if status != 200 or not value.isascii() or not value.isdigit():
                        raise ValueError("native review total unavailable")
                    count = int(value)
                    if count > self.max_pages or total is not None and count != total:
                        raise ValueError("native review scan capped or moving")
                    total = count
                    entries = list_value(raw)
                    if len(entries) > 1:
                        raise ValueError("native review limit ignored")
                    for entry in entries:
                        data = object_value(entry)
                        if (
                            data.get("pull_request_url")
                            != self.binding.origin + "/" + self.binding.namespace + "/pulls/" + key
                        ):
                            raise ValueError("native review belongs to another change")
                        identity = native_number(data.get("id"))
                        if identity in seen:
                            raise ValueError("duplicate native review")
                        seen.add(identity)
                        if data.get("state") not in {
                            "APPROVED",
                            "PENDING",
                            "COMMENT",
                            "REQUEST_CHANGES",
                            "REQUEST_REVIEW",
                            "",
                        }:
                            raise ValueError("unsupported native review state")
                        if (
                            any(type(data.get(name)) is not bool for name in ("stale", "official", "dismissed"))
                            or type(data.get("body")) is not str
                        ):
                            raise ValueError("partial native review")
                        text(data.get("updated_at"))
                        user, team = data.get("user"), data.get("team")
                        if not isinstance(user, dict) and not isinstance(team, dict):
                            raise ValueError("review actor unavailable")
                        if isinstance(user, dict):
                            text(object_value(cast(object, user)).get("login"))
                        else:
                            text(object_value(cast(object, team)).get("name"))
                        commit = data.get("commit_id")
                        related = (
                            ()
                            if commit == "" and data["state"] == "REQUEST_REVIEW"
                            else (DriverSubject(self.binding, "commit", oid(text(commit)), oid(text(commit))),)
                        )
                        items.append(
                            _fact(
                                self.binding,
                                "review",
                                key + "/" + identity,
                                None,
                                now,
                                30,
                                data,
                                related=related,
                                qualification="native-review-state-stale-dismissed;not-Kernel-acceptance",
                            )
                        )
                    if page >= max(1, total):
                        break
                else:
                    raise ValueError("native review scan capped")
                if total != len(items):
                    raise ValueError("permission-filtered pending reviews remain unknown")
                scans.append(tuple(items))
                totals.append(total)
            after = self.forge.change_snapshot(self.repository_reference, selected)
            _binding(self.forge, self.binding, "scm")
            if (
                before != after
                or [item.native for item in scans[0]] != [item.native for item in scans[1]]
                or totals[0] != totals[1]
                or not now <= self.clock() <= now + 30
            ):
                raise ValueError("native reviews changed during observation")
            return DriverScan(
                scans[-1], True, True, scope, now, now + 30, "repeated-native-visible-with-total;not-snapshot"
            )
        except (OSError, ValueError, TypeError, OverflowError):
            return DriverScan(
                (), False, False, scope, now, now, "partial-permission-moving-or-unsupported-native-reviews"
            )

    def open_changes(self) -> DriverScan:
        now, scope = self.clock(), "all-open-changes-in-repository:" + self.binding.digest
        scans: list[tuple[DriverFact, ...]] = []
        try:
            for _ in range(2):
                items: list[DriverFact] = []
                seen: set[str] = set()
                for page in range(1, self.max_pages + 1):
                    status, raw = self._read(f"/pulls?state=open&page={page}&limit={self.forge.page_size}")
                    if status != 200:
                        raise ValueError("native change inventory unavailable")
                    entries = list_value(raw)
                    if len(entries) > self.forge.page_size:
                        raise ValueError("native inventory limit ignored")
                    if not entries:
                        break
                    for value in entries:
                        row = object_value(value)
                        key = native_number(row.get("number"))
                        if key in seen:
                            raise ValueError("duplicate native change")
                        seen.add(key)
                        status, raw_detail = self._read("/pulls/" + key)
                        detail = object_value(raw_detail)
                        if (
                            status != 200
                            or row.get("state") != "open"
                            or detail.get("state") != "open"
                            or detail.get("number") != row["number"]
                            or text(row.get("updated_at")) != text(detail.get("updated_at"))
                        ):
                            raise ValueError("native inventory changed")
                        subjects: list[DriverSubject] = []
                        for role in ("head", "base"):
                            listed, current = object_value(row.get(role)), object_value(detail.get(role))
                            if (
                                listed != current
                                or object_value(current.get("repo")).get("full_name") != self.binding.namespace
                            ):
                                raise ValueError("fork or moving native inventory")
                            revision = oid(text(current.get("sha")))
                            text(current.get("ref"))
                            subjects.append(DriverSubject(self.binding, "commit", revision, revision))
                        items.append(
                            _fact(
                                self.binding,
                                "change",
                                key,
                                subjects[0].revision,
                                now,
                                30,
                                detail,
                                related=tuple(subjects),
                                qualification="native-open-change;exact-head-and-base",
                            )
                        )
                else:
                    raise ValueError("native inventory capped")
                scans.append(tuple(items))
            if scans[0] != scans[1] or not now <= self.clock() <= now + 30:
                raise ValueError("native inventory changed or expired")
            return DriverScan(scans[-1], True, True, scope, now, now + 30, "repeated-native-open-changes;not-snapshot")
        except (OSError, ValueError, TypeError, OverflowError):
            return DriverScan(
                (), False, False, scope, now, now, "partial-permission-moving-or-unsupported-native-inventory"
            )

    def apply_legacy(self, effect: Effect) -> Receipt:
        """No identity remapping, new authority or changed AGit request bytes."""
        return self.forge.apply(effect)

    def reconcile_legacy(self, effect: Effect, known_reference: Reference | None = None) -> Receipt:
        return self.forge.reconcile(effect, known_reference)


class ForgejoCI:
    def __init__(
        self,
        forge: ForgejoForge,
        binding: DriverBinding,
        source_binding: DriverBinding,
        clock: Callable[[], int],
        *,
        max_pages: int = 20,
        remote_mapping: TrustedSCMRemote | None = None,
    ) -> None:
        self.repository = _binding(forge, binding, "ci")
        native_source = (
            source_binding.family,
            source_binding.implementation,
            source_binding.api_version,
            source_binding.origin,
            source_binding.tenant,
            source_binding.namespace,
        ) == ("scm", "forgejo-facade", "1", binding.origin, binding.tenant, binding.namespace)
        if (
            type(max_pages) is not int
            or not 0 < max_pages <= 1000
            or source_binding.family != "scm"
            or (
                not native_source
                and (
                    remote_mapping is None
                    or remote_mapping.binding != source_binding
                    or remote_mapping.remote != binding.origin + "/" + binding.namespace + ".git"
                )
            )
        ):
            raise ValueError("finite Forgejo CI scan and exact SCM repository or trusted mapping required")
        if remote_mapping is not None and (
            remote_mapping.binding != source_binding
            or remote_mapping.remote != binding.origin + "/" + binding.namespace + ".git"
        ):
            raise ValueError("Forgejo CI trusted remote mapping differs")
        self.forge, self.binding, self.source_binding, self.clock = forge, binding, source_binding, clock
        self.max_pages = max_pages
        self.mapping_digest = None if remote_mapping is None else driver_digest(asdict(remote_mapping))

    def run(self, key: str, source: DriverSubject) -> DriverFact:
        """v13 run metadata is NOT actual checkout, attempt or required-step proof."""
        if not key.isascii() or not key.isdigit() or len(key) > 18 or key.startswith("0"):
            raise ValueError("exact native Actions database run ID required")
        _binding(self.forge, self.binding, "ci")
        if source.binding != self.source_binding or source.kind != "commit" or source.key != source.revision:
            raise ValueError("configured exact SCM commit required")
        oid(source.key)
        now = self.clock()
        if not self.forge.http.supports_reads:
            return _missing(self.binding, "workflow-run", key, now, 0)
        status = 0
        try:
            status, raw = self.forge.http.request("GET", "/repos/" + self.binding.namespace + "/actions/runs/" + key)
            _binding(self.forge, self.binding, "ci")
            data = object_value(raw)
            repository = object_value(data.get("repository"))
            if (
                status != 200
                or native_number(data.get("id")) != key
                or data.get("commit_sha") != source.key
                or repository.get("full_name") != self.binding.namespace
                or repository.get("html_url") != self.binding.origin + "/" + self.binding.namespace
                or data.get("status")
                not in {"unknown", "waiting", "running", "success", "failure", "cancelled", "skipped", "blocked"}
            ):
                raise ValueError("native exact run metadata unavailable")
            text(data.get("workflow_id"))
            text(data.get("event"))
            return _fact(
                self.binding,
                "workflow-run",
                key,
                None,
                now,
                30,
                data,
                related=(source,),
                complete=False,
                qualification="forgejo-v13-run-metadata-only;actual-checkout-steps-and-job-attempts-unavailable;not-qualified-verification"
                + (";trusted-remote-mapping-sha256:" + self.mapping_digest if self.mapping_digest else ""),
            )
        except (OSError, ValueError, TypeError):
            return _missing(self.binding, "workflow-run", key, now, status)

    def build(self, key: str, source: DriverSubject) -> DriverFact:
        _binding(self.forge, self.binding, "ci")
        oid(key)
        if (
            source.binding != self.source_binding
            or source.kind != "commit"
            or source.key != source.revision
            or key != source.key
        ):
            raise ValueError("configured exact SCM commit required")
        now = self.clock()
        cursor = None
        scans: list[list[dict[str, object]]] = []
        for _ in range(2):
            items: list[dict[str, object]] = []
            seen: set[str] = set()
            cursor = None
            for _ in range(self.max_pages):
                page = self.forge.checks(self.repository, Reference("forgejo:" + source.key), cursor)
                if len(page.items) > self.forge.page_size:
                    return _missing(self.binding, "commit-statuses", key, now, 0)
                for item in page.items:
                    if item.presence is not Presence.FOUND or item.reference is None or item.reference.value in seen:
                        return _missing(self.binding, "commit-statuses", key, now, 0)
                    seen.add(item.reference.value)
                items.extend(asdict(item) for item in page.items)
                if page.complete and page.next_cursor is None:
                    break
                if page.next_cursor is None or page.next_cursor == cursor:
                    return _missing(self.binding, "commit-statuses", key, now, 0)
                cursor = page.next_cursor
            else:
                return _missing(self.binding, "commit-statuses", key, now, 0)
            scans.append(items)
        if scans[0] != scans[1]:
            return _missing(self.binding, "commit-statuses", key, now, 0)
        _binding(self.forge, self.binding, "ci")
        if not now <= self.clock() <= now + 30:
            return _missing(self.binding, "commit-statuses", key, now, 0)
        return _fact(
            self.binding,
            "commit-statuses",
            key,
            None,
            now,
            30,
            scans[1],
            related=(source,),
            qualification="repeated-legacy-status-scan;native-revision-unavailable;native-checks-not-Kernel-acceptance"
            + (";trusted-remote-mapping-sha256:" + self.mapping_digest if self.mapping_digest else ""),
        )
