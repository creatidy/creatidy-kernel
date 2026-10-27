# SPDX-License-Identifier: Apache-2.0
"""Opt-in finite Codex/Forgejo composition; caller supplies scoped, pinned adapters.

No credential loading or sandbox is implemented here. The owner must use a disposable
trusted-development account without broad owner credentials; Codex workspace-write is
not hostile-worker isolation. A connection's RPC timeout must be finite as well.
"""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.boot_clock import read_boot_clock
from creatidy_kernel.adapters.codex_runtime import (
    CodexConnection,
    CodexInputs,
    CodexRuntime,
    owned_group_callbacks,
)
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
from creatidy_kernel.core.deadlines import BootClockReading
from creatidy_kernel.core.domain import ActivateProgram, AttemptSpec, ProgramStatus, WorkUnitStatus
from creatidy_kernel.core.execution import (
    Activity,
    Artifact,
    ArtifactManifest,
    Candidate,
    ExecutionConflict,
    ExecutionRequest,
    OperationKey,
    RuntimeIdentity,
    TrustMode,
    UnsupportedExecution,
    WorkspaceHandle,
    WorkspaceSpec,
)
from creatidy_kernel.core.execution import (
    Presence as RuntimePresence,
)
from creatidy_kernel.core.forge import Presence, Reference
from creatidy_kernel.core.resources import Allocation
from creatidy_kernel.core.work_budget import BudgetRefused, WorkBudgetPolicy, WorkUncertain
from creatidy_kernel.ports.allocation import (
    decode_allocation,
    decode_runtime_receipt_with_group,
    identity_matches,
    is_legacy_allocation,
    load_attempt_inputs,
)
from creatidy_kernel.ports.application import advance_work_unit, manifest_bytes
from creatidy_kernel.ports.forge import Forge
from creatidy_kernel.ports.program_store import OperationRecord
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
    boot_clock: Callable[[], BootClockReading | None] = read_boot_clock,
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
        SQLiteProgramStore(directory / "kernel.sqlite3", boot_clock=boot_clock) as store,
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
        budget = WorkBudgetPolicy.basic(
            program.spec.budget.max_attempts,
            program.spec.budget.max_active_attempts,
            verifications=program.spec.budget.max_attempts * len(POLICY.checks),
            retained_bytes=64 * 1024,
            auxiliary=1,
        )
        store.bind_work_budget("reference", repository.value, budget, budget, deadline)
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

        def work_authorized() -> bool:
            try:
                store.check_work_authority("reference")
            except BudgetRefused:
                return False
            return True

        def authorize(request: ExecutionRequest) -> bool:
            try:
                store.check_work_dispatch(request.operation.operation_id)
            except BudgetRefused:
                return False
            operation = store.operation(request.operation.operation_id)
            return (
                work_authorized()
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

        retire_owned_group, observe_owned_group, observe_owned_group_reference = owned_group_callbacks(connection)
        retirement_attempted = False

        def retire_once() -> None:
            nonlocal retirement_attempted
            if not retirement_attempted and retire_owned_group is not None:
                retirement_attempted = True
                if observe_owned_group is None or observe_owned_group() is not True:
                    retire_owned_group()

        runtime = CodexRuntime(
            connection,
            version=version,
            resolve=resolve,
            authorize=authorize,
            collect=collect,
            supported_efforts=supported_efforts,
            retire=retire_once if retire_owned_group is not None else None,
            settled=observe_owned_group,
            group_reference=observe_owned_group_reference,
            clock=lambda: int(time.time()),
        )

        def original_receipt(
            attempt: AttemptSpec, operation: OperationRecord, data: bytes | None
        ) -> tuple[Allocation, str, RuntimeIdentity, str | None]:
            if data is None:
                raise WorkUncertain("original runtime receipt is unavailable")
            if (
                operation.operation_id != f"runtime:{attempt.attempt_id}"
                or store.load("reference").attempt(attempt.attempt_id).spec != attempt
            ):
                raise WorkUncertain("runtime Operation differs from originating Attempt")
            allocation_data, _ = load_attempt_inputs(store, attempt)
            selected = decode_allocation(allocation_data)
            handle, fence, identity, group = decode_runtime_receipt_with_group(data)
            parts = handle.removeprefix("codex:").split(":") if handle.startswith("codex:") else []
            if (
                selected.runtime_id != "codex"
                or fence != operation.fence
                or operation.attempts == 0
                or operation.retry_proof is not None
                or operation.status not in {"dispatched", "unknown", "accepted", "terminal"}
                or operation.accepted_reference not in (None, handle)
                or len(parts) != 2
                or not all(parts)
                or attempt.agent_definition_reference is None
                or not identity_matches(
                    identity,
                    selected,
                    attempt.agent_definition_reference,
                    require_resolved=False,
                    legacy=is_legacy_allocation(allocation_data),
                )
            ):
                raise WorkUncertain("runtime receipt differs from original Operation/fence/Attempt bindings")
            return selected, handle, identity, group

        def restore(request: ExecutionRequest, handle: str) -> None:
            operation = store.operation(request.operation.operation_id)
            try:
                selected, original_handle, original_identity, original_group = original_receipt(
                    request.attempt, operation, store.find_artifact(operation.operation_id, "runtime-receipt")
                )
            except (ValueError, OperationConflict) as error:
                raise WorkUncertain("original runtime receipt cannot be verified") from error
            if (
                request.operation
                != OperationKey(operation.operation_id, operation.effect_key, operation.request_digest)
                or request.fence != operation.fence
                or handle != original_handle
                or request.allocation != selected
            ):
                raise WorkUncertain("restoration differs from original runtime receipt")
            if (
                original_group is None
                and observe_owned_group_reference is not None
                and observe_owned_group_reference() is not None
            ):
                # Missing historical group proof cannot bind a newly supplied owner.
                existing = runtime.reconcile(request.operation, handle)
                if existing.presence is not RuntimePresence.FOUND:
                    raise WorkUncertain("original owned-group binding is unavailable")
            try:
                lookup = runtime.restore(
                    request, handle, owned_group_reference=original_group, receipt_identity=original_identity
                )
            except (ExecutionConflict, UnsupportedExecution) as error:
                raise WorkUncertain("original runtime restoration cannot be verified") from error
            if lookup.presence is not RuntimePresence.FOUND or lookup.handle != handle:
                raise WorkUncertain("original runtime restoration is unverified; no replacement-group adoption")

        status = "budget_exhausted"
        authority_refused = bool(store.operations("interruption:live-authority"))
        restoration_uncertain = bool(store.operations("interruption:live-restoration"))
        used = len(store.operations("reference:live-poll:"))
        for index in range(used, max_observations + 1):
            if not work_authorized():
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
                    authorize_dispatch=work_authorized,
                    budget_subject=("reference", "reference:second"),
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
            try:
                existing_attempt = next(
                    (item for item in program.attempts if item.attempt_id == f"reference:{unit}"), None
                )
                if existing_attempt is not None:
                    prior = store.operation(f"runtime:{existing_attempt.attempt_id}")
                    if prior.attempts or prior.accepted_reference is not None:
                        try:
                            original_receipt(
                                existing_attempt.spec, prior, store.find_artifact(prior.operation_id, "runtime-receipt")
                            )
                        except ValueError as error:
                            raise WorkUncertain("original runtime receipt cannot be verified") from error
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
                    clock=lambda: int(time.time()),
                )
            except BudgetRefused:
                authority_refused = True
                status = "budget_exhausted"
                store.intent(
                    "interruption:live-authority",
                    "interruption:live-authority",
                    {"reason": "new work authority refused inside admission"},
                )
                break
            except WorkUncertain:
                restoration_uncertain = True
                status = "unknown"
                store.intent(
                    "interruption:live-restoration",
                    "interruption:live-restoration",
                    {"reason": "original runtime restoration unverified"},
                )
                break
            if status in {"unknown", "rejected", "identity_unavailable"}:
                break
        if status not in {"unknown", "rejected", "identity_unavailable"}:
            status = "budget_exhausted"
        store.revoke_work_budget("reference", "interruption:live-budget")
        owned_cleanup: list[dict[str, object]] = []
        for attempt in store.load("reference").attempts:
            operation = store.operation(f"runtime:{attempt.spec.attempt_id}")
            receipt_data: bytes | None = None
            try:
                receipt_data = store.find_artifact(operation.operation_id, "runtime-receipt")
                if (
                    operation.attempts == 0
                    and operation.status == "intent"
                    and operation.accepted_reference is None
                    and receipt_data is None
                ):
                    continue  # Original admission was never dispatched.
                selected, handle, receipt_identity, expected_group = original_receipt(
                    attempt.spec, operation, receipt_data
                )
                if operation.accepted_reference is None:
                    # Exactly the causal receipt replay used by ordinary admission:
                    # record a native return, never a candidate acceptance or dispatch.
                    store.record_transport(operation.operation_id, operation.fence, True)
                    operation = store.observe(
                        operation.operation_id, operation.fence, f"accepted:{handle}", "accepted", reference=handle
                    )
            except (ValueError, WorkUncertain):
                restoration_uncertain = True
                store.intent(
                    "interruption:live-restoration",
                    "interruption:live-restoration",
                    {"reason": "original runtime restoration unverified"},
                )
                unknown: dict[str, object] = {
                    "version": 1,
                    "operation": operation.operation_id,
                    "request_digest": operation.request_digest,
                    "runtime_fence": operation.fence,
                    "attempt": attempt.spec.digest,
                    "handle": operation.accepted_reference,
                    "receipt_digest": None
                    if receipt_data is None
                    else "sha256:" + hashlib.sha256(receipt_data).hexdigest(),
                    "owned_group_settled": None,
                    "result_reconciled": False,
                    "reason": "original runtime receipt/bindings unavailable or invalid",
                }
                data = manifest_bytes(unknown)
                store.finalize_artifact(
                    operation.operation_id, f"stop-uncertain:{hashlib.sha256(data).hexdigest()}", data
                )
                owned_cleanup.append(unknown)
                continue
            if operation.accepted_reference is not None:
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
                cancellation_id = f"cancel:{attempt.spec.attempt_id}"
                cancellation = store.intent(
                    cancellation_id,
                    cancellation_id,
                    {"operation": operation.operation_id, "handle": operation.accepted_reference},
                )
                cached = store.find_artifact(cancellation_id, "owned-group-settlement")
                if cached is not None:
                    proof = cast(dict[str, object], json.loads(cached))
                    if (
                        proof.get("operation") == operation.operation_id
                        and proof.get("effect_key") == operation.effect_key
                        and proof.get("request_digest") == operation.request_digest
                        and proof.get("runtime_fence") == operation.fence
                        and proof.get("cancellation_fence") == cancellation.fence
                        and proof.get("handle") == operation.accepted_reference
                        and proof.get("attempt") == attempt.spec.digest
                        and expected_group is not None
                        and proof.get("owned_group_reference") == expected_group
                        and proof.get("owned_group_settled") is True
                        and proof.get("result_reconciled") is True
                    ):
                        owned_cleanup.append(proof)
                        continue
                try:
                    current_group = (
                        observe_owned_group_reference() if observe_owned_group_reference is not None else None
                    )
                except (OSError, RuntimeError, ValueError):
                    current_group = None
                group_matches = expected_group is not None and current_group == expected_group
                if not group_matches and (expected_group is not None or retire_owned_group is not None):
                    restoration_uncertain = True
                    store.intent(
                        "interruption:live-restoration",
                        "interruption:live-restoration",
                        {"reason": "original runtime restoration unverified"},
                    )
                    owned_cleanup.append(
                        {
                            "operation": operation.operation_id,
                            "owned_group_settled": None,
                            "reason": "original owned group unavailable; no adoption",
                        }
                    )
                    continue
                restored = True
                try:
                    try:
                        restore(request, operation.accepted_reference)
                    except WorkUncertain:
                        restored = False
                        restoration_uncertain = True
                        store.intent(
                            "interruption:live-restoration",
                            "interruption:live-restoration",
                            {"reason": "original runtime restoration unverified"},
                        )
                    if cancellation.attempts == 0:
                        fence = store.claim(cancellation_id, now=int(time.time()), lease_seconds=1)
                        # Claim/observation deduplicate interrupt delivery, not physical retirement.
                        store.observe(cancellation_id, fence, f"uncertain:{cancellation_id}", "unknown")
                        if restored:
                            try:
                                runtime.cancel(operation.accepted_reference)
                            except (OSError, RuntimeError):
                                pass
                finally:
                    if group_matches:
                        try:
                            retire_once()
                        except (OSError, RuntimeError):
                            pass
                observation = runtime.observe(operation.accepted_reference, now=int(time.time())) if restored else None
                if observation is not None:
                    observation_data = manifest_bytes(asdict(observation))
                    store.finalize_artifact(
                        cancellation_id,
                        f"observation:{hashlib.sha256(observation_data).hexdigest()}",
                        observation_data,
                    )
                if observation is not None and observation.activity is Activity.TERMINAL:
                    # A terminal target is evidence of stopping, not of interrupt delivery or acceptance.
                    store.observe(
                        operation.operation_id,
                        operation.fence,
                        f"terminal:{operation.operation_id}",
                        "terminal",
                        reference=operation.accepted_reference,
                    )
                try:
                    settled = observe_owned_group() if group_matches and observe_owned_group is not None else None
                except (OSError, RuntimeError, ValueError):
                    settled = None
                reconciled = (
                    observation is not None
                    and observation.activity is Activity.TERMINAL
                    and observation.handle == operation.accepted_reference
                    and group_matches
                    and observation.identity == receipt_identity
                )
                proof = cast(
                    dict[str, object],
                    {
                        "version": 1,
                        "operation": operation.operation_id,
                        "effect_key": operation.effect_key,
                        "request_digest": operation.request_digest,
                        "runtime_fence": operation.fence,
                        "cancellation_fence": store.operation(cancellation_id).fence,
                        "attempt": attempt.spec.digest,
                        "handle": operation.accepted_reference,
                        "owned_group_reference": expected_group,
                        "owned_group_settled": settled,
                        "result_reconciled": reconciled,
                        "descendants": "unproven",
                    },
                )
                proof_data = manifest_bytes(proof)
                store.finalize_artifact(
                    cancellation_id, f"owned-observation:{hashlib.sha256(proof_data).hexdigest()}", proof_data
                )
                if group_matches and settled is True and reconciled:
                    reference = store.finalize_artifact(cancellation_id, "owned-group-settlement", proof_data)
                    store.record_work_settlement(operation.operation_id, reference)
                owned_cleanup.append(proof)
        store.intent(
            "interruption:live-budget",
            "interruption:live-budget",
            {"reason": "finite observation/deadline exit; cancellation requested, settlement not assumed"},
        )
        result = reference_export(store)
        result.update(
            condition="cancel_uncertain"
            if (authority_refused or restoration_uncertain or retire_owned_group is not None)
            and any(
                item.get("owned_group_settled") is not True or item.get("result_reconciled") is not True
                for item in owned_cleanup
            )
            else status,
            mode="live",
            isolation="not attested",
            interruption="finite observation/deadline exit; cancellation requested, settlement not assumed",
            owned_cleanup=owned_cleanup,
        )
        return result
