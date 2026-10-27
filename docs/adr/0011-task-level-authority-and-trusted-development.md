# ADR 0011: Task-Level Authority And Trusted-Development Contract

- Status: Accepted
- Date: 2026-10-18

Compatible revision: [ADR 0012](0012-unified-work-coordination-and-drivers.md), integrated in PR #104,
retains this accepted managed trusted-development baseline. Cooperative external pull clients have
explicitly weaker task-scope control and cannot claim this profile's process-settlement guarantee.
No historical #53 evidence, strict-expiry disposition or clock requirement is superseded here.

## Purpose And Decision Boundary

This decision aligns Kernel acceptance with the product's purpose: reliable
orchestration of AI coding agents with bounded controller work, user-requested STOP,
durable recovery, controlled external effects, and honest failure reporting. It
defines the accepted task-level AC4 baseline for the existing trusted-development Codex
path. This ADR is authoritative for that defined profile; it does not itself implement
the required runtime behavior or claim strict native-effect expiry.

Issue #53 remains **CLOSED / NOT IMPLEMENTED**, PR #85 remains **CLOSED / UNMERGED**,
and their branch evidence is preserved. This ADR does not merge, reopen, or reclassify
that work. The #53 post-expiry regression remains positive evidence that strict native
expiry failed for the investigated mechanism, not an acceptance pass or proof of the
accepted task-level baseline's implementation.

## Existing Requirement And Evidence

The closed issue #53 retains its historical AC4 wording:
“File/terminal operations execute only through identified constrained executor;
denied/expired/revoked permission cannot expand authority. Cold/native review remains
independent and candidate-specific.” The
original strict interpretation required the native effect boundary to enforce
expiry/revocation on file and terminal effects, not merely on Kernel dispatch. The owner
accepted the task-level baseline in this ADR on 2026-10-18 for the defined
trusted-development profile. The historical #53 requirement remains unmet and strict
native-effect expiry is explicitly unsupported under the accepted baseline.

The controlled #53 regression observed a native descendant start a subprocess and
create a workspace file more than one second after the persisted grant expiry while
PID-1 had not yet observed its deadline. Receipt:
`/tmp/kilo/runtime-conformance-dyg_9gb4/grant-expiry-race-receipt.json`, SHA-256
`efe32596a60bb98546da62a90ecdd48d4d41e624739b71d8a5d6d5685106d6bd`. It used a
diagnostic-only Podman candidate, not an accepted production/native toolchain. The
positive result refutes strict native expiration for the current mechanism. It remains
diagnostic evidence, not task-level acceptance or proof of a native effect fence.

Current `CodexRuntime` checks authority before Runtime thread/turn dispatch. After
dispatch, the native Codex process and terminal descendants can perform filesystem
and subprocess operations without a Kernel check on each syscall. Deadline expiry
and STOP can trigger cancellation/termination, but they do not synchronously revoke
those process capabilities. The current CLI uses `workspace-write` and explicit
`TRUSTED_DEVELOPMENT`; that profile is not a hostile-worker sandbox. Issue #90 tracks
truthful STOP and owned process-group settlement.

## Accepted AC4 Baseline

> **AC4 — Task authority, bounded STOP, external effects, and honest recovery
> (accepted 2026-10-18).** For every new Kernel-controlled dispatch or external
> state-changing operation, Kernel MUST bind the operation to the exact task, Attempt,
> Operation, resource scope, current revocation state, and original task deadline.
> Kernel MUST check authority before dispatch, durably record operation intent before invoking an
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
> authorization and durable identity/reconciliation. The Kernel does not pass its
> controller API tokens directly to Codex. The Codex app-server requires provider
> authentication from its configured `HOME` and network connectivity. The current task
> environment forwards `HOME`; the repository does not prove that a native terminal
> child cannot read that HOME or use provider auth. Treat the app-server and native tool
> tree as one same-principal trusted task, not isolated secret domains. Ordinary local
> Codex use may continue when the owner/user accepts that trust and uses a limited
> provider account with no production or remote-write credentials available to the
> task. If a task requires provider-key secrecy, protected remote mutation, or verified
> shell egress denial, the current path cannot guarantee it; refuse that sensitive task
> rather than silently refusing all ordinary local coding or claiming Kernel control of
> those effects.

