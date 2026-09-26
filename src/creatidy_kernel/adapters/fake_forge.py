# SPDX-License-Identifier: Apache-2.0
"""Independent in-memory Forge and synthetic HTTP/Git fixtures."""

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

from creatidy_kernel.adapters.forge_refs import (
    ForgeBinding,
    oid,
    positive_id,
    pr_payload,
    repository_path,
    valid_branch,
)
from creatidy_kernel.adapters.forge_refs import number as _number
from creatidy_kernel.core.forge import (
    CheckResult,
    Effect,
    EffectStatus,
    ForgeConflict,
    Observation,
    Page,
    Presence,
    Receipt,
    Reference,
    UnsupportedForge,
)
from creatidy_kernel.ports.forge import Forge


class SyntheticForgeTransport:
    def __init__(self, repository: Reference, *, page_size: int = 2) -> None:
        self.binding = ForgeBinding("https://synthetic.invalid", repository_path(repository))
        self.repository = repository
        self.page_size = page_size
        self.branches: dict[str, str] = {"develop": "b" * 40, "feature": "a" * 40}
        self.pulls: list[dict[str, object]] = []
        self.statuses: list[dict[str, object]] = []
        self.status_revision = "a" * 40
        self.forbidden = False
        self.lost_reply = False
        self.domain_rejection = False
        self.pushes = 0
        self.posts = 0
        self.direct_absence = False
        self.repository_missing = False
        self.supports_reads = True
        self.supports_pr = True
        self.supports_conditional_push = True
        self.supports_write_once = True
        self.max_bytes = 1_000_000
        self._held_snapshot: str | None = None
        self.contend_snapshot = False
        self.tamper_snapshot_on_post = False
        self.move_base_on_post = False

    @contextmanager
    def hold(self, repository: Reference, branch: str):
        if repository != self.repository or not branch.startswith("kernel/snapshots/"):
            raise ForgeConflict("snapshot outside isolation binding")
        if not self.supports_write_once:
            raise UnsupportedForge("snapshot isolation unavailable")
        if self._held_snapshot is not None:
            raise UnsupportedForge("snapshot isolation already occupied")
        self._held_snapshot = branch
        try:
            yield
        finally:
            self._held_snapshot = None

    def authoritative_absence(self, path: str) -> bool:
        return self.direct_absence and not self.forbidden

    def compare_and_push(
        self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
    ) -> EffectStatus:
        if repository != self.repository:
            raise ForgeConflict("repository outside configured Git remote")
        if self.forbidden:
            return EffectStatus.UNKNOWN
        if branch.startswith("kernel/snapshots/"):
            if self._held_snapshot != branch or expected is not None:
                raise UnsupportedForge("snapshot requires exclusive absent-target creation")
            if self.contend_snapshot:
                self.branches[branch] = "c" * 40
                return EffectStatus.STALE
            if branch in self.branches:
                return EffectStatus.STALE
        old = self.branches.get(branch)
        if old != (expected.value.removeprefix("forgejo:") if expected else None):
            return EffectStatus.STALE
        self.pushes += 1
        self.branches[branch] = revision.value.removeprefix("forgejo:")
        if self.lost_reply:
            self.lost_reply = False
            raise OSError("synthetic lost push reply")
        return EffectStatus.ACCEPTED

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        if self.forbidden:
            return 403, {}
        uri = urlsplit(path)
        parts = [unquote(part) for part in uri.path.split("/") if part]
        if len(parts) < 3 or parts[:3] != ["repos", *self.repository.value.removeprefix("forgejo:").split("/")]:
            return 404, {}
        if self.repository_missing and method == "GET":
            return 404, {}
        route = parts[3:]
        if method == "GET" and not route:
            return 200, {"full_name": self.repository.value.removeprefix("forgejo:")}
        if method == "GET" and len(route) == 2 and route[0] == "branches":
            sha = self.branches.get(route[1])
            return (200, {"name": route[1], "commit": {"id": sha}}) if sha else (404, {})
        if method == "GET" and len(route) == 3 and route[0] == "commits" and route[2] == "statuses":
            return self._paged(self.statuses if route[1] == self.status_revision else [], uri.query)
        if method == "GET" and route == ["pulls"]:
            return self._paged(self.pulls, uri.query)
        if method == "GET" and len(route) == 2 and route[0] == "pulls":
            if not _number(route[1]):
                return 404, {}
            matching = [pull for pull in self.pulls if pull.get("number") == int(route[1])]
            return (200, matching[0]) if matching else (404, {})
        if method == "POST" and route == ["pulls"] and body is not None:
            self.posts += 1
            if self.move_base_on_post:
                self.branches[str(body["base"])] = "d" * 40
            if self.tamper_snapshot_on_post:
                self.branches[str(body["head"])] = "c" * 40
            if self.domain_rejection:
                return 422, {"message": "rejected"}
            head = self.branches.get(str(body["head"]))
            base = self.branches.get(str(body["base"]))
            if not head or not base:
                return 422, {"message": "missing branch"}
            pull: dict[str, object] = {
                "number": len(self.pulls) + 1,
                "head": {
                    "sha": head,
                    "ref": body["head"],
                    "repo": {"full_name": self.repository.value.removeprefix("forgejo:")},
                },
                "base": {
                    "sha": base,
                    "ref": body["base"],
                    "repo": {"full_name": self.repository.value.removeprefix("forgejo:")},
                },
                "body": body["body"],
                "title": body["title"],
            }
            self.pulls.append(pull)
            if self.lost_reply:
                self.lost_reply = False
                raise OSError("synthetic lost PR reply")
            return 201, pull
        return 404, {}

    def _paged(self, records: list[dict[str, object]], query: str) -> tuple[int, object]:
        parameters = parse_qs(query)
        page = int(parameters.get("page", ["1"])[0])
        limit = min(int(parameters.get("limit", [str(self.page_size)])[0]), self.page_size)
        return 200, records[(page - 1) * limit : page * limit]


