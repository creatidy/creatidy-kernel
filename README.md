# Creatidy Kernel

**Democratize software development with AI.**

Creatidy Kernel is the foundation of a local-first, provider-neutral control plane for autonomous
software engineering. It is for individual developers and small teams with limited AI budgets,
premium-model quota and human attention.

**Status: A0 architecture and boundary proof, not a working autonomous Program engine.** The code
currently demonstrates a replaceable resource allocator and tested package boundaries. It does not
run agents, contact providers, execute Programs or modify your repositories.

## Why A Kernel?

A coding agent does the work. Kernel is being designed to retain the intent, authority, durable
state and evidence that make a sequence of engineering work reliable, even when an agent crashes
or a model is replaced. The goal is fewer wasted attempts and owner interruptions, not just cheaper
tokens. It is not a new coding agent, IDE or general multi-agent framework.

The target supports existing harnesses and providers, including US and Chinese providers and local
models. No Creatidy cloud account is required. Source, control-plane state, policies and outcome
history can stay self-hosted; only explicitly chosen cloud intelligence/integrations require egress.
There is no mandatory telemetry. These are architecture commitments, not a claim that all adapters
already exist.

[Scarcity Router](https://github.com/creatidy/scarcity-router) independently allocates scarce machine
intelligence. Kernel will consume it through a small ResourceAllocator port, without embedding its
policy. Either product remains useful without the other; the example below uses a FixedAllocator.

## Development Authority

[Forgejo](https://forgejo.creatidy.com/Creatidy/creatidy-kernel) is canonical for source, issues,
pull requests and development CI. Forgejo is also the first reference forge for the future runtime
integration, behind a neutral Forge port. GitHub is the
[public mirror](https://github.com/creatidy/creatidy-kernel), not the place to develop a second fork
of the project. Integration targets `develop`; `main` promotion and release tags remain human-owned.
No package or release is published by A0.

## Run The Foundation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From a checkout:

```sh
uv sync --locked
uv run --locked python examples/fixed_allocation.py
make check
make package-check
```

The example prints a configured allocation; it makes no model call or reservation. Tests need no
Forgejo server, Scarcity Router, Prefect, M5-B or private Creatidy infrastructure. Dependency setup
needs access to the configured package index unless already cached; the tests and example run offline.
On systems without Make, the individual commands are listed in [CONTRIBUTING.md](CONTRIBUTING.md).

## Architecture Decisions

| Contract | Document |
| --- | --- |
| Components, authority map, vocabulary and invariants | [Greenfield target](docs/architecture/target.md) |
| Existing mechanisms and reuse choices, including upstream limitations | [Reference catalogue](docs/architecture/reuse.md) |
| Adapter conformance, effect recovery and merge races | [Port contracts](docs/architecture/contracts.md) |
| Trust boundaries and malicious-worker assumptions | [Threat model](docs/architecture/threat-model.md) |
| Existing Creatidy mapping, without wholesale migration | [Migration analysis](docs/architecture/migration.md) |
| Bounded next program and failure regressions A-F | [Successor plan](docs/architecture/successor.md) |
| Independent challenge and remaining risks | [A0 review](docs/architecture/a0-review.md) |
| Canonical CI, mirror and deferred release activation | [Delivery contract](docs/architecture/delivery.md) |

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-product-and-ownership.md) | Mission, local ownership, public/private topology and Forgejo authority |
| [0002](docs/adr/0002-stack-and-package.md) | Python, one typed package and inward dependencies |
| [0003](docs/adr/0003-durability-and-effects.md) | SQLite history, atomic outbox and external reconciliation |
| [0004](docs/adr/0004-ports-and-resources.md) | Runtime, resource, workspace and forge boundaries |
| [0005](docs/adr/0005-authority-and-acceptance.md) | Capabilities, independent verification and genuine Human Gates |
| [0006](docs/adr/0006-context-and-outcomes.md) | Bounded context and user-owned outcome history |

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
Licensed under [Apache-2.0](LICENSE). No upstream product implementation source is vendored in A0.
