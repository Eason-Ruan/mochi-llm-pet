"""The preferences surface mounted in the production desktop pet."""
from __future__ import annotations

import re
from threading import Thread
from uuid import uuid4
import sqlite3

from PyQt6.QtCore import QSignalBlocker, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QFrame, QHBoxLayout, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from deskpet.observation_v2.routes import BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, OFFICIAL_BASE_URL
from deskpet.settings import Habit, HabitBehavior, SettingsConflict

from .refine_panels import (
    CARD_OUTLINE, GREEN, INK, MUTED, PAPER, SAND, _Panel, _card, _text, _title,
)


_ROLES = {
    "system2": ("对话与动作", "分享已发布的活动摘要"),
    "observation_vision": ("屏幕图像理解", "发送获准窗口的截图"),
    "observation_state": ("活动状态判断", "发送脱敏屏幕文字"),
}

_ROUTE_NAMES = {BAILIAN: "阿里云百炼", "openai": "OpenAI", "local_ollama": "Ollama"}
_ROUTE_ENDPOINTS = {
    BAILIAN: BAILIAN_BASE_URL, "openai": OFFICIAL_BASE_URL, "local_ollama": "http://127.0.0.1:11434",
}

_CHECKBOXES = (
    ("link_issues", "关联事项与专注"),
    ("auto_adjust", "根据活动建议开始或调整专注"),
    ("habits_enabled", "允许习惯自动切换阶段"),
    ("allow_proactive_movement", "允许桌宠主动移动"),
    ("reduce_motion", "减少动效"),
    ("reminder_sound", "提醒音"),
    ("phase_end_sound", "阶段结束音"),
    ("allow_system_notifications", "允许系统通知"),
    ("lock_screen_detail", "在锁屏显示通知详情"),
    ("observation_screenshots", "允许截取当前窗口"),
    ("observation_browser_metadata", "读取浏览器域名"),
    ("observation_paused", "暂停活动采集"),
)

_CONTROL_STYLE = f"""
QLabel, QCheckBox {{ color: {INK}; }}
QPushButton, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    color: {INK}; background: {PAPER}; border: 1px solid {CARD_OUTLINE};
    border-radius: 6px; padding: 4px 6px; min-height: 18px;
}}
QPushButton:hover {{ background: {SAND}; }}
QPushButton:disabled {{ color: {MUTED}; background: {SAND}; }}
QPushButton:focus, QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {GREEN};
}}
"""


class _DurationSpinBox(QDoubleSpinBox):
    def textFromValue(self, value: float) -> str:
        digits = 0 if value.is_integer() else self.decimals()
        return self.locale().toString(value, "f", digits)


def _choice(choices):
    box = QComboBox()
    for label, value in choices:
        box.addItem(label, value)
    return box


def _duration():
    field = _DurationSpinBox()
    field.setDecimals(2)
    field.setRange(1 / 60, 1440)
    field.setSingleStep(1)
    field.setSuffix(" 分钟")
    field.setKeyboardTracking(False)
    return field


def verify_route_in_background(routes, role, finished):
    from deskpet.observation_v2.openai_cloud import verify_cloud_route

    expected = routes.snapshot(role)

    def verify():
        try:
            if role == "system2":
                from deskpet.system2.dsh import verify_system2_route

                verify_system2_route(routes, expected=expected)
            else:
                verify_cloud_route(routes, role, expected=expected)
        except (ValueError, RuntimeError, sqlite3.Error):
            pass
        finally:
            try:
                finished.emit(role)
            except RuntimeError:
                # A user can close the window before the capability probe finishes.
                pass

    Thread(target=verify, daemon=True).start()


def _dialog_buttons(dialog):
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.rejected.connect(dialog.reject)
    return buttons