class FakeForge(Forge):
    """Models forge state without HTTP or the Forgejo adapter's payload mapping."""

    def __init__(self, transport: SyntheticForgeTransport, authorize: Callable[[Effect], bool]) -> None:
        self.transport = transport
        self.authorize = authorize

    def capabilities(self) -> frozenset[str]:
        capabilities: set[str] = set()
        if self.transport.supports_reads:
            capabilities.update({"identity", "branch", "change", "checks"})
        if self.transport.supports_conditional_push:
            capabilities.add("conditional_push")
            if self.transport.supports_reads:
                capabilities.add("branch_create")
        if (
            self.transport.supports_reads
            and self.transport.supports_pr
            and self.transport.supports_conditional_push
            and self.transport.supports_write_once
        ):
            capabilities.add("pr")
        return frozenset(capabilities)

    def _repo(self, repository: Reference) -> None:
        repository_path(repository)
        if repository != self.transport.repository:
            raise ForgeConflict("repository outside configured forge binding")

    def _require_reads(self) -> None:
        if not self.transport.supports_reads:
            raise UnsupportedForge("forge reads unsupported")

    def identity(self, repository: Reference) -> Observation:
        self._require_reads()
        self._repo(repository)
        if self.transport.forbidden:
            return Observation(Presence.INACCESSIBLE)
        if self.transport.repository_missing:
            return Observation(Presence.ABSENT if self.transport.direct_absence else Presence.UNKNOWN)
        if repository != self.transport.repository:
            return Observation(Presence.UNKNOWN)
        return Observation(Presence.FOUND, repository)

    def branch(self, repository: Reference, branch: str) -> Observation:
        self._require_reads()
        self._repo(repository)
        valid_branch(branch)
        if self.identity(repository).presence is not Presence.FOUND:
            return Observation(self.identity(repository).presence)
        sha = self.transport.branches.get(branch)
        if sha:
            if not _valid_oid(sha):
                return Observation(Presence.UNKNOWN)
            return Observation(Presence.FOUND, revision=Reference(f"forgejo:{sha}"))
        return Observation(Presence.ABSENT if self.transport.direct_absence else Presence.UNKNOWN)

    def change(self, repository: Reference, change: Reference) -> Observation:
        self._require_reads()
        self._repo(repository)
        suffix = change.value.removeprefix(f"{repository.value}#")
        if not change.value.startswith(f"{repository.value}#") or not _number(suffix):
            raise ForgeConflict("change does not belong to repository")
        if self.identity(repository).presence is not Presence.FOUND:
            return Observation(self.identity(repository).presence)
        for pull in self.transport.pulls:
            if change.value == f"{repository.value}#{pull.get('number')}":
                try:
                    return self._pull(repository, pull)
                except ValueError:
                    return Observation(Presence.UNKNOWN)
        return Observation(Presence.ABSENT if self.transport.direct_absence else Presence.UNKNOWN)

    @staticmethod
    def _pull(repository: Reference, pull: dict[str, object]) -> Observation:
        head = pull.get("head")
        base = pull.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ValueError("invalid synthetic pull")
        repo = {"full_name": repository.value.removeprefix("forgejo:")}
        if cast(dict[str, object], head).get("repo") != repo or cast(dict[str, object], base).get("repo") != repo:
            raise ValueError("pull repository differs")
        number = pull.get("number")
        if not positive_id(number):
            raise ValueError("invalid pull request number")
        head_sha = cast(dict[str, object], head).get("sha")
        base_sha = cast(dict[str, object], base).get("sha")
        if (
            not isinstance(head_sha, str)
            or not isinstance(base_sha, str)
            or not _valid_oid(head_sha)
            or not _valid_oid(base_sha)
        ):
            raise ValueError("invalid synthetic revisions")
        return Observation(
            Presence.FOUND,
            Reference(f"{repository.value}#{pull['number']}"),
            base=Reference(f"forgejo:{base_sha}"),
            head=Reference(f"forgejo:{head_sha}"),
        )

    def _page(
        self, repository: Reference, records: list[dict[str, object]], cursor: str | None
    ) -> tuple[list[dict[str, object]], str | None, bool]:
        self._require_reads()
        self._repo(repository)
        if self.identity(repository).presence is not Presence.FOUND:
            return [], None, False
        if cursor is not None and not _number(cursor):
            return [], None, False
        page = 1 if cursor is None else int(cursor)
        size = self.transport.page_size
        items = records[(page - 1) * size : page * size]
        return items, str(page + 1) if items else None, not items

    def changes(self, repository: Reference, cursor: str | None = None) -> Page:
        records, next_cursor, complete = self._page(repository, self.transport.pulls, cursor)
        try:
            return Page(tuple(self._pull(repository, pull) for pull in records), next_cursor, complete)
        except ValueError:
            return Page((), None, False)

    def checks(self, repository: Reference, revision: Reference, cursor: str | None = None) -> Page:
        self._require_reads()
        self._repo(repository)
        self._revision(revision)
        records, next_cursor, complete = self._page(
            repository,
            self.transport.statuses if revision.value == f"forgejo:{self.transport.status_revision}" else [],
            cursor,
        )
        checks: list[Observation] = []
        for data in records:
            check_id, context, status = (data.get(name) for name in ("id", "context", "status"))
            if (
                not positive_id(check_id)
                or not isinstance(context, str)
                or not context
                or not isinstance(status, str)
                or not status
            ):
                return Page((), None, False)
            result = {
                "pending": CheckResult.PENDING,
                "success": CheckResult.PASSED,
                "failure": CheckResult.FAILED,
                "error": CheckResult.ERROR,
            }.get(status, CheckResult.UNKNOWN)
            checks.append(
                Observation(
                    Presence.FOUND,
                    Reference(f"{repository.value}@{check_id}"),
                    revision=revision,
                    check_context=context,
                    check_result=result,
                )
            )
        return Page(tuple(checks), next_cursor, complete)

    def _authorized(self, effect: Effect) -> None:
        self._repo(effect.repository)
        valid_branch(effect.branch)
        if effect.base_branch is not None:
            valid_branch(effect.base_branch)
        if effect.repository != self.transport.repository or not self.authorize(effect):
            raise ForgeConflict("exact forge effect was not authorized")
        self._revision(effect.revision)
        if effect.expected is not None:
            self._revision(effect.expected)
        if effect.base_revision is not None:
            self._revision(effect.base_revision)
        required = {"branch": "branch_create", "push": "conditional_push", "pr": "pr"}[effect.action]
        if required not in self.capabilities():
            raise UnsupportedForge(f"{required} unsupported")

    @staticmethod
    def _revision(reference: Reference | None) -> None:
        if reference is not None:
            if not reference.value.startswith("forgejo:"):
                raise ForgeConflict("wrong revision provider")
            if not _valid_oid(reference.value.removeprefix("forgejo:")):
                raise ForgeConflict("revision must be a full Git object ID")

    def apply(self, effect: Effect) -> Receipt:
        self._authorized(effect)
        if effect.delivery_attempts > 1:
            return self.reconcile(effect)
        if effect.revision is None:
            raise ForgeConflict("revision required")
        if effect.action in {"branch", "push"}:
            if effect.action == "branch":
                if effect.expected is not None:
                    raise ForgeConflict("new branch must expect absence")
                source = self.branch(effect.repository, effect.base_branch or "")
                if source.presence is not Presence.FOUND:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                if source.revision != effect.base_revision:
                    return Receipt(EffectStatus.STALE, effect.operation)
            if self.transport.forbidden:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            old = self.transport.branches.get(effect.branch)
            expected = effect.expected.value.removeprefix("forgejo:") if effect.expected else None
            if old != expected:
                return Receipt(EffectStatus.STALE, effect.operation)
            self.transport.branches[effect.branch] = effect.revision.value.removeprefix("forgejo:")
            self.transport.pushes += 1
            if self.transport.lost_reply:
                self.transport.lost_reply = False
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            if (
                effect.action == "branch"
                and self.branch(effect.repository, effect.base_branch or "").revision != effect.base_revision
            ):
                return Receipt(EffectStatus.UNKNOWN, effect.operation, effect.revision)
            return Receipt(EffectStatus.ACCEPTED, effect.operation, effect.revision)
        try:
            snapshot, body, _ = pr_payload(effect, self.transport.max_bytes)
        except ValueError:
            return Receipt(EffectStatus.REJECTED, effect.operation, reason="local request limit")
        base = self.branch(effect.repository, effect.base_branch or "")
        if base.presence is not Presence.FOUND:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        with self.transport.hold(effect.repository, snapshot):
            try:
                status = self.transport.compare_and_push(effect.repository, snapshot, None, effect.revision)
            except OSError:
                status = EffectStatus.UNKNOWN
            if status is EffectStatus.STALE:
                raise ForgeConflict("snapshot namespace occupied")
            if status is not EffectStatus.ACCEPTED:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            after = self.branch(effect.repository, snapshot)
            if after.presence is Presence.FOUND and after.revision != effect.revision:
                raise ForgeConflict("snapshot head differs")
            if after.presence is not Presence.FOUND:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            try:
                status, record = self.transport.request(
                    "POST",
                    f"/repos/{self.transport.binding.repository}/pulls",
                    {"head": snapshot, "base": effect.base_branch or "", "title": effect.title or "", "body": body},
                )
            except OSError:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            if status in {403, 409, 413, 422, 423}:
                return Receipt(EffectStatus.REJECTED, effect.operation)
            if status != 201 or not isinstance(record, dict):
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            data = cast(dict[str, object], record)
            try:
                pull = self._pull(effect.repository, data)
            except ValueError:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            head_data, base_data = data.get("head"), data.get("base")
            if (
                pull.head != effect.revision
                or not isinstance(head_data, dict)
                or cast(dict[str, object], head_data).get("ref") != snapshot
                or not isinstance(base_data, dict)
                or cast(dict[str, object], base_data).get("ref") != effect.base_branch
                or data.get("body") != body
                or data.get("title") != effect.title
            ):
                raise ForgeConflict("PR response differs from authorized head or base identity")
            final_snapshot = self.branch(effect.repository, snapshot)
            if final_snapshot.presence is Presence.FOUND and final_snapshot.revision != effect.revision:
                raise ForgeConflict("snapshot head moved during PR creation")
            if final_snapshot.presence is not Presence.FOUND:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            return Receipt(EffectStatus.ACCEPTED, effect.operation, pull.reference, observed_base=pull.base)

    def reconcile(self, effect: Effect, known_reference: Reference | None = None) -> Receipt:
        self._authorized(effect)
        if effect.action != "pr":
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        if known_reference is None:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        try:
            snapshot, body, _ = pr_payload(effect, self.transport.max_bytes)
        except ValueError:
            return Receipt(EffectStatus.REJECTED, effect.operation, reason="local request limit")
        suffix = known_reference.value.removeprefix(f"{effect.repository.value}#")
        if not known_reference.value.startswith(f"{effect.repository.value}#") or not _number(suffix):
            raise ForgeConflict("change does not belong to repository")
        if self.change(effect.repository, known_reference).presence is not Presence.FOUND:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        for pull in self.transport.pulls:
            if pull.get("number") != int(suffix):
                continue
            try:
                observation = self._pull(effect.repository, pull)
            except ValueError:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            head = pull.get("head")
            base = pull.get("base")
            if (
                not isinstance(head, dict)
                or not isinstance(base, dict)
                or cast(dict[str, object], head).get("ref") != snapshot
                or cast(dict[str, object], base).get("ref") != effect.base_branch
                or observation.head != effect.revision
            ):
                raise ForgeConflict("PR head or base identity differs from authorized effect")
            if (
                observation.reference != known_reference
                or pull.get("body") != body
                or pull.get("title") != effect.title
            ):
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            current = self.branch(effect.repository, snapshot)
            if current.presence is Presence.FOUND and current.revision != effect.revision:
                raise ForgeConflict("snapshot head moved")
            if current.presence is not Presence.FOUND:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            return Receipt(EffectStatus.ACCEPTED, effect.operation, known_reference, observed_base=observation.base)
        return Receipt(EffectStatus.UNKNOWN, effect.operation)


def _valid_oid(value: str) -> bool:
    try:
        oid(value)
        return True
    except ValueError:
        return False
