"""Юніт-тести для WinAPI обгортки та системного захисту (Т54 мілітарі-hardening).

Після суду (23.07) фейкові тести замінено на ЧЕСНІ — викликають реальний код і
перевіряють факти, а не відтворюють мок-послідовність у тілі тесту:
  • trigger_panic_lock — реальний метод DesktopApp знищує plaintext temp-копії
    нарад і кеш ключів (не лише мінімізує);
  • симетрія хоткей-матриці — реальні set_*_hotkey/_apply_key через фейк-self;
  • set_clipboard_text_excluded — чесний False при мовчазному провалі запису тексту;
  • WER-виняток — реальний DesktopApp.__init__ його викликає;
  • WDA на діалог наради — показ VideoPlayerDialog накладає афінність за тумблером.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core import win_hardening
from whisper_core.config import Config
from whisper_core.meeting import storage_crypto
from fronts.desktop.hotkey import combos_equal
from fronts.desktop.app import DesktopApp


class _FakeTray:
    def __init__(self):
        self.notices = []

    def notify(self, text):
        self.notices.append(text)


class TestWinHardeningWrapper(unittest.TestCase):
    def test_is_display_affinity_supported_on_non_win(self):
        with patch("whisper_core.win_hardening.is_windows", return_value=False):
            self.assertFalse(win_hardening.is_display_affinity_supported())

    @patch("sys.getwindowsversion")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_is_display_affinity_supported_win10_2004(self, mock_is_win, mock_ver):
        mock_v = MagicMock()
        mock_v.major = 10
        mock_v.build = 19041
        mock_ver.return_value = mock_v
        self.assertTrue(win_hardening.is_display_affinity_supported())

        mock_v.build = 18362  # older Win10
        self.assertFalse(win_hardening.is_display_affinity_supported())

    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_set_window_display_affinity(self, mock_is_win, mock_u32):
        mock_u32.SetWindowDisplayAffinity.return_value = 1
        res = win_hardening.set_window_display_affinity(12345, True)
        self.assertTrue(res)
        mock_u32.SetWindowDisplayAffinity.assert_called_once()
        args = mock_u32.SetWindowDisplayAffinity.call_args[0]
        self.assertEqual(args[1].value, win_hardening.WDA_EXCLUDEFROMCAPTURE)

        mock_u32.reset_mock()
        res_off = win_hardening.set_window_display_affinity(12345, False)
        self.assertTrue(res_off)
        args_off = mock_u32.SetWindowDisplayAffinity.call_args[0]
        self.assertEqual(args_off[1].value, win_hardening.WDA_NONE)

    @patch("whisper_core.win_hardening._wer")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_exclude_process_from_wer(self, mock_is_win, mock_wer):
        mock_wer.WerAddExcludedApplication.return_value = 0
        self.assertTrue(win_hardening.exclude_process_from_wer("Balachky.exe"))
        mock_wer.WerAddExcludedApplication.assert_called_once()

    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening._kernel32")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_clear_clipboard(self, mock_is_win, mock_k32, mock_u32):
        mock_u32.OpenClipboard.return_value = 1
        mock_u32.EmptyClipboard.return_value = 1
        mock_u32.CloseClipboard.return_value = 1

        self.assertTrue(win_hardening.clear_clipboard())
        mock_u32.OpenClipboard.assert_called_once()
        mock_u32.EmptyClipboard.assert_called_once()
        mock_u32.CloseClipboard.assert_called_once()


class TestClipboardExcludedHonesty(unittest.TestCase):
    """Блокер суду 2а: set_clipboard_text_excluded НЕ бреше про успіх. Текст, що
    не потрапив у CF_UNICODETEXT (мовчазний провал GlobalAlloc/Lock/SetClipboardData),
    = НЕ успіх → False, щоб paste.py увімкнув pyperclip-фолбек."""

    def _mocks(self, mock_u32, mock_k32):
        mock_u32.RegisterClipboardFormatW.return_value = 1
        mock_u32.OpenClipboard.return_value = 1
        mock_u32.CloseClipboard.return_value = 1
        mock_k32.GlobalAlloc.return_value = 111
        mock_k32.GlobalLock.return_value = 222
        # ctypes.memmove мокуємо — у фейкові вказівники (222) реальний memmove
        # писати не має; перевіряємо ЛОГІКУ повернення, не WinAPI-мемрайти.

    @patch("ctypes.memmove")
    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening._kernel32")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_false_when_setclipboarddata_text_fails(self, _w, mock_k32, mock_u32, _mm):
        self._mocks(mock_u32, mock_k32)
        mock_u32.SetClipboardData.return_value = 0    # запис тексту мовчки провалився
        self.assertFalse(win_hardening.set_clipboard_text_excluded("текст"))
        mock_u32.CloseClipboard.assert_called_once()  # буфер закрито навіть при провалі

    @patch("ctypes.memmove")
    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening._kernel32")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_false_when_globalalloc_text_fails(self, _w, mock_k32, mock_u32, _mm):
        self._mocks(mock_u32, mock_k32)
        mock_k32.GlobalAlloc.return_value = 0         # алокація тексту не вдалась
        self.assertFalse(win_hardening.set_clipboard_text_excluded("текст"))

    @patch("ctypes.memmove")
    @patch("whisper_core.win_hardening._user32")
    @patch("whisper_core.win_hardening._kernel32")
    @patch("whisper_core.win_hardening.is_windows", return_value=True)
    def test_true_when_text_written(self, _w, mock_k32, mock_u32, _mm):
        self._mocks(mock_u32, mock_k32)
        mock_u32.SetClipboardData.return_value = 111  # успіх запису тексту
        self.assertTrue(win_hardening.set_clipboard_text_excluded("текст"))


class TestPanicLockReal(unittest.TestCase):
    """Блокер суду 1: реальний DesktopApp.trigger_panic_lock знищує РОЗШИФРОВАНІ
    temp-копії нарад (не лише мінімізує вікно). Перевіряємо ФАКТИ."""

    def tearDown(self):
        storage_crypto._PASSWORD_CACHE.clear()

    def test_panic_destroys_plaintext_keys_and_minimizes(self):
        import tempfile
        # 1. Реальна temp-тека з «розшифрованою» нарадою у плейн-кеші.
        owner = tempfile.TemporaryDirectory(prefix="balachky-panic-test-")
        plain_dir = owner.name
        self.assertTrue(os.path.isdir(plain_dir))
        # 2. Кеш ключів населений.
        storage_crypto._PASSWORD_CACHE["fake_root"] = b"0" * 32

        app = SimpleNamespace()
        app._meeting_plain_cache = {"sess1": (owner, plain_dir)}
        # Реальний _clear_meeting_plain_cache (unbound) — саме його має покликати panic.
        app._clear_meeting_plain_cache = (
            lambda sid=None: DesktopApp._clear_meeting_plain_cache(app, sid))
        app.window = MagicMock()
        notices = []
        app.tray = SimpleNamespace(notify=notices.append)

        with patch("whisper_core.win_hardening.clear_clipboard") as clip:
            DesktopApp.trigger_panic_lock(app)

        # ФАКТИ, не мок-послідовність:
        self.assertFalse(os.path.exists(plain_dir),
                         "розшифрована temp-копія наради має бути знищена panic-lock")
        self.assertEqual(app._meeting_plain_cache, {})
        self.assertEqual(len(storage_crypto._PASSWORD_CACHE), 0,
                         "кеш ключів має бути порожній")
        app.window.showMinimized.assert_called_once()
        clip.assert_called_once()
        self.assertEqual(len(notices), 1)


class TestHotkeyMatrixSymmetry(unittest.TestCase):
    """Блокер суду 3: конфлікт-матриця хоткеїв симетрична НА РЕАЛЬНОМУ коді —
    для кожної впорядкованої пари (сетер, інший ключ) зайнята «інша» комбінація
    відхиляє сетер. Прибирання будь-якого ключа з будь-якого taken-списку (напр.
    panic з set_note) валить відповідну ітерацію."""

    _KEYS = ("ptt_key", "undo_paste_key", "insert_last_key", "note_hotkey",
             "command_edit_hotkey", "meeting_bookmark_hotkey", "panic_lock_hotkey")
    _COMBO = "ctrl+alt+shift+k"

    def _app(self, **fields):
        defaults = {k: "" for k in self._KEYS}
        defaults["ptt_key"] = "ctrl+shift+space"    # дефолт PTT
        defaults.update(fields)
        app = SimpleNamespace()
        app.cfg = SimpleNamespace(**defaults, save=lambda: None)
        app.tray = _FakeTray()
        return app

    def _call_setter(self, app, key, combo):
        """Викликати РЕАЛЬНИЙ сетер для config-ключа (конфлікт-шлях повертає
        рано, тож фейку досить cfg+tray)."""
        if key == "ptt_key":
            return DesktopApp._apply_key(app, combo)
        if key == "undo_paste_key":
            return DesktopApp.set_action_hotkey(app, "undo", combo)
        if key == "insert_last_key":
            return DesktopApp.set_action_hotkey(app, "insert", combo)
        if key == "note_hotkey":
            return DesktopApp.set_note_hotkey(app, combo)
        if key == "command_edit_hotkey":
            return DesktopApp.set_command_edit_hotkey(app, combo)
        if key == "meeting_bookmark_hotkey":
            return DesktopApp.set_meeting_bookmark_hotkey(app, combo)
        if key == "panic_lock_hotkey":
            return DesktopApp.set_panic_lock_hotkey(app, combo)
        raise AssertionError(key)

    def test_full_conflict_matrix_both_directions(self):
        for setter_key in self._KEYS:
            for other_key in self._KEYS:
                if setter_key == other_key:
                    continue
                # Зайняти «інший» ключ комбінацією; сетер має її відхилити.
                app = self._app(**{other_key: self._COMBO})
                res = self._call_setter(app, setter_key, self._COMBO)
                self.assertFalse(
                    res, f"set({setter_key}) мав відхилити збіг із {other_key}")

    def test_note_rejects_panic_combo(self):
        # Пряма мутація-детектор: прибирання panic з taken set_note валить це.
        app = self._app(panic_lock_hotkey=self._COMBO)
        self.assertFalse(DesktopApp.set_note_hotkey(app, self._COMBO))
        self.assertEqual(app.cfg.note_hotkey, "")

    def test_panic_rejects_note_combo(self):
        app = self._app(note_hotkey=self._COMBO)
        self.assertFalse(DesktopApp.set_panic_lock_hotkey(app, self._COMBO))
        self.assertEqual(app.cfg.panic_lock_hotkey, "")

    def test_combos_equal_is_order_insensitive(self):
        self.assertTrue(combos_equal("ctrl+alt+n", "alt+ctrl+n"))
        self.assertFalse(combos_equal("ctrl+alt+n", "ctrl+alt+m"))


class TestWerExclusionOnInit(unittest.TestCase):
    """Блокер суду 6: DesktopApp.__init__ реєструє WER-виняток. Мокуємо обгортку
    так, щоб вона кинула сентинел — доводить, що __init__ ДІЙСНО її кличе (а не
    що метод сам по собі працює). Видалення виклику з __init__ → сентинел не
    злетить → тест впаде."""

    def test_init_invokes_exclude_process_from_wer(self):
        from PySide6.QtWidgets import QApplication
        qapp = QApplication.instance() or QApplication([])
        self.addCleanup(qapp.processEvents)
        from fronts.desktop import app as desktop_app

        sentinel = RuntimeError("wer-sentinel")
        calls = {"n": 0}

        def fake_wer(*a, **k):
            calls["n"] += 1
            raise sentinel

        with patch("whisper_core.win_hardening.exclude_process_from_wer", fake_wer), \
             patch.object(desktop_app, "_migrate_update_cache", lambda: None):
            with self.assertRaises(RuntimeError) as ctx:
                desktop_app.DesktopApp(qapp, MagicMock(), cfg=Config())

        self.assertIs(ctx.exception, sentinel, "має злетіти саме наш сентинел")
        self.assertEqual(calls["n"], 1, "__init__ має покликати WER-виняток рівно раз")


class TestVideoDialogCaptureProtection(unittest.TestCase):
    """Блокер суду 4: WDA_EXCLUDEFROMCAPTURE на вікно відтворення наради. Показ
    діалога при увімкненому тумблері накладає афінність; вимкнення тумблера знімає
    її з ЖИВОГО вікна. Форсуємо фолбек без QtMultimedia — нативний QMediaPlayer
    тесту не потрібен (і ризикує 0xC000041D при offscreen-деструкції)."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qapp = QApplication.instance() or QApplication([])

    def tearDown(self):
        win_hardening.set_capture_protection_enabled(False)
        self.qapp.processEvents()

    def _dialog(self):
        from fronts.desktop import video_player
        with patch.object(video_player, "_HAVE_QTMM", False):
            dlg = video_player.VideoPlayerDialog(path=None, parent=None)
        return dlg

    def test_show_applies_wda_when_toggle_on(self):
        win_hardening.set_capture_protection_enabled(True)
        with patch("whisper_core.win_hardening.set_window_display_affinity") as m:
            dlg = self._dialog()
            dlg.show()
            self.qapp.processEvents()
        try:
            true_calls = [c for c in m.call_args_list
                          if len(c.args) >= 2 and c.args[1] is True]
            self.assertTrue(
                true_calls,
                "показ вікна наради при увімкненому тумблері має накласти WDA=True")
        finally:
            dlg.close()
            dlg.deleteLater()
            self.qapp.processEvents()

    def test_toggle_off_clears_live_dialog(self):
        win_hardening.set_capture_protection_enabled(True)
        dlg = self._dialog()
        dlg.show()
        self.qapp.processEvents()
        with patch("whisper_core.win_hardening.set_window_display_affinity") as m:
            win_hardening.set_capture_protection_enabled(False)
        try:
            false_calls = [c for c in m.call_args_list
                           if len(c.args) >= 2 and c.args[1] is False]
            self.assertTrue(
                false_calls,
                "вимкнення тумблера має зняти WDA з живого вікна наради")
        finally:
            dlg.close()
            dlg.deleteLater()
            self.qapp.processEvents()


