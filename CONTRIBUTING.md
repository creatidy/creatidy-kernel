# Contributing

Development is canonical on [Forgejo](https://forgejo.creatidy.com/Creatidy/creatidy-kernel).
Open issues and pull requests there; GitHub is a mirror. Fork or branch from `develop`, keep changes
bounded, and target `develop`. Human maintainers control integration, promotion to `main` and release
tags. An ordinary contribution does not authorize paid model calls, production access or deployment.

## Local Gate

Install Python 3.12+ and uv, then `uv sync --locked`. `make check` runs:

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
testing a clean wheel installation. `make audit` runs the dependency vulnerability check and requires
public advisory/package-index access. The ordinary tests/example do not contact models or services.
Format intentional edits with `uv run --locked ruff format .`; do not weaken checks to make them pass.

Use core-owned immutable values and protocols. Adapters depend inward; core must not depend on
private systems, vendor SDKs, a forge or a coding harness. The first real application layer will be
shared by CLI/REST/MCP, not copied into each entrance. The prototype allocator API is not yet stable;
new hard constraints must be explicitly supported or rejected rather than silently dropped.

## Scope And Evidence

A0 deliberately does not implement a Program engine. Follow the bounded successor plan rather than
the historical private Engine v1 brief. Add behavior/negative tests for each implemented invariant;
do not claim that an architecture document is an implemented control. Tests use synthetic evidence,
not credentials or private workload data. Runtime state/artifacts belong outside this source checkout.

Use existing dependencies/designs before inventing mechanisms. For substantial source reuse, verify
the exact upstream revision/file license, preserve required copyright/license/NOTICE, mark modifications
and record upstream origin. Add `NOTICE`/third-party notices when an actual obligation arises. A0 has
no copied upstream product source and therefore no third-party source NOTICE requirement. Dependency
licenses remain with their packages. Never copy proprietary Factory.ai source or confuse that company
with the independent Apache-2.0 `watt-mind/factory` project.

Contributions are under Apache-2.0. Describe what changed, why, validation performed and limits.
Do not add mandatory central telemetry, cloud accounts, monetization restrictions, one-service-per-box
scaffolding, or a new protocol without an explicit architectural decision.
