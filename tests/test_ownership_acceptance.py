# SPDX-License-Identifier: Apache-2.0
"""Fresh physical-subject acceptance and closed-history availability boundaries."""

import json
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from test_codex_stdio import FAKE
from test_sqlite_store import OWNER, VERIFIER, WORKER, spec
from test_work_budget import Clock, initialize, policy
from test_work_ownership import claim, client, prepare_generic, workspace

from creatidy_kernel.adapters.codex_stdio import CodexStdio
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.domain import (
    ActivateProgram,
    FinishAttempt,
    InputBinding,
    SatisfyWorkUnit,
    StartAttempt,
    TrustedSatisfaction,
)
from creatidy_kernel.core.ownership import OwnershipConflict
from creatidy_kernel.core.work_budget import WorkKind
from tools.check_schema_upgrade import CREATE_LOGICAL_GENERIC, PINNED_PRE_ACCEPTANCE_ID, install_pinned_reader

pytest_plugins = ["test_sqlite_store"]


def acceptance(store: SQLiteProgramStore, attempt_id: str, *, program_id: str = "program-1") -> SatisfyWorkUnit:
    program = store.load(program_id)
    attempt = program.attempt(attempt_id).spec
    fact = TrustedSatisfaction(
        f"accepted:{attempt_id}",
        program_id,
        attempt.work_unit_id,
        attempt.spec_revision,
        attempt.spec_digest,
        program.spec.work_unit_applicability_fingerprint(attempt.work_unit_id),
        VERIFIER,
        attempt_id,
        tuple(
            InputBinding(name, f"artifact:{attempt_id}:{name}")
            for name in sorted(program.spec.work_unit(attempt.work_unit_id).outputs)
        ),
    )
    return SatisfyWorkUnit(program.revision, VERIFIER, fact)


def finish_generic(store: SQLiteProgramStore, attempt_id: str, *, program_id: str = "program-1") -> None:
    program = store.load(program_id)
    program = store.admit(program_id, f"start:{attempt_id}", StartAttempt(program.revision, WORKER, attempt_id))
    store.admit(program_id, f"finish:{attempt_id}", FinishAttempt(program.revision, WORKER, attempt_id))


def test_active_retained_group_cannot_be_accepted_from_logical_terminal_result(sqlite_tmp_path: Path) -> None:
    tree = workspace(sqlite_tmp_path, "one")
    binary = sqlite_tmp_path / "synthetic-app-server"
    binary.write_text(FAKE)
    binary.chmod(0o700)
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        base = policy()
        configured = replace(
            base,
            counts=tuple(
                (kind, 2 if kind in {WorkKind.TOOL, WorkKind.AUXILIARY} else count) for kind, count in base.counts
            ),
        )
        program = store.create(spec(), "create")
        store.bind_work_budget("program-1", "project", configured, configured, 110)
        store.admit("program-1", "activate", ActivateProgram(program.revision, OWNER))
        connection = CodexStdio(
            (str(binary), "app-server"), "0.99.1", environment={"HOME": str(sqlite_tmp_path), "PATH": os.defpath}
        )
        try:
            attempt = prepare_generic(store, "program-1", "generic", str(tree.path))
            fence = store.claim("runtime:generic", now=100, lease_seconds=1)
            store.record_transport("runtime:generic", fence, True)
            store.observe("runtime:generic", fence, "result:accepted", "accepted", reference="synthetic:handle")
            store.observe("runtime:generic", fence, "result:terminal", "terminal", reference="synthetic:handle")
            finish_generic(store, "generic")  # A logical terminal report is not a stop witness.
            store.record_work_completion("runtime:generic", "logical:terminal-result")
            assert connection.owned_group_active
            before = store.load("program-1"), store.work_budget_status("program-1"), store.operations()
            with pytest.raises(OwnershipConflict, match="settlement|proof"):
                store.admit("program-1", "accept:generic", acceptance(store, "generic"))
            assert (store.load("program-1"), store.work_budget_status("program-1"), store.operations()) == before
            connection.close()
            assert connection.owned_group_settled is True
            operation = store.operation("runtime:generic")
            proof = store.finalize_artifact(
                operation.operation_id,
                "owned-stop",
                json.dumps(
                    {
                        "operation": operation.operation_id,
                        "request_digest": operation.request_digest,
                        "fence": operation.fence,
                        "attempt": attempt.digest,
                        "workspace": str(tree.path),
                        "owned_group_reference": connection.owned_group_reference,
                        "owned_group_settled": True,
                    },
                    sort_keys=True,
                ).encode(),
            )
            store.record_work_settlement(operation.operation_id, proof, expected_fence=fence)
            for name, kind in (
                ("verification", WorkKind.VERIFICATION),
                ("review", WorkKind.REVIEW),
                ("tool", WorkKind.TOOL),
            ):
                store.budgeted_intent("program-1", "generic", kind, name, name, {"kind": kind.value})
                with pytest.raises(OwnershipConflict, match="settlement|proof"):
                    store.admit("program-1", "accept:generic", acceptance(store, "generic"))
                store.record_work_settlement(name, f"trusted:{name}:complete", expected_fence=0)
            store.admit("program-1", "accept:generic", acceptance(store, "generic"))
            store.budgeted_intent("program-1", "generic", WorkKind.AUXILIARY, "publication", "publication", {})
            store.check_work_dispatch("publication")  # Only this actual pending publication is excluded.
            assert store.claim("publication", now=100, lease_seconds=1) == 1
            store.budgeted_intent("program-1", "generic", WorkKind.TOOL, "other-effect", "other-effect", {})
            with pytest.raises(OwnershipConflict, match="settlement|proof"):
                store.check_work_dispatch("publication")
            store.record_work_settlement("other-effect", "trusted:other-effect:reconciled", expected_fence=0)
            store.check_work_dispatch("publication")
        finally:
            connection.close()


