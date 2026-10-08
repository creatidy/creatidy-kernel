# Current Creatidy Mapping

Current direction: [ADR 0007](../adr/0007-shared-harness-routing-observability.md) and
[registered coverage](successor.md). The dated second-pass mappings below are history, not current
rollout instructions or authorization to move code. No private source,
secrets, operational payloads or customer data are incorporated in this public foundation.

## Current Public Transition

Kernel `eb4f4a2956712bfaf39a3271e1523e7f77a91e26` integrates public source acquisition, generic
profiles/optional SOPS, Codex discovery, user-local defaults and doctor/preflight/status/export
(#44/PR #45). Private onprem is optional machine deployment, not the normal product operator owner.
The old coordinated private rollout/#34 gate is superseded; original history remains in Forgejo.

| Existing mechanism | Preserve | Required transition / registered receipt |
| --- | --- | --- |
| Immutable domain/Attempt/Operation/artifact/Allocation state | IDs, original byte/digest provenance, unknowns, journal/receipt/reconciliation and no recovery reselection | Each changed codec must retain old records; [#62](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/62) compatibility/backup/interrupted migration proof |
| Router recommendation then direct Codex/one configured binding | Strict parsing, explicit effort separate from variant, durable-before-dispatch compatibility and missing-resolution refusal | [#51](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/51) approved requirements; [#52](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/52) dynamic executable-route consumer, no false gateway relabelling |
| Frozen #143/#166 TaskSpec examples | Existing immutable task identities and controller-owned authority | [#49](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/49) ordinary intake; no issue/AGENTS/model authorization or automatic adoption of issue edits |
| Canonical cache/preflight and advanced checkout override | No sibling guessing, read-only remote, exact origin/base, source/candidate separation | [#61](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/61) host-qualified keys, owned locking/cleanup, shared dispatch validation and non-destructive old-cache migration |
| Generic dotenv/SOPS profile outside source | Actual ciphertext, ownership/metadata and recovery access, never plaintext logs/files | #62 producer-shaped encrypted fixtures, failed/atomic migration/rollback. Git history cannot restore an uncommitted user profile. Do not read real owner profiles for public proof. |
| Dedicated EXCLUSIVE SQLite | Current topology, synchronization, owner process/thread and immutable facts | [#57](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/57) owner-served projection; no Console SQL or durability downgrade |

Router observed `8d9d4b04bcb23fe19ff702b6209fbcb1537cdf1b` has distinct recommendation,
Python route/admission and Chat Completions gateway contracts; Responses is deferred and generic
effort-from-variant behavior needs producer correction. Its D-063 backend-native ZCode workspace
editing conflicts with shared task ownership: [Router #180](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router/issues/180)
owns explicit authority/migration disposition. Do not silently delete or reinterpret that behavior.
MI `fb7299810fdc4612d4e0559465faef571662c6ef` integrates a synthetic temporal evidence proof,
not publication; MI #13 -> Router #176 -> Kernel #55 is the publication/consumer order. Producer
revisions were corroborated by merge metadata; live deployment and sibling file equality were not proved.

No product migration or cutover is performed by this documentation change. Concrete schemas,
lifetimes, thresholds and platform support await their selected issue/owner decision; Linux-only
native storage enforcement is not an exercised WSL/Windows/macOS installation matrix.

## Ordinary Record Compatibility

The local ordinary preparation API in #49 does not migrate the frozen #143/#166 definitions,
digest payloads, exact-base envelopes, allocation bytes or receipts. The frozen registry/CLI and
legacy SQLite codec remain unchanged. An ordinary task is neither a third frozen task nor an
alias for historical execution; preparing/approving meaning cannot enable `run_task`.

New inert metadata is tagged `ordinary-intake-record` with local version 1 and inner
`ordinary-draft`, `ordinary-owner-decision`, `ordinary-consumption` or `ordinary-baseline-age`
kind/version. It uses the
existing journal/artifact tables, without a database schema migration or another state tracker.
SQLite refuses to claim these metadata entries for delivery. Unsupported/malformed versions
fail on ordinary recovery instead of reinterpreting old payloads. No legacy record is rewritten.
Kind-aware recovery validates namespace and subject bindings. New consumption IDs use
`ordinary:consumed:<task>:record:<ordinal>` while existing four-part consumption IDs retain their
original bytes and meaning. If a legacy consumption already occupies a draft ID, the new draft uses
`ordinary:record:ordinary-draft:<task>:<revision>`; both draft namespaces are read with strict kind
and lineage checks. No controller task ID is banned merely because it resembles a record kind.

Learning a lower observation time for an otherwise unchanged baseline receipt writes an immutable
`ordinary-baseline-age` fact through the same journal/artifact mechanism, not a new draft revision
or renewed decision. These non-dispatchable facts participate in preparation, approval and handoff
after reopen, including interrupted artifact-publication recovery. The original draft, decision
and expiry stay byte-identical; a genuinely new identified receipt retains its own age.

Reopen through `OrdinaryIntake.history(task_id)` and `historical_decision(decision_id)` to inspect
original records. Historical decision bytes retain their original expiry and do not approve
current work. Repeating `approve` must supply the same original decision identity/expiry/policy
and pass fresh owner/revision/source/issue/baseline checks; expired or superseded receipts remain
historical. Interrupted artifact/create/amend recovery is tested with original command identities.
A new current decision can supersede an interrupted expired decision without reviving it.

Operators configure the trusted local reader, evidence producers and policy outside untrusted
declaration JSON. No new profile, server/channel authentication or CLI approval flag is introduced.
The installed smoke exercises actual preparation/approval/refusal/reopen with synthetic data;
it is not live ordinary execution. See [README Ordinary Preparation](../../README.md#ordinary-preparation)
and [Local Intake Reception](contracts.md#local-intake-reception) for the received path and limits.

#51 adds an optional full `RequirementsHandoff` to in-memory ResourceRequest and a version-2
Allocation only for newly translated ordinary recommendations. Its `requirements_provenance` retains
exact approved bytes, draft/revision/evidence/decision/Program, sent request, separate interface and
independent harness facts. Ordinary evidence is private controller state, not a Router wire field.
The original public decision retains catalog, resource-policy and optional profile/version provenance.
Version 2 requires nonempty requirements evidence; malformed/unknown versions fail closed.

No database schema change or automatic migration occurs. Legacy unversioned and version-1 Allocation
records remain readable; encoding an Allocation without ordinary evidence still yields the exact old
version-1 field set/bytes. Original Attempt references, operation request digests, allocation/context
bytes, task #143/#166 digests and L0 decision identities are never rewritten/relabelled. Recovery reads
original immutable evidence and does not call a now-changed/unavailable allocator. Existing textual
ordinary declarations remain historical meaning and explicit refusal, not auto-upgraded typed markers;
new meaning requires the normal explicit draft amendment and fresh owner decision. Installed smoke
receives typed selection via synthetic loopback HTTP and checks exact evidence after reopening.

## Verification Compatibility

Verification changes under #50 preserve the original Task #143/#166 payloads, policy references,
spec/Attempt/candidate digests, allocation and recovery encodings. Core verification now stops before
later producer effects after a failed prerequisite. Historical accepted evidence still has all checks;
new rejected results never manufacture evidence for unrun checks. No database migration is introduced.
`VerificationProfile.LINUX_BUBBLEWRAP` is a new explicitly composed child-test executor, not a new
worker `TrustMode` or profile-file default. Existing trusted-development stays non-isolated and is
never automatically upgraded. See [ADR 0008](../adr/0008-linux-bubblewrap-verification.md).

## Source Cache Compatibility

#61 introduces a separate host-qualified `source/v2/<origin-digest>/<owner>/<repository>` namespace,
not an in-place identity migration. The digest is SHA-256 of the existing Forge canonical HTTPS
origin (lower-case host, port 443 omitted); repository syntax uses the same Forge ref validator.
TaskSpec repository/digest, policy, Program, Attempt, allocation, request/Operation/effect identities,
exact pins and Source provenance subjects are unchanged. No profile or database codec is migrated.

Historical hostless entries are preserved byte-for-byte in their original locations. Kernel does
not rename, quarantine, reclassify, delete or adopt a tree based on cleanliness or a remote URL.
The old `kernel-source-cache-v1` plain `1` marker is not sufficient ownership evidence. A new
qualified acquisition is separate, including if the legacy entry belongs to another user/host or
is corrupt. Failed acquisitions/publications retain their unique staging bytes; they are not
published as usable checkouts. Corrupt qualified entries refuse and remain in place. Operators may
choose a separate supported cache placement; automatic repair/deletion authority is not inferred.

New JSON markers bind root/location, canonical repository, checkout/Git-directory and stable lock
inodes, UID and a per-acquisition token matched to the controller's locked metadata. Lock files are
never deleted or replaced. Interrupted metadata writes refuse and retain all checkout/staging bytes. Metadata or inode
substitution, unsupported mounts, permissions, Git indirection, gitlinks/submodules and executable local configuration
refuse with closed categories. This is a single trusted local controller/cooperative reader-writer
protocol; it does not protect against an adversarial same-UID process or hostile remounts.

Recovery loads the ORIGINAL mode `source` literal path when no override was supplied, not a new
default namespace or a reacquired/re-routed repository. Existing receipt/terminal/cancellation/PR
reconciliation uses its original frozen objects without a new live source/baseline requirement.
A new native start still validates those exact controller objects. Missing original objects refuse;
recovery never rewrites an old Source field, renews the deadline, or substitutes a fresh identity.
New admissions validate source under a use lease and copy without hardlinks, validating the exact
snapshot before dispatch. A returned acquisition Path carries no protected lifetime after release.

## Historical Second-Pass Mapping

## Scarcity Router

Inspected clean local `release/v0.1.0-readiness` at
`8ae1fdb5ea9090604c26fd81f23010ad85a1536d` on 2026-09-21. Sources: `AGENTS.md`,
`docs/machine-interfaces.md`, `scarcity_router/selection_types.py`, `scarcity_router/selector.py`,
`docs/release-engineering.md`, `.forgejo/workflows/ci.yml`, `pyproject.toml` and `Makefile`.
Its current canonical namespace is still `BioMedical-IT/scarcity-router`; A0 does not relocate it.
Kernel's approved namespace is `Creatidy/creatidy-kernel` regardless of that historical location.

| Element | Classification | Mapping and cost |
| --- | --- | --- |
| Selector, capability profiles, capacity collection, provider/subscription policy | KEEP AS INDEPENDENT PRODUCT | Never migrate or duplicate in Kernel. Public adapter integration is small; mapping/contract fixtures need deliberate validation. |
| REST `/v1/select` and MCP `scarcity_select` | REUSE AS-IS through ResourceAllocator adapter later | Preserve versioned envelope and full decision provenance. `profile_id XOR requirement`; tightening only with profile. HTTP 200 with `selected=null` is valid no-solution, not a selection. |
| Runtime choice / reservations | REFACTOR BEHIND PORT / DEFER | Current recommendation returns model/provider/variant evaluation, not an arbitrary harness reservation. Adapter selects a configured compatible runtime and fails closed when unsupported. Do not fabricate a reservation or reduce diagnostics to a model-name string. |
| Optional execution gateway and native workers | KEEP AS INDEPENDENT PRODUCT | Do not pull gateway, credential store or worker protocol into Kernel. A future runtime can use a supported endpoint; independent recommendation mode remains valid. |
| Forgejo CI, mirror and release authority split | BORROW DESIGN | Reuse `ci`/`check`, develop PR validation, pinned actions, no configured private secrets in PR workers, exact-artifact release discipline. Forgejo ignores the upstream GitHub-style permissions declaration; do not inherit its read-only-token assurance. Do not copy the Windows/OCI/PyPI release pipeline into an empty foundation. |

The audit observed identical Forgejo/GitHub heads for `develop` (`9d6192eda987`), `main`
(`0d097e843def`) and the audited release branch (`8ae1fdb5ea90`). This verifies branch parity at
that instant, not the mirror scheduler, credential scope or future delivery. The upstream release
document explicitly marks several activation steps external; a workflow file is not proof that
release infrastructure is enabled. Its GitHub issues/wiki are still enabled, so A0 adopts the
authority convention but improves mirror signposting rather than reproducing that ambiguity.

No source copied. The new development workflow uses the same established action pins and authority
model; its small gate is independently written for this package.

## Private Composition And Deployment

Inspected clean local snapshots, not deployed versions: `creatidy-autonomy`
`872a6e55be691e8d2da353f9af2e6325722a5dbb`, `creatidy-onprem`
`038dfaf89c39078eba9a99f1c4d30eb5ec1357f1`, `creatidy-docs`
`9462c1b10d41b6309af5a48383de9e9f09dfde86`. Workspace Program rules were read at
`84831cc863790b8ce9c51c67b65a2a42cda0726d`; existing unrelated workspace/submodule state was untouched.
Private paths below identify audited seams without reproducing private source or operational data.

| Element / evidence | Classification | Migration cost and constraint |
| --- | --- | --- |
| Autonomy `tasks/service.py`, `tasks/store.py`, `flows/task_queue_dispatch.py` | KEEP PRIVATE / DO NOT MIGRATE YET | M5-A is a bounded task service, not a Program graph. Provider success is task completion, not evidence-based acceptance. Semantic replacement is large; no wholesale extraction. |
| Autonomy `pr_review/controller.py`, `flows/pr_review_runtime.py`, `pr_review/trigger.py` | KEEP PRIVATE ADAPTER | M5-B is an independent review workflow. Preserve standalone usage and trusted publication. Medium effort for a bounded evidence/receipt adapter; not a mandatory Kernel service. |
| Autonomy `pr_review/marker.py`, `pr_review/publisher.py`, `tests/test_pr_recovery_wakeup.py` | REUSE AS-IS inside the private adapter | Exact-comment redispatch and matching publication adoption are useful. Small wrapper/fixture work; do not copy private implementation into OSS. |
| Autonomy `providers/base.py`, `providers/openai.py`, `execution.py` | REFACTOR BEHIND PORT | Synchronous execute/readiness is not start/observe/cancel/recover. Medium-to-large effort to establish durable Runtime semantics and isolated coding workspaces. |
| Autonomy `pr_review/forgejo.py`, `routing.py` | REFACTOR BEHIND PORT | Preserve narrow translation clients. Existing Forgejo client has no merge capability; conditional integration is new work. Medium conformance effort. |
| Autonomy `policy.py`, `providers/registry.py`, `pr_review/config.py` | KEEP PRIVATE ADAPTER | Subscription/workload rules and deployment allowlists are composition policy, not universal Kernel law. Small-to-medium separation effort. |
| Autonomy `review/context_pack.py`, `pr_review/snapshot.py`, `pr_review/parser.py` | MOVE RESPONSIBILITY EVENTUALLY TO KERNEL | Generalize source manifests, redaction and exact-subject evidence; retain review-specific parsers privately. Medium-to-large effort, no code move now. |
| Workspace `.kilo/rules/60-program-execution.md`, `.kilo/command/execute-program.md` | MOVE RESPONSIBILITY EVENTUALLY; DEPRECATE prose ownership AFTER PARITY | Graph legality, dispatch, waiting and authority belong in code. Current mandatory M5-B/Prefect path and one-remediation escalation conflict with the target. Large, incremental replacement; keep working safety controls until tested parity. |
| Onprem `deployments/autonomy/compose.yml`, `scripts/validate-autonomy-env.py`, `scripts/autonomy-pr-review-webhooks.py` | KEEP IN DEPLOYMENT | Correct owner for containers, networks, volumes, secrets wiring and health. No Program semantics migrate here. Optional later deployment contract is small-to-medium; no deployment in A0. |
| Independent repository review/watch, active task/review DBs, workload repositories | DO NOT MIGRATE YET | Existing non-Program work remains useful. No shared database, bulk import, active Program conversion or product runtime dependency. |

Cost bands are relative engineering estimates, not delivery commitments. The largest work is proving
new semantics/recovery, not moving files. Reuse working private adapters without weakening the target.

## Concrete Gaps To Avoid Inheriting

- M5-B persistence imports the M5-A task schema/version and shares its database. The task store uses
  mutable records and `synchronous=NORMAL`, not the target journal/outbox durability contract.
- Invalid review markers can return a normal flow result before durable request ingestion. The existing
  check command interprets an absent formal review as pending. Transport/Prefect success therefore
  cannot establish domain acceptance, directly relevant to D/E.
- Static inspection found parameterless crash recovery selecting the oldest global recovery candidate
  while concurrent watch executions are permitted. An unrelated healthy request could be terminalized.
  This was not reproduced live and is not claimed to be the cause of the historical incident. Do not
  reuse this as generic Kernel recovery; correlate recovery to exact execution identity and lease.
- Selected reviewer provenance is not necessarily observed execution identity or usage. Finding text
  and locations are not yet stable cross-remediation identities. Preserve unknowns and lineage.
- The private deployment's shared worker/controller environment is not evidence of hostile coding-worker
  isolation. A read-only child environment and filtered variables do not prove denial of all host state.

These are audit findings, not changes to the private runtime. Upstream/private tests were inspected,
not executed during this mapping.

## Historical Engine v1 Brief

Located and read `Infrastructure/creatidy-autonomy#53`, titled "[ProgramEngine] Program Execution
Engine v1 - deterministic automation-first orchestration" (open, unassigned, last updated 2026-09-20
at audit). No Program implementation module existed in the inspected source tree.

Its P0-P9 proposal runs from audit, domain/FSM and persistence through private execution, M5-B review,
REST/CLI/MCP, recovery, onprem contract, synthetic tests and a new DemandTrace pilot. It correctly
preserves standalone M5-B, exact-comment recovery and the prohibition on rerunning Program #62.
Its private-repository-first placement and immediate three-interface/pilot scope are superseded in
this foundation by [the bounded Kernel successor](successor.md). Reading the brief is not permission
to execute it, mutate live Programs or cut over any runtime. A0 makes no such migration.
