"""Unit and UI tests for fronts.desktop.pages.remote (Telegram / Remote access UI)."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from fronts.desktop.pages.remote import RemotePage
from fronts.telegram.service import TelegramRuntimeState
from whisper_core.history import log_history

_APP = None


def get_app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


class DummyController(QObject):
    telegram_state_changed = Signal(object)
    telegram_error = Signal(str)

    def __init__(self, tmp_dir):
        super().__init__()
        self.cfg = SimpleNamespace(
            telegram_enabled=False,
            telegram_user_id=0,
            telegram_chat_id=0,
        )
        self.history_path = Path(tmp_dir) / "history.jsonl"
        self.profile = SimpleNamespace(
            history_path=str(self.history_path),
            memory_enabled=True,
        )
        self._status = {
            "state": TelegramRuntimeState.DISABLED.value,
            "bot_name": "",
            "pairing_url": "",
            "error": "",
        }
        self.verify_token_called_with = None
        self.begin_pairing_called = False
        self.set_enabled_called_with = None
        self.disconnect_called = False

    def telegram_status(self):
        return dict(self._status)

    def telegram_verify_token(self, token):
        self.verify_token_called_with = token

    def telegram_begin_pairing(self):
        self.begin_pairing_called = True
        return "https://t.me/balachky_test_bot?start=secret123"

    def telegram_set_enabled(self, enabled):
        self.set_enabled_called_with = enabled
        self.cfg.telegram_enabled = bool(enabled)

    def telegram_disconnect(self):
        self.disconnect_called = True
        self.cfg.telegram_enabled = False
        self.cfg.telegram_user_id = 0
        self.cfg.telegram_chat_id = 0
        self._status = {
            "state": TelegramRuntimeState.DISABLED.value,
            "bot_name": "",
            "pairing_url": "",
            "error": "",
        }
        return True


class TelegramSettingsUiTests(unittest.TestCase):
    def setUp(self):
        self.app = get_app()
        self.tmp = tempfile.TemporaryDirectory()
        self.controller = DummyController(self.tmp.name)
        self.page = RemotePage(self.controller)

    def tearDown(self):
        self.page.deleteLater()
        self.tmp.cleanup()

    def test_telegram_controls_are_masked_accessible_and_wired(self):
        token_input = self.page.findChild(QLineEdit, "telegramTokenInput")
        connect_btn = self.page.findChild(QPushButton, "telegramConnectButton")
        status_lbl = self.page.findChild(QLabel, "telegramStatusLabel")
        toggle = self.page.findChild(QCheckBox, "telegramEnableToggle")
        pair_btn = self.page.findChild(QPushButton, "telegramPairButton")
        disconnect_btn = self.page.findChild(QPushButton, "telegramDisconnectButton")

        self.assertIsNotNone(token_input)
        self.assertEqual(token_input.echoMode(), QLineEdit.Password)
        self.assertTrue(token_input.accessibleName())
        self.assertTrue(connect_btn.accessibleName())
        self.assertIsNotNone(status_lbl)
        self.assertTrue(toggle.accessibleName())
        self.assertTrue(pair_btn.accessibleName())
        self.assertTrue(disconnect_btn.accessibleName())

    def test_token_input_enables_connect_button_and_calls_verify(self):
        token_input = self.page.findChild(QLineEdit, "telegramTokenInput")
        connect_btn = self.page.findChild(QPushButton, "telegramConnectButton")

        self.assertFalse(connect_btn.isEnabled())
        token_input.setText("123456:FAKE_TOKEN")
        self.assertTrue(connect_btn.isEnabled())

        connect_btn.click()
        self.assertEqual(self.controller.verify_token_called_with, "123456:FAKE_TOKEN")
        self.assertEqual(token_input.text(), "")

    def test_enable_toggle_triggers_controller(self):
        toggle = self.page.findChild(QCheckBox, "telegramEnableToggle")
        toggle.setChecked(True)
        self.assertEqual(self.controller.set_enabled_called_with, True)

    def test_status_label_updates_on_state_signal(self):
        status_lbl = self.page.findChild(QLabel, "telegramStatusLabel")
        self.controller.telegram_state_changed.emit({
            "state": TelegramRuntimeState.ACTIVE.value,
            "bot_name": "balachky_test_bot",
            "pairing_url": "",
            "error": "",
        })
        self.app.processEvents()
        self.assertIn("@balachky_test_bot", status_lbl.text())

    def test_pairing_button_calls_controller(self):
        pair_btn = self.page.findChild(QPushButton, "telegramPairButton")
        pair_btn.click()
        self.assertTrue(self.controller.begin_pairing_called)

    def test_disconnect_button_shows_confirmation(self):
        disconnect_btn = self.page.findChild(QPushButton, "telegramDisconnectButton")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            disconnect_btn.click()
            self.assertTrue(self.controller.disconnect_called)

    def test_feed_displays_remote_transcripts(self):
        # Log a local transcript and a remote transcript
        log_history(
            self.controller.profile.history_path,
            "Локальне диктування",
            "Локальне диктування",
            source="desktop",
        )
        log_history(
            self.controller.profile.history_path,
            "Голосове з телефону",
            "Голосове з телефону",
            source="remote",
        )

        self.page.refresh()
        labels = [lbl.text() for lbl in self.page.findChildren(QLabel)]
        self.assertTrue(any("Голосове з телефону" in t for t in labels))
        self.assertFalse(any("Локальне диктування" in t for t in labels))

    def test_history_page_structural_separation_for_remote_records(self):
        from fronts.desktop.pages.history import HistoryPage
        from PySide6.QtWidgets import QFrame

        log_history(
            self.controller.profile.history_path,
            "Локальне диктування на ПК",
            "Локальне диктування на ПК",
            source="desktop",
        )
        log_history(
            self.controller.profile.history_path,
            "Файлове розпізнавання",
            "Файлове розпізнавання",
            source="file",
        )
        log_history(
            self.controller.profile.history_path,
            "Віддалений запис із Telegram",
            "Віддалений запис із Telegram",
            source="remote",
        )

        hist_page = HistoryPage(self.controller)
        hist_page.refresh()

        remote_section = hist_page.findChild(QFrame, "remoteHistorySection")
        self.assertIsNotNone(remote_section, "Не знайдено окрему секцію remoteHistorySection в Історії")
        title_lbl = remote_section.findChild(QLabel, "remoteHistoryTitle")
        self.assertIsNotNone(title_lbl, "Не знайдено заголовок remoteHistoryTitle")
        self.assertEqual(title_lbl.text(), "Голосові з інших пристроїв")

        # Remote card text is inside remote_section
        remote_labels = [lbl.text() for lbl in remote_section.findChildren(QLabel)]
        self.assertTrue(any("Віддалений запис із Telegram" in t for t in remote_labels))
        self.assertFalse(any("Локальне диктування на ПК" in t for t in remote_labels))
        self.assertFalse(any("Файлове розпізнавання" in t for t in remote_labels))

        # Local card text is on the page outside remote_section
        all_labels = [lbl.text() for lbl in hist_page.findChildren(QLabel)]
        self.assertTrue(any("Локальне диктування на ПК" in t for t in all_labels))
        self.assertTrue(any("Файлове розпізнавання" in t for t in all_labels))
        hist_page.deleteLater()


class DesktopAppTelegramIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "history.jsonl"
        self.profile = SimpleNamespace(
            terms_path=str(Path(self.tmp.name) / "terms.toml"),
            phrases_path=str(Path(self.tmp.name) / "phrases.toml"),
            history_path=str(self.history_path),
            memory_enabled=True,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_telegram_transcribe_invokes_terms_fallback_and_logs_history(self):
        from unittest.mock import Mock
        from fronts.desktop.app import DesktopApp

        app = SimpleNamespace()
        app.profile = self.profile
        app.telegram_controller = SimpleNamespace(
            status=lambda: {"state": "active", "bot_name": "bot"}
        )
        app.telegram_state_changed = Mock()
        app._profile_terms = Mock(return_value=["mock_term"])
        app._transcribe_with_fallback = Mock(
            return_value=("сирий текст", "фінальний текст", 3.0, [], [])
        )

        result = DesktopApp._telegram_transcribe(app, b"dummy_audio_bytes")
        self.assertEqual(result[1], "фінальний текст")
        app._profile_terms.assert_called_once_with(self.profile)
        app._transcribe_with_fallback.assert_called_once_with(
            b"dummy_audio_bytes", ["mock_term"], should_cancel=None
        )
        app.telegram_state_changed.emit.assert_called_once_with(
            {"state": "active", "bot_name": "bot"}
        )

        # Verify history file has source='remote'
        lines = self.history_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        import json
        rec = json.loads(lines[0])
        self.assertEqual(rec["source"], "remote")
        self.assertEqual(rec["final"], "фінальний текст")

    def test_telegram_transcribe_without_active_profile_uses_active_profile_lookup(self):
        from unittest.mock import Mock, patch
        from fronts.desktop.app import DesktopApp

        app = SimpleNamespace()
        app.profile = None
        app.telegram_controller = SimpleNamespace(
            status=lambda: {"state": "active", "bot_name": "bot"}
        )
        app.telegram_state_changed = Mock()
        app._profile_terms = Mock(return_value=["mock_term"])
        app._transcribe_with_fallback = Mock(
            return_value=("сирий текст", "фінальний текст", 3.0, [], [])
        )

        with patch("fronts.desktop.app.profiles.get_active", return_value=self.profile) as mock_get_active:
            result = DesktopApp._telegram_transcribe(app, b"dummy_audio_bytes")
            mock_get_active.assert_called_once()
            self.assertEqual(result[1], "фінальний текст")
            app._profile_terms.assert_called_once_with(self.profile)
            app._transcribe_with_fallback.assert_called_once_with(
                b"dummy_audio_bytes", ["mock_term"], should_cancel=None
            )
            app.telegram_state_changed.emit.assert_called_once_with(
                {"state": "active", "bot_name": "bot"}
            )

        # Verify history file has source='remote'
        lines = self.history_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        import json
        rec = json.loads(lines[0])
        self.assertEqual(rec["source"], "remote")
        self.assertEqual(rec["final"], "фінальний текст")

    def test_telegram_transcribe_without_active_profile_when_no_profiles_exist(self):
        from unittest.mock import Mock, patch
        from fronts.desktop.app import DesktopApp

        app = SimpleNamespace()
        app.profile = None
        app.telegram_controller = SimpleNamespace(
            status=lambda: {"state": "active", "bot_name": "bot"}
        )
        app.telegram_state_changed = Mock()
        app._profile_terms = Mock(return_value=None)
        app._transcribe_with_fallback = Mock(
            return_value=("сирий текст", "фінальний текст", 3.0, [], [])
        )

        with patch("fronts.desktop.app.profiles.get_active", return_value=None):
            result = DesktopApp._telegram_transcribe(app, b"dummy_audio_bytes")
            self.assertEqual(result[1], "фінальний текст")
            app._transcribe_with_fallback.assert_called_once_with(
                b"dummy_audio_bytes", None, should_cancel=None
            )
            app.telegram_state_changed.emit.assert_not_called()

    def test_desktop_app_cleanup_calls_telegram_stop_with_warning_on_timeout(self):
        from unittest.mock import Mock
        from fronts.desktop.app import DesktopApp

        app = SimpleNamespace()
        ctrl = Mock()
        ctrl.stop.return_value = False
        app.telegram_controller = ctrl
        app.transcribed = Mock()
        app.finished = Mock()
        app.file_status = Mock()
        app.file_done = Mock()
        app.rec_state = Mock()
        app.transcription_error = Mock()
        app.cpu_fallback = Mock()
        app.watch_ready = Mock()
        app.mic_test_result = Mock()
        app.preview_ready = Mock()
        app.meeting_state = Mock()
        app.meeting_track_done = Mock()
        app.meeting_session_done = Mock()
        app.meeting_error = Mock()
        app.meeting_audio_ready = Mock()
        app.meeting_storage_warning = Mock()
        app.meeting_screen_error = Mock()
        app.meeting_processing_progress = Mock()
        app.meeting_processing_done = Mock()
        app.screen_record_state = Mock()
        app.screen_record_error = Mock()
        app.screen_record_finished = Mock()
        app.live_dictation_segment = Mock()
        app.live_meeting_segment = Mock()
        app.live_error = Mock()
        app.live_disable_requested = Mock()
        app.dictation_audio_state = Mock()
        app.meeting_audio_state = Mock()
        app._clear_meeting_plain_cache = Mock()
        app._shutdown_meeting_for_exit = Mock()
        app._stop_live_dictation = Mock()
        app._stop_live_meeting = Mock()
        app.window = SimpleNamespace(remember_geometry=Mock())

        with self.assertLogs(level="WARNING") as log_capture:
            DesktopApp._cleanup(app)
            ctrl.stop.assert_called_once_with(timeout=5.0)
            self.assertTrue(any("TelegramController" in m for m in log_capture.output))

    def test_desktop_app_init_wiring_ast(self):
        import ast
        app_path = Path(__file__).resolve().parents[1] / "fronts" / "desktop" / "app.py"
        app_code = app_path.read_text(encoding="utf-8")
        tree = ast.parse(app_code)

        found_init = False
        found_cleanup = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "DesktopApp":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        init_src = ast.get_source_segment(app_code, item)
                        if init_src and "TelegramController" in init_src and "self.telegram_controller" in init_src:
                            found_init = True
                    elif isinstance(item, ast.FunctionDef) and item.name == "_cleanup":
                        clean_src = ast.get_source_segment(app_code, item)
                        if clean_src and "telegram_ctrl.stop" in clean_src:
                            found_cleanup = True
        self.assertTrue(found_init, "TelegramController не знайдено в DesktopApp.__init__")
        self.assertTrue(found_cleanup, "telegram_ctrl.stop не знайдено в DesktopApp._cleanup")


if __name__ == "__main__":
    unittest.main()