class TestConfigDefaults(unittest.TestCase):
    def test_config_defaults(self):
        cfg = Config()
        self.assertFalse(cfg.screen_protection)
        self.assertEqual(cfg.panic_lock_hotkey, "")


class StubSignalsExistOnRealController(unittest.TestCase):
    """Вартовий проти пастки канону §4.1: тест-заглушка _NavController оголошує
    сигнал, а справжній DesktopApp — ні. Тоді тести зелені, а живий застосунок
    падає AttributeError (так загубився panic_lock_key_captured при злитті хвиль).
    Кожен сигнал заглушки має існувати й на реальному контролері."""

    def test_every_stub_signal_exists_on_desktop_app(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import Signal
        from fronts.desktop.app import DesktopApp
        from tests.render_nav_smoke import _NavController

        stub_signals = {
            name for name, value in vars(_NavController).items()
            if isinstance(value, Signal)
        }
        self.assertTrue(stub_signals, "заглушка втратила сигнали — вартовий сліпий")
        missing = sorted(n for n in stub_signals if not hasattr(DesktopApp, n))
        self.assertEqual(
            missing, [],
            f"заглушка оголошує сигнали, яких немає у DesktopApp: {missing} — "
            "живий застосунок упаде AttributeError")


if __name__ == "__main__":
    unittest.main()
