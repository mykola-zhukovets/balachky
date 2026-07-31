"""Регресії продуктивності рендеру Settings."""
import os
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop.pages.settings import SettingsPage
from whisper_core.config import Config


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _DummySignal:
    def connect(self, _slot):
        pass

    def __call__(self, *_args, **_kwargs):
        return None


class _InstallerController:
    def __init__(self):
        self.cfg = Config()
        self.window = None
        self._ctx_matcher = None

    def get_models_info(self):
        return {}

    def update_state(self):
        return "1.0.0", "1.1.0", "https://example.invalid/release", True

    def delivery_state(self):
        return "https://example.invalid/setup.exe", "a" * 64, "notes"

    def list_voice_memories(self):
        return []

    def list_meeting_screen_monitors(self):
        return []

    def list_meetings(self):
        return []

    def list_recordings(self):
        return []

    def corpus_count(self):
        return 0

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _DummySignal()


def test_installer_hash_during_settings_render_runs_only_in_worker(qapp):
    caller_thread = threading.get_ident()
    integrity_threads = []
    integrity_checked = threading.Event()

    def check_installer(*_args, **_kwargs):
        integrity_threads.append(threading.get_ident())
        integrity_checked.set()
        return Path("C:/cache/setup.exe")

    with mock.patch(
            "whisper_core.updater.installer_ready",
            side_effect=check_installer):
        page = SettingsPage(_InstallerController())
        assert caller_thread not in integrity_threads
        assert integrity_checked.wait(2), "Settings worker не перевірив інсталятор"

    assert integrity_threads
    assert caller_thread not in integrity_threads
    page.close()
    page.deleteLater()
    qapp.processEvents()


def test_stale_settings_worker_does_not_override_downloaded_installer(qapp):
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_returned = threading.Event()
    downloaded = "C:/cache/setup.exe"

    def delayed_missing(*_args, **_kwargs):
        worker_started.set()
        release_worker.wait(2)
        worker_returned.set()
        return None

    with mock.patch(
            "whisper_core.updater.installer_ready",
            side_effect=delayed_missing):
        page = SettingsPage(_InstallerController())
        assert worker_started.wait(2), "Settings worker не стартував"
        page._on_downloaded(downloaded)
        release_worker.set()
        assert worker_returned.wait(2), "Settings worker не завершився"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

    assert page._upd_ready_path == downloaded
    assert not page._upd_install.isHidden()
    assert page._upd_get.isHidden()
    page.close()
    page.deleteLater()
    qapp.processEvents()
