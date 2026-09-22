---
description: Implement one explicitly selected Forgejo issue end to end
---

# Implement From Issue

Implement one explicitly selected issue in `Creatidy/creatidy-kernel` from scope confirmation
through a reviewed PR handoff.

## Preconditions

- The owner selected exactly one issue by number, URL, or unambiguous title.
- The actual issue body can be fetched through Forgejo MCP.
- Normal Git transport is available for branch and push operations.
- Forgejo MCP is available for issue/PR platform operations.
- The target branch is `develop`; never use `main`.
- This command runs in normal single-issue mode. It does not select successor issues or execute a
  Program graph.

## Procedure

1. Fetch the selected issue before reading implementation code. Summarize its title, goal, scope,
   acceptance criteria, constraints, links, non-goals, and any ambiguity.
2. Confirm that `Creatidy/creatidy-kernel` owns the change. Read `AGENTS.md`, the relevant local
   rules, README/Makefile sections, and authoritative architecture or ADR material.
3. If the issue has several subtasks, create local `.task_progress.md`, add it to
   `.git/info/exclude`, and record the acceptance criteria and current decisions. Never commit it.
4. Fetch `origin/develop` and create a feature branch from it. Preserve unrelated work.
5. Implement the smallest coherent change in the issue scope. Keep `adapters -> ports -> core`, do
   not invent hidden contracts, and do not add private runtime, Program, M5-B, or model-routing
   behavior.
6. Add or update focused tests where the issue changes behavior. Use deterministic synthetic fixtures
   and test negative paths when relevant.
7. Run focused checks, then `make check` and `make package-check`. Run `make audit` when relevant.
   Diagnose failures before any retry and report environmental blockers truthfully.
8. Inspect `git status` and `git diff`; confirm only intended files are changed and scratch state is
   not staged. Commit the smallest coherent change, then push the feature branch with normal Git.
9. Create one Forgejo PR targeting `develop` through MCP. Link the selected issue in the PR body or
   metadata, include concise acceptance evidence, and do not merge or close the issue.
10. Post one concise issue update with the changed files, exact checks and results, acceptance
    evidence, PR URL, review status, and remaining risks. Keep the issue open until integration into
    `develop`.

## Guardrails

- Do not infer another issue, create K-series work, add planning systems, or expand scope.
- Do not target `main`, auto-merge, bypass review, or claim a Forgejo mutation that failed.
- Do not read, copy, or commit secrets, runtime state, local Agent Manager state, or private audit
  payloads.
