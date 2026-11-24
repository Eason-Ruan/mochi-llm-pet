"""Local activity accounting and optional desktop sampling for Observation v2."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
from threading import Lock
import time

from .documents import (
    browser_document_source, document_source_allowed, document_title, is_mochi_app,
    native_document_identity, native_document_source, normalize_document_sources, page_domain as _page_domain,
)


POLL_INTERVAL_S = {"darwin": 1.5, "win32": 0.5, "linux": 1.0}
CAPTURE_INTERVAL_S = 30.0
APP_COOLDOWN_S = 3.0
BROWSER_COOLDOWN_S = 1.2
_CAPTURE_STATUS = ("skipped", "failed", "captured")
_DOCUMENT_COLUMNS = ("document_name", "document_source", "document_status", "document_change", "document_change_at")
_DATA_TIMESTAMPS = ("started_at", "ended_at", "system_sampled_at", "document_change_at")
_DEFAULT_EXCLUDED_APPS = ("1password", "bitwarden", "keychain")
_SENSITIVE_TITLE_WORDS = ("bank", "login", "password", "密码", "银行", "支付")
_BROWSERS = frozenset({
    "arc", "brave", "brave browser", "chrome", "chromium", "chromium browser", "edge",
    "firefox", "google chrome", "microsoft edge", "msedge", "opera", "opera gx", "safari",
    "vivaldi", "zen browser",
})
_PRIVATE_TITLE = re.compile(
    r"\b(?:incognito|inprivate|private (?:browsing|window))\b|无痕(?:浏览|模式|式?窗口)|隐私(?:浏览|窗口)|"
    r"無痕(?:瀏覽|模式|式?視窗)|隱私(?:瀏覽|視窗)|私密(?:浏览|瀏覽)|プライベートブラウズ", re.IGNORECASE,
)
_MAC_CHROMIUM_APPS = {
    "brave browser": "com.brave.Browser",
    "chromium": "org.chromium.Chromium",
    "google chrome": "com.google.Chrome",
    "microsoft edge": "com.microsoft.edgemac",
    "vivaldi": "com.vivaldi.Vivaldi",
}

_MAC_SESSION_LOOKUP = Lock()

@dataclass(frozen=True, slots=True)
class ActivitySample:
    observed_at: datetime
    monotonic_s: float
    app_name: str | None = None
    window_id: int | str | None = None
    window_title: str | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    idle_s: float | None = None
    screen_locked: bool | None = None
    session_id: str | None = None
    system_sampled_at: datetime | None = None
    private_window: bool | None = None
    app_id: str | None = field(default=None, repr=False)
    active_tab_title: str | None = field(default=None, repr=False)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Activity timestamps must include a timezone")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class Activity:
    activity_id: int
    started_at: datetime
    ended_at: datetime
    duration_s: float
    app_name: str | None = None
    window_title_redacted: str | None = None
    domain: str | None = None
    idle_s: float | None = None
    screen_locked: bool | None = None
    system_sampled_at: datetime | None = None
    capture_status: str = "skipped"
    document_name: str | None = None
    document_source: str | None = None
    document_status: str | None = None
    document_change: str | None = None
    document_change_at: datetime | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        for name in _DATA_TIMESTAMPS:
            if data[name] is not None:
                data[name] = _iso(data[name])
        for name in _DOCUMENT_COLUMNS:
            if data[name] is None:
                del data[name]
        return data


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    activity: Activity
    reason: str


SampleWrite = Callable[[ActivitySample, Activity | None, bool, bool], None]


class ActivityStore:
    def __init__(self, path: str | Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS activities (
                        activity_id INTEGER PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        ended_at TEXT NOT NULL CHECK (ended_at >= started_at),
                        duration_s REAL NOT NULL CHECK (duration_s >= 0),
                        app_name TEXT,
                        window_title_redacted TEXT,
                        domain TEXT,
                        idle_s REAL CHECK (idle_s >= 0),
                        screen_locked INTEGER CHECK (screen_locked IN (0, 1)),
                        system_sampled_at TEXT,
                        capture_status TEXT NOT NULL CHECK (capture_status IN ('skipped', 'failed', 'captured')),
                        document_name TEXT,
                        document_source TEXT,
                        document_status TEXT,
                        document_change TEXT,
                        document_change_at TEXT
                    )
                """)
                self.connection.execute("CREATE INDEX IF NOT EXISTS activities_ended_at ON activities (ended_at)")
                columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(activities)")}
                for name in _DOCUMENT_COLUMNS:
                    if name not in columns:
                        self.connection.execute(f"ALTER TABLE activities ADD COLUMN {name} TEXT")
        except sqlite3.Error:
            self.connection.close()
            raise

    def __enter__(self) -> ActivityStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _insert(self, activity: Activity) -> Activity:
        result = self.connection.execute("""
            INSERT INTO activities (started_at, ended_at, duration_s, app_name,
                                    window_title_redacted, domain, idle_s, screen_locked,
                                    system_sampled_at, capture_status,
                                    document_name, document_source, document_status, document_change, document_change_at)
            VALUES (:started_at, :ended_at, :duration_s, :app_name,
                    :window_title_redacted, :domain, :idle_s, :screen_locked,
                    :system_sampled_at, :capture_status, :document_name, :document_source, :document_status,
                    :document_change, :document_change_at)
        """, dict.fromkeys(_DOCUMENT_COLUMNS) | activity.to_dict())
        return replace(activity, activity_id=result.lastrowid)

    def _extend(self, activity: Activity) -> None:
        # Capture completion may have happened since the Activity was read.
        self.connection.execute("""
            UPDATE activities SET ended_at = :ended_at, duration_s = :duration_s,
                                  idle_s = :idle_s, screen_locked = :screen_locked,
                                  system_sampled_at = :system_sampled_at,
                                  document_change = :document_change, document_change_at = :document_change_at
            WHERE activity_id = :activity_id
        """, dict.fromkeys(_DOCUMENT_COLUMNS) | activity.to_dict())

    @staticmethod
    def _decode(row: sqlite3.Row) -> Activity:
        data = dict(row)
        for name in _DATA_TIMESTAMPS:
            if data[name] is not None:
                data[name] = datetime.fromisoformat(data[name])
        if data["screen_locked"] is not None:
            data["screen_locked"] = bool(data["screen_locked"])
        return Activity(**data)

    def get_activity(self, activity_id: int) -> Activity | None:
        row = self.connection.execute(
            "SELECT * FROM activities WHERE activity_id = ?", (activity_id,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def list_activities(self, *, since: datetime | None = None) -> list[Activity]:
        if since is None:
            rows = self.connection.execute("SELECT * FROM activities ORDER BY started_at, ended_at, activity_id")
        else:
            rows = self.connection.execute("""
                SELECT * FROM activities WHERE ended_at >= ? ORDER BY started_at, ended_at, activity_id
            """, (_iso(since),))
        return [self._decode(row) for row in rows]

    def set_capture_status(self, activity_id: int, status: str) -> None:
        """Summarize capture outcomes across the segment, retaining any success."""
        if status not in _CAPTURE_STATUS:
            raise ValueError("Unknown capture status")
        with self.connection:
            result = self.connection.execute("""
                UPDATE activities SET capture_status = CASE
                    WHEN capture_status = 'captured' OR :status = 'captured' THEN 'captured'
                    WHEN capture_status = 'failed' OR :status = 'failed' THEN 'failed'
                    ELSE 'skipped' END
                WHERE activity_id = :activity_id
            """, {"activity_id": activity_id, "status": status})
            if result.rowcount == 0:
                raise KeyError(activity_id)


def _app_name(name: str | None) -> str | None:
    if name:
        name = "".join(char for char in name if char.isprintable()).strip()[:128]
    return name or None


def _is_browser(app_name: str | None) -> bool:
    return bool(app_name and app_name.casefold().removesuffix(".exe").replace("-", " ") in _BROWSERS)


def _private_title(title: str | None) -> bool:
    return bool(title and _PRIVATE_TITLE.search(title))


def _idle_value(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value >= 0 else None


def _excluded_app_rules(exclusions: tuple[str, ...]) -> tuple[str, ...]:
    return (*_DEFAULT_EXCLUDED_APPS, *(app.strip().casefold() for app in exclusions if app.strip()))


def _app_excluded(app_name: str | None, app_id: str | None, exclusions: tuple[str, ...]) -> bool:
    name, bundle = (app_name or "").casefold(), (app_id or "").casefold()
    return any(rule in name or rule in bundle for rule in exclusions)


def _excluded_domain_rules(exclusions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(domain.strip().casefold().strip(".") for domain in exclusions if domain.strip().strip("."))


def _domain_excluded(domain: str, exclusions: tuple[str, ...]) -> bool:
    return any(domain == rule or domain.endswith("." + rule) for rule in exclusions)


def _mochi_process(pid: int) -> bool:
    module = getattr(sys.modules.get("__main__"), "__spec__", None)
    return pid == os.getpid() or (
        pid == os.getppid() and getattr(module, "name", None) == "deskpet.observation_v2.desktop"
    )


@dataclass(frozen=True, slots=True)
class _Identity:
    app: str | None
    window: int | str | None
    title: bytes | None
    page: bytes | None
    private: bool
    document: tuple[str | None, str | None]


class ActivityRecorder:
    """Join contiguous observations, and commit the Activity before requesting a frame."""

    def __init__(self, store: ActivityStore, *, platform: str = sys.platform,
                 poll_interval_s: float | None = None,
                 capture_interval_s: float = CAPTURE_INTERVAL_S,
                 capture_enabled: bool = False, excluded_apps: tuple[str, ...] = (),
                 excluded_domains: tuple[str, ...] = (),
                 document_local_sources: tuple[str, ...] = (),
                 on_sample_write: SampleWrite | None = None,
                 reset_wall_on_rollback: bool = False) -> None:
        self.store = store
        self.poll_interval_s = (POLL_INTERVAL_S.get(platform, 1.0)
                                if poll_interval_s is None else poll_interval_s)
        for interval in (self.poll_interval_s, capture_interval_s):
            if not math.isfinite(interval) or interval <= 0:
                raise ValueError("Activity and capture intervals must be positive and finite")
        self.capture_interval_s = capture_interval_s
        self.capture_enabled = capture_enabled
        self._on_sample_write = on_sample_write
        self._reset_wall_on_rollback = reset_wall_on_rollback
        self._excluded_apps = _excluded_app_rules(excluded_apps)
        self._excluded_domains = _excluded_domain_rules(excluded_domains)
        self._document_local_sources = normalize_document_sources(document_local_sources)
        self.last_capture_reason: str | None = None
        self.capture_ineligibility: str | None = "foreground_unavailable"
        self.browser_url_status: str | None = None
        self._activity_id: int | None = None
        self._wall_floor: datetime | None = None
        self._identity: _Identity | None = None
        self._previous: ActivitySample | None = None
        self._previous_document_marker: bool | None = None
        self._last_capture_at: float | None = None
        self._last_capture_identity: _Identity | None = None
        self._key = secrets.token_bytes(32)

    @property
    def current_activity(self) -> Activity | None:
        return self.store.get_activity(self._activity_id) if self._activity_id else None

    @property
    def capture_eligible(self) -> bool:
        return self.capture_ineligibility is None

    def _document(self, sample: ActivitySample, app: str | None, *, browser: bool,
                  own: bool, private: bool, excluded: bool) -> tuple[str | None, str | None, str]:
        if sample.screen_locked is True:
            return None, None, "screen_locked"
        if own:
            return None, None, "mochi_foreground"
        if private or excluded:
            return None, None, "private_window"
        if not app or not sample.window_id or sample.screen_locked is not False:
            return None, None, "foreground_unavailable"
        if browser:
            if sample.private_window is not False or not _page_domain(sample.url):
                return None, None, "browser_metadata_unavailable"
            source = browser_document_source(sample.url)
            if source is None:
                return None, None, "title_unavailable"
            title = sample.active_tab_title or sample.window_title
        else:
            source = native_document_source(app, sample.app_id)
            title = sample.window_title
        if not document_source_allowed(source, self._document_local_sources):
            return None, None, "source_not_allowed"
        name, status = document_title(title, source)
        return name, source, status

    def _fingerprint(self, value: str | None) -> bytes | None:
        if value:
            return hmac.digest(self._key, value.encode("utf-8", errors="replace"), "sha256")[:16]
        return None

    def _elapsed(self, previous: ActivitySample, current: ActivitySample) -> float | None:
        elapsed = current.monotonic_s - previous.monotonic_s
        wall = (current.observed_at - previous.observed_at).total_seconds()
        if (not 0 < elapsed <= 3 * self.poll_interval_s or wall <= 0
                or abs(wall - elapsed) > 1.0):
            return None
        return elapsed

    def observe(self, sample: ActivitySample) -> CaptureRequest | None:
        observed_at = _utc(sample.observed_at)
        if not math.isfinite(sample.monotonic_s):
            raise ValueError("Monotonic sample time must be finite")
        if sample.screen_locked is not None and not isinstance(sample.screen_locked, bool):
            raise ValueError("Screen lock must be a boolean or None")
        idle_s = _idle_value(sample.idle_s)
        system_at = None
        if sample.system_sampled_at is not None or idle_s is not None or sample.screen_locked is not None:
            system_at = _utc(sample.system_sampled_at) if sample.system_sampled_at is not None else observed_at
            if system_at > observed_at:
                raise ValueError("System sample is newer than its observation")
        app = None if sample.screen_locked else _app_name(sample.app_name)
        own = app is not None and is_mochi_app(app, sample.app_id)
        if own:
            app = "Mochi"
        browser = _is_browser(app)
        titles = (sample.window_title, sample.active_tab_title if browser else None)
        sensitive_title = any(word in title.casefold() for title in titles if title for word in _SENSITIVE_TITLE_WORDS)
        private = (sample.private_window is True or browser and any(_private_title(title) for title in titles)
                   or sensitive_title)
        candidate_domain = _page_domain(sample.url) if browser else None
        domain = candidate_domain if not private else None
        normalized_app = app.casefold() if app else None
        excluded = bool(
            app and _app_excluded(app, sample.app_id, self._excluded_apps)
            or candidate_domain and _domain_excluded(candidate_domain, self._excluded_domains)
        )
        self.browser_url_status = None
        if browser:
            self.browser_url_status = "private_window" if private else ("available" if domain else "unavailable")
        document_name, document_source, document_status = self._document(
            sample, app, browser=browser, own=own, private=private, excluded=excluded,
        )
        native = native_document_identity(sample.window_title, document_source) if document_name else None
        document_identity, document_marker = native if native is not None else (document_name, None)
        redacted_title = "[redacted]" if app and sample.window_title and not (private or own) else None
        capture_block = self._capture_blocked(sample, app=app, own=own, private=private, excluded=excluded)
        capture_ineligibility = capture_block or (
            "lock_state_unavailable" if sample.screen_locked is None else None
        )
        identity = _Identity(normalized_app,
                             sample.window_id if app and not (private or own) else None,
                             self._fingerprint(document_identity or (
                                 sample.window_title if redacted_title and not domain and not document_source else None
                             )),
                             self._fingerprint(sample.url) if domain else None, private,
                             (document_source, document_status))
        # Keep only filtered or hashed fields after this call. Session and window IDs
        # are used in memory and never written to the timeline.
        sanitized = ActivitySample(observed_at, sample.monotonic_s, idle_s=idle_s,
                                   screen_locked=sample.screen_locked, session_id=sample.session_id,
                                   system_sampled_at=system_at)
        previous = self._previous
        current = self.current_activity
        if previous == sanitized and identity == self._identity:
            self.capture_ineligibility = capture_ineligibility
            self.last_capture_reason = "duplicate_sample"
            return None
        elapsed = self._elapsed(previous, sanitized) if previous is not None else None
        continuous = (previous is not None and elapsed is not None
                      and previous.screen_locked is sanitized.screen_locked
                      and previous.session_id == sanitized.session_id)
        if self._wall_floor is not None and observed_at < self._wall_floor:
            if not self._reset_wall_on_rollback:
                self.close()
                self.last_capture_reason = "clock_rollback"
                return None
            self.close()
            current, continuous = None, False
        same = not excluded and continuous and identity == self._identity and current is not None
        boundary = (not continuous or excluded or self._identity is not None and (
            self._identity.private != private or (self._identity.app == "mochi") != own
        ))
        with self.store.connection:
            if continuous and current is not None:
                # A missing poll can hide a clean, dirty, or different editing view.
                appeared = (
                    same and current.document_change is None and elapsed <= 1.5 * self.poll_interval_s
                    and document_marker is True and self._previous_document_marker is False
                )
                current = replace(
                    current, ended_at=observed_at, duration_s=current.duration_s + elapsed,
                    idle_s=idle_s if same else current.idle_s,
                    system_sampled_at=system_at if same else current.system_sampled_at,
                    document_change="unsaved_changes_appeared" if appeared else current.document_change,
                    document_change_at=observed_at if appeared else current.document_change_at,
                )
                self.store._extend(current)
            if not same and not excluded:
                current = self.store._insert(Activity(
                    0, observed_at, observed_at, 0.0, app, redacted_title,
                    domain, idle_s, sample.screen_locked, system_at,
                    document_name=document_name, document_source=document_source, document_status=document_status,
                ))
            if self._on_sample_write is not None:
                # Readers see both the new Activity and any realtime invalidation
                # when this transaction commits.
                self._on_sample_write(sanitized, None if excluded else current, boundary, not same)

        if excluded:
            self.close()
            self.capture_ineligibility = "privacy_excluded"
            self._wall_floor = observed_at
            self.last_capture_reason = "privacy_excluded"
            return None
        if not continuous:
            self._last_capture_identity = None
        if self._last_capture_at is not None and sample.monotonic_s < self._last_capture_at:
            self._last_capture_at = None
        self._activity_id = current.activity_id
        self._identity = identity
        self._previous = sanitized
        self._previous_document_marker = document_marker
        self._wall_floor = observed_at
        self.capture_ineligibility = capture_ineligibility
        self.last_capture_reason = self._capture_reason(sanitized, identity, capture_block)
        if self.last_capture_reason in {"initial", "periodic", "app_change", "browser_page_change"}:
            self._last_capture_at = sample.monotonic_s
            self._last_capture_identity = identity
            return CaptureRequest(self.current_activity, self.last_capture_reason)
        return None

    def _capture_blocked(self, sample: ActivitySample, *, app: str | None, own: bool,
                         private: bool, excluded: bool) -> str | None:
        if excluded:
            return "privacy_excluded"
        if sample.screen_locked:
            return "screen_locked"
        if own:
            return "mochi_foreground"
        if private:
            return "private_window"
        if app is None:
            return "foreground_unavailable"
        if self._excluded_domains and self.browser_url_status == "unavailable":
            return "browser_url_unavailable"
        return None

    def _capture_reason(self, sample: ActivitySample, identity: _Identity, capture_block: str | None) -> str:
        if not self.capture_enabled:
            return "capture_disabled"
        if capture_block is not None:
            return capture_block
        if self._last_capture_at is None:
            return "initial"
        since_capture = sample.monotonic_s - self._last_capture_at
        if since_capture >= self.capture_interval_s:
            return "periodic"
        previous = self._last_capture_identity
        if identity != previous:
            browser_navigation = (previous is not None and identity.app == previous.app
                                  and identity.window == previous.window
                                  and identity.page is not None and previous.page is not None
                                  and identity.page != previous.page)
            cooldown = BROWSER_COOLDOWN_S if browser_navigation else APP_COOLDOWN_S
            if since_capture >= cooldown:
                return "browser_page_change" if browser_navigation else "app_change"
            return "cooldown"
        return "browser_url_unavailable" if self.browser_url_status == "unavailable" else "not_due"

    def add_recording_interval(self, started_at: datetime, ended_at: datetime, *,
                               capture_status: str = "skipped") -> Activity:
        start, end = _utc(started_at), _utc(ended_at)
        if start > end:
            raise ValueError("Activity interval ends before it starts")
        if capture_status not in _CAPTURE_STATUS:
            raise ValueError("Unknown capture status")
        activity = Activity(0, start, end, (end - start).total_seconds(), capture_status=capture_status)
        with self.store.connection:
            return self.store._insert(activity)

    def close(self) -> None:
        """End the session at its last observation without inventing a tail."""
        self._activity_id = None
        self._identity = None
        self._previous = None
        self._previous_document_marker = None
        self._last_capture_identity = None
        self.capture_ineligibility = "foreground_unavailable"


def _command(*args: str, timeout: float = 0.4) -> str:
    return subprocess.run(args, capture_output=True, check=True, text=True,
                          errors="replace", timeout=timeout).stdout.strip()


class NativeActivitySampler:
    """Read desktop metadata without Qt, input hooks, or screenshots."""

    def __init__(self, *, browser_metadata: bool = False,
                 document_local_sources: tuple[str, ...] = (),
                 excluded_apps: tuple[str, ...] = (), excluded_domains: tuple[str, ...] = ()) -> None:
        self.platform = sys.platform
        self.browser_metadata = browser_metadata
        self._excluded_apps = _excluded_app_rules(excluded_apps)
        self._excluded_domains = _excluded_domain_rules(excluded_domains)
        self._document_domains = tuple(
            domain for source in normalize_document_sources(document_local_sources) if source.startswith("domain:")
            and not _domain_excluded(domain := source.removeprefix("domain:"), self._excluded_domains)
        )
        self.errors: dict[str, str] = {}
        self._browser_retry_after: dict[str, float] = {}

    def _probe(self, name, reader, default=None):
        try:
            return reader()
        except Exception as error:
            self.errors[name] = type(error).__name__
            return default

    def sample(self) -> ActivitySample:
        self.errors = {}
        if self.platform == "darwin":
            desktop, foreground, idle_reader = _mac_session, _mac_foreground, _mac_idle
        elif self.platform == "win32":
            desktop, foreground, idle_reader = _win_session, _win_foreground, _win_idle
        elif self.platform == "linux":
            desktop, foreground, idle_reader = _linux_session, _linux_foreground, _linux_idle
        else:
            raise RuntimeError(f"Native activity metadata is unavailable on {self.platform}")
        locked, session_id = self._probe("session", desktop, (None, None))
        if locked is None:
            self.errors.setdefault("session", "lock_state_unavailable")
        app, window_id, title, app_id = None, None, None, None
        if locked is not True:
            app, window_id, title, *native_ids = self._probe("foreground", foreground, (None, None, None))
            app_id = native_ids[0] if native_ids else None
            if app is None:
                self.errors.setdefault("foreground", "no_foreground_window")
        private, url, tab_title = None, None, None
        if _is_browser(app):
            if _private_title(title) or any(word in (title or "").casefold() for word in _SENSITIVE_TITLE_WORDS):
                private = True
            elif not _app_excluded(app, app_id, self._excluded_apps):
                private, url, tab_title = self._browser_window(app, app_id, locked is False)
                if browser_document_source(url) and _page_domain(url) in self._document_domains:
                    current = self._probe("foreground", foreground, (None, None, None))
                    if current[:2] != (app, window_id) or current[2] != title:
                        self.errors["browser_url"] = "foreground_changed"
                        app, window_id, title = None, None, None
                        private, url, tab_title = None, None, None
            if private:
                title, tab_title = None, None
        idle = self._probe("idle", idle_reader)
        if idle is None:
            self.errors.setdefault("idle", "idle_unavailable")
        observed_at = datetime.now(UTC)
        return ActivitySample(observed_at, time.monotonic(), app, window_id, title, url,
                              idle_s=idle, screen_locked=locked, session_id=session_id,
                              system_sampled_at=observed_at,
                              private_window=private, app_id=app_id, active_tab_title=tab_title)

    def _browser_window(self, app: str, app_id: str | None,
                        document_titles: bool) -> tuple[bool | None, str | None, str | None]:
        bundle_id = _MAC_CHROMIUM_APPS.get(app.casefold()) if self.platform == "darwin" else None
        document_domains = self._document_domains if document_titles else ()
        if not self.browser_metadata and not document_domains:
            self.errors["browser_url"] = "native_url_disabled"
        elif bundle_id is None or app_id is not None and bundle_id.casefold() != app_id.casefold():
            self.errors["browser_url"] = "unsupported_browser"
        elif time.monotonic() < self._browser_retry_after.get(bundle_id, 0.0):
            self.errors["browser_url"] = "probe_backoff"
        else:
            try:
                private, url, title = _mac_browser_window(bundle_id, document_domains)
            except (OSError, subprocess.SubprocessError) as error:
                self.errors["browser_url"] = type(error).__name__
                self._browser_retry_after[bundle_id] = time.monotonic() + 30.0
            else:
                if _page_domain(url) not in document_domains or browser_document_source(url) is None:
                    title = None
                    if not self.browser_metadata and private is not True:
                        private, url = None, None
                if private is not True and url is None:
                    self.errors["browser_url"] = "native_url_unavailable"
                return private, url, title
        return None, None, None


def _mac_active_bundle_path(bundle_id: str) -> str | None:
    import AppKit

    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None or app.bundleIdentifier() != bundle_id:
        return None
    bundle = app.bundleURL()
    return bundle.path() if bundle is not None else None


def _mac_browser_window(bundle_id: str, document_domains: tuple[str, ...]) -> tuple[bool | None, str | None, str | None]:
    path = _mac_active_bundle_path(bundle_id)
    if not path or "\n" in path or "\r" in path:
        return None, None, None
    # Resolve only the allowed, frontmost bundle, and check mode before reading a URL.
    target = path.replace("\\", "\\\\").replace('"', '\\"')
    permitted_hosts = " or ".join(
        f'pageURL starts with "{scheme}://{host}/"' for host in document_domains for scheme in ("https", "http")
    )
    tab_title = ""
    if permitted_hosts:
        tab_title = f'''            if {permitted_hosts} then
                return "normal:" & pageURL & linefeed & (title of active tab as text)
            end if
'''
    script = f'''if application "{target}" is not running then return ""
tell application "{target}"
    if not frontmost then return ""
    if (count of windows) is 0 then return ""
    tell front window
        if mode is not "normal" then return "private"
        set pageURL to (URL of active tab as text)
{tab_title}        return "normal:" & pageURL
    end tell
end tell'''
    response = _command("/usr/bin/osascript", "-e", script, timeout=1.2)
    if response == "private":
        return True, None, None
    if response.startswith("normal:"):
        url, _, title = response.removeprefix("normal:").partition("\n")
        if _page_domain(url):
            return False, url, title or None
        return False, None, None
    return None, None, None


def _mac_session() -> tuple[bool | None, str | None]:
    import Quartz

    # PyObjC resolves this symbol lazily. The UI, scheduler and sampler can
    # reach it together on startup, while the bridge consumes its metadata.
    with _MAC_SESSION_LOOKUP:
        try:
            copy_session = Quartz.CGSessionCopyCurrentDictionary
        except (AttributeError, KeyError):
            return None, None
    session = copy_session()
    if session is None:
        return None, None
    console = session.get("kCGSSessionOnConsoleKey")
    locked = session.get("CGSSessionScreenIsLocked")
    user_id = session.get("kCGSSessionUserIDKey")
    locked = bool(locked) if locked is not None else None
    if console is not None and not console:
        locked = True
    elif console and locked is None:
        locked = False
    return locked, str(user_id) if user_id is not None else None


def _mac_foreground() -> tuple[str | None, int | None, str | None, str | None]:
    import AppKit
    import Quartz
    from deskpet.perception.macos import window_infos

    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None, None, None, None
    pid = app.processIdentifier()
    app_name, app_id = app.localizedName(), app.bundleIdentifier() or "macos:unidentified"
    if _mochi_process(pid) or is_mochi_app(app_name, app_id):
        return "Mochi", None, None, app_id
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    ) or []
    front_windows = (window for window in windows if window.get("kCGWindowOwnerPID") == pid)
    window = next(iter(window_infos(front_windows, pid, os.getpid())), None)
    return app_name, (window.handle if window else None), (window.title if window else None), app_id


def _mac_idle() -> float | None:
    import Quartz

    idle = Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType,
    )
    return _idle_value(idle)


def _win_session() -> tuple[bool | None, str | None]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    open_desktop = user32.OpenInputDesktop
    open_desktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_desktop.restype = wintypes.HANDLE
    info = user32.GetUserObjectInformationW
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD))
    info.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
    desktop = open_desktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    desktop_error = ctypes.get_last_error() if not desktop else 0
    session = wintypes.DWORD()
    session_id = (str(session.value) if ctypes.windll.kernel32.ProcessIdToSessionId(
        os.getpid(), ctypes.byref(session),
    ) else None)
    if not desktop:
        return (True if desktop_error == 5 else None), session_id
    try:
        name = ctypes.create_unicode_buffer(128)
        needed = wintypes.DWORD()
        if info(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):  # UOI_NAME
            return name.value.casefold() != "default", session_id
        return None, session_id
    finally:
        user32.CloseDesktop(desktop)


