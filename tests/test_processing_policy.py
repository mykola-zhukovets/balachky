"""feature/processing-slider — контракт політики рівнів обробки (спека §3, §9).

Джерело правди «позиція → етапи». Тести замикають точну матрицю: Дослівно нічого
не змінює, Без слів-паразитів — лише консервативна чистка, Під документ — верхня
межа з усіма етапами. Плюс міграція старих прапорців і серіалізовність/незмінність.
"""
import unittest
from dataclasses import FrozenInstanceError, asdict

from whisper_core import processing
from whisper_core.processing import ProcessingMode, policy_for_mode
from whisper_core.fillers import apply_filler_cleanup


class PolicyMatrix(unittest.TestCase):
    def test_verbatim_touches_nothing(self):
        p = policy_for_mode(ProcessingMode.VERBATIM)
        self.assertEqual(p.source, "raw")
        self.assertEqual(p.cleanup_level, "off")
        for flag in (p.voice_commands, p.macros, p.autocorrect, p.punctuator,
                     p.context_formatting, p.document_rewrite):
            self.assertFalse(flag)

    def test_fillers_is_conservative_medium_only(self):
        p = policy_for_mode(ProcessingMode.FILLERS)
        self.assertEqual(p.source, "raw")            # обхід словників — «лише філери»
        self.assertEqual(p.cleanup_level, "medium")  # НЕ strong: дискурсивні слова лишаємо
        for flag in (p.voice_commands, p.macros, p.autocorrect, p.punctuator,
                     p.context_formatting, p.document_rewrite):
            self.assertFalse(flag)

    def test_document_is_upper_bound(self):
        p = policy_for_mode(ProcessingMode.DOCUMENT)
        self.assertEqual(p.source, "glossary")
        self.assertEqual(p.cleanup_level, "strong")
        for flag in (p.voice_commands, p.macros, p.autocorrect, p.punctuator,
                     p.context_formatting):
            self.assertTrue(flag)
        # document_rewrite=False: генеративного переписування локальною LLM у
        # конвеєрі ще нема (спека §11 стадія 3), тож не заявляємо його прапорцем,
        # який ніхто не читає (блокер рецензії — «тихий no-op під виглядом фічі»).
        self.assertFalse(p.document_rewrite)

    def test_source_text_selection(self):
        raw, gloss = "сирий", "словниковий"
        self.assertEqual(
            processing.source_text(policy_for_mode(ProcessingMode.VERBATIM), raw, gloss), raw)
        self.assertEqual(
            processing.source_text(policy_for_mode(ProcessingMode.FILLERS), raw, gloss), raw)
        self.assertEqual(
            processing.source_text(policy_for_mode(ProcessingMode.DOCUMENT), raw, gloss), gloss)


class ModeResolution(unittest.TestCase):
    def test_normalize_accepts_str_enum_and_defaults(self):
        self.assertIs(processing.normalize_mode("fillers"), ProcessingMode.FILLERS)
        self.assertIs(processing.normalize_mode(ProcessingMode.DOCUMENT), ProcessingMode.DOCUMENT)
        self.assertIs(processing.normalize_mode("СМІТТЯ"), processing.DEFAULT_MODE)
        self.assertIs(processing.normalize_mode(None), processing.DEFAULT_MODE)
        self.assertIs(processing.normalize_mode(""), processing.DEFAULT_MODE)

    def test_default_is_verbatim(self):
        # найбезпечніша відповідь на скаргу «ШІ сам перефразовує»
        self.assertIs(processing.DEFAULT_MODE, ProcessingMode.VERBATIM)

    def test_index_roundtrip(self):
        for i, mode in enumerate(processing.MODES):
            self.assertEqual(processing.mode_index(mode), i)
            self.assertIs(processing.mode_from_index(i), mode)
        self.assertIs(processing.mode_from_index(99), processing.DEFAULT_MODE)
        self.assertIs(processing.mode_from_index(-1), processing.DEFAULT_MODE)


class PolicyIntegrity(unittest.TestCase):
    def test_frozen_immutable(self):
        p = policy_for_mode(ProcessingMode.VERBATIM)
        with self.assertRaises(FrozenInstanceError):
            p.cleanup_level = "strong"    # type: ignore[misc]

    def test_serializable(self):
        d = asdict(policy_for_mode(ProcessingMode.DOCUMENT))
        self.assertEqual(d["cleanup_level"], "strong")
        self.assertEqual(d["mode"], ProcessingMode.DOCUMENT)

    def test_same_object_each_call(self):
        # політики спільні (незмінні) — не плодимо копії
        self.assertIs(policy_for_mode("verbatim"), policy_for_mode(ProcessingMode.VERBATIM))


class FillersLevelContract(unittest.TestCase):
    """Середня позиція справді відповідає apply_filler_cleanup(..., 'medium') і
    зберігає змістовні дискурсивні слова (їх чистить лише strong = Під документ)."""

    def test_medium_removes_hesitations_and_repeats(self):
        raw = "я я хотів ееее сказати"
        out = apply_filler_cleanup(raw, policy_for_mode(ProcessingMode.FILLERS).cleanup_level)
        self.assertEqual(out, "я хотів сказати")

    def test_medium_keeps_discourse_words(self):
        raw = "ну це власне важливо"       # «ну»/«власне» — зміст, medium їх лишає
        out = apply_filler_cleanup(raw, policy_for_mode(ProcessingMode.FILLERS).cleanup_level)
        self.assertIn("ну", out)
        self.assertIn("власне", out)


class Migration(unittest.TestCase):
    def _m(self, level, preserve=False, autoc=False, punct=False):
        return processing.migrate_dictation_mode(
            level, preserve_speech=preserve, autocorrect_enabled=autoc,
            punctuator_enabled=punct)

    def test_preserve_and_off_to_verbatim(self):
        self.assertIs(self._m("off", preserve=True), ProcessingMode.VERBATIM)

    def test_enhancers_to_document(self):
        self.assertIs(self._m("off", autoc=True), ProcessingMode.DOCUMENT)
        self.assertIs(self._m("off", punct=True), ProcessingMode.DOCUMENT)

    def test_strong_cleanup_downgrades_to_fillers(self):
        # strong стара чистка → «Без слів-паразитів» (medium): strong лишається
        # тільки всередині явно перетворювального «Під документ» (спека §2)
        self.assertIs(self._m("strong"), ProcessingMode.FILLERS)
        self.assertIs(self._m("medium"), ProcessingMode.FILLERS)

    def test_bare_off_to_verbatim(self):
        self.assertIs(self._m("off"), ProcessingMode.VERBATIM)


if __name__ == "__main__":
    unittest.main()
