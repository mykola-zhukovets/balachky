"""feature/punctuation-plus — тести двох opt-in кроків постобробки STT:
автокорекція одруків (symspellpy + частотний словник) і пунктуатор/ITN
(punctuators). Покрито: чисту логіку корекції із захистом слів профілю,
функції доступності/graceful degradation, конфіг-дефолти, шляхи компонентів,
витяг захищених слів і гейт конвеєра постобробки.

Стиль — як test_filler_cleanup.py / test_voice_punctuation.py.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core import autocorrect, punctuator, paths
from whisper_core.config import Config
from whisper_core.terms import Terms


# Крихітний частотний словник (symspellpy-формат «слово частота» на рядок).
_FIXTURE = """привіт 1000
світ 900
питання 800
балачки 700
комп'ютер 600
нікого 500
"""


def _fixture_dict() -> Path:
    f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8")
    f.write(_FIXTURE)
    f.close()
    return Path(f.name)


class ConfigDefaultTests(unittest.TestCase):
    def test_autocorrect_defaults_off(self):
        self.assertFalse(Config().autocorrect_enabled)

    def test_punctuator_defaults_off(self):
        self.assertFalse(Config().punctuator_enabled)

    def test_flags_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            c = Config()
            c.autocorrect_enabled = True
            c.punctuator_enabled = True
            c.save(p)
            loaded = Config.load(p)
            self.assertTrue(loaded.autocorrect_enabled)
            self.assertTrue(loaded.punctuator_enabled)


class PathsTests(unittest.TestCase):
    def test_component_paths_under_user_dir(self):
        root = paths.user_dir()
        self.assertEqual(paths.autocorrect_dict_path().parent, root / "components")
        self.assertEqual(paths.punctuator_model_dir(), root / "components" / "punctuator")


class AutocorrectAvailabilityTests(unittest.TestCase):
    def test_symspell_available(self):
        # symspellpy стоїть у venv → True; тест лишається валідним і як
        # документація очікуваного контракту функції
        self.assertIsInstance(autocorrect.symspell_available(), bool)

    def test_dictionary_available_true_false(self):
        p = _fixture_dict()
        try:
            self.assertTrue(autocorrect.dictionary_available(p))
        finally:
            p.unlink()
        self.assertFalse(autocorrect.dictionary_available(p))       # уже видалено
        self.assertFalse(autocorrect.dictionary_available("Z:/nope/missing.txt"))

    def test_available_requires_both(self):
        p = _fixture_dict()
        try:
            with patch.object(autocorrect, "symspell_available", return_value=True):
                self.assertTrue(autocorrect.available(p))
            with patch.object(autocorrect, "symspell_available", return_value=False):
                self.assertFalse(autocorrect.available(p))
        finally:
            p.unlink()

    def test_load_corrector_none_without_package(self):
        with patch.object(autocorrect, "symspell_available", return_value=False):
            self.assertIsNone(autocorrect.load_corrector("whatever.txt"))

    def test_load_corrector_none_missing_dict(self):
        # пакет є, але файла словника немає → None (без винятку)
        if not autocorrect.symspell_available():
            self.skipTest("symspellpy не встановлено")
        self.assertIsNone(autocorrect.load_corrector("Z:/nope/missing.txt"))


class AutocorrectLogicTests(unittest.TestCase):
    """Реальна корекція на fixture-словнику (потребує symspellpy)."""

    @classmethod
    def setUpClass(cls):
        if not autocorrect.symspell_available():
            raise unittest.SkipTest("symspellpy не встановлено")
        cls.dict_path = _fixture_dict()
        cls.corr = autocorrect.load_corrector(cls.dict_path)

    @classmethod
    def tearDownClass(cls):
        cls.dict_path.unlink(missing_ok=True)

    def test_corrector_built(self):
        self.assertIsNotNone(self.corr)

    def test_typo_corrected(self):
        self.assertEqual(self.corr.apply("привт"), "привіт")

    def test_known_word_untouched(self):
        self.assertEqual(self.corr.apply("світ"), "світ")

    def test_short_word_untouched(self):
        # «свт» — 3 літери (< MIN_WORD_LEN), не чіпаємо навіть за наявності «світ»
        self.assertEqual(self.corr.apply("свт"), "свт")

    def test_case_preserved_capital(self):
        self.assertEqual(self.corr.apply("Привт"), "Привіт")

    def test_case_preserved_upper(self):
        self.assertEqual(self.corr.apply("ПРИВТ"), "ПРИВІТ")

    def test_apostrophe_word_is_one_token(self):
        # «комп'ютар» → «комп'ютер» (апостроф не розриває токен)
        self.assertEqual(self.corr.apply("комп'ютар"), "комп'ютер")

    def test_protected_word_not_corrected(self):
        # «балачкі» без захисту виправилось би на «балачки»; у захисті — лишається
        protected = frozenset({"балачкі"})
        self.assertEqual(self.corr.apply("балачкі", protected), "балачкі")
        self.assertEqual(self.corr.apply("балачкі"), "балачки")   # без захисту — виправляє

    def test_empty_text(self):
        self.assertEqual(self.corr.apply(""), "")

    def test_unknown_no_candidate_untouched(self):
        self.assertEqual(self.corr.apply("xyzqwmнн"), "xyzqwmнн")

    def test_surrounding_text_and_punctuation_kept(self):
        self.assertEqual(self.corr.apply("привт, світ!"), "привіт, світ!")


class PunctuatorGracefulTests(unittest.TestCase):
    def test_availability_returns_bool(self):
        self.assertIsInstance(punctuator.punctuators_available(), bool)

    def test_model_unavailable_in_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(punctuator.model_available(d))
            self.assertFalse(punctuator.available(d))

    def test_model_marker_makes_available(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "READY").write_text("ok", encoding="utf-8")
            self.assertTrue(punctuator.model_available(d))

    def test_load_model_none_when_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(punctuator.load_model(d))

    def test_apply_none_model_returns_text(self):
        self.assertEqual(punctuator.apply_punctuation("привіт світ", None), "привіт світ")

    def test_apply_empty_text(self):
        self.assertEqual(punctuator.apply_punctuation("", object()), "")

    def test_apply_uses_model_infer(self):
        # мок-модель: infer повертає список списків речень → склеюємо
        model = SimpleNamespace(infer=lambda batch: [["Привіт, світе.", "Як справи?"]])
        self.assertEqual(
            punctuator.apply_punctuation("привіт світе як справи", model),
            "Привіт, світе. Як справи?")

    def test_apply_infer_failure_falls_back(self):
        def boom(_batch):
            raise RuntimeError("onnx впав")
        model = SimpleNamespace(infer=boom)
        self.assertEqual(punctuator.apply_punctuation("сирий текст", model), "сирий текст")

    def test_download_raises_without_package(self):
        with patch.object(punctuator, "punctuators_available", return_value=False):
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(punctuator.PunctuatorDownloadError):
                    punctuator.download_and_install(d)


class ProtectedWordsTests(unittest.TestCase):
    def test_extracts_canons_and_variants(self):
        from fronts.desktop.app import _profile_protected_words
        terms = Terms(hotwords="Балачки, Ко ворк",
                      variant_map={"ко ворк": "Ко ворк", "балачкі": "Балачки"})
        protected = _profile_protected_words(terms)
        self.assertIn("балачки", protected)
        self.assertIn("ко", protected)
        self.assertIn("ворк", protected)
        self.assertIn("балачкі", protected)

    def test_none_terms_safe(self):
        from fronts.desktop.app import _profile_protected_words
        self.assertEqual(_profile_protected_words(None), frozenset())


class EnhancementGateTests(unittest.TestCase):
    """Гейт _apply_text_enhancements: кроки застосовуються ЛИШЕ коли конфіг
    увімкнено; порядок — автокорекція, потім пунктуатор."""

    @staticmethod
    def _fake(cfg_flags, calls):
        cfg = SimpleNamespace(language="uk", **cfg_flags)
        return SimpleNamespace(
            cfg=cfg,
            _run_autocorrect=lambda final, terms: (calls.append("ac"), final + "|ac")[1],
            _run_punctuator=lambda final: (calls.append("pn"), final + "|pn")[1],
        )

    def test_both_disabled_no_calls(self):
        from fronts.desktop.app import DesktopApp
        calls = []
        fake = self._fake(dict(autocorrect_enabled=False, punctuator_enabled=False), calls)
        out = DesktopApp._apply_text_enhancements(fake, "текст", None)
        self.assertEqual(out, "текст")
        self.assertEqual(calls, [])

    def test_autocorrect_only(self):
        from fronts.desktop.app import DesktopApp
        calls = []
        fake = self._fake(dict(autocorrect_enabled=True, punctuator_enabled=False), calls)
        out = DesktopApp._apply_text_enhancements(fake, "текст", None)
        self.assertEqual(out, "текст|ac")
        self.assertEqual(calls, ["ac"])

    def test_order_autocorrect_then_punctuator(self):
        from fronts.desktop.app import DesktopApp
        calls = []
        fake = self._fake(dict(autocorrect_enabled=True, punctuator_enabled=True), calls)
        out = DesktopApp._apply_text_enhancements(fake, "текст", None)
        self.assertEqual(out, "текст|ac|pn")
        self.assertEqual(calls, ["ac", "pn"])

    def test_empty_final_short_circuits(self):
        from fronts.desktop.app import DesktopApp
        calls = []
        fake = self._fake(dict(autocorrect_enabled=True, punctuator_enabled=True), calls)
        self.assertEqual(DesktopApp._apply_text_enhancements(fake, "", None), "")
        self.assertEqual(calls, [])

    def test_preserve_speech_bypasses_both(self):
        # feature/edit-pack: preserve_speech ОБХОДИТЬ обидва кроки, навіть коли
        # автокорекція й пунктуатор увімкнені — текст лишається як є.
        from fronts.desktop.app import DesktopApp
        calls = []
        fake = self._fake(dict(autocorrect_enabled=True, punctuator_enabled=True,
                               preserve_speech=True), calls)
        out = DesktopApp._apply_text_enhancements(fake, "мій суржик", None)
        self.assertEqual(out, "мій суржик")
        self.assertEqual(calls, [])

    def test_preserve_speech_off_still_enhances(self):
        # Явний False лишає поведінку конвеєра незмінною.
        from fronts.desktop.app import DesktopApp
        calls = []
        fake = self._fake(dict(autocorrect_enabled=True, punctuator_enabled=True,
                               preserve_speech=False), calls)
        out = DesktopApp._apply_text_enhancements(fake, "текст", None)
        self.assertEqual(out, "текст|ac|pn")
        self.assertEqual(calls, ["ac", "pn"])


class PolicyDrivenEnhancementTests(unittest.TestCase):
    """feature/processing-slider (блокер №1): коли передано політику рівня обробки,
    що ВМИКАЄ автокорекцію/пунктуатор — вирішує САМЕ політика, а не старі тумблери
    cfg (спека §3, §5). «Під документ» на типовому профілі (тумблери вимкнені) має
    все одно запускати обидва кроки; «Дослівно» — жодного навіть із увімкненими."""

    @staticmethod
    def _fake(cfg_flags, calls):
        cfg = SimpleNamespace(language="uk", **cfg_flags)
        return SimpleNamespace(
            cfg=cfg,
            _run_autocorrect=lambda final, terms: (calls.append("ac"), final + "|ac")[1],
            _run_punctuator=lambda final: (calls.append("pn"), final + "|pn")[1],
        )

    def test_document_policy_runs_both_despite_toggles_off(self):
        from fronts.desktop.app import DesktopApp
        from whisper_core.processing import policy_for_mode, ProcessingMode
        calls = []
        fake = self._fake(dict(autocorrect_enabled=False, punctuator_enabled=False), calls)
        out = DesktopApp._apply_text_enhancements(
            fake, "текст", None, policy_for_mode(ProcessingMode.DOCUMENT))
        self.assertEqual(out, "текст|ac|pn")
        self.assertEqual(calls, ["ac", "pn"])

    def test_verbatim_policy_runs_neither_despite_toggles_on(self):
        from fronts.desktop.app import DesktopApp
        from whisper_core.processing import policy_for_mode, ProcessingMode
        calls = []
        fake = self._fake(dict(autocorrect_enabled=True, punctuator_enabled=True), calls)
        out = DesktopApp._apply_text_enhancements(
            fake, "текст", None, policy_for_mode(ProcessingMode.VERBATIM))
        self.assertEqual(out, "текст")
        self.assertEqual(calls, [])

    def test_fillers_policy_runs_neither(self):
        from fronts.desktop.app import DesktopApp
        from whisper_core.processing import policy_for_mode, ProcessingMode
        calls = []
        fake = self._fake(dict(autocorrect_enabled=True, punctuator_enabled=True), calls)
        out = DesktopApp._apply_text_enhancements(
            fake, "текст", None, policy_for_mode(ProcessingMode.FILLERS))
        self.assertEqual(out, "текст")
        self.assertEqual(calls, [])

    def test_document_policy_still_respects_preserve_speech(self):
        # «Не виправляй мою мову» лишається запобіжником навіть під «Під документ».
        from fronts.desktop.app import DesktopApp
        from whisper_core.processing import policy_for_mode, ProcessingMode
        calls = []
        fake = self._fake(dict(autocorrect_enabled=False, punctuator_enabled=False,
                               preserve_speech=True), calls)
        out = DesktopApp._apply_text_enhancements(
            fake, "мій суржик", None, policy_for_mode(ProcessingMode.DOCUMENT))
        self.assertEqual(out, "мій суржик")
        self.assertEqual(calls, [])


class RunAutocorrectIntegrationTests(unittest.TestCase):
    """_run_autocorrect: доступність гейтить крок; захист слів профілю діє."""

    def test_skips_when_unavailable(self):
        from fronts.desktop.app import DesktopApp
        fake = SimpleNamespace(cfg=SimpleNamespace())
        with patch.object(autocorrect, "available", return_value=False):
            self.assertEqual(DesktopApp._run_autocorrect(fake, "привт", None), "привт")

    def test_applies_and_protects(self):
        if not autocorrect.symspell_available():
            self.skipTest("symspellpy не встановлено")
        from fronts.desktop.app import DesktopApp
        dict_path = _fixture_dict()
        try:
            fake = SimpleNamespace(cfg=SimpleNamespace())
            terms = Terms(hotwords="", variant_map={"балачкі": "Балачки"})
            with patch.object(paths, "autocorrect_dict_path", return_value=dict_path):
                # «привт» виправляється, «балачкі» (слово профілю) — ні
                out = DesktopApp._run_autocorrect(fake, "привт балачкі", terms)
            self.assertEqual(out, "привіт балачкі")
        finally:
            dict_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
