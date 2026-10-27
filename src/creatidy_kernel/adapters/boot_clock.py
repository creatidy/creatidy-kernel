# SPDX-License-Identifier: Apache-2.0
"""Verified Linux boot clock acquisition; boot identity is not execution ownership."""

import sys
import time
import uuid
from pathlib import Path

from creatidy_kernel.core.deadlines import BootClockReading


def read_boot_clock() -> BootClockReading | None:
    if sys.platform != "linux" or not hasattr(time, "CLOCK_BOOTTIME"):
        return None
    try:
        before = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        identity = str(uuid.UUID(before))
        if identity != before:
            return None
        boot_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        wall = int(time.time())
        after = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if after != identity or time.clock_getres(time.CLOCK_BOOTTIME) <= 0:
            return None
        return BootClockReading(wall, boot_ns, identity)
    except (OSError, ValueError, OverflowError):
        return None
