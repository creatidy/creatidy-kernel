# Port And Recovery Contracts

Current target and planned port split: [ADR 0012](../adr/0012-unified-work-coordination-and-drivers.md)
and [unified work architecture](target.md), integrated through PR #104. Their unified pull/push
semantics and six independently bound driver families replace a universal managed/gateway-only
interpretation, not the existing wire contracts below. New work claims are distinct from outbox
delivery claims. Legacy numeric intake, `Forge` facade, Codex, allocation bytes and AGit recovery stay
readable and keep their original guarantees. See [current migration](migration.md#unified-core-migration).

The table records post-A0 requirements, not a claim that every adapter passes every case. A0 coded
only the narrower ResourceAllocator; subsequent bounded ports/adapters exist. Current verification
and missing receipts are in the [coverage roadmap](successor.md), governed by
[ADR 0007](../adr/0007-shared-harness-routing-observability.md). Ports use core-owned values;
unsupported requirements must fail closed, not through weaker fallbacks.

## Local Owner-Served Contract

The version-1 local contract is implemented by `ports.served.OwnerProjection` and
`adapters.served_local`. The trusted host owns the existing exclusive SQLite connection and must call
the handler on that same owning thread. The observer never opens the database or changes an Attempt,
dispatches work, renews a lease or grants authority. A snapshot contains the Program revision/spec,
work-unit and Attempt facts, recorded budget revocation, exact reservation/Operation correlations and
per-Program served-command receipt coverage. Command history is a deterministic sample of at most 64
records, with `(sampled, total, complete)` coverage; when incomplete there is no history continuation
API, but a caller can replay a known exact command ID to read its own historical receipt. It is not a
complete event log. Route identity and physical settlement remain explicitly
unknown where no producer supplies them. Claim generation, recorded proof stages, quarantine, release,
recorded workspace subject and recorded reconciliation need are journal facts, not a fresh
filesystem scan, current lease guarantee or physical-settlement assertion. When multiple subjects were
observed, the legacy journal cannot establish their last visit, so these fields are unknown. Recorded
workspace, candidate and runtime identifiers and review dispositions are correlated to their Attempt;
absent evidence remains unknown. Decision applicability is `pending`, `historical` or `unknown` based
on the current Program, WorkUnit and Attempt specification; resolved or superseded decisions remain
in history but do not generate new action-required alerts. Usage values are grouped by metric, unit and provenance as known
subtotals; unreported work remains unknown and is never counted as zero.

The status includes the original task deadline, original claim expiry and latest recorded renewal
horizon. Its clock sample is explicitly uncommitted and display-only: `not-expired-observed` is not
authorization, and dispatch always repeats the producer's durable clock, budget and claim checks.
Unverifiable clocks yield `unknown`; expiration or revocation suggests reconciliation or STOP, never
claims physical settlement, and observing status does not renew or persist a clock sample.

Progress positions contain an ephemeral producer epoch, Program scope and monotonically increasing
sequence. The bounded buffer is an observation hint, **not a durable global event log**. Because a
snapshot sampler cannot prove it observed every intermediate mutation, a detected state change emits a
`resync-required` hint with `gap=true`; restart, wrong producer/scope, future position and buffer
overflow also require a fresh snapshot. Reads and reconnects cannot dispatch or renew. Positions from
Forgejo pagination or native runtime notifications are not interchangeable with this producer position.
While command coverage is incomplete, event reads continue to report a gap because the sampled history
cannot prove it observed changes to omitted receipts.
Snapshot collections are returned in pages of at most eight records each, with per-collection
`(offset, total, at_end)` coverage. Request `snapshot --page N` and compare the producer position
across pages; if it changes, restart from a fresh snapshot. While any collection exceeds the first
page, event reads also report a gap. Records exceeding 4096 encoded bytes appear as a digest, byte
count and index rather than inflating the frame; `record` reads their canonical JSON in 8192-byte
base64 chunks, qualified by the exact producer position, collection and index. A changed position
refuses the detail request. All of these reads preserve durable authority.
The position conservatively incorporates SQLite's owning-connection and external change counters
across every page, so unrelated database writes can also require resynchronization. A clock sample's
exact display timestamp is not part of the durable cut; changes in task or claim expiry classifications
do advance the position.
Program IDs are not shortened in storage. IDs whose encoded representation exceeds 4096 bytes use
a SHA-256 scope reference in the cursor and an oversized marker in the wire snapshot; the exact ID is
recoverable through the position-qualified `record` operation with collection `scope` and index zero.

The host creates an already-connected local `socket.socketpair()` and binds one endpoint to a
`LocalReadChannel`, `LocalWorkerSession` or `LocalOwnerSession`; it passes only the peer descriptor to
`creatidy-kernel-client` (`--fd N`). The host must service `serve_once()` on the SQLite owner thread
after the descriptor becomes readable, then detach one-shot channels. Separate worker and owner
capabilities are bound to their existing trusted `OrdinaryWorkClient` or `OrdinaryProgram` objects; a
wire payload cannot select a `Principal`, policy, provider credential, outbox fence or review verdict.
Closing/revoking a descriptor revokes communication only, not task authority or physical settlement.

For optional local alerts the host may bind a connected `AF_UNIX` datagram endpoint to
`LocalDecisionNotifier` and supply it to a `LocalWorkerSession`. A completed served report publishes
bounded `decision-required` hints without blocking and deduplicates each decision within that notifier's
lifetime. Full, closed or unavailable endpoints set `dropped` rather than retrying work; after loss,
restart, or reconnect the consumer must read the authoritative snapshot. The host may also call
`publish(snapshot)` after other owner-thread changes; notifications exist only while the host services
the session and are not a persistent service or remote notification guarantee.
Uncertain product reviews are derived from the original review request, reservation, operation and
receipt state, not from the transport receipt; resolving one does not issue another review prompt.
An immutable earlier served `review_unknown` receipt remains historical after its exact original
review operation is accepted with a recorded receipt; it does not create a second pending decision.

Frames use a four-byte network-order length and UTF-8 JSON version1; input is bounded to64KiB and
output to1MiB. Read actions are `snapshot` and `events`. The worker capability accepts `claim`,
`context`, `start`, `quiesce`, `renew`, `submit` and `report`; `context` is bounded and uses the bound
original claim's existing authorization and workspace-observation handler to supply the approved
declaration, effective inputs and any original remediation findings. It is not a historical grant of
authority and can be refused after expiry or revocation. The owner capability also supports `proposal`:
it returns the complete canonical host-bound proposal payload, its scope and SHA-256 digest, or refuses
if the proposal is unavailable or the response exceeds the frame limit. It is a read, not approval.
The installed client uses `proposal` before `approve --proposal-digest <digest>`; the digest is an
identity, not an authorization credential. The owner capability accepts `approve`, `reconcile`,
`start-managed` and `stop`. Mutations require a command ID and caller-observed expected Program revision
(`-1` means absent for initial approval). Trusted host composition binds an exact approved proposal,
claim and managed target where applicable; the request cannot choose them. Approval requires the exact
digest observed by the caller; missing or mismatched identities refuse even for legacy requests. The
handler captures one frozen proposal and uses that same object for the journal and application approval,
so a later host binding replacement cannot switch the approved meaning. Existing authority, revision and
evidence checks still apply. New approval requires the existing exact producer-recorded proposal artifact
needed for recovery; an unrecorded host-supplied proposal refuses. Historical pre-change approval receipts
remain stored but the legacy request format cannot replay them through this command; no old request grants
fresh authority. Exact duplicate command
inputs return their original receipt as historical evidence, not renewed permission. If an intent
exists but its outcome receipt is missing, the response is `unknown` and no automatic repeat occurs.
Cooperative task-scope quiescence is never represented as managed process settlement; a STOP receipt
can prove at most its one explicitly bound original managed target, not all effects or descendants.
Worker `start` is limited to pull handoffs; a managed handoff can start only through the original owner's
`start-managed` capability.
Before a new managed start, the retained managed client's exact approval must match the session's
current host-bound approval. An amendment does not retarget an old managed client. A matching historical
start receipt remains readable without dispatching again, even if the shared approval subsequently changes.

This descriptor capability assumes a trusted local host chooses and passes the correct endpoint; it
is not remote authentication, a named service socket, hostile same-UID isolation or a grant to expose
the store over a network. Authentication, caller admission/revocation, exposure/origin policy and
credential-safe diagnostics must be received before any remote transport is enabled. The CLI does not
create a controller, open SQLite or discover a capability by itself. Installed conformance exercises
separate observer/worker/owner client processes against the existing owner, including stale revisions,
role injection, duplicate commands and explicit gaps; it is not a Console UI or live-provider proof.

### Supervisor-Launched Owner Backend

`adapters.served_host.launch_owner` is a Linux-only supervisor SDK for a child backend, not a listener
or attachment to an existing process. The trusted supervisor selects and authenticates an absolute
executable and its arguments, supplies a deliberately restricted environment, and binds an existing
`LocalOwnerSession` to the existing owner, approved proposal/work and exact Program scope. The SDK
creates a Unix stream socketpair, attaches the owner end, and appends the numeric child descriptor as
the last argument. Only that descriptor is passed; the supervisor closes its copy of the child end.
`receive_owner` takes ownership of any integer descriptor numbered at least 3 even on refusal, including
unsupported platforms and non-socket descriptors. It never consumes standard descriptors 0, 1 or 2.
The backend must receive the descriptor, expected scope, producer label and recipient label from its trusted startup configuration and
call `receive_owner` before using the version-1 served protocol. The fixed greeting identifies
`creatidy-kernel-owner-descriptor-v1`, wire version 1, role owner, exact scope, and supervisor-configured
producer and recipient labels; unknown versions, extra/duplicate fields and mismatches refuse. Labels are
non-secret, bounded ASCII identifiers: the trusted supervisor must map each producer label to the intended
actual Kernel composition and each recipient label to the intended backend, and independently configure
the backend's expectations. Scope alone does not identify a producer. This checks the supervisor's
configured pairing, not the actual database, executable, durable identity or cryptographic authentication.
There is no durable SQLite producer ID: projection positions change with each construction, and filesystem
ownership metadata does not authenticate a producer. Duplicate or incorrect label assignment, descriptor
substitution, a forged greeting or a compromised supervisor defeats this check. Trust derives from the
supervisor's correct selection and protected descriptor inheritance, not the labels. No hostile same-UID
isolation is claimed.
Owner session and channel construction additionally require the projection, application, bound ownership
controller, and any managed workspace and managed worker/control bindings to reference the very same
in-process store object. This prevents accidental cross-store composition even for equal Program scopes;
it is not a durable database identity or authentication of the host's chosen store.

The owner thread polls `launched.channel.fileno()` and calls `launched.serve_once()` when readable; it
detaches the channel if serving fails, including EOF. The host must also call `launched.close()` in a
`finally` when a child exits, a launch is replaced, or the host stops polling it; merely observing process
exit or using `launched.channel.serve_once()` directly does not detach it. `close()`
detaches and closes that channel, not the child process or task. Session revocation closes all attached
channels; individual launch handles may still be detached afterward. Concurrent clients
need separate launches and descriptors, subject to the session's 64-channel bound; requests are serialized
by the owner thread. Revocation is serviced on that same thread, so it cannot preempt a command already
being handled; that command's outcome must be recovered from its original producer evidence. Each served frame has existing bounded read limits; long-running underlying actions
retain their own existing limits and uncertainty. A launch failure detaches the channel. There is no
descriptor expiry or reconnect to an existing process: on backend restart launch a new child and channel;
on Kernel/supervisor restart rebuild the original authorized owner composition and launch anew. Old
channels cease communication when their owning process exits, but a surviving child may need separate
supervisor cleanup. No channel closure cancels work, rolls back an effect, revokes task authority or proves
settlement. The owner must separately invoke existing Kernel authority revocation and the existing
command handlers recheck the applicable current authority; a historical receipt is not a renewed grant.
When STOP has a bound managed target but its managed object has no retained handoff, it revokes work
authority and records settlement as unknown. A missing in-memory handoff may reflect process loss; it
does not establish that native work was never started or has physically settled.

The descriptor is a bearer capability. Do not put it in URLs, browser storage, logs or diagnostics, or
pass it to workers; child code must prevent further descriptor inheritance. The descriptor number is not
a secret on its own, but command-line arguments and process metadata can expose startup details. Never
pass credentials in arguments; the host must not inherit ambient credentials into the backend environment.
There is no on-disk token or network transport. Socketpair inheritance protects against ordinary accidental
endpoint discovery, not same-user inspection, descriptor theft, replay by a holder, compromised children,
or a misconfigured supervisor launching the wrong executable. Browser input and login do not supply the
host-bound principal, approval, proposal or target. Separate observer and worker session interfaces remain
unchanged; this launch SDK provisions neither and cannot elevate them.
Synthetic launch tests check that an unrelated inheritable descriptor and an omitted ambient environment
value do not reach the backend, and that the received descriptor is non-inheritable across an ordinary
worker exec. A trusted backend can still deliberately duplicate or explicitly pass its capability.

Use the exact original command ID and inputs after a lost reply. Stored receipts are historical and
per-Program snapshot coverage is bounded as described above; a missing receipt is not proof of nonexecution.
An existing intent without a receipt remains unknown and must be reconciled against the original operation
and producer evidence, not retried blindly. The existing per-command conflict and revision checks apply;
no global exactly-once or physical-settlement guarantee follows. Snapshot positions and gaps remain
producer-scoped and restart requires snapshot resynchronization.

The repository's installed-wheel pytest phase includes `test_served_host.py` and the ordinary-program
supervisor receipt test. These exercise synthetic separate child processes and the installed producer,
including active channel closure, more than 64 refused replacements, producer-process death and event-gap
resync, wrong scope/version/configured binding, concurrent duplicate approvals, dropped reply and a missing
approval receipt. A separate synthetic ordinary-producer process test replays the original approval
receipt and preserves a missing receipt as unknown after the original producer exits and a fresh producer
reopens the store. Separate forced exits occur after the approval receipt commit and after another
approval's intent commit, before their replies and first replay; the restarted test compares exact
recorded command/receipt evidence and history/operations. It does not cover every command or crash point. This is
not a consumer-owned Console conformance receipt or a live/hostile-local security proof. In particular,
the test evidence does not cover every owner command across process or Kernel crash boundaries.

## Private Outcome Export

`export_private_outcome` reads one owner-thread Program cut from the existing durable store using the
explicit `private-metadata-v1` policy. It exports a versioned local-only projection of Program/spec,
WorkUnit, Attempt, claim, original Operation and delivery observations, exact-subject check and review
evidence, satisfaction references, predecessor and remediation edges, and source-qualified usage
observations including correction links. Recorded check/review freshness is not reevaluated during
export, and a recorded PASS or APPROVE is conditional evidence, not causal model quality. Its references
are HMAC pseudonyms; the default fresh key is discarded, so links remain valid only within that export.
An owner may provide the same separately held 32-byte key when cross-export correlation is intended.
The key is never put in the document; pseudonymization is not anonymization or sharing permission.

Only enumerated non-content metric and unit labels are emitted verbatim. Other labels are represented
by pseudonymous references with null labels, not guessed or silently changed. Reported, measured,
estimated and owner-record provenance remain distinct. Known subtotals are separated by exact
source/metric/unit/provenance; cross-source and complete totals, hidden external work, route/call identity and physical
settlement remain unknown where no producer supplies them. Original source contents, prompts,
free-form findings, workspace paths, native handles and raw identifiers are not emitted. Source rights
are not established by provenance; no source content, upload, MI intake, third-party consent or Router
learning authority is granted by local creation. Unsupported owner policies and invalid required
producer records are refused; an unverifiable review receipt has no inferred verdict. A changing
durable cut is refused rather than mixed.

`command_results` contains one source-qualified historical record per reserved ordinary or bounded-task command operation, including
its pseudonymous exact subject and owner, fence, typed result fields and independently qualified
settlement witness. Missing or inconsistent result, owner, integrity or witness remains unknown;
command completion is not a verification decision. Raw command arguments and output are excluded.
Retained aggregate command Evidence is projected in `checks` and deduplicated with manifest evidence.
`domain_cancellations` preserves WorkUnit and Attempt decision records separately from driver
`cancellations`; their physical settlement remains unknown. `program_conclusions` preserves
historical terminal observations across amendments, with original specification and record order.

The projection retains the original deadline/boot projection, recorded work revocation, exact managed
cancellation intent and any qualified original-target settlement evidence without asserting the current
physical process state. A completed Program can retain its completion while remaining authority is
revoked. Ordinary native publication and the reference PR driver retain pseudonymous original
issue/repository, operation, acceptance and receipt lineage when producer records exist; a retained
receipt is historical, not current Forge state. Finding IDs and fingerprints are pseudonymized and
linked to the original review, rejection and remediation Attempt; finding prose is omitted.
The bounded-task `task-pr-v1` publication uses its own original task/Program binding and the same
historical-receipt rule. When an approving review is followed by an expired prerequisite, its approval
is retained as historical review evidence without claiming the rejected candidate was accepted.
An original bounded-task cancellation recorded before any Attempt is represented separately as
pre-Attempt evidence; an absent runtime Operation does not fabricate an Attempt, runtime handle or
proof about escaped descendants.
Original candidate metadata, terminal candidate dispositions, standalone check results and retained
verification manifests remain visible even if the controller stopped before accepting or rejecting the
Attempt. A recorded missing check is unknown, not a passing result; overlapping standalone and
manifest evidence is deduplicated while retaining both references. Candidate patch contents are never
read into the export: their retained digest is reported as a recorded reference, not fresh content
verification. The bounded-task candidate preserves its original task, base, tree and head from the
task-bound journal. If its output artifact is not yet recorded, the candidate digest remains unknown;
when it is recorded, the exported digest is reconstructed from that metadata and does not by itself
prove the runtime candidate matched it. Independent reference and bounded-task cancellation intents and their original-handle,
fence, transport, observation and owned-group settlement records remain historical evidence, including
uncertainty and mismatched/unqualified observations; they are not redispatched or adopted as a current
process or proof about escaped descendants.

`write_private_outcome` publishes only this policy's result into an already-open owner-owned private
directory using a new 0600 file and never replaces an existing name. A failure before publication may
leave only a private temporary orphan; a failure after publication is uncertain and requires inspection,
not automatic retry or deletion. It is local cooperative custody, not a hostile-same-user sandbox.

## Current Boundary Evidence

This section's original cut is historical; #54/PR92 and #90/PR93 now provide bounded durable product
review and cancellation-settlement machinery, #101 utilized-reference retention and #102 synthetic
platform/profile safety. The [current evidence table](target.md#current-implementation-cut) names
what remains missing. Normal completion and external pull quiescence still require their own evidence.

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

## Independent Provider Contracts

The implemented #105 family ports are `WorkManagement`, `SourceManagement`, `ContinuousIntegration`
and `Knowledge` in `ports.drivers`. `ProjectDrivers.observe` has real family consumers and returns
attributed native evidence; it does not choose work, translate external state into satisfaction or
implement #106. `DriverApplication` publishes only already owner-authorized accepted work through
the retained outbox and ownership machinery. Each concrete adapter explicitly validates its supported
implementation/API major and independent binding before requests. Core remains offline/stdlib-only.

| Profile | Actual interface and rights | Native guarantee and refusal |
| --- | --- | --- |
| `jira-cloud`, implementation 1, Cloud REST 3 | `GET /rest/api/3/issue/{issueIdOrKey}` with explicit summary/description/updated/status/project/issuelinks/parent. Browse Projects and applicable issue security; classic `read:jira-work` or documented granular equivalents; approved Basic/OAuth or public read as supported by the native account | Opaque issue key/ID, native status and directed typed links/parent retained; linked issues independently read for exact project/revision. Two identical bounded reads, not a native snapshot. Visible links are **never** complete absence proof; an explicit trusted owner exact-edge/freshness obligation is mandatory for readiness consumers. Missing fields, inaccessible target, cap, changed scan, 404/permission or time failure remain unknown. Conditional edit/transition unsupported: updated strings are not server-enforced CAS. |
| `github`, implementation 1, REST `2026-03-10` | `/repos/{owner}/{repo}`, `/git/ref/heads/{ref}`, `/git/commits/{sha}`, `/pulls/{number}`, `/pulls/{number}/reviews`. Read repository metadata/Contents/Pull requests as applicable; public reads can be anonymous. Review append needs native Pull requests **write** for the configured writer, not merge/admin | Exact namespace/ref/commit/change base+head and native review actor/state/commit. Bounded numbered pages continue to an empty page even if server caps below requested page size; duplicate/moving/missing pages or changed PR cut refuse completeness. Explicit `commit_id`, `event=COMMENT` creates an append bound to that commit; it **does not CAS current PR head**. PR create/PATCH lack a documented expected-head precondition and are refused; merge is not exposed. No hidden REST retry or GitHub AGit claim. |
| `jenkins`, implementation 1, `jenkins-2.479.3;git-5.7.0;git-client-6.1.0` | `GET /job/{folder}/job/{job}/{build}/api/json?depth=5`. Native Overall/Read + Job/Read and access to the exact build; approved Basic header/API token or authorized public read | Runtime `X-Jenkins` must match the declared core profile. The trusted host explicitly maps the exact Git remote to the complete SCM binding and supplies a mapping evidence reference; this is a host attestation, not independent repository-identity proof. The fact retains the mapping digest. The numbered build's `BuildData` must identify that exact remote and revision and include a `buildsByBranchName` entry binding that revision to the same build number. Missing or inherited evidence is unavailable. Parameters, job names, latest-build aliases, different remotes/bindings and ambiguous multi-SCM evidence are not checkout proof. Plugin profile is explicit owner configuration evidence, **not independently live-attested** by a build response. Trigger/cancel unsupported. No SUCCESS-to-Kernel-acceptance conversion. |
| `confluence-cloud`, implementation 1, Cloud REST 2 | `GET /wiki/api/v2/pages/{id}?body-format=storage&version={number}`, with repeated current-visible page read; native `read:page:confluence` and space/page visibility. `PUT /wiki/api/v2/pages/{id}` requires `write:page:confluence` and page update rights | Exact space/page/version/storage bytes and configured data rights. Withdrawn/moving/inaccessible page, missing version/body and 404 are not content absence. PUT uses the **server-required incremented `version.number`**, never an ignored If-Match header/read-then-write CAS. The provider may merge into or overwrite an unpublished concurrent draft: a separate host `DriverDraftGrant` bound to the exact claim, mutation and OperationKey explicitly authorizes that risk. It expires and can be revoked independently at the final durable cut. Without it, new sends refuse. A read cannot protect against a concurrent draft. 409 is stale; unqualified errors are unknown. Response plus visible versioned marker/actor/content proves a receipt; valid-but-not-yet-visible response is unknown. No blind PUT retry. |
| `repository-docs`, implementation 1, `git-object-v1` | Owner-prepared local `GitContentSource` bound to explicit repository origin/namespace, exact commit and literal regular-blob path; operator owns use/access/data rights | Bounded UTF-8 Git object bytes and SHA-256 provenance, no ambient Git credentials/config, lazy fetch or working-tree fallback. Missing/binary/symlink/unsafe paths are unavailable. This is independently useful knowledge, not a Confluence fallback, source acquisition or a second cache owner. |
| `forgejo-facade`, implementation 1, API profile 1 | Explicit family views over actual `ForgejoForge` read/effect methods | Legacy numeric intake, Reference and AGit bytes/conditional Git/marker/reconciliation unchanged. `apply_legacy`/`reconcile_legacy` retain the original exact Effect and trusted authorizer. No synthesized native review enumeration or universal remote commit reader: those unsupported facade operations explicitly report unavailable. |

All HTTP profiles require configured canonical HTTPS origins and exact namespace prefixes, reject
redirects/cross-origin URLs and path escapes, bound response/request bytes and each total request
deadline, and retain the existing main-thread/no-nested-timer restriction. They neither read ambient
credentials nor infer another family's host, namespace, credential scope or trust. Read and write
Authorization callbacks are separate. No token is needed merely to construct/read a public binding;
no mutation credential is resolved on missing expected revision or unsupported action. Confluence
and GitHub effects include the expected native actor in immutable inputs and check the native actor
on receipts/reconciliation; a matching public marker written by another actor is not a receipt.
Review `commit_id` and CI checkout SHAs identify **related SCM commits**, not revisions of mutable
review/build/status resources. Those resources explicitly have unavailable native revisions;
`DriverFact.observation_digest` protects observed bytes without inventing a native version/CAS token.

An Operation request uses `kernel-driver-effect-v1`, the full driver-qualified target/revision, exact
canonical input fields, owner authority reference, original Attempt/claim/generation. The original
typed publication approval and AUX budget reservation precede dispatch. The final native-send check
combines current work/claim/candidate/spec/body/stop/budget/clock gates with the actual committed
outbox fence/request/attempt/lease; a worker-supplied fence or callback Boolean is not delivery proof.
The filesystem/credential/authorizer waits run outside business SQL before the final durable cut.
The host authorizer resolves exactly once before dispatch into local `DriverEffectGrant | None`.
This immutable, nonserialized value binds the exact WorkClaim and DriverMutation, a positive bounded
integer policy revision, absolute `expires_at` and an exact standard-library Event (not an overridable
flag callback). The host permanently sets the Event on revoke or policy replacement and issues a new
grant/revision/handle; it must never clear the old Event. Worker input cannot supply this token.
The existing final store delivery check returns the successful transaction's already captured wall-time
seconds after all physical preflight and independent durable clock/authority/fence checks. Immediately
after that cut only immutable expiry comparison (`decision_time < expires_at`) and `Event.is_set()`
are evaluated. No policy/validation callback, additional clock read, network or filesystem/store
preflight follows before the HTTP opener. A blocking/reentrant authorizer therefore runs before the
fresh store gate, never after it; scope mismatch/denial refuses before dispatch, while expiry/revoke
during later reads/capture preserves conservative uncertainty without sending. This is a trusted-host
grant contract, not a new persisted capability registry, worker authority or remote atomicity claim.
Historical receipt replay is read-only evidence, never renewal. Retry deliveries only reconcile; even
complete empty scans, rate limits and native eventual indexing do not authorize a second append.
Each driver's outcome remains independently visible in the existing journal. No cross-driver atomicity,
compensation, distributed transaction or new authority source is claimed.

### Provider Evidence And Rights

Inspected 2026-10-28 before implementation, using official public documentation and tagged public
source. REST documentation is a dated contract observation, not a vendored immutable implementation
or live account receipt. The named **live provider/profile reception gate** must verify deployed
versions, expected principal, effective permissions, data rights, conditional conflicts and actual
receipts before a deployment claims reception; it is not a global prerequisite for these implemented
adapters or for unrelated Kernel work.

- [Jira Cloud v3 get issue, fields and permissions](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get).
- [GitHub API versions](https://docs.github.com/en/rest/about-the-rest-api/api-versions), [pull request reads/create/update](https://docs.github.com/en/rest/pulls/pulls) and [review reads/create, commit_id and permissions](https://docs.github.com/en/rest/pulls/reviews).
- [Jenkins Remote Access API, tree and authentication](https://www.jenkins.io/doc/book/using/remote-access-api/); exact core tag [jenkins-2.479.3 Run exports](https://github.com/jenkinsci/jenkins/blob/jenkins-2.479.3/core/src/main/java/hudson/model/Run.java), plugin tag [git-5.7.0 BuildData exports](https://github.com/jenkinsci/git-plugin/blob/git-5.7.0/src/main/java/hudson/plugins/git/util/BuildData.java), client tag [git-client-6.1.0 Revision SHA1 export](https://github.com/jenkinsci/git-client-plugin/blob/git-client-6.1.0/src/main/java/hudson/plugins/git/Revision.java). These are export-shape pins, not new installed dependencies or endorsement of an old service release.
- Anonymous read-only public Git tag resolution pinned those source revisions respectively to `ff61e3756a7e7c301c28a5a042f965ebcc6a90cc`, `184036c37732c4c87eb28a45de96025937dbfe0f`, and `673e5a8c4df7dcae1efc92b504b0d92ae87d2a48`. The MIT [core licence](https://github.com/jenkinsci/jenkins/blob/ff61e3756a7e7c301c28a5a042f965ebcc6a90cc/LICENSE.txt) and file-specific MIT headers were inspected; no implementation was copied.
- [Confluence Cloud v2 page read/update, version and permissions](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/); official public [CONFCLOUD-76640](https://jira.atlassian.com/browse/CONFCLOUD-76640) records server version-increment refusal; [native eventual version visibility](https://community.developer.atlassian.com/t/lag-in-updating-page-version-number-v2-confluence-rest-api/68821) is why response/empty read alone never establishes committed state/nonexecution.
- [GitHub API terms](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#h-api-terms) and [Atlassian Developer Terms](https://developer.atlassian.com/platform/marketplace/atlassian-developer-terms/) require authorized account/data use and respect for rate limits; public access is not redistribution/commercial data permission. Native Jenkins source is MIT and was inspected for protocol evidence only. No SDK, vendor implementation, sample payload or third-party code was copied; all fixtures are synthetic. Existing local transport mechanisms are reused; no new dependency or NOTICE attribution is needed.

## Local Work And Resource Ownership

The concrete #59 handler is `ports.ownership.WorkOwnership`, composed by the trusted local host with
an authenticated principal, a `LocalOwnedWorkspace`, explicit scopes/mode/profile and any native witness
producer. No request-body Principal, independent SQL reader, remote authentication channel or scheduler
is introduced. The same handler serves cooperative pull and managed handoffs; the ordinary dispatcher
and remote entrance remain #106/#57.

Claim acceptance atomically combines current domain preparation/eligibility, exact spec and actor,
original work-clock authority, project/work capacity, Operation intent, claimant, resource conflicts
and increasing generation. These immutable journal records are not outbox delivery claims. Duplicate
command/explicit request returns its historical result before filesystem capture or live profile checks;
the first selected baseline is retained observation, not request input. Changed claimant, revision,
Attempt, mode, profile, resources or lease refuses even after exact retirement. Reading the original
result does not renew a lease, allocate a generation or restore authority. Original wall/boot
projection, pending marker and committed high-water evidence are reused for reports/renewal/delivery,
never reprojected. Expiry/revoke/heartbeat loss fences authority but does not release unsettled resources.

Common store preparation, known-Attempt reservation, candidate/acceptance and delivery paths enforce
canonical absolute physical workspace conflicts across all Programs, not only callers with a work claim.
WorkUnit identity remains Program-scoped; ancestor/descendant workspace scopes remain global. Ambiguous
absolute paths or symlink aliases refuse without normalization, while legacy opaque workspace keys stay uninterpreted.
Unsettled generic work with a known physical subject blocks silent resource adoption and competing
generic work; applicable trusted settlement, not cancellation/expiry alone, permits later ownership.

Potentially blocking physical-path and ownership-artifact preflight completes outside business SQL;
the existing independent pending/high-water capture then takes a fresh original wall/boot sample.
Business guards consume only decision-local physical facts and staged digests, never resolving paths
or validating artifact storage after that cut. Changed preflight paths/store state refuse without
erasing committed cancellations or clock evidence; staged orphan bytes confer no authority. Controller
custody and cooperative namespace stability remain required, not a new hostile-filesystem guarantee.

Logical capacity completion is not physical/task-scope resource proof. Acceptance and
`record_work_completion` record the former; the trusted-profile settlement entry and exact owned
`HandoffProof` establish only their applicable operation/resource scope. Typed `ownership-settlement`
records distinguish the basis and bind original reservation/Attempt/workspace/request/fence identity.
No operation status, arbitrary evidence-string contents or historical untyped row is promoted to
quiescence. Old untyped records remain intact and require a qualified producer reconciliation. A
closed operation cannot restart by reusing its old proof, and another effect/fence cannot borrow it.

Fresh acceptance for a generic canonical physical subject requires its exact runtime and all actual
effect settlements. Logical `FinishAttempt` may precede verification, but terminal/accepted history alone
does not authorize fresh acceptance or publication. Publication intent/dispatch may exclude only their
actual current AUXILIARY operation, with other real effects still required. Old accepted facts remain
historical evidence, not automatic new publication authority.

An owned publication's original owner decision is a separate typed receipt, atomically committed
with the exact AUX reservation/Operation/binding. Historical lookup validates the full original
claim and owner, accepted candidate and immutable op/effect/request before any capture/live checks;
it returns current Operation status without renewing authority or consuming capacity. No untyped AUX
state implies that receipt. Changed input refuses; fresh authorization and delivery still require
current accepted subject, original clock authority and all applicable other effect settlements.

Qualified closed generic subjects are omitted before historical path dereference and preflight work;
their changed old names cannot globally gate independent resources. Current decision targets remain
explicit and must pass canonical physical preflight even when already closed. Live/unknown effects
are not filtered by capacity rows, terminal status, or pathname changes.

RETRY and REMEDIATION are count-only reservations, not external effects. They remain consumed and
deduplicated without fabricated receipts; actual runtime/tool/helper/verification/review/publication
effects retain their separate settlement requirements. All retry reservation paths, including
interrupted `BudgetedChecks` recovery, use the same outside-SQL preflight/final durable clock cut.

Pull `kernel:cooperative-task-scope:v1` receipts are authenticated task-scope promises only. Managed
`CodexOwnedSessionWitness` uses the original dedicated `CodexStdio`, exact opaque group, version-qualified
profile and exact task resources, with a start witness required before normal/STOP settlement. A missing
owner, unsupported profile, reported escape or cold PID is unknown, not a completed process. The handler
never signals an owner Kilo server and does not upgrade ADR 0011 or the test-child profile.

Every candidate/acceptance guard and protected publication compares the claim generation and exact
captured subject. Filesystem changes retain bounded original bytes and unknown attribution until explicit
owner reconciliation; no manual change is silently model output. `candidate_subject` is the fresh guard
for an existing trusted verification/acceptance producer, not a verification verdict. Owned effects use
`dispatch` for a fresh subject plus the existing outbox fence; receipt/reconciliation is still separate.

Workspace ownership records and capacity-authority binding live in a protected controller parent, outside
the mutable leaf. Source-cache use leases do not confer mutation ownership. Cleanup requires applicable
task settlement and reconciliation of every reserved effect; no bare lease or leader PID qualifies.
Only empty exact owned leaves can be moved into custody and removed nonrecursively. Identity replacement,
late writes, nonempty/unknown data and artifacts are preserved. An exact durable post-rmdir witness can
complete an interrupted SQLite release; absence alone cannot. Explicit owner release can instead retain
settled, reconciled bytes and admit later nonconflicting ownership with a higher generation.

## Cross-Product Contract Obligations

These are **agreed semantic requirements and to-prove contracts**, not approved new wire shapes,
endpoints, flags or versions. Each producer owns its schema and executable producer-consumer tests.

- Separate harness control from model-backend ingress; validate exact harness/adapter/protocol/source
  versions and tool/stream/retry semantics. ACP client file/terminal execution needs its own proved
  permission boundary. Unsupported resume/recovery stays unknown or refused.
- For a separately evidenced route-enabled profile, persist Router's executable route before Attempt dispatch; keep main calls sticky, explicit user
  pin and separately authorized helper/review route evidence. Preserve provider/model/resource/access
  mode/explicit effort/opaque variant/harness versions and requested/resolved/observed gaps. No
  physical identity inference for plan-managed execution or effort inferred from variant.
  This conditional contract is not a mandatory ordinary-work or #60 initial-acceptance capability;
  #52 was retired wontfix without implementation. Unsupported or unknown strict profiles must refuse.
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
