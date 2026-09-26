---
description: Deliver one selected issue through a bounded independent-review loop
---

# Deliver Issue

Deliver exactly one owner-selected Forgejo issue in `Creatidy/creatidy-kernel`:

`selected issue -> implement -> validate -> PR -> fresh independent review -> bounded remediation -> fresh review -> READY_FOR_OWNER_MERGE`

This is normal single-issue development, not Program execution or a Kernel runtime state machine.
The owner selects the issue once; the delivery session carries PR/review results and remediation
instructions between sessions, without asking the owner to relay them. Use `/implement-issue` for
issue-to-PR work and `/review-pr` for each full PR review. Architecture/model-correction reviews
use its read-only, fresh-context and frozen-HEAD guardrails but have their own verdicts. Do not
duplicate either procedure here.

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

1. For **each** new full PR review, including a review after any changed HEAD, freeze the exact PR
   HEAD and base first. A fresh HEAD is a new AI dispatch boundary; never reuse the previous
   selection just because it is the same PR. Build the repository-review requirements from the
   selected issue, full PR, immutable correctness/security concerns, actual review task capability
   floor, known author capability floor when available, and review criticality. Treat persistence or
   durability, recovery/reconciliation, concurrency, authority/security/privacy, lifecycle/state
   semantics, external-effect safety, and corruption-sensitive migrations as architecture/security
   criticality. Do not guess unknown author capability. Review-requirement classification determines
   capability requirements only; it does not select or rank a reviewer model.
2. Use the existing Creatidy repository-review routing contract: Scarcity Router profile
   `repository_review` plus a **monotone** `tightening` equal to the element-wise maximum of actual
   task requirements, known author floor, and review-criticality requirements. Architecture/security
   and remediation reviews require task level `L4`, minima at least
   `{"reasoning":5,"coding":5,"tool_use":5}`, and both `requires_tool_use=true` and
   `requires_reasoning_mode=true`; preserve any known stronger author floor. For an ordinary review,
   derive the task floor from its actual requirements and preserve the calibrated profile and any
   stronger author floor. Do not lower requirements for availability or scarcity.
3. Make **one valid Scarcity Router selection for this review dispatch boundary**, with `profile_id`
   XOR `requirement` (use the established profile `repository_review` with `tightening`; do not
   invent router fields). A changed review HEAD or a separate architecture/model-correction review
   is a new boundary and requires a new selection. Never make repeated selections to shop for a
   preferred identity. A router `invalid_request` is a malformed call, not a selection: diagnose the
   schema/input defect and use only the bounded corrected retry policy. Use the exact eligible
   identity returned by the valid selection. If the router returns no selection or is unavailable,
   record the routing failure and fail closed: after bounded corrected infrastructure attempts use
   `REVIEW_INFRASTRUCTURE_BLOCKED`; if proceeding would require an owner decision to change the
   review requirement or routing policy, use `GENUINE_OWNER_DECISION_REQUIRED`. Never reuse an
   earlier selection or invent a route.
4. If the selected configuration cannot be dispatched, follow the existing harness-fallback policy:
   inspect **only** the Router-returned ordered eligible alternatives and use the first dispatchable
   eligible alternative that satisfies every hard requirement. Never dispatch an excluded candidate,
   lower requirements, or create a local reviewer ranking/fallback table. Record both
   `router_selected` and `dispatched`, with `routing_result=HARNESS_FALLBACK` when applicable. If no
   eligible configuration is dispatchable, stop with `REVIEW_INFRASTRUCTURE_BLOCKED`. Resolve model
   identifiers against the current Agent Manager catalog; do not guess, translate, or alias model or
   variant names. Use only the existing provider-ID adapter documented in the parent workspace's
   model-routing contract. This is public Kernel **development workflow** guidance, not a Kernel
   product dependency on Scarcity Router or the parent workspace.
