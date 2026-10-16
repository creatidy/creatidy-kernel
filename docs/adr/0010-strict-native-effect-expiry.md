# ADR 0010: Strict Expiry for Native Effects

- Status: Proposed; owner rejected the candidates below; no architecture or implementation authorized
- Date: 2026-10-15

## Context And Evidence

Issue #53 AC4 remains binding: denied, expired, or revoked authority must not expand.
At HEAD `a9bcde7be26b2daddd8482f3216abfaef0167b3d`, `SQLiteProgramStore.worker_deadline`
passes the minimum of the original lease and grant expiry to `oci_bootstrap.py`. PID-1
checks it before worker launch and then in a userspace `select` loop capped at 100 ms.
That controls eventual worker lifetime, not the worker's direct syscall boundary.

The controlled regression proved a positive post-expiry subprocess and workspace file
creation while only authenticated PID-1 was stopped and the cgroup was not frozen.
Receipt: `/tmp/kilo/runtime-conformance-dyg_9gb4/grant-expiry-race-receipt.json`,
SHA-256 `efe32596a60bb98546da62a90ecdd48d4d41e624739b71d8a5d6d5685106d6bd`. It used
diagnostic-only Podman `2528a39892077aef1b7a5c7ea0b19a132f5945cf4b428a73f618140d7d94a9a4`;
it is not accepted general native conformance and is not part of PR #85's head. No
Bwrap or Podman source/pin investigation was repeated.

Relevant existing boundaries:

- `AuthorityGrant.expires_at` is an integer Unix timestamp. `check_scope` compares a
  caller-provided `now` and validates repository/operation/path scope, but it does not
  perform the filesystem or process effect (`core/authority.py:31-102`).
- `SQLiteProgramStore.check` validates and binds one existing logical `OperationIntent`
  to its durable operation/outbox. Its docstring explicitly says it does not claim or
  execute the effect (`adapters/sqlite_store.py:759-819`). The current time value is
  supplied by the caller before the transaction; it is not sampled at a syscall gate.
- Authority binding is not a reusable syscall lease: `single_use` grants are consumed
  by one durable operation binding. Rechecking the same single-use grant once per
  syscall would conflict with the current binding invariant. A per-effect design needs
  durable child-effect identities under one parent worker/grant binding, not repeated
  calls to the existing `check()` API.
- `OperationRecord` has one operation ID/effect key/request digest, delivery fence,
  lease, status and reconciliation data. The operation/outbox schema is logical-task
  level, not a durable record per open/write/rename/clone/exec syscall
  (`ports/program_store.py:18-29`, `adapters/sqlite_store.py:1213-1248`). The store
  owns one SQLite connection on its creating process/thread and rejects other writer
  threads (`adapters/sqlite_store.py:323-363, 2238-2248`).
- `CodexRuntime` checks durable authority before `thread/start` and again before
  `turn/start`; after dispatch, Codex and its terminal descendants execute natively
  (`adapters/codex_runtime.py:217-278`). `CodexStdio` launches the worker with
  `subprocess.Popen` and serially exchanges app-server RPC; it does not mediate each
  filesystem or process syscall (`adapters/codex_stdio.py:128-165, 307-354`).
- The OCI profile drops capabilities, uses `no-new-privileges`, a static seccomp
  profile, a read-only root, no network, a writable workspace mount and writable
  `/tmp` and `/home/worker` tmpfs (`adapters/worker_profile.py:84-118`). This is useful
  static isolation, but no `SCMP_ACT_NOTIFY` effect listener exists in the current
  profile. A writable FD/mount remains authority until the worker is frozen or exits.

## Candidate: Inline Controller Syscall Broker

This is the smallest candidate that reuses the existing trusted controller and SQLite
owner thread without introducing a separate broker process. It is a design candidate,
not an accepted architecture.

### Trust Boundaries And Flow

1. Treat Codex, every command, and every descendant as untrusted for authority. Keep
   the host controller, its exact workspace directory handles, SQLite connection,
   OCI identity and seccomp listener trusted. Do not trust app-server notifications as
   proof that all native effects were mediated.
