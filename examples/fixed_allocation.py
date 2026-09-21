# SPDX-License-Identifier: Apache-2.0
"""Print a synthetic, configured selection without executing any external work."""

from creatidy_kernel.adapters.fixed_allocator import FixedAllocator
from creatidy_kernel.core.resources import Allocation, ResourceRequest
from creatidy_kernel.ports.resources import ResourceAllocator

allocator: ResourceAllocator = FixedAllocator(
    Allocation(
        runtime_id="example-harness",
        provider_id="local",
        model_id="configured-model",
        capabilities=frozenset({"code-edit"}),
        context_tokens=8192,
        rationale="Explicit offline example configuration; no model has been called.",
    )
)
request = ResourceRequest("example-work-unit", frozenset({"code-edit"}), 4096)
selection = allocator.select(request)
print(f"{selection.runtime_id}: {selection.provider_id}/{selection.model_id}")
print(selection.rationale)
