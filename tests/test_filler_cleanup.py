"""feature/filler-cleanup — тести чистої функції apply_filler_cleanup та гейта
конфігу в PTT-конвеєрі (_work). Стиль — як test_voice_punctuation.py."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core.config import Config
from whisper_core.fillers import apply_filler_cleanup as fc


class VocalizationTests(unittest.TestCase):
    """Кожна вокалізація рівня 1 як окреме слово зникає, сусіди лишаються."""

    def test_e_single(self):
        self.assertEqual(fc("думаю е що"), "думаю що")

    def test_e_double(self):
        self.assertEqual(fc("де ее коли"), "де коли")

    def test_e_triple_plus(self):
        self.assertEqual(fc("привіт еее світ"), "привіт світ")
        self.assertEqual(fc("привіт ееее світ"), "привіт світ")

    def test_e_dash_e(self):
        self.assertEqual(fc("я е-е там"), "я там")

    def test_em(self):
        self.assertEqual(fc("тоді ем добре"), "тоді добре")

    def test_emm(self):
        self.assertEqual(fc("ага емм так"), "ага так")

    def test_m_dash_m(self):
        self.assertEqual(fc("отже м-м далі"), "отже далі")

    def test_m_triple_plus(self):
        self.assertEqual(fc("стоп ммм іди"), "стоп іди")
        self.assertEqual(fc("стоп мммм іди"), "стоп іди")

    def test_hm(self):
        self.assertEqual(fc("тут гм ось"), "тут ось")

    def test_hmm(self):
        self.assertEqual(fc("тут гмм ось"), "тут ось")

    def test_a_dash_a(self):
        self.assertEqual(fc("тут а-а там"), "тут там")

    def test_a_triple_plus(self):
        self.assertEqual(fc("ой ааа стоп"), "ой стоп")

    def test_case_insensitive(self):
        self.assertEqual(fc("Думаю ЕМ що"), "Думаю що")
        self.assertEqual(fc("привіт ЕЕЕ світ"), "привіт світ")

    def test_two_fillers_in_a_row(self):
        self.assertEqual(fc("я е ем там"), "я там")


class LeadingTrailingFillerTests(unittest.TestCase):
    def test_leading_filler_keeps_lowercase_start(self):
        # оригінал починався з малої (сам філер) — велику не нав'язуємо
        self.assertEqual(fc("гм цікаво"), "цікаво")

    def test_leading_filler_restores_capital(self):
        # оригінал починався з великої — повертаємо її наступному слову,
        # осиротілу кому після філера прибираємо
        self.assertEqual(fc("Ем, я думаю"), "Я думаю")
        self.assertEqual(fc("Гм, цікаво"), "Цікаво")

    def test_trailing_filler(self):
        self.assertEqual(fc("це все е-е"), "це все")


class DuplicateWordTests(unittest.TestCase):
    def test_simple_double(self):
        self.assertEqual(fc("я я хотів"), "я хотів")

    def test_triple_collapses_to_one(self):
        self.assertEqual(fc("я я я бачив"), "я бачив")

    def test_double_case_insensitive_keeps_first(self):
        self.assertEqual(fc("Привіт привіт друже"), "Привіт друже")

    def test_double_spaces_between_still_collapses(self):
        self.assertEqual(fc("я  я хотів"), "я хотів")

    def test_duplicate_separated_by_comma_is_kept(self):
        # кома між словами = намір, не заїкання
        text = "ні, ні"
        self.assertEqual(fc(text), text)


class UntouchedTests(unittest.TestCase):
    def test_hyphenated_repeat_is_kept(self):
        # навмисний повтор через дефіс — один токен, не чіпаємо
        text = "дуже-дуже гарно"
        self.assertEqual(fc(text), text)

    def test_meaningful_word_nu_is_kept(self):
        # «ну» — змістовне слово (рівень 2 свідомо не робимо)
        text = "ну я думаю"
        self.assertEqual(fc(text), text)

    def test_other_meaningful_fillers_kept(self):
        text = "типу коротше значить якось"
        self.assertEqual(fc(text), text)

    def test_conjunction_a_is_kept(self):
        # одиночне «а» — сполучник, не вокалізація
        text = "я прийшов а він пішов"
        self.assertEqual(fc(text), text)

    def test_double_a_and_double_m_not_fillers(self):
        # «аа» і «мм» у списку рівня 1 немає — лишаємо
        self.assertEqual(fc("коса аа мала"), "коса аа мала")
        self.assertEqual(fc("німа мм тиша"), "німа мм тиша")

    def test_numbers_not_collapsed(self):
        text = "мав 5 5 гривень"
        self.assertEqual(fc(text), text)

    def test_text_without_fillers_unchanged(self):
        text = "звичайний текст без жодних паразитів"
        self.assertEqual(fc(text), text)

    def test_double_spaces_preserved_when_no_change(self):
        # ранній вихід: без чистки текст байт-у-байт незмінний
        text = "два  пробіли  тут"
        self.assertEqual(fc(text), text)

    def test_empty_text(self):
        self.assertEqual(fc(""), "")


class TerminalPunctuationTests(unittest.TestCase):
    """DEFECT 1: філер наприкінці речення НЕ повинен з'їдати межу речення —
    термінальна пунктуація (. ! ? …) зберігається, гине лише зайвий пробіл."""

    def test_period_between_sentences_kept(self):
        self.assertEqual(fc("Готово е-е. Почнемо."), "Готово. Почнемо.")

    def test_period_with_mmm_kept(self):
        self.assertEqual(fc("Стоп ммм. Далі йдемо."), "Стоп. Далі йдемо.")

    def test_bang_kept(self):
        self.assertEqual(fc("Я подумав ем! Ура."), "Я подумав! Ура.")

    def test_question_kept(self):
        self.assertEqual(fc("Скажи ем? Так."), "Скажи? Так.")

    def test_trailing_filler_before_period_keeps_period(self):
        self.assertEqual(fc("готово е-е."), "готово.")


class FillerBetweenDuplicatesTests(unittest.TestCase):
    """DEFECT 2: філер МІЖ двома однаковими словами не має тихо злипати їх в одне —
    це емфатичний повтор, а не заїкання. Прибираємо лише філер."""

    def test_tak_e_tak(self):
        self.assertEqual(fc("так е так"), "так так")

    def test_test_e_test(self):
        self.assertEqual(fc("тест е тест"), "тест тест")


class SingleLetterCaseRepeatTests(unittest.TestCase):
    """DEFECT 3: регістрочутливість для однобуквених — сентенс-початкова велика
    не зливається зі сполучником; при цьому «я я» (той самий регістр) стискається."""

    def test_capital_then_conjunction_kept(self):
        # «А» (початок речення) + «а» (сполучник) — різний регістр, не чіпаємо
        text = "А а потім"
        self.assertEqual(fc(text), text)

    def test_same_case_single_letter_still_collapses(self):
        # той самий регістр — звичайний повтор, стискаємо
        self.assertEqual(fc("я я хотів"), "я хотів")


class PunctuationInvariantTests(unittest.TestCase):
    """Текст без філерів і без справжніх повторів — байт-у-байт незмінний,
    включно з усією пунктуацією."""

    def test_sentence_with_punctuation_unchanged(self):
        text = "Він сказав: привіт, світе! Як справи?"
        self.assertEqual(fc(text), text)


class CombinedTests(unittest.TestCase):
    def test_filler_and_duplicate_together(self):
        self.assertEqual(fc("ну е-е я я думаю"), "ну я думаю")


class CleanupLevelTests(unittest.TestCase):
    """feature/clean-mix — рівні агресивності на одному прикладі."""

    SAMPLE = "ну е-е я я думаю"

    def test_off_leaves_text_untouched(self):
        self.assertEqual(fc(self.SAMPLE, "off"), self.SAMPLE)

    def test_light_removes_only_hesitations(self):
        # Лише вокалізація «е-е»; повтор «я я» і вставне «ну» лишаються.
        self.assertEqual(fc(self.SAMPLE, "light"), "ну я я думаю")

    def test_medium_adds_repeat_collapse(self):
        # + стиск повтору «я я» → «я»; «ну» лишається.
        self.assertEqual(fc(self.SAMPLE, "medium"), "ну я думаю")

    def test_strong_adds_discourse_words(self):
        # + прибирає дискурсивне вставне «ну».
        self.assertEqual(fc(self.SAMPLE, "strong"), "я думаю")

    def test_unknown_level_falls_back_to_medium(self):
        self.assertEqual(fc(self.SAMPLE, "bogus"), "ну я думаю")

    def test_default_is_medium(self):
        self.assertEqual(fc(self.SAMPLE), "ну я думаю")

    def test_strong_keeps_meaningful_repeat_word(self):
        # «значить» як дискурсивне зникає, але звичайні слова лишаються.
        self.assertEqual(fc("значить ми йдемо", "strong"), "ми йдемо")
        self.assertEqual(fc("значить ми йдемо", "light"), "значить ми йдемо")


class CleanupLevelResolverTests(unittest.TestCase):
    """cleanup_level_for_cfg — пріоритет явного рівня + сумісність зі старим тумблером."""

    def test_explicit_level_wins(self):
        from whisper_core.config import cleanup_level_for_cfg
        cfg = SimpleNamespace(cleanup_level="strong", filler_cleanup=False)
        self.assertEqual(cleanup_level_for_cfg(cfg), "strong")

    def test_empty_level_derives_from_toggle(self):
        from whisper_core.config import cleanup_level_for_cfg
        self.assertEqual(cleanup_level_for_cfg(
            SimpleNamespace(cleanup_level="", filler_cleanup=True)), "medium")
        self.assertEqual(cleanup_level_for_cfg(
            SimpleNamespace(cleanup_level="", filler_cleanup=False)), "off")

    def test_invalid_level_derives_from_toggle(self):
        from whisper_core.config import cleanup_level_for_cfg
        self.assertEqual(cleanup_level_for_cfg(
            SimpleNamespace(cleanup_level="huge", filler_cleanup=True)), "medium")


class ConfigDefaultTests(unittest.TestCase):
    def test_filler_cleanup_defaults_off(self):
        self.assertFalse(Config().filler_cleanup)

    def test_cleanup_level_defaults_empty(self):
        # Порожнє → похідне від тумблера; за замовчуванням «off».
        from whisper_core.config import cleanup_level_for_cfg
        self.assertEqual(Config().cleanup_level, "")
        self.assertEqual(cleanup_level_for_cfg(Config()), "off")


class PipelineGateTests(unittest.TestCase):
    """Гейт у _work: чистка застосовується ЛИШЕ коли конфіг увімкнено."""

    @staticmethod
    def _controller(filler_cleanup, final="я я хотів"):
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
            cfg=SimpleNamespace(sounds=False, filler_cleanup=filler_cleanup,
                                voice_punctuation=False, language="uk"),
            _busy=True,
        )
        return controller, recorded

    def test_enabled_cleans_text(self):
        from fronts.desktop import app as desktop_app

        controller, recorded = self._controller(True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        self.assertEqual(recorded["final"], "я хотів")
        self.assertFalse(controller._busy)
        self.assertEqual(controller.finished.count, 1)

    def test_disabled_leaves_text_untouched(self):
        from fronts.desktop import app as desktop_app

        controller, recorded = self._controller(False)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        self.assertEqual(recorded["final"], "я я хотів")


if __name__ == "__main__":
    unittest.main()
