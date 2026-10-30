# SPDX-License-Identifier: Apache-2.0
"""Bounded native Forgejo issue/dependency observations, never execution authority.

The v13 dependency endpoint pages before substituting permission-limited blockers.
An unidentified blocker is therefore an incomplete obligation, not an absent edge.
Repeated scans qualify an observation only; Forgejo supplies no snapshot token.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import cast
from urllib.parse import urlsplit

from creatidy_kernel.adapters.forgejo import HTTPTransport
from creatidy_kernel.core.intake import IssueSubject, digest, encode
from creatidy_kernel.core.ordinary import DependencyEvidence


class ScanStatus(StrEnum):
    OBSERVED = "stable_repeated_observation"
    INCOMPLETE = "incomplete"
    PERMISSION = "permission_limited"
    CHANGED = "changed_during_scan"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class NativeIssue:
    origin: str
    repository: str
    number: int
    title: str
    body: str
    state: str
    updated_at: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class IssueScan:
    scope: str
    status: ScanStatus
    issues: tuple[NativeIssue, ...]
    observed_at: int
    fresh_until: int
    pages: int
    source: str = "forgejo-api:v13.0.1"
    snapshot: bool = False

    @property
    def evidence_digest(self) -> str:
        # Time is observation metadata, not a provider revision or renewal of approval.
        return digest(encode({"scope": self.scope, "status": self.status, "issues": self.issues_payload()}))

    def issues_payload(self) -> list[dict[str, object]]:
        return [
            {
                "origin": item.origin,
                "repository": item.repository,
                "number": item.number,
                "content": item.content_digest,
                "updated_at": item.updated_at,
                "state": item.state,
            }
            for item in self.issues
        ]


class ForgejoDiscovery:
    """Exact configured repository, bounded pages and explicitly weaker freshness.

    No index search query, textual link, label or closed state becomes a DAG edge or
    accepted predecessor. Consumers must validate applicability separately. The
    version is a configured compatibility profile, not live-server attestation.
    """

    def __init__(
        self,
        http: HTTPTransport,
        clock: Callable[[], int],
        *,
        max_pages: int,
        freshness_seconds: int,
        page_size: int = 30,
    ) -> None:
        if any(type(value) is not int or not 0 < value <= 10000 for value in (max_pages, freshness_seconds, page_size)):
            raise ValueError("finite positive discovery bounds required")
        self.http, self.clock = http, clock
        self.binding = http.binding
        self.max_pages, self.freshness_seconds, self.page_size = max_pages, freshness_seconds, page_size

    def _issue(self, value: object, *, dependencies: bool) -> NativeIssue:
        data = cast(dict[str, object], value) if type(value) is dict else {}
        number = data.get("number")
        if (
            type(number) is not int
            or number < 1
            or data.get("pull_request") is not None
            or data.get("state") not in {"open", "closed"}
            or any(type(data.get(name)) is not str for name in ("title", "body", "updated_at", "html_url"))
            or not data.get("title")
            or not data.get("updated_at")
        ):
            raise ValueError("incomplete or permission-limited native issue")
        url = urlsplit(cast(str, data["html_url"]))
        if (
            url.scheme != "https"
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or f"{url.scheme}://{url.netloc}" != self.binding.origin
        ):
            raise ValueError("native issue origin unavailable")
        parts = url.path.split("/")
        if len(parts) != 5 or parts[0] or parts[3:] != ["issues", str(number)]:
            raise ValueError("native issue resource identity unavailable")
        repository = "/".join(parts[1:3])
        if not dependencies and repository != self.binding.repository:
            raise ValueError("issue enumeration escaped configured repository")
        # A confidential dependency can retain title/index while omitting body,
        # timestamps and URL. Require the whole public subject, never its title.
        return NativeIssue(
            self.binding.origin,
            repository,
            number,
            cast(str, data["title"]),
            cast(str, data["body"]),
            cast(str, data["state"]),
            cast(str, data["updated_at"]),
            digest(encode({name: data[name] for name in ("title", "body")})),
        )

    def _read(self, path: str) -> tuple[int, object]:
        if self.http.binding != self.binding:
            return 0, None
        try:
            return self.http.request("GET", path)
        except OSError:
            return 0, None

    def _scan(self, path: str, *, dependencies: bool) -> tuple[ScanStatus, tuple[NativeIssue, ...], int]:
        found: list[NativeIssue] = []
        identities: set[tuple[str, int]] = set()
        for page in range(1, self.max_pages + 1):
            separator = "&" if "?" in path else "?"
            status, raw = self._read(f"{path}{separator}page={page}&limit={self.page_size}")
            if status in {401, 403}:
                return ScanStatus.PERMISSION, tuple(found), page
            if status == 404:
                # Disabled dependency unit and concealed permission failure both
                # use 404. Neither establishes an empty dependency set.
                return ScanStatus.UNSUPPORTED if dependencies else ScanStatus.PERMISSION, tuple(found), page
            if status != 200 or type(raw) is not list:
                return ScanStatus.INCOMPLETE, tuple(found), page
            entries = cast(list[object], raw)
            if not entries:
                return ScanStatus.OBSERVED, tuple(found), page
            if len(entries) > self.page_size:
                return ScanStatus.INCOMPLETE, tuple(found), page
            for entry in entries:
                try:
                    issue = self._issue(entry, dependencies=dependencies)
                except (ValueError, TypeError):
                    return ScanStatus.PERMISSION if dependencies else ScanStatus.INCOMPLETE, tuple(found), page
                identity = (issue.repository, issue.number)
                if identity in identities:
                    return ScanStatus.CHANGED, tuple(found), page
                identities.add(identity)
                found.append(issue)
        return ScanStatus.INCOMPLETE, tuple(found), self.max_pages

    def _observe(self, path: str, *, dependencies: bool) -> IssueScan:
        opened_at = self.clock()
        if type(opened_at) is not int or opened_at < 0:
            raise ValueError("qualified discovery clock required")
        status, issues, pages = self._scan(path, dependencies=dependencies)
        if status is ScanStatus.OBSERVED:
            again, closing, closing_pages = self._scan(path, dependencies=dependencies)
            pages += closing_pages
            if again is not ScanStatus.OBSERVED:
                status = again
            elif issues != closing:
                status = ScanStatus.CHANGED
        closed_at = self.clock()
        # Do not make a slow scan fresh by stamping only its final response.
        if type(closed_at) is not int or not opened_at <= closed_at <= opened_at + self.freshness_seconds:
            status = ScanStatus.CHANGED
        return IssueScan(
            self.binding.origin + path, status, issues, opened_at, opened_at + self.freshness_seconds, pages
        )

    def discover(self) -> IssueScan:
        return self._observe(
            f"/repos/{self.binding.repository}/issues?state=open&type=issues&sort=oldest", dependencies=False
        )

    def dependencies(self, number: int) -> IssueScan:
        if type(number) is not int or number <= 0:
            raise ValueError("exact positive native issue number required")
        return self._observe(f"/repos/{self.binding.repository}/issues/{number}/dependencies", dependencies=True)

    def read_dependencies(self, subject: IssueSubject) -> DependencyEvidence:
        if (subject.origin, subject.repository) != (self.binding.origin, self.binding.repository):
            raise ValueError("dependency subject outside configured native repository")
        observed = self.dependencies(subject.issue)
        return DependencyEvidence(
            subject,
            tuple(sorted((item.origin, item.repository, item.number) for item in observed.issues)),
            observed.evidence_digest,
            observed.observed_at,
            observed.fresh_until,
            observed.status is ScanStatus.OBSERVED,
        )
