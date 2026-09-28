# SPDX-License-Identifier: Apache-2.0
"""Offline producer-shaped fixtures; no Router package or live service required.

Fixture shape follows machine_api.selection_envelope, selector.SelectionDecision
and CandidateEvaluation.to_dict at Router 81b8e6393a1b2df8a6c7a9c6c3dcb8a7f6884e4e.
All identities, timestamps, configuration and capacity evidence are synthetic.
"""

from __future__ import annotations

import http.client
import json
import ssl
import traceback
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from typing import cast

import pytest

from creatidy_kernel.adapters.scarcity_router import (
    MAX_RESPONSE_BYTES,
    ScarcityRouterAllocator,
    ScarcityRouterUnavailable,
)
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest

REQUEST = ResourceRequest("unit-1", frozenset({"reference"}), 128)
BINDING = Allocation("codex", "openai", "fixture-model", frozenset({"reference"}), 4096, "controller binding", "high")


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parent / "fixtures/scarcity_router_selected.json").read_bytes(),
        ),
    )


def decision(document: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], document["decision"])


def selected(document: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], decision(document)["selected"])


def stub(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> ScarcityRouterAllocator:
    def exchange(_self: ScarcityRouterAllocator, _requirement: dict[str, object]) -> bytes:
        return raw

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", exchange)
    return ScarcityRouterAllocator("http://127.0.0.1:12345", (BINDING,))


@contextmanager
def server(
    body: bytes,
    *,
    status: int = 200,
    delay: float = 0,
    trickle: bool = False,
    declared_length: int | None = None,
) -> Generator[tuple[str, list[dict[str, object]]]]:
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body) if declared_length is None else declared_length))
            self.send_header("Location", "http://127.0.0.1:1/credential-trap")
            self.end_headers()
            if delay:
                sleep(delay)
            try:
                if trickle:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        sleep(0.005)
                else:
                    self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("key", [None, "synthetic-bearer"])
def test_public_select_transport_and_exact_binding(key: str | None) -> None:
    document = fixture()
    with server(json.dumps(document).encode()) as (origin, requests):
        allocator = ScarcityRouterAllocator(origin, (BINDING,), api_key=key)
        allocation = allocator.select(REQUEST)
    assert allocation.runtime_id == "codex"
    assert allocation.provider_id == "openai"
    assert allocation.model_id == "fixture-model"
    assert allocation.reasoning_effort == "high"
    assert allocation.variant == "opaque-configuration" != allocation.reasoning_effort
    assert allocation.capabilities == BINDING.capabilities
    assert allocation.context_tokens == BINDING.context_tokens
    assert allocation.decision_provenance == json.dumps(document, sort_keys=True, separators=(",", ":"))
    assert "selected_degraded_capacity" in allocation.rationale
    assert requests == [
        {
            "path": "/v1/select",
            "authorization": None if key is None else f"Bearer {key}",
            "body": {"requirement": decision(document)["requirement"]},
        }
    ]
    if key:
        assert key not in repr(allocator)


@pytest.mark.parametrize("effort", [None, "none", "custom-exact-effort"])
def test_effort_is_required_exact_and_not_variant(monkeypatch: pytest.MonkeyPatch, effort: str | None) -> None:
    document = fixture()
    selected(document)["reasoning_effort"] = effort
    allocator = stub(monkeypatch, json.dumps(document).encode())
    with pytest.raises(AllocationUnavailable, match="incompatible"):
        allocator.select(REQUEST)
    allocator = replace(allocator, runtime_bindings=(replace(BINDING, reasoning_effort=effort),))
    allocation = allocator.select(REQUEST)
    assert allocation.reasoning_effort == effort
    assert allocation.variant == "opaque-configuration"


@pytest.mark.parametrize(
    "binding",
    [
        replace(BINDING, provider_id="zai"),
        replace(BINDING, model_id="wrong-model"),
        replace(BINDING, reasoning_effort=None),
        replace(BINDING, reasoning_effort="none"),
        replace(BINDING, capabilities=frozenset()),
        replace(BINDING, context_tokens=127),
        replace(BINDING, variant="different-opaque-config"),
    ],
)
def test_selected_runtime_must_be_explicitly_compatible(monkeypatch: pytest.MonkeyPatch, binding: Allocation) -> None:
    allocator = stub(monkeypatch, json.dumps(fixture()).encode())
    with pytest.raises(AllocationUnavailable, match="incompatible"):
        replace(allocator, runtime_bindings=(binding,)).select(REQUEST)


