# Bubblewrap 0.13.0 Native Test Pin Provenance

**Status:** accepted native test pin; not a production/runtime pin. The owner decision dated
2026-10-15 authorizes SHA-256 `956cb41aec4589d5e0ee04d06f56e93305f161fe8f4e3eed39bb4e50ccc29394`
for repository-native verification. It does not authorize deployment, production use, or AC4
closure.

## Source

| Field | Value |
| --- | --- |
| Upstream | `https://github.com/containers/bubblewrap.git` |
| Version | `0.13.0` |
| Commit | `719a4fd474d44b26906bcf2b1b0fb6eddd8d56d0` |
| Source archive SHA-256 | `e55bdb06f051664ecd3297d449a8b679b7cd1c73adb292b73a81d9b03c5fc462` |
| License | `LGPL-2.1-or-later` |
| Upstream modifications | None |

## Build Inputs

The binary was built once through the repository-native `tools/setup_bubblewrap.py` with the
locked `sandbox-build` group. The exact output SHA is recorded above; no reproducible-binary
claim is made because historical 0.13.0 outputs differed by build path.

| Input | Version or SHA-256 |
| --- | --- |
| Repository setup source | Kernel HEAD `5e54d83e020966f5cdda7bf46b2b0d629a6310be` |
| Setup script SHA-256 | `32af48c3a164e1744dbd737836cfacb81801d7b473a1d9418e1635d0e9ccf1c4` |
| Meson | `1.12.1` |
| Ninja | `1.13.2` |
| GCC | `13.3.0-6ubuntu2~24.04.1` |
| `libcap-dev` package | `1:2.66-5ubuntu2.4`, SHA-256 `07f2462867569a2119a2ad0f1593232663f2d1612b791c230d22a8d73a15abee` |
| `libcap2` package | `1:2.66-5ubuntu2.4`, SHA-256 `23c627c77c66552a658e62852f244f4946dc543898e0ec5428d5dc6bffde2d6a` |
| Runtime `libcap.so.2` SHA-256 | `6ac6abc86ac891c6e13486470e26f1d939f47fda9e6b5d5508a7f5ec881adc84` |
| Test host `libseccomp.so.2` SHA-256 | `b4d9140a7797ccf3ac14f5d9d579b64f157111662940cb8330c632b11a8cea8e` |
| ELF `DT_NEEDED` closure | `libcap.so.2`, `libc.so.6` |

No packages were installed globally and no host Podman configuration was changed.

## Native Test Results

| Suite | Result |
| --- | --- |
| `make native-proof` | 67 passed, 0 skipped, 0 failed |
| `make codex-oci-proof` Codex OCI tests | 42 passed, 0 skipped, 0 failed |
| Installed-wheel native reception | 17 passed, 0 skipped, 0 failed |

The tests used a disposable checkout with only this exact Bwrap digest added to the existing
test allowlist. Actual observations included six denied paths, matching mount/PID/network/user
namespace identities, 12 init-FD refusals, zero effective capabilities, `NoNewPrivs=1`, seccomp
mode 2/filter 1, a single UID mapping, loopback-only networking, read-only root/cgroup mounts,
and a writable candidate workspace. The real-Codex provider was synthetic; no paid inference ran.

The `edit-legacy` and `edit-diagnostic` negative cases reported `UNMET` as expected with
`policy_relaxed=false`. The workspace-write/outer-namespace conflict remains unresolved; these
tests do not close universal AC4.

## Historical Digest Reconciliation

The formally receipted 0.13.0 hashes from ADR 0008 and issue #80 are `7c0da12cc27c4de4bac5bc1dcf50d19c62a445bdb5f30cb8c9740800b0b8b237` and
`77a5060956dd8356f737c775174aa92c06264d0f3b2ccad43fd731e6d4caa827`.

`ec5ffd1a2bc560f2868bc6e7e06a9ed9f4379a9b2cf4dab0fc0f408158ad6143` appeared in a previous
test allowlist and has a local standalone 67/0 receipt, but its full historical provenance
artifact is unavailable and no issue #80/#79 full Codex/factory receipt or ADR acceptance was
found. It was removed from the active test allowlist; the hash remains in historical records only.

## Qualification Boundary

This hash is accepted as a **native test pin only**. The separate Podman candidate used by the
Codex integration toolroot, SHA-256 `2528a39892077aef1b7a5c7ea0b19a132f5945cf4b428a73f618140d7d94a9a4`, remains diagnostic-only and was not promoted. Autonomous expiry enforcement, effects outside the
OwnedOCI subtree, and universal AC4 remain unresolved.
