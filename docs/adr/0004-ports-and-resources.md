# ADR 0004: Replaceable Execution And Resource Ports

Status: accepted, 2026-09-21; target fixed before the Creatidy implementation audit.

## Decision

An agent runtime is a replaceable worker, never the owner of Program truth. Define internal Runtime,
Forge, Workspace and ResourceAllocator ports as needed, with conformance tests. Do not create a new
wire protocol. Adapt supported native APIs/SDKs, MCP, A2A or Agent Client Protocol at the edge;
negotiate real capabilities rather than asserting that one protocol solves durable execution.

Runtime start receives an immutable Attempt specification, explicit workspace, ContextPackage,
allocation and capability references plus Operation identity. Observe, cancel, retrieve candidate
and recover/reconcile must distinguish found, absent and unknown. Persist remote identifiers before
waiting on them. If a runtime cannot recover sessions, Kernel can still retain truth and start a new
Attempt only after reconciling/quarantining the old one. Replacement does not rewrite old provenance.

Forge owns actual repository objects, issues, PRs, reviews, CI and merge state. Kernel owns why an
operation is authorized and what satisfies its Program. Use opaque repository/change/check handles
and content revision IDs, not Forgejo issue numbers or GitHub states in core. Forgejo is the first
reference adapter, tested against this same contract; unsupported capabilities fail explicitly.

ResourceAllocator requests carry WorkUnit/spec identity, capability and context requirements,
quality policy, deadline/latency, priority, locality, resource/attention budget and existing
reservation references when available. A selection records opaque runtime/provider/model/reasoning
configuration, decision provenance, rationale and optional reservation receipt. No vendor name
encodes a capability ranking in core. Record requested, resolved and actually observed identities
separately. Selection is neither authority to execute nor proof that quota was reserved.

Begin with a pure FixedAllocator. The small A0 contract proves capability/context selection only;
it does not pretend to implement the complete request schema. Additional hard constraints must be
versioned and handled or rejected, never silently ignored. A future ScarcityRouterAllocator translates
to Scarcity Router's public machine interface without importing its implementation or requiring a
reverse Kernel dependency. A simple policy allocator remains possible. Both products run alone.

Reservations, when supported, are external operations with idempotent acquire/release/expiry and
uncertainty reconciliation. The allocator owns quota observations and allocation policy; Kernel owns
Program spend authorization and records measured consumption. No reserve-floor, burn-before-reset,
adaptive routing or subscription-policy shadow implementation belongs in A0.

Workspace creation uses explicit repository/base revision, artifact overlays, toolchain/image digest,
filesystem/network policy and lifetime. An ordinary Git worktree is an explicitly trusted-development
option, not hostile-code isolation. Use an existing container/sandbox for untrusted execution, never
the owner's environment or Docker socket. Workspace identity and exportable artifacts survive
runtime replacement; destroy workspaces only after durable result/effect reconciliation.

## Consequences

Codex, Kilo, Cline, OpenHands, cloud providers, local providers, Prefect and Scarcity Router can each
disappear without redefining Program semantics. They are not all implemented in A0. Native adapters
may be needed where protocol support is incomplete. Do not lower correctness or privacy requirements
to accommodate an adapter. Server frameworks, remote workers and distributed reservation policy are
deferred until an actual integration warrants them.
