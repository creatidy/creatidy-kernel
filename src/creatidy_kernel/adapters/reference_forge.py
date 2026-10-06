# SPDX-License-Identifier: Apache-2.0
"""One bounded exact-head PR delivery, with a durable offline forge fixture."""

import json
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import cast

from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forge_refs import AGitPush, fresh_agit_topic, oid
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.sqlite_store import OperationConflict, SQLiteProgramStore
from creatidy_kernel.core.execution import OperationKey
from creatidy_kernel.core.forge import Effect, EffectStatus, ForgeConflict, Receipt, Reference
from creatidy_kernel.ports.forge import Forge

PR_OPERATION = "reference:pr"
SYNTHETIC_REPOSITORY = Reference("forgejo:synthetic/reference")


def _artifact(store: SQLiteProgramStore, operation: str, name: str) -> bytes | None:
    try:
        return store.artifact(operation, name)
    except OperationConflict as error:
        if str(error) != "unknown artifact reference":
            raise
        return None


class DurableSyntheticForge(SyntheticForgeTransport):
    """Synthetic remote reality survives disposal of the controller and adapter."""

    def __init__(self, store: SQLiteProgramStore) -> None:
        super().__init__(SYNTHETIC_REPOSITORY)
        self.store = store
        data = _artifact(store, "synthetic:pr", "pull")
        if data is not None:
            pull = cast(dict[str, object], json.loads(data))
            self.pulls = [pull]
            self.topics = {str(pull["topic"])}
            self.pushes = self.posts = 1

    def create_agit_pr(
        self, repository: Reference, base: str, topic: str, revision: Reference, title: str, description: str
    ) -> AGitPush:
        result = super().create_agit_pr(repository, base, topic, revision, title, description)
        if result.status is EffectStatus.ACCEPTED:
            self.store.intent("synthetic:pr", "synthetic:pr", {"topic": topic, "head": revision.value})
            # This immutable remote fact is the synthetic effect, committed before the reply.
            self.store.finalize_artifact("synthetic:pr", "pull", json.dumps(self.pulls[0], sort_keys=True).encode())
        return result


def deliver_reference_pr(
    store: SQLiteProgramStore,
    *,
    repository_path: Path,
    head: str,
    acceptance_reference: str,
    fault: str | None = None,
    forge: Forge | None = None,
    repository: Reference = SYNTHETIC_REPOSITORY,
    base_branch: str = "develop",
    operation_id: str = PR_OPERATION,
    schema: str = "reference-pr-v1",
    title: str = "Bounded two-node reference result",
    body: str | None = None,
    authorize_dispatch: Callable[[], bool] = lambda: True,
) -> dict[str, object]:
    """Called by the trusted application only after exact-head acceptance.

    A supplied live Forge owns its own bounded authorization and transport. The default
    uses Forgejo's real normalization against a durable synthetic server, without network.
    Unknown creation without a retained reference stays unknown; there is no blind retry.
    Dispatch authority is checked before claim and apply, not required for read-only reconciliation.
    """
    oid(head)
    if not acceptance_reference:
        raise ForgeConflict("PR creation requires an accepted-result reference")
    if forge is None and repository != SYNTHETIC_REPOSITORY:
        raise ForgeConflict("synthetic forge cannot represent a live repository")
    if body is None:
        body = f"Accepted result: {acceptance_reference}. No merge or deployment authorized."
    mode = "synthetic" if forge is None else "live"
    request: dict[str, object]
    try:
        operation = store.operation(operation_id)
        request = cast(dict[str, object], json.loads(operation.request_json)["request"])
    except OperationConflict as error:
        if str(error) != "unknown operation":
            raise
        request = {
            "schema": schema,
            "mode": mode,
            "repository": repository.value,
            "head": head,
            "base_branch": base_branch,
            "acceptance_reference": acceptance_reference,
            "topic": fresh_agit_topic(),
        }
        operation = store.intent(operation_id, operation_id, request)
    expected = (mode, repository.value, head, base_branch, acceptance_reference)
    if (
        tuple(request.get(key) for key in ("mode", "repository", "head", "base_branch", "acceptance_reference"))
        != expected
    ):
        raise ForgeConflict("PR request differs from the durable accepted subject")
    result: dict[str, object] = {
        "mode": mode,
        "operation_id": operation_id,
        "head": head,
        "status": operation.status,
        "reference": operation.accepted_reference,
    }
    if operation.status == "rejected":
        return result
    if fault == "pr-commit":
        return {**result, "interrupted": "pr-commit"}
    now = int(time.time())
    first_delivery = operation.attempts == 0
    if first_delivery:
        if not authorize_dispatch():
            return result
        store.claim(operation_id, now=now, lease_seconds=1)
        operation = store.operation(operation_id)
    effect = Effect(
        OperationKey(operation.operation_id, operation.effect_key, operation.request_digest),
        repository,
        "pr",
        str(request["topic"]),
        revision=Reference(f"forgejo:{head}"),
        base_branch=base_branch,
        title=title,
        body=body,
        fence=operation.fence,
        delivery_attempts=operation.attempts,
    )
    with ExitStack() as stack:
        if forge is None:
            remote_directory = repository_path.parent / "synthetic-forge"
            remote_directory.mkdir(mode=0o700, exist_ok=True)
            remote_store = stack.enter_context(SQLiteProgramStore(remote_directory / "forge.sqlite3"))
            transport = DurableSyntheticForge(remote_store)
            forge = ForgejoForge(transport, transport, lambda proposed: proposed == effect)
        if first_delivery:
            if not authorize_dispatch():
                return {**result, "status": "unknown"}
            receipt = forge.apply(effect)
            if fault == "pr-send":
                return {**result, "status": "unknown", "interrupted": "pr-send"}
            if receipt.reference is not None:
                store.finalize_artifact(operation_id, "known-reference", receipt.reference.value.encode())
            if receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None:
                store.observe(
                    operation_id,
                    operation.fence,
                    "reference:pr:accepted",
                    "accepted",
                    reference=receipt.reference.value,
                )
            elif receipt.status in {EffectStatus.REJECTED, EffectStatus.STALE}:
                store.observe(operation_id, operation.fence, "reference:pr:rejected", "rejected")
            else:
                store.observe(operation_id, operation.fence, "reference:pr:unknown", "unknown")
        else:
            known = _artifact(store, operation_id, "known-reference")
            reference = operation.accepted_reference or (known.decode() if known is not None else None)
            receipt = forge.reconcile(effect, Reference(reference) if reference else None)
            if operation.status in {"dispatched", "unknown"} and (operation.lease_until or 0) <= now:
                recovered = receipt.status is EffectStatus.ACCEPTED and receipt.reference is not None
                store.reconcile(
                    operation_id,
                    operation.fence,
                    "found" if recovered else "unknown",
                    now=now,
                    evidence="reference:pr:exact-readback",
                    reference=receipt.reference.value if recovered and receipt.reference is not None else None,
                    matched_digest=operation.request_digest if recovered else None,
                )
        return _result(result, receipt, fault, store.operation(operation_id).status)


def _result(original: dict[str, object], receipt: Receipt, fault: str | None, durable_status: str) -> dict[str, object]:
    status = receipt.status.value
    if status == "accepted" and durable_status != "accepted":
        status = "unknown"
    return {
        **original,
        "status": status,
        "reference": receipt.reference.value if receipt.reference is not None else None,
        "observed_base": receipt.observed_base.value if receipt.observed_base is not None else None,
        "interrupted": "pr-receipt" if fault == "pr-receipt" else None,
    }
