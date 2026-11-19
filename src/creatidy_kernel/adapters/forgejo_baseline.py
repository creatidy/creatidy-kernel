# SPDX-License-Identifier: Apache-2.0
"""Native status evidence for a pinned baseline, never Kernel task acceptance.

Forgejo v13.0.1 modules/structs/status.go exposes status/context/id/created_at.
The commit is bound by the exact-SHA native endpoint, not an invented status SHA.
"""

from datetime import datetime
from typing import cast

from creatidy_kernel.adapters.forgejo import HTTPTransport
from creatidy_kernel.core.intake import BaselineSnapshot, IssueSubject, digest, encode


class ForgejoBaseline:
    def __init__(
        self, http: HTTPTransport, recipes: tuple[str, ...], contexts: frozenset[str], *, max_pages: int = 8
    ) -> None:
        if not recipes or not contexts or type(max_pages) is not int or max_pages <= 0:
            raise ValueError("finite trusted native baseline profile required")
        self.http, self.recipes, self.contexts, self.max_pages = http, recipes, contexts, max_pages
        self.binding = http.binding

    def _read(self, subject: IssueSubject) -> list[dict[str, object]] | None:
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
                return result
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

    def read_baseline(self, subject: IssueSubject, recipe_digest: str) -> BaselineSnapshot | None:
        if (
            (subject.origin, subject.repository) != (self.binding.origin, self.binding.repository)
            or self.http.binding != self.binding
            or recipe_digest != digest(encode(self.recipes))
        ):
            return None
        try:
            rows = self._read(subject)
            if rows is None or rows != self._read(subject):
                return None
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
            return BaselineSnapshot(
                subject,
                recipe_digest,
                "forgejo-status-baseline:v13.0.1",
                "forgejo-statuses:" + digest(encode(rows)),
                result,
                observed,
            )
        except (OSError, ValueError, OverflowError):
            return None
