"""Versioned preferences and named focus habits for the desktop pet."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime
from functools import cached_property
import os
from pathlib import Path
import re
import sqlite3
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_OBSERVATION_PRIVACY_FIELDS = {
    "observation_screenshots", "observation_browser_metadata", "observation_paused",
    "observation_excluded_apps", "observation_excluded_domains",
}


class SettingsConflict(ValueError):
    """The requested settings or habit version is no longer current."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    version: int = Field(ge=0, default=0)
    default_focus_s: int = Field(ge=1, le=86400, default=1500)
    default_break_s: int = Field(ge=1, le=86400, default=300)
    link_issues: bool = True
    auto_adjust: bool = False
    habits_enabled: bool = True
    allow_proactive_movement: bool = True
    reduce_motion: bool = False
    reminder_sound: bool = True
    phase_end_sound: bool = True
    allow_system_notifications: bool = False
    lock_screen_detail: bool = False
    observation_screenshots: bool = False
    observation_browser_metadata: bool = False
    observation_excluded_apps: tuple[str, ...] = Field(default=(), max_length=100)
    observation_excluded_domains: tuple[str, ...] = Field(default=(), max_length=100)
    observation_paused: bool = False
    observation_generation: int = Field(ge=0, default=0)
    input_mode: Literal["voice", "typing"] = "typing"

    @field_validator("observation_excluded_apps")
    @classmethod
    def _clean_apps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = tuple(dict.fromkeys(name.strip().casefold() for name in value if name.strip()))
        if any(len(name) > 120 or any(ord(char) < 32 for char in name) for name in names):
            raise ValueError("Use an application name or part of its name")
        return names

    @field_validator("observation_excluded_domains")
    @classmethod
    def _clean_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        domains = []
        for domain in value:
            if not domain.strip():
                continue
            normalized = domain.strip().strip(".").casefold().encode("idna").decode("ascii")
            if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
                                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", normalized):
                raise ValueError("Use a domain without a URL or path")
            domains.append(normalized)
        return tuple(dict.fromkeys(domains))

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


