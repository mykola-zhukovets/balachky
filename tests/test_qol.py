"""Тести чистої логіки пакета зручностей (whisper_core.qol) — feature/qol-pack."""
import unittest

from whisper_core.qol import (
    UndoBuffer, parse_hhmm, in_quiet_hours, sounds_muted_now,
    AutostopMonitor, duration_status,
)


class UndoBufferTest(unittest.TestCase):
    def test_empty_by_default(self):
        b = UndoBuffer()
        self.assertFalse(b.has_undo())
        self.assertFalse(b.has_text())
        self.assertIsNone(b.last_text)
        self.assertEqual(b.consume_undo(), 0)

    def test_record_counts_characters(self):
        b = UndoBuffer()
        b.record("привіт")            # 6 символів (кирилиця рахується правильно)
        self.assertTrue(b.has_undo())
        self.assertEqual(b.last_text, "привіт")
        self.assertEqual(b.consume_undo(), 6)

    def test_undo_is_one_shot(self):
        b = UndoBuffer()
        b.record("abc")
        self.assertEqual(b.consume_undo(), 3)
        # повторне скасування нічого не видаляє
        self.assertFalse(b.has_undo())
        self.assertEqual(b.consume_undo(), 0)

    def test_last_text_survives_undo(self):
        """Після скасування повторна вставка ще можлива (текст лишається)."""
        b = UndoBuffer()
        b.record("hello world")
        b.consume_undo()
        self.assertTrue(b.has_text())
        self.assertEqual(b.last_text, "hello world")

    def test_empty_record_clears(self):
        b = UndoBuffer()
        b.record("x")
        b.record("")
        self.assertFalse(b.has_undo())
        self.assertFalse(b.has_text())
        self.assertIsNone(b.last_text)

    def test_none_record_safe(self):
        b = UndoBuffer()
        b.record(None)
        self.assertFalse(b.has_undo())
        self.assertIsNone(b.last_text)

    def test_newline_counts_as_char(self):
        b = UndoBuffer()
        b.record("a\nb")
        self.assertEqual(b.consume_undo(), 3)


class ParseHhmmTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_hhmm("00:00"), 0)
        self.assertEqual(parse_hhmm("07:30"), 7 * 60 + 30)
        self.assertEqual(parse_hhmm("22:00"), 22 * 60)
        self.assertEqual(parse_hhmm("23:59"), 23 * 60 + 59)

    def test_whitespace(self):
        self.assertEqual(parse_hhmm("  8:05 "), 8 * 60 + 5)

    def test_invalid(self):
        for bad in ("", "abc", "25:00", "12:60", "12", "12:00:00", ":", None, 700):
            self.assertIsNone(parse_hhmm(bad), bad)


class QuietHoursTest(unittest.TestCase):
    def test_daytime_range(self):
        # 09:00..17:00
        s, e = 9 * 60, 17 * 60
        self.assertTrue(in_quiet_hours(10 * 60, s, e))
        self.assertFalse(in_quiet_hours(8 * 60, s, e))
        self.assertFalse(in_quiet_hours(18 * 60, s, e))

    def test_start_inclusive_end_exclusive(self):
        s, e = 9 * 60, 17 * 60
        self.assertTrue(in_quiet_hours(s, s, e))       # старт включно
        self.assertFalse(in_quiet_hours(e, s, e))      # кінець виключно

    def test_wrap_past_midnight(self):
        # 22:00..07:00 — типова нічна тиша
        s, e = 22 * 60, 7 * 60
        self.assertTrue(in_quiet_hours(23 * 60, s, e))     # 23:00 — тиша
        self.assertTrue(in_quiet_hours(0, s, e))           # північ — тиша
        self.assertTrue(in_quiet_hours(6 * 60 + 59, s, e)) # 06:59 — тиша
        self.assertFalse(in_quiet_hours(7 * 60, s, e))     # 07:00 — вже ні
        self.assertFalse(in_quiet_hours(12 * 60, s, e))    # полудень — ні
        self.assertTrue(in_quiet_hours(22 * 60, s, e))     # 22:00 — старт включно

    def test_empty_range_never_quiet(self):
        self.assertFalse(in_quiet_hours(12 * 60, 8 * 60, 8 * 60))

    def test_sounds_muted_now(self):
        class Cfg:
            quiet_hours_enabled = True
            quiet_hours_start = "22:00"
            quiet_hours_end = "07:00"
        cfg = Cfg()
        self.assertTrue(sounds_muted_now(cfg, 23 * 60))
        self.assertFalse(sounds_muted_now(cfg, 12 * 60))
        cfg.quiet_hours_enabled = False
        self.assertFalse(sounds_muted_now(cfg, 23 * 60))

    def test_sounds_muted_now_bad_config(self):
        class Cfg:
            quiet_hours_enabled = True
            quiet_hours_start = "nonsense"
            quiet_hours_end = "07:00"
        self.assertFalse(sounds_muted_now(Cfg(), 23 * 60))

    def test_sounds_muted_now_missing_attrs(self):
        self.assertFalse(sounds_muted_now(object(), 23 * 60))


