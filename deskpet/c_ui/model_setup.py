"""The desktop model connections dialog shared by both GUI entry points."""
from __future__ import annotations

import sqlite3
from threading import Thread

from PyQt6.QtCore import QSignalBlocker, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QVBoxLayout, QWidget

from deskpet.model_setup import (
    MODEL_ROLES, ModelInput, can_reuse_api_key, configure_models, model_input_defaults,
    model_setup_defaults, verify_models,
)
from deskpet.observation_v2.routes import SYSTEM2, VISION, _normalize_base_url
from deskpet.settings import SettingsStore

from .refine_style import MUTED, _CONTROL_STYLE, _card, _text


_LABELS = {
    SYSTEM2: ("System 2 · 对话与动作", "向此接收方分享已发布的活动摘要"),
    VISION: ("Observation · 屏幕图像理解", "授权此接收方接收所选范围内获准的截图"),
}
_PROBE_STATUS = {
    "verified": "连接已验证。",
    "permission_required": "连接已保存。授权截图后可验证识图能力。",
    "failed": "连接已保存，验证未通过。请核对 API Base URL、API Key 和模型权限。",
}
RAW_SCREEN_SCOPE_NOTICE = (
    "当前截图范围为所有显示器的原始画面。获准并验证的阿里云百炼通义千问可接收画面，"
    "其中可能包含排除列表中的应用、域名和无痕窗口。"
)


def _endpoint_identity(base_url):
    try:
        return _normalize_base_url(base_url.strip())
    except ValueError:
        return None


class _ModelRoleForm:
    def __init__(self, role, snapshot, *, unfiltered_desktop=False):
        self.snapshot = snapshot
        self._permission = None
        label, permission = _LABELS[role]
        url, model = model_input_defaults(snapshot)
        self._endpoint = _endpoint_identity(url)
        self.card, body = _card(spacing=6)
        self.selected = QCheckBox(label)
        self.selected.setObjectName(f"model-setup-role-{role}")
        self.selected.setChecked(True)
        body.addWidget(self.selected)

        self.fields = QWidget()
        fields = QVBoxLayout(self.fields)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(6)
        form = QFormLayout()
        self.endpoint = QLineEdit(url)
        self.endpoint.setObjectName(f"model-setup-{role}-base-url")
        self.model = QLineEdit(model)
        self.model.setObjectName(f"model-setup-{role}-model")
        self.key = QLineEdit()
        self.key.setObjectName(f"model-setup-{role}-api-key")
        self.key.setAccessibleName(f"{label} API Key")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API Base URL", self.endpoint)
        form.addRow("模型", self.model)
        form.addRow("API Key", self.key)
        fields.addLayout(form)

        self.permission = QCheckBox(permission)
        self.permission.setObjectName(f"model-setup-permission-{role}")
        fields.addWidget(self.permission)
        if unfiltered_desktop:
            scope_notice = _text(RAW_SCREEN_SCOPE_NOTICE, size=11, color=MUTED, wrap=True)
            scope_notice.setObjectName("model-setup-raw-screen-scope")
            fields.addWidget(scope_notice)
        self.status = _text(size=11, color=MUTED, wrap=True)
        self.status.setObjectName(f"model-setup-status-{role}")
        body.addWidget(self.fields)
        body.addWidget(self.status)

        self.selected.toggled.connect(self._render)
        self.endpoint.textChanged.connect(self._endpoint_changed)
        self.model.textChanged.connect(self._model_changed)
        self.key.textChanged.connect(self._render)
        self.permission.toggled.connect(self._permission_changed)
        self._render()

    def _endpoint_changed(self):
        endpoint = _endpoint_identity(self.endpoint.text())
        if endpoint != self._endpoint:
            self._endpoint = endpoint
            self._permission = None
            self.key.clear()
        self._render()

    def _model_changed(self):
        self._permission = None
        self._render()

    def _permission_changed(self, allowed):
        self._permission = allowed

    def _render(self):
        route = self.snapshot.route
        same_endpoint = route is not None and route.base_url == self._endpoint
        key_entered = bool(self.key.text().strip())
        keep_route = same_endpoint and route.model == self.model.text().strip() and not key_entered
        same_local_recipient = same_endpoint and not route.is_cloud and not key_entered
        enabled = self.selected.isChecked()
        self.fields.setEnabled(enabled)
        self.permission.setEnabled(not same_local_recipient)
        with QSignalBlocker(self.permission):
            allowed = keep_route and self.snapshot.permission if self._permission is None else self._permission
            self.permission.setChecked(enabled and not same_local_recipient and allowed)

        if can_reuse_api_key(self.snapshot, self.endpoint.text()):
            placeholder = "留空保留此地址的 API Key" if same_endpoint and route.credential_ref else "此连接可留空"
        else:
            placeholder = "输入此地址对应的 API Key"
        self.key.setPlaceholderText(placeholder)

        if not enabled or route is None:
            status = ""
        elif not keep_route:
            status = f"当前连接为 {route.display_name} · {route.model}。"
        elif self.snapshot.role == VISION and route.is_cloud and not self.snapshot.permission:
            status = _PROBE_STATUS["permission_required"]
        else:
            status = "当前连接已验证。" if self.snapshot.verified else "当前连接已保存。"
        self.set_status(status)

    def set_status(self, message):
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def model_input(self):
        return ModelInput(
            base_url=self.endpoint.text().strip(), model=self.model.text().strip(),
            api_key=self.key.text().strip(), permission=self._permission if self.permission.isEnabled() else None,
        )

    def mark_saved(self, snapshot):
        self.selected.setEnabled(False)
        with QSignalBlocker(self.key):
            self.key.clear()
        self.fields.setEnabled(False)
        if snapshot is None:
            self.set_status("")
        elif snapshot.role == VISION and snapshot.route.is_cloud and not snapshot.permission:
            self.set_status(_PROBE_STATUS["permission_required"])
        else:
            self.set_status("正在使用合成数据验证…")


