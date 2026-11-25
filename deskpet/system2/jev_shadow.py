"""Evaluate Jev on ordinary idle ticks beside the unchanged System 2 turn."""

from datetime import datetime
import json
import logging
from threading import Lock, Thread

from .admission import json_text, stable_id, timestamp
from .jev_api import JevClient, JevResult, jev_shadow_requested, load_jev_settings
from .jev_projection import JevReview, capture_jev_review, project_jev_snapshot
from .scheduler import TICK_INTERVAL


log = logging.getLogger(__name__)
_WAKE = "wake_system2"
_DEFER = "defer"
_DECISIONS = {
    _WAKE: "Send new actionable evidence, material contradictions, or relevant uncertainty to System 2. Examples include accumulated independent work evidence without a focus phase or two spaced, gap-free captures of unrelated entertainment during focus. Distinguish entertainment from course or research material related to the current focus.",
    _DEFER: "Wait until the next idle tick when there is no new actionable evidence, no candidate, or an existing matching offer, or when only a first independent unrelated-entertainment capture can safely wait for confirmation. Continuing work during a fitting focus phase can wait if activity coverage shows no meaningful gap.",
}
_ALREADY_HANDLED = {"no_candidate", "pending_offer"}
_MIN_DEFER_CONFIDENCE = 0.8


def is_pure_tick(event: dict) -> bool:
    wakes = event.get("wakes", ())
    if (event.get("kind") != "background" or event.get("trigger_reasons") != ["tick"]
            or event.get("kinds") != ["tick"] or len(wakes) != 1
            or any(event.get(key) for key in ("subject_refs", "occurrence_ids", "phase_elapsed"))):
        return False
    wake = wakes[0]
    return bool(wake.get("event_id") and event.get("trigger_ids") == [wake["event_id"]]
                and wake.get("kind") == wake.get("reason") == "tick"
                and not any(wake.get(key) is not None for key in (
                    "occurrence_id", "publication_cursor", "producer_generation", "phase_elapsed", "task_version",
                    "after_cursor", "original_presentation", "retry_of_event_id", "scope_review_of",
                )) and not any(wake.get(key) for key in ("subject_refs", "change_categories", "carried_for_scope_review")))


def jev_cap_key(snapshot) -> str:
    review = snapshot.review
    return stable_id(review.phase, review.candidate, review.activity, review.feedback)


def jev_cap_reached(snapshot, previous_record, *, as_of: datetime) -> bool:
    if (snapshot.local_defer_reason in _ALREADY_HANDLED or not previous_record
            or previous_record.get("host_choice") != _DEFER
            or previous_record.get("cap_key") != jev_cap_key(snapshot)):
        return False
    previous = datetime.fromisoformat(previous_record["tick_at"])
    return previous < as_of <= previous + TICK_INTERVAL * 2


def adjudicate_jev(settings, client, snapshot, *, send_permitted, cap_reached=False):
    if snapshot.skip_reason:
        return None, _WAKE, snapshot.skip_reason
    try:
        result = client.adjudicate(settings, snapshot.state, criteria=_DECISIONS, send_permitted=send_permitted)
    except Exception as exc:
        log.info("Jev shadow evaluation failed (%s)", type(exc).__name__)
        return None, _WAKE, "client_error"
    if result.status != "ok":
        return result, _WAKE, result.status
    if result.choice not in _DECISIONS or result.confidence is None:
        return result, _WAKE, "invalid_response"
    if result.choice == _WAKE:
        return result, _WAKE, "model_wake"
    if result.confidence < _MIN_DEFER_CONFIDENCE:
        return result, _WAKE, "low_confidence"
    if cap_reached:
        return result, _WAKE, "defer_cap"
    return result, _DEFER, snapshot.local_defer_reason or "model_defer"


