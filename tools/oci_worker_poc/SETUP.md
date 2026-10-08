# Whole-Worker Isolation PoC Toolroot Provisioning

Temporary, user-owned development setup for the [#78](https://forgejo.creatidy.com/Creatidy/creatidy-kernel/issues/78)
rootless OCI proof. Nothing here is a product dependency, a permanent service, or an
authorization for privileged installation: every component is pinned, hash-recorded and
lives under a `0700` user-owned directory. The native receipt requires
`CREATIDY_TEST_OCI_TOOLROOT=/tmp/kilo/kernel78-tooling`.

The toolroot at `/tmp/kilo/kernel78-tooling` was provisioned 2026-10-08 and is fully
described by its `provenance.json` (exact upstream pins, source archives, hashes, build
flags and verification methods). The steps below reproduce it; they were executed as the
normal unprivileged user with no host-global change.

## Components

| Component | Version | Provenance |
| --- | --- | --- |
| podman | 6.1.3 | Built from unmodified pinned upstream commit `85b994955e0b4e30fbce9c8351cab85676140ede` (`make bin/podman`, Go 1.26.8, CGO with libseccomp 2.5.5-1ubuntu3.1); Apache-2.0 |
| crun | 1.30.1 | Official release asset, SHA-256 matched and GPG signature verified against the pinned source keyring; GPL-2.0-or-later (libcrun LGPL-2.1) |
| conmon | 2.2.1 | Official release asset, SHA-256 matched; Apache-2.0 |
| netavark | 1.4.0 | Ubuntu noble archive `netavark` package extracted with `dpkg -x` (podman 6.x requires the helper binary to be present even for `--network=none`); Apache-2.0 |
| busybox | 1:1.36.1-6ubuntu3.1 | Ubuntu noble archive `busybox-static` package extracted with `dpkg -x`; image layer content; GPL-2.0 (BusyBox upstream; the package copyright references GPL-2) |
| policy.json | user-owned | `$XDG_CONFIG_HOME/containers/policy.json` (`insecureAcceptAnything`); podman 6.x reads the XDG path first, so no root-owned `/etc/containers/policy.json` is needed |

Upstream maintenance at provisioning time: podman 6.1.3 (2026-09-29) and crun 1.30.1
(2026-09-25, includes the CVE-2026-84042 fixes) are the current maintained releases; conmon
2.2.1 (2026-02-12) is current. Compare the decision record's advisory table before reuse.

## Layout and environment

```
/tmp/kilo/kernel78-tooling/
  bin/{podman,crun,conmon,netavark,busybox}
  containers.conf         # pinned crun/conmon paths, vfs-friendly engine settings, events_logger=none
  storage.conf            # driver vfs; graphroot under <root>/data; runroot under <root>/runtime
  config/containers/policy.json
  provenance.json         # exact pins, hashes, build parameters
  {home,tmp,cache,data,runtime,downloads,debs}/
```

The PoC resolves the toolroot via `oci_worker_poc.toolchain.resolve_toolchain()` and invokes
podman with a closed environment: pinned `PATH`, toolroot `HOME`/`TMPDIR`/cache/config/data,
`XDG_RUNTIME_DIR=/run/user/<uid>` (the real systemd user session — podman needs its bus for
the systemd cgroup manager), `USER=nobody`, and `CONTAINERS_CONF`/`CONTAINERS_STORAGE_CONF`
pointing at the pinned configs.

## Why `USER=nobody` (single-UID rootless mapping)

This host advertises a `/etc/subuid` range for the user but has no setuid `newuidmap`.
Writing a multi-line UID/GID mapping is kernel-denied without CAP_SETUID, and installing the
`uidmap` package is a privileged change outside this task's authorization. With `USER`
pointed at a user without subuid entries, podman takes its documented **rootless single
mapping** path (`0 <uid> 1`): the container's root is the invoking user's own host identity.
All namespace/mount/network/capability/seccomp enforcement is unchanged; the multi-UID
ownership layer (container root mapped into subuids) is absent and recorded as a limitation.
Consequences proven by the tests: foreign-UID image layers cannot be unpacked (the PoC image
is all-zero-ownership) and `--userns=keep-id` ID-mapped copies are unavailable.

## Why the harness refuses the cgroupfs fallback

Podman silently falls back to `--cgroup-manager=cgroupfs` when the systemd user session bus
is unreachable, leaving `--memory`/`--pids-limit` unenforced while argv still claims them.
`XDG_RUNTIME_DIR` therefore points at the real session directory, and
`AttemptRunner.wait` raises on that fallback warning; the flood-fork test additionally reads
`/sys/fs/cgroup/pids.max` inside the container to observe the limit directly.

## Reproduction sketch

The original provisioning scripts remain under the toolroot (`provision.sh`,
`build-podman.sh`, `build-conmon.sh`, `probe.sh`) with their download cache in `downloads/`.
For the two `dpkg -x` extractions added during the PoC:

```sh
cd /tmp/kilo/kernel78-tooling/debs
apt-get download netavark busybox-static
dpkg -x netavark_*.deb ../netavark-x && cp ../netavark-x/usr/lib/podman/netavark ../bin/
dpkg -x busybox-static_*.deb ../root && cp ../root/usr/bin/busybox ../bin/
```

`make oci-worker-proof` runs the full native suite against this toolroot. A toolroot-less
environment must leave the receipt UNPROVED; it must never substitute synthetic skips or a
weaker runner.
