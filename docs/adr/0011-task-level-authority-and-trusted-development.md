# ADR 0011: Proposed Task-Level Authority And Trusted-Development Contract

- Status: Proposed; not authoritative until explicit owner acceptance
- Date: 2026-10-16

## Purpose And Decision Boundary

This proposal aligns Kernel acceptance with the product's purpose: reliable
orchestration of AI coding agents with bounded controller work, user-requested STOP,
durable recovery, controlled external effects, and honest failure reporting. It
proposes a narrower interpretation of AC4 for the existing trusted-development Codex
path. It does not change the contract by itself.

Issue #53 remains **CLOSED / NOT IMPLEMENTED**, PR #85 remains **CLOSED / UNMERGED**,
and their branch evidence is preserved. This ADR does not merge, reopen, or reclassify
that work. The #53 post-expiry regression remains positive evidence of a strict native
expiry failure, not an acceptance pass.

## Existing Requirement And Evidence

Issue #53 AC4 currently states: “File/terminal operations execute only through
identified constrained executor; denied/expired/revoked permission cannot expand
authority. Cold/native review remains independent and candidate-specific.” The
current architecture therefore requires the native effect boundary to enforce
expiry/revocation on file and terminal effects, not merely on Kernel dispatch.

The controlled #53 regression observed a native descendant start a subprocess and
create a workspace file more than one second after the persisted grant expiry while
PID-1 had not yet observed its deadline. Receipt:
`/tmp/kilo/runtime-conformance-dyg_9gb4/grant-expiry-race-receipt.json`, SHA-256
`efe32596a60bb98546da62a90ecdd48d4d41e624739b71d8a5d6d5685106d6bd`. It used a
diagnostic-only Podman candidate, not an accepted production/native toolchain. The
positive result refutes strict native expiration for the current mechanism; it does
not prove that a revised baseline is accepted.

Current `CodexRuntime` checks authority before Runtime thread/turn dispatch. After
dispatch, the native Codex process and terminal descendants can perform filesystem
and subprocess operations without a Kernel check on each syscall. Deadline expiry
and STOP can trigger cancellation/termination, but they do not synchronously revoke
those process capabilities. The current CLI uses `workspace-write` and explicit
`TRUSTED_DEVELOPMENT`; that profile is not a hostile-worker sandbox. Issue #90 tracks
truthful STOP and owned process-group settlement.

## Proposed AC4 Wording

> **AC4 — Task authority, bounded STOP, external effects, and honest recovery
> (proposed).** For every new Kernel-controlled dispatch or external state-changing
> operation, Kernel MUST bind the operation to the exact task, Attempt, Operation,
> resource scope, current revocation state, and original task deadline. Kernel MUST
> check authority before dispatch, durably record operation intent before invoking an
> external effect, and never silently renew, reroute, or replay an uncertain
> operation. After the original deadline, revocation, or user STOP, Kernel MUST stop
> admitting new Kernel-controlled work, persist cancellation intent through the
> current single-writer controller, and initiate bounded cancellation/termination
> using the selected supported execution path. Request, cancellation, process
> termination, settlement, and acceptance are distinct states. If the outcome or
> owned-process settlement is uncertain, preserve the workspace/evidence, report
> `unknown`, block candidate acceptance/publication and a successor Attempt, and do not
> blindly retry an external effect. Cancellation is not rollback.
>
> In the explicitly acknowledged `TRUSTED_DEVELOPMENT` path, the user authorizes a
> task-scoped local workspace/process capability at task admission. That capability
> remains present in the active native process tree until the tree is observed
> terminated. The original deadline prevents new Kernel-controlled admissions and
> triggers STOP; it is not an atomic per-syscall revocation. A running worker or
> descendant may initiate or complete local workspace/process effects during
> termination. This is a deliberate, visible residual of this trust profile, not a
> strict-expiry guarantee, rollback, or hostile-worker isolation claim. It does not
> authorize another task, Attempt, target, workspace, or Kernel-controlled external
> effect.
>
> Kernel-owned external changes (including remote Git/Forgejo mutation, publication,
> merge, release, deployment, and protected APIs) require separate exact operation
> authorization and durable identity/reconciliation. Native workers must not receive
> Kernel/controller/production credentials or an unapproved direct external-mutation
> path. The selected Codex/OS profile must be verified to enforce the declared
> workspace and network/credential boundary; if that cannot be established for a
> task, refuse that execution mode/task rather than relying on prompt text or a status
> label.

This explicitly changes the old per-file/terminal-effect interpretation. Until the
owner accepts this wording, the existing strict AC4 remains authoritative. A passing
task-level STOP test or the bounded cgroup cold-recovery proof cannot be reported as
proof that native syscalls stop at the expiry timestamp.

## Deadline Clock And Recovery

