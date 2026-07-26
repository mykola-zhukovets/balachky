"""Хвиля «вузькі місця моделей» — під-хвилі 2 і 7.

Під-хвиля 7 (anti-repeat): no_repeat_ngram_size з конфігу доходить до
faster_whisper.transcribe — захист від циклів-повторів на довгих аудіо.

Під-хвиля 2 (word_timestamps у диктуванні): тумблер highlight_uncertain_words
керує include_word_timestamps у PTT-диктуванні; пословні ймовірності доходять
до картки стрічки (transcribed.emit), де _render_html підсвічує непевні слова.

Групи:
  * AntiRepeatConfigTests / AntiRepeatTranscribeTests — під-хвиля 7;
  * HighlightConfigTests / WordTimestampsDictationTests — під-хвиля 2 (проброс);
  * RenderHtmlHighlightTests — поріг probability<0.5 у стрічці.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Тести нижче імпортують desktop-код — Qt без реального екрана.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core.config import Config, NO_REPEAT_NGRAM_DEFAULT
from whisper_core.engine import Engine


# ── Під-хвиля 7: anti-repeat ────────────────────────────────────────────────

class AntiRepeatConfigTests(unittest.TestCase):
    def test_default_is_three(self):
        # Розумний дефолт — триграми (2 різали б легітимні укр. повтори «так-так»).
        self.assertEqual(NO_REPEAT_NGRAM_DEFAULT, 3)
        self.assertEqual(Config().no_repeat_ngram_size, NO_REPEAT_NGRAM_DEFAULT)

    def test_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.no_repeat_ngram_size = 2
            cfg.save(path)
            self.assertEqual(Config.load(path).no_repeat_ngram_size, 2)


class AntiRepeatTranscribeTests(unittest.TestCase):
    @staticmethod
    def _transcribe_kwargs(cfg):
        with patch("whisper_core.engine.WhisperModel") as model:
            model.return_value.transcribe.return_value = (
                [], SimpleNamespace(duration=1.0))
            Engine(cfg).transcribe("audio.wav")
        return model.return_value.transcribe.call_args.kwargs

    def test_default_no_repeat_ngram_reaches_transcribe(self):
        # Мутація: прибрати no_repeat_ngram_size у engine.transcribe → KeyError тут.
        self.assertEqual(
            self._transcribe_kwargs(Config())["no_repeat_ngram_size"], 3)

    def test_custom_no_repeat_ngram_reaches_transcribe(self):
        cfg = Config()
        cfg.no_repeat_ngram_size = 2
        self.assertEqual(
            self._transcribe_kwargs(cfg)["no_repeat_ngram_size"], 2)

    def test_disabled_when_zero(self):
        cfg = Config()
        cfg.no_repeat_ngram_size = 0        # 0 = вимкнено (дефолт faster-whisper)
        self.assertEqual(
            self._transcribe_kwargs(cfg)["no_repeat_ngram_size"], 0)


# ── Під-хвиля 2: word_timestamps + підсвітка ────────────────────────────────

class HighlightConfigTests(unittest.TestCase):
    def test_default_is_true(self):
        self.assertIs(Config().highlight_uncertain_words, True)

    def test_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.highlight_uncertain_words = False
            cfg.save(path)
            self.assertIs(Config.load(path).highlight_uncertain_words, False)


class WordTimestampsDictationTests(unittest.TestCase):
    """Головний фікс під-хвилі 2: PTT-шлях (_work) вмикає word_timestamps за
    тумблером і слова доходять до картки стрічки (transcribed.emit)."""

    _WORDS = [("привіт", 0.9), ("світ", 0.3)]

    def _controller(self, highlight):
        calls = {}
        recorded = {}

        class Card:
            def emit(self, *a):
                recorded["card"] = a

        class Counter:
            def __init__(self):
                self.count = 0

            def emit(self, *a):
                self.count += 1

        def fake_transcribe(audio, terms, *, include_word_timestamps=False):
            # Імітуємо engine: коли word_timestamps вимкнено — words порожні
            # (seg.words=None), коли ввімкнено — заповнені + 6-й елемент timed.
            calls["wt"] = include_word_timestamps
            if include_word_timestamps:
                return ("привіт світ", "привіт світ", 1.0,
                        list(self._WORDS), [], [])
            return ("привіт світ", "привіт світ", 1.0, [], [])

        cfg = SimpleNamespace(
            sounds=False, voice_punctuation=False, language="uk",
            restore_clipboard=True, paste_typing_fallback=False,
            highlight_uncertain_words=highlight, paste_preview=False,
            save_dictation_audio=False, auto_export_enabled=False)
        controller = SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=fake_transcribe,
            output_mode="show",          # показ у стрічці, без paste-шляху
            transcription_error=Counter(),
            transcribed=Card(),
            finished=Counter(),
            cfg=cfg,
            _busy=True,
        )
        return controller, calls, recorded

    def _run(self, controller):
        from fronts.desktop import app as desktop_app
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())

    def test_highlight_on_enables_word_timestamps_and_delivers_words(self):
        controller, calls, recorded = self._controller(highlight=True)
        self._run(controller)
        self.assertTrue(calls["wt"])                 # прапорець увімкнено
        self.assertIn("card", recorded)
        # третій аргумент transcribed.emit — words (пари слово/ймовірність)
        self.assertEqual(recorded["card"][2], self._WORDS)

    def test_highlight_off_disables_word_timestamps(self):
        controller, calls, recorded = self._controller(highlight=False)
        self._run(controller)
        self.assertFalse(calls["wt"])                # прапорець вимкнено (без DTW)
        self.assertEqual(recorded["card"][2], [])    # слів для підсвітки нема


class FileJobWordTimestampsTests(unittest.TestCase):
    """Проброс під-хвилі 2 у шлях файлів: _transcribe_file_job передає прапорець
    у рушій і нормалізує 5/6-кортеж (UI-склейку картки файлу покриває
    render_uncertainty_smoke). Мутація: прибрати include_word_timestamps=... →
    kw['include_word_timestamps'] зникне → тест червоніє."""

    def test_flag_reaches_engine_and_result_trimmed_to_five(self):
        from fronts.desktop.app import DesktopApp
        calls = {}

        def fake_fallback(path, terms, **kw):
            calls.update(kw)
            # 6-кортеж (як engine з word_timestamps): timed_words у хвості
            return ("raw", "світ", 1.0, [("світ", 0.3)], [], [])

        ns = SimpleNamespace(
            cfg=SimpleNamespace(model_name="large-v3"),
            _transcribe_with_fallback=fake_fallback)
        result = DesktopApp._transcribe_file_job(
            ns, "x.wav", None, None, include_word_timestamps=True)
        self.assertTrue(calls["include_word_timestamps"])
        self.assertEqual(len(result), 5)                 # timed_words відкинуто
        self.assertEqual(result[3], [("світ", 0.3)])     # words збережено


class RenderHtmlHighlightTests(unittest.TestCase):
    """Поріг непевності probability<0.5 у стрічці диктування. Регрес-страж: якщо
    word_timestamps знову перестане долітати, WordTimestampsDictationTests
    червоніють; тут фіксуємо, що сам рендер підсвічує саме непевні слова."""

    @staticmethod
    def _render(final, words):
        from fronts.desktop.main_window import DictationPage
        return DictationPage._render_html(final, words)

    def test_uncertain_word_highlighted_certain_not(self):
        out = self._render("привіт світ", [("привіт", 0.9), ("світ", 0.3)])
        self.assertEqual(out.count("<span"), 1)      # лише «світ» (0.3<0.5)
        self.assertIn("світ", out)

    def test_all_certain_no_highlight(self):
        out = self._render("привіт світ", [("привіт", 0.9), ("світ", 0.8)])
        self.assertNotIn("<span", out)

    def test_no_words_no_highlight(self):
        # Порожні words (word_timestamps вимкнено) → жодної підсвітки.
        out = self._render("привіт світ", [])
        self.assertNotIn("<span", out)


if __name__ == "__main__":
    unittest.main()