def test_runtime_bindings_are_not_ranked_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    document = fixture()
    alternate = selected(fixture())
    alternate["identity"] = {"provider": "openai", "model": "other-model", "variant": "opaque"}
    decision(document)["alternatives"] = [alternate]
    allocator = stub(monkeypatch, json.dumps(document).encode())
    only_alternative = replace(BINDING, model_id="other-model")
    with pytest.raises(AllocationUnavailable, match="incompatible"):
        replace(allocator, runtime_bindings=(only_alternative,)).select(REQUEST)


def test_refusal_preserves_public_wait_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    document = fixture()
    candidate = selected(document)
    candidate.update(
        eligible=False, degraded=False, exclusion_stage="unknown_capacity", reason_codes=["unknown_capacity_blocked"]
    )
    candidate.pop("capability_margin")
    candidate["unknown_capacity_decision"] = {
        "mode": "strict",
        "eligible_by_unknown_policy": False,
        "degraded": False,
        "reason_codes": ["unknown_capacity_blocked"],
    }
    decision(document).update(
        selected=None,
        degraded=False,
        excluded=[candidate],
        closest_candidates=[candidate],
        reason_codes=["no_eligible_candidate"],
    )
    allocator = stub(monkeypatch, json.dumps(document).encode())
    with pytest.raises(ScarcityRouterUnavailable, match="no eligible") as caught:
        allocator.select(REQUEST)
    assert caught.value.decision_provenance is not None
    assert json.loads(caught.value.decision_provenance) == document


INVALID_FIELDS: list[tuple[tuple[str, ...], object]] = [
    (("schema_version",), True),
    (("schema_version",), 1.0),
    (("schema_version",), "1"),
    (("schema_version",), 2),
    (("decision",), []),
    (("decision", "catalog_version"), True),
    (("decision", "resource_policy_version"), 1.0),
    (("decision", "catalog_updated_on"), "invalid"),
    (("decision", "evaluated_at"), "2026-09-28T08:00:00"),
    (("decision", "selector_mode"), "unknown"),
    (("decision", "reason_codes"), "selected_balanced"),
    (("decision", "reason_codes"), ["no_eligible_candidate"]),
    (("decision", "degraded"), 1),
    (("decision", "alternatives"), {}),
    (("decision", "preference_order"), [{}]),
    (("decision", "selected", "identity"), {}),
    (("decision", "selected", "identity", "provider"), ""),
    (("decision", "selected", "identity", "variant"), None),
    (("decision", "selected", "eligible"), 1),
    (("decision", "selected", "eligible"), False),
    (("decision", "selected", "degraded"), False),
    (("decision", "selected", "reasoning_effort"), ""),
    (("decision", "selected", "reasoning_effort"), 1),
    (("decision", "selected", "capability_margin"), True),
    (("decision", "selected", "scarcity_assessment"), []),
    (("decision", "requirement", "hard_constraints", "requires_tool_use"), 1),
    (("decision", "requirement", "hard_constraints", "minimum_input_context_tokens"), 127),
]


@pytest.mark.parametrize("path,value", INVALID_FIELDS)
def test_malformed_consumed_contract(monkeypatch: pytest.MonkeyPatch, path: tuple[str, ...], value: object) -> None:
    document = fixture()
    target = document
    for key in path[:-1]:
        target = cast(dict[str, object], target[key])
    target[path[-1]] = value
    with pytest.raises(AllocationUnavailable, match="invalid public response"):
        stub(monkeypatch, json.dumps(document).encode()).select(REQUEST)