class ModelSetupDialog(QDialog):
    _verification_finished = pyqtSignal(object)

    def __init__(self, routes, parent=None):
        super().__init__(parent)
        self.routes = routes
        self._snapshots = model_setup_defaults(routes)
        self._saved = False
        self.setObjectName("model-setup")
        self.setWindowTitle("Mochi · 模型连接")
        self.setStyleSheet(_CONTROL_STYLE)
        self.setMinimumWidth(515)
        body = QVBoxLayout(self)
        body.addWidget(_text("勾选要保存和验证的模型连接。", size=11, color=MUTED, wrap=True))

        preferences = SettingsStore.read_at(routes.path, user_scope=routes.user_scope)
        self.forms = {}
        for role in MODEL_ROLES:
            form = _ModelRoleForm(
                role, self._snapshots[role],
                unfiltered_desktop=role == VISION and preferences.observation_screen_scope == "all_displays_unfiltered",
            )
            self.forms[role] = form
            self.finished.connect(form.key.clear)
            form.selected.toggled.connect(self._update_save_button)
            body.addWidget(form.card)

        self.feedback = _text(size=11, color=MUTED, wrap=True)
        self.feedback.setObjectName("model-setup-feedback")
        self.feedback.hide()
        body.addWidget(self.feedback)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self._save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self._save_button.setText("保存并验证")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        body.addWidget(self.buttons)
        self._verification_finished.connect(self._show_verification)

    def _update_save_button(self):
        self._save_button.setEnabled(any(form.selected.isChecked() for form in self.forms.values()))

    def _show_feedback(self, message):
        self.feedback.setText(message)
        self.feedback.show()

    def _save(self):
        if self._saved:
            return
        updates = {role: form.model_input() for role, form in self.forms.items() if form.selected.isChecked()}
        if not updates:
            return
        try:
            configured = configure_models(self.routes, updates, expected=self._snapshots)
        except ValueError as exc:
            self._show_feedback(str(exc))
            return
        except (RuntimeError, sqlite3.Error):
            self._show_feedback("保存失败。请检查系统凭据权限，或重新打开此窗口核对连接设置。")
            return

        self._saved = True
        for role, form in self.forms.items():
            form.mark_saved(configured.get(role))
        self._show_feedback("连接已保存。请等待验证完成。")
        self.buttons.setStandardButtons(QDialogButtonBox.StandardButton.Close)
        self.buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        Thread(target=self._verify, args=(configured,), daemon=True).start()

    def _verify(self, configured):
        try:
            statuses = verify_models(self.routes, configured)
        except (ValueError, RuntimeError, sqlite3.Error):
            statuses = dict.fromkeys(configured, "failed")
        try:
            self._verification_finished.emit(statuses)
        except RuntimeError:
            # The dialog can close before the provider responds.
            pass

    def _show_verification(self, statuses):
        for role, status in statuses.items():
            self.forms[role].set_status(_PROBE_STATUS[status])
        self.feedback.hide()
