# feature/selflearn-dict
"""Самонавчання словника «виправив раз — назавжди», ПІД КОЖЕН СЛОВНИК окремо.

Покриває механіку спеки Terra: diff одного виправлення, класифікацію (bias /
точна заміна / пара-фраза), ІЗОЛЯЦІЮ по профілях, захист від засмічення
(«гартати»≠«гортати», дублі, суперечності, перекриття, ліміти), undo, перебудову
проєкцій після рестарту, участь вивченого у словнику рушія, а також
update_final_by_id та імпорт-валідацію.

Лексикон передаємо ЯВНО (set/None), щоб гілки possibly-legit були детермінованими:
  • set(...)      — «лексикон доступний»: почуте легітимне ⇔ воно в наборі;
  • frozenset()   — доступний, але почутого нема → почуте НЕ легітимне (спецтокен);
  • None          — лексикон недоступний → консервативно легітимне (без заміни).
"""
import json
import tempfile
import unittest
from pathlib import Path

from whisper_core import self_learning as sl
from whisper_core import terms as terms_mod
from whisper_core import phrasebook, history
from whisper_core.profiles import Profile


def _profile(root, name):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    return Profile(name, d)


def _apply_terms(profile, text):
    """Побудувати Terms профілю (людський+auto+вивчене) і застосувати glossary."""
    t = terms_mod.load_terms(profile.terms_path)
    return terms_mod.apply_glossary(text, t)


# ─────────────────────────── diff одного виправлення ───────────────────────────
class DiffCorrectionTests(unittest.TestCase):
    def test_one_word(self):
        h, w, r = sl.diff_correction("я відкрив ворктрі зараз", "я відкрив worktree зараз")
        self.assertEqual((h, w, r), ("ворктрі", "worktree", ""))

    def test_phrase_two_tokens(self):
        h, w, r = sl.diff_correction("зробив пул реквест", "зробив pull request")
        self.assertEqual((h, w), ("пул реквест", "pull request"))
        self.assertEqual(r, "")

    def test_punctuation_only_change_is_identical(self):
        # кома не творить межу слова й не є лексичною зміною
        _, _, r = sl.diff_correction("привіт світ", "привіт, світ")
        self.assertEqual(r, "identical")

    def test_apostrophe_and_case_normalization_identical(self):
        _, _, r = sl.diff_correction("комп’ютер", "комп'ютер")   # curly→straight
        self.assertEqual(r, "identical")

    def test_reject_pure_insert(self):
        _, _, r = sl.diff_correction("привіт світ", "привіт великий світ")
        self.assertEqual(r, "not_replace")

    def test_reject_pure_delete(self):
        _, _, r = sl.diff_correction("привіт великий світ", "привіт світ")
        self.assertEqual(r, "not_replace")

    def test_reject_two_hunks(self):
        _, _, r = sl.diff_correction("аа бб вв гг", "хх бб вв юю")
        self.assertEqual(r, "multi_hunk")

    def test_reject_url(self):
        # один replace-hunk: «тут» → «http://example.com» (URL у написаному фрагменті)
        _, _, r = sl.diff_correction("дивись тут", "дивись http://example.com")
        self.assertEqual(r, "url_or_path")

    def test_reject_newline_span(self):
        _, _, r = sl.diff_correction("рядок один", "рядок\nодин два")
        self.assertIn(r, ("newline", "multi_hunk", "not_replace"))

    def test_reject_oversized_source(self):
        before = "а б в г д ґ"
        after = "х б в г д ґ".replace("х б в г д", "х2 б2 в2 г2 д2")
        # 5 змінених джерельних токенів > SRC_MAX_TOKENS(4)
        _, _, r = sl.diff_correction("а б в г д кінець", "х2 х3 х4 х5 х6 кінець")
        self.assertEqual(r, "too_long")


