# ADR 0003: Transactional History And Reconciliation

Status: accepted, 2026-09-21; target fixed before the Creatidy implementation audit.

## Decision

Start with one local control-plane process and one SQLite database, not Temporal, Prefect, a broker,
Kubernetes, or a distributed scheduler requirement. Use maintained SQLite transaction/recovery
machinery, not custom storage. Keep append-only, versioned domain history and current projections
in the same transaction, together with command deduplication and any pending external operation.
This borrows event-runtime/outbox patterns without promising arbitrary workflow-code replay.

Each mutation checks the expected aggregate revision and legal transition. History records contain
an event ID, aggregate ID/sequence, schema version, authenticated actor, correlation/causation IDs,
recorded time, observation time when different, spec/policy references and the decision reason.
Unique command keys bind an operation family and normalized input digest: identical repetition
returns the prior result; the same key with different input is rejected. Per-aggregate ordering is
authoritative; wall clocks and cross-system arrival order are not causal ordering.

The history is canonical for Kernel decisions. Transactionally maintained projections are its fast
read representation and must be rebuildable from versioned records without executing external
effects or calling models. Persist new immutable ProgramSpec/Attempt input revisions; never rewrite
the record of what was authorized or executed. Version migrations need backup and replay fixtures.

Use local disk, foreign keys, uniqueness/check constraints, explicit short write transactions,
bounded busy retry, WAL and `synchronous=FULL`. No network calls inside a DB transaction. A dedicated
writer serializes commits; compare-and-swap revisions and monotonically increasing fencing epochs
still reject stale workers. Use a patched SQLite version (see the reference catalogue), verify
startup pragmas, and use SQLite's backup API with an artifact manifest, not a naked copy of an open
database file. Never share WAL over a network filesystem. Storage/disk-full failure stops new effects.

Large artifacts live in user-controlled storage outside Git, addressed by content digest. Flush and
atomically finalize an artifact before recording its reference. Orphan blobs are recoverable garbage;
an accepted result must not refer to a missing/unverified blob. History excludes credentials and
unnecessary prompt bodies. Local retention/export is owner-controlled; deletion is explicit and
must not leave an active Program falsely claiming complete evidence. Cryptographic tamper-proofing
against the machine owner is not promised.

## External Operations

Kernel cannot atomically commit with a forge or runtime. Every consequential external action has a
stable Operation identity and request digest. Commit INTENT and outbox entry before dispatch. Before
I/O, durably record a delivery attempt/lease and fence. A crash after this point means **uncertain**,
even if the request may not have left the process. Acknowledgement requires a validated domain
receipt, not an HTTP success, log line, tool exit code, or LLM statement.

Track delivery, remote acceptance and remote execution separately. Useful observations are
dispatched, acknowledged, running, waiting, terminal and unknown, but not every operation must visit
each. An immediate branch creation need not be running. A rejected review request is not a running
review. Terminal observations cannot be regressed by stale callbacks.

Before retrying uncertainty, look up reality by stable key/remote reference and compare the exact
request. Retry only after proof of no effect, or through a documented idempotent API using the same
key. Reconcile a found effect rather than duplicate it. If absence cannot be established, retain
uncertainty, quarantine competing writes and wait/reconcile within policy; do not invent success.
Cancellation requests likewise do not prove the worker or its effects stopped.

Lease expiry means lost coordination ownership, not proof of remote failure. Database fences protect
database writes, not a Git push or merge. An effect needs external idempotency/CAS or an enforced
exclusive capability. Unsafe automatic operations remain unsupported if the adapter cannot supply
that guarantee. Webhooks only prompt a fresh observation and are authenticated/deduplicated.

## Alternatives

Issue comments or chat as the sole database cannot enforce atomicity or recover uncertain effects.
Full event-sourcing infrastructure and Temporal deployment are excessive for the initial local
scope. A mutable state row without history cannot explain authority or accepted outcomes. A long-lived
fork/dependency on `watt-mind/factory` is not chosen: borrow its proven mechanisms, not its evolving
ticket-first ownership. Persisted schema and transition implementation remain post-A0 work.
