# SPDX-License-Identifier: Apache-2.0
import ast
import re
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
    assert set(config["build-system"]["requires"]) <= set(config["dependency-groups"]["dev"])
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
    steps = cast(list[dict[str, str]], job["steps"])
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    assert "persist-credentials: false" in workflow_text
    assert not re.search(r"\bsecrets\s*[.\[]", yaml.safe_dump(workflow))
    assert "make check" in workflow_text and "make package-check" in workflow_text
    assert not list((ROOT / ".github" / "workflows").glob("*"))


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
