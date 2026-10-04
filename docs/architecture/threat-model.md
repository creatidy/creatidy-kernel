# Initial Threat Model

Scope: a trusted owner and control-plane host, potentially malicious workers and workload content.
A compromised host administrator can read/change the database and credentials; A0 does not promise
protection from that administrator. K3 synthetic fakes exercise worker and authority behavior but
provide no OS isolation, real credential broker or live worker execution.

Current bounded native Codex and verification commands run in trusted-development mode. They do not
prove same-user filesystem/network/control-state isolation. At inspected Kernel
`eb4f4a2956712bfaf39a3271e1523e7f77a91e26`, all verification checks execute before final scope
rejection; candidate-modified tooling can therefore run. [#50](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50)
requires no-execution precondition evidence and existing-sandbox denial tests for worker, tests and
reviewer. Full output capture/process/whole-task bounds are [#56](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56).
These are source findings, not executed exploit or live isolation proof.

[ADR 0007](../adr/0007-shared-harness-routing-observability.md) fixes shared ownership. ACP/native
file/terminal operations need an actual identified executor; permission text is not enforcement.
Cancel is not rollback, interrupt is not descendant settlement, UI closure is not process ownership.
Router's existing native workspace-editing lane requires its own authority decision before Kernel
integration. Do not remove a working producer lane or take over an owner's active IDE session here.

Cache host collisions, unchecked cleanup ownership and concurrent source operations are
[#61](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/61); one mutating controller per
Attempt/manual-edit reconciliation is [#59](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/59).
Console state/commands are served by Kernel with current revision/grant checks, never direct SQL or
client-owned permissions. Progress/notifications/lossy telemetry cannot replace durable authority.
Migration/export must protect actual ciphertext and user data, including uncommitted profiles;
synthetic fixtures and local/private-by-default evidence are required, not secret inspection.

## Boundaries

Owner UI/CLI uses an authenticated control channel. The control process, selected policy and trusted
adapter/broker code form the trusted computing base. Workers have only a narrow result/evidence
submission channel and execution capabilities. Git/forge/CI/providers are independent external
systems with potentially stale or hostile responses. Artifacts are untrusted until validated.
Cloud egress is explicit per selected model/integration; local operation needs none.

| Threat | Required control | Residual risk / post-A0 test |
| --- | --- | --- |
| Prompt injection in code, issue, comment or derived memory | Treat content as data, not authority; pin source manifests and owner policies | Models can still follow bad instructions; enforce permissions outside the model |
| Compromised worker steals owner authority | Separate process identity/sandbox; no control DB, owner API credential, broad forge token or container socket | Worktree/same-UID execution is trusted-development only, not a security claim |
| Candidate changes test/review policy to approve itself | Trusted gate definition pinned outside candidate authority; independent verifier | Tests may lie or miss defects; record coverage and exact subjects rather than asserting correctness |
| Malformed/fabricated tool or LLM result | Strict versioned schema and domain validation; limits on bytes, paths and artifacts; reject unknown fields at untrusted boundaries | Transport success and JSON validity alone remain insufficient |
| Secret leakage through logs/context/artifacts | Brokered credential references, minimal egress, redaction, no default telemetry, scoped artifact access | Provider/harness credential APIs may be coarse; refuse unsupported isolation rather than claim path-scoped tokens |
| Path traversal, symlinks, malicious Git hooks or archives | Existing sandbox, controlled Git config/hooks, canonical path checks and safe extraction | Test producer-shaped malicious fixtures, not only path strings |
| Hostile dependencies/toolchain | Pin supported versions/images, review lock changes, dependency/secret scanning, unprivileged build workers | Scanning is not proof of safety; no production secrets in PR CI |
| Replayed events/owner decisions | Authenticate source, dedupe identity plus payload digest, CAS revisions and consume-once effect-bound grants | Webhook signature does not establish current remote truth |
| Stale observation or merge TOCTOU | Exact-subject checks; conditional external mutation; reconcile first | Disable automatic operation when the forge cannot guarantee required atomicity |
| Worker/controller crash or lease theft | Atomic journal/outbox, durable handles, fences and remote reconciliation | A local fence cannot revoke a remote request already in flight |
| Resource exhaustion / infinite remediation | Time/resource limits, bounded polling/backoff, progress/oscillation detection | Hard quotas can overrun in-flight requests; reservations must be real and provider limitations disclosed |
| Accidental owner over-authorization | Show exact scope/subject, expiry, operation and budget; dangerous grants single-use | Owner can intentionally authorize risk; record this honestly, do not add fictitious enterprise approval tiers |

Use existing OS/container, credential and transport security. No custom sandbox, secrets manager,
signature scheme or policy language. Public Sigstore upload and external OpenTelemetry export are
opt-in; evidence hashes themselves may disclose information. Private project history is never required
for contributing to or using this public project. Detailed enforcement fixtures belong in the
[successor graph](successor.md), before enabling unattended live workers.
