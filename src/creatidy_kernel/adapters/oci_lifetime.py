# SPDX-License-Identifier: Apache-2.0
"""Kernel-authenticated PID-1 lifetime, never inspect-PID polling or cold numeric reacquisition.

SO_PASSPIDFD/SCM_PIDFD values are maintained Linux ABI facts from v6.18
7d0a66e4bb9081d75c82ec4957c50034cb0ea449 include/uapi/asm-generic/socket.h
and include/linux/socket.h. net/core/scm.c retains the actual sender's struct pid
for the message; no implementation is copied. Unsupported kernels refuse.
"""

import array
import os
import select
import socket
import struct
from pathlib import Path

from creatidy_kernel.adapters.worker_files import read_bounded_regular
from creatidy_kernel.core.execution import ExecutionConflict

SO_PASSPIDFD = 76
SCM_PIDFD = 4


class LifetimeChannel:
    """A single controller-owned socketpair, received pidfd and retained namespace handle."""

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        self.controller, self.worker = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.pidfd: int | None = None
        self.namespace_fd: int | None = None
        self.identity: dict[str, object] | None = None
        try:
            self.controller.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            self.controller.setsockopt(socket.SOL_SOCKET, SO_PASSPIDFD, 1)
            if self.worker.fileno() < 3:
                raise ExecutionConflict("dedicated lifetime descriptor unavailable")
        except BaseException:
            self.close()
            raise

    def receive(self, *, timeout: float) -> dict[str, object]:
        """Acquire actual sender's pidfd while trusted PID-1 waits for the execution gate."""
        if self.pidfd is not None or self.namespace_fd is not None:
            raise ExecutionConflict("OCI namespace ownership already captured or uncertain")
        self.controller.settimeout(timeout)
        data, controls, flags, _ = self.controller.recvmsg(
            128,
            socket.CMSG_SPACE(12) + socket.CMSG_SPACE(4),
            socket.MSG_CMSG_CLOEXEC,
        )
        descriptors: list[int] = []
        credentials: tuple[int, int, int] | None = None
        kinds: list[int] = []
        try:
            for level, kind, payload in controls:
                if level == socket.SOL_SOCKET and kind in {SCM_PIDFD, socket.SCM_RIGHTS}:
                    values = array.array("i")
                    values.frombytes(payload[: len(payload) - len(payload) % values.itemsize])
                    descriptors.extend(fd for fd in values if fd >= 0)
                if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(payload) == 12:
                    credentials = struct.unpack("iII", payload)
                kinds.append(kind if level == socket.SOL_SOCKET else -1)
            if (
                data != b"READY:" + self.nonce.encode("ascii")
                or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC)
                or sorted(kinds) != sorted((SCM_PIDFD, socket.SCM_CREDENTIALS))
                or len(descriptors) != 1
                or descriptors[0] < 0
                or credentials is None
                or credentials[1:] != (os.getuid(), os.getgid())
            ):
                raise ExecutionConflict("unauthenticated OCI lifetime message")
            pidfd = descriptors[0]
            os.set_inheritable(pidfd, False)
            if self._exited(pidfd) is not False:
                raise ExecutionConflict("OCI init exited before ownership capture")
            info = dict(
                line.split(":", 1)
                for line in read_bounded_regular(
                    Path(f"/proc/self/fdinfo/{pidfd}"),
                    4096,
                )
                .decode("ascii")
                .splitlines()
            )
            pid = int(info["Pid"])
            if pid <= 0 or pid != credentials[0]:
                raise ExecutionConflict("OCI process/proc coordinates differ")
            status = dict(
                line.split(":", 1)
                for line in read_bounded_regular(
                    Path(f"/proc/{pid}/status"),
                    65536,
                )
                .decode("ascii")
                .splitlines()
            )
            own = dict(
                line.split(":", 1)
                for line in read_bounded_regular(
                    Path("/proc/self/status"),
                    65536,
                )
                .decode("ascii")
                .splitlines()
            )
            nspid = tuple(map(int, status["NSpid"].split()))
            if (
                int(status["Pid"]) != pid
                or int(status["Tgid"]) != pid
                or int(own["Pid"]) != os.getpid()
                or nspid[0] != pid
                or nspid[-1] != 1
                or len(nspid) <= len(own["NSpid"].split())
                or set(map(int, status["Uid"].split())) != {os.getuid()}
            ):
                raise ExecutionConflict("dedicated owned OCI namespace init unproved")
            namespace = os.open(f"/proc/{pid}/ns/pid", os.O_RDONLY | os.O_CLOEXEC)
            self.namespace_fd = namespace
            native, parent = os.fstat(namespace), os.stat("/proc/self/ns/pid")
            if (native.st_dev, native.st_ino) == (parent.st_dev, parent.st_ino):
                raise ExecutionConflict("OCI init joined the controller namespace")
            stat_text = read_bounded_regular(Path(f"/proc/{pid}/stat"), 4096).decode("ascii")
            if int(stat_text.split("(", 1)[0].strip()) != pid:
                raise ExecutionConflict("OCI init stat identity differs")
            start_ticks = int(stat_text.rpartition(")")[2].split()[19])
            if self._exited(pidfd) is not False:
                raise ExecutionConflict("OCI init exited during ownership capture")
            self.pidfd = pidfd
            descriptors.clear()
            self.identity = {
                "pid": pid,
                "start_ticks": start_ticks,
                "namespace": [native.st_dev, native.st_ino],
                "controller_namespace": [parent.st_dev, parent.st_ino],
                "uid": os.getuid(),
            }
            return dict(self.identity)
        except (OSError, ValueError, KeyError, IndexError) as error:
            raise ExecutionConflict("owned OCI namespace identity unavailable") from error
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    @staticmethod
    def _exited(pidfd: int) -> bool | None:
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)
        events = poll.poll(0)
        if not events:
            return False
        flags = events[0][1]
        return True if flags & select.POLLIN and not flags & (select.POLLERR | select.POLLNVAL) else None

    def exited(self) -> bool:
        if self.pidfd is None or self.identity is None:
            return False
        try:
            return self._exited(self.pidfd) is True
        except OSError:
            return False

    def release(self) -> None:
        if self.pidfd is None or self.identity is None or self.exited():
            raise ExecutionConflict("owned OCI init is not live at execution gate")
        self.controller.sendall(b"GO")

    def close_liveness(self) -> None:
        self.controller.close()
        self.worker.close()

    def close(self) -> None:
        self.close_liveness()
        for name in ("pidfd", "namespace_fd"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def __del__(self) -> None:
        if hasattr(self, "controller"):
            try:
                self.close()
            except OSError:
                pass