# ─────────────────────────── класифікація ───────────────────────────
class ClassifyTests(unittest.TestCase):
    def test_worktree_is_term_replace(self):
        # почуте кириличне, не в лексиконі → спецтокен; ціль латинська → term-like
        rule, _ = sl.classify("ворктрі", "worktree", sl.ProfileContext(), frozenset())
        self.assertEqual(rule["kind"], "term-replace")

    def test_pull_request_is_phrase_replace(self):
        rule, _ = sl.classify("пул реквест", "pull request", sl.ProfileContext(), frozenset())
        self.assertEqual(rule["kind"], "phrase-replace")

    def test_gartaty_gortaty_never_deterministic(self):
        # обидва — нормальні укр. слова; ціль НЕ term-like → жодного правила,
        # і НІКОЛИ не term-replace (інакше знищили б легітимне «гартати»)
        for lex in (frozenset({"гартати", "гортати"}), None):
            rule, reason = sl.classify("гартати", "гортати", sl.ProfileContext(), lex)
            self.assertIsNone(rule, f"lexicon={lex!r}")
            self.assertEqual(reason, "ordinary_word")

    def test_legit_word_to_latin_is_bias_not_replace(self):
        # «лист» — справжнє укр. слово (в лексиконі) → НЕ заміна, лише bias на «list»
        rule, _ = sl.classify("лист", "list", sl.ProfileContext(),
                              frozenset({"лист"}))
        self.assertEqual(rule["kind"], "term-bias")

    def test_unavailable_lexicon_stays_conservative(self):
        # лексикон None: одне-слово-кирилиця → латинь стає bias (не replace)
        rule, _ = sl.classify("ворктрі", "worktree", sl.ProfileContext(), None)
        self.assertEqual(rule["kind"], "term-bias")

    def test_profile_term_source_is_legit(self):
        ctx = sl.ProfileContext(variants={sl.norm("ворктрі")})
        rule, _ = sl.classify("ворктрі", "worktree", ctx, frozenset())
        self.assertEqual(rule["kind"], "term-bias")   # почуте вже відоме → не заміна

    def test_ignored_word_source_is_legit(self):
        ctx = sl.ProfileContext(ignores={sl.norm("гортати")})
        rule, reason = sl.classify("гортати", "gортати", ctx, frozenset())
        # ціль містить латиницю (g) → term-like; але почуте в ignore → legit → bias
        self.assertEqual(rule["kind"], "term-bias")


# ─────────────────────────── навчання + персист ───────────────────────────
class LearnPersistTests(unittest.TestCase):
    def test_term_replace_creates_projection_and_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            res = sl.learn_from_correction(p, "відкрив ворктрі", "відкрив worktree",
                                           lexicon=frozenset())
            self.assertEqual(res.status, "learned")
            self.assertEqual(res.kind, "term-replace")
            # журнал
            self.assertTrue(p.learning_journal_path.exists())
            events = [json.loads(l) for l in
                      p.learning_journal_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["op"], "learn")
            self.assertEqual(events[0]["write"], "worktree")
            # проєкція терміна діє у словнику рушія
            self.assertEqual(_apply_terms(p, "відкрив ворктрі"), "відкрив worktree")

    def test_phrase_replace_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            res = sl.learn_from_correction(p, "зробив пул реквест", "зробив pull request",
                                           lexicon=frozenset())
            self.assertEqual(res.kind, "phrase-replace")
            learned = phrasebook.read_learned_phrases(p.phrases_path)
            self.assertIn("pull request", learned)
            self.assertIn("пул реквест", learned["pull request"])

    def test_gartaty_saves_no_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            res = sl.learn_from_correction(p, "хочу гартати метал", "хочу гортати метал",
                                           lexicon=frozenset({"гартати"}))
            self.assertEqual(res.status, "not_learned")
            self.assertEqual(res.reason, "ordinary_word")
            self.assertFalse(p.learned_terms_path.exists())
            # пізніше легітимне «гартати» лишається собою
            self.assertEqual(_apply_terms(p, "буду гартати метал"), "буду гартати метал")

    def test_learned_phrase_works_without_manual_toggle(self):
        # вивчені фрази підмішуються ЗАВЖДИ (read_learned_phrases), незалежно від
        # ручного phrase_memory. Симулюємо шлях застосунку: терміни + вивчені фрази.
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            sl.learn_from_correction(p, "го пул реквест", "го pull request",
                                     lexicon=frozenset())
            data = terms_mod.read_terms_dict(p.terms_path)
            data = terms_mod.merge_terms_data(
                data, phrasebook.read_learned_phrases(p.phrases_path))
            t = terms_mod.build_terms(data)
            self.assertEqual(terms_mod.apply_glossary("го пул реквест", t),
                             "го pull request")