class HabitBehavior(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    focus_s: int = Field(ge=1, le=86400, default=1500)
    short_break_s: int = Field(ge=1, le=86400, default=300)
    long_break_enabled: bool = False
    long_break_s: int | None = Field(ge=1, le=86400, default=None)
    long_break_basis: Literal["focus_seconds", "completed_phases"] = "completed_phases"
    threshold: int = Field(ge=1, default=4)
    auto_start_break: bool = False
    auto_start_focus: bool = False

    @model_validator(mode="after")
    def _require_long_break_duration(self):
        if self.long_break_enabled and self.long_break_s is None:
            raise ValueError("A long break needs a duration")
        return self


class Habit(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", str_strip_whitespace=True)

    id: str
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=100)
    status: Literal["enabled", "disabled", "archived"] = "enabled"
    behavior: HabitBehavior
    created_at: datetime
    updated_at: datetime


class SettingsStore:
    """Use the business database for preferences and habit transition checks."""

    def __init__(self, path: str | Path, *, user_scope: str = "default",
                 initial_input_mode: str = "typing"):
        if not isinstance(user_scope, str) or not user_scope.strip():
            raise ValueError("A user scope is required")
        self.path = Path(path).expanduser().resolve()
        self.user_scope = user_scope
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with self._write() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS pet_settings_schema (
                    id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL
                )
            """)
            db.execute("INSERT OR IGNORE INTO pet_settings_schema VALUES (1, 1)")
            if db.execute("SELECT version FROM pet_settings_schema").fetchone()[0] != 1:
                raise ValueError("Unsupported settings database schema")
            db.execute("""
                CREATE TABLE IF NOT EXISTS pet_settings (
                    user_scope TEXT PRIMARY KEY, settings TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS pet_habits (
                    user_scope TEXT NOT NULL, habit_id TEXT NOT NULL, habit TEXT NOT NULL,
                    PRIMARY KEY (user_scope, habit_id)
                )
            """)
            initial = Settings(input_mode=initial_input_mode if initial_input_mode in {"voice", "typing"} else "typing")
            db.execute("INSERT OR IGNORE INTO pet_settings VALUES (?, ?)",
                       (self.user_scope, initial.model_dump_json()))

    @cached_property
    def routes(self):
        from deskpet.observation_v2.routes import ObservationCloudSettings

        return ObservationCloudSettings(self.path, user_scope=self.user_scope)

    @contextmanager
    def _write(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def read(self, *, db: sqlite3.Connection | None = None) -> Settings:
        return self.read_at(self.path, user_scope=self.user_scope, db=db)

    @staticmethod
    def read_at(
        path: str | Path, *, user_scope: str = "default", db: sqlite3.Connection | None = None, timeout: float = 5,
    ) -> Settings:
        """Read existing settings without creating a database from an Observation worker."""
        if db is None:
            target = Path(path).expanduser().resolve()
            try:
                connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=timeout)
            except sqlite3.OperationalError:
                if target.exists():
                    raise
                return Settings()
            with closing(connection):
                return SettingsStore.read_at(target, user_scope=user_scope, db=connection)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pet_settings'").fetchone():
            return Settings()
        row = db.execute("SELECT settings FROM pet_settings WHERE user_scope = ?", (user_scope,)).fetchone()
        return Settings.model_validate_json(row[0]) if row else Settings()

    def update(
        self, *, expected_version: int,
        before_commit: Callable[[sqlite3.Connection, Settings, Settings], None] | None = None,
        db: sqlite3.Connection | None = None,
        **changes,
    ) -> Settings:
        if {"version", "observation_generation"}.intersection(changes):
            raise ValueError("The service assigns settings versions")
        if db is not None:
            if not db.in_transaction:
                raise ValueError("a write transaction is required to update settings")
            if _OBSERVATION_PRIVACY_FIELDS.intersection(changes):
                raise ValueError("Observation privacy updates require the settings privacy gate")
            return self._update(db, expected_version, before_commit, changes)
        guard = nullcontext()
        if _OBSERVATION_PRIVACY_FIELDS.intersection(changes):
            from deskpet.observation_v2.routes import observation_privacy_gate

            guard = observation_privacy_gate(self.path)
        with guard, self._write() as db:
            return self._update(db, expected_version, before_commit, changes)

    def _update(self, db, expected_version, before_commit, changes):
        current = self.read(db=db)
        if current.version != expected_version:
            raise SettingsConflict("Settings have changed")
        updated = Settings.model_validate({**current.model_dump(), **changes, "version": current.version + 1})
        if any(getattr(updated, name) != getattr(current, name) for name in _OBSERVATION_PRIVACY_FIELDS):
            updated = updated.model_copy(update={"observation_generation": current.observation_generation + 1})
        db.execute("UPDATE pet_settings SET settings = ? WHERE user_scope = ?",
                   (updated.model_dump_json(), self.user_scope))
        if before_commit is not None:
            before_commit(db, current, updated)
        return updated

    def list_habits(self, *, db: sqlite3.Connection | None = None) -> tuple[Habit, ...]:
        if db is None:
            with closing(sqlite3.connect(self.path)) as connection:
                return self.list_habits(db=connection)
        rows = db.execute("SELECT habit FROM pet_habits WHERE user_scope = ?", (self.user_scope,))
        habits = [Habit.model_validate_json(row[0]) for row in rows]
        return tuple(sorted(habits, key=lambda habit: (habit.title.casefold(), habit.id)))

    def get_habit(self, habit_id: str, *, db: sqlite3.Connection | None = None) -> Habit | None:
        if db is None:
            with closing(sqlite3.connect(self.path)) as connection:
                return self.get_habit(habit_id, db=connection)
        row = db.execute("SELECT habit FROM pet_habits WHERE user_scope = ? AND habit_id = ?",
                         (self.user_scope, habit_id)).fetchone()
        return Habit.model_validate_json(row[0]) if row else None

    def save_habit(self, *, title: str, behavior: HabitBehavior, expected_version: int,
                   habit_id: str | None = None, status: str = "enabled",
                   db: sqlite3.Connection | None = None, now: datetime | None = None) -> Habit:
        if db is None:
            with self._write() as connection:
                return self.save_habit(title=title, behavior=behavior, expected_version=expected_version,
                                       habit_id=habit_id, status=status, db=connection, now=now)
        if not db.in_transaction:
            raise ValueError("a write transaction is required to save a habit")
        current = self.get_habit(habit_id, db=db) if habit_id else None
        if (habit_id and current is None) or (current.version if current else 0) != expected_version:
            raise SettingsConflict("Habit has changed")
        if current and current.status == "archived":
            raise SettingsConflict("Habit is archived")
        now = now or datetime.now(UTC)
        habit = Habit(
            id=current.id if current else uuid4().hex,
            version=expected_version + 1,
            title=title,
            behavior=behavior,
            status=status,
            created_at=current.created_at if current else now,
            updated_at=now,
        )
        db.execute("""
            INSERT INTO pet_habits VALUES (?, ?, ?)
            ON CONFLICT (user_scope, habit_id) DO UPDATE SET habit = excluded.habit
        """, (self.user_scope, habit.id, habit.model_dump_json()))
        return habit
