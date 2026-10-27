# ADR 0012: Unified Work Coordination And Capability-Based Drivers

Status: accepted architectural direction, 2026-10-26; independently reviewed and integrated through
[PR #104](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/pulls/104) on 2026-10-27 at
`ace7175e087cd70e207594f5631c8f4f70fabfd4`.
[Mandate #103](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/103) records canonical
publication and the owner's 2026-10-27 delegated objective consistency-acceptance evidence.
Architecture integration is not feature implementation or live acceptance.

## Context And Decision

Creatidy Kernel is a vendor-independent software-engineering work-control and resource-coordination
layer. Existing coding agents own reasoning, model/tool loops and their native user interfaces.
External trackers, SCM, CI and knowledge systems retain their own records. Kernel owns approved
work, its durable identity and decisions, not copies of those products. The primary user can continue
working in Kilo. Neither Codex, Kilo, ZCode, a gateway, web Console nor IDE extension is mandatory.

There is **one core and one application decision path**, with two execution interaction modes:

- **Pull:** an authenticated existing work client discovers/claims work and reports evidence.
  The external harness owns its session and model/tool loop. Kernel does not acquire process ownership
  through a claim, MCP connection, heartbeat, plugin or UI.
- **Push:** Kernel initiates a dedicated managed worker/session through a version-evidenced Runtime
  adapter. Only capabilities actually received for that adapter/profile permit start, observation,
  cancellation and settlement claims. Dedicated ownership is not detached supervision.

Both use the same Program/WorkUnit/Attempt, admission/claim/reservation policy, operation journal,
verification/review/remediation, acceptance and external-resource bindings. There is no pull scheduler,
push workflow database or frontend authority engine. A mode is a property of a particular handoff,
not a new Program lifecycle. Moving modes requires settlement and a new authorized Attempt, not
rewriting an old Attempt's provenance or attaching to an owner's session.

## Ownership

| Owner | Authoritative responsibility | Boundary |
| --- | --- | --- |
| Kernel | Planning/selection and validated dependencies; approved Task intent, Program/WorkUnit/Attempt; work admission/claims/leases, work budgets/concurrency; authority, verification/review/remediation/acceptance; durable effects, uncertainty, recovery and owner decisions | Not a coding agent, tracker, CI service, model catalogue/ranker/quota collector, inference gateway or general automation platform |
| Work Management provider | Native work records, edits, state and dependency records it actually supports | A closed ticket is not Kernel acceptance; issue prose cannot grant authority |
| SCM / Code Review provider | Repositories, refs/commits, changes, native reviews and merge state | A PR or transport success is not product acceptance or merge permission |
| CI / Verification provider | Its jobs, checks, artifacts and execution observations | A green job is evidence for an exact subject, not a worker grant |
| Knowledge provider / MI | Source content, provenance and its publication/withdrawal semantics | Context is not task authority; MI is not a private usage/work database |
| Harness | Native session and model/tool execution loop | A result or self-reported completion is an untrusted candidate |
| Scarcity Router | Provider accounts, execution sources/subscriptions/quota, model compatibility/ranking, selection/admission/gateway, resource-side cost evidence | Not Kernel work acceptance or a competing workspace controller |
| Console / CLI / TUI / optional IDE UI | Presentation and forwarding authenticated commands to the owning product | No direct Kernel SQL, scheduler, policy or permission database |

Kernel's local history records qualified observations and decisions, not an authoritative mirror of
external mutable records. Acceptance and external synchronization are separate durable facts; a failed
tracker update does not erase acceptance or fabricate external completion. Reconciliation exposes both.

## Unified Work Contract

The following are normative semantic operations, **not shipped endpoints, serialized identifiers or
new enum values**. Implementation extends existing domain/application/ports and versions concrete
records through their functional deliveries. CLI, MCP, HTTP or SDK entrances call the same handlers.

| Step | Shared semantics and durable boundary |
| --- | --- |
| Discover / refresh | Observe selected projects through configured work drivers, exhaust supported pages, retain query scope/revision/time/completeness and permission limits. Re-read current work/source/PR/CI requirements before eligibility; a notification is a refresh hint, not truth. Unreadable or changing evidence is unknown, never empty work. |
| Plan / select | Build a finite approved Program DAG from explicit obligations and required outputs; validate cycles, missing references, external-edge semantics and applicability of accepted predecessors. LLM planning is a proposal. Approval binds exact revisions, scope, recipes, evidence and policy; discovery/selection alone starts nothing. |
| Claim / reserve | On the single writer, atomically compare current work/spec/policy revision, owner authority, eligibility, resource conflicts and finite capacity; bind one claimant, Attempt preparation identity, reservation and increasing fencing generation. This work claim is not the existing outbox delivery claim. Repeating a command key with identical input returns its recorded outcome; changed input conflicts. |
| Handoff | Persist immutable approved scope, original time envelope, capabilities/profile, workspace/context/source subjects and allocation evidence with Attempt and Operation before permitting a push start or returning a pull work grant. A discovery response is not a grant. Uncertain handoff receipt is reconciled, not a second Attempt. |
| Report / renew | Authenticate claimant and exact Attempt/generation, check current scope and original deadline. Progress may be lossy; authority changes, candidate/effect receipts and decisions are durable. Renewal cannot extend original authority or revive a stale generation. Quiet work need not be stalled. |
| Submit / verify | Bind immutable collected candidate artifacts and exact base/head/spec/policy to the Attempt. Check trusted preconditions before candidate commands; run policy-selected verification outside author authority and with the selected execution safety profile. Submission is not acceptance. |
| Review / remediate | Use the existing durable product review flow with a fresh separate read-scoped conversation and exact subject. Review receipt, verdict, findings and transport success differ. Fixes consume a new authorized Attempt within shared limits; changed head/base/spec/policy invalidates applicable checks/review. |
| Accept / synchronize | Only Kernel validates all exact-subject evidence, current authority, applicable predecessors and task-scope settlement. Record acceptance atomically with enabling successors; separately journal each authorized SCM/tracker/notification effect. Acceptance grants no automatic merge, publication or deployment. |
| STOP / expire / recover | Persist revoke/cancel intent, deny new admissions/effects, fence stale reports and initiate only supported cancellation. Reconcile exact original handles and effects; never renew, reroute, resubmit or infer absence from incomplete search. Retain evidence/workspace while conflicting work is uncertain. |

Claim expiry is loss of authority, **not proof of stopped execution**. A replaced generation cannot
publish results or mutate through Kernel, but an externally running process can still affect reachable
files or services. Do not reassign conflicting workspace/session/effect ownership or release uncertain
capacity merely because heartbeats stopped. Admission of unrelated non-conflicting work can continue.

For pull, a supported cooperative task-scope quiescence/candidate-handoff receipt is weaker than a
managed process witness. Record its source and assurance; it must cover the claimed task resources,
not assert that the user's entire Kilo session exited. If such evidence is missing, quarantine the
conflicting resources and block acceptance/publication and successor dispatch. Independent immutable
artifact capture does not by itself settle outstanding mutations. Profiles requiring physical process
settlement or hard inference admission refuse a merely cooperative client. Owner-authorized isolation
of a conflicting resource is explicit reconciliation evidence, not a fabricated no-effects receipt.

## Capabilities And Compatibility

An open-ended registry binds configured instance/scope and authenticated client identity to a
versioned driver implementation, contract major/minor support, declared operations and independently
verified evidence/profile. Registration is trusted configuration, never plugin self-assertion. A
declaration says what may be attempted; current authority and actual conformance still gate use.

These semantic categories are composed capabilities, not wire identifiers or an ordinal trust ladder:

| Category | Required capability | Does not imply |
| --- | --- | --- |
| Work client | Discover/read approved work; claim/report/submit over authorized Kernel handlers, with dedupe and fencing | Native event feed, session takeover, process cancellation or observed model identity |
| Session observer | Authorized stable session correlation and supported observations/events, declared gaps/freshness | Work mutation, start/cancel, durable event history or descendant settlement |
| Managed executor | Dedicated owned target with versioned start/observe/candidate/cancel/reconcile and explicit unknown/unsupported results | Hostile isolation, detached survival, per-syscall expiry or no hidden inference |
| Controlled-execution profile | Additional separately demonstrated restrictions such as scoped executor, egress, exact route/pin admission, process witness and supported resource bounds | Universal compatibility, physical model identity, zero cost after STOP or capabilities absent from its evidence |

MCP, CLI, native SDK/API, ACP and other documented interfaces are transports/mechanisms, not these
guarantees. A basic Kilo work client can operate without intercepting its internal inference HTTP.
An optional Kilo integration may request/report work; an optional IDE UI may display state. Neither
becomes a mandatory trusted enforcement plugin. #52 proves that a plugin loading failure can leave
published unguarded configuration usable; loading or naming a guard is not fail-closed enforcement.

Unknown required capability, incompatible major/schema, unverified profile, inaccessible required
evidence or unsupported native semantics refuses before mutation. Optional missing fields remain
explicitly unavailable. Additive compatibility is allowed only where the declared contract permits it;
unknown authority-bearing fields are not ignored. Pin conformance to exact adapter/harness/protocol/
platform and source revisions. Upgrades do not silently promote old receipts or profiles.

## Driver Design

Split responsibilities behind inward-dependent ports, not a universal provider client:

| Family | Required boundary | Existing starting point and migration |
| --- | --- | --- |
| Work Management | Read/search/refresh native work; qualified dependency edges; explicitly optional conditional issue updates | Extract intake's issue/snapshot reads from `Forge` / `ForgeIntakeEvidence`; replace numeric-only ordinary selection for new records, preserve legacy `IssueSubject` bytes |
| SCM / Code Review | Exact repositories/objects/refs/change subjects, native review facts; conditional authorized branch/push/PR/update operations | Retain `Forge`, `ForgeBinding`, Git source validation and Forgejo AGit receipts as a versioned reference facade; move tracker/CI responsibilities behind separate bindings |
| CI / Verification | Observe exact-subject check/job/result/artifact evidence and optional authorized trigger/cancel/reconcile | Extract Forge checks from SCM; keep trusted verification recipes and `Verifier` separate from a CI provider's evidence/mutation adapter |
| Knowledge / Context | Fetch explicit source revisions/content, permissions, provenance, withdrawal/freshness when supported | Extend existing context manifests/readers; repository docs and configured Confluence can supply context. Existing MI utilized-reference retention is not direct MI content retrieval |
| Harness / Execution | Work-client participation or dedicated managed lifecycle, separately declared observations and safety | Reuse `Runtime` / `Workspace` and existing Codex adapter; add cooperative entrance to the shared application, not a fake push Runtime or new model loop |
| Resource / Capacity | Work-level finite non-model capacity/reservations and external model-resource selection/evidence where requested | Reuse `BudgetPolicy`, authority, `ResourceAllocator` and durable allocation/reservation references. Keep Router translation separate from local work capacity; no model inventory |

Each task has independent bindings. Jira tasks + GitHub SCM/reviews + Jenkins checks + Confluence
context is a supported architectural composition, not a claim those adapters already ship. Forgejo
can implement several families through one configured implementation without forcing them to share
host, project, credentials or authority. No driver infers the other bindings from a task URL.

Every external subject preserves provider family/implementation and version, instance/host including
port, tenant/project/namespace, opaque native key, resource kind, source revision (or explicit absence),
and observation provenance. Internal Task/Program/Attempt identities are separate. `ENG-42`, UUIDs,
URLs and provider-owned strings are not converted into a universal issue integer. Native revisions
need not be Git SHAs or sortable integers; references compare only under that producer's semantics.

Normalized fields are the smallest validated common meaning. Preserve attributed native states,
dependency types, revision tokens and evidence needed for interpretation; do not flatten Jira workflow
states, GitHub checks or Jenkins results into a fictional universal pass/completed state. Unsupported
required dependency enumeration refuses eligibility unless an explicit owner-approved Kernel edge
and freshness policy supplies that obligation; textual links never create edges automatically.

Paged reads carry scope/cursor, observation cut and completeness. A capped scan, missing page,
permission-limited 404, eventual index or moving revision is not authoritative absence. Drivers state
whether rereads establish a stable qualified observation or a stronger native snapshot. Conditional
updates bind the native precondition and exact changed fields; if the provider cannot enforce the
required condition, refuse that automated mutation rather than emulate CAS with read-then-write.

All external effects use existing intent/outbox/delivery fencing/receipt/reconciliation. Journal the
family/binding version, exact target/revision, input digest, authority and dedupe identity before send.
Preserve native correlation/receipt and post-effect observation separately. Timeouts remain unknown;
retry only after authoritative nonexecution or a proven native idempotency contract. Cross-driver
operations are not one transaction or exactly-once delivery. Partial synchronization is visible and
recoverable; compensation is separately authorized, not invented rollback.

Workspace materialization/collection/cleanup and content-addressed artifacts already have concrete
ports/storage ownership; retain them. No additional generic notification, IAM or artifact-provider
framework is justified now. #57 owns useful decision notifications as replaceable delivery, the
authority broker and host authentication own principals/policy, and existing artifact manifests own
bytes/retention. Remote storage or identity federation requires an actual consumer/authority contract
before a new driver family, not speculative interfaces.

## Resource And Economic Rules

Kernel reserves work capacity before admission: project/work concurrency, workspace ownership, finite
Attempts and review/remediation capacity, original time envelopes and owner-approved money limits
where required. Reservations and consumption are distinct; release requires evidence appropriate to
the reserved resource. Use atomic local accounting for Kernel capacity; external reservations require
real producer receipts. Selection is not reservation or permission.

Keep money/currency, tokens, subscription-specific units, elapsed/compute time and owner attention
separate. Unknown consumption is not zero, quota is not tokens, and an estimate is not billing.
Corrections retain receipt lineage and do not double-count. Reserve conservative headroom or refuse
a hard-budget profile when unobserved/in-flight consumption cannot be bounded. Do not invent numeric
owner budgets: implementing validated configurable limits with synthetic fixtures does not require
choosing a real user's budget; real admissions do.

Router remains the owner of model/source compatibility, quota/admission/route-affinity and gateway
cost facts. Basic work coordination may use an existing explicitly acknowledged harness configuration
without Router; record inference attribution/admission as unavailable rather than forge an Allocation
or call it Router-enforced. If a task requires exact pinning, enforced routing, strict inference
admission, hard quota authority or no unobserved calls, require the proven corresponding integration
or refuse. Dynamic Router choice remains the intended default **within that route-enabled profile**;
explicit pins retain their strict semantics. This does not weaken the Router executable contract.

The #52 Kilo 7.8.18 multipart refusal and fail-open-plugin evidence remains negative, with exact pins
in [harness feasibility](../architecture/harness-feasibility.md#bounded-52-kilo-offline-falsification).
Basic Kilo work participation is not gated by that inference-wire mismatch. Successful full executable
Router/harness integration remains a genuine #52 acceptance obligation, not renamed recommendation.

## Command, State And Owner Interaction

One owner-served application contract is consumed by CLI/TUI, pull work clients, managed-execution
adapters, Console and optional IDE/Kilo UI. It must supply discover/read, exact-revision commands,
approval/amendment, claim/handoff/report/submit, verification/review decisions and STOP/reconciliation
with capability-scoped authorization. Clients cannot pass an arbitrary trusted `Principal`, set state,
accept their own candidate or invoke driver tokens directly.

Mutating requests have caller identity/scope, subject and expected revision, stable command identity
and input digest. Replayed receipts are historical, not renewed authority. Authenticate outside
untrusted request bodies; separate owner, worker and observer rights. A local trusted API can remain
in-process; remote transport must receive authentication, grant/revocation, origin/exposure and
credential-safety semantics before activation. No specific endpoint/auth scheme is selected here.

Serve snapshots from the exclusive store owner or its controlled projection, not a second reader.
Durable decisions/effects are distinct from bounded lossy progress. Positions are producer-scoped;
define dedupe, retention, explicit gaps and snapshot resync. Reconnect performs no execution. Commands
recheck current revision/authority after waits; accepted request is not settled effect. Correlate
Task/Program/Attempt/claim/Operation/native session/candidate and available route/call IDs without
inventing missing ones. Useful owner-decision alerts supplement durable pending decisions; the system
remains correct with no plugin, browser, notification delivery or Console connection.

## Security, STOP And Recovery

[ADR 0011](0011-task-level-authority-and-trusted-development.md) remains authoritative for its
trusted-development profile: original deadline, suspend-inclusive verified clock projection and
fail-closed recovery requirements; durable task authority and cancellation; unknown outcomes block
acceptance/publication/conflicting successors. This ADR does not implement missing clock behavior.
Bounded managed STOP proof does not become strict native syscall expiry or hostile-worker isolation.
An external pull session provides weaker cooperative task-scope control, explicitly selected and
labelled; it cannot be advertised as the managed ADR 0011 process profile.

Revoke authorization immediately at the Kernel admission boundary. Physical cancellation is a
separate operation possible only for the exact owned supported target. An expired lease, terminal turn,
leader exit, interrupt receipt, UI close or heartbeat gap is not descendant settlement. Never kill a
shared Kilo server or unrelated session. Preserve unresolved scope/effects and deny conflicting reuse;
do not confuse reconciliation with starting a new worker. Existing same-principal credential/egress
limitations remain visible; strict or protected tasks refuse unsupported profiles. Verification-only
ADR 0008 does not isolate the author harness or reviewer conversation.

## Supersession And Migration

This revises ADR 0007's universal push/gateway interpretation and extends ADR 0001/0004/0006. Those
historical decisions and evidence remain intact; their ownership, inward dependencies, durable effects,
exact acceptance and privacy rules remain. Owned Sessions was the bounded #47 proof choice, not an
exclusive full-product architecture. #53 remains closed/not implemented and PR #85 unmerged; no code
or negative test from that experiment is accepted by relabelling it.

Retain current Program/WorkUnit/Attempt, SQLite single writer, operation journal, artifacts, approved
ordinary-intake bytes, allocation codecs v1/v2/v3, source ownership, bounded Codex/Forgejo adapters,
product review/remediation and STOP. Extend through explicit migrations and new functional deliveries:

1. Deliver split driver bindings and executable heterogeneous adapters with Forgejo conformance and
   provider-qualified new identities. Keep legacy `Forge` facade and frozen #143/#166 recovery intact.
2. Deliver shared work claims/finite work budgets (#59/#56). No #59 dependency on its own future
   presentation and no blanket #52/Console/MI readiness gate.
3. Deliver ordinary Program planning/selection/admission and pull/push handoffs through the same
   durable core, reusing approved intake and product review; #57 then exposes its full shared served
   commands/events without gating local execution on remote transport. This is complete work
   functionality, not another preparation-only draft, PoC or new generic workflow engine.
4. Complete independent strict Router integration (#52), private full outcomes (#58), compatibility/
   installed migrations (#62), and optional direct MI evidence (#55) in their real dependency lanes.
   #60 receives the declared installed product profiles after components; it never enables them.

Each changed schema/binding/profile preserves original identities, bytes, evidence strength and
unknowns; backup and interrupted-upgrade/downgrade refusal precede replacement. No automatic mapping
from numeric legacy IDs to unrelated tracker keys, legacy Allocation to enforced route, trusted profile
to sandbox, or configuration version to a new guarantee. Unsupported migration preserves data and
refuses without destructive repair. A changed harness/source revision invalidates its compatibility
cell, not every independent driver or unrelated Program.

The [current target](../architecture/target.md), [roadmap](../architecture/successor.md) and
[backlog/impact publication plan](../architecture/backlog-alignment.md) form one target and evidence
map. Integration of this record is not implementation, live readiness, owner numeric budget, merge
authority or acceptance of the complete product. Ordinary development requires consistent canonical
publication and acceptance under the owner mandate. The 2026-10-27 owner instruction delegates that
objective acceptance and subsequent normal `/loop`; #103 records whether those gates actually passed.
