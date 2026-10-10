# Creatidy Kernel

**Democratize software development with AI.**

Creatidy Kernel is a local-first work controller for engineering with existing AI harnesses. It is
for individual creators and small teams with limited money, subscription quota, time and attention.
The objective is an accepted result including verification, review and fixes, not the cheapest token.

**Status: deterministic domain, durable persistence, bounded Forge and Codex Runtime adapters,
an offline two-node CLI reference flow, bounded TaskSpec support for two frozen real
tasks, and inert ordinary preparation/approval through a trusted local API, not a general
autonomous Program engine.**
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

The same SQLite store now implements the local `AuthorityBroker` admission interface. Authenticated
trusted composition supplies principals and an explicit `resolve_authority_intent` interpreter for
the producer's complete original journal request. Issuance checks the stored Program owner and
immutable Attempt; revocation and single-use operation bindings survive reopen. Consumption is
committed atomically with validation of the exact existing intent/outbox, not with an external
effect. A claim, current fence, deadline/cancellation checks and uncertain-effect reconciliation
remain separate requirements. Grant-management records are not dispatchable outbox items.
Schema 3 adds these records without rewriting historical request or Program bytes; schema 1/2
migration and backup restoration remain supported. Retired workspace roots do not discard history
or prevent owner revocation, but cannot renew execution authority. This is a controller-local
admission increment, not native worker mediation, an isolated Runtime, or complete #53 reception.

For explicitly composed trusted-development Codex execution, `store.runtime_authorizers(clock)`
returns guards for `CodexRuntime`'s optional `authorize_resolved` and `authorize_recovery` hooks.
The dispatch guard requires pre-bound execute authority, the controller's own fresh claim, exact
original Attempt inputs, a current fence/lease and the actual resolved cwd. It records each native
`thread/start` and `turn/start` stage before sending, and rechecks revocation/expiry between them.
A restarted controller cannot turn an inherited lease or an uncertain stage into a new start.
Recovery instead checks the exact retained request, owned handle and durable receipt; expiry or
cancellation does not turn inspection into dispatch permission. Cached duplicate receipt lookup
does not send another native request. Existing default trusted-caller composition is unchanged;
these hooks do not add CLI admission, native-effect mediation or `ISOLATED` support.

Descriptor-safe worker-file reads and bounded directory collection are available in installed
adapters; the OCI proof tools import the same implementation. Leaf/directory symlink refusal,
entry/depth/size bounds and exact-byte sinks remain intact. Callers must protect root ancestors
and collector destinations, establish worker settlement, and treat any recorded refusal as a
failed collection. The proof disk sink is not crash-durable artifact publication. Sharing these
primitives does not activate the proof runner, supply an engineering image, or adopt its profile.

The installed `worker_profile` module also provides the shared immutable OCI profile/mount values
and fixed run-argument prefix used by both proof builders. It rejects ambiguous identities,
paths, duplicate mount destinations/environment keys and invalid limits without environment
inheritance, filesystem discovery or process launch. These are representation checks, not
verified tool/image provenance, mount-overlap/ownership checks or actual cgroup/rootless
enforcement. The deadline remains metadata here, not an expiry timer or admission decision.

The opt-in installed `oci_lifecycle.OwnedOCI` adapter records one immutable Operation/Attempt,
workspace, capability, runtime identity and explicit engine/profile binding in the existing artifact
journal before a trusted caller sends anything. This reservation is not launch permission. Inspect
must match the exact image, name and ownership label before capturing a full container ID; recovery
and stop then use only that ID, never a reusable name. Engine errors, timeouts and diagnostic output
remain unknown, and a missing ID cannot establish absence. The caller supplies a bounded CLI executor
and verified resources in a closed environment. Stop acknowledgement and confirmed container absence
do not establish descendant settlement, cgroup enforcement, safe workspace cleanup or full isolation.
Reconciliation alone does not launch a worker, adopt a security profile or enable `ISOLATED` Runtime support.

