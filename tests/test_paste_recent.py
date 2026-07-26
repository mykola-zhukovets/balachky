"""feature/paste-safety — буфер останніх вставок (whisper_core.qol.PasteHistory).

Це НЕ історія розшифровок: тут саме останні N доставлених у вставку текстів
сесії, для «повторити останню» і перегляду останніх N. Чиста логіка — unit.
"""
import unittest

from whisper_core.qol import PasteHistory


class PasteHistoryTests(unittest.TestCase):
    def test_empty_by_default(self):
        h = PasteHistory()
        self.assertFalse(h.has_items())
        self.assertIsNone(h.last)
        self.assertEqual(h.recent(), [])
        self.assertEqual(len(h), 0)

    def test_record_and_last(self):
        h = PasteHistory()
        h.record("перший")
        h.record("другий")
        self.assertEqual(h.last, "другий")
        self.assertEqual(len(h), 2)

    def test_recent_is_newest_first(self):
        h = PasteHistory()
        for t in ("a", "b", "c"):
            h.record(t)
        self.assertEqual(h.recent(), ["c", "b", "a"])

    def test_capacity_limit_evicts_oldest(self):
        h = PasteHistory(capacity=10)
        for i in range(15):
            h.record(f"t{i}")
        self.assertEqual(len(h), 10)
        # лишились останні 10 (t5..t14), найновіша першою
        self.assertEqual(h.recent()[0], "t14")
        self.assertEqual(h.recent()[-1], "t5")
        self.assertNotIn("t4", h.recent())

    def test_empty_and_none_ignored(self):
        h = PasteHistory()
        h.record("")
        h.record(None)
        self.assertFalse(h.has_items())

    def test_default_capacity_is_ten(self):
        h = PasteHistory()
        for i in range(12):
            h.record(str(i))
        self.assertEqual(len(h), 10)

    def test_clear_forgets_all(self):
        h = PasteHistory()
        h.record("секрет")
        h.clear()
        self.assertFalse(h.has_items())
        self.assertEqual(h.recent(), [])
        self.assertIsNone(h.last)


class PasteHistoryToggleTests(unittest.TestCase):
    """feature/paste-safety: тумблер історії. Вимкнено → доставка НЕ поповнює
    буфер (нова поверхня витоку для чутливих диктувань закрита)."""

    @staticmethod
    def _app(enabled):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        return SimpleNamespace(
            cfg=SimpleNamespace(
                restore_clipboard=False,
                paste_typing_fallback=False,
                paste_confirm_sound=False,
                paste_confirm_on_window_change=True,
                paste_history_enabled=enabled),
            transcription_error=SimpleNamespace(emit=lambda *_: None),
            _undo=MagicMock(),
            _paste_history=PasteHistory(),
        )

    def _deliver(self, app):
        from unittest.mock import patch
        from fronts.desktop import app as desktop_app
        with patch.object(desktop_app, "paste_text", return_value="ctrl_v"):
            desktop_app._deliver_paste(app, "текст", False, pinned_target=None)

    def test_disabled_does_not_record(self):
        app = self._app(enabled=False)
        self._deliver(app)
        self.assertFalse(app._paste_history.has_items())   # у буфер не пишемо

    def test_enabled_records(self):
        app = self._app(enabled=True)
        self._deliver(app)
        self.assertEqual(app._paste_history.recent(), ["текст"])

    def test_default_config_enabled(self):
        from whisper_core.config import Config
        self.assertTrue(Config().paste_history_enabled)

    def test_config_roundtrip(self):
        import os
        import tempfile
        from whisper_core.config import Config
        c = Config()
        c.paste_history_enabled = False
        fd, path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            c.save(path)
            self.assertFalse(Config.load(path).paste_history_enabled)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
