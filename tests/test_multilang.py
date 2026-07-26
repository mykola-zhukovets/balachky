"""feature/multilang-asr (Т44) — вибір мови розпізнавання з-поміж усіх, що
вміє Whisper, і безпечне «Автоматично» (language=None).

Покриває чотири вимоги задачі:
  • перелік мов збігається з faster-whisper (нічого не загубили/не вигадали);
  • нормалізація вибору в аргумент рушія: "auto"/порожнє/невідоме → None;
  • рушій справді передає нормалізований код у model.transcribe(language=);
  • config зберігає й читає вибір (uk / auto / будь-яка мова);
  • конвеєр чистки не ламається й не псує НЕ-український текст.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core import languages as L
from whisper_core.config import Config
from whisper_core.engine import Engine
from whisper_core.fillers import apply_filler_cleanup


class LanguageCatalogTests(unittest.TestCase):
    def test_catalog_matches_faster_whisper_exactly(self):
        from faster_whisper.tokenizer import _LANGUAGE_CODES
        mine = {code for code, *_ in L.LANGUAGES}
        self.assertEqual(mine, set(_LANGUAGE_CODES),
                         "перелік мов розійшовся з faster-whisper — онови languages.py")

    def test_no_duplicate_codes(self):
        codes = [code for code, *_ in L.LANGUAGES]
        self.assertEqual(len(codes), len(set(codes)))

    def test_every_language_has_both_names(self):
        for code, uk, en in L.LANGUAGES:
            self.assertTrue(uk and en, code)

    def test_is_supported(self):
        self.assertTrue(L.is_supported("uk"))
        self.assertTrue(L.is_supported("zh"))
        self.assertFalse(L.is_supported("auto"))
        self.assertFalse(L.is_supported("xx"))
        self.assertFalse(L.is_supported(None))


class TranscribeArgTests(unittest.TestCase):
    def test_auto_and_empty_map_to_none(self):
        for value in (L.AUTO, "auto", "", None, "   ", "xx", "zz-lang"):
            self.assertIsNone(L.transcribe_language_arg(value), value)

    def test_known_codes_pass_through(self):
        self.assertEqual(L.transcribe_language_arg("uk"), "uk")
        self.assertEqual(L.transcribe_language_arg("de"), "de")
        self.assertEqual(L.transcribe_language_arg("yue"), "yue")

    def test_normalizes_case_and_whitespace(self):
        self.assertEqual(L.transcribe_language_arg("  DE "), "de")
        self.assertEqual(L.transcribe_language_arg("EN"), "en")


class OrderedForUiTests(unittest.TestCase):
    def test_pins_ukrainian_and_english_first(self):
        order = L.ordered_for_ui("uk")
        self.assertEqual([code for code, _ in order[:2]], ["uk", "en"])
        self.assertEqual(len(order), len(L.LANGUAGES))

    def test_ukrainian_tail_is_alphabetical(self):
        order = L.ordered_for_ui("uk")
        names = [name for _, name in order[2:]]
        self.assertEqual(names, sorted(names, key=L._uk_sort_key))
        # Азербайджанська передує Албанській (з < л) — доказ укр-колації
        self.assertLess(names.index("Азербайджанська"), names.index("Албанська"))

    def test_english_names_when_ui_english(self):
        order = L.ordered_for_ui("en")
        self.assertEqual([code for code, _ in order[:2]], ["uk", "en"])
        self.assertEqual(dict(order)["de"], "German")

    def test_display_name_fallback_for_unknown(self):
        self.assertEqual(L.display_name("xx"), "xx")


class EngineLanguageTests(unittest.TestCase):
    """Рушій передає нормалізований код у faster-whisper.transcribe(language=)."""

    def _transcribe_language(self, cfg_language):
        cfg = Config()
        cfg.language = cfg_language
        with patch("whisper_core.engine.WhisperModel") as model:
            model.return_value.transcribe.return_value = (
                [], SimpleNamespace(duration=1.0))
            Engine(cfg).transcribe("audio.wav")
        return model.return_value.transcribe.call_args.kwargs["language"]

    def test_default_ukrainian_passes_uk(self):
        self.assertEqual(self._transcribe_language("uk"), "uk")

    def test_auto_passes_none(self):
        self.assertIsNone(self._transcribe_language("auto"))

    def test_other_language_passes_code(self):
        self.assertEqual(self._transcribe_language("de"), "de")

    def test_unknown_language_falls_back_to_none(self):
        self.assertIsNone(self._transcribe_language("zz"))


class ConfigRoundTripTests(unittest.TestCase):
    def test_default_language_unchanged(self):
        self.assertEqual(Config().language, "uk")

    def test_language_survives_save_and_load(self):
        for value in ("uk", "auto", "de", "yue"):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.toml"
                cfg = Config()
                cfg.language = value
                cfg.save(path)
                self.assertEqual(Config.load(path).language, value)


class NonUkrainianCleanupSafetyTests(unittest.TestCase):
    """Чистка філерів укр-специфічна (регекси матчать лише кириличні токени).
    Для НЕ-кириличного тексту вона — безпечний no-op: не падає й не псує зміст.
    Це навмисна межа: багатомовну чистку філерів у цій задачі не робимо."""

    _NON_CYRILLIC = (
        "Hello hello world",
        "Das ist ein ein Test",
        "The quick brown fox jumps",
        "Bonjour le monde",
        "これはテストです",
        "这是一个测试",
    )

    def test_non_cyrillic_text_unchanged_on_every_level(self):
        for level in ("off", "light", "medium", "strong"):
            for text in self._NON_CYRILLIC:
                self.assertEqual(apply_filler_cleanup(text, level), text,
                                 (level, text))

    def test_pipeline_does_not_raise_on_non_ukrainian(self):
        # весь набір рівнів × мов — жодного винятку (контракт «не крашить конвеєр»)
        for level in ("off", "light", "medium", "strong"):
            for text in self._NON_CYRILLIC:
                self.assertIsInstance(apply_filler_cleanup(text, level), str)


if __name__ == "__main__":
    unittest.main()
