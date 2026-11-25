"""The preferences surface mounted in the production desktop pet."""
from __future__ import annotations

import re
from sys import platform
from threading import Thread
from uuid import uuid4
import sqlite3

from PyQt6.QtCore import QSignalBlocker, QTimer, pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from deskpet.desktop_profile import read_desktop_language, save_desktop_language
from deskpet.i18n import language, tr
from deskpet.model_setup import bailian_display_name
from deskpet.observation_v2.routes import BAILIAN, BAILIAN_BASE_URL, BAILIAN_MODEL, OFFICIAL_BASE_URL, SYSTEM2, VISION
from deskpet.perception.macos import screen_recording_access
from deskpet.settings import Habit, HabitBehavior, Settings, SettingsConflict, SettingsStore

from .model_setup import ModelSetupDialog, raw_screen_scope_notice
from .refine_style import (
    DETAIL_CONTROL_STYLE, MUTED, MenuField, Toggle, _CONTROL_STYLE, _Panel, _card, _scroll_area, _text, _title, stepper,
)
from .work_panel import MinutesSpinBox


_ROLES = {
    SYSTEM2: (("对话与动作", "Chat and actions"), ("分享已发布的活动摘要", "published activity summaries")),
    VISION: (("屏幕图像理解", "Screen image understanding"),
             ("发送所选范围内获准的截图", "approved screenshots within the selected scope")),
    "observation_state": (("活动状态判断（旧版／对照）", "Activity status (legacy and comparison)"),
                          ("发送脱敏屏幕文字", "redacted screen text")),
}

_ROUTE_NAMES = {"openai": "OpenAI", "local_ollama": "Ollama"}
_ROUTE_ENDPOINTS = {
    BAILIAN: BAILIAN_BASE_URL, "openai": OFFICIAL_BASE_URL, "local_ollama": "http://127.0.0.1:11434",
}

_CHECKBOXES = (
    ("link_issues", "关联事项与专注", "Link tasks and focus"),
    ("auto_adjust", "自动番茄钟（工作中也可建议）", "Suggest timers, even during work"),
    ("habits_enabled", "允许习惯自动切换阶段", "Auto-advance habit phases"),
    ("allow_proactive_movement", "允许桌宠主动移动", "Allow the pet to move on its own"),
    ("reduce_motion", "减少动效", "Reduce motion"),
    ("reminder_sound", "提醒音", "Reminder sound"),
    ("phase_end_sound", "阶段结束音", "Phase end sound"),
    ("allow_system_notifications", "允许系统通知", "Allow system notifications"),
    ("lock_screen_detail", "在锁屏显示通知详情", "Show details on the lock screen"),
    ("observation_screenshots", "允许截取屏幕图像", "Allow screenshots"),
    ("observation_browser_metadata", "读取浏览器域名", "Read browser domains"),
    ("observation_paused", "暂停活动采集", "Pause activity capture"),
)


def _choice(choices):
    box = QComboBox()
    for label, value in choices:
        box.addItem(label, value)
    return box


