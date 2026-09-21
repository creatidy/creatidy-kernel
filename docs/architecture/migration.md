# Current Creatidy Mapping

Second pass: the [target](target.md) and accepted ADRs were fixed before inspecting existing
Creatidy implementation. This is a mapping, not authorization to move code. No private source,
secrets, operational payloads or customer data are incorporated in this public foundation.

## Scarcity Router

Inspected clean local `release/v0.1.0-readiness` at
`8ae1fdb5ea9090604c26fd81f23010ad85a1536d` on 2026-09-21. Sources: `AGENTS.md`,
`docs/machine-interfaces.md`, `scarcity_router/selection_types.py`, `scarcity_router/selector.py`,
`docs/release-engineering.md`, `.forgejo/workflows/ci.yml`, `pyproject.toml` and `Makefile`.
Its current canonical namespace is still `BioMedical-IT/scarcity-router`; A0 does not relocate it.
Kernel's approved namespace is `Creatidy/creatidy-kernel` regardless of that historical location.

| Element | Classification | Mapping and cost |
| --- | --- | --- |
| Selector, capability profiles, capacity collection, provider/subscription policy | KEEP AS INDEPENDENT PRODUCT | Never migrate or duplicate in Kernel. Public adapter integration is small; mapping/contract fixtures need deliberate validation. |
| REST `/v1/select` and MCP `scarcity_select` | REUSE AS-IS through ResourceAllocator adapter later | Preserve versioned envelope and full decision provenance. `profile_id XOR requirement`; tightening only with profile. HTTP 200 with `selected=null` is valid no-solution, not a selection. |
| Runtime choice / reservations | REFACTOR BEHIND PORT / DEFER | Current recommendation returns model/provider/variant evaluation, not an arbitrary harness reservation. Adapter selects a configured compatible runtime and fails closed when unsupported. Do not fabricate a reservation or reduce diagnostics to a model-name string. |
| Optional execution gateway and native workers | KEEP AS INDEPENDENT PRODUCT | Do not pull gateway, credential store or worker protocol into Kernel. A future runtime can use a supported endpoint; independent recommendation mode remains valid. |
| Forgejo CI, mirror and release authority split | BORROW DESIGN | Reuse `ci`/`check`, develop PR validation, pinned actions, no configured private secrets in PR workers, exact-artifact release discipline. Forgejo ignores the upstream GitHub-style permissions declaration; do not inherit its read-only-token assurance. Do not copy the Windows/OCI/PyPI release pipeline into an empty foundation. |

The audit observed identical Forgejo/GitHub heads for `develop` (`9d6192eda987`), `main`
(`0d097e843def`) and the audited release branch (`8ae1fdb5ea90`). This verifies branch parity at
that instant, not the mirror scheduler, credential scope or future delivery. The upstream release
document explicitly marks several activation steps external; a workflow file is not proof that
release infrastructure is enabled. Its GitHub issues/wiki are still enabled, so A0 adopts the
authority convention but improves mirror signposting rather than reproducing that ambiguity.

No source copied. The new development workflow uses the same established action pins and authority
model; its small gate is independently written for this package.

## Private Composition And Deployment

Inspected clean local snapshots, not deployed versions: `creatidy-autonomy`
`872a6e55be691e8d2da353f9af2e6325722a5dbb`, `creatidy-onprem`
`038dfaf89c39078eba9a99f1c4d30eb5ec1357f1`, `creatidy-docs`
`9462c1b10d41b6309af5a48383de9e9f09dfde86`. Workspace Program rules were read at
`84831cc863790b8ce9c51c67b65a2a42cda0726d`; existing unrelated workspace/submodule state was untouched.
Private paths below identify audited seams without reproducing private source or operational data.

