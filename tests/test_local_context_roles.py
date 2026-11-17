# SPDX-License-Identifier: Apache-2.0
"""No ambient documents, credentials, symlinks or remote fallbacks in context tests."""

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from creatidy_kernel.adapters import local_context as local_context_module
from creatidy_kernel.adapters.forge_refs import ForgeBinding
from creatidy_kernel.adapters.intake import GitContentSource
from creatidy_kernel.adapters.local_context import LocalDocumentKnowledge
from creatidy_kernel.adapters.project_drivers import RepositoryDocumentation
from creatidy_kernel.core.drivers import DriverBinding, DriverSubject
from creatidy_kernel.core.forge import Presence
from tools.check_drivers import Clock


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "changed",
        "missing",
        "denied",
        "revoked",
        "symlink",
        "parent-symlink",
        "root-replaced",
        "hardlink",
        "oversized",
        "fifo",
        "directory",
        "during-read",
        "during-parent-rename",
        "expired",
        "during-revocation",
        "config-drift",
        "nonboolean-permission",
        "late-authorization",
        "authorization-replacement",
    ],
)
def test_authorized_local_document_snapshot_refuses_unsafe_or_withdrawn_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root = tmp_path / "documents"
    folder = root / "nested"
    folder.mkdir(parents=True)
    path = folder / "guide.txt"
    content = b"approved document\n"
    path.write_bytes(content)
    clock = Clock()
    allowed = [True]
    calls = [0]

    def authorized(subject: DriverSubject, now: int) -> bool:
        assert subject.key == "nested/guide.txt" and now >= 100
        calls[0] += 1
        if mode == "nonboolean-permission":
            return cast(bool, "false")
        if mode == "late-authorization" and calls[0] == 2:
            clock.now += 31
        if mode == "authorization-replacement" and calls[0] == 2:
            folder.rename(root / "moved-during-authorization")
        return allowed[0] and not (mode == "during-revocation" and calls[0] == 2)

    binding = DriverBinding(
        "knowledge",
        "local-documents",
        "1",
        "https://owner-documents.example.test",
        "owner",
        "selected-native-document-root",
        "file-snapshot-v1",
        "owner:selected-file-read",
    )
    reader = LocalDocumentKnowledge(binding, root, clock.wall, authorized, max_bytes=32)
    # Obtain the initial explicitly authorized content snapshot before applying a fault.
    selected_mode = mode
    mode = "normal"
    initial = reader.snapshot("nested/guide.txt")
    mode = selected_mode
    calls[0] = 0
    revision = "sha256:" + hashlib.sha256(content).hexdigest()
    assert initial.complete and initial.subject == DriverSubject(binding, "document", "nested/guide.txt", revision)
    initial.require_current(100)
    if mode == "changed":
        path.write_bytes(b"different bytes")
    elif mode == "missing":
        path.unlink()
    elif mode == "denied":
        path.chmod(0)
    elif mode == "revoked":
        allowed[0] = False
    elif mode == "config-drift":
        reader.max_bytes = 64
    elif mode == "symlink":
        outside = tmp_path / "outside-secret.txt"
        outside.write_bytes(b"synthetic secret must not be received")
        path.unlink()
        path.symlink_to(outside)
    elif mode == "parent-symlink":
        folder.rename(root / "saved")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "guide.txt").write_bytes(b"synthetic secret must not be received")
        folder.symlink_to(outside, target_is_directory=True)
    elif mode == "root-replaced":
        root.rename(tmp_path / "old-root")
        folder.mkdir(parents=True)
        path.write_bytes(content)
    elif mode == "hardlink":
        os.link(path, tmp_path / "another-name")
    elif mode == "oversized":
        path.write_bytes(b"x" * 33)
    elif mode == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif mode == "directory":
        path.unlink()
        path.mkdir()
    elif mode in {"during-read", "during-parent-rename", "expired"}:
        original = os.read
        changed = [False]

        def intercepted(descriptor: int, size: int) -> bytes:
            raw = original(descriptor, size)
            if not changed[0]:
                changed[0] = True
                if mode == "during-read":
                    path.write_bytes(b"modified after initial bytes")
                elif mode == "during-parent-rename":
                    folder.rename(root / "renamed-parent")
                else:
                    clock.now += 31
            return raw

        monkeypatch.setattr(
            local_context_module,
            "os",
            SimpleNamespace(
                open=os.open,
                read=intercepted,
                fstat=os.fstat,
                close=os.close,
                O_RDONLY=os.O_RDONLY,
                O_DIRECTORY=os.O_DIRECTORY,
                O_NOFOLLOW=os.O_NOFOLLOW,
                O_NONBLOCK=os.O_NONBLOCK,
            ),
        )
    received = reader.content("nested/guide.txt", revision)
    if mode == "normal":
        assert received.complete and bytes.fromhex(json.loads(received.native)["content_hex"]) == content
        assert received.subject.revision == revision and received.fresh_until == 130
        assert "not-a-document-version-service" in received.qualification
    else:
        assert not received.complete and received.presence is not Presence.FOUND
        assert "synthetic secret" not in received.native.decode()
        if mode == "config-drift":
            assert calls[0] == 0


