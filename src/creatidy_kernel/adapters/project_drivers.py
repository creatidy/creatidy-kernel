# SPDX-License-Identifier: Apache-2.0
"""Configured native driver implementations. No discovery, credentials or authority from task URLs."""

import hashlib
from collections.abc import Callable
from dataclasses import asdict, replace
from urllib.parse import quote, urlencode

from creatidy_kernel.adapters.driver_http import DriverHTTP, list_value, native_number, object_value, text
from creatidy_kernel.adapters.forge_refs import oid, valid_branch
from creatidy_kernel.adapters.intake import GitContentSource
from creatidy_kernel.core.drivers import (
    DriverBinding,
    DriverDelivery,
    DriverFact,
    DriverMutation,
    DriverReceipt,
    DriverScan,
    DriverSubject,
    NativeDependency,
    WorkObservation,
    driver_bytes,
)
from creatidy_kernel.core.forge import EffectStatus, Presence, UnsupportedForge, effect_marker


def binding_from_mapping(value: object) -> DriverBinding:
    data = object_value(value)
    names = {"family", "implementation", "version", "origin", "tenant", "namespace", "api_version", "rights"}
    if set(data) != names:
        raise ValueError("unsupported driver configuration schema or authority fields")
    return DriverBinding(**{key: text(data[key]) for key in names})


def _bound(http: DriverHTTP, family: str, implementation: str, api_version: str) -> DriverBinding:
    binding = http.binding
    if (binding.family, binding.implementation, binding.api_version) != (family, implementation, api_version):
        raise ValueError("unsupported native driver contract")
    return binding


def native_fact(
    binding: DriverBinding,
    kind: str,
    key: str,
    revision: str | None,
    now: int,
    ttl: int,
    native: object = None,
    *,
    presence: Presence = Presence.FOUND,
    related: tuple[DriverSubject, ...] = (),
    complete: bool = True,
    qualification: str = "native-exact-read;not-serializable-snapshot",
) -> DriverFact:
    return DriverFact(
        DriverSubject(binding, kind, key, revision),
        presence,
        now,
        now + ttl,
        f"{binding.implementation}:{binding.version}:{binding.api_version}:{binding.rights}",
        driver_bytes(native),
        related,
        complete,
        qualification,
    )


def _read(http: DriverHTTP, path: str) -> tuple[int, object, dict[str, str]]:
    try:
        return http.request("GET", path)
    except OSError:
        return 0, None, {}


def unavailable_fact(binding: DriverBinding, kind: str, key: str, now: int, status: int) -> DriverFact:
    return native_fact(
        binding,
        kind,
        key,
        None,
        now,
        0,
        presence=Presence.INACCESSIBLE if status in {401, 403, 404} else Presence.UNKNOWN,
        complete=False,
        qualification="permission-or-absence-unknown" if status == 404 else "unavailable",
    )