2. Before launching the worker, remove writable workspace/tmpfs mounts and inherited
   writable FDs. Expose only a read-only projection of the grant's read paths (a broad
   read-only workspace is still a read-scope bypass) and standard transport
   descriptors. Install a seccomp user-notification filter before any Codex process
   starts; notify every supported mutating filesystem and process-creation syscall,
   and deny all unmodelled paths by default. Descendants inherit the filter. The
   controller receives the exact listener FD; the worker cannot access it.
3. Multiplex the listener with Codex stdio/RPC on the existing SQLite owner thread.
   Copy syscall arguments into bounded controller memory; never execute from tracee
   pointers after validation. Resolve paths relative to a pinned workspace directory
   handle with a no-escape policy, not a worker-provided absolute path.
4. For each effect request, open a SQLite `BEGIN IMMEDIATE` transaction on the owner
   thread, read the durable parent grant/binding/revocation fence, sample the selected
   trusted clock after acquiring the transaction, validate the exact effect scope, and
   journal a unique child-effect identity under the parent. Do not rebind/consume a
   `single_use` grant per syscall. Commit the child intent before executing any
   host-side effect. A denied request gets an error response and no host effect.
5. The controller performs supported filesystem operations using its own pinned
   handles. It never injects a writable workspace FD back into the worker. If native
   FD semantics are retained, the controller needs a virtual FD table or inert
   placeholders; every write, seek, duplicate, close, and descriptor-transfer path
   must be mediated. Reads may use read-only handles. Writable `mmap`, `mprotect` to
   shared-write, `io_uring`, `splice`/`sendfile`, descriptor passing and all
   unimplemented mutation calls are denied unless separately implemented and proved.
6. The controller returns results through the notification response. It never uses
   `SECCOMP_IOCTL_NOTIF_CONTINUE` as an authorization mechanism. An effect admitted
   before expiry may complete afterward; a new notification first admitted at/after
   expiry is denied.

The candidate's admission linearization point is the trusted clock sample and grant
check performed after `BEGIN IMMEDIATE` obtains the serialized SQLite writer lock and
before the per-effect intent is committed. The host effect cannot start before commit.
If this check occurred before expiry but commit or the effect completes afterward, it
is a pre-expiry in-flight operation. If the lock is acquired or the clock sample occurs
after expiry, it is rejected. Existing `check()` is insufficient for this: it receives
`now` from its caller and binds a logical operation rather than a specific syscall.

The stored grant uses wall-clock Unix seconds. The candidate uses `CLOCK_BOOTTIME` for
per-effect admission so suspend time counts and a later wall-clock rollback cannot
extend a running grant. At grant issuance, the controller must derive and durably bind
a boot-ID-scoped monotonic deadline to the persisted Unix expiry using trusted paired
`CLOCK_REALTIME`/`CLOCK_BOOTTIME` samples. The current grant/schema has no such
projection. The mapping's sampling precision and conservative rounding must be
specified; missing, mismatched, or uncertain clock state fails closed and cannot be
recomputed into a longer lease for an uncertain worker. A boot-ID change invalidates
the old worker/policy binding; recovery must prove the old worker is gone before a new
projection is considered.

### Crash, Suspension, And Reconciliation

- If the controller is suspended before an admission, the worker's notified syscall
  stays blocked. On resume, the broker samples the clock then; an expired request is
  denied. PID-1 termination remains a separate eventual lifetime control.
- If the controller exits, listener closure must fail the blocked syscall closed; the
  exact OCI/kernel failure behavior must be verified and any allow/resume behavior
  rejects the design. A durable intent committed before the controller performed the
  effect can still be uncertain if it crashed between effect and outcome receipt. The
  journal must record `unknown`, never blindly replay it, and reconcile using exact
  effect identity.
- If the operation outcome cannot be distinguished after restart, retain the unknown
  worker/workspace and do not publish a candidate. Existing cgroup freeze/kill and
  exact settlement receipts remain separate; a process exit or `populated=0` is not a
  per-effect receipt.
- Revocation still persists the admission fence before the existing cgroup freeze,
  commits revocation only after freeze acknowledgement, then retires and settles. A
  syscall broker must serialize revocation and effect admissions on the same trusted
  owner thread or provide an equivalent atomic order.

