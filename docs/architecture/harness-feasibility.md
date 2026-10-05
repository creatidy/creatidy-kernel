# Bounded Harness Feasibility

Research result for [Kernel #47](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/47),
2026-10-05. This receives a **demonstrated gap and bounded adaptation direction**, not a working
new adapter, selected harness, approved dependency or live integration. Follow-on implementation
remains separately selected. No new protocol, plugin, supervisor, runtime behavior or model ranking
is introduced. [ADR 0007](../adr/0007-shared-harness-routing-observability.md) remains authoritative.

## Owner Direction And Verdicts

The owner selected **Owned Sessions** on 2026-10-05 and explicitly resumed the stopped development
invocation: first prove dedicated Kernel-owned sessions/processes, with read-only UI observation
and explicit controller-exit limitations. Independent supervision and existing IDE-session handoff
are deferred, **not requirements proved or silently removed**. This choice does not select ACP,
Codex, Kilo, Claude or ZCode, upgrade trust, authorize inference or permit takeover of owner sessions.

| Boundary | Bounded verdict | Consequence |
| --- | --- | --- |
| Existing harness control | Demonstrated lifecycle/recovery/ownership gaps; bounded native and ACP adapter candidates | Keep Runtime/Workspace and durable Operation seams; prove a specific version before #53 integration. No universal control PASS. |
| Router-backed model inference | Direct Responses and Anthropic Messages ingress unsupported at inspected producer revision; Chat Completions is a conditional subset | Native control compatibility does not establish backend compatibility. No translator or gateway consumer selected. |
| UI/client/controller closure | Owned-session proof direction approved; detached supervision and IDE handoff unproved | Never kill a shared server for one Attempt, attach as a competing mutator or claim controller-exit survival. |
| Permissions/isolation | Protocol capability and trusted-development mode are not OS enforcement | #50 must receive the actual constrained executor and isolation proof before untrusted execution. |

## Evidence Cut

Kernel source: `e5f30c9cef89e9991c12cae924093cf95d87d75b`, the clean canonical develop base.
Relevant local files are `ports/execution.py`, `core/execution.py`, `adapters/codex_runtime.py`,
`adapters/codex_stdio.py`, and tests `test_codex_runtime.py` / `test_codex_stdio.py`. Runtime exposes
`start`, `observe`, `candidate`, `cancel`, `reconcile`; Workspace separately owns materialization,
artifact collection and cleanup. Native method names cannot supply missing authority or acceptance.

Public Router source was resolved twice through read-only canonical metadata to
`6c337a400b4cdc28ded543d2ae8ccf3749d79cb7`. Official candidate sources below are exact inspected
snapshots, **not installed binary or supported release attestations**. Changing a revision invalidates
its conformance evidence. [Canonical research evidence](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/47#issuecomment-14697)
records executed probes, provenance, research limits and the pre-decision stop.

No credentials, existing session histories, runtime state or private source were inspected. No
candidate code was copied, installed or added as a dependency. Source licensing and subscription,
service, account, data and redistribution rights are separate. Unknown rights remain unknown.

## Candidate Matrix

ACP is evaluated first as required, not adopted by default. Links pin the source tree; the named
files below provide the substantive evidence rather than a current mutable documentation page.

| Candidate / source pin | Version and inspected files | License / access evidence | Disposition |
| --- | --- | --- | --- |
| [ACP](https://github.com/agentclientprotocol/agent-client-protocol/tree/cae7aca7f07cefd59da2a3cb374933cb623a2959) | Stable wire v1, schema crate 1.10.2; `agent-client-protocol-schema/Cargo.toml`, `docs/protocol/v1/{session-setup,prompt-turn,session-list,file-system,terminals}.mdx`, `LICENSE` | Apache-2.0 protocol/schema; selected SDK and backend/account terms separate and unverified | Candidate control interface, not an executor or backend API. Optional/unstable capabilities must be evidenced independently. |
| [Kilo](https://github.com/Kilo-Org/kilocode/tree/76bcfd40be616a72f4697b3041565f322245b462) | Source CLI 7.8.3, ACP SDK 0.21.0; `packages/opencode/package.json`, `src/acp/{service,event,permission}.ts`, HTTP session handlers, `src/tool/{write,task}.ts`, `src/cli/cmd/attach.ts`; `packages/kilo-vscode/src/services/cli-backend/server-manager.ts` | Root MIT with Kilo/OpenCode attribution; bundled/dependency closure and exact Kilo/provider service eligibility unverified | Native HTTP/SDK and ACP are candidates. Extension server ownership does not imply a Kernel-owned shared session. |
| [Codex](https://github.com/openai/codex/tree/4ad985e2caaf877b96dafd8138dae2def467e01e) | Installed CLI 0.159.3 separately probed; source `codex-rs/app-server/README.md`; existing Kernel adapter pin is `3fd5160cd6c78f2051bb54359d53f09207171733` and its v2 generated schemas | Existing reuse record establishes Apache-2.0 native protocol; bundled attribution/account/service entitlement and binary-to-source correspondence not re-attested | Retain existing bounded adapter as evidence, not a complete lifetime/gateway proof. New source snapshot cannot replace its pin silently. |
| [Claude Python SDK](https://github.com/anthropics/claude-agent-sdk-python/tree/9c69ce7aced5cdf2aa1ac86fe62e877b4962de8b) | SDK 0.2.163, declared bundled CLI 2.1.289; `src/claude_agent_sdk/{client,types,_version,_cli_version}.py`, `_internal/transport/subprocess_cli.py`, `_internal/sessions.py`, `README.md`, `LICENSE` | MIT wrapper; README invokes Anthropic Commercial Terms. Bundled Claude Code is not established as MIT; account and legal applicability unverified | Candidate native client; CLI internals, IDE attachment and complete descendant handling not proved by Python wrapper. |
| [ZCode](https://github.com/zai-org/ZCode/tree/29628c9acdb81b703bbd4080c207a0e7ce5e276e) | Source package 3.14.3; `package.json`, bootstrap `zcode-protocol-v4/{command-inbox,v4-gateway,cold-session-resume}.ts`, `commands/handlers/{session-flow,session-mgmt}.ts`, adapters `node-execution-adapter-lifecycle.ts`, core `permission/service.ts`, `subagent/context-builder.ts`, `LICENSE`, `NOTICE.md` | Root Apache-2.0 at this exact revision, correcting the historical issue inventory's MIT claim. Independent bundled components and service/access-mode rights unverified | Candidate native control with useful command-key hooks, not universal durable Kernel receipts or accepted workspace authority. |

Kilo paths prefixed `src/` in the table are within `packages/opencode/`; ZCode bootstrap/adapters/core
paths are within `apps/zcode-cli/packages/`. Version declarations describe source, not what is
installed here. Kilo/Claude/ZCode executables were not found on this context's PATH; IDE installation
absence is **not** established. Complete current-supported platform/version matrices remain unverified.

## Runtime Obligations And Gaps

| Obligation | Existing surfaces | Required guarantee / missing evidence |
| --- | --- | --- |
| Start and receipt | ACP session/new then session/prompt; Kilo create/prompt or promptAsync; Codex thread/start then turn/start; Claude connect/query; ZCode createSession/sendText | Persist Operation intent/claim and the exact accepted native handle. A transport write or accepted session is not accepted task execution. |
| Observe | ACP session/update and stopReason; Kilo events/status/messages; Codex thread/read and turn status; Claude result/task messages; ZCode topics/snapshots | Bind observations to exact Attempt/handle, preserve waiting versus running/terminal/unknown, freshness and honest requested/resolved/observed identity. Quiet work is not stalled. |
| Candidate | Native diff/result/file/artifact surfaces | Controller collects immutable Workspace artifact manifest bound to Attempt/spec/exact candidate. Done text, diff display or result message is not acceptance. |
| Cancel | ACP cancel; Kilo abort/session-tree cancel; Codex turn/interrupt; Claude interrupt/stop_task; ZCode foreground stop/background cancel | Journal cancel separately, observe terminality and descendant settlement. Interrupt acknowledgment cannot authorize cleanup or prove rollback. |
| Reconcile | Capability-gated ACP list/load/resume; Kilo session reads; Codex durable handle plus thread/read; Claude session readers; ZCode command inbox/persistent hooks | Reconcile exact original Operation, not oldest session or repeated prompt. Missing/lost/incomplete discovery is unknown, not absence. Preserve original envelope and identity. |
| Cold review / delegation | Fresh child/thread, native review/task/subagent surfaces | Prove read-only fresh context and exact candidate/base/spec/policy. Fork/load/resume or a child identifier alone is not independent review. |

Specific negative source evidence narrows these candidates:

- ACP `fs/*` and `terminal/*` run in the advertised **client environment**; other tools can be
  agent-side. A headless client must identify and constrain its actual executor. Absolute cwd,
  workspace roots and capability flags are not path/symlink/FD/network/credential enforcement.
- Kilo ACP catches backing abort failures; session-tree cancellation tracks registered children,
  not all escaped OS/remote descendants. Its permission bridge includes an unawaited client file
  write while native tooling can also write. Client notification is not exclusive execution or
  acknowledged editor/disk convergence. The extension server manager owns watchdog/disposal;
  attaching to it does not establish server-owner handoff or UI-close survival.
- CodexStdio validates the pinned executable and supplied generated method inventory, starts a
  process group and closes it with TERM/KILL/reaping. These are owned-process actions, not a claim
  about a shared daemon or all remote descendants. The current transport rejects unsupported
  server approval requests instead of implementing general permission mediation.
- Claude query writes NDJSON; session-reading errors/history omissions cannot prove Operation
  absence. Transport close targets the direct child, not a proved descendant census. SDK types
  warn that in-process MCP tools can continue after abandoned calls. ResultMessage is not proof
  all background work has settled.
- ZCode command-key lookup depends on host wiring/persisted facts; process-local entries are not
  universal crash-safe receipts. Background Bash detaches from parent abort. Ordinary yolo mode
  allows operations before some deny checks; a permission-mode name is not an isolation profile.

## Backend Compatibility Is Separate

At the pinned Router revision, `scarcity_router/gateway_server.py` dispatches models and Chat
Completions ingress, and `gateway_openai.py` defines its closed text/function-tool vocabulary.
`docs/execution-surface.md` distinguishes implemented Chat Completions from future Responses.

| Harness/backend demand | Verdict at evidence cut | Required reception |
| --- | --- | --- |
| Codex Responses requests | Direct ingress unsupported | Producer #181 exact supported-subset implementation and consumer wire proof, not Router's outbound Codex adapter. |
| Anthropic Messages requests | Direct ingress unsupported | Explicit producer disposition and exact adapter proof; no invented translator here. |
| Kilo or another configured Chat Completions backend | Conditional candidate only | Exact configured provider/harness/version payload, tool/stream/retry/pin/auth negatives; generic provider support is insufficient. |
| ACP or ZCode native control protocol | Not a backend compatibility claim | Trace the actual model backend separately; backend-native editing must first receive Router #180 authority/migration decision. |

`machine_api.py` / `selection_app.py` recommendation is not `routing_core.py` executable-resource
admission. `gateway_coordinator.py` pin provenance does not authorize work, reserve quota or attest
physical identity. Current opaque variant/effort conflation remains producer #179 work; Kernel must
not infer fields or relabel historical decisions. Dispatch target provenance is not observed identity.

`worker_bridged_adapter.py` / `worker_protocol.py` expose a qualified PARTIAL Codex client-tool
bridge. Suspension state is in memory, not durable restart/replay. Text tool results map lossily to
upstream success plus text, not attestation the tool succeeded. Chat Completions SSE is not Responses
framing. A single HTTP send path does not prove absence of harness/SDK/backend hidden retries.

Producer #174 executable handoff, #179 identity/effort, #180 editing authority and #181 protocol
conformance were open at inspection. #165 cancellation is open with `wontfix`, not a promised repair:
pending-read cancellation and some stream/tool-suspension disconnect paths lack complete cancel
propagation. A cancelled audit or disconnect is not backend/descendant settlement. Producer issue
state is a dated observation, not a substitute for source and fresh integration evidence.

## Reproducible Offline Probes

Executed on Linux `6.18.33.2-microsoft-standard-WSL2`, no live thread/turn or gateway operation:

```sh
command -v codex
codex --version
codex --help
codex app-server --help
codex app-server generate-json-schema --help
codex app-server generate-ts --help
codex app-server generate-json-schema --out /tmp/kilo/kernel-47-schema-20261005
uv run --locked pytest tests/test_codex_runtime.py tests/test_codex_stdio.py
```

Resolved executable: `/home/adrian/.local/bin/codex`; reported `codex-cli 0.159.3`; executable
SHA-256 `8bf204b36a2f6dd0dab73aa2f639892e67ef9ac8befccb4a05b1496ebf25c479`.
This identifies observed bytes, not signed provenance or a binary-to-upstream commit attestation.
Use a fresh owned scratch output directory for reproduction; inspect the installed help first.
Do not substitute app-server startup, account/session reads, doctor or daemon attachment for schema
generation. Those may inspect private state or contact services and were not run.

Generated `ClientRequest.json` advertises thread/start/read/resume, turn/start/interrupt and
review/start. Review description deprecates detached delivery in favor of separate-thread inline
review. Advertised methods are not integration proof. Kernel's initialize handshake does not supply
a supported-method inventory; a trusted caller supplies the exact binary-generated inventory.

**47 synthetic tests passed** in 2.45 seconds. The inspected tests use fake native connections and
local fake processes, never the real Codex executable. They demonstrate lost start replies remaining
unknown without duplicate turns, restore requiring a handle, missing-session uncertainty, quiet
running work/cancel races, incompatible version/method refusal before delivery, exact candidate and
identity checks, and closed child environments. They do not prove native daemon, IDE, descendant,
hostile-worker or backend behavior. No unsupported installed-version launch was attempted.

## Separately Authorized Proof Plan

Before a live probe, separately approve the exact binary/SDK/schema/platform, disposable workspace,
service/account rights, inference and tool/effect/network scope, numeric envelope, privacy and selected
security profile. Freeze source/candidate subjects and redact evidence; never use broad owner
credentials or existing owner work. No such execution permission is granted by this report.

| Scenario | Reproducible setup after authorization | Required observation / refusal |
| --- | --- | --- |
| Lost start reply | Drop reply after accepted start; restart controller from journal | Recover original exact handle/receipt or remain unknown; no second start from absence guesses. |
| Lost handle | Withhold durable handle after dispatch, including crash boundary | Distinguish unknown from proven nonexecution; refuse new dispatch until reconciliation settles it. |
| Active cancel | Controlled delayed turn with one known harmless tool | Persist cancel Operation, observe original turn terminality separately; no acceptance of late candidate. |
| Descendant still running | Owned disposable child/background tool that outlives foreground | Retain workspace and unknown settlement; independently observe/reap only owned descendants, never unrelated processes. |
| Permission refusal | Denied/expired/revoked file/terminal/network capability plus late approval | No unauthorized effect marker; identify enforcing executor, not only a final rejection message. |
| Restart / version mismatch | Restart with original journal/envelope, then mismatched version/schema | Original identity/deadline/authority retained; unsupported configuration refuses without renewed dispatch. |
| Resume versus resubmit | Native history restore with an observable prior tool effect | Prove actual continuation or explicit unknown; repeated prompt cannot be called recovery. |
| Same-owner IDE session | Independent harmless IDE session plus dedicated Kernel session | No takeover, competing mutator, unrelated session cancellation or shared-server kill. |
| UI close | Disconnect/close observer while dedicated owned controller remains; separately stop controller | Distinguish observer detach, cancel and controller exit. Record actual lifetime; no detached-supervisor survival claim. |
| Cold review | Fresh read-only native context on exact frozen candidate and policy | No author conversation/fork contamination; changed candidate invalidates verdict; formal platform review remains separate. |
| Backend/tool/stream failure | Exact producer-supported request cells, pin/effort conflicts, disconnect, tool continuation and restart | Fail unsupported protocols and unknown identity/usage honestly; no silent fallback, retransmission or unauthorized auxiliary route. |

Offline fake timeline tests can exercise the assertion shapes first; actual native/gateway claims need
the authorized observation above. UI observer tooling itself is not implemented by #47. No installed
IDE session is attached merely to test ownership. Hidden retries must be counted across harness,
SDK, tools and gateway; unavailable monetary/quota/identity observations remain unknown, not zero.

## Reuse And Downstream Reception

The ladder yields **retain existing seams and prove bounded adaptation**, not BUILD a replacement
harness. ADOPT is conditional on complete version/rights/semantic/security/dependency/maintenance
evidence; unchanged VENDOR/COPY adds no demonstrated advantage and is not performed. PORT is not
needed for this research. ADAPT may map native lifecycle and constrained executor receipts behind
Runtime; BUILD is limited to necessary Kernel authority/evidence mapping, never a new agent loop.
The existing [Symphony SPEC reference](reuse.md#agent-and-workspace-references) supplies useful
tracker/workspace/runner/status comparisons, not portable durable Kernel truth or a supervisor to
import. Kernel owns maintenance of any later selected adapter; upstream versions need explicit
compatibility review, not an automatic upgrade.

#48 receives original-envelope/lifetime/freshness/cancel repair; #50 receives actual executor and
isolation; #51 receives approved requirement semantics; #52 receives the producer executable route;
#53 receives only a specifically proved adapter; #54 receives candidate-bound cold product review;
#56 receives full-path retries/cost/byte/process bounds; #59 receives concurrency/manual-edit
ownership; #62 receives non-destructive state/profile/protocol migration evidence. Existing immutable
Attempt/Operation/Allocation and native-handle records must not be rewritten to appear newly proved.
Unknown historical evidence stays unknown, and unsupported migrations fail without destructive repair.

This research result can be accepted independently of those implementations. It does not close
G01/G04/G10, authorize live task execution or establish product GO. Later expanded supervision,
shared-session authority, security-level and contested service/identity choices still need their
specific owner decisions and proof.
