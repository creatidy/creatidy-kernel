# SPDX-License-Identifier: Apache-2.0
"""GitHub 2026-03-10 visible issue dependencies, never issue-prose authority."""

import re
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import quote, unquote

from creatidy_kernel.adapters.driver_http import DriverHTTP, list_value, native_number, object_value, text
from creatidy_kernel.adapters.project_drivers import GitHubSCM, native_fact, unavailable_fact
from creatidy_kernel.core.drivers import DriverSubject, NativeDependency, WorkObservation, driver_digest
from creatidy_kernel.core.forge import UnsupportedForge


class GitHubIssues:
    def __init__(self, http: DriverHTTP, clock: Callable[[], int], *, max_pages: int = 20, ttl: int = 30) -> None:
        self.binding = http.binding
        if (
            (self.binding.family, self.binding.implementation, self.binding.api_version)
            != ("work", "github-issues", "2026-03-10")
            or len(self.binding.namespace.split("/")) != 2
            or any(not part or part in {".", ".."} for part in self.binding.namespace.split("/"))
            or type(max_pages) is not int
            or not 0 < max_pages <= 1000
            or type(ttl) is not int
            or not 0 < ttl <= 3600
        ):
            raise ValueError("bounded version-qualified GitHub Issues contract required")
        self.prefix = "/repos/" + "/".join(quote(part, safe="") for part in self.binding.namespace.split("/"))
        self.http, self.clock, self.max_pages, self.ttl = http, clock, max_pages, ttl
        self._configured()

    def _configured(self) -> None:
        if (
            self.http.binding != self.binding
            or self.http.prefix != self.prefix
            or self.http.headers.get("X-GitHub-Api-Version") != "2026-03-10"
            or self.http.headers.get("Accept") != "application/vnd.github+json"
        ):
            raise ValueError("GitHub Issues binding, namespace or version changed")

    def _read(self, path: str) -> tuple[int, object, dict[str, str]]:
        self._configured()
        result = self.http.request("GET", path)
        self._configured()
        return result

    def _subject(self, value: dict[str, object]) -> DriverSubject:
        repository_url = text(value.get("repository_url"))
        root = self.binding.origin + "/repos/"
        if not repository_url.startswith(root):
            raise ValueError("native dependency repository origin differs")
        parts = repository_url[len(root) :].split("/")
        names = [unquote(part) for part in parts]
        if (
            len(names) != 2
            or any(not part or part in {".", ".."} or "/" in part or "\\" in part for part in names)
            or root + "/".join(quote(part, safe="") for part in names) != repository_url
        ):
            raise ValueError("canonical native dependency repository required")
        number = native_number(value.get("number"))
        if (
            value.get("url") != repository_url + "/issues/" + number
            or value.get("pull_request") is not None
            or value.get("state") not in {"open", "closed"}
        ):
            raise ValueError("exact native issue identity required")
        native_number(value.get("id"))
        text(value.get("node_id"))
        return DriverSubject(
            replace(self.binding, namespace="/".join(names)), "issue", number, text(value.get("updated_at"))
        )

    def work(self, key: str) -> WorkObservation:
        GitHubSCM.number(key)
        now = self.clock()
        try:
            status, raw, _ = self._read(self.prefix + "/issues/" + key)
            if status != 200:
                return WorkObservation(unavailable_fact(self.binding, "issue", key, now, status), (), False)
            issue = object_value(raw)
            selected = self._subject(issue)
            if selected.binding != self.binding or selected.key != key:
                raise ValueError("selected native issue moved or differs")
            scans: list[list[dict[str, object]]] = []
            scan_complete = True
            qualification = (
                "repeated-visible-blocked-by;not-snapshot;"
                "hidden-edge-visibility-unproved;owner-edge-obligation-required"
            )
            for _ in range(2):
                items: list[dict[str, object]] = []
                seen: set[str] = set()
                seen_subjects: set[tuple[str, str]] = set()
                for page in range(1, self.max_pages + 1):
                    code, values, headers = self._read(
                        self.prefix + f"/issues/{key}/dependencies/blocked_by?per_page=100&page={page}"
                    )
                    if code != 200:
                        scan_complete = False
                        qualification = "dependency-permission-partial-or-unsupported;owner-edge-obligation-required"
                        break
                    rows = list_value(values)
                    if len(rows) > 100:
                        raise ValueError("native dependency limit ignored")
                    if not rows:
                        if re.search(r'rel\s*=\s*"?next\b', headers.get("link", ""), re.I):
                            scan_complete = False
                        break
                    for value in rows:
                        row = object_value(value)
                        target = self._subject(row)
                        identity = native_number(row.get("id"))
                        target_identity = (target.binding.digest, target.key)
                        if identity in seen or target_identity in seen_subjects:
                            raise ValueError("duplicate native dependency")
                        seen.add(identity)
                        seen_subjects.add(target_identity)
                        items.append(row)
                else:
                    scan_complete = False
                scans.append(items)
            code, closing, _ = self._read(self.prefix + "/issues/" + key)
            stable = code == 200 and issue == closing and scans[0] == scans[1] and now <= self.clock() <= now + self.ttl
            if not stable:
                scan_complete = False
                qualification = "moving-stale-or-unavailable-dependency-cut;owner-edge-obligation-required"
            edges = tuple(
                NativeDependency(
                    "derived-native-edge:" + driver_digest([selected.reference, row["id"], row["node_id"]]),
                    "github-2026-03-10-blocked-by",
                    "blocked_by",
                    "outward",
                    self._subject(row),
                )
                for row in scans[-1]
            )
            fact = native_fact(
                self.binding,
                "issue",
                key,
                selected.revision,
                now,
                self.ttl,
                {
                    "issue": issue,
                    "dependencies": scans[-1],
                    "visible_scan_complete": scan_complete,
                    "visibility_scope": "caller-visible-native-blocked-by-only",
                    "snapshot": False,
                },
                complete=code == 200 and issue == closing,
                qualification=qualification,
            )
            # GitHub's public contract does not guarantee that invisible edges
            # appear in this list. Visible exhaustion is not global completeness.
            return WorkObservation(fact, edges, False)
        except (OSError, ValueError, TypeError, OverflowError):
            return WorkObservation(unavailable_fact(self.binding, "issue", key, now, 0), (), False)

    def validate_mutation(self, mutation: object) -> None:
        raise UnsupportedForge("this version-qualified GitHub Issues role is read-only; native CAS is unavailable")
