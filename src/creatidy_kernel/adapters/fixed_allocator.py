# SPDX-License-Identifier: Apache-2.0
"""Offline allocator for explicitly configured capability and context limits."""

from dataclasses import dataclass

from creatidy_kernel.core.resources import Allocation, AllocationUnavailable, ResourceRequest
from creatidy_kernel.ports.resources import ResourceAllocator


@dataclass(frozen=True, slots=True)
class FixedAllocator(ResourceAllocator):
    allocation: Allocation

    def select(self, request: ResourceRequest) -> Allocation:
        if not request.required_capabilities <= self.allocation.capabilities:
            raise AllocationUnavailable("configured allocation lacks a required capability")
        if request.context_tokens > self.allocation.context_tokens:
            raise AllocationUnavailable("configured allocation has insufficient context capacity")
        return self.allocation
