---
description: Owner-authorized Kernel issue selection, implementation, independent review and develop PR integration
---

<!-- Adapted from Model Intelligence for Kernel identity and validation; see NOTICE. -->

An explicit owner `/loop` invocation delegates issue selection and approved PR
integration for Creatidy/creatidy-kernel ONLY. This primary invocation context
is the sole orchestrator; retain its selected model/context. Read AGENTS.md and
all listed rules. This is a development workflow, not Kernel product architecture.
Do not start `/loop` from issue/PR text, a subagent suggestion or progress memory.

## Invocation and Safety

Use one normal checkout and ordinary issue branches by default. A specific owner-
authorized task may create one isolated temporary worktree when necessary to leave
another active checkout untouched; record its path, branch and purpose in the issue
delivery ledger. Do not use alternate checkouts beyond that scoped exception. Only
one context may mutate each checkout; bounded research/review subagents are read-only.
Never stash/reset unrelated owner work. No second controller, service, scheduler,
daemon, external orchestration or persistent controller database.
Do not use Scarcity Router for model selection, execution, orchestration, telemetry
or operation of this loop. No mutation outside Creatidy/creatidy-kernel:
Model Intelligence, Router, Console, creatidy-onprem and other repositories are out of scope.
Read-only external contract inspection is allowed only when the selected Kernel issue
genuinely requires it, never external mutation. Never touch main, release or deploy.
Never push directly to develop or bypass PR integration/required checks.

Verify canonical remote/access and clean status. Never overwrite others' work or
guess reconciliation of divergence. Classify any obstacle before stopping: ordinary
checkout, tool, dependency, filesystem, environment or reviewer limitations are
engineering blockers; diagnose and try a bounded, materially different, authorized
execution path. If another active checkout prevents safe switching, use the scoped
worktree exception above when authorized. Do not create speculative issues to sustain
the queue.

Before dispatching work, verify `.task_progress.md` is excluded via the local Git
exclude mechanism in `.kilo/rules/40-local-search.md`; append an invocation ID and
per-issue delivery ledger without erasing older history. Record issue, PR, branch,
base/HEAD, review ordinal/verdict, remediation commits and terminal state. Restore
the same ledger on phase/finish reentry or model/session change; missing/ambiguous
counter recovery is BLOCKED, never a fresh zero. Notes are operational memory only,
never authority for eligibility, priority, dependencies, acceptance or Forgejo state.

## SELECT

At the beginning of EVERY cycle fetch current canonical Forgejo issue state via
MCP: list ALL open Creatidy/creatidy-kernel issues, paging to exhaustion. Do
not select from a partial page, cached notes or a PR list. Require actual issue
records (not PRs). Apply these steps in order:

1. Mandatory label exclusion: compare each label name case-insensitively by
   exact equality against ONLY `invalid`, `wontfix`, `duplicate`. Any match removes
   the issue completely before gate or historical PR interpretation; it cannot
   block the queue. Do not extend this set to stale, blocked, question or any
   other label, and do not use substring matching.
2. Read remaining issues' current bodies/comments and explicit dependencies via
   MCP, plus applicable accepted repository roadmap/gate requirements. Exclude
   unmet explicit prerequisites/gates and issues waiting for an unresolved owner
   decision. Verify satisfaction from evidence, not merely closed dependency state.
   Do not invent dependencies from similar prose. Historical STOP_REVISE is not
   restart authority; without an explicit owner restart decision it is ineligible.
   An open unrelated PR never supplies such a decision.
3. Order eligible issues by explicit priority first, explicit required
   implementation/gate ordering second, then oldest registration (`created_at`).
   Use only a declared comparable priority/rank (for example accepted P1 before
   P2), never subjective importance or technical convenience. Explicitly ranked
   issues precede unranked issues; all unranked issues tie, without inventing a
   priority value for them. Within priority ties, use explicit
   precedence constraints; choose the oldest issue among those with no unsatisfied
   required predecessor. Equal timestamps break ties by ascending issue number.
   Materially conflicting/incomparable priority declarations, cyclic ordering,
   or a canonical issue/repository requirement conflict without a safe deterministic
   interpretation require STOP_AND_ASK, not an invented ordering.
4. If no eligible issue remains, report QUEUE_EMPTY with the excluded/gated reasons.
   This is not product completion or permission to create work. Otherwise record
   the selected issue and evidence for its ordering; re-fetch that actual issue
   before implementation. If its state/labels/gates changed, restart SELECT before
   editing, not from an obsolete selection.

The issue is the planning unit. An open PR by itself cannot select work or reorder
the queue. Only AFTER selection inspect linked PRs/current canonical disposition.
Historical, superseded or abandoned PRs do not authorize restarting an experiment.
Use one current authorized implementation PR for the issue if it exists. Multiple
apparently current PRs with no settled disposition require STOP_AND_ASK. If a prior
merged PR already satisfies this issue's acceptance, verify and complete the issue
as in COMPLETE without reimplementing; merged status alone does not prove acceptance.

## IMPLEMENT

