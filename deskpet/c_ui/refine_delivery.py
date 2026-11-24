"""Reminder queues and per-item presentation receipts for the desktop pet."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont, QFontMetrics

from deskpet.contracts import DisplayCommand
from deskpet.system2.semantic_policy import semantic_background_enabled

from .native_notifications import SNOOZE_SECONDS
from .presentation import DesktopPresentation


@dataclass
class Prompt:
    kind: str
    text: str
    on_answer: Callable[[bool], None] | None = None
    command: DisplayCommand | None = None
    painted: bool = False
    direct_focus: bool = False
    siblings: tuple[DisplayCommand, ...] = ()
    preview: tuple[DisplayCommand, ...] | None = None
    delivery_id: str | None = None
    valid: Callable[[], bool] | None = None
    before_paint: Callable[[], bool] | None = None
    on_painted: Callable[[], bool] | None = None
    answer_labels: tuple[str, str] | None = None
    on_dismiss: Callable[[], None] | None = None

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
        self._pet_motion_delivery = None
        self._foreground_group = None
        self._reminder_timer = QTimer(self.pet)
        self._reminder_timer.setSingleShot(True)
        self._reminder_timer.timeout.connect(self._show_next_prompt)
        path = getattr(self, "business_db", None)
        data_dir = getattr(self, "data_dir", None)
        synthetic = getattr(self, "_synthetic_delivery", False)
        self.pet._synthetic_receipts = synthetic
        self.presentation = DesktopPresentation(
            path=path, observation=data_dir / "observation.sqlite3" if path and data_dir else None,
            notifications=getattr(self, "_native_notifications", None), synthetic=synthetic,
        )
        self.presentation.bind_foreground(self._show_foreground_reminder)
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

    def _can_show_reminder(self, *, collapsed_ok=False):
        if not self.production:
            return self.pet.isVisible()
        handle = self.pet.windowHandle()
        return (self.pet.isVisible() and (self.pet._expanded or collapsed_ok) and not self.pet.isMinimized()
                and bool(handle and handle.isExposed())
                and self.presentation.can_present())

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
                    and task.get("occurrence", 1) == command.occurrence
                    and not task.get("acknowledged_at") and not task.get("expired_at"))

    def _deadline(self, command):
        draft = self.window.views.get(command.task_id, {}).get("draft", {})
        deadline = draft.get("remind_by") or draft.get("latest_first_delivery_at")
        return datetime.fromisoformat(deadline) if deadline else None

    def _card_showable(self, command):
        return self._can_show_reminder(collapsed_ok=(semantic_background_enabled()
                                       or command.reason in {"hard_due", "deadline_fallback"}))

    def _valid_prompt(self, item):
        if not item.command:
            if item.delivery_id and self.presentation.catchup_effect(item.delivery_id):
                return False
            return item.valid() if item.valid is not None else (item.kind != "pomodoro" or self._can_start_focus())
        if not all(self._valid_command(command) for command in item.commands()):
            return False
        if item.painted:
            return not self.production or self._card_showable(item.command)
        return all(self.presentation.now() < command.expires_at and not self.presentation.catchup_delivery(command)
                   for command in item.commands())

    def _clear_prompt(self):
        self._foreground_group = None
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
        semantic = self.production and semantic_background_enabled()
        visible = self._can_show_reminder(collapsed_ok=semantic)
        ready = []
        badges = {}
        owned = {(row["task_id"], row["occurrence"]): row for row in self.presentation.rows
                 if row["state"] in {"unknown", "submitting", "scheduled", "submitted", "delivered",
                                     "cancel_intent", "cancelling", "painted", "grouped", "badge_shown", "passive"}}
        for delivery_id, command in list(self._reminder_pending.items()):
            if delivery_id in self._quiet_grouped:
                continue
            soft_os = command.reason == "tool" and command.channel == "os_notification"
            if soft_os and now >= command.expires_at:
                self._reminder_pending.pop(delivery_id, None)
                continue
            if not self._valid_command(command) or not command.requested_at <= now < command.expires_at:
                continue
            if self.presentation.catchup_delivery(command):
                self._reminder_pending.pop(delivery_id, None)
                continue
            if soft_os and (self.prompt is not None or self._pet_motion_delivery is not None):
                continue
            if owner := owned.get((command.task_id, command.occurrence)):
                if (semantic and owner["state"] == "scheduled" and owner["channel"] == "os_notification"
                        and command.reason == "tool"):
                    if (command.channel == "os_notification"
                            or (command.channel == "pet_badge" and self._can_show_reminder())
                            or (command.channel == "pet_card" and visible and self.prompt is None
                                and self._pet_motion_delivery is None)):
                        self.presentation.transfer_early(command)
                continue
            if command.channel == "os_notification":
                self.presentation.route_hidden(command)
                continue
            if command.channel == "pet_badge":
                if self._can_show_reminder():
                    badges[delivery_id] = (
                        lambda command=command: self.presentation.permit_badge(command),
                        lambda command=command: self._painted_badge(command),
                    )
                continue
            if self.production and command.reason in {"hard_due", "deadline_fallback"}:
                deadline = self._deadline(command)
                if deadline and (deadline in self._shown_deadlines or self.presentation.deadline_has_primary(deadline)):
                    self.presentation.remember_group([command])
                    self._quiet_grouped.add(delivery_id)
                    continue
            if not self._card_showable(command):
                self.presentation.route_hidden(command)
            elif self.presentation.soft_valid(command):
                ready.append(command)
        if self.production:
            self.pet._badge.set_deliveries(badges)
        if self.production:
            ready.sort(key=lambda command: command.reason not in {"hard_due", "deadline_fallback"})
        return ready

    def _painted_badge(self, command):
        if not self._can_show_reminder() or not self.presentation.painted([command], allow_sound=False):
            return False
        self._reminder_pending.pop(command.delivery_id, None)
        self._reporting_group = True
        try:
            self.context.report_display(command.delivery_id)
        finally:
            self._reporting_group = False
        return True

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

    def _native_followup_prompt(self):
        if not self.production or not self._can_show_reminder(collapsed_ok=True):
            return None
        followup = next(iter(self.presentation.native_followups(include_painted=False, automatic=True)), None)
        if followup is None:
            return None

        def current():
            return self._can_show_reminder(collapsed_ok=True) and self._valid_command(followup)

        def painted():
            return current() and self.presentation.native_followup_current(followup, mark_painted=True)

        def can_snooze():
            return (followup.expires_at is None
                    or self.presentation.now() + timedelta(seconds=SNOOZE_SECONDS) < followup.expires_at)

        offer_snooze = can_snooze()

        def answer(yes):
            if not current() or not self.presentation.native_followup_current(followup, include_painted=True):
                return
            if yes:
                self._respond_to_reminder("ack", followup.task_id, followup.occurrence)
            elif offer_snooze and can_snooze():
                self._respond_to_reminder("snooze", followup.task_id, followup.occurrence, seconds=SNOOZE_SECONDS)
            else:
                self._customize_reminder_snooze(followup.task_id, followup.occurrence)

        title = "提醒" if followup.delivered else "待确认提醒"
        return Prompt("native_reminder", f"{title}：{followup.content}", on_answer=answer, valid=current,
                      delivery_id=followup.delivery_id,
                      before_paint=lambda: current() and self.presentation.native_followup_current(followup),
                      on_painted=painted, answer_labels=("确认收到", "5 分钟后" if offer_snooze else "查看详情"))

    def _show_foreground_reminder(self, identifier, decide):
        row, task = self.presentation.foreground_target(identifier)
        if row is not None and task is None:
            decide(True)
            return
        deadline = task.draft.hard_deadline if task else None
        if (self._closing or deadline is None or self.presentation.now() < deadline
                or self.presentation.is_catchup_deadline(deadline) or self._pet_motion_delivery is not None
                or not self._can_show_reminder(collapsed_ok=True)):
            decide(False)
            return

        def choose(candidate, decision, *, grouped=False):
            claimed = False

            def permit():
                nonlocal claimed
                result = self.presentation.claim_foreground_pet(candidate, grouped=grouped)
                claimed = result == "claimed"
                return bool(result)

            return decision(True, permit=permit) and claimed

        def choose_grouped(candidate, decision, sound_allowed):
            if choose(candidate, decision, grouped=True) and sound_allowed:
                self.presentation.request_attention_sound("reminder", candidate["delivery_id"], contact_kind="reminder",
                                                          batch_at=deadline)

        if self._foreground_group and self._foreground_group[0] == deadline:
            _, group = self._foreground_group
            if self.prompt.painted:
                choose_grouped(row, decide, task.draft.sound is True)
            else:
                group.append((row, decide, task.draft.sound is True))
            return

        if deadline in self._shown_deadlines or self.presentation.deadline_has_primary(deadline, pet_only=True):
            choose_grouped(row, decide, task.draft.sound is True)
            return

        self._retire_prompt()
        handed_over = False
        group = []
        self._foreground_group = deadline, group

        def current():
            return (self._can_show_reminder(collapsed_ok=True)
                    and self._current_reminder_occurrence(task.task_id, task.occurrence) is not None)

        def permit():
            nonlocal handed_over
            if not current() or not self.pet._bubble_exposed(self.pet.bubble):
                return False
            if not handed_over:
                handed_over = choose(row, decide)
            return handed_over

        def painted():
            if not self.presentation.paint_foreground_pet(row):
                return False
            if task.draft.sound is not False:
                self.presentation.request_attention_sound("reminder", row["delivery_id"], contact_kind="reminder",
                                                          batch_at=deadline)
            for member, decision, sound_allowed in group:
                choose_grouped(member, decision, sound_allowed)
            group.clear()
            return True

        item = Prompt("foreground_reminder", f"提醒：{task.draft.content}", valid=current, before_paint=permit,
                      delivery_id=row["delivery_id"],
                      on_painted=painted, on_answer=lambda yes: self._answer_reminder(yes, task.task_id,
                                                                                    task.occurrence),
                      answer_labels=self._reminder_answer_labels(task.draft.expires_at))
        self._show_prompt(item)

    def _reminder_answer_labels(self, expiry):
        return "确认收到", "5 分钟后" if self._can_snooze_reminder(expiry) else "查看详情"

    def _can_snooze_reminder(self, expiry):
        return expiry is None or self.presentation.now() + timedelta(seconds=SNOOZE_SECONDS) < expiry

    def _answer_reminder(self, yes, task_id, occurrence):
        if not self._can_show_reminder(collapsed_ok=True):
            return
        task = self._current_reminder_occurrence(task_id, occurrence)
        if task is None:
            return
        expiry = task["draft"].get("expires_at")
        if yes:
            self._respond_to_reminder("ack", task_id, occurrence)
        elif self._can_snooze_reminder(datetime.fromisoformat(expiry) if expiry else None):
            self._respond_to_reminder("snooze", task_id, occurrence, seconds=SNOOZE_SECONDS)
        else:
            self._customize_reminder_snooze(task_id, occurrence)

    def _show_next_prompt(self):
        if self._closing:
            return
        reminders = self._ready_reminders()
        semantic = self.production and semantic_background_enabled()
        first_due = reminders and reminders[0].reason in {"hard_due", "deadline_fallback"}
        if self._pet_motion_delivery is not None:
            return
        if self.prompt:
            if first_due and self.production:
                active = self.prompt.command
                timed = self.prompt.kind == "foreground_reminder" or (
                    active and active.reason in {"hard_due", "deadline_fallback"})
                if timed and not self.prompt.painted:
                    return
                self._retire_prompt()
            elif not semantic and not self.prompt.command and reminders and self.prompt.kind != "foreground_reminder":
                if self.prompt.kind not in {"proposal", "native_reminder"}:
                    self.waiting_prompts.appendleft(self.prompt)
                self._clear_prompt()
            else:
                return
        while self.waiting_prompts and not self._valid_prompt(self.waiting_prompts[0]):
            self.waiting_prompts.popleft()
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
        else:
            item = self._native_followup_prompt()
            if item is None:
                if not self.waiting_prompts:
                    return
                item = self.waiting_prompts.popleft()
        self._show_prompt(item)

    def _show_prompt(self, item):
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
                if self._valid_prompt(item) and (item.painted or item.before_paint()):
                    return True
                QTimer.singleShot(0, lambda: self._clear_prompt() if self.prompt is item else None)
                return False

            def painted():
                item.painted = item.on_painted()
                if item.painted and (semantic_background_enabled() or item.kind in {"native_reminder", "foreground_reminder"}):
                    self._expire_visible_prompt(item)
                return item.painted

        labels = item.answer_labels
        if item.kind == "proposal":
            labels = ("接受", "拒绝") if item.direct_focus else ("查看", "稍后")
        elif self.production and item.command and not item.siblings:
            task = self._current_reminder_occurrence(item.command.task_id, item.command.occurrence)
            expiry = task["draft"].get("expires_at") if task else None
            labels = self._reminder_answer_labels(datetime.fromisoformat(expiry) if expiry else None)
        dismissible = (not interactive or item.on_dismiss or (self.production and item.command)
                       or item.kind in {"native_reminder", "foreground_reminder"})
        self.pet.show_bubble(
            item.text, on_answer=answer if interactive else None,
            on_painted=painted,
            on_dismiss=(lambda: self._dismiss_popup(item)) if dismissible else None,
            animate=item.kind == "pomodoro",
            before_paint=before_paint,
            answer_labels=labels,
        )

    def _permit_prompt(self, item):
        if self.prompt is not item:
            return False
        if item.painted:
            return self._card_showable(item.command) and all(self._valid_command(command) for command in item.commands())
        visible = item.visible_commands()
        showable = self._card_showable(item.command)
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
            if item.painted and semantic_background_enabled():
                self._expire_visible_prompt(item)
            if item.painted and item.command.reason in {"hard_due", "deadline_fallback"}:
                self._shown_deadlines.add(self._deadline(item.command))
        elif item.command:
            item.painted = self.context.report_display(item.command.delivery_id)
        return item.painted

    def _expire_visible_prompt(self, item):
        def expire():
            if self.prompt is item:
                self._clear_prompt()
                self._show_next_prompt()
        QTimer.singleShot(15000, expire)

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
        if self.presentation.painted([command], allow_sound=False, passive=True):
            self._reporting_group = True
            try:
                self.context.report_display(command.delivery_id)
            finally:
                self._reporting_group = False

    def answer_prompt(self, yes):
        item = self.prompt
        if item is None:
            return
        if not yes and (item.siblings or (item.kind == "proposal" and not item.direct_focus)):
            self._dismiss_popup(item)
            return
        self._clear_prompt()
        if item.command and self.production and not item.siblings:
            command = item.command
            self._answer_reminder(yes, command.task_id, command.occurrence)
        elif yes and item.command:
            self._show_section("reminders")
            panel = getattr(self, "reminder_panel", self.reminders)
            if item.siblings:
                self._panel_details("reminders", True)
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

    def _dismiss_popup(self, item):
        if self.prompt is not item:
            return
        if item.painted:
            if item.on_dismiss:
                item.on_dismiss()
            if self.production and item.command:
                for command in item.visible_commands():
                    self.presentation.dismiss_contact(command.delivery_id)
            elif item.kind in {"native_reminder", "foreground_reminder"}:
                self.presentation.dismiss_contact(item.delivery_id)
        self._clear_prompt()
        self._show_next_prompt()
