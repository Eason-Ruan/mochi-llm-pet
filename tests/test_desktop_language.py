import sys
from types import SimpleNamespace

import pytest

from deskpet import i18n
from deskpet.__main__ import main
from deskpet.desktop_profile import read_desktop_language, save_desktop_language


def test_language_preference_is_per_profile_and_respects_other_preferences(tmp_path):
    qt = pytest.importorskip("PyQt6.QtCore")
    profile = tmp_path / "work"
    assert read_desktop_language(profile) == "zh"
    assert not (profile / "window.ini").exists()

    settings = qt.QSettings(str(profile / "window.ini"), qt.QSettings.Format.IniFormat)
    settings.setValue("chatHeight", 295)
    settings.sync()
    save_desktop_language(profile, "en")
    settings.sync()

    assert read_desktop_language(profile) == "en"
    assert settings.value("chatHeight", type=int) == 295
    assert read_desktop_language(tmp_path / "personal") == "zh"
    settings.setValue("uiLanguage", "unrecognized")
    settings.sync()
    assert read_desktop_language(profile) == "zh"


def test_language_is_selected_before_desktop_or_model_setup(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtCore")
    monkeypatch.setattr(i18n, "_language", "zh")
    launches = []

    def record(data_dir, **options):
        launches.append((data_dir, i18n.language(), i18n.tr("专注", "Focus")))
        return 0

    monkeypatch.setitem(sys.modules, "deskpet.c_ui.refine", SimpleNamespace(
        run_desktop=record, configure_models=record,
    ))
    save_desktop_language(tmp_path, "en")
    args = ["--data-dir", str(tmp_path)]

    assert main(args) == 0
    assert main([*args, "--language", "zh"]) == 0
    assert main([*args, "--configure-models"]) == 0
    assert read_desktop_language(tmp_path) == "en"
    assert launches == [
        (tmp_path, "en", "Focus"),
        (tmp_path, "zh", "专注"),
        (tmp_path, "en", "Focus"),
    ]
