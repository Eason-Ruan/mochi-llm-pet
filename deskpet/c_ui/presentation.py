"""Desktop channel selection, reconciliation, and local audio preferences."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

from deskpet.contracts import TaskView
from deskpet.m0.observation_input import DesktopObservationInput
from deskpet.system2.semantic_policy import semantic_background_enabled
from deskpet.task_service.presentation import EARLY_OS_TRANSFER, PresentationLedger, native_followup_due

from .native_notifications import notification_id, platform_notifications


@dataclass(frozen=True)
class NativeReminderFollowup:
    delivery_id: str
    task_id: str
    task_version: int
    occurrence: int
    content: str
    delivered: bool
    expires_at: datetime | None


def _session_locked():
    from deskpet.observation_v2.activity import _mac_session, _win_session

    probe = {"darwin": _mac_session, "win32": _win_session}.get(sys.platform)
    if probe is not None:
        try:
            return probe()[0]
        except (ImportError, OSError, RuntimeError):
            return None
    return None


class DesktopPresentation(QObject):
    changed = pyqtSignal()
    _callback = pyqtSignal(object)

    def __init__(self, *, path=None, observation=None, notifications=None, lock_probe=None, synthetic=False):
        super().__init__()
        self.native = bool(QGuiApplication.instance()) and QGuiApplication.platformName() not in {"offscreen", "minimal"} and not synthetic
        self.ledger = (PresentationLedger(Path(path).resolve(), source="native" if self.native else "offscreen")
                       if path is not None else None)
        self.observations = DesktopObservationInput(observation, settings_path=path) if observation is not None else None
        self.synthetic = synthetic
        self.notifications = notifications or platform_notifications(native=self.native and self.ledger is not None)
        self.lock_probe = lock_probe or (_session_locked if self.native else lambda: False if synthetic else None)
        self.settings = {}
        self.rows = []
        self.snapshot = {}
        self._active_phase = None
        self._last_inventory_at = None
        self._in_progress = False
        self._last_notification_status = None
        self._early_transfers = {}
        self._callback.connect(self._run_callback)

    def _run_callback(self, callback):
        callback()

    def now(self, snapshot=None):
        snapshot = self.snapshot if snapshot is None else snapshot
        return (datetime.now(timezone.utc) if snapshot.get("clock") == "wall"
                else datetime.fromisoformat(snapshot["now"]))

    def notification_status(self) -> str:
        status = self.notifications.status
        if self.settings.get("lock_screen_detail"):
            status += "。此通道使用通用提醒文案。"
        return status

    def refresh_notification_permission(self):
        self.notifications.refresh(request=bool(self.settings.get("allow_system_notifications")))
        return self.notification_status()

    def preview_sound(self, kind):
        if kind not in {"reminder", "phase_end"}:
            raise ValueError("unknown sound kind")
        if not self.native:
            return "当前会话没有本机音频出口。"
        QApplication.beep()
        return "已请求播放本机提示音。"

    def _sound_enabled(self, kind):
        setting = {"reminder": "reminder_sound", "phase_end": "phase_end_sound"}.get(kind)
        return bool(setting and self.settings.get(setting))

    def observe(self, snapshot):
        self.snapshot = snapshot
        old_settings = self.settings
        self.settings = snapshot.get("settings", {})
        if self.settings.get("allow_system_notifications") and not old_settings.get("allow_system_notifications"):
            self.refresh_notification_permission()
        self._phase_ended(snapshot)
        if self.ledger is None or not snapshot.get("now"):
            return {}
        now = self.now(snapshot)
        self.rows = self.ledger.rows()
        if self._last_inventory_at is None or abs((now - self._last_inventory_at).total_seconds()) >= 2:
            self._last_inventory_at = now
            self._reconcile(now)
            if self.settings.get("allow_system_notifications"):
                self.notifications.refresh()
        status = self.notification_status()
        if status != self._last_notification_status:
            self._last_notification_status = status
            self.changed.emit()
        self._schedule_fallbacks(now)
        self._early_transfers = {key: command for key, command in self._early_transfers.items()
                                 if now < command.expires_at}
        return self.pending_projection(now)

    def _schedule_fallbacks(self, now):
        if (not self.settings.get("allow_system_notifications")
                or not self.notifications.can_schedule or not self.notifications.available):
            return
        for task in self._active_tasks():
            deadline = task.draft.hard_deadline
            if deadline is None or deadline <= now + timedelta(seconds=2) or task.draft.expires_at is not None:
                continue
            delivery_id = self._delivery_id(task)
            if transfer := self._early_transfers.get(delivery_id):
                if self.ledger.can_finish_early_transfer(transfer, now):
                    continue
                self._early_transfers.pop(delivery_id, None)
            self._submit_os(task, deadline, sound=False)

    def restore_fallbacks(self):
        if self._early_transfers and self.ledger is not None and self.snapshot.get("now"):
            self._early_transfers.clear()
            self._schedule_fallbacks(self.now())

    def _phase_ended(self, snapshot):
        previous = self._active_phase
        self._active_phase = snapshot.get("active_phase")
        if not previous or previous.get("state") != "running" or not self._sound_enabled("phase_end"):
            return
        current_id = previous.get("phase_id") or previous.get("id")
        candidate = next((item for item in snapshot.get("phases", [])
                          if (item.get("phase_id") or item.get("id")) == current_id), None)
        if candidate and candidate.get("state") == "elapsed" and self.native and self.lock_probe() is False:
            QApplication.beep()

    def _active_tasks(self):
        return [TaskView.model_validate(item) for item in self.snapshot.get("tasks", [])
                if item["status"] == "registered" and not item.get("acknowledged_at")
                and not item.get("displayed_at") and not item.get("expired_at") and not item.get("archived_at")
                and item["draft"].get("reminder_enabled", True)]

    def native_followups(self, *, include_painted=True):
        if not self.snapshot.get("now") or not self.rows:
            return []
        now = self.now()
        by_id = {task.task_id: task for task in self._active_tasks()
                 if task.draft.expires_at is None or now < task.draft.expires_at}
        due_followups = {}
        for row in self.rows:
            task = by_id.get(row["task_id"])
            if task is None or (row["followup_painted"] and not include_painted):
                continue
            due = native_followup_due(task, row, now)
            if due is None:
                continue
            followup = NativeReminderFollowup(row["delivery_id"], task.task_id, task.version,
                                              task.occurrence, task.draft.content, row["state"] == "delivered",
                                              task.draft.expires_at)
            due_followups[task.task_id] = (due, followup)
        return [followup for _, followup in sorted(due_followups.values(), key=lambda item: (item[0], item[1].task_id))]

    def native_followup_current(self, followup, *, include_painted=False, mark_painted=False):
        current = (self.ledger is not None and self.lock_probe() is False
                   and self.ledger.native_followup_current(followup, self.now(), include_painted=include_painted,
                                                          mark_painted=mark_painted))
        if current and mark_painted:
            self.rows = self.ledger.rows()
        return current

    @staticmethod
    def _delivery_id(task):
        return f"delivery:{task.task_id}:{task.occurrence}:v{task.version}"

    def _submit_os(self, task, due_at, *, sound, command=None):
        now = self.now()
        delivery_id = self._delivery_id(task)
        identifier = notification_id(delivery_id)
        if sound:
            deadline = task.draft.hard_deadline
            sound = not any(
                row["delivery_id"] != delivery_id and row["deadline"] and deadline is not None
                and datetime.fromisoformat(row["deadline"]) == deadline
                and row["state"] in {"submitting", "scheduled", "submitted", "delivered", "unknown",
                                     "cancel_intent", "cancelling", "painted", "grouped"}
                for row in self.ledger.rows()
            )
        if not self.ledger.prepare_os(task, delivery_id, identifier, now, command=command):
            return
        sound = sound and self.ledger.os_sound_enabled()

        def complete(state, reason):
            self._callback.emit(lambda: self.ledger.update(delivery_id, {"submitting"}, state, self.now(), reason))

        try:
            self.notifications.submit(identifier, due_at, sound=sound, complete=complete)
        except (OSError, RuntimeError, ValueError) as exc:
            complete("unknown", type(exc).__name__)

    def route_hidden(self, command):
        requested_os = command.channel == "os_notification"
        if self.ledger is None or (not requested_os and command.reason not in {"hard_due", "deadline_fallback"}):
            return
        if not self.settings.get("allow_system_notifications"):
            return
        for task in self._active_tasks():
            if self._delivery_id(task) == command.delivery_id:
                if self.notifications.available:
                    self._submit_os(task, None, sound=self._sound_enabled(command.sound),
                                    command=command if requested_os else None)
                else:
                    self.ledger.record_unavailable(task, command.delivery_id, self.now(), self.notifications.status)
                return

    def soft_valid(self, command):
        if command.reason not in {"opportunity", "tool"} or self.ledger is None:
            return True
        if self.ledger.authorize_early is not None:
            # The ledger checks current authority under the same transaction as the permit.
            return True
        if self.observations is None:
            return False
        current, status = self.observations.read_current(datetime.now(timezone.utc))
        return bool(status == "fresh" and current and current.state == "available"
                    and current.context_revision == command.context_revision
                    and command.context_valid_until and self.now() < command.context_valid_until)

    def permit(self, command):
        if self.ledger is None:
            return True
        now = self.now()
        return self.lock_probe() is False and self.soft_valid(command) and self.ledger.permit_qt(command, now)

    def permit_badge(self, command):
        return (self.ledger is not None and self.lock_probe() is False and self.soft_valid(command)
                and self.ledger.permit_badge(command, self.now()))

    def transfer_early(self, command):
        if (self.ledger is None or not semantic_background_enabled()
                or not self._can_verify_early_transfer() or not self.notifications.available):
            return
        now = self.now()
        row = self.ledger.begin_early_transfer(command, now)
        if row is not None:
            self._early_transfers[command.delivery_id] = command
            self._remove_os(now, row, reason=EARLY_OS_TRANSFER)

    def _can_verify_early_transfer(self):
        return getattr(self.notifications, "can_verify_early_transfer", self.notifications.can_verify_removal)

    def can_group(self, command):
        return self.ledger is None or (self.lock_probe() is False and self.ledger.can_group(command, self.now()))

    def remember_group(self, commands):
        if self.ledger is not None:
            self.ledger.record_grouped(commands, self.now())

    def release_unpainted(self, command):
        if self.ledger is not None:
            self.ledger.update(command.delivery_id, {"permitted"}, "released", self.now(),
                               "group_rebuilt_before_paint", current_session=True)

    def painted(self, commands, *, allow_sound=True):
        if self.ledger is None:
            return list(commands)
        now = self.now()
        painted = [command for command in commands if self.ledger.mark_painted(command, now)]
        ringing = next((command for command in painted if allow_sound and self._sound_enabled(command.sound)
                        and self.native and self.lock_probe() is False and self.soft_valid(command)
                        and self.ledger.sound_event(command, self.now(), True, None)), None)
        if ringing is not None:
            QApplication.beep()
        for command in painted:
            if command is not ringing:
                self.ledger.sound_event(command, now, False, "silent_or_grouped")
        return painted

    def _reconcile(self, now):
        os_rows = [row for row in self.rows if row["channel"] == "os_notification" and row["state"] in
                   {"submitting", "scheduled", "submitted", "unknown", "cancel_intent", "cancelling"}]
        for row in self.rows:
            if row["state"] == "permitted" and row["owner_session"] != self.ledger.session:
                self.ledger.update(row["delivery_id"], {"permitted"}, "unknown", now, "restarted_after_qt_permit")
        if not os_rows or self._in_progress:
            return
        self._in_progress = True

        def received(pending, delivered):
            self._callback.emit(lambda: self._apply_inventory(self.now(), os_rows, pending, delivered))

        try:
            self.notifications.inventory(received)
        except (OSError, RuntimeError, ValueError):
            self._in_progress = False

    def _apply_inventory(self, now, rows, pending, delivered):
        self._in_progress = False
        active = {(item.task_id, item.occurrence, item.draft.hard_deadline) for item in self._active_tasks()
                  if item.draft.expires_at is None or now < item.draft.expires_at}
        enabled = bool(self.settings.get("allow_system_notifications"))
        for row in rows:
            delivery, identifier, state = row["delivery_id"], row["os_id"], row["state"]
            due = datetime.fromisoformat(row["deadline"]) if row["deadline"] else None
            live = (row["task_id"], row["occurrence"], due) in active
            if delivered is not None and identifier in delivered:
                self.ledger.update(delivery, {state}, "delivered", now)
            elif pending is not None and identifier in pending:
                if not live or not enabled:
                    self._cancel_os(now, row)
                elif state in {"submitting", "unknown"}:
                    self.ledger.update(delivery, {state}, "scheduled", now)
            elif state == "cancelling" or (state == "cancel_intent" and row["reason"] == EARLY_OS_TRANSFER):
                early = row["reason"] == EARLY_OS_TRANSFER
                can_verify = ((self._can_verify_early_transfer() and delivered is not None)
                              if early else self.notifications.can_verify_removal)
                verified = (pending is not None and due is not None
                            and now + timedelta(seconds=1) < due and can_verify)
                self.ledger.update(delivery, {state}, "cancelled" if verified else "unknown", now,
                                   row["reason"] if verified else "retraction_unconfirmed")
            elif state == "cancel_intent":
                self.ledger.update(delivery, {state}, "unknown", now, "retraction_unconfirmed")
            elif state in {"scheduled", "submitted"} or (state == "submitting" and row["owner_session"] != self.ledger.session):
                self.ledger.update(delivery, {state}, "unknown", now, "no_confirmed_platform_record")
        self.rows = self.ledger.rows()

    def _cancel_os(self, now, row):
        delivery_id = row["delivery_id"]
        if not self.ledger.update(delivery_id, {row["state"]}, "cancel_intent", now):
            return
        self._remove_os(now, row)

    def _remove_os(self, now, row, *, reason=None):
        try:
            removed = self.notifications.remove_pending(row["os_id"])
        except (OSError, RuntimeError, ValueError):
            removed = False
        self.ledger.update(row["delivery_id"], {"cancel_intent"}, "cancelling" if removed else "unknown", now,
                           reason if removed else "retraction_unconfirmed")

    def pending_projection(self, now):
        notes = {}
        descriptions = {"scheduled": "已登记系统通知", "submitted": "系统已受理，等待显示",
                        "delivered": "系统报告已送达，等待确认", "unknown": "投递结果未知 · 静默待处理",
                        "failed": "系统通知未提交", "unavailable": "系统通知不可用", "cancelling": "等待系统撤回",
                        "cancel_intent": "等待系统撤回", "painted": "桌宠已展示，等待确认",
                        "badge_shown": "已显示轻提醒，打开列表查看",
                        "grouped": "同批到期 · 待查看"}
        for task in self._active_tasks():
            if task.draft.expires_at and now >= task.draft.expires_at:
                notes[task.task_id] = "提醒已失效"
                continue
            if self.snapshot.get("pending_deliveries", {}).get(task.task_id) == task.version:
                notes[task.task_id] = "待展示"
            for row in self.rows:
                if row["task_id"] != task.task_id or row["occurrence"] != task.occurrence:
                    continue
                text = descriptions.get(row["state"])
                if text:
                    notes[task.task_id] = text
                    if row["state"] in {"failed", "unavailable"} and row["reason"]:
                        notes[task.task_id] += " · " + row["reason"]
        return notes
