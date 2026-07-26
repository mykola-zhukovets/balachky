"""feature/paste-preview — тести логіки картки перегляду перед вставкою.

Покриваємо чисту логіку (розташування картки, гейт конфігу) без підняття Qt-вікна:
UI-рендер картки — у tests/render_preview_smoke.py (поза unittest discover).
Стиль — як test_cascade_paste.py.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fronts.desktop.preview import place_near_cursor


class PlaceNearCursorTests(unittest.TestCase):
    """Розташування картки: нижче-праворуч, дзеркалення й кламп у межі екрана."""

    SCREEN = (0, 0, 1920, 1080)      # x, y, w, h
    SIZE = (380, 200)                # w, h картки

    def test_below_right_by_default(self):
        # курсор у центрі — картка нижче-праворуч на margin
        x, y = place_near_cursor((500, 400), self.SIZE, self.SCREEN, margin=14)
        self.assertEqual((x, y), (514, 414))

    def test_flip_left_when_overflow_right(self):
        # курсор біля правого краю — картка дзеркалиться ліворуч від курсора
        x, y = place_near_cursor((1900, 400), self.SIZE, self.SCREEN, margin=14)
        self.assertEqual(x, 1900 - 14 - 380)
        self.assertGreaterEqual(x, 0)

    def test_flip_up_when_overflow_bottom(self):
        # курсор біля нижнього краю — картка дзеркалиться вгору
        x, y = place_near_cursor((500, 1070), self.SIZE, self.SCREEN, margin=14)
        self.assertEqual(y, 1070 - 14 - 200)
        self.assertGreaterEqual(y, 0)

    def test_clamped_fully_on_screen(self):
        # кутовий курсор: після дзеркалення все одно тримаємось у межах
        for cursor in [(0, 0), (1920, 1080), (1919, 5), (5, 1079)]:
            x, y = place_near_cursor(cursor, self.SIZE, self.SCREEN)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + self.SIZE[0], 1920)
            self.assertLessEqual(y + self.SIZE[1], 1080)

    def test_respects_screen_offset(self):
        # вторинний монітор (від'ємне/зсунуте походження) — кламп у ЙОГО межі
        screen = (-1920, 0, 1920, 1080)
        x, y = place_near_cursor((-10, 400), self.SIZE, screen)
        self.assertGreaterEqual(x, -1920)
        self.assertLessEqual(x + self.SIZE[0], 0)


class ConfigGateTests(unittest.TestCase):
    def test_default_is_false(self):
        from whisper_core.config import Config
        self.assertFalse(Config().paste_preview)

    def test_save_load_roundtrip(self):
        from whisper_core.config import Config
        c = Config()
        c.paste_preview = True
        fd, path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            c.save(path)
            self.assertTrue(Config.load(path).paste_preview)
        finally:
            os.remove(path)


class WorkPreviewBranchTests(unittest.TestCase):
    """Наскрізь через _work: paste_preview=True → картка (preview_ready), а не
    негайна вставка; вимкнено → звичайний paste-шлях (_deliver_paste → paste_text)."""

    @staticmethod
    def _controller(paste_preview):
        emitted = []

        class Rec:
            def __init__(self):
                self.args = None

            def emit(self, *a):
                self.args = a

        class Preview:
            def emit(self, *a):
                emitted.append(a)

        controller = SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: ("raw", "текст", 1.0, [], []),
            output_mode="paste",
            transcription_error=Rec(),
            transcribed=Rec(),
            finished=Rec(),
            preview_ready=Preview(),
            cfg=SimpleNamespace(sounds=False, voice_punctuation=False, language="uk",
                                restore_clipboard=True, paste_typing_fallback=False,
                                paste_preview=paste_preview),
            _busy=True,
        )
        return controller, emitted

    def test_preview_shows_card_and_skips_paste(self):
        from fronts.desktop import app as desktop_app
        controller, emitted = self._controller(paste_preview=True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None), \
             patch.object(desktop_app, "paste_text") as pt:
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        pt.assert_not_called()                     # нічого не вставили одразу
        self.assertEqual(emitted, [("текст", False)])  # (текст, auto_enter)

    def test_disabled_uses_normal_paste(self):
        from fronts.desktop import app as desktop_app
        controller, emitted = self._controller(paste_preview=False)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None), \
             patch.object(desktop_app, "snapshot_clipboard", return_value="old"), \
             patch.object(desktop_app, "paste_text", return_value="ctrl_v") as pt, \
             patch.object(desktop_app, "restore_clipboard"):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        pt.assert_called_once_with("текст", typing_fallback=False)
        self.assertEqual(emitted, [])              # картку не показували


if __name__ == "__main__":
    unittest.main()