class JiraWorkManagement:
    """Jira Cloud v3 selected issues; native links are visible-scope evidence, not a complete DAG."""

    def __init__(self, http: DriverHTTP, clock: Callable[[], int], *, ttl: int = 30, max_edges: int = 100) -> None:
        self.binding = _bound(http, "work", "jira-cloud", "3")
        if http.prefix != "/rest/api/3/issue" or not 0 < ttl <= 3600 or not 0 < max_edges <= 1000:
            raise ValueError("bounded Jira issue interface required")
        self.http, self.clock, self.ttl, self.max_edges = http, clock, ttl, max_edges

    def _issue(self, key: str) -> dict[str, object]:
        DriverSubject(self.binding, "issue", key, None)
        status, payload, _ = _read(
            self.http,
            f"/rest/api/3/issue/{quote(key, safe='')}"
            "?fields=summary,description,updated,status,project,issuelinks,parent",
        )
        if status != 200:
            raise LookupError(status)
        data = object_value(payload)
        if key not in {text(data.get("id")), text(data.get("key"))}:
            raise ValueError("Jira issue identity moved or differs")
        fields = object_value(data.get("fields"))
        for name in ("summary", "updated"):
            text(fields.get(name))
        project = object_value(fields.get("project"))
        text(project.get("key"))
        native_state = object_value(fields.get("status"))
        text(native_state.get("id"))
        text(native_state.get("name"))
        if "description" not in fields or "issuelinks" not in fields:
            raise ValueError("required Jira fields omitted")
        return data

    def work(self, key: str) -> WorkObservation:
        started = self.clock()
        try:
            observations: list[tuple[dict[str, object], tuple[NativeDependency, ...]]] = []
            for _ in range(2):
                data = self._issue(key)
                fields = object_value(data["fields"])
                if text(object_value(fields["project"])["key"]) != self.binding.namespace:
                    raise ValueError("selected Jira issue outside configured project")
                links = list_value(fields["issuelinks"])
                if len(links) > self.max_edges:
                    raise ValueError("Jira link scan capped")
                dependencies: list[NativeDependency] = []
                for value in links:
                    link = object_value(value)
                    kind = object_value(link.get("type"))
                    directions = [name for name in ("inwardIssue", "outwardIssue") if name in link]
                    if len(directions) != 1:
                        raise ValueError("ambiguous Jira link direction")
                    direction = directions[0]
                    target = self._issue(text(object_value(link[direction]).get("id")))
                    target_fields = object_value(target["fields"])
                    target_binding = replace(
                        self.binding, namespace=text(object_value(target_fields["project"])["key"])
                    )
                    dependencies.append(
                        NativeDependency(
                            text(link.get("id")),
                            text(kind.get("id")),
                            text(kind.get("name")),
                            text(kind.get("inward" if direction == "inwardIssue" else "outward")),
                            DriverSubject(target_binding, "issue", text(target["key"]), text(target_fields["updated"])),
                        )
                    )
                if "parent" in fields and fields["parent"] is not None:
                    parent = self._issue(text(object_value(fields["parent"]).get("id")))
                    parent_fields = object_value(parent["fields"])
                    dependencies.append(
                        NativeDependency(
                            "parent:" + text(parent["id"]),
                            "parent",
                            "parent",
                            "parent",
                            DriverSubject(
                                replace(self.binding, namespace=text(object_value(parent_fields["project"])["key"])),
                                "issue",
                                text(parent["key"]),
                                text(parent_fields["updated"]),
                            ),
                        )
                    )
                observations.append((data, tuple(sorted(dependencies, key=lambda edge: edge.identity))))
            first, second = observations
            if driver_bytes(asdict_edges(first)) != driver_bytes(asdict_edges(second)):
                raise ValueError("Jira work/link scan moved")
            data, edges = second
            now = self.clock()
            if now < started or now - started > self.ttl:
                raise ValueError("Jira scan expired")
            fields = object_value(data["fields"])
            return WorkObservation(
                native_fact(
                    self.binding,
                    "issue",
                    text(data["key"]),
                    text(fields["updated"]),
                    started,
                    self.ttl,
                    data,
                    related=tuple(edge.target for edge in edges),
                    qualification="repeated-visible-links;owner-edge-obligation-required;native-state-not-acceptance",
                ),
                edges,
                False,
            )
        except LookupError as error:
            return WorkObservation(unavailable_fact(self.binding, "issue", key, started, int(error.args[0])), (), False)
        except (ValueError, KeyError, TypeError):
            return WorkObservation(unavailable_fact(self.binding, "issue", key, started, 0), (), False)

    def validate_mutation(self, mutation: DriverMutation) -> None:
        raise UnsupportedForge("Jira Cloud v3 edit/transition has no supported expected-updated CAS")


def asdict_edges(value: tuple[dict[str, object], tuple[NativeDependency, ...]]) -> object:
    return {"issue": value[0], "edges": [asdict(edge) for edge in value[1]]}


