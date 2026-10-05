# Issue Delivery Workflow

<!-- Adapted from Model Intelligence for Kernel identity and issue-quality rules; see NOTICE. -->

- Outside an explicit owner `/loop`, start implementation only when Adrian selects one Forgejo issue by
  number, URL or unambiguous title. Fetch the actual issue via MCP before planning
  or editing. Summarize title, goal, scope, acceptance criteria, constraints, links
  and explicit non-goals. If selection is ambiguous, clarify; never infer work from order,
  age, labels, milestones, Projects, branches, documentation queues or memory.
- Do not create issues without explicit authorization. No Program Execution Mode,
  external controller, execution graph or generic planning framework. Autonomous
  selection is permitted ONLY by the explicit `/loop` exception below.
  An explicit owner documentation/planning mandate can authorize deduplicated
  registration of its main/gap issues; it does not authorize implementing the
  registered features. Record the actual mandate and use returned Forgejo IDs.
- `/loop` delegates successive issue selection to its sole primary invocation
  context under `.kilo/command/loop.md`, not a second controller. Refresh all open
  canonical Kernel issues each cycle; exclude exact invalid/wontfix/duplicate labels
  case-insensitively before historical PR interpretation, verify explicit gates,
  then explicit priority/required ordering/oldest registration. Open PRs are not
  planning authority. Unresolved owner decisions are ineligible; if a genuine
  decision arises for selected work, STOP_AND_ASK terminates the entire loop.
  Preserve STOP_REVISE dispositions; an open PR cannot authorize restarting them.
  No issue registration is authorized merely to keep `/loop` running.
- Verify the canonical remote, fetch current `develop`, record its exact SHA and
  inspect files/status/branches. Demonstrate access by successful operations.
  Use one normal checkout, never `git worktree` or alternate checkout management
  and only one mutator at a time. Preserve unrelated changes/branches;
  never stash/reset others' work. If unrelated changes prevent safe switching,
  stop with a precise blocker.
- Create an ordinary branch named `issue-<number>-<short-topic>` from that recorded
  fetched SHA in this checkout. Use normal Git transport. Never implement
  directly on `develop`; never target, modify, merge into or promote `main`.
- Confirm accepted scope, implement the smallest coherent change, run checks,
  inspect status/full base delta, commit only intended files, push to canonical
  Forgejo, create one PR to `develop` via MCP and post a concise issue update.
- Standalone implementation/finish/review never merge or auto-merge. Only explicit
  `/loop` authorizes the supported Forgejo PR merge to develop after fresh exact
  approval/currentness gates, verified integrated acceptance then issue closure.
  Never direct-push develop, touch main, release, promote or deploy. Outside that
  completion gate keep the issue open at handoff; use `Refs #N`,
  not automatic closing keywords. Report issue/PR, base/head SHAs, validation and
  genuine blockers; READY_FOR_REVIEW means implemented and verified, not approved.
- `/finish-pr` selects an existing PR and authorizes only its linked issue's
  accepted-scope remediation. Use a fresh foreground `pr-reviewer` native `task`
  for each frozen whole-PR review; consume its result without owner relaying.
  At most 10 whole-PR review invocations per issue delivery, including initial,
  COMMENT and retries. Persist ordinals before dispatch in excluded progress;
  finish reentry, internal phase, new task/model/session cannot reset the counter.
  Current APPROVE yields READY_TO_MERGE, never a standalone merge; only `/loop`
  may continue through its separate merge/completion gates.
  At the bound return STOP_REVISE with new defects versus incomplete fixes and
  recurring architectural/semantic patterns. Material scope/architecture decisions
  yield OWNER_DECISION_NEEDED; unavailable tools yield a precise finite BLOCKED.
  In `/loop` map OWNER_DECISION_NEEDED to STOP_AND_ASK; stop, never skip selected work.

## Issue Quality

Agent-created issues use durable `Goal`, `Why`, `Scope`, `Acceptance Criteria`, `Constraints`
and `Links` sections. Acceptance criteria must be observable and verifiable. Keep one primary
issue for repository-owned work; do not create speculative queues, status/priority taxonomies,
Projects or milestones for execution order. Default labels are optional and genuinely useful.

Authorized follow-ups must be durable, distinct from the current issue and actionable with
verifiable acceptance. Search narrowly for duplicates and link the triggering issue/PR.
Ordinary blockers are fixed in the same issue branch, not turned into follow-up issues.
