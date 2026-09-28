# SPDX-License-Identifier: Apache-2.0
# Adapted from BioMedical-IT/scarcity-router, Apache-2.0, revision
# 81b8e6393a1b2df8a6c7a9c6c3dcb8a7f6884e4e: scarcity_router/remote.py
# and selection_app.py (strict JSON hooks). Modified for Kernel's select-only
# port, optional credentials, bounded parsing, safe errors and runtime bindings.
"""Optional public POST /v1/select client, not an execution or policy client.

Only the finite reference capability is translated: tool use and the requested
input context, with no inferred ordinal quality minima. Runtime bindings are
controller assertions of executable limits, never candidates for local ranking.
Opaque decision evidence is neither a reservation receipt nor an attestation.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
import ssl
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from threading import Timer
from time import monotonic
from typing import cast
from urllib.parse import urlsplit

from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.ports.resources import ResourceAllocator

MAX_RESPONSE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}\Z")


class ScarcityRouterUnavailable(AllocationUnavailable):
    """Safe refusal, optionally retaining a validated public no-selection decision."""

    def __init__(self, message: str, decision_provenance: str | None = None) -> None:
        super().__init__(message)
        self.decision_provenance = decision_provenance


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("expected object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    if type(value) is not list:
        raise ValueError("expected array")
    return cast(list[object], value)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty string")
    return value


def _integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("expected integer")
    return value


def _strings(value: object) -> list[str]:
    return [_string(item) for item in _array(value)]


def _identity(value: object) -> tuple[str, str, str]:
    identity = _object(value)
    values = tuple(_string(identity[key]) for key in ("provider", "model", "variant"))
    if set(identity) != {"provider", "model", "variant"} or any(not _IDENTIFIER.fullmatch(v) for v in values):
        raise ValueError("invalid identity")
    return values[0], values[1], values[2]


def _candidate(value: object, *, eligible: bool) -> dict[str, object]:
    candidate = _object(value)
    _identity(candidate["identity"])
    _string(candidate["display_name"])
    effort = candidate["reasoning_effort"]  # Missing is not the public unconfigured null.
    if effort is not None:
        _string(effort)
    if candidate["eligible"] is not eligible or type(candidate["degraded"]) is not bool:
        raise ValueError("invalid eligibility")
    if candidate["degraded"]:
        unknown = _object(candidate["unknown_capacity_decision"])
        if (
            unknown["degraded"] is not True
            or unknown["eligible_by_unknown_policy"] is not True
            or unknown["mode"] != "degraded"
            or unknown["reason_codes"] != ["unknown_capacity_degraded"]
        ):
            raise ValueError("missing degraded evidence")
    if eligible:
        _integer(candidate["capability_margin"])
        if any(key in candidate for key in ("exclusion_stage", "hard_constraint_failures", "capability_failures")):
            raise ValueError("selected candidate carries failures")
        if candidate.get("reason_codes", []) != []:
            raise ValueError("selected candidate carries exclusion codes")
    else:
        _string(candidate["exclusion_stage"])
        if not _strings(candidate["reason_codes"]):
            raise ValueError("missing exclusion evidence")
    # These records remain opaque. Validate their container types, not Router policy.
    for key in (
        "scarcity_assessment",
        "unknown_capacity_decision",
        "blackout_decision",
        "happy_hour_decision",
        "execution_eligibility",
    ):
        if key in candidate:
            _object(candidate[key])
    for key in (
        "reservation_decisions",
        "replenishment_evaluations",
        "hard_constraint_failures",
        "capability_failures",
    ):
        if key in candidate:
            for item in _array(candidate[key]):
                _object(item)
    return candidate


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _contains_credential(value: object, credential: str) -> bool:
    if isinstance(value, str):
        return credential in value
    if isinstance(value, dict):
        return any(
            credential in key or _contains_credential(item, credential)
            for key, item in _object(cast(object, value)).items()
        )
    if isinstance(value, list):
        return any(_contains_credential(item, credential) for item in _array(cast(object, value)))
    return False


def _duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise ValueError("nonfinite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _parse(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    text = raw.decode("utf-8")
    depth = 0
    quoted = escaped = False
    # Bound nesting before invoking the recursive stdlib decoder.
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON too deep")
        elif character in "]}":
            depth -= 1
    return _object(
        cast(
            object,
            json.loads(
                text,
                object_pairs_hook=_duplicates,
                parse_constant=_constant,
                parse_float=_finite_float,
            ),
        )
    )


def _decision(document: dict[str, object], requirement: dict[str, object]) -> tuple[dict[str, object], str]:
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("unsupported schema")
    decision = _object(document["decision"])
    if _canonical(decision["requirement"]) != _canonical(requirement):
        raise ValueError("requirement mismatch")
    _integer(decision["catalog_version"], 1)
    _integer(decision["resource_policy_version"], 1)
    date.fromisoformat(_string(decision["catalog_updated_on"]))
    instant = datetime.fromisoformat(_string(decision["evaluated_at"]))
    if instant.tzinfo is None or instant.utcoffset() is None or decision["selector_mode"] != "balanced":
        raise ValueError("invalid decision provenance")
    if "profile_id" in decision or "profile_policy_version" in decision:
        raise ValueError("unexpected profile expansion")
    if type(decision["degraded"]) is not bool:
        raise ValueError("invalid decision degraded flag")
    codes = _strings(decision["reason_codes"])
    for item in _array(decision["preference_order"]):
        _identity(item)
    if "expired_happy_hour_rules" in decision:
        _strings(decision["expired_happy_hour_rules"])
    for key in ("alternatives", "excluded", "closest_candidates", "recoverable_candidates"):
        for item in _array(decision[key]):
            _candidate(item, eligible=key == "alternatives")
    if decision["selected"] is None:
        if "no_eligible_candidate" not in codes or decision["alternatives"] or decision["degraded"]:
            raise ValueError("invalid refusal")
    else:
        selected = _candidate(decision["selected"], eligible=True)
        if (
            "selected_balanced" not in codes
            or set(codes) - {"selected_balanced", "selected_degraded_capacity"}
            or selected["degraded"] != decision["degraded"]
        ):
            raise ValueError("inconsistent selection")
        if any(_object(item)["identity"] == selected["identity"] for item in _array(decision["alternatives"])):
            raise ValueError("duplicate selection")
    evidence = _canonical(document)
    if len(evidence.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("normalized provenance too large")
    return decision, evidence


@dataclass(frozen=True, slots=True)
class ScarcityRouterAllocator(ResourceAllocator):
    """Explicit origin and exact runtime bindings; no discovery or local fallback.

    HTTP is allowed only for literal loopback hosts (including localhost).
    timeout_seconds bounds socket operations and the response deadline; OS DNS
    resolution remains subject to the operating system's resolver timeout.
    """

    base_url: str = field(repr=False)
    runtime_bindings: tuple[Allocation, ...]
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0
    _scheme: str = field(init=False, repr=False)
    _hostname: str = field(init=False, repr=False)
    _port: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        valid = False
        try:
            if (
                type(self.base_url) is not str
                or not self.base_url.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in self.base_url)
            ):
                raise ValueError("invalid origin")
            parts = urlsplit(self.base_url)
            hostname = parts.hostname
            if (
                parts.scheme not in ("http", "https")
                or not hostname
                or parts.username is not None
                or parts.password is not None
                or parts.path not in ("", "/")
                or parts.query
                or parts.fragment
                or "?" in self.base_url
                or "#" in self.base_url
            ):
                raise ValueError("invalid origin")
            if parts.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("HTTP requires loopback")
            port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
            if not 1 <= port <= 65535:
                raise ValueError("invalid port")
            if self.api_key is not None and (
                type(self.api_key) is not str or not self.api_key or any(not 33 <= ord(c) <= 126 for c in self.api_key)
            ):
                raise ValueError("invalid credential")
            if (
                type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 60
            ):
                raise ValueError("invalid timeout")
            if type(self.runtime_bindings) is not tuple or not self.runtime_bindings:
                raise ValueError("runtime bindings required")
            identities: set[tuple[str, str, str | None]] = set()
            for binding in self.runtime_bindings:
                if type(binding) is not Allocation:
                    raise ValueError("invalid binding")
                identity = binding.provider_id, binding.model_id, binding.reasoning_effort
                if identity in identities:
                    raise ValueError("ambiguous runtime binding")
                identities.add(identity)
            object.__setattr__(self, "_scheme", parts.scheme)
            object.__setattr__(self, "_hostname", hostname)
            object.__setattr__(self, "_port", port)
            valid = True
        except (ValueError, TypeError, OverflowError):
            pass
        if not valid:
            raise ScarcityRouterUnavailable("invalid Scarcity Router allocator configuration")

    def _exchange(self, requirement: dict[str, object]) -> bytes:
        payload = _canonical({"requirement": requirement}).encode("utf-8")
        if self._scheme == "https":
            connection = http.client.HTTPSConnection(
                self._hostname,
                self._port,
                timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(self._hostname, self._port, timeout=self.timeout_seconds)
        deadline = monotonic() + self.timeout_seconds
        active_socket: socket.socket | None = None

        def expire() -> None:
            # Interrupt trickling headers/body, rather than resetting a full timeout per byte.
            if active_socket is not None:
                try:
                    active_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer: Timer | None = None
        try:
            connection.connect()
            active_socket = connection.sock
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("connection exceeded response deadline")
            # Start only with an established socket: a timer that expires during
            # DNS/connect must not leave later trickling headers unbounded.
            timer = Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if self.api_key is not None:
                headers["Authorization"] = f"Bearer {self.api_key}"
            connection.request("POST", "/v1/select", body=payload, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                # http.client does not follow redirects. Never inspect or echo error bodies.
                raise ValueError("Router HTTP refusal")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError("unsupported encoding")
            body = bytearray()
            while len(body) <= MAX_RESPONSE_BYTES:
                if monotonic() >= deadline:
                    raise TimeoutError("response deadline")
                chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if (
                len(body) > MAX_RESPONSE_BYTES
                or monotonic() >= deadline
                or response.length is not None
                and response.length != 0
            ):
                raise ValueError("response bounds exceeded")
            return bytes(body)
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def select(self, request: ResourceRequest) -> Allocation:
        if request.required_capabilities != frozenset({"reference"}):
            raise ScarcityRouterUnavailable("unsupported Scarcity Router request capabilities")
        hard: dict[str, object] = {"requires_tool_use": True}
        if request.context_tokens:
            hard["minimum_input_context_tokens"] = request.context_tokens
        requirement: dict[str, object] = {"task_level": "L0", "capability_minima": {}, "hard_constraints": hard}
        failure = "Scarcity Router unavailable or invalid public response"
        evidence: str | None = None
        try:
            document = _parse(self._exchange(requirement))
            if self.api_key is not None and _contains_credential(document, self.api_key):
                raise ValueError("response contains configured credential")
            decision, provenance = _decision(document, requirement)
            if decision["selected"] is None:
                failure = "Scarcity Router returned no eligible allocation"
                evidence = provenance
            else:
                selected = _object(decision["selected"])
                provider, model, variant = _identity(selected["identity"])
                effort = cast(str | None, selected["reasoning_effort"])
                matches = [
                    binding
                    for binding in self.runtime_bindings
                    if (
                        binding.provider_id == provider
                        and binding.model_id == model
                        and binding.reasoning_effort == effort
                    )
                ]
                if len(matches) == 1:
                    binding = matches[0]
                    if (
                        request.required_capabilities <= binding.capabilities
                        and request.context_tokens <= binding.context_tokens
                        and (binding.variant is None or binding.variant == variant)
                    ):
                        return replace(
                            binding,
                            variant=variant,
                            decision_provenance=provenance,
                            rationale="Scarcity Router: " + ", ".join(_strings(decision["reason_codes"])),
                        )
                failure = "Scarcity Router selection is incompatible with configured runtime bindings"
                evidence = provenance
        except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            pass
        # Raise outside the handler: no raw response/credential exception in even __context__.
        raise ScarcityRouterUnavailable(failure, evidence)
