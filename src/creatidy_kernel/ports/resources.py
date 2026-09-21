# SPDX-License-Identifier: Apache-2.0
"""Resource selection is replaceable without changing domain values."""

from typing import Protocol

from creatidy_kernel.core.resources import Allocation, ResourceRequest


class ResourceAllocator(Protocol):
    def select(self, request: ResourceRequest) -> Allocation:
        """Select or raise AllocationUnavailable; never reserve, execute or grant authority."""
        ...
