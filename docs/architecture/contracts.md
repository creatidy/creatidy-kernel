# Port And Recovery Contracts

The table records post-A0 requirements, not a claim that every adapter passes every case. A0 coded
only the narrower ResourceAllocator; subsequent bounded ports/adapters exist. Current verification
and missing receipts are in the [coverage roadmap](successor.md), governed by
[ADR 0007](../adr/0007-shared-harness-routing-observability.md). Ports use core-owned values;
unsupported requirements must fail closed, not through weaker fallbacks.

## Current Boundary Evidence

Inspected Kernel revision: `eb4f4a2956712bfaf39a3271e1523e7f77a91e26`. Source/test definitions prove
bounded mechanisms, not deployment or live reception.

| Boundary | Built subset / evidence | Missing receipt and owner |
| --- | --- | --- |
| Runtime control | `ports/execution.py` five methods; `codex_runtime.py` native start/read/interrupt/known-handle restore; `codex_stdio.py` owned process/RPC bounds | [#47](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/47) control/backend proof, [#53](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53) adapter, [#48](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/48) lifetime/cancel/deadline repair. No generic events/permissions/resume/IDE guarantee. |
| Allocation and inference | `scarcity_router.py` machine v1/D-057 recommendation and configured binding; `ports/allocation.py` durable no-reselection recovery | [#51](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/51) approved requirements and [#52](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/52) executable route consumer. Router owns producer selection/admission/pin; recommendation is not gateway execution. |
| Task intake | Frozen `TaskSpec` registry, core immutable ProgramSpec | [#49](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/49) ordinary approved/versioned spec, fresh relevance/PR/source/test baseline. No prose authority. |
| Acceptance/review | `core/verification.py` exact subject/freshness/independent-evidence seam; current task policy does not require reviewer | [#54](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/54) durable product loop; repository development review is not implementation. Actual decision-time freshness repair also #48. |
| Workspace/source | Disposable candidate, canonical cache/preflight checks, same-user trusted-development | [#50](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50) safe verifier/isolation, [#61](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/61) source identity/locking/dispatch checks, [#59](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/59) concurrency/manual edits. Diff allowlist is not sandbox. |
| State/observation | Dedicated EXCLUSIVE SQLite and offline CLI export/status | [#57](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/57) owner-served snapshot/events/commands/notifications. Console cannot open active SQLite or weaken durability. |
| Knowledge/outcomes | Durable context/allocation artifacts, pure unitful `core.outcomes`, richer reference and thin task export | [#55](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/55) utilized MI references, [#56](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56) full budgets/costs, [#58](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/58) versioned private export. Unknown is not zero or model quality. |

## Cross-Product Contract Obligations

These are **agreed semantic requirements and to-prove contracts**, not approved new wire shapes,
endpoints, flags or versions. Each producer owns its schema and executable producer-consumer tests.

- Separate harness control from model-backend ingress; validate exact harness/adapter/protocol/source
  versions and tool/stream/retry semantics. ACP client file/terminal execution needs its own proved
  permission boundary. Unsupported resume/recovery stays unknown or refused.
- Persist Router's executable route before Attempt dispatch; keep main calls sticky, explicit user
  pin and separately authorized helper/review route evidence. Preserve provider/model/resource/access
  mode/explicit effort/opaque variant/harness versions and requested/resolved/observed gaps. No
  physical identity inference for plan-managed execution or effort inferred from variant.
- MI produces complete versioned knowledge with provenance/time/conflict/withdrawal semantics;
  Router admits/consumes it. Kernel retains actual utilized references, frozen evidence cut and
  evaluation semantics without ranking or granting authority. Missing publication cannot be invented
  from a Python object/package version; unknown freshness is not a renewed guarantee on retrieval.
- Kernel produces its own snapshot and progress/event view or controlled projection: reconnect,
  position, deduplication, gap/resync, bounded retention and correlations are required. Commands are
  checked against current owner authority/revision. Console, notifications and lossy telemetry are
  never the only durable truth. PWA/IDE/web do not guarantee process or alert lifetime.
- Full task budgets cover implementation/review/remediation/tools/tests/retries/auxiliary work and
  separate money/quota/tokens/time/attention with sourced unknowns. Router's call admission/rate cap
  is not that whole-task authorization. No reset/credit/overflow or hidden retry authority.
- Preserve immutable IDs and actual state/profile ciphertext in migrations; backward compatibility
  follows persisted data/contract evidence, not speculative aliases. Installation/Linux/WSL and
  future-platform compatibility need documented reception under [#62](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/62).

Producer-owned counterparts and exact current limitations are in the roadmap. Router's native
workspace-editing lane conflicts with the agreed single workspace-controller boundary; its
[authority decision #180](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/180) must be
received before enabling that affected path, not silently removed or relabelled.

| Boundary | Minimum obligations | Required conformance cases |
| --- | --- | --- |
| Runtime | Start an immutable Attempt via a stable Operation key; observe with freshness, retrieve candidate, request cancellation, reconcile by known key/handle. Return domain acceptance/rejection separately from transport status. Persist requested/resolved/observed identity and agent-definition version. | Lost start reply, duplicate start, terminal callback replay, restart with missing session, delayed healthy work, cancel race, invalid candidate, unavailable identity attestation. Unsupported recovery produces unknown, not invented absence. |
| ResourceAllocator | Select against explicit hard requirements without executing a worker. Return allocation, bounded wait, or refusal with reasons. Reservation receipt only if actually acquired; never invent quota. | Fixed allocator substitution, impossible capability/context, stale quota, missing reservation support, unknown usage. A0 implements only deterministic selection/refusal. |
| Workspace | Materialize explicit repository/revision, verified overlays, toolchain and enforcement profile; expose supported isolation; collect immutable artifact manifest; reconcile and clean up. | Reproducible inputs, occupied handle, partial launch, symlink/path escape, forbidden network/mount, lost workspace, cleanup after uncertain worker termination. Worktree-only cannot satisfy hostile-worker isolation. |
| Forge | Resolve opaque repository/change/revision/check references; normalize domain receipts; create/update only authorized effects; observe current remote state and capability-gate conditional mutations. Forgejo PR creation submits the authorized full candidate SHA through AGit to `refs/for/<authorized-base>` with a fresh, collision-resistant Operation topic push option, not a mutable ordinary head branch. The base authority is target repository plus target branch; observed base SHA is evidence, not an authority precondition. A domain creation receipt and authoritative read-back of PR identity, repository, base ref and current exact head are required before acknowledgement. A topic collision/update is not creation; uncertain delivery is reconciled by a durably known reference or remains unknown, never blindly retried. | Forgejo and fake run the same suite; exact-SHA AGit submission, never-reused topic, collision/update refusal, lost reply, replay, moved base tip, moved PR head, inaccessible versus absent, permission/capability refusal and missing exact-head check. No ordinary mutable source branch, `gh` subprocess or Forgejo payload in core; no merge/deploy effect in K5. |
| Verification | Run policy-selected checks outside worker authority, bind evidence/verdict to candidate/spec/policy/subject, use a fresh independent reviewer when required. | Schema-valid but false success, fabricated test marker, changed head/base/policy, missing evidence, tampered artifact, candidate-modified gate, fresh review after remediation. |
| Authority broker | Authenticate owner versus worker; derive least grants from approved scope; enforce repository/path/network/operation/expiry; consume exact single-use grants with intent. | Worker tries to grant/accept, expired/revoked capability, changed spec, replay with same or changed input, escaped path, overbroad provider credential. |

## K5 AGit Creation

The trusted caller generates a fresh random topic and durably binds it to one Operation before
delivery. Forgejo's AGit `refs/for/<base>` push with `-o topic=<topic>` is a creation request,
not a conditional update to an ordinary branch. A successful Git transport exit or uploaded commit
does not prove creation: an existing topic can update an open PR. The adapter must distinguish a new-PR domain
receipt from a topic update, then read the identified PR and verify its repository, base ref and
current full head SHA against the authorized effect. Only the observed base SHA is recorded as
provenance; moving the base tip does not change the target repository/base authority. An ambiguous
or lost response leaves the Operation unknown unless its already durable remote PR reference can be
checked; it does not authorize another push. Any later PR head change invalidates exact-head evidence
for subsequent decisions. This K5 effect creates no merge or deployment authority.

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

This is historical design for a separately authorized future merge capability, not a current
product command or permission. Current deliveries create PRs only; no auto-merge/deploy is granted.

Acceptance binds `ProgramSpec + WorkUnitSpec + AttemptSpec + candidate artifact subjects + policy +
evidence`. A merge operation is separate. If policy requires tests against the current base, merely
passing an expected head to the forge merge API is insufficient: the base may move between check and
merge. Require a forge merge-queue/equivalent guarantee, or a precomputed tested merge commit applied
with atomic expected-old target-ref comparison and branch-policy compliance. If neither is supported,
disable automatic merge. Do not silently weaken the gate for a particular forge.
