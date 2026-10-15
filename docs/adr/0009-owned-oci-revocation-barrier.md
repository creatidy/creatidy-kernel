# ADR 0009: Owned OCI Revocation Barrier

- Status: Accepted
- Date: 2026-10-15

## Context

`AuthorityBroker.revoke` previously committed the immutable revocation record directly.
An active `OwnedOCI` worker could therefore continue native file, terminal, or
subprocess effects until a later Runtime poll or worker deadline. Codex's native
app-server and its subprocesses execute inside one rootless OCI worker subtree; there
is no per-effect Kernel executor in that path.

The owner authorized a bounded revocation continuation for the existing `OwnedOCI`
path. A persisted cgroup-v2 `populated=0` receipt plus exact container absence is
accepted as the *no-live-effects* witness for cold revocation recovery. This witness
does not prove PID-namespace teardown or that every zombie was reaped. Normal warm
settlement continues to use the retained PIDFD witness.

## Decision

Keep revocation orchestration inside the existing single-writer `SQLiteProgramStore`
and `OwnedOCI` adapter. Do not add an independent monitor, supervisor, effect engine,
service, or global Podman configuration.

`AuthorityBroker.revoke` follows this order:

1. Persist an immutable grant-level admission fence. Every subsequent authority
   check rejects the fenced grant before another worker operation can be admitted.
2. Enumerate every bound, unsettled worker generation. Require an exact registered
   `OwnedOCI` handler for each native generation; an unsupported active worker keeps
   the grant fenced and prevents final revocation.
3. Verify each full OCI identity and persisted cgroup-v2 incarnation. Pause each
   worker and require `cgroup.events frozen=1` for the worker root and every populated
   descendant. Require the root freeze acknowledgment even when the initial subtree
   reports `populated=0`; an empty, unfrozen subtree can be repopulated and is not a
   barrier. Do not freeze or kill the enclosing host/controller cgroup.
4. Only after all freeze acknowledgments, commit the immutable authority revocation.
5. Kill the exact worker subtree with `cgroup.kill`, observe `cgroup.events
   populated=0` while the exact cgroup still exists, and durably persist that
   observation before force-removing the exact owned container without unpausing it.
6. Confirm exact container absence before recording retirement settlement or allowing
   candidate collection. A cold recovery may finish from the durable empty receipt
   and container identity without the original PIDFD.

The OwnedOCI run uses rootless cgroupfs management, a private cgroup namespace, and
does not use Podman's `--rm`; explicit removal preserves the exact cgroup long enough
to record the empty receipt. No global Podman configuration is changed. If cgroup v2,
delegation, identity, freeze acknowledgment, empty observation, or exact removal
cannot be verified, retain the durable fence, report unresolved worker risk, and do
not claim retirement or permit candidate collection.

The in-memory handler registry is only the current controller's execution hook; it
is not durable authority. Cold recovery reconstructs a handler from the durable
OwnedOCI launch/container/cgroup artifacts and retries the fenced revocation through
the same AuthorityBroker. If reconstruction or evidence is unavailable, the outcome
remains UNKNOWN and fenced.

## Consequences

- Active native effects stop at the kernel cgroup freeze barrier, before final
  revocation becomes visible. A failed barrier does not commit revocation.
- New Kernel-dispatched work is denied as soon as the durable fence is committed.
  The container remains frozen through post-commit `cgroup.kill` and explicit
  removal; no thaw is used as a recovery step.
- `cgroup.events populated=0` proves no live processes in the exact subtree. It is a
  no-live-effects witness only; zombies may remain. It must be bound to the recorded
  cgroup incarnation, full container ID, and owner label, then paired with exact
  container absence.
- A missing receipt, changed cgroup identity, thawed/repopulated worker subtree,
  failed removal, or uncertain container state remains UNKNOWN. It cannot authorize
  candidate collection or workspace cleanup.
- The path fails closed where rootless cgroupfs control is unavailable. This ADR
  does not adopt a host profile, make universal AC4 true, cover generic host
  `CodexRuntime` effects, or authorize live inference, paid access, release, or
  deployment.
- Workspace files written before revocation and journal evidence remain preserved.
  No automatic workspace cleanup is added.

## Evidence Boundary

The bounded Linux/Podman diagnostic demonstrated cgroup freeze, post-revocation
`podman exec` rejection, controller/PIDFD loss, cgroup kill, a persisted
`populated=0` receipt, exact force-removal, workspace retention, and bystander
survival. It used a diagnostic-only Podman binary and synthetic rootless identity; it
is design evidence, not production/native acceptance. The implementation must still
pass the repository's accepted pinned native toolchain and exact-head review gates.
