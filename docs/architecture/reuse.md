# Reference Mechanism Catalogue

Audit date: 2026-09-21/22. This catalogue records reuse decisions, not product endorsements.
References describe upstream behavior; they do not establish guarantees in this A0 skeleton.
A0 copied no upstream product source. K8's bounded transport adaptation is recorded below.
Default-branch snapshots below are inspection evidence, not production-tested dependency pins.
Licensing was read for repository scope; copying requires checking the particular file and its notices.

## Shared-Architecture Reuse Gate

[ADR 0007](../adr/0007-shared-harness-routing-observability.md) and
[#47](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/47) require a new version-evidenced
control/backend assessment. The dated tables below preserve original design choices; `ADAPT BEHIND
PORT` there is not proof that a dependency or complete adapter was installed. Only explicitly
described built mechanisms (such as K8 transport and native Codex) are implemented here.

Start with current local Runtime/Workspace, immutable allocation/identity, effects, verification,
outcome correction/deduplication and SQLite/backup mechanisms. Retain them unless an alternative
preserves guarantees with a demonstrated advantage; similar upstream names are not replacement proof.
Use `ADOPT -> VENDOR/COPY -> PORT -> ADAPT -> BUILD`, justifying retained local code as well as reuse.

| Candidate / problem | Evidence required before a concrete adoption recommendation | Registered owner |
| --- | --- | --- |
| ACP and existing client/agent implementations | Exact protocol/implementation versions; start/events/load/resume/cancel/permissions; actual client file/terminal executor and enforcement; lost response and native resume distinctions | #47 control proof, #53 adapter, #50 isolation |
| Kilo CLI/ACP/serve/attach/SDK, Codex App Server, Claude Agent SDK, ZCode | Current installed generation/API; native delegation/cold review; explicit session/process ownership, descendant settlement and UI-close/attach; separately prove gateway backend/tool/stream/pin/retry compatibility | #47/#48/#53 and #52 consumer; no forced selection |
| Symphony SPEC tracker/workspace/runner/status patterns | Exact revision/licence; loss of response, persistence/authority and restart semantics, not just presence of start | #47/#49; preserve Kernel reception authority, no imported scheduler |
| Existing sandboxes, HAR/OpenHands workspace mechanisms | Real worker/test/reviewer host/DB/credential/network/terminal denials, cleanup/process ownership and platform limitations | #50/#59/#61; no own sandbox/framework |
| Rich and OpenTelemetry | Human/JSON/pipe compatibility, safe bounded presentation/correlation and opt-in exporter; diagnostic loss must not lose durable state | #57; no new collector required |
| Native bounded process and migration tools, SOPS | Streaming byte/time/process bounds; real synthetic ciphertext and interrupted atomic upgrade/rollback, not field-name compatibility | #56/#62; no secrets manager |

For any actual recommendation record source and exact revision/files, file-specific licence and
attribution, semantics and negative tests, security/trust boundary, dependencies and maintenance.
Code licensing is distinct from subscription/service access and data redistribution rights. Missing
evidence leaves a **candidate**, not an approved library, command, protocol or public contract.
Permission, inference and live service evidence are separately scoped; this documentation task grants
no product call. ACP's candidacy does not authorize a custom harness, protocol or VS Code plugin.

## Development Workflow Adaptation

Issue [#64](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/64) adapts the current local
Model Intelligence development workflow inspected 2026-10-05, checkout HEAD
`013cbb43e11d7f698d359db5a456d26d8075e34e`. The source was the actual local files, not remembered
text or an older Git revision. Their paths and modifications are recorded in [NOTICE](../../NOTICE).
The source LICENSE is Apache-2.0, Copyright 2026 Creatidy; there is no source NOTICE or additional
file-level copyright notice in the adapted files. The complete license is distributed here.

Changes are repository identity/paths, accepted Kernel product boundaries, existing final gates
(`make check` and `make package-check`), pytest/import-linter permissions, compatible test hash
constants/lint annotations and a four-command inventory guard. The same native reviewer permissions,
isolation, exact-SHA contract and owner-invoked loop model are retained. This is development context,
not a Kernel product dependency, Router call, new controller or live runtime acceptance.

## Historical Audit Catalogue

## Agent And Workspace References

| Problem / reference | Primary implementation evidence | Chosen approach | Reason / limit |
| --- | --- | --- | --- |
| Tracker polling, claims, liveness and backoff: OpenAI Symphony | [SPEC at be10a1b79df7](https://github.com/openai/symphony/blob/be10a1b79df723d6d7612b5651c8522704dafb2e/SPEC.md), Apache-2.0 | BORROW DESIGN | Useful reconcile/claim loop, but tracker-owned scheduling and Codex-specific execution. Running/retry bookkeeping is not durable across orchestrator restart. Per-invocation turn limits are not Program budgets. Do not rebuild its harness supervision. |
| Agent engine and persisted conversation: OpenHands | [Software Agent SDK at 4726601bb698](https://github.com/OpenHands/software-agent-sdk/blob/4726601bb6987ddac497e0edb983e332282ba1f9/README.md), MIT | ADAPT BEHIND PORT | Run/events/history, pause/interrupt and persisted conversations; local/container/remote workspace options. Pause waits for an inference call; interrupt differs. Restored history cannot recreate lost files/processes. Current OpenHands root is Agent Canvas, so use the SDK rather than assuming the older monolith. |
| Reproducible execution slots and exact-tree validation: HAR | [os-factory/har at a4f304402587](https://github.com/os-factory/har/blob/a4f3044025875464b5e57569823539987677941c/README.md), [verification](https://github.com/os-factory/har/blob/a4f3044025875464b5e57569823539987677941c/docs/src/content/docs/docs/guides/verification.md), Apache-2.0 | ADAPT BEHIND PORT; BORROW DESIGN | Strong Workspace candidate: preflight, occupied-slot protection, worktrees, service slots and content-bound proof. Recovery is environment-launch recovery, not portable agent-session recovery. Worktrees are not a security sandbox. Do not rebuild slot/container plumbing if HAR fits. |
| Existing runtime with interrupt/resume: Codex | [app-server protocol at 3fd5160cd6c7](https://github.com/openai/codex/blob/3fd5160cd6c78f2051bb54359d53f09207171733/codex-rs/app-server-protocol/src/protocol/common.rs), Apache-2.0 | ADAPT BEHIND PORT | Thread/turn start, events, interrupt, read/resume and custom/local providers. Separate Workspace allocation remains necessary. Use a fresh thread for cold review; detached review is deprecated at this snapshot. Candidate first public runtime adapter because its lifecycle is explicit, not because OpenAI is core authority. |
| Provider-neutral headless execution: Kilo | [CLI at 4b0e3699158c](https://github.com/Kilo-Org/kilocode/blob/4b0e3699158c400f4e4adc082119777c0b93ba6c/packages/kilo-docs/pages/code-with-ai/platforms/cli-reference.md), repository-reported MIT | ADAPT BEHIND PORT | Headless JSON events and continuation; newer durable input/replay routes are experimental. Agent Manager worktrees differ from local sessions. Do not use UI recovery JSON as an API or copy owner `.env` credentials into workers. Gateway/cloud is optional, not a Kernel dependency. |
| Replaceable agent library: Cline | [Agent API at db5d3d00ccd9](https://github.com/cline/cline/blob/db5d3d00ccd9950dc542e904c390965be31452ba/docs/sdk/reference/agent.mdx), Apache-2.0 | ADAPT BEHIND PORT | Run/subscribe/abort/snapshot/restore, local and multiple providers, persisted sessions/usage. Teams do not prove workspace isolation. Limits/abort are not atomic quota reservations. Do not rebuild a coding agent or adopt its team coordinator as Program authority. |
| Milestone work and validator/fix loops: Factory.ai | [Missions](https://docs.factory.ai/missions/overview.md), [subagents](https://docs.factory.ai/harness/subagents.md), [execution](https://docs.factory.ai/droid-exec/overview.md) | BORROW DESIGN; competitive/UX benchmark only | Commercial product, no source copied. Fresh subagents and separate validation roles are useful patterns; public material does not prove every retry is cold or every worker isolated. Local/BYOK and airgapped options exist, but commercial Missions ownership/permission/economic assumptions are not our core. |

Factory.ai and `watt-mind/factory` are unrelated references. HAR is specifically `os-factory/har`
([official site](https://harproject.dev/)), not a similarly named agent framework. None of the runtime
references supplies portable Program truth merely by persisting a conversation. Cancel is not
rollback; fresh context is not isolated execution; agent completion is not acceptance.

## Infrastructure And Protocols

| Problem | Primary reference | Chosen approach | Reason / limit |
| --- | --- | --- | --- |
| Durable local transactions | [SQLite WAL](https://sqlite.org/wal.html), [Python sqlite3](https://docs.python.org/3.12/library/sqlite3.html) | USE AS DEPENDENCY in the first persistence slice | Embedded, inspectable, no service. One writer, local filesystem, explicit transactions and FULL synchronization. Inspect the actual runtime, capabilities, pragmas, and writer topology at startup. Require a WAL-reset fix (SQLite 3.51.3+ or a verified patched backport) only when the demonstrated topology permits concurrent WAL writers; a single-controller, dedicated-writer configuration has no unconditional version floor. A Python version alone does not establish SQLite behavior. |
| Atomic state plus pending effect | [Transactional outbox](https://microservices.io/patterns/data/transactional-outbox.html) | BORROW DESIGN | Commit intent with state and history; deliver later. At-least-once delivery is not exactly-once external execution. No broker required. |
| Recover after a controller crash | [Kubernetes controllers](https://kubernetes.io/docs/concepts/architecture/controller/) | BORROW DESIGN | Compare desired and observed state; notifications are hints. Do not deploy Kubernetes merely to run a local controller. |
| Long-lived work and durable decisions | [Temporal execution](https://docs.temporal.io/workflow-execution) | BORROW DESIGN; DEFER dependency | Separate deterministic decisions from side effects. Temporal is a credible optional execution adapter, not a required multi-service installation. Do not reproduce its generic workflow language, replay VM, or scheduler. |
| Tool calls with deferred results | [MCP tasks, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) | ADAPT BEHIND PORT when supported | Experimental, negotiated support; results may expire and cancellation is best effort. Preserve receipts/results locally. A completed tool call is not accepted engineering work. |
| Remote agent execution | [A2A task lifecycle](https://a2a-protocol.org/latest/topics/life-of-a-task/) | ADAPT BEHIND PORT where an actual runtime supports it | Tasks, artifacts, cancellation and terminal immutability are useful. Neither conversation context nor remote completion owns Program truth. No A2A requirement for local subprocesses. |
| Editor-to-agent sessions | [Agent Client Protocol](https://agentclientprotocol.com/protocol/session-setup) | ADAPT BEHIND PORT where supported | Capability-negotiate load/resume/cancel. Session recovery is not durable effect deduplication. ACP here means Agent Client Protocol, not another similarly named protocol. |
| Least authority | [Capsicum](https://www.cl.cam.ac.uk/research/security/capsicum/), [MCP roots](https://modelcontextprotocol.io/specification/2025-11-25/client/roots) | BORROW DESIGN | Grant narrow capabilities, deny ambient authority. Protocol roots and instruction text are not OS enforcement. No custom capability cryptography or policy language. |
| Worker isolation | [Docker Engine security](https://docs.docker.com/engine/security/) | ADAPT BEHIND PORT | Use maintained container/sandbox machinery. A worktree only separates Git working files. Never expose the control database, owner credentials, or container socket to an untrusted worker. |
| Traces, metrics, logs | [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/) | USE AS DEPENDENCY when instrumentation exists | Standard correlation/export; opt-in user-selected exporter. Telemetry is lossy diagnostics, not the durable journal. No collector required in A0. |
| Artifact origin and signatures | [SLSA provenance](https://slsa.dev/spec/v1.2/provenance), [Sigstore](https://docs.sigstore.dev/cosign/signing/overview/) | BORROW DESIGN; DEFER signing integration | Keep subjects/digests/materials/producer identity now in the design. Do not claim a SLSA level. Public transparency uploads are not a default for private work; signing is not evidence of correctness. |
| Import-direction enforcement | [Import Linter](https://import-linter.readthedocs.io/en/stable/) | USE AS DEPENDENCY for development | Reuse a maintained architecture checker instead of writing an import graph engine. |

## Event Runtime Reference: watt-mind/factory

Inspected implementation and regression tests at
[`d57be4bfbfc4931f6948da969dd9ac541ba63b66`](https://github.com/watt-mind/factory/commit/d57be4bfbfc4931f6948da969dd9ac541ba63b66)
(2026-09-15; default `develop`, checked 2026-09-21). Repository
[license](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/LICENSE)
is Apache-2.0. Its [NOTICE](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/NOTICE)
also identifies separately licensed vendored material; do not infer every file is Apache-only.
Upstream tests were read, not executed by A0. This remains a design reference, not a dependency/fork.

In the following table, linked files are pinned to that revision.

| Problem | Implementation evidence | Chosen approach | Adaptation / verified limitation |
| --- | --- | --- | --- |
| Execution drifts from approved inputs | [run-spec](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/run-spec.mjs), [proposals](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/proposals.mjs) | BORROW DESIGN | Pin model, policy and agent definition; Kernel uses explicit immutable revisions. Upstream can overwrite a still-proposed spec under the same run ID. |
| State/journal split writes | [lifecycle](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/lifecycle.mjs), [reaper tests](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/reaper.test.mjs) | BORROW DESIGN | Closed FSM and atomic state/history/result/outbox publication. Individual hashes are not an externally tamper-proof history chain. |
| Duplicate requests | [intake](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/intake.mjs) | BORROW DESIGN | Delivery identity, logical action identity and terminal generations differ. Reject changed payload under the same key. Admission dedupe is not exactly-once mutation. |
| Zombie/competing workers | [concurrency tests](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/concurrency.test.mjs), [worker](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/worker.mjs) | BORROW DESIGN | Atomic claims, monotonic fencing and stale-publication rejection. External tools need their own idempotency or conditional operation; a SQLite fence is not distributed consensus. |
| Lost notifications after commit | [outbox](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/outbox.mjs) | BORROW DESIGN | Enqueue atomically; bounded backoff/parking. Upstream serve sink logs, and is not a general claimed external-mutation executor. Add per-operation reconciliation, not a claim of exactly-once delivery. |
| Agent self-attestation | [verify](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/verify.mjs), [verification tests](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/verify.test.mjs) | BORROW DESIGN | Strict schemas, hash recomputation, artifact confinement and trusted command records. Do not copy form-only completion acceptance or advisory-only path checks into a strict acceptance gate. |
| Stale review and landing evidence | [merge-apply](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/merge-apply.mjs), [review tests](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/merge-reviews.test.mjs) | BORROW DESIGN | Exact-head gate, positive CI evidence, fresh review after fixes. Upstream can reuse review across changed base and only atomically guards head at merge. Kernel requires evidence for the actual accepted/tested subject and explicit base-race handling. |
| Biased implementation review | [merge-review definition](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/agents/merge-review.json) | BORROW DESIGN | Separate read-scoped reviewer, cold context, pinned definition. Separation is not proof of semantic correctness or independent execution identity. Record both. |
| External reality drifts | [reconcile](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/orchestrator/reconcile.mjs) | BORROW DESIGN | Repair from observed forge facts. Bounded/unreadable results must stay unknown, never be treated as proof that no open work exists. |
| Owner permission replay | [inbox](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/inbox.mjs), [worker authority tests](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/worker.test.mjs) | BORROW DESIGN | Scope/hash binding and decision CAS are useful. Upstream worker gate does not consume a durable grant bound to the executing Attempt. Kernel must bind/consume single-use authority with Operation intent. |
| Runtime/forge coupling | [runtime adapters](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/adapters/index.mjs), [forge contract tests](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/lib/forge/contract.test.mjs) | BORROW DESIGN | Reuse port/conformance-test approach. Only memory/GitHub forge implementations ship, with direct `gh` calls still in merge paths. Do not assume demonstrated Forgejo portability. |
| Attempt cost attribution | [database and usage](https://github.com/watt-mind/factory/blob/d57be4bfbfc4931f6948da969dd9ac541ba63b66/event-runtime/lib/db.mjs) | BORROW DESIGN | Keep attempt-level provider/model/usage provenance. Unknown usage must remain unknown rather than upstream's zero normalization; distinguish accounting observations from reservation authority. |

## Historical Ownership And Deferral

| Subsystem | Chosen approach | Reason |
| --- | --- | --- |
| Program/WorkUnit decisions and acceptance | BUILD OUR OWN small domain, using the patterns above | Ownership of bounded Program semantics is central; existing products expose different ticket/harness authority. Not a general workflow engine. Implementation is deferred after the boundary proof. |
| Resource selection | ADOPT public Scarcity Router recommendation contract; ADAPT its transport in K8; retain tiny FixedAllocator | Independent products and offline tests need replaceable allocation, not a second routing engine. The K8 decision below records exact source and boundaries. |
| Capability checks and verification policy | BORROW DESIGN; BUILD OUR OWN narrow domain checks later | Required digest-bound consumption and acceptance semantics exceed the audited references. Credential storage, isolation and check tools remain existing components. |
| Context compilation and local outcomes | BORROW DESIGN; DEFER implementation | Pin sources, separate evidence/verdict, retain unitful observations; no vector platform, evaluation service or analytics engine yet. |
| Adaptive allocation, distributed controller, UI, release signing | DEFER | Need real usage and the first durable slice before their integration/maintenance cost is justified. |

These are A0-era timing statements. Bounded domain/verification/context/outcome code now exists;
full product integration is still incomplete. Known shared requirements have actual issues in
[the coverage roadmap](successor.md), not an unregistered DEFER queue. Kernel does not adopt
adaptive model ranking, a distributed controller or release signing merely to look complete.

## Reuse Discipline

Use existing mechanisms before building alternatives. A port is a Python contract inside this
package, not a new network protocol, service, or repository. Dependencies and design references
are distinct from copied source. Before future source copying, verify the exact file/revision's
license, preserve required notices and modifications, and record origin in attribution and history.
Unclear licensing means no copy, not a legal research program. Commercial Factory.ai is never a
source-code donor.

## K8 Recommendation Transport

The bounded K8 check selected **ADAPT**, not a Router package dependency. Source:
[Scarcity Router](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router) revision
`81b8e6393a1b2df8a6c7a9c6c3dcb8a7f6884e4e`, `scarcity_router/remote.py` and the
strict JSON mechanism in `scarcity_router/selection_app.py`, under Apache-2.0.
`NOTICE` records the modifications and is distributed with the wheel and source archive.

The decision ladder was evaluated narrowly for this integration:

| Option | Decision |
| --- | --- |
| ADOPT | Consume public machine-interface v1 plus D-057; do not install the Router package or its MCP/application/selector dependency closure. |
| VENDOR/COPY | Reject unchanged vendoring: `remote.py` imports Router application parsers and validates only a shallow envelope, not a Kernel Allocation. |
| PORT | No language port needed: both projects use Python and standard-library HTTP/TLS. |
| ADAPT | Retain explicit origins, authenticated bearer headers, verified TLS, loopback HTTP, redirect refusal, bounded reads and explicit no-fallback failure; strengthen hostile-response validation. |
| BUILD | Own only consumed-wire validation, configured Runtime compatibility and Kernel evidence mapping; never a second selector or transport framework. |

Relevant upstream evidence: `tests/test_interfaces_guardrails.py` remote bridge cases,
`tests/test_control_api.py` authenticated selection, and `tests/test_selector.py`
`ReasoningEffortSerializationTests`. Kernel uses synthetic public-wire fixtures and its own
focused transport tests, not upstream's application or live telemetry fixtures. Adaptation must
not inherit shallow version equality (which accepts booleans/floats), unbounded nested JSON,
non-finite exponent overflow, raw exception chains or unsupported Runtime assumptions.
Kernel owns maintenance and security testing of the adapted code; upstream changes require an
explicit public-contract review rather than automatic dependency updates.

D-057 is an additive public contract: every candidate carries explicit `reasoning_effort`,
including null for unconfigured, distinct from the configured string `"none"`. It comes from the
exact selected catalog entry. Opaque variant identifiers remain unrelated to execution effort.
The recommendation is neither a reservation nor an execution receipt. Kernel does not import
Router internals, use its gateway, or infer resource/account/access-mode semantics.

### Native Effort Evidence

Codex effort wiring uses the existing native-protocol pin
`openai/codex@3fd5160cd6c78f2051bb54359d53f09207171733`, not Router internals:
`codex-rs/app-server-protocol/schema/typescript/v2/ThreadStartParams.ts` (configuration
overrides), `ThreadStartResponse.ts` (`reasoningEffort`), `TurnStartParams.ts` (`effort`),
and `Model.ts` (`supportedReasoningEfforts`). The sibling `ReasoningEffort.ts` is a string
contract, not evidence of universal support for any particular value. The upstream
`app-server/tests/suite/v2/thread_start.rs` exercises `model_reasoning_effort` and the returned
configuration. Kernel requires independent, version-bound support supplied by its controller,
passes exact explicit effort to thread and turn, and rejects a mismatching configuration receipt.
This is protocol-source and offline fixture evidence, not a live-provider execution claim.
