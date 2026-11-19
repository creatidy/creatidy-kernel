# SPDX-License-Identifier: Apache-2.0
"""Owner-received semantic relevance for ordinary, non-frozen coding obligations.

Native/actual-source observations are mechanical. Relevance/equivalence of arbitrary
requirements is an explicit authenticated owner judgement, never a title heuristic,
proposer claim, green status or model-issued authorization. A judgement is inert and
applies only to its exact received snapshot. Program approval remains separate.
"""

from collections.abc import Callable
from dataclasses import replace
from typing import cast

from creatidy_kernel.adapters.forge_refs import ForgeBinding
from creatidy_kernel.adapters.intake import ContentSource, ForgeIntakeEvidence, IntakeForge
from creatidy_kernel.core.authority import Principal
from creatidy_kernel.core.intake import (
    Declaration,
    Disposition,
    IntakeEvidence,
    IntakeRefused,
    IssueSubject,
    digest,
    encode,
    object_from,
)
from creatidy_kernel.ports.intake import BaselineReader, IntakeStore


class OwnerReviewedForgejoEvidence:
    def __init__(
        self,
        store: IntakeStore,
        owner: Principal,
        forge: IntakeForge,
        binding: ForgeBinding,
        source: ContentSource,
        baseline: BaselineReader,
        clock: Callable[[], int],
        *,
        max_pages: int,
    ) -> None:
        if owner.role != "owner":
            raise IntakeRefused("source assessment requires an owner authenticated outside proposer/worker data")
        self.store, self.owner, self.clock = store, owner, clock
        self.producer = "forgejo-owner-relevance:v1"
        self.observer = ForgeIntakeEvidence(
            forge,
            binding,
            source,
            baseline,
            (),
            "forgejo-owner-snapshot:v1",
            clock,
            max_pages=max_pages,
            inventory_only=True,
        )
        self.baseline = baseline

    def snapshot(self, subject: IssueSubject, declaration: Declaration) -> IntakeEvidence:
        return self.observer.read(subject, declaration)

    def _history(self, snapshot_digest: str) -> tuple[dict[str, object], ...]:
        records: list[dict[str, object]] = []
        prefix = "ordinary-source-review:" + digest(self.owner.actor_id.encode()) + ":"
        for operation in self.store.operations(prefix):
            request = object_from(operation.request_json.encode())
            outer = cast(dict[str, object], request.get("request"))
            if (
                type(outer) is not dict
                or outer.get("kind") != "ordinary-intake-record"
                or type(outer.get("bytes")) is not str
            ):
                raise IntakeRefused("unrecognized source assessment envelope")
            raw = cast(str, outer["bytes"]).encode()
            record = object_from(raw)
            if (
                set(record)
                != {"version", "kind", "issuer", "decision", "snapshot", "disposition", "reason", "revision", "parent"}
                or record["version"] != 1
                or record["kind"] != "owner-source-assessment"
                or record["issuer"] != self.owner.actor_id
                or operation.operation_id != prefix + cast(str, record["decision"])
                or self.store.artifact(operation.operation_id, "record") != raw
            ):
                raise IntakeRefused("source assessment identity/version differs")
            if record["snapshot"] == snapshot_digest:
                records.append(record)
        records.sort(key=lambda item: cast(int, item["revision"]))
        for index, record in enumerate(records):
            expected_parent = None if index == 0 else digest(encode(records[index - 1]))
            if (
                type(record["revision"]) is not int
                or record["revision"] != index + 1
                or record["parent"] != expected_parent
            ):
                raise IntakeRefused("broken immutable source assessment history")
        return tuple(records)

    @staticmethod
    def snapshot_digest(evidence: IntakeEvidence, declaration: Declaration) -> str:
        return digest(encode({"snapshot": evidence.digest, "declaration": declaration.digest}))

    def review(
        self,
        subject: IssueSubject,
        declaration: Declaration,
        *,
        expected_snapshot: str,
        disposition: Disposition,
        decision_id: str,
        reason: str,
    ) -> str:
        if (
            disposition
            not in {Disposition.REMAINS, Disposition.IMPLEMENTED, Disposition.ACTIVE_PR, Disposition.EQUIVALENCE}
            or type(decision_id) is not str
            or not decision_id.strip()
            or len(decision_id) > 128
            or type(reason) is not str
            or not reason.strip()
            or len(reason) > 4096
        ):
            raise IntakeRefused("bounded explicit owner assessment, reason and decision identity required")
        snapshot = self.snapshot(subject, declaration)
        basis = self.snapshot_digest(snapshot, declaration)
        proof = snapshot.relevance_proof
        if (
            basis != expected_snapshot
            or snapshot.disposition is not Disposition.EQUIVALENCE
            or b'"owner_review_tree_available":true' not in proof
            or snapshot.baseline_result not in {"passed", "failed"}
        ):
            raise IntakeRefused(
                "source inventory changed, incomplete, inaccessible or unsupported; "
                "judgement cannot repair missing evidence"
            )
        history = self._history(basis)
        prefix = "ordinary-source-review:" + digest(self.owner.actor_id.encode()) + ":"
        key = prefix + decision_id
        prior = self.store.find_artifact(key, "record")
        if prior is not None:
            record = object_from(prior)
            if (record["snapshot"], record["disposition"], record["reason"]) != (basis, disposition.value, reason):
                raise IntakeRefused("owner assessment idempotency input changed")
            return digest(prior)
        record = {
            "version": 1,
            "kind": "owner-source-assessment",
            "issuer": self.owner.actor_id,
            "decision": decision_id,
            "snapshot": basis,
            "disposition": disposition.value,
            "reason": reason,
            "revision": len(history) + 1,
            "parent": None if not history else digest(encode(history[-1])),
        }
        raw = encode(record)
        self.store.intent(key, key, {"kind": "ordinary-intake-record", "version": 1, "bytes": raw.decode()})
        self.store.finalize_artifact(key, "record", raw)
        return digest(raw)

    def read(self, subject: IssueSubject, declaration: Declaration) -> IntakeEvidence:
        snapshot = self.snapshot(subject, declaration)
        if snapshot.disposition is not Disposition.EQUIVALENCE:
            return snapshot
        history = self._history(self.snapshot_digest(snapshot, declaration))
        if not history:
            return snapshot
        record = history[-1]
        disposition = Disposition(cast(str, record["disposition"]))
        proof = encode({"received_native_snapshot": snapshot.payload(), "owner_assessment": record})
        return replace(
            snapshot,
            relevance_producer=self.producer,
            relevance_reference="sha256:" + digest(proof),
            relevance_proof=proof,
            disposition=disposition,
        )
