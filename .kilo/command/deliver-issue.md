---
description: Deliver one selected issue through a bounded independent-review loop
---

# Deliver Issue

Deliver exactly one owner-selected Forgejo issue in `Creatidy/creatidy-kernel`:

`selected issue -> implement -> validate -> PR -> fresh independent review -> bounded remediation -> fresh review -> READY_FOR_OWNER_MERGE`

This is normal single-issue development, not Program execution or a Kernel runtime state machine.
The owner selects the issue once; the delivery session carries PR/review results and remediation
instructions between sessions, without asking the owner to relay them. Use `/implement-issue` for
issue-to-PR work and `/review-pr` for each read-only review; their preconditions and guardrails still
apply. Do not duplicate their procedures here.

## Start And Deliver

1. Require exactly one issue explicitly selected by the owner (number, URL, or unambiguous title).
   Fetch its actual body through Forgejo MCP and confirm scope, acceptance criteria, constraints,
   links, and non-goals. Do not select another issue or infer one from a PR, branch, or ledger. On a
   resumed delivery, re-fetch that same issue, PR, and ledger; do not rely on conversation memory.
2. Follow `/implement-issue`: fetch `origin/develop`, branch from it, make the focused change, run
   focused checks, `make check` and `make package-check` (and `make audit` when relevant), then push
   and create **one** PR to `develop`. Keep the selected issue open until owner integration. Never
   modify or target `main`, open a replacement PR for a blocker, or auto-merge.
3. Link the PR to the issue. On the issue or PR, record an initial delivery ledger with the issue,
   PR, branch, current HEAD, `max_remediation_cycles = 10`, and `remediation_used = 0`. Keep all later
   entries together on that issue or PR. Before every transition, reconcile ledger entries with the
   actual PR HEAD, previous reviews, and validation; an unexplained HEAD or ledger gap blocks a
   claimed approval until resolved. Never reset the budget on session, reviewer, or HEAD changes.

## Independent Review

1. Freeze the **exact PR HEAD** and base before each review. Classify the change, then resolve the
   reviewer through the configured maintainer/model-routing policy when available. Use
   `critical-independent-review` for persistence/durability, recovery/reconciliation, concurrency,
   authority/security/privacy, lifecycle/state-machine semantics, external-effect safety, and
   migrations that risk corruption or semantic reinterpretation. Otherwise use
   `standard-independent-review`. Architecture/model-correction review is always critical. These
   are capability classes, not public model/provider assignments.
2. Create a **fresh, distinct, read-only reviewer session** for each substantive review, passing the
   PR, frozen HEAD, selected issue, and `/review-pr` contract. Require a full review of the current
   PR and acceptance evidence, not only previous findings or the latest diff. The implementation
   session cannot review or approve its own work. Do not reuse a prior reviewer session after a HEAD
   change. Use the configured private dispatch rules rather than encoding provider details here.
3. Track separately: reviewer requested; dispatch configuration valid; requested model resolved
   where the execution environment exposes resolution evidence; independent session completed; and
   verdict obtained for the frozen HEAD. Request acceptance, session status, prompt text, and
   reviewer self-report are not model-resolution evidence. Require explicit resolution evidence
   when the environment supplies it; follow the governing routing policy for any unobservable
   identity. Do not count a malformed dispatch, wrong/unresolved required reviewer, incomplete
   review, or changed HEAD as a valid review cycle. Never silently substitute for an explicitly
   required reviewer.
4. Diagnose a concrete launch failure before retrying. Allow at most **two corrected launch retries**
   for a frozen HEAD, changing only the diagnosed bad input; do not retry the same malformed request
   or churn unrelated parameters. If no valid independent session can be obtained, stop with
   `REVIEW_INFRASTRUCTURE_BLOCKED`; dispatch failures consume no remediation budget. Record failed
   attempts only as clearly invalid diagnostics, never as review cycles. If routing requires a new
   owner policy decision rather than infrastructure repair, stop with
   `GENUINE_OWNER_DECISION_REQUIRED`.

## Ledger And Remediation

After **each valid, completed full review**, persist a concise numbered entry on the selected issue
or PR: review cycle number; exact reviewed HEAD and reviewer class; independent review reference and
verdict (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`); concise blocker IDs/titles; remediation status
(`none`, `pending`, or `done`); resulting HEAD if changed; and `remediation_used / 10`. Include the
dispatch-verification outcome without publishing private routing details. Update or append a linked
remediation entry immediately after pushing a changed HEAD, so a fresh session can reconstruct both
the count and the next review subject. Preserve prior entries and finding identities; reconcile an
interrupted or inconsistent ledger before continuing. No prompts, chain-of-thought, or transcripts.

- Initial review and every fresh review cost **zero** remediation cycles. A `REQUEST_CHANGES` verdict
  costs **one** only when valid in-scope findings lead to implementation changes; several findings
  fixed together cost one. Invalid, duplicate, out-of-scope findings and dispatch failures cost zero.
- Validate blockers against the frozen HEAD, selected issue, and accepted architecture. For valid
  in-scope blockers, amend the **same** branch and PR, increment once for that changed HEAD, run the
  repository-required validation, push, update the ledger, then dispatch a **new** independent
  reviewer for a full review of the new exact HEAD. Do not create issues for ordinary blockers.
  Record useful out-of-scope findings only as candidates for later owner triage; do not implement
  them here. `COMMENT` is not approval; resolve its actionable uncertainty without treating it as
  remediation unless a valid in-scope change is made, and obtain a conclusive fresh review.
- Do not point-patch indefinitely. If a fundamental defect recurs, fixes oscillate, a domain/model
  inconsistency appears, or resolution seems to change the issue contract or accepted architecture,
  first dispatch a **fresh, read-only critical** architecture/model-correction review of the frozen
  HEAD. It returns `BOUNDED_CORRECTION` or `GENUINE_OWNER_DECISION_REQUIRED`. The review costs zero;
  a bounded correction compatible with the selected issue and architecture costs one when changed,
  followed by validation, push, ledger update, and fresh full independent PR review. Never widen
  scope or change acceptance criteria to obtain approval. A material owner authority, architecture,
  security/privacy, scope, or policy decision stops delivery; an ordinary defect does not.
- After the tenth remediation, obtain the required fresh review of its changed HEAD. If it does not
  approve, stop at `10 / 10` with `REMEDIATION_BUDGET_EXHAUSTED`. Never start an eleventh change or
  extend the budget autonomously. A later reviewer/session/HEAD never resets the count.

## Finish

`APPROVE` alone is insufficient. For the **exact approved HEAD**, verify repository-required local
validation, green required Forgejo CI, issue acceptance criteria, and no unresolved blocking review
finding. If the HEAD moved, repeat the fresh full review and required checks; do not reuse approval.
If required evidence is temporarily unavailable, diagnose it and do not claim readiness or invent a
human gate. Report the precise blocker in the ledger. End with exactly one of:

- `READY_FOR_OWNER_MERGE`: exact-HEAD approval and all final evidence verified; do not merge.
- `REMEDIATION_BUDGET_EXHAUSTED`: 10/10 and no approval, with remaining blockers identified.
- `REVIEW_INFRASTRUCTURE_BLOCKED`: bounded corrected attempts could not obtain a valid independent
  review or its required verification evidence.
- `GENUINE_OWNER_DECISION_REQUIRED`: a material decision outside this issue's granted scope.

Leave the issue open and the PR unmerged. Report the ledger location, reviewed/approved HEAD,
validation and CI evidence, budget used, and exact terminal condition to the owner.
