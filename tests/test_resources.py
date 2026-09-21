# SPDX-License-Identifier: Apache-2.0
from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest

from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.ports.resources import ResourceAllocator


def allocation() -> Allocation:
    return Allocation("runtime", "local", "model", frozenset({"code-edit"}), 8192, "operator configuration")


def test_fixed_allocator_satisfies_the_port_without_services() -> None:
    configured = allocation()
    allocator: ResourceAllocator = FixedAllocator(configured)
    request = ResourceRequest("work-unit", frozenset({"code-edit"}), 8192)
    assert allocator.select(request) == configured
    assert allocator.select(request) == allocator.select(request)


@pytest.mark.parametrize(
    "resource_request",
    [
        ResourceRequest("work-unit", frozenset({"unknown-capability"}), 1),
        ResourceRequest("work-unit", frozenset({"code-edit"}), 8193),
    ],
)
def test_unsatisfied_constraints_are_not_silently_downgraded(resource_request: ResourceRequest) -> None:
    with pytest.raises(AllocationUnavailable):
        FixedAllocator(allocation()).select(resource_request)


def test_another_implementation_needs_no_adapter_inheritance() -> None:
    class OtherAllocator:
        def select(self, request: ResourceRequest) -> Allocation:
            return replace(
                allocation(), capabilities=request.required_capabilities, context_tokens=request.context_tokens
            )

    allocator: ResourceAllocator = OtherAllocator()
    request = ResourceRequest("different-work", frozenset({"review"}), 100)
    assert allocator.select(request).capabilities == frozenset({"review"})


def test_domain_values_are_immutable() -> None:
    configured = allocation()
    request = ResourceRequest("work-unit", frozenset(), 0)
    for target, field, value in ((configured, "model_id", "changed"), (request, "context_tokens", 10)):
        with pytest.raises(FrozenInstanceError):
            setattr(target, field, value)


@pytest.mark.parametrize("value", [-1, True, 2.5])
def test_invalid_context_capacity_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        ResourceRequest("work-unit", frozenset(), cast(int, value))
    with pytest.raises(ValueError, match="nonnegative integer"):
        replace(allocation(), context_tokens=cast(int, value))


def test_mutable_or_blank_capabilities_are_rejected() -> None:
    for capabilities in (cast(frozenset[str], {"code-edit"}), frozenset({" "})):
        with pytest.raises(ValueError, match="immutable set"):
            ResourceRequest("work-unit", capabilities, 1)
        with pytest.raises(ValueError, match="immutable set"):
            replace(allocation(), capabilities=capabilities)


def test_blank_identity_and_reason_are_rejected() -> None:
    with pytest.raises(ValueError, match="work_unit_id"):
        ResourceRequest(" ", frozenset(), 0)
    with pytest.raises(ValueError, match="identities and rationale"):
        replace(allocation(), model_id="")
    with pytest.raises(ValueError, match="identities and rationale"):
        replace(allocation(), rationale=" ")
    with pytest.raises(ValueError, match="reasoning_effort"):
        replace(allocation(), reasoning_effort="")
