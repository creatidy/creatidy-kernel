# Validation Rules

Run focused checks while iterating, then use the repository-owned gates:

- `make check`
- `make package-check`

Run `make audit` when dependency or tooling changes make it relevant, or when the complete
CI-equivalent gate is requested. The Makefile owns the detailed check list; do not duplicate or
silently replace it. Python static checking is `basedpyright`, invoked by the repository tooling,
not `pyright`.

Before handoff, inspect `git status` and `git diff`, run `git diff --check` when it is not already
covered by `make check`, and report exact commands and outcomes. Add focused tests for changed
behavior when practical. If a check fails, distinguish a change-caused failure from an unrelated or
environmental blocker and do not claim the gate passed.
