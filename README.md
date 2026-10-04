# Creatidy Kernel

**Democratize software development with AI.**

Creatidy Kernel is a local-first work controller for engineering with existing AI harnesses. It is
for individual creators and small teams with limited money, subscription quota, time and attention.
The objective is an accepted result including verification, review and fixes, not the cheapest token.

**Status: deterministic domain, durable persistence, bounded Forge and Codex Runtime adapters,
an offline two-node CLI reference flow, and bounded TaskSpec support for two frozen real
tasks, not a general autonomous Program engine.**
The code includes immutable Program intent, legal domain commands, single-controller SQLite history
and rebuildable projections, an external-operation journal/outbox with a synthetic effect seam and
content-addressed artifacts, plus a replaceable resource allocator. Synthetic Runtime, Workspace and
authority fixtures exercise execution and candidate submission without a live harness. Their
trusted-development mode is not an OS/container security boundary for hostile code; real isolation
requires an existing sandbox and scoped credentials. The Forgejo reference adapter includes optional
HTTPS and conditional Git transports for trusted-controller use; if explicitly configured and invoked,
these can contact Forgejo and perform authorized branch, push and PR effects. The optional Codex
app-server adapter starts a native thread and turn only when a trusted caller supplies a durable
Operation claim, pinned Codex version and matching generated-schema method inventory, resolved
inputs and workspace collection. The native handshake does not advertise supported methods. Unknown
starts cannot be retried blindly; an accepted thread/turn handle must be stored before waiting, and missing
recovery or generated-turn identity remains unknown. It neither isolates hostile workers nor accepts
their results. The package does not schedule arbitrary Programs on its own.
The PR effect uses Forgejo AGit to submit the authorized full commit SHA to the target base with a
fresh Operation topic. It requires a validated creation receipt and an authoritative read-back of
the PR's repository, base branch and current exact head; an uncertain push is not retried blindly.
This is not a merge or deployment capability. The AGit creation mechanism is covered by synthetic
adapter conformance, not a claimed live integration test.

The SQLite adapter accepts databases only in an existing data directory validated as one supported
native local Linux filesystem mount. The database and its WAL/SHM/journal siblings must share that
mount; file-only mounts, split sidecars, OverlayFS, tmpfs, ramfs, checkpoint-disabled F2FS, and
network filesystems are rejected. It uses one SQLite connection explicitly opened with
`cache=private` and `locking_mode=EXCLUSIVE`, retained for the store's lifetime. Startup reports the
directory/mount identity, SQLite runtime, and required pragma checks. The adapter does not impose a
blanket SQLite version minimum.

Only the creating process and thread may use or close the store. After `fork()`, a child must not
use or finalize the inherited SQLite connection; it must remain inert and then `exec` or `_exit`.
When the controller process exits, SQLite releases its process-owned lock so a fresh controller can
recover the database even while an inert child still holds inherited descriptors.
The host must not remount, replace or move the data directory while the store is open; close the
store before changing its storage topology.

## Why A Kernel?

A coding agent does the work. Kernel is being designed to retain the intent, authority, durable
state and evidence that make a sequence of engineering work reliable, even when an agent crashes
or a model is replaced. The goal is fewer wasted attempts and owner interruptions, not just cheaper
tokens. It is not a new coding agent, IDE or general multi-agent framework.

The target controls existing harnesses through documented, version-evidenced adapters; it does not
rebuild their model-tool-result loop or operate by clicking an IDE. Source, control-plane state,
policies and outcome history can stay self-hosted; only explicitly chosen cloud integrations require egress.
There is no mandatory telemetry. These are architecture commitments, not a claim that all adapters
already exist.

