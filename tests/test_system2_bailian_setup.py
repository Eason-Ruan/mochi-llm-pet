import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_standalone_key_entry_saves_to_the_desktop_profile_and_verifies(tmp_path):
    pytest.importorskip("PyQt6.QtWidgets")
    script = r'''
import sys
from pathlib import Path
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit
from deskpet.__main__ import main
from deskpet.c_ui import settings as ui
from deskpet.observation_v2 import openai_cloud
from deskpet.observation_v2.routes import BAILIAN_BASE_URL, BAILIAN_MODEL, SYSTEM2
from deskpet.settings import SettingsStore

profile = Path(sys.argv[1])
app = QApplication([])
states = []
saved = []
openai_cloud.store_route_credential = lambda route, key: saved.append((route.credential_ref, key))

def probe(routes, role, finished):
    chosen = routes.snapshot(role)
    assert (role, chosen.route.provider_kind, chosen.route.base_url, chosen.route.model) == (
        SYSTEM2, 'bailian', BAILIAN_BASE_URL, BAILIAN_MODEL)
    assert saved == [(chosen.route.credential_ref, 'synthetic-key-only')]
    states.append('verified')
    routes.record_result(chosen, probe=True)
    QTimer.singleShot(0, lambda: finished.emit(role))
    QTimer.singleShot(25, lambda: QApplication.activeModalWidget().accept())

ui.verify_route_in_background = probe

def save():
    editor = QApplication.activeModalWidget()
    assert isinstance(editor, ui._RouteEditor)
    assert editor.key.echoMode() == QLineEdit.EchoMode.Password
    assert editor.key.text() == ''
    editor.key.setText('synthetic-key-only')
    box = editor.findChild(QDialogButtonBox)
    box.button(QDialogButtonBox.StandardButton.Save).click()

QTimer.singleShot(0, save)
assert main(['--configure-system2', '--data-dir', str(profile)]) == 0
assert states == ['verified']
assert SettingsStore(profile / 'business.sqlite3').routes.snapshot(SYSTEM2).verified
assert not (profile / 'observation.sqlite3').exists()
assert b'synthetic-key-only' not in (profile / 'business.sqlite3').read_bytes()
'''
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, check=True, timeout=20,
    )
