# ADR 0002: Python And A Single Typed Package

Status: accepted, 2026-09-21; target fixed before the Creatidy implementation audit.

## Comparison

These are engineering trade-offs, not benchmark claims about agent coding quality.

| Criterion | Python 3.12+ | Go | TypeScript / Node | Rust |
| --- | --- | --- | --- | --- |
| Correctness / types | Strict static checks plus runtime edge validation required | Strong static types; explicit errors | Strict types; runtime validation still needed | Strongest compile-time ownership; higher complexity |
| Solo velocity / agent editability | Small, widely understood integration code | Predictable, some extra serialization glue | Strong harness ecosystem, tooling churn | More lifetime/async/trait maintenance |
| Concurrency | Async I/O; serialize DB writes; workers are external processes | Excellent goroutines and cancellation | Excellent I/O; avoid blocking event loop | Excellent with deliberate runtime selection |
| Local SQLite | Standard library, explicit transactions | Mature drivers; choose cgo/pure-Go trade-off | Mature drivers; packaging/native-binding choice | Mature crates; more implementation ceremony |
| Cross-platform / packaging / CLI | Interpreter required; wheel and uv; stdlib CLI available | Best single-binary distribution | Requires Node or bundled runtime | Excellent binaries; build matrix cost |
| API / MCP / A2A / observability | Mature libraries and SDKs | Good ecosystem | Particularly strong SDK coverage | Improving, less direct integration coverage |
| Tests / dependency hygiene | pytest, Ruff, basedpyright, locked tooling | Excellent standard tools | Mature but broader toolchain | Excellent standard tools |
| Scarcity/Creatidy integration | Can adapt over HTTP without source sharing | Same neutral HTTP boundary | Same neutral HTTP boundary | Same neutral HTTP boundary |

## Decision

Use Python >=3.12, one `creatidy-kernel` distribution with `src/creatidy_kernel` and a standard
wheel/sdist build. Use `uv` with a committed lock for reproducible development, Hatchling for
packaging, pytest, Ruff and strict basedpyright. Import Linter enforces inward dependency direction.
No web framework, ORM, agent SDK, private package or routing product is a runtime dependency in A0.

The choice is driven by control-plane/integration velocity with a small maintenance budget, not by
the language of a sibling repository. Go is the strongest alternative if installation friction or
measured controller concurrency later dominates. CPU-intensive work and agents are out of process;
Python's interpreter lock is not a reason to build an agent harness or distributed scheduler.

Core values are typed, immutable and dependency-light. Future untrusted inputs require explicit,
strict, versioned validation at adapters; type annotations are not a security boundary. Use an
existing validation library when that boundary is implemented, rather than a custom JSON schema
engine. No stable public wire API is declared by the foundation.

## Package Direction

`adapters -> ports -> core`, with adapters also permitted to use core values. A future application
layer calls ports and core; CLI/REST/MCP entrances call that same application layer. Core never
imports adapters. Composition selects implementations outside core. Adapter packages/extras are
added only when implemented; no empty class for each architecture box.

The A0 proof is deliberately just a resource request, allocation, allocator protocol, fixed adapter
and executable example. Persistence, Program transitions and runtime execution are future work.
