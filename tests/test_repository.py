# SPDX-License-Identifier: Apache-2.0
import ast
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_license_and_runtime_dependency_contract() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["license"] == "Apache-2.0"
    assert config["project"]["license-files"] == ["LICENSE", "NOTICE"]
    assert config["project"]["dependencies"] == []
    groups = config["dependency-groups"]
    dev = set(item for item in groups["dev"] if isinstance(item, str))
    for item in groups["dev"]:
        if isinstance(item, dict):
            dev.update(groups[item["include-group"]])
    assert set(config["build-system"]["requires"]) <= dev
    assert config["tool"]["uv"]["no-build-isolation-package"] == ["creatidy-kernel"]
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert len(license_text) > 10000
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    # Public source commit, not a credential.
    assert "81b8e6393a1b2df8a6c7a9c6c3dcb8a7f6884e4e" in notice  # pragma: allowlist secret
    assert "scarcity_router/remote.py" in notice
    for path in (ROOT / "src").rglob("*.py"):
        assert path.read_text(encoding="utf-8").startswith("# SPDX-License-Identifier: Apache-2.0"), path


def test_local_document_links_resolve() -> None:
    paths = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
    for path in paths:
        for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            assert (path.parent / target.split("#")[0]).exists(), f"{path.relative_to(ROOT)}: {target}"


def test_ci_is_canonical_with_pinned_actions_and_no_configured_secrets() -> None:
    workflow_text = (ROOT / ".forgejo" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    workflow = cast(dict[str, object], yaml.safe_load(workflow_text))
    events = cast(dict[str, object], workflow["on"])
    assert set(events) == {"push", "pull_request"}
    assert all(cast(dict[str, object], event)["branches"] == ["develop"] for event in events.values())
    # Forgejo ignores GitHub's permissions key; YAML cannot prove token isolation.
    assert "permissions" not in workflow
    jobs = cast(dict[str, object], workflow["jobs"])
    assert set(jobs) == {"check"}
    job = cast(dict[str, object], jobs["check"])
    steps = cast(list[dict[str, object]], job["steps"])
    for step in steps:
        uses = step.get("uses")
        if isinstance(uses, str):
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses)
            if uses.startswith("actions/checkout@"):
                options = cast(dict[str, object], step["with"])
                assert options["fetch-depth"] == 0
                assert options["persist-credentials"] is False
            if uses.startswith("astral-sh/setup-uv@"):
                options = cast(dict[str, object], step["with"])
                assert options["github-token"] == ""
    assert "persist-credentials: false" in workflow_text
    assert not re.search(r"\bsecrets\s*[.\[]", yaml.safe_dump(workflow))
    assert "make check" in workflow_text and "make package-check" in workflow_text
    assert not list((ROOT / ".github" / "workflows").glob("*"))


def test_parallel_and_serial_test_partitions_are_complete(tmp_path: Path) -> None:
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.defpath,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }

    def collect(*selection: str) -> set[str]:
        result = subprocess.run(  # noqa: S603 - fixed pytest invocation with synthetic environment.
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", *selection],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        items = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
        assert len(items) == len(set(items))
        return set(items)

    all_tests = collect()
    parallel = collect("-m", "not serial_native")
    serial = collect("-m", "serial_native")
    assert all_tests and serial and parallel
    assert not parallel & serial
    assert all_tests == parallel | serial
    native_files = (
        "tests/test_bubblewrap_verification.py::",
        "tests/test_codex_oci_isolation.py::",
        "tests/test_kilo_native_runtime.py::",
        "tests/test_oci_worker_isolation.py::",
        "tests/test_ordinary_program.py::test_ordinary_two_obligation_program_with_real_native_cold_review[",
    )
    for prefix in native_files:
        selected = {item for item in all_tests if item.startswith(prefix)}
        assert selected and selected <= serial
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "uv run --locked pytest -m serial_native" in makefile
    package_check = (ROOT / "tools/check_package.py").read_text(encoding="utf-8")
    assert '"not serial_native"' in package_check and '"pytest"' in package_check


def test_isolated_child_uses_installed_wheel(tmp_path: Path) -> None:
    configured = os.environ.get("CREATIDY_TEST_INSTALLED_PREFIX")
    if configured is None:
        return
    result = subprocess.run(  # noqa: S603 - fixed isolated interpreter and synthetic environment.
        [sys.executable, "-I", "-c", "import creatidy_kernel; print(creatidy_kernel.__file__)"],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": os.defpath},
        check=True,
        capture_output=True,
        text=True,
    )
    origin = Path(result.stdout.strip()).resolve(strict=True)
    assert origin.is_relative_to(Path(configured).resolve(strict=True))
    assert not origin.is_relative_to(ROOT)


def test_public_runtime_uses_neutral_task_names() -> None:
    # Historical docs may retain old names. Only the two explicit negative
    # regression tests and the rejecting parser may mention them in live source.
    pattern = re.compile("dogfood", re.IGNORECASE)
    for path in (ROOT / "src").rglob("*"):
        if not path.is_file() or path.suffix != ".py":
            continue
        assert not pattern.search(path.name), path
        for line in path.read_text().splitlines():
            if pattern.search(line):
                assert path.name == "task_execution.py" and 'name.startswith("CREATIDY_" + "DOGFOOD_")' in line
    for path in (ROOT / "tests").glob("*.py"):
        assert not pattern.search(path.name), path
        text = path.read_text()
        negative_tests = {
            "test_legacy_environment_is_rejected_without_fallback",
            "test_legacy_command_is_not_a_compatibility_alias",
            "test_public_runtime_uses_neutral_task_names",
        }
        allowed_lines = {
            line
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.FunctionDef) and node.name in negative_tests
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
        }
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                assert number in allowed_lines, f"{path}:{number}"
    for path in (ROOT / "README.md", ROOT / "Makefile", ROOT / "pyproject.toml"):
        assert not pattern.search(path.read_text()), path