Its optional trusted-development `connect` composition consumes controller-fixed `OCIResources`:
raw native/config/seccomp pins, trusted read-only closure digests, protected controller paths and a
private disposable workspace as the sole writable bind. It refuses extra, overlapping, unsafe or
changed resources and unknown image identity, then launches by the exact immutable image ID. The
existing resource digest is reused without promoting the verification-only bubblewrap profile;
sockets and undeclared channels remain unsupported. Trusted host code must protect resource
ancestors; this is not hostile same-UID race immunity or complete tool/configuration conformance.
Opaque historical `WorkspaceSpec` identifiers are not silently treated as resource hashes.
SQLite's `worker_authorizers` require the original durable request, same-controller claim, current
fence/lease and live grant with explicit read/write/execute rights for the writable project bind.
A distinct immutable worker-send stage is reserved before native send;
lost startup/receipt or controller reopen cannot authorize another launch. This real composition
rechecks resources and authority **after** the finite Codex version probe, immediately before worker
startup. Without the opt-in gate below, no namespace settlement is supplied. Continuous revocation,
relay implementation and isolated Runtime remain unsupported.

An opt-in `LifetimeSpec` uses the installed read-only PID-1 bootstrap and one explicitly preserved
private socket descriptor. Candidate/harness code waits for a distinct durable `worker-enter` gate
after resource and authority revalidation. Linux `SO_PASSPIDFD` supplies the actual ready sender's
process handle; numeric Podman PIDs, repeated inspect and namespace inode values alone are not an
ownership handoff. The retained handle is corroborated against the owned container and dedicated
PID namespace. Controller socket EOF or the original grant/lease deadline makes PID-1 exit and
invokes kernel descendant teardown; candidate processes do not inherit the socket. Unsupported
kernel/descriptor/proc coordinates refuse, without PID polling or cold numeric reacquisition.
`settled()` requires that exact retained kernel exit and owned container absence, retaining a durable
receipt for later recovery. Namespace termination does not attest cgroups, external helpers or full
effect closure. These remain bounded optional engineering, not adopted product isolation.

`collect_candidate` consumes this settlement gate and the candidate subtree fixed in the original
resource binding. It pins private ancestry/workspace identity and traverses through held descriptors,
rejects all collection refusals, durably stores exact bytes and then an immutable Candidate proposal.
Private runtime roots are not implicitly published; changed roots/identities and absence-only claims
refuse. This does not implement general Git materialization or workspace deletion. Those lifetime
steps, continuous revocation and complete Runtime/Workspace/channel reception remain unfinished.

After a successful lifetime-gated `connect`, `OwnedOCI.runtime` consumes only that owned connection
through the existing `CodexRuntime`. Its controller-resolved cwd must equal the verified host workspace;
native thread configuration instead receives the fixed `/workspace` bind destination. SQLite's
`worker_active_authorizer` rechecks the exact admitted worker/entry binding, original lease and current
grant without reserving a new send, including after receipt acceptance. Runtime observation/reconciliation
checks current authority; expiry, revocation or resource drift retire the owned boundary rather than
reading or collecting further work. Cancellation and uncertain starts also retire it, even when the
interrupt RPC fails. Native terminality alone does not report boundary terminality or permit collection:
both retained init exit and owned container absence are required. Exact confirmed completion is retained
in the active adapter so settlement can finish after stdio closes. Cold native connection reconstruction
remains unsupported. These are synthetic-composed regressions, not native conformance of the full
factory, continuous effect mediation, independent review, complete AC4 or `ISOLATED` activation.

`CodexStdio` can also receive an explicit `worker_command` and `worker_environment`, separately
from its pinned Codex version-probe command and environment. Both subprocess environments must be
explicit in this mode; no ambient merging or shell interpretation occurs. The installed
`attached_run_argv` serializer is shared with the native OCI proof. Construction starts the worker
and initializes immediately, so trusted composition must establish current launch authority and
verified resources **before** construction, not through later Runtime thread/turn checks. The
optional `before_worker` hook permits a last current check after the probe and before worker send;
the trusted composition above consumes it, rather than treating a callback as authority. A host
version probe does not attest the in-boundary executable. These seams do not supply launch admission,
container/descendant settlement or an isolated Runtime; existing direct-Codex behavior is preserved.

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
foundations and bounded intake are not evidence that ordinary execution, rich routing, gateway,
product review, isolation, full costs or
Console integration are complete. No Creatidy cloud account is required by the target architecture.

## Development Authority

