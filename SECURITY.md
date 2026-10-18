# Security

This is a pre-alpha controller with bounded trusted-development Codex execution, not a proved
whole hostile-worker/reviewer sandbox or production security product. A closed environment and
diff allowlist do not isolate same-user host access. Verification now rejects trusted preconditions
before candidate commands. The separate opt-in [ADR 0008](docs/adr/0008-linux-bubblewrap-verification.md)
Linux bubblewrap boundary covers untrusted verification/test children only, not their parent harness
or model context. Native reception is required on the selected platform; unsupported enforcement
refuses, never falls back. No live execution is authorized by these docs.
The [threat model](docs/architecture/threat-model.md) separates target controls from implemented code.

## Trusted-Development Expiry Status

The current trusted-development Codex path does not enforce grant expiry at each native filesystem
or subprocess syscall. Deadline expiry and user STOP prevent new Kernel-controlled dispatch and
initiate cancellation/termination; an already-running worker or descendant may still make local
effects during teardown. A stop/interrupt acknowledgement is not settlement, and local changes are
not rolled back. The accepted AC4 requires unknown termination/effect outcomes to remain unknown
and block acceptance/publication, but current owned-process settlement does not yet enforce that
gate: a terminal turn can reach candidate acceptance or finalize cancellation before the CLI closes
`CodexStdio` and obtains its owned-group settlement result. This is the unresolved implementation
gap tracked by [#90](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/90), not a current
runtime guarantee. The approved #56/#60 body changes require deadline-triggered cancellation
to use the same gate once synchronized.

`TRUSTED_DEVELOPMENT` is an explicit owner-trusted coding profile, not hostile-worker isolation. The
Codex app-server needs provider authentication in `HOME`; the native terminal may share the same
principal, and `workspace-write` is not a proven read boundary. Ordinary local Codex remains available
as an explicit same-principal trust choice with a limited provider account and no production or
remote-write credentials in that account. This does not hide provider auth from a tool that can read
`HOME`. Kernel adapter tokens are not placed directly in the child environment; this does not protect
secrets stored in files reachable through `HOME`. The current profile also does not prove all terminal
egress is blocked. Tasks needing provider-key confidentiality or enforced
remote-effect containment are unsupported unless the exact profile proves them; do not silently refuse
ordinary local coding or falsely claim those protections. The historical strict native-effect expiry
requirement in #53 remains unmet and explicitly unsupported under the accepted baseline; #53 is closed
not implemented and PR #85 is closed unmerged.

Accepted [ADR 0011](docs/adr/0011-task-level-authority-and-trusted-development.md) defines the
authoritative task-level baseline for this profile. Its [approved Kernel issue-body patches](docs/architecture/ac4-issue-description-proposals.md)
record the associated issue alignment; this authority does not imply runtime implementation is
complete.
The current task deadline is persisted as wall-clock Unix time and recovery retains its original
value. The suspend-inclusive boot-scoped projection required by ADR 0011 is not implemented; a
recovery that cannot validate the original clock authority must not be described as rollback-proof.

Report vulnerabilities through the mirror's
[private security advisory intake](https://github.com/creatidy/creatidy-kernel/security/advisories/new).
This is confidential intake only; fixes and development remain canonical on Forgejo. Do not post
credentials, private repository contents or exploit details in public issues. Provide the affected
revision, impact, a minimal synthetic reproduction and suggested mitigation where known. No response
SLA or supported production release is claimed for A0.

Dependency installation and CI fetch public tooling; ordinary tests make no model/provider calls.
PR jobs must be isolated and unprivileged, with no configured deployment, model, publishing or owner
secrets. Forgejo's automatic repository token is separate: its scope depends on the event and deployed
version, and a GitHub-style `permissions` declaration does not restrict it. Checkout credential cleanup
is not token containment. Runner/token settings require administrative verification before untrusted
contributions; A0's local tests do not certify that infrastructure boundary.
A Git worktree, path instruction, or MCP root is not a hostile-code sandbox. Never give a worker
control-plane storage, owner API authority or a container daemon socket.

Console must use a Kernel-owned state/command view or controlled projection, never SQL against the
active exclusive SQLite store. UI commands require current revision/authority checks by Kernel;
progress, alerts and telemetry do not grant authority or replace durable evidence. Profile/state
migrations must preserve actual user ciphertext and immutable history with no plaintext leakage;
synthetic fixtures, not owner secrets, are the public test evidence. See
[ADR 0007](docs/adr/0007-shared-harness-routing-observability.md) and the
[registered security/lifecycle/migration work](docs/architecture/successor.md).