This accepted wording supersedes the old per-file/terminal-effect interpretation only
for the defined trusted-development profile. Strict native-effect expiry remains
unsupported. A passing task-level STOP test or the bounded cgroup cold-recovery proof
cannot be reported as proof that native syscalls stop at the expiry timestamp or as
completion of #53.

## Deadline Clock And Recovery

The current store persists an absolute wall-clock deadline and Runtime call sites
compare it using `int(time.time())`. That preserves the original timestamp across
ordinary process restart, but does not prove that a backward wall-clock step or host
reboot cannot lengthen the effective lease. The accepted baseline must not silently
renew it.

The required projection must continue to elapse across system suspend. On supported
Linux hosts, prefer a verified `CLOCK_BOOTTIME` reading, which includes suspended time;
ordinary `CLOCK_MONOTONIC` is insufficient where it pauses during suspend. When the
original Attempt/deadline is committed, persist the original wall deadline, a
boot-scoped deadline projection derived from the remaining duration, the verified
clock identity, and the host boot identity. Use the existing single-writer
serialization point for that binding.

Same-boot recovery reuses the original projection; it must never recompute a later
expiry from the current wall clock. A wall-clock rollback therefore cannot extend the
persisted deadline, and suspend time counts toward it. If `CLOCK_BOOTTIME`, its reading,
or the host boot identity is unavailable, unsupported, or cannot be verified, or if
recovery observes a different boot identity, fail closed: deny new dispatch and a
successor Attempt, retain `unknown`, and require reconciliation rather than extending
the original deadline. The wall deadline remains the user-visible audit value. This is
durable metadata in the existing task store, not a service or replacement executor.
Schema/clock support and deterministic tests are implementation requirements under this
accepted decision. Until implementation and clock tests exist, do not claim that the
runtime provides rollback-resistant deadlines.

The suspend-inclusive projection protects Kernel admission only. Expiry initiates the
bounded cancellation path, but does not prove process termination or settlement and
does not eliminate the native termination window: an effect under an already-running
trusted-development Attempt may still begin after admission expiry and before that
Attempt is settled. No strict per-syscall cutoff is claimed.

## Trust Model And Supported Scope

- Kernel, its single-writer SQLite owner, AuthorityBroker, operation journal, and
  external-effect adapters own durable admission and external mutation authority.
- Model output, repository instructions, native command output, and descendants do
  not create grants, change the original deadline, retarget a Kernel operation,
  authorize publication, or accept a candidate.
- `TRUSTED_DEVELOPMENT` is a user-trusted local coding mode. It is not suitable for
  hostile code, adversarial PR tests, or protected production workspaces. The user
  explicitly trusts the native task tree with the local files and provider credentials
  reachable to that OS identity during the active task/termination interval. Ordinary
  local Codex remains usable with that explicit same-principal trust; the mode must not
  be described as credential isolation.
- `task_execution.codex_environment` forwards only `HOME`, `PATH`, `LANG`, `LC_ALL` and
  `TMPDIR`, but `HOME` is not a secret-free path: source comments identify it as the
  native Codex authentication/configuration home. The installed standalone
  `codex-cli 0.159.3` is configured with `workspace-write`/`approvalPolicy=never`; the
  Kernel adapter rejects explicit workspace mount/network-destination grants, but it
  does not implement or attest Codex's sandbox. The repository has no test proving that
  native terminal commands cannot read outside the workspace or access `HOME` auth.