class AutostopMonitorTest(unittest.TestCase):
    def test_disabled_never_stops(self):
        m = AutostopMonitor(0)
        for t in range(100):
            self.assertFalse(m.update(0.0, float(t)))

    def test_stops_after_continuous_silence(self):
        m = AutostopMonitor(3.0)
        self.assertFalse(m.update(0.0, 0.0))   # тиша почалась
        self.assertFalse(m.update(0.0, 2.0))   # ще рано
        self.assertTrue(m.update(0.0, 3.0))    # 3 с тиші — стоп

    def test_speech_resets_silence(self):
        m = AutostopMonitor(3.0)
        m.update(0.0, 0.0)
        m.update(0.5, 1.0)                      # голос — скидання
        self.assertFalse(m.update(0.0, 2.0))   # тиша почалась заново
        self.assertFalse(m.update(0.0, 4.0))   # 2 с від нового старту
        self.assertTrue(m.update(0.0, 5.0))    # 3 с — стоп

    def test_reset(self):
        m = AutostopMonitor(2.0)
        m.update(0.0, 0.0)
        m.reset()
        self.assertFalse(m.update(0.0, 1.5))   # після reset тиша починається заново тут
        self.assertFalse(m.update(0.0, 3.0))   # лише 1.5 с від нового старту
        self.assertTrue(m.update(0.0, 3.5))    # 2.0 с тиші — стоп

    def test_threshold_boundary(self):
        m = AutostopMonitor(1.0, threshold=0.01)
        self.assertFalse(m.update(0.01, 0.0))  # рівно на порозі = не тиша
        self.assertFalse(m.update(0.009, 0.5)) # нижче порога — тиша почалась
        self.assertTrue(m.update(0.009, 1.5))


class DurationStatusTest(unittest.TestCase):
    def test_disabled(self):
        self.assertEqual(duration_status(9999, 0), "ok")

    def test_ok_warn_stop(self):
        limit = 1200            # 20 хв
        self.assertEqual(duration_status(0, limit), "ok")
        self.assertEqual(duration_status(1000, limit), "ok")
        self.assertEqual(duration_status(1170, limit, warn_before_s=30), "warn")
        self.assertEqual(duration_status(1200, limit), "stop")
        self.assertEqual(duration_status(1300, limit), "stop")

    def test_warn_window_boundary(self):
        limit, warn = 100, 10
        self.assertEqual(duration_status(89, limit, warn), "ok")
        self.assertEqual(duration_status(90, limit, warn), "warn")
        self.assertEqual(duration_status(99, limit, warn), "warn")
        self.assertEqual(duration_status(100, limit, warn), "stop")


class ConfigRoundTripTest(unittest.TestCase):
    """Нові ключі feature/qol-pack переживають save → load."""

    def test_qol_keys_roundtrip(self):
        import tempfile
        from pathlib import Path
        from whisper_core.config import Config
        c = Config()
        c.undo_paste_key = "ctrl+alt+z"
        c.insert_last_key = "ctrl+alt+v"
        c.dictation_autostop_silence_s = 7
        c.dictation_max_duration_s = 600
        c.paste_confirm_sound = True
        c.quiet_hours_enabled = True
        c.quiet_hours_start = "23:30"
        c.quiet_hours_end = "06:15"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            c.save(p)
            c2 = Config.load(p)
        self.assertEqual(c2.undo_paste_key, "ctrl+alt+z")
        self.assertEqual(c2.insert_last_key, "ctrl+alt+v")
        self.assertEqual(c2.dictation_autostop_silence_s, 7)
        self.assertEqual(c2.dictation_max_duration_s, 600)
        self.assertTrue(c2.paste_confirm_sound)
        self.assertTrue(c2.quiet_hours_enabled)
        self.assertEqual(c2.quiet_hours_start, "23:30")
        self.assertEqual(c2.quiet_hours_end, "06:15")

    def test_defaults_are_safe(self):
        from whisper_core.config import Config
        c = Config()
        self.assertEqual(c.undo_paste_key, "")           # хоткеї вимкнені
        self.assertEqual(c.insert_last_key, "")
        self.assertEqual(c.dictation_autostop_silence_s, 0)   # автостоп вимкнено
        self.assertEqual(c.dictation_max_duration_s, 1200)    # 20 хв — розумний дефолт
        self.assertTrue(c.paste_confirm_sound)   # звук вставки увімкнено за замовчуванням
        self.assertFalse(c.quiet_hours_enabled)


