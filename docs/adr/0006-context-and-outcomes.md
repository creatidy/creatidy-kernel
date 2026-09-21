# ADR 0006: User-Owned Context, Evidence And Outcome History

Status: accepted, 2026-09-21; target fixed before the Creatidy implementation audit.

## Decision

Context compilation is a bounded function over explicit sources, not an LLM memory service.
Keep four categories distinct:

| Category | Example | Authority |
| --- | --- | --- |
| Authoritative | Owner-approved ProgramSpec/policy, pinned source/ADR revisions, forge objects in their own domain | Only authenticated decisions and validated revisions establish authority; prose in source is still untrusted instruction content |
| Episodic | Prior Attempts, outcomes, active findings | Evidence of what happened, not permission for future actions |
| Derived | Search indexes, summaries, embeddings | Disposable and rebuildable, never the only copy of truth |
| Procedural | Vetted instructions, build/test recipes | Versioned and policy-selected; candidate edits cannot silently change trusted procedures |

A ContextPackage is a manifest with source IDs/revisions/digests, selection and redaction policy,
compiler version, size/token budget and selected content. Secrets stay as broker-side references.
Only the selected content goes to a chosen model. Store enough manifest/history to explain the
decision without retaining every sensitive prompt forever. Do not require a vector database.

Outcome history is a durable local projection/export of Program, WorkUnit class and Attempt facts:
requested/resolved/observed runtime-model-provider-effort, quota snapshot provenance/freshness,
usage units, wall time, validation, findings, remediation, acceptance/rejection and interventions.
Unknown usage or human time stays unknown, not zero. Estimates, provider reports and measurements
remain distinguishable; corrected observations append a superseding fact. Prevent duplicate receipts
from double-counting consumption. Link remediation and review cost to the accepted outcome, not just
the successful final Attempt.

Costs are a resource vector, not an unqualified float: currency amounts, subscription quota units,
tokens, compute seconds, wall time and owner minutes retain units/source/time. Attention budgets
include interruption policy and owner-recorded time where available. OpenTelemetry traces may link
to these facts but may be sampled/deleted without losing Program truth. Never upload history
centrally by default. Owner-controlled export can later inform Scarcity Router without introducing a
reverse dependency or shared database.

## Consequences

Preserve inputs for expected scarce-resource cost to accepted outcome, first-pass acceptance,
remediation cost, autonomous recovery and interruptions per Program. Do not implement learning,
adaptive routing, evaluation infrastructure, central analytics or a special provenance platform now.
Use standard artifact digests and later SLSA/Sigstore integration where useful; provenance is neither
proof of correctness nor a marketing differentiator. A0 has no outcome database or context compiler.
