"""The SQLite owner thread must release its connection before it stops."""
import os
from pathlib import Path
import subprocess
import sys


def test_sqlite_connection_closed_on_service_thread(tmp_path):
    code = r'''
from types import SimpleNamespace
from PyQt6.QtCore import QMetaObject, QThread, QTimer, Qt
from PyQt6.QtWidgets import QApplication
from deskpet.m0.gui import QtDeliveryBridge, ServiceWorker
app = QApplication([])
bridge = QtDeliveryBridge()
worker = ServiceWorker(SimpleNamespace(profile='scenario', scenario='sqlite-shutdown',
    output=None, adapters={'task_service':'deskpet.task_service:factory'}), bridge)
thread = QThread()
worker.moveToThread(thread)
errors=[]
worker.error.connect(errors.append)
thread.started.connect(worker.start)
def ready():
    assert not worker.env.tasks._closed
    QMetaObject.invokeMethod(worker, 'stop', Qt.ConnectionType.QueuedConnection)
worker.ready.connect(ready)
worker.stopped.connect(app.quit)
QTimer.singleShot(5000, app.quit)
thread.start()
app.exec()
assert thread.wait(3000), 'owner thread did not stop'
assert not errors, errors
assert worker.env.tasks._closed, 'connection remained open after shutdown'
'''
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", code], cwd=root,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", MOCHI_TASK_DB=str(tmp_path / "tasks.sqlite3"), PYTHONPATH=str(root)),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