class SendBackspacesTest(unittest.TestCase):
    """send_backspaces: к-сть подій (down+up на символ) і поведінка на 0."""

    def test_event_count(self):
        from unittest import mock
        from fronts.desktop import wininput
        sent = []
        with mock.patch.object(wininput, "_call_sendinput",
                               side_effect=lambda seq: sent.append(len(seq)) or len(seq)):
            self.assertTrue(wininput.send_backspaces(5))
        self.assertEqual(sent, [10])          # 5 символів × (down + up)

    def test_zero_is_noop(self):
        from unittest import mock
        from fronts.desktop import wininput
        with mock.patch.object(wininput, "_call_sendinput") as m:
            self.assertTrue(wininput.send_backspaces(0))
            m.assert_not_called()

    def test_partial_send_reports_false(self):
        from unittest import mock
        from fronts.desktop import wininput
        with mock.patch.object(wininput, "_call_sendinput", return_value=1):
            self.assertFalse(wininput.send_backspaces(3))


class CombosEqualTest(unittest.TestCase):
    """Канонічне порівняння комбінацій (валідація конфліктів хоткеїв)."""

    def test_order_and_case_insensitive(self):
        from fronts.desktop.hotkey import combos_equal
        self.assertTrue(combos_equal("ctrl+shift+space", "shift+ctrl+space"))
        self.assertTrue(combos_equal("Ctrl+Alt+Z", "alt+ctrl+z"))

    def test_different_combos(self):
        from fronts.desktop.hotkey import combos_equal
        self.assertFalse(combos_equal("ctrl+alt+z", "ctrl+alt+v"))
        self.assertFalse(combos_equal("ctrl+z", "ctrl+shift+z"))

    def test_empty_never_conflicts(self):
        from fronts.desktop.hotkey import combos_equal
        self.assertFalse(combos_equal("", ""))
        self.assertFalse(combos_equal("", "ctrl+z"))


class ActionHotkeyConflictTest(unittest.TestCase):
    """set_action_hotkey відхиляє конфлікти з PTT і з іншим хоткеєм дії."""

    def _controller(self):
        import types
        from fronts.desktop.app import DesktopApp

        class _Tray:
            def __init__(self):
                self.messages = []

            def notify(self, text):
                self.messages.append(text)

        class _Hotkeys:
            def __init__(self):
                self.applied = []

            def apply(self, undo, insert):
                self.applied.append((undo, insert))

        cfg = types.SimpleNamespace(
            ptt_key="ctrl+shift+space", undo_paste_key="", insert_last_key="",
            save=lambda *a, **k: None)
        ns = types.SimpleNamespace(cfg=cfg, tray=_Tray(), action_hotkeys=_Hotkeys())
        ns.set_action_hotkey = DesktopApp.set_action_hotkey.__get__(ns)
        return ns

    def test_conflict_with_ptt_rejected(self):
        c = self._controller()
        # той самий PTT-комбо в іншому порядку — все одно конфлікт
        self.assertFalse(c.set_action_hotkey("undo", "shift+ctrl+space"))
        self.assertEqual(c.cfg.undo_paste_key, "")       # нічого не записано
        self.assertEqual(len(c.tray.messages), 1)        # тост показано
        self.assertEqual(c.action_hotkeys.applied, [])   # хук не перевішувався

    def test_conflict_with_other_action_rejected(self):
        c = self._controller()
        self.assertTrue(c.set_action_hotkey("undo", "ctrl+alt+z"))
        self.assertFalse(c.set_action_hotkey("insert", "alt+ctrl+z"))
        self.assertEqual(c.cfg.insert_last_key, "")
        self.assertEqual(len(c.tray.messages), 1)

    def test_reassign_same_key_to_same_action_ok(self):
        c = self._controller()
        self.assertTrue(c.set_action_hotkey("undo", "ctrl+alt+z"))
        # своє поточне значення — не конфлікт (повторне збереження)
        self.assertTrue(c.set_action_hotkey("undo", "ctrl+alt+z"))
        self.assertEqual(c.tray.messages, [])

    def test_clear_always_allowed(self):
        c = self._controller()
        self.assertTrue(c.set_action_hotkey("undo", "ctrl+alt+z"))
        self.assertTrue(c.set_action_hotkey("undo", ""))
        self.assertEqual(c.cfg.undo_paste_key, "")


if __name__ == "__main__":
    unittest.main()