@pytest.mark.parametrize("key", ["identity", "reasoning_effort", "eligible", "degraded", "capability_margin"])
def test_missing_candidate_fields_rejected(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    document = fixture()
    selected(document).pop(key)
    with pytest.raises(AllocationUnavailable):
        stub(monkeypatch, json.dumps(document).encode()).select(REQUEST)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json secret",
        b"\xff",
        b"[]",
        b'{"secret":1,"secret":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e999}',
        b"[" * 33 + b"]" * 33,
        b" " * (MAX_RESPONSE_BYTES + 1),
    ],
)
def test_strict_bounded_json_safe_errors(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    with pytest.raises(ScarcityRouterUnavailable) as caught:
        stub(monkeypatch, raw).select(REQUEST)
    assert "secret" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert caught.value.decision_provenance is None


@pytest.mark.parametrize("status", [301, 302, 307, 400, 401, 403, 429, 500, 503])
def test_http_failures_never_redirect_or_echo_bodies(status: int) -> None:
    with server(b"sensitive upstream response", status=status) as (origin, requests):
        allocator = ScarcityRouterAllocator(origin, (BINDING,), api_key="synthetic-secret")
        with pytest.raises(AllocationUnavailable) as caught:
            allocator.select(REQUEST)
    rendered = "".join(traceback.format_exception(caught.value))
    assert "sensitive upstream" not in rendered
    assert "synthetic-secret" not in str(caught.value)
    assert caught.value.__context__ is None
    assert len(requests) == 1


def test_transport_byte_limit_and_timeout() -> None:
    with server(b" " * (MAX_RESPONSE_BYTES + 1)) as (origin, _requests):
        with pytest.raises(AllocationUnavailable):
            ScarcityRouterAllocator(origin, (BINDING,)).select(REQUEST)
    with server(json.dumps(fixture()).encode(), delay=0.3) as (origin, _requests):
        started = monotonic()
        with pytest.raises(AllocationUnavailable):
            ScarcityRouterAllocator(origin, (BINDING,), timeout_seconds=0.03).select(REQUEST)
        assert monotonic() - started < 0.25


def test_trickling_body_cannot_reset_deadline() -> None:
    with server(json.dumps(fixture()).encode(), trickle=True) as (origin, _requests):
        started = monotonic()
        with pytest.raises(AllocationUnavailable):
            ScarcityRouterAllocator(origin, (BINDING,), timeout_seconds=0.03).select(REQUEST)
        assert monotonic() - started < 0.25


def test_expired_connect_cannot_leave_response_without_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    connect = http.client.HTTPConnection.connect

    def slow_connect(connection: http.client.HTTPConnection) -> None:
        connect(connection)
        clock[0] = 2.0

    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow_connect)
    monkeypatch.setattr("creatidy_kernel.adapters.scarcity_router.monotonic", lambda: clock[0])
    with server(json.dumps(fixture()).encode()) as (origin, requests):
        with pytest.raises(AllocationUnavailable):
            ScarcityRouterAllocator(origin, (BINDING,), timeout_seconds=1).select(REQUEST)
        assert requests == []


def test_truncated_http_body_cannot_look_like_complete_json() -> None:
    body = json.dumps(fixture()).encode()
    with server(body, declared_length=len(body) + 5) as (origin, _requests):
        with pytest.raises(AllocationUnavailable):
            ScarcityRouterAllocator(origin, (BINDING,)).select(REQUEST)


def test_unreachable_router_is_explicit_unavailable() -> None:
    with server(b"") as (origin, _requests):
        pass
    with pytest.raises(AllocationUnavailable) as caught:
        ScarcityRouterAllocator(origin, (BINDING,)).select(REQUEST)
    assert caught.value.__context__ is None


def test_normalized_provenance_is_bounded_and_strings_do_not_increase_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    document = fixture()
    decision(document)["additive_public_evidence"] = "[" * 100
    assert stub(monkeypatch, json.dumps(document).encode()).select(REQUEST).decision_provenance is not None
    decision(document)["additive_public_evidence"] = "\u00e9" * 50_000
    raw = json.dumps(document, ensure_ascii=False).encode()
    assert len(raw) < MAX_RESPONSE_BYTES
    with pytest.raises(AllocationUnavailable):
        stub(monkeypatch, raw).select(REQUEST)


