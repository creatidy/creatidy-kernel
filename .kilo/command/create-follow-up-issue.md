---
description: Create a durable Forgejo follow-up issue for missing Kernel work
---

# Create Follow-Up Issue

Create a follow-up issue only for real missing work discovered during implementation, review, or
triage in `Creatidy/creatidy-kernel`.

## Eligibility

The proposed work must be all three:

- **Durable**: it matters after the current issue or PR.
- **Distinct**: it is not required to satisfy the current issue or already covered elsewhere.
- **Actionable**: it has a bounded outcome and verifiable acceptance criteria.

Do not create speculative, duplicate, trivial, or merely blocking-fix issues. Fix work required by
the current issue in that issue's branch instead.

## Procedure

1. Record the trigger: implementation, PR review, or triage, and the evidence for the finding.
2. Search `Creatidy/creatidy-kernel` narrowly for duplicates through Forgejo MCP. Do not create
   labels, Projects, milestones, queue taxonomies, or K-series planning systems.
3. Write an implementation-grade body using the template below. Keep the scope repository-local and
   link the triggering issue or PR.
4. Create the issue through Forgejo MCP. Use a default label only when it is genuinely useful; do
   not claim creation or linking unless the calls succeed.
5. Link the new issue from the triggering issue or PR when the platform operation is available, and
   report the exact number and URL.

## Issue Template

```markdown
## Goal
[One sentence describing the durable outcome.]

## Why
[Evidence and why it matters after the current task.]

## Scope
**In scope:**
- [bounded item]

**Out of scope:**
- [explicit non-goal]

## Acceptance Criteria
- [ ] [Verifiable condition]
- [ ] Manual verification: [human-runnable check and expected result]

## Constraints
[Architecture, authority, security, and delivery constraints.]

## Links
- Triggered by: [issue/PR URL]
- Related: [links or None]

## Follow-up Justification
- Durable: [why]
- Distinct: [why]
- Actionable: [why]
```
