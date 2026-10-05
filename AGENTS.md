# Repository Agent Guidance

<!-- Adapted from Model Intelligence for Kernel identity and product boundaries; see NOTICE. -->

Creatidy Kernel is a public, self-contained repository. A0 is the completed resource-boundary proof;
future product work follows the accepted Kernel architecture and one authorized Forgejo issue at a
time. Read README.md, docs/architecture/successor.md and
docs/adr/0007-shared-harness-routing-observability.md, plus these rules before work; do not rely on
automatic discovery of nested rule files:

- `.kilo/rules/10-task-system.md`: owner selection, branch and delivery gates.
- `.kilo/rules/20-forgejo-mcp.md`: repository authority and tool boundaries.
- `.kilo/rules/30-implementation-discipline.md`: product, reuse and scope limits.
- `.kilo/rules/40-local-search.md`: evidence and local working context.
- `.kilo/rules/validation.md`: repository-owned checks and handoff evidence.
- `.kilo/rules/docs.md`: documentation scope and public claims.

Commands: `/implement-issue <number|URL|unambiguous title>`,
`/finish-pr <Forgejo PR number|URL>`, `/review-pr <Forgejo PR number|URL>` and `/loop`.
A plain `Implement issue #N` follows the implementation workflow. `/finish-pr`
authorizes bounded in-scope remediation on the selected PR, not additional issues
or merging. It consumes native `task` results directly, with at most 10 whole-PR
review invocations per issue delivery, including initial/COMMENT/retries; its
excluded progress ledger preserves the count across finish/phase/session reentry.
`/review-pr` is standalone read-only review using the same
`.kilo/agents/pr-reviewer.md` agent. A newly spawned reviewer subagent's isolated
context satisfies independence; implementation self-review and resumed reviewers
do not. Every changed HEAD/base requires a fresh whole-PR review. Forgejo review
publication is optional, never orchestration state. No external controller exists.

Only an explicit owner `/loop` invocation delegates autonomous selection and
approved PR merge to develop/completed-issue closure. Its primary context is the
sole orchestrator, reusing implement-issue/finish-pr and the unchanged reviewer.
Read `.kilo/command/loop.md`: refresh all canonical open issues each cycle, exclude
exact invalid/wontfix/duplicate labels case-insensitively, verify explicit gates,
then order by explicit priority, required ordering and oldest registration.
Issues, not open PRs, are planning authority. Stop the entire invocation for
STOP_AND_ASK, STOP_REVISE or BLOCKED; no eligible issues yields QUEUE_EMPTY.
This does not authorize product GO, cross-repository mutation, Scarcity Router
operation, main, releases or deployments. Standalone implementation remains
owner-selected and unmerged; standalone review remains read-only.

## Architecture Boundaries

- Keep dependencies inward: `adapters -> ports -> core`.
- Core imports and tests must not require a private repository, Prefect, M5-B, Scarcity Router, live
  Forgejo, or a cloud runtime.
- Code, not model prose, determines legal transitions and authority. Models, runtime sessions,
  issues, comments, and tool responses do not grant authority.
- Preserve the A0 boundary proof and accepted architecture/ADR decisions. Do not invent fields,
  APIs, state transitions, or hidden contracts.
- Kernel controls existing harnesses and owns task acceptance; Router owns product execution
  selection/admission/gateway, MI owns external knowledge, Console consumes owner-served state.
  Keep agreed targets, revision-verified behavior, proposals and proof gaps distinct.
- `.kilo/` is development context, not product runtime. The development loop does not implement a
  Kernel Program executor or change product Router ownership. Do not use Scarcity Router for model
  selection, execution, orchestration, telemetry or operation of this development workflow.
- An explicit owner documentation/planning mandate may authorize issue registration; it does not
  select those functional issues for implementation. A registered gap is not execution authority;
  documentation approval is not live readiness, product GO or closure of functional requirements.
- Do not read or copy secrets, runtime state, local Agent Manager state, or private audit payloads.

Use one normal checkout and ordinary issue branches by default. A specific owner-
authorized task may use one isolated temporary worktree when necessary to preserve
an active checkout; record that exception in its issue/ledger. Only one context may
mutate a given checkout. Review exact frozen Git objects/current clean PR branch
read-only; the parent must not edit it while the reviewer task runs. Never stash/reset
unrelated changes to make branch switching possible.

`.kilo/command/*` and `.kilo/agents/*` are loaded by the Kilo workspace runtime;
availability is not dynamically guaranteed when files appear. After adding
or changing commands, a VS Code/Kilo workspace reload may be required. The current
repository checkout supplies its local commands/agents/rules. A missing native
agent/task is a review-infrastructure blocker to diagnose and remediate through an
available equivalent independent, read-only review path before declaring BLOCKED;
it is never permission to substitute parent self-review or external orchestration.
