# SPDX-License-Identifier: Apache-2.0
"""Installed collection primitives shared with the bounded native proofs."""

import os
from pathlib import Path

import pytest

from creatidy_kernel.adapters import worker_collection, worker_files
from tools.codex_oci_proof import collect
from tools.oci_worker_poc import saferead


def test_proof_tools_reuse_one_installed_implementation() -> None:
    assert collect.collect_directory is worker_collection.collect_directory
    assert collect.SafeCollection is worker_collection.SafeCollection
    assert collect.read_bounded_regular is worker_files.read_bounded_regular
    assert saferead.read_bounded_regular is worker_files.read_bounded_regular
    assert saferead.read_bounded_fd is worker_files.read_bounded_fd


def test_installed_collection_delivers_exact_bytes_and_reports_refusals(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "result").write_bytes(b"bounded candidate")
    (root / "bad-link").symlink_to(root / "result")
    captured: dict[str, bytes] = {}

    def sink(relative: str, data: bytes, record: worker_collection.CollectedFile) -> None:
        assert record.path == relative and record.size == len(data)
        captured[relative] = data

    result = worker_collection.collect_directory(root, sink=sink)
    assert captured == {"result": b"bounded candidate"}
    assert result.refusals == {"bad-link": "symlink-refused"}
    assert result.files["result"].sha256 == worker_collection.sha256_file(root / "result")


def test_shared_reader_refuses_leaf_symlinks_and_oversize(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"1234")
    link = tmp_path / "link"
    link.symlink_to(regular)
    assert worker_files.read_bounded_regular(regular, limit=4) == b"1234"
    with pytest.raises(ValueError, match="bound"):
        worker_files.read_bounded_regular(regular, limit=3)
    with pytest.raises(ValueError, match="symlink"):
        worker_files.read_bounded_regular(link)


def test_growth_probe_reads_only_one_excess_byte(tmp_path: Path) -> None:
    target = tmp_path / "grown"
    target.write_bytes(b"a" * 100)
    fd = os.open(target, os.O_RDONLY)
    try:
        assert worker_files.read_bounded_fd(fd, declared_size=1, limit=3) is None
        assert os.lseek(fd, 0, os.SEEK_CUR) == 4
    finally:
        os.close(fd)


@pytest.mark.parametrize("limit", (-1, True))
def test_invalid_bound_is_refused_before_read(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        worker_files.read_bounded_regular(tmp_path / "absent", limit=limit)