class SettingsPanel(_Panel):
    settings_changed = pyqtSignal(object)
    _verification_finished = pyqtSignal(str)

    def __init__(
        self, store, *, sound_preview=None, notification_status=None,
        refresh_notification_permission=None, submit=None, save_preferences=None, save_habit=None, parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("refine-settings")
        self.setStyleSheet(_CONTROL_STYLE)
        self.store = store
        self.submit = submit
        self.sound_preview = sound_preview
        self.notification_status = notification_status
        self.refresh_notification_permission = refresh_notification_permission
        self.save_preferences = save_preferences
        self.save_habit = save_habit
        self._habit_dialog = None
        self._habit_signature = None
        self._settings = store.read()
        self._saving = False
        self._checking: set[str] = set()
        self._height_limit: int | None = None
        self._verification_finished.connect(self._finish_verification)

        self.body.addLayout(_title("设置"))
        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(355)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.body.addWidget(self._scroll)
        contents = QWidget()
        contents.setStyleSheet("QWidget#settings-sections { background: transparent; }")
        contents.setObjectName("settings-sections")
        self._sections = QVBoxLayout(contents)
        self._sections.setContentsMargins(0, 0, 4, 0)
        self._sections.setSpacing(8)
        self._scroll.setWidget(contents)

        self._checkboxes = {key: QCheckBox(label) for key, label in _CHECKBOXES}
        for key, checkbox in self._checkboxes.items():
            checkbox.setObjectName(f"settings-{key}")
            checkbox.toggled.connect(lambda enabled, name=key: self._save(**{name: enabled}))
        self._build_focus()
        self._build_delivery()
        self._build_models()
        self._build_input()
        self._sections.addStretch()

        self.status = _text(size=11, color=MUTED, wrap=True)
        self.status.hide()
        self.body.addWidget(self.status)
        self.refresh()

    def _section(self, title):
        card, layout = _card(spacing=7)
        layout.addWidget(_text(title, bold=True))
        self._sections.addWidget(card)
        return layout

    def _build_focus(self):
        section = self._section("专注与休息")
        form = QFormLayout()
        self._durations = {}
        for key, title in (("default_focus_s", "默认专注"), ("default_break_s", "默认休息")):
            field = _duration()
            field.setObjectName(f"settings-{key}")
            field.editingFinished.connect(lambda name=key: self._save_duration(name))
            self._durations[key] = field
            form.addRow(title, field)
        section.addLayout(form)
        for key in ("link_issues", "auto_adjust", "habits_enabled"):
            section.addWidget(self._checkboxes[key])
        self._habit_selector = QComboBox()
        self._habit_selector.setAccessibleName("命名习惯")
        self._habit_selector.currentIndexChanged.connect(self._render_habit_actions)
        section.addWidget(self._habit_selector)
        actions = QHBoxLayout()
        create = QPushButton("新建习惯")
        create.setObjectName("settings-habit-create")
        create.clicked.connect(lambda: self._edit_habit(None))
        self._habit_edit = QPushButton("编辑")
        self._habit_edit.clicked.connect(lambda: self._edit_habit(self._habit_selector.currentData()))
        self._habit_start = QPushButton("开始")
        self._habit_start.clicked.connect(self._start_habit)
        actions.addWidget(create)
        actions.addWidget(self._habit_edit)
        if self.submit is not None:
            actions.addWidget(self._habit_start)
        section.addLayout(actions)

    def _build_delivery(self):
        section = self._section("桌宠与通知")
        for key in ("allow_proactive_movement", "reduce_motion"):
            section.addWidget(self._checkboxes[key])
        for key, kind in (("reminder_sound", "reminder"), ("phase_end_sound", "phase_end")):
            row = QHBoxLayout()
            row.addWidget(self._checkboxes[key])
            row.addStretch()
            preview = QPushButton("试听")
            preview.setAccessibleName(f"试听{self._checkboxes[key].text()}")
            preview.setEnabled(self.sound_preview is not None)
            preview.clicked.connect(lambda _, sound=kind: self._report(self.sound_preview(sound)))
            row.addWidget(preview)
            section.addLayout(row)
        for key in ("allow_system_notifications", "lock_screen_detail"):
            section.addWidget(self._checkboxes[key])
        self._notification_label = _text(size=11, color=MUTED, wrap=True)
        section.addWidget(self._notification_label)
        if self.refresh_notification_permission is not None:
            refresh = QPushButton("检查系统通知权限")
            refresh.clicked.connect(self._check_notifications)
            section.addWidget(refresh)

    def _build_models(self):
        section = self._section("模型与隐私")
        for key in ("observation_paused", "observation_screenshots", "observation_browser_metadata"):
            section.addWidget(self._checkboxes[key])
        exclusions = QPushButton("编辑排除应用与域名")
        exclusions.clicked.connect(self._edit_exclusions)
        section.addWidget(exclusions)
        self._route_labels = {}
        self._route_buttons = {}
        self._verify_buttons = {}
        for role, (name, _) in _ROLES.items():
            section.addWidget(_text(name, bold=True))
            status = _text(size=11, color=MUTED, wrap=True)
            self._route_labels[role] = status
            section.addWidget(status)
            row = QHBoxLayout()
            configure = QPushButton("配置")
            configure.setObjectName(f"settings-route-{role}")
            configure.setAccessibleName(f"配置{name}")
            configure.clicked.connect(lambda _, key=role: self._configure_route(key))
            self._route_buttons[role] = configure
            verify = QPushButton("验证能力")
            verify.setObjectName(f"settings-verify-{role}")
            verify.setAccessibleName(f"验证{name}能力")
            verify.clicked.connect(lambda _, key=role: self._verify_route(key))
            self._verify_buttons[role] = verify
            row.addWidget(configure)
            row.addWidget(verify)
            section.addLayout(row)

    def _build_input(self):
        section = self._section("默认输入方式")
        section.addWidget(_text("打字 · 当前版本仅支持文字聊天", size=11, color=MUTED, wrap=True))

    def refresh(self):
        self._settings = self.store.read()
        for key, widget in self._checkboxes.items():
            with QSignalBlocker(widget):
                widget.setChecked(getattr(self._settings, key))
        for key, widget in self._durations.items():
            with QSignalBlocker(widget):
                widget.setValue(getattr(self._settings, key) / 60)
        self._load_habits()
        self._render_routes()
        self._render_notification_status()

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def sync_settings_version(self, version: int):
        if version > self._settings.version:
            self.refresh()

    def sync_habits(self, signature):
        signature = tuple(tuple(part) for part in signature)
        if signature != self._habit_signature:
            self._load_habits()

    def set_height_limit(self, max_height: int):
        self._height_limit = max_height
        status_height = 0 if self.status.isHidden() else self.status.heightForWidth(266) + 8
        self._scroll.setFixedHeight(min(355, max(42, max_height - 55 - status_height)))

    def _report(self, message):
        self.status.setText(message or "")
        self.status.setVisible(bool(message))
        if self._height_limit is not None:
            self.set_height_limit(self._height_limit)

    def _save_duration(self, name):
        self._save(**{name: round(self._durations[name].value() * 60)})

    def _save(self, **changes):
        if self._saving or all(getattr(self._settings, key) == value for key, value in changes.items()):
            return
        if self.save_preferences is not None:
            self._saving = True
            self._scroll.setEnabled(False)
            self._report("正在保存设置…")
            try:
                self.save_preferences(self._settings.version, changes)
            except RuntimeError:
                self.finish_preference_save(False)
            return
        try:
            self._settings = self.store.update(expected_version=self._settings.version, **changes)
        except SettingsConflict:
            self.refresh()
            self._report("设置已更新，请重新操作。")
            return
        except (ValueError, sqlite3.Error):
            self.refresh()
            self._report("保存设置失败。")
            return
        self._report("")
        self._render_notification_status()
        self.settings_changed.emit(self._settings)

    def finish_preference_save(self, applied: bool):
        if not self._saving:
            return
        self.refresh()
        self._saving = False
        self._scroll.setEnabled(True)
        self._report("" if applied else "保存设置失败，请核对当前设置后重试。")
        if applied:
            self.settings_changed.emit(self._settings)

    def _render_notification_status(self):
        if not self._settings.allow_system_notifications:
            status = "系统通知已关闭。可用渠道为桌宠卡片。"
        elif self.notification_status is None:
            status = "系统通知渠道不可用。可用渠道为桌宠卡片。"
        else:
            status = self.notification_status()
        self._notification_label.setText(status)

    def _check_notifications(self):
        self.refresh_notification_permission()
        self._render_notification_status()

    def _edit_exclusions(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("活动采集排除范围")
        dialog.setStyleSheet(_CONTROL_STYLE)
        body = QVBoxLayout(dialog)
        form = QFormLayout()
        apps = QLineEdit(", ".join(self._settings.observation_excluded_apps))
        apps.setPlaceholderText("应用名称，逗号分开")
        domains = QLineEdit(", ".join(self._settings.observation_excluded_domains))
        domains.setPlaceholderText("例如 example.com")
        form.addRow("应用", apps)
        form.addRow("域名", domains)
        body.addLayout(form)
        body.addWidget(_text("域名排除含子域名。启用域名排除后，无法确认网页域名时会跳过截图。",
                             size=11, color=MUTED, wrap=True))
        buttons = _dialog_buttons(dialog)
        buttons.accepted.connect(dialog.accept)
        body.addWidget(buttons)
        if dialog.exec():
            self._save(
                observation_excluded_apps=tuple(re.split(r"[,，]", apps.text())),
                observation_excluded_domains=tuple(re.split(r"[,，]", domains.text())),
            )

    def _load_habits(self):
        selected = self._habit_selector.currentData()
        habits = self.store.list_habits()
        self._habit_signature = tuple((habit.id, habit.version) for habit in habits)
        with QSignalBlocker(self._habit_selector):
            self._habit_selector.clear()
            self._habit_selector.addItem("选择命名习惯", None)
            for habit in habits:
                if habit.status != "archived":
                    label = habit.title if habit.status == "enabled" else f"{habit.title}（已停用）"
                    self._habit_selector.addItem(label, habit.id)
            self._habit_selector.setCurrentIndex(max(0, self._habit_selector.findData(selected)))
        self._render_habit_actions()

    def _render_habit_actions(self):
        habit_id = self._habit_selector.currentData()
        habit = self.store.get_habit(habit_id) if habit_id else None
        self._habit_edit.setEnabled(habit is not None)
        self._habit_start.setEnabled(habit is not None and habit.status == "enabled")

    def _edit_habit(self, habit_id):
        habit = self.store.get_habit(habit_id) if habit_id else None
        if habit_id and habit is None:
            self._load_habits()
            return
        dialog = _HabitEditor(self.store, self._settings, habit, self, submit=self.save_habit)
        self._habit_dialog = dialog
        try:
            if dialog.exec():
                self._load_habits()
                self._habit_selector.setCurrentIndex(max(0, self._habit_selector.findData(dialog.saved.id)))
                self.settings_changed.emit(self._settings)
        finally:
            self._habit_dialog = None
            dialog.deleteLater()

    def receive_habit_result(self, result):
        if self._habit_dialog is not None:
            self._habit_dialog.receive_result(result)

    def _start_habit(self):
        habit_id = self._habit_selector.currentData()
        habit = self.store.get_habit(habit_id) if habit_id else None
        if habit is not None and habit.status == "enabled" and self.submit is not None:
            self.submit({"kind": "start_habit", "habit_id": habit.id, "expected_version": habit.version})

    def selected_habit_id(self):
        return self._habit_selector.currentData()

    def show_system2_route(self):
        button = self._route_buttons["system2"]
        QTimer.singleShot(0, lambda: self._scroll.ensureWidgetVisible(button))

    def _render_routes(self):
        for role, label in self._route_labels.items():
            snapshot = self.store.routes.snapshot(role)
            route = snapshot.route
            local = route is not None and route.provider_kind == "local_ollama"
            if route is None:
                status = ("配置阿里云百炼 API Key 后，点击验证能力。"
                          if role == "system2" else "本地默认路由")
            else:
                if role == "observation_vision" and not (local or snapshot.permission):
                    capability = "请先授权该接收方，再验证识图能力"
                elif role in self._checking:
                    capability = "正在验证"
                else:
                    capability = "能力已验证" if snapshot.verified else "待验证能力"
                status = f"{route.display_name} · {route.model}\n{capability}"
                if role == "system2" and not local:
                    status += "\n活动摘要分享已开启" if snapshot.permission else "\n活动摘要分享已关闭"
                elif snapshot.permission:
                    status += "\n已授权该接收方"
                if snapshot.last_success:
                    status += f"\n最近成功 {snapshot.last_success}"
                if snapshot.last_failure:
                    status += f"\n验证失败 · {snapshot.last_failure}"
            label.setText(status)
            self._verify_buttons[role].setEnabled(
                (route is not None and role not in self._checking)
                and (role != "observation_vision" or local or snapshot.permission)
            )

    def _configure_route(self, role):
        dialog = _RouteEditor(self.store.routes, role, self)
        dialog.exec()
        dialog.deleteLater()
        self._render_routes()

    def _verify_route(self, role):
        self._checking.add(role)
        self._render_routes()
        verify_route_in_background(self.store.routes, role, self._verification_finished)

    def _finish_verification(self, role):
        self._checking.discard(role)
        self._render_routes()


class _HabitEditor(QDialog):
    def __init__(self, store, settings, habit, parent, *, submit=None):
        super().__init__(parent)
        self.setWindowTitle("编辑习惯" if habit else "新建习惯")
        self.setStyleSheet(_CONTROL_STYLE)
        self.store = store
        self.habit = habit
        self.submit = submit
        self.saved = None
        self.pending = None
        behavior = habit.behavior if habit else HabitBehavior(
            focus_s=settings.default_focus_s, short_break_s=settings.default_break_s,
        )
        body = QVBoxLayout(self)
        form = QFormLayout()
        body.addLayout(form)
        self.title = QLineEdit(habit.title if habit else "")
        self.title.setObjectName("settings-habit-title")
        self.title.setMaxLength(100)
        self.title.setPlaceholderText("例如 写论文")
        self.focus = _duration()
        self.short_break = _duration()
        self.long_break = _duration()
        self.focus.setValue(behavior.focus_s / 60)
        self.short_break.setValue(behavior.short_break_s / 60)
        self.long_break.setValue((behavior.long_break_s or 900) / 60)
        self.long_enabled = QCheckBox("启用长休息")
        self.long_enabled.setChecked(behavior.long_break_enabled)
        self.basis = _choice((("完成轮数", "completed_phases"), ("累计专注分钟", "focus_seconds")))
        self.basis.setCurrentIndex(self.basis.findData(behavior.long_break_basis))
        self.threshold = QSpinBox()
        self.threshold.setRange(1, 1440)
        self.threshold.setValue(
            max(1, round(behavior.threshold / 60)) if behavior.long_break_basis == "focus_seconds"
            else behavior.threshold
        )
        self.auto_break = QCheckBox("专注到点后自动开始休息")
        self.auto_break.setChecked(behavior.auto_start_break)
        self.auto_focus = QCheckBox("休息到点后自动开始专注")
        self.auto_focus.setChecked(behavior.auto_start_focus)
        self.status = _choice((("启用", "enabled"), ("停用", "disabled")))
        if habit:
            self.status.addItem("归档", "archived")
            self.status.setCurrentIndex(self.status.findData(habit.status))
        for label, field in (
            ("名称", self.title), ("状态", self.status), ("专注", self.focus), ("短休息", self.short_break),
            ("", self.long_enabled), ("长休息", self.long_break), ("长休息依据", self.basis),
            ("阈值", self.threshold), ("", self.auto_break), ("", self.auto_focus),
        ):
            form.addRow(label, field)
        self.long_enabled.toggled.connect(self._toggle_long_break)
        self._toggle_long_break(self.long_enabled.isChecked())
        self.error = _text(size=11, color=MUTED, wrap=True)
        body.addWidget(self.error)
        self.buttons = _dialog_buttons(self)
        self.buttons.accepted.connect(self._save)
        body.addWidget(self.buttons)

    def _toggle_long_break(self, enabled):
        for field in (self.long_break, self.basis, self.threshold):
            field.setEnabled(enabled)

    def _save(self):
        if self.pending:
            return
        if not self.title.text().strip():
            self.error.setText("请输入习惯名称。")
            return
        basis = self.basis.currentData()
        try:
            behavior = HabitBehavior(
                focus_s=round(self.focus.value() * 60), short_break_s=round(self.short_break.value() * 60),
                long_break_enabled=self.long_enabled.isChecked(), long_break_s=round(self.long_break.value() * 60),
                long_break_basis=basis, threshold=self.threshold.value() * (60 if basis == "focus_seconds" else 1),
                auto_start_break=self.auto_break.isChecked(), auto_start_focus=self.auto_focus.isChecked(),
            )
            values = {"title": self.title.text().strip(), "behavior": behavior.model_dump(mode="json"),
                      "status": self.status.currentData()}
            if self.submit is not None:
                if self.habit is None:
                    args = {**values, "expected_version": 0}
                else:
                    args = {"id": self.habit.id, "patch": values, "expected_version": self.habit.version}
                self.pending = self.submit("habit.update" if self.habit else "habit.create", args)
                if self.pending is None:
                    self.error.setText("暂时无法提交习惯，请稍后重试。")
                else:
                    self.error.setText("正在保存习惯…")
                    self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
                return
            self.saved = self.store.save_habit(
                title=values["title"], behavior=behavior, status=values["status"],
                habit_id=self.habit.id if self.habit else None, expected_version=self.habit.version if self.habit else 0,
            )
        except SettingsConflict:
            self.error.setText("习惯已更新，请重新打开后再编辑。")
            return
        except (ValueError, sqlite3.Error):
            self.error.setText("保存习惯失败。")
            return
        self.accept()

    def receive_result(self, result):
        if self.pending is None or result.get("event_id") != self.pending:
            return
        self.pending = None
        if result.get("status") == "applied" and result.get("view"):
            self.saved = Habit.model_validate_json(result["view"])
            self.accept()
            return
        changed = result.get("error_code") == "STALE_TASK"
        self.error.setText("习惯已更新，请重新打开后再编辑。" if changed else "保存习惯失败。")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not changed)


class _RouteEditor(QDialog):
    def __init__(self, routes, role, parent, *, preset_provider=None):
        super().__init__(parent)
        self.routes = routes
        self.role = role
        self.snapshot = routes.snapshot(role)
        self.route = self.snapshot.route
        self._preset_provider = preset_provider
        name, permission = _ROLES[role]
        self.setWindowTitle(name)
        self.setStyleSheet(_CONTROL_STYLE)
        self.setMinimumWidth(395)
        body = QVBoxLayout(self)
        form = QFormLayout()
        body.addLayout(form)
        if role == "system2":
            choices = ((_ROUTE_NAMES[BAILIAN], BAILIAN), ("OpenAI", "openai"), ("OpenAI 兼容服务", "openai_compatible"),
                       ("本地 Ollama", "local_ollama"), ("停用模型对话", None))
        else:
            choices = (("本地默认", None), ("本地 Ollama", "local_ollama"),
                       ("OpenAI", "openai"), ("OpenAI 兼容服务", "openai_compatible"))
        self.provider = _choice(choices)
        if preset_provider is not None:
            index = self.provider.findData(preset_provider)
            if index < 0:
                raise ValueError("The preset provider is not available for this role")
            self.provider.setCurrentIndex(index)
        elif self.route:
            self.provider.setCurrentIndex(self.provider.findData(self.route.provider_kind))
        self._previous_provider = self.provider.currentData()
        self.recipient = QLineEdit()
        self.endpoint = QLineEdit()
        self.model = QLineEdit()
        self.key = QLineEdit()
        self.key.setObjectName("settings-route-api-key")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.finished.connect(self.key.clear)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 300)
        self.timeout.setSuffix(" 秒")
        for label, field in (
            ("服务类型", self.provider), ("接收方名称", self.recipient), ("服务地址", self.endpoint),
            ("模型", self.model), ("API 密钥", self.key), ("超时", self.timeout),
        ):
            form.addRow(label, field)
        self._key_label = form.labelForField(self.key)

        self.permission = QPushButton()
        self.permission.setAccessibleName(permission)
        self.permission.clicked.connect(self._change_permission)
        body.addWidget(self.permission)
        self.permission.setVisible(preset_provider is None)
        self.help = _text(size=11, color=MUTED, wrap=True)
        body.addWidget(self.help)
        self.error = _text(size=11, color=MUTED, wrap=True)
        body.addWidget(self.error)
        buttons = _dialog_buttons(self)
        buttons.accepted.connect(self._save)
        body.addWidget(buttons)
        self._prefill(self.provider.currentData())
        self._provider_changed()
        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.endpoint.textChanged.connect(self._endpoint_changed)
        if self.provider.currentData() == BAILIAN:
            self.key.setFocus()

    def _prefill(self, provider):
        if self.route is not None and self.route.provider_kind == provider:
            recipient, endpoint = self.route.display_name, self.route.base_url
            model, timeout = self.route.model, round(self.route.timeout_s)
        elif provider == BAILIAN:
            recipient, endpoint, model, timeout = _ROUTE_NAMES[BAILIAN], BAILIAN_BASE_URL, BAILIAN_MODEL, 90
        else:
            recipient, endpoint, model, timeout = "", "", "", 90
        self.recipient.setText(recipient)
        self.endpoint.setText(endpoint)
        self.model.setText(model)
        self.timeout.setValue(timeout)

    def _provider_changed(self):
        provider = self.provider.currentData()
        previous = self._previous_provider
        if self.role == "system2" and provider != previous:
            self.key.clear()
            if BAILIAN in (provider, previous):
                self._prefill(provider)
        self._previous_provider = provider
        for field in (self.recipient, self.endpoint, self.model, self.timeout):
            field.setEnabled(provider is not None)
        self.key.setEnabled(provider not in {None, "local_ollama"})
        self.endpoint.setReadOnly(provider == "openai")
        self.model.setReadOnly(provider == BAILIAN)
        if provider == "openai":
            self.endpoint.setText(OFFICIAL_BASE_URL)
        elif previous == "openai" and self.endpoint.text() == OFFICIAL_BASE_URL:
            self.endpoint.clear()
        self.endpoint.setPlaceholderText(_ROUTE_ENDPOINTS.get(provider, OFFICIAL_BASE_URL))
        label = "百炼 API Key" if provider == BAILIAN else "API 密钥"
        self._key_label.setText(label)
        self.key.setAccessibleName(label)
        self._render_permission()

    def _endpoint_changed(self):
        if self.provider.currentData() == BAILIAN:
            self.key.clear()
        if self.role == "system2":
            self._render_permission()

    def _selected_endpoint(self, provider):
        from deskpet.observation_v2.routes import _normalize_base_url

        return _normalize_base_url(self.endpoint.text().strip() or _ROUTE_ENDPOINTS.get(provider, ""))

    def _same_recipient(self, route, provider):
        if route is None or route.provider_kind != provider:
            return False
        try:
            return route.base_url == self._selected_endpoint(provider)
        except ValueError:
            return False

    def _render_permission(self):
        _, permission = _ROLES[self.role]
        route = self.snapshot.route
        provider = self.provider.currentData()
        if self.role == "system2" and not self._same_recipient(route, provider):
            route = None
        is_local = route is not None and route.provider_kind == "local_ollama"
        self.permission.setEnabled(route is not None and not is_local and self._preset_provider is None)
        if route is not None and self.snapshot.permission:
            self.permission.setText(f"撤回授权 · {permission}")
        else:
            self.permission.setText(f"授权 · {permission}")
        status = (
            f"当前接收方：{route.display_name}\n{route.base_url}\n"
            "更改授权适用于以后发送。已发送的信息无法撤回。"
            if route else "保存云端接收方后可单独授权发送内容。"
        )
        if is_local:
            status = f"本地模型：{route.display_name}\n{route.base_url}"
        elif self.role == "system2" and route:
            status = (f"接收方：{route.display_name}\n{route.base_url}\n"
                      "模型接收与你的请求相关的聊天、事项和结构化活动状态。"
                      "活动摘要需单独授权。撤回只影响后续发送。")
        elif self.role == "system2":
            status = "保存服务和模型后验证文本与工具调用能力。活动摘要分享默认关闭。"
        if provider == BAILIAN:
            if route is not None:
                self.key.setPlaceholderText("留空保留此地址的 Key")
            else:
                self.key.setPlaceholderText("输入百炼 API Key")
            status = ("使用与服务地址同地域的百炼 API Key。默认地址为华北2（北京），"
                      "也可填写业务空间专属的 OpenAI 兼容地址。\n" + status)
        else:
            self.key.setPlaceholderText("输入新密钥")
        self.help.setText(status)

    def _change_permission(self):
        route = self.snapshot.route
        if not self.permission.isEnabled() or route is None:
            return
        allowed = not self.snapshot.permission
        if allowed:
            name, data = _ROLES[self.role]
            prompt = QMessageBox(self)
            prompt.setWindowTitle(f"{name}授权")
            prompt.setTextFormat(self.help.textFormat())
            prompt.setText(f"允许向 {route.display_name} ({route.base_url}) {data}？")
            prompt.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            prompt.setDefaultButton(QMessageBox.StandardButton.No)
            prompt.button(QMessageBox.StandardButton.Yes).setText("授权")
            prompt.button(QMessageBox.StandardButton.No).setText("取消")
            if prompt.exec() != QMessageBox.StandardButton.Yes:
                return
        try:
            self.snapshot = self.routes.set_permission(self.role, allowed, expected=self.snapshot)
        except (ValueError, RuntimeError, sqlite3.Error):
            if self.role == "system2":
                self.error.setText("授权状态已变化，请重新打开设置后再操作。")
            else:
                self.snapshot = self.routes.snapshot(self.role)
                self.error.setText("授权状态已变化，请核对接收方后再操作。")
        else:
            self.error.clear()
        self._render_permission()

    def _save(self):
        from deskpet.observation_v2.openai_cloud import delete_route_credential, store_route_credential
        from deskpet.observation_v2.routes import CloudRoute

        provider = self.provider.currentData()
        key = "" if provider in {None, "local_ollama"} else self.key.text().strip()
        self.key.clear()
        try:
            if provider is None:
                self.routes.clear(self.role, expected=self.snapshot)
            else:
                endpoint = self._selected_endpoint(provider)
                same_recipient = self.route is not None and self.route.recipient == (provider, endpoint)
                credential_ref = uuid4().hex if key else self.route.credential_ref if same_recipient else None
                if provider == BAILIAN and credential_ref is None:
                    self.error.setText("请输入此服务地址对应的百炼 API Key。")
                    self.key.setFocus()
                    return
                route = CloudRoute(
                    provider_kind=provider, base_url=endpoint,
                    model=self.model.text().strip(),
                    display_name=self.recipient.text().strip() or _ROUTE_NAMES.get(provider, ""),
                    credential_ref=credential_ref, timeout_s=self.timeout.value(),
                )
                if key:
                    store_route_credential(route, key)
                try:
                    self.snapshot = self.routes.configure(self.role, route, expected=self.snapshot)
                except (ValueError, RuntimeError, sqlite3.Error):
                    if key:
                        delete_route_credential(route)
                    raise
        except (ValueError, RuntimeError, sqlite3.Error):
            self.error.setText("保存路由失败，请检查服务地址、模型与系统凭据权限。")
            return
        self.accept()