@pytest.mark.parametrize("in_key", [False, True])
@pytest.mark.parametrize("no_selection", [False, True])
def test_echoed_bearer_never_becomes_durable_evidence(
    monkeypatch: pytest.MonkeyPatch,
    in_key: bool,
    no_selection: bool,
) -> None:
    document = fixture()
    token = "synthetic-bearer-credential"  # noqa: S105 - deliberately synthetic test credential.
    if no_selection:
        decision(document).update(selected=None, degraded=False, reason_codes=["no_eligible_candidate"])
    decision(document)["additive_public_evidence"] = {token: "redacted"} if in_key else ["echo:" + token]
    raw = json.dumps(document).replace(token, "".join(f"\\u{ord(c):04x}" for c in token)).encode()
    assert token.encode() not in raw
    allocator = replace(stub(monkeypatch, raw), api_key=token)
    with pytest.raises(ScarcityRouterUnavailable) as caught:
        allocator.select(REQUEST)
    assert caught.value.decision_provenance is None
    assert token not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


def test_zero_context_uses_exact_public_omission(monkeypatch: pytest.MonkeyPatch) -> None:
    document = fixture()
    requirement = cast(dict[str, object], decision(document)["requirement"])
    requirement["hard_constraints"] = {"requires_tool_use": True}

    def exchange(_self: ScarcityRouterAllocator, requested: dict[str, object]) -> bytes:
        assert requested == requirement
        return json.dumps(document).encode()

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", exchange)
    allocation = ScarcityRouterAllocator("http://localhost", (BINDING,)).select(replace(REQUEST, context_tokens=0))
    assert allocation.context_tokens == BINDING.context_tokens


def test_https_requires_default_verified_context_and_sanitizes_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ssl.SSLContext] = []

    def connect(host: str, port: int, *, timeout: float, context: ssl.SSLContext) -> http.client.HTTPSConnection:
        seen.append(context)
        raise OSError("raw-network-secret")

    monkeypatch.setattr(http.client, "HTTPSConnection", connect)
    with pytest.raises(AllocationUnavailable) as caught:
        ScarcityRouterAllocator("https://router.invalid", (BINDING,)).select(REQUEST)
    assert seen[0].verify_mode == ssl.CERT_REQUIRED
    assert seen[0].check_hostname
    assert "raw-network-secret" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.invalid",
        "ftp://localhost",
        "https://user:secret@router.invalid",  # pragma: allowlist secret - synthetic rejection fixture
        "https://router.invalid/path",
        "https://router.invalid?secret",
        "https://router.invalid#secret",
        "https://router.invalid:bad",
        "https://router.invalid\n",
        "http://127.0.0.2",
        "https://router.invalid:0",
    ],
)
def test_invalid_origins_are_safe(origin: str) -> None:
    with pytest.raises(AllocationUnavailable) as caught:
        ScarcityRouterAllocator(origin, (BINDING,))
    assert "secret" not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan"), 61])
def test_invalid_timeout(timeout: float) -> None:
    with pytest.raises(AllocationUnavailable):
        ScarcityRouterAllocator("http://localhost", (BINDING,), timeout_seconds=timeout)


def test_configuration_and_unsupported_capabilities_fail_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    for bindings in ((), (BINDING, BINDING), (BINDING, replace(BINDING, runtime_id="other-runtime"))):
        with pytest.raises(AllocationUnavailable):
            ScarcityRouterAllocator("http://localhost", bindings)
    for key in ("", "bad key", "bad\nheader", "\x00", "\x7f"):
        with pytest.raises(AllocationUnavailable):
            ScarcityRouterAllocator("http://localhost", (BINDING,), api_key=key)

    def forbidden(_self: ScarcityRouterAllocator, _requirement: dict[str, object]) -> bytes:
        pytest.fail("unsupported requirements must not call Router")

    monkeypatch.setattr(ScarcityRouterAllocator, "_exchange", forbidden)
    for capabilities in (frozenset[str](), frozenset({"reference", "vision"}), frozenset({"tool_use"})):
        with pytest.raises(AllocationUnavailable, match="unsupported"):
            ScarcityRouterAllocator("http://localhost", (BINDING,)).select(
                replace(REQUEST, required_capabilities=capabilities),
            )
