# SPDX-License-Identifier: Apache-2.0
"""Opt-in finite Codex/Forgejo composition; caller supplies scoped, pinned adapters.

No credential loading or sandbox is implemented here. The owner must use a disposable
trusted-development account without broad owner credentials; Codex workspace-write is
not hostile-worker isolation. A connection's RPC timeout must be finite as well.
"""

import hashlib
import time
from dataclasses import asdict
from pathlib import Path

from creatidy_kernel.adapters.codex_runtime import CodexConnection, CodexInputs, CodexRuntime
from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
from creatidy_kernel.adapters.reference import (
    EXPECTED,
    POLICY,
    ReferenceCollector,
    prepare_reference_directory,
    reference_commit,
    reference_export,
    reference_git,
    reference_request,
    reference_spec,
)
from creatidy_kernel.adapters.reference_forge import deliver_reference_pr
from creatidy_kernel.adapters.sqlite_store import OperationConflict, ProgramNotFound, SQLiteProgramStore
from creatidy_kernel.core.domain import ActivateProgram, ProgramStatus, WorkUnitStatus
from creatidy_kernel.core.execution import (
    Activity,
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionRequest,
    OperationKey,
    RuntimeIdentity,
    TrustMode,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.forge import Presence, Reference
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.ports.allocation import load_allocation
from creatidy_kernel.ports.application import advance_work_unit, manifest_bytes
from creatidy_kernel.ports.forge import Forge
from creatidy_kernel.ports.resources import ResourceAllocator


def run_live_reference(
    directory: Path,
    *,
    owner_approved: bool,
    trusted_development_acknowledged: bool,
    connection: CodexConnection,
    version: str,
    allocation: Allocation | None = None,
    forge: Forge,
    repository: Reference,
    object_source: Path,
    deadline: int,
    max_observations: int,
    allocator: ResourceAllocator | None = None,
    supported_efforts: frozenset[tuple[str, str, str]] = frozenset(),
) -> dict[str, object]:
    """Run at most two native turns and bounded observations, interrupt on budget exit.

    Supply a version-pinned CodexStdio connection with a finite request timeout and a
    scoped ForgejoForge. Neither a missing reply nor a lost thread is retried. Reuse
    the same directory, deadline and adapter bindings for recovery. Supply exactly
    one fixed allocation or external allocator; existing Attempts use durable inputs.
    The owner must seed the disposable remote develop branch with the deterministic
    empty reference commit, and bind ConditionalGitTransport to object_source.
    """
    now = int(time.time())
    if (allocation is None) == (allocator is None):
        raise ValueError("supply exactly one allocation or allocator")
    resource_allocator = FixedAllocator(allocation) if allocation is not None else allocator
    assert resource_allocator is not None  # noqa: S101 - established by the exclusive input check.
    if owner_approved is not True or trusted_development_acknowledged is not True:
        raise ValueError("explicit owner approval and trusted-development acknowledgment required")
    if (
        type(deadline) is not int
        or deadline > now + 3600
        or (deadline <= now and not (directory / "kernel.sqlite3").is_file())
    ):
        raise ValueError("owner deadline must be within one hour")
    if type(max_observations) is not int or not 1 <= max_observations <= 100:
        raise ValueError("finite observation budget must be between 1 and 100")
    directory = directory.resolve()
    if not object_source.is_absolute() or object_source.is_symlink() or not (object_source / "HEAD").is_file():
        raise ValueError("explicit controller-owned bare object source required")
    if reference_git(object_source, "rev-parse", "--is-bare-repository") != "true":
        raise ValueError("controller object source must be bare")
    workspace_path = prepare_reference_directory(directory)
    if object_source.resolve().is_relative_to(workspace_path):
        raise ValueError("controller object source must be outside the worker workspace")
    base = reference_commit(workspace_path, b"")
    with (
        SQLiteProgramStore(directory / "kernel.sqlite3") as store,
        SQLiteProgramStore(directory / "runtime.sqlite3") as artifacts,
    ):
        store.intent(
            "reference:mode",
            "reference:mode",
            {
                "mode": "live",
                "deadline": deadline,
                "max_observations": max_observations,
                "version": version,
                "repository": repository.value,
                **(
                    {"model": allocation.model_id, "provider": allocation.provider_id}
                    if allocation is not None
                    else {"allocator": "external"}
                ),
                "object_source": str(object_source),
                **({"supported_efforts": sorted(supported_efforts)} if supported_efforts else {}),
            },
        )
        try:
            program = store.load("reference")
        except ProgramNotFound:
            program = store.create(reference_spec(), "create")
        if now < deadline and program.status is not ProgramStatus.COMPLETED:
            remote_base = forge.branch(repository, "develop")
            if remote_base.presence is not Presence.FOUND or remote_base.revision != Reference(f"forgejo:{base}"):
                raise ValueError("disposable remote develop must equal the owner-seeded reference base")
        if program.status is ProgramStatus.DRAFT:
            program = store.admit("reference", "activate", ActivateProgram(program.revision, "owner"))

        def resolve(request: ExecutionRequest) -> CodexInputs:
            selected = request.allocation
            if selected is None:
                raise ValueError("native reference requires the durable allocation")
            unit = request.attempt.work_unit_id
            predecessor = ""
            if unit == "second":
                data = store.artifact("runtime:reference:first", "output")
                if (
                    request.attempt.effective_inputs[0].reference
                    != "artifact:sha256:" + hashlib.sha256(data).hexdigest()
                ):
                    raise ValueError("accepted predecessor input mismatch")
                predecessor = f"Accepted predecessor bytes: {data.decode()!r}. "
            return CodexInputs(
                request.workspace.key,
                request.context_reference,
                request.allocation_reference,
                request.capability_reference,
                str(workspace_path),
                predecessor + f"Write only result.txt with exactly these UTF-8 bytes: {EXPECTED[unit]!r}. "
                "Do not access parent directories, credentials, network, or change Git configuration.",
                selected.model_id,
                selected.provider_id,
                reasoning_effort=selected.reasoning_effort,
            )

        def authorize(request: ExecutionRequest) -> bool:
            operation = store.operation(request.operation.operation_id)
            return (
                int(time.time()) < deadline
                and operation.status == "dispatched"
                and operation.fence == request.fence
                and operation.request_digest == request.operation.request_digest
            )

        def collect(request: ExecutionRequest) -> Candidate | None:
            path = workspace_path / "result.txt"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
                return None
            content = path.read_bytes()
            artifacts.intent(
                request.operation.operation_id,
                request.operation.effect_key,
                {"digest": request.operation.request_digest},
            )
            digest = artifacts.finalize_artifact(request.operation.operation_id, "output", content)
            return Candidate(
                request.attempt.attempt_id,
                request.attempt.digest,
                ArtifactManifest(request.workspace.key, (Artifact("result.txt", digest),)),
            )

        runtime = CodexRuntime(
            connection,
            version=version,
            resolve=resolve,
            authorize=authorize,
            collect=collect,
            supported_efforts=supported_efforts,
        )

        def restore(request: ExecutionRequest, handle: str) -> None:
            runtime.restore(request, handle)

        status = "budget_exhausted"
        used = len(store.operations("reference:live-poll:"))
        for index in range(used, max_observations + 1):
            if int(time.time()) >= deadline:
                break
            program = store.load("reference")
            pending = [
                unit for unit in ("first", "second") if program.state(unit).status is not WorkUnitStatus.SATISFIED
            ]
            if not pending:
                accepted = reference_request(store, "acceptance:reference:second")
                reference_export(store)  # Refuse legacy acceptance with incomplete identity evidence.
                published = reference_commit(object_source, b"")
                for node in ("first", "second"):
                    published = reference_commit(
                        object_source, store.artifact(f"runtime:reference:{node}", "output"), published
                    )
                    expected_head = reference_request(store, f"acceptance:reference:{node}")["head"]
                    if published != expected_head:
                        raise ValueError("controller object source differs from accepted Git subject")
                receipt = deliver_reference_pr(
                    store,
                    repository_path=workspace_path,
                    head=str(accepted["head"]),
                    acceptance_reference=str(accepted["manifest"]),
                    forge=forge,
                    repository=repository,
                )
                if receipt.get("status") == "accepted":
                    try:
                        store.artifact("reference:pr", "export")
                    except OperationConflict:
                        store.finalize_artifact("reference:pr", "export", manifest_bytes(receipt))
                result = reference_export(store)
                result.update(pr=receipt, trust_mode="trusted_development", isolation="not attested", mode="live")
                return result
            if index == max_observations:
                break
            unit = pending[0]
            store.intent(f"reference:live-poll:{index}", f"reference:live-poll:{index}", {"index": index, "unit": unit})
            workspace = WorkspaceHandle(
                f"workspace:{unit}",
                WorkspaceSpec(
                    str(workspace_path),
                    base,
                    (),
                    "reference:v1",
                    "codex-workspace-write",
                    TrustMode.TRUSTED_DEVELOPMENT,
                ),
            )
            status = advance_work_unit(
                store,
                program,
                unit,
                allocator=resource_allocator,
                runtime=runtime,
                workspace=workspace,
                collector=ReferenceCollector(artifacts, store, workspace_path, base),
                policy=POLICY,
                now=int(time.time()),
                restore=restore,
            )
            if status in {"unknown", "rejected", "identity_unavailable"}:
                break
        if status not in {"unknown", "rejected", "identity_unavailable"}:
            status = "budget_exhausted"
        for attempt in store.load("reference").attempts:
            operation = store.operation(f"runtime:{attempt.spec.attempt_id}")
            if operation.accepted_reference is not None and operation.status != "terminal":
                selected = load_allocation(store, attempt.spec)
                request = ExecutionRequest(
                    OperationKey(operation.operation_id, operation.effect_key, operation.request_digest),
                    attempt.spec,
                    WorkspaceHandle(
                        str(attempt.spec.workspace_reference),
                        WorkspaceSpec(
                            str(workspace_path),
                            base,
                            (),
                            "reference:v1",
                            "codex-workspace-write",
                            TrustMode.TRUSTED_DEVELOPMENT,
                        ),
                    ),
                    str(attempt.spec.context_reference),
                    str(attempt.spec.allocation_reference),
                    "owner-approved-reference",
                    RuntimeIdentity(
                        selected.model_id,
                        None,
                        None,
                        "reference:v1",
                        requested_provider=selected.provider_id,
                        requested_effort=selected.reasoning_effort,
                    ),
                    operation.fence,
                    allocation=selected,
                )
                runtime.restore(request, operation.accepted_reference)
                cancellation_id = f"cancel:{attempt.spec.attempt_id}"
                cancellation = store.intent(
                    cancellation_id,
                    cancellation_id,
                    {"operation": operation.operation_id, "handle": operation.accepted_reference},
                )
                if cancellation.attempts == 0:
                    fence = store.claim(cancellation_id, now=int(time.time()), lease_seconds=1)
                    # The adapter cannot prove interrupt delivery, even when the RPC returns.
                    store.observe(cancellation_id, fence, f"uncertain:{cancellation_id}", "unknown")
                    runtime.cancel(operation.accepted_reference)
                observation = runtime.observe(operation.accepted_reference, now=int(time.time()))
                observation_data = manifest_bytes(asdict(observation))
                store.finalize_artifact(
                    cancellation_id,
                    f"observation:{hashlib.sha256(observation_data).hexdigest()}",
                    observation_data,
                )
                if observation.activity is Activity.TERMINAL:
                    # A terminal target is evidence of stopping, not of interrupt delivery or acceptance.
                    store.observe(
                        operation.operation_id,
                        operation.fence,
                        f"terminal:{operation.operation_id}",
                        "terminal",
                        reference=operation.accepted_reference,
                    )
        store.intent(
            "interruption:live-budget",
            "interruption:live-budget",
            {"reason": "finite observation/deadline exit; cancellation requested, settlement not assumed"},
        )
        result = reference_export(store)
        result.update(
            condition=status,
            mode="live",
            isolation="not attested",
            interruption="finite observation/deadline exit; cancellation requested, settlement not assumed",
        )
        return result
