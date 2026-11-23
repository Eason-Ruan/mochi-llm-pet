"""Production desktop composition and the durable UI handoff for System 2."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from hashlib import sha256
import json
import sqlite3

from deskpet.observation_v2.routes import SYSTEM2
from deskpet.settings import SettingsStore
from deskpet.timezones import resolve_timezone

from .refine_conversation_store import ConversationStore


class DesktopSystem2:
    def __init__(self, args, env, settings):
        from deskpet.system2.host import System2Host
        from deskpet.system2.items import ItemService
        from deskpet.system2.scheduler import Scheduler
        from deskpet.system2.tools import System2ToolExecutor

        self.env = env
        self.settings = settings
        self.lock_probe = args.desktop_lock_probe
        self.chat = ConversationStore(args.business_database)
        timezone = resolve_timezone(getattr(args, "user_timezone", None))
        items = ItemService(env.tasks, timezone=timezone.key)
        self.scheduler = Scheduler(env.tasks, items, observation=env.observation_context_reader,
                                   clock=env.clock, lock_probe=self.lock_probe)
        self.pet = PetIntentStore(args.business_database, self.lock_probe, self.scheduler, settings.routes)
        env.tasks.auto_opportunity = False
        env.tasks.on_occurrence_invalidated = self.scheduler.invalidate_occurrence
        env.sessions.on_focus_released = self.scheduler.on_focus_released
        self.gateway = System2ToolExecutor(
            args.business_database, items=items, phases=env.sessions, settings=settings,
            scheduler=self.scheduler, pet=self.pet, context_reader=env.observation_context_reader,
            now=env.clock.wall_now,
        )
        self.host = System2Host(
            args.data_dir, scheduler=self.scheduler, gateway=self.gateway, settings_store=settings,
            context_reader=env.observation_context_reader, clock=env.clock,
            engine=getattr(args, "system2_engine", None),
            display_capabilities=args.desktop_display_capabilities,
            timezone=timezone.key,
        )
        self.gateway.after_local_commit = self.host.fence_local
        self.host.start()

    def apply_preferences(self, action):
        from deskpet.system2.tool_schema import SettingsPatch

        if not set(action["changes"]).issubset(SettingsPatch.model_fields):
            return None
        return self.gateway.execute_local("settings.update", {
            "scope": "default", "patch": action["changes"], "expected_version": action["expected_version"],
        }, event_id=action["event_id"])

    def pump(self):
        for message_id, body, context in self.chat.queued_turns():
            if existing := self.host.admission_for_message(message_id):
                self.chat.record_admission(message_id, existing)
                continue
            queued_at = context.pop("queued_at", None)
            now = self.env.clock.wall_now()
            created_at = datetime.fromisoformat(queued_at) if queued_at else None
            if created_at is None or not created_at <= now < created_at + self.host.turn_timeout:
                self.chat.record_answer(f"expired-{message_id}", message_id,
                                        "这条消息在提交前已过期。仍需执行时，请重新发送。")
                continue
            predecessor = context.pop("supersedes_message_id", None)
            if predecessor:
                context["supersedes_admission_id"] = self.chat.admission_for(predecessor)
            try:
                admission = self.host.submit_user(body, event_id=message_id, created_at=created_at, **context)
            except (ValueError, RuntimeError):
                self.chat.record_answer(f"submission-{message_id}", message_id,
                                        "这条消息未能提交，请核对当前状态后重试。")
            else:
                self.chat.record_admission(message_id, admission)
        processed = []
        for event in self.host.ui_events():
            if event.kind == "reply" and (text := event.payload.get("text")):
                self.chat.record_answer(event.answer_id, event.reply_to_message_id, text)
            self.host.ack_ui_event(event.sequence)
            processed.append(event)
        return processed

    def resolve(self, action):
        if action.get("origin") != "user":
            raise ValueError("A proposal button requires a trusted user action")
        self.host.resolve_proposal(
            action["proposal_id"], action["answer"], version=action["expected_version"],
            event_id=action["event_id"], amendments=action.get("amendments"),
        )

    def snapshot_fields(self):
        route = self.settings.routes.snapshot(SYSTEM2)
        return {
            "system2_route_status": ("verified" if route.verified else "unverified") if route.route else "not_configured",
            "system2_chat_version": self.chat.projection_version(),
            "system2_habits_version": tuple((habit.id, habit.version) for habit in self.settings.list_habits()),
            "system2_proposals": self.host.pending_proposals(),
            "system2_proposal_presentations": self.gateway.pending_presentations(self.settings.user_scope),
            "system2_pet_intents": self.pet.pending(self.env.clock.wall_now()),
        }

    def fence_task(self, db, command, previous):
        """Order a successful Qt edit against pending chat edits to the same target."""
        function = {
            "edit": "item.update", "configure": "reminder.configure", "archive": "item.archive",
            "cancel": "item.set_status", "complete": "item.set_status", "set_status": "item.set_status",
            "snooze": "reminder.snooze", "ack": "reminder.ack",
        }.get(command.kind)
        if function is None or previous is None:
            return
        if command.kind in {"snooze", "ack"}:
            target = {"occurrence_id": previous.occurrence_id}
        else:
            target = {"id": previous.task_id}
        self.host.fence_local(db, function, target, command.command_id)

    def close(self):
        self.host.close()


class PetIntentStore:
    """Record model motion requests before dispatch and claim each at most once."""

    def __init__(self, path, lock_probe, scheduler, routes):
        self.path = path
        self.lock_probe = lock_probe
        self.scheduler = scheduler
        self.routes = routes
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS system2_pet_intents (
                delivery_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL, payload TEXT NOT NULL,
                expires_at TEXT NOT NULL, state TEXT NOT NULL
            )""")

    def present(self, *, intent, position_hint=None, animation=None, sound=None, authority, now, db):
        poses = {"idle", "look_up", "wave", "approach"}
        positions = {"stay", "home", "screen_edge"}
        if not isinstance(position_hint, (str, type(None))) or (
            position_hint is not None and position_hint not in positions
        ) or (animation is not None and animation not in poses):
            return {"status": "rejected", "error_code": "INVALID_ARGUMENT"}
        pose = animation or (intent if intent in poses else None)
        if pose in {None, "idle"} and position_hint in {None, "stay"} and not sound:
            return {"status": "rejected", "error_code": "UNSUPPORTED_CHANNEL"}
        if (
            authority.context_revision is None or authority.observation_cursor is None
            or authority.observation_generation is None or authority.observation_valid_until is None
        ):
            return {"status": "rejected", "error_code": "STALE_CONTEXT"}
        context = {
            "context_revision": authority.context_revision, "observation_cursor": authority.observation_cursor,
            "observation_generation": authority.observation_generation,
            "context_valid_until": authority.observation_valid_until,
        }
        opportunity = self.scheduler.authorize_pet(now=now, db=db, **context)
        if not opportunity or opportunity != authority.opportunity_id:
            return {"status": "rejected", "error_code": "STALE_CONTEXT"}
        delivery_id = "pet-" + sha256(
            f"{authority.user_scope}:{authority.admission_id}:{authority.call_id}".encode()
        ).hexdigest()[:32]
        if not self.scheduler.claim_opportunity(opportunity_id=opportunity, delivery_id=delivery_id, now=now, db=db):
            return {"status": "rejected", "error_code": "BUSY"}
        effect = {"position_hint": position_hint or "stay", "animation": pose or "idle", "sound": bool(sound)}
        context["context_valid_until"] = context["context_valid_until"].isoformat()
        payload = {"effect": effect, "context": context, "opportunity_id": opportunity,
                   "route": self._route_version(db)}
        expires_at = min(authority.expires_at, authority.observation_valid_until)
        db.execute("INSERT OR IGNORE INTO system2_pet_intents VALUES (?,?,?,?,?)",
                   (delivery_id, authority.user_scope, json.dumps(payload), expires_at.isoformat(), "queued"))
        return {"status": "pending", "delivery_id": delivery_id, "view": {"channel": "qt", **effect}}

    def _route_version(self, db):
        route = self.routes._get(db, SYSTEM2)
        return [route.route_generation, route.consent_generation, route.verified]

    def _valid(self, db, delivery_id, payload, expires_at, now):
        if now >= datetime.fromisoformat(expires_at) or payload["route"] != self._route_version(db):
            return False
        context = payload["context"]
        context["context_valid_until"] = datetime.fromisoformat(context["context_valid_until"])
        return self.scheduler.pet_permit(opportunity_id=payload["opportunity_id"], delivery_id=delivery_id,
                                         now=now, db=db, **context)

    def _revoke(self, db, delivery_id):
        db.execute("UPDATE system2_pet_intents SET state='revoked' WHERE delivery_id=? AND user_scope=? AND state='queued'",
                   (delivery_id, self.routes.user_scope))
        self.scheduler.release_opportunity(delivery_id=delivery_id, db=db)

    def pending(self, now):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM system2_pet_intents WHERE user_scope=? AND state='queued' LIMIT 1",
                          (self.routes.user_scope,)).fetchone() is None:
                return ()
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT delivery_id,payload,expires_at FROM system2_pet_intents
                WHERE user_scope=? AND state='queued' ORDER BY rowid""", (self.routes.user_scope,)).fetchall()
            active = []
            for row in rows:
                if self._valid(db, row["delivery_id"], json.loads(row["payload"]), row["expires_at"], now):
                    active.append(row["delivery_id"])
                else:
                    self._revoke(db, row["delivery_id"])
            return tuple(active)

    def claim(self, delivery_id, now):
        if self.lock_probe() is not False:
            return None
        try:
            with closing(sqlite3.connect(self.path, timeout=0.025)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("""SELECT user_scope, payload, expires_at FROM system2_pet_intents
                    WHERE delivery_id=? AND user_scope=? AND state='queued'""",
                    (delivery_id, self.routes.user_scope)).fetchone()
                if row is None:
                    return None
                payload = json.loads(row["payload"])
                if not self._valid(db, delivery_id, payload, row["expires_at"], now):
                    self._revoke(db, delivery_id)
                    return None
                settings = SettingsStore.read_at(self.path, user_scope=row["user_scope"], db=db)
                payload = payload["effect"]
                if not settings.allow_proactive_movement:
                    payload["position_hint"] = "stay"
                if settings.reduce_motion:
                    payload.update(position_hint="stay", animation="idle")
                payload["sound"] = payload["sound"] and settings.reminder_sound
                if payload["position_hint"] == "stay" and payload["animation"] == "idle" and not payload["sound"]:
                    self._revoke(db, delivery_id)
                    return None
                db.execute("UPDATE system2_pet_intents SET state='claimed' WHERE delivery_id=?", (delivery_id,))
                return payload
        except sqlite3.OperationalError:
            return None

    def receipt(self, delivery_id, *, sound_requested=False):
        try:
            with closing(sqlite3.connect(self.path, timeout=0.025)) as db, db:
                db.execute("UPDATE system2_pet_intents SET state=? WHERE delivery_id=? AND user_scope=? AND state='claimed'",
                           ("motion_and_sound_requested" if sound_requested else "motion_requested",
                            delivery_id, self.routes.user_scope))
        except sqlite3.OperationalError:
            return
