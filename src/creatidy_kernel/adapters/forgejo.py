# SPDX-License-Identifier: Apache-2.0
"""Forgejo reference adapter with injected HTTP and conditional Git transport.

The caller must durably claim the Operation and supply a trusted authorization
predicate. Neither HTTP success nor a locally cached key establishes acceptance.
"""

from collections.abc import Callable, Mapping
from typing import Protocol, cast
from urllib.parse import quote

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


class HTTPTransport(Protocol):
    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]: ...


class GitTransport(Protocol):
    """Atomic expected-old ref update; never an unconditional push."""

    def compare_and_push(
        self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
    ) -> EffectStatus: ...


def _object(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("unexpected forge payload")
    entries = cast(dict[object, object], payload)
    if not all(isinstance(key, str) for key in entries):
        raise ValueError("unexpected forge payload")
    return {str(key): value for key, value in entries.items()}


def _field(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError("missing forge field")
    return value


class ForgejoForge(Forge):
    def __init__(
        self,
        http: HTTPTransport,
        git: GitTransport,
        authorize: Callable[[Effect], bool],
        *,
        page_size: int = 30,
    ) -> None:
        if page_size <= 0:
            raise ValueError("page size must be positive")
        self.http = http
        self.git = git
        self.authorize = authorize
        self.page_size = page_size

    def capabilities(self) -> frozenset[str]:
        return frozenset({"identity", "branch", "change", "checks", "branch_create", "conditional_push", "pr"})

    def _repo(self, reference: Reference) -> str:
        if not reference.value.startswith("forgejo:"):
            raise ForgeConflict("wrong forge provider")
        path = reference.value.removeprefix("forgejo:")
        if len(path.split("/")) != 2 or any(not part or part in {".", ".."} for part in path.split("/")):
            raise ForgeConflict("invalid repository handle")
        return "/".join(quote(part, safe="") for part in path.split("/"))

    @staticmethod
    def _revision(reference: Reference) -> str:
        if not reference.value.startswith("forgejo:"):
            raise ForgeConflict("wrong revision provider")
        return reference.value.removeprefix("forgejo:")

    def _request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        return self.http.request(method, path, body)

    def _read(self, path: str) -> tuple[int, object]:
        try:
            return self._request("GET", path)
        except OSError:
            return 0, None

    def identity(self, repository: Reference) -> Observation:
        status, payload = self._read(f"/repos/{self._repo(repository)}")
        if status == 403:
            return Observation(Presence.INACCESSIBLE)
        if status != 200:
            return Observation(Presence.UNKNOWN)
        try:
            data = _object(payload)
            name = _field(data, "full_name")
        except ValueError:
            return Observation(Presence.UNKNOWN)
        if name != repository.value.removeprefix("forgejo:"):
            return Observation(Presence.UNKNOWN)
        return Observation(Presence.FOUND, repository)

    def branch(self, repository: Reference, branch: str) -> Observation:
        status, payload = self._read(f"/repos/{self._repo(repository)}/branches/{quote(branch, safe='')}")
        if status == 403:
            return Observation(Presence.INACCESSIBLE)
        if status != 200:
            return Observation(Presence.UNKNOWN)
        try:
            data = _object(payload)
            commit = _object(data.get("commit"))
            name = _field(data, "name")
            revision = _field(commit, "id")
            reference = Reference(f"forgejo:{revision}")
        except ValueError:
            return Observation(Presence.UNKNOWN)
        if name != branch:
            return Observation(Presence.UNKNOWN)
        return Observation(Presence.FOUND, revision=reference)

    def change(self, repository: Reference, change: Reference) -> Observation:
        prefix = f"{repository.value}#"
        if not change.value.startswith(prefix) or not change.value[len(prefix) :].isdigit():
            raise ForgeConflict("change does not belong to repository")
        number = change.value[len(prefix) :]
        status, payload = self._read(f"/repos/{self._repo(repository)}/pulls/{number}")
        if status == 403:
            return Observation(Presence.INACCESSIBLE)
        if status != 200:
            return Observation(Presence.UNKNOWN)
        try:
            observation = self._change_observation(repository, _object(payload))
            return observation if observation.reference == change else Observation(Presence.UNKNOWN)
        except ValueError:
            return Observation(Presence.UNKNOWN)

    @staticmethod
    def _change_observation(repository: Reference, data: dict[str, object]) -> Observation:
        head = _object(data.get("head"))
        base = _object(data.get("base"))
        number = data.get("number")
        if not isinstance(number, int) or number <= 0:
            raise ValueError("invalid pull request number")
        return Observation(
            Presence.FOUND,
            Reference(f"{repository.value}#{number}"),
            base=Reference(f"forgejo:{_field(base, 'sha')}"),
            head=Reference(f"forgejo:{_field(head, 'sha')}"),
        )

    def _page(self, repository: Reference, path: str, cursor: str | None, *, subject: Reference | None = None) -> Page:
        page = 1 if cursor is None else int(cursor)
        if page < 1:
            raise ValueError("invalid page cursor")
        separator = "&" if "?" in path else "?"
        status, payload = self._read(
            f"/repos/{self._repo(repository)}/{path}{separator}page={page}&limit={self.page_size}"
        )
        if status != 200:
            return Page((), None, False)
        if not isinstance(payload, list):
            return Page((), None, False)
        items: list[Observation] = []
        try:
            for entry in cast(list[object], payload):
                data = _object(entry)
                if subject is not None:
                    check_id = data.get("id")
                    if not isinstance(check_id, int) or check_id <= 0:
                        raise ValueError("invalid check identity")
                    status_value = _field(data, "status")
                    result = {
                        "pending": CheckResult.PENDING,
                        "success": CheckResult.PASSED,
                        "failure": CheckResult.FAILED,
                        "error": CheckResult.ERROR,
                    }.get(status_value, CheckResult.UNKNOWN)
                    check = Observation(
                        Presence.FOUND,
                        Reference(f"{repository.value}@{check_id}"),
                        revision=Reference(f"forgejo:{_field(data, 'sha')}"),
                        check_context=_field(data, "context"),
                        check_result=result,
                    )
                    if check.revision != subject:
                        raise ValueError("check subject differs")
                    items.append(check)
                else:
                    items.append(self._change_observation(repository, data))
        except (ValueError, TypeError):
            return Page((), None, False)
        # A short page is only a pagination endpoint, never proof of remote absence.
        return Page(tuple(items), str(page + 1) if len(items) == self.page_size else None, len(items) < self.page_size)

    def changes(self, repository: Reference, cursor: str | None = None) -> Page:
        return self._page(repository, "pulls?state=all", cursor)

    def checks(self, repository: Reference, revision: Reference, cursor: str | None = None) -> Page:
        return self._page(
            repository, f"commits/{quote(self._revision(revision), safe='')}/statuses", cursor, subject=revision
        )

    def _authorized(self, effect: Effect) -> None:
        self._repo(effect.repository)
        if not self.authorize(effect):
            raise ForgeConflict("exact forge effect was not authorized")
        if effect.revision is None:
            raise ForgeConflict("revision required")
        self._revision(effect.revision)
        if effect.expected is not None:
            self._revision(effect.expected)
        if effect.base_revision is not None:
            self._revision(effect.base_revision)
        required = {"branch": "branch_create", "push": "conditional_push", "pr": "pr"}[effect.action]
        if required not in self.capabilities():
            raise UnsupportedForge(f"{required} unsupported")

    def apply(self, effect: Effect) -> Receipt:
        self._authorized(effect)
        if effect.revision is None:
            raise ForgeConflict("revision required")
        if effect.delivery_attempts > 1:
            return self.reconcile(effect)
        if effect.action in {"branch", "push"}:
            if "conditional_push" not in self.capabilities():
                raise UnsupportedForge("atomic expected-old push unavailable")
            if effect.action == "branch" and effect.expected is not None:
                raise ForgeConflict("new branch must expect absent target")
            if effect.action == "branch":
                if effect.base_branch is None or effect.base_revision is None:
                    raise ForgeConflict("source branch and revision required")
                source = self.branch(effect.repository, effect.base_branch)
                if source.presence is not Presence.FOUND:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation, reason="source not observable")
                if source.revision != effect.base_revision:
                    return Receipt(EffectStatus.STALE, effect.operation)
            try:
                status = self.git.compare_and_push(effect.repository, effect.branch, effect.expected, effect.revision)
            except OSError:
                status = EffectStatus.UNKNOWN
            if status not in {EffectStatus.ACCEPTED, EffectStatus.STALE, EffectStatus.UNKNOWN}:
                status = EffectStatus.UNKNOWN
            if (
                status is EffectStatus.ACCEPTED
                and effect.action == "branch"
                and effect.base_branch is not None
                and self.branch(effect.repository, effect.base_branch).revision != effect.base_revision
            ):
                return Receipt(EffectStatus.UNKNOWN, effect.operation, effect.revision, "source moved after effect")
            return Receipt(status, effect.operation, effect.revision if status is EffectStatus.ACCEPTED else None)
        if effect.base_branch is None or effect.base_revision is None:
            raise ForgeConflict("PR base required")
        base = self.branch(effect.repository, effect.base_branch)
        head = self.branch(effect.repository, effect.branch)
        if base.presence is not Presence.FOUND or head.presence is not Presence.FOUND:
            return Receipt(EffectStatus.UNKNOWN, effect.operation, reason="head/base not observable")
        if base.revision != effect.base_revision or head.revision != effect.revision:
            return Receipt(EffectStatus.STALE, effect.operation)
        marker = f"<!-- forge-effect:{effect.operation.effect_key}:{effect.operation.request_digest} -->"
        body = f"{effect.body or ''}\n\n{marker}"
        try:
            status, payload = self._request(
                "POST",
                f"/repos/{self._repo(effect.repository)}/pulls",
                {"head": effect.branch, "base": effect.base_branch, "title": effect.title or "", "body": body},
            )
        except OSError:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        if status == 403 or status == 422:
            return Receipt(EffectStatus.REJECTED, effect.operation)
        if status != 201:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        try:
            data = _object(payload)
            observed = self._change_observation(effect.repository, data)
            if (
                data.get("body") != body
                or data.get("title") != effect.title
                or _object(data.get("head")).get("ref") != effect.branch
                or _object(data.get("base")).get("ref") != effect.base_branch
                or observed.head != effect.revision
                or observed.base != effect.base_revision
            ):
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            if (
                self.branch(effect.repository, effect.base_branch).revision != effect.base_revision
                or self.branch(effect.repository, effect.branch).revision != effect.revision
            ):
                return Receipt(EffectStatus.UNKNOWN, effect.operation, observed.reference, "head/base moved")
            return Receipt(EffectStatus.ACCEPTED, effect.operation, observed.reference)
        except (ValueError, TypeError):
            return Receipt(EffectStatus.UNKNOWN, effect.operation)

    def reconcile(self, effect: Effect) -> Receipt:
        self._authorized(effect)
        if effect.action != "pr":
            observed = self.branch(effect.repository, effect.branch)
            if observed.presence is Presence.FOUND and observed.revision == effect.revision:
                return Receipt(EffectStatus.ACCEPTED, effect.operation, effect.revision)
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        marker = f"<!-- forge-effect:{effect.operation.effect_key}:{effect.operation.request_digest} -->"
        cursor: str | None = None
        while True:
            page = self.changes(effect.repository, cursor)
            if not page.complete and page.next_cursor is None:
                return Receipt(EffectStatus.UNKNOWN, effect.operation)
            for item in page.items:
                if item.reference is None:
                    continue
                status, payload = self._read(
                    f"/repos/{self._repo(effect.repository)}/pulls/{item.reference.value.rsplit('#', 1)[-1]}"
                )
                if status != 200:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                try:
                    data = _object(payload)
                except ValueError:
                    return Receipt(EffectStatus.UNKNOWN, effect.operation)
                expected_body = f"{effect.body or ''}\n\n{marker}"
                if marker in str(data.get("body", "")):
                    try:
                        head_ref = _object(data.get("head")).get("ref")
                        base_ref = _object(data.get("base")).get("ref")
                    except ValueError:
                        return Receipt(EffectStatus.UNKNOWN, effect.operation)
                    if (
                        data.get("body") != expected_body
                        or data.get("title") != effect.title
                        or (head_ref != effect.branch or base_ref != effect.base_branch)
                    ):
                        return Receipt(EffectStatus.UNKNOWN, effect.operation, item.reference, "conflicting key")
                    if item.head == effect.revision and item.base == effect.base_revision:
                        if effect.base_branch is None:
                            return Receipt(EffectStatus.UNKNOWN, effect.operation)
                        if (
                            self.branch(effect.repository, effect.base_branch).revision != effect.base_revision
                            or self.branch(effect.repository, effect.branch).revision != effect.revision
                        ):
                            return Receipt(EffectStatus.UNKNOWN, effect.operation, item.reference, "head/base moved")
                        return Receipt(EffectStatus.ACCEPTED, effect.operation, item.reference)
                    return Receipt(EffectStatus.UNKNOWN, effect.operation, item.reference, "changed PR subject")
            if page.next_cursor is None:
                return Receipt(EffectStatus.UNKNOWN, effect.operation, reason="absence is not authoritative")
            cursor = page.next_cursor
