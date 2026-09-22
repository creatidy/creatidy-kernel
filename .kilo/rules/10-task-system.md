# Task System Rules

Forgejo Issues in `Creatidy/creatidy-kernel` are the durable source of truth for repository work.
Documentation can explain architecture and current state, but it is not an execution queue.

## Explicit Selection

- Normal implementation requires one issue explicitly selected by the owner by number, URL, or
  unambiguous title.
- Fetch the actual issue before planning or editing. Summarize its title, goal, scope, acceptance
  criteria, constraints, links, and explicit non-goals.
- Do not infer active work from issue age or number, title order, labels, Projects, milestones,
  branch names, or prior conversation memory.
- If no issue is selected, or the issue cannot be fetched, stop before implementation.
- This repository has no Program execution exception yet. Do not select successor issues or encode
  Program state transitions in Markdown.

## Issue Quality

Agent-created issues must contain these headings:

- `Goal`
- `Why`
- `Scope`
- `Acceptance Criteria`
- `Constraints`
- `Links`

Acceptance criteria must be observable and verifiable. Keep one primary issue for work owned by this
repository. Do not create speculative issues, planning queues, status/priority taxonomies, Projects,
or milestones for execution order. Default labels are optional and must be genuinely useful.

## Follow-Ups And Completion

A follow-up issue is allowed only when the work is durable, distinct from the current issue, and
actionable with verifiable acceptance criteria. Search narrowly for duplicates first and link the
result to the triggering issue or PR.

Normal delivery is:

`selected issue -> branch from develop -> focused implementation -> checks -> PR to develop -> review -> integration`

Keep the issue open while its PR is open. Close it only after the accepted change is integrated into
`develop`. Integration is done; promotion to `main` is not part of issue completion.
