# Local Search Rules

Use local tools for repository discovery and verification.

- Use semantic/index search for conceptual discovery such as architecture, boundaries, workflow, or
  implementation patterns.
- Use `grep` for exact identifiers, config keys, environment variables, error messages, and imports;
  use `glob` for filenames and paths.
- Search results are hints. Open the authoritative local source, contract, test, or documentation
  before editing or relying on a claim.
- Keep exploration pragmatic and proportional to the selected issue. Do not impose rigid file-read
  counts, token thresholds, or broad directory scans as workflow gates.
- After editing, use `git diff`, focused checks, and repository validation rather than rereading files
  only to confirm that a patch was applied.
- Architecture and ADR work is authoritative in this repository's `docs/architecture/` and
  `docs/adr/` directories.
