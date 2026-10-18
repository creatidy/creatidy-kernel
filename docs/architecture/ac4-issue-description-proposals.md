# Approved AC4 Issue-Description And Dependency Changes

- Status: Owner accepted 2026-10-18; this document records the approved Kernel body text
- Live state: This PR does not change issue bodies, labels, states, or dependency edges
- Original draft date: 2026-10-16
- Owner acceptance: 2026-10-18
- Authority: Owner approval authorizes the listed Kernel issue-body updates after PR #91
  merges. Cross-repository Console text still requires Console-owner approval.

## Dependency Reconciliation

The canonical Forgejo dependency APIs return no formal dependencies or dependents for
#53. #59, #60 and #90 also have no formal incoming dependency edges. The issue bodies
and comments do carry local prerequisites; closure of #53 does not satisfy them.

| Issue | Current gate/reference | Owner-approved direction after PR #91 merges | Still required |
| --- | --- | --- | --- |
| #53 | CLOSED / NOT IMPLEMENTED; PR #85 CLOSED / UNMERGED. Positive native post-expiry effect is diagnostic evidence. | Leave closed. Do not retrofit its history or claim its strict-expiry acceptance. | Historical strict native expiry remains unmet and unsupported under ADR 0011; the diagnostic is not a task-level implementation receipt. |
| #52 | Open; historical issue comment [#14078](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/52#issuecomment-14078) orders producer contracts before a “#52/#53 consumer receipt”; this wording is in the comment, not the current issue body. | After PR #91 merges, add the approved clarification to the #52 issue body. Preserve comment #14078 unchanged; do not silently edit or claim to replace it. | Router #174/#178/#179/#180/#181, existing Router #165 cancellation evidence, and Kernel #47/#49/#51/#52 gates remain. |
| #54 | Open; body depends on #47/#48/#49/#50 and a separately authorized Router review route; no direct #53/#90 edge. | Preserve synthetic-first eligibility independently of #90 and #60. Require #90 STOP/settlement evidence only for live trusted-development review/remediation that can incur native effects. | Review-route, live inference/effect scopes, #50 test-child scope and existing policy/budget gates remain. |
| #56 | Open; whole-task deadline, in-flight overrun and unknown-cost accounting. The current source records terminal cancellation before owned-process settlement is established. | Specify the accepted suspend-inclusive deadline projection and tests; expiry uses the same owned-process settlement gate as user STOP. Record the current ordering defect as an implementation gap, not as fixed by this documentation. | Router #184 and approved task budget/usage evidence remain; uncertainty is scoped to the affected Attempt/resources. |
| #57 | Open; owner-served task state and user-facing status. | Distinguish deadline/STOP request from owned-process settlement and uncertainty; don't imply exact syscall cutoff. | SQLite owner-thread, snapshot/event and current-authority gates remain. |
| #58 | Open; full-path private outcome/cost export. | Export trust profile, deadline decision and cancellation/settlement evidence; never report unknown local effects/usage as zero or rollback. | MI/Router producer contracts, privacy and versioned export gates remain. |
| #59 | Open; body says “#53 chosen harness” and requires owned workspace/process cleanup. | Replace #53 as a strict-expiry/harness prerequisite with the accepted task-level profile and scoped #90 STOP evidence. State same-principal provider-HOME trust; no production/remote-write credentials in the task identity. | #48 lifetime, #50 verification/test-child, #57 view, #61 cache ownership, manual-edit and Router parallel-call gates remain. |
| #60 | Open; integrated reception includes #53 harness adapter/lifetime. | Replace #53 as a strict native-expiry acceptance prerequisite with accepted ADR 0011 + #90 and other actual component receipts. Require expiry-triggered cancellation to meet the same owned-process settlement gate as user STOP, and report the termination-window residual and same-principal provider-HOME trust explicitly. | #47/#48/#49/#50/#52/#54/#55/#56/#57/#58/#59/#61/#62, Router #141 and MI #19 gates remain. |
| #62 | Open; installed/profile/platform compatibility. | Add an explicit trusted-development profile row: no strict native effect cutoff or hostile-worker claim; provider-auth HOME is same-principal trust unless a test proves otherwise; no production/remote-write credentials; no fallback to a broader profile. | Actual producer versions, migration safety and platform evidence remain. |
| #79 | Open verification/test-child follow-up; PR #83 is already merged. Its body says #53's whole-worker gate remains separate. | Clarify that #50/#79 are test-child only and neither proves whole-worker isolation nor strict expiry. | Exact upstream/license/provenance and test-child acceptance remain; do not promote the profile. |
| #90 | Open user-requested STOP issue; no formal dependency on #53. Its body already records the terminal-turn-before-process-group-settlement ordering gap. | Keep #90 scoped to user-requested STOP and its existing implementation gap; do not claim documentation acceptance fixes it. Approved #56/#60 criteria separately require the same settlement gate for deadline cancellation. | Exact owned-process-group settlement, unknown reporting and single-writer behavior remain. |
| Console #4/#8/#12/#13 | Open Console consumers; #8/#13 link #53 and #12 tests revoked/expired permissions. No formal edge to #53 is registered. | Proposed consumer copy below; requires Console-owner acceptance, not a Kernel issue mutation. | Console parent/producer/browser/security/service gates remain. |

