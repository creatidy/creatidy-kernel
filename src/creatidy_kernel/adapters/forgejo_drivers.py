# SPDX-License-Identifier: Apache-2.0
"""Version-1 family views over the unchanged Forgejo reference/effect facade."""

from collections.abc import Callable
from dataclasses import asdict

from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.project_drivers import native_fact as _fact
from creatidy_kernel.adapters.project_drivers import unavailable_fact as _missing
from creatidy_kernel.core.drivers import DriverBinding, DriverFact, DriverScan, DriverSubject, WorkObservation
from creatidy_kernel.core.forge import Effect, Presence, Receipt, Reference, UnsupportedForge


def _binding(forge: ForgejoForge, binding: DriverBinding, family: str) -> Reference:
    if (binding.family, binding.implementation, binding.api_version) != (family, "forgejo-facade", "1"):
        raise ValueError("unsupported Forgejo facade contract")
    if (binding.origin, binding.namespace) != (forge.binding.origin, forge.binding.repository):
        raise ValueError("Forgejo facade binding differs from retained transport")
    return Reference("forgejo:" + binding.namespace)


class ForgejoWorkManagement:
    def __init__(self, forge: ForgejoForge, binding: DriverBinding, clock: Callable[[], int]) -> None:
        self.repository = _binding(forge, binding, "work")
        self.forge, self.binding, self.clock = forge, binding, clock

    def work(self, key: str) -> WorkObservation:
        if not key.isascii() or not key.isdigit() or key.startswith("0") or len(key) > 18:
            raise ValueError("legacy Forgejo facade requires a native positive issue number")
        snapshot = self.forge.issue_snapshot(self.repository, int(key))
        if snapshot.presence is not Presence.FOUND:
            return WorkObservation(_missing(self.binding, "issue", key, self.clock(), 0), (), False)
        fact = _fact(
            self.binding,
            "issue",
            key,
            snapshot.updated_at,
            self.clock(),
            30,
            asdict(snapshot),
            qualification="legacy-native-issue;dependency-enumeration-unsupported;owner-edge-obligation-required",
        )
        return WorkObservation(fact, (), False)


class ForgejoSCM:
    def __init__(self, forge: ForgejoForge, binding: DriverBinding, clock: Callable[[], int]) -> None:
        self.repository_reference = _binding(forge, binding, "scm")
        self.forge, self.binding, self.clock = forge, binding, clock

    def repository(self) -> DriverFact:
        observed = self.forge.identity(self.repository_reference)
        return _fact(
            self.binding,
            "repository",
            self.binding.namespace,
            None,
            self.clock(),
            30,
            asdict(observed),
            presence=observed.presence,
            complete=observed.presence is Presence.FOUND,
        )

    def ref(self, branch: str) -> DriverFact:
        observed = self.forge.branch(self.repository_reference, branch)
        revision = None if observed.revision is None else observed.revision.value.removeprefix("forgejo:")
        return _fact(
            self.binding,
            "ref",
            branch,
            revision,
            self.clock(),
            30,
            asdict(observed),
            presence=observed.presence,
            complete=observed.presence is Presence.FOUND,
        )

    def change(self, key: str) -> DriverFact:
        observed = self.forge.change(self.repository_reference, Reference(self.repository_reference.value + "#" + key))
        revision = None if observed.head is None else observed.head.value.removeprefix("forgejo:")
        related = tuple(
            DriverSubject(
                self.binding, "commit", item.value.removeprefix("forgejo:"), item.value.removeprefix("forgejo:")
            )
            for item in (observed.head, observed.base)
            if item is not None
        )
        return _fact(
            self.binding,
            "change",
            key,
            revision,
            self.clock(),
            30,
            asdict(observed),
            presence=observed.presence,
            related=related,
            complete=observed.presence is Presence.FOUND,
        )

    def commit(self, revision: str) -> DriverFact:
        raise UnsupportedForge("legacy facade has no independent remote commit-object reader; use owned Git source")

    def reviews(self, key: str) -> DriverScan:
        now = self.clock()
        return DriverScan(
            (),
            False,
            False,
            self.binding.digest + ":reviews:" + key,
            now,
            now,
            "legacy-facade-native-review-enumeration-unsupported",
        )

    def apply_legacy(self, effect: Effect) -> Receipt:
        """No identity remapping, new authority or changed AGit request bytes."""
        return self.forge.apply(effect)

    def reconcile_legacy(self, effect: Effect, known_reference: Reference | None = None) -> Receipt:
        return self.forge.reconcile(effect, known_reference)


class ForgejoCI:
    def __init__(
        self,
        forge: ForgejoForge,
        binding: DriverBinding,
        source_binding: DriverBinding,
        clock: Callable[[], int],
        *,
        max_pages: int = 20,
    ) -> None:
        self.repository = _binding(forge, binding, "ci")
        if not 0 < max_pages <= 1000 or source_binding.family != "scm":
            raise ValueError("finite Forgejo CI scan and independent SCM binding required")
        self.forge, self.binding, self.source_binding, self.clock = forge, binding, source_binding, clock
        self.max_pages = max_pages

    def build(self, key: str, source: DriverSubject) -> DriverFact:
        if (
            source.binding != self.source_binding
            or source.kind != "commit"
            or source.key != source.revision
            or key != source.key
        ):
            raise ValueError("configured exact SCM commit required")
        now = self.clock()
        cursor = None
        scans: list[list[dict[str, object]]] = []
        for _ in range(2):
            items: list[dict[str, object]] = []
            cursor = None
            for _ in range(self.max_pages):
                page = self.forge.checks(self.repository, Reference("forgejo:" + source.key), cursor)
                items.extend(asdict(item) for item in page.items)
                if page.complete and page.next_cursor is None:
                    break
                if page.next_cursor is None or page.next_cursor == cursor:
                    return _missing(self.binding, "commit-statuses", key, now, 0)
                cursor = page.next_cursor
            else:
                return _missing(self.binding, "commit-statuses", key, now, 0)
            scans.append(items)
        if scans[0] != scans[1]:
            return _missing(self.binding, "commit-statuses", key, now, 0)
        return _fact(
            self.binding,
            "commit-statuses",
            key,
            None,
            now,
            30,
            scans[1],
            related=(source,),
            qualification="repeated-legacy-status-scan;native-revision-unavailable;native-checks-not-Kernel-acceptance",
        )
