"""Local boot identity and paired-clock continuity for live Observation."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
import sys


# Samples take both readings in succession. Allow scheduling jitter and
# normal clock slew when comparing the two elapsed times.
CLOCK_TOLERANCE_S = 0.05


def clocks_agree(wall: datetime, monotonic_s: float, then: datetime, then_s: float) -> bool:
    age = monotonic_s - then_s
    wall_age = (wall - then).total_seconds()
    return age >= 0 and wall_age >= 0 and abs(wall_age - age) <= CLOCK_TOLERANCE_S


@lru_cache(maxsize=1)
def boot_id() -> str | None:
    """Return an OS-issued identifier shared by processes of this boot."""
    try:
        if sys.platform == "darwin":
            import ctypes

            sysctl = ctypes.CDLL(None).sysctlbyname
            sysctl.argtypes = (
                ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
            )
            buffer = ctypes.create_string_buffer(64)
            size = ctypes.c_size_t(len(buffer))
            if sysctl(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0) == 0:
                return buffer.value.decode("ascii") or None
        if sys.platform == "linux":
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
        if sys.platform == "win32":
            import ctypes

            # SYSTEM_BOOT_ENVIRONMENT_INFORMATION starts with the boot GUID.
            information = ctypes.create_string_buffer(64)
            status = ctypes.windll.ntdll.NtQuerySystemInformation(
                90, ctypes.byref(information), ctypes.sizeof(information), None,
            )
            guid = information.raw[:16]
            if status == 0 and any(guid):
                return guid.hex()
    except (AttributeError, OSError, UnicodeError):
        pass
    return None
