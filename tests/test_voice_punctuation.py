"""feature/voice-punctuation — тести чистої функції apply_voice_punctuation
та гейта конфігу в PTT-конвеєрі (_work). Стиль — як test_backend_regressions.py."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core.config import Config
from whisper_core.punctuation import apply_voice_punctuation as vp

DASH = "—"   # тире (em dash) з набору команд


class UkCommandTests(unittest.TestCase):
    def test_comma(self):
        self.assertEqual(vp("привіт кома світ", "uk"), "привіт, світ")

    def test_period_strips_trailing_space(self):
        self.assertEqual(vp("готово крапка", "uk"), "готово.")

    def test_colon(self):
        self.assertEqual(vp("список двокрапка яблуко", "uk"), "список: яблуко")

    def test_dash_keeps_surrounding_spaces(self):
        self.assertEqual(vp("слово тире друге", "uk"), f"слово {DASH} друге")

    def test_question_mark_multiword(self):
        self.assertEqual(vp("як справи знак питання", "uk"), "як справи?")

    def test_exclamation_mark_multiword(self):
        self.assertEqual(vp("вітаю знак оклику", "uk"), "вітаю!")

    def test_new_line(self):
        self.assertEqual(vp("перший новий рядок другий", "uk"), "перший\nдругий")

    def test_new_line_alias(self):
        self.assertEqual(vp("перший з нового рядка другий", "uk"), "перший\nдругий")

    def test_parentheses(self):
        self.assertEqual(
            vp("текст дужка відкривається нота дужка закривається кінець", "uk"),
            "текст (нота) кінець")


class EnCommandTests(unittest.TestCase):
    def test_comma(self):
        self.assertEqual(vp("hello comma world", "en"), "hello, world")

    def test_period(self):
        self.assertEqual(vp("done period", "en"), "done.")

    def test_full_stop_alias(self):
        self.assertEqual(vp("done full stop next", "en"), "done. Next")

    def test_question_mark(self):
        self.assertEqual(vp("really question mark", "en"), "really?")

    def test_exclamation_mark(self):
        self.assertEqual(vp("wow exclamation mark", "en"), "wow!")

    def test_colon(self):
        self.assertEqual(vp("list colon item", "en"), "list: item")

    def test_dash(self):
        self.assertEqual(vp("a dash b", "en"), f"a {DASH} b")

    def test_new_line(self):
        self.assertEqual(vp("first new line second", "en"), "first\nsecond")


class MultiwordPriorityTests(unittest.TestCase):
    """Багатослівні команди мусять матчитися раніше за коротші входження."""

    def test_double_krapka_in_dvokrapka_is_not_split(self):
        # «двокрапка» не має розкластися на «крапка»
        self.assertEqual(vp("отже двокрапка кінець", "uk"), "отже: кінець")

    def test_znak_pytannia_matched_as_whole(self):
        self.assertEqual(vp("що знак питання все", "uk"), "що? Все")


class CaseTests(unittest.TestCase):
    def test_command_is_case_insensitive(self):
        self.assertEqual(vp("Привіт КОМА світ", "uk"), "Привіт, світ")

    def test_capital_after_period(self):
        self.assertEqual(vp("привіт крапка світ", "uk"), "привіт. Світ")

    def test_capital_after_question(self):
        self.assertEqual(vp("добре знак питання справді", "uk"), "добре? Справді")

    def test_capital_after_exclamation(self):
        self.assertEqual(vp("так знак оклику ура", "uk"), "так! Ура")


class CleanupTests(unittest.TestCase):
    def test_doubled_marks_collapse(self):
        self.assertEqual(vp("кінець крапка крапка", "uk"), "кінець.")

    def test_no_space_before_mark(self):
        # між словом і знаком не лишається зайвого пробілу
        self.assertEqual(vp("а кома б кома в", "uk"), "а, б, в")


class UnchangedTests(unittest.TestCase):
    def test_text_without_commands_is_untouched(self):
        text = "звичайний текст без жодних команд"
        self.assertEqual(vp(text, "uk"), text)

    def test_double_spaces_preserved_when_no_command(self):
        # без команд — НЕ чіпаємо взагалі (навіть подвійні пробіли лишаються)
        text = "два  пробіли  тут"
        self.assertEqual(vp(text, "uk"), text)

    def test_empty_text(self):
        self.assertEqual(vp("", "uk"), "")

    def test_wrong_language_leaves_command_word(self):
        # укр. команда при англ. наборі — не команда, текст без змін
        self.assertEqual(vp("привіт кома світ", "en"), "привіт кома світ")
        # англ. команда при укр. наборі — теж не команда
        self.assertEqual(vp("hello comma world", "uk"), "hello comma world")

    def test_unknown_language_is_untouched(self):
        self.assertEqual(vp("привіт кома світ", "fr"), "привіт кома світ")


class ConfigDefaultTests(unittest.TestCase):
    def test_voice_punctuation_defaults_off(self):
        self.assertFalse(Config().voice_punctuation)


class PipelineGateTests(unittest.TestCase):
    """Гейт у _work: пунктуація застосовується ЛИШЕ коли конфіг увімкнено;
    вимкнений конфіг лишає навіть слова-команди недоторканими."""

    @staticmethod
    def _controller(voice_punctuation, language="uk", final="привіт кома світ"):
        recorded = {}

        class Transcribed:
            def emit(self, raw, fin, words, ts):
                recorded["final"] = fin

        class Counter:
            def __init__(self):
                self.count = 0

            def emit(self, *args):
                self.count += 1

        controller = SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: (
                "raw", final, 1.0, [], []),
            output_mode="show",
            transcription_error=Counter(),
            transcribed=Transcribed(),
            finished=Counter(),
            cfg=SimpleNamespace(sounds=False, voice_punctuation=voice_punctuation,
                                language=language),
            _busy=True,
        )
        return controller, recorded

    def test_enabled_converts_command(self):
        from fronts.desktop import app as desktop_app

        controller, recorded = self._controller(True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        self.assertEqual(recorded["final"], "привіт, світ")
        self.assertFalse(controller._busy)
        self.assertEqual(controller.finished.count, 1)

    def test_disabled_leaves_command_word_untouched(self):
        from fronts.desktop import app as desktop_app

        controller, recorded = self._controller(False)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        self.assertEqual(recorded["final"], "привіт кома світ")


if __name__ == "__main__":
    unittest.main()