def _win_foreground() -> tuple[str | None, int | None, str | None, str | None]:
    import psutil
    import win32gui
    import win32process

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None, None, None, None
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    if _mochi_process(pid):
        return "Mochi", None, None, None
    app = psutil.Process(pid).name()
    return app, hwnd, win32gui.GetWindowText(hwnd), app


def _win_idle() -> float | None:
    import ctypes
    from ctypes import wintypes

    class LastInputInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.UINT), ("last_input_ms", wintypes.DWORD)]

    info = LastInputInfo()
    info.size = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.last_input_ms) % (2 ** 32)
    return elapsed_ms / 1000


def _linux_session() -> tuple[bool | None, str | None]:
    session = os.environ.get("XDG_SESSION_ID")
    if not session:
        return None, None
    options = _command("loginctl", "show-session", session, "-p", "LockedHint", "-p", "Active")
    props = dict(line.split("=", 1) for line in options.splitlines() if "=" in line)
    if props.get("Active") == "no" or props.get("LockedHint") == "yes":
        return True, session
    return (False if props.get("LockedHint") == "no" else None), session


def _linux_is_x11() -> bool:
    return bool(os.environ.get("DISPLAY")) and os.environ.get("XDG_SESSION_TYPE") != "wayland"


def _linux_foreground() -> tuple[str | None, str | None, str | None, str | None]:
    if not _linux_is_x11():
        raise RuntimeError("Desktop foreground metadata requires an X11 session")
    root = _command("xprop", "-root", "-notype", "_NET_ACTIVE_WINDOW")
    window = re.search(r"0x[\da-fA-F]+", root)
    if window is None or int(window[0], 16) == 0:
        return None, None, None, None
    props = _command("xprop", "-id", window[0], "-notype", "WM_CLASS", "_NET_WM_NAME", "WM_NAME", "_NET_WM_PID")
    fields = dict(line.split(" = ", 1) for line in props.splitlines() if " = " in line)
    pid = fields.get("_NET_WM_PID", "")
    if pid.isdecimal() and _mochi_process(int(pid)):
        return "Mochi", None, None, None
    classes = re.findall(r'"([^"\\]*)"', fields.get("WM_CLASS", ""))
    title = fields.get("_NET_WM_NAME", fields.get("WM_NAME", "")).strip('"')
    app = classes[-1] if classes else None
    # X11 client-supplied class names do not establish an authorized app identity.
    return app, window[0], title or None, (f"x11:{app}" if app else None)


def _linux_idle() -> float | None:
    if not _linux_is_x11():
        raise RuntimeError("Native idle time requires an X11 session")
    return _idle_value(float(_command("xprintidle")) / 1000)
