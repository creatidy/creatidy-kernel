# SPDX-License-Identifier: Apache-2.0
# Adapted from BioMedical-IT/scarcity-router, Apache-2.0, revision
# 81b8e6393a1b2df8a6c7a9c6c3dcb8a7f6884e4e: scarcity_router/remote.py
# and selection_app.py (strict JSON hooks). Modified for Kernel's select-only
# port, optional credentials, bounded parsing, safe errors and runtime bindings.
# Ordinary marker validation adapted from kernel_requirements.py,
# selection_types.py and routing_core.py at 5c48d51f1eb1f11424a5100eb2ccf20a6cba4581.
# Modified for recommendation-only projection and independent harness checks.
"""Optional public POST /v1/select client, not an execution or policy client.

The historical reference request and explicit approved ordinary markers are
translated without inferred ordinal quality minima. Runtime bindings are
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
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from threading import Timer
from time import monotonic
from typing import cast
from urllib.parse import urlsplit

from creatidy_kernel.core.intake import RequirementsHandoff, TranslationRefusal, object_from
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.ports.resources import ResourceAllocator

MAX_RESPONSE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}\Z")
PRODUCER_REVISION = "5c48d51f1eb1f11424a5100eb2ccf20a6cba4581"  # pragma: allowlist secret - public source Git SHA
QUALITY_PREFIX = "scarcity-router.requirement.v1:"
INTERFACE_PREFIX = "scarcity-router.request.v1:"
_DIMENSIONS = frozenset(
    {"reasoning", "coding", "scientific_methodological", "writing_editorial", "tool_use", "translation_multilingual"}
)
_FEATURES = frozenset({"tool_calls", "structured_output", "streaming", "reasoning_controls", "reasoning_mode"})


class RouterFailureCategory(StrEnum):
    """Closed safe failure vocabulary; never response bodies, credentials or free text."""

    REQUEST_UNSUPPORTED = "request_unsupported"
    REQUEST_INVALID = "request_invalid"
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"
    HTTP_REJECTED = "http_rejected"
    INVALID_RESPONSE = "invalid_response"
    NO_ELIGIBLE_SELECTION = "no_eligible_selection"
    SELECTION_INCOMPATIBLE = "selection_incompatible"


class ScarcityRouterUnavailable(AllocationUnavailable):
    """Safe refusal, optionally retaining a validated public no-selection decision."""

    def __init__(
        self,
        message: str,
        decision_provenance: str | None = None,
        category: RouterFailureCategory | None = None,
    ) -> None:
        super().__init__(message)
        self.decision_provenance = decision_provenance
        self.category = category


class _ExchangeFailure(ValueError):
    """Transport-phase refusal carrying its safe closed category."""

    def __init__(self, category: RouterFailureCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


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


def _safe_id(value: object) -> str:
    result = _string(value)
    if not _IDENTIFIER.fullmatch(result):
        raise ValueError("invalid identifier")
    return result


def _model(value: object) -> dict[str, object]:
    model = _object(value)
    if set(model) != {"provider", "model"} or model["provider"] not in ("openai", "zai"):
        raise ValueError("invalid model")
    _safe_id(model["model"])
    return model


def _requirement(value: object) -> dict[str, object]:
    """Validate/normalize only the pinned public input vocabulary, never policy."""
    requirement = _object(value)
    if set(requirement) != {"task_level", "capability_minima", "hard_constraints"}:
        raise ValueError("invalid requirement fields")
    if requirement["task_level"] not in tuple(f"L{i}" for i in range(6)):
        raise ValueError("invalid task level")
    minima = _object(requirement["capability_minima"])
    if set(minima) - _DIMENSIONS:
        raise ValueError("unsupported dimension")
    normalized: dict[str, object] = {}
    for key, value in minima.items():
        if value is not None:
            if _integer(value, 1) > 5:
                raise ValueError("invalid minimum")
            normalized[key] = value
    hard = _object(requirement["hard_constraints"])
    numeric = {"minimum_input_context_tokens", "minimum_output_tokens"}
    boolean = {"requires_tool_use", "requires_vision", "requires_reasoning_mode"}
    identity = {"required_provider", "required_model", "required_variant", "privacy_constraint"}
    if set(hard) - numeric - boolean - identity:
        raise ValueError("unsupported hard constraint")
    constraints: dict[str, object] = {}
    for key, value in hard.items():
        if key in boolean:
            if type(value) is not bool:
                raise ValueError("invalid boolean")
            if value:
                constraints[key] = True
        elif value is not None:
            if key in numeric:
                _integer(value, 1)
            elif key == "required_model":
                _model(value)
            else:
                _safe_id(value)
                if key == "required_provider" and value not in ("openai", "zai"):
                    raise ValueError("unsupported provider")
            constraints[key] = value
    if "required_provider" in constraints and "required_model" in constraints:
        if constraints["required_provider"] != _model(constraints["required_model"])["provider"]:
            raise ValueError("contradictory model pin")
    return {"task_level": requirement["task_level"], "capability_minima": normalized, "hard_constraints": constraints}


def requirement_markers(
    requirement: dict[str, object], *, profile_id: str | None = None, interface: dict[str, object] | None = None
) -> dict[str, object]:
    """Controller preparation helper; these generated strings must be approved in Declaration.raw.

    Does not choose a profile, calibrate minima or replace any other declaration field.
    Translation still validates unsupported interface meaning at the receiving seam.
    """
    payload: dict[str, object] = {"requirement": _requirement(requirement)}
    if profile_id is not None:
        payload["profile_id"] = _safe_id(profile_id)
    return {
        "quality": [QUALITY_PREFIX + _canonical(payload)],
        "interface": [] if interface is None else [INTERFACE_PREFIX + _canonical(interface)],
    }


def _ordinary(handoff: RequirementsHandoff) -> tuple[dict[str, object], dict[str, object], str | None]:
    """Bound model projection plus separate structural demands; no authority projection."""
    data = handoff.draft.declaration.value
    if data["unknowns"]:
        raise ValueError("unknowns.unresolved")
    if data["context"]:
        raise ValueError("context.unsupported")
    quality = _strings(data["quality"])
    if len(quality) != 1 or not quality[0].startswith(QUALITY_PREFIX):
        raise ValueError("quality.unsupported")
    try:
        payload = _parse(quality[0][len(QUALITY_PREFIX) :].encode())
        if "requirement" not in payload or set(payload) - {"requirement", "profile_id"}:
            raise ValueError("invalid marker fields")
        requirement = _requirement(payload["requirement"])
        profile = _safe_id(payload["profile_id"]) if "profile_id" in payload else None
    except (ValueError, TypeError, KeyError):
        raise ValueError("quality.invalid_requirement") from None
    hard = _object(requirement["hard_constraints"])
    approved_quality = _canonical(requirement)
    if hard.get("requires_vision"):
        raise ValueError("quality.vision_channel_unsupported")
    if "privacy_constraint" in hard:
        raise ValueError("quality.privacy_unsupported")
    interface = _strings(data["interface"])
    if not interface:
        binding: dict[str, object] = {}
    elif len(interface) == 1 and interface[0].startswith(INTERFACE_PREFIX):
        try:
            binding = _parse(interface[0][len(INTERFACE_PREFIX) :].encode())
        except ValueError:
            raise ValueError("interface.invalid_binding") from None
    else:
        raise ValueError("interface.unsupported")
    booleans = {
        "requires_tool_calls",
        "requires_structured_output",
        "requires_streaming",
        "requires_reasoning_controls",
    }
    route_only = {"profile_alias", "pinned_target", "maximum_output_tokens"}
    if set(binding) - booleans - {"minimum_input_context_tokens", "explicit_model", "explicit_variant"} - route_only:
        # These existing route-core fields are NOT recommendation-v1 wire fields.
        raise ValueError("interface.recommendation_unsupported")
    if any(binding.get(key) is not None for key in route_only):
        raise ValueError("interface.recommendation_unsupported")
    try:
        for key, value in binding.items():
            if key in booleans:
                if type(value) is not bool:
                    raise ValueError("invalid boolean")
            elif value is not None:
                if key == "minimum_input_context_tokens":
                    _integer(value, 1)
                elif key == "explicit_model":
                    model = _model(value)
                    if (
                        hard.get("required_model", model) != model
                        or hard.get("required_provider", model["provider"]) != model["provider"]
                    ):
                        raise ValueError("contradictory pin")
                    hard["required_model"] = model
                else:
                    _safe_id(value)
                    if hard.get("required_variant", value) != value:
                        raise ValueError("contradictory pin")
                    hard["required_variant"] = value
        if binding.get("requires_tool_calls"):
            hard["requires_tool_use"] = True
        if binding.get("requires_reasoning_controls"):
            hard["requires_reasoning_mode"] = True
        if binding.get("minimum_input_context_tokens") is not None:
            hard["minimum_input_context_tokens"] = max(
                _integer(hard.get("minimum_input_context_tokens", 0)),
                _integer(binding["minimum_input_context_tokens"], 1),
            )
    except (ValueError, TypeError, KeyError):
        raise ValueError("interface.invalid_or_contradictory_binding") from None
    if profile is not None and _canonical(requirement) != approved_quality:
        raise ValueError("quality.profile_requires_full_projection")
    return requirement, binding, profile


def _features(requirement: dict[str, object], binding: dict[str, object]) -> frozenset[str]:
    hard = _object(requirement["hard_constraints"])
    features = {
        name.removeprefix("requires_")
        for name, value in binding.items()
        if name.startswith("requires_") and value is True
    }
    if hard.get("requires_tool_use"):
        features.add("tool_calls")
    if hard.get("requires_reasoning_mode"):
        features.update({"reasoning_mode", "reasoning_controls"})
    return frozenset(features)


@dataclass(frozen=True, slots=True)
class RuntimeSupport:
    """Independent trusted-controller harness facts, not Router-created capabilities."""

    runtime_id: str
    version: str
    features: frozenset[str]
    output_tokens: int
    evidence_reference: str

    def __post_init__(self) -> None:
        for value in (self.runtime_id, self.version, self.evidence_reference):
            _string(value)
        if type(self.features) is not frozenset or self.features - _FEATURES:
            raise ValueError("invalid runtime features")
        _integer(self.output_tokens)


def _decision(
    document: dict[str, object],
    requirement: dict[str, object],
    profile_id: str | None = None,
    profile_policy_version: int | None = None,
) -> tuple[dict[str, object], str]:
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
    if profile_id is not None:
        if (
            decision["profile_id"] != profile_id
            or _integer(decision["profile_policy_version"], 1) != profile_policy_version
        ):
            raise ValueError("profile expansion mismatch")
    elif "profile_id" in decision or "profile_policy_version" in decision:
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
    runtime_support: tuple[RuntimeSupport, ...] = ()
    profile_policy_version: int | None = None
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
            if type(self.runtime_support) is not tuple or any(
                type(item) is not RuntimeSupport for item in self.runtime_support
            ):
                raise ValueError("invalid runtime support")
            if len({item.runtime_id for item in self.runtime_support}) != len(self.runtime_support):
                raise ValueError("ambiguous runtime support")
            if self.profile_policy_version is not None:
                _integer(self.profile_policy_version, 1)
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
        # The legacy internal call still passes a bare requirement; ordinary
        # calls pass the exact existing profile-XOR-requirement wire document.
        payload = _canonical(requirement if "task_level" not in requirement else {"requirement": requirement}).encode(
            "utf-8"
        )
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
                category = (
                    RouterFailureCategory.REQUEST_INVALID
                    if response.status == 400
                    else RouterFailureCategory.HTTP_REJECTED
                )
                raise _ExchangeFailure(category, "Router HTTP refusal")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise _ExchangeFailure(RouterFailureCategory.INVALID_RESPONSE, "unsupported encoding")
            body = bytearray()
            while len(body) <= MAX_RESPONSE_BYTES:
                if monotonic() >= deadline:
                    raise _ExchangeFailure(RouterFailureCategory.INVALID_RESPONSE, "response deadline")
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

    @staticmethod
    def translate_ordinary(handoff: RequirementsHandoff) -> ResourceRequest | TranslationRefusal:
        """Inert projection from exact approved markers, retaining every original field."""
        try:
            requirement, binding, _ = _ordinary(handoff)
            return ResourceRequest(
                "ordinary",
                _features(requirement, binding),
                _integer(_object(requirement["hard_constraints"]).get("minimum_input_context_tokens", 0)),
                handoff,
            )
        except ValueError as error:
            return TranslationRefusal(handoff, problems=(str(error),))

    def select(self, request: ResourceRequest) -> Allocation:
        ordinary = request.requirements_handoff
        profile: str | None = None
        interface: dict[str, object] = {}
        requirement: dict[str, object]
        wire: dict[str, object]
        if ordinary is not None:
            translated = self.translate_ordinary(ordinary)
            if isinstance(translated, TranslationRefusal) or translated != request:
                raise ScarcityRouterUnavailable(
                    "ordinary requirements invalid or changed", category=RouterFailureCategory.REQUEST_UNSUPPORTED
                )
            requirement, interface, profile = _ordinary(ordinary)
            if profile is not None and self.profile_policy_version is None:
                raise ScarcityRouterUnavailable(
                    "profile policy version must be configured", category=RouterFailureCategory.REQUEST_UNSUPPORTED
                )
            wire = (
                {"requirement": requirement} if profile is None else {"profile_id": profile, "tightening": requirement}
            )
            if not any(self._compatible(binding, request, requirement) for binding in self.runtime_bindings):
                raise ScarcityRouterUnavailable(
                    "ordinary requirements incompatible with configured harness",
                    category=RouterFailureCategory.SELECTION_INCOMPATIBLE,
                )
            try:
                decision_value = object_from(ordinary.decision_bytes)
            except ValueError:
                decision_value = None
            if decision_value is None:
                raise ScarcityRouterUnavailable(
                    "ordinary approval encoding invalid", category=RouterFailureCategory.REQUEST_UNSUPPORTED
                )
            if self.api_key is not None and (
                _contains_credential(ordinary.draft.payload(), self.api_key)
                or _contains_credential(ordinary.draft.declaration.value, self.api_key)
                or _contains_credential(decision_value, self.api_key)
                or _contains_credential(wire, self.api_key)
                or _contains_credential(interface, self.api_key)
            ):
                raise ScarcityRouterUnavailable(
                    "ordinary evidence contains configured credential",
                    category=RouterFailureCategory.REQUEST_UNSUPPORTED,
                )
        elif request.required_capabilities != frozenset({"reference"}):
            raise ScarcityRouterUnavailable(
                "unsupported Scarcity Router request capabilities",
                category=RouterFailureCategory.REQUEST_UNSUPPORTED,
            )
        else:
            hard: dict[str, object] = {"requires_tool_use": True}
            if request.context_tokens:
                hard["minimum_input_context_tokens"] = request.context_tokens
            requirement = {"task_level": "L0", "capability_minima": {}, "hard_constraints": hard}
            wire = {"requirement": requirement}
        failure = "Scarcity Router unavailable or invalid public response"
        category = RouterFailureCategory.INVALID_RESPONSE
        evidence: str | None = None
        try:
            document = _parse(self._exchange(wire if ordinary is not None else requirement))
            if self.api_key is not None and _contains_credential(document, self.api_key):
                raise ValueError("response contains configured credential")
            decision, provenance = _decision(document, requirement, profile, self.profile_policy_version)
            if decision["selected"] is None:
                failure = "Scarcity Router returned no eligible allocation"
                category = RouterFailureCategory.NO_ELIGIBLE_SELECTION
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
                        self._compatible(binding, request, requirement)
                        and (binding.variant is None or binding.variant == variant)
                        and _object(requirement["hard_constraints"]).get("required_variant", variant) == variant
                    ):
                        requirements_provenance: str | None = None
                        if ordinary is not None:
                            support = next(
                                item for item in self.runtime_support if item.runtime_id == binding.runtime_id
                            )
                            support_value = asdict(support)
                            support_value["features"] = sorted(support.features)
                            requirements_evidence: dict[str, object] = {
                                "version": 1,
                                "producer_revision": PRODUCER_REVISION,
                                "draft": ordinary.draft.payload(),
                                "decision_id": ordinary.decision_id,
                                "decision_bytes": ordinary.decision_bytes.decode(),
                                "program_digest": ordinary.program_digest,
                                "sent_request": wire,
                                "interface": interface,
                                "runtime_support": support_value,
                                "runtime_binding": {
                                    "runtime_id": binding.runtime_id,
                                    "provider": binding.provider_id,
                                    "model": binding.model_id,
                                    "effort": binding.reasoning_effort,
                                    "variant": binding.variant,
                                    "context_tokens": binding.context_tokens,
                                },
                            }
                            if self.api_key is not None and _contains_credential(requirements_evidence, self.api_key):
                                raise ValueError("evidence contains configured credential")
                            requirements_provenance = _canonical(requirements_evidence)
                        return replace(
                            binding,
                            variant=variant,
                            decision_provenance=provenance,
                            rationale="Scarcity Router: " + ", ".join(_strings(decision["reason_codes"])),
                            requirements_provenance=requirements_provenance,
                        )
                failure = "Scarcity Router selection is incompatible with configured runtime bindings"
                category = RouterFailureCategory.SELECTION_INCOMPATIBLE
                evidence = provenance
        except _ExchangeFailure as error:
            failure, category = str(error), error.category
        except OSError:
            # Socket-level failures (including connect timeouts) mean the endpoint
            # itself could not be reached; protocol and schema failures stay below.
            failure, category = "Scarcity Router endpoint unreachable", RouterFailureCategory.ENDPOINT_UNREACHABLE
        except (http.client.HTTPException, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            pass
        # Raise outside the handler: no raw response/credential exception in even __context__.
        raise ScarcityRouterUnavailable(failure, evidence, category)

    def _compatible(self, binding: Allocation, request: ResourceRequest, requirement: dict[str, object]) -> bool:
        if request.context_tokens > binding.context_tokens:
            return False
        if request.requirements_handoff is None:
            return request.required_capabilities <= binding.capabilities
        hard = _object(requirement["hard_constraints"])
        model = hard.get("required_model")
        if (
            hard.get("required_provider", binding.provider_id) != binding.provider_id
            or model is not None
            and model != {"provider": binding.provider_id, "model": binding.model_id}
            or binding.variant is not None
            and hard.get("required_variant", binding.variant) != binding.variant
        ):
            return False
        support = next((item for item in self.runtime_support if item.runtime_id == binding.runtime_id), None)
        return (
            support is not None
            and request.required_capabilities <= support.features
            and _integer(hard.get("minimum_output_tokens", 0)) <= support.output_tokens
        )