[Forgejo](https://forgejo.creatidy.com/Creatidy/creatidy-kernel) is canonical for source, issues,
pull requests and development CI. Forgejo is also the first reference forge behind the existing
neutral Forge port; autonomous runtime integration remains planned. GitHub is the
[public mirror](https://github.com/creatidy/creatidy-kernel), not the place to develop a second fork
of the project. Integration targets `develop`; `main` promotion and release tags remain human-owned.
No package or release is published by A0.

Repository commands are `/implement-issue`, `/review-pr`, `/finish-pr` and `/loop`;
see [AGENTS.md](AGENTS.md) and the repository-local `.kilo/command/` files. Standalone
implementation requires one owner-selected issue; review is read-only and finish performs
bounded remediation without merging. Only an explicit owner `/loop` invocation delegates
fresh canonical issue selection, independent review, approved Forgejo PR merge into `develop`,
verified acceptance/issue closure and continuation. The primary context is the sole orchestrator
in one normal checkout by default, with a locally excluded delivery ledger and at most ten whole-PR
reviews per issue delivery. A specific owner-authorized task may use an isolated temporary worktree
to preserve another active checkout. No external controller, cross-repository mutation or Scarcity
Router operation is used for this development workflow. It does not implement the product runtime,
authorize product GO, touch `main`, release or deploy. A workspace reload may be needed to load
changed commands or the read-only `pr-reviewer` agent.

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
`--repo` remains an advanced override. Preflight and every new native admission share the same
local canonical-origin, clean-checkout, exact-base and frozen structural-baseline gate. Dispatch
does not run the network/Router/Codex preflight. Readiness is never reused as execution authority.

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
and dispatch check explicit `--repo` identity. Work happens in a disposable clone;
the controller itself creates the candidate commit from the workspace tree. The base is durably
frozen, and recovery uses that exact base even if a source branch moves. Changed paths must remain
inside the task's frozen allowed set for acceptance. That is not a worker sandbox. Verification
now validates trusted evidence in order and rejects changed-path/structural preconditions before
candidate commands; trusted-development capture still truncates after buffering output.
[#50](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50) and
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

`task run --deadline UNIX_SECONDS` records an absolute deadline no more than one hour ahead. A new
task defaults to 55 minutes ahead; recovery without `--deadline` retains the original deadline and
rejects envelope/version/mode/source changes. The CLI keeps its dedicated owned connection open
while running/waiting and advances the same recorded turn, rather than closing after one step.
Expiry and Ctrl-C journal cancellation separately; lost replies are never blindly reissued. Target
terminality is not interrupt-delivery or descendant-settlement proof, and cancellation is not rollback.
Ctrl-C is deferred across finite framed RPCs so the original receipt can be cancelled before owned
teardown; framing/transport failures still leave delivery uncertain. A durable receipt published
before a crash can bind the cancellation target without rewriting its intent or resubmitting work.
Cancellation before Attempt preparation remains a stable no-dispatch result; recovery never
reactivates allocation or execution and does not delete the original envelope or stop intent.
Expired work remains observable but cannot be newly dispatched or accepted; checks and admission
use actual decision times. This is a synthetic-proved owned-session repair, not detached supervision
or a live conformance receipt. Verification commands retain finite timeouts; full monetary/quota,
hidden-call and auxiliary-effect accounting remains unimplemented. Remaining work belongs to
[#56](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/56) and the separately proved
native integration under [#53](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53), not
promised detached recovery features. [#48](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/48)
supplies the bounded owned-session repair.
Native-shaped offline fixtures are not live model or remote-Forge acceptance.

The separately opt-in [ADR 0008 verification-only profile](docs/adr/0008-linux-bubblewrap-verification.md)
uses native Linux bubblewrap and libseccomp for untrusted test children, including such execution
for review. A trusted controller supplies exact subjects/recipes/resources and current authorization;
candidate JSON or prompt text cannot select mounts or native policy. It is not activated by `task run`,
ordinary preparation, a profile migration or Codex workspace-write. Trusted-development remains
non-isolated; worker/harness and cold-reviewer model context isolation remain deferred.

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
repository `/finish-pr` and `/loop` are development workflow, not its implementation or budget.

`task export` is a thin persisted task/Allocation/candidate/acceptance/PR summary, not the richer
reference export or a task-integrated `core.outcomes` pipeline. It lacks full requested/resolved/observed
identity, provenance, usage, review/remediation and cancellation history. A stored PR receipt is
historical evidence, not current remote state. [#58](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/58)
covers versioned private full-path outcome/cost export, preserving unknowns and causal limits.
Current status/export open SQLite themselves and are not concurrent views of an active exclusive
controller. [#57](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/57) owns the served
snapshot/events/command/notification contract; no future API/flag is asserted here.

## Ordinary Preparation

Ordinary issues use the installed Python API `creatidy_kernel.ports.intake.OrdinaryIntake`,
not `task run`, a new CLI approval flag, or a remote approval endpoint. Preparation takes one
controller-selected `IssueSubject` (HTTPS origin, repository, issue number, branch and exact base)
and untrusted `Declaration` bytes. It returns a versioned inert `Draft`, including evidence-backed
relevance and explicit unknowns. An ordinary draft is not an approved `ProgramSpec`.

The trusted local controller composes `ForgeIntakeEvidence` with a read-only `ForgejoForge`,
its exact `ForgeBinding`, `GitContentSource` over vetted controller-owned Git objects, an identified
`BaselineReader`, explicit `ContentCriterion` values, a clock, and a finite `max_pages` scan bound.
No recipe, hook, test, inference, worker or Forge effect runs during preparation. Baseline evidence
must already exist and bind the exact selected source and recipe. Empty checks and unknown baselines
are not passes. The built-in positive relevance producer supports literal exact-file-content
obligations only, not general semantic correctness. General criteria yield an unresolved-equivalence
draft; missing objects, unsupported forks, partial/moving scans and permission failures remain distinct.

`prepare(task_id, selected, declaration, expected_parent=...)` appends immutable amendments when
meaning or evidence changes. Reobserving unchanged content returns the existing revision. `history`
recovers those original bytes. `approve(principal, draft, policy, decision_id=..., expires_at=...)`
requires a current owner `Principal` supplied **outside** proposer/Console data, an explicit trusted
`OwnerPolicy` (including budget, authority, scopes, recipes, producer identities and freshness), and
fresh current evidence. It persists the exact decision and consumption and creates/amends a domain
Program that remains **DRAFT**, without an Attempt or worker grant. Unknown required meaning/evidence,
expired decisions, stale revisions, issue edits, changed baseline and unapproved scope changes refuse.
A failing baseline is admissible only for its exact explicitly approved repair reference.

Host authentication is the existing trusted-caller assumption in `ports.authority`, not authentication
by a label or JSON field. The controller must authenticate callers and protect policy, store and
reader composition before supplying a `Principal`. Workers and Console must never be given this
trusted controller access. No same-UID hostile isolation or new authenticated channel is claimed.

`handoff(principal, approved, policy, ScarcityRouterAllocator)` revalidates current authority and
delivers every original declaration field/byte, evidence subject, revision and decision to the real
Router translator. Explicit typed markers received under #51/Router #175 return an inert
`ResourceRequest` retaining the full handoff; unsupported textual meaning returns
`ordinary_requirement_mapping_unavailable` with safe field-specific `problems`. Neither result performs
transport or enables ordinary execution. The historical exact-encoding/private-local fixture still
refuses intact, without reference/L0 fallback or changes to frozen #143/#166.

The trusted controller's `adapters.scarcity_router.requirement_markers(owner_requirement,
interface=owner_binding)` generates the single quality marker and optional interface marker for
review **inside the original Declaration bytes before approval**. It does not choose/calibrate a
profile or infer numerical minima from prose. The format follows Router
`5c48d51f1eb1f11424a5100eb2ccf20a6cba4581` (D-075): complete `TaskRequirement` in
`scarcity-router.requirement.v1:`, optional existing `profile_id`, and existing `RequestBinding` in
`scarcity-router.request.v1:`. Nonempty textual `context` and `unknowns` refuse. Result criteria,
recipes, path/network/effect requests and provenance stay bound to Kernel's original approval,
not converted into Router permission.

Only explicit `intake.select(principal, approved, policy, configured_allocator)` requests a model
recommendation, rechecking current approval after transport before returning it. Configure exact
`runtime_bindings` and independent `RuntimeSupport` facts (runtime/version/evidence, supported
structural features, output allowance); Router cannot create harness support. Optional profiles
require an independently configured `profile_policy_version`, the complete approved expanded floor,
and exact returned profile/version/requirement equality. There is no local profile catalogue or
floor lookup. Selection is not reservation, gateway admission, privacy enforcement, a worker grant
or ordinary dispatch. The reference application refuses to allocate new ordinary work via its L0
path. #52/#53 still own executable-route/native reception.

`make package-check` exercises preparation, approval, intact refusal, typed recommendation over
synthetic loopback HTTP and exact evidence reopen in an isolated installed wheel with synthetic
Forgejo-shaped reads and real local Git objects. It also exercises offline
host-qualified source acquisition, an owned use lease and the shared exact local admission gate.
The preparation fixture is also runnable as
`uv run --locked python tools/check_prepared.py <existing-empty-native-directory>`; it creates only
synthetic local state, not a real task or live approval. See [contract reception](docs/architecture/contracts.md#local-intake-reception)
for the strict local declaration format and [migration](docs/architecture/migration.md#ordinary-record-compatibility).

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
  canonical origin before every new use, refreshes it with a bounded fetch, and fails closed if the
  refresh fails. The location is `source/v2/<sha256(canonical HTTPS origin)>/<owner>/<repository>`:
  hosts and non-default ports are distinct; explicit port 443 is equivalent to the default.
  There is no sibling-directory or workspace-layout search; a dirty cache is refused.
  Stable per-repository native `flock` files in `source/.locks-v2` coordinate processes and threads.
  An acquisition/object-copy lease has a finite contention window (`source_busy`), not a lock held
  throughout a healthy model session. Native Git children retain the same lease descriptor if the
  controller dies while they still use the source; model sessions do not inherit it.
  The JSON ownership marker binds canonical URL, root/location,
  checkout/Git-directory/lock inodes and UID, with its token matched to the controller metadata
  on the same locked inode; plain historical `1` markers are not proof.
  Unknown/corrupt/foreign entries and interrupted unique staging trees are retained, never deleted,
  replaced or adopted. Do not delete lock files or relabel unknown data to force readiness.
  Explicit source overrides are read-only; their local executable Git configuration, symlinked Git
  paths, alternate objects, linked worktrees and gitlinks/submodules are refused. Git's inert
  no-include config listing validates the actual grammar before operational commands. No source hooks,
  filters, fsmonitor or ambient credential helpers run. Source transport keeps TLS verification and
  refuses redirects. A local mirror is an explicit advanced transport, not a new source identity.
  See [source compatibility](docs/architecture/migration.md#source-cache-compatibility).
- **Codex.** `CREATIDY_KERNEL_CODEX_BIN`/`VERSION` are optional: Kernel deterministically resolves
  the explicit override or the first `codex` on `PATH`, probes `codex --version` in the closed
  operational environment, and validates the version shape and native schema with the existing
  inference-free mechanisms. A unusable resolved installation is reported by safe category and is
  never silently substituted with another installation.
- **State.** Task state defaults to `${XDG_STATE_HOME:-~/.local/state}/creatidy-kernel/task`;
  the SQLite storage-safety requirements are unchanged, and `--data-dir` remains the explicit
  override.

Source leases cover local validation and copying exact objects into controller-owned bare storage
with no hardlinks, then validate that snapshot before new thread/turn starts, including after
blocking allocation/authorization callbacks. Explicit source owners must not move or modify their
checkout during the copy cut; Kernel writes no lease/lockfiles there. The Path-returning acquisition
API is an unleased compatibility handle; Python consumers use `source_use` through their own copy.
Only supported native local Linux filesystems are accepted, using the existing storage topology
gate. This is cooperative trusted-local-controller ownership, not hostile same-UID isolation, a
multi-task controller proof, NFS support or an upgrade of the separate verification-only sandbox.

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

Ordinary model pins and supported tool/context/reasoning requirements tighten only existing
`TaskRequirement` fields. Streaming/structured-output and harness feature demands are independently
checked and retained separately, never invented as `/v1/select` wire fields. Executable resource pins,
client profile aliases, explicit output ceilings, unsupported access/channel fields, vision and privacy
demands refuse before transport. `null` optional minima mean no additional demand, not unknown
adequacy; false booleans are not denials or grants. The selected exact model/variant/effort must still
match controller facts. Safe categories distinguish unsupported/invalid requirements, no eligible
selection, incompatible harness, unreachable endpoint, HTTP rejection and malformed response.
Ordinary Allocation version 2 additionally retains the complete approved draft/revision/evidence,
decision bytes, Program digest, actual sent request, interface and independent compatibility facts.
Legacy/version-1 Allocation bytes stay unchanged and readable; old L0 decisions are never relabelled.

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
