"""Replay synthetic System 2 inputs through locally isolated production services."""

from collections import Counter
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from math import ceil, isfinite
import os
from pathlib import Path
from random import Random
import sqlite3
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from deskpet.c_ui.desktop_system2 import PetIntentStore
from deskpet.contracts import DisplayReceipt, TaskCommand
from deskpet.mocks.clock import FakeClock
from deskpet.mocks.delivery import FakeDeliveryPort
from deskpet.observation_v2.context import ObservationContextReader
from deskpet.observation_v2.pipeline import ObservationStore
from deskpet.observation_v2.state import Observation
from deskpet.settings import SettingsStore
from deskpet.system2.host import System2Host
from deskpet.system2.items import ItemService
from deskpet.system2.scheduler import Scheduler
from deskpet.system2.semantic_policy import semantic_background_enabled
from deskpet.system2.tool_schema import READ_TOOLS, canonical_tool_name, tool_definitions
from deskpet.system2.tools import System2ToolExecutor
from deskpet.task_service.presentation import PresentationLedger
from deskpet.task_service.sqlite import SQLiteTaskService
from deskpet.work_sessions.models import PhaseView
from deskpet.work_sessions.persistent import PersistentWorkSessionService
from evaluation.interruption_replay import SimulatedNotifications, score_interruption_case, summarize_interruptions


FIXTURES = Path(__file__).with_name("data") / "system2-semantic-replay.json"
BACKGROUND_POLICIES = {"legacy_background": "0", "semantic_background": "1"}
DISPLAY_EFFECTS = frozenset({"reminder_shown", "proposal_shown", "pet_shown"})
FAILURES = frozenset({"model_unavailable", "expired_request", "superseded", "interrupted", "parse_failed"})
VALID_TOOLS = {tool.canonical_name for tool in tool_definitions}


def read_cases():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["scenarios"]


def _stamp(at):
    return at.isoformat(timespec="seconds")


def _public_receipt(function, result):
    action = canonical_tool_name(function)
    entry = {"action": action if action in VALID_TOOLS else "unknown_tool", "status": result.get("status")}
    for field in ("original_status", "error_code", "retry_at", "outcome"):
        if result.get(field) is not None:
            entry[field] = result[field]
    return entry


class ReplayHost(System2Host):
    def __init__(self, *args, **kwargs):
        self.receipts = []
        super().__init__(*args, **kwargs)

    def _call(self, admission, bound, context, grants, name, native_call_id, args, **kwargs):
        result = super()._call(admission, bound, context, grants, name, native_call_id, args, **kwargs)
        self.receipts.append(_public_receipt(name, result))
        return result


class ReplayEngine:
    """Use fixture or captured decisions at the engine boundary, without making API requests."""

    def __init__(self, runtime, recordings):
        self.runtime = runtime
        self.recordings = recordings
        self.step = None
        self.turns = []

    def close(self):
        pass

    def run(self, request, invoke):
        data = json.loads(request.input_json)
        event = data["event"]
        utterance = (data.get("user_intent") or {}).get("utterance", "")
        observation = data["context"].get("current") or {}
        trace = {
            "_admission_id": request.admission_id,
            "origin": request.source,
            "at": event["at"],
            "trigger_kinds": event.get("kinds", [event["kind"]]),
            "trigger_reasons": event["trigger_reasons"],
            "context_state": observation.get("state"),
            "context_status": data["context"].get("current_status"),
            "proposed": [],
        }
        if elapsed := event.get("phase_elapsed"):
            trace["phase_elapsed"] = _phase_elapsed(elapsed)
        self.turns.append(trace)
        if self.recordings is None:
            behavior = self.step.get("engine", {})
        else:
            ordinal = len(self.turns) - 1
            try:
                behavior = self.recordings[self.step["id"]][ordinal]
            except (KeyError, IndexError):
                trace["source_error"] = "missing_recorded_model_turn"
                raise LookupError("No recorded turn at this position") from None
            if behavior["origin"] != request.source or set(behavior["trigger_reasons"]) != set(event["trigger_reasons"]):
                trace["source_error"] = "recorded_model_trigger_mismatch"
                raise LookupError("The recorded turn belongs to a different trigger")
            usage = behavior.get("usage", {})
            trace["recorded_usage"] = {k: usage[k] for k in ("requests", "latency_ms", "cost_usd") if k in usage}
        if behavior.get("delay_s"):
            self.runtime.clock.advance(behavior["delay_s"])
        if behavior.get("unavailable"):
            raise TimeoutError("Recorded model failure")
        if behavior.get("parse_failure"):
            trace["source_error"] = "parse_failed"
            raise ValueError("Recorded model response did not parse")
        if self.recordings is None:
            for ordinal, event in enumerate(behavior.get("before_calls", ())):
                self.runtime.action(event, f"in-flight-{self.step['id']}-{ordinal}")
        for ordinal, tool in enumerate(behavior.get("calls", ()), 1):
            if tool.get("utterance_contains", "") not in utterance:
                continue
            if tool.get("wake_reason") and tool["wake_reason"] not in event["trigger_reasons"]:
                continue
            name = tool["action"]
            try:
                args = self.runtime.resolve(tool.get("args", {}))
            except KeyError:
                trace["source_error"] = "missing_replay_subject"
                continue
            if tool.get("at_request_plus_s") is not None:
                due = datetime.fromisoformat(event["at"]) + timedelta(seconds=tool["at_request_plus_s"])
                args.setdefault("reminder_rule", {})["remind_by"] = _stamp(due)
            result = invoke(name, f"replay-{ordinal}", args)
            trace["proposed"].append(_public_receipt(name, result))
            if (alias := tool.get("save_result_as")) and isinstance(view := result.get("view"), dict):
                self.runtime.save_alias(alias, view)
        return behavior.get("reply", "quiet")