The current store persists an absolute wall-clock deadline and Runtime call sites
compare it using `int(time.time())`. That preserves the original timestamp across
ordinary process restart, but does not prove that a backward wall-clock step or host
reboot cannot lengthen the effective lease. The proposed baseline must not silently
renew it.

Recommended contract: when the original Attempt/deadline is committed, also persist a
boot-scoped monotonic deadline projection and its host boot identity, derived from the
original wall deadline. Admission checks use that same monotonic projection after the
existing single-writer serialization point; recovery on the same boot reuses it. If
the boot identity/clock mapping changes or cannot be validated, do not dispatch or
start a new Attempt; report `unknown` and require reconciliation. The wall deadline
remains the user-visible audit value. This is durable metadata in the existing task
store, not a service or replacement executor; its schema/clock cost is part of the
owner decision. Until implementation and clock tests exist, do not claim
rollback-resistant deadlines.

The monotonic mapping protects Kernel admission; it does not eliminate the documented
native termination window. A native effect under an already-running trusted-development
Attempt may still begin after the wall deadline and before that Attempt is settled.

## Trust Model And Supported Scope

- Kernel, its single-writer SQLite owner, AuthorityBroker, operation journal, and
  external-effect adapters own durable admission and external mutation authority.
- Model output, repository instructions, native command output, and descendants do
  not create grants, change the original deadline, retarget a Kernel operation,
  authorize publication, or accept a candidate.
- `TRUSTED_DEVELOPMENT` is a user-trusted local coding mode. It is not suitable for
  hostile code, adversarial PR tests, protected production workspaces, or a
  credential-rich same-user account. The user explicitly trusts the native task to
  modify its declared local workspace during the active task and possible termination
  interval.
- The current child environment is constructed from a small allowlist, but a
  configured `HOME` can expose files readable by the same OS identity. The proposed
  contract therefore requires a credential-clean profile and actual test of
  workspace/network restrictions. If remote-write credentials, host control data, or
  other protected resources are reachable by the worker, refuse the task/profile; do
  not claim those resources are protected by a prompt.
- The task's native local capability ends only when the selected owned process group
  or cgroup has profile-appropriate settlement evidence. If a descendant escapes the
  observable group, the result remains `unknown`; no universal process-tree containment
  is claimed.

## Guarantees Preserved

- Exact task/Attempt/Operation identity, scoped grants, original immutable deadline,
  revocation fences, single-writer SQLite ownership and durable operation intents.
- No new Kernel-controlled dispatch or external mutation after the original
  deadline/revocation. Recovery never renews a deadline, creates a successor Attempt,
  changes the route, or blindly replays an uncertain effect.
- User STOP records a cancellation against the exact active Attempt and invokes the
  existing Runtime interrupt plus bounded process-group/OCI cleanup. An interrupt
  acknowledgement, turn status or leader exit alone is not settlement.
- Candidate acceptance/publication is blocked while the task or its external effects
  are unknown. Partial workspace changes are retained, not relabelled as verified or
  rolled back.
- Kernel-controlled Forgejo/Router/MI effects remain separately authorized and
  reconciled. A task-level native grant does not confer controller API credentials or
  external write authority.
