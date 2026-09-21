# SPDX-License-Identifier: Apache-2.0
import re
import tomllib
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_license_and_runtime_dependency_contract() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["license"] == "Apache-2.0"
    assert config["project"]["license-files"] == ["LICENSE"]
    assert config["project"]["dependencies"] == []
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert len(license_text) > 10000
    for path in (ROOT / "src").rglob("*.py"):
        assert path.read_text(encoding="utf-8").startswith("# SPDX-License-Identifier: Apache-2.0"), path


def test_local_document_links_resolve() -> None:
    paths = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))
    for path in paths:
        for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            assert (path.parent / target.split("#")[0]).exists(), f"{path.relative_to(ROOT)}: {target}"


def test_ci_is_canonical_read_only_and_actions_are_pinned() -> None:
    workflow_text = (ROOT / ".forgejo" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    workflow = cast(dict[str, object], yaml.safe_load(workflow_text))
    events = cast(dict[str, object], workflow["on"])
    assert set(events) == {"push", "pull_request"}
    assert all(cast(dict[str, object], event)["branches"] == ["develop"] for event in events.values())
    assert workflow["permissions"] == {"contents": "read"}
    jobs = cast(dict[str, object], workflow["jobs"])
    assert set(jobs) == {"check"}
    job = cast(dict[str, object], jobs["check"])
    steps = cast(list[dict[str, str]], job["steps"])
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    assert "persist-credentials: false" in workflow_text
    assert "secrets." not in workflow_text
    assert "make check" in workflow_text and "make package-check" in workflow_text
    assert not list((ROOT / ".github" / "workflows").glob("*"))
