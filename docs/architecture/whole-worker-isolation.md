# Whole-Worker Isolation Decision

Bounded research result for [Kernel #78](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/78),
2026-10-08. This receives a demonstrated synthetic feasibility verdict and an integration
proposal. It does **not** close [#53](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53),
adopt a product security profile, authorize live inference, or claim real-harness AC4
conformance. [ADR 0007](../adr/0007-shared-harness-routing-observability.md) ownership and
[ADR 0008](../adr/0008-linux-bubblewrap-verification.md) (verification-only boundary) are
unchanged; no production behavior changed in this delivery.

Owner decision in force ([#53 comment 15606](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53#issuecomment-15606)):
**universal executor enforcement** — Kernel authority over file/terminal effects must be
enforced by a trusted worker/executor boundary independently of harness approval callbacks,
with expiry/revocation preventing new unauthorized effects and triggering bounded
termination/reconciliation, without rollback or instantaneous-revocation claims.

## Verdict

**FEASIBLE** for the bounded synthetic proof: a maintained rootless Podman/crun boundary
confined the entire synthetic worker — its native shell/Python commands, child and detached
(`setsid`) descendants, and its only external channel — with observed, non-fabricated
denials for every prohibited effect class in the required matrix. The proof ran entirely
unprivileged on the owner's own WSL2 host with disposable synthetic state.

This verdict is bounded exactly as follows:

- It covers the **synthetic worker** of this PoC. It is not Codex/Kilo/ZCode AC4 conformance;
  [#53](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53) reception still
  requires a real-harness executor-closure proof ([#53 comment 15612](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53#issuecomment-15612)).
- It is **single-UID** rootless confinement: no setuid `newuidmap` exists here and privileged
  installation was outside authorization, so container root maps to the invoking user's own
  host identity rather than into a subuid range. Multi-UID confinement remains a deployment
  prerequisite to separate worker writes from owner-file ownership.
- Static revocation semantics are unchanged: a writable bind mount is **not** dynamically
  revocable; termination bounds future effects but never retracts past or in-flight writes.

## Candidate comparison

| Candidate | Maintained? | License | Encompasses native tool paths? | Disposition |
| --- | --- | --- | --- | --- |
| Rootless Podman 6.1.3 + crun 1.30.1 | Yes (current releases, 2026-09) | podman Apache-2.0; crun GPL-2.0-or-later (libcrun LGPL-2.1); conmon Apache-2.0; netavark Apache-2.0 | Yes: every process, FD, mount and socket of the whole worker tree lives inside one user/PID/mount/network namespace set | **Primary candidate — proved here** |
| Verification-only bubblewrap (ADR 0008, 0.11.0) | Pinned; 0.11.0 affected by CVE-2026-87766 (fixed 0.12.0) | LGPL-2.1-or-later (0.12.0+) | No: it isolates explicit child commands, not the harness process tree that produces candidates | Unchanged role: verification/test children only; not promoted |
| Harness-internal approval callbacks (Codex workspace-write et al.) | n/a | n/a | No: #53 source evidence shows unmediated native paths and approval-skip bypasses; denial cannot constrain an operation that never asks | Rejected by owner decision 15606 as the enforcement boundary |
| OpenHands-style Docker workspace (#53 research) | External SDK terms unverified here | MIT SDK; service terms separate | Partially (container workspace) | Not adopted: no complete executor/authority closure evidence at this cut; podman path already satisfies the same need without an SDK dependency |

Podman 5.x/6.x and crun ≥ 1.20 receive the fixed handling for the sandbox-relevant advisory
classes listed below; the proved pin is the current maintained series. Ubuntu noble's podman
4.9.3 is EOL upstream and was **not** used for the proof (it also lacks a user-owned policy
path and refuses to create images without root-owned `/etc/containers/policy.json`).

### Security advisories observed at the evidence cut (2026-10-08)

| Advisory | Affects proved pin? | Note |
| --- | --- | --- |
| GHSA-pxhw-h44j-8pfx / CVE-2026-87766 (bubblewrap < 0.12.0, path traversal via parent symlinks during setup) | **Yes — the ADR 0008 pin 0.11.0 is in range** | Setup-time, same-privilege file writes as the invoking user; no privilege escalation for the unprivileged fail-closed profile. Remediation (0.12.0+ rebuild/repin) is recorded as [#80](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/80) and is deliberately **not** changed here. See [ADR 0008](../adr/0008-linux-bubblewrap-verification.md). |
| CVE-2026-94603 (checkpoint-annotation sandbox bypass), CVE-2026-57231 (image Env host-env leak) | No — fixed in 5.8.4/5.8.8; proved pin is 6.1.3 | Both are image-content-driven; the PoC image is controller-built with fixed config anyway. |
| CVE-2024-1753/9407/9675 (build-time escapes) | No — and the PoC never runs `podman build` | Controller-built docker-archive + `image load` only. |
| crun GHSA-7vwr-4279-7gq5 (CVE-2026-47766), GHSA-j87h-qqch-77vc (CVE-2026-88264), GHSA-5j66-q287-fhqw (CVE-2026-88265) | No — fixed by crun 1.28/1.30; proved pin is 1.30.1 | Crafted-image `/dev` symlink classes; container rootfs is controller-built and `/dev` is runtime-managed. |
| CVE-2025-24965, CVE-2026-84042 (krun handlers) | No — krun is not used | Not applicable to the crun OCI runtime path. |

## Proved boundary and evidence

Toolroot provisioning, versions, hashes and licenses: [tools/oci_worker_poc/SETUP.md](../../tools/oci_worker_poc/SETUP.md)
and the toolroot `provenance.json`. Exact native environment: Ubuntu 24.04.5, Linux
`6.18.33.2-microsoft-standard-WSL2` x86-64, unprivileged user, podman 6.1.3 (pinned upstream
commit build), crun 1.30.1 (signature-verified release asset), conmon 2.2.1, systemd cgroup
v2 user delegation (cpu/memory/pids), vfs storage driver.

Run the receipt with `CREATIDY_TEST_OCI_TOOLROOT=/tmp/kilo/kernel78-tooling make oci-worker-proof`.
Observed results for this record (2026-10-08): **18 native items passed, 0 failed, 0 native
skips** (plus 8 closed-environment authority/profile items), including:

| Matrix cell | Observed evidence |
| --- | --- |
| Approved workspace access | Read of bind-mounted read-only source, shell and Python 3.12.3 subprocesses, writable `/workspace` artifact — all succeeded; host artifacts verified byte-exact |
| Host filesystem | Synthetic host markers (tmp dir, user home, `/etc/ssh`, `/root`) unreadable (`denied:ENOENT`) and byte-unchanged after the run |
| Credentials | Three synthetic controller/provider/forge env canaries absent by name and by content; synthetic `HOME=/home/worker`, closed PATH; no host env inheritance |
| Native commands | busybox shell and host-closure Python executed inside the same boundary |
| Child processes | `setsid` shell + sleep grandchild spawned; after `podman stop`, host `/proc` scan proved descendant settlement; an unrelated host bystander survived untouched |
| Network | IPv4 loopback, IPv4 private and IPv6 loopback connects denied; DNS resolution denied; the network namespace contains only the loopback interface (observed `UP` with no external interface and no configured addressing) |
| Host control sockets | `/var/run/docker.sock`, `/run/containerd/containerd.sock`, an unmounted host Unix socket and an abstract-socket connect all denied |
| Runtime privileges | Observed in-container: `CapEff=0`, `NoNewPrivs=1`, `Seccomp=2` (deny-list filter), single-line `uid_map`, mounts limited to the enumerated allowlist, `unshare(CLONE_NEWUSER)` → `EPERM`, `pids.max=8` read inside the cgroup with fork flood denied |
| Cancellation | `podman stop` targeted only the owned container; graceful SIGTERM recorded as `cancelled`; bystander preserved |
| Expiry/revocation | Relay refused `unknown_grant`, `grant_expired`, `grant_revoked` (revoked mid-run), `grant_already_consumed` (single-use replay), `grant_op_mismatch`; worker self-expiry refused post-deadline steps |
| Cleanup | Per-attempt sandboxes removed; unrelated host files/processes intact |
| Crash/recovery | Monitor process killed → terminality explicitly unknown until the next observation reconciled it to an observed state; workspace retained |

Every negative above is an observed in-container or host-side fact (worker status records,
host `/proc` scans, host file reads), not a return-code claim. Evidence JSON per attempt is
written beside each attempt workspace.

## Authority and revocation analysis (AC4 posture)

The PoC implements the required architecture shape: **trusted controller → isolated worker →
explicitly authorized external-effect relay**. The worker has no general network; its only
external channel is one controller-owned Unix socket. The relay owns authorization: grants
are bound to the Attempt and the operation, are single-use by default, and refuse expired,
revoked, replayed, mismatched or cross-Attempt authority. Lost replies are recorded as
**uncertain effects**, never as cancellations or rollbacks, and the relay's receipt log —
not the worker's belief — is the reconciliation authority. A zero worker exit is not
acceptance: controller-side checks and receipts remain separate.

The requested distinction is preserved explicitly:

1. **Static containment** — namespaced, cap-dropped, seccomp-filtered, network-less
   confinement of the whole worker tree: **proved**.
2. **New external-operation authorization** — every relay effect requires currently valid,
   Attempt-bound authority: **proved at relay seam level** (synthetic; the real broker is
   future work).
3. **Dynamic revocation of already-granted native capabilities** — **not proved and not
   claimable**: a revoked grant stops new relay effects (observed), and termination stops the
   worker, but the writable workspace bind and any already-open file descriptors remain
   effective until the process tree dies; last-write timing is not observable as an instant.
4. **Termination and settlement** — `podman stop`/kill tears down the PID namespace;
   settlement is **observed** by host `/proc` scan, or retained as unknown on observer loss.
5. **In-flight uncertainty** — a dropped relay reply leaves the effect executed-but-unconfirmed
   (observed `lost` event); workspace bytes written before/while dying persist and are not
   rolled back (observed partial stream).

**AC4 compatibility:** the existing AC4 contract requires new unauthorized effects to be
refused under current authority and forbids instantaneous-cancellation claims. This boundary
satisfies the refusal requirement for the relayed channel and confines all native effects
statically; it does **not** provide instantaneous revocation of native in-flight writes, and
this record does not reinterpret AC4 to claim it. The enforcement combination that satisfies
AC4 for a real harness is: static whole-worker containment + relay-only external effects +
bounded termination + durable receipts — with the workspace treated as a disposable,
owner-visible-until-settled scratch resource, not as a revocable authority surface.

## Limitations, residual threats and unproved guarantees

- **Single-UID mapping**: container root == invoking host UID. A kernel-enforced multi-UID
  mapping (setuid `newuidmap` or `/etc/subuid`-capable environment) is required before
  worker writes are separated from owner-file ownership by UID, not only by mount view.
- **Shared kernel**: no VM boundary; kernel attack surface outside the seccomp deny-list
  remains. `clone` with namespace flags cannot be masked in this profile format (only the
  `unshare`/`setns`/`clone3` entry points are denied); a libseccomp-grade filter like ADR
  0008's would close more.
- **cgroup delegation requires an active systemd user session**; the harness fails closed on
  the silent cgroupfs fallback, but environments without a user session get no receipt.
- **Trusted controller/toolroot**: the unisolated parent, the pinned toolchain and the host
  administrator remain outside the boundary; hash pins are provenance, not authorship proof.
- **No adversarial penetration testing** was performed (out of scope): denials are proved
  for ordinary native paths and named escape primitives, not against a dedicated evasion
  campaign. Rootless-container kernel escapes published after this cut are unassessed.
- **Synthetic worker only**: real harnesses add MCP servers, extension hosts, browsers,
  delegated tools and network clients; each is a separate closure requirement under #53.
- **Relay is a seam, not a service**: production broker semantics (durability, fencing,
  reconciliation) remain #53/#56 work.
- Image/toolchain provenance is hash-pinned, community-maintained upstream; subscription,
  support and redistribution terms were checked only at the license level.

## Integration proposal for the existing Kernel (planned, not implemented)

Attachment points, preserving owned Sessions, immutable Attempt identity, Router ownership,
independent verification and durable receipts:

1. **Workspace port (`ports.resources` / workspace adapters)**: a new opt-in *isolated
   worker workspace* adapter composes what this PoC's `AttemptResources`/`WorkerProfile`
   build: per-Attempt disposable root, read-only source closure, one writable workspace,
   synthetic HOME/tmp, enumerated mounts, forbidden-prefix refusal. It must reuse the #61
   source-ownership admission and #50 resource-validation patterns rather than inventing new
   vocabulary.
2. **Runtime port (`ports.execution`)**: a `start` implementation launches the whole harness
   process inside the boundary (this PoC's `build_run_argv` composition) and records the
   exact container identity as the durable native handle; `observe` maps container state to
   waiting/running/terminal/unknown; `cancel` maps to `podman stop -t` + pidfd-grade
   settlement observation (`wait_settled`), preserving "interrupt is not settlement" and
   `settlement_unknown` retention semantics from ADR 0008/#48; `reconcile` maps to container
   existence/state reads after monitor loss (the observed crash/recovery cell).
3. **Authority**: the relay seam maps onto the existing external-operation journal/outbox and
   capability grants (`ports.authority`): grants bound to Attempt+operation+expiry,
   single-use consumption, controller-side receipt log as reconciliation truth, lost replies
   retained as unknown. No worker-held capability material; no approval-callback authority.
4. **Verification independence**: the #50 bubblewrap verifier continues to wrap untrusted
   test/verification children; a podman-based worker boundary does not replace or weaken it,
   and cold reviewers remain separately isolated.
5. **Trust-mode plumbing**: a new explicit worker trust mode (isolation profile name +
   toolroot pin) beside `TRUSTED_DEVELOPMENT`, never auto-selected; trusted-development stays
   labelled non-isolated per ADR 0008.

Remaining native-harness/tool-closure proofs before #53 AC4 reception (per comment 15612):
enumerate the real harness's full executor/resource/transport closure (native tools,
subprocesses, delegated/subagent tools, direct fs/process RPCs, MCP/extension/hook/browser
channels) and show each is either inside this boundary or refused; prove launch, candidate
collection, cancel and restart against the real adapter; decide and deploy multi-UID
mapping; then run the #15612 falsification matrix against the real harness.