- `workspace-write` must not be assumed to deny reads outside the workspace. Provider
  inference requires the app-server's auth HOME and provider connectivity; the model
  app-server and terminal tool run under the same native trust profile. Internal
  provider subcalls within a running turn are not individually admitted by Kernel and
  may remain in flight through STOP/expiry; usage/result is UNKNOWN if the provider
  does not report it.
- Ordinary Codex local coding can remain available under explicit
  `TRUSTED_DEVELOPMENT` same-principal consent: the user accepts that the app-server
  and native tool may read files/credentials reachable to that account. Use a limited
  inference account, and do not place production or remote-write credentials in that
  identity. This is a trust decision, not confidentiality. If a task requires the
  provider key hidden from the tool, all outside-workspace reads denied, or verified
  shell egress denial, current supported mechanisms cannot prove that property; refuse
  that sensitive task. A stronger guarantee would require a new owner-authorized
  mechanism and is explicitly not part of this accepted baseline.
- The task's native local capability ends only when the selected owned process group
  or cgroup has profile-appropriate settlement evidence. If a descendant escapes the
  observable group, the result remains `unknown`; no universal process-tree containment
  is claimed.

## Current Codex Credential And Network Assessment

The current source passes `workspace-write` and `approvalPolicy=never` to the native
Codex app-server and rejects explicit Kernel workspace-mount/network-destination
grants. It does not implement the native sandbox or attest its read/egress behavior.
`task_execution.codex_environment` forwards `HOME`; its source comment identifies
`~/.codex` as the app-server's authentication/configuration home. The app-server needs
that provider auth and network connectivity to perform inference. A terminal command is
spawned natively under the same Codex trust profile. The repository contains no
evidence that the terminal child cannot read files outside the workspace or `HOME`,
including provider auth, and no evidence that all terminal egress is blocked. The
existing #53 app-server tests use a fresh synthetic `CODEX_HOME` in a disposable
container, not the host trusted-development auth home; that test is not a credential
isolation proof. No live inference or owner credential was used in forming this ADR.

Therefore this baseline does **not** guarantee provider-key secrecy from model-driven
terminal commands. It permits ordinary local Codex only as an explicit same-principal
trust decision: the user trusts the native app-server/tool tree with data reachable to
that OS account and uses a limited inference account with no production or remote-write
credentials in it. This avoids refusing all ordinary local Codex work while stating the
real boundary. If a task requires the provider credential hidden from the native tool,
all reads outside the workspace denied, or guaranteed terminal egress control, the
current supported path cannot establish it; that sensitive task/effect is unsupported
unless an owner later authorizes a different enforcement mechanism. Do not mislabel
`workspace-write` or an empty declared `network_destinations` set as proof of those
properties.

## Guarantees Preserved

- Exact task/Attempt/Operation identity, scoped grants, original immutable deadline,
  revocation fences, single-writer SQLite ownership and durable operation intents.
- No new Kernel-controlled dispatch or external mutation after the original
  deadline/revocation. Recovery never renews a deadline, creates a successor Attempt,
  changes the route, or blindly replays an uncertain effect.
- Deadline expiry and user STOP record cancellation against the exact active Attempt
  and use the existing Runtime interrupt plus bounded process-group/OCI cleanup. An
  interrupt acknowledgement, terminal turn status or leader exit alone is not
  settlement; the applicable owned-process witness is required before settlement.
- Candidate acceptance/publication is blocked while the task or its external effects
  are unknown. Partial workspace changes are retained, not relabelled as verified or
  rolled back.
- Kernel-controlled Forgejo/Router/MI effects remain separately authorized and
  reconciled. A task-level native grant does not confer controller API credentials or
  external write authority.