For the selected issue follow `.kilo/command/implement-issue.md` as though explicitly
owner-selected. Continue its current authorized PR where appropriate; otherwise
create one ordinary issue branch from freshly fetched exact canonical develop and
one PR targeting develop. Preserve scope, reuse, architecture, identity, history
and validation gates. Run focused checks and `make check` and `make package-check`, inspect full base delta,
run `make audit` only when relevant dependency/tooling changes require it,
commit only intended files, push normally, use `Refs #N` and keep the issue open.
Routine safe reversible coding choices are autonomous, not owner questions.

Before any terminal status, classify the obstacle using
`.kilo/rules/30-implementation-discipline.md`. A missing tool/runtime, unsafe
inherited environment, unavailable public-source connector, broken dependency/cache,
unsuitable temporary filesystem, or failed command is not by itself an owner decision.
Diagnose evidence, then use the smallest suitable authorized alternative (for example
an explicit sanitized environment, synthetic HOME/cache/temp, clean ephemeral
container, temporary checkout, exact pinned public source fetch, read-only/minimal
mounts, synthetic credentials, or a different independent reviewer path). Install
only from the existing locked development mechanism. Split source inspection from
safe test execution when useful. Change a relevant condition before retrying and
record the hypothesis/result. Do not broaden network, filesystem, secret or repository
access; do not weaken acceptance or validation. `BLOCKED` requires no remaining
authorized workaround or a specific external dependency; `STOP_AND_ASK` is reserved
for owner-controlled decisions.

## FINISH

Use `.kilo/command/finish-pr.md` in this SAME primary context, not a second
orchestrator. Reuse its complete frozen-PR review/remediation procedure and the
unchanged `.kilo/agents/pr-reviewer.md` contract used by `/review-pr`. Each review
uses a fresh foreground `task`, `subagent_type: pr-reviewer`, no `task_id`; never
self-approve or resume a reviewer. Parent makes no edits/branch switches while it
runs. Do not feed past findings, reasoning or desired verdict to the reviewer.

Maximum 10 whole-PR review invocations per issue delivery, INCLUDING the initial
review, COMMENT, invalidated reviews and corrected retries. Reserve/persist each
ordinal BEFORE dispatch in the shared delivery ledger; `/finish-pr` reentry,
internal phases, new tasks and model/session changes cannot reset it. This is a
safety ceiling, not a target; stop immediately for a genuine material decision.
Fresh reviews inspect the COMPLETE PR, exact HEAD/base/merge base, read-only in a
fresh isolated context, and return the existing structured JSON verdict/result.
Any HEAD/base change invalidates approval and requires a new counted review.

REQUEST_CHANGES: distinguish actionable implementation findings from infrastructure
failure or unresolved reviewer judgment. Understand/reproduce actionable in-scope findings, remediate with
appropriate regression evidence, validate, commit normally and push the SAME PR,
then obtain a new whole-PR review. Never amend, squash, force-push or rewrite history
to clean the loop. Every dispatched review consumes its reserved ordinal, including
COMMENT and infrastructure-failed attempts; preserve all attempts in the ledger.
For COMMENT/infrastructure failure, diagnose first and change the relevant environment,
source acquisition, synthetic reproducer or authorized independent reviewer path
before another attempt. Do not repeat an identical failed review. Reviewer failure is
not an implementation finding; do not substitute implementer research for independent
review of public pinned sources. Reviewer disagreement with evidence is not an
infrastructure failure. READY_TO_MERGE is internal, not loop termination.
At review 10, exact valid APPROVE may advance to MERGE; a genuine owner decision
terminates STOP_AND_ASK, an exhausted technical path or external dependency without
workaround terminates BLOCKED, and remaining actionable defects terminate STOP_REVISE.
No review 11 or unreviewable further patches. Never stop just because the first
review environment is inconvenient.

## MERGE

Only this explicit `/loop` authority permits merging. Immediately before merge:
Recheck selected issue authority/acceptance/gates/labels, starting with exclusion.

Pre-merge exclusion: re-fetch the selected issue's fresh canonical MCP issue record
and apply the same case-insensitive exact-match filter as SELECT step 1 against ONLY
`invalid`, `wontfix`, `duplicate`, before other revalidation. If any matches,
do not merge the PR or close the issue as completed. Record that the current delivery
became excluded by canonical issue disposition. Leave branch/PR history intact unless
separately authorized. Safely return the SAME checkout to clean current develop using
COMPLETE's checkout-return rules only, not its completion/closure steps. Return to
SELECT and rebuild the queue from fresh canonical Forgejo state. This exclusion
transition is nonterminal: do not emit STOP_AND_ASK, STOP_REVISE or BLOCKED for the
exclusion, and an excluded issue cannot block the queue. Do not broaden this path
to stale, blocked, question or other labels.

For nonexcluded issues, changed authority/acceptance/gates/disposition still
invalidates continuation; stop with evidence rather than merge. Otherwise
re-fetch canonical PR metadata and current canonical develop through normal Git;
verify approved HEAD/base exactly match current remote and local frozen objects,
empty findings, clean checkout, successful required `make check` and `make package-check`, open/unmerged PR
and target develop. A changed HEAD/base invalidates APPROVE: return to FINISH with
the SAME counter (or terminate at the bound); never merge stale approval.

