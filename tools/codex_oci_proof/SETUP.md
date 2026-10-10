# Real-Codex Worker Proof Provisioning (#53 Bounded Slice)

Temporary, user-owned development setup for the [#53](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/53)
bounded native proof: the real, version-pinned Codex app-server executing inside the exact
[#78](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/78) rootless OCI boundary.
Nothing here is a product dependency or a permanent service, and nothing privileged is
installed. The native receipt requires the #78 toolroot (`CREATIDY_TEST_OCI_TOOLROOT`, see
[oci_worker_poc/SETUP.md](../oci_worker_poc/SETUP.md)) and one real Codex binary.

## Codex binary

| Component | Version | Provenance |
| --- | --- | --- |
| codex (standalone release) | 0.159.3 | The host's installed standalone Codex CLI release, resolved via `CREATIDY_TEST_CODEX_BIN` or `PATH`; static-pie musl binary, SHA-256 recorded per run in the attempt evidence; Apache-2.0 (upstream Codex CLI). The binary is mounted read-only into the boundary; the owner's Codex home, sessions and credentials are never read — every run uses a fresh synthetic `CODEX_HOME` under the disposable workspace. |

The proof pins `0.159.3` in `tools/codex_oci_proof/codexbin.py`; a different version refuses
with a message telling the caller to update the pin deliberately. The native app-server
protocol inventory is taken from the binary itself (`codex app-server generate-json-schema`)
and checked against the four methods the Kernel Runtime contract requires
(`thread/start`, `turn/start`, `thread/read`, `turn/interrupt`).

## Run

```sh
CREATIDY_TEST_OCI_TOOLROOT=/tmp/kilo/kernel78-tooling \
CREATIDY_TEST_CODEX_BIN=/path/to/codex \
make codex-oci-proof
```

`CREATIDY_TEST_CODEX_BIN` may be omitted when `codex` is on `PATH`. Missing components are
explicitly UNPROVED (skips with precise reasons), never synthetic passes.

## Collection trust boundary

The isolated worker controls every path in its writable workspace, including symbolic links
to host paths invisible in-boundary. Post-settlement collection is fail-closed
(``tools/codex_oci_proof/collect.py``): publishable candidate artifacts come only from the
controller-declared ``/workspace/candidate`` root, symlinks are refused in every component,
exact publishable bytes are preserved and digest-bound under the attempt's controller-owned
``collected/`` tree, and everything else (``codex-home``, ``mock``, specs, observations,
scratch) is private runtime/evidence inventory retained under controller authority.

## What runs where

- **Controller (outside the boundary):** attempt identity, synthetic scenario, relay
  authority, the production `CodexStdio` transport with an explicit attached worker command
  and a separate pinned host version probe, both in closed environments,
  cancellation/expiry, settlement observation, immutable collection and verification.
- **Inside one rootless OCI Attempt:** the in-boundary launcher (`worker_entry.py`), the
  controller-authored synthetic Responses model backend (`mockmodel.py`, container-internal
  loopback only, no paid inference), the real `codex app-server` process, and every native
  tool/subprocess it spawns (`exec_command` sessions, direct `command/exec` children,
  detached descendants).
- **Image:** `tools/codex_oci_proof/ocimage.py` builds the deterministic
  `localhost/kernel53-codex-worker:v1` archive from the same pinned busybox with busybox
  applet symlinks and a minimal static `/etc` (the #78 synthetic worker invoked
  `/bin/busybox <applet>` explicitly; Codex's native exec runs bare commands through bash).
  No registry is contacted; `podman image load` applies the user-owned policy file.

## Seccomp note (deliberate deviation from the #78 profile, recorded)

The #78 deny-list returned EPERM for `clone3`. glibc's `pthread_create` only falls back from
`clone3` to plain `clone` on ENOSYS, so with the EPERM variant no native thread could be
spawned in-boundary (observed: the model backend and Codex's async runtime both failed with
`can't start new thread`). The codex proof's profile keeps the identical deny set and returns
ENOSYS for `clone3` only — the upstream OCI default-profile practice. The syscall remains
denied; namespace-creation refusal evidence in the #78 record is unaffected.
