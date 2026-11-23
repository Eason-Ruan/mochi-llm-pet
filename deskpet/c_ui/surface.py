"""C workspace: separate editing pages and an always-visible reminder inbox."""
from __future__ import annotations
from uuid import uuid4
from datetime import datetime
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QHBoxLayout, QScrollArea, QTabWidget, QVBoxLayout, QWidget
from deskpet.contracts import UiAction
from .work_panel import WorkPanel, label
from .reminder import ReminderCard, ReminderPanel
from .state_panel import StatePanel
from .inbox import ReminderInbox


class ScrollRouter:
    """Explicit navigation helper for user actions and diagnostic scripts."""
    def __init__(self, workspace): self.workspace=workspace
    def ensureWidgetVisible(self, widget):
        for key,scroll in self.workspace.pages.items():
            if scroll.widget() is widget or scroll.widget().isAncestorOf(widget):
                self.workspace.open_section(key)
                scroll.ensureWidgetVisible(widget)
                return


class CWorkspace(QWidget):
    def __init__(self, *, context, parent=None):
        super().__init__(parent)
        self.setObjectName('c-workspace')
        self.context=context
        self.snapshot={}
        self.cards={}
        self.setStyleSheet('''
QWidget#c-workspace { background: #f6f5f0; color: #243b32; }
QWidget#c-workspace QGroupBox { border: 1px solid #cbd6cd; border-radius: 8px; margin-top: 14px; padding: 12px 8px 8px; }
QWidget#c-workspace QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; }
QWidget#c-workspace QLineEdit, QWidget#c-workspace QComboBox, QWidget#c-workspace QSpinBox { min-height: 26px; }
QWidget#c-workspace QPushButton { min-height: 28px; padding: 3px 8px; }
''')
        outer=QVBoxLayout(self)
        self.heading=label('Mochi · 工作伙伴')
        self.heading.setStyleSheet('font-size: 22px; font-weight: 600;')
        outer.addWidget(self.heading)
        self.connection=label('等待服务连接…')
        outer.addWidget(self.connection)
        row=QHBoxLayout(); outer.addLayout(row,1)
        self.sections=QTabWidget()
        row.addWidget(self.sections,1)
        self.reminder_panel=ReminderPanel(submit=self._submit)
        self.work_panel=WorkPanel(submit=self._submit)
        self.state_panel=StatePanel(submit=context.submit_action)
        self.pages={}
        self.indices={}
        for key,title,panel in (('reminders','提醒事项',self.reminder_panel),('work','专注与回顾',self.work_panel),('state','状态与纠正',self.state_panel)):
            page=QWidget(); layout=QVBoxLayout(page); layout.addWidget(panel); layout.addStretch()
            scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.Shape.NoFrame); scroll.setWidget(page)
            self.pages[key]=scroll
            self.indices[key]=self.sections.addTab(scroll,title)
        self.inbox=ReminderInbox()
        row.addWidget(self.inbox)
        self.empty_reminders=self.inbox.empty
        self.scroll=ScrollRouter(self)
        self.reminder_panel.actions_changed.connect(self._refresh_cards)
        self.reminder_panel.edit_opened.connect(self._scroll_to_reminder_editor)
        self.work_panel.review_opened.connect(self._scroll_to_review)
        self.goal=self.work_panel.goal; self.start_button=self.work_panel.start_button
        self.session_summary=self.work_panel.summary; self.result=self.work_panel.result
        self.task_summary=label('尚无任务'); outer.addWidget(self.task_summary)
        context.result_received.connect(self.work_panel.receive_result)
        context.result_received.connect(self.reminder_panel.receive_result)
        context.result_received.connect(self.state_panel.renderer.receive_result)

    def open_section(self,key):
        self.sections.setCurrentIndex(self.indices[key])

    def _scroll_to_review(self):
        QTimer.singleShot(0,lambda:self.scroll.ensureWidgetVisible(self.work_panel.confirm_end))

    def _scroll_to_reminder_editor(self):
        QTimer.singleShot(0,lambda:self.scroll.ensureWidgetVisible(self.reminder_panel.content))

    def _submit(self, action_id, *, task_id=None, version=None, payload=None):
        return self.context.submit_action(UiAction(event_id=f'c-ui-{uuid4().hex}',action_id=action_id,
            task_id=task_id,expected_task_version=version,surface_revision=max(1,version or 1),payload=payload or {}))

    def render_snapshot(self,snapshot):
        self.snapshot=snapshot
        self.state_panel.render_snapshot(snapshot)
        self.reminder_panel.render_snapshot(snapshot)
        self.work_panel.render_snapshot(snapshot)
        if not snapshot: return
        self.connection.setText('演示模式 · 使用「任务与工作段」页的快进按钮推进模拟时间' if snapshot.get('clock')=='virtual' else '真实时间模式 · 随系统时间自动计时，无需快进')
        self.task_summary.setText(f"事项 {len(snapshot['tasks'])} 项 · 工作段 {len(snapshot['sessions'])} 段 · 到期提醒 {len(self.cards)} 条")

    def show_reminder(self,command):
        if command.delivery_id in self.cards: return
        view=self.reminder_panel.view(command.task_id)
        if view and (view['version']>command.task_version or view['status'] in ('completed','cancelled')): return
        if self.snapshot and command.expires_at<=datetime.fromisoformat(self.snapshot['now']): return
        card=ReminderCard(command,context=self.context,controller=self.reminder_panel)
        self.cards[command.delivery_id]=card
        self.inbox.add_card(card)
        # Never change the editing page, scroll position or keyboard focus here.

    def dismiss_reminder(self,task_id):
        for key,card in list(self.cards.items()):
            if card.command.task_id==task_id:
                self.inbox.remove_card(key)
                del self.cards[key]

    def _refresh_cards(self):
        for card in list(self.cards.values()):
            view=self.reminder_panel.view(card.command.task_id)
            expired=(not card._reported and self.snapshot and card.command.expires_at<=datetime.fromisoformat(self.snapshot['now']))
            if expired or (view and (view['version']>card.command.task_version or view['status'] in ('completed','cancelled'))):
                self.dismiss_reminder(card.command.task_id)
            else: card.refresh()


def create_surface(*,context,parent): return CWorkspace(context=context,parent=parent)