@pytest.mark.parametrize(
    "key", ["", ".", "../guide", "/guide", "nested//guide", "nested/./guide", "nested\\guide", "nested/\0guide"]
)
def test_local_document_key_refuses_before_file_or_authority_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    binding = DriverBinding(
        "knowledge",
        "local-documents",
        "1",
        "https://owner-documents.example.test",
        "owner",
        "selected",
        "file-snapshot-v1",
        "owner:read",
    )

    def unauthorized(subject: DriverSubject, now: int) -> bool:
        raise AssertionError("invalid path must not reach authorization")

    reader = LocalDocumentKnowledge(binding, tmp_path, lambda: 100, unauthorized)

    def no_open(*args: object, **kwargs: object) -> int:
        raise AssertionError("invalid path must not access files")

    monkeypatch.setattr(local_context_module, "os", SimpleNamespace(open=no_open))
    with pytest.raises(ValueError, match="document key"):
        reader.content(key, "sha256:" + "a" * 64)


def test_local_source_root_symlink_has_no_implicit_resolution(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    binding = DriverBinding(
        "knowledge",
        "local-documents",
        "1",
        "https://owner-documents.example.test",
        "owner",
        "selected",
        "file-snapshot-v1",
        "owner:read",
    )
    with pytest.raises(OSError):
        LocalDocumentKnowledge(binding, alias, lambda: 100, lambda subject, now: True)


def test_real_repository_documentation_is_immutable_and_refuses_source_drift(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    executable = shutil.which("git", path=os.defpath)
    assert executable is not None

    def git(*arguments: str) -> str:
        return subprocess.run(  # noqa: S603 - Fixed synthetic fixture Git; no hooks or ambient config.
            [executable, "-C", str(repository), *arguments],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    git("init")
    (repository / "guide.txt").write_bytes(b"immutable original\n")
    git("add", "guide.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "synthetic context")
    revision = git("rev-parse", "HEAD")
    blob = git("rev-parse", revision + ":guide.txt")
    binding = DriverBinding(
        "knowledge",
        "repository-docs",
        "1",
        "https://source.example.test",
        "owner",
        "owner/source",
        "git-object-v1",
        "owner:exact-Git-context-read",
    )
    source = GitContentSource(repository, ForgeBinding(binding.origin, binding.namespace))
    reader = RepositoryDocumentation(binding, source, lambda: 100)
    first = reader.content("guide.txt", revision)
    assert first.complete and first.subject.kind == "document" and first.subject.revision == revision
    (repository / "guide.txt").unlink()
    retained = reader.content("guide.txt", revision)
    assert retained.native == first.native  # Working-tree disappearance does not withdraw an immutable Git blob.
    original = source.binding
    source.binding = ForgeBinding(binding.origin, "other/source")
    assert not reader.content("guide.txt", revision).complete
    source.binding = original
    (repository / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    assert not reader.content("guide.txt", revision).complete
    reader.binding = replace(binding, rights="owner:withdrawn")
    assert not reader.content("guide.txt", revision).complete
