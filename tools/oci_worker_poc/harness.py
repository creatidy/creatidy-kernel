# SPDX-License-Identifier: Apache-2.0
"""Controller-side synthetic Attempt lifecycle over the rootless OCI boundary.

Owns sandbox preparation, deterministic launch, cancellation/expiry targeting only the
owned container, settlement verification by host observation (never assumption), and
evidence collection. Cleanup refuses to proceed when settlement cannot be observed.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess  # noqa: S404 - developer harness executes explicit fixed argv, no shell.
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import UNPROVED, RunEvidence
from .ociimage import IMAGE_REF, ImageRecord, build_image_archive, image_exists, load_archive
from .profile import AttemptResources, WorkerProfile, build_profile, build_run_argv
from .toolchain import Toolchain, resolve_toolchain


@dataclass(frozen=True, slots=True)
class Sandbox:
    root: Path
    resources: AttemptResources
    worker_entry: Path


def prepare_sandbox(base: Path, attempt: str) -> Sandbox:
    """Create the disposable per-Attempt resources; the workspace is the only writable bind."""
    root = base / f"attempt-{attempt}"
    workspace = root / "workspace"
    home = root / "home"
    scratch = root / "scratch"
    relay_dir = root / "relay"
    for directory in (workspace, home, scratch, relay_dir):
        directory.mkdir(parents=True, exist_ok=True)
    resources = AttemptResources(
        attempt=attempt,
        workspace=workspace,
        home=home,
        scratch=scratch,
        relay_socket=relay_dir / "effects.sock",
    )
    worker_entry = root / "worker_main.py"
    shutil.copy(Path(__file__).parent / "worker_main.py", worker_entry)
    return Sandbox(root=root, resources=resources, worker_entry=worker_entry)


def write_attempt_spec(sandbox: Sandbox, spec: dict[str, Any], deadline_seconds: int) -> None:
    spec = {"attempt": sandbox.resources.attempt, "deadline_seconds": deadline_seconds, **spec}
    (sandbox.resources.workspace / "attempt.json").write_text(json.dumps(spec, indent=1))


def ensure_image(toolchain: Toolchain, archive_path: Path) -> ImageRecord:
    """Build the deterministic image once and load it into the store when absent."""
    record = build_image_archive(Path(toolchain.busybox.path), archive_path)
    env = toolchain.env()
    podman_prefix = [str(toolchain.root / "bin/podman")]
    if not image_exists(podman_prefix, env, IMAGE_REF):
        load_archive(podman_prefix, env, archive_path)
    return record


def run_profile(
    toolchain: Toolchain, sandbox: Sandbox, image: ImageRecord, deadline_seconds: int
) -> tuple[WorkerProfile, list[str]]:
    profile = build_profile(toolchain, sandbox.resources, sandbox.worker_entry, deadline_seconds)
    container_name = f"kernel78-worker-{sandbox.resources.attempt}"
    argv = build_run_argv(toolchain, profile, image.reference, container_name)
    return profile, argv


def launch(argv: list[str], env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )  # noqa: S603 - fixed controller-built podman argv, never workload input.


def wait_container_exit(process: subprocess.Popen[bytes], timeout: float) -> tuple[int, str, str]:
    stdout, stderr = process.communicate(timeout=timeout)
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def stop_container(toolchain: Toolchain, container_name: str, grace_seconds: int = 5) -> str:
    """Stop only the owned container; returns observed final podman stop output."""
    argv = [
        str(toolchain.root / "bin/podman"),
        "stop",
        "-t",
        str(grace_seconds),
        container_name,
    ]
    result = subprocess.run(
        argv,
        env=toolchain.env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )  # noqa: S603 - fixed tool argv.
    return (result.stdout + result.stderr).strip()


CONTAINER_EXISTS = "exists"
CONTAINER_ABSENT = "absent"
CONTAINER_UNKNOWN = "unknown"


def container_state(toolchain: Toolchain, container_name: str) -> str:
    """Observe owned-container existence without ever inventing terminality.

    ``podman container exists`` distinguishes exit 0 (exists) and exit 1 (confirmed
    absent) from every other outcome — podman 125-style engine/storage/access errors,
    timeouts and lost observations are inconclusive by definition. Those map to
    ``unknown``, never to a confirmed state. Callers must treat ``unknown`` as an
    unresolved lifecycle observation: fail-closed for collection/publication and other
    settlement authorization, and bounded-termination (not silent skipping) for disposal.
    """
    argv = [
        str(toolchain.root / "bin/podman"),
        "container",
        "exists",
        container_name,
    ]
    try:
        result = subprocess.run(
            argv,
            env=toolchain.env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )  # noqa: S603 - fixed tool argv.
    except subprocess.TimeoutExpired:
        return CONTAINER_UNKNOWN
    except OSError:
        return CONTAINER_UNKNOWN
    if result.returncode == 0:
        return CONTAINER_EXISTS
    if result.returncode == 1:
        return CONTAINER_ABSENT
    return CONTAINER_UNKNOWN


def scan_host_for_marker(marker: str) -> list[int]:
    """Observe host /proc for any process whose cmdline still carries the Attempt marker."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if marker in cmdline:
            found.append(int(entry.name))
    return found


def wait_settled(marker: str, timeout_seconds: float = 15.0) -> tuple[bool, list[int]]:
    """Poll for observed descendant settlement; never assume it from a zero exit."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        found = scan_host_for_marker(marker)
        if not found:
            return True, []
        if time.monotonic() >= deadline:
            return False, found
        time.sleep(0.2)


def worker_status(sandbox: Sandbox) -> dict[str, Any]:
    path = sandbox.resources.workspace / "worker-status.json"
    if not path.is_file():
        return {"observed": False}
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def new_evidence(attempt: str, toolchain: Toolchain | None, image: ImageRecord | None, argv: list[str]) -> RunEvidence:
    evidence = RunEvidence(
        attempt=attempt,
        platform=f"{platform.system()} {platform.release()} {platform.machine()}",
        kernel=platform.release(),
        podman_argv=argv,
    )
    if toolchain is not None:
        evidence.tools = [toolchain.podman, toolchain.crun, toolchain.conmon, toolchain.netavark, toolchain.busybox]
    if image is not None:
        evidence.image_archive_sha256 = image.archive_sha256
        evidence.image_config_digest = image.config_digest
        evidence.image_diff_id = image.diff_id
    return evidence


def cleanup_sandbox(sandbox: Sandbox) -> None:
    shutil.rmtree(sandbox.root, ignore_errors=False)


def resolve_or_unproved() -> Toolchain | None:
    """Resolve the toolchain or return None so callers record UNPROVED honestly."""
    try:
        return resolve_toolchain()
    except (FileNotFoundError, RuntimeError):
        return None


__all__ = [
    "CONTAINER_ABSENT",
    "CONTAINER_EXISTS",
    "CONTAINER_UNKNOWN",
    "IMAGE_REF",
    "Sandbox",
    "UNPROVED",
    "cleanup_sandbox",
    "container_state",
    "ensure_image",
    "launch",
    "new_evidence",
    "prepare_sandbox",
    "resolve_or_unproved",
    "run_profile",
    "scan_host_for_marker",
    "stop_container",
    "wait_container_exit",
    "wait_settled",
    "worker_status",
    "write_attempt_spec",
]
