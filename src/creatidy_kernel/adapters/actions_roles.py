# SPDX-License-Identifier: Apache-2.0
"""Explicit native Actions profiles, not CI discovery, a scheduler or worktree attestation."""

import base64
import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from creatidy_kernel.adapters.driver_http import DriverHTTP, list_value, native_number, object_value, text
from creatidy_kernel.adapters.forge_refs import oid
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.project_drivers import TrustedSCMRemote, native_fact, unavailable_fact
from creatidy_kernel.adapters.qualified_baseline import ConfiguredQualifiedBaseline, QualifiedCIProfile
from creatidy_kernel.core.drivers import DriverBinding, DriverFact, DriverSubject
from creatidy_kernel.core.forge import UnsupportedForge
from creatidy_kernel.core.intake import digest, encode, object_from


class _ReadUnavailable(ValueError):
    def __init__(self, status: int) -> None:
        super().__init__("native Actions read unavailable")
        self.status = status


@dataclass(frozen=True, slots=True)
class ActionsRecipe:
    """Trusted selection labels, not replacements for native job/attempt identities."""

    repository_id: int
    workflow_id: str
    workflow_path: str
    workflow_sha256: str
    ref: str
    recipes: tuple[str, ...]
    required_jobs: tuple[str, ...]
    required_steps: tuple[tuple[str, tuple[str, ...]], ...]
    decision_reference: str
    visibility_reference: str

    def __post_init__(self) -> None:
        values = (
            self.workflow_id,
            self.workflow_path,
            self.workflow_sha256,
            self.ref,
            self.decision_reference,
            self.visibility_reference,
        )
        if (
            any(type(value) is not str or len(value) > 4096 for value in values)
            or type(self.recipes) is not tuple
            or type(self.required_jobs) is not tuple
            or type(self.required_steps) is not tuple
            or any(type(value) is not str or len(value) > 4096 for value in (*self.recipes, *self.required_jobs))
            or any(
                type(pair) is not tuple
                or len(pair) != 2
                or type(pair[0]) is not str
                or type(pair[1]) is not tuple
                or any(type(name) is not str or len(name) > 256 for name in pair[1])
                for pair in self.required_steps
            )
        ):
            raise ValueError("immutable typed Actions recipe/profile fields required")
        native_number(self.repository_id)
        path = PurePosixPath(self.workflow_path)
        if (
            path.is_absolute()
            or path.as_posix() != self.workflow_path
            or len(path.parts) != 3
            or path.parts[0] not in {".github", ".forgejo", ".gitea"}
            or path.parts[1] != "workflows"
            or path.suffix not in {".yml", ".yaml"}
            or any(part in {".", ".."} for part in path.parts)
            or len(self.workflow_sha256) != 64
            or any(value not in "0123456789abcdef" for value in self.workflow_sha256)
            or not self.ref.startswith("refs/heads/")
            or not self.ref[11:]
            or ".." in self.ref
            or any(ord(c) < 33 for c in self.ref)
            or not self.recipes
            or len(self.recipes) > 128
            or len(self.required_jobs) > 64
            or any(len(steps) > 128 for _, steps in self.required_steps)
            or not self.required_jobs
            or len(set(self.recipes)) != len(self.recipes)
            or len(set(self.required_jobs)) != len(self.required_jobs)
            or len({job for job, _ in self.required_steps}) != len(self.required_steps)
            or any(
                job not in self.required_jobs or not steps or len(set(steps)) != len(steps)
                for job, steps in self.required_steps
            )
            or any(
                not value.strip()
                for value in (
                    self.workflow_id,
                    self.decision_reference,
                    self.visibility_reference,
                    *self.recipes,
                    *self.required_jobs,
                    *(name for _, steps in self.required_steps for name in steps),
                )
            )
        ):
            raise ValueError("finite trusted workflow/recipe/job/visibility profile required")