class GitHubSCM:
    """REST 2026-03-10 exact reads and explicit-commit COMMENT review append. No merge permission."""

    def __init__(
        self,
        http: DriverHTTP,
        clock: Callable[[], int],
        *,
        ttl: int = 30,
        max_pages: int = 20,
        writer_login: str | None = None,
    ) -> None:
        self.binding = _bound(http, "scm", "github", "2026-03-10")
        parts = self.binding.namespace.split("/")
        if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
            raise ValueError("exact GitHub owner/repository required")
        self.prefix = "/repos/" + "/".join(quote(part, safe="") for part in parts)
        if (
            http.prefix != self.prefix
            or not 0 < ttl <= 3600
            or not 0 < max_pages <= 1000
            or http.headers.get("X-GitHub-Api-Version") != self.binding.api_version
        ):
            raise ValueError("pinned bounded GitHub interface required")
        self.http, self.clock, self.ttl, self.max_pages = http, clock, ttl, max_pages
        self.writer_login = writer_login

    def repository(self) -> DriverFact:
        now = self.clock()
        status, payload, _ = _read(self.http, self.prefix)
        try:
            data = object_value(payload)
            if status != 200 or data.get("full_name") != self.binding.namespace:
                raise ValueError("GitHub repository identity differs")
            return native_fact(self.binding, "repository", self.binding.namespace, None, now, self.ttl, data)
        except ValueError:
            return unavailable_fact(self.binding, "repository", self.binding.namespace, now, status)

    def ref(self, branch: str) -> DriverFact:
        valid_branch(branch)
        return self._source("ref", branch, f"/git/ref/heads/{quote(branch, safe='')}")

    def commit(self, revision: str) -> DriverFact:
        oid(revision)
        return self._source("commit", revision, f"/git/commits/{revision}")

    def _source(self, kind: str, key: str, path: str) -> DriverFact:
        now = self.clock()
        status, payload, _ = _read(self.http, self.prefix + path)
        try:
            data = object_value(payload)
            revision = (
                oid(text(object_value(data.get("object")).get("sha"))) if kind == "ref" else oid(text(data.get("sha")))
            )
            if (
                status != 200
                or (kind == "commit" and revision != key)
                or (
                    kind == "ref"
                    and (data.get("ref") != f"refs/heads/{key}" or object_value(data["object"]).get("type") != "commit")
                )
            ):
                raise ValueError("source identity differs")
            return native_fact(self.binding, kind, key, revision, now, self.ttl, data)
        except ValueError:
            return unavailable_fact(self.binding, kind, key, now, status)

    def _change(self, data: dict[str, object]) -> tuple[str, str, str]:
        key = native_number(data.get("number"))
        head, base = object_value(data.get("head")), object_value(data.get("base"))
        if any(object_value(side.get("repo")).get("full_name") != self.binding.namespace for side in (head, base)):
            raise ValueError("fork/foreign repository unsupported")
        for side in (head, base):
            valid_branch(text(side.get("ref")))
        if data.get("state") not in {"open", "closed"} or type(data.get("merged")) is not bool:
            raise ValueError("invalid native PR state")
        text(data.get("updated_at"))
        return key, oid(text(head.get("sha"))), oid(text(base.get("sha")))

    def change(self, key: str) -> DriverFact:
        self.number(key)
        now = self.clock()
        status, payload, _ = _read(self.http, f"{self.prefix}/pulls/{key}")
        try:
            data = object_value(payload)
            number, head, base = self._change(data)
            if status != 200 or number != key:
                raise ValueError("PR identity differs")
            return native_fact(
                self.binding,
                "change",
                key,
                head,
                now,
                self.ttl,
                data,
                related=(
                    DriverSubject(self.binding, "commit", head, head),
                    DriverSubject(self.binding, "commit", base, base),
                ),
            )
        except ValueError:
            return unavailable_fact(self.binding, "change", key, now, status)

    @staticmethod
    def number(key: str) -> None:
        if not key.isascii() or not key.isdigit() or len(key) > 18 or key.startswith("0"):
            raise ValueError("invalid native GitHub number")

    def reviews(self, key: str) -> DriverScan:
        self.number(key)
        started = self.clock()
        scope = f"{self.binding.digest}:pulls/{key}/reviews"
        scans: list[tuple[DriverFact, ...]] = []
        before = self.change(key)
        if before.presence is not Presence.FOUND:
            return DriverScan((), False, False, scope, started, started, "change-unavailable")
        try:
            for _ in range(2):
                items: list[DriverFact] = []
                seen: set[str] = set()
                for page in range(1, self.max_pages + 1):
                    status, payload, _ = _read(self.http, f"{self.prefix}/pulls/{key}/reviews?per_page=100&page={page}")
                    if status != 200:
                        raise ValueError("review page unavailable")
                    entries = list_value(payload)
                    if not entries:
                        break
                    for value in entries:
                        data = object_value(value)
                        number = native_number(data.get("id"))
                        if number in seen:
                            raise ValueError("duplicate/moving review page")
                        seen.add(number)
                        revision = oid(text(data.get("commit_id")))
                        text(data.get("state"))
                        if type(data.get("body")) is not str:
                            raise ValueError("review body omitted")
                        text(object_value(data.get("user")).get("login"))
                        items.append(
                            native_fact(
                                self.binding,
                                "review",
                                key + "/" + number,
                                None,
                                started,
                                self.ttl,
                                data,
                                related=(DriverSubject(self.binding, "commit", revision, revision),),
                                qualification="review-native-revision-unavailable;related-reviewed-commit",
                            )
                        )
                else:
                    raise ValueError("review scan capped")
                scans.append(tuple(items))
            after = self.change(key)
            same = (
                [item.native for item in scans[0]] == [item.native for item in scans[1]]
                and (before.subject, before.native) == (after.subject, after.native)
                and after.presence is Presence.FOUND
                and started <= self.clock() <= started + self.ttl
            )
            return DriverScan(
                scans[-1],
                same,
                same,
                scope,
                started,
                started + self.ttl,
                "repeated-visible-scan;not-native-snapshot" if same else "moving-scan",
            )
        except ValueError:
            return DriverScan((), False, False, scope, started, started, "incomplete-or-permission-limited")

    def validate_mutation(self, mutation: DriverMutation) -> None:
        if (
            mutation.target.binding != self.binding
            or mutation.target.kind != "change"
            or mutation.action != "append_review"
            or set(dict(mutation.fields)) != {"actor", "body"}
        ):
            raise UnsupportedForge(
                "only explicit-commit COMMENT append supported; conditional PR create/edit/merge refused"
            )
        self.number(mutation.target.key)
        oid(mutation.target.revision or "")
        if not dict(mutation.fields)["body"].strip():
            raise ValueError("review body required")
        if self.writer_login is None or dict(mutation.fields)["actor"] != self.writer_login:
            raise UnsupportedForge("explicit configured GitHub writer identity required")

    def apply(self, delivery: DriverDelivery, before_send: Callable[[], None]) -> DriverReceipt:
        self.validate_mutation(delivery.mutation)
        if delivery.attempts != 1:
            return self.reconcile(delivery)
        target = delivery.mutation.target
        current = self.change(target.key)
        if current.presence is not Presence.FOUND:
            return DriverReceipt(EffectStatus.UNKNOWN, delivery.operation, reason="change-unavailable")
        if current.subject != target:
            return DriverReceipt(EffectStatus.STALE, delivery.operation, reason="native-head-drift")
        body = dict(delivery.mutation.fields)["body"] + "\n\n" + effect_marker(delivery.operation)
        try:
            status, payload, _ = self.http.request(
                "POST",
                f"{self.prefix}/pulls/{target.key}/reviews",
                {"commit_id": target.revision, "event": "COMMENT", "body": body},
                before_send=before_send,
            )
        except OSError:
            return DriverReceipt(EffectStatus.UNKNOWN, delivery.operation, reason="review-delivery-unknown")
        try:
            data = object_value(payload)
            number = native_number(data.get("id"))
            if (
                status != 200
                or data.get("commit_id") != target.revision
                or data.get("body") != body
                or data.get("state") != "COMMENTED"
                or object_value(data.get("user")).get("login") != self.writer_login
            ):
                raise ValueError("review receipt differs")
            reference = DriverSubject(self.binding, "review", target.key + "/" + number, None).reference
            return DriverReceipt(
                EffectStatus.ACCEPTED, delivery.operation, reference, "native-append;not-current-head-CAS"
            )
        except ValueError:
            return DriverReceipt(EffectStatus.UNKNOWN, delivery.operation, reason="unqualified-native-reply")

    def reconcile(self, delivery: DriverDelivery) -> DriverReceipt:
        self.validate_mutation(delivery.mutation)
        scan = self.reviews(delivery.mutation.target.key)
        body = dict(delivery.mutation.fields)["body"] + "\n\n" + effect_marker(delivery.operation)
        matches = [
            item
            for item in scan.items
            if object_value_json(item.native).get("body") == body
            and item.related
            == (
                DriverSubject(
                    self.binding, "commit", delivery.mutation.target.revision or "", delivery.mutation.target.revision
                ),
            )
            and object_value_json(item.native).get("state") == "COMMENTED"
        ]
        if (
            scan.complete
            and scan.stable
            and len(matches) == 1
            and object_value(object_value_json(matches[0].native).get("user")).get("login") == self.writer_login
        ):
            return DriverReceipt(
                EffectStatus.ACCEPTED, delivery.operation, matches[0].subject.reference, "reconciled-native-append"
            )
        return DriverReceipt(
            EffectStatus.UNKNOWN, delivery.operation, reason="absence-or-duplicate-not-nonexecution-proof"
        )


