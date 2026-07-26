"""feature/context-profiles — контекстні профілі за активним застосунком.

Ядро без Qt: резолвер вікна (моки Win32 — звичайне вікно й UWP-кейс), матчер
(порядок=пріоритет, регістр, дефолт, title_regex), гейт безпеки (блок-лист),
TOML-сховище (round-trip, порядок) і поведінка enabled=false у PTT-конвеєрі
(_work, мок-стиль як test_voice_punctuation). Плюс живий мікро-тест резолвера.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fronts.desktop.context import (
    ContextResolver, ProfileMatcher, SecurityGate, ContextProfile, Behavior,
    WindowContext, load_profiles, save_profiles, APPLICATION_FRAME_HOST,
)


# ─────────────────────────── резолвер ───────────────────────────
class ResolverTests(unittest.TestCase):
    def _resolver(self, **overrides):
        r = ContextResolver()
        patchers = [patch.object(r, name, **kw) for name, kw in overrides.items()]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        return r

    def test_normal_window(self):
        r = self._resolver(
            _foreground_hwnd=dict(return_value=111),
            _pid_for_hwnd=dict(return_value=222),
            _exe_for_pid=dict(return_value="chrome.exe"),
            _title_for_hwnd=dict(return_value="GitHub — Chrome"),
            _uwp_real_pid=dict(return_value=0),
        )
        ctx = r.get_window_context()
        self.assertEqual(ctx.exe, "chrome.exe")
        self.assertEqual(ctx.title, "GitHub — Chrome")
        self.assertEqual(ctx.hwnd, 111)

    def test_uwp_resolves_real_process(self):
        # ApplicationFrameHost.exe — лише хост; реальний процес за CoreWindow-PID
        def exe_for(pid):
            return "ApplicationFrameHost.exe" if pid == 222 else "CalculatorApp.exe"

        r = self._resolver(
            _foreground_hwnd=dict(return_value=111),
            _pid_for_hwnd=dict(return_value=222),
            _exe_for_pid=dict(side_effect=exe_for),
            _title_for_hwnd=dict(return_value="Калькулятор"),
            _uwp_real_pid=dict(return_value=999),
        )
        ctx = r.get_window_context()
        self.assertEqual(ctx.exe.lower(), "calculatorapp.exe")
        self.assertNotEqual(ctx.exe.lower(), APPLICATION_FRAME_HOST)

    def test_no_foreground_window_is_empty(self):
        r = self._resolver(_foreground_hwnd=dict(return_value=0))
        ctx = r.get_window_context()
        self.assertEqual(ctx.exe, "")
        self.assertEqual(ctx.hwnd, 0)

    def test_failure_never_raises(self):
        r = self._resolver(_foreground_hwnd=dict(side_effect=OSError("boom")))
        self.assertEqual(r.get_window_context().exe, "")   # тихо → порожній контекст


class LiveResolverTest(unittest.TestCase):
    """Мікро-тест на реальному Win32: ланцюг має відпрацювати без винятку й
    повернути WindowContext. У headless-середовищі exe може бути порожнім —
    тоді перевіряємо лише тип (ланцюг цілий)."""

    def test_returns_window_context(self):
        ctx = ContextResolver().get_window_context()
        self.assertIsInstance(ctx, WindowContext)
        self.assertIsInstance(ctx.exe, str)
        self.assertIsInstance(ctx.title, str)


# ─────────────────────────── матчер ───────────────────────────
def _prof(name, apps, title_regex=None, **beh):
    return ContextProfile(name, apps, title_regex, Behavior(**beh))


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.default = _prof("default", [])

    def test_matches_by_exe_case_insensitive(self):
        p = _prof("Msg", ["Telegram.exe"])
        m = ProfileMatcher([p], self.default)
        self.assertIs(m.match(WindowContext(exe="telegram.exe")), p)
        self.assertIs(m.match(WindowContext(exe="TELEGRAM.EXE")), p)

    def test_default_when_no_match(self):
        m = ProfileMatcher([_prof("Msg", ["Telegram.exe"])], self.default)
        self.assertIs(m.match(WindowContext(exe="notepad.exe")), self.default)
        self.assertIs(m.match(WindowContext(exe="")), self.default)

    def test_order_is_priority_first_wins(self):
        first = _prof("A", ["chrome.exe"], auto_enter=True)
        second = _prof("B", ["chrome.exe"], auto_enter=False)
        m = ProfileMatcher([first, second], self.default)
        self.assertIs(m.match(WindowContext(exe="chrome.exe")), first)

    def test_title_regex_narrows_match(self):
        p = _prof("Docs", ["chrome.exe"], r"Google Docs")
        m = ProfileMatcher([p], self.default)
        self.assertIs(
            m.match(WindowContext(exe="chrome.exe", title="Звіт — Google Docs")), p)
        # той самий exe, але заголовок не збігся → дефолт
        self.assertIs(
            m.match(WindowContext(exe="chrome.exe", title="YouTube")), self.default)

    def test_broken_regex_falls_back_to_exe_match(self):
        p = _prof("X", ["chrome.exe"], "(")   # невалідний regex
        m = ProfileMatcher([p], self.default)
        self.assertIs(m.match(WindowContext(exe="chrome.exe", title="будь-що")), p)


# ─────────────────────────── гейт безпеки ───────────────────────────
class SecurityGateTests(unittest.TestCase):
    def test_blocks_password_managers(self):
        g = SecurityGate()
        for exe in ("KeePass.exe", "keepassxc.exe", "Bitwarden.exe",
                    "1Password.exe", "CredentialUIBroker.exe"):
            self.assertTrue(g.is_blocked(exe), exe)

    def test_allows_ordinary_apps(self):
        g = SecurityGate()
        self.assertFalse(g.is_blocked("chrome.exe"))
        self.assertFalse(g.is_blocked(""))

    def test_extra_blocklist(self):
        g = SecurityGate(extra=["MyVault.exe"])
        self.assertTrue(g.is_blocked("myvault.exe"))
        self.assertTrue(g.is_blocked("KeePass.exe"))   # база лишається


# ─────────────────────────── сховище TOML ───────────────────────────
class StorageTests(unittest.TestCase):
    def test_round_trip_preserves_order_and_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context_profiles.toml"
            profs = [
                _prof("Msg", ["Telegram.exe", "WhatsApp.exe"], r"Chat",
                      auto_enter=True, dictionary="робота", enabled=True),
                _prof("Bank", ["bank.exe"], None, enabled=False),
            ]
            default = _prof("default", [], None,
                            auto_enter=False, dictionary=None, enabled=True)
            save_profiles(path, profs, default)
            loaded, dflt = load_profiles(path)

            self.assertEqual([p.name for p in loaded], ["Msg", "Bank"])
            self.assertEqual(loaded[0].apps, ["Telegram.exe", "WhatsApp.exe"])
            self.assertEqual(loaded[0].title_regex, "Chat")
            self.assertTrue(loaded[0].behavior.auto_enter)
            self.assertEqual(loaded[0].behavior.dictionary, "робота")
            self.assertFalse(loaded[1].behavior.enabled)
            self.assertIsNone(loaded[1].behavior.dictionary)  # "" → None
            self.assertTrue(dflt.behavior.enabled)

    def test_round_trip_preserves_formatting(self):
        # feature/output-formats: профіль форматування виводу переживає round-trip,
        # відомий дефолт — plain, невідоме значення нормалізується до plain.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context_profiles.toml"
            profs = [
                _prof("Md", ["obsidian.exe"], None),
                _prof("Legacy", ["old.exe"], None),
            ]
            profs[0].behavior.formatting = "markdown"
            profs[1].behavior.formatting = "letter"
            default = _prof("default", [], None)
            save_profiles(path, profs, default)
            loaded, dflt = load_profiles(path)
            self.assertEqual(loaded[0].behavior.formatting, "markdown")
            self.assertEqual(loaded[1].behavior.formatting, "letter")
            self.assertEqual(dflt.behavior.formatting, "plain")

    def test_unknown_formatting_normalized_to_plain(self):
        from fronts.desktop.context import _behavior_from
        self.assertEqual(_behavior_from({"formatting": "bogus"}).formatting, "plain")
        self.assertEqual(_behavior_from({}).formatting, "plain")

    def test_missing_file_gives_empty_and_default(self):
        loaded, dflt = load_profiles(Path(tempfile.gettempdir()) / "nope_xyz.toml")
        self.assertEqual(loaded, [])
        self.assertTrue(dflt.behavior.enabled)

    def test_missing_file_no_warning(self):
        # Відсутній файл профілів — очікувано (перший запуск): без WARNING-шуму.
        missing = Path(tempfile.gettempdir()) / "nope_no_warn_xyz.toml"
        with self.assertLogs("fronts.desktop.context", level="DEBUG") as cm:
            load_profiles(missing)
        self.assertFalse(
            [r for r in cm.records if r.levelno >= 30],  # WARNING=30 і вище
            "Відсутній файл профілів не має логувати WARNING",
        )

    def test_broken_toml_still_warns(self):
        # Реальна помилка читання (битий TOML) лишається WARNING — це не рутина.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context_profiles.toml"
            path.write_text("this is = = not toml", encoding="utf-8")
            with self.assertLogs("fronts.desktop.context", level="WARNING") as cm:
                loaded, dflt = load_profiles(path)
            self.assertEqual(loaded, [])
            self.assertTrue(any(r.levelno >= 30 for r in cm.records))


# ─────────────────────────── поведінка у _work ───────────────────────────
class WorkBehaviorTests(unittest.TestCase):
    """Гейт вставки в _work за профілем вікна (мок-стиль test_voice_punctuation)."""

    @staticmethod
    def _controller(matcher, gate, exe, output_mode="paste", final="привіт світ",
                    restore=False):
        recorded = {}

        class Card:
            def emit(self, *a):
                recorded["card"] = a

        class Counter:
            def __init__(self):
                self.count = 0
                self.msgs = []

            def emit(self, *a):
                self.count += 1
                self.msgs.append(a[0] if a else None)

        resolver = SimpleNamespace(
            get_window_context=lambda: WindowContext(exe=exe))
        return SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: ("raw", final, 1.0, [], []),
            output_mode=output_mode,
            transcription_error=Counter(),
            transcribed=Card(),
            finished=Counter(),
            cfg=SimpleNamespace(sounds=False, voice_punctuation=False,
                                language="uk", restore_clipboard=restore),
            _busy=True,
            _ctx_resolver=resolver,
            _ctx_gate=gate,
            _ctx_matcher=matcher,
        ), recorded

    def _run(self, controller):
        from fronts.desktop import app as desktop_app
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None), \
             patch.object(desktop_app, "paste_text") as paste, \
             patch.object(desktop_app, "press_enter") as enter:
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        return paste, enter

    def test_disabled_profile_holds_paste(self):
        matcher = ProfileMatcher([_prof("Bank", ["bank.exe"], enabled=False)],
                                 _prof("default", []))
        controller, recorded = self._controller(matcher, SecurityGate(), "bank.exe")
        paste, enter = self._run(controller)
        paste.assert_not_called()                     # вставки НЕ було
        enter.assert_not_called()
        self.assertIn("card", recorded)               # картка все одно є
        self.assertEqual(controller.transcription_error.count, 1)  # трей-нота

    def test_security_blocklist_holds_paste(self):
        matcher = ProfileMatcher([], _prof("default", []))
        controller, recorded = self._controller(matcher, SecurityGate(),
                                                "KeePass.exe")
        paste, enter = self._run(controller)
        paste.assert_not_called()
        self.assertEqual(controller.transcription_error.count, 1)

    def test_enabled_profile_pastes_and_auto_enter(self):
        matcher = ProfileMatcher(
            [_prof("Msg", ["telegram.exe"], auto_enter=True)], _prof("default", []))
        controller, recorded = self._controller(matcher, SecurityGate(),
                                                "telegram.exe")
        paste, enter = self._run(controller)
        paste.assert_called_once()                    # вставка відбулась
        enter.assert_called_once()                    # auto_enter → Enter
        self.assertEqual(controller.transcription_error.count, 0)

    def test_default_profile_pastes_without_auto_enter(self):
        matcher = ProfileMatcher([], _prof("default", []))   # дефолт: enabled, без Enter
        controller, recorded = self._controller(matcher, SecurityGate(), "notepad.exe")
        paste, enter = self._run(controller)
        paste.assert_called_once()
        enter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
