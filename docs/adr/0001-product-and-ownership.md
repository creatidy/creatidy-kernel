# ADR 0001: Product, Ownership, And Repository Boundaries

Status: accepted from the A0 owner constraints, 2026-09-21.

## Decision

Creatidy Kernel's mission is **Democratize software development with AI**. Its reference user is
one developer or a small team with limited premium quota, affordable subscriptions, possibly local
compute, self-hosted tools, and limited attention. Optimize adoption and accepted engineering
outcomes, not token consumption, seat billing, or hypothetical enterprise tiers.

Kernel owns the durable engineering Program and its legal decisions, not the coding agent.
Local operation requires no Creatidy account, service, telemetry upload, or cloud model. A user's
source, policies, history and artifacts remain theirs. Cloud intelligence and integrations are
explicit, optional data-egress choices. Subscription access is used only through supported interfaces
and applicable provider terms; a subscription is not automatically unrestricted API access.

| Repository | Responsibility | Dependency rule |
| --- | --- | --- |
| Public `Creatidy/creatidy-kernel` | Generic Program/control-plane product; modular monorepo | No private dependencies; no required allocator product or deployment repo |
| Public `scarcity-router` | Independently useful intelligence allocation | Must not depend on Kernel |
| Private `creatidy-autonomy` | Creatidy composition, policies, M5-B/Prefect integrations | May consume both public products through their contracts |
| Private `creatidy-onprem` | Machine deployment, networking, secrets wiring, backup, monitoring | Does not own Program, review or resource-policy semantics |
| DemandTrace and other workloads | Independent applications and content | Do not embed Kernel into their product runtimes |

Product architecture != source architecture != package architecture != process architecture !=
deployment architecture. A module is not a service; a repository is not a container. Extract another
repository only after independent users, versioning, releases and upgrade cadence exist.

Forgejo is authoritative for source, issues, PRs, branches, tags, history and development CI.
GitHub is a public mirror/discovery and optional artifact-distribution surface, never a second
development authority. Release intent originates in canonical Forgejo tags, with a single approved
publisher. A0 publishes no release. Branch/mirror mechanics follow the audited OSS workflow.

## Alternatives And Consequences

A hosted-only product, GitHub-owned Program, mandatory paid allocator, open-core restrictions and
per-box repositories contradict the mission. Reusing an existing complete autonomous product is
valid for users who only need that product, but does not satisfy the owner's objective of a separately
owned OSS Kernel asset. This is not justification for independently rebuilding its solved mechanisms.
Apache-2.0 permits copying; reputation, compatibility and user-owned outcome history can compound
without artificial lock-in. No migration is authorized by this boundary decision.