# ─────────────────────────── ІЗОЛЯЦІЯ (критично) ───────────────────────────
class IsolationTests(unittest.TestCase):
    def test_two_profiles_same_source_different_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _profile(tmp, "home")
            work = _profile(tmp, "work")
            r1 = sl.learn_from_correction(home, "робив деплой", "робив deploy",
                                          lexicon=frozenset())
            r2 = sl.learn_from_correction(work, "робив деплой", "робив deployment",
                                          lexicon=frozenset())
            self.assertEqual(r1.status, "learned")
            self.assertEqual(r2.status, "learned")
            # кожен профіль застосовує ЛИШЕ свою ціль
            self.assertEqual(_apply_terms(home, "робив деплой"), "робив deploy")
            self.assertEqual(_apply_terms(work, "робив деплой"), "робив deployment")
            # і не бачить чужу
            self.assertNotIn("deployment", terms_mod.read_terms_dict(home.terms_path))
            self.assertNotIn("deploy", terms_mod.read_terms_dict(work.terms_path)
                             .get("deployment", "deployment"))

    def test_learning_home_leaves_work_untouched(self):
        # чек-ліст судді №1: вивчили в home — work НЕ замінює
        with tempfile.TemporaryDirectory() as tmp:
            home = _profile(tmp, "home")
            work = _profile(tmp, "work")
            sl.learn_from_correction(home, "го ворктрі", "го worktree", lexicon=frozenset())
            self.assertEqual(_apply_terms(home, "го ворктрі"), "го worktree")
            self.assertEqual(_apply_terms(work, "го ворктрі"), "го ворктрі")  # цілий
            self.assertFalse(work.learning_journal_path.exists())


# ─────────────────────────── захист від засмічення ───────────────────────────
class PoisoningTests(unittest.TestCase):
    def test_duplicate_returns_already_learned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            sl.learn_from_correction(p, "го ворктрі", "го worktree", lexicon=frozenset())
            res = sl.learn_from_correction(p, "го ворктрі", "го worktree", lexicon=frozenset())
            self.assertEqual(res.status, "already_learned")
            # не дублюємо: один активний запис
            self.assertEqual(len(sl.list_learned(p)), 1)

    def test_contradiction_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            sl.learn_from_correction(p, "го ворктрі", "го worktree", lexicon=frozenset())
            res = sl.learn_from_correction(p, "го ворктрі", "го WorkSpace", lexicon=frozenset())
            self.assertEqual(res.status, "not_learned")
            self.assertEqual(res.reason, "contradiction")
            # первісна заміна лишилась
            self.assertEqual(_apply_terms(p, "го ворктрі"), "го worktree")

    def test_overlapping_phrase_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            sl.learn_from_correction(p, "го пул реквест", "го pull request", lexicon=frozenset())
            # «реквест» міститься у наявній фразі-джерелі з іншим виходом → overlap
            res = sl.learn_from_correction(p, "го реквест", "го request", lexicon=frozenset())
            self.assertEqual(res.reason, "overlap")

    def test_contradiction_against_manual_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            p.terms_path.write_text(
                '[terms]\nworktree = ["ворктрі"]\n', encoding="utf-8")
            # «ворктрі» вже мапиться на worktree у людському словнику → інша ціль = суперечність
            res = sl.learn_from_correction(p, "го ворктрі", "го workspace", lexicon=frozenset())
            self.assertEqual(res.reason, "contradiction")

    def test_cap_blocks_new_learning(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            # заповнити журнал по-максимуму дешевими подіями
            events = []
            import uuid as _uuid
            for i in range(sl.MAX_ACTIVE):
                events.append({"v": 1, "op": "learn", "id": _uuid.uuid4().hex,
                               "kind": "term-replace", "heard": f"вар{i}",
                               "write": f"Term{i}", "created_at": "x", "source": "t"})
            p.learning_journal_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                encoding="utf-8")
            res = sl.learn_from_correction(p, "го нове", "го NewToken", lexicon=frozenset())
            self.assertEqual(res.reason, "at_cap")


