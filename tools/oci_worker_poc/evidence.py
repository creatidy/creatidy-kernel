# SPDX-License-Identifier: Apache-2.0
"""Typed evidence records for the isolation proof: observed facts, never claims.

Every record distinguishes observed enforcement (values read from the running system) from
configuration intent (argv/flags passed to podman). A skipped or unavailable probe is
recorded as UNPROVED, never as a pass.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNPROVED = "UNPROVED"
PASSED = "passed"
FAILED = "failed"


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ToolRecord:
    """One external binary: provenance pins, not an authorship attestation."""

    name: str
    path: str
    version: str
    sha256: str
    origin: str

    def to_json_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "sha256": self.sha256,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    """One observed denial or allowance inside the worker boundary."""

    probe: str
    expectation: str
    outcome: str  # PASSED / FAILED / UNPROVED
    observation: str

    def to_json_dict(self) -> dict[str, str]:
        return {
            "probe": self.probe,
            "expectation": self.expectation,
            "outcome": self.outcome,
            "observation": self.observation,
        }


@dataclass(slots=True)
class RunEvidence:
    """Collected facts for one synthetic Attempt execution."""

    attempt: str
    platform: str
    kernel: str
    podman_argv: list[str] = field(default_factory=list)
    tools: list[ToolRecord] = field(default_factory=list)
    image_archive_sha256: str = ""
    image_config_digest: str = ""
    image_diff_id: str = ""
    probes: list[ProbeRecord] = field(default_factory=list)
    host_observations: dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "platform": self.platform,
            "kernel": self.kernel,
            "podman_argv": self.podman_argv,
            "tools": [tool.to_json_dict() for tool in self.tools],
            "image_archive_sha256": self.image_archive_sha256,
            "image_config_digest": self.image_config_digest,
            "image_diff_id": self.image_diff_id,
            "probes": [probe.to_json_dict() for probe in self.probes],
            "host_observations": dict(self.host_observations),
        }

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json_dict(), indent=1, sort_keys=True) + "\n")
