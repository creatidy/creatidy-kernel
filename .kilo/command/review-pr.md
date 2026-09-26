---
description: Conduct a read-only independent review of one Forgejo pull request
---

# Review PR

Review one pull request in `Creatidy/creatidy-kernel` against its selected issue and the Kernel
architecture. The review is read-only and never merges.

## Procedure

1. Fetch the exact PR metadata through Forgejo MCP. Record the title, author, head branch and commit,
   base branch, and changed-file count. Reject or flag any base other than `develop`.
2. Freeze the exact PR head. Identify the linked issue only from PR metadata, body, branch, or
   comments; fetch that issue's goal, scope, constraints, and acceptance criteria.
3. Fetch the changed files, full diff, existing comments, and reviews through Forgejo MCP. If the PR
   head changes, stop and restart the review against the new frozen head.
4. Use a fresh reviewer context that does not rely on the implementation session's conversation or
   verdict. Inspect local `AGENTS.md`, relevant `.kilo/rules`, README, Makefile, architecture, ADRs,
   tests, and producer contracts as needed.
5. Review the actual diff against issue intent and acceptance evidence, including architecture and
   dependency direction, correctness, authority/security/privacy boundaries, tests, documentation,
   validation, and manual verifiability. Treat PR prose as claims, not proof.
6. Classify findings by severity: blocking defect, non-blocking suggestion, author question,
   security/privacy note, test/documentation gap, or durable follow-up candidate. Missing work needed
   by the issue remains blocking or non-blocking review work, not an automatic follow-up issue.
7. Give a substantive `### Overall Recommendation` for the exact frozen HEAD, with explicit blocking
   findings (or none). Present the review or post the explicitly requested review through Forgejo MCP.
   Report the actual persisted Forgejo review state separately. If the shared PR-author account
   prevents equivalent formal submission, preserve the substantive verdict in the review body and
   record the identity limitation; a stored `COMMENT` is not a formal `APPROVED` review. Do not edit
   the branch, push commits, auto-approve your own work, auto-merge, or bypass branch protection.

## Review Format

```markdown
## PR Review: [title] (#[number])

**Repository:** Creatidy/creatidy-kernel
**Branch:** [head] -> [base]
**Frozen head:** [commit]
**Linked issue:** [issue or None/Ambiguous]

### Summary
### Intent / Acceptance Evidence
### Architecture / Information Flow
### Blocking Issues
### Non-blocking Suggestions
### Questions for the Author
### Security / Privacy Notes
### Test / Documentation Gaps
### Follow-Up Candidates
### Overall Recommendation
[APPROVE | REQUEST_CHANGES | COMMENT]
```

`### Overall Recommendation` is the independent reviewer's substantive verdict, not a transcription
of Forgejo's persisted state. Do not infer `APPROVE` from arbitrary comment text or claim Forgejo
formally approved when it recorded `COMMENT`.

Use an acceptance-evidence table when a linked issue exists. Create a follow-up only through
`/create-follow-up-issue.md` after confirming it is durable, distinct, actionable, and non-duplicate.
