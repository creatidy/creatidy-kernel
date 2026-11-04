# SPDX-License-Identifier: Apache-2.0
"""Synthetic durable owner outcome export, never private runtime data."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from ordinary_flow_fixture import run_ordinary_flow
from test_allocation_recovery import CountingAllocator, RecordingRuntime, initial, step
from test_sqlite_store import _complete_and_cancel  # pyright: ignore[reportPrivateUsage]
from test_sqlite_store import sqlite_tmp_path as sqlite_tmp_path
from test_work_budget import Clock, initialize, policy, prepare
from test_work_ownership import claim, client, workspace

from creatidy_kernel.adapters.outcome_file import write_private_outcome
from creatidy_kernel.adapters.reference import ReferenceInterrupted, reference_spec, run_reference
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import FinishAttempt, StartAttempt
from creatidy_kernel.core.outcomes import Provenance, UsageObservation
from creatidy_kernel.core.verification import Evidence, EvidenceSubject
from creatidy_kernel.core.work_budget import WorkKind
from creatidy_kernel.ports.application import BudgetedChecks
from creatidy_kernel.ports.outcome_export import PRIVATE_METADATA, export_private_outcome
from creatidy_kernel.ports.program_store import OperationRecord


def test_private_export_preserves_correction_and_unknown_coverage(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        operation = "runtime:a1"
        store.reserve_work(program.program_id, "a1", operation, WorkKind.ATTEMPT)
        original = UsageObservation(
            "obs-1",
            "a1",
            "receipt-1",
            "money",
            "USD",
            Decimal("2"),
            Provenance.PROVIDER_REPORT,
            "synthetic-provider",
            100,
        )
        store.record_usage(operation, original)
        store.record_usage(operation, replace(original, source="different-provider", quantity=Decimal("3")))
        store.record_usage(
            operation, replace(original, observation_id="corrected", quantity=Decimal("4"), supersedes_id="obs-1")
        )
        store.record_usage(
            operation,
            replace(
                original,
                observation_id="unknown",
                receipt_id="receipt-2",
                metric="tokens",
                unit="input_tokens",
                quantity=None,
            ),
        )
        before = (store.load(program.program_id), store.operations(), store.work_budget_status(program.program_id))
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        assert result["schema"] == "creatidy.outcome.private/1"
        usage = cast(list[dict[str, object]], result["usage_history"])
        assert len(usage) == 4
        exported_attempt = cast(list[dict[str, object]], result["attempts"])[0]["id"]
        assert all(item["attempt"] == exported_attempt and item["attempt_binding"] == "recorded" for item in usage)
        assert cast(dict[str, object], result["coverage"])["unattributed_usage_observations"] == 0
        assert sorted(
            cast(str, item["quantity"]) for item in usage if item["effective"] and item["quantity"] is not None
        ) == ["3", "4"]
        correction = next(item for item in usage if item["quantity"] == "4")
        prior = next(item for item in usage if item["quantity"] == "2")
        assert correction["supersedes"] == prior["observation"] and prior["effective"] is False
        different_source = next(item for item in usage if item["quantity"] == "3")
        assert different_source["observation"] != prior["observation"]
        assert different_source["receipt"] != prior["receipt"]
        assert next(item for item in usage if item["unit"] == "input_tokens")["quantity"] is None
        totals = cast(list[dict[str, object]], result["usage_totals"])
        assert {item["known_subtotal"] for item in totals if item["unit"] == "USD"} == {"3", "4"}
        assert all(item["quantity"] is None and item["complete"] is False for item in totals)
        assert cast(dict[str, object], result["coverage"])["complete_cost"] is None
        budget = cast(dict[str, object], result["work_budget"])
        deadline = cast(dict[str, object], budget["original_deadline"])
        assert deadline["wall_deadline"] == 110 and deadline["wall_anchor"] == 100
        assert deadline["boot_deadline_ns"] == 11_000_000_000
        assert deadline["boot_id"] != "synthetic-boot-a" and "synthetic-boot-a" not in json.dumps(result)
        assert (
            store.load(program.program_id),
            store.operations(),
            store.work_budget_status(program.program_id),
        ) == before


def test_domain_cancellations_and_conclusions_survive_reopen(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "domain-cancellations.sqlite3"
    with SQLiteProgramStore(path) as store:
        expected = _complete_and_cancel(store)
    with SQLiteProgramStore(path) as store:
        before = (store.served_change_token(), store.operations())
        result = export_private_outcome(
            store, expected.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        decisions = cast(list[dict[str, object]], result["domain_cancellations"])
        conclusions = cast(list[dict[str, object]], result["program_conclusions"])
        assert result["cancellations"] == []
        assert {item["kind"] for item in decisions} == {"work-unit", "attempt"}
        assert {item["record_order"] for item in decisions} == {
            item.record_order for item in (*expected.cancellations, *expected.attempt_cancellations)
        }
        assert all(item["physical_settlement"] == "unknown" for item in decisions)
        assert [(item["status"], item["spec_revision"], item["record_order"]) for item in conclusions] == [
            (item.status.value, item.spec_revision, item.record_order) for item in expected.conclusions
        ]
        assert "owner narrowed scope" not in json.dumps(result)
        assert (store.served_change_token(), store.operations()) == before


def test_private_export_redacts_untrusted_ids_and_rejects_other_policy(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "private.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store, "synthetic-secret-program"), "synthetic-secret-attempt")
        before = store.operations()
        with pytest.raises(ValueError, match="policy"):
            export_private_outcome(store, program.program_id, owner_policy="share-everything")
        assert store.operations() == before
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        encoded = json.dumps(result)
        assert "synthetic-secret-program" not in encoded and "synthetic-secret-attempt" not in encoded
        assert result["source_rights"] == "not_retained; source content omitted"
        assert result == export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )


def test_absent_budget_does_not_imply_no_work_or_cost(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "unbound.sqlite3") as store:
        program = store.create(reference_spec(), "create")
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        coverage = cast(dict[str, object], result["coverage"])
        assert coverage["budget_configured"] is False
        assert coverage["effect_inventory"] == "unknown-budget-not-bound"
        assert coverage["review_inventory"] == "unknown-budget-not-bound"
        assert coverage["complete_cost"] is None and result["work_budget"] is None


def test_other_program_does_not_adopt_pre_attempt_task_cancellation(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "foreign-cancel.sqlite3") as store:
        program = store.create(reference_spec(), "create")
        store.intent(
            "cancel:task_execution:change",
            "cancel:task_execution:change",
            {"operation": "runtime:task_execution:change", "descendants": "unproven"},
        )
        exported = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        assert exported["cancellations"] == [] and exported["attempts"] == []


def test_private_export_reopens_same_durable_evidence_without_mutation(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "reopen.sqlite3"
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        operation = "runtime:a1"
        store.reserve_work(program.program_id, "a1", operation, WorkKind.ATTEMPT)
        store.record_usage(
            operation,
            UsageObservation(
                "one", "a1", "receipt", "money", "USD", Decimal("2"), Provenance.PROVIDER_REPORT, "source", 100
            ),
        )
        first = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
    with SQLiteProgramStore(path, boot_clock=Clock()) as store:
        before = (store.history(program.program_id), store.operations())
        reopened = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        assert reopened == first and (store.history(program.program_id), store.operations()) == before
        assert (
            export_private_outcome(store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"x" * 32)
            != first
        )


def test_export_isolates_other_program_in_shared_project(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "isolated-programs.sqlite3", boot_clock=Clock()) as store:
        first = initialize(store, project=policy(active=2))
        second = initialize(store, "program-2", project=policy(active=2))
        prepare(store, first, "a1")
        prepare(store, second, "a2")
        for program, attempt, amount in ((first, "a1", "2"), (second, "a2", "9")):
            operation = f"runtime:{attempt}"
            store.reserve_work(program.program_id, attempt, operation, WorkKind.ATTEMPT)
            store.record_usage(
                operation,
                UsageObservation(
                    f"obs-{attempt}",
                    attempt,
                    f"receipt-{attempt}",
                    "money",
                    "USD",
                    Decimal(amount),
                    Provenance.OWNER_RECORD,
                    "source",
                    100,
                ),
            )
        result = export_private_outcome(store, first.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
        attempts = cast(list[dict[str, object]], result["attempts"])
        history = cast(list[dict[str, object]], result["usage_history"])
        assert len(attempts) == 1 and len(history) == 1 and history[0]["quantity"] == "2"
        assert "program-2" not in json.dumps(result) and "receipt-a2" not in json.dumps(result)


def test_private_file_never_overwrites_and_failure_preserves_existing_data(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = sqlite_tmp_path / "private"
    directory.mkdir(mode=0o700)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with SQLiteProgramStore(sqlite_tmp_path / "file.sqlite3", boot_clock=Clock()) as store:
            program = initialize(store, "synthetic-secret-program")
            before = (store.history(program.program_id), store.operations())
            write_private_outcome(
                store,
                program.program_id,
                owner_policy=PRIVATE_METADATA,
                directory_fd=descriptor,
                name="outcome.json",
                pseudonym_key=b"k" * 32,
            )
            output = directory / "outcome.json"
            original = output.read_bytes()
            assert output.stat().st_mode & 0o777 == 0o600
            assert json.loads(original)["schema"] == "creatidy.outcome.private/1"
            assert b"synthetic-secret-program" not in original
            with pytest.raises(FileExistsError):
                write_private_outcome(
                    store,
                    program.program_id,
                    owner_policy=PRIVATE_METADATA,
                    directory_fd=descriptor,
                    name="outcome.json",
                )
            assert output.read_bytes() == original
            (directory / "existing-link").symlink_to("outcome.json")
            with pytest.raises(FileExistsError):
                write_private_outcome(
                    store,
                    program.program_id,
                    owner_policy=PRIVATE_METADATA,
                    directory_fd=descriptor,
                    name="existing-link",
                )
            assert (directory / "existing-link").is_symlink() and output.read_bytes() == original
            with monkeypatch.context() as failure:

                def fail_link(*args: object, **kwargs: object) -> None:
                    del args, kwargs
                    raise OSError("synthetic link failure")

                failure.setattr(os, "link", fail_link)
                with pytest.raises(OSError, match="synthetic"):
                    write_private_outcome(
                        store,
                        program.program_id,
                        owner_policy=PRIVATE_METADATA,
                        directory_fd=descriptor,
                        name="unpublished.json",
                    )
            assert not (directory / "unpublished.json").exists()
            assert sorted(item.name for item in directory.iterdir()) == ["existing-link", "outcome.json"]
            assert (store.history(program.program_id), store.operations()) == before
    finally:
        os.close(descriptor)


def test_private_file_refuses_untrusted_directory(sqlite_tmp_path: Path) -> None:
    directory = sqlite_tmp_path / "untrusted"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with SQLiteProgramStore(sqlite_tmp_path / "untrusted.sqlite3", boot_clock=Clock()) as store:
            program = initialize(store)
            with pytest.raises(PermissionError):
                write_private_outcome(
                    store,
                    program.program_id,
                    owner_policy=PRIVATE_METADATA,
                    directory_fd=descriptor,
                    name="outcome.json",
                )
            assert not list(directory.iterdir())
    finally:
        os.close(descriptor)


def test_private_export_exact_large_subtotal_and_redacted_unknown_labels(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "precision.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        operation = "runtime:a1"
        store.reserve_work(program.program_id, "a1", operation, WorkKind.ATTEMPT)
        first = UsageObservation(
            "one",
            "a1",
            "receipt-one",
            "synthetic-secret-metric",
            "synthetic-secret-unit",
            Decimal("9999999999999999999999999999999999999999"),
            Provenance.MEASUREMENT,
            "source",
            100,
        )
        store.record_usage(operation, first)
        store.record_usage(
            operation, replace(first, observation_id="two", receipt_id="receipt-two", quantity=Decimal(1))
        )
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        totals = cast(list[dict[str, object]], result["usage_totals"])
        assert len(totals) == 1 and totals[0]["known_subtotal"] == "10000000000000000000000000000000000000000"
        assert totals[0]["metric"] is None and totals[0]["unit"] is None
        assert "synthetic-secret-metric" not in json.dumps(result)
        assert "synthetic-secret-unit" not in json.dumps(result)


def test_resource_provenance_and_units_are_never_conflated(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "provenance.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        operation = "runtime:a1"
        store.reserve_work(program.program_id, "a1", operation, WorkKind.ATTEMPT)
        original = UsageObservation(
            "one", "a1", "receipt-one", "money", "USD", Decimal("1"), Provenance.ESTIMATE, "source", 100
        )
        store.record_usage(operation, original)
        store.record_usage(
            operation,
            replace(
                original,
                observation_id="two",
                receipt_id="receipt-two",
                provenance=Provenance.PROVIDER_REPORT,
                quantity=Decimal("2"),
            ),
        )
        store.record_usage(
            operation,
            replace(
                original,
                observation_id="three",
                receipt_id="receipt-three",
                unit="EUR",
                provenance=Provenance.OWNER_RECORD,
                quantity=Decimal("3"),
            ),
        )
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        totals = cast(list[dict[str, object]], result["usage_totals"])
        assert {(item["unit"], item["provenance"], item["known_subtotal"]) for item in totals} == {
            ("USD", "estimate", "1"),
            ("USD", "provider_report", "2"),
            ("EUR", "owner_record", "3"),
        }
        assert all(item["complete"] is False and item["quantity"] is None for item in totals)


def test_private_export_refuses_mutating_source_cut(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "changing.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        operation = "runtime:a1"
        store.reserve_work(program.program_id, "a1", operation, WorkKind.ATTEMPT)
        original = SQLiteProgramStore.work_budget_status

        def change_after_read(target: SQLiteProgramStore, program_id: str) -> dict[str, object]:
            status = original(target, program_id)
            target.record_usage(
                operation,
                UsageObservation(
                    "after", "a1", "receipt", "money", "USD", Decimal(1), Provenance.OWNER_RECORD, "owner", 100
                ),
            )
            return status

        monkeypatch.setattr(SQLiteProgramStore, "work_budget_status", change_after_read)
        with pytest.raises(ValueError, match="changed during export"):
            export_private_outcome(store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
        assert store.work_budget_status(program.program_id)["usage_history"]


def test_original_effect_evidence_is_scoped_and_separate_from_transport(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "effect-evidence.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        operation = "runtime:a1"
        store.reserve_work(program.program_id, "a1", operation, WorkKind.ATTEMPT)
        store.deliver_fake(
            operation,
            "synthetic-observation",
            now=100,
            lease_seconds=3,
            fake=lambda _key, _request: (True, "accepted", "synthetic-private-receipt"),
        )
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        effects = cast(list[dict[str, object]], result["effects"])
        actual = next(item for item in effects if item["category"] == "attempt")
        assert actual["status"] == "accepted"
        assert cast(list[dict[str, object]], actual["transports"]) == [{"fence": 1, "succeeded": True}]
        observations = cast(list[dict[str, object]], actual["observations"])
        assert len(observations) == 1 and observations[0]["kind"] == "accepted"
        assert observations[0]["reference"] == actual["receipt"]
        assert "synthetic-private-receipt" not in json.dumps(result)
        with pytest.raises(ValueError, match="selected Program"):
            store.outcome_effect_evidence("different-program", operation)


def test_recorded_missing_check_is_not_invented_as_pass_or_failure(sqlite_tmp_path: Path) -> None:
    class NoEvidence:
        def check(self, name: str, subject: EvidenceSubject) -> Evidence | None:
            assert name == "synthetic-check" and subject.attempt_digest
            return None

    with SQLiteProgramStore(sqlite_tmp_path / "missing-check.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        original = program.attempt("a1")
        program = store.admit(program.program_id, "start-a1", StartAttempt(program.revision, "owner", "a1"))
        program = store.admit(program.program_id, "finish-a1", FinishAttempt(program.revision, "owner", "a1"))
        subject = EvidenceSubject(
            "0" * 64,
            original.spec.digest,
            original.spec.spec_digest,
            "1" * 64,
            "synthetic-private-repository",
            "2" * 40,
            "3" * 40,
        )
        assert (
            BudgetedChecks(store, NoEvidence(), program.program_id, "a1", lambda: 100).check("synthetic-check", subject)
            is None
        )
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        records = cast(list[dict[str, object]], result["check_results"])
        assert len(records) == 1 and records[0]["result_recorded"] is True and records[0]["evidence_recorded"] is False
        assert result["checks"] == [] and not result["satisfactions"]
        assert "synthetic-private-repository" not in json.dumps(result)


def test_uncertain_effect_and_reconciliation_remain_historical_without_retry(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "uncertain-effect.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        operation = "runtime:a1"
        store.reserve_work(program.program_id, "a1", operation, WorkKind.ATTEMPT)
        fence = store.claim(operation, now=100, lease_seconds=3)
        store.record_transport(operation, fence, False)
        request_digest = store.operation(operation).request_digest
        store.reconcile(
            operation, fence, "idempotent", now=103, evidence="synthetic-private-proof", matched_digest=request_digest
        )
        before = store.operation(operation)
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        effect = next(
            item for item in cast(list[dict[str, object]], result["effects"]) if item["category"] == "attempt"
        )
        assert effect["status"] == "unknown" and effect["retry_proof"] == "idempotent"
        assert cast(list[dict[str, object]], effect["transports"]) == [{"fence": 1, "succeeded": False}]
        assert cast(list[dict[str, object]], effect["reconciliations"])[0]["outcome"] == "idempotent"
        assert "synthetic-private-proof" not in json.dumps(result)
        assert store.operation(operation) == before and before.attempts == 1


def test_original_claim_quarantine_is_retained_privately(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "claim-evidence.sqlite3", boot_clock=Clock()) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "claim-evidence")
        worker = client(store, tree)
        owner = client(store, tree, owner=True)
        granted = claim(store, worker, tree)
        owner.quarantine(granted, evidence="synthetic-private-quarantine")
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        exported = cast(list[dict[str, object]], result["claims"])[0]
        assert exported["quarantined_recorded"] is True and exported["released_recorded"] is False
        assert exported["quarantine_evidence"] is not None and exported["release_evidence"] is None
        assert exported["workspace_reconciliations"] == []
        assert exported["physical_settlement"] is None
        assert "synthetic-private-" not in json.dumps(result)


def test_original_workspace_reconciliation_and_release_are_retained_privately(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "claim-reconcile.sqlite3", boot_clock=Clock()) as store:
        program = initialize(store)
        tree = workspace(sqlite_tmp_path, "claim-reconcile")
        worker = client(store, tree)
        owner = client(store, tree, owner=True)
        granted = claim(store, worker, tree)
        worker.quiesce(granted, "synthetic-private-quiescence")
        (tree.path / "manual.txt").write_text("synthetic owner edit")
        observed = owner.observe(granted)
        owner.reconcile(granted, observed, evidence="synthetic-private-reconciliation")
        owner.release(granted, evidence="synthetic-private-release")
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        exported = cast(list[dict[str, object]], result["claims"])[0]
        assert exported["released_recorded"] is True and exported["release_evidence"] is not None
        assert len(cast(list[object], exported["workspace_reconciliations"])) == 1
        assert exported["physical_settlement"] is None
        assert "synthetic-private-" not in json.dumps(result)


def test_legacy_runtime_receipt_keeps_missing_identity_unknown(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "legacy-identity.sqlite3", boot_clock=Clock()) as store:
        program = prepare(store, initialize(store), "a1")
        legacy = {
            "version": 1,
            "handle": "synthetic-secret-handle",
            "fence": 1,
            "identity": {
                "requested": "synthetic-model",
                "resolved": None,
                "observed": None,
                "agent_definition_version": "synthetic-agent",
            },
        }
        store.finalize_artifact("runtime:a1", "runtime-receipt", json.dumps(legacy).encode())
        result = export_private_outcome(
            store, program.program_id, owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        attempt = cast(list[dict[str, object]], result["attempts"])[0]
        identity = cast(dict[str, object], attempt["runtime_receipt_identity"])
        assert identity["requested"] is not None and identity["observed"] is None
        assert identity["requested_provider"] is None and attempt["allocation"] is None
        assert "synthetic-model" not in json.dumps(result) and "synthetic-secret-handle" not in json.dumps(result)


def test_original_unpublished_allocation_is_read_without_reselection(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "allocation-recovery.sqlite3") as store:
        initial(store)
        allocator = CountingAllocator()
        with pytest.raises(ReferenceInterrupted, match="prepared"):
            step(store, allocator, RecordingRuntime(), fault="prepared")
        assert store.find_artifact("runtime:reference:first", "allocation") is None
        before = store.served_change_token()
        result = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
        attempt = cast(list[dict[str, object]], result["attempts"])[0]
        assert attempt["allocation"] is not None and allocator.calls == 1
        assert "test-runtime" not in json.dumps(result) and "original rationale" not in json.dumps(result)
        assert store.served_change_token() == before


def test_completed_independent_reference_driver_exports_actual_lineage(sqlite_tmp_path: Path) -> None:
    assert run_reference(sqlite_tmp_path, owner_approved=True)["status"] == "completed"
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        before = store.served_change_token()
        result = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
        attempts = cast(list[dict[str, object]], result["attempts"])
        satisfactions = cast(list[dict[str, object]], result["satisfactions"])
        assert len(attempts) == len(satisfactions) == 2
        assert all(item["allocation"] is not None for item in attempts)
        assert all(cast(dict[str, object], item["terminal_outcome"])["candidate"] == "present" for item in attempts)
        assert all(cast(dict[str, object], item["allocation"])["route"] is None for item in attempts)
        assert all(item["runtime_receipt_recorded"] is True for item in attempts)
        source = satisfactions[0]
        assert any(
            binding["source_kind"] == "predecessor" and binding["satisfaction"] == source["id"]
            for attempt in attempts
            for binding in cast(list[dict[str, object]], attempt["inputs"])
        )
        coverage = cast(dict[str, object], result["coverage"])
        assert result["reviews"] == [] and coverage["complete_cost"] is None
        checks = cast(list[dict[str, object]], result["checks"])
        assert checks and all(item["passed"] is True and item["freshness"] == "not-evaluated" for item in checks)
        native = cast(list[dict[str, object]], result["native_effects"])
        assert len(native) == 1 and native[0]["driver"] == "reference-pr-v1" and native[0]["action"] == "pr"
        assert native[0]["transport_mode"] == "synthetic" and native[0]["current_remote_state"] is None
        assert coverage["strict_inference_profile"] is False and coverage["route_and_call_identity"] is None
        assert store.served_change_token() == before


def test_reference_publication_unknown_remains_historical_without_retry(sqlite_tmp_path: Path) -> None:
    interrupted = run_reference(sqlite_tmp_path, owner_approved=True, fault="pr-send")
    assert cast(dict[str, object], interrupted["pr"])["status"] == "unknown"
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        before = store.operation("reference:pr")
        result = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
        native = cast(list[dict[str, object]], result["native_effects"])
        assert len(native) == 1 and native[0]["driver"] == "reference-pr-v1"
        assert native[0]["receipt_recorded"] is False and native[0]["current_remote_state"] is None
        assert store.operation("reference:pr") == before and before.attempts == 1


def test_review_findings_remain_linked_after_reopen(sqlite_tmp_path: Path) -> None:
    root = sqlite_tmp_path / "reopened-review-findings"
    assert run_ordinary_flow(root, None, mode="pull", exercise="review-remediation")["status"] == "completed"
    with SQLiteProgramStore(root / "state.db") as store:
        result = export_private_outcome(
            store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        review = next(
            item for item in cast(list[dict[str, object]], result["reviews"]) if item["verdict"] == "request_changes"
        )
        remediation = next(
            item
            for item in cast(list[dict[str, object]], result["attempts"])
            if item["remediation_of"] == review["attempt"]
        )
        findings = cast(list[dict[str, object]], review["findings"])
        assert findings and all(
            item in cast(list[dict[str, object]], remediation["remediation_findings"]) for item in findings
        )
        assert "Implement the approved edge case." not in json.dumps(result)


@pytest.mark.parametrize("damage", ["subject", "reviewer"])
def test_mismatched_review_receipt_never_exports_approval(sqlite_tmp_path: Path, damage: str) -> None:
    root = sqlite_tmp_path / f"mismatched-review-{damage}"
    assert run_ordinary_flow(root, None, mode="pull")["status"] == "completed"
    with SQLiteProgramStore(root / "state.db") as store:
        operation = store.operations("product-review:")[0].operation_id
        request = json.loads(store.artifact(operation, "review-request"))
        original = SQLiteProgramStore.find_artifact

        def altered(target: SQLiteProgramStore, operation_id: str, name: str) -> bytes | None:
            data = original(target, operation_id, name)
            if operation_id == operation and name == "review-receipt" and data is not None:
                raw = json.loads(data)
                if damage == "subject":
                    raw["subject"]["head_revision"] = "synthetic-different-head"
                else:
                    raw["reviewer_id"] = request["author_actor_id"]
                return json.dumps(raw).encode()
            return data

        with patch.object(SQLiteProgramStore, "find_artifact", altered):
            result = export_private_outcome(
                store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
            )
        reviews = cast(list[dict[str, object]], result["reviews"])
        assert sum(item["verdict"] is None for item in reviews) == 1
        assert sum(item["verdict"] == "approve" for item in reviews) == 1


def test_corrupt_verification_artifact_refuses_export(sqlite_tmp_path: Path) -> None:
    assert run_reference(sqlite_tmp_path, owner_approved=True)["status"] == "completed"
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3") as store:
        original = SQLiteProgramStore.find_artifact

        def altered(target: SQLiteProgramStore, operation_id: str, name: str) -> bytes | None:
            data = original(target, operation_id, name)
            if operation_id == "runtime:reference:first" and name.startswith("verification:") and data is not None:
                return b"{}"
            return data

        with patch.object(SQLiteProgramStore, "find_artifact", altered):
            with pytest.raises(ValueError, match="verification evidence reference mismatch"):
                export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)


def test_native_receipt_from_different_original_effect_is_refused(sqlite_tmp_path: Path) -> None:
    root = sqlite_tmp_path / "native-receipt-mismatch"
    assert run_ordinary_flow(root, None, mode="pull")["status"] == "completed"
    with SQLiteProgramStore(root / "state.db") as store:
        operation = next(
            item.operation_id for item in store.operations("ordinary-publish:") if item.operation_id.endswith(":pr")
        )
        original = SQLiteProgramStore.find_artifact

        def altered(target: SQLiteProgramStore, operation_id: str, name: str) -> bytes | None:
            data = original(target, operation_id, name)
            if operation_id == operation and name == "native-receipt" and data is not None:
                record = json.loads(data)
                record["reference"] = "forgejo:synthetic/wrong-receipt"
                return json.dumps(record).encode()
            return data

        with patch.object(SQLiteProgramStore, "find_artifact", altered):
            with pytest.raises(ValueError, match="native receipt differs"):
                export_private_outcome(
                    store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
                )


@pytest.mark.parametrize(("exercise", "exit_code"), [("command-export-crash", 0), ("command-export-failed", 7)])
def test_command_result_survives_interrupted_aggregate_and_reopen(
    sqlite_tmp_path: Path, exercise: str, exit_code: int
) -> None:
    root = sqlite_tmp_path / exercise
    assert run_ordinary_flow(root, None, mode="pull", exercise=exercise)["status"] == "interrupted"
    with SQLiteProgramStore(root / "state.db") as store:
        before = (store.served_change_token(), store.operations())
        with patch("subprocess.Popen", side_effect=AssertionError("export dispatched a subprocess")):
            result = export_private_outcome(
                store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
            )
        assert (store.served_change_token(), store.operations()) == before
        commands = cast(list[dict[str, object]], result["command_results"])
        assert len(commands) == 1 and commands[0]["exit_code"] == exit_code
        assert commands[0]["settlement"] == "witnessed" and commands[0]["integrity"] == "recorded"
        assert "synthetic-private-command-output" not in json.dumps(result)
        assert result["satisfactions"] == []
        operations = SQLiteProgramStore.operations

        def hide_witness(target: SQLiteProgramStore, prefix: str = "") -> tuple[OperationRecord, ...]:
            return () if prefix.startswith("command-settlement:") else operations(target, prefix)

        with patch.object(SQLiteProgramStore, "operations", hide_witness):
            incomplete = export_private_outcome(
                store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
            )
        assert cast(list[dict[str, object]], incomplete["command_results"])[0]["settlement"] == "unknown"
        original_find = SQLiteProgramStore.find_artifact

        def hide_result(target: SQLiteProgramStore, operation: str, name: str) -> bytes | None:
            if operation.startswith("ordinary-command:") and name == "result":
                return None
            return original_find(target, operation, name)

        with patch.object(SQLiteProgramStore, "find_artifact", hide_result):
            incomplete = export_private_outcome(
                store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
            )
        partial = cast(list[dict[str, object]], incomplete["command_results"])[0]
        assert partial["result_recorded"] is True and partial["integrity"] == "unknown"
        store.create(reference_spec(), "another-program")
        other = export_private_outcome(store, "reference", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32)
        assert other["command_results"] == []


def test_command_evidence_and_manifest_are_not_duplicated(sqlite_tmp_path: Path) -> None:
    root = sqlite_tmp_path / "command-export-manifest"
    assert run_ordinary_flow(root, None, mode="pull")["status"] == "completed"
    with SQLiteProgramStore(root / "state.db") as store:
        result = export_private_outcome(
            store, "everyday-program", owner_policy=PRIVATE_METADATA, pseudonym_key=b"k" * 32
        )
        commands = cast(list[dict[str, object]], result["command_results"])
        checks = cast(list[dict[str, object]], result["checks"])
        assert len(commands) == 2
        assert len({(check["attempt"], check["evidence"]) for check in checks}) == len(checks)


@pytest.mark.parametrize("payload", [b"not-json", b'{"version":99}'])
def test_unsupported_or_corrupt_runtime_receipt_refuses_without_file(sqlite_tmp_path: Path, payload: bytes) -> None:
    directory = sqlite_tmp_path / "private-corrupt"
    directory.mkdir(mode=0o700)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with SQLiteProgramStore(sqlite_tmp_path / "corrupt-identity.sqlite3", boot_clock=Clock()) as store:
            program = prepare(store, initialize(store), "a1")
            store.finalize_artifact("runtime:a1", "runtime-receipt", payload)
            with pytest.raises(ValueError):
                write_private_outcome(
                    store,
                    program.program_id,
                    owner_policy=PRIVATE_METADATA,
                    directory_fd=descriptor,
                    name="outcome.json",
                )
            assert not list(directory.iterdir())
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("after_link", [False, True])
def test_real_process_interruption_preserves_source_and_private_publication(
    sqlite_tmp_path: Path, after_link: bool
) -> None:
    directory = sqlite_tmp_path / "crash-private"
    directory.mkdir(mode=0o700)
    database = sqlite_tmp_path / "crash-source.sqlite3"
    with SQLiteProgramStore(database, boot_clock=Clock()) as store:
        program = initialize(store)
        before = (store.load(program.program_id), store.history(program.program_id), store.operations())
    script = """
