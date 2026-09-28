# Creatidy Kernel

**Democratize software development with AI.**

Creatidy Kernel is the foundation of a local-first, provider-neutral control plane for autonomous
software engineering. It is for individual developers and small teams with limited AI budgets,
premium-model quota and human attention.

**Status: deterministic domain, durable persistence, bounded Forge and Codex Runtime adapters,
an offline two-node CLI reference flow, and an experimental owner-approved dogfood path for one
frozen real task, not a general autonomous Program engine.**
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

The target supports existing harnesses and providers, including US and Chinese providers and local
models. No Creatidy cloud account is required. Source, control-plane state, policies and outcome
history can stay self-hosted; only explicitly chosen cloud intelligence/integrations require egress.
There is no mandatory telemetry. These are architecture commitments, not a claim that all adapters
already exist.

[Scarcity Router](https://github.com/creatidy/scarcity-router) independently allocates scarce machine
intelligence. The optional `ScarcityRouterAllocator` consumes its public recommendation interface
through the ResourceAllocator port, without embedding its policy or using its execution gateway.
Either product remains useful without the other; the example below uses a FixedAllocator.

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

## Dogfood CLI (experimental, owner-approved)

`creatidy-kernel dogfood` is the D1-02 experimental entry point for one frozen, controller-owned
task contract. It composes the existing allocator, Codex runtime, controller-owned Git candidate,
bounded verification and AGit delivery into a single supported path. It is **not** a general
`run issue` feature and never merges or deploys.

```sh
uv run --locked creatidy-kernel dogfood run \
  --data-dir /path/on/native-disk/dogfood \
  --repo /path/to/scarcity-router-checkout \
  --task 143 --approve --trusted-development
uv run --locked creatidy-kernel dogfood status --data-dir /path/on/native-disk/dogfood
uv run --locked creatidy-kernel dogfood export --data-dir /path/on/native-disk/dogfood
```

The frozen task `143` represents `BioMedical-IT/scarcity-router#143`. The exact base is
established once from the source checkout (`refs/remotes/origin/<branch>`, falling back to
`refs/heads/<branch>`) or pinned with `--expected-base <sha>`, then durably frozen; the owner
checkout is only ever read, and a later run reuses the frozen base even if the source branch
moves. Work happens in a disposable controller-owned clone; the controller itself creates the
candidate commit from the workspace tree. Changed paths must stay inside the frozen allowed set,
trusted argv-only verification commands run in the candidate workspace with bounded capture and
timeouts, and deterministic structural checks reject obvious test weakening with the semantic
residue explicitly deferred to independent review. Verification artifacts that stay untracked are
confined and never change the accepted Git subject; tracked mutations reject the candidate.

Live composition requires documented environment configuration
(`CREATIDY_DOGFOOD_ROUTER_URL`, optional `CREATIDY_DOGFOOD_ROUTER_KEY`,
`CREATIDY_DOGFOOD_RUNTIME_BINDING`, `CREATIDY_DOGFOOD_CODEX_BIN`,
`CREATIDY_DOGFOOD_CODEX_VERSION`, `CREATIDY_DOGFOOD_FORGE_API`,
`CREATIDY_DOGFOOD_FORGE_REMOTE`, `CREATIDY_DOGFOOD_FORGE_TOKEN`, optional
`CREATIDY_DOGFOOD_FORGE_ASKPASS`). Credentials are read only from the environment; they are never
task data, evidence, log output, or command-line arguments. The coding runtime runs in
**trusted-development** Codex workspace-write mode; hostile-worker isolation is not attested.
An uncertain dispatch or PR delivery stays unknown until reconciled; nothing is blindly retried.

The owner supplies an absolute deadline no more than one hour ahead and an observation budget of
1-100. Both are pinned across restarts, with at most two runtime Attempts. Bounded exit requests
cancellation but does not assert that a remote worker has stopped. These limits are not a provider-side
hard monetary/token quota. Verification failures pause this reference, whose remediation budget is
zero. The live composition is covered by native-shaped offline fixtures; no live paid-provider or
remote-Forgejo execution is claimed by the offline suite.

Runtime requests carry requested identity only. Acceptance requires matching runtime-resolved
identity evidence, retained durably for the exact operation/accepted handle. A receipt-only Codex
restart cannot reconstruct that resolution from a native thread read: without previously stored
resolution evidence it reports `identity_unavailable` and cannot accept a candidate. Unknown
generated-turn attestation remains distinct from verified configuration resolution. Cancellation has
its own durable operation; once delivery is claimed, restarts observe the target without blindly
reissuing the interrupt. Exports distinguish uncertain cancellation delivery from an observed terminal
target, neither of which grants engineering acceptance.

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
| Bounded next program and failure regressions A-F | [Successor plan](docs/architecture/successor.md) |
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

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).
Licensed under [Apache-2.0](LICENSE). Adapted-source attribution is distributed in [NOTICE](NOTICE).
