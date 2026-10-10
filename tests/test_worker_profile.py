# SPDX-License-Identifier: Apache-2.0
"""Shared inert OCI command composition, not native enforcement reception."""

from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from creatidy_kernel.adapters.worker_profile import WorkerMount, WorkerProfile, rootless_run_prefix
from tools.codex_oci_proof import boundary
from tools.oci_worker_poc import profile as proof_profile
from tools.oci_worker_poc.evidence import ToolRecord
from tools.oci_worker_poc.toolchain import Toolchain


def profile() -> WorkerProfile:
    return WorkerProfile(
        "attempt-one",
        (WorkerMount(Path("/prepared/workspace"), PurePosixPath("/workspace"), True),),
        (("HOME", "/home/worker"),),
        Path("/prepared/seccomp.json"),
        64,
        268435456,
        0.5,
        45,
    )


def toolchain() -> Toolchain:
    root = Path("/explicit/toolroot")

    def record(name: str) -> ToolRecord:
        return ToolRecord(name, str(root / "bin" / name), "synthetic", "0" * 64, "synthetic")

    return Toolchain(root, record("podman"), record("crun"), record("conmon"), record("netavark"), record("busybox"))


def test_exact_fixed_prefix_and_shared_proof_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNTHETIC_AMBIENT_SECRET", "fixture-must-not-be-inherited")
    expected = [
        "/explicit/toolroot/bin/podman",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        "owned-one",
        "--hostname",
        "attempt-one",
        "--network=none",
        "--cap-drop=all",
        "--security-opt",
        "no-new-privileges",
        "--security-opt",
        "seccomp=/prepared/seccomp.json",
        "--read-only",
        "--read-only-tmpfs",
        "--tmpfs",
        "/tmp:rw,size=32m,mode=1777",  # noqa: S108 - expected container-internal tmpfs argument.
        "--tmpfs",
        "/home/worker:rw,size=8m,mode=700",
        "--pids-limit",
        "64",
        "--memory",
        "268435456",
        "--cpus",
        "0.5",
        "--stop-timeout",
        "10",
        "-v",
        "/prepared/workspace:/workspace:rw",
        "--env",
        "HOME=/home/worker",
    ]
    assert rootless_run_prefix(toolchain().root / "bin/podman", profile(), "owned-one") == expected
    suffix = ["localhost/proof:v1", "/bin/busybox", "sh", "-c", "exec /toolchain/bin/python3.12 -S /worker/main.py"]
    assert proof_profile.build_run_argv(toolchain(), profile(), suffix[0], "owned-one") == expected + suffix
    assert (
        boundary.build_run_argv(toolchain(), profile(), suffix[0], "owned-one")
        == expected[:2] + ["-i"] + expected[2:] + suffix
    )
    assert proof_profile.WorkerProfile is WorkerProfile and boundary.WorkerProfile is WorkerProfile
    assert proof_profile.WorkerMount is WorkerMount and boundary.WorkerMount is WorkerMount
    assert "fixture-must-not-be-inherited" not in " ".join(expected)


@pytest.mark.parametrize("field", ("pids_limit", "memory_bytes", "deadline_seconds"))
@pytest.mark.parametrize("value", (0, -1, True, 2**63))
def test_invalid_integer_limits_are_refused(field: str, value: int) -> None:
    with pytest.raises(ValueError, match="integer limit"):
        rootless_run_prefix(Path("/explicit/podman"), replace(profile(), **{field: value}), "owned")


@pytest.mark.parametrize("value", (0.0, -1.0, True, float("nan"), float("inf")))
def test_invalid_cpu_limit_is_refused(value: float) -> None:
    with pytest.raises(ValueError, match="CPU"):
        rootless_run_prefix(Path("/explicit/podman"), replace(profile(), cpus=value), "owned")


@pytest.mark.parametrize("name", ("", "-option", "spaces are unsafe", "a\nline", "a:name"))
def test_identity_cannot_become_an_option_or_delimiter(name: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        rootless_run_prefix(Path("/explicit/podman"), profile(), name)


@pytest.mark.parametrize(
    "path", (Path("relative"), Path("/prepared/../other"), Path("/prepared/bad:option"), Path("/prepared/bad\nline"))
)
def test_mount_sources_are_absolute_and_unambiguous(path: Path) -> None:
    mount = WorkerMount(path, PurePosixPath("/workspace"), True)
    with pytest.raises(ValueError, match="resource path"):
        rootless_run_prefix(Path("/explicit/podman"), replace(profile(), mounts=(mount,)), "owned")


def test_duplicate_destinations_and_environment_are_refused() -> None:
    mount = profile().mounts[0]
    with pytest.raises(ValueError, match="duplicate OCI mount"):
        rootless_run_prefix(Path("/explicit/podman"), replace(profile(), mounts=(mount, mount)), "owned")
    for entries in (
        (("HOME", "/home/worker"), ("HOME", "/other")),
        (("BAD=NAME", "value"),),
        (("VALID", "bad\nvalue"),),
    ):
        with pytest.raises(ValueError, match="environment"):
            rootless_run_prefix(Path("/explicit/podman"), replace(profile(), env_allowlist=entries), "owned")
