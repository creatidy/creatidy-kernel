# SPDX-License-Identifier: Apache-2.0
"""Read genuine owned-command receipts without another executor or CI identity conversion."""

from collections.abc import Callable
from dataclasses import asdict
from typing import cast

from creatidy_kernel.adapters.driver_http import object_value
from creatidy_kernel.adapters.ordinary_workspace import OrdinaryGitWorkspace
from creatidy_kernel.adapters.project_drivers import TrustedSCMRemote
from creatidy_kernel.adapters.task_execution import VerificationCommand
from creatidy_kernel.core.drivers import DriverBinding, DriverSubject
from creatidy_kernel.core.forge import UnsupportedForge
from creatidy_kernel.core.intake import digest, encode, object_from
from creatidy_kernel.core.ordinary_qualified import QualifiedBaseline
from creatidy_kernel.core.verification import Evidence, EvidenceSubject, VerificationPolicy
from creatidy_kernel.ports.verification import VerificationProfile


class TrustedLocalVerification:
    """Selectable CheckProducer over existing exact historical local execution evidence.

    The binding names the trusted controller instance, not a contacted HTTPS CI.
    Current Program authority and acceptance remain the existing application's job.
    """

    def __init__(
        self,
        binding: DriverBinding,
        workspace: OrdinaryGitWorkspace,
        commands: tuple[VerificationCommand, ...],
        policy: VerificationPolicy,
        profile: VerificationProfile,
        clock: Callable[[], int],
    ) -> None:
        if (binding.family, binding.implementation, binding.api_version) != ("ci", "trusted-local", "owned-command-v1"):
            raise ValueError("explicit trusted local verification binding required")
        if profile is not VerificationProfile.TRUSTED_DEVELOPMENT:
            raise UnsupportedForge("owned command receipts do not attest a sandbox or external CI")
        if not commands or commands != workspace.commands or "commands" not in policy.checks:
            raise ValueError("exact existing command recipe/policy required")
        self.binding, self.workspace, self.commands, self.policy, self.profile, self.clock = (
            binding,
            workspace,
            commands,
            policy,
            profile,
            clock,
        )
        self.configuration = self._configuration()

    def _configuration(self) -> bytes:
        return encode(
            {
                "binding": asdict(self.binding),
                "commands": [asdict(command) for command in self.workspace.commands],
                "policy": asdict(self.policy),
                "profile": self.profile.value,
                "repository": self.workspace.repository_url,
                "assurance": "trusted-development-owned-groups;not-external-attestation;not-hostile-worker-isolation",
            }
        )

    def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
        now = self.clock()
        if (
            name != "commands"
            or self._configuration() != self.configuration
            or subject.policy_digest != self.policy.reference.digest
            or subject.repository != self.workspace.repository_url
            or self.workspace.base is not None
            and subject.base_revision != self.workspace.base
        ):
            return None
        received = self.workspace.recorded_commands(subject)
        if received is None:
            return None
        evidence, raw = received
        if (
            evidence.subject != subject
            or type(evidence.passed) is not bool
            or evidence.producer != "ordinary-controller"
            or evidence.tool_version != "owned-ordinary:v1"
            or evidence.check != name
            or not evidence.observed_at <= now <= evidence.fresh_until
        ):
            return None
        value = object_from(raw)
        expected = [(list(command.argv), repeat) for command in self.commands for repeat in range(command.repeats)]
        runs = value.get("runs")
        if type(runs) is not list:
            return None
        rows = cast(list[object], runs)
        if len(rows) != len(expected):
            return None
        stamps: list[int] = []
        passed = True
        for run, (argv, repeat) in zip(rows, expected, strict=True):
            if type(run) is not dict:
                return None
            result = object_value(cast(object, run))
            at = result.get("observed_at")
            if (
                result.get("argv") != argv
                or type(result.get("repeat")) is not int
                or result.get("repeat") != repeat
                or result.get("owned_group_settled") is not True
                or result.get("authority_lost") is not False
                or result.get("timed_out") is not False
                or result.get("output_exhausted") is not False
                or type(result.get("exit_code")) is not int
                or type(at) is not int
                or not evidence.observed_at <= at <= now
            ):
                return None
            stamps.append(at)
            passed = passed and result["exit_code"] == 0
        if (
            not stamps
            or min(stamps) != evidence.observed_at
            or evidence.passed != passed
            or self._configuration() != self.configuration
        ):
            return None
        # Preserve native core subject, producer, original age and artifact identity.
        # This observation creates neither an execution grant nor a new receipt.
        return evidence

    def baseline(
        self, subject: EvidenceSubject, mapping: TrustedSCMRemote, recipes: tuple[str, ...], decision_reference: str
    ) -> "RecordedLocalBaseline":
        return RecordedLocalBaseline(self, subject, mapping, recipes, decision_reference)


class RecordedLocalBaseline:
    """Project an existing genuine Kernel receipt into a Kernel baseline, not a provider build."""

    def __init__(
        self,
        local: TrustedLocalVerification,
        subject: EvidenceSubject,
        mapping: TrustedSCMRemote,
        recipes: tuple[str, ...],
        decision_reference: str,
    ) -> None:
        if (
            mapping.remote != local.workspace.repository_url
            or type(decision_reference) is not str
            or not decision_reference.strip()
            or type(recipes) is not tuple
            or not recipes
            or len(recipes) > 128
            or any(type(item) is not str or not item.strip() or len(item) > 4096 for item in recipes)
            or len(set(recipes)) != len(recipes)
        ):
            raise ValueError("exact trusted local source and recipe mapping required")
        self.local, self.subject, self.mapping, self.recipes, self.decision_reference = (
            local,
            subject,
            mapping,
            recipes,
            decision_reference,
        )
        self.configuration = self._configuration()
        self.producer = "trusted-local-owned-baseline:v1:" + digest(self.configuration)

    def _configuration(self) -> bytes:
        return encode(
            {
                "local_configuration_digest": digest(self.local.configuration),
                "binding": asdict(self.local.binding),
                "execution_profile": self.local.profile.value,
                "subject": asdict(self.subject),
                "source_mapping": asdict(self.mapping),
                "recipes": list(self.recipes),
                "decision": self.decision_reference,
            }
        )

    def read_baseline(self, source: DriverSubject, recipe_digest: str) -> QualifiedBaseline | None:
        if (
            source.binding != self.mapping.binding
            or source.kind != "ref"
            or source.revision != self.subject.head_revision
            or recipe_digest != digest(encode(self.recipes))
            or self._configuration() != self.configuration
        ):
            return None
        evidence = self.local.check("commands", self.subject)
        if evidence is None:
            return None
        proof = encode(
            {
                "configuration": self.configuration.hex(),
                "source": source.reference,
                "recipe_digest": recipe_digest,
                "execution_profile": self.local.profile.value,
                "evidence": asdict(evidence),
                "qualification": (
                    "genuine-owned-local-command-receipt;not-external-attestation;not-hostile-worker-isolation"
                ),
            }
        )
        return QualifiedBaseline(
            source,
            recipe_digest,
            self.producer,
            "sha256:" + digest(proof),
            "passed" if evidence.passed else "failed",
            evidence.observed_at,
            proof,
        )
