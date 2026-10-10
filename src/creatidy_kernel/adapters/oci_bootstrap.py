# SPDX-License-Identifier: Apache-2.0
"""Trusted OCI PID-1 gate: no harness before GO; controller EOF/deadline exits the namespace.

Run this immutable, read-only source with the selected in-boundary Python using
``-I -S``. It has no Kernel imports, model protocol or external service. Candidate
code never inherits the private lifetime socket and cannot reopen a nondumpable
PID-1's descriptors under the selected dropped-capability boundary. Full boundary
conformance is separate from this gate's lifecycle enforcement.
"""

import ctypes
import math
import os
import select
import signal
import socket
import subprocess
import sys
import time


def main(arguments: list[str]) -> int:
    if len(arguments) < 5 or arguments[3] != "--" or os.getpid() != 1:
        raise ValueError("explicit PID-1 lifetime gate arguments required")
    descriptor, nonce, deadline = int(arguments[0]), arguments[1], float(arguments[2])
    command = arguments[4:]
    if (
        descriptor < 3
        or len(nonce) != 64
        or any(char not in "0123456789abcdef" for char in nonce)
        or not math.isfinite(deadline)
        or not command[0].startswith("/")
    ):
        raise ValueError("invalid PID-1 lifetime gate configuration")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    # Maintained prctl ABI: PR_SET_DUMPABLE, not a hand-numbered syscall.
    if libc.prctl(4, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PID-1 descriptor protection unavailable")

    def stopped(_signal: int, _frame: object) -> None:
        os._exit(143)  # PID-1 death invokes kernel namespace-descendant teardown.

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    os.set_inheritable(descriptor, False)
    with socket.socket(fileno=descriptor) as lifetime:
        if lifetime.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET:
            raise ValueError("private seqpacket lifetime channel required")
        remaining = deadline - time.time()
        if remaining <= 0:
            return 124
        lifetime.settimeout(remaining)
        lifetime.sendall(b"READY:" + nonce.encode("ascii"))
        if lifetime.recv(16) != b"GO" or time.time() >= deadline:
            return 124
        process = subprocess.Popen(  # noqa: S603 - controller-fixed explicit argv, no shell or inherited lifetime fd.
            command,
            close_fds=True,
            env=dict(os.environ),
        )
        while process.poll() is None:
            remaining = deadline - time.time()
            if remaining <= 0:
                return 124
            if select.select([lifetime], [], [], min(remaining, 0.1))[0]:
                lifetime.recv(16)  # Any further message or EOF retires this namespace.
                return 143
        return process.returncode


if __name__ == "__main__":
    # Do not run arbitrary Python finalizers while namespace descendants may live.
    os._exit(main(sys.argv[1:]))
