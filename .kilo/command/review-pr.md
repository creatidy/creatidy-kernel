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

Use the current clean checkout when safe. If switching would disturb another active
checkout, use one owner-authorized isolated temporary worktree and record it; do not
stash/reset unrelated work. Freeze exact HEAD/base/merge-base and refuse divergence.
Prepare the existing offline locked environment for required `make check` and
`make package-check`. Before tests/tools that observe inherited state, construct a
closed environment with only deliberate synthetic values and minimum operational
variables; do not expose ambient secrets or mount credential directories. Reviewers
inspect frozen objects read-only and never mutate the delivery. The parent must not
edit the reviewed checkout while the reviewer is active.

Classify missing task/agent/tool/runtime, public-source access, dependency, filesystem,
environment or test-execution capabilities as review infrastructure failures, not
findings or owner decisions. Diagnose and change the execution condition before a new
attempt: for example use a sanitized environment, synthetic HOME/cache/temp, clean
ephemeral container when suitable, exact pinned public source checkout, split source
inspection from test execution, or another authorized independent reviewer path.
Do not weaken isolation, permissions, validation, exact-HEAD checks or independence;
never self-review. Return a precise BLOCKED only after no authorized alternative
remains, without an artificial owner question. Distinguish this from findings and
evidence-backed reviewer judgment uncertainty.

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