5. For every full PR review, submit this **literal logical Agent Manager payload**, substituting the
   exact routed model `M`, routed provider `P` after the existing provider adapter, routed variant
   `V`, and actual PR URL:

   ```json
   {
     "mode": "local",
     "versions": false,
     "tasks": [{
       "prompt": "/review-pr <PR URL>",
       "model": "M",
       "provider": "P_AFTER_EXISTING_PROVIDER_ADAPTER",
       "variant": "V"
     }]
   }
   ```

   `mode` and `versions` are top-level request fields. `prompt`, `model`, `provider`, and `variant`
   belong inside **each** `tasks[]` entry; never put model, provider, or variant at top level. Use the
   returned model and variant verbatim and only the established provider adapter; no guessed
   identity, alias, local routing table, or implementation/controller-model substitution. For an
   architecture/model-correction dispatch, the task prompt begins with `/review-pr <PR URL>` and
   adds the correction question and its distinct verdict contract below, while preserving the
   read-only/fresh/frozen-HEAD requirements.
6. Launch a **fresh, distinct, read-only reviewer session** for each substantive review. A distinct
   session provides independence even if Router legitimately selects the implementation session's
   same model configuration. Do not add a different-family/provider requirement unless the review
   contract explicitly requires it. The reviewer fetches the PR and selected issue, confirms the
   current HEAD/base still match the frozen inputs, and performs a full review of the **whole current
   PR** and acceptance evidence, not only prior findings or the latest diff. The implementation
   session never performs the substantive independent review.
7. Track separately: routing selected; `REQUESTED` (dispatch payload submitted); `DISPATCH_VERIFIED`
   (task-level configuration valid and explicit Agent Manager resolution equivalent to
   `Resolved models: - <session>: <M> (<P_AFTER_EXISTING_PROVIDER_ADAPTER>) · <V>` matches the actual
   dispatched Router-eligible model/variant and provider after the documented adapter);
   reviewer session completed; and substantive verdict obtained for the frozen HEAD from `/review-pr`'s
   `### Overall Recommendation`, independently of Forgejo's persisted review state. A harness fallback
   must match the eligible alternative actually dispatched, not be mislabeled as the primary selection.
   Request acceptance, `action=list` session/state, prompt text, and reviewer self-report are not
   model-resolution evidence. If resolution is absent or mismatched, mark review `NOT_RUN`; do not
   increment the valid review counter or remediation budget. Inspect the payload against the actual
   Agent Manager schema, correct the diagnosed error, and never silently substitute a reviewer.
8. Diagnose a concrete launch failure before retrying. Allow at most **two corrected launch retries**
   for the frozen review boundary, changing only the diagnosed bad input; do not repeat the same
   malformed request or churn unrelated parameters. Router-returned eligible alternatives are the
   only harness fallback. If no valid independent reviewer can be dispatched or resolved, stop with
   `REVIEW_INFRASTRUCTURE_BLOCKED`; record failures only as invalid diagnostics, never as review
   cycles. Infrastructure/routing failures consume no remediation budget. A genuine material owner
   decision stops with `GENUINE_OWNER_DECISION_REQUIRED`.

## Ledger And Remediation

After **each valid, completed full review**, persist a concise numbered entry on the selected issue
or PR: review cycle number; exact reviewed HEAD; review profile and monotone capability floor; route
selected and dispatched identities (including adapter/fallback status); Router catalog/policy
versions and degraded flag when present; reviewer session/task ID; dispatch-verification state;
independent review reference; `substantive_verdict` (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`)
from `### Overall Recommendation`; actual `forgejo_review_state` (`APPROVED`, `REQUEST_CHANGES`,
or `COMMENT`); `platform_approval_recorded` (true only for persisted `APPROVED`); blocking finding
count and concise blocker IDs/titles; `platform_identity_limitation` when the shared PR-author
account prevents equivalent formal submission; remediation status (`none`, `pending`, or `done`);
resulting HEAD if changed; and `remediation_used / 10`. Do not record quota snapshots or transcripts.
Update or append a linked remediation entry immediately after pushing a changed HEAD, so a fresh
session can reconstruct both the count and next review subject. Preserve prior entries and finding
identities without rewriting historical review records; reconcile an interrupted or inconsistent
ledger before continuing. No prompts, chain-of-thought, or transcripts.