class Runtime:
    def __init__(self, directory, case, recordings):
        self.start = datetime.fromisoformat(case["at"])
        self.timezone = case["timezone"]
        self.clock = FakeClock(self.start)
        self.path = directory / "business.sqlite3"
        self.locked = False
        self.aliases = {}
        self.delivered = set()
        self.prompt_responses = []
        self.proposal_buttons = []
        self.primary_by_occurrence = {}
        self.synthetic_sound_requests = []
        self.last_sound_event = 0
        self.native_projection = {}
        self.passive_reminders = set()
        self.rendered_channels = set()
        self.observation_path = directory / "observation.sqlite3"
        self.observation = ObservationStore(self.observation_path, live=True, boot_id="offline-replay")
        self.observation.begin_live_run(self.now, monotonic_s=0, poll_interval_s=30)
        clock = self.clock

        class RecordedClockReader(ObservationContextReader):
            def read_observation_context(self, as_of, **kwargs):
                return self.read_realtime_context(as_of, monotonic_s=clock.monotonic_now(), **kwargs)

        reader = RecordedClockReader(self.observation_path, boot_id="offline-replay")
        self.settings = SettingsStore(self.path)
        self.port = FakeDeliveryPort(self.clock, outcome="missing")
        self.tasks = SQLiteTaskService(self.clock, self.port, lambda *_, **__: None,
                                       path=self.path, auto_opportunity=False)
        self.phases = PersistentWorkSessionService(path=self.path, clock=self.clock, settings_store=self.settings)
        self.items = ItemService(self.tasks, timezone=self.timezone)
        self.scheduler = Scheduler(self.tasks, self.items, observation=reader, clock=self.clock,
                                   lock_probe=lambda: self.locked)
        self.tasks.on_occurrence_invalidated = self.scheduler.invalidate_occurrence
        self.phases.on_focus_released = self.scheduler.on_focus_released
        self.phases.attach_system2(on_phase_elapsed=self.scheduler.on_phase_elapsed,
                                   on_phase_started=self.scheduler.invalidate_focus_waits)
        self.pet = PetIntentStore(self.path, lambda: self.locked, self.scheduler, self.settings.routes)
        self.gateway = System2ToolExecutor(self.path, items=self.items, phases=self.phases, settings=self.settings,
                                           scheduler=self.scheduler, pet=self.pet, context_reader=reader)
        self.ledger = PresentationLedger(self.path, source="simulated", authorize_early=self.scheduler.authorize_qt)
        self.surface = None
        if "interruption" in case:
            from deskpet.c_ui.presentation import DesktopPresentation

            self.notifications = SimulatedNotifications(self.clock)
            self.notifications.available = case["interruption"].get("native", False)
            self.surface = DesktopPresentation(path=self.path, synthetic=True, notifications=self.notifications,
                                               lock_probe=lambda: self.locked)
            self.surface.ledger = self.ledger
            # The isolated transport and patched beep exercise the actual routing and audio gates.
            self.surface.native = True
        self.engine = ReplayEngine(self, recordings)
        self.directory = directory
        self.reader = reader
        self.open_host()

    def open_host(self):
        self.host = ReplayHost(self.directory, scheduler=self.scheduler, gateway=self.gateway, engine=self.engine,
                               settings_store=self.settings, clock=self.clock, manage_scheduler=False,
                               context_reader=self.reader, timezone=self.timezone, display_capabilities=lambda: {
                                   "pet_card": self.locked is False, "pet_badge": self.locked is False,
                                   "os_notification": bool(self.surface and self.notifications.available),
                               })
        self.gateway.after_local_commit = self.host.fence_local

    @property
    def now(self):
        return self.clock.wall_now()

    def close(self):
        self.host.close()
        self.phases.close()
        self.tasks.close()
        self.observation.close()

    def resolve(self, value):
        if isinstance(value, dict):
            return {key: self.resolve(field) for key, field in value.items()}
        if isinstance(value, list):
            return [self.resolve(field) for field in value]
        if isinstance(value, str) and value.startswith("$"):
            if value == "$cursor":
                return self.scheduler.snapshot(self.now).get("publication_cursor") or 0
            if value.startswith("$at:"):
                return _stamp(self.start + timedelta(seconds=float(value.removeprefix("$at:"))))
            name, field = value[1:].split(".", 1)
            return self.aliases[name][field]
        return value

    def save_alias(self, alias, view):
        self.aliases[alias] = {**view, "occurrence_id": (view.get("occurrence") or {}).get("id")}

    def publish(self, settings):
        now = self.now
        mono = self.clock.monotonic_now()
        with self.observation.connection:
            self.observation._record_live_sample(now, mono)
        self.observation.save_observation(Observation(
            settings.get("kind", "work"), settings["state"], settings.get("summary"), (),
            now, now + timedelta(seconds=settings.get("ttl_s", 15)), settings["revision"],
        ), published_at=now, monotonic_s=mono)

    def action(self, event, identity):
        kind, payload = next(iter(event.items()))
        if kind == "local":
            result = self.gateway.execute_local(payload["action"], self.resolve(payload.get("args", {})),
                                                event_id=identity)
            if result.status != "applied":
                raise ValueError(f"Fixture setup {identity} was rejected ({result.error_code})")
            if alias := payload.get("save_as"):
                self.save_alias(alias, result.view)
        elif kind == "settings":
            self.settings.update(expected_version=self.settings.read().version, **payload)
        elif kind == "chat":
            sent_at = self.start + timedelta(seconds=payload["sent_offset_s"]) if "sent_offset_s" in payload else self.now
            predecessor = payload.get("supersedes_message_id")
            self.host.submit_user(payload["text"], event_id=payload.get("message_id", identity),
                                  supersedes_admission_id=self.host.admission_for_message(predecessor) if predecessor else None,
                                  selected_ref=self.resolve(payload.get("selected_ref")), created_at=sent_at)
        elif kind == "observation":
            self.publish(payload)
        elif kind == "lock":
            self.locked = payload
        elif kind == "poll":
            self.phases.pump()
            self.scheduler.poll()
        elif kind == "local_pump":
            self.phases.pump()
            self.tasks.pump()
        elif kind == "sleep":
            if semantic_background_enabled():
                self.host.suspend_background()

                def suspend(db, at):
                    self.host.fence_background(db)
                    self.scheduler.close_opportunity(reason="sleep", now=at, db=db)

                self.phases.record_sleep(on_sleep=suspend)
            else:
                self.phases.record_sleep()
                self.scheduler.close_opportunity(reason="sleep", now=self.now)
        elif kind == "resume":
            self.phases.record_wake(on_wake=lambda db, at: self.scheduler.resume(at, db=db))
            self.host.resume_background()
        elif kind == "resolve_proposal":
            cards = self.host.pending_proposals()
            if cards:
                card = cards[0]
                self.host.resolve_proposal(card["id"], payload, version=card["version"], event_id=identity)
                if self.surface:
                    self.proposal_buttons.append((card["id"], payload))
        elif kind in {"reminder_action", "native_action"}:
            self.reminder_action(payload, identity, native=kind == "native_action")
        elif kind == "cancel_reminder":
            item = self.aliases[payload]
            self.tasks.apply(TaskCommand(command_id=identity, kind="cancel", task_id=item["id"],
                                         expected_version=item["version"]))
        elif kind == "restart_host":
            self.host.close()
            self.open_host()
        else:
            raise ValueError(f"Unknown replay event {kind}")

    def reminder_action(self, payload, identity, *, native):
        item = self.aliases.get(payload["reminder"], {})
        occurrence = item.get("occurrence_id")
        display = self.primary_by_occurrence.get(occurrence, {})
        action = payload["action"]
        target = None
        status, error = "no_prompt", None
        if native and (identifier := display.get("os_id")):
            target = self.ledger.native_response_target(identifier, action, self.now)
            status, error = "rejected", "STALE_PRESENTATION"
        elif not native and display.get("channel") == "pet_card":
            target = self.tasks.get(item["id"])
            status, error = "rejected", "STALE_PRESENTATION"
        if target and (native or target.occurrence_id == occurrence):
            args = {"occurrence_id": target.occurrence_id, "expected_version": target.version}
            if action == "snooze":
                args.update(delay_s=payload.get("delay_s", 300), mode="exact")
            result = self.gateway.execute_local(f"reminder.{action}", args, event_id=identity)
            status, error = result.status, result.error_code
            if status == "applied" and (alias := payload.get("save_as")):
                self.save_alias(alias, result.view)
        self.prompt_responses.append({"occurrence": occurrence, "subject": occurrence, "delivery_id": display.get("id"),
                                      "action": action, "status": status, "error_code": error})

    def resolve_proposal_buttons(self):
        if not self.proposal_buttons:
            return
        with closing(sqlite3.connect(self.path)) as db:
            proposals = db.execute("SELECT id,business_key,json_extract(view, '$.state') FROM system2_proposals")
            by_id = {identity: (key, state) for identity, key, state in proposals}
        for identity, action in self.proposal_buttons:
            subject, state = by_id[identity]
            applied = state == {"accept": "accepted", "reject": "rejected", "cancel": "cancelled"}[action]
            display = self.primary_by_occurrence.get(identity, {})
            self.prompt_responses.append({"occurrence": identity, "subject": subject, "delivery_id": display.get("id"),
                                          "action": action, "status": "applied" if applied else "rejected"})
        self.proposal_buttons.clear()

    def state(self):
        items = {task.task_id: task for task in self.tasks.list_tasks()}
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT view FROM phase_views ORDER BY rowid").fetchall()
            proposals = dict(db.execute("SELECT id,json_extract(view, '$.state') FROM system2_proposals"))
        phases = [PhaseView.model_validate_json(view) for (view,) in rows]
        return BusinessState(items, {phase.id: phase for phase in phases}, self.settings.read().to_dict(),
                             {habit.id: habit for habit in self.settings.list_habits()}, proposals)

    def displays(self):
        self.tasks.pump()
        results = []
        foreground_used = False
        if self.surface:
            results = self.interruption_displays()
            foreground_used = any(display["status"] == "shown" and display["channel"] == "pet_card"
                                  for display in results)
        for command in (self.port.commands if self.surface is None else ()):
            if command.delivery_id in self.delivered:
                continue
            if command.channel == "os_notification":
                results.append(self.display("reminder", command.delivery_id, False, channel=command.channel,
                                            status="display_simulation_unsupported_channel"))
                continue
            if foreground_used and command.channel == "pet_card":
                continue
            permit = self.ledger.permit_badge if command.channel == "pet_badge" else self.ledger.permit_qt
            shown = (self.locked is False and permit(command, self.now)
                     and self.ledger.mark_painted(command, self.now))
            sound = bool(shown and command.channel == "pet_card" and command.sound
                         and self.ledger.sound_event(command, self.now, True, "simulated"))
            if shown:
                self.tasks.record_receipt(DisplayReceipt(
                    delivery_id=command.delivery_id, task_id=command.task_id, task_version=command.task_version,
                    rendered_at=self.now, outcome="displayed", source="simulated",
                ))
                foreground_used = foreground_used or command.channel == "pet_card"
            results.append(self.display("reminder", command.delivery_id, shown,
                                        reason=command.reason, occurrence=f"{command.task_id}:{command.occurrence}",
                                        sound=sound, channel=command.channel))
        for row in self.gateway.pending_presentations():
            channel = row["channel"]
            if foreground_used and channel == "pet_card":
                continue
            key = row["delivery_id"]
            generation = row["presentation_generation"]
            permitted = self.gateway.permit_presentation(key, generation=generation)
            shown = bool(self.locked is False and permitted and self.gateway.validate_presentation(key, generation=generation))
            if permitted:
                shown = self.gateway.ack_presentation(key, displayed=shown, generation=generation) and shown
            foreground_used = foreground_used or (shown and channel == "pet_card")
            fields = {}
            if self.surface:
                with closing(sqlite3.connect(self.path)) as db:
                    subject, = db.execute("SELECT business_key FROM system2_proposals WHERE id=?",
                                          (row["proposal_id"],)).fetchone()
                fields = {"occurrence": row["proposal_id"], "subject": subject, "primary": channel == "pet_card",
                          "actionable": channel == "pet_card"}
                if shown and row.get("request_sound"):
                    self.request_simulated_sound(key, "proposal")
            results.append(self.display("proposal", key, shown, channel=channel, **fields))
        for delivery in self.pet.pending(self.now):
            if foreground_used:
                break
            key = delivery["delivery_id"] if isinstance(delivery, dict) else delivery
            if effect := self.pet.claim(key, self.now):
                sound = effect["sound"]
                if self.surface and sound:
                    sound = self.request_simulated_sound(key, "pet")
                shown = self.pet.receipt(key, displayed=True, sound_requested=sound, now=self.now)
                results.append(self.display("pet", key, shown, sound=sound, channel="qt",
                                            position_hint=effect["position_hint"], animation=effect["animation"]))
                foreground_used = shown
        for display in results:
            if display["status"] == "shown" and display.get("primary"):
                self.primary_by_occurrence.setdefault(display["occurrence"], display)
            if display["kind"] == "reminder" and display["status"] == "shown":
                self.rendered_channels.add((display["channel"], display["id"]))
        self.record_revoked_displays(results)
        if self.surface:
            self.collect_sounds()
            self.passive_reminders = {f"{entry['task_id']}:{entry['occurrence']}"
                                      for entry in self.scheduler.passive_reminders(self.now)}
        return results

    def request_simulated_sound(self, delivery_id, contact_kind):
        with patch("deskpet.c_ui.presentation.QApplication"):
            return self.surface.request_attention_sound("reminder", delivery_id, contact_kind=contact_kind)

    def collect_sounds(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("""SELECT e.id, json_extract(e.fields, '$.delivery_id'), r.contact_kind,
                                         p.task_id, p.occurrence
                                  FROM events e LEFT JOIN attention_sound_requests r
                                  ON json_extract(e.fields, '$.delivery_id')=r.delivery_id
                                  LEFT JOIN presentations p
                                  ON json_extract(e.fields, '$.delivery_id')=p.delivery_id
                                  WHERE e.kind='sound.requested' AND e.id > ? ORDER BY e.id""",
                              (self.last_sound_event,)).fetchall()
        for position, identity, kind, task, ordinal in rows:
            occurrence = f"{task}:{ordinal}" if task else identity
            if kind == "phase_end":
                occurrence = identity.rsplit(":", 1)[0]
            self.synthetic_sound_requests.append({"kind": kind, "occurrence": occurrence, "delivery_id": identity})
            self.last_sound_event = position

    def interruption_displays(self):
        state = self.state()
        phases = [phase.model_dump(mode="json") for phase in state.phases.values()]
        active = next((phase for phase in phases if phase["state"] in {"running", "paused"}), None)
        snapshot = {"now": _stamp(self.now), "tasks": [task.model_dump(mode="json") for task in state.items.values()],
                    "settings": state.settings, "active_phase": active, "phases": phases,
                    "pending_deliveries": self.tasks.pending_deliveries()}
        with patch("deskpet.c_ui.presentation.QApplication"):
            self.surface.observe(snapshot)
        results = []
        commands = sorted(self.port.commands, key=lambda cmd: cmd.reason not in {"hard_due", "deadline_fallback"})
        for command in commands:
            if (command.channel, command.delivery_id) in self.rendered_channels:
                continue
            if self.locked is not False or command.channel == "os_notification":
                self.surface.route_hidden(command)
                continue
            if command.channel == "pet_badge":
                if self.surface.permit_badge(command):
                    results.extend(self.paint_reminders([command]))
                continue
            if any(display["channel"] == "pet_card" for display in results):
                continue
            if self.surface.permit(command):
                if command.reason in {"hard_due", "deadline_fallback"}:
                    due = state.items[command.task_id].draft.hard_deadline
                    others = [other for other in commands if other != command
                              and (other.channel, other.delivery_id) not in self.rendered_channels
                              and other.reason in {"hard_due", "deadline_fallback"}
                              and state.items[other.task_id].draft.hard_deadline == due
                              and self.surface.can_group(other)]
                    group = [command, *others]
                    for other in group[1:2]:
                        self.surface.permit(other)
                    results.extend(self.paint_reminders(group[:2]))
                    self.surface.remember_group(group[2:])
                    self.rendered_channels.update((other.channel, other.delivery_id) for other in group[2:])
                else:
                    results.extend(self.paint_reminders([command]))
        rows = self.ledger.rows()
        by_os = {row["os_id"]: row for row in rows if row["channel"] == "os_notification"}
        for identifier, at in self.notifications.delivered.items():
            row = by_os[identifier]
            if ("os_notification", row["delivery_id"]) not in self.rendered_channels:
                results.append(self.display("reminder", row["delivery_id"], True, channel="os_notification",
                                            occurrence=f"{row['task_id']}:{row['occurrence']}", primary=True,
                                            actionable=bool(row.get("native_actions")), reason="hard_due",
                                            os_id=identifier) | {"at": _stamp(at)})
        self.surface.rows = rows
        self.native_projection = self.surface.pending_projection(self.now)
        if self.locked is False and not any(display["channel"] == "pet_card" for display in results):
            for followup in self.surface.native_followups(include_painted=False, automatic=True)[:1]:
                if self.surface.native_followup_current(followup, mark_painted=True):
                    occurrence = f"{followup.task_id}:{followup.occurrence}"
                    results.append(self.display("reminder", f"followup:{followup.delivery_id}", True,
                                                channel="pet_card", occurrence=occurrence, primary=True,
                                                actionable=True, reason="native_followup"))
        return results

    def paint_reminders(self, commands):
        with patch("deskpet.c_ui.presentation.QApplication"):
            painted = self.surface.painted(commands)
        with closing(sqlite3.connect(self.path)) as db:
            sound_ids = {identity for identity, in db.execute(
                "SELECT json_extract(fields, '$.delivery_id') FROM events WHERE kind='sound.requested'"
            )}
        results = []
        for command in painted:
            occurrence = f"{command.task_id}:{command.occurrence}"
            sound = command.delivery_id in sound_ids
            self.tasks.record_receipt(DisplayReceipt(
                delivery_id=command.delivery_id, task_id=command.task_id, task_version=command.task_version,
                rendered_at=self.now, outcome="displayed", source="simulated",
            ))
            actionable = command.channel == "pet_card"
            results.append(self.display("reminder", command.delivery_id, True, reason=command.reason,
                                        occurrence=occurrence, sound=sound, channel=command.channel,
                                        primary=actionable, actionable=actionable))
        return results

    def record_revoked_displays(self, displays):
        seen = self.delivered | {display["id"] for display in displays}
        with closing(sqlite3.connect(self.path)) as db:
            for table, kind in (("system2_proposal_presentations", "proposal"), ("system2_pet_intents", "pet")):
                ids = db.execute(f"SELECT delivery_id FROM {table} WHERE state IN ('revoked','unknown')").fetchall()
                for (identity,) in ids:
                    if identity not in seen:
                        displays.append(self.display(kind, identity, False))
        self.delivered.update(display["id"] for display in displays)

    def display(self, kind, identity, shown, *, channel=None, status=None, **fields):
        return {"kind": kind, "id": identity, "channel": channel,
                "status": status or ("shown" if shown else "display_layer_blocked"),
                "at": _stamp(self.now), **fields}

    def decisions(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("""SELECT a.admission_id,a.source,a.event,d.record FROM s2_decisions d
                                 JOIN s2_admissions a ON d.decision_id=a.admission_id ORDER BY a.sequence""").fetchall()
        return [{"_admission_id": identity, "origin": source, "at": decision["as_of"],
                 "trigger_kinds": trigger.get("kinds", [trigger["kind"]]),
                 "trigger_reasons": trigger["trigger_reasons"],
                 "action": decision["action"], "reason_code": decision["reason_code"]}
                for identity, source, event, record in rows
                for trigger, decision in [(json.loads(event), json.loads(record))]]


@dataclass(frozen=True)
class BusinessState:
    items: dict
    phases: dict
    settings: dict
    habits: dict
    proposals: dict


def _changed(before, after, displays):
    effects = []
    for identity, task in after.items.items():
        old = before.items.get(identity)
        view = {"id": identity, "occurrence": task.occurrence, "_title": task.draft.content,
                "_description": task.draft.description, "_due_at": task.draft.due_at,
                "status": task.effective_issue_status if task.draft.item_type == "issue" else task.status,
                "deadline": _stamp(task.draft.hard_deadline) if task.draft.hard_deadline else None}
        if old is None:
            effects.append({"action": f"created_{task.draft.item_type}", "at": _stamp(task.created_at), **view})
        elif task.occurrence > old.occurrence:
            effects.append({"action": "rescheduled_reminder", **view})
        elif old.version != task.version:
            effects.append({"action": "changed_item", **view})
    for identity, phase in after.phases.items():
        old = before.phases.get(identity)
        if old is None:
            effects.append({"action": "started_focus" if phase.kind == "focus" else "started_break",
                            "id": identity, "source": phase.source, "duration_s": phase.planned_duration_s})
        elif phase.version != old.version:
            action = "phase_elapsed" if phase.state == "elapsed" and old.state != "elapsed" else "changed_phase"
            effects.append({"action": action, "id": identity, "kind": phase.kind,
                            "before_state": old.state, "state": phase.state, "duration_s": phase.planned_duration_s})
    if before.settings != after.settings:
        effects.append({"action": "changed_settings", "fields": [field for field in after.settings
                        if field != "version" and before.settings.get(field) != after.settings[field]]})
    for identity, habit in after.habits.items():
        old = before.habits.get(identity)
        if old is None:
            effects.append({"action": "created_habit", "id": identity, "status": habit.status})
        elif old != habit:
            effects.append({"action": "changed_habit", "id": identity, "status": habit.status})
    effects.extend({"action": f"{display['kind']}_shown", **display}
                   for display in displays if display["status"] == "shown")
    return effects


def _phase_elapsed(elapsed):
    return [{"kind": phase.get("phase_kind"), "elapsed_at": phase.get("elapsed_at"),
             "transition_reason": phase.get("transition_reason"),
             "successor": (phase.get("successor") or {}).get("kind")} for phase in elapsed]


def _evaluate(expectation, effects, decisions, replies, turns, resolve):
    actual = Counter(effect["action"] for effect in effects)
    expected = Counter(expectation.get("effects", ()))
    missed = list((expected - actual).elements())
    false = list((actual - expected).elements())
    checks = {}
    if "deadline" in expectation:
        deadlines = [effect["deadline"] for effect in effects if "deadline" in effect]
        target = datetime.fromisoformat(expectation["deadline"])
        checks["original_time_deadline"] = len(deadlines) == 1 and bool(deadlines[0]) and (
            datetime.fromisoformat(deadlines[0]) == target)
    if "reply" in expectation:
        checks["reply_status"] = [reply["status"] for reply in replies] == expectation["reply"]
    if "question_contains" in expectation:
        questions = [line for reply in replies for line in reply["_text"].splitlines()
                     if line.rstrip().endswith(("?", "？"))]
        checks["specific_clarification"] = len(questions) == 1 and all(
            fragment in questions[0] for fragment in expectation["question_contains"])
    if "wake_reasons" in expectation:
        checks["merged_wake_reasons"] = any(
            decision["origin"] == "background"
            and set(expectation["wake_reasons"]).issubset(decision["trigger_reasons"]) for decision in decisions
        )
    if "no_wake_reasons" in expectation:
        checks["no_extra_wake"] = not any(
            set(expectation["no_wake_reasons"]).intersection(decision["trigger_reasons"]) for decision in decisions
        )
    if "local_deadline" in expectation:
        expected_at = datetime.fromisoformat(expectation["local_deadline"])
        hard = [item for item in effects if item["action"] == "reminder_shown"
                and item["reason"] in {"hard_due", "deadline_fallback"}]
        checks["local_hard_deadline"] = len(hard) == 1 and datetime.fromisoformat(hard[0]["at"]) == expected_at
    if expectation.get("silent"):
        checks["no_sound"] = not any(effect.get("sound") for effect in effects)
    if expectation.get("no_motion"):
        checks["no_motion"] = all(effect.get("position_hint") == "stay" and effect.get("animation") == "idle"
                                  for effect in effects if effect["action"] == "pet_shown")
    if "successor" in expectation:
        checks["automatic_successor_in_context"] = any(
            phase["successor"] == expectation["successor"] for turn in turns for phase in turn.get("phase_elapsed", ())
        )
    if expected_item := expectation.get("item"):
        matching = [effect for effect in effects if effect.get("_title") == expected_item.get("title")]
        checks["intended_item"] = len(matching) == 1
        if len(matching) == 1:
            item = matching[0]
            for field in ("description", "occurrence", "status"):
                if field in expected_item:
                    checks[f"item_{field}"] = item.get(f"_{field}", item.get(field)) == expected_item[field]
            if "id" in expected_item:
                checks["item_target"] = item["id"] == resolve(expected_item["id"])
            if "due_at" in expected_item:
                checks["item_due_at"] = item.get("_due_at") == datetime.fromisoformat(expected_item["due_at"])
    if "channel" in expectation:
        checks["display_channel"] = [effect.get("channel") for effect in effects if effect["action"] in DISPLAY_EFFECTS
                                     ] == [expectation["channel"]]
    return {"missed_actions": missed, "false_actions": false,
            "appropriate_interventions": sum((actual & expected)[name] for name in DISPLAY_EFFECTS),
            "unnecessary_interruptions": sum(action in DISPLAY_EFFECTS for action in false), "checks": checks}


def run_case(case, recordings=None):
    with TemporaryDirectory(prefix="mochi-system2-replay-") as name:
        runtime = Runtime(Path(name), case, recordings)
        try:
            for index, event in enumerate(case.get("setup", ())):
                runtime.action(event, f"setup-{index}")
            reports = []
            prior_decisions = set()
            for step in case["steps"]:
                at = runtime.start + timedelta(seconds=step["offset_s"])
                runtime.clock.advance((at - runtime.now).total_seconds())
                before = runtime.state()
                prior_projection = (set(runtime.native_projection), runtime.passive_reminders.copy())
                runtime.prompt_responses.clear()
                runtime.synthetic_sound_requests.clear()
                for index, event in enumerate(step.get("events", ())):
                    runtime.action(event, f"{step['id']}-{index}")
                runtime.engine.step = step
                runtime.engine.turns.clear()
                runtime.host.receipts.clear()
                elapsed = []
                if step.get("run", True):
                    for _ in range(16):
                        started = perf_counter()
                        if not runtime.host.pump():
                            break
                        elapsed.append((perf_counter() - started) * 1000)
                    else:
                        raise RuntimeError(f"Replay never drained {case['id']}/{step['id']}")
                decisions = [decision for decision in runtime.decisions()
                             if decision["_admission_id"] not in prior_decisions]
                source_errors = {turn.pop("_admission_id"): turn["source_error"] for turn in runtime.engine.turns
                                 if "source_error" in turn}
                for decision in decisions:
                    identity = decision.pop("_admission_id")
                    prior_decisions.add(identity)
                    if identity in source_errors and decision["reason_code"] == "model_unavailable":
                        decision["engine_failure"] = source_errors[identity]
                for turn in runtime.engine.turns:
                    turn.pop("_admission_id", None)
                displays = runtime.displays() if step.get("render", True) else []
                state = runtime.state()
                runtime.resolve_proposal_buttons()
                effects = _changed(before, state, displays)
                proposal_responses = [response for identity, response in state.proposals.items()
                                      if response in {"accepted", "rejected", "cancelled"}
                                      and before.proposals.get(identity) != response]
                if alias := step.get("save_created_item_as"):
                    created = state.items.keys() - before.items.keys()
                    if len(created) == 1:
                        identity, = created
                        task = state.items[identity]
                        runtime.aliases[alias] = {"id": identity, "version": task.version, "occurrence_id": task.occurrence_id}
                events = runtime.host.ui_events()
                replies = [{"status": event.payload.get("status"), "_text": event.payload.get("text", "")}
                           for event in events]
                for event in events:
                    runtime.host.ack_ui_event(event.sequence)
                report = {"id": step["id"], "at": _stamp(at), "ended_at": _stamp(runtime.now),
                          "turn_latency_ms": elapsed, "turns": runtime.engine.turns.copy(),
                          "decisions": decisions, "tool_receipts": runtime.host.receipts.copy(),
                          "simulated_deliveries": displays, "effects": [
                              {key: value for key, value in effect.items() if not key.startswith("_")} for effect in effects
                          ], "replies": [{"status": reply["status"]} for reply in replies],
                          "user_proposal_responses": proposal_responses,
                          "phases": [{"kind": phase.kind, "state": phase.state, "source": phase.source}
                                     for phase in state.phases.values()]}
                if "expect" in step:
                    report["score"] = _evaluate(step["expect"], effects, decisions, replies, runtime.engine.turns, runtime.resolve)
                if runtime.surface:
                    _record_interruption_step(runtime, step, before, state, report, prior_projection)
                reports.append(report)
            report = {"id": case["id"], "timezone": runtime.timezone, "started_at": _stamp(runtime.start),
                      "ended_at": _stamp(runtime.now), "steps": reports}
            if runtime.surface:
                deadlines = {(runtime.resolve(occurrence)
                              if occurrence[1:].split(".", 1)[0] in runtime.aliases else occurrence): runtime.resolve(at)
                             for occurrence, at in case["interruption"].get("deadlines", {}).items()
                             }
                report["interruption"] = score_interruption_case(report, deadlines)
            return report
        finally:
            runtime.close()


def _record_interruption_step(runtime, step, before, state, report, prior_projection):
    invalidated = [{"occurrence": task.occurrence_id, "reason": "expired"} for task in state.items.values()
                   if task.expired_at and (previous := before.items.get(task.task_id)) and not previous.expired_at]
    invalidated.extend({"occurrence": identity, "reason": "expired"} for identity, status in state.proposals.items()
                       if status == "expired" and before.proposals.get(identity) != "expired")
    report.update(prompt_responses=runtime.prompt_responses.copy(), invalidated_occurrences=invalidated,
                  synthetic_sound_requests=runtime.synthetic_sound_requests.copy(),
                  native_pending=runtime.native_projection.copy(), passive_reminders=sorted(runtime.passive_reminders))
    expected = step.get("expect", {})
    checks = report.get("score", {}).get("checks", {})
    if "action_status" in expected:
        checks["prompt_action_status"] = [response["status"] for response in runtime.prompt_responses] == expected["action_status"]
    if "menu_contains" in expected:
        checks["expected_menu_entries"] = all(runtime.resolve(item) in runtime.native_projection for item in expected["menu_contains"])
    if "menu_excludes" in expected:
        checks["hidden_menu_entries"] = all(runtime.resolve(item) not in runtime.native_projection for item in expected["menu_excludes"])
    if step.get("same_batch"):
        report["same_batch"] = True
        checks["coalesced_sound"] = len(runtime.synthetic_sound_requests) == 1
    if step.get("quiet"):
        previous_native, previous_passive = prior_projection
        report["quiet_check"] = (not any(display["status"] == "shown" for display in report["simulated_deliveries"])
                                 and not runtime.synthetic_sound_requests
                                 and set(runtime.native_projection) <= previous_native
                                 and runtime.passive_reminders <= previous_passive)
        checks["quiet_preserves_badge_and_surface"] = report["quiet_check"]


def _latency(values):
    values = sorted(values)
    return {key: round(values[ceil(len(values) * fraction) - 1], 3) if values else None
            for key, fraction in (("p50", 0.5), ("p95", 0.95))}


def summarize(cases, mode):
    steps = [step for case in cases for step in case["steps"]]
    scores = [step["score"] for step in steps if "score" in step]
    turns = [turn for step in steps for turn in step["turns"]]
    decisions = [decision for step in steps for decision in step["decisions"]]
    receipts = [receipt for step in steps for receipt in step["tool_receipts"]]
    effects = [effect for step in steps for effect in step["effects"]]
    displays = [display for step in steps for display in step["simulated_deliveries"]]
    shown = [display for display in displays if display["status"] == "shown"]
    hours = sum((datetime.fromisoformat(case["ended_at"]) - datetime.fromisoformat(case["started_at"])).total_seconds()
                for case in cases) / 3600
    # These counters distinguish model input and parse errors, runtime guard denials, and rendering outcomes.
    causes = Counter(receipt["error_code"] for receipt in receipts if receipt.get("error_code"))
    causes.update(decision["reason_code"] for decision in decisions
                  if decision["reason_code"] in FAILURES and "engine_failure" not in decision)
    causes.update(turn["source_error"] for turn in turns if "source_error" in turn)
    causes.update(display["status"] for display in displays if display["status"] != "shown")
    causes["user_rejected"] = sum(step["user_proposal_responses"].count("rejected") for step in steps)
    usage = [turn.get("recorded_usage", {}) for turn in turns]
    request_counts = [value.get("requests") for value in usage]
    costs = [value.get("cost_usd") for value in usage]
    observed_requests = sum(request_counts) if request_counts and None not in request_counts else None
    observed_cost = sum(costs) if costs and None not in costs else None
    visible = Counter(display["kind"] for display in shown)
    occurrences = Counter((case["id"], display["occurrence"]) for case in cases for step in case["steps"]
                          for display in step["simulated_deliveries"]
                          if display["kind"] == "reminder" and display["status"] == "shown")
    original_latencies = [value["latency_ms"] for value in usage if "latency_ms" in value]
    return {
        "scenarios": len(cases), "scored_steps": len(scores), "replayed_hours": round(hours, 6),
        "false_actions": sum(len(score["false_actions"]) for score in scores),
        "missed_actions": sum(len(score["missed_actions"]) for score in scores),
        "appropriate_interventions": sum(score["appropriate_interventions"] for score in scores),
        "unnecessary_interruptions": sum(score["unnecessary_interruptions"] for score in scores),
        "failed_checks": sum(not value for score in scores for value in score["checks"].values()),
        "admitted_turns": len(decisions), "wake_kinds": dict(Counter(
            kind for decision in decisions if decision["origin"] == "background" for kind in decision["trigger_kinds"])),
        "engine_turns": len(turns), "engine_turns_per_replayed_hour": round(len(turns) / hours, 3) if hours else None,
        "provider_requests_during_replay": 0, "recorded_provider_requests": observed_requests,
        "recorded_provider_requests_per_hour": round(observed_requests / hours, 3)
        if observed_requests is not None and hours else None,
        "tool_calls": len(receipts), "model_proposals": dict(Counter(
            tool["action"] for turn in turns for tool in turn["proposed"] if tool["action"] not in READ_TOOLS)),
        "actual_effects": dict(Counter(effect["action"] for effect in effects)),
        "visible_prompts": len(shown), "visible_prompts_by_kind": dict(visible),
        "visible_prompts_per_replayed_hour": round(len(shown) / hours, 3) if hours else None,
        "duplicate_reminder_displays": sum(count - 1 for count in occurrences.values()),
        "denial_causes": dict(causes), "replay_turn_latency_ms": _latency(
            value for step in steps for value in step["turn_latency_ms"]),
        "recorded_model_latency_ms": _latency(original_latencies),
        "replay_provider_cost_usd": 0, "recorded_provider_cost_usd": observed_cost,
        "recorded_latency_sample_count": len(original_latencies),
        "missing_or_mismatched_recorded_turns": sum(
            turn.get("source_error") in {"missing_recorded_model_turn", "recorded_model_trigger_mismatch"}
            for turn in turns),
        "engine_behavior_source": mode,
        "interruption": summarize_interruptions(cases),
    }


def run_replay(cases, *, recordings=None):
    mode = "scripted_fixture" if recordings is None else "offline_recorded_model"
    report = {"schema": 2, "engine_behavior_source": mode, "presentation_source": "simulated",
              "input_source": "synthetic_fixtures", "chat_policy": "semantic", "variants": {}}
    for variant, flag in BACKGROUND_POLICIES.items():
        if recordings is not None and variant not in recordings:
            continue
        with patch.dict(os.environ, {"MOCHI_SYSTEM2_SEMANTIC_BACKGROUND": flag}):
            runs = [run_case(case, None if recordings is None else recordings[variant].get(case["id"], {}))
                    for case in cases]
        report["variants"][variant] = {"metrics": summarize(runs, mode), "scenarios": runs}
    return report


def blind_review(cases, report, seed):
    """Randomize run labels independently for each case. Keep the key separate."""
    variants = report["variants"]
    if len(variants) != 2:
        raise ValueError("Blind review requires recordings for both background policies")
    if any(run["metrics"]["missing_or_mismatched_recorded_turns"] for run in variants.values()):
        raise ValueError("Blind review requires matching recorded turns for both background policies")
    rng = Random(seed)
    review, key = [], {}
    for index, case in enumerate(cases):
        order = list(variants)
        rng.shuffle(order)
        key[case["id"]] = dict(zip(("A", "B"), order))
        review.append({
            "id": case["id"], "description": case["description"], "setup": deepcopy(case.get("setup", [])),
            "steps": [{"id": step["id"], "events": deepcopy(step.get("events", []))} for step in case["steps"]],
            "outputs": {
                label: [{"id": step["id"], "at": step["at"], "effects": [
                    {k: v for k, v in effect.items() if k not in {"id", "occurrence", "subject", "os_id"}}
                    for effect in step["effects"]],
                    "reply_statuses": [reply["status"] for reply in step["replies"]],
                    "proposed": [proposal["action"] for turn in step["turns"] for proposal in turn["proposed"]],
                } for step in variants[variant]["scenarios"][index]["steps"]]
                for label, variant in key[case["id"]].items()
            },
        })
    return {"cases": review, "rate_each_arm": ["missed_actions", "false_actions", "appropriate_interventions",
                                                 "unnecessary_interruptions"]}, key


def load_recordings(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schema") not in (1, 2)
            or data.get("synthetic_inputs_only") is not True):
        raise ValueError("Expected schema 1 or 2 recordings captured from synthetic inputs only")
    variants = data["variants"]
    if not isinstance(variants, dict):
        raise ValueError("Expected recorded variants keyed by policy name")
    if data["schema"] == 1:
        # Its baseline was captured with the removed chat policy.
        if "semantic" not in variants:
            raise ValueError("Schema 1 recordings must include the semantic variant")
        variants = {"semantic_background": variants["semantic"]}
    elif "semantic_background" not in variants or variants.keys() - BACKGROUND_POLICIES.keys():
        raise ValueError("Schema 2 recordings require semantic_background and may include legacy_background")
    for cases in variants.values():
        for steps in cases.values():
            for turns in steps.values():
                for turn in turns:
                    if (turn.get("origin") not in {"user", "background"}
                            or not isinstance(turn.get("trigger_reasons"), list)
                            or not all(isinstance(reason, str) for reason in turn["trigger_reasons"])):
                        raise ValueError("Each recorded turn must identify its origin and trigger reasons")
                    if "before_calls" in turn:
                        raise ValueError("Recorded model turns cannot define local user events")
                    usage = turn.get("usage", {})
                    if any(type(value) not in (int, float) or not isfinite(value) or value < 0 for value in usage.values()):
                        raise ValueError("Recorded usage must contain finite nonnegative values")
    return variants