@pytest.fixture(scope="module")
def accepted_history_reader(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return install_pinned_reader(
        tmp_path_factory.mktemp("pre-acceptance-proof-reader"), commit_id=PINNED_PRE_ACCEPTANCE_ID
    )


@pytest.mark.parametrize("pending_history", [False, True])
def test_actual_prior_accepted_history_does_not_authorize_new_publication(
    sqlite_tmp_path: Path,
    accepted_history_reader: Path,
    pending_history: bool,
) -> None:
    subprocess.run(  # noqa: S603 - exact prior public producer; synthetic closed fixture.
        [
            str(accepted_history_reader),
            "-I",
            "-c",
            CREATE_LOGICAL_GENERIC,
            str(sqlite_tmp_path),
            "pending-publication" if pending_history else "accepted-history",
        ],
        check=True,
    )
    path = sqlite_tmp_path / "kernel.sqlite3"
    with closing(sqlite3.connect(path)) as old:
        original_history = old.execute("SELECT * FROM history ORDER BY sequence").fetchall()
    store = SQLiteProgramStore(path, boot_clock=Clock())
    try:
        before = store.operations(), store.work_budget_status("reference")
        if pending_history:
            with pytest.raises(OwnershipConflict, match="settlement|proof"):
                store.check_work_dispatch("publish")
            with pytest.raises(OwnershipConflict, match="settlement|proof"):
                store.claim("publish", now=100, lease_seconds=1)
        else:
            with pytest.raises(OwnershipConflict, match="settlement|proof"):
                store.budgeted_intent(
                    "reference",
                    "generic",
                    WorkKind.AUXILIARY,
                    "publish",
                    "publish",
                    {"accepted": "logical:owned-group-stopped"},
                )
        assert (store.operations(), store.work_budget_status("reference")) == before
        store.record_work_settlement("runtime:generic", "trusted:original-task-quiescent", expected_fence=0)
        if not pending_history:
            store.budgeted_intent(
                "reference",
                "generic",
                WorkKind.AUXILIARY,
                "publish",
                "publish",
                {"accepted": "logical:owned-group-stopped"},
            )
        store.check_work_dispatch("publish")
        assert store.claim("publish", now=100, lease_seconds=1) == 1
    finally:
        store.close()
    with closing(sqlite3.connect(path)) as current:
        assert current.execute("SELECT * FROM history ORDER BY sequence").fetchall() == original_history


@pytest.mark.parametrize("entry", ["new-claim", "prepared-generic", "prepared-owned", "ancestor-generic"])
def test_qualified_closed_generic_alias_cannot_globally_block_independent_work(
    sqlite_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    old, independent = workspace(sqlite_tmp_path, "old"), workspace(sqlite_tmp_path, "independent")
    with SQLiteProgramStore(sqlite_tmp_path / "kernel.sqlite3", boot_clock=Clock()) as store:
        initialize(store, project=policy(active=4))
        initialize(store, "program-2", project=policy(active=4))
        prepare_generic(store, "program-1", "old", str(old.path))
        finish_generic(store, "old")
        store.record_work_settlement("runtime:old", "trusted:old-scope-closed", expected_fence=0)
        prepared = None
        if entry == "prepared-generic":
            prepare_generic(store, "program-2", "independent", str(independent.path))
        elif entry == "prepared-owned":
            prepared = claim(store, client(store, independent), independent, "program-2", "independent")
            current = store.load("program-2")
            store.admit("program-2", "start:independent", StartAttempt(current.revision, WORKER, "independent"))
        preserved = old.path.with_name("retained-old")
        old.path.rename(preserved)
        old.path.symlink_to(preserved, target_is_directory=True)
        actual_resolve = Path.resolve
        probes: list[Path] = []

        def resolve(path: Path, strict: bool = False) -> Path:
            probes.append(path)
            return actual_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        if entry == "new-claim":
            prepared = claim(store, client(store, independent), independent, "program-2", "independent")
        elif entry == "ancestor-generic":
            prepare_generic(store, "program-2", "independent", str(old.path.parent))
        if prepared is None:
            assert store.claim("runtime:independent", now=100, lease_seconds=1) == 1
            finish_generic(store, "independent", program_id="program-2")
            store.record_work_settlement("runtime:independent", "trusted:independent-scope-closed", expected_fence=1)
        else:
            client(store, independent).quiesce(prepared, "client:independent-task-quiescent")
            client(store, independent).submit(prepared)
            if entry == "new-claim":
                current = store.load("program-2")
                # Logical start cannot occur after a quiescence receipt; keep this
                # case at submission while prepared-owned exercises acceptance.
                assert current.attempt("independent").spec == prepared.attempt
            else:
                current = store.load("program-2")
                store.admit("program-2", "finish:independent", FinishAttempt(current.revision, WORKER, "independent"))
        assert old.path not in probes
        if entry != "new-claim":
            store.admit("program-2", "accept:independent", acceptance(store, "independent", program_id="program-2"))
        with pytest.raises(OwnershipConflict, match="canonical"):
            prepare_generic(store, "program-2", "unsafe-alias", str(old.path))
        assert old.path.is_symlink() and preserved.is_dir()
