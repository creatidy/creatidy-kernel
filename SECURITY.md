# Security

This is a pre-alpha architecture/boundary foundation. It does not yet execute untrusted agents or
enforce the future capability/sandbox design. Do not interpret it as a production security product.
The [threat model](docs/architecture/threat-model.md) separates target controls from implemented code.

Report vulnerabilities through the mirror's
[private security advisory intake](https://github.com/creatidy/creatidy-kernel/security/advisories/new).
This is confidential intake only; fixes and development remain canonical on Forgejo. Do not post
credentials, private repository contents or exploit details in public issues. Provide the affected
revision, impact, a minimal synthetic reproduction and suggested mitigation where known. No response
SLA or supported production release is claimed for A0.

Dependency installation and CI fetch public tooling; ordinary tests make no model/provider calls.
PR jobs must be isolated and unprivileged, with no deployment, model, publishing or owner credentials.
A Git worktree, path instruction, or MCP root is not a hostile-code sandbox. Never give a worker
control-plane storage, owner API authority or a container daemon socket.
