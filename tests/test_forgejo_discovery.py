# SPDX-License-Identifier: Apache-2.0
"""Public native endpoint fixtures through the shipped bounded HTTPS transport."""

import json
from collections.abc import Sequence
from io import BytesIO
from unittest.mock import patch

import pytest

from creatidy_kernel.adapters.forgejo_discovery import ForgejoDiscovery, ScanStatus
from creatidy_kernel.adapters.forgejo_transport import HTTPSForgejoTransport
from creatidy_kernel.core.forge import Reference


class Response(BytesIO):
    status = 200


def issue(number: int, *, repository: str = "team/project", title: str | None = None) -> dict[str, object]:
    return {
        "number": number,
        "title": title or f"Obligation {number}",
        "body": "Goal and observable acceptance criteria, not execution permission.",
        "state": "open",
        "updated_at": "2026-10-29T12:00:00Z",
        "html_url": f"https://forge.invalid/{repository}/issues/{number}",
    }


def test_complete_enumeration_uses_two_bounded_native_scans_not_a_snapshot() -> None:
    transport = HTTPSForgejoTransport(
        "https://forge.invalid/api/v1", Reference("forgejo:team/project"), lambda: "fixture"
    )
    discovery = ForgejoDiscovery(transport, lambda: 100, max_pages=3, freshness_seconds=60, page_size=1)
    pages = [[issue(8)], [issue(10)], []] * 2
    with patch.object(
        transport.opener, "open", side_effect=[Response(json.dumps(page).encode()) for page in pages]
    ) as sent:
        observed = discovery.discover()
    assert observed.status is ScanStatus.OBSERVED
    assert [item.number for item in observed.issues] == [8, 10]
    assert not observed.snapshot
    assert (observed.observed_at, observed.fresh_until, observed.pages) == (100, 160, 6)
    assert all(call.args[0].method == "GET" for call in sent.call_args_list)
    assert sent.call_args_list[0].args[0].full_url.endswith("issues?state=open&type=issues&sort=oldest&page=1&limit=1")


@pytest.mark.parametrize("hidden", [{}, {"number": 2, "title": "HIDDEN"}, {"number": 2, "title": "confidential"}])
def test_permission_hidden_native_predecessor_is_not_an_empty_dependency_set(hidden: dict[str, object]) -> None:
    transport = HTTPSForgejoTransport(
        "https://forge.invalid/api/v1", Reference("forgejo:team/project"), lambda: "fixture"
    )
    discovery = ForgejoDiscovery(transport, lambda: 100, max_pages=3, freshness_seconds=60)
    with patch.object(transport.opener, "open", return_value=Response(json.dumps([hidden]).encode())):
        observed = discovery.dependencies(106)
    assert observed.status is ScanStatus.PERMISSION
    assert not observed.issues
    assert observed.pages == 1


def test_native_dependencies_retain_external_repository_identity_and_closed_state_only() -> None:
    transport = HTTPSForgejoTransport(
        "https://forge.invalid/api/v1", Reference("forgejo:team/project"), lambda: "fixture"
    )
    discovery = ForgejoDiscovery(transport, lambda: 100, max_pages=2, freshness_seconds=60)
    dependency = {**issue(73, repository="team/contracts"), "state": "closed"}
    with patch.object(
        transport.opener,
        "open",
        side_effect=[
            Response(json.dumps([dependency]).encode()),
            Response(b"[]"),
            Response(json.dumps([dependency]).encode()),
            Response(b"[]"),
        ],
    ) as sent:
        observed = discovery.dependencies(106)
    assert observed.status is ScanStatus.OBSERVED
    assert observed.issues[0].repository == "team/contracts"
    assert observed.issues[0].state == "closed"  # Not Kernel acceptance or executable dependency satisfaction.
    assert "/issues/106/dependencies?" in sent.call_args_list[0].args[0].full_url


@pytest.mark.parametrize("mode", ["bounded", "changed", "duplicate", "malformed", "escaped", "slow", "rollback"])
def test_partial_moving_or_unqualified_scan_never_proves_readiness(mode: str) -> None:
    transport = HTTPSForgejoTransport(
        "https://forge.invalid/api/v1", Reference("forgejo:team/project"), lambda: "fixture"
    )
    moments = iter([100, 161] if mode == "slow" else [100, 99] if mode == "rollback" else [100, 100])
    discovery = ForgejoDiscovery(transport, lambda: next(moments), max_pages=2, freshness_seconds=60, page_size=1)
    pages: Sequence[object] = [[issue(1)], [], [issue(1)], []]
    expected = ScanStatus.CHANGED
    if mode == "bounded":
        pages, expected = [[issue(1)], [issue(2)]], ScanStatus.INCOMPLETE
    elif mode == "changed":
        pages = [[issue(1)], [], [issue(1, title="Changed scope")], []]
    elif mode == "duplicate":
        pages = [[issue(1)], [issue(1)]]
    elif mode == "malformed":
        pages, expected = [{"message": "not a page"}], ScanStatus.INCOMPLETE
    elif mode == "escaped":
        pages, expected = [[issue(1, repository="other/project")]], ScanStatus.INCOMPLETE
    with patch.object(transport.opener, "open", side_effect=[Response(json.dumps(page).encode()) for page in pages]):
        assert discovery.discover().status is expected
