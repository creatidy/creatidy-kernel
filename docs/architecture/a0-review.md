# A0 Adversarial Challenge

This checklist evaluates the target, not an implemented engine. Independent review results and
executed validation are recorded in the completion report. A0's code is only the allocation boundary.

| Challenge | Answer and scope consequence |
| --- | --- |
| Another coding-agent harness? | No. Use existing Runtime engines; no tool loop, model client or agent SDK implementation in core. |
| Rebuilding watt-mind/factory unnecessarily? | Borrow its tested transactional mechanisms; own only Program/authority/acceptance semantics. No dependency or fork; do not inherit weaker audited guarantees. |
| Rebuilding OpenHands/Symphony/HAR? | Use runtime/workspace implementations where they fit; borrow reconcile/claim patterns. Do not rebuild their SDKs, supervisor plumbing or environment slot systems. |
| Inventing mechanisms for uniqueness? | No. SQLite, outbox, CAS, fences, cold review and digests are established patterns. The catalogue records reasons for each small owned domain responsibility. |
| Could an existing implementation replace new work? | Yes: maintained storage, sandbox, harness, validation and protocol libraries. Reassess exact-file permissive reuse at implementation time rather than preemptively rewriting everything. |
| Is Forgejo behind an abstraction? | Target Forge port uses opaque qualified IDs and capability checks. A0 imports no Forgejo client; real portability must be proved by K5 conformance tests, not claimed today. |
| Can GitHub disappear? | Yes. It is a mirror/distribution/security-intake convenience, not Program or development truth. Move intake/distribution if needed. |
| Can Codex disappear? | Yes. Historical Attempt provenance remains; replace its Runtime adapter without changing Program IDs. Initial adapter preference is not a foundational dependency. |
| Can Kilo disappear? | Yes. UI/session state is not a Kernel database or API. |
| Can OpenAI disappear? | Yes. Opaque allocation identity and replaceable supported runtimes; no vendor behavior in core. |
| Can Z.ai disappear? | Yes. Same boundary; providers do not own authority or Program truth. |
| Can Scarcity Router become trivial? | Yes. FixedAllocator is the executable A0 proof; the sophisticated adapter is optional. |
| Can Prefect disappear? | Yes. Private composition may use it; public core/tests never require it. |
| Can a model/agent crash without losing Program truth? | Required by immutable specs plus local transactional history and receipt reconciliation. Not implemented in A0; K2/K3 fault tests are release gates for that claim. |
| Can a compromised worker get owner authority? | Target denies this via separate identities, broker and sandbox. A0 executes no worker; same-UID unsandboxed operation is explicitly not secure. K3 must prove enforcement. |
| Do ordinary defects stay autonomous? | Yes within authorized finite budgets; findings/remediation do not automatically create HumanGate. A-F regression corpus is mandatory future evidence. |
| Can one developer operate it? | Target is one local controller, SQLite and artifact directory plus chosen existing tools. No required broker, Kubernetes, Temporal server or hosted account. First-run usability remains to be measured. |
| Useful with stronger models in 2032? | Authority, evidence, effects, resources and accountability remain; models are interchangeable execution choices. |
| Useful with weak/cheap models in 2026? | Deterministic gates and bounded retries protect outcomes; allocation permits stronger help without making premium-only execution mandatory. Quality/economics require real measurement. |
| Any mandatory cloud besides chosen intelligence/integrations? | No. Local model plus self-hosted forge is a supported target. Dependency installation is not mandatory runtime telemetry. |
| Any service/repo merely because of a box? | No. One modular package; context/outcome/verification/authority are responsibilities, not services. |
| Smallest useful Kernel we can maintain? | Bounded Program legality, durable decisions/effect reconciliation and evidence-based acceptance; existing components execute everything else. |

## Remaining Risks

Primary product risk is maintenance cost versus simply adopting an existing agent product. The first
slice must demonstrate fewer wasted attempts/interruptions, not architectural novelty. Engineering
risks are external idempotency/recovery gaps, coarse harness credentials, exact-base merge races,
provider subscription restrictions and SQLite/schema/artifact recovery. Adapter churn and misleading
usage data can undermine economics. Single-writer local operation and no automatic merge keep the
first slice small. No claimed financial saving or hostile-worker safety is justified by A0 alone.

Explicit exclusions: agent/IDE/framework, generic scheduler, custom SCM/CI/sandbox/secrets/policy
language/protocol, adaptive routing, production UI/SaaS, enterprise tiers, full Program engine,
large private migration, extra repositories, production services, tags and releases.
