# SPDX-License-Identifier: Apache-2.0
"""Bounded #53 native proof: the real Codex app-server inside the #78 rootless OCI boundary.

This package reuses the #78 whole-worker isolation toolchain (``oci_worker_poc``) and points
it at a real, version-pinned Codex binary driven over its native app-server protocol. A
controller-authored synthetic Responses model backend runs inside the disposable boundary so
the proof stays offline and free of paid inference. It is development-context evidence for
[#53](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53); it is not product
runtime, not a Router, and not an AC4 closure claim beyond what the receipts record.
"""
