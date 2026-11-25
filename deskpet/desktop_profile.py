"""Resolve the native Mochi profile without creating a desktop window."""

from pathlib import Path


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