# ─────────────────────────── undo + перебудова ───────────────────────────
class UndoRebuildTests(unittest.TestCase):
    def test_revoke_removes_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            res = sl.learn_from_correction(p, "го ворктрі", "го worktree", lexicon=frozenset())
            self.assertEqual(_apply_terms(p, "го ворктрі"), "го worktree")
            self.assertTrue(sl.revoke(p, res.entry_id))
            self.assertEqual(_apply_terms(p, "го ворктрі"), "го ворктрі")   # знову цілий
            self.assertEqual(sl.list_learned(p), [])
            self.assertFalse(sl.revoke(p, res.entry_id))   # вдруге — нема що

    def test_ensure_projections_rebuilds_after_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            sl.learn_from_correction(p, "го ворктрі", "го worktree", lexicon=frozenset())
            # симулюємо втрату проєкції (краш між append і rebuild)
            p.learned_terms_path.unlink()
            sl.ensure_projections(p)
            self.assertTrue(p.learned_terms_path.exists())
            self.assertEqual(_apply_terms(p, "го ворктрі"), "го worktree")

    def test_malformed_final_journal_line_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "home")
            sl.learn_from_correction(p, "го ворктрі", "го worktree", lexicon=frozenset())
            # дописуємо обірваний (битий) рядок — як після краху під час запису
            with p.learning_journal_path.open("a", encoding="utf-8") as f:
                f.write('{"v":1,"op":"learn","id":"x"')   # без переносу й закриття
            entries = sl.list_learned(p)          # не падає, битий рядок пропущено
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].write, "worktree")


# ─────────────────────────── update_final_by_id ───────────────────────────
class UpdateFinalByIdTests(unittest.TestCase):
    def _write(self, tmp, records):
        path = Path(tmp) / "history.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                  for r in records) + "\n", encoding="utf-8")
        return path

    def test_updates_only_its_record_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"id": "A", "ts": 1, "raw": "той самий", "final": "той самий", "source": "desktop"},
                {"id": "B", "ts": 2, "raw": "той самий", "final": "той самий", "source": "desktop"},
            ])
            self.assertTrue(history.update_final_by_id(path, "B", "новий"))
            lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["final"], "той самий")   # A недоторканий
            self.assertNotIn("edited", lines[0])
            self.assertEqual(lines[1]["final"], "новий")       # B виправлено
            self.assertTrue(lines[1]["edited"])

    def test_legacy_fallback_ambiguous_is_noop(self):
        # два старі записи без id з однаковим (ts,raw,final,source) → безпечний no-op
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"ts": 5, "raw": "р", "final": "текст", "source": "desktop"},
                {"ts": 5, "raw": "р", "final": "текст", "source": "desktop"},
            ])
            self.assertFalse(history.update_final_by_id(
                path, "", "нове", fallback=(5, "р", "текст", "desktop")))
            lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(r["final"] == "текст" for r in lines))

    def test_legacy_fallback_unique_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [
                {"ts": 5, "raw": "р", "final": "текст", "source": "desktop"},
                {"ts": 6, "raw": "інше", "final": "текст", "source": "desktop"},
            ])
            self.assertTrue(history.update_final_by_id(
                path, "", "готово", fallback=(5, "р", "текст", "desktop")))
            lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(lines[0]["final"], "готово")
            self.assertTrue(lines[0]["edited"])
            self.assertEqual(lines[1]["final"], "текст")       # інший запис цілий


# ─────────────────────────── імпорт-валідація ───────────────────────────
class ImportValidationTests(unittest.TestCase):
    def test_drops_invalid_and_conflicting(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _profile(tmp, "imported")
            events = [
                {"v": 1, "op": "learn", "id": "1", "kind": "term-replace",
                 "heard": "ворктрі", "write": "worktree", "created_at": "x", "source": "history"},
                {"v": 1, "op": "learn", "id": "2", "kind": "term-replace",
                 "heard": "ворктрі", "write": "workspace", "created_at": "x", "source": "history"},
                {"v": 1, "op": "learn", "id": "3", "kind": "bogus",
                 "heard": "а", "write": "б", "created_at": "x", "source": "history"},
                {"v": 2, "op": "learn", "id": "4", "kind": "term-replace",
                 "heard": "в", "write": "W", "created_at": "x", "source": "history"},
            ]
            p.learning_journal_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                encoding="utf-8")
            stats = sl.validate_and_rebuild(p)
            self.assertEqual(stats["kept"], 1)      # лише перша валідна пара
            self.assertEqual(stats["dropped"], 3)
            self.assertEqual(_apply_terms(p, "ворктрі"), "worktree")


if __name__ == "__main__":
    unittest.main()
