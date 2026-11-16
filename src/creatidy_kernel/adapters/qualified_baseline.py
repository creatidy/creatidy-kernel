# SPDX-License-Identifier: Apache-2.0
"""Receive a host-configured Jenkins baseline without executing repository code."""

from collections.abc import Callable

from creatidy_kernel.core.drivers import DriverBinding, DriverFact, DriverSubject
from creatidy_kernel.core.forge import Presence
from creatidy_kernel.core.intake import digest, encode, object_from
from creatidy_kernel.core.ordinary_qualified import QualifiedBaseline
from creatidy_kernel.ports.drivers import ContinuousIntegration


class QualifiedCIProfile:
    """Trusted interpretation of one configured CI contract, not provider discovery.

    The interpreter must verify native terminal state, age and the configured
    assurance. The shared consumer independently checks exact binding and source.
    """

    def __init__(
        self,
        ci: ContinuousIntegration,
        binding: DriverBinding,
        producer: str,
        interpret: Callable[[DriverFact, int], tuple[str, int] | None],
    ) -> None:
        if binding != ci.binding or binding.family != "ci" or not producer.strip() or producer == "unknown":
            raise ValueError("exact configured CI interpretation profile required")
        self.ci, self.binding, self.producer, self.interpret = ci, binding, producer, interpret

    def read(
        self, selected: DriverSubject, source: DriverSubject, now: Callable[[], int]
    ) -> tuple[DriverFact, str, int] | None:
        if (
            selected.binding != self.binding
            or selected.kind != "build"
            or selected.revision is not None
            or source.binding.family != "scm"
            or source.kind != "commit"
            or source.revision is None
            or source.key != source.revision
            or self.ci.binding != self.binding
        ):
            return None
        fact = self.ci.build(selected.key, source)
        at = now()
        fact.require_current(at)
        if (
            self.ci.binding != self.binding
            or fact.subject != selected
            or fact.related != (source,)
            or not fact.complete
            or fact.qualification in {"", "unavailable"}
        ):
            return None
        result = self.interpret(fact, at)
        if (
            result is None
            or result[0] not in {"passed", "failed"}
            or type(result[1]) is not int
            or not 0 <= result[1] <= at
            or self.ci.binding != self.binding
        ):
            return None
        return fact, result[0], result[1]


