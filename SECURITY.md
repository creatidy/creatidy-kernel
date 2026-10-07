# Security

This is a pre-alpha controller with bounded trusted-development Codex execution, not a proved
whole hostile-worker/reviewer sandbox or production security product. A closed environment and
diff allowlist do not isolate same-user host access. Verification now rejects trusted preconditions
before candidate commands. The separate opt-in [ADR 0008](docs/adr/0008-linux-bubblewrap-verification.md)
Linux bubblewrap boundary covers untrusted verification/test children only, not their parent harness
or model context. Native reception is required on the selected platform; unsupported enforcement
refuses, never falls back. No live execution is authorized by these docs.
The [threat model](docs/architecture/threat-model.md) separates target controls from implemented code.

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
