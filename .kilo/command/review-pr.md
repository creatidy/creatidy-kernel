---
description: Fresh read-only whole-PR review through the native pr-reviewer subagent
---

<!-- Adapted from Model Intelligence for Kernel identity and validation; see NOTICE. -->

Review the exact Forgejo PR: $ARGUMENTS

Run in the primary context; do not require a separate owner-opened session.
Fetch actual PR metadata and its linked issue through `forgejo-mcp`. Require an
open, unmerged canonical Creatidy/creatidy-kernel PR targeting `develop`.
Read AGENTS.md and its rules, verify canonical remote, fetch current Git objects,
freeze exact HEAD/base/merge-base and inspect status/branches. Do not edit the PR.

Use ONE normal checkout: safely switch to the fetched PR branch if needed, creating
its local tracking branch at fetched HEAD only if absent. Require clean status and
exact frozen HEAD; refuse local divergence rather than rewriting a branch.
Never stash/reset unrelated changes;
if they prevent safe switching, report a precise blocker. No git worktree,
additional checkout or alternate checkout management. Prepare the offline locked
environment for required `make check` and `make package-check` here; review frozen
Git objects/current clean branch read-only. The
parent must not edit/switch while the reviewer is active. Locate `pr-reviewer`.
If native task/agent or required access is unavailable, report a finite blocker as BLOCKED;
never self-review instead.

Invoke `task` with `subagent_type: pr-reviewer`, `background: false`, no `task_id`.
Pass only the PR number/URL, expected HEAD/base and fresh whole-PR review
instructions including this checkout path. Do not pass implementation
reasoning, previous findings or desired verdict. The agent definition owns the
JSON result contract and GPT-6.1 Sol High selection. Never resume a past reviewer.

Require JSON fields reviewed_head, reviewed_base, verdict, findings, limitations,
checks_run as defined in `.kilo/agents/pr-reviewer.md`. Validate their types and
exact frozen SHAs. Recheck local HEAD/clean status and MCP before reporting. A
changed HEAD/base, dirty checkout, malformed result or mismatch cannot support
approval; report COMMENT with the precise limitation. Present findings in severity
order and exactly one verdict.
Do not remediate, commit, push, publish a formal review, merge or modify Forgejo
state. The returned task result is the handoff, not an owner copy or PR comment.
