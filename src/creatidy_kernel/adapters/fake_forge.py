# SPDX-License-Identifier: Apache-2.0
"""Independent in-memory Forge and synthetic HTTP/Git fixtures."""

from collections.abc import Callable, Mapping
from typing import cast
from urllib.parse import parse_qs, unquote, urlsplit

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
    effect_marker,
)
from creatidy_kernel.ports.forge import Forge


class SyntheticForgeTransport:
    def __init__(self, repository: Reference, *, page_size: int = 2) -> None:
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

    def authoritative_absence(self, path: str) -> bool:
        return self.direct_absence and not self.forbidden

    def compare_and_push(
        self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
    ) -> EffectStatus:
        if repository != self.repository or self.forbidden:
            return EffectStatus.UNKNOWN
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
            matching = [pull for pull in self.pulls if pull["number"] == int(route[1])]
            return (200, matching[0]) if matching else (404, {})
        if method == "POST" and route == ["pulls"] and body is not None:
            self.posts += 1
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
        return frozenset({"identity", "branch", "change", "checks", "branch_create", "conditional_push", "pr"})

    def identity(self, repository: Reference) -> Observation:
        if self.transport.forbidden:
            return Observation(Presence.INACCESSIBLE)
        if self.transport.repository_missing:
            return Observation(Presence.ABSENT if self.transport.direct_absence else Presence.UNKNOWN)
        if repository != self.transport.repository:
            return Observation(Presence.UNKNOWN)
        return Observation(Presence.FOUND, repository)

    def branch(self, repository: Reference, branch: str) -> Observation:
        if self.identity(repository).presence is not Presence.FOUND:
            return Observation(self.identity(repository).presence)
        sha = self.transport.branches.get(branch)
        if sha:
            return Observation(Presence.FOUND, revision=Reference(f"forgejo:{sha}"))
        return Observation(Presence.ABSENT if self.transport.direct_absence else Presence.UNKNOWN)

    def change(self, repository: Reference, change: Reference) -> Observation:
        suffix = change.value.removeprefix(f"{repository.value}#")
        if (
            not change.value.startswith(f"{repository.value}#")
            or not suffix.isascii()
            or not suffix.isdigit()
            or suffix[0] == "0"
        ):
            raise ForgeConflict("change does not belong to repository")
        if self.identity(repository).presence is not Presence.FOUND:
            return Observation(self.identity(repository).presence)
        for pull in self.transport.pulls:
            if change == Reference(f"{repository.value}#{pull['number']}"):
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
        head_sha = cast(dict[str, object], head).get("sha")
        base_sha = cast(dict[str, object], base).get("sha")
        if not isinstance(head_sha, str) or not isinstance(base_sha, str):
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
        if self.identity(repository).presence is not Presence.FOUND:
            return [], None, False
        if cursor is not None and (not cursor.isascii() or not cursor.isdigit() or cursor[0] == "0"):
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
                not isinstance(check_id, int)
                or check_id <= 0
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
        if effect.repository != self.transport.repository or not self.authorize(effect):
            raise ForgeConflict("exact forge effect was not authorized")
        self._revision(effect.revision)
        if effect.expected is not None:
            self._revision(effect.expected)
        if effect.base_revision is not None:
            self._revision(effect.base_revision)

    @staticmethod
    def _revision(reference: Reference | None) -> None:
        if reference is not None and not reference.value.startswith("forgejo:"):
            raise ForgeConflict("wrong revision provider")

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
        base = self.branch(effect.repository, effect.base_branch or "")
        head = self.branch(effect.repository, effect.branch)
        if base.presence is not Presence.FOUND or head.presence is not Presence.FOUND:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        if base.revision != effect.base_revision or head.revision != effect.revision:
            return Receipt(EffectStatus.STALE, effect.operation)
        self.transport.posts += 1
        if self.transport.domain_rejection or self.transport.forbidden:
            return Receipt(EffectStatus.REJECTED, effect.operation)
        marker = effect_marker(effect.operation)
        record: dict[str, object] = {
            "number": len(self.transport.pulls) + 1,
            "head": {
                "sha": effect.revision.value.removeprefix("forgejo:"),
                "ref": effect.branch,
                "repo": {"full_name": effect.repository.value.removeprefix("forgejo:")},
            },
            "base": {
                "sha": effect.base_revision.value.removeprefix("forgejo:") if effect.base_revision else "",
                "ref": effect.base_branch,
                "repo": {"full_name": effect.repository.value.removeprefix("forgejo:")},
            },
            "title": effect.title,
            "body": f"{effect.body or ''}\n\n{marker}",
        }
        self.transport.pulls.append(record)
        if self.transport.lost_reply:
            self.transport.lost_reply = False
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        pull = self._pull(effect.repository, record)
        if (
            self.branch(effect.repository, effect.base_branch or "").revision != effect.base_revision
            or self.branch(effect.repository, effect.branch).revision != effect.revision
        ):
            return Receipt(EffectStatus.UNKNOWN, effect.operation, pull.reference)
        return Receipt(EffectStatus.ACCEPTED, effect.operation, pull.reference)

    def reconcile(self, effect: Effect, known_reference: Reference | None = None) -> Receipt:
        self._authorized(effect)
        if effect.action != "pr":
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        if known_reference is None:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        suffix = known_reference.value.removeprefix(f"{effect.repository.value}#")
        if (
            not known_reference.value.startswith(f"{effect.repository.value}#")
            or not suffix.isascii()
            or not suffix.isdigit()
            or suffix[0] == "0"
        ):
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
                observation.reference != known_reference
                or pull.get("body") != f"{effect.body or ''}\n\n{effect_marker(effect.operation)}"
                or pull.get("title") != effect.title
                or not isinstance(head, dict)
                or not isinstance(base, dict)
                or cast(dict[str, object], head).get("ref") != effect.branch
                or cast(dict[str, object], base).get("ref") != effect.base_branch
                or observation.head != effect.revision
                or observation.base != effect.base_revision
            ):
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            if (
                self.branch(effect.repository, effect.base_branch or "").revision != effect.base_revision
                or self.branch(effect.repository, effect.branch).revision != effect.revision
            ):
                return Receipt(EffectStatus.UNKNOWN, effect.operation, known_reference, "head/base moved")
            return Receipt(EffectStatus.ACCEPTED, effect.operation, known_reference)
        return Receipt(EffectStatus.UNKNOWN, effect.operation)
