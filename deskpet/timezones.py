"""Resolve the desktop user's IANA time zone for scheduling and display."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _desktop_timezone_names():
    yield os.environ.get("TZ", "").removeprefix(":")
    try:
        yield str(Path("/etc/localtime").resolve()).partition("/zoneinfo/")[2]
    except OSError:
        pass

    try:
        from PyQt6.QtCore import QLocale, QTimeZone
    except ImportError:
        pass
    else:
        name = QTimeZone.systemTimeZoneId()
        yield bytes(name).decode("ascii")
        # Qt normally returns an IANA ID. Also handle native Windows IDs.
        yield bytes(QTimeZone.windowsIdToDefaultIanaId(name, QLocale.system().territory())).decode("ascii")
        yield bytes(QTimeZone.windowsIdToDefaultIanaId(name)).decode("ascii")

    try:
        yield Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        pass


def resolve_timezone(name: str | None = None) -> ZoneInfo:
    """Use an explicit IANA ID, or resolve the system's configured local zone."""
    if name is not None:
        return ZoneInfo(name)
    for candidate in _desktop_timezone_names():
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ValueError, ZoneInfoNotFoundError):
                continue
    return ZoneInfo("UTC")