The current journal cannot yet meet these recovery requirements per syscall. It
records a logical operation/outbox, not a stable syscall identity or child-effect
relation. A seccomp notification ID is transient; retries, concurrent tracee threads,
a crash after the effect, and non-idempotent operations need new effect identities,
durable outcomes and operation-specific reconciliation. This implies a store/contract
change and potentially one synchronous durable transaction per effect.

## Falsification: Native Process Creation

The candidate fails as a transparent extension of current Codex native execution.
File syscalls can in principle be emulated by the controller if direct writable
mounts/FDs and every bypass are removed. Process creation is different:

- `fork`/`clone` creates a task in the tracee's namespace and process tree. An ordinary
  controller process cannot transparently create that child and return the correct
  parent/child, wait, signal and descriptor semantics to the blocked caller. Doing so
  via ptrace/remote injection or a controller-owned OCI exec protocol would add a new
  privileged mechanism or replace the executor; neither is present or authorized.
- `execve`/`execveat` replaces the calling process. Without `CONTINUE`, the controller
  cannot transparently perform that replacement in the tracee. `CONTINUE` would use
  mutable tracee arguments after the authorization check and is specifically not
  accepted as a security policy here.
- Replacing these calls with controller-owned OCI `exec` and pipes would create a new
  command-execution protocol. It changes process identity/parentage, stdio/TTY behavior,
  child lifetime and Codex's native terminal executor. It is not the current
  `workspace-write` behavior and needs its own conformance/permission decision.
- Allowing native clone/exec to preserve current behavior lets a descendant create new
  processes after expiry. Denying them breaks Codex's app-server/runtime threads and
  terminal commands. Intercepting only high-level Codex tool calls does not close this
  direct syscall path.

Therefore the inline broker is not a complete, minimum-scope, behavior-preserving
architecture for the current native Codex executor. Making it complete would be a
security-critical effect-broker/executor rewrite despite living in the existing
controller process.

## Required Capability And Codex Compatibility

The no-new-service candidate still requires all of the following changes before any
implementation could be considered:

- OCI/seccomp listener creation and exact FD handoff before worker entry, plus a
  controller event loop that services notifications without violating SQLite's
  creating-thread invariant or blocking Codex transport/recovery.
- A fail-closed syscall allowlist covering workspace and tmpfs mutations, open and
  inherited FDs, rename/unlink/truncate, writable mappings, descriptor transfer,
  alternate I/O paths and all descendants. Anything unmodelled must fail closed.
- A redesigned command executor for process creation, or a verified kernel mechanism
  that enforces task/exec deadlines without unsafe continuation. Neither exists in the
  current Codex/OCI path.
- Durable effect identities, outcomes and crash reconciliation, plus a trusted
  monotonic expiry projection and boot/suspend policy.

The current Codex adapter explicitly uses `workspace-write` and `approvalPolicy=never`
and only accepts `TRUSTED_DEVELOPMENT`; it does not attest hostile-worker isolation.
The diagnostic used that existing permission configuration and demonstrated direct
native effects. Replacing it with `danger-full-access` is not an acceptable workaround.
An inline broker would have to preserve native behavior before expiry and deny only
new post-expiry admissions. The proposed controller-owned `exec` replacement cannot be
claimed compatible without end-to-end Codex/TTY/descendant tests.

### Operational Costs And Residual Bypasses

- Every mediated mutation becomes a host round trip and a serialized SQLite
  transaction. Normal Codex commands issue many metadata and file operations; per-call
  journaling adds latency, WAL/fsync pressure, and a new backpressure limit. Batching
  admissions would authorize a batch before expiry and must be separately bounded and
  journaled; it cannot silently inherit an entire long-running command's authority.
- The filter must cover the writable workspace and every writable scratch mount, not
  only the repository. Direct writable FDs, inherited descriptors, `SCM_RIGHTS`,
  `/proc/*/fd`, `memfd`, writable shared mappings, `mprotect`, `io_uring`,
  `splice`/`sendfile`, `copy_file_range`, `execveat`, and descendant clone/exec paths
  are bypass candidates. The design must remove or deny each path it cannot mediate.
