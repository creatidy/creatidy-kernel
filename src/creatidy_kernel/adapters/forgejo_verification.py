# SPDX-License-Identifier: Apache-2.0
"""Trusted recipe-to-status selection, not Forgejo Actions or Kernel acceptance."""

import json
from dataclasses import asdict, replace

from creatidy_kernel.adapters.driver_http import list_value, object_value
from creatidy_kernel.adapters.forgejo_drivers import ForgejoCI
from creatidy_kernel.adapters.qualified_baseline import ConfiguredQualifiedBaseline, QualifiedCIProfile
from creatidy_kernel.core.drivers import DriverFact, DriverSubject, driver_digest
from creatidy_kernel.core.forge import Presence
from creatidy_kernel.core.intake import encode, object_from


class ForgejoStatusVerification:
    """An explicit profile selector over native exact-commit status observations.

    The port's build key denotes this configured selection, not a native build ID.
    Native status identity, scope and assurance remain separately represented.
    """

    def __init__(
        self, ci: ForgejoCI, contexts: tuple[str, ...], recipes: tuple[str, ...], decision_reference: str
    ) -> None:
        if (
            not contexts
            or not recipes
            or not decision_reference.strip()
            or any(not value.strip() for value in (*contexts, *recipes))
            or len(set(contexts)) != len(contexts)
            or len(set(recipes)) != len(recipes)
        ):
            raise ValueError("explicit trusted recipe/context mapping required")
        self.ci, self.binding, self.contexts, self.recipes = ci, ci.binding, contexts, recipes
        self.decision_reference = decision_reference
        self.mapping = self._configuration()
        self.key = "status-profile:" + driver_digest(json.loads(self.mapping))
        self.producer = "forgejo-native-status-profile:v1:" + self.key

    def _configuration(self) -> bytes:
        return encode(
            {
                "ci": asdict(self.ci.binding),
                "source": asdict(self.ci.source_binding),
                "contexts": list(self.contexts),
                "recipes": list(self.recipes),
                "decision": self.decision_reference,
                "remote_mapping": self.ci.mapping_digest,
            }
        )

    def build(self, key: str, source: DriverSubject) -> DriverFact:
        if key != self.key or self.ci.binding != self.binding or self._configuration() != self.mapping:
            raise ValueError("exact configured status profile required")
        fact = self.ci.build(source.key, source)
        selected = DriverSubject(self.binding, "build", self.key, None)
        if fact.presence is not Presence.FOUND or not fact.complete or fact.qualification in {"", "unavailable"}:
            return replace(fact, subject=selected)
        if fact.subject != DriverSubject(self.binding, "commit-statuses", source.key, None) or fact.related != (
            source,
        ):
            raise ValueError("native exact-commit status evidence differs")
        return replace(
            fact,
            subject=selected,
            native=encode(
                {
                    "native_subject": fact.subject.reference,
                    "native": fact.native.hex(),
                    "qualification": fact.qualification,
                    "mapping": self.mapping.hex(),
                }
            ),
            qualification="configured-exact-status-collection;not-native-build;not-Actions;not-Kernel-acceptance",
        )

    def _result(self, fact: DriverFact, now: int) -> tuple[str, int] | None:
        value = object_from(fact.native)
        if value.get("mapping") != self.mapping.hex() or fact.subject.key != self.key or not fact.related:
            return None
        source = fact.related[0]
        if (
            source.binding != self.ci.source_binding
            or value.get("native_subject") != DriverSubject(self.binding, "commit-statuses", source.key, None).reference
        ):
            return None
        native = value.get("native")
        if type(native) is not str:
            return None
        rows = list_value(json.loads(bytes.fromhex(native)))
        required: dict[str, str] = {}
        seen: set[str] = set()
        for value in rows:
            row = object_value(value)
            identity = object_value(row.get("reference")).get("value")
            revision = object_value(row.get("revision")).get("value")
            context, result = row.get("check_context"), row.get("check_result")
            if (
                type(identity) is not str
                or identity in seen
                or revision != "forgejo:" + source.key
                or row.get("presence") != "found"
            ):
                return None
            seen.add(identity)
            if context in self.contexts:
                if type(context) is not str or context in required or result not in {"passed", "failed", "error"}:
                    return None
                required[context] = "passed" if result == "passed" else "failed"
        if set(required) != set(self.contexts) or not fact.observed_at <= now <= fact.fresh_until:
            return None
        # Native statuses do not supply a build timestamp in the retained facade.
        # This is retrieval age, with the generic baseline's original nonrenewal.
        return ("passed" if all(result == "passed" for result in required.values()) else "failed", fact.observed_at)

    def profile(self) -> QualifiedCIProfile:
        return QualifiedCIProfile(self, self.binding, self.producer, self._result)

    def baseline(self) -> ConfiguredQualifiedBaseline:
        return ConfiguredQualifiedBaseline(
            self.profile(), DriverSubject(self.binding, "build", self.key, None), self.recipes, self.ci.clock
        )
