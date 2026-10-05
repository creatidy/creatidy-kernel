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
  Use one normal checkout by default and only one mutator per checkout. A specific
  owner-authorized task may use one isolated temporary worktree when needed to leave
  another active checkout untouched; record the exception in the issue/ledger.
  Preserve unrelated changes/branches; never stash/reset others' work. Classify
  checkout contention as an engineering blocker and use a clean authorized checkout
  or scoped worktree when sufficient; do not escalate routine Git/environment issues.
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
  yield OWNER_DECISION_NEEDED only for genuine owner-controlled decisions. Unavailable
  tools yield a precise finite BLOCKED only after bounded diagnosis and attempts to
  use materially different authorized execution/review paths; every review dispatch
  still consumes its ordinal. A technical limitation is not permission to self-review,
  weaken criteria, or repeat the same failed setup.
  In `/loop` map OWNER_DECISION_NEEDED to STOP_AND_ASK; stop, never skip selected work.

## Blocker Classification and Remediation

- A blocker is not automatically an owner decision. Class-A engineering/execution
  blockers include missing reviewer tools/runtime, secret-contaminated ambient
  environment, need for clean isolation, inaccessible public source via one reader,
  broken dependency/tool state, unsuitable temp/worktree filesystem, unsafe test
  execution in the current process, inadequate evidence path, and diagnosable command
  failure. Diagnose then try the smallest sufficient existing authorized mechanism:
  sanitized explicit environment, synthetic HOME/cache/temp and fixture values,
  ephemeral container where useful, temporary checkout/worktree, exact pinned public
  source fetch, read-only/minimal mounts, existing locked dependency setup, split
  source verification from test execution, smaller offline reproducer, or another
  independent reviewer path. Docker is optional execution isolation, not a product
  dependency or automatic answer. Do not add persistent services.
- “No blind retries” prohibits repeating a failed operation with the same relevant
  inputs and environment; it does not prohibit changed-strategy remediation. State
  the failure diagnosis and changed condition/hypothesis in the ledger. Keep each
  attempt bounded, preserve review ordinals (including COMMENT/infrastructure
  failures), and stop at the existing finite budget. No silent acceptance/validation
  weakening, implementation self-review or substitution of implementer claims for
  independent public-source verification.
- When tests/tools can observe environment state, never expose ambient owner secrets
  to simulate inheritance. Use a closed environment with deliberate synthetic values
  and only minimum operational variables. Do not mount SSH/cloud/provider/model/
  Forge/browser/other credential directories unless the exact authorized operation
  requires them; never copy secrets into images or print values. Record only safe
  variable names/categories in diagnostics.
- A public research reviewer verifies the cited exact revision independently. If one
  access path fails, try another reader or fetch/clone that exact public revision in
  an isolated environment and preserve pin/provenance. Split source verification
  from tests if their execution requirements differ. Inability of one reviewer
  process to browse is neither an implementation finding nor grounds to accept the
  implementer's report as independent review.
- Distinguish (1) review finding: concrete evidence the change is wrong/incomplete;
  (2) review infrastructure failure: no verdict could be established; and (3) reviewer
  disagreement/uncertainty: evidence exists but its interpretation is unresolved.
  Infrastructure failure consumes its ordinal; change strategy/environment before
  another review. Use any authorized independent reviewer path satisfying the same
  isolation, exact-HEAD and full-review contract. If none remains, report a precise
  external capability blocker, not an artificial owner question.
- Class-B owner decisions include materially different architecture, authority/trust
  boundary, scope/acceptance, paid/live inference or external effects, new access to
  private credentials/sensitive data, weaker security/isolation, destructive or
  irreversible operations, changed product boundary, adopting a dependency as a
  product commitment, meaningful cost, or merge/release/deployment authority not
  already delegated. Ordinary safe reversible in-scope choices are autonomous.
- Before `STOP_AND_ASK`, record in the durable ledger: the exact unresolved decision;
  why it is owner-controlled rather than engineering; reasonable autonomous
  remediation paths considered; why those paths cannot resolve it without changing
  authority, architecture, security, scope, cost or another owner commitment; and the
  smallest materially distinct choices. Do not fabricate options where one safe
  engineering remedy is sufficient. Ask owners neither to select test mechanics nor
  diagnose routine tooling.
- `BLOCKED` is reserved for an external condition the agent cannot change or no
  authorized technical remediation path remaining (including exhausted finite
  review budget where exact approval/findings are unresolved). Report the exact
  missing capability/dependency, diagnosis, materially changed attempts and why no
  authorized workaround remains. Ask no artificial question when no owner decision
  is needed. If proceeding requires a genuine owner decision, use `STOP_AND_ASK`.

## Issue Quality

Agent-created issues use durable `Goal`, `Why`, `Scope`, `Acceptance Criteria`, `Constraints`
and `Links` sections. Acceptance criteria must be observable and verifiable. Keep one primary
issue for repository-owned work; do not create speculative queues, status/priority taxonomies,
Projects or milestones for execution order. Default labels are optional and genuinely useful.

Authorized follow-ups must be durable, distinct from the current issue and actionable with
verifiable acceptance. Search narrowly for duplicates and link the triggering issue/PR.
Ordinary blockers are fixed in the same issue branch, not turned into follow-up issues.
