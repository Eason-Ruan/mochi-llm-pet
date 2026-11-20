"""One desktop pet entry and one shared workbench; no old Application/Brain loop."""
from __future__ import annotations
import copy
from pathlib import Path
import sys
from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QGuiApplication
from PyQt6.QtWidgets import QApplication, QMenu, QWidget
from deskpet.m0.composition import load_adapters
from deskpet.m0.gui import Workbench
from deskpet.sprite.player import AnimationPlayer
from deskpet.sprite.registry import build


class DesktopPet(QWidget):
    open_requested=pyqtSignal(str)
    quit_requested=pyqtSignal()

    def __init__(self):
        super().__init__(None,Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(150,132)
        self.setToolTip('单击打开提醒 · 拖动移动 · 右键选择功能或退出')
        self.player=AnimationPlayer(build(Path(__file__).resolve().parents[2]/'assets'))
        self.pending_count=0
        self._press=None
        self._moved=False
        self.timer=QTimer(self); self.timer.setInterval(100)
        self.timer.timeout.connect(self._animate); self.timer.start()
        screen=QApplication.primaryScreen()
        if screen:
            area=screen.availableGeometry()
            self.move(area.right()-self.width()-24,area.bottom()-self.height()-24)

    def _animate(self):
        self.player.update(.1); self.update()

    def render_snapshot(self,snapshot,count):
        self.pending_count=count
        focusing=any(s['state']=='focusing' for s in snapshot.get('sessions',[]))
        self.player.set_state('sit' if focusing else 'idle')
        self.update()

    def paintEvent(self,event):
        painter=QPainter(self)
        frame=self.player.current_frame()
        if frame is not None and not frame.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,False)
            painter.drawPixmap(27,0,frame.scaled(96,96,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.FastTransformation))
        painter.setBrush(QColor('#eef5e9')); painter.setPen(QColor('#c6d6bd'))
        painter.drawRoundedRect(2,98,146,30,10,10)
        painter.setPen(QColor('#254632'))
        painter.drawText(2,98,146,30,Qt.AlignmentFlag.AlignCenter,
                         f'{self.pending_count} 条待处理提醒' if self.pending_count else 'Mochi · 打开工作面板')

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self._press=event.globalPosition().toPoint()-self.pos()
            self._moved=False

    def mouseMoveEvent(self,event):
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            target=event.globalPosition().toPoint()-self._press
            if (target-self.pos()).manhattanLength()>3: self._moved=True
            if self._moved:
                area=self.screen().availableGeometry()
                self.move(max(area.left(),min(target.x(),area.right()-self.width()+1)),
                          max(area.top(),min(target.y(),area.bottom()-self.height()+1)))

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and self._press is not None:
            self._press=None
            if not self._moved: self.open_requested.emit('reminders')

    def contextMenuEvent(self,event):
        menu=QMenu(self)
        for title,key in (('提醒事项','reminders'),('专注与回顾','work'),('状态与纠正','state')):
            action=menu.addAction(title)
            action.triggered.connect(lambda checked=False,k=key:self.open_requested.emit(k))
        menu.addSeparator(); menu.addAction('退出工作伙伴',self.quit_requested.emit)
        menu.exec(event.globalPos())

    def closeEvent(self,event):
        self.timer.stop(); super().closeEvent(event)


class DesktopWorkbench(Workbench):
    finished=pyqtSignal()
    exit_requested=False

    def closeEvent(self,event):
        if not self.exit_requested and not self._finishing:
            self.hide(); event.ignore(); return
        super().closeEvent(event)
        if event.isAccepted() and self._closing: self.finished.emit()


class DesktopSession:
    def __init__(self,app,args):
        self.app=app
        configured=load_adapters(getattr(args,'adapters',None))
        configured.pop('gui',None)
        configured.setdefault('ui_surface','deskpet.c_ui.surface:create_surface')
        options=copy.copy(args); options.adapters=configured
        self.window=DesktopWorkbench(options)
        self.window.setWindowTitle('Mochi · 工作伙伴')
        self.window.resize(1180,820)
        self.window.log.hide(); self.window.status.hide()
        self.pet=DesktopPet()
        self.pet.open_requested.connect(self.open)
        self.pet.quit_requested.connect(self.quit)
        self.window.finished.connect(self._finished)
        self.window.worker.snapshot_ready.connect(self._snapshot)
        self.window.menuBar().addAction('收起到桌宠',self.window.hide)
        diagnostics=self.window.menuBar().addAction('显示联调日志')
        diagnostics.setCheckable(True)
        diagnostics.toggled.connect(self.window.log.setVisible)
        diagnostics.toggled.connect(self.window.status.setVisible)
        self.pet.show()
        self.window.show()
        QTimer.singleShot(0,self.window.activate_for_launch)

    def _snapshot(self,snapshot):
        self.pet.render_snapshot(snapshot,len(self.window.ui_surface.cards) if self.window.ui_surface else 0)

    def open(self,section='reminders'):
        w=self.window
        if w._closing: return
        w.tabs.setCurrentWidget(w.ui_surface)
        w.ui_surface.open_section(section)
        w.showNormal(); w.raise_(); w.activateWindow()
        if sys.platform=='darwin' and QGuiApplication.platformName() not in ('offscreen','minimal'):
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    def quit(self):
        self.window.exit_requested=True
        self.window.close()

    def _finished(self):
        self.pet.close(); self.app.quit()


def run_gui(args):
    app=QApplication.instance() or QApplication(['mochi-workspace'])
    app.setQuitOnLastWindowClosed(False)
    session=DesktopSession(app,args)
    app.exec()
    if getattr(args,'auto_smoke',False):
        return 0 if session.window.report and session.window.report['status']=='passed' else 1
    return 0
