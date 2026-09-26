# SPDX-License-Identifier: Apache-2.0
"""The K3 synthetic boundary uses K2B intent and K1 Attempt truth, not a second journal."""

from pathlib import Path

import pytest
from test_sqlite_store import spec

from creatidy_kernel.adapters.fake_authority import FakeAuthorityBroker
from creatidy_kernel.adapters.fake_execution import FakeRuntime, FakeWorkspace
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.authority import AuthorityDenied, AuthorityGrant, OperationIntent, Principal
from creatidy_kernel.core.domain import (
    ActivateProgram,
    AttemptSpec,
    AttemptStatus,
    FinishAttempt,
    PrepareAttempt,
    StartAttempt,
    WorkUnitStatus,
)
from creatidy_kernel.core.execution import (
    Activity,
    Artifact,
    Candidate,
    ExecutionRequest,
    OperationKey,
    RuntimeIdentity,
    TrustMode,
    WorkerScope,
    WorkspaceSpec,
)

pytest_plugins = ["test_sqlite_store"]


def test_attempt_to_untrusted_candidate_preserves_journal_and_authority(sqlite_tmp_path: Path) -> None:
    workspace_root = sqlite_tmp_path / "workspace"
    workspace_root.mkdir()
    workspace = FakeWorkspace()
    handle = workspace.materialize(
        "workspace-1",
        WorkspaceSpec("repo:1", "base:1", (), "sha256:toolchain", "synthetic", TrustMode.TRUSTED_DEVELOPMENT),
    )
    runtime = FakeRuntime()
    broker = FakeAuthorityBroker()
    owner = Principal("owner", "owner")
    worker = Principal("attempt-1", "worker")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        program = store.create(spec(), "create")
        program = store.admit(program.program_id, "activate", ActivateProgram(program.revision, "owner"))
        attempt = AttemptSpec(
            "attempt-1",
            program.program_id,
            "first",
            program.spec.revision,
            program.spec.digest,
            program.resolved_inputs("first"),
            "allocation:1",
            "agent:v1",
            handle.key,
            "context:1",
        )
        program = store.admit(program.program_id, "prepare", PrepareAttempt(program.revision, "owner", attempt))
        program = store.admit_with_intent(
            program.program_id,
            "start",
            StartAttempt(program.revision, "owner", attempt.attempt_id),
            "operation-1",
            "effect-1",
            {"attempt": attempt.digest, "workspace": handle.key},
        )
        assert program.attempt(attempt.attempt_id).status is AttemptStatus.EXECUTING
        operation = store.operation("operation-1")
        grant = AuthorityGrant(
            "grant-1",
            "owner",
            attempt.digest,
            program.program_id,
            program.spec.digest,
            handle.spec.repository,
            workspace_root,
            frozenset({workspace_root / "output"}),
            frozenset(),
            frozenset({"write", "publish_candidate"}),
            100,
            single_use=True,
        )
        broker.issue(owner, grant)
        intent = OperationIntent(
            operation.operation_id,
            operation.effect_key,
            operation.request_digest,
            "publish_candidate",
            handle.spec.repository,
            path=Path("output/result"),
        )
        assert broker.check(worker, grant.grant_id, attempt, intent, now=1) == intent
        with pytest.raises(AuthorityDenied):
            broker.check(
                worker,
                grant.grant_id,
                attempt,
                OperationIntent(
                    operation.operation_id, operation.effect_key, "changed", "publish_candidate", handle.spec.repository
                ),
                now=1,
            )
        fence = store.claim(operation.operation_id, now=1, lease_seconds=5)
        request = ExecutionRequest(
            OperationKey(operation.operation_id, operation.effect_key, operation.request_digest),
            attempt,
            handle,
            "context:1",
            "allocation:1",
            grant.grant_id,
            RuntimeIdentity("runtime:1", "runtime:1", "runtime:1", "agent:v1"),
            fence,
        )
        execution = runtime.start(request)
        store.record_transport(operation.operation_id, fence, True)
        store.observe(operation.operation_id, fence, "accepted", "accepted", reference=execution)
        runtime.progress(execution, now=2, fresh_for=20, waiting=True)
        assert runtime.observe(execution, now=18).activity is Activity.WAITING
        store.observe(operation.operation_id, fence, "waiting", "waiting", reference=execution)
        scope = WorkerScope(
            handle.key,
            attempt.attempt_id,
            fence,
            grant.expires_at,
            frozenset({"output"}),
            grant.operations,
            grant.network,
            frozenset(),
        )
        manifest = workspace.publish_artifact(handle, scope, Artifact("output/result", "sha256:result"), now=3)
        candidate = Candidate(attempt.attempt_id, attempt.digest, manifest)
        runtime.publish(execution, scope, candidate, now=3)
        assert runtime.candidate(execution) == candidate
        assert (
            store.observe(operation.operation_id, fence, "terminal", "terminal", reference=execution).status
            == "terminal"
        )
        with pytest.raises(OperationConflict):
            store.observe(operation.operation_id, fence - 1, "stale", "terminal", reference=execution)
        program = store.admit(
            program.program_id, "finish", FinishAttempt(program.revision, "owner", attempt.attempt_id)
        )
        assert program.attempt(attempt.attempt_id).status is AttemptStatus.FINISHED
        assert program.state("first").status is WorkUnitStatus.READY
