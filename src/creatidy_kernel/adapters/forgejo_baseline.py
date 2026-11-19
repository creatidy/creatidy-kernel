# SPDX-License-Identifier: Apache-2.0
"""Native status evidence for a pinned baseline, never Kernel task acceptance.

Forgejo v13.0.1 modules/structs/status.go exposes status/context/id/created_at.
The commit is bound by the exact-SHA native endpoint, not an invented status SHA.
"""

from datetime import datetime
from typing import cast

from creatidy_kernel.adapters.forgejo import HTTPTransport
from creatidy_kernel.core.intake import BaselineSnapshot, IntakeEvidence, IssueSubject, digest, encode


class ForgejoBaseline:
    _PRODUCER = "forgejo-status-baseline:v13.0.1"
    _DISCOVERY_HISTORY = 128

    def __init__(
        self, http: HTTPTransport, recipes: tuple[str, ...], contexts: frozenset[str], *, max_pages: int = 8
    ) -> None:
        if not recipes or not contexts or type(max_pages) is not int or max_pages <= 0:
            raise ValueError("finite trusted native baseline profile required")
        self.http, self.recipes, self.contexts, self.max_pages = http, recipes, contexts, max_pages
        self.binding = http.binding
        self._observed_profiles: dict[tuple[IssueSubject, str, str, str], tuple[frozenset[str], int]] = {}

    def _read(self, subject: IssueSubject) -> tuple[list[dict[str, object]], int] | None:
        result: list[dict[str, object]] = []
        ids: set[int] = set()
        for page in range(1, self.max_pages + 1):
            status, value = self.http.request(
                "GET", f"/repos/{subject.repository}/commits/{subject.base}/statuses?page={page}&limit=30"
            )
            if status != 200 or type(value) is not list:
                return None
            rows = cast(list[object], value)
            if not rows:
                return result, page
            if len(rows) > 30:
                return None
            for raw in rows:
                if type(raw) is not dict:
                    return None
                row = cast(dict[str, object], raw)
                key = row.get("id")
                if (
                    type(key) is not int
                    or key <= 0
                    or key in ids
                    or any(type(row.get(field)) is not str for field in ("context", "status", "created_at"))
                ):
                    return None
                ids.add(key)
                result.append({field: row[field] for field in ("id", "context", "status", "created_at")})
        return None

    def _supports(self, subject: IssueSubject, recipe_digest: str) -> bool:
        return (
            (subject.origin, subject.repository) == (self.binding.origin, self.binding.repository)
            and self.http.binding == self.binding
            and recipe_digest == digest(encode(self.recipes))
        )

    def supports_baseline(self, evidence: IntakeEvidence) -> bool:
        profile = self._observed_profiles.get(
            (evidence.subject, evidence.recipe_digest, evidence.baseline_reference, evidence.baseline_result)
        )
        return (
            self.http.supports_reads
            and evidence.baseline_producer == self._PRODUCER
            and self._supports(evidence.subject, evidence.recipe_digest)
            and profile is not None
            and profile[0] == self.contexts
            and profile[1] <= self.max_pages
        )

    def read_baseline(self, subject: IssueSubject, recipe_digest: str) -> BaselineSnapshot | None:
        if not self._supports(subject, recipe_digest):
            return None
        try:
            scanned = self._read(subject)
            if scanned is None:
                return None
            closing = self._read(subject)
            if closing is None or scanned[0] != closing[0]:
                return None
            rows, pages = scanned
            selected: dict[str, dict[str, object]] = {}
            for row in rows:
                context = cast(str, row["context"])
                if context in self.contexts and context not in selected:
                    selected[context] = row
            if frozenset(selected) != self.contexts:
                return None
            if any(row["status"] not in {"success", "failure", "error"} for row in selected.values()):
                # Pending and unrecognised native states are unresolved, not completed failures.
                return None
            timestamps = [datetime.fromisoformat(cast(str, row["created_at"])) for row in selected.values()]
            if any(value.tzinfo is None for value in timestamps):
                return None
            observed = min(int(value.timestamp()) for value in timestamps)
            result = "passed" if all(row["status"] == "success" for row in selected.values()) else "failed"
            reference = "forgejo-statuses:" + digest(encode(rows))
            # The retained reference hashes rows, but does not identify the status selection or scan bound.
            key = (subject, recipe_digest, reference, result)
            self._observed_profiles.pop(key, None)
            self._observed_profiles[key] = (self.contexts, max(pages, closing[1]))
            if len(self._observed_profiles) > self._DISCOVERY_HISTORY:
                del self._observed_profiles[next(iter(self._observed_profiles))]
            return BaselineSnapshot(subject, recipe_digest, self._PRODUCER, reference, result, observed)
        except (OSError, ValueError, OverflowError):
            return None
