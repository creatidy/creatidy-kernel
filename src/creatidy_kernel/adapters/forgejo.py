# SPDX-License-Identifier: Apache-2.0
"""Forgejo reference adapter with injected HTTP and conditional Git transport.

The caller must durably claim the Operation and supply a trusted authorization
predicate. Neither HTTP success nor a locally cached key establishes acceptance.
"""

from collections.abc import Callable, Mapping
from typing import Protocol, cast
from urllib.parse import quote

from creatidy_kernel.adapters.forge_refs import (
    AGitPush,
    ForgeBinding,
    oid,
    positive_id,
    pr_payload,
    repository_path,
    valid_branch,
)
from creatidy_kernel.adapters.forge_refs import number as valid_number
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
    binding: ForgeBinding
    max_bytes: int

    @property
    def supports_reads(self) -> bool: ...

    @property
    def supports_pr(self) -> bool: ...

    def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]: ...

    def authoritative_absence(self, path: str) -> bool: ...


class GitTransport(Protocol):
    """Conditional branch updates and exact-SHA AGit PR creation."""

    supports_conditional_push: bool
    supports_agit: bool
    binding: ForgeBinding

    def compare_and_push(
        self, repository: Reference, branch: str, expected: Reference | None, revision: Reference
    ) -> EffectStatus: ...

    def create_agit_pr(
        self, repository: Reference, base: str, topic: str, revision: Reference, title: str, description: str
    ) -> AGitPush: ...


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
        if http.binding != git.binding:
            raise ForgeConflict("HTTP and Git forge bindings differ")
        self.binding = http.binding
        self.http = http
        self.git = git
        self.authorize = authorize
        self.page_size = page_size

    def capabilities(self) -> frozenset[str]:
        capabilities: set[str] = set()
        if self.http.supports_reads:
            capabilities.update({"identity", "branch", "change", "checks"})
        if self.git.supports_conditional_push:
            capabilities.add("conditional_push")
            if self.http.supports_reads:
                capabilities.add("branch_create")
        if self.http.supports_reads and self.http.supports_pr and self.git.supports_agit:
            capabilities.add("pr")
        return frozenset(capabilities)

    def _repo(self, reference: Reference) -> str:
        path = repository_path(reference)
        if path != self.binding.repository:
            raise ForgeConflict("repository outside configured forge binding")
        return path

    @staticmethod
    def _revision(reference: Reference) -> str:
        if not reference.value.startswith("forgejo:"):
            raise ForgeConflict("wrong revision provider")
        value = reference.value.removeprefix("forgejo:")
        try:
            return oid(value)
        except ValueError as error:
            raise ForgeConflict("revision must be a full Git object ID") from error

    def _request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
        return self.http.request(method, path, body)

    def _read(self, path: str) -> tuple[int, object]:
        try:
            return self._request("GET", path)
        except OSError:
            return 0, None

    def identity(self, repository: Reference) -> Observation:
        self._require_reads()
        path = f"/repos/{self._repo(repository)}"
        status, payload = self._read(path)
        if status in {401, 403}:
            return Observation(Presence.INACCESSIBLE)
        if status == 404 and self.http.authoritative_absence(path):
            return Observation(Presence.ABSENT)
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
        self._require_reads()
        valid_branch(branch)
        path = f"/repos/{self._repo(repository)}/branches/{quote(branch, safe='')}"
        status, payload = self._read(path)
        if status in {401, 403}:
            return Observation(Presence.INACCESSIBLE)
        if status == 404 and self.http.authoritative_absence(path):
            return Observation(Presence.ABSENT)
        if status != 200:
            return Observation(Presence.UNKNOWN)
        try:
            data = _object(payload)
            commit = _object(data.get("commit"))
            name = _field(data, "name")
            revision = oid(_field(commit, "id"))
            reference = Reference(f"forgejo:{revision}")
        except ValueError:
            return Observation(Presence.UNKNOWN)
        if name != branch:
            return Observation(Presence.UNKNOWN)
        return Observation(Presence.FOUND, revision=reference)

    def change(self, repository: Reference, change: Reference) -> Observation:
        self._require_reads()
        prefix = f"{repository.value}#"
        suffix = change.value.removeprefix(prefix)
        if not change.value.startswith(prefix) or not valid_number(suffix):
            raise ForgeConflict("change does not belong to repository")
        number = suffix
        path = f"/repos/{self._repo(repository)}/pulls/{number}"
        status, payload = self._read(path)
        if status in {401, 403}:
            return Observation(Presence.INACCESSIBLE)
        if status == 404 and self.http.authoritative_absence(path):
            return Observation(Presence.ABSENT)
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
        expected_repo = repository.value.removeprefix("forgejo:")
        if (
            _field(_object(head.get("repo")), "full_name") != expected_repo
            or _field(_object(base.get("repo")), "full_name") != expected_repo
        ):
            raise ValueError("pull request repository differs")
        number = data.get("number")
        if not positive_id(number):
            raise ValueError("invalid pull request number")
        return Observation(
            Presence.FOUND,
            Reference(f"{repository.value}#{number}"),
            base=Reference(f"forgejo:{oid(_field(base, 'sha'))}"),
            head=Reference(f"forgejo:{oid(_field(head, 'sha'))}"),
        )

    def _page(self, repository: Reference, path: str, cursor: str | None, *, subject: Reference | None = None) -> Page:
        self._require_reads()
        if cursor is not None and not valid_number(cursor):
            return Page((), None, False)
        try:
            page = 1 if cursor is None else int(cursor)
        except (TypeError, ValueError):
            return Page((), None, False)
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
                    if not positive_id(check_id):
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
                        revision=subject,
                        check_context=_field(data, "context"),
                        check_result=result,
                    )
                    items.append(check)
                else:
                    items.append(self._change_observation(repository, data))
        except (ValueError, TypeError):
            return Page((), None, False)
        # Servers may cap below the requested limit. Only an empty page ends this scan.
        return Page(tuple(items), str(page + 1) if items else None, not items)

    def changes(self, repository: Reference, cursor: str | None = None) -> Page:
        return self._page(repository, "pulls?state=all", cursor)

    def checks(self, repository: Reference, revision: Reference, cursor: str | None = None) -> Page:
        return self._page(
            repository, f"commits/{quote(self._revision(revision), safe='')}/statuses", cursor, subject=revision
        )

    def _require_reads(self) -> None:
        if not self.http.supports_reads:
            raise UnsupportedForge("forge reads unsupported")

    def _authorized(self, effect: Effect) -> None:
        self._repo(effect.repository)
        valid_branch(effect.branch)
        if effect.base_branch is not None:
            valid_branch(effect.base_branch)
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
        if effect.base_branch is None:
            raise ForgeConflict("PR base branch required")
        try:
            topic, _, description = pr_payload(effect, self.http.max_bytes)
        except ForgeConflict:
            raise
        except ValueError:
            return Receipt(EffectStatus.REJECTED, effect.operation, reason="local request limit")
        base = self.branch(effect.repository, effect.base_branch)
        if base.presence is not Presence.FOUND:
            return Receipt(EffectStatus.UNKNOWN, effect.operation, reason="base not observable")
        try:
            created = self.git.create_agit_pr(
                effect.repository, effect.base_branch, topic, effect.revision, effect.title or "", description
            )
        except OSError:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        if created.status is not EffectStatus.ACCEPTED or created.reference is None:
            return Receipt(
                created.status if created.status is not EffectStatus.ACCEPTED else EffectStatus.UNKNOWN,
                effect.operation,
                reason=created.reason,
            )
        receipt = self.reconcile(effect, created.reference)
        if receipt.status is EffectStatus.UNKNOWN:
            return Receipt(EffectStatus.UNKNOWN, effect.operation, created.reference, reason=receipt.reason)
        return receipt

    def reconcile(self, effect: Effect, known_reference: Reference | None = None) -> Receipt:
        self._authorized(effect)
        if effect.action != "pr":
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        if known_reference is None:
            return Receipt(EffectStatus.UNKNOWN, effect.operation)
        try:
            _, body, _ = pr_payload(effect, self.http.max_bytes)
        except ForgeConflict:
            raise
        except ValueError:
            return Receipt(EffectStatus.REJECTED, effect.operation, reason="local request limit")
        prefix = f"{effect.repository.value}#"
        suffix = known_reference.value.removeprefix(prefix)
        if not known_reference.value.startswith(prefix) or not valid_number(suffix):
            raise ForgeConflict("change does not belong to repository")
        status, payload = self._read(f"/repos/{self._repo(effect.repository)}/pulls/{suffix}")
        if status != 200:
            return Receipt(EffectStatus.UNKNOWN, effect.operation, known_reference)
        try:
            data = _object(payload)
            observed = self._change_observation(effect.repository, data)
            if (
                _object(data.get("head")).get("ref") != f"refs/pull/{suffix}/head"
                or _object(data.get("base")).get("ref") != effect.base_branch
            ):
                raise ForgeConflict("PR head or base identity differs from authorized effect")
            if observed.reference != known_reference or data.get("title") != effect.title or data.get("body") != body:
                return Receipt(EffectStatus.UNKNOWN, effect.operation, known_reference)
            if observed.head != effect.revision:
                return Receipt(EffectStatus.STALE, effect.operation, known_reference, "PR head moved", observed.base)
        except ForgeConflict:
            raise
        except (ValueError, TypeError):
            return Receipt(EffectStatus.UNKNOWN, effect.operation, known_reference)
        return Receipt(EffectStatus.ACCEPTED, effect.operation, known_reference, observed_base=observed.base)
