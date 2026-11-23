"""Live Observation reads for the desktop. A failed read closes the soft-reminder gate."""
from __future__ import annotations

from datetime import UTC, datetime
import logging
from pathlib import Path
import sqlite3
from time import monotonic

from deskpet.observation_v2.context import ObservationContext, ObservationContextReader
from deskpet.observation_v2.realtime import sample_realtime_clock
from deskpet.settings import SettingsStore


log = logging.getLogger(__name__)
_CONTEXT_BUSY_TIMEOUT_S = 0.1


class DesktopObservationInput:
    def __init__(self, path, *, settings_path=None):
        self.reader = ObservationContextReader(path)
        self.settings_path = Path(settings_path).resolve() if settings_path is not None else None
        self._unavailable = False

    def _failed(self, exc):
        if not self._unavailable:
            log.warning("Observation database is unavailable (%s)", type(exc).__name__)
        self._unavailable = True

    def _policy(self, *, timeout=0):
        if self.settings_path is None:
            return True, None
        settings = SettingsStore.read_at(self.settings_path, timeout=timeout)
        return not settings.observation_paused, settings.observation_generation

    def read_current(self, as_of):
        try:
            allowed, generation = self._policy()
            if not allowed:
                return None, "context_changed"
            reading = sample_realtime_clock(lambda: datetime.now(UTC), monotonic)
            if reading is None:
                return None, "no_data"
            sampled_at, sampled_monotonic = reading
            current = self.reader.read_realtime_current(sampled_at, monotonic_s=sampled_monotonic,
                                                        policy_generation=generation)
            if current[0] is not None and not current[0].observed_at <= as_of < current[0].valid_until:
                return None, "context_changed"
            if current[0] is not None and self._policy() != (allowed, generation):
                return None, "context_changed"
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._failed(exc)
            return None, "no_data"
        self._unavailable = False
        return current

    def read_observation_context(self, as_of, *, history_start=None, history_end=None):
        # A model admission reads policy and history beside concurrent business
        # writes. Let a short commit finish without delaying the immediate gate.
        timeout = _CONTEXT_BUSY_TIMEOUT_S
        try:
            allowed, generation = self._policy(timeout=timeout)
            reading = sample_realtime_clock(lambda: datetime.now(UTC), monotonic)
            current_at, started = reading if reading is not None else (as_of, monotonic())
            context = self.reader.read_realtime_context(
                as_of, current_at=current_at, monotonic_s=started, policy_generation=generation,
                include_current=allowed and reading is not None,
                busy_timeout_s=timeout, history_start=history_start, history_end=history_end,
            )
            if context.current is not None and (
                self._policy(timeout=timeout) != (allowed, generation)
                or monotonic() - started >= (context.current.valid_until - current_at).total_seconds()
            ):
                context = self.reader.read_realtime_context(
                    as_of, include_current=False, busy_timeout_s=timeout,
                    history_start=history_start, history_end=history_end,
                )
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._failed(exc)
            return ObservationContext.empty(as_of)
        self._unavailable = False
        return context
