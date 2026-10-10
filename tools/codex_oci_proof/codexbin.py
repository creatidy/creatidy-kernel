# SPDX-License-Identifier: Apache-2.0
"""Version-pinned real Codex binary resolution and native protocol inventory.

The binary is the host's own installed standalone Codex release: this module only resolves,
versions, hashes and records it — it never reads the owner's Codex home, sessions or
credentials, and the proof always runs with a fresh synthetic ``CODEX_HOME``. A missing or
version-mismatched binary refuses so the native receipt stays UNPROVED instead of silently
substituting another executable. The app-server protocol inventory is taken from the binary
itself via its schema generator, so protocol claims carry exact-version evidence.
"""

import json
import os
import re
import shutil
import subprocess  # noqa: S404 - developer tool executes explicit fixed argv, no shell.
from dataclasses import dataclass
from pathlib import Path

from tools.oci_worker_poc.evidence import ToolRecord, sha256_path

CODEX_BIN_ENV = "CREATIDY_TEST_CODEX_BIN"
PINNED_CODEX_VERSION = "0.159.3"
_VERSION_LINE = re.compile(r"^codex(?:-cli)? (?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?)$")

# The four native methods the Kernel Runtime contract requires, and the schema types the
# binary's own generator must emit for each. The generator does not publish a method-name
# registry, so the mapping is stated here and checked against generated files.
REQUIRED_METHOD_SCHEMAS: tuple[tuple[str, str], ...] = (
    ("thread/start", "ThreadStartParams.json"),
    ("turn/start", "TurnStartParams.json"),
    ("thread/read", "ThreadReadParams.json"),
    ("turn/interrupt", "TurnInterruptParams.json"),
)


@dataclass(frozen=True, slots=True)
class CodexBinary:
    """The resolved pinned Codex executable: provenance pins, not an authorship attestation."""

    path: Path
    version: str
    sha256: str
    code_mode_host: Path | None

    def tool_records(self, probe_context: str) -> list[ToolRecord]:
        origin = (
            f"host standalone Codex release resolved for the #53 bounded proof ({probe_context}); "
            "fresh synthetic CODEX_HOME only; owner Codex state never read"
        )
        records = [
            ToolRecord(
                name="codex",
                path=str(self.path),
                version=f"codex-cli {self.version}",
                sha256=self.sha256,
                origin=origin,
            )
        ]
        if self.code_mode_host is not None:
            records.append(
                ToolRecord(
                    name="codex-code-mode-host",
                    path=str(self.code_mode_host),
                    version="static",
                    sha256=sha256_path(self.code_mode_host),
                    origin=origin,
                )
            )
        return records


def _probe_version(binary: Path, probe_home: Path) -> str:
    probe_home.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(probe_home),
        "CODEX_HOME": str(probe_home / "codex-home"),
        "LC_ALL": "C",
    }
    result = subprocess.run(
        [str(binary), "--version"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )  # noqa: S603 - fixed pinned-binary argv built from resolved paths.
    if result.returncode != 0:
        raise RuntimeError(f"codex --version failed: {result.stderr.strip()[:200]}")
    first = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
    match = _VERSION_LINE.match(first)
    if match is None:
        raise RuntimeError(f"unrecognized codex version output: {first!r}")
    return match.group("version")


def resolve_codex_binary(probe_root: Path) -> CodexBinary:
    """Resolve the pinned Codex executable or raise with the precise missing pin."""
    configured = os.environ.get(CODEX_BIN_ENV)
    candidate = Path(configured).resolve() if configured else None
    if candidate is None:
        discovered = shutil.which("codex")
        if discovered is None:
            raise FileNotFoundError(
                f"real Codex binary required for the native proof: set {CODEX_BIN_ENV} or put codex on PATH"
            )
        candidate = Path(discovered).resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"resolved Codex binary is not a file: {candidate}")
    probe_root.mkdir(parents=True, exist_ok=True)
    version = _probe_version(candidate, probe_root)
    if version != PINNED_CODEX_VERSION:
        raise RuntimeError(
            f"Codex version pin changed: found {version}, proof pins {PINNED_CODEX_VERSION}; "
            "update the pinned record deliberately, never silently"
        )
    sibling = candidate.parent / "codex-code-mode-host"
    return CodexBinary(
        path=candidate,
        version=version,
        sha256=sha256_path(candidate),
        code_mode_host=sibling if sibling.is_file() else None,
    )


@dataclass(frozen=True, slots=True)
class ProtocolInventory:
    """Native protocol facts taken from the pinned binary's own schema generator."""

    required_methods: frozenset[str]
    generated_param_files: int
    method_evidence: tuple[str, ...]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "required_methods": sorted(self.required_methods),
            "generated_param_files": self.generated_param_files,
            "method_evidence": list(self.method_evidence),
        }


def protocol_inventory(binary: CodexBinary, workdir: Path) -> ProtocolInventory:
    """Generate the app-server JSON schema with the pinned binary and check required methods."""
    out_dir = workdir / "app-server-schema"
    probe_home = workdir / "schema-probe-home"
    probe_home.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(probe_home),
        "CODEX_HOME": str(probe_home / "codex-home"),
        "TMPDIR": str(workdir),
        "LC_ALL": "C",
    }
    result = subprocess.run(
        [str(binary.path), "app-server", "generate-json-schema", "--out", str(out_dir)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )  # noqa: S603 - fixed pinned-binary argv.
    if result.returncode != 0:
        raise RuntimeError(f"app-server schema generation failed: {result.stderr.strip()[:300]}")
    evidence: list[str] = []
    for method, schema_file in REQUIRED_METHOD_SCHEMAS:
        if (out_dir / "v2" / schema_file).is_file():
            evidence.append(f"{method} <- v2/{schema_file}")
        else:
            raise RuntimeError(f"generated schema lacks {schema_file} required for {method}")
    generated = len(list((out_dir / "v2").glob("*Params.json"))) if (out_dir / "v2").is_dir() else 0
    return ProtocolInventory(
        required_methods=frozenset(method for method, _ in REQUIRED_METHOD_SCHEMAS),
        generated_param_files=generated,
        method_evidence=tuple(evidence),
    )


def write_evidence(record: object, path: Path) -> None:
    """Persist one JSON evidence artifact (compact, sorted, deterministic enough for diffs)."""
    path.write_text(json.dumps(record, indent=1, sort_keys=True, default=str) + "\n")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = [
    "CODEX_BIN_ENV",
    "PINNED_CODEX_VERSION",
    "CodexBinary",
    "ProtocolInventory",
    "ensure_dir",
    "protocol_inventory",
    "resolve_codex_binary",
    "write_evidence",
]
