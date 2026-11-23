"""Reminder queues and per-item presentation receipts for the desktop pet."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont, QFontMetrics

from deskpet.contracts import DisplayCommand

from .presentation import DesktopPresentation


@dataclass
class Prompt:
    kind: str
    text: str
    on_answer: Callable[[bool], None] | None = None
    command: DisplayCommand | None = None
    painted: bool = False
    siblings: tuple[DisplayCommand, ...] = ()
    preview: tuple[DisplayCommand, ...] | None = None
    delivery_id: str | None = None
    valid: Callable[[], bool] | None = None
    before_paint: Callable[[], bool] | None = None
    on_painted: Callable[[], bool] | None = None

    def commands(self):
        return (self.command, *self.siblings) if self.command else ()

    def visible_commands(self):
        return self.commands() if self.preview is None else self.preview


class RefineDeliveryMixin:
    def _init_reminder_delivery(self):
        self.prompt = None
        self.waiting_prompts = deque()
        self.seen_deliveries = {}
        self._reminder_pending = {}
        self._quiet_grouped = set()
        self._shown_deadlines = set()
        self._reporting_group = False
        self._reminder_timer = QTimer(self.pet)
        self._reminder_timer.setSingleShot(True)
        self._reminder_timer.timeout.connect(self._show_next_prompt)
        path = getattr(self, "business_db", None)
        data_dir = getattr(self, "data_dir", None)
        synthetic = getattr(self, "_synthetic_delivery", False)
        self.pet._synthetic_receipts = synthetic
        self.presentation = DesktopPresentation(
            path=path, observation=data_dir / "observation.sqlite3" if path and data_dir else None,
            synthetic=synthetic,
        )
        if self.production:
            self.reminders.bind_group_paints(self._painted_group_row, lambda: self.pet._backing_store_paint)

    def _render_reminder_delivery(self, snapshot):
        notes = self.presentation.observe(snapshot)
        tasks = {view["task_id"]: view for view in snapshot.get("tasks", ())}
        for delivery_id, command in list(self.seen_deliveries.items()):
            task = tasks.get(command.task_id, {})
            if task.get("status") != "registered" or task.get("version") != command.task_version:
                self.seen_deliveries.pop(delivery_id, None)
                self._reminder_pending.pop(delivery_id, None)
                self._quiet_grouped.discard(delivery_id)
        if self.production:
            for command in self._reminder_pending.values():
                if self._valid_command(command):
                    notes.setdefault(command.task_id, "待展示")
        self.waiting_prompts = deque(item for item in self.waiting_prompts if self._valid_prompt(item))
        if self.prompt and not self._valid_prompt(self.prompt):
            self._retire_prompt()
        if not self._reminder_timer.isActive():
            self._show_next_prompt()
        return notes

    def can_report_display(self):
        if self._reporting_group:
            return not self._closing and self._can_show_reminder()
        bubble = self.pet.bubble
        return bool(self.prompt and self.prompt.command and not self._closing and bubble
                    and self.pet._bubble_exposed(bubble))

    def _can_show_reminder(self):
        if not self.production:
            return self.pet.isVisible()
        handle = self.pet.windowHandle()
        return (self.pet.isVisible() and self.pet._expanded and not self.pet.isMinimized()
                and bool(handle and handle.isExposed())
                and self.presentation.lock_probe() is False)

    def show_reminder(self, command):
        previous = self.seen_deliveries.get(command.delivery_id)
        if previous == command:
            return
        self.seen_deliveries[command.delivery_id] = command
        if self.prompt and self.prompt.painted and previous in self.prompt.commands():
            return
        self._reminder_pending[command.delivery_id] = command
        if self._reminder_timer.isActive() and not self.production:
            return
        # Collect exact deadlines that were dispatched in the same worker pump.
        self._reminder_timer.start(40 if self.production else 0)

    def dismiss_reminder(self, task_id):
        for delivery_id, command in list(self._reminder_pending.items()):
            if command.task_id == task_id:
                self._reminder_pending.pop(delivery_id, None)
        if self.prompt and any(command.task_id == task_id for command in self.prompt.commands()):
            self._retire_prompt(excluding=task_id)
        self._show_next_prompt()

    def _valid_command(self, command):
        task = next((view for view in self.snapshot.get("tasks", ()) if view["task_id"] == command.task_id), None)
        return bool(task and task["version"] == command.task_version and task["status"] == "registered"
                    and not task.get("acknowledged_at"))

    def _deadline(self, command):
        draft = self.window.views.get(command.task_id, {}).get("draft", {})
        return draft.get("remind_by") or draft.get("latest_first_delivery_at")

    def _valid_prompt(self, item):
        if not item.command:
            return item.valid() if item.valid is not None else (item.kind != "pomodoro" or self._can_start_focus())
        if not all(self._valid_command(command) for command in item.commands()):
            return False
        if item.painted:
            return True
        return all(self.presentation.now() < command.expires_at for command in item.commands())

    def _clear_prompt(self):
        if self.prompt and self.prompt.kind == "pomodoro" and not self._can_start_focus():
            self._invite_waiting = False
        self.prompt = None
        self.pet.clear_bubble()
        stop_motion = getattr(self.pet, "end_temporary_motion", None)
        if stop_motion:
            stop_motion()

    def _retire_prompt(self, *, excluding=None):
        item = self.prompt
        self._clear_prompt()
        if item is not None and not item.painted:
            for command in item.commands():
                if command.task_id != excluding and self._valid_command(command):
                    self._reminder_pending[command.delivery_id] = self.seen_deliveries.get(command.delivery_id, command)

    def _ready_reminders(self):
        if not self.snapshot.get("now"):
            return []
        now = self.presentation.now()
        visible = self._can_show_reminder()
        ready = []
        os_owned = {(row["task_id"], row["occurrence"]) for row in self.presentation.rows
                    if row["state"] in {"unknown", "submitting", "scheduled", "submitted", "delivered", "cancel_intent", "cancelling", "painted", "grouped"}}
        for delivery_id, command in list(self._reminder_pending.items()):
            if delivery_id in self._quiet_grouped or (command.task_id, command.occurrence) in os_owned:
                continue
            if not self._valid_command(command) or not command.requested_at <= now < command.expires_at:
                continue
            if self.production and command.reason in {"hard_due", "deadline_fallback"}:
                deadline = self._deadline(command)
                if deadline and (deadline in self._shown_deadlines or any(
                    row["deadline"] == deadline and row["state"] in {"painted", "grouped"}
                    for row in self.presentation.rows
                )):
                    self.presentation.remember_group([command])
                    self._quiet_grouped.add(delivery_id)
                    continue
            if not visible:
                self.presentation.route_hidden(command)
            elif self.presentation.soft_valid(command):
                ready.append(command)
        return ready

    def _group_preview(self, commands):
        font = QFont(self.pet.font())
        font.setPixelSize(14)
        font.setWeight(QFont.Weight.Bold)
        metrics = QFontMetrics(font)
        visible = tuple(command for command in commands if "\n" not in command.content
                        and metrics.horizontalAdvance("• " + command.content) <= 240)[:2]
        text = [f"{len(commands)} 条提醒同时到期"]
        text.extend("• " + command.content for command in visible)
        if len(visible) < len(commands):
            text.append(f"还有 {len(commands) - len(visible)} 条，打开列表查看")
        return "\n".join(text), visible

    def _show_next_prompt(self):
        if self._closing:
            return
        reminders = self._ready_reminders()
        if self.prompt:
            if self.prompt.command or not reminders:
                return
            if self.prompt.kind != "proposal":
                self.waiting_prompts.appendleft(self.prompt)
            self._clear_prompt()
        if reminders:
            command = reminders[0]
            siblings = ()
            if self.production and command.reason in {"hard_due", "deadline_fallback"}:
                deadline = self._deadline(command)
                siblings = tuple(item for item in reminders[1:] if deadline and self._deadline(item) == deadline
                                 and item.reason in {"hard_due", "deadline_fallback"})
            for item in (command, *siblings):
                self._reminder_pending.pop(item.delivery_id, None)
            preview = None
            if siblings:
                text, preview = self._group_preview((command, *siblings))
            else:
                text = (f"提醒：{command.content}" if self.production
                        else f"现在该{command.content}了。要查看这条提醒吗？")
            item = Prompt("reminder", text, command=command, siblings=siblings, preview=preview)
        elif self.waiting_prompts:
            item = self.waiting_prompts.popleft()
        else:
            return
        self.prompt = item

        def answer(yes):
            if self.prompt is item:
                self.answer_prompt(yes)

        interactive = bool(item.command or item.on_answer)
        before_paint = None
        painted = None
        if item.command:
            before_paint = (lambda: self._permit_prompt(item)) if self.production else None
            painted = lambda: self._painted(item)
        elif item.before_paint:
            def before_paint():
                if item.painted or item.before_paint():
                    return self._valid_prompt(item)
                QTimer.singleShot(0, lambda: self._clear_prompt() if self.prompt is item else None)
                return False

            def painted():
                item.painted = item.on_painted()
                return item.painted

        labels = None
        if item.kind == "proposal":
            labels = ("查看", "稍后")
        elif self.production and item.command and not item.siblings:
            labels = ("好的", "关闭")
        self.pet.show_bubble(
            item.text, on_answer=answer if interactive else None,
            on_painted=painted,
            on_dismiss=(lambda: answer(False)) if not interactive else None,
            animate=item.kind == "pomodoro",
            before_paint=before_paint,
            answer_labels=labels,
        )

    def _permit_prompt(self, item):
        if self.prompt is not item:
            return False
        if item.painted:
            return all(self._valid_command(command) for command in item.commands())
        visible = item.visible_commands()
        showable = self._can_show_reminder()
        permitted = [command for command in visible if showable and self._valid_command(command)
                     and self.presentation.permit(command)]
        hidden = [command for command in item.commands() if command not in visible and showable
                  and self._valid_command(command) and self.presentation.can_group(command)]
        valid = [*permitted, *hidden]
        if len(valid) == len(item.commands()):
            return True
        for command in permitted:
            self.presentation.release_unpainted(command)

        def retire():
            if self.prompt is item:
                if valid:
                    self._clear_prompt()
                    self._reminder_pending.update((command.delivery_id, command) for command in valid)
                else:
                    self._retire_prompt()
                self._reminder_timer.start(120)

        QTimer.singleShot(0, retire)
        return False

    def _painted(self, item):
        if self.prompt is not item or item.painted or not self.can_report_display():
            return item.painted
        commands = self.presentation.painted(item.visible_commands())
        if self.production:
            if item.preview is not None:
                hidden = [command for command in item.commands() if command not in item.preview]
                self.presentation.remember_group(hidden)
                self._quiet_grouped.update(command.delivery_id for command in hidden)
            for command in commands:
                self.context.report_display(command.delivery_id)
            motion = next((command for command in commands if command.position_hint or command.animation), None)
            if motion is not None:
                self.pet.apply_motion_intent(motion.position_hint, motion.animation)
            item.painted = bool(commands) or item.preview is not None
            if item.painted and item.command.reason in {"hard_due", "deadline_fallback"}:
                self._shown_deadlines.add(self._deadline(item.command))
        elif item.command:
            item.painted = self.context.report_display(item.command.delivery_id)
        return item.painted

    def _painted_group_row(self, task_id, version):
        if self.section != "reminders" or not self._can_show_reminder():
            return
        command = next((command for command in reversed(tuple(self.seen_deliveries.values()))
                        if (command.task_id, command.task_version) == (task_id, version)), None)
        if command is None:
            return
        grouped = command.delivery_id in self._quiet_grouped or any(
            row["task_id"] == task_id and row["occurrence"] == command.occurrence and row["state"] == "grouped"
            for row in self.presentation.rows
        )
        if not grouped or not self.presentation.permit(command):
            return
        if self.presentation.painted([command], allow_sound=False):
            self._reporting_group = True
            try:
                self.context.report_display(command.delivery_id)
            finally:
                self._reporting_group = False

    def answer_prompt(self, yes):
        item = self.prompt
        if item is None:
            return
        self._clear_prompt()
        if yes and item.command and self.production and not item.siblings:
            command = item.command
            self.reminders.request("ack", command.task_id, command.task_version)
        elif yes and item.command:
            self._show_section("reminders")
            panel = getattr(self, "reminder_panel", self.reminders)
            if item.siblings:
                panel.open_delivery_group({command.task_id for command in item.commands()})
                scroll = self.panels["reminders"]
                QTimer.singleShot(0, lambda: scroll.ensureWidgetVisible(panel.tasks) if self.section == "reminders" else None)
            tasks = getattr(panel, "tasks", None)
            if tasks is not None:
                from PyQt6.QtCore import Qt
                for index in range(tasks.count()):
                    row = tasks.item(index)
                    if row.data(Qt.ItemDataRole.UserRole) == item.command.task_id:
                        tasks.setCurrentItem(row)
                        break
        if item.on_answer:
            item.on_answer(yes)
        self._show_next_prompt()
