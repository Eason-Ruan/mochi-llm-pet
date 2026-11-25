"""Resolve the native Mochi profile without creating a desktop window."""

from pathlib import Path
from platform import mac_ver
from sys import platform

from .settings import Settings, SettingsStore


DESKTOP_NAME = "Mochi"


def desktop_data_dir(data_dir: Path | str | None = None) -> Path:
    if data_dir is None:
        from PyQt6.QtCore import QCoreApplication, QStandardPaths

        QCoreApplication.setOrganizationName(DESKTOP_NAME)
        QCoreApplication.setApplicationName(DESKTOP_NAME)
        data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        if not data_dir:
            raise RuntimeError("The platform did not provide a Mochi data directory")
    return Path(data_dir).expanduser().resolve()


def desktop_settings_store(data_dir: Path) -> SettingsStore:
    """Initialize a desktop profile with the defaults for its operating system."""
    initial = None
    if platform == "darwin":
        initial = Settings(
            default_focus_s=900,
            auto_adjust=True,
            allow_system_notifications=True,
            lock_screen_detail=True,
            observation_screenshots=True,
            observation_screen_scope="all_displays_unfiltered" if _all_displays_available() else "foreground_window",
        )
    return SettingsStore(data_dir / "business.sqlite3", initial_settings=initial)


def _all_displays_available() -> bool:
    try:
        major, minor = (int(part) for part in mac_ver()[0].split(".")[:2])
        if (major, minor) < (14, 2):
            return False
        from ScreenCaptureKit import SCScreenshotManager

        return hasattr(SCScreenshotManager, "captureImageWithFilter_configuration_completionHandler_")
    except (ImportError, OSError, ValueError):
        return False
