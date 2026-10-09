# SPDX-License-Identifier: Apache-2.0
"""Controller-side orchestration of one real-Codex Attempt over the rootless OCI boundary.

The controller owns Attempt identity, the synthetic scenario, durable authority at the relay
seam, cancellation/expiry, settlement observation, and immutable collection. The native
app-server is reached through the production ``CodexStdio`` transport driven against the
per-Attempt generated wrapper, so the transport under test is the repository's own; its
constructor performs the native ``initialize`` handshake and fails closed on mismatch. Every
wait is bounded; nothing is assumed from a zero exit or an acknowledgment alone.
"""

from __future__ import annotations

import dataclasses
import json
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from creatidy_kernel.adapters.codex_stdio import CodexRPCError, CodexStdio
from tools.oci_worker_poc.evidence import RunEvidence, sha256_path
from tools.oci_worker_poc.harness import (
    container_state,
    scan_host_for_marker,
    stop_container,
    wait_settled,
)
from tools.oci_worker_poc.ociimage import ImageRecord
from tools.oci_worker_poc.profile import WorkerProfile
from tools.oci_worker_poc.relay import EffectsRelay
from tools.oci_worker_poc.toolchain import Toolchain

from .boundary import (
    build_codex_profile,
    build_run_argv,
    container_name_for,
    new_attempt_id,
    prepare_codex_sandbox,
    toolchain_records,
    write_wrapper_script,
)
from .codexbin import CodexBinary, ProtocolInventory
from .collect import SafeCollection, collect_directory, read_bounded_regular, stored_bytes_sink

# Recorded verbatim into evidence: the thread is created with Codex's own approval machinery
# configured to never ask and never self-restrict, so every observed enforcement below comes
# from the OCI boundary, not from a native approval callback or native sandbox.
THREAD_START_PARAMS: dict[str, object] = {
    "model": "synthetic-model",
    "modelProvider": "synthetic",
    "cwd": "/workspace",
    "sandbox": "danger-full-access",
    "approvalPolicy": "never",
}

DANGEROUS_SANDBOX_POLICY: dict[str, object] = {"type": "dangerFullAccess"}

# The only publishable candidate-artifact root inside the worker workspace. The worker can
# write anywhere in /workspace (that is the #78 static boundary), but publication draws
# exclusively from this controller-declared root; everything else is private runtime/evidence
# state retained under controller authority and never presented as candidate artifacts.
CANDIDATE_DIRNAME = "candidate"


class AttemptExpired(RuntimeError):
    """The controller refuses new external operations after Attempt expiry/revocation."""


def exec_step(cmd: str) -> dict[str, Any]:
    return {"kind": "exec", "cmd": cmd}


def final_step(text: str) -> dict[str, Any]:
    return {"kind": "final", "text": text}


def write_scenario(workspace: Path, steps: list[dict[str, Any]], attempt: str) -> Path:
    """Persist the scenario with ``{attempt}`` placeholders substituted (the mock loads this
    file once at boundary start, so attempt-unique markers must be baked in before launch)."""

    def substitute(value: object) -> object:
        if isinstance(value, str):
            return value.replace("{attempt}", attempt)
        if isinstance(value, list):
            return [substitute(item) for item in cast(list[object], value)]
        if isinstance(value, dict):
            return {str(key): substitute(item) for key, item in cast(dict[object, object], value).items()}
        return value

    path = workspace / "mock" / "scenario.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"steps": substitute(steps)}, indent=1, sort_keys=True) + "\n")
    return path


@dataclass(frozen=True, slots=True)
class AttemptCollection:
    """Post-settlement collection split by trust class (see CodexAttempt.collect)."""

    publishable: SafeCollection
    private: SafeCollection
    collected_root: Path


@dataclass(slots=True)
class TurnRecord:
    turn_id: str
    prompt: str
    status: str = "unknown"
    statuses_seen: list[str] = field(default_factory=list)