def object_value_json(value: bytes) -> dict[str, object]:
    import json

    raw: object = json.loads(value)
    return object_value(raw)


class JenkinsCI:
    """Exact numbered builds, with Git BuildData evidence, never branch/job-name matching."""

    API_VERSION = "jenkins-2.479.3;git-5.7.0;git-client-6.1.0"

    def __init__(
        self, http: DriverHTTP, clock: Callable[[], int], *, remote: str, source_binding: DriverBinding, ttl: int = 30
    ) -> None:
        self.binding = _bound(http, "ci", "jenkins", self.API_VERSION)
        self.prefix = "/" + "/".join("job/" + quote(part, safe="") for part in self.binding.namespace.split("/"))
        if http.prefix != self.prefix or not 0 < ttl <= 3600 or not remote.startswith("https://"):
            raise ValueError("exact Jenkins job/SCM remote and finite freshness required")
        self.http, self.clock, self.remote, self.ttl = http, clock, remote, ttl
        self.source_binding = source_binding

    def build(self, key: str, source: DriverSubject) -> DriverFact:
        GitHubSCM.number(key)
        oid(source.revision or "")
        if source.binding != self.source_binding or source.kind != "commit" or source.key != source.revision:
            raise ValueError("exact SCM commit subject required")
        now = self.clock()
        tree = (
            "number,building,result,timestamp,duration,actions[_class,remoteUrls,lastBuiltRevision[SHA1]],"
            "artifacts[fileName,relativePath]"
        )
        status, payload, headers = _read(self.http, f"{self.prefix}/{key}/api/json?{urlencode({'tree': tree})}")
        try:
            data = object_value(payload)
            if (
                status != 200
                or headers.get("x-jenkins") != "2.479.3"
                or native_number(data.get("number")) != key
                or type(data.get("building")) is not bool
                or type(data.get("timestamp")) is not int
                or type(data.get("duration")) is not int
            ):
                raise ValueError("unsupported/unqualified Jenkins build")
            actions = [object_value(value) for value in list_value(data.get("actions"))]
            matching = [
                action
                for action in actions
                if action.get("_class") == "hudson.plugins.git.util.BuildData"
                and action.get("remoteUrls") == [self.remote]
            ]
            if len(matching) != 1 or object_value(matching[0].get("lastBuiltRevision")).get("SHA1") != source.revision:
                raise ValueError("exact checkout unavailable or ambiguous")
            if data.get("result") not in {None, "SUCCESS", "FAILURE", "UNSTABLE", "ABORTED", "NOT_BUILT"}:
                raise ValueError("unknown native Jenkins result")
            if data["building"] is False and data.get("result") is None:
                raise ValueError("terminal result unavailable")
            list_value(data.get("artifacts"))
            return native_fact(
                self.binding,
                "build",
                key,
                None,
                now,
                self.ttl,
                data,
                related=(source,),
                qualification=(
                    "build-native-revision-unavailable;native-Git-BuildData;"
                    "configured-plugin-profile;not-Kernel-verification"
                ),
            )
        except ValueError:
            return unavailable_fact(self.binding, "build", key, now, status)

    def validate_mutation(self, mutation: DriverMutation) -> None:
        raise UnsupportedForge("Jenkins trigger/cancel not supported by this exact-subject read profile")