def _duration():
    field = MinutesSpinBox()
    field.setDecimals(2)
    field.setRange(1 / 60, 1440)
    field.setSingleStep(1)
    field.setSuffix(tr(" 分钟", " min"))
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
    buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("保存", "Save"))
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消", "Cancel"))
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
        self.setStyleSheet(DETAIL_CONTROL_STYLE)
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

        self.body.addLayout(_title(tr("设置", "Settings")))
        contents = QWidget()
        self._scroll = _scroll_area(contents)
        self._scroll.setFixedHeight(480)
        self.body.addWidget(self._scroll)
        self._sections = QVBoxLayout(contents)
        self._sections.setContentsMargins(1, 0, 2, 0)
        self._sections.setSpacing(8)

        self._checkboxes = {key: Toggle(tr(zh, en)) for key, zh, en in _CHECKBOXES}
        for key, checkbox in self._checkboxes.items():
            checkbox.setObjectName(f"settings-{key}")
            checkbox.toggled.connect(lambda enabled, name=key: self._save(**{name: enabled}))
        self._build_language()
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
        layout.addWidget(_text(title, bold=True, wrap=True))
        self._sections.addWidget(card)
        return layout

    def _build_language(self):
        title = tr("语言", "Language")
        section = self._section(title)
        self._language_selector = MenuField()
        self._language_selector.setObjectName("settings-language")
        self._language_selector.setAccessibleName(title)
        self._language_selector.addItem("简体中文", "zh")
        self._language_selector.addItem("English", "en")
        self._language_selector.activated.connect(self._save_language)
        section.addWidget(self._language_selector)
        self._language_label = _text(size=11, color=MUTED, wrap=True)
        self._language_label.setObjectName("settings-language-status")
        section.addWidget(self._language_label)

    def _render_language(self):
        saved = read_desktop_language(self.store.path.parent)
        self._language_selector.setCurrentIndex(self._language_selector.findData(saved))
        if saved == language():
            message = tr("语言更改将在下次启动 Mochi 时生效。", "Language changes take effect the next time you launch Mochi.")
        else:
            selected = tr("简体中文", "Simplified Chinese") if saved == "zh" else "English"
            message = tr(
                "下次启动 Mochi 时将使用 {selected}。", "{selected} will be used the next time you launch Mochi.",
            ).format(selected=selected)
        self._language_label.setText(message)

    def _save_language(self, index):
        try:
            save_desktop_language(self.store.path.parent, self._language_selector.itemData(index))
        except (OSError, RuntimeError, ValueError):
            self._render_language()
            self._language_label.setText(tr("无法保存语言。请重试。", "Could not save the language. Please try again."))
        else:
            self._render_language()

    def _build_focus(self):
        section = self._section(tr("专注与休息", "Focus and breaks"))
        self._durations = {}
        for key, title in (
            ("default_focus_s", tr("默认专注", "Default focus")),
            ("default_break_s", tr("默认休息", "Default break")),
        ):
            field = _duration()
            field.setObjectName(f"settings-{key}")
            field.setAccessibleName(title)
            field.editingFinished.connect(lambda name=key: self._save_duration(name))
            self._durations[key] = field
            row = QHBoxLayout()
            row.addWidget(_text(title, size=12, wrap=True), 1)
            row.addWidget(stepper(field, on_step=lambda name=key: self._save_duration(name)))
            section.addLayout(row)
        for key in ("link_issues", "auto_adjust", "habits_enabled"):
            section.addWidget(self._checkboxes[key])
        section = self._section(tr("习惯", "Habits"))
        self._habit_selector = MenuField()
        self._habit_selector.setAccessibleName(tr("命名习惯", "Named habit"))
        self._habit_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._habit_selector.currentIndexChanged.connect(self._render_habit_actions)
        section.addWidget(self._habit_selector)
        actions = QHBoxLayout()
        create = QPushButton(tr("新建习惯", "New habit"))
        create.setObjectName("settings-habit-create")
        create.clicked.connect(lambda: self._edit_habit(None))
        self._habit_edit = QPushButton(tr("编辑", "Edit"))
        self._habit_edit.clicked.connect(lambda: self._edit_habit(self._habit_selector.currentData()))
        self._habit_start = QPushButton(tr("开始", "Start"))
        self._habit_start.clicked.connect(self._start_habit)
        actions.addWidget(create)
        actions.addWidget(self._habit_edit)
        if self.submit is not None:
            actions.addWidget(self._habit_start)
        section.addLayout(actions)

    def _build_delivery(self):
        section = self._section(tr("桌宠与通知", "Pet and notifications"))
        for key in ("allow_proactive_movement", "reduce_motion"):
            section.addWidget(self._checkboxes[key])
        for key, kind in (("reminder_sound", "reminder"), ("phase_end_sound", "phase_end")):
            row = QHBoxLayout()
            checkbox = self._checkboxes[key]
            text = checkbox.text()
            checkbox.setAccessibleName(text)
            checkbox.setText("")
            checkbox.setFixedWidth(31)
            row.addWidget(_text(text, size=12, wrap=True), 1)
            preview = QPushButton(tr("试听", "Preview"))
            preview.setAccessibleName(tr("试听{sound}", "Preview {sound}").format(sound=text))
            preview.setEnabled(self.sound_preview is not None)
            preview.clicked.connect(lambda _, sound=kind: self._report(self.sound_preview(sound)))
            row.addWidget(preview)
            row.addWidget(checkbox)
            section.addLayout(row)
        for key in ("allow_system_notifications", "lock_screen_detail"):
            section.addWidget(self._checkboxes[key])
        self._notification_label = _text(size=11, color=MUTED, wrap=True)
        section.addWidget(self._notification_label)
        if self.refresh_notification_permission is not None:
            refresh = QPushButton(tr("检查系统通知权限", "Check notification permissions"))
            refresh.clicked.connect(self._check_notifications)
            section.addWidget(refresh)

    def _build_models(self):
        section = self._section(tr("模型与隐私", "Models and privacy"))
        self._model_setup = QPushButton(tr("配置模型连接", "Set up model connections"))
        self._model_setup.setObjectName("settings-model-setup")
        self._model_setup.clicked.connect(self._configure_models)
        section.addWidget(self._model_setup)
        for key in ("observation_paused", "observation_screenshots"):
            section.addWidget(self._checkboxes[key])
        self._screen_scope = MenuField()
        self._screen_scope.setObjectName("settings-observation_screen_scope")
        self._screen_scope.setAccessibleName(tr("截图范围", "Screenshot scope"))
        self._screen_scope.addItem(tr("当前窗口", "Current window"), "foreground_window")
        if platform == "darwin":
            self._screen_scope.addItem(tr("所有显示器（排除黑名单应用）", "All displays (filtered)"), "all_displays")
            self._screen_scope.addItem(tr("所有显示器原始画面（实验）", "All displays (raw, experimental)"), "all_displays_unfiltered")
        else:
            self._screen_scope.addItem(tr("所有显示器（此系统不可用）", "All displays (unavailable)"), "all_displays")
            self._screen_scope.model().item(1).setEnabled(False)
            self._screen_scope.addItem(tr("所有显示器原始画面（此系统不可用）", "All displays (raw, unavailable)"), "all_displays_unfiltered")
            self._screen_scope.model().item(2).setEnabled(False)
        section.addWidget(_text(tr("截图范围", "Screenshot scope"), size=12))
        section.addWidget(self._screen_scope)
        self._screen_scope.activated.connect(
            lambda _: self._save(observation_screen_scope=self._screen_scope.currentData()),
        )
        note = tr(
            "若已授权云端识图，所选范围内获准的截图可发送给该接收方。",
            "If cloud vision is authorized, approved screenshots within the selected scope may be sent to that recipient.",
        )
        if platform == "darwin":
            note = tr(
                "所有显示器范围需要 macOS 14.2 或更高版本及屏幕录制权限。"
                "过滤模式会排除已识别的无痕窗口。未设域名排除时，无法判断是否无痕的窗口仍可采集。",
                "Capturing all displays requires macOS 14.2 or later and screen recording access. Filtered mode "
                "excludes blocked apps and recognized private windows. Without domain exclusions, it can still "
                "capture windows whose private browsing status it cannot determine. ",
            ) + note
        section.addWidget(_text(note, size=11, color=MUTED, wrap=True))
        if platform == "darwin":
            raw_note = tr(
                "实验模式将原始截图发送到已授权并验证的阿里云百炼通义千问。"
                "截图可能包含排除列表中的应用、域名和无痕窗口。模型只在输出中尝试脱敏。",
                "Experimental mode sends raw screenshots to the authorized and verified Qwen model on "
                "Alibaba Cloud Model Studio. Screenshots can include excluded apps and domains, as well as private "
                "browsing windows. The model only attempts redaction in its output.",
            )
            section.addWidget(_text(raw_note, size=11, color=MUTED, wrap=True))
        section.addWidget(self._checkboxes["observation_browser_metadata"])
        exclusions = QPushButton(tr("编辑排除应用与域名", "Edit excluded apps and domains"))
        exclusions.setObjectName("settings-observation-exclusions")
        exclusions.clicked.connect(self._edit_exclusions)
        section.addWidget(exclusions)
        self._screen_recording_status = None
        if QGuiApplication.platformName() == "cocoa":
            request = QPushButton(tr("请求屏幕录制权限", "Request screen recording access"))
            request.setObjectName("settings-request-screen-recording")
            request.clicked.connect(lambda: self._render_screen_recording(request=True))
            section.addWidget(request)
            self._screen_recording_status = _text(size=11, color=MUTED, wrap=True)
            section.addWidget(self._screen_recording_status)
        documents = QPushButton(tr("编辑文档名权限", "Edit document name permissions"))
        documents.setObjectName("settings-document-permissions")
        documents.clicked.connect(self._edit_document_sources)
        section.addWidget(documents)
        self._document_permissions_label = _text(size=11, color=MUTED, wrap=True)
        section.addWidget(self._document_permissions_label)
        self._route_labels = {}
        self._verify_buttons = {}
        for role, (name_labels, _) in _ROLES.items():
            name = tr(*name_labels)
            section.addWidget(_text(name, bold=True, wrap=True))
            status = _text(size=11, color=MUTED, wrap=True)
            self._route_labels[role] = status
            section.addWidget(status)
            row = QHBoxLayout()
            configure = QPushButton(tr("高级配置", "Advanced setup") if role in {SYSTEM2, VISION} else tr("配置", "Configure"))
            configure.setObjectName(f"settings-route-{role}")
            configure.setAccessibleName(tr("配置{role}", "Configure {role}").format(role=name))
            configure.clicked.connect(lambda _, key=role: self._configure_route(key))
            verify = QPushButton(tr("验证能力", "Verify"))
            verify.setObjectName(f"settings-verify-{role}")
            verify.setAccessibleName(tr("验证{role}能力", "Verify {role}").format(role=name))
            verify.clicked.connect(lambda _, key=role: self._verify_route(key))
            self._verify_buttons[role] = verify
            row.addWidget(configure)
            row.addWidget(verify)
            section.addLayout(row)

    def _build_input(self):
        section = self._section(tr("默认输入方式", "Default input method"))
        section.addWidget(_text(tr("打字 · 当前版本仅支持文字聊天", "Typing · Only text chat is supported in this version."), size=11, color=MUTED, wrap=True))

    def refresh(self):
        self._settings = self.store.read()
        self._render_language()
        for key, widget in self._checkboxes.items():
            with QSignalBlocker(widget):
                widget.setChecked(getattr(self._settings, key))
        for key, widget in self._durations.items():
            with QSignalBlocker(widget):
                widget.setValue(getattr(self._settings, key) / 60)
        self._screen_scope.setCurrentIndex(self._screen_scope.findData(self._settings.observation_screen_scope))
        self._load_habits()
        self._render_routes()
        self._render_document_permissions()
        self._render_notification_status()
        self._render_screen_recording()

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
        self._scroll.setFixedHeight(min(480, max(1, max_height - 68 - status_height)))

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
            self._report(tr("正在保存设置…", "Saving settings…"))
            try:
                self.save_preferences(self._settings.version, changes)
            except RuntimeError:
                self.finish_preference_save(False)
            return
        try:
            self._settings = self.store.update(expected_version=self._settings.version, **changes)
        except SettingsConflict:
            self.refresh()
            self._report(tr("设置已更新，请重新操作。", "Settings have changed. Please try again."))
            return
        except (ValueError, sqlite3.Error):
            self.refresh()
            self._report(tr("保存设置失败。", "Could not save settings."))
            return
        self._report("")
        self._render_document_permissions()
        self._render_notification_status()
        self.settings_changed.emit(self._settings)

    def finish_preference_save(self, applied: bool):
        if not self._saving:
            return
        self.refresh()
        self._saving = False
        self._scroll.setEnabled(True)
        self._report("" if applied else tr("保存设置失败，请核对当前设置后重试。", "Could not save settings. Review the current settings and try again."))
        if applied:
            self.settings_changed.emit(self._settings)

    def _render_notification_status(self):
        if not self._settings.allow_system_notifications:
            status = tr("系统通知已关闭。可用渠道为桌宠卡片。", "System notifications are off. Reminders can appear on pet cards.")
        elif self.notification_status is None:
            status = tr("系统通知渠道不可用。可用渠道为桌宠卡片。", "System notifications are unavailable. Reminders can appear on pet cards.")
        else:
            status = self.notification_status()
        self._notification_label.setText(status)

    def _check_notifications(self):
        self.refresh_notification_permission()
        self._render_notification_status()

    def _render_screen_recording(self, *, request=False):
        if self._screen_recording_status is None:
            return
        granted = screen_recording_access(request=request)
        if granted is True:
            status = tr("macOS 屏幕录制权限已授权。", "macOS screen recording access is granted.")
        elif granted is False:
            status = tr(
                "macOS 屏幕录制尚未授权。可在「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」"
                "中允许 Mochi，然后重新启动应用。",
                "Screen recording access is off. Allow Mochi in System Settings → Privacy & Security → "
                "Screen & System Audio Recording, then relaunch the app.",
            )
        else:
            status = tr("无法读取 macOS 屏幕录制权限。", "Could not read macOS screen recording permissions.")
        self._screen_recording_status.setText(status)

    def _edit_exclusions(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("活动采集排除范围", "Activity capture exclusions"))
        dialog.setStyleSheet(_CONTROL_STYLE)
        body = QVBoxLayout(dialog)
        form = QFormLayout()
        apps = QLineEdit(", ".join(self._settings.observation_excluded_apps))
        apps.setObjectName("settings-observation_excluded_apps")
        apps.setPlaceholderText(tr("应用名称片段或 Bundle ID，逗号分开", "App name fragments or bundle IDs, comma-separated"))
        domains = QLineEdit(", ".join(self._settings.observation_excluded_domains))
        domains.setPlaceholderText(tr("例如 example.com", "e.g. example.com"))
        form.addRow(tr("应用", "Apps"), apps)
        form.addRow(tr("域名", "Domains"), domains)
        body.addLayout(form)
        body.addWidget(_text(
            tr(
                "应用名称与 Bundle ID 均按片段匹配。所有显示器范围内，前台和后台窗口均适用。"
                "域名排除含子域名。启用域名排除后，无法确认域名的浏览器窗口会被排除。",
                "App names and bundle IDs match by substring. When capturing all displays, "
                "exclusions apply to foreground and background windows. Domain exclusions include subdomains. "
                "When domain exclusions are on, browser windows with unknown domains are excluded.",
            ),
            size=11, color=MUTED, wrap=True,
        ))
        buttons = _dialog_buttons(dialog)
        buttons.accepted.connect(dialog.accept)
        body.addWidget(buttons)
        if dialog.exec():
            self._save(
                observation_excluded_apps=tuple(re.split(r"[,，]", apps.text())),
                observation_excluded_domains=tuple(re.split(r"[,，]", domains.text())),
            )

    def _render_document_permissions(self):
        local = len(self._settings.document_local_sources)
        cloud = len(self._settings.document_cloud_sources)
        self._document_permissions_label.setText(tr(
            "本机记录 {local} 个来源 · 云端共享 {cloud} 个来源",
            "Local sources: {local} · Cloud sharing sources: {cloud}",
        ).format(local=local, cloud=cloud))

    def _edit_document_sources(self):
        preferences = self._settings
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("文档名权限", "Document name permissions"))
        dialog.setStyleSheet(_CONTROL_STYLE)
        dialog.setMinimumWidth(395)
        body = QVBoxLayout(dialog)
        form = QFormLayout()
        fields = {}
        for key, label, example in (
            ("document_local_sources", tr("本机记录", "Store locally"),
             tr("例如 VS Code，Word，Excel，docs.google.com", "e.g. VS Code, Word, Excel, docs.google.com")),
            ("document_cloud_sources", tr("云端对话共享", "Share with cloud chat"),
             tr("例如 Excel，docs.google.com", "e.g. Excel, docs.google.com")),
        ):
            field = QLineEdit(", ".join(getattr(preferences, key)))
            field.setObjectName(f"settings-{key}")
            field.setAccessibleName(label)
            field.setPlaceholderText(example)
            form.addRow(label, field)
            fields[key] = field
        body.addLayout(form)
        body.addWidget(_text(
            tr(
                "输入支持的应用名称或文档网站的完整域名，用逗号分开。留空即关闭。"
                "云端共享仅保留本机已允许的来源，并需要为当前对话接收方开启活动摘要分享。",
                "Enter supported app names or full domains for document sites, separated by commas. "
                "Leave a field blank to turn it off. Cloud sharing includes only sources allowed locally "
                "and requires activity summary sharing for the current chat recipient.",
            ),
            size=11, color=MUTED, wrap=True,
        ))
        error = _text(size=11, color=MUTED, wrap=True)
        body.addWidget(error)
        changes = {}

        def accept():
            values = {
                key: tuple(source for source in re.split(r"[,，]", field.text()) if source.strip())
                for key, field in fields.items()
            }
            try:
                updated = Settings.model_validate({**preferences.model_dump(), **values})
            except ValueError:
                error.setText(tr(
                    "请输入支持的应用名称或完整域名，每栏最多 100 项。",
                    "Enter supported app names or full domains, up to 100 per field.",
                ))
                return
            changes.update({key: getattr(updated, key) for key in fields})
            dialog.accept()

        buttons = _dialog_buttons(dialog)
        buttons.accepted.connect(accept)
        body.addWidget(buttons)
        if dialog.exec() and changes:
            if preferences.version == self._settings.version:
                self._save(**changes)
            else:
                self._report(tr("设置已更新，请重新操作。", "Settings have changed. Please try again."))
        dialog.deleteLater()

    def _load_habits(self):
        selected = self._habit_selector.currentData()
        habits = self.store.list_habits()
        self._habit_signature = tuple((habit.id, habit.version) for habit in habits)
        with QSignalBlocker(self._habit_selector):
            self._habit_selector.clear()
            self._habit_selector.addItem(tr("选择命名习惯", "Choose a habit"), None)
            for habit in habits:
                if habit.status != "archived":
                    label = habit.title
                    if habit.status != "enabled":
                        label = tr("{title}（已停用）", "{title} (disabled)").format(title=label)
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
        QTimer.singleShot(0, lambda: self._scroll.ensureWidgetVisible(self._model_setup))

    def _render_routes(self):
        for role, label in self._route_labels.items():
            snapshot = self.store.routes.snapshot(role)
            route = snapshot.route
            local = route is not None and route.provider_kind == "local_ollama"
            if route is None:
                status = (
                    tr("尚未配置对话模型。", "No conversation model has been configured.")
                    if role == SYSTEM2 else tr("本地默认路由", "Default local route")
                )
            else:
                if role == VISION and not (local or snapshot.permission):
                    capability = tr(
                        "请先授权该接收方，再验证识图能力",
                        "Authorize this recipient before verifying image understanding.",
                    )
                elif role in self._checking:
                    capability = tr("正在验证", "Verifying")
                else:
                    capability = tr("能力已验证", "Verified") if snapshot.verified else tr("待验证能力", "Pending verification")
                status = f"{route.display_name} · {route.model}\n{capability}"
                if role == "system2" and not local:
                    status += "\n" + (
                        tr("活动摘要分享已开启", "Activity summary sharing is on")
                        if snapshot.permission else tr("活动摘要分享已关闭", "Activity summary sharing is off")
                    )
                elif role == VISION and snapshot.permission:
                    status += "\n" + (
                        tr("已授权所选范围内的截图与近期系统信号",
                           "Sharing approved for screenshots within the selected scope and recent system signals")
                        if snapshot.vision_signals_permission else
                        tr("已授权所选范围内的截图", "Screenshot sharing is authorized for the selected scope")
                    )
                elif snapshot.permission:
                    status += tr("\n已授权该接收方", "\nThis recipient is authorized")
                if snapshot.last_success:
                    status += "\n" + tr("最近成功 {time}", "Last success {time}").format(time=snapshot.last_success)
                if snapshot.last_failure:
                    status += "\n" + tr("验证失败 · {failure}", "Verification failed · {failure}").format(
                        failure=snapshot.last_failure,
                    )
            if role == "observation_state":
                status += tr("\n仅用于旧版双模型采集与对照回放。", "\nUsed only for legacy dual-model capture and comparison playback.")
            label.setText(status)
            self._verify_buttons[role].setEnabled(
                (route is not None and role not in self._checking)
                and (role != VISION or local or snapshot.permission)
            )

    def _configure_route(self, role):
        dialog = _RouteEditor(self.store.routes, role, self)
        dialog.exec()
        dialog.deleteLater()
        self._render_routes()

    def _configure_models(self):
        dialog = ModelSetupDialog(self.store.routes, self)
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
        self.setWindowTitle(tr("编辑习惯", "Edit habit") if habit else tr("新建习惯", "New habit"))
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
        self.title.setPlaceholderText(tr("例如 写论文", "e.g. Write a paper"))
        self.focus = _duration()
        self.short_break = _duration()
        self.long_break = _duration()
        self.focus.setValue(behavior.focus_s / 60)
        self.short_break.setValue(behavior.short_break_s / 60)
        self.long_break.setValue((behavior.long_break_s or 900) / 60)
        self.long_enabled = QCheckBox(tr("启用长休息", "Enable long breaks"))
        self.long_enabled.setChecked(behavior.long_break_enabled)
        self.basis = _choice((
            (tr("完成轮数", "Completed rounds"), "completed_phases"),
            (tr("累计专注分钟", "Total focus minutes"), "focus_seconds"),
        ))
        self.basis.setCurrentIndex(self.basis.findData(behavior.long_break_basis))
        self.threshold = QSpinBox()
        self.threshold.setRange(1, 1440)
        self.threshold.setValue(
            max(1, round(behavior.threshold / 60)) if behavior.long_break_basis == "focus_seconds"
            else behavior.threshold
        )
        self.auto_break = QCheckBox(tr("专注到点后自动开始休息", "Start breaks automatically after focus"))
        self.auto_break.setChecked(behavior.auto_start_break)
        self.auto_focus = QCheckBox(tr("休息到点后自动开始专注", "Start focus automatically after breaks"))
        self.auto_focus.setChecked(behavior.auto_start_focus)
        self.status = _choice(((tr("启用", "Enabled"), "enabled"), (tr("停用", "Disabled"), "disabled")))
        if habit:
            self.status.addItem(tr("归档", "Archived"), "archived")
            self.status.setCurrentIndex(self.status.findData(habit.status))
        for label, field in (
            (tr("名称", "Name"), self.title),
            (tr("状态", "Status"), self.status),
            (tr("专注", "Focus"), self.focus),
            (tr("短休息", "Short break"), self.short_break),
            ("", self.long_enabled),
            (tr("长休息", "Long break"), self.long_break),
            (tr("长休息依据", "Long break trigger"), self.basis),
            (tr("阈值", "Threshold"), self.threshold),
            ("", self.auto_break),
            ("", self.auto_focus),
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
            self.error.setText(tr("请输入习惯名称。", "Enter a habit name."))
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
                    self.error.setText(tr("暂时无法提交习惯，请稍后重试。", "Could not submit the habit. Please try again later."))
                else:
                    self.error.setText(tr("正在保存习惯…", "Saving habit…"))
                    self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
                return
            self.saved = self.store.save_habit(
                title=values["title"], behavior=behavior, status=values["status"],
                habit_id=self.habit.id if self.habit else None, expected_version=self.habit.version if self.habit else 0,
            )
        except SettingsConflict:
            self.error.setText(tr("习惯已更新，请重新打开后再编辑。", "The habit has changed. Reopen it before editing."))
            return
        except (ValueError, sqlite3.Error):
            self.error.setText(tr("保存习惯失败。", "Could not save this habit."))
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
        self.error.setText(
            tr("习惯已更新，请重新打开后再编辑。", "The habit has changed. Reopen it before editing.")
            if changed else tr("保存习惯失败。", "Could not save this habit.")
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not changed)


class _RouteEditor(QDialog):
    def __init__(self, routes, role, parent, *, preset_provider=None):
        super().__init__(parent)
        self.routes = routes
        self.role = role
        self.snapshot = routes.snapshot(role)
        self.route = self.snapshot.route
        self._preset_provider = preset_provider
        name, permission = (tr(*labels) for labels in _ROLES[role])
        self.setWindowTitle(name)
        self.setStyleSheet(_CONTROL_STYLE)
        self.setMinimumWidth(395)
        body = QVBoxLayout(self)
        form = QFormLayout()
        body.addLayout(form)
        if role == "system2":
            choices = (
                (bailian_display_name(), BAILIAN),
                ("OpenAI", "openai"),
                (tr("OpenAI 兼容服务", "OpenAI-compatible service"), "openai_compatible"),
                (tr("本地 Ollama", "Local Ollama"), "local_ollama"),
                (tr("停用模型对话", "Disable model chat"), None),
            )
        else:
            choices = (
                (tr("本地默认", "Local default"), None),
                (tr("本地 Ollama", "Local Ollama"), "local_ollama"),
                ("OpenAI", "openai"),
                (tr("OpenAI 兼容服务", "OpenAI-compatible service"), "openai_compatible"),
            )
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
        self.timeout.setSuffix(tr(" 秒", " sec"))
        for label, field in (
            (tr("服务类型", "Provider"), self.provider),
            (tr("接收方名称", "Recipient name"), self.recipient),
            (tr("服务地址", "Service URL"), self.endpoint),
            (tr("模型", "Model"), self.model),
            (tr("API 密钥", "API key"), self.key),
            (tr("超时", "Timeout"), self.timeout),
        ):
            form.addRow(label, field)
        self._key_label = form.labelForField(self.key)

        self.permission = QPushButton()
        self.permission.setAccessibleName(permission)
        self.permission.clicked.connect(self._change_permission)
        body.addWidget(self.permission)
        self.permission.setVisible(preset_provider is None)
        self._signals_permission = None
        if role == VISION:
            self._signals_permission = QPushButton()
            self._signals_permission.setObjectName("settings-route-vision-signals")
            self._signals_permission.clicked.connect(self._change_vision_signals_permission)
            self._signals_permission.setVisible(preset_provider is None)
            body.addWidget(self._signals_permission)
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
            recipient, endpoint, model, timeout = bailian_display_name(), BAILIAN_BASE_URL, BAILIAN_MODEL, 90
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
        label = tr("百炼 API Key", "Model Studio API key") if provider == BAILIAN else tr("API 密钥", "API key")
        self._key_label.setText(label)
        self.key.setAccessibleName(label)
        self._render_permission()

    def _endpoint_changed(self):
        if self.provider.currentData() == BAILIAN:
            self.key.clear()
        if self.role in {"system2", VISION}:
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
        permission = tr(*_ROLES[self.role][1])
        route = self.snapshot.route
        provider = self.provider.currentData()
        if self.role in {"system2", VISION} and not self._same_recipient(route, provider):
            route = None
        is_local = route is not None and route.provider_kind == "local_ollama"
        self.permission.setEnabled(route is not None and not is_local and self._preset_provider is None)
        if route is not None and self.snapshot.permission:
            self.permission.setText(tr("撤回授权 · {permission}", "Stop sharing {permission}").format(
                permission=permission,
            ))
        else:
            self.permission.setText(tr("授权 · {permission}", "Allow sharing {permission}").format(
                permission=permission,
            ))
        if self._signals_permission is not None:
            self._signals_permission.setEnabled(self.permission.isEnabled() and self.snapshot.permission)
            self._signals_permission.setText(
                tr("撤回近期系统信号授权", "Revoke recent system signals permission") if route and self.snapshot.vision_signals_permission
                else tr("升级授权 · 同时发送近期系统信号", "Allow sharing recent system signals too")
            )
        status = tr(
            "保存云端接收方后可单独授权发送内容。",
            "After saving the cloud recipient, you can separately authorize what to send.",
        )
        if is_local:
            status = tr("本地模型：{name}\n{url}", "Local model: {name}\n{url}").format(
                name=route.display_name, url=route.base_url,
            )
        elif self.role == "system2" and route:
            status = tr(
                "接收方：{name}\n{url}\n"
                "模型接收与你的请求相关的聊天、事项和结构化活动状态。"
                "活动摘要需单独授权。文档名共享还需要开启相应来源的权限。撤回只影响后续发送。",
                "Recipient: {name}\n{url}\n"
                "The model receives chats, tasks, and structured activity status related to your request. "
                "Activity summaries require separate permission. Sharing document names also requires permission "
                "for the relevant sources. Revoking permission only affects future sending.",
            ).format(name=route.display_name, url=route.base_url)
        elif self.role == "system2":
            status = tr(
                "保存服务和模型后验证文本与工具调用能力。活动摘要分享默认关闭。",
                "Save the service and model before verifying text and tool calling. Activity summary sharing starts off.",
            )
        elif route:
            status = tr(
                "当前接收方：{name}\n{url}\n更改授权适用于以后发送。已发送的信息无法撤回。",
                "Current recipient: {name}\n{url}\nPermission changes apply to future sending. "
                "Previously sent information cannot be recalled.",
            ).format(name=route.display_name, url=route.base_url)
            if self.role == VISION:
                status += "\n" + tr(
                    "截图获准后，可为同一接收方追加近期系统信号授权。",
                    "Once screenshots are authorized, you can also authorize recent system signals for the same recipient.",
                )
        if provider == BAILIAN:
            if route is not None:
                self.key.setPlaceholderText(tr("留空保留此地址的 Key", "Leave blank to keep this URL’s key"))
            else:
                self.key.setPlaceholderText(tr("输入百炼 API Key", "Enter a Model Studio API key"))
            status = tr(
                "使用与服务地址同地域的百炼 API Key。默认地址为华北2（北京），"
                "也可填写业务空间专属的 OpenAI 兼容地址。",
                "Use a Model Studio API key from the same region as the service URL. The default URL uses "
                "China North 2 (Beijing). You can also enter your workspace’s OpenAI-compatible URL.",
            ) + "\n" + status
        else:
            self.key.setPlaceholderText(tr("输入新密钥", "Enter a new key"))
        self.help.setText(status)

    def _confirm_permission(self, title, message):
        prompt = QMessageBox(self)
        prompt.setWindowTitle(title)
        prompt.setTextFormat(self.help.textFormat())
        prompt.setText(message)
        prompt.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        prompt.setDefaultButton(QMessageBox.StandardButton.No)
        prompt.button(QMessageBox.StandardButton.Yes).setText(tr("授权", "Allow"))
        prompt.button(QMessageBox.StandardButton.No).setText(tr("取消", "Cancel"))
        return prompt.exec() == QMessageBox.StandardButton.Yes

    def _change_permission(self):
        route = self.snapshot.route
        if not self.permission.isEnabled() or route is None:
            return
        allowed = not self.snapshot.permission
        if allowed:
            name, data = (tr(*labels) for labels in _ROLES[self.role])
            message = tr(
                "允许向 {name} ({url}) {data}？",
                "Allow Mochi to share {data} with {name} ({url})?",
            ).format(name=route.display_name, url=route.base_url, data=data)
            if self.role == "system2":
                message += tr("\n文档名按「文档名权限」中的云端来源设置共享。", "\nDocument name sharing follows the cloud sources in Document name permissions.")
            elif self.role == VISION:
                try:
                    prefs = SettingsStore.read_at(self.routes.path, user_scope=self.routes.user_scope)
                except (OSError, ValueError, sqlite3.Error):
                    self.error.setText(tr("无法读取当前截图范围，请重新打开设置后再操作。", "Could not read the current screenshot scope. Reopen Settings and try again."))
                    return
                if prefs.observation_screen_scope == "all_displays_unfiltered":
                    message += "\n" + raw_screen_scope_notice()
            if not self._confirm_permission(tr("{role}授权", "{role} permission").format(role=name), message):
                return
        try:
            self.snapshot = self.routes.set_permission(self.role, allowed, expected=self.snapshot)
        except (ValueError, RuntimeError, sqlite3.Error):
            if self.role == "system2":
                self.error.setText(tr("授权状态已变化，请重新打开设置后再操作。", "Permissions have changed. Reopen Settings and try again."))
            else:
                self.snapshot = self.routes.snapshot(self.role)
                self.error.setText(tr("授权状态已变化，请核对接收方后再操作。", "Permissions have changed. Check the recipient and try again."))
        else:
            self.error.clear()
        self._render_permission()

    def _change_vision_signals_permission(self):
        route = self.snapshot.route
        if self._signals_permission is None or not self._signals_permission.isEnabled() or route is None:
            return
        allowed = not self.snapshot.vision_signals_permission
        if allowed and not self._confirm_permission(
            tr("近期系统信号授权", "Recent system signals permission"),
            tr(
                "允许向 {name} ({url}) 随截图发送近期系统信号？\n"
                "可发送采样时间、前台停留和切换、锁屏与近期输入状态，以及上一帧的场景类别和界面类型。",
                "Allow Mochi to send recent system signals with screenshots to {name} ({url})?\n"
                "Signals can include sampling time, time spent in the foreground and app switches, lock-screen and "
                "recent input status, and the previous frame’s scene category and interface type.",
            ).format(name=route.display_name, url=route.base_url),
        ):
            return
        try:
            self.snapshot = self.routes.set_vision_signals_permission(allowed, expected=self.snapshot)
        except (ValueError, RuntimeError, sqlite3.Error):
            self.snapshot = self.routes.snapshot(VISION)
            self.error.setText(tr("授权状态已变化，请核对接收方后再操作。", "Permissions have changed. Check the recipient and try again."))
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
                    self.error.setText(tr("请输入此服务地址对应的百炼 API Key。", "Enter the Model Studio API key for this service URL."))
                    self.key.setFocus()
                    return
                default_name = bailian_display_name() if provider == BAILIAN else _ROUTE_NAMES.get(provider, "")
                route = CloudRoute(
                    provider_kind=provider, base_url=endpoint,
                    model=self.model.text().strip(),
                    display_name=self.recipient.text().strip() or default_name,
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
            self.error.setText(tr("保存路由失败，请检查服务地址、模型与系统凭据权限。", "Could not save the route. Check the service URL, model, and access to system credentials."))
            return
        self.accept()
