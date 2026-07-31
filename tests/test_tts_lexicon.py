"""Хвиля 4: словник наголосів/вимови (§6, §11.2).

text_replace застосовується; stress міняє вихід; span-map цілий після підміни;
per-профіль ізоляція; валідація відхиляє криве правило (не втрачає введене);
конфлікт/дубль; pymorphy3 форми з fallback; lexicon_rev міняє cache-key."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import lexicon as L
from whisper_core.tts import timings as T
from whisper_core.tts.normalize import normalize


def _profile():
    return tempfile.mkdtemp(prefix="prof-")


class TestValidation(unittest.TestCase):
    def test_empty_match_rejected(self):
        with self.assertRaises(L.RuleError):
            L.validate_rule("", "щось")

    def test_empty_value_rejected(self):
        with self.assertRaises(L.RuleError):
            L.validate_rule("слово", "")

    def test_regex_mode_rejected_v1(self):
        # СУД БЛОКЕР 3: вільний regex у v1 НЕ підтримується (ReDoS-повнота статичного
        # детектора недосяжна). Будь-який regex → RuleError, reason_key для UI.
        with self.assertRaises(L.RuleError) as ctx:
            L.validate_rule(r"Коро\w+", "Коро́стень", match_mode=L.MATCH_REGEX)
        self.assertEqual(ctx.exception.reason_key, "tts_pron_regex_bad")

    def test_regex_class_all_rejected(self):
        # КЛАС загроз (не один патерн): nested-quantifier, ambiguous alternation,
        # звичайний — усі відхилені, бо regex-режим вимкнено.
        for pat in (r"(a+)+$", r"(a|aa)+$", r"(a|a)*$", r"([a-z", r"проста"):
            with self.assertRaises(L.RuleError):
                L.validate_rule(pat, "x", match_mode=L.MATCH_REGEX)

    def test_regex_rule_not_applied_in_pipeline(self):
        # from_ipc із regex-правилом (старий журнал/імпорт) → pipeline його НЕ застосовує
        # (ReDoS-захист на рівні конвеєра теж)
        pipe = L.PronPipeline([L.PronRule(id="1", match=r"(a+)+", value="БЕЗПЕКА",
                                          match_mode=L.MATCH_REGEX)])
        nr = pipe.apply_text_replace(normalize("aaaa тут"))
        self.assertNotIn("БЕЗПЕКА", nr.text)     # regex не застосовано


class TestStorage(unittest.TestCase):
    def test_learn_and_list(self):
        p = _profile()
        res = L.learn(p, "Коростень", "Коро́стень")
        self.assertEqual(res.status, "learned")
        rules = L.list_rules(p)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].match, "Коростень")

    def test_upsert_updates_value(self):
        # СУД БЛОКЕР 1: повторне збереження того самого слова з НОВИМ значенням →
        # активним стає НОВЕ (upsert), не застрягає старе; статус "updated".
        p = _profile()
        L.learn(p, "Коростень", "Коро́стеньА")
        res = L.learn(p, "Коростень", "Коро́стеньБ")
        self.assertEqual(res.status, "updated")
        rules = L.list_rules(p)
        self.assertEqual(len(rules), 1)              # НЕ дублікат
        self.assertEqual(rules[0].value, "Коро́стеньБ")   # активне — НОВЕ значення

    def test_first_learn_status(self):
        p = _profile()
        self.assertEqual(L.learn(p, "слово", "вимова").status, "learned")

    def test_revoke(self):
        p = _profile()
        r = L.learn(p, "Коростень", "Коро́стень").rule
        rev = L.revoke(p, r.id)
        self.assertEqual(rev.status, "learned")
        self.assertEqual(L.list_rules(p), [])

    def test_revoke_missing_not_learned(self):
        p = _profile()
        self.assertEqual(L.revoke(p, "nope").status, "not_learned")

    def test_per_profile_isolation(self):
        p1, p2 = _profile(), _profile()
        L.learn(p1, "Коростень", "Коро́стень")
        self.assertEqual(len(L.list_rules(p1)), 1)
        self.assertEqual(len(L.list_rules(p2)), 0)   # ізольовано на профіль


class TestTextReplaceSpanMap(unittest.TestCase):
    def test_text_replace_applied(self):
        pipe = L.PronPipeline([L.PronRule(id="1", match="Коростень",
                                          value="Коро́стень")])
        nr = pipe.apply_text_replace(normalize("Коростень тут"))
        self.assertIn("Коро́стень", nr.text)

    def test_span_map_intact_after_replace(self):
        # §11.2 КРИТИЧНЕ: замінене слово мапиться на raw-діапазон ОРИГІНАЛУ (караоке)
        pipe = L.PronPipeline([L.PronRule(id="1", match="Коростень",
                                          value="Коро́стень")])
        nr = pipe.apply_text_replace(normalize("Коростень тут"))
        spans = T.normalized_word_raw_spans(nr)
        self.assertEqual(spans[0], (0, 9))       # «Коро́стень» → raw оригіналу «Коростень»
        self.assertEqual(spans[1], (10, 13))     # «тут» — власний діапазон, не зсунутий

    def test_no_rules_returns_same(self):
        pipe = L.PronPipeline([])
        nr0 = normalize("Коростень тут")
        nr1 = pipe.apply_text_replace(nr0)
        self.assertIs(nr1, nr0)

    def test_number_expansion_plus_replace_spanmap(self):
        # normalize «23»→«двадцять три», потім text_replace іншого слова — span-map цілий
        pipe = L.PronPipeline([L.PronRule(id="1", match="тут", value="ось")])
        nr = pipe.apply_text_replace(normalize("23 тут"))
        spans = T.normalized_word_raw_spans(nr)
        # «двадцять» і «три» досі мапляться на raw «23» (0,2)
        self.assertEqual(spans[0], (0, 2))
        self.assertEqual(spans[1], (0, 2))


class TestStressOverride(unittest.TestCase):
    def test_stress_changes_output(self):
        pipe = L.PronPipeline([L.PronRule(id="1", match="замок", value="за́мок",
                                          correction_type=L.CORRECTION_STRESS)])
        out = pipe.apply_stress("старий замок стоїть")
        self.assertIn("за́мок", out)

    def test_stress_no_rule_unchanged(self):
        pipe = L.PronPipeline([])
        self.assertEqual(pipe.apply_stress("замок"), "замок")


class TestRevision(unittest.TestCase):
    def test_revision_changes_with_rules(self):
        a = L.PronPipeline([]).revision()
        b = L.PronPipeline([L.PronRule(id="1", match="x", value="y")]).revision()
        self.assertNotEqual(a, b)

    def test_lexicon_rev_changes_cache_key(self):
        rev_a = L.PronPipeline([]).revision()
        rev_b = L.PronPipeline([L.PronRule(id="1", match="x", value="y")]).revision()
        ka = T.cache_key("текст", "v1", "r1", "e1", rev_a, 0)
        kb = T.cache_key("текст", "v1", "r1", "e1", rev_b, 0)
        self.assertNotEqual(ka, kb)              # зміна словника → інший кеш-ключ


class TestForms(unittest.TestCase):
    def test_forms_never_crash(self):
        forms = L.generate_forms("Коростень")
        self.assertTrue(forms)                   # завжди принаймні база (fallback)

    def test_real_forms_with_pymorphy(self):
        # СУД БЛОКЕР 4: коли pymorphy3 є — РЕАЛЬНІ відмінкові форми (не лише [word])
        import importlib.util
        if importlib.util.find_spec("pymorphy3") is None:
            self.skipTest("pymorphy3 відсутній — real-forms лише зі встановленим пакетом")
        forms = L.generate_forms("Коростень")
        self.assertGreater(len(forms), 1)        # кілька відмінків, не лише база
        # усі форми — варіанти того самого слова (спільний префікс кореня)
        self.assertTrue(all(f.lower().startswith("коростен") for f in forms))

    def test_forms_empty_word(self):
        self.assertEqual(L.generate_forms(""), [])

    def test_forms_preserve_capitalization(self):
        # хвіст рецензії хв.4: pymorphy3 дає нижній регістр; форми капіталізованого слова
        # мають лишитись капіталізованими (§6.1 «Коростень»→«Коростеня»)
        import importlib.util
        if importlib.util.find_spec("pymorphy3") is None:
            self.skipTest("pymorphy3 відсутній")
        forms = L.generate_forms("Коростень")
        self.assertTrue(all(f[:1].isupper() for f in forms if f))
        # а слово з малої — форми з малої
        low = L.generate_forms("замок")
        self.assertTrue(all(f[:1].islower() for f in low if f))


class TestWorkerIntegration(unittest.TestCase):
    """Наскрізь: synthesize_stream застосовує text_replace зі словника і зберігає
    span-map (raw-координати оригіналу) — караоке Хвилі 2 лишається коректним."""

    def test_lexicon_snapshot_applied_in_worker(self):
        import os as _os
        import tempfile as _tf
        from whisper_core.tts import worker as W
        from whisper_core.tts.engines.fake import FakeTtsEngine
        eng = FakeTtsEngine()
        eng.load("")
        rule = L.PronRule(id="1", match="Коростень", value="Коро́стень")
        events = []
        d = _tf.mkdtemp(prefix="lex-")
        W.synthesize_stream(
            eng, {"id": "g", "text": "Коростень тут", "wav_dir": d,
                  "want_timings": True, "source_start_cp": 0,
                  "lexicon_snapshot": L.PronPipeline([rule]).to_ipc()},
            events.append, lambda: False)
        chunks = [e for e in events if e["type"] == "chunk_ready"]
        # нормалізований текст містить замінене слово (fake echo у normalized_text)
        self.assertIn("Коро́стень", chunks[0]["normalized_text"])
        # span-map: перше слово мапиться на raw оригіналу «Коростень» (0..9)
        wt = chunks[0]["timings"]
        self.assertEqual(wt[0]["raw_start"], 0)
        self.assertEqual(wt[0]["raw_end"], 9)


class TestIpcRoundtrip(unittest.TestCase):
    def test_pipeline_ipc(self):
        pipe = L.PronPipeline([L.PronRule(id="1", match="Коростень",
                                          value="Коро́стень")])
        again = L.PronPipeline.from_ipc(pipe.to_ipc())
        nr = again.apply_text_replace(normalize("Коростень"))
        self.assertIn("Коро́стень", nr.text)


if __name__ == "__main__":
    unittest.main()