def _configuration(
    binding: DriverBinding,
    source: DriverBinding,
    recipe: ActionsRecipe,
    mapping: TrustedSCMRemote | None,
    *,
    native_scm: str,
) -> bytes:
    if source.family != "scm":
        raise ValueError("exact source binding required")
    native = (binding.origin, binding.tenant, binding.namespace) == (
        source.origin,
        source.tenant,
        source.namespace,
    ) and source.implementation == native_scm
    if (not native and mapping is None) or mapping is not None and mapping.binding != source:
        raise ValueError("separate Actions source requires explicit trusted remote mapping")
    return encode(
        {
            "binding": asdict(binding),
            "source": asdict(source),
            "recipe": asdict(recipe),
            "remote_mapping": None if mapping is None else asdict(mapping),
        }
    )


def _time(value: object, now: int) -> int:
    stamp = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("native timezone required")
    result = int(stamp.timestamp())
    if not 0 < result <= now:
        raise ValueError("native result time unavailable or future")
    return result


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _actor(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    actor = object_value(value)
    if (
        actor.get("id") is not None
        and type(actor["id"]) is not int
        or any(actor.get(key) is not None and type(actor[key]) is not str for key in ("login", "html_url"))
    ):
        raise ValueError("native actor provenance is partial or malformed")
    return {key: actor.get(key) for key in ("id", "login", "html_url")}


def _pages(
    read: Callable[[str], tuple[int, object]],
    path: str,
    params: dict[str, object],
    field: str,
    *,
    limit_name: str,
    limit: int,
    max_pages: int,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    total: int | None = None
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        code, raw = read(path + "?" + urlencode({**params, limit_name: limit, "page": page}))
        value = object_value(raw)
        count = value.get("total_count")
        if (
            code != 200
            or type(count) is not int
            or not 0 <= count <= max_pages * limit
            or total is not None
            and total != count
        ):
            raise ValueError("native enumeration incomplete, inaccessible or moving")
        total = count
        rows = list_value(value.get(field))
        if len(rows) > limit or not rows and len(result) < total:
            raise ValueError("native page hidden, capped or incomplete")
        for row in rows:
            item = object_value(row)
            identity = native_number(item.get("id"))
            if identity in seen:
                raise ValueError("duplicate native record")
            seen.add(identity)
            result.append(item)
        if len(result) == total:
            return result
        if len(result) > total:
            raise ValueError("native count differs")
    raise ValueError("native enumeration capped")


def _workflow(
    read: Callable[[str], tuple[int, object]], prefix: str, source: DriverSubject, recipe: ActionsRecipe
) -> dict[str, object]:
    code, raw = read(
        prefix + "/contents/" + quote(recipe.workflow_path, safe="/") + "?" + urlencode({"ref": source.key})
    )
    value = object_value(raw)
    size = value.get("size")
    if (
        code != 200
        or value.get("type") != "file"
        or value.get("path") != recipe.workflow_path
        or value.get("encoding") != "base64"
        or type(size) is not int
        or not 0 <= size <= 262144
    ):
        raise ValueError("exact bounded workflow file unavailable")
    encoded = "".join(text(value.get("content")).splitlines())
    content = base64.b64decode(encoded, validate=True)
    blob = text(value.get("sha"))
    framed = b"blob " + str(len(content)).encode() + b"\0" + content
    actual_blob = (
        hashlib.sha1(framed, usedforsecurity=False).hexdigest()
        if len(blob) == 40
        else hashlib.sha256(framed).hexdigest()
    )
    if len(content) != size or digest(content) != recipe.workflow_sha256 or blob != actual_blob:
        raise ValueError("workflow content/recipe/blob identity differs")
    return {"path": recipe.workflow_path, "blob": blob, "sha256": recipe.workflow_sha256, "size": len(content)}


def _result(fact: DriverFact, now: int, configuration: bytes) -> tuple[str, int] | None:
    value = object_from(fact.native)
    result, at = value.get("result"), value.get("result_at")
    if (
        value.get("configuration") != configuration.hex()
        or type(result) is not str
        or result not in {"passed", "failed"}
        or type(at) is not int
        or not 0 < at <= now
    ):
        return None
    return result, at


def _execution_identity(fact: DriverFact) -> bytes:
    value = object_from(fact.native)
    run = object_value(value["run"])
    # Forgejo v16 has per-job attempts, not a trustworthy aggregate run attempt.
    if fact.subject.binding.implementation == "github-actions":
        identity: object = {"run": run["id"], "attempt": run["run_attempt"]}
    else:
        identity = {"run": run["id"]}
    binding = fact.subject.binding
    return encode(
        {
            "provider": binding.implementation,
            "origin": binding.origin,
            "repository": binding.namespace,
            "execution": identity,
        }
    )


class GitHubActionsCI:
    def __init__(
        self,
        http: DriverHTTP,
        source_binding: DriverBinding,
        recipe: ActionsRecipe,
        clock: Callable[[], int],
        *,
        remote_mapping: TrustedSCMRemote | None = None,
        max_pages: int = 20,
        ttl: int = 30,
    ) -> None:
        self.binding, self.http, self.source_binding, self.recipe, self.clock = (
            http.binding,
            http,
            source_binding,
            recipe,
            clock,
        )
        if (
            (self.binding.family, self.binding.implementation, self.binding.api_version)
            != ("ci", "github-actions", "2026-03-10")
            or not recipe.workflow_id.isascii()
            or not recipe.workflow_id.isdigit()
            or str(int(recipe.workflow_id)) != recipe.workflow_id
            or len(self.binding.namespace.split("/")) != 2
            or any(not part or part in {".", ".."} for part in self.binding.namespace.split("/"))
            or recipe.workflow_path.split("/")[0] != ".github"
            or type(max_pages) is not int
            or not 0 < max_pages <= 1000
            or type(ttl) is not int
            or not 0 < ttl <= 3600
        ):
            raise ValueError("bounded GitHub Actions version/workflow profile required")
        native_number(int(recipe.workflow_id))
        self.mapping, self.max_pages, self.ttl = remote_mapping, max_pages, ttl
        self.prefix = "/repos/" + "/".join(quote(part, safe="") for part in self.binding.namespace.split("/"))
        self.configuration = _configuration(self.binding, source_binding, recipe, remote_mapping, native_scm="github")
        self.key, self.producer = (
            "actions-profile:" + digest(self.configuration),
            "github-actions-exact:v1:" + digest(self.configuration),
        )
        self._configured()

    def _configured(self) -> None:
        if (
            self.http.binding != self.binding
            or self.http.prefix != self.prefix
            or self.http.headers.get("X-GitHub-Api-Version") != "2026-03-10"
            or self.http.headers.get("Accept") != "application/vnd.github+json"
            or self.configuration
            != _configuration(self.binding, self.source_binding, self.recipe, self.mapping, native_scm="github")
        ):
            raise ValueError("GitHub Actions configured identity/profile changed")

    def _read(self, path: str) -> tuple[int, object]:
        self._configured()
        code, value, _ = self.http.request("GET", path)
        self._configured()
        if code != 200:
            raise _ReadUnavailable(code)
        return code, value

    def _run(self, raw: dict[str, object], source: DriverSubject) -> dict[str, object]:
        recipe = self.recipe
        run_id, attempt = native_number(raw.get("id")), native_number(raw.get("run_attempt"))
        repository, head_repository = object_value(raw.get("repository")), object_value(raw.get("head_repository"))
        for owner in (repository, head_repository):
            if (
                native_number(owner.get("id")) != str(recipe.repository_id)
                or owner.get("full_name") != self.binding.namespace
                or owner.get("url") != self.binding.origin + self.prefix
            ):
                raise ValueError("Actions repository/fork identity differs")
        path = text(raw.get("path"))
        allowed_paths = {
            recipe.workflow_path,
            self.binding.namespace + "/" + recipe.workflow_path + "@" + recipe.ref[11:],
            recipe.workflow_path + "@" + recipe.ref,
        }
        if (
            path not in allowed_paths
            or raw.get("head_sha") != source.key
            or native_number(raw.get("workflow_id")) != recipe.workflow_id
            or raw.get("event") != "push"
            or raw.get("head_branch") != recipe.ref[11:]
            or raw.get("referenced_workflows") not in (None, [])
        ):
            raise ValueError("exact supported push workflow/attempt/source unavailable")
        return {
            "id": int(run_id),
            "run_attempt": int(attempt),
            "workflow_id": raw["workflow_id"],
            "path": path,
            "head_sha": source.key,
            "head_branch": raw["head_branch"],
            "event": "push",
            "status": raw.get("status"),
            "conclusion": raw.get("conclusion"),
            "updated_at": raw.get("updated_at"),
            "repository": {name: repository[name] for name in ("id", "full_name", "url")},
            "head_repository": {name: head_repository[name] for name in ("id", "full_name", "url")},
            "actor": _actor(raw.get("actor")),
            "triggering_actor": _actor(raw.get("triggering_actor")),
        }

    def _observe(self, source: DriverSubject) -> dict[str, object]:
        recipe, prefix = self.recipe, self.prefix
        code, raw_repository = self._read(prefix)
        repository = object_value(raw_repository)
        if (
            code != 200
            or native_number(repository.get("id")) != str(recipe.repository_id)
            or repository.get("full_name") != self.binding.namespace
            or repository.get("url") != self.binding.origin + prefix
            or self.mapping is not None
            and repository.get("clone_url") != self.mapping.remote
        ):
            raise ValueError("native CI repository or trusted source mapping differs")
        runs = _pages(
            self._read,
            prefix + "/actions/runs",
            {"head_sha": source.key, "event": "push", "branch": recipe.ref[11:]},
            "workflow_runs",
            limit_name="per_page",
            limit=100,
            max_pages=self.max_pages,
        )
        selected = [row for row in runs if row.get("workflow_id") == int(recipe.workflow_id)]
        if len(selected) != 1:
            raise ValueError("missing or ambiguous exact execution")
        listing = self._run(selected[0], source)
        run_path = prefix + f"/actions/runs/{listing['id']}/attempts/{listing['run_attempt']}"
        code, raw = self._read(run_path)
        run = self._run(object_value(raw), source)
        if code != 200 or run != listing:
            raise ValueError("exact run attempt changed")
        rows = _pages(
            self._read, run_path + "/jobs", {}, "jobs", limit_name="per_page", limit=100, max_pages=self.max_pages
        )
        jobs: list[dict[str, object]] = []
        names: set[str] = set()
        for row in rows:
            identity = native_number(row.get("id"))
            name = text(row.get("name"))
            if (
                name in names
                or native_number(row.get("run_id")) != str(run["id"])
                or native_number(row.get("run_attempt")) != str(run["run_attempt"])
                or row.get("head_sha") != source.key
                or row.get("url") != self.binding.origin + prefix + "/actions/jobs/" + identity
                or row.get("run_url") != self.binding.origin + prefix + "/actions/runs/" + str(run["id"])
            ):
                raise ValueError("job/attempt/source identity missing, duplicated or changed")
            names.add(name)
            steps: list[dict[str, object]] = []
            if "steps" in row:
                seen_steps: set[int] = set()
                for value in list_value(row["steps"]):
                    step = object_value(value)
                    number = int(native_number(step.get("number")))
                    if number in seen_steps:
                        raise ValueError("duplicate native step")
                    seen_steps.add(number)
                    steps.append(
                        {
                            "name": text(step.get("name")),
                            "number": number,
                            "status": step.get("status"),
                            "conclusion": step.get("conclusion"),
                        }
                    )
            jobs.append(
                {
                    "id": int(identity),
                    "run_id": row["run_id"],
                    "run_attempt": row["run_attempt"],
                    "head_sha": source.key,
                    "name": name,
                    "status": row.get("status"),
                    "conclusion": row.get("conclusion"),
                    "completed_at": row.get("completed_at"),
                    "steps": steps,
                }
            )
        workflow = _workflow(self._read, prefix, source, recipe)
        code, closing = self._read(run_path)
        if code != 200 or self._run(object_value(closing), source) != run:
            raise ValueError("run attempt changed during collection")
        return {"run": run, "jobs": jobs, "workflow": workflow}

    def build(self, key: str, source: DriverSubject) -> DriverFact:
        if (
            key != self.key
            or source.binding != self.source_binding
            or source.kind != "commit"
            or source.key != source.revision
        ):
            raise ValueError("exact configured Actions selection/source required")
        oid(source.key)
        now = self.clock()
        try:
            first, second = self._observe(source), self._observe(source)
            self._configured()
            if first != second or not now <= self.clock() <= now + self.ttl:
                raise ValueError("native execution changed or expired")
            run, jobs = object_value(first["run"]), [object_value(value) for value in list_value(first["jobs"])]
            required = [job for job in jobs if job["name"] in self.recipe.required_jobs]
            complete = (
                len(required) == len(self.recipe.required_jobs)
                and run["status"] == "completed"
                and run["conclusion"] in {"success", "failure", "timed_out"}
            )
            complete = complete and all(
                job["status"] == "completed" and job["conclusion"] in {"success", "failure", "timed_out"}
                for job in required
            )
            failed = run["conclusion"] != "success" or any(job["conclusion"] != "success" for job in required)
            for job_name, step_names in self.recipe.required_steps:
                matching = [job for job in required if job["name"] == job_name]
                for name in step_names:
                    steps = [object_value(value) for value in list_value(matching[0]["steps"])] if matching else []
                    selected = [step for step in steps if step["name"] == name]
                    complete = (
                        complete
                        and len(selected) == 1
                        and selected[0]["status"] == "completed"
                        and selected[0]["conclusion"] in {"success", "failure"}
                    )
                    failed = failed or any(step["conclusion"] != "success" for step in selected)
            times = [_time(job["completed_at"], self.clock()) for job in required] if complete else []
            return native_fact(
                self.binding,
                "build",
                key,
                None,
                now,
                self.ttl,
                {
                    **first,
                    "configuration": self.configuration.hex(),
                    "result": ("failed" if failed else "passed") if complete else None,
                    "result_at": max(times) if times else None,
                },
                related=(source,),
                complete=complete,
                qualification="github-native-run-attempt-job-step-status;trusted-top-workflow-recipe;caller-visible-not-atomic;actual-worktree-and-external-actions-unproved",
            )
        except _ReadUnavailable as error:
            return unavailable_fact(self.binding, "build", key, now, error.status)
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return unavailable_fact(self.binding, "build", key, now, 0)

    def profile(self) -> QualifiedCIProfile:
        return QualifiedCIProfile(
            self,
            self.binding,
            self.producer,
            lambda fact, now: _result(fact, now, self.configuration),
            reusable_selection=DriverSubject(self.binding, "build", self.key, None),
            execution_identity=_execution_identity,
        )

    def baseline(self) -> ConfiguredQualifiedBaseline:
        return ConfiguredQualifiedBaseline(
            self.profile(), DriverSubject(self.binding, "build", self.key, None), self.recipe.recipes, self.clock
        )

    def validate_mutation(self, mutation: object) -> None:
        raise UnsupportedForge("Actions execution dispatch/rerun/cancel effects are not authorized by this read role")


class ForgejoActionsCI:
    """v16.0.0 job-attempt profile; no invented run-attempt or native step API."""

    def __init__(
        self,
        forge: ForgejoForge,
        binding: DriverBinding,
        source_binding: DriverBinding,
        recipe: ActionsRecipe,
        clock: Callable[[], int],
        *,
        remote_mapping: TrustedSCMRemote | None = None,
        max_pages: int = 20,
        max_jobs: int = 256,
        ttl: int = 30,
    ) -> None:
        if (
            (binding.family, binding.implementation, binding.api_version) != ("ci", "forgejo-actions", "16.0.0")
            or len(binding.namespace.split("/")) != 2
            or any(not part or part in {".", ".."} for part in binding.namespace.split("/"))
            or recipe.workflow_id != PurePosixPath(recipe.workflow_path).name
            or type(max_pages) is not int
            or not 0 < max_pages <= 1000
            or type(max_jobs) is not int
            or not 0 < max_jobs <= 1000
            or type(ttl) is not int
            or not 0 < ttl <= 3600
        ):
            raise ValueError("bounded exact Forgejo v16.0.0 Actions contract required")
        if recipe.required_steps:
            raise UnsupportedForge(
                "Forgejo v16 has no native structured step-result API; required step capability unavailable"
            )
        self.forge, self.binding, self.source_binding, self.recipe, self.clock = (
            forge,
            binding,
            source_binding,
            recipe,
            clock,
        )
        self.mapping, self.max_pages, self.max_jobs, self.ttl = remote_mapping, max_pages, max_jobs, ttl
        self.prefix = "/repos/" + binding.namespace
        self.configuration = _configuration(
            binding, source_binding, recipe, remote_mapping, native_scm="forgejo-facade"
        )
        self.key, self.producer = (
            "actions-profile:" + digest(self.configuration),
            "forgejo-actions-jobs-exact:v1:" + digest(self.configuration),
        )
        self._configured()

    def _configured(self) -> None:
        if (
            (self.forge.binding.origin, self.forge.binding.repository) != (self.binding.origin, self.binding.namespace)
            or self.forge.http.binding != self.forge.binding
            or self.forge.git.binding != self.forge.binding
            or self.configuration
            != _configuration(self.binding, self.source_binding, self.recipe, self.mapping, native_scm="forgejo-facade")
        ):
            raise ValueError("Forgejo Actions retained binding/profile changed")

    def _read(self, path: str) -> tuple[int, object]:
        self._configured()
        if not self.forge.http.supports_reads:
            raise UnsupportedForge("finite native Actions reads unavailable")
        result = self.forge.http.request("GET", path)
        self._configured()
        if result[0] != 200:
            raise _ReadUnavailable(result[0])
        return result

    def _directory(self, path: str, source: DriverSubject) -> dict[str, dict[str, object]]:
        code, raw = self._read(
            self.prefix + "/contents" + ("/" + path if path else "") + "?" + urlencode({"ref": source.key})
        )
        rows = list_value(raw)
        if code != 200 or len(rows) > 1024:
            raise ValueError("exact workflow directory enumeration unavailable")
        result: dict[str, dict[str, object]] = {}
        for value in rows:
            row = object_value(value)
            name = text(row.get("name"))
            if (
                name in result
                or row.get("path") != (path + "/" if path else "") + name
                or row.get("type") not in {"dir", "file", "symlink", "submodule"}
            ):
                raise ValueError("partial or ambiguous workflow directories")
            result[name] = row
        return result

    def _workflow(self, source: DriverSubject) -> dict[str, object]:
        root = self._directory("", source)
        for name in (".forgejo", ".gitea", ".github"):
            if name not in root:
                continue
            if root[name]["type"] != "dir":
                raise ValueError("workflow directory source unsupported")
            parent = self._directory(name, source)
            if "workflows" not in parent:
                continue
            if (
                parent["workflows"]["type"] != "dir"
                or self.recipe.workflow_path.rsplit("/", 1)[0] != name + "/workflows"
            ):
                raise ValueError("native workflow directory precedence differs")
            return _workflow(self._read, self.prefix, source, self.recipe)
        raise ValueError("no exact native workflow directory")

    def _run(self, raw: dict[str, object], source: DriverSubject) -> dict[str, object]:
        native_number(raw.get("id"))
        repository = object_value(raw.get("repository"))
        payload = object_from(text(raw.get("event_payload")).encode())
        payload_repository = object_value(payload.get("repository"))
        if (
            native_number(repository.get("id")) != str(self.recipe.repository_id)
            or repository.get("full_name") != self.binding.namespace
            or repository.get("html_url") != self.binding.origin + "/" + self.binding.namespace
            or raw.get("commit_sha") != source.key
            or raw.get("workflow_id") != self.recipe.workflow_id
            or raw.get("event") != "push"
            or raw.get("trigger_event") != "push"
            or raw.get("is_fork_pull_request") is not False
            or raw.get("need_approval") is not False
            or payload.get("after") != source.key
            or payload.get("ref") != self.recipe.ref
            or native_number(payload_repository.get("id")) != str(self.recipe.repository_id)
        ):
            raise ValueError("exact native push/source/workflow consistency unavailable")
        return {
            "id": raw["id"],
            "commit_sha": source.key,
            "workflow_id": raw["workflow_id"],
            "event": "push",
            "trigger_event": "push",
            "is_fork_pull_request": False,
            "need_approval": False,
            "status": raw.get("status"),
            "started": raw.get("started"),
            "stopped": raw.get("stopped"),
            "repository": {key: repository[key] for key in ("id", "full_name", "html_url")},
            "trigger_user": _actor(raw.get("trigger_user")),
            "event_payload": {
                "after": payload["after"],
                "ref": payload["ref"],
                "repository": {"id": payload_repository["id"]},
            },
        }

    def _observe(self, source: DriverSubject) -> dict[str, object]:
        code, raw = self._read(self.prefix)
        repository = object_value(raw)
        if (
            code != 200
            or native_number(repository.get("id")) != str(self.recipe.repository_id)
            or repository.get("full_name") != self.binding.namespace
            or repository.get("html_url") != self.binding.origin + "/" + self.binding.namespace
            or self.mapping is not None
            and repository.get("clone_url") != self.mapping.remote
        ):
            raise ValueError("CI repository or trusted remote mapping differs")
        owner_id = int(native_number(object_value(repository.get("owner")).get("id")))
        rows = _pages(
            self._read,
            self.prefix + "/actions/runs",
            {"head_sha": source.key, "workflow_id": self.recipe.workflow_id, "event": "push", "ref": self.recipe.ref},
            "workflow_runs",
            limit_name="limit",
            limit=1,
            max_pages=self.max_pages,
        )
        if len(rows) != 1:
            raise ValueError("missing or ambiguous configured native run")
        listing = self._run(rows[0], source)
        run_path = self.prefix + "/actions/runs/" + str(listing["id"])
        code, raw = self._read(run_path)
        run = self._run(object_value(raw), source)
        if code != 200 or run != listing:
            raise ValueError("native run changed")
        code, raw = self._read(run_path + "/jobs")
        values = list_value(raw)
        if code != 200 or len(values) > self.max_jobs:
            raise ValueError("complete bounded native job inventory unavailable")
        jobs: list[dict[str, object]] = []
        ids: set[str] = set()
        names: set[str] = set()
        for value in values:
            job = object_value(value)
            identity, name = native_number(job.get("id")), text(job.get("name"))
            attempt, task = job.get("attempt"), job.get("task_id")
            if (
                identity in ids
                or name in names
                or native_number(job.get("run_id")) != str(run["id"])
                or native_number(job.get("repo_id")) != str(self.recipe.repository_id)
                or native_number(job.get("owner_id")) != str(owner_id)
                or type(attempt) is not int
                or not 0 <= attempt < 10**18
                or type(task) is not int
                or not 0 <= task < 10**18
                or type(job.get("handle")) is not str
                or job.get("status")
                not in {"unknown", "waiting", "running", "blocked", "success", "failure", "cancelled", "skipped"}
            ):
                raise ValueError("native job membership/attempt identity invalid or ambiguous")
            ids.add(identity)
            names.add(name)
            needs, runs_on = list_value(job.get("needs")), list_value(job.get("runs_on"))
            if any(type(item) is not str for item in (*needs, *runs_on)):
                raise ValueError("native job requirements partial")
            jobs.append(
                {
                    key: job[key]
                    for key in (
                        "id",
                        "run_id",
                        "repo_id",
                        "owner_id",
                        "name",
                        "attempt",
                        "handle",
                        "task_id",
                        "status",
                        "needs",
                        "runs_on",
                    )
                }
            )
        workflow = self._workflow(source)
        code, closing = self._read(run_path)
        if code != 200 or self._run(object_value(closing), source) != run:
            raise ValueError("run changed during job observation")
        return {"run": run, "jobs": jobs, "workflow": workflow}

    def build(self, key: str, source: DriverSubject) -> DriverFact:
        if (
            key != self.key
            or source.binding != self.source_binding
            or source.kind != "commit"
            or source.key != source.revision
        ):
            raise ValueError("exact configured Actions selection/source required")
        oid(source.key)
        now = self.clock()
        try:
            first, second = self._observe(source), self._observe(source)
            self._configured()
            if first != second or not now <= self.clock() <= now + self.ttl:
                raise ValueError("native run/job attempts changed or expired")
            run, jobs = object_value(first["run"]), [object_value(value) for value in list_value(first["jobs"])]
            required = [job for job in jobs if job["name"] in self.recipe.required_jobs]
            complete = len(required) == len(self.recipe.required_jobs) and run["status"] in {"success", "failure"}
            complete = complete and all(
                job["status"] in {"success", "failure"}
                and _positive(job["attempt"])
                and _positive(job["task_id"])
                and bool(job["handle"])
                for job in required
            )
            result_at = _time(run["stopped"], self.clock()) if complete else None
            failed = run["status"] != "success" or any(job["status"] != "success" for job in required)
            return native_fact(
                self.binding,
                "build",
                key,
                None,
                now,
                self.ttl,
                {
                    **first,
                    "configuration": self.configuration.hex(),
                    "result": ("failed" if failed else "passed") if complete else None,
                    "result_at": result_at,
                },
                related=(source,),
                complete=complete,
                qualification=(
                    "forgejo-v16-native-run-job-attempts;trusted-top-workflow-recipe;caller-visible-not-atomic;"
                    "workflow-job-key-unavailable;structured-steps-actual-worktree-and-external-actions-unproved;"
                    "live-version-unattested"
                ),
            )
        except _ReadUnavailable as error:
            return unavailable_fact(self.binding, "build", key, now, error.status)
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return unavailable_fact(self.binding, "build", key, now, 0)

    def profile(self) -> QualifiedCIProfile:
        return QualifiedCIProfile(
            self,
            self.binding,
            self.producer,
            lambda fact, now: _result(fact, now, self.configuration),
            reusable_selection=DriverSubject(self.binding, "build", self.key, None),
            execution_identity=_execution_identity,
        )

    def baseline(self) -> ConfiguredQualifiedBaseline:
        return ConfiguredQualifiedBaseline(
            self.profile(), DriverSubject(self.binding, "build", self.key, None), self.recipe.recipes, self.clock
        )

    def validate_mutation(self, mutation: object) -> None:
        raise UnsupportedForge(
            "Actions dispatch/rerun/cancel effects and native step attestation are not supplied by this read role"
        )