No formal Forgejo dependency edge is authorized or changed by this acceptance. In
particular, #54 synthetic-first work has no #90 or #60 prerequisite. Potential #59 -> #90
and #60 -> #59/#90 edges remain unregistered and are not part of this task. #52's Router
producer gates remain independent. External Router/MI/Console issue bodies and statuses
are not changed here.

## Kernel Issue-Description Patches

The following excerpts are the exact owner-approved replacements/additions for the
Kernel issue bodies after PR #91 merges. All unquoted criteria, authorities, acceptance
evidence and external producer gates remain in force. The PR itself does not mutate live
Forgejo issue descriptions.

### #52 — Route Consumer

**Historical source (issue comment #14078; preserve unchanged, not issue-body text):**

> First integration prerequisite after #47 control/backend proof + approved #49/#51
> requirements; producer #178/#179/#180 and #174 conformance; #181 specific protocol
> evidence; Kernel #52/#53 consumer receipt. No source/effort/access-mode parsing or
> physical attestation inference from gateway audit.

**Approved issue-body addition (apply after PR #91 merges):**

Add this paragraph to the #52 issue body's **Dependencies And Priority** section, after its existing ordering paragraph:

> Retain the existing prerequisites: #47 control/backend proof; approved #49/#51
> requirements; Router #174 executable decision/admission, #178 route identity, #179
> identity/effort, #180 workspace authority, #181 protocol evidence, and #165
> cancellation evidence; and an exact Kernel #52 consumer receipt. This receipt proves
> only Kernel-controlled route/admission and Attempt-bound identity; it does not prove
> strict native effect expiry. Under the accepted task-level baseline, expiry denies
> new Kernel-controlled dispatch and initiates bounded STOP while native local effects
> may continue until the exact owned task settles. Preserve the existing prohibition on
> source/effort/access-mode parsing or physical-attestation inference from gateway audit.
> #53/PR #85 remains CLOSED / NOT IMPLEMENTED; its diagnostic is neither a shipped
> route adapter nor an acceptance pass.

This is the approved addition to the issue body, not a replacement or edit of comment
#14078. After PR #91 merges, the new body text is the actionable current-scope
clarification; the historical comment remains intact and is not a formal dependency edge.
This addition does not retire any listed producer or Kernel consumer gate, resolve the
explicit D-063 workspace-owner decision, or grant live inference/effect authority.

### #54 — Review And Remediation

**Current excerpt (Dependencies And Priority):**

> Product acceptance prerequisite after approved spec #49, proved harness #47/adapter,
> isolation #50 and lifecycle #48. Router owns separately authorized review route;
> whole-task budget/cost and exports must consume review/remediation facts. No manual
> owner handoff as integration plan.

**Approved replacement:**

> Product acceptance prerequisite after approved spec #49, proved harness/control
> evidence #47, verification/test-child profile #50 and lifecycle #48. Synthetic
> fixture-first review/remediation remains independently eligible after these existing
> synthetic prerequisites; it does not wait for #90, #53 or #60. Only a live
> trusted-development review/remediation path that may create native effects must use
> an explicit trusted-development profile and consume #90 STOP/uncertainty/settlement
> facts, with separately authorized Router route and effect scope. An independent verdict
> is not proof that native effects stopped at expiry or that the reviewer was
> hostile-worker isolated. Router owns the separately authorized review route;
> whole-task budget/cost and exports must consume review/remediation facts. No manual
> owner handoff as integration plan.

This does not grant live review/inference/effect scope or promote #50 into a worker
profile.

### #56 — Whole-Task Budget

**Current excerpt (Acceptance Criterion 2):**

> Admission and each permitted new effect respect approved current envelope; restart
> cannot reset task budget/deadline. In-flight overrun/unknown accounting is explicit
> with tested pause/reconciliation behavior.

**Approved replacement:**

> Each new Kernel-controlled admission respects the approved envelope and original
> deadline; restart cannot reset it. Use a verified suspend-inclusive Linux
> `CLOCK_BOOTTIME` projection anchored to the original wall deadline and host boot
> identity. Same-boot recovery reuses the persisted projection; it must not derive a
> later expiry from the current wall clock. Deadline expiry stops new Kernel dispatch
> and initiates bounded cancellation. If the clock, its reading, or boot identity is
> unavailable or unverifiable, or the boot identity changes, fail closed: deny dispatch
> and a successor Attempt and keep the Attempt `unknown` pending reconciliation.
> Tests use controlled clocks to cover suspend/resume across expiry, wall-clock rollback
> after projection creation, reboot/boot-ID mismatch, unavailable/unverifiable clock
> identity, and same-boot recovery without projection renewal. Local native effects and
> provider usage during an active turn/termination interval are recorded as observed or
> `unknown`, never assumed zero or rolled back. No strict per-syscall expiry is claimed.
> In-flight overrun/unknown accounting is explicit with tested pause/reconciliation
> behavior.

Router #184's call/quota evidence and approved numeric task budget remain required.

**Approved addition to Acceptance Criteria (new criterion 7):**

> Deadline-triggered cancellation uses the same exact owned-process settlement gate as
> user-requested STOP. A terminal Codex turn or successful interrupt acknowledgement
> is not settlement. Test a live owned-group descendant, an escaped/unobservable child,
> controller loss during termination or before the settlement witness is durable, and
> incomplete or missing termination evidence. Unless the exact profile-appropriate
> settlement witness is durably verified, retain the workspace, report `unknown` or
> `cancel_uncertain`, and block acceptance, publication, retry and any conflicting
> successor Attempt. Recovery must not infer settlement from a terminal turn or
> reacquire a numeric PID. Scope uncertainty to the affected Attempt/resources; do not
> block independent eligible tasks with non-conflicting authority and workspaces.

### #57 — Served State And Progress

**Current excerpt (Acceptance Criterion 4):**

> CLI human status shows work/blocked/unknown/decision needed/actual
> workspace/candidate and safe next action; JSON/pipes remain machine-usable with
> version/freshness/unknowns. No leaked secrets/transcripts/private telemetry.

**Approved replacement:**

> CLI human status shows work/blocked/unknown/decision needed/actual
> workspace/candidate and safe next action; JSON/pipes remain machine-usable with
> version/freshness/unknowns. When a deadline or STOP occurs, distinguish request,
> Kernel dispatch cutoff, and observed owned-process settlement/uncertainty. Do not
> imply that an expired task or interrupt acknowledgement stopped every native effect.
> No leaked secrets/transcripts/private telemetry.

Keep the existing single-writer/store and owner-served projection contracts.

### #58 — Outcome And Cost Export

**Current excerpt (Acceptance Criterion 1):**

> Offline scenario exports task/Attempt/session/route/call/candidate/spec/policy/verdict/
> provenance and multidimensional consumption/unknowns for the whole journey; imported/
> consumer fixture reproduces facts without granting authority.

**Approved replacement:**

> Offline scenario exports task/Attempt/session/route/call/candidate/spec/policy/verdict/
> provenance, the selected trust profile, original deadline decision, STOP/cancellation
> outcome and multidimensional consumption/unknowns for the whole journey; imported/
> consumer fixture reproduces facts without granting authority. Native effects during
> the termination interval are not exported as zero or rolled back, and no field/receipt
> claims strict native syscall expiry.

Do not add telemetry, prompt contents, a new service, or inferred provider usage.

### #59 — Concurrent Tasks And Workspaces

**Current excerpt (Dependencies And Priority):**

> Depends on safe source-cache ownership, #48 lifetime, #50 isolation and #53 chosen
> harness; #57 supplies concurrent observation. May specify existing ownership
> invariants independently. Producer Router parallel-call admission remains external,
> not a Kernel scheduler to clone.

**Approved replacement:**

> Depends on safe source-cache ownership (#61), #48 lifetime, the #50
> verification/test-child profile, #57 owner-served concurrency state, and #90 user
> STOP/owned-process outcome for baseline cancellation acceptance. #50 is not promoted
> to whole-worker isolation. #53/PR #85 is closed NOT IMPLEMENTED; its cold-recovery
> evidence is diagnostic, not a shipped harness integration or strict-expiry proof.
> Under the accepted baseline, task expiry stops new Kernel admissions and initiates
> STOP while native local effects may continue during termination. Unsettled/escaped
> work remains `unknown`; retain the workspace and block acceptance/publication/successor
> Attempt while uncertain. Producer Router parallel-call admission remains external,
> not a Kernel scheduler to clone. The task user accepts the native Codex same-principal
> provider-HOME trust and must not expose production/remote-write credentials through
> the worker identity.

Keep the manual-edit, immutable Attempt, cache-ownership and foreign-resource cleanup
criteria. Applying this body text does not mark #59 complete or authorize live multi-task work.

### #60 — Installed End-To-End Acceptance

**Current excerpt (Dependencies And Priority):**

> Final reception after harness proof/adapter/lifetime (#47/#48/#53), approved
> intake/requirements (#49/#51), Router route (#52), isolation (#50), review (#54), MI
> refs (#55), budgets (#56), served state (#57), export (#58), and safe
> cache/concurrency/migration work. Producer contracts must be accepted first.
> Fixture/protocol planning can start earlier, but unavailable functionality cannot be
> counted as received.

**Approved replacement:**

> Final reception after #47/#48, approved intake/requirements (#49/#51), Router route
> (#52), verification/test-child profile (#50), review (#54), MI refs (#55), budgets
> (#56), served state (#57), export (#58), safe cache/concurrency/migration (#59/#61),
> platform matrix (#62), and user STOP evidence (#90). #53/PR #85 remains closed and
> its strict-expiry diagnostic is not a supported adapter or acceptance pass. Producer
> contracts must be accepted first. Fixture/protocol planning can start earlier, but
> unavailable functionality cannot be counted as received.

**Approved baseline acceptance addition:**

> A trusted-development receipt must show that new Kernel-controlled dispatch/external
> operations stop at the original deadline/revocation/STOP, and that deadline-triggered
> cancellation uses the same owned-process settlement gate and bounded termination
> evidence as user-requested STOP. A terminal Codex turn or successful interrupt
> acknowledgement alone is not settlement. Installed acceptance must cover a live
> descendant, an escaped/unobservable child, controller loss during termination or
> before durable settlement evidence, and incomplete termination evidence. Until the
> exact settlement witness is verified, retain the workspace, report `unknown` or
> `cancel_uncertain`, and block acceptance/publication and a conflicting successor
> Attempt; independent eligible tasks with non-conflicting resources remain eligible.
> It must separately disclose local native effects during termination. Such a receipt
> does not prove strict native-effect expiry or hostile-worker isolation. It must also
> record the selected Codex trust profile and same-principal provider-auth exposure;
> production/remote-write credentials are not made available to the native worker.

Keep actual Router #141, MI #19, producer, privacy, live-scope and independent-review
gates. Do not close #60 based on documentation or synthetic fixtures alone.

### #62 — Public Install And Compatibility

**Approved addition to Acceptance Criteria:**

> The installed compatibility matrix labels `TRUSTED_DEVELOPMENT` as trusted native
> execution with task-level deadline/STOP semantics, not strict per-syscall expiry or
> hostile-worker isolation. It must state that Codex provider auth in `HOME` may be
> readable to terminal tools, that no production/remote-write secrets are permitted in
> the task identity, and that native egress is unverified unless the exact sandbox test
> proves it. Ordinary same-principal local coding is an explicit trust choice; a task
> requiring secret secrecy or enforced egress control is unsupported without evidence.
> Never fall back to a broader profile.

Keep existing ciphertext, migration, package and producer-version gates.

### #79 — Bubblewrap Verification/Test-Child Follow-Up

**Current excerpt (Acceptance Criterion 6):**

> #50 history remains unchanged, profile stays verification/test-child only, #53
> whole-worker gate remains separate.

**Approved replacement:**

> #50 history remains unchanged and this profile stays verification/test-child only.
> Neither #50 nor this issue proves whole-worker isolation or strict native effect
> expiry. #53/PR #85 is closed NOT IMPLEMENTED; its post-expiry diagnostic remains
> evidence of the gap, not a promotion of this profile. This issue does not close or
> weaken the accepted AC4 baseline.

No bubblewrap qualification, source/pin change, or implementation is authorized by this
AC4 document.

### #90 — Trusted-Development STOP

**Existing implementation gap (source-confirmed; already recorded in the live #90 body):**

`src/creatidy_kernel/adapters/task_execution.py` records the runtime Operation as
`terminal` and calls `finish_cancelled_attempt` after observing a terminal Codex turn
(`runtime.observe`, currently around lines 1792-1805), before the normal controller
close path obtains `CodexStdio.owned_group_settled`. Runtime terminality can therefore
precede process-group settlement. This remains an implementation gap for #90's existing
user-requested STOP scope; this documentation PR does not fix it. The approved #56/#60
criteria below separately require deadline-triggered cancellation to consume the same
settlement gate.

**Current excerpt (Constraints):**

> Do not claim strict native grant expiry, universal effect mediation, hostile-worker
> containment, or issue #53 AC4 acceptance.

**Approved replacement:**

> This issue supplies only the STOP/cancellation component of a task-level baseline,
> under accepted ADR 0011. It does not prove strict native effect expiry, universal
> mediation, hostile-worker containment, rollback, or full AC4 acceptance. During
> bounded termination local native effects may still occur; report unknown/unsettled
> descendants honestly and block acceptance/publication while uncertainty remains.

#90 remains a live issue; this documentation PR does not change its live issue body.

## Cross-Repository Consumer Issue Proposals

These patches are suggestions to the Console owner. They are included because the
current open bodies consume Kernel expiry/task state or explicitly link #53. No Console
issue, state, or dependency is mutated by this Kernel PR.

### Console #8 — Supported Harness Navigation

**Current excerpt (Links / Dependencies):**

> Parent #1; Console #2/#4/#5/#9. Router #140/#183; Kernel #47 and /issues/53.

**Proposed replacement:**

> Parent #1; Console #2/#4/#5/#9. Router #140/#183; Kernel #47 (harness evidence) and
> #90 (trusted-development STOP/status). Closed #53/PR #85 is historical strict-expiry
> evidence, not a remote/IDE attach or no-effect guarantee. Remote navigation remains
> separately authorized and displays the actual trust profile.

### Console #12 — Producer/Browser/Security Acceptance

**Current excerpt (Acceptance Criteria):**

> Exercise missing peer, duplicate/reordered/missing events/gaps, stale/shared
> quota/unknown usage, mismatched versions, revoked/expired permission,
> HTML/script/link payloads, changed approval revision, denied mutation and
> timeout-after-acceptance.

**Proposed replacement:**

> Exercise missing peer, duplicate/reordered/missing events/gaps, stale/shared
> quota/unknown usage, mismatched versions, revoked/expired Kernel admission,
> HTML/script/link payloads, changed approval revision, denied mutation and
> timeout-after-acceptance. Separately exercise a running trusted-development task at
> deadline/STOP: distinguish Kernel admission denial from native termination, expose
> `cancel_uncertain` when settlement is unknown, and do not infer rollback or zero local
> effects from expiry/closure.

### Console #13 — Remote/PWA/IDE Surfaces

**Current excerpt (Links / Dependencies):**

> Parent #1; Console #2 and local installation/notification/navigation issues. Kernel
> #47 and /issues/53; Router #183.

**Proposed replacement:**

> Parent #1; Console #2 and local installation/notification/navigation issues. Kernel
> #47 (harness evidence) and #90 (STOP/status); Router #183. Closed #53/PR #85 is
> historical diagnostic evidence, not remote/IDE attachment authority, background
> task-lifetime proof, or strict effect expiry. Remote topology remains a separate
> owner/security decision.

### Console #4 — Cross-Product Overview

No direct #53 dependency is present in its current description. If the Console owner
accepts ADR 0011, the issue's existing exact-lineage/status acceptance should add:

> Display the selected trust profile and distinguish Kernel admission, STOP request,
> owned-process settlement and `unknown`. Expiry/interrupt/closed UI does not imply
> rollback or zero native effects; candidate acceptance requires authoritative current
> evidence.

No cross-repository issue patch or dependency edge is applied here.

## Dependency Impact Table

| Current relation | Formal edge? | Approved issue-body impact |
| --- | --- | --- |
| #53 → #59 “chosen harness” | None; issue-body gate only | Retire from the accepted-baseline body after PR #91 merges; keep #53 closed and its historical strict diagnostic unmet. #90 STOP is a scoped body receipt, not a formal edge. |
| #53 → #60 installed reception | None; issue-body gate only | Replace after PR #91 merges with accepted baseline plus actual #90/#59/component receipts. Do not count #53 diagnostic as native acceptance. |
| #53 ↔ #90 | None; #90 links #53 as history | #90 handles STOP only; it neither depends on nor satisfies strict expiry. |
| #53 ↔ #79 | None; #79 says the worker gate is separate | Keep #79 verification/test-child only; no promotion to task runtime or AC4 proof. |
| #52 route/consumer | No formal #53 edge; historical issue comment #14078 names #52/#53 consumer order | Add the specified #52 issue-body clarification after PR #91 merges; preserve comment #14078 unchanged. Keep Router #165/#174/#178/#179/#180/#181 and Kernel #47/#49/#51/#52 gates. A route receipt does not prove native effect cutoff. |
| #59 → #90 (not authorized) | No current formal edge | No formal edge is authorized by this task. #59 may consume scoped STOP/settlement evidence in its body without adding a graph dependency. |
| #60 → #59/#90 (not authorized) | No current formal edge | No formal edge is authorized by this task. #60's body retains installed acceptance gates without mutating dependency state. |
| Console #8/#12/#13 references | No formal edge to #53 in the inspected Console graph | Propose display/acceptance wording to Console owner; no cross-repository mutation here. |

Other upstream producer/compatibility gates (Router #174/#181/etc., MI publication,
Console parents) are not retired by accepted ADR 0011. The owner-approved Kernel body
text does not modify formal dependency state; Console consumers still require their
separate owner approval.