- Initial review and every fresh review cost **zero** remediation cycles. A substantive
  `REQUEST_CHANGES` verdict costs **one** only when valid in-scope findings lead to implementation
  changes; several findings fixed together cost one. Invalid, duplicate, out-of-scope findings and
  dispatch failures cost zero.
- Validate blockers against the frozen HEAD, selected issue, and accepted architecture. For valid
  in-scope blockers, amend the **same** branch and PR, increment once for that changed HEAD, run the
  repository-required validation, push, update the ledger, then dispatch a **new** independent
  reviewer for a full review of the new exact HEAD. Do not create issues for ordinary blockers.
  Record useful out-of-scope findings only as candidates for later owner triage; do not implement
  them here. A substantive `COMMENT` is not approval; resolve its actionable uncertainty without
  treating it as remediation unless a valid in-scope change is made, and obtain a conclusive fresh
  review. Do not derive the substantive verdict solely from `forgejo_review_state` or infer `APPROVE`
  from arbitrary comment text.
- Do not point-patch indefinitely. If a fundamental defect recurs, fixes oscillate, a domain/model
  inconsistency appears, or resolution seems to change the issue contract or accepted architecture,
  freeze the exact HEAD/base, derive architecture/security requirements, and make a **new**
  `repository_review` Scarcity Router dispatch using L4/5/5/5 (and any known stronger author floor).
  Launch a fresh read-only reviewer with the returned configuration and verified task-level dispatch.
  This architecture/model-correction review returns `BOUNDED_CORRECTION` or
  `GENUINE_OWNER_DECISION_REQUIRED`. The review costs zero;
  a bounded correction compatible with the selected issue and architecture costs one when changed,
  followed by validation, push, ledger update, and fresh full independent PR review. Never widen
  scope or change acceptance criteria to obtain approval. A material owner authority, architecture,
  security/privacy, scope, or policy decision stops delivery; an ordinary defect does not.
- After the tenth remediation, obtain the required fresh review of its changed HEAD. If it does not
  receive substantive `APPROVE`, stop at `10 / 10` with `REMEDIATION_BUDGET_EXHAUSTED`. Never start
  an eleventh change or extend the budget autonomously. A later reviewer/session/HEAD never resets
  the count.

## Finish

For the **exact frozen HEAD**, require verified independent dispatch, a completed fresh full review
with `substantive_verdict == APPROVE`, zero unresolved blocking findings, repository-required local
validation, green required Forgejo CI, and satisfied issue acceptance criteria. A formal Forgejo
`APPROVED` state is additional evidence when available, not intrinsically required here. If Forgejo
persists `COMMENT` solely because the configured reviewer identity authored the PR, preserve the
substantive `APPROVE`, record `forgejo_review_state = COMMENT`, `platform_approval_recorded = false`,
and the known shared-identity limitation verified from the PR author and platform rejection evidence;
continue the readiness evaluation. This is not `REVIEW_INFRASTRUCTURE_BLOCKED`. Do not infer
substantive `APPROVE` from an arbitrary `COMMENT` body.
If an observed repository/branch-protection policy explicitly requires formal platform approval,
report that concrete merge requirement separately; do not assume it from an inaccessible rule.
Owner merge authority remains separate. If the HEAD moved, repeat the fresh full review and required
checks; do not reuse its verdict.
If required evidence is temporarily unavailable, diagnose it and do not claim readiness or invent a
human gate. After bounded corrected attempts to obtain required verification evidence fail, record
the precise blocker and use `REVIEW_INFRASTRUCTURE_BLOCKED`. End with exactly one of:

- `READY_FOR_OWNER_MERGE`: exact-HEAD substantive `APPROVE` and all final evidence verified; do not merge.
- `REMEDIATION_BUDGET_EXHAUSTED`: 10/10 and no substantive `APPROVE`, with remaining blockers identified.
- `REVIEW_INFRASTRUCTURE_BLOCKED`: bounded corrected attempts could not obtain a valid independent
  review or its required verification evidence.
- `GENUINE_OWNER_DECISION_REQUIRED`: a material decision outside this issue's granted scope.

Leave the issue open and the PR unmerged. Report the ledger location, reviewed/approved HEAD,
validation and CI evidence, budget used, and exact terminal condition to the owner.
