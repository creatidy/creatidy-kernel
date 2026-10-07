# ADR 0008: Linux Bubblewrap Verification Boundary

Status: owner-selected direction, 2026-10-07; local verification-only implementation and native
reception below. Independent whole-PR review and canonical integration remain to prove.
Issue: [Kernel #50](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50).

## Decision And Scope

The owner selected daemon-free Linux bubblewrap as the fail-closed enforcement boundary for the
first hostile **verification/test execution** profile. Filesystem and network access are denied
by default; only explicitly required workspace and trusted toolchain resources are exposed.
The decision is recorded in [the issue](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/50#issuecomment-15516).

This receives verification/test children, including such execution performed for review. It does
not isolate a complete model/harness/worker session, attest every native tool invocation, supply
remote approval authentication, or enable ordinary live execution. The broader G09 requirements
remain distinct. A sandboxed verifier cannot retroactively isolate the harness that produced its
candidate. Existing trusted-development remains explicitly selectable and labelled non-isolated;
configuration migration must not silently promote it.

Managed rootless OCI remains deferred until reproducible image/toolchain or whole-worker isolation
requirements justify its runtime, lifecycle and operator surface. No container daemon is introduced
as a dependency of this profile. No custom sandbox framework or credential store is constructed.

## Existing Mechanism And Rights

The inspected upstream basis is bubblewrap v0.11.0, exact public commit
[`9ca3b05ec787acfb4b17bed37db5719fa777834f`](https://github.com/containers/bubblewrap/tree/9ca3b05ec787acfb4b17bed37db5719fa777834f).
Its source and `COPYING` are LGPL-2.0-or-later. Commercial use is permitted. Kernel invokes the
separate utility and does not copy or translate its implementation or bundle its binary into the
Python package. Redistribution of bubblewrap itself would require its licence/notices and
corresponding-source/build compliance; attribution alone would not suffice for copied code.
Its libcap dependency and optional caller-side seccomp support have their own distribution terms.

The upstream README and SECURITY document a construction toolkit, not a security policy. Kernel
owns the selected resource policy, authorization checks, invocation and reception evidence. The
upstream version string is not by itself executable provenance or a denial receipt.

## Fail-Closed Profile Requirements

The implementation must require namespace facilities explicitly. In the pinned source,
`--unshare-all` selects user/cgroup **try** variants; it is not sufficient for this profile.
Required namespace operations use explicit user, PID, network, IPC, UTS and cgroup isolation,
with mount isolation supplied by bubblewrap itself. Missing or denied enforcement refuses before
the command, without retrying unsandboxed, dropping a namespace, or using `-try` options.

Require capability dropping, no-new-privileges, a separate session, parent-death handling, the
normal PID-1 reaper and effective prohibition of further user-namespace creation. Bubblewrap's
default capabilities, a setuid binary, or a feature name is not adequate evidence. Seccomp must
actually be installed with the supported architecture and network/escape restrictions; an
unknown ABI or missing feature refuses rather than admitting a weaker path.

Start from an empty sandbox filesystem, not a read-only bind of the host root. Bind only approved
resources, read-only unless a particular writable workspace/scratch resource is explicitly needed.
Host `/usr`, `/etc`, HOME, credentials, controller state/artifacts, package caches and sockets must
not be exposed merely to make an import work. A read-only mount of a credential or socket is still
exposure, not denial. Trusted interpreter, loader, shared libraries, standard library and required
test dependencies need an identified resource closure; compiler/build resources are not implied
runtime resources. Runtime compilation would need a separate explicit resource decision.

Use a closed, declared environment with synthetic HOME/temp/cache. Candidate text, instructions,
requested resources or grants cannot choose the bubblewrap binary, trusted toolchain, namespace
options, seccomp policy or extra mounts. Resolve and constrain source/destination paths and
reject unauthorized overlap, symlink/mount escape and socket/device exposure. Workspace snapshots
must not import controller `.git` state, untracked credentials or inherited private data by accident.

Close inherited descriptors; only controlled synthetic stdio and specifically consumed setup
descriptors may be passed. A directory, file or connected socket descriptor can bypass pathname
or network isolation. Use pipes/DEVNULL rather than an owner terminal or private output file.
Network namespaces alone permit local socket operations; the profile must establish the promised
network denial, including inherited, pathname and abstract sockets and alternative syscall paths.

The trusted controller checks the exact verification subject, recipe, bounded authorization and
current expiry/revocation before launch. Prompt or candidate data never supplies a trusted policy
or expands its grant. A rejected scope/tooling precondition prevents executing that candidate
code; collecting failure evidence after executing it is insufficient. Trusted verification code
and gate definitions cannot come from the candidate being judged.

Timeout/cancellation targets only the dedicated owned sandbox process/namespace. Prove settlement
or retain explicit unknown liveness and unsafe-cleanup refusal; process exit is not a general
host descendant census. No other session, process group, workspace or control path is signalled.

## Evidence And Compatibility

Actual positive execution and negative denial tests are required on the exact native binary and
supported OS/kernel configuration. Source inspection, a version response, a successful bootstrap,
or green synthetic mocks alone cannot close the denial requirements. Tests cover controller and
artifact reads/writes, synthetic owner/provider/Forge credentials, host parents, symlink/mount/proc
and descriptor escapes, host/container sockets, unauthorized networking, unsupported enforcement,
stale/revoked/expired authority and owned timeout/parent-death cleanup. Test payloads use synthetic
resources only, never real owner credentials or private state.

Record upstream pin, binary digest, build/dependency versions, architecture/kernel configuration,
resource manifests, enforcement state and residual limitations. The first tested platform is
Linux; Windows/macOS or an unsupported Linux environment must refuse this profile. WSL is not
supported merely because its kernel says Linux: the actual facility and denial tests must pass.
The shared kernel, explicitly exposed toolchain, controller/host administrator and broader DoS/
resource accounting remain separate risks; do not label this a VM or full hostile-worker proof.

The native dependency is optional for import-time safety and trusted-development use. Selecting
the hostile profile makes enforcement mandatory. Setup may build the pinned external utility
with intentional locked developer tooling, but deterministic test execution must not download or
install dependencies. Required native reception must not be reported passing when skipped or
when the command failed to start. Repository checks, installed-package smoke, independent
security-critical whole-PR review and canonical CI remain unchanged acceptance gates.

This ADR records the owner's selected boundary. Any implementation or installed/live readiness
claim is conditional on the named tests and receipts, not an authority grant from this document.

## Verification-Only Reception

`ports.verification.VerificationProfile` is separate from worker `TrustMode`.
`TRUSTED_DEVELOPMENT` remains non-isolated and unchanged. The opt-in
`adapters.bubblewrap_verification.BubblewrapVerifier` does not upgrade Codex, task run,
ordinary preparation, the host controller, or a cold reviewer's model/harness context.
Verification and review callers can use this same child-test executor, not a weaker reviewer
test path. There is no model launch, new daemon, approval service, grant database or policy DSL.

The trusted controller supplies `VerificationInvocation` and `VerificationAuthorizer` outside
untrusted data. The callback must authenticate its caller and compare the exact candidate, Attempt,
approved spec, policy, repository, base/head, recipe, resources, snapshot digest and current
expiry/revocation. It is consulted before setup, immediately before launch and during execution;
loss cancels only the owned bubblewrap PID. A caller label is not authentication. The executor also
requires its constructor-fixed resources; even an erroneous callback cannot add mounts.
Current authority is checked again at completion, and the receipt records that decision time.

Snapshots contain explicit regular-file bytes, not a checkout directory. The collector must use
the exact controller Git subject and approved file set; this adapter never discovers tracked or
untracked files. Absolute/traversing/noncanonical/duplicate paths, `.git` and hidden paths refuse.
Symlinks, devices, sockets and inherited checkout metadata cannot be represented. Created symlinks
stay inside the namespace. This does not certify that a trusted collector never included secret bytes.

Resources are explicit read-only files/directories under `/toolchain`, with deterministic closure
digests. Symlinks, nested mounts (including file binds), sockets/devices, foreign ownership,
group/other writability, setuid/setgid and file capabilities refuse. Utility/libseccomp additionally
have caller-pinned raw SHA-256 values. The host must protect these resources and their ancestors
against concurrent same-UID/administrator modification: the unisolated parent is trusted. Hashes
do not establish upstream authorship; the source pin is setup provenance, not binary attestation.

The tested Python closure contains Python 3.12.3, its loader, libc, libm, zlib, libexpat, libffi and
standard library without config/build archives, site/dist-packages or bytecode caches. Only these
specific resources are mounted, never host `/usr`, `/etc`, HOME, package caches or controller state.
The loader uses an explicit library path and synthetic `PYTHONHOME`. Scratch is newly created under
an owned private parent; only the snapshot workspace is a writable host bind. `/tmp` and `/home/test`
are private tmpfs, `/proc` belongs to the new PID namespace, `/dev` is synthetic and the remaining
root is read-only. Additional dependencies require a reviewed closure, not wider imports/mounts.
The new proc filesystem's PID-1 subtree is masked with a read-only native tmpfs mount: the initial
same-UID negative found reaper anonymous descriptors reachable via `/proc/1/fd`. The mask prevents
reopening its event/lifetime FDs; `/proc/self` remains available for the child and denial proof.

Installed libseccomp 2.5.5 (Ubuntu `libseccomp2` copyright, LGPL-2.1) compiles the fixed policy lazily.
No library source or BPF implementation is copied. Only native x86-64 is supported; alternate
x86/x32 ABIs terminate. Socket/namespace/mount/ptrace/foreign-FD escape operations return EPERM,
clone namespace flags refuse, clone3 returns ENOSYS and io_uring is denied. Other shared-kernel
syscalls remain attack surface: this is not a syscall allowlist, VM or full resource/DoS boundary.

`close_fds=True`, DEVNULL and controlled pipes deny inherited files/sockets/terminal/control FDs.
Only consumed seccomp/native-info, PID-1 lifetime and anonymous bootstrap setup descriptors are
passed. A fixed trusted Python `-P/-S` bootstrap (outside candidate code) signals readiness, waits
for one anonymous exec token, closes its setup FDs, then execs the exact authorized recipe. EOF
refuses execution. Bubblewrap's `--block-fd` is deliberately not used: its EOF also releases the
command. The controller binds the native producer's exact direct init child with a pidfd, checks
ownership, and rechecks current authorization after bootstrap readiness before releasing exec.
The fixed bootstrap requires the declared Python/loader closure; it has no policy parser or model
loop and implements no namespace, filesystem, network or seccomp enforcement itself.
Stdout/stderr are drained
continuously and retained at most 64 KiB each as private untrusted data, never public diagnostics
or authority. Timeout/cancel kills only the owned direct PID; parent-death handling terminates
PID-1, whose namespace teardown kills descendants including setsid children. Both lifetime EOF
and exact PID-1 pidfd exit readiness are required before cleanup: EOF alone can precede kernel
descendant teardown. The managed Python 3.12.0 build lacks `os.pidfd_open`; the same required
kernel operation is available through libc's maintained `pidfd_open` ABI, checked lazily before
launch. Missing libc/kernel support refuses; there is no PID-polling or weaker-settlement substitute.
Missing settlement is `settlement_unknown`, retains owned
scratch and never becomes a passing command. This is not a host process census or a guarantee
against uninterruptible kernel I/O. Parent crash can leave inert scratch for trusted owner cleanup.

Local actual native reception on 2026-10-07 used Ubuntu 24.04.5, Linux
`6.18.33.2-microsoft-standard-WSL2`, x86-64 and the actual unprivileged user:

| Input | Exact value |
| --- | --- |
| bubblewrap version/source | `0.11.0`, `9ca3b05ec787acfb4b17bed37db5719fa777834f` |
| Source archive SHA-256 | `552cec9c79bb85c8ecd25ae3e55efca9af03390c17d829885ee45722dedada4f` |
| Tested binary SHA-256 | `a75623409c0755c93ff5bba87ec4c407bf3dcb01d7b0d3c9802ee9a02a272e6f` |
| Repository setup rebuild SHA-256 (also natively tested) | `8f928cda894d165a835f058e82e91cb84b5210a38b988fd25b305fba2eb0437d` |
| Loaded libseccomp SHA-256 | `b4d9140a7797ccf3ac14f5d9d579b64f157111662940cb8330c632b11a8cea8e` |
| Build tooling | Meson 1.12.1, Ninja 1.13.2, GCC 13.3.0, extracted libcap 2.66 development/runtime prefix |

`test_bubblewrap_verification.py` runs Python and checks no-new-privileges/seccomp/zero capabilities,
PID-1 layout and userns refusal; it attempts synthetic controller/artifact/credential/host-parent
reads/writes, symlink/proc/FD and mount/privilege/syscall escapes, IPv4/IPv6/UNIX networking and
host loopback/pathname/abstract/container listeners. Alternate ABI children actually receive SIGSYS.
Timeout floods and cancel/revocation/expiry/parent-death tests include actual setsid descendant
locks: finite reacquisition proves release, not only monitor exit. The leaf userns sysctl is not
the exhausted parent's counter; `--assert-userns-disabled` verifies refusal before seccomp/exec.
An additional parent-crash fault before the exec token proves no candidate marker effect and
actual namespace-init pidfd settlement. Synthetic observer loss retains scratch despite a zero
command exit; that injected observer test is not a native denial claim. A live synthetic bystander
survives cancellation. Private stdout/stderr and snapshot bytes are excluded from object repr.

`test_verification_preconditions.py` proves forbidden Makefile/pyproject/tool/AGENTS changes cause
no command marker effect and direct TaskChecks calls cannot bypass structural rejection. Core
checks stop on invalid/missing/stale/worker/failing evidence before later producer calls. Unrun
checks receive findings, never invented evidence. Historical Task policy order/reference,
#143/#166 digests and recovery encodings are unchanged; command-local prerequisites do not rewrite them.

`make native-proof` requires an explicit setup binary. Normal pytest reports native cases skipped
when absent: that run does not prove this profile. CI requires equally explicit supported
unprivileged setup. Unavailable namespaces refuse without changing host security settings or
falling back. Local reception does not assert canonical CI, independent review or full G09 completion.

The offline setup tool initially refused the upstream archive's `LICENSE -> COPYING` symlink.
Inspection confirmed that exact internal link in the pinned archive; setup now permits only that
known link and retains safe extraction/bounds. The subsequent repository-owned build and native
reception succeeded. The two binary hashes differ with build paths; no reproducible native binary
claim is made. Python wheel/sdist reproducibility remains a separate required package gate.
