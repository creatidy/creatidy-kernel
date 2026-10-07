# Contributing

Development is canonical on [Forgejo](https://forgejo.creatidy.com/Creatidy/creatidy-kernel).
Open issues and pull requests there; GitHub is a mirror. Fork or branch from `develop`, keep changes
bounded, and target `develop`. Standalone commands do not merge. Only an explicit owner `/loop`
delegates approved Forgejo PR integration into `develop` under [AGENTS.md](AGENTS.md); promotion to
`main` and release tags remain human-controlled. An ordinary contribution does not authorize paid
model calls, production access or deployment.

## Local Gate

Install Python 3.12+ and uv, then `uv sync --locked`. `make` (or `make help`) lists development
targets without running checks or live execution. `make check` runs:

```sh
uv lock --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked basedpyright
uv run --locked lint-imports
uv run --locked pytest
uv run --locked python tools/check_secrets.py
git diff --check
```

`make package-check` runs `uv run --locked python tools/check_package.py`, building wheel/sdist and
testing a clean wheel installation. The builder and its dependencies are in the development lock;
project installation and package checks use that environment without a second isolated resolution.
`make audit` runs `uv run --locked python tools/audit_dependencies.py` and requires public
advisory/package-index access. The ordinary tests/example do not contact models or services.
Format intentional edits with `uv run --locked ruff format .`; do not weaken checks to make them pass.

### Native Verification Proof

The optional verification-only profile needs a separately provisioned unprivileged Linux x86-64
bubblewrap 0.11.0 and libseccomp 2.5.5+ plus an explicit trusted runtime closure. No root installation,
daemon or host security-setting changes are needed or performed. Native tests never download/build
dependencies. Missing native setup is explicitly skipped/unproved in normal pytest; `make native-proof`
requires `CREATIDY_TEST_BWRAP_BIN` and fails on missing enforcement instead of skipping acceptance.

For an offline build, first obtain the exact public source archive for commit
`9ca3b05ec787acfb4b17bed37db5719fa777834f` (SHA-256 in ADR 0008), and extract matching public libcap
development/runtime packages into an owned prefix without installation. Inspect upstream source
before executing its build. The repository setup tool verifies the archive, builds only the utility
using locked optional tooling, and prints provenance metadata, not a denial receipt:

```sh
uv run --locked --group sandbox-build python tools/setup_bubblewrap.py \
  --archive /path/to/pinned-source.tar.gz --output /owned/parent/new-build \
  --libcap-prefix /path/to/extracted-libcap-prefix
CREATIDY_TEST_BWRAP_BIN=/owned/parent/new-build/build/bwrap make native-proof
```

Supply the managed binary in the same closed operational environment for `make check package-check
audit` when claiming native reception. Unsupported namespace/platform/libseccomp/resource prerequisites
refuse; never relax flags or run unsandboxed to make this proof green. The initial test closure is
Ubuntu 24.04 amd64 Python 3.12; other closures need their own exact resources and native receipts.
No binary or upstream source is included in wheel/sdist. Redistributing the external utility/libraries
has separate LGPL/source compliance obligations. CI infrastructure setup and independent whole-PR
security review remain separate gates, not claims from a local build or green mocks.

Use core-owned immutable values and protocols. Adapters depend inward; core must not depend on
private systems, vendor SDKs, a forge or a coding harness. The existing application layer is shared by
the CLI compositions; future entrances must consume it rather than duplicate semantics. The allocator
API is not yet stable;
new hard constraints must be explicitly supported or rejected rather than silently dropped.

## Scope And Evidence

A0's resource proof and K1-K8 bounded foundations are historical deliveries, not the complete shared
architecture. Follow one issue selected explicitly by the owner or under owner-invoked `/loop`, and
the [coverage roadmap](docs/architecture/successor.md),
not the historical private Engine v1 brief or an inferred execution queue. Add behavior/negative tests
for each implemented invariant;
do not claim that an architecture document is an implemented control. Tests use synthetic evidence,
not credentials or private workload data. Runtime state/artifacts belong outside this source checkout.

Use existing dependencies/designs before inventing mechanisms. For substantial source reuse, verify
the exact upstream revision/file license, preserve required copyright/license/NOTICE, mark modifications
and record upstream origin. Add `NOTICE`/third-party notices when an actual obligation arises. A0 copied
no product source; the later K8 transport adaptation has attribution in `NOTICE` and must preserve it.
Dependency licenses remain with their packages. Never copy proprietary Factory.ai source or confuse that company
with the independent Apache-2.0 `watt-mind/factory` project.

Contributions are under Apache-2.0. Describe what changed, why, validation performed and limits.
Do not add mandatory central telemetry, cloud accounts, monetization restrictions, one-service-per-box
scaffolding, or a new protocol without an explicit architectural decision.
