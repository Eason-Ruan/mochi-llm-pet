"""Native desktop UI backed by the same services as the C workbench."""
from __future__ import annotations

from collections import deque
import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from uuid import uuid4

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

from deskpet.contracts import DisplayCommand
from deskpet.m0.composition import load_adapters
from deskpet.perception.dpi import set_dpi_awareness

from .desktop import DesktopWorkbench
from .refine_chat import RefineChat
from .refine_panels import RefineReminders, RefineSettings, RefineWork, active_reminders, current_work
from .refine_window import RefinePet
from .time_format import ZONE

DEMO_GOAL = "完成论文的方法部分"
REPORT_ID = "demo-report"
DEMO_REMINDERS = (
    (REPORT_ID, "交小组报告", "hard", 0, 17, 0, 0),
    ("demo-milk", "买牛奶", "soft", 0, 17, 0, 20),
    ("demo-meeting", "预约组会", "hard", 1, 10, 0, 0),
)


@dataclass
class Prompt:
    kind: str
    text: str
    on_answer: Callable[[bool], None] | None = None
    command: DisplayCommand | None = None
    painted: bool = False


class RefineSession:
    """Compose the pet, demo controls, and diagnostic window in one Qt process."""

    def __init__(self, app, args, *, settings=None):
        if args.profile != "scenario":
            raise ValueError("Mochi 桌面演示需要 scenario 配置")
        self.app = app
        self.context = None
        self.snapshot = {}
        self.section = None
        self.prompt: Prompt | None = None
        self.waiting_prompts: deque[Prompt] = deque()
        self.seen_deliveries: dict[str, DisplayCommand] = {}
        self._pending_seed = {}
        self._pending_invite = None
        self._invite_waiting = False
        self._closing = False
        self.settings = QSettings("Mochi", "Desktop Pet") if settings is None else settings
        self.input_mode = self.settings.value("defaultInput", "voice", type=str)
        if self.input_mode not in {"voice", "typing"}:
            self.input_mode = "voice"

        self.pet = RefinePet()
        self.reminders = RefineReminders()
        self.work = RefineWork()
        self.settings_panel = RefineSettings()
        self.settings_panel.set_input_mode(self.input_mode)
        self.settings_panel.input_mode_changed.connect(self.set_input_mode)
        self.chat = RefineChat(self.submit, lambda: self.snapshot)
        self.chat.set_preference(self.input_mode)
        self.chat.toolbar_mode_changed.connect(self.pet.set_chat_toolbar)
        self.chat.typing_requested.connect(self.pet.activate_for_input)
        self.panels = {
            "reminders": self.reminders,
            "work": self.work,
            "settings": self.settings_panel,
            "input": self.chat,
        }

        options = copy.copy(args)
        configured = load_adapters(getattr(args, "adapters", None))
        configured.pop("gui", None)
        configured.setdefault("ui_surface", "deskpet.c_ui.surface:create_surface")
        configured.setdefault("work_sessions", "deskpet.work_sessions.service:factory")
        options.adapters = configured
        options.realtime_scenario = getattr(args, "realtime_scenario", True)
        options.scenario_initial_time = getattr(args, "scenario_initial_time", None) or datetime.now(ZONE).replace(
            hour=16, minute=30, second=0, microsecond=0
        )
        options.auto_smoke = False
        self.demo_day = options.scenario_initial_time.astimezone(ZONE)
        self.window = DesktopWorkbench(options, external_surface=self)
        self.window.setWindowTitle("Mochi · 协作联调台")
        self.window.worker.ready.connect(self.seed_tasks)
        self.window.worker.error.connect(self._service_error)
        self.window.finished.connect(self._finished)
        self._install_demo_controls()

        self.pet.section_requested.connect(self.open_section)
        self.pet.input_held.connect(self._input_held)
        self.pet.collapsed.connect(lambda: self._show_section(None))
        self.pet.diagnostics_requested.connect(self.show_diagnostics)
        self.pet.quit_requested.connect(self.quit)
        self._render_toolbar()
        self.pet.show()

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
        if not self._closing:
            self.show_message(message)

    def render_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.reminders.render_snapshot(snapshot)
        self.work.render_snapshot(snapshot)
        self._render_toolbar()
        tasks = {task["task_id"]: task for task in snapshot.get("tasks", [])}
        for delivery, command in list(self.seen_deliveries.items()):
            task = tasks.get(command.task_id, {})
            if task.get("status") != "registered" or task.get("version") != command.task_version:
                self.seen_deliveries.pop(delivery, None)
        self.waiting_prompts = deque(item for item in self.waiting_prompts if self._valid_prompt(item))
        if not any(item.kind == "pomodoro" for item in self.waiting_prompts) and not self._can_start_focus():
            self._invite_waiting = False
        if self.prompt and not self._valid_prompt(self.prompt):
            self._clear_prompt()
        self._show_next_prompt()

    def _render_toolbar(self):
        work = current_work(self.snapshot)
        self.pet.set_status(
            len(active_reminders(self.snapshot)), work["state"] if work else None, self.input_mode
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
        self.section = key
        if key == "input":
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

    def can_report_display(self):
        return bool(
            self.prompt and self.prompt.command and not self._closing and self.pet.isVisible()
            and QGuiApplication.platformName() not in {"offscreen", "minimal"}
        )

    def show_reminder(self, command):
        previous = self.seen_deliveries.get(command.delivery_id)
        if previous:
            if command.expires_at == previous.expires_at:
                return
            prompts = list(self.waiting_prompts)
            if self.prompt:
                prompts.append(self.prompt)
            for item in prompts:
                if item.command and item.command.delivery_id == command.delivery_id:
                    item.command = command
                    self.seen_deliveries[command.delivery_id] = command
                    return
        self.seen_deliveries[command.delivery_id] = command
        item = Prompt("reminder", f"现在该{command.content}了。要查看这条提醒吗？", command=command)
        # A due reminder can interrupt a demo message. An already-visible reminder keeps its place.
        if self.prompt and not self.prompt.command:
            self.waiting_prompts.appendleft(self.prompt)
            self._clear_prompt()
        first_other = next((i for i, queued in enumerate(self.waiting_prompts) if not queued.command),
                           len(self.waiting_prompts))
        self.waiting_prompts.insert(first_other, item)
        self._show_next_prompt()

    def dismiss_reminder(self, task_id):
        def keep(item):
            return not item.command or item.command.task_id != task_id

        self.waiting_prompts = deque(item for item in self.waiting_prompts if keep(item))
        if self.prompt and not keep(self.prompt):
            self._clear_prompt()
        self._show_next_prompt()

    def _valid_prompt(self, item):
        if not item.command:
            return item.kind != "pomodoro" or self._can_start_focus()
        command = item.command
        task = next((task for task in self.snapshot.get("tasks", [])
                     if task["task_id"] == command.task_id), None)
        return bool(task and task["version"] == command.task_version and task["status"] == "registered"
                    and (item.painted or datetime.fromisoformat(self.snapshot["now"]) < command.expires_at))

    def _clear_prompt(self):
        if self.prompt and self.prompt.kind == "pomodoro" and not self._can_start_focus():
            self._invite_waiting = False
        self.prompt = None
        self.pet.clear_bubble()

    def _show_next_prompt(self):
        if self.prompt or not self.waiting_prompts:
            return
        item = self.waiting_prompts[0]
        if item.command:
            now = self.snapshot.get("now")
            if not now or datetime.fromisoformat(now) < item.command.requested_at:
                return
        self.waiting_prompts.popleft()
        self.prompt = item

        def answer(yes):
            if self.prompt is item:
                self.answer_prompt(yes)

        interactive = bool(item.command or item.on_answer)
        self.pet.show_bubble(
            item.text,
            on_answer=answer if interactive else None,
            on_painted=(lambda: self._painted(item)) if item.command else None,
            on_dismiss=(lambda: answer(False)) if not interactive else None,
            animate=item.kind == "pomodoro",
        )

    def _painted(self, item):
        if self.prompt is item and self._valid_prompt(item) and not item.painted:
            item.painted = self.context.report_display(item.command.delivery_id)
        return item.painted

    def answer_prompt(self, yes):
        item = self.prompt
        if not item:
            return
        # The bubble's callback owns one decision even if the button is clicked again.
        self._clear_prompt()
        if yes and item.command:
            self._show_section("reminders")
        if item.on_answer:
            item.on_answer(yes)
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
