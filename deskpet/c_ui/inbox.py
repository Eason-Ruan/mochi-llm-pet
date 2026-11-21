"""Arrival-ordered reminders: only the selected card can be visibly painted."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox, QGroupBox, QScrollArea, QStackedWidget, QVBoxLayout
from .work_panel import label


class ReminderInbox(QGroupBox):
    def __init__(self, parent=None):
        super().__init__('到期提醒', parent)
        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        layout=QVBoxLayout(self)
        self.count=label('暂无到期提醒')
        layout.addWidget(self.count)
        self.selector=QComboBox()
        self.selector.setAccessibleName('选择待处理提醒')
        self.selector.currentIndexChanged.connect(self._select)
        layout.addWidget(self.selector)
        self.empty=label('新提醒会在这里出现。输入和正在查看的页面会保留。')
        layout.addWidget(self.empty)
        self.stack=QStackedWidget()
        self.scroll=QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidget(self.stack)
        layout.addWidget(self.scroll)
        self._last_key=None
        self.cards={}
        self.selector.hide()
        self.stack.hide()
        self.scroll.hide()

    def add_card(self, card):
        key=card.command.delivery_id
        if key in self.cards: return
        current=self.selector.currentData()
        self.cards[key]=card
        self.stack.addWidget(card)
        self.selector.blockSignals(True)
        self.selector.addItem(card.command.content, key)
        self.selector.setCurrentIndex(max(0,self.selector.findData(current)))
        self.selector.blockSignals(False)
        self._select()
        self._summary()

    def remove_card(self, key):
        card=self.cards.pop(key,None)
        if card is None: return
        current=self.selector.currentData()
        self.selector.blockSignals(True)
        self.selector.removeItem(self.selector.findData(key))
        if current != key and self.selector.findData(current)>=0:
            self.selector.setCurrentIndex(self.selector.findData(current))
        self.selector.blockSignals(False)
        card.retire()
        self.stack.removeWidget(card)
        card.deleteLater()
        self._select()
        self._summary()

    def _select(self):
        card=self.cards.get(self.selector.currentData())
        if card is not None:
            self.stack.setCurrentWidget(card)
            if self._last_key!=self.selector.currentData():
                self.scroll.verticalScrollBar().setValue(0)
            self._last_key=self.selector.currentData()
            card.update()
        self._summary()

    def _summary(self):
        total=len(self.cards)
        self.count.setText(f'{total} 条待处理 · 按到达顺序排列' if total else '暂无到期提醒')
        self.empty.setVisible(not total)
        self.selector.setVisible(total>1)
        self.stack.setVisible(bool(total))
        self.scroll.setVisible(bool(total))