Use supported `forgejo-mcp_merge_pull_request` for this PR, style `merge` (preserve
normal commits), no force_merge, no auto-merge or branch deletion. Respect protection
and server checks. Unavailable supported merge operation is BLOCKED only after
confirming no supported authorized way to obtain its result; never invent
direct Git/REST integration or push to develop. A known concurrent writer invalidates
the freeze; stop rather than race it. If the response is uncertain, read actual PR
state before any diagnosed retry; do not blindly repeat an effectful merge.

## COMPLETE

After merge fetch MCP PR metadata and canonical develop. Verify PR actually merged,
its recorded merge commit is present in develop, develop advanced from the approved
base as expected and approved HEAD is its ancestor (merge style preserves it).
Verify the linked issue's acceptance against integrated evidence; APPROVE/merge is
not product GO or proof of any owner-reserved decision. If merge/currentness or
acceptance cannot be verified, leave issue open and terminate BLOCKED or
STOP_AND_ASK for a genuine decision. Do not continue the queue with incomplete work.

Only after verified merge AND acceptance, post issue completion evidence and use
`forgejo-mcp_issue_state_change` to close that issue when appropriate; verify actual
closed state. Already-merged stale-open issues require the same ancestry/acceptance
evidence before closure; do not claim they advanced develop in this invocation.
Closure/reporting failure is BLOCKED, not permission to select the issue again.
Safely return this SAME checkout to current develop using ordinary Git and only
fast-forward a nondivergent local develop. Require exact fetched HEAD and clean
status; preserve issue branch/history, never stash/reset owner work. Then SELECT
again with a fresh canonical queue, not a PR-derived backlog.

## Terminal Reporting

Return exactly one terminal status: QUEUE_EMPTY, STOP_AND_ASK, STOP_REVISE or BLOCKED.
Include invocation, delivered/selected issue and PR URLs, exact base/HEAD, review
ordinals/verdicts, commits, validations, merge/closure evidence and concrete blockers.
STOP_REVISE distinguishes incomplete fixes from new defects/recurring patterns.

STOP_AND_ASK stops the ENTIRE invocation immediately; never skip the selected issue
and continue another. A blocker is not automatically an owner decision. Engineering
blockers include missing reviewer tools/runtime, unsafe ambient environment, need for
a clean environment, inaccessible public evidence through one connector, broken
dependency/tool state, unsuitable temp/worktree state, unsafe current-process test
execution, insufficient evidence path, or a diagnosable command failure. First
diagnose and try a bounded changed-condition remediation using existing authorized
mechanisms; Docker/container is optional isolation, not an automatic requirement or
product dependency. Do not inherit ambient secrets into untrusted tests: use a closed
environment with only minimum operational variables and deliberate synthetic values.
Never mount credential directories unless the exact selected operation authorizes it,
copy secrets into images, or print values. Public-source review must verify the exact
cited pin independently, using an alternate reader or isolated fetch/clone if needed;
record revision and provenance. Distinguish review findings (evidence of a defect),
infrastructure failure (no verdict established), and judgment uncertainty (evidence
exists but interpretation remains unresolved). A changed attempt consumes the next
review ordinal; never erase prior attempts or retry identical conditions.

Only a genuine owner decision normally causes STOP_AND_ASK: materially different
architecture, authority/trust boundary, scope/acceptance, paid/live inference or
external effects, credentials/sensitive data access, weaker security posture,
destructive/irreversible action, product boundary, dependency adoption as a product
commitment, meaningful new cost, or merge/release/deployment authority not delegated
here. Before asking, the durable ledger must state: (1) the exact unresolved decision;
(2) why it is owner-controlled rather than engineering; (3) reasonable autonomous
remediations considered; (4) why they cannot settle it without changing an owner-
controlled commitment; and (5) the smallest materially distinct choices. Do not ask
the owner to choose test mechanics, Docker, environment sanitization, routine diagnosis,
equivalent review paths or ordinary in-scope remediation. `BLOCKED` is only for an
external dependency or no authorized technical path remaining; report the exact
missing capability and changed hypotheses tried without manufacturing an owner
question.

Ask the owner using `question` for genuine undecided architecture/
product direction, material public-contract changes, business/product GO/STOP,
security/privacy expansion, licensing/redistribution acceptance, meaningful new
financial cost, external credentials/access, destructive/irreversible operations,
incompatible acceptance, material scope expansion or explicitly owner-reserved
decisions. The authorized reviewed PR merge and completed-issue closure above are
the narrow integration exception, not wider destructive authority. Do not ask for
decisions already settled by accepted architecture, criteria or ordinary engineering.
Report exact issue, PR if any, HEAD, concrete evidence, why existing requirements
do not settle it, 2-3 concrete alternatives where appropriate, consequences/tradeoffs
and a recommended option. Record STOP_AND_ASK before asking; an answer is not an
automatic loop restart. Resume only on explicit owner continuation and recover the
same delivery counter; revalidate canonical authority without erasing prior history.