- The verification/test-child profile (#50) remains a separate, narrower use. It is
  not promoted into whole-worker isolation by this proposal.

## Guarantees Deliberately Relinquished

If accepted, the baseline will **not** guarantee that no new native filesystem or
subprocess syscall starts after exact expiry/STOP. A task already admitted may
initiate local effects during the bounded termination interval. This explicitly
changes the authority horizon from the old strict effect-level reading to a
task-level trusted-development capability that ends at observed termination.

The baseline also does not guarantee instantaneous physical termination, rollback,
termination of a descendant that escaped the selected process group/cgroup, zero
provider cost after an active Codex turn is interrupted, or a hard real-time upper
bound while the controller/host is descheduled or a process is uninterruptible. A
bounded controller cancellation protocol is required; if its outcome is not
observable, it remains `cancel_uncertain`/`unknown` and is not accepted.

## Adversarial Evaluation

| Scenario | Proposed baseline response | Remaining risk/containment |
| --- | --- | --- |
| Deadline expires during an active Codex turn | Deny new Kernel-controlled operations, persist expiry/cancel intent, interrupt and terminate the exact owned runtime. | Native local writes or a new subprocess may still occur while termination is in progress; the workspace remains partial/unaccepted until settlement and fresh verification. |
| Controller/host restarts or wall clock moves backward | Reuse the original boot-scoped monotonic deadline; on invalid/mismatched clock identity, deny new dispatch and report `unknown`. | The current wall-clock-only schema does not yet prove rollback-resistant expiry; implementation and clock tests are required. |
| User presses STOP during a framed RPC | Latch the request; handle it on the existing owner thread after the finite RPC boundary; persist cancellation before teardown. | The stop is not instantaneous. Report requested/cancelled/uncertain honestly. |
| Interrupt acknowledgement arrives but the process group remains | Do not report `cancelled` or accept work until the profile's termination evidence is observed. | Retain workspace and mark `unknown`/`cancel_uncertain`; no automatic restart or duplicate external effect. |
| Descendant calls `setsid`/escapes the observable group | Keep the task uncertain; do not claim all descendants have terminated. | This mode is not a hostile-worker sandbox. If the task requires guaranteed containment, refuse it rather than silently widening support. |
| Native command attempts remote write or protected API access | No controller/production credentials are supplied to the worker; the selected sandbox must deny unapproved egress. | If direct credentials/network access cannot be ruled out in the actual profile, refuse tasks needing controlled external effects. |
| Controller loses a remote effect reply | Preserve the exact Operation as unknown and reconcile it; no fallback/retry. | No claim that timeout means the external system did nothing. |
| Native provider turn is still active at deadline | Do not start a new Kernel turn; interrupt the current turn and retain cost/result uncertainty. | A hard per-subcall cutoff or exact cost cap requires producer/Router evidence (#52/#56); it is not claimed here. |

## Codex Compatibility And Unsupported Work

The proposal keeps the existing Codex `workspace-write`/`approvalPolicy=never`
integration and the existing `TRUSTED_DEVELOPMENT` acknowledgement. It does not switch
to `danger-full-access`, add a syscall broker, or replace the Codex executor. Ordinary
local coding remains possible where the selected native sandbox, workspace and
credential-clean environment are actually verified.

The current native shell path must be tested against outside-workspace writes, network
egress, child access to credential sentinels, and subprocess escape. If a supported
Codex version falls back to broader permissions or a credential/egress boundary cannot
be proved, refuse that execution mode/task. Strict no-native-effect-after-expiry,
hostile PR execution, broad owner secrets, production resources, and guaranteed
termination of escaped processes remain unsupported without a separately authorized
enforcement architecture.

## Bounded Acceptance/Falsification Plan

After owner acceptance only, use synthetic local resources and the exact supported
Codex/profile versions. Do not use live inference or production credentials:

1. **Kernel admission:** show that expiry/revocation denies a new Kernel turn,
   controller command and external Operation at their existing admission points.
   Recovery must retain the original deadline and may not generate a new Attempt or
   authority; simulated wall-clock rollback and boot-identity mismatch fail closed.
2. **Native residual:** reproduce a positive local workspace write/subprocess during
   the termination interval under the selected `TRUSTED_DEVELOPMENT` profile. Record
   it as a residual partial effect and prove that it cannot be accepted, published,
   or converted into a new Attempt while termination/verification is outstanding.
   Do not treat the absence of a marker as proof of a strict fence.
3. **STOP:** exercise Ctrl-C before dispatch, during a finite RPC, after an interrupt
   reply, with a live same-group child, and with an unobservable/escaped child. Verify
   cancellation intent, bounded TERM/KILL, exact-target settlement, `cancel_uncertain`
   when unresolved, retained workspace, no duplicate start and honest CLI/export
   output.
4. **Credential/egress refusal:** use synthetic secrets and a fake remote to test the
   actual native profile. If protected credentials or direct external mutation remain
   reachable, refuse that profile/task. Never infer safety from prompt language.
5. **External effect recovery:** lose a reply after an authorized Kernel-controlled
   operation. Prove operation identity, durable unknown/reconciliation, and no blind
   retry or route change.
6. **Deadline/accounting:** test an in-flight native/provider operation at expiry. The
   system may report a residual/unknown, but may not report zero usage, no effect, or
   strict syscall expiration without evidence.

These checks evaluate the proposed task-level contract. They do not reopen #53, repeat
its source/pin investigation, or convert its positive strict-expiry diagnostic into an
acceptance pass.

## Recommendation And Owner Gate

**RECOMMENDATION: REVISE.** A task-level admission/STOP contract can support the
existing trusted-development Codex workflow without a new broker, privileged kernel
subsystem or replacement executor, while preserving Kernel control over external
effects, durable identity, bounded cancellation attempts and honest `unknown` outcomes.
It deliberately relinquishes strict per-syscall expiry and hostile-worker containment
for this profile, and it is supportable only when the credential/workspace/egress
preconditions are true.

The proposal is **not authoritative**. Until explicit owner acceptance, the previous
AC4 remains binding and #53 stays closed not implemented. Acceptance must explicitly
approve the task-level capability horizon, the local native termination residual, the
credential/egress refusal condition, and the no-candidate/no-retry behavior while
termination or external outcomes are unknown. If any of those conditions is rejected,
the trusted-development mode must remain unavailable for the affected task rather than
silently reverting to the strict but unimplemented contract.
