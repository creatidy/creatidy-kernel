# SPDX-License-Identifier: Apache-2.0
"""Bounded rootless OCI whole-worker isolation proof for Kernel #78/#53.

Research harness, not product code: it is not imported by ``creatidy_kernel`` and adds no
Runtime/Workspace port implementation. The synthetic worker, isolation profile, relay and
controller-side harness approximate a coding-agent worker inside one rootless OCI execution
boundary using a user-owned pinned podman toolchain. See
``docs/architecture/whole-worker-isolation-proof.md`` for the decision record and limits.
"""
