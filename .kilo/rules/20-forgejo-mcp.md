# Forgejo MCP Rules

Forgejo at `https://forgejo.creatidy.com/Creatidy/creatidy-kernel` is the canonical development
authority for this repository. The configured Forgejo MCP is used for platform operations; its
permissions must be demonstrated by successful calls and must never be assumed.

## Operation Boundaries

- Use normal Git transport for fetch, pull, branch, commit, push, and other repository object
  operations through the configured remote.
- Use Forgejo MCP for issue and comment reads/writes, PR metadata and creation, labels, reviews, and
  repository platform state when the operation exists there.
- Do not call Forgejo REST endpoints through `curl`, `wget`, or custom scripts when MCP supports the
  operation. Do not inspect local source through Forgejo MCP.
- Use the exact repository `Creatidy/creatidy-kernel`; do not copy a legacy cross-repository identity
  scope into Kernel rules.

## Retrieval And Writes

- Read only the selected issue or directly relevant PR and linked objects. Fetch the full issue body
  before implementation and the exact PR head/diff before review.
- Do not claim an issue, comment, PR, review, label, or other Forgejo mutation succeeded unless the
  MCP call succeeded. Report failed or unavailable operations explicitly.
- If an MCP call fails, change the input or strategy after diagnosis; do not repeat an identical
  failed operation blindly.
- Keep issue comments, PR descriptions, and reviews factual, concise, and free of secrets.

## Branch And Review Authority

- Branch from `develop` and target `develop` in PRs. Never target `main`.
- A PR review freezes the exact head and runs in a fresh, read-only reviewer context. The authoring
  session must not treat its own assessment as independent review.
- Do not auto-merge, force-merge, bypass branch protection, or claim that review/integration occurred
  when it did not.