[Scarcity Router](https://forgejo.creatidy.com/BioMedical-IT/scarcity-router) owns execution sources,
private quota/telemetry, selection/admission policy and the provider gateway. Today the optional
`ScarcityRouterAllocator` consumes its model recommendation interface, **not the execution gateway**;
the bounded live composition then uses native Codex and one configured binding. The agreed target is
dynamic Router-backed harness inference, with an explicit user pin as an exception, not a permanent
hand-maintained model table. The offline example remains a FixedAllocator proof.

[Model Intelligence](https://forgejo.creatidy.com/Creatidy/model-intelligence) owns versioned external
model/interface/benchmark/public-offer evidence, not private balances or routing. Router normally
consumes it; Kernel will retain the utilized knowledge reference. Console will consume state served
by each product owner and forward authorized commands, not query Kernel SQLite or own scheduling,
ranking or permissions. Separate products do not imply four services, a broker or shared database.
Public dependencies may be reused; no private `creatidy-onprem` is required for the public operator.

[ADR 0007](docs/adr/0007-shared-harness-routing-observability.md) records the owner's 2026-10-04
shared-architecture direction and source availability. [The coverage roadmap](docs/architecture/successor.md)
distinguishes **Agreed**, revision-**Verified**, **Proposed clarification** and **To prove**. The current
foundations are not evidence that ordinary intake, gateway, product review, isolation, full costs or
Console integration are complete. No Creatidy cloud account is required by the target architecture.

## Development Authority

[Forgejo](https://forgejo.creatidy.com/Creatidy/creatidy-kernel) is canonical for source, issues,
pull requests and development CI. Forgejo is also the first reference forge behind the existing
neutral Forge port; autonomous runtime integration remains planned. GitHub is the
[public mirror](https://github.com/creatidy/creatidy-kernel), not the place to develop a second fork
of the project. Integration targets `develop`; `main` promotion and release tags remain human-owned.
No package or release is published by A0.

## Run The Foundation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From a checkout:

```sh
uv sync --locked
uv run --locked python examples/fixed_allocation.py
make check
make package-check
```

The example prints a configured allocation; it makes no model call or reservation. Tests need no
Forgejo server, Scarcity Router, Prefect, M5-B or private Creatidy infrastructure. Dependency setup
needs access to the configured package index unless already cached; the tests and example run offline.
On systems without Make, the individual commands are listed in [CONTRIBUTING.md](CONTRIBUTING.md).

SQLite persistence tests require a verified native local Linux filesystem. They use pytest's temporary
directory by default; if it is on a rejected filesystem, set `CREATIDY_TEST_STORAGE_DIR` to a
directory on a supported native mount. CI selects a dedicated directory under the checkout; the
adapter verifies its topology before opening SQLite.

## Reference CLI

The installed `creatidy-kernel` command delegates lifecycle decisions to a shared application layer.
The default scenario is entirely offline: a durable deterministic runtime produces two small Git
commits in a disposable local repository, controller-owned checks verify exact content, and the
Forgejo adapter creates a synthetic PR against a durable local forge fixture. This is not a live
Forgejo PR or paid model call, and deterministic verification is not an independent model review.

```sh
uv run --locked creatidy-kernel reference --data-dir /path/on/native-disk/reference --approve
uv run --locked creatidy-kernel export --data-dir /path/on/native-disk/reference
```

`--approve` explicitly authorizes only this fixed two-node scenario. The first accepted artifact is
an explicit input to the second WorkUnit. Reusing the same data directory resumes or inspects that
same Program rather than starting another one. The SQLite filesystem restrictions above apply.
The JSON export contains Attempt and operation identities, acceptance/evidence bindings and unknown
resource consumption; unknown usage is not reported as zero. Local records remain user-owned.

For recovery exercises, add `--fault commit`, `--fault send`, or `--fault receipt`, then repeat the
command without `--fault`. Delivery intent, a committed delivery claim, a runtime receipt, and
engineering acceptance remain distinct. An uncertain effect is reconciled before any retry; a lost
PR reply without a recoverable reference remains unknown rather than creating a replacement PR.
`lost-context` exercises unavailable runtime recovery; `pr-commit`, `pr-send`, and `pr-receipt`
exercise the separate PR delivery boundaries. These faults operate only on the offline fixture.

The reference uses **trusted-development mode**, not a sandbox. Its local synthetic runtime has no
provider or owner credentials. Same-UID live workers must not be presented as isolated from control
state or owner secrets. No reference command can merge or deploy.

The supplementary live composition is `run_live_reference` in
`creatidy_kernel.adapters.reference_live`, not a default CLI mode. It requires explicit owner and
trusted-development acknowledgments, a version-pinned `CodexConnection` with finite RPC timeouts,
either a fixed allocation or an external `ResourceAllocator`, and a scoped `ForgejoForge` supplied by the trusted caller. It does
not load credentials. Use a disposable account/repository without broad owner credentials.

Before opting in, create a controller-owned bare `object_source` outside the worker workspace and
configure `ConditionalGitTransport` to use that same directory. Seed the disposable remote
`develop` branch with the deterministic empty commit produced by `reference_commit(object_source,
b"")`; the runner verifies the base and never initializes or updates the remote base itself. Accepted
commit objects are reconstructed from verified durable bytes in the bare source before AGit delivery.
This setup is deliberately specific to the reference scenario, not a general repository importer.

## Bounded Tasks

`creatidy-kernel task` composes the existing allocator, Codex runtime, controller-owned Git
candidate, bounded verification and AGit delivery for a frozen, controller-owned task registry.
Task `143` represents `BioMedical-IT/scarcity-router#143`: a test-only cancellation-race repair
with one allowed changed path, `tests/test_e2e_execution.py`. Task `166` represents
`BioMedical-IT/scarcity-router#166`: real-pipe fake-process feeder shutdown/descriptor ownership
with one allowed changed path, `tests/test_openai_codex_acquisition.py`. Its registration is
support only, not execution authorization; that support is integrated through PR #43. Any live task
still needs fresh zero-inference readiness and separate owner authorization. This is
**not arbitrary autonomous issue execution**. Issue prose cannot create a task or alter its
authority. There is no workflow DSL, scheduler or general agent framework, and no automatic merge
or deployment.

Infrastructure configuration must be provisioned before these commands. The following illustrates
the public CLI contract, not authorization to perform the first live execution:

**Execution boundary:** public operator support is integrated through PR #45. Historical coordinated
private-operator rollout and preflight #34 are no longer current prerequisites. #34 closed as
`INFRASTRUCTURE_BLOCKED`; #39 closed as `TASK_STALE`, not successful live acceptance. Readiness is
neither owner approval nor proof of the full shared architecture. Current lifetime, verification,
source and budget deviations are [registered blockers](docs/architecture/successor.md#first-priority-deviations),
not hidden by documentation acceptance. These examples grant no model/task, merge or deployment authority.

```sh
uv run --locked creatidy-kernel doctor --task 143
uv run --locked creatidy-kernel task preflight --task 143 --json
# Only after separate owner authorization, pin the exact base reported by preflight:
uv run --locked creatidy-kernel task run \
  --task 143 --expected-base <exact-preflight-sha> --approve --trusted-development
# Optional: add --deadline UNIX_SECONDS to pin an explicit finite run deadline.
uv run --locked creatidy-kernel task status
uv run --locked creatidy-kernel task export
```

The normal path needs no local Scarcity Router checkout and no `creatidy-onprem` access:
the controller acquires its own read-only source cache from the TaskSpec canonical
repository, discovers Codex on `PATH`, and selects user-local state defaults. An explicit
`--repo` remains an advanced override. Preflight checks its exact canonical origin and clean source;
the current run path does not share all those checks. [#61](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/61)
tracks enforcement before dispatch, cache ownership and host-qualified identity.

Preflight is quota-free: it validates runtime configuration, source/state suitability, exact base,
frozen structural assumptions, allowed paths and verification command shapes; probes the pinned
Codex version/schema and app-server initialization; requests a Router recommendation constrained
by the controller binding; and checks Forge read/config readiness. It never starts a model thread
or turn, writes Forge state, or runs the post-candidate eight-repeat verification plan. A blocked
preflight exits nonzero. Readiness is `READY_FOR_LIVE_TASK`, not owner execution approval, a quota
reservation or a guarantee that external infrastructure cannot change before dispatch. Human output
and `--json` are supported. The schema-1 JSON contains `status` (`READY_FOR_LIVE_TASK` or `BLOCKED`),
`ready`, `task_id`, `task_digest`, `repository`, `exact_base_sha`, `base_ref`, `blockers` and `evidence`.
Blockers contain a typed `reason`, safe `fields` (key names only), and an optional safe closed
`category` (for example Router and source diagnostic sub-classifications). Reasons classify
configuration,
source, state, task baseline, Codex, Router and Forge readiness failures. `inference_performed`,
`forge_writes_performed` and `execution_authorized` are false. Operators must require both a zero
exit status and readiness before using `exact_base_sha` for a separately authorized run.
Initialization does not attest authenticated model execution; the Codex evidence reports
`authentication_attested: false`. Operator credential provisioning remains a separate prerequisite.
Preflight does not repair control state: an existing database must be checkpointed with no active
or unreconciled WAL/SHM/journal sidecars. Such state returns `state_not_ready`; do not delete sidecars
to force readiness. Offline status/export use the normal store opening/closing path without
dispatching work. Runtime-effect reconciliation remains owned by the bounded run/recovery path.

Task work treats the source as read-only: normal operation uses the controller cache acquired
from the TaskSpec canonical repository. Cache acquisition/refresh does write local files; preflight
checks explicit `--repo` identity, while shared dispatch enforcement remains #61. Work happens in a disposable clone;
the controller itself creates the candidate commit from the workspace tree. The base is durably
frozen, and recovery uses that exact base even if a source branch moves. Changed paths must remain
inside the task's frozen allowed set for acceptance. That is a post-hoc gate, not a sandbox:
verification currently runs all checks even after a scope failure, and capture is truncated only
after buffering command output. [#50](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50) and
[#56](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56) cover safe preconditions,
isolation and full execution bounds. Task-owned argv-only commands have per-command timeouts.
Task `143` retains targeted cancellation checks, exactly
eight repeated targeted runs, and one `make check`. Task `166` checks `SafeTermination`, the acquisition
module, exactly eight repeated module runs, and `make check`. A closed typed structural seam selects
the task-specific baseline; #166 also preserves real pipe/non-blocking feeder structure and existing
test assertions. Structural checks reject obvious test weakening; semantic correctness, including
the forced-interleaving proof of unsafe baseline and corrected ownership, remains subject to
independent review. After verification the workspace must be
Git-clean for tracked and non-ignored untracked state; ignored cache artifacts remain ignored.

### Runtime Configuration

Kernel owns one typed, immutable parser for the following neutral infrastructure contract:

| Key | Requirement |
| --- | --- |
| `CREATIDY_KERNEL_ROUTER_URL` | Router origin; verified HTTPS, or HTTP on a literal loopback address only |
| `CREATIDY_KERNEL_ROUTER_KEY` | Optional Router bearer credential |
| `CREATIDY_KERNEL_RUNTIME_BINDING` | Exact controller assertion `provider/model[/effort]` |
| `CREATIDY_KERNEL_CODEX_BIN` | Optional absolute Codex executable; discovered from `PATH` when unset |
| `CREATIDY_KERNEL_CODEX_VERSION` | Optional exact Codex version; probed from the resolved binary when unset |
| `CREATIDY_KERNEL_FORGE_API` | HTTPS Forge API URL ending in `/api/v1` |
| `CREATIDY_KERNEL_FORGE_REMOTE` | HTTPS Git remote on the same origin, matching the exact task repository with `.git` |
| `CREATIDY_KERNEL_FORGE_TOKEN` | Required scoped Forge credential |
| `CREATIDY_KERNEL_FORGE_ASKPASS` | Optional absolute Git askpass executable |
| `CREATIDY_KERNEL_SOURCE_REPOSITORY` | Optional advanced source override; default is the controller-owned canonical cache |
| `CREATIDY_KERNEL_STATE_DIRECTORY` | Optional absolute control-state directory; default is user-local XDG state |

The process environment or an explicitly provided mapping feeds the parser, then typed configuration
feeds live components. Missing/invalid configuration is reported by key name, not value. Credentials
are excluded from configuration repr, logs and evidence, and are never task data or CLI arguments.
Explicit `--repo` and `--data-dir` override the optional profile path defaults. Status/export are
offline state operations: they require a state directory but no runtime credentials or Router access.
Secret management and deployment remain **operator-owned**: public Kernel neither decrypts owner
infrastructure nor stores deployment details; the optional generic SOPS profile adapter below covers
only a user-supplied profile and the user's own SOPS key environment.

Runtime configuration describes executable infrastructure. `TaskSpec` separately owns repository,
base branch, allowed paths, instructions and acceptance/verification authority. The parsed runtime
binding constrains which Router recommendation can become an Allocation; its exact configured
`provider/model/effort` tuple also supplies trusted Codex `supported_efforts`. A binding without
explicit effort supplies no effort support; null is distinct from the literal effort `"none"`.
A Router recommendation never creates runtime capability or execution authority.

Codex receives a closed controller-built operational environment (`HOME`, `PATH`, locale and temp
entries only), derived from the supplied configuration mapping. Controller credentials and unrelated
ambient secrets are not inherited. Execution requires explicit owner approval and a
**trusted-development** acknowledgment; workspace-write mode is not hostile-worker isolation.
Uncertain dispatch or PR delivery remains unknown until reconciled and is never blindly retried.

Provision `HOME` in the supplied mapping as an absolute, existing accessible directory for the
intended native Codex configuration. `PATH` is the supplied value (absolute, nonempty components)
or the controller's standard `os.defpath`, not an ambient factory lookup. Optional `LANG` and
`LC_ALL` must contain no control characters; optional `TMPDIR` must be an absolute, existing writable
directory. Only supplied `HOME`, `LANG`, `LC_ALL` and `TMPDIR` are copied. The factory never merges
an unrelated ambient environment; controller-owned verification also uses closed operational values.

`task run --deadline UNIX_SECONDS` validates an incoming absolute dispatch deadline no more than
one hour ahead; the CLI defaults to 55 minutes ahead on each invocation. The task mode records the
original deadline but **does not enforce its recovery**. It advances once and closes the owned Codex
transport even if the returned state is running; it has no durable task cancellation loop. Verification
and delivery are not bounded by a whole-task monetary/quota/time envelope. These current deviations
are [#48](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/48) and
[#56](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56), not promised recovery features.
Native-shaped offline fixtures are not live model or remote-Forge acceptance.

The separate `run_live_reference` Python composition has a 1-100 observation budget, original
deadline recovery and a cancellation Operation. Its bounded exit does not prove remote descendants
have stopped. Those reference guarantees do not apply automatically to `task run`.

Runtime requests carry requested identity only. Acceptance requires matching runtime-resolved
identity evidence, retained durably for the exact operation/accepted handle. A receipt-only Codex
restart cannot reconstruct that resolution from a native thread read: without previously stored
resolution evidence it reports `identity_unavailable` and cannot accept a candidate. Unknown
generated-turn attestation remains distinct from verified configuration resolution. The live reference's
cancellation journal separates uncertain interrupt delivery from terminal observation; the task
composition does not yet supply that loop or its export. Task acceptance currently satisfies a frozen
deterministic policy (`reviewer_required=False`), **not independent product review**. The durable
product review/remediation loop is [#54](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/54);
repository `/deliver-issue` is development workflow, not its implementation or budget.

`task export` is a thin persisted task/Allocation/candidate/acceptance/PR summary, not the richer
reference export or a task-integrated `core.outcomes` pipeline. It lacks full requested/resolved/observed
identity, provenance, usage, review/remediation and cancellation history. A stored PR receipt is
historical evidence, not current remote state. [#58](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/58)
covers versioned private full-path outcome/cost export, preserving unknowns and causal limits.
Current status/export open SQLite themselves and are not concurrent views of an active exclusive
controller. [#57](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/57) owns the served
snapshot/events/command/notification contract; no future API/flag is asserted here.

## Public Operator Surface

The installed `creatidy-kernel` command is self-contained for the bounded-task operator path;
the former private operator wrapper is no longer required.

```sh
uv run --locked creatidy-kernel config template            # generic profile template with placeholders
uv run --locked creatidy-kernel config validate --profile ~/.config/creatidy-kernel/kernel.env
uv run --locked creatidy-kernel --profile ~/.config/creatidy-kernel/kernel.env doctor --task 166
uv run --locked creatidy-kernel --profile ~/.config/creatidy-kernel/kernel.env task preflight --task 166
```

`doctor` runs the same quota-free readiness composition as `task preflight` and renders safe,
closed-vocabulary diagnostics with fixed remediation hints. It never starts a native thread or
turn, never writes Forge state, and readiness is still not run approval. Blocked results carry a
typed `reason` plus an optional safe `category`: Router failures distinguish endpoint unreachable,
HTTP rejection, invalid response/schema, no eligible selection, and selection incompatible with the
controller runtime binding; source failures distinguish acquisition, fetch, remote-mismatch,
checkout, and exact-base problems. Response bodies and credentials are never echoed.

Normal operation requires no checkout-path, Codex, or state configuration:

- **Source.** Kernel acquires its own read-only controller cache from the TaskSpec canonical
  repository under `${XDG_CACHE_HOME:-~/.cache}/creatidy-kernel/source`, verifies the exact
  canonical origin before every use, refreshes it with a bounded fetch, and fails closed if the
  refresh fails. There is no sibling-directory or workspace-layout search; a dirty cache is refused.
  Current foreign-entry replacement, hostless keys and lack of acquisition
  locking are limitations under #61, not proved cleanup ownership or multi-task safety.
- **Codex.** `CREATIDY_KERNEL_CODEX_BIN`/`VERSION` are optional: Kernel deterministically resolves
  the explicit override or the first `codex` on `PATH`, probes `codex --version` in the closed
  operational environment, and validates the version shape and native schema with the existing
  inference-free mechanisms. A unusable resolved installation is reported by safe category and is
  never silently substituted with another installation.
- **State.** Task state defaults to `${XDG_STATE_HOME:-~/.local/state}/creatidy-kernel/task`;
  the SQLite storage-safety requirements are unchanged, and `--data-dir` remains the explicit
  override.

Profiles are generic dotenv files of the same neutral runtime contract (literal `KEY=VALUE`
values, never shell input). `--profile` values override the process environment for that
invocation; `--sops` additionally decrypts a user-supplied SOPS-encrypted profile with the
user's own SOPS key environment through the `sops` executable. This is not a secrets manager:
there is no key generation, storage, or recipient configuration, no committed secrets, and no
deployment-specific constants in the public product. Profile values are never printed, logged,
or serialized into evidence; diagnostics report key names only.

## Optional Recommendations

`creatidy_kernel.adapters.scarcity_router.ScarcityRouterAllocator` implements only public
`POST /v1/select` (machine-interface v1 with D-057 explicit effort). Configure a bare Router
origin and exact supported Runtime bindings; an optional bearer credential travels only in the
request header. Non-loopback traffic requires verified HTTPS, and redirects are refused. The
echoed-credential check is deliberately conservative: use an opaque secret, not a public model
name, effort label or short common string that may also occur in legitimate decision text. A
response containing the configured credential is refused, never persisted as provenance. The
adapter maps the bounded reference capability/context request to public hard requirements;
unsupported capability identifiers fail explicitly. It never ranks alternatives or falls back to
FixedAllocator. A valid selection is independently checked against the configured Runtime's exact
provider/model/effort support and capability/context limits.

New Attempts preserve provider, model, opaque variant, explicit effort, rationale and canonical
public decision provenance. `null` effort remains unconfigured; `"none"` remains an actual effort.
Missing effort, incompatible versions/configurations, no eligible candidate, malformed responses,
authentication and network failures raise `AllocationUnavailable`. No source/account/access mode,
quota reservation or guaranteed future capacity is inferred. The transport's source revision,
reuse decision and modifications are recorded in [the reuse record](docs/architecture/reuse.md)
and [NOTICE](NOTICE); the Router package is not a runtime dependency.

Preparation atomically binds recoverable allocation/context bytes with the immutable Attempt and
operation intent, before dispatch. Restart uses those original bytes and reconstructs missing
artifact publications without calling the allocator. An outage or changed Router recommendation
cannot change an existing Attempt; a new Attempt is a separate allocation boundary. Corrupt or
missing durable evidence fails closed rather than triggering re-routing.

The Codex composition receives per-Attempt inputs from that durable Allocation. Explicit effort
requires a controller-configured `supported_efforts` set of `(provider, model, effort)` tuples,
evidenced against the pinned native runtime version, not inferred from the recommendation.
Codex receives explicit thread configuration and turn effort, verifies its thread configuration
receipt, and retains resolved evidence separately from unknown generated-turn attestation.
The native handle and its configuration snapshot are published together before the runtime
receipt is accepted. Recovery replays that durable snapshot if a process stops before receipt
acceptance or before the separate identity artifact is published. Known later observations must
not contradict the original resolution, including when Allocation effort was unconfigured.
`run_reference(..., allocator=...)` supports offline substitution; `run_live_reference` accepts
exactly one `allocation` or `allocator`. Native-shaped tests prove the two-WorkUnit flow without
paid inference; the CLI remains fixed/offline by default. Program acceptance and the separate AGit
Forge effect are unchanged, and neither path can merge or deploy.

## Architecture Decisions

| Contract | Document |
| --- | --- |
| Components, authority map, vocabulary and invariants | [Greenfield target](docs/architecture/target.md) |
| Existing mechanisms and reuse choices, including upstream limitations | [Reference catalogue](docs/architecture/reuse.md) |
| Adapter conformance, effect recovery and merge races | [Port contracts](docs/architecture/contracts.md) |
| Trust boundaries and malicious-worker assumptions | [Threat model](docs/architecture/threat-model.md) |
| Existing Creatidy mapping, without wholesale migration | [Migration analysis](docs/architecture/migration.md) |
| Current G01-G13 coverage, registered work and historical A-F corpus | [Coverage roadmap](docs/architecture/successor.md) |
| Independent challenge and remaining risks | [A0 review](docs/architecture/a0-review.md) |
| Canonical CI, mirror and deferred release activation | [Delivery contract](docs/architecture/delivery.md) |

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-product-and-ownership.md) | Mission, local ownership, public/private topology and Forgejo authority |
| [0002](docs/adr/0002-stack-and-package.md) | Python, one typed package and inward dependencies |
| [0003](docs/adr/0003-durability-and-effects.md) | SQLite history, atomic outbox and external reconciliation |
| [0004](docs/adr/0004-ports-and-resources.md) | Runtime, resource, workspace and forge boundaries |
| [0005](docs/adr/0005-authority-and-acceptance.md) | Capabilities, independent verification and genuine Human Gates |
| [0006](docs/adr/0006-context-and-outcomes.md) | Bounded context and user-owned outcome history |
| [0007](docs/adr/0007-shared-harness-routing-observability.md) | Shared product/harness/routing/state ownership and explicit proof gaps |

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
Licensed under [Apache-2.0](LICENSE). Adapted-source attribution is distributed in [NOTICE](NOTICE).