import os, sys
from creatidy_kernel.adapters.outcome_file import write_private_outcome
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.ports.outcome_export import PRIVATE_METADATA
real_link = os.link
def interrupt(*args, **kwargs):
    if sys.argv[3] == 'after':
        real_link(*args, **kwargs)
    os._exit(97)
os.link = interrupt
fd = os.open(sys.argv[2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
with SQLiteProgramStore(sys.argv[1]) as store:
    write_private_outcome(store, 'program-1', owner_policy=PRIVATE_METADATA, directory_fd=fd,
                          name='outcome.json', pseudonym_key=b'k' * 32)
"""
    completed = subprocess.run(  # noqa: S603 - Fixed interpreter and synthetic local fixture.
        [sys.executable, "-I", "-c", script, str(database), str(directory), "after" if after_link else "before"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={"HOME": str(sqlite_tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8"},
        timeout=30,
        check=False,
    )
    assert completed.returncode == 97, completed.stderr.decode(errors="replace")
    output = directory / "outcome.json"
    assert output.exists() is after_link
    orphans = tuple(item for item in directory.iterdir() if item.name.startswith(".outcome-"))
    assert len(orphans) == 1 and orphans[0].stat().st_mode & 0o777 == 0o600
    if after_link:
        assert json.loads(output.read_bytes())["schema"] == "creatidy.outcome.private/1"
        assert output.read_bytes() == orphans[0].read_bytes()
    with SQLiteProgramStore(database, boot_clock=Clock()) as store:
        assert (store.load(program.program_id), store.history(program.program_id), store.operations()) == before
