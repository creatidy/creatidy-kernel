# SPDX-License-Identifier: Apache-2.0
"""Installed, offline source acquisition/admission smoke on owned native fixtures."""

import os
import sys
from dataclasses import replace
from pathlib import Path

from creatidy_kernel.adapters.reference import reference_git
from creatidy_kernel.adapters.source_cache import acquire_source, source_cache_key, source_use
from creatidy_kernel.adapters.task_execution import VerificationCommand, scarcity_router_143_task, validate_task_source


def main(directory: Path) -> None:
    remote = directory / "synthetic-remote"
    remote.mkdir()
    reference_git(remote, "init", "--initial-branch=develop")
    task = replace(
        scarcity_router_143_task(),
        task_id="installed-source-smoke",
        repository_url="https://source-smoke.invalid/BioMedical-IT/scarcity-router",
        verification=(VerificationCommand((sys.executable, "-m", "unittest", "tests.test_e2e_execution"), 30),),
    )
    tests = remote / "tests"
    tests.mkdir()
    content = "import unittest\nclass CancellationTests(unittest.TestCase):\n"
    for name in task.structural.required_names:
        content += (
            f"    def {name}(self):\n"
            "        for _ in range(1):\n"
            "            piece = raw.recv(1)\n"
            "            if not piece:\n"
            "                break\n"
            "        self.assertTrue(worker.cancels)\n"
            "        self.assertEqual([], bystander.cancels)\n"
        )
    (tests / "test_e2e_execution.py").write_text(content)
    reference_git(remote, "add", "-A")
    reference_git(remote, "commit", "-m", "synthetic installed source")
    base = reference_git(remote, "rev-parse", "HEAD")
    source = acquire_source(
        task.repository_url, cache_root=directory / "cache", clone_from=str(remote), environment={"PATH": os.defpath}
    )
    if source != (directory / "cache").joinpath(*source_cache_key(task.repository_url)):
        raise SystemExit("Installed source key mismatch")
    with source_use(source, task.repository_url):
        checked, _, evidence = validate_task_source(source, replace(task, expected_base_sha=base), {"PATH": os.defpath})
        if checked != base or evidence["verification_executed"] is not False:
            raise SystemExit("Installed source admission mismatch")
    if reference_git(source, "config", "--get", "remote.origin.url") != task.repository_url:
        raise SystemExit("Installed canonical source mismatch")
    print(
        "Installed source smoke: host-qualified acquisition, owned lease, exact local admission; no inference/network"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
