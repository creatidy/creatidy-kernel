# SPDX-License-Identifier: Apache-2.0
"""Exact-head reference PR effects retain remote truth across controller restarts."""

from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.fake_forge import SyntheticForgeTransport
from creatidy_kernel.adapters.forgejo import ForgejoForge
from creatidy_kernel.adapters.reference_forge import PR_OPERATION, SYNTHETIC_REPOSITORY, deliver_reference_pr
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.core.forge import ForgeConflict

pytest_plugins = ["test_sqlite_store"]
HEAD = "a" * 40


def deliver(store: SQLiteProgramStore, root: Path, fault: str | None = None) -> dict[str, object]:
    return deliver_reference_pr(
        store, repository_path=root / "repository", head=HEAD, acceptance_reference="accepted:second", fault=fault
    )


@pytest.mark.parametrize("fault", [None, "pr-commit", "pr-receipt"])
def test_reference_pr_is_exact_and_repeated_delivery_does_not_duplicate(
    sqlite_tmp_path: Path, fault: str | None
) -> None:
    path = sqlite_tmp_path / "controller.sqlite3"
    with SQLiteProgramStore(path) as store:
        deliver(store, sqlite_tmp_path, fault)
    with SQLiteProgramStore(path) as store:
        result = deliver(store, sqlite_tmp_path)
        assert result["status"] == "accepted"
        assert result["head"] == HEAD
        assert result["reference"] == "forgejo:synthetic/reference#1"
        assert store.operation(PR_OPERATION).attempts == 1
        assert deliver(store, sqlite_tmp_path) == result
        with pytest.raises(ForgeConflict, match="durable accepted subject"):
            deliver_reference_pr(
                store,
                repository_path=sqlite_tmp_path / "repository",
                head="c" * 40,
                acceptance_reference="accepted:second",
            )
    with SQLiteProgramStore(sqlite_tmp_path / "synthetic-forge" / "forge.sqlite3") as remote:
        assert remote.artifact("synthetic:pr", "pull").count(b'"number": 1') == 1


def test_lost_pr_reply_never_repeats_creation(sqlite_tmp_path: Path) -> None:
    path = sqlite_tmp_path / "controller.sqlite3"
    with patch("creatidy_kernel.adapters.reference_forge.time.time", return_value=100):
        with SQLiteProgramStore(path) as store:
            assert deliver(store, sqlite_tmp_path, "pr-send")["status"] == "unknown"
    with patch("creatidy_kernel.adapters.reference_forge.time.time", return_value=102):
        with SQLiteProgramStore(path) as store:
            assert deliver(store, sqlite_tmp_path)["status"] == "unknown"
            assert deliver(store, sqlite_tmp_path)["status"] == "unknown"
            assert store.operation(PR_OPERATION).attempts == 1
    with SQLiteProgramStore(sqlite_tmp_path / "synthetic-forge" / "forge.sqlite3") as remote:
        assert remote.artifact("synthetic:pr", "pull").count(b'"number": 1') == 1


def test_missing_acceptance_and_bad_head_fail_before_intent(sqlite_tmp_path: Path) -> None:
    with SQLiteProgramStore(sqlite_tmp_path / "controller.sqlite3") as store:
        for head, acceptance in ((HEAD, ""), ("not-a-full-sha", "accepted:second")):
            with pytest.raises(ValueError):
                deliver_reference_pr(
                    store, repository_path=sqlite_tmp_path / "repository", head=head, acceptance_reference=acceptance
                )


def test_known_unaccepted_pr_handle_recovers_then_moved_head_stales(sqlite_tmp_path: Path) -> None:
    class DelayedReadback(SyntheticForgeTransport):
        unavailable = True

        def request(self, method: str, path: str, body: Mapping[str, object] | None = None) -> tuple[int, object]:
            if self.unavailable and "/pulls/" in path:
                return 503, {}
            return super().request(method, path, body)

    transport = DelayedReadback(SYNTHETIC_REPOSITORY)
    forge = ForgejoForge(
        transport, transport, lambda effect: effect.revision is not None and effect.revision.value == f"forgejo:{HEAD}"
    )
    path = sqlite_tmp_path / "controller.sqlite3"

    def invoke(store: SQLiteProgramStore) -> dict[str, object]:
        return deliver_reference_pr(
            store,
            repository_path=sqlite_tmp_path / "repository",
            head=HEAD,
            acceptance_reference="accepted:second",
            forge=forge,
        )

    with patch("creatidy_kernel.adapters.reference_forge.time.time", return_value=100):
        with SQLiteProgramStore(path) as store:
            assert invoke(store)["status"] == "unknown"
            assert store.operation(PR_OPERATION).accepted_reference is None
            assert store.artifact(PR_OPERATION, "known-reference") == b"forgejo:synthetic/reference#1"
    transport.unavailable = False
    with patch("creatidy_kernel.adapters.reference_forge.time.time", return_value=102):
        with SQLiteProgramStore(path) as store:
            assert invoke(store)["status"] == "accepted"
            assert store.operation(PR_OPERATION).attempts == 1
            transport.pulls[0]["head"] = {
                "sha": "c" * 40,
                "ref": "refs/pull/1/head",
                "repo": {"full_name": "synthetic/reference"},
            }
            assert invoke(store)["status"] == "stale"
    assert transport.pushes == 1