class ConfluenceKnowledge:
    def __init__(
        self, http: DriverHTTP, clock: Callable[[], int], *, ttl: int = 30, writer_account_id: str | None = None
    ) -> None:
        self.binding = _bound(http, "knowledge", "confluence-cloud", "2")
        if http.prefix != "/wiki/api/v2/pages" or not 0 < ttl <= 3600:
            raise ValueError("bounded Confluence Cloud v2 page interface required")
        self.http, self.clock, self.ttl = http, clock, ttl
        self.writer_account_id = writer_account_id

    def content(self, key: str, revision: str) -> DriverFact:
        GitHubSCM.number(key)
        GitHubSCM.number(revision)
        now = self.clock()
        path = f"/wiki/api/v2/pages/{key}?body-format=storage"
        status, payload, _ = _read(self.http, path)
        try:
            latest = object_value(payload)
            if status != 200 or latest.get("status") != "current" or latest.get("spaceId") != self.binding.namespace:
                raise ValueError("page unavailable/withdrawn or wrong space")
            status, payload, _ = _read(self.http, path + f"&version={revision}")
            data = object_value(payload)
            version = object_value(data.get("version"))
            if (
                status != 200
                or data.get("id") != key
                or data.get("spaceId") != self.binding.namespace
                or native_number(version.get("number")) != revision
                or data.get("status") not in {"current", "historical"}
            ):
                raise ValueError("versioned page identity differs")
            text(data.get("title"))
            storage = object_value(object_value(data.get("body")).get("storage"))
            if storage.get("representation") != "storage" or type(storage.get("value")) is not str:
                raise ValueError("versioned content unavailable")
            # Recheck current visibility/status after the pinned historical read.
            final_status, final, _ = _read(self.http, path)
            if (
                final_status != 200
                or driver_bytes(final) != driver_bytes(latest)
                or not now <= self.clock() <= now + self.ttl
            ):
                raise ValueError("page/permission observation moved")
            return native_fact(
                self.binding,
                "page",
                key,
                revision,
                now,
                self.ttl,
                data,
                qualification="pinned-storage-content;visible-current-reread;rights-from-config;not-authority",
            )
        except ValueError:
            return unavailable_fact(self.binding, "page", key, now, status)

    def validate_mutation(self, mutation: DriverMutation) -> None:
        if (
            mutation.target.binding != self.binding
            or mutation.target.kind != "page"
            or mutation.action != "replace_page"
            or set(dict(mutation.fields)) != {"actor", "body", "title"}
        ):
            raise UnsupportedForge("only version-conditional current page replacement supported")
        GitHubSCM.number(mutation.target.key)
        GitHubSCM.number(mutation.target.revision or "")
        text(dict(mutation.fields)["title"])
        if self.writer_account_id is None or dict(mutation.fields)["actor"] != self.writer_account_id:
            raise UnsupportedForge("explicit configured Confluence writer account required")

    def apply(self, delivery: DriverDelivery, before_send: Callable[[], None]) -> DriverReceipt:
        self.validate_mutation(delivery.mutation)
        if delivery.attempts != 1:
            return self.reconcile(delivery)
        target = delivery.mutation.target
        revision = str(int(target.revision or "") + 1)
        current = self.content(target.key, target.revision or "")
        if current.presence is not Presence.FOUND:
            return DriverReceipt(
                EffectStatus.UNKNOWN, delivery.operation, reason="page-space-or-visibility-unavailable"
            )
        fields = dict(delivery.mutation.fields)
        message = effect_marker(delivery.operation)
        # version.number is the native server-enforced optimistic concurrency token.
        try:
            status, payload, _ = self.http.request(
                "PUT",
                f"/wiki/api/v2/pages/{target.key}",
                {
                    "id": target.key,
                    "status": "current",
                    "title": fields["title"],
                    "body": {"representation": "storage", "value": fields["body"]},
                    "version": {"number": int(revision), "message": message},
                },
                before_send=before_send,
            )
        except OSError:
            return DriverReceipt(EffectStatus.UNKNOWN, delivery.operation, reason="page-delivery-unknown")
        if status == 409:
            return DriverReceipt(EffectStatus.STALE, delivery.operation, reason="native-version-conflict")
        if status == 200 and self._matches(payload, delivery, revision):
            # A valid update response can precede indexed visibility; it is not commit proof.
            return self.reconcile(delivery)
        return DriverReceipt(EffectStatus.UNKNOWN, delivery.operation, reason="page-reply-unqualified")

    def _matches(self, payload: object, delivery: DriverDelivery, revision: str) -> bool:
        try:
            data = object_value(payload)
            version = object_value(data.get("version"))
            fields = dict(delivery.mutation.fields)
            storage = object_value(object_value(data.get("body")).get("storage"))
            return (
                data.get("id") == delivery.mutation.target.key
                and data.get("spaceId") == self.binding.namespace
                and data.get("title") == fields["title"]
                and storage.get("value") == fields["body"]
                and storage.get("representation") == "storage"
                and native_number(version.get("number")) == revision
                and version.get("message") == effect_marker(delivery.operation)
                and version.get("authorId") == self.writer_account_id
            )
        except ValueError:
            return False

    def reconcile(self, delivery: DriverDelivery) -> DriverReceipt:
        self.validate_mutation(delivery.mutation)
        target = delivery.mutation.target
        revision = str(int(target.revision or "") + 1)
        fact = self.content(target.key, revision)
        if fact.presence is Presence.FOUND and self._matches(object_value_json(fact.native), delivery, revision):
            return DriverReceipt(
                EffectStatus.ACCEPTED, delivery.operation, fact.subject.reference, "reconciled-versioned-page"
            )
        return DriverReceipt(EffectStatus.UNKNOWN, delivery.operation, reason="missing-version-not-nonexecution-proof")


class RepositoryDocumentation:
    """Independently useful local exact-object knowledge reader, without Confluence fallback."""

    def __init__(
        self, binding: DriverBinding, source: GitContentSource, clock: Callable[[], int], *, ttl: int = 30
    ) -> None:
        if (binding.family, binding.implementation, binding.api_version) != (
            "knowledge",
            "repository-docs",
            "git-object-v1",
        ):
            raise ValueError("unsupported repository documentation contract")
        if not 0 < ttl <= 3600:
            raise ValueError("finite documentation freshness required")
        if (source.binding.origin, source.binding.repository) != (binding.origin, binding.namespace):
            raise ValueError("repository documentation source binding differs")
        self.binding, self.source, self.clock, self.ttl = binding, source, clock, ttl

    def content(self, key: str, revision: str) -> DriverFact:
        oid(revision)
        now = self.clock()
        data = self.source.read(revision, key)
        if data is None:
            return unavailable_fact(self.binding, "document", key, now, 0)
        return native_fact(
            self.binding,
            "document",
            key,
            revision,
            now,
            self.ttl,
            {"content_hex": data.hex(), "sha256": hashlib.sha256(data).hexdigest()},
            qualification="exact-local-Git-blob;configured-repository-and-rights;not-authority",
        )
