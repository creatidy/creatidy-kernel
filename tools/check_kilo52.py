# SPDX-License-Identifier: Apache-2.0
"""Reproduce the bounded #52 negative on explicitly prepared public-source exports.

No install, live endpoint, product dependency, adapter or passing reception claim.
The caller prepares an unchanged exact Kilo export with its frozen upstream lock;
this tool checks the critical source files against the supplied pinned Git objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Public source revisions and downloaded runtime digest, not credentials.
KILO_REVISION = "0bc1c373a9d9c5abd2a1a941f2bcebb321f467f1"  # pragma: allowlist secret
ROUTER_REVISION = "4162f6fa9f774beaf6e92d86b7ebc92f306da4bd"  # pragma: allowlist secret
BUN_SHA256 = "10702245766b225e70bbf6235fd6b47214fd94b4760d00ffaed8ab0df57ac235"  # pragma: allowlist secret
ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kilo-objects", type=Path, required=True)
    parser.add_argument("--kilo-export", type=Path, required=True)
    parser.add_argument("--router-source", type=Path, required=True)
    parser.add_argument("--bun", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    args = parser.parse_args()
    source: Path = args.kilo_export.resolve()
    objects: Path = args.kilo_objects.resolve()
    router: Path = args.router_source.resolve()
    bun: Path = args.bun.resolve()
    receipts: Path = args.receipts.resolve()
    if (source / ".git").exists() or receipts.exists():
        raise ValueError("use a disposable non-repository source export and a new receipt directory")
    if hashlib.sha256(bun.read_bytes()).hexdigest() != BUN_SHA256:
        raise ValueError("unexpected proof Bun 1.4.4 binary")
    receipts.mkdir()
    with tempfile.TemporaryDirectory(prefix="kernel52-", dir=receipts) as temporary:
        home = Path(temporary)
        env = {
            "HOME": str(home),
            "PATH": f"{bun.parent}:/usr/bin:/bin",
            "TMPDIR": str(home),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "KILO_TELEMETRY_LEVEL": "off",
            "KILO_DISABLE_MODELS_FETCH": "1",
            "KILO_NO_DAEMON": "1",
            "KILO_AUTH_CONTENT": "{}",
            "KILO_DISABLE_DEFAULT_PLUGINS": "1",
            "KILO_SESSION_RETRY_LIMIT": "0",
            "KERNEL52_RECEIPTS": str(receipts),
        }
        # Compare the complete tracked export, not just the files supporting the finding.
        # Additional dependency/build artifacts are not attested as a release binary.
        with tempfile.TemporaryFile(dir=home) as archive:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(objects),
                    "-c",
                    "filter.lfs.required=false",
                    "-c",
                    "filter.lfs.process=",
                    "-c",
                    "filter.lfs.smudge=",
                    "archive",
                    KILO_REVISION,
                ],
                env=env,
                timeout=60,
                check=True,
                stdout=archive,
            )
            _ = archive.seek(0)
            export_digest = hashlib.file_digest(archive, "sha256").hexdigest()
            _ = archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r|") as exported:
                for member in exported:
                    destination = source / member.name
                    if member.issym():
                        if not destination.is_symlink() or str(destination.readlink()) != member.linkname:
                            raise ValueError(f"changed pinned symlink: {member.name}")
                    elif member.isfile():
                        expected = exported.extractfile(member)
                        if expected is None or destination.is_symlink() or destination.read_bytes() != expected.read():
                            raise ValueError(f"changed pinned source: {member.name}")
        observed = subprocess.check_output(
            ["git", "-C", str(router), "rev-parse", "HEAD"], env=env, timeout=30, text=True
        ).strip()
        if observed != ROUTER_REVISION:
            raise ValueError("unexpected Router source revision")
        subprocess.run(
            ["git", "-C", str(router), "diff", "--exit-code", ROUTER_REVISION, "--", "scarcity_router"],
            env=env,
            timeout=30,
            check=True,
            capture_output=True,
        )
        stage = source / "packages/opencode/test/kernel52"
        stage.mkdir(exist_ok=True)
        for fixture in (ROOT / "tests/fixtures/kilo52").iterdir():
            shutil.copyfile(fixture, stage / fixture.name)
        command = [
            str(bun),
            "test",
            "--preload",
            str(stage / "network.mjs"),
            "test/kernel52/probe.test.ts",
            "--timeout",
            "60000",
        ]
        subprocess.run(command, cwd=source / "packages/opencode", env=env, timeout=180, check=True)
        subprocess.run(
            command,
            cwd=source / "packages/opencode",
            env={**env, "KILO_PURE": "1", "KERNEL52_EXPECT_BYPASS": "1"},
            timeout=180,
            check=True,
        )
        # Use the actual producer parser, not the fake's permissive capture endpoint.
        check = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from scarcity_router.gateway_openai import parse_chat_completion_request
from scarcity_router.gateway_contracts import GatewayError
from scarcity_router.executable_contract import ExecutableRequirements
body = json.loads(pathlib.Path(sys.argv[2]).read_bytes())
context = ExecutableRequirements.from_dict(body["execution_requirements"])
assert context.to_dict() == body["execution_requirements"]
control = dict(body)
# Parser isolation only. Never forwarded or claimed as a semantics-preserving mapping.
control["messages"] = [{"role": "user", "content": "synthetic parser control"}]
parse_chat_completion_request(control)
try:
    parse_chat_completion_request(body)
except GatewayError as error:
    assert (error.code, error.param) == ("unsupported_parameter", "messages")
    print(json.dumps({"outcome": "refused", "code": error.code, "param": error.param}))
else:
    raise SystemExit("expected producer refusal was not reproduced")
"""
        result = subprocess.check_output(
            [sys.executable, "-c", check, str(router), str(receipts / "capture.body.json")],
            env=env,
            timeout=30,
            text=True,
        )
        refusal: object = json.loads(result)
        report = {
            "kilo_revision": KILO_REVISION,
            "router_revision": ROUTER_REVISION,
            "bun_sha256": BUN_SHA256,
            "source_archive_sha256": export_digest,
            "upstream_lock_sha256": hashlib.sha256((source / "bun.lock").read_bytes()).hexdigest(),
            "fixture_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.iterdir()},
            "producer_refusal": refusal,
            "feasibility": "NOT_ESTABLISHED",
            "unproved": [
                "all auxiliary paths",
                "durable send claims",
                "ambiguous execution recovery",
                "owned process settlement",
                "network isolation",
                "hostile worker isolation",
            ],
            "live_inference": False,
            "runtime_dependency_adopted": False,
        }
        (receipts / "report.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        print(json.dumps({"feasibility": "NOT_ESTABLISHED", "producer_refusal": refusal}))


if __name__ == "__main__":
    main()
