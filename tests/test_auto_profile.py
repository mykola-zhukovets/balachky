"""feature/auto-profile — авто-вибір профілю-словника за активним вікном.

Матчер правил (wildcard процесу/заголовка, пріоритет точнішого, відсутність
збігу → None, enabled=False → мовчить), TOML-сховище у ТОМУ Ж файлі, що й
контекстні профілі (round-trip + збереження поряд із [[profile]]), і поведінка
контролера: тимчасове застосування словника лише на фразу + пріоритет явного
вибору користувача (self._profile_manual).
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from fronts.desktop.context import (
    AutoProfileRule, AutoProfileMatcher, WindowContext,
    ContextProfile, Behavior, load_profiles, save_profiles, load_auto_rules,
)


def _rule(process="", title="", profile="p"):
    return AutoProfileRule(process=process, title=title, profile=profile)


# ─────────────────────────── матчер правил ───────────────────────────
class AutoMatcherTests(unittest.TestCase):
    def test_matches_exact_process(self):
        m = AutoProfileMatcher([_rule("WINWORD.EXE", profile="діловий")])
        self.assertEqual(m.match(WindowContext(exe="winword.exe")), "діловий")

    def test_wildcard_process(self):
        m = AutoProfileMatcher([_rule("*.exe", profile="будь-який")])
        self.assertEqual(m.match(WindowContext(exe="notepad.exe")), "будь-який")
        self.assertIsNone(m.match(WindowContext(exe="")))

    def test_title_fragment_is_substring(self):
        m = AutoProfileMatcher([_rule("chrome.exe", "Gmail", "лист")])
        self.assertEqual(
            m.match(WindowContext(exe="chrome.exe", title="Вхідні — Gmail — Chrome")),
            "лист")
        # той самий процес, але заголовок не містить фрагмента → без збігу
        self.assertIsNone(
            m.match(WindowContext(exe="chrome.exe", title="YouTube")))

    def test_more_specific_rule_wins(self):
        # правило із фрагментом заголовка точніше за лише-процес, попри порядок
        broad = _rule("chrome.exe", profile="веб")
        narrow = _rule("chrome.exe", "Gmail", "лист")
        m = AutoProfileMatcher([broad, narrow])
        self.assertEqual(
            m.match(WindowContext(exe="chrome.exe", title="… Gmail …")), "лист")
        # де фрагмент не підходить — лишається ширше правило
        self.assertEqual(
            m.match(WindowContext(exe="chrome.exe", title="Docs")), "веб")

    def test_exact_process_beats_wildcard(self):
        wild = _rule("*.exe", profile="широке")
        exact = _rule("winword.exe", profile="діловий")
        m = AutoProfileMatcher([wild, exact])
        self.assertEqual(m.match(WindowContext(exe="winword.exe")), "діловий")

    def test_tie_keeps_first_in_order(self):
        a = _rule("chrome.exe", profile="A")
        b = _rule("chrome.exe", profile="B")
        m = AutoProfileMatcher([a, b])
        self.assertEqual(m.match(WindowContext(exe="chrome.exe")), "A")

    def test_no_rule_no_change(self):
        m = AutoProfileMatcher([_rule("winword.exe", profile="діловий")])
        self.assertIsNone(m.match(WindowContext(exe="notepad.exe")))

    def test_disabled_matcher_is_silent(self):
        m = AutoProfileMatcher([_rule("winword.exe", profile="діловий")],
                               enabled=False)
        self.assertIsNone(m.match(WindowContext(exe="winword.exe")))

    def test_empty_rule_matches_nothing(self):
        m = AutoProfileMatcher([_rule("", "", "порожнє")])
        self.assertIsNone(m.match(WindowContext(exe="anything.exe", title="x")))

    def test_rule_without_profile_ignored(self):
        m = AutoProfileMatcher([_rule("chrome.exe", profile="")])
        self.assertIsNone(m.match(WindowContext(exe="chrome.exe")))


# ─────────────────────────── сховище TOML ───────────────────────────
class AutoStorageTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context_profiles.toml"
            rules = [_rule("WINWORD.EXE", "", "діловий"),
                     _rule("chrome.exe", "Gmail", "лист")]
            save_profiles(path, [], None, auto_rules=rules, auto_enabled=True)
            loaded, enabled = load_auto_rules(path)
            self.assertTrue(enabled)
            self.assertEqual([(r.process, r.title, r.profile) for r in loaded],
                             [("WINWORD.EXE", "", "діловий"),
                              ("chrome.exe", "Gmail", "лист")])

    def test_missing_file_gives_empty_and_unset(self):
        rules, enabled = load_auto_rules(Path(tempfile.gettempdir()) / "nope_ap.toml")
        self.assertEqual(rules, [])
        self.assertIsNone(enabled)   # None → «увімк., якщо правила є»

    def test_rules_coexist_with_profiles_in_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context_profiles.toml"
            profs = [ContextProfile("Msg", ["Telegram.exe"], None,
                                    Behavior(dictionary="робота"))]
            rules = [_rule("winword.exe", "", "діловий")]
            save_profiles(path, profs, None, auto_rules=rules, auto_enabled=True)
            # обидві секції читаються своїми лоадерами
            gp, _ = load_profiles(path)
            gr, en = load_auto_rules(path)
            self.assertEqual(gp[0].name, "Msg")
            self.assertEqual(gp[0].behavior.dictionary, "робота")
            self.assertEqual(gr[0].profile, "діловий")
            self.assertTrue(en)

    def test_saving_profiles_preserves_existing_rules(self):
        # правка профілів (без передачі auto_rules) не має стирати правила
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context_profiles.toml"
            save_profiles(path, [], None,
                          auto_rules=[_rule("winword.exe", "", "діловий")],
                          auto_enabled=True)
            save_profiles(path, [ContextProfile("X", ["x.exe"])], None)  # лише профілі
            rules, enabled = load_auto_rules(path)
            self.assertEqual([r.profile for r in rules], ["діловий"])
            self.assertTrue(enabled)


# ─────────────────── поведінка контролера (тимчасовість) ───────────────────
class ControllerApplyTests(unittest.TestCase):
    """_apply_auto_profile ставить лише self._context_terms (glossary фрази).
    Активний профіль не чіпається, тож застосування тимчасове за побудовою."""

    @staticmethod
    def _controller(matcher, exe="winword.exe", manual=False, active="default"):
        return SimpleNamespace(
            _ctx_resolver=SimpleNamespace(
                get_window_context=lambda: WindowContext(exe=exe)),
            _auto_matcher=matcher,
            _profile_manual=manual,
            profile=SimpleNamespace(name=active),
            _context_terms=None,
        )

    def _apply(self, controller, ctx=None):
        from fronts.desktop import app as desktop_app
        ctx = ctx or WindowContext(exe="winword.exe")
        term_obj = object()
        # feature/bilingual-memory: словник профілю тепер вантажиться через
        # self._profile_terms (терміни + опційна пам'ять фраз), а не модульний
        # load_terms — мокаємо саме його на контролері.
        controller._profile_terms = Mock(return_value=term_obj)
        with patch.object(desktop_app, "profiles") as pf, \
             patch.object(desktop_app, "ROOT", "ROOT"):
            pf.get.return_value = SimpleNamespace(terms_path="tp")
            desktop_app.DesktopApp._apply_auto_profile(controller, ctx)
        return term_obj, controller._profile_terms

    def test_matched_rule_sets_context_terms(self):
        m = AutoProfileMatcher([_rule("winword.exe", "", "діловий")])
        c = self._controller(m)
        term_obj, lt = self._apply(c)
        self.assertIs(c._context_terms, term_obj)   # словник профілю на фразу
        lt.assert_called_once()

    def test_no_rule_leaves_terms_none(self):
        m = AutoProfileMatcher([_rule("winword.exe", "", "діловий")])
        c = self._controller(m, exe="notepad.exe")
        self._apply(c, WindowContext(exe="notepad.exe"))
        self.assertIsNone(c._context_terms)         # без змін

    def test_manual_choice_has_priority(self):
        m = AutoProfileMatcher([_rule("winword.exe", "", "діловий")])
        c = self._controller(m, manual=True)        # користувач сам обрав профіль
        self._apply(c)
        self.assertIsNone(c._context_terms)         # авто мовчить на сесію

    def test_rule_pointing_to_active_profile_is_noop(self):
        m = AutoProfileMatcher([_rule("winword.exe", "", "діловий")])
        c = self._controller(m, active="діловий")   # уже активний — нема що міняти
        self._apply(c)
        self.assertIsNone(c._context_terms)


if __name__ == "__main__":
    unittest.main()
