---
description: Finish an existing PR through bounded native review and in-scope remediation
---

<!-- Adapted from Model Intelligence for Kernel identity, A0 and validation; see NOTICE. -->

Finish the owner-selected Forgejo PR: $ARGUMENTS

Operate in the primary implementation context. Read AGENTS.md and its rules.
This invocation authorizes only the selected PR's linked issue scope and normal
remediation commits/pushes; no additional issue, replacement PR or merge.
An explicit owner `/loop` may reuse this procedure in its sole primary context;
selection/merge/closure authority lives only in loop.md, never in `/finish-pr`.

Delivery budget: at most 10 whole-PR review invocations per issue delivery,
INCLUDING the initial review, COMMENT, invalidated reviews and corrected retries.
Before the first review verify `.task_progress.md` is Git-locally excluded, then
append a delivery ledger keyed by issue/PR (and `/loop` invocation ID if applicable).
Before EVERY task dispatch reserve/persist the next review ordinal, frozen SHAs
and pending result. Record returned verdict, remediation commits and checks.
Reinvoking `/finish-pr`, changing phase, reviewer task, model or session MUST reuse
the same delivery counter, not initialize zero. If prior dispatch/count recovery
is ambiguous or unavailable, BLOCKED. Do not erase history or reset a stopped
delivery; STOP_REVISE still requires an explicit owner decision, not a new PR/name.

1. Fetch actual PR metadata and linked issue via `forgejo-mcp`. Require an open,
   unmerged canonical Creatidy/creatidy-kernel PR targeting `develop`. Verify
   canonical remote, fetch current base/head and inspect status/branches. Use the
   normal checkout when safe; for an owner-authorized concurrent task, use one
   isolated temporary worktree rather than disturb the active checkout. Safely switch
   to the existing PR branch, or create its local
   tracking branch at the fetched PR HEAD if absent. Never create a replacement
   branch. Never stash/reset or overwrite others' work. Diagnose dirty/diverged state;
   use a clean authorized worktree when that resolves ordinary checkout contention.
   It is not an owner decision unless resolving it requires an owner-controlled action.
2. Require the current PR branch to be clean and match MCP HEAD. Freeze exact
   HEAD, base and merge base; run required `make check` and `make package-check`, prepare their offline locked
   environment and inspect
   reviewer permissions. Review frozen objects/current clean branch in this SAME
   checkout. The parent must not edit/switch branches while the reviewer is active.
   A missing native task/agent is a review-infrastructure failure: diagnose it and
   try an authorized independent read-only reviewer path with the same exact-HEAD
   contract. Never self-review, lower the contract, or use an unapproved service.
3. Spawn a fresh foreground native `task` with `subagent_type: pr-reviewer`,
   `background: false`, no `task_id`. Supply only PR URL/number, exact expected
   HEAD/base and whole-PR review instructions with this checkout path.
   Do not supply past findings, implementation reasoning or a requested outcome.
   The agent owns the result contract/model. Parse its JSON and require the exact
   frozen SHAs and valid field types/verdict. Consume returned findings directly.
   Recheck local HEAD/clean status; a changed or dirty checkout invalidates review.
4. Re-fetch PR metadata and canonical develop. APPROVE with empty findings,
   matching current HEAD/base SHAs, clean checkout, successful required validation
   and still open/unmerged develop target yields READY_TO_MERGE / APPROVE. Never merge.
   Changed HEAD/base invalidates approval. Classify every COMMENT or unusable verdict
   as stale subject, infrastructure failure, evidence-backed uncertainty or finding.
   Diagnose before continuing. An infrastructure failure consumes its ordinal but is
   not a finding; change a relevant condition (review environment, sanitized inputs,
   pinned-source acquisition, smaller offline reproducer or authorized independent
   reviewer path) before another review. A new session/model alone is not a changed
   condition. Do not repeat identical failed conditions; keep the same ordinal ledger.
5. REQUEST_CHANGES: first check findings against accepted scope and authority.
   Genuine incompatible acceptance, architecture or material scope decisions
   yield OWNER_DECISION_NEEDED with concrete alternatives. Tool, environment, public-
   source access, dependency, filesystem and command failures are engineering blockers;
   try the smallest authorized changed-strategy remediation before escalation.
   Otherwise reproduce
   each actionable finding. Before patching check the remaining delivery budget;
   at 10 return STOP_REVISE without patches that cannot receive a fresh review.
   Remediate all in scope and add regression tests.
   Classify each as direct incomplete remediation or genuinely new semantic/
   architectural defect; record that distinction and the reviewed HEAD/verdict.
6. Run focused validation and full `make check` and `make package-check`; run `make audit`
   only when relevant dependency/tooling changes require it. Inspect status, intended diff,
   full base delta, whitespace and recent commit style. Verify configured and
   effective author/committer are Adrian Tkacz <adrian.tkacz@creatidy.com>.
   Stage only intended files, commit normally and push the SAME PR branch through
   canonical Git. Never amend, squash, force-push or rewrite published history.
   Recheck remote/MCP head and return to step 2 with a NEW reviewer task.
7. Never dispatch review 11. At the 10-review bound an exact valid APPROVE may
   yield READY_TO_MERGE; genuine decisions yield OWNER_DECISION_NEEDED. An external
   dependency or exhausted authorized infrastructure alternatives yields BLOCKED,
   and remaining actionable findings yield STOP_REVISE;
   summarize recurring patterns,
   especially substantial new semantic/architectural defects in A0 proofs. Never
   reset the issue delivery's review count or patch indefinitely. The ceiling is
   not a target: stop as soon as an owner decision is clearly required.
8. COMMENT: distinguish stale/incomplete/tool failure from evidence-backed judgment
   uncertainty and findings. Do not treat first-path failure as terminal. Attempt a
   bounded remediation with a changed relevant condition and reserve the next ordinal
   before dispatch. Do not repeat the same failed inputs/environment. If no authorized
   route can establish the verdict, return BLOCKED with exact missing capability and
   remediation paths already attempted; if evidence exists but material judgment
   cannot be resolved within accepted criteria, return OWNER_DECISION_NEEDED.

Return one final report: issue/PR URLs, every reviewed HEAD/base/verdict,
review ordinals, remediation commits, final exact HEAD, validation/limitations and exactly one
terminal status READY_TO_MERGE, STOP_REVISE, OWNER_DECISION_NEEDED or BLOCKED.
Optional concise final issue/PR comments are reporting only; reviewer task results
are the handoff. Do not require owner relaying, Forgejo review publication,
external orchestration, release, deployment or main promotion.
When reused by `/loop`, READY_TO_MERGE is internal; map OWNER_DECISION_NEEDED to
STOP_AND_ASK and stop the entire invocation. Other blockers/bounds terminate the
loop too, never skip to another issue. Merge/completion remain loop.md's procedure.
