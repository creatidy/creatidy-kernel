# SPDX-License-Identifier: Apache-2.0
"""Receive a host-configured Jenkins baseline without executing repository code."""

from collections.abc import Callable

from creatidy_kernel.core.drivers import DriverSubject
from creatidy_kernel.core.forge import Presence
from creatidy_kernel.core.intake import digest, encode, object_from
from creatidy_kernel.core.ordinary_qualified import QualifiedBaseline
from creatidy_kernel.ports.drivers import ContinuousIntegration


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