def jenkins_result(fact: DriverFact, now: int) -> tuple[str, int] | None:
    """The retained Jenkins BuildData contract; not applicable to other providers."""
    if fact.subject.binding.implementation != "jenkins":
        return None
    native = object_from(fact.native)
    timestamp = native.get("timestamp")
    if (
        type(timestamp) is not int
        or not 0 < timestamp // 1000 <= now
        or native.get("building") is not False
        or native.get("result") not in {"SUCCESS", "FAILURE"}
    ):
        return None
    return ("passed" if native["result"] == "SUCCESS" else "failed", timestamp // 1000)


class ConfiguredQualifiedBaseline:
    """Receive an existing exact build through a separately selected native profile."""

    def __init__(
        self, profile: QualifiedCIProfile, build: DriverSubject, recipes: tuple[str, ...], clock: Callable[[], int]
    ) -> None:
        if (
            build.binding != profile.binding
            or build.kind != "build"
            or build.revision is not None
            or not recipes
            or any(type(item) is not str or not item.strip() for item in recipes)
            or len(set(recipes)) != len(recipes)
        ):
            raise ValueError("exact build and finite trusted recipe profile required")
        self.profile, self.build, self.recipes, self.clock = profile, build, recipes, clock

    def read_baseline(self, source: DriverSubject, recipe_digest: str) -> QualifiedBaseline | None:
        if source.kind != "ref" or source.revision is None or recipe_digest != digest(encode(self.recipes)):
            return None
        commit = DriverSubject(source.binding, "commit", source.revision, source.revision)
        try:
            first = self.profile.read(self.build, commit, self.clock)
            second = self.profile.read(self.build, commit, self.clock)
            if first is None or second is None:
                return None
            fact, result, observed_at = first
            closing, closing_result, closing_at = second
            now = self.clock()
            fact.require_current(now)
            closing.require_current(now)
            if (fact.subject, fact.native, fact.provenance, fact.qualification, result, observed_at) != (
                closing.subject,
                closing.native,
                closing.provenance,
                closing.qualification,
                closing_result,
                closing_at,
            ):
                return None
            proof = encode(
                {
                    "build": self.build.reference,
                    "source": commit.reference,
                    "recipes": list(self.recipes),
                    "native": fact.native.hex(),
                    "provenance": fact.provenance,
                    "qualification": fact.qualification,
                    "profile": self.profile.producer,
                }
            )
            return QualifiedBaseline(
                source, recipe_digest, self.profile.producer, "sha256:" + digest(proof), result, observed_at, proof
            )
        except (OSError, ValueError, TypeError, OverflowError):
            return None


class JenkinsQualifiedBaseline:
    """Read one exact Jenkins build under a trusted job-to-recipe profile.

    The host owns the recipe binding. Jenkins's result is baseline evidence for
    that profile, not Kernel acceptance and not a claim about other recipes.
    """

    def __init__(
        self,
        ci: ContinuousIntegration,
        build: DriverSubject,
        recipes: tuple[str, ...],
        clock: Callable[[], int],
    ) -> None:
        if (
            type(build) is not DriverSubject
            or build.binding != ci.binding
            or build.binding.family != "ci"
            or build.binding.implementation != "jenkins"
            or build.kind != "build"
            or build.revision is not None
            or type(recipes) is not tuple
            or not recipes
            or any(type(item) is not str or not item.strip() for item in recipes)
            or len(set(recipes)) != len(recipes)
        ):
            raise ValueError("exact Jenkins build and finite trusted recipe profile required")
        self.ci, self.build, self.recipes, self.clock = ci, build, recipes, clock

    def read_baseline(self, source: DriverSubject, recipe_digest: str) -> QualifiedBaseline | None:
        if (
            type(source) is not DriverSubject
            or source.binding.family != "scm"
            or source.kind != "ref"
            or source.revision is None
            or self.ci.binding != self.build.binding
            or recipe_digest != digest(encode(self.recipes))
        ):
            return None
        commit = DriverSubject(source.binding, "commit", source.revision, source.revision)
        try:
            first = self.ci.build(self.build.key, commit)
            second = self.ci.build(self.build.key, commit)
            now = self.clock()
            first.require_current(now)
            second.require_current(now)
            if (
                self.ci.binding != self.build.binding
                or first.presence is not Presence.FOUND
                or second.presence is not Presence.FOUND
                or first.subject != self.build
                or second.subject != self.build
                or first.related != (commit,)
                or second.related != (commit,)
                or first.complete is not True
                or second.complete is not True
                or not first.qualification
                or first.qualification != second.qualification
                or first.provenance != second.provenance
                or first.native != second.native
            ):
                return None
            native = object_from(first.native)
            timestamp = native.get("timestamp")
            if (
                type(timestamp) is not int
                or timestamp <= 0
                or timestamp // 1000 <= 0
                or timestamp // 1000 > now
                or native.get("building") is not False
                or type(native.get("result")) is not str
                or native.get("result") not in {"SUCCESS", "FAILURE"}
            ):
                return None
            proof = encode(
                {
                    "build": self.build.reference,
                    "source": commit.reference,
                    "recipes": list(self.recipes),
                    "native": first.native.hex(),
                    "provenance": first.provenance,
                    "qualification": first.qualification,
                }
            )
            return QualifiedBaseline(
                source,
                recipe_digest,
                "jenkins-qualified-baseline:v1",
                "sha256:" + digest(proof),
                "passed" if native["result"] == "SUCCESS" else "failed",
                timestamp // 1000,
                proof,
            )
        except (OSError, ValueError, OverflowError):
            return None
