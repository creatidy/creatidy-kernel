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
| Workspace/source | Disposable candidate; host-qualified owned cache with stable native acquisition/use lease; shared source/base/clean/structural admission gate; validated copied objects before new native starts | Source paths remain literal on historical recovery; unrecognized trees and staging bytes are preserved. Native-local cooperative ownership is not hostile same-UID isolation or complete multi-task control. [#50](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50) verification-only isolation and [#59](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/59) controllers/manual edits remain separate. |
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

## Proposed Ordinary Intake Contract (#49)

**Status: reviewed logical proposal, not an accepted cross-product wire format or execution authority.** This section
describes the independent preparation slice of [#49](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/49).
It does not add an endpoint, operator flag, worker protocol, or ordinary-task execution path.
The bounded local implementation is described under [Local Intake Reception](#local-intake-reception).
The issue still requires all six acceptance criteria and final independent review; contract review alone is
not functional acceptance, an owner grant, or live readiness.

### Scope And Trust

Preparation receives one explicitly identified ordinary issue and produces inert, reviewable task
meaning. It neither discovers a queue nor creates a planner, scheduler, harness, or execution graph.
Issue text, repository instructions and optional model suggestions are attributed proposal data.
They cannot select the authenticated principal, approve themselves, choose a trusted policy, or
create an Attempt, resource allocation, worker grant, baseline process, or Forge effect.

The existing trusted-caller boundary in `ports/authority.py` supplies `Principal` separately from
proposal data. Approval is owner-only. A principal label is not authentication: this slice retains
the existing trusted local controller assumption and must not deserialize a principal from issue,
model, Console, or declaration fields. Remote approval authentication, hostile same-UID protection,
new credentials/signatures and a stronger sandbox are not implemented by this proposal. A Console
may forward an owner-served request only after its actual authenticated channel is received; a
Console claim cannot turn a worker/proposer into an owner.

No preparation step runs repository hooks, repository-supplied verification commands, or inference.
Known baseline evidence may be received from an identified, trusted evidence producer with an exact
source/recipe subject. Unknown baseline remains unknown; it cannot be changed into a pass by owner
approval of the task's meaning. Untrusted live execution still requires the separately accepted
isolation and execution receipts.

### Logical Records

The following are logical contents to review, not prescribed new shared JSON keys or HTTP shapes.
Concrete local serialization below follows the logical review and must reject ambiguous,
malformed and unsupported input without echoing secret values in diagnostics.

| Record | Logical contents and invariant |
| --- | --- |
| Declaration | Kernel-owned task identity; host-qualified canonical repository and issue; proposed outcome and acceptance criteria; quality/interface/context needs with explicit unknowns; bounded path/network/effect requests; verification recipe and policy references; proposal provenance. No model inventory, new quality scale, inferred minimum, numeric budget, or authority-bearing approval field. |
| Draft revision | Immutable declaration bytes and digest; positive revision and prior revision digest; references to its exact issue, source and baseline observations. A changed proposal creates another revision, never overwrites earlier bytes. A draft revision is not an approved `ProgramSpec`. |
| Issue observation | Bound repository/issue identity, current issue state and provider-update evidence, content digest and observation time. Permission failures, incomplete pages or moving observations are retained as unknown/incomplete rather than negative existence evidence. |
| Source and baseline observations | Exact source/base object IDs, relevant implementation evidence, baseline outcome and producer/recipe identity, subject references and observation time. Empty checks, structural presence, stale results and unsupported baseline collection are not passing tests. |
| PR/equivalence observation | Exact PR identity, state, base/head/source binding, scan completeness and supported equivalence evidence. A link, matching title, open issue, or prose claiming equivalence is insufficient proof. Relevant fork PRs must be supported or make the assessment explicitly incomplete. |
| Owner decision | Durable decision identity; separately authenticated owner; exact draft revision/digest, issue/source/baseline subjects, approved scope/recipes/policies and expiry. Its bytes and original expiry are immutable. It approves the bound meaning, not arbitrary future amendments or worker effects. |
| Approved requirements handoff | Complete approved requirements and provenance, bound to the current approved revision/decision and exact evidence subjects. The actual translation/refusal seam must receive these contents intact; a recording fake or a blanket refusal before delivery is insufficient. |

Issue/source observation times are freshness evidence, not semantic identity by themselves. Repeated
observations of unchanged content must not manufacture declaration revisions or renew a decision.
Changed issue content, source/base, baseline subject, requirement, scope or recipe must be detected
and cannot silently reuse prior approval. Purely operational changes remain distinct from changes
to intended results and their acceptance applicability.

### Current-Relevance Dispositions

The preparation result distinguishes positive current-source implementation evidence, a positively
established active equivalent PR, known work remaining, incomplete search, permission-limited search,
stale source, unresolved equivalence and unknown baseline. It includes the evidence and its limits,
not only a status label.

An already implemented issue is not redispatched because it remains open. A merged PR requires
current-source evidence before it establishes implementation. An active equivalent PR requires
exact state/head and supported equivalence evidence. Bounded or moving pagination, 404, missing
checks, an unsupported fork, or inability to interpret equivalence cannot prove exhaustive absence
or become `work remains`. Relevance may remain indeterminate while an inert draft is reviewable.

A known failing baseline can be relevant to an approved repair. Its intended failure, unrelated
failures, applicable recipe and exact subject must be explicit. Unknown baseline and unrelated
failure are not interchangeable with that evidence. Unresolved required freshness/relevance/
baseline evidence prevents admission; approving task meaning does not resolve those facts.

### Approval, History And Applicability

Reuse the immutable `ProgramSpec` revision/parent chain, existing domain owner checks, consumed
inputs/pinned policies, `SQLiteProgramStore` command journal and content-addressed artifacts.
Do not introduce another WorkUnit/Attempt lifecycle, controller database or synthetic approval
signature service. Inert draft/decision records must not appear as executable worker outbox effects.

Only a validated owner decision can produce an approved ordinary `ProgramSpec`. Any required
budget/authority value must come from the trusted owner policy/decision, not a proposal, price,
model name or a new fallback. Approval of incomplete meaning may be recorded, but missing required
values or evidence cannot yield an admissible Program or executable grant. Preparing or approving
meaning does not activate an ordinary task or enable the historical frozen execution entry point.

Bind the declaration, relevant source/baseline/recipe evidence and result-affecting scope through
inputs actually consumed by the WorkUnit or its pinned policy. Existing domain applicability
deliberately excludes unused inputs and operational-only budget/authority changes. Therefore an
unused metadata reference or changed ProgramSpec digest alone is not proof that stale acceptance
was invalidated. Test amendments that change meaning/verification/scope and separately test
unaffected history. A proposer amendment does not apply an owner amendment or widen a grant.

Decision reuse first validates current principal, exact revision/digest, all required evidence,
approved scope and expiry at decision time. Poll/read callbacks finish before the authority clock
is sampled. Existing journal deduplication may recover a historical receipt, but cannot make an
expired or superseded decision current, revive an earlier revision, renew expiry, or authorize
another effect. Recovery distinguishes an immutable historical approval from present admission.

### Requirements Delivery And Refusal

Kernel owns approved task meaning; Router owns capability calibration, profiles, model catalogue,
selection/admission and supported requirement interpretation. A full declaration is not just
prompt text, `reference` plus a context count, or a task-level choice.

Current public Router producer revision
[`1dae1948f372e0f1739896655bb7db97d6b08460`](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/commit/1dae1948f372e0f1739896655bb7db97d6b08460)
defines the existing three-part `TaskRequirement`, six capability dimensions with supplied minima
and nine typed hard constraints. It does not establish every proposed task/harness/authority
mapping; its privacy identifier does not prove privacy-policy enforcement. Router
[#175](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/175) receives actual Kernel
declarations and agrees supported interpretation before richer execution reception.

The #49 implementation must deliver the complete approved declaration and subject to a real,
requirements-preserving translation/refusal boundary. Unsupported or unavailable mapping returns
an explicit diagnostic tied to those original requirements, before any Router transport. It must
not construct the historical L0/reference request, invent fields/minima or drop privacy, interface,
context, verification or authority requirements. Successful rich mapping and execution preservation
remain [#51](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/51) and its producer reception;
this refusal boundary is not a claim those dependent requirements are complete.

### Compatibility And Reception

The frozen #143/#166 task definitions, logical digest encodings, exact-base rules, original
allocation/envelope and receipt recovery are unchanged. Do not retrofit ordinary defaults or
approval into them. Ordinary records must be explicitly distinguishable and versioned, survive
reopen/recovery, and fail clearly on unsupported versions without modifying historical state.

The issue's final proof includes immutable draft/approval round trips, owner/worker/Console and
prose injection negatives, edits/replay/expiry/scope/recipe/baseline changes, current-source and
equivalent-PR cases, partial/permission-limited scans, amendment applicability, actual intact
translation/refusal delivery, historical #143/#166 regression and installed operator migration
documentation. No new syntax is advertised as supported before this contract is reviewed.

The reuse comparison uses the independently inspected
[Symphony SPEC at `be10a1b79df723d6d7612b5651c8522704dafb2e`](https://github.com/openai/symphony/blob/be10a1b79df723d6d7612b5651c8522704dafb2e/SPEC.md)
and its Apache-2.0 licence. Borrow qualified tracker snapshots and reconciliation-before-dispatch
patterns only. Symphony's tracker/workspace/in-memory retry state, prompt policy, hooks and
implementation-defined approval posture do not replace Kernel's durable owner decision, grants
or acceptance. No source is copied, library adopted, scheduler imported or runtime constructed.

### Local Intake Reception

The implemented local path is `ports/intake.OrdinaryIntake` with `core/intake` inert values and
`adapters/intake.ForgeIntakeEvidence` / `GitContentSource`. It introduces no shared endpoint, CLI flag,
Router payload, remote authentication or execution contract. The preceding section remains the logical
proposal/history; these are the narrower actual local mechanisms, not new architectural authority.

`Declaration.raw` retains the exact UTF-8 proposer bytes. Its strict local JSON version 1 requires
exactly `version`, nonblank `outcome`, and string arrays `criteria`, `quality`, `interface`, `context`,
`unknowns`, `paths`, `network`, `effects`, `recipes`, `provenance`. Criteria/recipes/provenance are
nonempty; paths are relative without traversal, network requests name explicit HTTPS origins.
Unknown fields, duplicate keys, unsupported versions and authority/principal/policy/budget claims
are rejected. No integers for quality, prices, model-derived minima or Router vocabulary are inferred.
Unknowns are reviewable but prevent approval/admission in this slice.

Forgejo adds read-only `issue_snapshot`, `change_snapshot`, `intake_changes` normalization over
existing issue/PR reads and `changes` pagination. State, merged status, number, `updated_at`, exact
head/base, target branch (`base.ref`) and repository fields come from actual provider shapes; no API field is invented.
Snapshots are re-read and scans compared before disposition. A page bound, failure, unsupported fork
or observed movement refuses exhaustive absence. Repeated stable reads are qualified observations,
not a claim of a globally serializable Forgejo snapshot. Merged PRs do not establish current source
implementation. An active equivalent PR requires the exact selected target branch and supported
content proof at its observed head. Missing/malformed targets or target movement remain incomplete;
a known different target branch does not suppress selected-branch work merely because its SHA matches.
Permission-limited reads take explicit primary precedence over generic scan mismatch/incompleteness
and observed source movement. Normalized proof retains initial/closing read presences, scan failures,
qualified PR subjects and observed source revisions/digests without copying response bodies or
credentials. A failed closing read is not called source movement; movement is recorded only when
both compared reads succeeded. If permission loss and a confirmed change coexist, both facts remain
in proof while the primary disposition is permission-limited. These finite reads do not establish a
global atomic snapshot or exhaustive absence through an inaccessible/partial scan.

The built-in relevance proof is deliberately **literal**, using controller-configured
`ContentCriterion(path, expected_bytes)`: criteria identify exact file bytes, not arbitrary semantic
claims decorated with a hash. `GitContentSource` reads bounded exact objects with closed operational
environment and replacement/lazy-fetch disabled. It does not import repository code or execute recipes.
General textual requirements are unresolved, not guessed from titles, links or claimed booleans.
The draft retains complete normalized proof bytes and exact subject, not only a disposition/digest.
The separate `BaselineReader` receives already-produced identified source/recipe-bound evidence;
it is reobserved after closing Forge reads with exact semantic receipt identity comparison. Both
observation times must be valid. Freshness uses the earliest durably known age of the same qualified
subject/source/recipe/producer/reference/result receipt across immutable draft histories and retained
lower-age observations in the
controller store, including task-ID aliases. Declaration, issue-content and proposal-provenance edits
do not renew that receipt; a genuinely new identified receipt has its own age. These checks also run
on approval/handoff after reopen without rewriting historical records or adding a tracker.
An unchanged reread learning an earlier age appends a versioned `ordinary-baseline-age` fact to the
existing non-executable journal/artifacts, including through approval or handoff. It preserves the
original draft/decision bytes, revision and expiry. Reopen reconstructs interrupted age publication
from its original intent. Genuinely different receipts do not inherit another receipt's lower age.
Unchanged rereads create no draft revision and do not renew expiry. No baseline runner
is added. Unknown, future,
stale or unrelated failing evidence cannot become a pass through approval.

`OwnerPolicy` and current `Principal` are injected through trusted local controller composition, never
decoded from declarations. A current owner decision freezes the exact draft/evidence/policy/expiry.
Approval constructs an approved `ProgramSpec` only after validation; the domain Program remains
DRAFT. Declaration, full relevance/baseline subject and recipe digests are **consumed** WorkUnit inputs;
result-affecting changes invalidate applicability. Operational-only budget/authority changes retain
the existing core applicability behavior. Scope requests are preserved in consumed declaration bytes.

Drafts, decisions, consumption and lower-age facts use versioned `ordinary-*` records in the existing SQLite journal
and content-addressed artifacts. Reserved `ordinary-intake-record` requests cannot be claimed by
SQLite delivery, even though existing outbox bookkeeping is retained. There is no synthetic Attempt,
second lifecycle/table/database or metadata worker effect. Interrupted artifact publication is
reconstructed from original journal bytes; interrupted create/amend resumes the original decision
only after current owner/revision/evidence/expiry checks. Reads/callbacks finish before the authority
clock is sampled. Historical decision recovery exposes immutable bytes, not renewed authority.
Current Core `CANCELLED` status (owner abandonment) blocks approval replay, new approvals/amendments
and translator handoff even when the Spec digest is unchanged. The final return/handoff guard uses
`program_read_cut` on the existing single-writer SQLite store: it loads the authoritative Program,
reads/revalidates the latest draft and durable receipt age, and finishes pure owner/evidence/scope
checks before sampling the clock. SQLite's existing in-memory `total_changes` counter detects
reentrant callback writes during the cut (including clock-time cancellation or amendment); an
invalidated cut refuses rather than silently retrying. No database/artifact reads or writes remain
after that final clock/expiry/freshness check. Stored historical decisions remain readable.
This uses existing connection exclusivity, writer-thread ownership and gate, not a new lock,
tracker/table or atomic remote/runtime-gateway promise. Journal-only callback writes conservatively
invalidate the cut too; a read-only callback can advance time but cannot bypass expiry/freshness.
Other Core states retain their existing amendment semantics, including explicit amendments from
COMPLETED. This adds no activation, resume or other lifecycle transition to ordinary intake.
Recovery validates the actual record kind, namespace and content subject, not a draft-looking ID
alone. New consumption IDs include `:record:` to avoid draft-ID collisions; legacy consumption IDs
remain readable as their actual kind. A legacy consumption occupying a new draft's normal ID uses
the disjoint `ordinary:record:ordinary-draft:<task>:<revision>` namespace without rewriting the
legacy row. Accepted task IDs such as `draft`, `consumed`, `decision` and `baseline-age` remain usable.

The installed package smoke and intake tests receive the complete ordinary declaration at
`ScarcityRouterAllocator.translate_ordinary`. Its static projection/refusal seam needs no configured
model/binding or Router operation. Both the inert `ResourceRequest.requirements_handoff` and
`TranslationRefusal.handoff` retain original declaration bytes/provenance, draft/evidence subjects,
revision, exact decision bytes and Program digest. Unsupported prose, including the historical
private-local fixture, still refuses before transport. No reference/L0 request is built for ordinary intake.

### Approved Recommendation Reception (#51)

Independently inspected producer: Router #175/PR #200 merge
`5c48d51f1eb1f11424a5100eb2ccf20a6cba4581`, public raw GitHub source, D-075 and
`kernel_requirements.py`, `selection_types.py`, `selection_app.py`, `selector.py`, `routing_core.py`,
`tests/test_kernel_requirements.py`. This receives a bounded model-recommendation subset, not the
producer's local executable `RouteRequest` or a gateway-conformance receipt.

`requirement_markers` lets a trusted controller generate reviewable markers before approval.
Quality must contain exactly one `scarcity-router.requirement.v1:` JSON object with required complete
`requirement` and optional `profile_id`. Interface is empty or one `scarcity-router.request.v1:`
existing `RequestBinding` object. Nested duplicate/nonfinite/oversized/deep JSON, unsupported dimensions,
malformed types and contradictory pins refuse with safe field codes, not submitted content. The six
producer dimensions and supplied 1..5 minima are validated, never classified, averaged or ranked here.
Producer normalization omits optional null minima/identities and false booleans only; exact original
bytes remain in the handoff. Nonempty textual context/unknowns cannot be erased by a typed marker.

| Requirement | Recommendation reception |
| --- | --- |
| Quality/task level/hard model properties | Preserve the full producer-normalized `TaskRequirement`; no default profile, L0 or guessed floor |
| Explicit model/variant | Intersect with quality's provider/model/variant; mismatch refuses before transport or at exact selected-identity check |
| Tools/reasoning/context | OR positive tools/reasoning demands, combine independent context amounts by maximum; never weaken quality |
| Structured output/streaming/reasoning controls | Retain the RequestBinding separately; require independently configured RuntimeSupport features |
| Output allowance floor | Preserve producer hard minimum and require configured harness output allowance; never synthesize an execution ceiling |
| Profile | Send existing `profile_id` plus full `tightening`, not `requirement`; require configured profile policy version and exact returned full expansion equality |
| Alias/resource pin/output ceiling/access/channel/vision/privacy | Refuse unsupported meaning before recommendation transport; null known optional route-only fields mean no demand, unknown fields still refuse |
| Result/criteria/recipes/paths/network/effects/provenance | Bind unchanged original handoff; never Router-derived grants, executed recipes or acceptance |

For a profile marker all structural hard/pin demands must already be included in the approved
quality requirement. An interface cannot mask an omitted profile floor. Router performs its existing
monotone profile resolution; HTTP 400 is `request_invalid` (including unknown profiles or weakening),
not no solution. Exact returned requirement equality refuses hidden profile additions or weakening;
matching profile/version provenance is mandatory. Kernel does not import or reproduce profile policy.
An explicitly supplied lower profile floor cannot be clamped locally or accepted as a weaker task.

`OrdinaryIntake.select` is an explicit trusted-controller recommendation call, not part of preparation
or approval. It calls the real handoff/allocator seam and revalidates current principal, issue/source/
baseline/revision/decision/Program/expiry after the allocator returns. An inert handoff is not a reusable
authority receipt: direct allocator callers remain trusted and must perform these current checks.
Configured runtime support records are external controller assertions tied to an identified runtime
version/evidence reference, not native negotiation or current executable gateway proof. Model-only
recommendation cannot grant unsupported protocol features or turn scope requests into permission.

Responses require machine schema 1, exact requirement/profile echo, positive catalog/resource-policy
versions, dated catalogue and aware decision time, valid selected/refusal semantics, and exact configured
provider/model/effort/variant plus harness compatibility. The existing bounded HTTP/strict JSON/credential
scanner remains in use. Ordinary evidence containing the configured bearer is refused rather than
persisted. Safe categories distinguish requirement problems, no eligible candidate, infrastructure and
compatibility. Router refusal never triggers local fallback or alternative ranking.

Ordinary Allocation version 2 retains complete original draft/decision/Program correlation, the actual
sent request and separate structural/runtime compatibility facts alongside the original public decision
(including catalog/policy/profile provenance). Existing atomic Attempt preparation binds these Allocation
bytes before dispatch. Synthetic lost-receipt/reopen tests use existing journal/domain/recovery primitives
and prove no reselection after unknown effects. No ordinary native dispatcher or execution engine is
introduced; the reference composition refuses new ordinary allocation rather than silently sending L0.
#52/#53, isolation and product acceptance remain separate receipts. This is offline producer-shaped
transport/recovery evidence, not live adequacy, admission, privacy or execution authority.
