# Documentation Rules

- `README.md`, `AGENTS.md`, `.kilo/`, and repository-local contribution/security documents describe
  how this repository is used and developed.
- `docs/architecture/` and `docs/adr/` are the authoritative public Kernel architecture and decision
  records. Do not redirect Kernel architecture or ADR work to another repository.
- Use ADRs for expensive-to-reverse architecture, authority, boundary, or persistence decisions;
  keep ordinary implementation notes in the issue, PR, or relevant local guidance.
- Mark proposed or deferred behavior as planned. Do not describe successor-plan behavior as live
  implementation.
- Keep documentation claims aligned with actual code, tests, Makefile targets, and Forgejo authority.
  Do not add product, runtime, or private-workflow policy merely because it exists elsewhere.