class JevShadow:
    # A desktop restart inside the same process can briefly leave the old
    # worker running. Scope the slot by the same database and user as DSH.
    _slots = set()
    _slots_lock = Lock()

    def __init__(self, store, clock, capture, stopping, *, settings_provider=None, client=None):
        self._store = store
        self._clock = clock
        self._capture = capture
        self._stopping = stopping
        self._settings_provider = settings_provider or load_jev_settings
        self._requested = jev_shadow_requested if settings_provider is None else lambda: True
        self._client = client if client is not None else JevClient()
        self._slot = self._store.path, self._store.scope
        self._epoch = 0

    def invalidate(self):
        with self._slots_lock:
            self._epoch += 1

    def capture_real_review(self, context):
        if not self._requested():
            return None
        try:
            # Only closed-set categories and local digests are persisted.
            return capture_jev_review(context, share_text=False).to_dict()
        except Exception as exc:
            log.info("Jev real review capture failed (%s)", type(exc).__name__)
            return None

    def start(self, admission, now):
        if not self._requested() or not is_pure_tick(admission.event):
            return
        tick_at = datetime.fromisoformat(admission.event["wakes"][0]["at"])
        if not tick_at <= now < tick_at + TICK_INTERVAL:
            return
        with self._slots_lock:
            if self._slot in self._slots or self._stopping.is_set():
                return
            self._slots.add(self._slot)
            epoch = self._epoch
        try:
            Thread(target=self._run, args=(admission, tick_at, now, epoch), name="mochi-jev-shadow", daemon=True).start()
        except RuntimeError:
            with self._slots_lock:
                self._slots.discard(self._slot)
            log.info("Jev shadow worker could not start")

    def _current(self, admission, epoch):
        with self._slots_lock:
            unchanged = epoch == self._epoch
        if (not unchanged or self._stopping.is_set()
                or not admission.created_at <= self._clock.wall_now() < admission.expires_at):
            return False
        with self._store.connect() as db:
            return self._store.jev_tick_current(admission.admission_id, db=db)

    def _run(self, admission, tick_at, started, epoch):
        record = {
            "admission_id": admission.admission_id,
            "tick_at": timestamp(tick_at),
            "started_at": timestamp(started),
            "sent_at": None,
            "model_choice": None,
            "host_choice": _WAKE,
            "last_real_review_at": None,
        }
        try:
            try:
                settings = self._settings_provider()
                if not settings.enabled:
                    return
                self._evaluate(admission, tick_at, epoch, settings, record)
            except Exception as exc:
                log.info("Jev shadow worker failed (%s)", type(exc).__name__)
                record.update(status="local_error", host_choice=_WAKE, host_reason="local_error")
            record["completed_at"] = timestamp(self._clock.wall_now())
            self._save(record)
        except Exception as exc:
            log.info("Jev shadow record unavailable (%s)", type(exc).__name__)
        finally:
            with self._slots_lock:
                self._slots.discard(self._slot)

    def _evaluate(self, admission, tick_at, epoch, settings, record):
        if not settings.available:
            record.update(status="unavailable", host_reason="unavailable")
            return
        record["model"] = settings.model
        if not self._current(admission, epoch):
            record.update(status="discarded", host_reason="context_changed")
            return
        with self._store.connect() as db:
            db.execute("BEGIN")
            last_review = self._store.jev_previous_review(admission.admission_id, db=db)
            previous = self._previous(admission.admission_id, db=db)
        review = None
        if last_review is not None:
            record["last_real_review_at"] = last_review["model_reviewed_at"]
            if last_review["jev_review"]:
                review = JevReview.from_dict(json.loads(last_review["jev_review"]))

        def capture():
            evidence = self._capture(admission, self._clock.wall_now())
            if evidence.get("bypass"):
                return None, None, evidence["bypass"]
            snapshot = project_jev_snapshot(evidence["context"], next_tick_in_s=evidence["next_tick_in_s"],
                                            share_text=settings.share_text and evidence["share_text"], last_review=review)
            return snapshot, evidence["guard"], snapshot.skip_reason

        snapshot, guard, bypass = capture()
        if bypass:
            record.update(status="bypassed", host_reason=bypass)
            return
        if record["last_real_review_at"] is not None:
            snapshot.state["last_system2_review_at"] = record["last_real_review_at"]
        record["cap_key"] = jev_cap_key(snapshot)
        cap_reached = jev_cap_reached(snapshot, previous, as_of=tick_at)

        def recheck():
            # Refresh policy and material facts beside dispatch and completion.
            # Routine observation publications do not change the projection fingerprint.
            if not self._current(admission, epoch) or self._settings_provider() != settings:
                return False
            latest, current_guard, bypass = capture()
            return (not bypass and current_guard == guard and latest.fingerprint == snapshot.fingerprint
                    and self._current(admission, epoch))

        if not recheck():
            record.update(status="discarded", host_reason="context_changed")
            return
        result, choice, reason = adjudicate_jev(settings, self._client, snapshot, send_permitted=recheck,
                                                cap_reached=cap_reached)
        if isinstance(result, JevResult):
            record.update(model_choice=result.choice if result.choice in _DECISIONS else None,
                          confidence=result.confidence, input_tokens=result.input_tokens,
                          output_tokens=result.output_tokens, latency_ms=result.latency_ms,
                          model=result.model or settings.model, probabilities=result.probabilities,
                          http_status=result.http_status,
                          sent_at=timestamp(result.send_started_at) if result.send_started_at is not None else None)
        if reason == "cancelled" or not recheck():
            record.update(status="discarded", host_reason="context_changed")
            return
        record.update(status=result.status if result is not None else reason,
                      host_choice=choice, host_reason=reason)

    @staticmethod
    def _create_table(db):
        db.execute("""CREATE TABLE IF NOT EXISTS s2_jev_shadow_records (
            user_scope TEXT NOT NULL, admission_id TEXT NOT NULL, record TEXT NOT NULL,
            PRIMARY KEY (user_scope, admission_id))""")

    @staticmethod
    def _has_table(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='s2_jev_shadow_records'").fetchone()

    def _previous(self, admission_id, *, db):
        if not self._has_table(db):
            return None
        row = db.execute("""SELECT r.record FROM s2_admissions AS a
            JOIN s2_jev_shadow_records AS r ON r.user_scope=a.user_scope AND r.admission_id=a.admission_id
            WHERE a.user_scope=? AND a.sequence < (SELECT sequence FROM s2_admissions WHERE admission_id=?)
            ORDER BY a.sequence DESC LIMIT 1""", (self._store.scope, admission_id)).fetchone()
        return json.loads(row["record"]) if row and row["record"] else None

    def _save(self, record):
        with self._store.write() as db:
            self._create_table(db)
            if record["status"] == "ok" and not self._store.jev_tick_current(record["admission_id"], db=db):
                record.update(status="discarded", host_choice=_WAKE, host_reason="context_changed")
            db.execute("INSERT OR IGNORE INTO s2_jev_shadow_records VALUES (?, ?, ?)",
                       (self._store.scope, record["admission_id"], json_text(record)))

    def records(self, limit):
        with self._store.connect() as db:
            if not self._has_table(db):
                return ()
            return tuple(json.loads(row["record"]) for row in db.execute("""SELECT record
                FROM s2_jev_shadow_records WHERE user_scope=? ORDER BY rowid DESC LIMIT ?""",
                (self._store.scope, limit)))
