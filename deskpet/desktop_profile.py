"""Resolve the native Mochi profile without creating a desktop window."""

from pathlib import Path
from platform import mac_ver
from sys import platform

from .i18n import DEFAULT_LANGUAGE, LANGUAGES, Language
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


def _window_settings(data_dir: Path):
    from PyQt6.QtCore import QSettings

    return QSettings(str(data_dir / "window.ini"), QSettings.Format.IniFormat)


def read_desktop_language(data_dir: Path) -> Language:
    selected = _window_settings(data_dir).value("uiLanguage", DEFAULT_LANGUAGE, type=str)
    return selected if selected in LANGUAGES else DEFAULT_LANGUAGE


def save_desktop_language(data_dir: Path, selected: Language) -> None:
    from PyQt6.QtCore import QSettings

    if selected not in LANGUAGES:
        raise ValueError("Unsupported desktop language")
    settings = _window_settings(data_dir)
    settings.setValue("uiLanguage", selected)
    settings.sync()
    if settings.status() != QSettings.Status.NoError:
        raise OSError("Could not save desktop language")


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
