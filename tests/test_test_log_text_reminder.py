"""One-time re-consent for legacy profiles that log dictated text."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QMainWindow, QMessageBox

from fronts.desktop import main_window
from fronts.desktop.app import DesktopApp
from fronts.desktop.i18n import STRINGS
from fronts.desktop.pages.settings import SettingsPage
from fronts.desktop.splash import SplashScreen
from whisper_core.config import Config


class _Controller:
    def __init__(self, *, include_text=True, notice_shown=False):
        self.cfg = Config()
        self.cfg.test_mode = True
        self.cfg.test_mode_include_text = include_text
        self.cfg.test_mode_text_notice_shown = notice_shown
        self.save_calls = 0
        self.set_calls = []

    def save_config(self):
        self.save_calls += 1

    def set_test_mode(self, enabled, include_text):
        self.set_calls.append((bool(enabled), bool(include_text)))
        self.cfg.test_mode = bool(enabled)
        self.cfg.test_mode_include_text = bool(include_text)
        self.save_calls += 1


class TestLogTextReminder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QMainWindow()

    def tearDown(self):
        self.parent.close()
        self.app.processEvents()

    def _api(self):
        helper = getattr(main_window, "maybe_show_test_log_text_reminder", None)
        dialog_cls = getattr(main_window, "TextLogReminderDialog", None)
        self.assertIsNotNone(helper, "main window must expose the reminder gate")
        self.assertIsNotNone(dialog_cls, "main window must expose the real dialog")
        return helper, dialog_cls

    def _show_parent(self):
        self.parent.show()
        self.app.processEvents()
        self.assertTrue(self.parent.isVisible())

    def test_enabled_legacy_profile_sees_reminder_once(self):
        helper, dialog_cls = self._api()
        controller = _Controller()
        self._show_parent()

        def choose_keep(dialog):
            dialog.keep_button.click()
            return 0

        with patch.object(dialog_cls, "exec", choose_keep):
            self.assertTrue(helper(self.parent, controller))
            self.assertFalse(helper(self.parent, controller))

        self.assertTrue(controller.cfg.test_mode_include_text)
        self.assertTrue(controller.cfg.test_mode_text_notice_shown)
        self.assertEqual(controller.save_calls, 1)

    def test_turn_off_now_really_disables_text_logging(self):
        helper, dialog_cls = self._api()
        controller = _Controller()
        self._show_parent()

        def choose_disable(dialog):
            dialog.disable_button.click()
            return 0

        with patch.object(dialog_cls, "exec", choose_disable):
            self.assertTrue(helper(self.parent, controller))

        self.assertFalse(controller.cfg.test_mode_include_text)
        self.assertTrue(controller.cfg.test_mode_text_notice_shown)
        self.assertEqual(controller.set_calls, [(True, False)])

    def test_fresh_default_profile_never_sees_reminder(self):
        helper, dialog_cls = self._api()
        controller = _Controller(include_text=False)
        self._show_parent()

        with patch.object(dialog_cls, "exec") as execute:
            self.assertFalse(helper(self.parent, controller))

        execute.assert_not_called()
        self.assertFalse(controller.cfg.test_mode_text_notice_shown)

    def test_hidden_main_window_does_not_open_modal_or_mark_notice(self):
        helper, dialog_cls = self._api()
        controller = _Controller()

        with patch.object(dialog_cls, "exec") as execute:
            self.assertFalse(helper(self.parent, controller))

        execute.assert_not_called()
        self.assertFalse(controller.cfg.test_mode_text_notice_shown)

    def test_both_buttons_have_accessible_names(self):
        _, dialog_cls = self._api()
        dialog = dialog_cls(_Controller(), self.parent)
        self.addCleanup(dialog.close)

        self.assertEqual(
            dialog.keep_button.accessibleName(), dialog.keep_button.text())
        self.assertEqual(
            dialog.disable_button.accessibleName(), dialog.disable_button.text())

    def test_notice_marker_round_trips_through_config(self):
        sandbox_tmp = os.environ.get("POST90_TEST_CONFIG_DIR")
        if sandbox_tmp:
            path = Path(sandbox_tmp) / "config-roundtrip.toml"
            path.unlink(missing_ok=True)
            cfg = Config()
            cfg.test_mode_text_notice_shown = True
            cfg.save(path)
            loaded = Config.load(path)
            path.unlink(missing_ok=True)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.toml"
                cfg = Config()
                cfg.test_mode_text_notice_shown = True
                cfg.save(path)
                loaded = Config.load(path)
        self.assertTrue(loaded.test_mode_text_notice_shown)

    def test_manual_opt_in_counts_as_confirmed(self):
        cfg = Config()
        controller = _Controller(include_text=False)
        controller.cfg = cfg
        fake_page = SimpleNamespace(
            controller=controller,
            _test_mode=QCheckBox(),
            _test_include_text=QCheckBox(),
        )
        fake_page._test_mode.setChecked(True)
        fake_page._test_include_text.setChecked(True)

        with patch(
            "fronts.desktop.pages.settings.QMessageBox.warning",
            return_value=QMessageBox.Ok,
        ):
            SettingsPage._on_test_include_text(fake_page, True)

        self.assertTrue(cfg.test_mode_text_notice_shown)
        self.assertEqual(controller.set_calls, [(True, True)])

    def test_reconsent_copy_exists_in_both_languages(self):
        keys = {
            "test_log_text_notice_title",
            "test_log_text_notice_body",
            "test_log_text_notice_keep",
            "test_log_text_notice_disable",
        }
        for language in ("uk", "en"):
            with self.subTest(language=language):
                self.assertTrue(keys <= STRINGS[language].keys())

    def test_opening_from_tray_schedules_reminder_after_window_activation(self):
        window = MagicMock()
        fake_app = SimpleNamespace(window=window)

        with patch(
            "fronts.desktop.app.QTimer.singleShot",
            side_effect=lambda _delay, callback: callback(),
        ) as single_shot:
            DesktopApp.show_window(fake_app)

        self.assertEqual(
            window.method_calls[:3],
            [call.show(), call.raise_(), call.activateWindow()],
        )
        single_shot.assert_called_once_with(
            0, window.maybe_show_test_log_text_reminder)
        window.maybe_show_test_log_text_reminder.assert_called_once_with()

    def test_normal_start_schedules_reminder_only_after_splash_finishes(self):
        splash = SplashScreen()
        self.addCleanup(splash.close)
        self.parent.maybe_show_test_log_text_reminder = MagicMock()

        with patch(
            "fronts.desktop.splash.motion.animations_enabled",
            return_value=False,
        ), patch(
            "fronts.desktop.splash.QTimer.singleShot",
            side_effect=lambda _delay, callback: callback(),
        ) as single_shot:
            splash.finish_to(self.parent)

        self.assertTrue(self.parent.isVisible())
        single_shot.assert_called_once_with(
            0, self.parent.maybe_show_test_log_text_reminder)
        self.parent.maybe_show_test_log_text_reminder.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
