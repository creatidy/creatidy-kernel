# SPDX-License-Identifier: Apache-2.0
"""Controller-owned source acquisition: placement-independent, canonical, read-only."""

import os
from pathlib import Path

import pytest

from creatidy_kernel.adapters import source_cache
from creatidy_kernel.adapters.forge_refs import https_origin
from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.source_cache import (
    ACQUIRED_MARKER,
    SourceAcquisitionError,
    SourceCategory,
    acquire_source,
    default_cache_root,
    source_cache_key,
)

CANONICAL = "https://forge.invalid/BioMedical-IT/scarcity-router"

pytest_plugins = ["test_sqlite_store"]


def make_remote(directory: Path) -> Path:
    """Synthetic stand-in for the canonical task remote; local transport only."""
    directory.mkdir(parents=True)
    reference_git(directory, "init", "--initial-branch=develop")
    (directory / "README.md").write_text("fixture remote\n")
    reference_git(directory, "add", "-A")
    reference_git(directory, "commit", "-m", "fixture remote base")
    return directory


def test_cache_root_follows_absolute_xdg_and_ignores_relative_overrides() -> None:
    assert default_cache_root({"XDG_CACHE_HOME": "/absolute/cache"}) == Path("/absolute/cache/creatidy-kernel/source")
    home_default = default_cache_root({"XDG_CACHE_HOME": "relative-is-ignored"})
    assert home_default == Path.home() / ".cache" / "creatidy-kernel" / "source"
    assert default_cache_root({}) == Path.home() / ".cache" / "creatidy-kernel" / "source"


def test_cache_key_is_deterministic_and_validated() -> None:
    assert source_cache_key(CANONICAL) == ("BioMedical-IT", "scarcity-router")
    assert source_cache_key(CANONICAL + ".git") == ("BioMedical-IT", "scarcity-router")
    with pytest.raises(SourceAcquisitionError) as path_error:
        source_cache_key("https://forge.invalid/only-one-segment")
    assert path_error.value.category is SourceCategory.REMOTE_MISMATCH
    with pytest.raises(ValueError, match="HTTPS"):
        acquire_source("http://forge.invalid/BioMedical-IT/scarcity-router")


def test_acquisition_verifies_canonical_origin_from_two_unrelated_layouts(
    sqlite_tmp_path: Path,
) -> None:
    """Placement independence: the same logical cache results from unrelated layouts."""
    layouts = (
        (sqlite_tmp_path / "one" / "deep" / "nest" / "checkout", sqlite_tmp_path / "two" / "state-x"),
        (
            sqlite_tmp_path / "B" / "totally" / "different" / "and" / "much" / "deeper" / "repo",
            sqlite_tmp_path / "z" / "other" / "state-y",
        ),
    )
    expected_base: str | None = None
    for remote_location, state_location in layouts:
        remote = make_remote(remote_location)
        cache_root = sqlite_tmp_path / f"cache-{state_location.name}"
        cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
        assert cache == cache_root / "BioMedical-IT" / "scarcity-router"
        assert reference_git(cache, "config", "--get", "remote.origin.url") == CANONICAL
        base = reference_git(cache, "rev-parse", "refs/remotes/origin/develop")
        if expected_base is None:
            expected_base = base
        assert base == expected_base
        # No checkout-placement information may leak into the acquired source.
        config = (cache / ".git" / "config").read_text()
        assert str(remote) not in config
        assert str(remote_location.parent) not in config
        assert (cache / ".git" / ACQUIRED_MARKER).is_file()


def test_acquisition_never_guesses_sibling_checkouts(sqlite_tmp_path: Path) -> None:
    """A canonical checkout adjacent to state or cache must never be adopted or touched."""
    remote = make_remote(sqlite_tmp_path / "remote" / "anywhere")
    cache_root = sqlite_tmp_path / "xdg-cache"
    state_neighbor = sqlite_tmp_path / "state" / "scarcity-router"
    make_remote(state_neighbor)
    reference_git(state_neighbor, "remote", "add", "origin", CANONICAL)
    decoy_marker = state_neighbor / "operator-file.txt"
    decoy_marker.write_text("operator-owned\n")
    decoy_head = reference_git(state_neighbor, "rev-parse", "HEAD")

    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))

    assert cache == cache_root / "BioMedical-IT" / "scarcity-router"
    assert decoy_marker.read_text() == "operator-owned\n"
    assert reference_git(state_neighbor, "rev-parse", "HEAD") == decoy_head


