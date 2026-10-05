# Validation and Handoff

<!-- Adapted from Model Intelligence for Kernel paths and existing final gates; see NOTICE. -->

- Python 3.12+, uv, src layout and typed `creatidy_kernel`. Prefer standard
  library and minimal dependencies. Maintain uv.lock with intentional dependency
  changes; do not regenerate it to conceal a locked-sync failure.
- Final gates are `make check` and `make package-check`. Makefile owns locked tooling,
  Ruff format/lint, basedpyright, import boundaries, deterministic local pytest,
  secret scanning, `git diff --check`, reproducible wheel/sdist and installed-package smoke checks.
  Do not replace basedpyright with pyright or a weaker substitute, duplicate the detailed check
  list or bypass any gate. Focused commands may diagnose before the gates.
  Run `make audit` for relevant dependency/tooling changes or a complete CI-equivalent gate.
- Tests use synthetic fixtures, controlled time and no live network/private
  accounts. SQLite tests need a supported native local Linux filesystem; use the existing
  `CREATIDY_TEST_STORAGE_DIR` override only when the default temporary mount is unsupported.
- Before commit inspect `git status`, intended diff, recent commit style and the
  complete delta against the recorded develop SHA. Stage explicit intended files;
  exclude secrets, credentials, runtime state, caches and scratch. After commit
  verify exact head, base ancestry and final delta/status. Never undo others' work.
  Run `git diff --check` when not already covered by `make check`.
- Handoff includes issue/PR URLs, exact base/head SHAs, substantive files, exact
  executed validation/results and genuine unresolved decisions/blockers. Distinguish
  change-caused failures from unrelated/environmental blockers; no claim
  of passing checks, push or PR creation without successful evidence.
- Parent uses the current normal checkout on the exact clean PR HEAD and prepares
  its offline locked development environment before invoking a reviewer. No
  additional checkout or branch switching during review. Reviewer verifies HEAD
  and clean status before/after checks and inspects frozen Git objects/full base
  delta. Ignored validation artifacts are allowed; tracked-file edits and
  Git/Forgejo mutations are not. Read checks before running them; permission
  allowlists do not make arbitrary repository code safe. Preserve unrelated work.
- `/finish-pr` records each reviewed HEAD/base/verdict, normal remediation commits,
  regression/check results and final currentness. Only an exact matching native
  reviewer result plus a final MCP currentness check can yield READY_TO_MERGE.
