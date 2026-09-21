# Repository Agent Guidance

This public repository is self-contained. Read README, the relevant ADR, and the target contract
before changing a boundary. The owner-authorized A0 bootstrap is the initial foundation, not an
instruction to execute the historical private Program Execution Engine v1 brief.

- Work on a feature branch from `develop`, target Forgejo `develop`, and preserve unrelated changes.
- Initial empty-repository bootstrapping is not a release. Never promote to `main`, tag or deploy
  without explicit owner authorization.
- Run `make check` and `make package-check`; Python static checking is basedpyright, not pyright.
- Keep `adapters -> ports -> core`. No private repo, Prefect, M5-B, Scarcity Router or live forge
  may be necessary to import core or run its tests.
- No coding-agent implementation, custom sandbox, custom protocol, adaptive routing or large migration
  belongs in A0. Documents describing future controls are not evidence of implementation.
- Models and runtime sessions do not grant authority. Ordinary engineering defects are not Human Gates.
- Use synthetic fixtures; never read/copy secrets for documentation or tests. Do not commit runtime
  state, local agent-manager state or private audit payloads.
- Use existing implementations and patterns first; record substantial source attribution mechanically.
- Canonical development is Forgejo; GitHub changes are limited to explicitly authorized mirror,
  security-intake and later distribution configuration.