class CodexAttempt:
    """One disposable real-Codex Attempt inside one rootless OCI boundary."""

    def __init__(
        self,
        base: Path,
        toolchain: Toolchain,
        codex_binary: CodexBinary,
        inventory: ProtocolInventory,
        scenario_steps: list[dict[str, Any]],
        image: ImageRecord,
        *,
        deadline_seconds: int = 240,
        pids_limit: int | None = None,
    ) -> None:
        self.toolchain = toolchain
        self.codex_binary = codex_binary
        self.inventory = inventory
        self.image = image
        self.attempt = new_attempt_id()
        self.root, codex_resources = prepare_codex_sandbox(base, self.attempt)
        self.resources = (
            dataclasses.replace(codex_resources, pids_limit=pids_limit) if pids_limit is not None else codex_resources
        )
        self.container = container_name_for(self.attempt)
        self.deadline_seconds = deadline_seconds
        self.expired = False
        self.relay = EffectsRelay(self.resources.relay_socket, self.attempt)
        self.notifications: list[dict[str, object]] = []
        self.turns: dict[str, TurnRecord] = {}
        self.command_execs: list[dict[str, object]] = []
        self.thread_id: str | None = None
        self.thread_start_result: dict[str, object] | None = None
        self.connection: CodexStdio | None = None
        self.owned_group_settled: bool | None = None
        self.terminal: str = "unknown"

        self._write_attempt_spec()
        write_scenario(self.resources.workspace, scenario_steps, self.attempt)
        worker_entry = self.root / "worker_main.py"
        worker_entry.write_text((Path(__file__).parent / "worker_entry.py").read_text())
        self.profile: WorkerProfile = build_codex_profile(
            toolchain,
            self.resources,
            worker_entry,
            Path(__file__).parent / "mockmodel.py",
            codex_binary,
            deadline_seconds,
        )
        self.wrapper = write_wrapper_script(
            self.root / "codex-wrapper.sh",
            toolchain,
            self.profile,
            image,
            self.container,
            codex_binary.path,
            self.root / "probe-home",
        )
        self.evidence = RunEvidence(
            attempt=self.attempt,
            platform=f"{platform.system()} {platform.release()} {platform.machine()}",
            kernel=platform.release(),
            podman_argv=build_run_argv(toolchain, self.profile, image.reference, self.container),
            tools=toolchain_records(toolchain, codex_binary.tool_records(str(codex_binary.path))),
        )

    def _write_attempt_spec(self) -> None:
        spec = {"attempt": self.attempt, "deadline_seconds": self.deadline_seconds}
        (self.resources.workspace / "attempt.json").write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n")

    # -- lifecycle ---------------------------------------------------------------------
    def connect(self) -> None:
        """Start the boundary; the production transport handshake must succeed or the
        Attempt fails closed (version mismatch, missing methods, dead app-server)."""
        self.relay.start()
        self.connection = CodexStdio(
            (str(self.wrapper), "app-server"),
            expected_version=self.codex_binary.version,
            timeout=60,
            schema_methods=self.inventory.required_methods,
            schema_version=self.codex_binary.version,
        )
        self._drain_notifications()

    def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if self.connection is None:
            raise RuntimeError("connect() before native requests")
        try:
            return self.connection.request(method, params)
        finally:
            self._drain_notifications()

    def _drain_notifications(self) -> None:
        if self.connection is None:
            raise RuntimeError("connect() before native requests")
        self.notifications.extend(self.connection.notifications())

    def thread_start(self) -> str:
        if self.expired:
            raise AttemptExpired("controller refuses thread creation after Attempt expiry")
        result = self._request("thread/start", dict(THREAD_START_PARAMS))
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(cast(dict[object, object], thread).get("id"), str):
            raise RuntimeError(f"thread/start returned no thread id: {result}")
        self.thread_start_result = result
        self.thread_id = str(cast(dict[object, object], thread)["id"])
        return self.thread_id

    def start_turn(self, prompt: str) -> str:
        if self.expired:
            raise AttemptExpired("controller refuses new turn dispatch after Attempt expiry/revocation")
        if self.thread_id is None:
            self.thread_start()
        if self.thread_id is None:
            raise RuntimeError("thread/start did not establish a thread id")
        result = self._request(
            "turn/start",
            {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}]},
        )
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(cast(dict[object, object], turn).get("id"), str):
            raise RuntimeError(f"turn/start returned no turn id: {result}")
        turn_id = str(cast(dict[object, object], turn)["id"])
        self.turns[turn_id] = TurnRecord(turn_id=turn_id, prompt=prompt)
        return turn_id

    def wait_turn(self, turn_id: str, timeout_seconds: float = 90.0) -> str:
        deadline = time.monotonic() + timeout_seconds
        status: str | None = None
        stable_terminal: str | None = None
        while time.monotonic() < deadline:
            status = self.turn_status(turn_id)
            record = self.turns.get(turn_id)
            if record is not None and (not record.statuses_seen or record.statuses_seen[-1] != status):
                record.statuses_seen.append(status)
            if status in {"completed", "failed", "interrupted"}:
                # A terminal status must hold across consecutive reads: the deprecated
                # hydration path was observed to report a transient fallback status on a
                # turn that its own rollout record proves completed.
                if stable_terminal == status:
                    if record is not None:
                        record.status = status
                    return status
                stable_terminal = status
            else:
                stable_terminal = None
            time.sleep(0.4)
        raise TimeoutError(f"turn {turn_id} never reached a terminal status (last: {status})")

    def _turn_status_from_notifications(self, turn_id: str) -> str | None:
        """The app-server's own turn lifecycle notifications, drained alongside every request."""
        status: str | None = None
        for notification in self.notifications:
            if notification.get("method") not in {"turn/started", "turn/completed"}:
                continue
            params = notification.get("params")
            if not isinstance(params, dict):
                continue
            turn = cast(dict[object, object], params).get("turn")
            if not isinstance(turn, dict):
                continue
            turn_object = cast(dict[object, object], turn)
            if turn_object.get("id") == turn_id and isinstance(turn_object.get("status"), str):
                status = str(turn_object["status"])
        return status

    def turn_status(self, turn_id: str) -> str:
        if self.thread_id is None:
            raise RuntimeError("thread/start did not establish a thread id")
        notified = self._turn_status_from_notifications(turn_id)
        if notified in {"completed", "failed", "interrupted"}:
            return notified
        # thread/read is the live status path; a paginated thread can reject includeTurns
        # hydration ("list_turns is not supported yet"), which leaves status unknown until
        # the server's own turn/completed notification is drained.
        try:
            result = self._request("thread/read", {"threadId": self.thread_id, "includeTurns": True})
        except CodexRPCError:
            return notified or "unknown"
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError(f"thread/read returned no thread: {result}")
        turns = cast(dict[object, object], thread).get("turns")
        entries = cast(list[object], turns) if isinstance(turns, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_object = cast(dict[object, object], entry)
            if entry_object.get("id") == turn_id and isinstance(entry_object.get("status"), str):
                return str(entry_object["status"])
        return notified or "unknown"

    def interrupt(self, turn_id: str) -> dict[str, object]:
        if self.thread_id is None:
            raise RuntimeError("thread/start did not establish a thread id")
        record = self.turns.get(turn_id)
        if record is not None:
            record.status = "interrupt-requested"
        return self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id})

    def command_exec(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout_ms: int = 20000,
        sandbox_policy: dict[str, object] | None = DANGEROUS_SANDBOX_POLICY,
    ) -> dict[str, object]:
        """Direct native terminal execution through the app-server, without any thread/turn."""
        if self.expired:
            raise AttemptExpired("controller refuses new native terminal operations after Attempt expiry")
        params: dict[str, object] = {"command": command, "timeoutMs": timeout_ms}
        if cwd is not None:
            params["cwd"] = cwd
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        result = self._request("command/exec", params)
        self.command_execs.append({"command": command, "result": result})
        return result

    # -- authority ---------------------------------------------------------------------
    def grant(self, grant_id: str, op: str, ttl_seconds: float = 60.0) -> None:
        self.relay.grant(grant_id, op, ttl_seconds=ttl_seconds)

    def revoke(self, grant_id: str) -> None:
        self.relay.revoke(grant_id)

    def expire(self) -> None:
        """Controller-owned expiry: no new external operation is dispatched after this."""
        self.expired = True

    def relay_events(self) -> tuple[Any, ...]:
        return self.relay.events()

    # -- observation, termination, settlement -------------------------------------------
    def podman_state(self) -> str:
        return container_state(self.toolchain, self.container)

    def stop(self, grace_seconds: int = 5) -> str:
        return stop_container(self.toolchain, self.container, grace_seconds=grace_seconds)

    def scan_marker(self, marker: str) -> list[int]:
        return scan_host_for_marker(marker)

    def wait_settled(self, marker: str, timeout_seconds: float = 20.0) -> tuple[bool, list[int]]:
        return wait_settled(marker, timeout_seconds=timeout_seconds)

    def worker_observations(self) -> dict[str, Any]:
        """Read the launcher's observation file without trusting worker-controlled paths.

        The worker can replace or symlink any workspace file after launch, so this fixed
        path is read with the fail-closed reader (no symlink following, regular files
        only, bounded); a missing file means pre-exec observations were never written.
        """
        path = self.resources.workspace / "worker-observations.json"
        try:
            data = read_bounded_regular(path)
        except FileNotFoundError:
            return {}  # pre-exec observations were never written: recorded as absence only
        # Any other refusal (planted symlink, non-regular replacement, oversized or
        # growing file) raises: worker tampering with the evidence path stays visible.
        return cast(dict[str, Any], json.loads(data.decode()))

    def close(self) -> None:
        if self.connection is not None:
            try:
                self._drain_notifications()
            except (OSError, TimeoutError, OverflowError, CodexRPCError):
                pass  # A failing drain during close leaves owned-group settlement to close().
            self.connection.close()
            self.owned_group_settled = self.connection.owned_group_settled
            self.connection = None
        self.relay.stop()

    def dispose(self) -> None:
        try:
            if self.podman_state() == "exists":
                self.stop(grace_seconds=2)
        except Exception:  # noqa: BLE001, S110 - best-effort disposal after native assertions.
            pass
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 - best-effort disposal after native assertions.
            pass

    # -- collection ----------------------------------------------------------------------
    def collect(self) -> AttemptCollection:
        """Fail-closed post-settlement collection, split by trust class.

        Publishable candidate artifacts come only from the controller-declared
        ``candidate/`` root; their exact bytes are durably preserved under the
        controller-owned ``collected/`` tree and digest-bound to each record, making the
        published set a collected artifact snapshot rather than a bare digest manifest.
        Everything else in the workspace — the synthetic ``CODEX_HOME`` (sessions,
        rollouts, config), the mock backend records, the attempt spec and launcher
        observations, and any worker scratch — is private runtime/evidence state: it is
        inventoried with the same fail-closed walker, retained in place under controller
        authority, and never presented as candidate artifacts. The worker controls every
        path in either class, so no lookup follows a symbolic link and no entry is
        silently skipped; refusals are recorded and are failures, not passes.
        """
        if self.podman_state() == "exists":
            raise RuntimeError("collection requires observed container settlement first")
        candidate_root = self.resources.workspace / CANDIDATE_DIRNAME
        publishable = collect_directory(candidate_root, sink=stored_bytes_sink(self.collected_root))
        private = collect_directory(self.resources.workspace, exclude=frozenset({CANDIDATE_DIRNAME}))
        return AttemptCollection(publishable=publishable, private=private, collected_root=self.collected_root)

    @property
    def collected_root(self) -> Path:
        return self.root / "collected"

    def dump_native_evidence(self, path: Path) -> None:
        self.evidence.host_observations = {
            "container": self.container,
            "wrapper_script_sha256": sha256_path(self.wrapper),
            "thread_start_params": json.dumps(THREAD_START_PARAMS, sort_keys=True),
            "thread_start_result": json.dumps(self.thread_start_result, sort_keys=True, default=str),
            "thread_id": self.thread_id or "",
            "turns": json.dumps(
                {key: dataclasses.asdict(value) for key, value in self.turns.items()}, sort_keys=True, default=str
            ),
            "command_execs": json.dumps(self.command_execs, sort_keys=True, default=str),
            "notifications_tail": json.dumps(self.notifications[-50:], sort_keys=True, default=str),
            "terminal": self.terminal,
            "worker_observations": json.dumps(self.worker_observations(), sort_keys=True),
            "method_inventory": json.dumps(self.inventory.to_json_dict(), sort_keys=True),
        }
        self.evidence.dump(path)


__all__ = [
    "AttemptCollection",
    "AttemptExpired",
    "CANDIDATE_DIRNAME",
    "CodexAttempt",
    "THREAD_START_PARAMS",
    "exec_step",
    "final_step",
    "write_scenario",
]
