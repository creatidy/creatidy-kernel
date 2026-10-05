# ADR 0007: Shared Harness, Routing And Observability Ownership

Status: agreed owner direction, 2026-10-04; implementation and public integration contracts remain
to prove. Documentation mandate: [Kernel #46](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/46).

## Source And Interpretation

Source reference: *Creatidy - shared system architecture*, v1.0, 2026-10-04,
`Creatidy_architektura_systemu_2026-10-04.md`, owner-supplied SHA-256
`4e64121599ac30896afb77574b2fd16cddfc3420afde37108611715c24b56e92`.
The full file was not found in the execution workspace. The owner's detailed Kernel brief is the
available source for sections 1-8, 9.2-9.4 and 10-18; the digest is a reference, not a locally
verified checksum. Exact missing subsection attribution or technical decisions are not inferred.

- **Agreed** records binding owner direction, not implemented behavior.
- **Verified** identifies an inspected revision/source or an explicitly executed check; integration,
  deployment and live acceptance are separate evidence.
- **Proposed clarification** is a review recommendation requiring the relevant owner/ADR approval
  before changing an expensive-to-reverse contract.
- **To prove** is a missing decision or guarantee with a registered bounded research/implementation
  task, not an excuse to invent an API or omit a known requirement.

These labels describe documentation evidence, not new serialized Kernel lifecycle states.

## Decision

The mission is an open, local-by-default and observable system for individual creators and small
teams. Optimize the path to an accepted result across money, subscription quota, time, fixes,
review and owner attention, not the cheapest token. Kernel is a work controller, not a second
agent harness or provider/router stack.

| Owner | Responsibility | Must not become |
| --- | --- | --- |
| Kernel | Intent, approved TaskSpec/WorkUnit/Attempt, authority, context/workspace, harness lifecycle, durable state, verification, review/remediation, effects and outcome evidence | A model catalogue, pricing/quota collector, benchmark ranker or replacement coding agent |
| Scarcity Router | Execution sources/accounts/quota pools, private telemetry, channel compatibility, cost/selection policy, admission, gateway and provider calls | Kernel task/spec/acceptance authority or a second task workspace controller |
| Model Intelligence | External model/interface/benchmark/public-offer evidence, provenance, applicability, conflicts and versioned knowledge publication | Private quota balance, routing or task authority |
| Console | Cross-product views and forwarding authorized commands to the functional owner | A scheduler, ranker, permission database or direct reader of another product's state database |

The existing harness owns its model-tool-result loop. Kernel controls it through a documented,
version-evidenced adapter; it does not click an IDE or create an agent because existing interfaces
were not investigated. `.kilo/` here is repository development context, not product runtime.

Target paths, **not an implemented integration claim**:

```text
Kernel -> harness adapter -> existing harness -> task workspace
existing harness -> Scarcity Router gateway -> permitted execution source
Model Intelligence -> versioned knowledge -> Scarcity Router
Kernel / Router / MI -> owner-served state/events -> Console and CLI
```

Public Kernel must not require private `creatidy-onprem`. Public module dependencies are allowed
when they avoid duplication; offline core/tests remain self-contained. Separate ownership does not
require four permanent services, a shared database, broker or Kubernetes. Public repository identifiers
are unchanged: Creatidy Kernel remains the orchestration/control-plane product, and Scarcity Router
remains the resource-routing product. No additional router product or repository is introduced, and
no rename of Creatidy Kernel is implied.

## Contract Consequences

Approved task requirements and scope/verification/grants precede admission. Ordinary issue content,
AGENTS.md and model responses can propose a versioned specification but cannot expand authority.
Current problem/source/PR/test-baseline evidence must be fresh; an open issue is not proof of work
remaining. Frozen Router #143/#166 tasks are historical cases, not the whole product.

Reuse Runtime `start`, `observe`, `candidate`, `cancel`, `reconcile` as the starting point, proving
each operation for a specific harness/version. ACP is the first standard-interface candidate to
evaluate, not the chosen protocol. File/terminal services need an identified constrained executor.
Neither a PID nor an accepted request proves execution. Done text is not acceptance; interrupt is
not descendant settlement; repeated prompt is not resume. Session/process/supervisor handoff and IDE/UI-close
guarantees remain open evidence/owner decisions under [#47](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/47).

On 2026-10-05 the owner selected **Owned Sessions** for the first bounded proof: dedicated
Kernel-owned sessions/processes, read-only UI observation and explicit controller-exit limitations.
This narrows the proof direction, not the full target. Independent supervision and existing
IDE-session handoff remain deferred and unproved; no harness, trust upgrade or live execution is
approved by this choice. [The bounded feasibility result](../architecture/harness-feasibility.md)
records distinct control/backend gaps and the remaining reception plan.

Router owns the executable decision/admission/pin contract; Kernel consumes it and persists the
main route before dispatch, bound to Attempt. Dynamic Router choice is the target default; a user
pin is an explicit exception. Main calls do not silently switch resources, and helper/review routes
need separate authorization. Harness control compatibility does not prove model-backend/gateway
compatibility. Provider/model/resource/access mode/effort/opaque variant/harness and their versions
remain separate, with requested/resolved/observed and unknown evidence. Plan-managed physical
identity needs an explicitly weaker contract. A pin is not approval, reservation, future availability
or reproducibility of a model response.

Kernel owns durable candidate-specific product review/remediation, distinct from repository
development review. Changed candidates require fresh exact-SHA acceptance. External effects retain
intent, claim/dispatch, receipt and reconciliation: no global exactly-once promise, incomplete
not-found-as-absence or retry of unknown effects. Hidden SDK/harness/gateway retries must be proved.

Kernel serves its own state or a controlled projection without weakening SQLite durability for UI
convenience. Progress and telemetry are distinct from necessary durable facts. Reconnect, cursor,
deduplication, gap/buffer and correlation semantics, revision/authority-checked commands and useful
decision notifications require a contract; no route/payload/version is approved by this ADR.

MI is normally consumed by Router; Kernel retains the utilized publication reference. Direct MI
harness evidence does not create a ranking or authority source. Full-path costs retain money,
quota, tokens, time and attention separately with provenance and unknowns. Export is local/private
by default; PASS and APPROVE are conditional evidence, not absolute model quality or causality.

## Superseded Assumptions And Open Decisions

This extends [ADR 0001](0001-product-and-ownership.md), [0004](0004-ports-and-resources.md) and
[0006](0006-context-and-outcomes.md). It supersedes treating recommendation-only allocation,
direct Codex plus one binding, private operator rollout gates or demo-first scope as the complete
target. Those delivered mechanisms and historical decisions remain evidence; they are not deleted
or reclassified as gateway integration. Immutable identities and real state/profile ciphertext must
survive future migrations, not merely field names or committed Git examples.

Allowlisted diffs and closed environments remain trusted-development, not a proved sandbox. Reuse
an existing isolation mechanism for workers, untrusted tests and review. One mutating controller per
Attempt, manual-edit reconciliation, host-qualified cache ownership/locking and safe cleanup remain
required; implementation evidence is in the [coverage roadmap](../architecture/successor.md).

Transport selection, supervisor/hand-off, stronger isolation profile, contested cross-product
identity/authority (including Router's existing backend-native workspace editing), publication and
event contracts need bounded proof and the appropriate owner decision. No fixed numerical budgets,
new endpoint, payload, flag, ontology or protocol version is invented here. A proposed minimal
owner-served view and reuse choices are recommendations until proved and approved.

The complete known work is registered, including migration, UX, security, distribution and installed
acceptance dependencies. Documentation approval does not close G01-G13, authorize inference or
mean `READY_FOR_LIVE_TASK`. Each functional issue requires authorized selection and reception:
explicit owner selection or selection under an explicit owner-invoked repository `/loop`.
That development delegation does not change product routing or live execution authority.
