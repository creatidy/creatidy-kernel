# SPDX-License-Identifier: Apache-2.0
"""Controller-owned source acquisition: placement-independent, canonical, read-only."""

import json
import os
import selectors
import shutil
import subprocess
import sys
import threading
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
    source_use,
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
    assert source_cache_key(CANONICAL)[-2:] == ("BioMedical-IT", "scarcity-router")
    assert source_cache_key(CANONICAL + ".git") == source_cache_key(CANONICAL)
    with pytest.raises(SourceAcquisitionError) as path_error:
        source_cache_key("https://forge.invalid/only-one-segment")
    assert path_error.value.category is SourceCategory.REMOTE_MISMATCH
    with pytest.raises(SourceAcquisitionError, match="remote_mismatch"):
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
        assert cache == cache_root.joinpath(*source_cache_key(CANONICAL))
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

    assert cache == cache_root.joinpath(*source_cache_key(CANONICAL))
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


def test_failed_canonical_fetch_fails_closed_without_recloning(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    marker = cache / ".git" / ACQUIRED_MARKER
    created = (marker.read_bytes(), marker.stat().st_mtime_ns)
    real_run = source_cache.run_git_bounded

    def failed_fetch(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int):
        if argv[1] == "fetch":
            argv = [argv[0], "fetch", str(sqlite_tmp_path / "missing-local-mirror")]
        return real_run(argv, cwd, env, timeout, max_bytes)

    monkeypatch.setattr(source_cache, "run_git_bounded", failed_fetch)

    with pytest.raises(SourceAcquisitionError) as error:
        acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))

    assert error.value.category is SourceCategory.FETCH_FAILED
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == created


