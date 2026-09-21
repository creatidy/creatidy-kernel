# Port And Recovery Contracts

These are post-A0 implementation requirements, not claims about existing adapters. The only coded
port in A0 is the deliberately narrower ResourceAllocator proof. Ports use core-owned values and
advertise supported operations; unsupported requirements fail closed, not through weaker fallbacks.

| Boundary | Minimum obligations | Required conformance cases |
| --- | --- | --- |
| Runtime | Start an immutable Attempt via a stable Operation key; observe with freshness, retrieve candidate, request cancellation, reconcile by known key/handle. Return domain acceptance/rejection separately from transport status. Persist requested/resolved/observed identity and agent-definition version. | Lost start reply, duplicate start, terminal callback replay, restart with missing session, delayed healthy work, cancel race, invalid candidate, unavailable identity attestation. Unsupported recovery produces unknown, not invented absence. |
| ResourceAllocator | Select against explicit hard requirements without executing a worker. Return allocation, bounded wait, or refusal with reasons. Reservation receipt only if actually acquired; never invent quota. | Fixed allocator substitution, impossible capability/context, stale quota, missing reservation support, unknown usage. A0 implements only deterministic selection/refusal. |
| Workspace | Materialize explicit repository/revision, verified overlays, toolchain and enforcement profile; expose supported isolation; collect immutable artifact manifest; reconcile and clean up. | Reproducible inputs, occupied handle, partial launch, symlink/path escape, forbidden network/mount, lost workspace, cleanup after uncertain worker termination. Worktree-only cannot satisfy hostile-worker isolation. |
| Forge | Resolve opaque repository/change/revision/check references; normalize domain receipts; create/update only authorized effects; observe current remote state and capability-gate conditional mutations. | Forgejo and fake run the same suite; pagination, replay, stale head/base, inaccessible versus absent, conflicting idempotency key, uncertain merge, missing exact-head check. No `gh` subprocess or Forgejo payload in core. |
| Verification | Run policy-selected checks outside worker authority, bind evidence/verdict to candidate/spec/policy/subject, use a fresh independent reviewer when required. | Schema-valid but false success, fabricated test marker, changed head/base/policy, missing evidence, tampered artifact, candidate-modified gate, fresh review after remediation. |
| Authority broker | Authenticate owner versus worker; derive least grants from approved scope; enforce repository/path/network/operation/expiry; consume exact single-use grants with intent. | Worker tries to grant/accept, expired/revoked capability, changed spec, replay with same or changed input, escaped path, overbroad provider credential. |

## External Effect Crash Matrix

| Crash / observation point | Durable fact | Safe next action |
| --- | --- | --- |
| Before intent transaction commits | No authorized intent | No dispatch. Repeated command may create intent once. |
| After commit, before delivery claim | Intent and outbox | Claim with a new fence, verify current authority, record delivery attempt. |
| After delivery attempt is recorded, before receipt commits | Effect uncertain, regardless of whether bytes were sent | Lookup remote by key/handle; use guaranteed idempotency or prove no effect before retry. |
| Transport reports success but domain rejects | Rejection receipt | Record rejected operation; remediate protocol/policy. Do not wait for nonexistent review. |
| Remote accepted, controller restarts | Domain receipt or uncertain delivery | Recover observation from receipt/key; never create a second execution merely because the controller restarted. |
| Stale worker publishes after lease replacement | Old fence | Reject publication; quarantine/reconcile external effects separately. |
| Cancel reply or connection loss | Cancellation request/observation, not rollback | Verify process/effect terminal state, revoke capabilities, preserve artifacts until settled. |
| Remote outcome cannot be established | Unknown | Retain uncertainty and bounded reconciliation schedule; no unsafe duplicate. Escalate only if a real authority/infrastructure decision is required. |

Use separate dedupe identities for command admission, logical effect and inbound observation.
Adapters must state whether a remote absence is authoritative, permission-limited or eventually
consistent. A bounded page of search results is not absence proof. Delayed observations can add
evidence but cannot silently regress terminal state or resurrect consumed authority.

## Acceptance And Merge Race

Acceptance binds `ProgramSpec + WorkUnitSpec + AttemptSpec + candidate artifact subjects + policy +
evidence`. A merge operation is separate. If policy requires tests against the current base, merely
passing an expected head to the forge merge API is insufficient: the base may move between check and
merge. Require a forge merge-queue/equivalent guarantee, or a precomputed tested merge commit applied
with atomic expected-old target-ref comparison and branch-policy compliance. If neither is supported,
disable automatic merge. Do not silently weaken the gate for a particular forge.
