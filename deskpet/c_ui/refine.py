"""Native desktop UI backed by the same services as the C workbench."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import logging
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

from PyQt6.QtCore import QLockFile, QPoint, QSettings, Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

from deskpet.desktop_profile import DESKTOP_NAME, desktop_data_dir, desktop_settings_store
from deskpet.m0.composition import load_adapters
from deskpet.perception.dpi import set_dpi_awareness
from deskpet.perception.macos import screen_recording_access
from deskpet.system2.semantic_policy import semantic_background_enabled
from deskpet.timezones import resolve_timezone

from .desktop import DesktopWorkbench
from .desktop_power import DesktopPower
from .native_notifications import SNOOZE_SECONDS, platform_notifications
from .refine_chat import RefineChat
from .refine_conversation_store import ConversationStore
from .refine_delivery import Prompt, RefineDeliveryMixin
from .refine_panels import RefineReminders, RefineSettings, RefineWork, active_reminders, current_work
from .refine_proposals import ProposalCard
from .refine_style import DetailPanel
from .refine_window import RefinePet
from .time_format import ZONE

DEMO_GOAL = "完成论文的方法部分"
REPORT_ID = "demo-report"
DEMO_REMINDERS = (
    (REPORT_ID, "交小组报告", "hard", 0, 17, 0, 0),
    ("demo-milk", "买牛奶", "soft", 0, 17, 0, 20),
    ("demo-meeting", "预约组会", "hard", 1, 10, 0, 0),
)
log = logging.getLogger(__name__)


class RefineSession(RefineDeliveryMixin):
    """Compose the native pet and its shared service host in one Qt process."""

    def __init__(self, app, args, *, settings=None, conversation=None, settings_store=None, default_chat_height=0):
        self.production = args.profile == "realtime"
        if not self.production and args.profile != "scenario":
            raise ValueError("Mochi 需要实时或演示配置")
        user_timezone = resolve_timezone(getattr(args, "user_timezone", None)).key if self.production else None
        self.app = app
        self.context = None
        self.snapshot = {}
        self.section = None
        self._pending_seed = {}
        self._pending_invite = None
        self._pending_habit = None
        self._pending_settings = None
        self._reminder_response_events = {}
        self._reminder_response_versions = {}
        self._native_responses = {}
        self._native_notifications = getattr(args, "native_notifications", None)
        self._invite_waiting = False
        self._closing = False
        self._last_service_error = None
        self._chat_selected_ref = None
        self._system2 = None
        self._display_capabilities = {"pet_card": False, "pet_badge": True, "os_notification": False}
        self._presented_proposals = set()
        self._synthetic_delivery = getattr(args, "synthetic_delivery", False)
        self.data_dir = Path(args.data_dir).expanduser().resolve() if self.production else None
        if self.data_dir is not None:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.business_db = self.data_dir / "business.sqlite3" if self.production else None
        if settings is None:
            settings = (QSettings(str(self.data_dir / "window.ini"), QSettings.Format.IniFormat)
                        if self.production else QSettings("Mochi", "Desktop Pet"))
        self.settings = settings
        self.observer = None
        self._observer_timer = None
        self._power = None
        self.input_mode = "typing" if self.production else self.settings.value("defaultInput", "voice", type=str)
        if self.input_mode not in {"voice", "typing"}:
            self.input_mode = "voice"

        home = None
        if self.production and self.settings.contains("homeAnchor"):
            home = self.settings.value("homeAnchor", type=QPoint)
        self.pet = RefinePet(
            diagnostics=not self.production, voice_available=not self.production,
            home=home,
        )
        if self.production:
            self.pet.home_moved.connect(lambda position: self.settings.setValue("homeAnchor", position))

        if self.production:
            from .reminder import ReminderPanel
            from .work_panel import PhasePanel

            self.reminders = ReminderPanel(submit=self._action, advanced=True, timezone=user_timezone)
            self.work = PhasePanel(submit=self._action, timezone=user_timezone)
        else:
            self.reminders = RefineReminders()
            self.work = RefineWork()
        self._init_reminder_delivery()
        if self.production:
            self.presentation.notification_response.connect(
                self._receive_native_reminder_response, Qt.ConnectionType.QueuedConnection,
            )
            if getattr(self.presentation.notifications, "can_respond", False):
                self.presentation.notifications.set_response_handler(self.presentation.notification_response.emit)
            from deskpet.settings import SettingsStore
            from .settings import SettingsPanel

            self.settings_store = settings_store if settings_store is not None else SettingsStore(self.business_db)
            self._settings_changed(self.settings_store.read())
            self.settings_panel = SettingsPanel(
                self.settings_store, submit=self._settings_submit,
                save_preferences=self._save_preferences,
                save_habit=self._save_habit,
                sound_preview=self.presentation.preview_sound,
                notification_status=self.presentation.notification_status,
                refresh_notification_permission=self.presentation.refresh_notification_permission,
            )
            self.settings_panel.settings_changed.connect(self._settings_changed)
        else:
            self.settings_panel = RefineSettings()
            self.settings_panel.set_input_mode(self.input_mode)
            self.settings_panel.input_mode_changed.connect(self.set_input_mode)

        chat_store = ConversationStore(self.business_db) if self.production else None
        self.proposal_card = ProposalCard(self._resolve_proposal, self._reply_to_proposal) if self.production else None
        self.chat = RefineChat(
            self.submit, lambda: self.snapshot, store=chat_store, conversation=conversation,
            system2=self._queue_chat_message if self.production else None,
            configure_system2=self._open_model_settings if self.production else None,
            proposal_card=self.proposal_card,
            clear_history=self._clear_chat_history if self.production else None,
            preferred_height=self.settings.value("chatHeight", default_chat_height, type=int),
        )
        self.chat_store = chat_store
        self.chat.set_preference(self.input_mode)
        self.chat.geometry_requested.connect(self.pet._layout_pet)
        self.chat.height_committed.connect(lambda height: self.settings.setValue("chatHeight", height))
        self.chat.toolbar_mode_changed.connect(self.pet.set_chat_toolbar)
        self.chat.typing_requested.connect(self.pet.activate_for_input)
        self.chat.storage_error.connect(self._service_error)
        self.panels = {
            "reminders": (DetailPanel("提醒事项", self.reminders, lambda: self._panel_details("reminders", False))
                          if self.production else self.reminders),
            "work": (DetailPanel("专注与回顾", self.work, lambda: self._panel_details("work", False))
                     if self.production else self.work),
            "settings": self.settings_panel,
            "input": self.chat,
        }

        if self.production:
            self.detail_panels = self.panels.copy()
            self.overviews = {
                "reminders": RefineReminders(timezone=user_timezone, respond=self._respond_to_reminder,
                                             customize=self._customize_reminder_snooze, busy=self._reminder_response_busy),
                "work": RefineWork(),
                "settings": RefineSettings(voice_available=False),
            }
            self.reminders.actions_changed.connect(self.overviews["reminders"].refresh_actions)
            for key, label in (("reminders", "管理提醒"), ("work", "管理计时"), ("settings", "更多设置")):
                self.overviews[key].add_manage_action(label, lambda checked=False, name=key: self._panel_details(name, True))
            back = QPushButton("返回摘要")
            back.setObjectName("refine-back-to-summary")
            back.clicked.connect(lambda: self._panel_details("settings", False))
            self.settings_panel.body.itemAt(0).layout().addWidget(back)
            self.panels.update(self.overviews)

        options = copy.copy(args)
        if self.production:
            from deskpet.observation_v2.desktop import DesktopObserver

            self.observer = DesktopObserver(self.data_dir)
            options.business_database = self.business_db
            options.observation_database = self.observer.database
            options.enable_system2 = True
            options.desktop_lock_probe = lambda: self.presentation.lock_probe()
            options.user_timezone = user_timezone
            options.desktop_display_capabilities = lambda: self._display_capabilities
            if QGuiApplication.platformName() not in {"offscreen", "minimal"}:
                self.observer.start()
                self._observer_timer = QTimer(self.pet)
                self._observer_timer.timeout.connect(self.observer.refresh)
                self._observer_timer.start(2000)
        else:
            configured = load_adapters(getattr(args, "adapters", None))
            configured.pop("gui", None)
            configured.setdefault("ui_surface", "deskpet.c_ui.surface:create_surface")
            configured.setdefault("work_sessions", "deskpet.work_sessions.service:factory")
            options.adapters = configured
            options.realtime_scenario = getattr(args, "realtime_scenario", True)
            options.scenario_initial_time = getattr(args, "scenario_initial_time", None) or datetime.now(ZONE).replace(
                hour=16, minute=30, second=0, microsecond=0
            )
            self.demo_day = options.scenario_initial_time.astimezone(ZONE)

        options.auto_smoke = False
        try:
            self.window = DesktopWorkbench(options, external_surface=self)
        except BaseException:
            if self.observer is not None:
                self.observer.stop()
            raise
        self.window.setWindowTitle("Mochi · 协作联调台")
        self.window.service_error.connect(self._service_error)
        self.window.finished.connect(self._finished)
        if self.production:
            self.window.services_ready.connect(self._service_ready)
            try:
                self._power = DesktopPower(app, self.pet, self._desktop_power_event)
            except (AttributeError, ImportError, OSError) as error:
                log.warning("Native power events are unavailable (%s)", type(error).__name__)
        else:
            self.window.services_ready.connect(self.seed_tasks)
            self._install_demo_controls()

        self.pet.section_requested.connect(self.open_section)
        self.pet.input_held.connect(self._input_held)
        self.pet.collapsed.connect(lambda: self._show_section(None))
        self.pet.diagnostics_requested.connect(self.show_diagnostics)
        self.pet.quit_requested.connect(self.quit)
        self._render_toolbar()
        self.pet.show()
        if self.production and self.app.platformName() == "cocoa":
            from .macos_desktops import MacDesktopFollower
            from .refine_window import WIDTH, CAT_HEIGHT
            self._desktop_follower = MacDesktopFollower(self.pet, WIDTH, CAT_HEIGHT)

    def _service_ready(self):
        self._system2 = self.window.worker.system2
        if self._system2 is not None:
            self.presentation.ledger.authorize_early = self._system2.scheduler.authorize_qt
        if self.chat.recovered_event:
            self.window.action.emit({"kind": "recover_chat_result", "event_id": self.chat.recovered_event})
        self._drain_native_reminder_responses()

    def _desktop_power_event(self, action):
        self.presentation.power_event(action["kind"])
        self.window.action.emit(action)

    def _queue_chat_message(self, message_id, text, *, proposal=None, supersedes_message_id=None):
        context = {"selected_ref": self._chat_selected_ref}
        if proposal:
            context.update(reply_to_proposal_id=proposal[0], reply_to_proposal_version=proposal[1])
        if supersedes_message_id:
            context["supersedes_message_id"] = supersedes_message_id
        self.chat_store.queue_turn(message_id, text, context)
        self.window.action.emit({"kind": "system2_message", "event_id": message_id})

    def _clear_chat_history(self) -> bool:
        if self._system2 is None:
            raise RuntimeError("Desktop System 2 is still starting")
        return self._system2.clear_chat_history()

    def _panel_details(self, key, show):
        self.panels[key] = self.detail_panels[key] if show else self.overviews[key]
        if self.section == key:
            self.pet.set_section(key, self.panels[key])

    def _current_reminder_occurrence(self, task_id, occurrence):
        task = next((task for task in active_reminders(self.snapshot) if task["task_id"] == task_id
                     and task.get("occurrence", 1) == occurrence), None)
        expires_at = task["draft"].get("expires_at") if task else None
        if expires_at and self.presentation.now() >= datetime.fromisoformat(expires_at):
            return None
        return task

    def _respond_to_reminder(self, kind, task_id, occurrence, *, seconds=None):
        overview = self.overviews["reminders"]
        task = self._current_reminder_occurrence(task_id, occurrence)
        if task is None:
            overview.set_feedback("这次提醒已更新，请查看当前安排。")
            return None
        if self._reminder_response_busy(task_id):
            return None
        payload = {"seconds": seconds, "mode": "exact"} if kind == "snooze" else None
        event = self.reminders.request(kind, task_id, task["version"], payload)
        overview.set_feedback(self.reminders.result.text())
        if event:
            self._reminder_response_events[event] = task_id
        return event

    def _reminder_response_busy(self, task_id):
        return task_id in self._reminder_response_versions or self.reminders.busy(task_id)

    def _customize_reminder_snooze(self, task_id, occurrence):
        if self._current_reminder_occurrence(task_id, occurrence) is None:
            self.overviews["reminders"].set_feedback("这次提醒已更新，请查看当前安排。")
            return
        self._show_reminder_details(task_id, self.reminders.snooze_controls)

    def _show_reminder_details(self, task_id, target):
        self._show_section("reminders")
        self.reminders.open_delivery_group(None)
        self.reminders.select_task(task_id)
        self.detail_panels["reminders"].reveal_on_show(target)
        self._panel_details("reminders", True)

    def _receive_native_reminder_response(self, identifier, action):
        if self._closing:
            return
        self._native_responses[identifier, action] = None
        self._drain_native_reminder_responses()

    def _drain_native_reminder_responses(self):
        if (not self._native_responses or self._closing or not self.snapshot.get("now")
                or not self.window.ready_for_actions):
            return
        if not self.presentation.unlocked():
            self._native_responses.clear()
            return
        for identifier, action in tuple(self._native_responses):
            try:
                task = self.presentation.ledger.native_response_target(
                    identifier, action, self.presentation.now(), record_exposure=True,
                )
            except sqlite3.OperationalError:
                # A snapshot after the active writer commits can retry the response.
                return
            if task is not None:
                shown = self._current_reminder_occurrence(task.task_id, task.occurrence)
                if (shown is None or shown["version"] != task.version
                        or self._reminder_response_busy(task.task_id)):
                    continue
            self._native_responses.pop((identifier, action))
            if task is None:
                continue
            if action == "open":
                self._show_reminder_details(task.task_id, self.reminders.detail_title)
            elif action == "snooze" and (task.draft.expires_at is not None
                    and self.presentation.now() + timedelta(seconds=SNOOZE_SECONDS) >= task.draft.expires_at):
                self._customize_reminder_snooze(task.task_id, task.occurrence)
            else:
                self._respond_to_reminder(action, task.task_id, task.occurrence, seconds=SNOOZE_SECONDS)
                continue
            if self.pet.isHidden() or self.pet.isMinimized():
                self.pet.showNormal()
            self.pet.activate_for_input()

    def _open_model_settings(self):
        self._show_section("settings")
        self._panel_details("settings", True)
        self.settings_panel.show_system2_route()

    def _resolve_proposal(self, proposal, answer, amendments):
        if not self.window.ready_for_actions or self._system2 is None:
            return None
        event_id = uuid4().hex
        self.window.action.emit({
            "kind": "system2_proposal", "event_id": event_id, "proposal_id": proposal["id"],
            "expected_version": proposal["version"], "origin": "user", "answer": answer,
            "amendments": amendments,
        })
        return event_id

    def _reply_to_proposal(self, proposal):
        self.chat.reply_to_proposal(proposal)

    def _settings_changed(self, settings):
        self.pet.set_reduce_motion(settings.reduce_motion)
        self.pet.set_proactive_movement(settings.allow_proactive_movement)

    def _save_preferences(self, expected_version, changes):
        if not self.window.ready_for_actions:
            self.settings_panel.finish_preference_save(False)
            return
        self._pending_settings = uuid4().hex
        self.window.action.emit({
            "kind": "settings_update", "event_id": self._pending_settings,
            "expected_version": expected_version, "changes": changes,
        })

    def _settings_submit(self, action):
        if not self.window.ready_for_actions or self._pending_habit:
            return
        self._pending_habit = uuid4().hex
        self.window.action.emit({**action, "event_id": self._pending_habit})

    def _save_habit(self, function, args):
        if not self.window.ready_for_actions:
            return None
        event_id = uuid4().hex
        self.window.action.emit({"kind": "habit_save", "function": function, "args": args, "event_id": event_id})
        return event_id

    def _install_demo_controls(self):
        demo = self.window.menuBar().addMenu("小猫演示")
        demo.addAction("显示一条主动消息", lambda: self.show_message("Mochi 在这里，有需要就叫我。"))
        demo.addAction("显示是／否气泡", lambda: self.show_message(
            "要查看今天的提醒事项吗？", lambda yes: self._show_section("reminders") if yes else None
        ))
        demo.addAction("触发下一条到期提醒", self.trigger_next_reminder)
        demo.addAction("邀请开始自动番茄钟", self.request_demo_invite)

    def show_diagnostics(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        if QGuiApplication.platformName() == "cocoa":
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def attach_context(self, context):
        self.context = context
        context.result_received.connect(self._receive_result)

    def submit(self, action):
        return self.context.submit_action(action)

    def _action(self, action_id, *, task_id=None, version=None, payload=None):
        return self.submit({
            "event_id": uuid4().hex,
            "action_id": action_id,
            "task_id": task_id,
            "expected_task_version": version,
            "surface_revision": version or 1,
            "payload": payload or {},
        })

    def seed_tasks(self):
        for task_id, content, mode, day, hour, minute, window in DEMO_REMINDERS:
            when = self.demo_day.replace(hour=hour, minute=minute) + timedelta(days=day)
            event = self._action("reminder.create", task_id=task_id, payload={
                "content": content, "mode": mode, "earliest_at": when.isoformat(), "window_s": window * 60,
            })
            self._pending_seed[event] = task_id

    def _receive_result(self, result):
        self.chat.receive_result(result)
        event = result.get("command_id") or result.get("event_id")
        if self.production:
            self.proposal_card.complete(event)
            self.settings_panel.receive_habit_result(result)
            reminder_id = self._reminder_response_events.pop(event, None)
            version = result.get("actual_version")
            if reminder_id is not None and version and result.get("status") == "applied":
                self._reminder_response_versions[reminder_id] = version
            self.reminders.receive_result(result)
            if reminder_id is not None:
                self.overviews["reminders"].set_feedback(self.reminders.result.text())
            self.work.receive_result(result)
            if event and event == self._pending_settings:
                self._pending_settings = None
                self.settings_panel.finish_preference_save(result.get("status") == "applied")
            if event and event == self._pending_habit:
                self._pending_habit = None
                if result.get("status") == "applied":
                    self._show_section("work")
                else:
                    self.show_message("未能开始这项习惯。请查看当前专注状态。")
            return
        task = self._pending_seed.pop(event, None)
        if task and result.get("status") == "applied":
            self._action("reminder.confirm", task_id=task, version=result["actual_version"])
        if event and event == self._pending_invite:
            self._pending_invite = None
            if result.get("status") == "applied":
                self._show_section("work")
            else:
                self.show_message("这轮专注未能开始，请从联调台查看结果。")

    def _service_error(self, message):
        if not self._closing and message != self._last_service_error:
            self._last_service_error = message
            self.show_message(message)

    def render_snapshot(self, snapshot):
        self.snapshot = snapshot
        notes = self._render_reminder_delivery(snapshot)
        if self.production:
            self._render_reminder_overview(snapshot)
            self.overviews["work"].render_snapshot(snapshot)
            self.overviews["settings"].policy_badge.setText("开始前询问" if snapshot["settings"]["auto_adjust"] else "已关闭")
            self.reminders.render_snapshot({**snapshot, "presentation_pending": notes})
        else:
            self.reminders.render_snapshot(snapshot)
            if not self._can_start_focus():
                self._invite_waiting = False
        self.work.render_snapshot(snapshot)
        if self.production:
            settings = snapshot["settings"]
            self.settings_panel.sync_settings_version(settings["version"])
            self.settings_panel.sync_habits(snapshot.get("system2_habits_version", ()))
            self.pet.set_reduce_motion(settings["reduce_motion"])
            self.pet.set_proactive_movement(settings["allow_proactive_movement"])
            self._render_system2(snapshot)
        self._render_toolbar()
        if self.production:
            self._drain_native_reminder_responses()

    def _render_reminder_overview(self, snapshot):
        now = self.presentation.now(snapshot)
        for task in snapshot.get("tasks", ()):
            task_id = task["task_id"]
            version = self._reminder_response_versions.get(task_id)
            if version is not None and task["version"] >= version:
                self._reminder_response_versions.pop(task_id)
        pending = snapshot.get("pending_deliveries", {})
        presented = {(row["task_id"], row["occurrence"]) for row in self.presentation.rows
                     if row["state"] in {"painted", "badge_shown", "grouped", "passive"}}
        presented.update((item.task_id, item.occurrence) for item in self.presentation.native_followups())
        presented.update((item["task_id"], item["occurrence"]) for item in snapshot.get("system2_passive_reminders", ()))
        actionable = set()
        for task in active_reminders(snapshot):
            draft = task["draft"]
            if draft.get("expires_at") and now >= datetime.fromisoformat(draft["expires_at"]):
                continue
            subject = (task["task_id"], task.get("occurrence", 1))
            deadline = draft.get("remind_by") or draft.get("latest_first_delivery_at")
            if (task.get("displayed_at") or subject in presented or pending.get(subject[0]) == task["version"]
                    or (deadline and now >= datetime.fromisoformat(deadline))):
                actionable.add(subject)
        self.overviews["reminders"].render_snapshot(snapshot, actionable=actionable)

    def _render_system2(self, snapshot):
        semantic = semantic_background_enabled()
        can_show = self._can_show_reminder()
        can_show_card = self._can_show_reminder(collapsed_ok=semantic)
        self._display_capabilities = {
            "pet_card": can_show_card, "pet_badge": can_show if semantic else True,
            "os_notification": (self.presentation.notifications.available
                                and snapshot["settings"]["allow_system_notifications"]),
        }
        try:
            if "system2_chat_version" in snapshot:
                self.chat.refresh_projection(snapshot["system2_chat_version"])
        except sqlite3.Error:
            self._service_error("暂时无法读取聊天记录。")
        self.chat.refresh_route(snapshot.get("system2_route_status"))
        proposals = snapshot.get("system2_proposals", ())
        self.pet.set_proposal_count(len(proposals))
        if self.proposal_card.refresh(proposals):
            self.chat.refresh_proposals()
        if self._system2 is None or not self._can_show_reminder(collapsed_ok=True):
            return
        indexed = {proposal["id"]: proposal for proposal in proposals}
        badge_effects = {}
        queued = {item.delivery_id for item in self.waiting_prompts if item.delivery_id}
        if self.prompt:
            queued.add(self.prompt.delivery_id)
        for delivery in snapshot.get("system2_proposal_presentations", ()):
            delivery_id = delivery["delivery_id"]
            proposal = indexed.get(delivery["proposal_id"])
            if (delivery_id in self._presented_proposals or delivery_id in queued or not proposal
                    or self.presentation.catchup_effect(delivery_id)
                    or delivery["proposal_version"] != proposal["version"]):
                continue
            prompt = self._proposal_prompt(delivery, proposal)
            if delivery.get("channel") == "pet_badge":
                badge_effects[delivery_id] = (prompt.before_paint, prompt.on_painted)
            elif prompt.direct_focus or (can_show_card and (semantic or self.section != "input")):
                self.waiting_prompts.append(prompt)
        self.pet._proposal_badge.set_deliveries(badge_effects)
        if self.prompt is None:
            self._show_next_prompt()
        if can_show_card and (semantic or self.section != "input"):
            self._render_pet_intents(snapshot)

    def _proposal_prompt(self, delivery, proposal):
        delivery_id = delivery["delivery_id"]
        deadline = datetime.fromisoformat(delivery["valid_until"])
        scope = self.settings_store.user_scope
        gateway = self._system2.gateway
        permitted = False
        generation = delivery["presentation_generation"]
        semantic = delivery.get("route_binding") is not None
        badge = delivery.get("channel") == "pet_badge"

        direct_focus = (proposal["function"] in {"phase.adjust", "phase.pause", "phase.resume"}
                        or (proposal["function"] == "phase.start" and
                            (semantic or proposal["args"].get("kind", "focus") == "focus")))

        def current():
            visible_until = (datetime.fromisoformat(proposal["valid_until"])
                             if delivery_id in self._presented_proposals else deadline)
            return (self._can_show_reminder(collapsed_ok=(semantic or direct_focus) and not badge)
                    and (semantic or direct_focus or self.section != "input")
                    and self.presentation.now() < visible_until
                    and any((item["id"], item["version"]) == (proposal["id"], proposal["version"])
                            for item in self.snapshot.get("system2_proposals", ())))

        def permit():
            nonlocal permitted
            if not current():
                return False
            try:
                if permitted:
                    return gateway.validate_presentation(delivery_id, scope, generation=generation)
                permitted = bool(gateway.permit_presentation(delivery_id, scope, generation=generation))
                return permitted
            except sqlite3.OperationalError:
                return False

        def painted():
            try:
                result = gateway.ack_presentation(delivery_id, scope, displayed=True, generation=generation)
            except sqlite3.OperationalError:
                return False
            if result:
                self._presented_proposals.add(delivery_id)
                if delivery.get("request_sound"):
                    self.presentation.request_attention_sound("reminder", delivery_id, contact_kind="proposal")
            return result

        def dismissed():
            try:
                gateway.dismiss_presentation(delivery_id, scope, generation=generation)
            except sqlite3.OperationalError:
                pass

        def open_proposal(yes):
            if not current():
                return
            if direct_focus:
                self._resolve_proposal(proposal, "accept" if yes else "reject", None)
            elif yes:
                self.proposal_card.select(proposal["id"])
                self._show_section("input")

        return Prompt("proposal", proposal["display_text"], open_proposal,
                      delivery_id=delivery_id, valid=current, before_paint=permit, on_painted=painted,
                      direct_focus=direct_focus, on_dismiss=dismissed)

    def _render_pet_intents(self, snapshot):
        if self.prompt is not None or self.pet._press is not None or self._pet_motion_delivery is not None:
            return
        for delivery in snapshot.get("system2_pet_intents", ()):
            delivery_id = delivery["delivery_id"] if isinstance(delivery, dict) else delivery
            if self.presentation.catchup_effect(delivery_id):
                continue
            if isinstance(delivery, dict) and delivery.get("text"):
                self._queue_pet_bubble(delivery)
                break
            intent = self._system2.pet.claim(delivery_id, self.presentation.now())
            if intent is None:
                continue
            motion, sound = self._apply_pet_effect(intent, delivery_id)
            if isinstance(delivery, dict) and (motion or sound):
                self._report_pet_motion(delivery_id, motion, sound)
            else:
                self._system2.pet.receipt(delivery_id, sound_requested=sound, now=self.presentation.now())
                QTimer.singleShot(1000, self.pet.end_temporary_motion)
            break

    def _apply_pet_effect(self, intent, delivery_id):
        motion = self.pet.apply_motion_intent(intent["position_hint"], intent["animation"])
        sound = bool(intent["sound"] and self.presentation.request_attention_sound("reminder", delivery_id,
                                                                                   contact_kind="pet"))
        return motion, sound

    def _report_pet_motion(self, delivery_id, motion, sound):
        self._pet_motion_delivery = delivery_id

        def receipt(displayed):
            return self._system2.pet.receipt(delivery_id, displayed=displayed, sound_requested=sound,
                                             now=self.presentation.now())

        if motion:
            self.pet._report_motion_paint = lambda: receipt(
                self._can_show_reminder(collapsed_ok=True) and self.pet._widget_exposed(self.pet),
            )

        def finish():
            if self._pet_motion_delivery != delivery_id:
                return
            self.pet._report_motion_paint = None
            receipt(False)
            self._pet_motion_delivery = None
            self.pet.end_temporary_motion()
            self._show_next_prompt()

        QTimer.singleShot(1000, finish)

    def _queue_pet_bubble(self, delivery):
        delivery_id = delivery["delivery_id"]
        deadline = datetime.fromisoformat(delivery["expires_at"])
        claimed = None
        sound = None

        def current():
            return self._can_show_reminder(collapsed_ok=True) and self.presentation.now() < deadline

        def permit():
            nonlocal claimed
            if not current():
                return False
            claimed = self._system2.pet.claim(delivery_id, self.presentation.now(), revalidate=claimed is not None)
            return claimed is not None

        def painted():
            nonlocal sound
            if claimed is None:
                return False
            if sound is None:
                _, sound = self._apply_pet_effect(claimed, delivery_id)
            return self._system2.pet.receipt(delivery_id, displayed=True, sound_requested=sound,
                                             now=self.presentation.now())

        self.waiting_prompts.append(Prompt("pet", delivery["text"], delivery_id=delivery_id, valid=current,
                                           before_paint=permit, on_painted=painted,
                                           on_dismiss=lambda: self._system2.pet.dismiss(delivery_id,
                                                                                        self.presentation.now())))
        self._show_next_prompt()

    def _render_toolbar(self):
        work = current_work(self.snapshot)
        state = work["state"] if work else None
        if (phase := self.snapshot.get("active_phase")) and phase["kind"] != "focus":
            state = "paused" if phase["state"] == "paused" else "resting"
        self.pet.set_status(
            len(active_reminders(self.snapshot)), state, self.input_mode
        )

    def set_input_mode(self, mode):
        self.input_mode = mode
        self.settings.setValue("defaultInput", mode)
        self.settings_panel.set_input_mode(mode)
        self.chat.set_preference(mode)
        self._render_toolbar()

    def open_section(self, key):
        self._show_section(None if self.section == key else key)

    def _show_section(self, key, *, alternate=False):
        if self.section == key:
            return
        if self.section == "input":
            self.chat.reset_input()
        previous = self.section
        self.section = key
        if key == "reminders":
            self.pet._badge.clear_attention()
        elif key == "input":
            self.pet._proposal_badge.clear_attention()
        if self.production and key in self.overviews:
            self.panels[key] = self.overviews[key]
        if key == "input":
            self._chat_selected_ref = None
            if (self.production and previous == "reminders" and self.panels[previous] is self.detail_panels[previous]
                    and (item := self.reminders.selected())):
                self._chat_selected_ref = {"kind": "item", "id": item["task_id"]}
            if self.production and previous == "work" and (phase := self.snapshot.get("active_phase")):
                self._chat_selected_ref = {"kind": "phase", "id": phase["id"]}
            if (self.production and previous == "settings" and self.panels[previous] is self.detail_panels[previous]
                    and (habit := self.settings_panel.selected_habit_id())):
                self._chat_selected_ref = {"kind": "habit", "id": habit}
            self.chat.open(self.input_mode, alternate=alternate)
        self.pet.set_section(key, self.panels.get(key))

    def _input_held(self):
        if self.section == "input":
            self.chat.toggle_input()
        else:
            self._show_section("input", alternate=True)

    def show_message(self, message, on_answer=None):
        self.waiting_prompts.append(Prompt("message", message, on_answer))
        self._show_next_prompt()

    def _can_start_focus(self):
        return not any(session["state"] in {"focusing", "paused", "break_due"}
                       for session in self.snapshot.get("sessions", []))

    def request_demo_invite(self):
        if self._invite_waiting or self._pending_invite or not self.snapshot:
            return
        if not self._can_start_focus():
            self.show_message("当前已有工作段。结束后可以再开始一轮专注。")
            return
        self._invite_waiting = True
        QTimer.singleShot(650, self._show_invite)

    def _show_invite(self):
        if self._closing or not self._can_start_focus():
            self._invite_waiting = False
            return
        self.waiting_prompts.append(Prompt(
            "pomodoro", f"要开始 25 分钟专注，继续{DEMO_GOAL}吗？", self._answer_invite
        ))
        self._show_next_prompt()

    def _answer_invite(self, yes):
        self._invite_waiting = False
        if not yes or not self._can_start_focus() or self._pending_invite:
            return
        task_id = next((task["task_id"] for task in active_reminders(self.snapshot)
                        if task["task_id"] == REPORT_ID), None)
        payload = {"session_id": uuid4().hex, "goal": DEMO_GOAL, "duration_s": 25 * 60, "only_if_idle": True}
        if task_id:
            payload["task_id"] = task_id
        self._pending_invite = self._action("session.start", payload=payload)

    def trigger_next_reminder(self):
        pending = [task for task in active_reminders(self.snapshot) if not task["displayed_at"]]
        if not pending or not self.snapshot:
            self.show_message("当前没有待触发的提醒。")
            return
        target = min(datetime.fromisoformat(task["draft"]["latest_first_delivery_at"]) for task in pending)
        now = datetime.fromisoformat(self.snapshot["now"])
        self.window.action.emit({"kind": "advance", "seconds": max(0.0, (target - now).total_seconds())})

    def quit(self):
        if self._closing:
            return
        self._closing = True
        self.window.exit_requested = True
        if not self.window.close():
            self.window.exit_requested = False
            self._closing = False

    def _finished(self):
        self.presentation.restore_fallbacks(finished=self._finish_desktop)

    def _finish_desktop(self):
        if self._power is not None:
            self._power.close()
        if self._observer_timer is not None:
            self._observer_timer.stop()
        if self.observer is not None:
            self.observer.stop()
        self.settings.sync()
        self.pet.close()
        self.app.quit()


def run_gui(args):
    set_dpi_awareness()
    app = QApplication.instance() or QApplication(["Mochi"])
    app.setQuitOnLastWindowClosed(False)
    session = RefineSession(app, args)
    try:
        return app.exec()
    finally:
        session.quit()


def _desktop_profile(data_dir):
    set_dpi_awareness()
    app = QApplication.instance() or QApplication(["Mochi"])
    app.setOrganizationName(DESKTOP_NAME)
    app.setApplicationName(DESKTOP_NAME)
    app.setQuitOnLastWindowClosed(False)
    data_dir = desktop_data_dir(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return app, data_dir


def configure_system2(data_dir=None):
    """Open the local credential editor without starting screen observation or the pet."""
    from PyQt6.QtCore import Qt, pyqtSignal
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox
    from deskpet.observation_v2.routes import SYSTEM2
    from .settings import _RouteEditor, verify_route_in_background

    class VerificationDialog(QMessageBox):
        probe_finished = pyqtSignal(str)

    app, data_dir = _desktop_profile(data_dir)
    store = desktop_settings_store(data_dir)
    editor = _RouteEditor(store.routes, SYSTEM2, None, preset_provider="bailian")
    editor.setWindowTitle("Mochi · System 2 模型连接")
    buttons = editor.findChild(QDialogButtonBox)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并验证")
    if editor.exec() != QDialog.DialogCode.Accepted or store.routes.snapshot(SYSTEM2).route is None:
        return 0

    outcome = VerificationDialog()
    outcome.setWindowTitle("Mochi · System 2 连接验证")
    outcome.setTextFormat(Qt.TextFormat.PlainText)
    outcome.setText("正在使用合成数据验证文本和工具调用…")
    outcome.setStandardButtons(QMessageBox.StandardButton.Close)
    outcome.button(QMessageBox.StandardButton.Close).setText("关闭")

    def finished(role):
        route = store.routes.snapshot(role)
        if route.verified and route.route:
            outcome.setText(f"连接已验证。System 2 将使用 {route.route.model}。")
        else:
            outcome.setText("连接已保存，验证未通过。请核对 API Key、模型权限和地域。")
        outcome.adjustSize()

    outcome.probe_finished.connect(finished)
    verify_route_in_background(store.routes, SYSTEM2, outcome.probe_finished)
    outcome.exec()
    return 0


def configure_models(data_dir=None):
    """Open the model connections dialog without starting the pet or Observation."""
    from .model_setup import ModelSetupDialog

    app, data_dir = _desktop_profile(data_dir)
    store = desktop_settings_store(data_dir)
    dialog = ModelSetupDialog(store.routes)
    dialog.exec()
    return 0


def run_desktop(data_dir=None, *, request_screen_recording=False):
    """Launch the daily desktop pet with a single writer for the local profile."""
    app, data_dir = _desktop_profile(data_dir)
    lock = QLockFile(str(data_dir / "desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock():
        message = "Mochi 已在使用此本地数据目录。"
        if QGuiApplication.platformName() in {"offscreen", "minimal"}:
            print(message, file=sys.stderr)
        else:
            QMessageBox.information(None, "Mochi", message)
        return 1
    session = None
    try:
        # Register the macOS delegate before constructing the service host. The
        # platform can launch Mochi to deliver a notification response.
        native_macos = app.platformName() == "cocoa"
        notifications = platform_notifications(native=True) if native_macos else None
        if request_screen_recording and native_macos:
            screen_recording_access(request=True)
        session = RefineSession(app, SimpleNamespace(
            profile="realtime", scenario="desktop", output=None, data_dir=data_dir, native_notifications=notifications,
        ), settings_store=desktop_settings_store(data_dir), default_chat_height=295 if native_macos else 0)
        return app.exec()
    finally:
        if session is not None:
            session.quit()
        lock.unlock()