| Element / evidence | Classification | Migration cost and constraint |
| --- | --- | --- |
| Autonomy `tasks/service.py`, `tasks/store.py`, `flows/task_queue_dispatch.py` | KEEP PRIVATE / DO NOT MIGRATE YET | M5-A is a bounded task service, not a Program graph. Provider success is task completion, not evidence-based acceptance. Semantic replacement is large; no wholesale extraction. |
| Autonomy `pr_review/controller.py`, `flows/pr_review_runtime.py`, `pr_review/trigger.py` | KEEP PRIVATE ADAPTER | M5-B is an independent review workflow. Preserve standalone usage and trusted publication. Medium effort for a bounded evidence/receipt adapter; not a mandatory Kernel service. |
| Autonomy `pr_review/marker.py`, `pr_review/publisher.py`, `tests/test_pr_recovery_wakeup.py` | REUSE AS-IS inside the private adapter | Exact-comment redispatch and matching publication adoption are useful. Small wrapper/fixture work; do not copy private implementation into OSS. |
| Autonomy `providers/base.py`, `providers/openai.py`, `execution.py` | REFACTOR BEHIND PORT | Synchronous execute/readiness is not start/observe/cancel/recover. Medium-to-large effort to establish durable Runtime semantics and isolated coding workspaces. |
| Autonomy `pr_review/forgejo.py`, `routing.py` | REFACTOR BEHIND PORT | Preserve narrow translation clients. Existing Forgejo client has no merge capability; conditional integration is new work. Medium conformance effort. |
| Autonomy `policy.py`, `providers/registry.py`, `pr_review/config.py` | KEEP PRIVATE ADAPTER | Subscription/workload rules and deployment allowlists are composition policy, not universal Kernel law. Small-to-medium separation effort. |
| Autonomy `review/context_pack.py`, `pr_review/snapshot.py`, `pr_review/parser.py` | MOVE RESPONSIBILITY EVENTUALLY TO KERNEL | Generalize source manifests, redaction and exact-subject evidence; retain review-specific parsers privately. Medium-to-large effort, no code move now. |
| Workspace `.kilo/rules/60-program-execution.md`, `.kilo/command/execute-program.md` | MOVE RESPONSIBILITY EVENTUALLY; DEPRECATE prose ownership AFTER PARITY | Graph legality, dispatch, waiting and authority belong in code. Current mandatory M5-B/Prefect path and one-remediation escalation conflict with the target. Large, incremental replacement; keep working safety controls until tested parity. |
| Onprem `deployments/autonomy/compose.yml`, `scripts/validate-autonomy-env.py`, `scripts/autonomy-pr-review-webhooks.py` | KEEP IN DEPLOYMENT | Correct owner for containers, networks, volumes, secrets wiring and health. No Program semantics migrate here. Optional later deployment contract is small-to-medium; no deployment in A0. |
| Independent repository review/watch, active task/review DBs, workload repositories | DO NOT MIGRATE YET | Existing non-Program work remains useful. No shared database, bulk import, active Program conversion or product runtime dependency. |

Cost bands are relative engineering estimates, not delivery commitments. The largest work is proving
new semantics/recovery, not moving files. Reuse working private adapters without weakening the target.

## Concrete Gaps To Avoid Inheriting

- M5-B persistence imports the M5-A task schema/version and shares its database. The task store uses
  mutable records and `synchronous=NORMAL`, not the target journal/outbox durability contract.
- Invalid review markers can return a normal flow result before durable request ingestion. The existing
  check command interprets an absent formal review as pending. Transport/Prefect success therefore
  cannot establish domain acceptance, directly relevant to D/E.
- Static inspection found parameterless crash recovery selecting the oldest global recovery candidate
  while concurrent watch executions are permitted. An unrelated healthy request could be terminalized.
  This was not reproduced live and is not claimed to be the cause of the historical incident. Do not
  reuse this as generic Kernel recovery; correlate recovery to exact execution identity and lease.
- Selected reviewer provenance is not necessarily observed execution identity or usage. Finding text
  and locations are not yet stable cross-remediation identities. Preserve unknowns and lineage.
- The private deployment's shared worker/controller environment is not evidence of hostile coding-worker
  isolation. A read-only child environment and filtered variables do not prove denial of all host state.

These are audit findings, not changes to the private runtime. Upstream/private tests were inspected,
not executed during this mapping.

## Historical Engine v1 Brief

Located and read `Infrastructure/creatidy-autonomy#53`, titled "[ProgramEngine] Program Execution
Engine v1 - deterministic automation-first orchestration" (open, unassigned, last updated 2026-09-20
at audit). No Program implementation module existed in the inspected source tree.

Its P0-P9 proposal runs from audit, domain/FSM and persistence through private execution, M5-B review,
REST/CLI/MCP, recovery, onprem contract, synthetic tests and a new DemandTrace pilot. It correctly
preserves standalone M5-B, exact-comment recovery and the prohibition on rerunning Program #62.
Its private-repository-first placement and immediate three-interface/pilot scope are superseded in
this foundation by [the bounded Kernel successor](successor.md). Reading the brief is not permission
to execute it, mutate live Programs or cut over any runtime. A0 makes no such migration.
