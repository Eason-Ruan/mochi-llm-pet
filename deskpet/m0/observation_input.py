"""Live Observation reads for the desktop. A failed read closes the soft-reminder gate."""
from __future__ import annotations

from dataclasses import replace
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

    def _current_survived_read(self, observation, current_at, started, policy):
        if self._policy(timeout=_CONTEXT_BUSY_TIMEOUT_S) != policy:
            return False
        if observation.valid_until is not None:
            return monotonic() - started < (observation.valid_until - current_at).total_seconds()
        reading = sample_realtime_clock(lambda: datetime.now(UTC), monotonic)
        if reading is None:
            return False
        at, sampled_monotonic = reading
        head = self.reader.read_realtime_head(
            at, monotonic_s=sampled_monotonic, policy_generation=policy[1],
        )
        current = head.current
        return not head.scene_invalidated and (
            current is None or (current.context_revision, current.visual_capture_id)
            == (observation.context_revision, observation.visual_capture_id)
        )

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
            observation = current[0]
            if observation is not None and (
                observation.observed_at > as_of
                or (observation.valid_until is not None and as_of >= observation.valid_until)
                or self._policy() != (allowed, generation)
            ):
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
            if context.current is not None and not self._current_survived_read(
                context.current, current_at, started, (allowed, generation),
            ):
                context = self.reader.read_realtime_context(
                    as_of, include_current=False, busy_timeout_s=timeout, policy_generation=generation,
                    history_start=history_start, history_end=history_end,
                )
            if self._policy(timeout=timeout) != (allowed, generation):
                return replace(ObservationContext.empty(as_of), current_status="context_changed")
            if not allowed:
                context = replace(context, activity_timeline=None)
        except (sqlite3.Error, OSError, ValueError) as exc:
            self._failed(exc)
            return ObservationContext.empty(as_of)
        self._unavailable = False
        return context