- Writable shared mappings must be denied, including ones created before expiry, or
  their later page dirties would not be a new mediated syscall. This can change tool
  compatibility. Unimplemented syscalls must fail closed, not fall back to direct
  kernel execution.
- The existing static OCI seccomp policy has no notification-listener handoff. Exact
  Podman/runtime support, listener lifetime, restart behavior, and inheritance into
  descendants would be new platform gates. Failure to establish the listener before
  worker entry must prevent launch.
- The single owner thread is intentional for SQLite safety. A notification thread
  cannot call the existing store; the controller needs a new selector/event loop that
  multiplexes Codex transport, worker lifecycle, seccomp requests, clock checks, and
  bounded queueing without holding store locks across an effect.
- The controller becomes a privileged effect executor holding workspace handles. Its
  path resolver, request parser, FD table, syscall compatibility, unknown-outcome
  recovery and audit surface would require security review and long-term maintenance.
  This is an effect broker in the trust model even if implemented in-process.

## Feasibility Judgment And Owner Gate

**Feasibility: no, not using only the existing controller, authority store, operation
journal and current Codex native executor.** The store can durably authorize a logical
Runtime operation, but it is not in the native syscall path. An inline seccomp broker
could be placed in the existing controller process, but safe filesystem emulation,
per-effect journal/recovery, and especially clone/exec semantics make it a new
security-critical mediator and executor architecture, not a bounded reuse of existing
components. Do not implement it under this design-only authorization.

A host-kernel LSM/eBPF guard could avoid a separate userspace service and preserve
Codex's native call path, but it is a new privileged enforcement mechanism. This
repository has no such program, loader, boot-scoped policy map, hook-coverage proof or
per-effect durable journal. Its completeness for writable mappings, all VFS mutation
paths, task creation, exec and descendants is unverified here. It is not a ready
alternative or an approved dependency.

### Owner Decision (2026-10-16)

The owner explicitly rejected the in-process seccomp mediator and did not authorize
privileged eBPF/LSM enforcement, a broker, service or executor redesign. AC4 remains
unchanged and unmet. The current native path must stay gated for strict expiry and must
not be represented as satisfying AC4. Reopening architecture work requires a new
explicit owner decision; this ADR and diagnostic regression do not grant that authority.

The regression's passing result is a diagnostic proof of a vulnerability: it observes
a post-expiry effect under the controlled test. It is not an acceptance pass, an
authorization to run this path as compliant, or evidence that the problem is resolved.

## Bounded Falsification And Acceptance Plan

Only after a specific mechanism is owner-approved, use one synthetic local OCI cell
with the exact pinned Codex/OCI/kernel profile; no live provider, private workspace,
pin qualification or broad kernel survey:

1. Verify pre-expiry filesystem and process requests succeed under the unchanged
   `workspace-write`/`never` configuration. Hold one already-admitted write/process
   operation across expiry and verify it may complete as pre-expiry in-flight work.
2. At a recorded exact persisted deadline, issue new writes via `write`/`pwrite`,
   rename/unlink/truncate, a shared writable mapping, inherited FD, an alternate I/O
   path, `fork`/`clone`, `execve`/`execveat`, and a descendant. Require independent
   trusted denial/admission receipts; absence of a marker alone is not proof.
3. Stop the controller before and across expiry, kill it before and after durable
   admission, and restart it. Require blocked/new effects to fail closed, pre-expiry
   admitted effects to remain `unknown` until reconciled, no blind replay, and exact
   cgroup/container settlement as a separate result.
4. Suspend/resume and kill/restart only the controller around expiry; use a controlled
   clock source for wall rollback and boot-ID mismatch; test a missing listener and
   cgroup identity mismatch. Any uncertain mapping/attachment denies launch or retains
   an unknown worker, never renews authority. Do not suspend the shared host during
   this bounded test.
5. Falsify compatibility with native Codex thread creation, terminal commands, file
   reads/writes, stdio/TTY, cancellation and descendants. Any need for
   `danger-full-access`, uncontrolled direct syscalls, or changed behavior not covered
   by an accepted contract rejects the design.

No implementation, service, API/schema, dependency, OCI security-policy change, pin,
review dispatch, commit/push, merge, release or deployment is authorized by this ADR
draft. Review ordinal 15 remains unused.
