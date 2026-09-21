# Initial Threat Model

Scope: a trusted owner and control-plane host, potentially malicious workers and workload content.
A compromised host administrator can read/change the database and credentials; A0 does not promise
protection from that administrator. No worker execution or enforcement broker is implemented yet.

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
