# Canonical Delivery Contract

Reference: Scarcity Router's inspected `docs/release-engineering.md` and
`.forgejo/workflows/ci.yml` at `8ae1fdb5ea9090604c26fd81f23010ad85a1536d`. Reuse its authority split,
stable check name and pinned-action discipline, not its full distribution pipeline.

## Authority And Bootstrap

- Canonical: <https://forgejo.creatidy.com/Creatidy/creatidy-kernel>.
- Mirror: <https://github.com/creatidy/creatidy-kernel>.
- Ordinary work branches from and targets `develop`; the initial empty repository requires a root
  bootstrap before that normal branch/PR flow exists. This does not authorize `main`, tags or release.
- Forgejo owns source/issue/PR decisions and development CI. GitHub accepts confidential security
  intake and points normal contributions to Forgejo. Never merge mirror PRs as an alternate workflow.

## CI In The Repository

`.forgejo/workflows/ci.yml` is `ci`, job/status context `check`, on PRs targeting `develop` and pushes
to `develop`. It uses pinned checkout/setup-uv actions, pinned uv, locked Python tooling and
`persist-credentials: false`. It calls `make check`, `make package-check` and `make audit`; it references
no configured provider/private secrets and performs no publication or deployment. No duplicate test
list, GitHub development CI or
release workflow is included in A0.

Forgejo [ignores GitHub's `permissions` key](https://forgejo.org/docs/v16.0/user/actions/github-actions/#known-list-of-differences).
It is intentionally omitted rather than tested as an enforced read-only guarantee. Automatic job-token
scope is event/version dependent: documented fork-PR restrictions do not imply read-only push or
same-repository jobs. Push validation runs maintainer-controlled source. Checkout cleanup prevents
persisted Git credentials but does not prove token denial to subprocesses. The source tests check
triggers, pins and absence of configured secret references, not deployed credential containment.

The local gate checks formatting, lint/security rules, strict basedpyright, inward imports, synthetic
unit/boundary tests, license/metadata and documentation-link hygiene, plus detect-secrets without
remote credential verification. The scanner selects Git-visible tracked/untracked source files and
does not walk ignored local state. Package validation builds wheel/sdist, compares rebuilt package
contents, validates license/runtime-dependency metadata and smoke-installs outside the source tree.
Hatchling and its dependency closure are in `uv.lock` and the audit export. uv's per-project no-build-
isolation setting installs that locked builder before building the editable project; both package
builds also explicitly use the same interpreter/environment without isolated resolution. Dependency
auditing contacts public advisory infrastructure; no runtime telemetry is introduced.

## External Activation

Repository files are not proof of external settings or a live green run. At initial audit both public
repositories already existed and were empty. The configured Forgejo MCP identity could read Kernel
but could not administer it; do not claim metadata/protections were changed from a failed API call.
The completion report records the final source revision. The initial snapshot
`b85d7c2d829a40f3747a3ed28235a657be9c267d` was published via normal Git transport to canonical
`develop` and `a0/foundation`; Forgejo selected `develop` as default. Its
[CI run](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/actions/runs/1) passed. Identical GitHub
heads arrived automatically without a direct GitHub source push, demonstrating mirror propagation,
not an administrative inspection of the mirroring mechanism or credential scope. GitHub defaults to
`develop`, identifies Forgejo in its description/homepage, has normal issues/wiki/projects disabled,
and has private vulnerability reporting enabled. No `main` or release tags were created.

Required operator configuration, without storing credentials in Git:

1. Canonical `develop` is the integration/default branch after bootstrap. Protect it with PR review,
   required `check`, no force push and no history rewriting. `main` remains human-controlled.
2. A Forgejo `ubuntu-latest` runner must be ephemeral/unprivileged per job, with no host/Docker socket,
   no private-network access and no provider/deployment/publishing secrets. Public contribution code
   is hostile. Verify event-specific automatic-token scope and isolation on the deployed Forgejo/
   runner, including filesystem/env/context exposure, before accepting untrusted contributions. A
   passing run or `persist-credentials: false` does not establish those controls. Administrative access
   was unavailable in A0, so branch protection and runner/token containment remain unverified.
3. Use Forgejo's existing one-way push-mirror facility to the approved GitHub repository. Configure
   the destination-scoped credential outside this repository; mirror canonical branches and authorized
   tags, not issues or a second development workflow. Do not build a new mirroring service. Current
   MCP tools expose no push-mirror configuration operation; activation must use the trusted owner UI.
4. Set the mirror description/homepage to identify Forgejo authority, disable normal issues/wiki/
   projects, direct PRs to Forgejo and enable private vulnerability reporting for `SECURITY.md`.
   Verify exact canonical/mirror branch SHAs after activation, not merely a successful sync request.

These are finite operational setup steps, not architectural owner decisions. Never copy another
product's mirror credential or imply its successful synchronization proves Kernel is configured.

## One Future Release Authority

After explicit owner release authorization: reviewed `develop` -> human promotion to canonical `main`
-> human SemVer tag -> mirror -> one artifact publisher. Reuse the existing OSS checks for tag/version
agreement, canonical tag/commit equality, reachability from stable `main`, exact-commit builds,
validated final bytes/checksums and provenance. Privileged publishing never runs from a PR. GitHub
distribution is replaceable; its disappearance does not affect Program data or canonical history.
Do not add PyPI, signing, OCI, Windows installers or automatic version bumps to this empty foundation.