- The verification/test-child profile (#50) remains a separate, narrower use. It is
  not promoted into whole-worker isolation by this ADR.

## Guarantees Deliberately Relinquished

The accepted baseline does **not** guarantee that no new native filesystem or
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

| Scenario | Accepted baseline response | Remaining risk/containment |
| --- | --- | --- |
| Deadline expires during an active Codex turn or across host suspend | Use the verified suspend-inclusive `CLOCK_BOOTTIME` projection to deny new Kernel admissions and initiate bounded cancellation. | Native local writes or a new subprocess may still occur during termination; preserve the workspace and keep it unaccepted until owned-process settlement and fresh verification. No syscall cutoff is claimed. |
| Controller/host restarts, wall clock moves backward, or boot identity changes | Same-boot recovery reuses the original projection. A missing/unverifiable clock or changed boot identity denies new dispatch and reports `unknown`. | The current wall-clock-only schema does not yet prove suspend-inclusive, rollback-resistant expiry; implementation and clock tests are required. |
| User presses STOP during a framed RPC | Latch the request; handle it on the existing owner thread after the finite RPC boundary; persist cancellation before teardown. | The stop is not instantaneous. Report requested/cancelled/uncertain honestly. |
| Deadline or STOP cancellation receives a terminal turn or interrupt acknowledgement while the process group remains | Neither runtime terminality nor acknowledgement is settlement; wait for the applicable owned-process evidence. | Retain workspace and mark `unknown`/`cancel_uncertain`; block acceptance/publication and a conflicting successor Attempt without blocking independent eligible tasks. |
| Descendant calls `setsid`/escapes the observable group | Keep the task uncertain; do not claim all descendants have terminated. | This mode is not a hostile-worker sandbox. If the task requires guaranteed containment, refuse it rather than silently widening support. |
| Controller is lost during termination or before settlement evidence is durable | Recovery uses only the exact durable operation and an authoritative settlement witness; absence of that witness remains uncertain. | Retain the workspace; do not infer settlement from a terminal turn, reacquire a numeric PID, accept/publish, or start a conflicting successor Attempt. Independent eligible tasks remain separate. |
| Native command attempts remote write or protected API access | Kernel adapter tokens are not forwarded, but provider auth in HOME may be readable and native terminal egress is unverified. | Ordinary local Codex is an explicit same-principal trust choice with a limited provider account. Tasks needing protected remote state or secret isolation are unsupported unless the existing profile proves that boundary. |
| Controller loses a remote effect reply | Preserve the exact Operation as unknown and reconcile it; no fallback/retry. | No claim that timeout means the external system did nothing. |
| Native provider turn is still active at deadline | Do not start a new Kernel turn; interrupt the current turn and retain cost/result uncertainty. | A hard per-subcall cutoff or exact cost cap requires producer/Router evidence (#52/#56); it is not claimed here. |

## Codex Compatibility And Unsupported Work

The accepted baseline keeps the existing Codex `workspace-write`/`approvalPolicy=never`
integration and the existing `TRUSTED_DEVELOPMENT` acknowledgement. It does not switch
to `danger-full-access`, add a syscall broker, or replace the Codex executor. Ordinary
local coding can continue as an explicit trusted use of the current same-principal
Codex app-server/tool path; the user must understand that provider credentials and
other OS-readable files may be accessible to the native tool. This is not isolation.

The Kernel excludes its own controller tokens from the child environment, but it
cannot hide a provider credential stored in a HOME shared with the native Codex
process. Users should use a limited provider account and keep production/remote-write
credentials out of that OS identity. The exact version's `workspace-write` read and
egress behavior must be tested; it must not be assumed to deny outside-workspace reads
or terminal network. If a task requires provider-key secrecy, protected remote state,
or enforced shell egress denial, the current path cannot guarantee it; refuse that
sensitive task rather than refusing all ordinary local Codex. Strict
no-native-effect-after-expiry, hostile PR execution, and guaranteed termination of
escaped processes remain unsupported absent a separately authorized architecture.

## Bounded Acceptance/Falsification Plan

For future implementation acceptance only, use synthetic local resources and the exact
supported Codex/profile versions. Owner acceptance of this ADR does not authorize live
inference or production credentials:

1. **Kernel admission and deadline clock:** show that expiry/revocation denies a new
   Kernel turn, controller command and external Operation at their existing admission
   points. With controlled wall and boot clocks, test suspend/resume by advancing
   `CLOCK_BOOTTIME` across the deadline, wall-clock rollback after projection creation,
   reboot/boot-ID mismatch, unavailable or unverifiable clock identity, and same-boot
   recovery. Recovery must reuse the original projection, fail closed when it cannot
   verify it, and never generate a new Attempt or authority to extend the deadline.
2. **Native residual:** reproduce a positive local workspace write/subprocess during
   the termination interval under the selected `TRUSTED_DEVELOPMENT` profile. Record
   it as a residual partial effect and prove that it cannot be accepted, published,
   or converted into a new Attempt while termination/verification is outstanding.
   Do not treat the absence of a marker as proof of a strict fence.
3. **STOP and deadline settlement:** exercise user Ctrl-C and deadline-triggered
   cancellation before dispatch, during a finite RPC, after a terminal Codex turn or
   successful interrupt reply, with a live same-group descendant, an escaped/unobservable
   child, controller loss during cleanup, and incomplete or missing termination
   evidence. Terminal turn status and interrupt acknowledgement alone are never
   settlement. Verify the same exact owned-process settlement gate for both causes;
   unresolved evidence remains `unknown`/`cancel_uncertain`, retains the workspace,
   blocks acceptance/publication/retry and a conflicting successor Attempt, and does
   not block independent eligible tasks with non-conflicting resources. Verify bounded
   TERM/KILL, recovery without blind retry or numeric-PID reacquisition, and honest
   CLI/export output.
4. **Credential/egress disclosure:** use a synthetic provider credential and fake
   remote to test the exact native profile. Record whether a terminal child can read
   the provider-auth HOME or reach the fake remote. A readable provider sentinel is a
   same-principal trust exposure, not a sandbox pass. Ordinary local Codex can remain
   available under that explicit trust with no production/remote-write credentials;
   tasks requiring secret isolation or enforced egress denial are refused if the
   current sandbox cannot prove those properties. Never infer safety from prompt
   language.
5. **External effect recovery:** lose a reply after an authorized Kernel-controlled
   operation. Prove operation identity, durable unknown/reconciliation, and no blind
   retry or route change.
6. **Deadline/accounting:** test an in-flight native/provider operation at expiry. The
   system may report a residual/unknown, but may not report zero usage, no effect, or
   strict syscall expiration without evidence.

These checks evaluate implementation of the accepted task-level contract. They do not
reopen #53, repeat its source/pin investigation, or convert its positive strict-expiry
diagnostic into an acceptance pass.

## Decision And Acceptance

**DECISION: ACCEPTED — 2026-10-18.** The owner accepts the task-level capability
horizon, local native termination residual, same-principal provider-auth tradeoff for
ordinary Codex, boundary for protected remote effects, suspend-inclusive
`CLOCK_BOOTTIME` deadline projection and fail-closed recovery behavior, and no-candidate/
no-retry behavior while termination or external outcomes are unknown.

This baseline is authoritative for the defined `TRUSTED_DEVELOPMENT` profile. It
deliberately relinquishes strict per-syscall expiry and hostile-worker containment;
strict native-effect expiry remains unsupported. Issue #53 remains closed/not
implemented, and its diagnostic is not retroactively accepted. The accepted decision is
requirements authority, not proof that the current runtime implements those requirements:
deadline clock projection and owned-process settlement remain explicit implementation
gaps tracked by the existing Kernel issues.

Ordinary local Codex remains available only as an explicit same-principal trust decision;
that does not amount to Kernel control over native terminal egress or provider-auth
confidentiality. Tasks requiring provider-key secrecy, protected remote state or
guaranteed shell egress isolation are unsupported on the current path. No owner decision
is pending to exclude those unsupported guarantees or to authorize the accepted
documentation and Kernel issue-body synchronization.