def test_existing_cache_is_fetched_not_recloned(sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reuse refreshes remote-tracking refs from origin; it never re-clones."""
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    marker = cache / ".git" / ACQUIRED_MARKER
    created = (marker.read_bytes(), marker.stat().st_mtime_ns)
    reference_git(remote, "commit", "--allow-empty", "-m", "second remote commit")
    advanced = reference_git(remote, "rev-parse", "refs/heads/develop")
    # Native-shaped transport fake: the canonical origin here is a fake HTTPS URL, so
    # the recorded fetch is served from the local mirror with the same refspec.
    real_run = source_cache.run_git_bounded
    fetches: list[tuple[str, ...]] = []

    def fetch_from_mirror(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int):
        if argv[1:2] == ["fetch"]:
            fetches.append(tuple(argv[1:]))
            return real_run(
                [argv[0], "fetch", "--prune", str(remote), "+refs/heads/*:refs/remotes/origin/*"],
                cwd,
                env,
                timeout,
                max_bytes,
            )
        return real_run(argv, cwd, env, timeout, max_bytes)

    monkeypatch.setattr(source_cache, "run_git_bounded", fetch_from_mirror)
    acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))

    assert fetches == [("fetch", "--prune", "origin")]
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == created
    assert reference_git(cache, "rev-parse", "refs/remotes/origin/develop") == advanced


def test_failed_canonical_fetch_fails_closed_without_recloning(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    marker = cache / ".git" / ACQUIRED_MARKER
    created = (marker.read_bytes(), marker.stat().st_mtime_ns)

    with pytest.raises(SourceAcquisitionError) as error:
        acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))

    assert error.value.category is SourceCategory.FETCH_FAILED
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == created


def test_foreign_or_corrupt_cache_entries_are_replaced_not_adopted(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    target = cache_root / "BioMedical-IT" / "scarcity-router"
    target.mkdir(parents=True)
    (target / "operator-file.txt").write_text("not a repository\n")
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    assert reference_git(cache, "config", "--get", "remote.origin.url") == CANONICAL
    assert not (cache / "operator-file.txt").exists()

    unrelated = acquire_source("https://forge.invalid/Other/Repository", cache_root=cache_root, clone_from=str(remote))
    assert unrelated == cache_root / "Other" / "Repository"


def test_mismatched_origin_cache_is_replaced(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    reference_git(cache, "remote", "set-url", "origin", "https://other.invalid/Other/repo.git")

    repaired = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))

    assert repaired == cache
    assert reference_git(cache, "config", "--get", "remote.origin.url") == CANONICAL


def test_dirty_cache_fails_closed_without_repair(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    (cache / "uncommitted.txt").write_text("dirty\n")

    with pytest.raises(SourceAcquisitionError) as error:
        acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    assert error.value.category is SourceCategory.UNSUITABLE_CACHE
    assert (cache / "uncommitted.txt").read_text() == "dirty\n"


def test_unreachable_transport_and_symlinked_locations_fail_with_closed_categories(
    sqlite_tmp_path: Path,
) -> None:
    with pytest.raises(SourceAcquisitionError) as clone_error:
        acquire_source(CANONICAL, cache_root=sqlite_tmp_path / "cache", clone_from=str(sqlite_tmp_path / "missing"))
    assert clone_error.value.category is SourceCategory.CLONE_FAILED

    real_root = sqlite_tmp_path / "real-cache-root"
    real_root.mkdir()
    link_root = sqlite_tmp_path / "linked-cache-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    remote = make_remote(sqlite_tmp_path / "remote-two")
    with pytest.raises(SourceAcquisitionError) as link_error:
        acquire_source(CANONICAL, cache_root=link_root, clone_from=str(remote))
    assert link_error.value.category is SourceCategory.UNSUITABLE_CACHE


def test_acquisition_uses_a_closed_transport_environment(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient Git configuration must never rewrite or redirect canonical acquisition."""
    remote = make_remote(sqlite_tmp_path / "remote")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://rewritten.invalid/pivot.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(remote))
    closed_home = sqlite_tmp_path / "closed-home"
    closed_home.mkdir()
    monkeypatch.setenv("HOME", str(closed_home))
    monkeypatch.setenv("PATH", os.defpath)

    cache = acquire_source(CANONICAL, cache_root=sqlite_tmp_path / "cache", clone_from=str(remote))

    assert reference_git(cache, "config", "--get", "remote.origin.url") == CANONICAL
    assert reference_git(cache, "rev-parse", "refs/remotes/origin/develop") != ""


def test_canonical_url_shape_is_bounded(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    escaping = "https://forge.invalid/BioMedical-IT/../escape"
    with pytest.raises(SourceAcquisitionError) as error:
        acquire_source(escaping, cache_root=sqlite_tmp_path / "cache", clone_from=str(remote))
    assert error.value.category is SourceCategory.REMOTE_MISMATCH
    assert https_origin(CANONICAL) == "https://forge.invalid"
