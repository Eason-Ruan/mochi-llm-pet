"""Versioned proposal controls for the desktop conversation."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
)

from deskpet.i18n import tr


class ProposalCard(QFrame):
    def __init__(self, resolve, reply):
        super().__init__()
        self.setObjectName("refine-chat-message")
        self._resolve = resolve
        self._reply = reply
        self._proposals = {}
        self._selected = None
        self._inflight = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 7, 8, 7)

    @property
    def has_proposals(self):
        return bool(self._proposals)

    def refresh(self, proposals):
        current = {proposal["id"]: proposal for proposal in proposals}
        if current == self._proposals:
            return False
        self._proposals = current
        self._selected = self._selected if self._selected in current else next(iter(current), None)
        self._render()
        return True

    def select(self, proposal_id):
        if proposal_id in self._proposals:
            self._selected = proposal_id
            self._render()

    def complete(self, event_id):
        if event_id == self._inflight:
            self._inflight = None
            self._render()

    def _send(self, proposal, answer, amendments=None):
        if self._inflight is None:
            self._inflight = self._resolve(proposal, answer, amendments)
            self._render()

    def _amend(self, proposal):
        function = proposal["function"]
        args = proposal["args"].get("patch", {}) if function == "item.update" else proposal["args"]
        duration_label = tr("总时长", "Total duration")
        titles = {"title": tr("标题", "Title"), "description": tr("说明", "Description"),
                  "planned_duration_s": duration_label, "total_s": duration_label, "add_s": tr("增加时间", "Add time")}
        if function in {"phase.start", "item.create", "item.update"}:
            fields = {key: args.get(key) or "" for key in ("title", "description")}
            if args.get("planned_duration_s") is not None:
                fields["planned_duration_s"] = args["planned_duration_s"]
        elif function == "phase.adjust":
            fields = {key: args[key] for key in ("total_s", "add_s") if args.get(key) is not None}
        else:
            fields = {}
        if not fields:
            self._reply(proposal)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("修改提议", "Edit proposal"))
        layout = QFormLayout(dialog)
        editors = {}
        for key, value in fields.items():
            if key in {"title", "description"}:
                field = QLineEdit(value)
                field.setMaxLength(500 if key == "title" else 4000)
            else:
                field = QSpinBox()
                field.setRange(1, 86_400)
                field.setSuffix(tr(" 秒", " sec"))
                field.setValue(round(value))
            field.setObjectName(f"proposal-edit-{key}")
            layout.addRow(titles[key], field)
            editors[key] = field
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("修改并接受", "Save and accept"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("返回", "Back"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updates = {}
            for key, field in editors.items():
                current = field.text().strip() if isinstance(field, QLineEdit) else field.value()
                if current != fields[key]:
                    updates[key] = current
            if function == "item.update" and updates:
                updates = {"patch": updates}
            self._send(proposal, "accept", updates)

    def _render(self):
        while self._layout.count():
            widget = self._layout.takeAt(0).widget()
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        proposal = self._proposals.get(self._selected)
        if proposal is None:
            return
        heading = tr("待确认提议", "Pending proposal")
        ids = tuple(self._proposals)
        if len(ids) > 1:
            heading += f" {ids.index(self._selected) + 1}/{len(ids)}"
        self._layout.addWidget(QLabel(heading))
        message = QLabel(proposal["display_text"])
        message.setObjectName("refine-proposal-text")
        message.setTextFormat(Qt.TextFormat.PlainText)
        message.setWordWrap(True)
        self._layout.addWidget(message)
        controls = QFrame()
        actions = QHBoxLayout(controls)
        actions.setContentsMargins(0, 0, 0, 0)
        for name, label, run in (
            ("accept", tr("接受", "Accept"), lambda: self._send(proposal, "accept")),
            ("reject", tr("拒绝", "Reject"), lambda: self._send(proposal, "reject")),
            ("amend", tr("修改", "Edit"), lambda: self._amend(proposal)),
            ("reply", tr("回复", "Reply"), lambda: self._reply(proposal)),
        ):
            button = QPushButton(label)
            button.setObjectName(f"refine-proposal-{name}")
            button.setEnabled(self._inflight is None)
            button.clicked.connect(run)
            actions.addWidget(button)
        if len(ids) > 1:
            button = QPushButton(tr("下一条", "Next"))
            button.setObjectName("refine-proposal-next")
            button.clicked.connect(lambda: self.select(ids[(ids.index(self._selected) + 1) % len(ids)]))
            actions.addWidget(button)
        self._layout.addWidget(controls)
