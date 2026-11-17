# SPDX-License-Identifier: Apache-2.0
"""Deliberately different synthetic role contracts, never native provider support."""

from dataclasses import replace

from creatidy_kernel.core.drivers import DriverBinding, DriverFact, DriverSubject, WorkObservation
from creatidy_kernel.core.forge import Presence
from creatidy_kernel.core.intake import encode, object_from
from creatidy_kernel.ports.drivers import ContinuousIntegration, Knowledge, WorkManagement


class SyntheticWork:
    def __init__(self, original: WorkManagement, instance: str) -> None:
        self.original = original
        self.binding = DriverBinding(
            "work", "synthetic-work", "1", instance, "separate-account", "engineering", "fixture/1", "read-only"
        )

    def work(self, key: str) -> WorkObservation:
        observed = self.original.work(key)
        native = object_from(observed.fact.native)
        fields = object_from(encode(native.get("fields", {})))
        status = object_from(encode(fields.get("status", {})))
        return WorkObservation(
            replace(
                observed.fact,
                subject=replace(observed.fact.subject, binding=self.binding),
                native=encode({"stage": "active" if status.get("id") == "3" else "closed", "original": native}),
                provenance="synthetic-work-read:v1",
                qualification="visible-native-links-not-complete",
            ),
            tuple(replace(edge, target=replace(edge.target, binding=self.binding)) for edge in observed.dependencies),
            observed.dependencies_complete,
        )


class SyntheticCI:
    def __init__(self, original: ContinuousIntegration) -> None:
        self.original = original
        self.binding = DriverBinding(
            "ci",
            "synthetic-ci",
            "1",
            "https://ci.separate.invalid:9443",
            "build-account",
            "verification-project",
            "fixture/2",
            "read-only",
        )

    def build(self, key: str, source: DriverSubject) -> DriverFact:
        fact = self.original.build(key, source)
        if fact.presence is not Presence.FOUND:
            return replace(fact, subject=replace(fact.subject, binding=self.binding))
        native = object_from(fact.native)
        return replace(
            fact,
            subject=replace(fact.subject, binding=self.binding),
            native=encode(
                {
                    "outcome": native.get("result"),
                    "active": native.get("building"),
                    "at": native.get("timestamp"),
                    "original": native,
                }
            ),
            provenance="synthetic-external-attestation:v2",
            qualification="fixture-exact-build-not-local-execution",
        )


def synthetic_ci_result(fact: DriverFact, now: int) -> tuple[str, int] | None:
    if (
        fact.subject.binding.implementation != "synthetic-ci"
        or fact.qualification != "fixture-exact-build-not-local-execution"
    ):
        return None
    native = object_from(fact.native)
    at = native.get("at")
    if (
        type(at) is not int
        or not 0 < at // 1000 <= now
        or native.get("active") is not False
        or native.get("outcome") not in {"SUCCESS", "FAILURE"}
    ):
        return None
    return "passed" if native["outcome"] == "SUCCESS" else "failed", at // 1000


class SyntheticDocument:
    def __init__(self, original: Knowledge) -> None:
        self.original = original
        self.binding = DriverBinding(
            "knowledge",
            "synthetic-document",
            "1",
            "https://docs.separate.invalid",
            "documentation-account",
            "repository-docs",
            "fixture/1",
            "read-only",
        )

    def content(self, key: str, revision: str) -> DriverFact:
        fact = self.original.content(key, revision)
        return replace(
            fact,
            subject=DriverSubject(self.binding, "document", key, revision),
            provenance="synthetic-document:v1",
            qualification="revision-bound-fixture-document",
        )
