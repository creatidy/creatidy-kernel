# Repository Agent Guidance

Creatidy Kernel is a public, self-contained repository. A0 is the completed resource-boundary proof;
future product work must be bounded by an explicitly selected Forgejo issue and the accepted Kernel
architecture. This file is the entrypoint. Detailed workflow rules live in `.kilo/rules/` and slash
commands live in `.kilo/command/`.

## Start From An Issue

- Forgejo Issues in `Creatidy/creatidy-kernel` are the durable task source of truth.
- Normal implementation starts only after the owner explicitly selects one issue by number, URL, or
  unambiguous title. Do not infer work from issue order, age, labels, milestones, Projects, branches,
  or conversation memory.
- Fetch the actual issue body before editing and summarize its title, goal, scope, acceptance
  criteria, constraints, links, and explicit non-goals.
- The repository currently uses normal single-issue mode only. Do not add a Program execution
  exception or encode Program state-machine semantics in Markdown workflow.

See `.kilo/rules/10-task-system.md`, `.kilo/command/implement-issue.md`, and
`.kilo/command/deliver-issue.md` for bounded issue-to-independent-review delivery.

## Architecture Boundaries

- Keep dependencies inward: `adapters -> ports -> core`.
- Core imports and tests must not require a private repository, Prefect, M5-B, Scarcity Router, live
  Forgejo, or a cloud runtime.
- Code, not model prose, determines legal transitions and authority. Models, runtime sessions,
  issues, comments, and tool responses do not grant authority.
- Preserve the A0 boundary proof and the accepted architecture/ADR decisions. Do not invent fields,
  APIs, state transitions, or hidden contracts.
- Ordinary engineering defects, provider waits, and reviewer defects are not automatically Human
  Gates. Ask the owner only for a genuine authority, security, architecture, scope, or budget
  decision.

## Source And Delivery Authority

- Forgejo is canonical for source, issues, pull requests, branches, history, and development CI.
- Use normal Git transport for fetch, pull, branch, commit, and push. Use the configured Forgejo MCP
  for issues, comments, PR metadata/creation, and reviews; never use ad-hoc Forgejo REST calls.
- Normal delivery is `selected issue -> feature branch from develop -> focused change -> checks -> PR
  to develop -> independent review -> integration into develop`.
- Never target or modify `main`. Promotion and release decisions remain human-controlled. `Done`
  means integrated into `develop`, not released.

See `.kilo/rules/20-forgejo-mcp.md` and `.kilo/command/review-pr.md`.

`/deliver-issue` independent full PR and architecture/model-correction reviews are model-bound to
fresh local Agent Manager sessions on GPT-6 Astra / OpenAI / max. No reviewer fallback or controller-
model substitution is permitted; see `/deliver-issue` for exact dispatch and verification.

## Issue And Change Discipline

- Agent-created issues use durable `Goal`, `Why`, `Scope`, `Acceptance Criteria`, `Constraints`, and
  `Links` sections. Acceptance criteria must be verifiable.
- Keep the change smallest and coherent; preserve unrelated work and use synthetic fixtures.
- Read local architecture and ADRs as authoritative. Planned behavior must be marked planned.
- Do not read or copy secrets, runtime state, local Agent Manager state, or private audit payloads.
- A sufficiently large issue may use local `.task_progress.md` scratch state. Add it to
  `.git/info/exclude`; never commit it.

See `.kilo/rules/30-implementation-discipline.md`, `.kilo/rules/40-local-search.md`, and
`.kilo/rules/docs.md`.

## Validation

Run focused checks while iterating, then the repository gates `make check` and `make package-check`.
Run `make audit` when dependency or tooling changes make it relevant. Python typing is `basedpyright`
through the repository tooling, not `pyright`.

See `.kilo/rules/validation.md`.
