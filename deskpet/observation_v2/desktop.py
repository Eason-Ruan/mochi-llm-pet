"""Manage the Observation process for the production pet."""
from __future__ import annotations

import logging
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

from deskpet.settings import OBSERVATION_POLICY_READ_TIMEOUT_S, SettingsStore


log = logging.getLogger(__name__)


class DesktopObserver:
    """The UI calls refresh periodically; stdin EOF also stops an orphaned collector."""

    def __init__(self, data_dir, *, settings_path=None):
        directory = Path(data_dir).expanduser().resolve()
        self.database = directory / "observation.sqlite3"
        self.settings_path = Path(settings_path or directory / "business.sqlite3").resolve()
        self.settings = SettingsStore(self.settings_path)
        self._process = None
        self._policy = None
        self._stop_requested_at = None
        self._retry_at = 0.0
        self._enabled = False

    def start(self):
        self._enabled = True
        self.refresh()

    def _arguments(self, prefs):
        args = [
            sys.executable, "-m", "deskpet.observation_v2.desktop", "live",
            "--database", str(self.database), "--cloud-settings", str(self.settings_path),
            "--capture-screen-from-settings", "--observation-generation", str(prefs.observation_generation),
            "--until-stopped",
        ]
        if prefs.observation_browser_metadata:
            args.append("--browser-metadata")
        for app in prefs.observation_excluded_apps:
            args.extend(("--exclude-app", app))
        for domain in prefs.observation_excluded_domains:
            args.extend(("--exclude-domain", domain))
        return tuple(args)

    def _request_stop(self, now):
        if self._stop_requested_at is None:
            self._stop_requested_at = now
            self._process.stdin.close()
        elif now - self._stop_requested_at > 2:
            self._process.kill()

    def refresh(self):
        now = time.monotonic()
        if not self._enabled:
            return
        if self._process is not None:
            code = self._process.poll()
            if code is not None:
                self._process.stdin.close()
                if code and self._stop_requested_at is None:
                    log.warning("Observation process stopped (exit %s); retrying", code)
                    self._retry_at = now + 5
                self._process = None
                self._stop_requested_at = None
        try:
            with closing(sqlite3.connect(
                self.settings_path.as_uri() + "?mode=ro", uri=True, timeout=OBSERVATION_POLICY_READ_TIMEOUT_S,
            )) as db:
                prefs = self.settings.read(db=db)
            policy = None if prefs.observation_paused else self._arguments(prefs)
        except (ValueError, OSError, sqlite3.Error):
            policy = None
        if self._process is not None:
            if policy != self._policy or self._stop_requested_at is not None:
                self._request_stop(now)
            return
        if policy is None or now < self._retry_at:
            return
        try:
            self._process = subprocess.Popen(
                policy, cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            log.warning("Could not start Observation (%s)", type(error).__name__)
            self._retry_at = now + 5
        else:
            self._policy = policy

    def stop(self):
        self._enabled = False
        process = self._process
        if process is not None:
            process.stdin.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self._process = None
        self._stop_requested_at = None


def main():
    from .__main__ import main as run

    halted = threading.Event()

    def watch_parent():
        try:
            sys.stdin.buffer.read(1)
        finally:
            halted.set()

    threading.Thread(target=watch_parent, name="observation-parent", daemon=True).start()
    run(stop_event=halted)


if __name__ == "__main__":
    main()
