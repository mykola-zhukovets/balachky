"""Юніти білінгвальної пам'яті фраз (feature/bilingual-memory).

Перевіряємо сховище (phrases.toml round-trip, add/delete/list), конвеєр заміни
через спільну машинерію terms (apply_glossary): межі слів, відмінки, регістр,
багатослівні фрази, довший-варіант-першим; злиття у словник термінів
(merge_terms_data) і авто-кандидати зі щоденника помилок (bilingual_suggestions).
Без Qt, диск — через tempfile.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from whisper_core import phrasebook
from whisper_core.terms import apply_glossary, build_terms, merge_terms_data


def _tmp():
    d = tempfile.mkdtemp()
    return Path(d) / "phrases.toml"


class StoreTests(unittest.TestCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(phrasebook.read_phrases(_tmp()), {})

    def test_add_read_roundtrip(self):
        p = _tmp()
        self.assertTrue(phrasebook.add_phrase(p, "worktree", "ворктрі"))
        self.assertEqual(phrasebook.read_phrases(p), {"worktree": ["ворктрі"]})

    def test_add_second_variant_to_same_write(self):
        p = _tmp()
        phrasebook.add_phrase(p, "worktree", "ворктрі")
        phrasebook.add_phrase(p, "worktree", "ворк трі")
        self.assertEqual(phrasebook.read_phrases(p),
                         {"worktree": ["ворктрі", "ворк трі"]})

    def test_duplicate_variant_case_insensitive_rejected(self):
        p = _tmp()
        self.assertTrue(phrasebook.add_phrase(p, "worktree", "ворктрі"))
        self.assertFalse(phrasebook.add_phrase(p, "worktree", "ВОРКТРІ"))
        self.assertEqual(phrasebook.read_phrases(p), {"worktree": ["ворктрі"]})

    def test_empty_fields_rejected(self):
        p = _tmp()
        self.assertFalse(phrasebook.add_phrase(p, "", "ворктрі"))
        self.assertFalse(phrasebook.add_phrase(p, "worktree", "  "))
        self.assertEqual(phrasebook.read_phrases(p), {})

    def test_delete(self):
        p = _tmp()
        phrasebook.add_phrase(p, "worktree", "ворктрі")
        self.assertTrue(phrasebook.delete_phrase(p, "worktree"))
        self.assertFalse(phrasebook.delete_phrase(p, "worktree"))
        self.assertEqual(phrasebook.read_phrases(p), {})

    def test_delete_last_removes_file(self):
        p = _tmp()
        phrasebook.add_phrase(p, "worktree", "ворктрі")
        phrasebook.delete_phrase(p, "worktree")
        self.assertFalse(p.exists())   # порожньо → без осиротілого [phrases]

    def test_list_sorted(self):
        p = _tmp()
        phrasebook.add_phrase(p, "worktree", "ворктрі")
        phrasebook.add_phrase(p, "pull request", "пул реквест")
        self.assertEqual(
            phrasebook.list_phrases(p),
            [("pull request", ["пул реквест"]), ("worktree", ["ворктрі"])])

    def test_broken_toml_is_empty(self):
        p = _tmp()
        p.write_text("[phrases]\nx = [unclosed", encoding="utf-8")
        self.assertEqual(phrasebook.read_phrases(p), {})

    def test_quoted_key_roundtrip(self):
        p = _tmp()
        phrasebook.add_phrase(p, "pull request", "пул реквест")
        # ключ із пробілом має лапкуватись; повторне читання його відновлює
        self.assertEqual(phrasebook.read_phrases(p),
                         {"pull request": ["пул реквест"]})


class ReplacementTests(unittest.TestCase):
    def _pb(self, pairs):
        p = _tmp()
        for write, heard in pairs:
            phrasebook.add_phrase(p, write, heard)
        return phrasebook.load_phrasebook(p)

    def test_single_token_replacement(self):
        pb = self._pb([("worktree", "ворктрі")])
        self.assertEqual(apply_glossary("новий ворктрі готовий", pb),
                         "новий worktree готовий")

    def test_case_insensitive(self):
        pb = self._pb([("worktree", "ворктрі")])
        self.assertEqual(apply_glossary("Ворктрі тут", pb), "worktree тут")

    def test_whole_token_only_no_substring(self):
        # межа слова: «ворктрі» всередині «ворктрійка» НЕ чіпається (відмінок/суфікс
        # без окремого варіанта лишається як є — задокументоване обмеження)
        pb = self._pb([("worktree", "ворктрі")])
        self.assertEqual(apply_glossary("ворктрійка велика", pb),
                         "ворктрійка велика")

    def test_multiword_phrase(self):
        pb = self._pb([("pull request", "пул реквест")])
        self.assertEqual(apply_glossary("зроби пул реквест зараз", pb),
                         "зроби pull request зараз")

    def test_longer_variant_wins(self):
        # «ко ворк» має матчитись раніше за «ворк» (сортування за довжиною)
        pb = self._pb([("Cowork", "ко ворк"), ("work", "ворк")])
        self.assertEqual(apply_glossary("це ко ворк", pb), "це Cowork")

    def test_empty_phrasebook_is_noop(self):
        pb = phrasebook.load_phrasebook(_tmp())
        self.assertEqual(apply_glossary("нічого не змінюй", pb),
                         "нічого не змінюй")


class MergeTests(unittest.TestCase):
    def test_merge_terms_and_phrases(self):
        terms = {"GitHub": ["гітхаб"]}
        phrases = {"worktree": ["ворктрі"]}
        merged = build_terms(merge_terms_data(terms, phrases))
        self.assertEqual(apply_glossary("гітхаб і ворктрі", merged),
                         "GitHub і worktree")

    def test_merge_same_canon_unions_variants(self):
        merged = merge_terms_data({"x": ["a"]}, {"x": ["b", "A"]})
        self.assertEqual(merged["x"], ["a", "b"])   # «A» — регістровий дубль «a»

    def test_phrase_write_form_feeds_hotwords(self):
        # інтеграція з STT-підказками: канон фрази потрапляє у hotwords
        pb = phrasebook.load_phrasebook(_tmp())  # порожньо
        p = _tmp()
        phrasebook.add_phrase(p, "worktree", "ворктрі")
        pb = phrasebook.load_phrasebook(p)
        self.assertIn("worktree", pb.hotwords)


class SuggestionTests(unittest.TestCase):
    def _s(self, recognized, corrected):
        return {"recognized": recognized, "corrected": corrected}

    def test_latin_correction_is_candidate(self):
        samples = [self._s("ворктрі", "worktree")] * 2
        rows = phrasebook.bilingual_suggestions(samples=samples)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["was"], "ворктрі")
        self.assertEqual(rows[0]["now"], "worktree")

    def test_cyrillic_only_correction_excluded(self):
        # одномовне кириличне виправлення — не білінгвальна фраза (лишається термінам)
        samples = [self._s("свято", "Свято")] * 2
        self.assertEqual(phrasebook.bilingual_suggestions(samples=samples), [])

    def test_min_count_respected(self):
        samples = [self._s("ворктрі", "worktree")]   # лише раз
        self.assertEqual(phrasebook.bilingual_suggestions(samples=samples), [])


class ControllerMergeTests(unittest.TestCase):
    """Інтеграція: DesktopApp._profile_terms підмішує пам'ять фраз у словник
    термінів ЛИШЕ коли тумблер увімкнено (незалежно від preserve_speech)."""

    @staticmethod
    def _profile():
        d = Path(tempfile.mkdtemp())
        (d / "terms.toml").write_text(
            '[terms]\nGitHub = ["гітхаб"]\n', encoding="utf-8")
        phrasebook.add_phrase(d / "phrases.toml", "worktree", "ворктрі")
        return SimpleNamespace(terms_path=d / "terms.toml",
                               phrases_path=d / "phrases.toml")

    def _terms(self, enabled, preserve=False):
        from fronts.desktop.app import DesktopApp
        me = SimpleNamespace(cfg=SimpleNamespace(
            phrase_memory_enabled=enabled, preserve_speech=preserve))
        return DesktopApp._profile_terms(me, self._profile())

    def test_enabled_merges_phrases(self):
        terms = self._terms(True)
        self.assertEqual(apply_glossary("гітхаб і ворктрі", terms),
                         "GitHub і worktree")
        self.assertIn("worktree", terms.hotwords)   # STT-підказки теж

    def test_disabled_keeps_terms_only(self):
        terms = self._terms(False)
        self.assertEqual(apply_glossary("гітхаб і ворктрі", terms),
                         "GitHub і ворктрі")        # фразу не чіпаємо

    def test_independent_of_preserve_speech(self):
        # preserve_speech про стиль мовлення; пам'ять фраз (терміни) діє попри нього
        terms = self._terms(True, preserve=True)
        self.assertEqual(apply_glossary("ворктрі", terms), "worktree")


if __name__ == "__main__":
    unittest.main()
