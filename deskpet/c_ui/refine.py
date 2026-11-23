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

from PyQt6.QtCore import QLockFile, QPoint, QSettings, QStandardPaths, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

from deskpet.m0.composition import load_adapters
from deskpet.perception.dpi import set_dpi_awareness
from deskpet.timezones import resolve_timezone

from .desktop import DesktopWorkbench
from .desktop_power import DesktopPower
from .refine_chat import RefineChat
from .refine_conversation_store import ConversationStore
from .refine_delivery import Prompt, RefineDeliveryMixin
from .refine_panels import RefineReminders, RefineSettings, RefineWork, active_reminders, current_work
from .refine_proposals import ProposalCard
from .refine_window import RefinePet, RefineScroll
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

    def __init__(self, app, args, *, settings=None, conversation=None):
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
            from deskpet.settings import SettingsStore
            from .settings import SettingsPanel

            self.settings_store = SettingsStore(self.business_db, initial_input_mode="typing")
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
        )
        self.chat_store = chat_store
        self.chat.set_preference(self.input_mode)
        self.chat.toolbar_mode_changed.connect(self.pet.set_chat_toolbar)
        self.chat.typing_requested.connect(self.pet.activate_for_input)
        self.chat.storage_error.connect(self._service_error)
        self.panels = {
            "reminders": RefineScroll(self.reminders) if self.production else self.reminders,
            "work": RefineScroll(self.work) if self.production else self.work,
            "settings": self.settings_panel,
            "input": RefineScroll(self.chat) if self.production else self.chat,
        }

        if self.production:
            self.detail_panels = self.panels.copy()
            self.overviews = {
                "reminders": RefineReminders(timezone=user_timezone),
                "work": RefineWork(),
                "settings": RefineSettings(voice_available=False),
            }
            for key, label in (("reminders", "管理提醒"), ("work", "管理计时"), ("settings", "更多设置")):
                self.overviews[key].add_manage_action(label, lambda checked=False, name=key: self._panel_details(name, True))
                back = QPushButton("返回摘要")
                back.setObjectName("refine-back-to-summary")
                back.clicked.connect(lambda checked=False, name=key: self._panel_details(name, False))
                editor = {"work": self.work, "reminders": self.reminders, "settings": self.settings_panel}[key]
                if key == "settings":
                    editor.body.itemAt(0).layout().addWidget(back)
                else:
                    editor.layout().insertWidget(0, back)
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
                self._power = DesktopPower(app, self.pet, self.window.action.emit)
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

    def _service_ready(self):
        self._system2 = self.window.worker.system2
        if self._system2 is not None:
            self.presentation.ledger.authorize_early = self._system2.scheduler.authorize_qt
        if self.chat.recovered_event:
            self.window.action.emit({"kind": "recover_chat_result", "event_id": self.chat.recovered_event})

    def _queue_chat_message(self, message_id, text, *, proposal=None, supersedes_message_id=None):
        context = {"selected_ref": self._chat_selected_ref}
        if proposal:
            context.update(reply_to_proposal_id=proposal[0], reply_to_proposal_version=proposal[1])
        if supersedes_message_id:
            context["supersedes_message_id"] = supersedes_message_id
        self.chat_store.queue_turn(message_id, text, context)
        self.window.action.emit({"kind": "system2_message", "event_id": message_id})

    def _panel_details(self, key, show):
        self.panels[key] = self.detail_panels[key] if show else self.overviews[key]
        if self.section == key:
            self.pet.set_section(key, self.panels[key])

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
            self.reminders.receive_result(result)
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
            self.overviews["reminders"].render_snapshot(snapshot)
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

    def _render_system2(self, snapshot):
        can_show = self._can_show_reminder()
        self._display_capabilities = {
            "pet_card": can_show, "pet_badge": True,
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
        if self._system2 is None or self.section == "input" or not can_show:
            return
        indexed = {proposal["id"]: proposal for proposal in proposals}
        queued = {item.delivery_id for item in self.waiting_prompts if item.delivery_id}
        if self.prompt:
            queued.add(self.prompt.delivery_id)
        for delivery in snapshot.get("system2_proposal_presentations", ()):
            delivery_id = delivery["delivery_id"]
            proposal = indexed.get(delivery["proposal_id"])
            if (delivery_id in self._presented_proposals or delivery_id in queued or not proposal
                    or delivery["proposal_version"] != proposal["version"]):
                continue
            self.waiting_prompts.append(self._proposal_prompt(delivery, proposal))
        if self.prompt is None:
            self._show_next_prompt()
        self._render_pet_intents(snapshot)

    def _proposal_prompt(self, delivery, proposal):
        delivery_id = delivery["delivery_id"]
        deadline = datetime.fromisoformat(delivery["valid_until"])
        scope = self.settings_store.user_scope
        gateway = self._system2.gateway
        permitted = False
        generation = delivery["presentation_generation"]

        def current():
            return (self._can_show_reminder() and self.section != "input" and self.presentation.now() < deadline
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
                if permitted and not gateway.validate_presentation(delivery_id, scope, generation=generation):
                    gateway.ack_presentation(delivery_id, scope, displayed=False, generation=generation)
                    permitted = False
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
            return result

        def open_proposal(yes):
            if yes and current():
                self.proposal_card.select(proposal["id"])
                self._show_section("input")

        return Prompt("proposal", proposal["display_text"], open_proposal,
                      delivery_id=delivery_id, valid=current, before_paint=permit, on_painted=painted)

    def _render_pet_intents(self, snapshot):
        if self.prompt is not None or self.pet._press is not None:
            return
        for delivery_id in snapshot.get("system2_pet_intents", ()):
            intent = self._system2.pet.claim(delivery_id, self.presentation.now())
            if intent is None:
                continue
            self.pet.apply_motion_intent(intent["position_hint"], intent["animation"])
            sound = bool(intent["sound"] and self.presentation.native and self.presentation.lock_probe() is False)
            if sound:
                self.app.beep()
            self._system2.pet.receipt(delivery_id, sound_requested=sound)
            QTimer.singleShot(1000, self.pet.end_temporary_motion)
            break

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
    app.setOrganizationName("Mochi")
    app.setApplicationName("Mochi")
    app.setQuitOnLastWindowClosed(False)
    if data_dir is None:
        data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    data_dir = Path(data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return app, data_dir


def configure_system2(data_dir=None):
    """Open the local credential editor without starting screen observation or the pet."""
    from PyQt6.QtCore import Qt, pyqtSignal
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox
    from deskpet.observation_v2.routes import SYSTEM2
    from deskpet.settings import SettingsStore
    from .settings import _RouteEditor, verify_route_in_background

    class VerificationDialog(QMessageBox):
        probe_finished = pyqtSignal(str)

    app, data_dir = _desktop_profile(data_dir)
    store = SettingsStore(data_dir / "business.sqlite3")
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


def run_desktop(data_dir=None):
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
        session = RefineSession(app, SimpleNamespace(
            profile="realtime", scenario="desktop", output=None, data_dir=data_dir,
        ))
        return app.exec()
    finally:
        if session is not None:
            session.quit()
        lock.unlock()