def test_hostless_foreign_entry_is_preserved_and_new_acquisition_is_separate(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    target = cache_root / "BioMedical-IT" / "scarcity-router"
    target.mkdir(parents=True)
    (target / "operator-file.txt").write_text("not a repository\n")
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    assert reference_git(cache, "config", "--get", "remote.origin.url") == CANONICAL
    assert not (cache / "operator-file.txt").exists()
    assert (target / "operator-file.txt").read_text() == "not a repository\n"

    unrelated = acquire_source("https://forge.invalid/Other/Repository", cache_root=cache_root, clone_from=str(remote))
    assert unrelated == cache_root.joinpath(*source_cache_key("https://forge.invalid/Other/Repository"))


def test_mismatched_origin_cache_is_refused_and_preserved(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache_root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    reference_git(cache, "remote", "set-url", "origin", "https://other.invalid/Other/repo.git")

    before = (cache / ".git" / "config").read_bytes()
    with pytest.raises(SourceAcquisitionError) as error:
        acquire_source(CANONICAL, cache_root=cache_root, clone_from=str(remote))
    assert error.value.category is SourceCategory.REMOTE_MISMATCH
    assert (cache / ".git" / "config").read_bytes() == before


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


@pytest.mark.parametrize(
    "url",
    [
        "http://forge.invalid/Owner/repo",
        "ssh://forge.invalid/Owner/repo",
        "https://user:password@forge.invalid/Owner/repo",  # pragma: allowlist secret - synthetic rejection fixture
        "https://forge.invalid/Owner/repo?q=secret",
        "https://forge.invalid/Owner/repo#secret",
        "https://forge.invalid/Owner/repo/",
        "https://forge.invalid//Owner/repo",
        "https://forge.invalid/Owner/repo/extra",
        "https://forge.invalid:/Owner/repo",
        "https://forge.invalid/Owner/..repo",
        "https://forge.invalid/Owner/repo\n",
    ],
)
def test_key_rejects_ambiguous_or_credential_bearing_urls(url: str) -> None:
    with pytest.raises(SourceAcquisitionError, match="remote_mismatch"):
        source_cache_key(url)


def test_hosts_and_ports_are_distinct_but_default_tls_port_is_equivalent(sqlite_tmp_path: Path) -> None:
    urls = [
        CANONICAL,
        CANONICAL.replace("forge.invalid", "another.invalid"),
        CANONICAL.replace("forge.invalid", "forge.invalid:8443"),
    ]
    caches: list[Path] = []
    for number, url in enumerate(urls):
        remote = make_remote(sqlite_tmp_path / f"remote-{number}")
        reference_git(remote, "commit", "--allow-empty", "-m", f"distinct-{number}")
        cache = acquire_source(url, cache_root=sqlite_tmp_path / "cache", clone_from=str(remote))
        caches.append(cache)
        assert reference_git(cache, "rev-parse", "HEAD") == reference_git(remote, "rev-parse", "HEAD")
        assert reference_git(cache, "config", "--get", "remote.origin.url") == url
    assert len(set(caches)) == 3
    assert source_cache_key(CANONICAL) == source_cache_key(
        CANONICAL.replace("forge.invalid", "FORGE.INVALID:443") + ".git"
    )


@pytest.mark.parametrize("marker", [None, "1\n", "{broken", '{"schema":2}'])
def test_unknown_qualified_entries_are_never_deleted_or_adopted(sqlite_tmp_path: Path, marker: str | None) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    target = root.joinpath(*source_cache_key(CANONICAL))
    make_remote(target)
    reference_git(target, "remote", "add", "origin", CANONICAL)
    canary = target / "operator-file.txt"
    canary.write_bytes(b"operator-owned\0bytes")
    if marker is not None:
        (target / ".git" / ACQUIRED_MARKER).write_text(marker)
    before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"):
        acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    assert before == {str(p.relative_to(target)): p.read_bytes() for p in target.rglob("*") if p.is_file()}


def test_copied_marker_cannot_authorize_a_different_root_or_inode(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    cache = acquire_source(CANONICAL, cache_root=sqlite_tmp_path / "first", clone_from=str(remote))
    foreign_root = sqlite_tmp_path / "second"
    foreign = foreign_root.joinpath(*source_cache_key(CANONICAL))
    shutil.copytree(cache, foreign)
    before = (foreign / ".git" / ACQUIRED_MARKER).read_bytes()
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"):
        acquire_source(CANONICAL, cache_root=foreign_root, clone_from=str(remote))
    assert (foreign / ".git" / ACQUIRED_MARKER).read_bytes() == before
    marker = json.loads(before)
    assert marker["checkout"][1] != foreign.stat().st_ino


@pytest.mark.parametrize("relative", [".git/config", ".git/objects", ".git/HEAD"])
def test_symlinked_git_paths_preserve_outside_canary(sqlite_tmp_path: Path, relative: str) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    path = cache / relative
    outside = sqlite_tmp_path / "outside"
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=outside.is_dir())
    before = outside.stat()
    with pytest.raises(SourceAcquisitionError):
        acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    assert path.is_symlink()
    assert outside.stat() == before


def test_use_lease_blocks_same_process_thread_and_an_exec_process(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    outcomes: list[SourceCategory] = []

    def contend() -> None:
        try:
            acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
        except SourceAcquisitionError as error:
            outcomes.append(error.category)

    locks = list((root / ".locks-v2").iterdir())
    identity = locks[0].stat().st_ino
    with source_use(cache, CANONICAL):
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcomes == [SourceCategory.LOCK_BUSY]
        code = """import sys
from pathlib import Path
from creatidy_kernel.adapters import source_cache as s
def forbidden(*args, **kwargs):
    raise AssertionError('Git must not run while leased')
s.run_git_bounded = forbidden
try:
    s.acquire_source(sys.argv[1], cache_root=Path(sys.argv[2]))
except s.SourceAcquisitionError as error:
    print(error.category.value)
else:
    raise AssertionError('acquisition bypassed current-use lease')
"""
        completed = subprocess.run(  # noqa: S603 - fixed fixture code, synthetic paths and closed environment.
            [sys.executable, "-I", "-c", code, CANONICAL, str(root)],
            env={"HOME": str(sqlite_tmp_path), "PATH": os.defpath},
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert completed.stdout.strip() == SourceCategory.LOCK_BUSY.value
    assert locks[0].stat().st_ino == identity


def test_interleaved_clone_is_not_published_until_complete(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    target = root.joinpath(*source_cache_key(CANONICAL))
    entered = threading.Event()
    release = threading.Event()
    real_run = source_cache.run_git_bounded
    results: list[Path] = []
    failures: list[BaseException] = []

    def paused(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int):
        if argv[1] == "clone":
            entered.set()
            assert release.wait(timeout=10)
        return real_run(argv, cwd, env, timeout, max_bytes)

    def acquire() -> None:
        try:
            results.append(acquire_source(CANONICAL, cache_root=root, clone_from=str(remote)))
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(source_cache, "run_git_bounded", paused)
    thread = threading.Thread(target=acquire)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        assert not target.exists()
        with pytest.raises(SourceAcquisitionError, match="source_busy"):
            acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    finally:
        release.set()
        thread.join(timeout=15)
    assert not thread.is_alive() and not failures
    assert results == [target]
    assert (target / ".git" / ACQUIRED_MARKER).is_file()


def test_interrupted_publication_retains_bytes_and_unique_staging(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    target = root.joinpath(*source_cache_key(CANONICAL))
    real_publish = source_cache._publish  # pyright: ignore[reportPrivateUsage] - forced publication boundary.

    def interrupted(_staging: Path, _target: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(source_cache, "_publish", interrupted)
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    stages = list(target.parent.glob(".source-staging-*"))
    assert len(stages) == 2 and not target.exists()
    before = {
        (stage.name, str(p.relative_to(stage))): p.read_bytes()
        for stage in stages
        for p in stage.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(source_cache, "_publish", real_publish)
    assert acquire_source(CANONICAL, cache_root=root, clone_from=str(remote)) == target
    assert before == {
        (stage.name, str(p.relative_to(stage))): p.read_bytes()
        for stage in stages
        for p in stage.rglob("*")
        if p.is_file()
    }


def test_replaced_lock_inode_cannot_bypass_an_active_use(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    lock = next((root / ".locks-v2").iterdir())
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"), source_use(cache, CANONICAL):
        lock.rename(lock.with_suffix(".retained"))
        with pytest.raises(SourceAcquisitionError, match="ownership_unproven"):
            acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    assert (cache / ".git" / ACQUIRED_MARKER).exists()


def test_fetch_and_current_use_contend_on_the_same_inode(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    entered = threading.Event()
    release = threading.Event()
    real_run = source_cache.run_git_bounded
    results: list[Path] = []
    failures: list[BaseException] = []

    def paused(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, max_bytes: int):
        if argv[1] == "fetch":
            entered.set()
            assert release.wait(timeout=10)
            argv = [argv[0], "fetch", "--prune", str(remote), "+refs/heads/*:refs/remotes/origin/*"]
        return real_run(argv, cwd, env, timeout, max_bytes)

    def fetch() -> None:
        try:
            results.append(acquire_source(CANONICAL, cache_root=root, clone_from=str(remote)))
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(source_cache, "run_git_bounded", paused)
    thread = threading.Thread(target=fetch)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(SourceAcquisitionError, match="source_busy"), source_use(cache, CANONICAL):
            pytest.fail("use cannot race an active refresh")
    finally:
        release.set()
        thread.join(timeout=15)
    assert not thread.is_alive() and not failures and results == [cache]


def test_actual_owned_target_substitution_is_refused_while_leased(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    retained = cache.with_name("retained-original")
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"), source_use(cache, CANONICAL):
        cache.rename(retained)
        cache.mkdir()
        (cache / "outside-canary").write_bytes(b"do not delete")
        with pytest.raises(SourceAcquisitionError, match="source_busy"):
            acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    assert (cache / "outside-canary").read_bytes() == b"do not delete"
    assert (retained / ".git" / ACQUIRED_MARKER).exists()
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"):
        acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))


def test_overflowing_native_git_retains_partial_clone_and_safe_diagnostics(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    fake_git = sqlite_tmp_path / "bounded-git"
    fake_git.write_text(
        "#!/usr/bin/python3\nimport os, pathlib, sys\n"
        "if sys.argv[1] != 'clone':\n"
        "    os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])\n"
        "(pathlib.Path(sys.argv[-1]) / 'partial-bytes').write_bytes(b'preserve me')\n"
        "sys.stderr.write('synthetic remote secret must not be echoed\\n')\n"
        "sys.stdout.write('x' * 200000)\n"
    )
    fake_git.chmod(0o700)
    real_which = shutil.which

    def which(name: str, *, path: str | None = None) -> str | None:
        return str(fake_git) if name == "git" else real_which(name, path=path)

    monkeypatch.setattr(source_cache.shutil, "which", which)
    with pytest.raises(SourceAcquisitionError) as failure:
        acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    assert failure.value.category is SourceCategory.CLONE_FAILED
    assert "synthetic remote secret" not in str(failure.value)
    target = root.joinpath(*source_cache_key(CANONICAL))
    assert not target.exists()
    stages = list(target.parent.glob(".source-staging-*"))
    assert len(stages) == 1 and (stages[0] / "partial-bytes").read_bytes() == b"preserve me"


def test_permission_and_native_mount_refusals_preserve_data(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    root.mkdir()
    canary = root / "operator-owned"
    canary.write_bytes(b"unchanged")
    root.chmod(0o770)
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"):
        acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    root.chmod(0o700)

    def unsupported(_directory: Path) -> str:
        raise source_cache.UnsupportedSQLiteConfiguration("synthetic unsupported mount")

    monkeypatch.setattr(source_cache, "validate_local_storage", unsupported)
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"):
        acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    assert canary.read_bytes() == b"unchanged" and not (root / ".locks-v2").exists()


@pytest.mark.parametrize("relative", [".git", ".git/objects"])
@pytest.mark.parametrize("filesystem", ["nfs", "ext4"])
def test_git_root_and_descendant_mounts_refuse_before_git(
    sqlite_tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, filesystem: str
) -> None:
    source = make_remote(sqlite_tmp_path / "source")
    original_read = Path.read_text
    table = Path("/proc/self/mountinfo")
    simulated = original_read(table).rstrip("\n") + (
        f"\n999999 1 0:999 / {source / relative} rw - {filesystem} synthetic:/repo rw\n"
    )

    def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == table:
            return simulated
        return original_read(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read)
    before = (source / ".git/config").read_bytes()
    with pytest.raises(SourceAcquisitionError, match="checkout_unsuitable"):
        source_cache.validate_checkout(source)
    assert (source / ".git/config").read_bytes() == before


@pytest.mark.parametrize("corrupt_lock", [False, True])
def test_owned_marker_requires_the_original_locked_metadata_token(
    sqlite_tmp_path: Path,
    corrupt_lock: bool,
) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    path = next((root / ".locks-v2").iterdir()) if corrupt_lock else cache / ".git" / ACQUIRED_MARKER
    if corrupt_lock:
        path.write_bytes(b'{"schema":')
    else:
        marker = json.loads(path.read_bytes())
        marker["token"] = "0" * 64
        path.write_text(json.dumps(marker))
    before = path.read_bytes()
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"):
        acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    assert path.read_bytes() == before and cache.exists()


def test_unowned_qualified_lookalike_use_writes_no_lockfiles(sqlite_tmp_path: Path) -> None:
    root = sqlite_tmp_path / "unowned"
    target = root.joinpath(*source_cache_key(CANONICAL))
    make_remote(target)
    before = set(root.rglob("*"))
    with pytest.raises(SourceAcquisitionError, match="ownership_unproven"), source_use(target, CANONICAL):
        pytest.fail("lookalike path cannot mint a use lease")
    assert set(root.rglob("*")) == before and not (root / ".locks-v2").exists()


def test_controller_death_keeps_native_git_use_leased_until_child_settlement(sqlite_tmp_path: Path) -> None:
    remote = make_remote(sqlite_tmp_path / "remote")
    root = sqlite_tmp_path / "cache"
    cache = acquire_source(CANONICAL, cache_root=root, clone_from=str(remote))
    reader, writer = os.pipe()
    code = """import os, subprocess, sys
from pathlib import Path
from creatidy_kernel.adapters import source_cache as s
ready = int(sys.argv[3])
real = s.run_git_bounded
native_popen = subprocess.Popen
def popen(argv, **kwargs):
    process = native_popen(argv, **kwargs)
    if argv[1:2] == ['cat-file']:
        os.write(ready, b'R')
    return process
subprocess.Popen = popen
def run(argv, cwd, env, timeout, max_bytes):
    if argv[1] == 'fetch':
        argv = [argv[0], 'cat-file', '--batch']
    return real(argv, cwd, env, timeout, max_bytes)
s.run_git_bounded = run
s.acquire_source(sys.argv[1], cache_root=Path(sys.argv[2]))
"""
    controller = subprocess.Popen(  # noqa: S603 - fixed synthetic controller; no network/ambient environment.
        [sys.executable, "-I", "-c", code, CANONICAL, str(root), str(writer)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={"HOME": str(sqlite_tmp_path), "PATH": os.defpath},
        pass_fds=(writer,),
    )
    os.close(writer)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(reader, selectors.EVENT_READ)
            assert selector.select(timeout=10), "native child did not enter its forced use window"
            assert os.read(reader, 1) == b"R"
        controller.kill()
        controller.wait(timeout=5)
        with pytest.raises(SourceAcquisitionError, match="source_busy"), source_use(cache, CANONICAL):
            pytest.fail("a surviving native Git child still owns the source lease")
        assert controller.stdin is not None
        controller.stdin.close()
        with source_use(cache, CANONICAL):
            assert cache.exists()
    finally:
        os.close(reader)
        if controller.stdin is not None and not controller.stdin.closed:
            controller.stdin.close()
        if controller.poll() is None:
            controller.kill()
        controller.wait(timeout=5)
        if controller.stderr is not None:
            controller.stderr.close()
