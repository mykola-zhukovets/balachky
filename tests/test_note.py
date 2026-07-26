"""feature/scratchpad-note — тести плаваючої нотатки.

Три шари (як у test_mouse_ptt):
- чиста логіка append_note (жодного Qt);
- конфіг: дефолт і round-trip note_hotkey;
- проводка контролера (unbound-методи DesktopApp з фейковим self): гейти
  взаємного виключення з PTT, старт/стоп запису в нотатку, буфер, хоткей.

UI-рендер (саме вікно) — у tests/render_note_smoke.py (поза unittest discover).
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core.config import Config
from fronts.desktop.note import append_note


class AppendNoteTests(unittest.TestCase):
    def test_first_line_is_verbatim(self):
        self.assertEqual(append_note("", "привіт"), "привіт")

    def test_second_line_on_new_row(self):
        self.assertEqual(append_note("привіт", "як справи"),
                         "привіт\nяк справи")

    def test_strips_whitespace(self):
        self.assertEqual(append_note("", "  текст  "), "текст")

    def test_empty_text_keeps_buffer(self):
        self.assertEqual(append_note("буфер", "   "), "буфер")

    def test_no_double_newline_when_buffer_ends_with_newline(self):
        self.assertEqual(append_note("рядок\n", "далі"), "рядок\nдалі")

    def test_none_text_safe(self):
        self.assertEqual(append_note("буфер", None), "буфер")


class ConfigTests(unittest.TestCase):
    def test_default_empty(self):
        self.assertEqual(Config().note_hotkey, "")

    def test_roundtrip(self):
        c = Config()
        c.note_hotkey = "ctrl+shift+n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            c.save(p)
            loaded = Config.load(p)
        self.assertEqual(loaded.note_hotkey, "ctrl+shift+n")

    def test_empty_not_written(self):
        """Порожній note_hotkey не пишеться у файл (opt-in, чистий конфіг)."""
        c = Config()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            c.save(p)
            with open(p, encoding="utf-8") as f:
                text = f.read()
        self.assertNotIn("note_hotkey", text)


class _FakeRecorder:
    def __init__(self, has_stream=True):
        self.has_stream = has_stream
        self.recording = False
        self.started = 0
        self.stopped = 0

    def start(self):
        self.recording = True
        self.started += 1

    def stop(self):
        self.recording = False
        self.stopped += 1
        return [b"audio"]


def _controller(**over):
    """Мінімальний DesktopApp-подібний self для unbound-методів нотатки."""
    notified = []
    states = []
    ctl = SimpleNamespace(
        _busy=False, _capturing=False, _mic_testing=False,
        _meeting_active=False, _note_dictating=False, _note_state="idle",
        _note_buffer="", _note_window=None, _rec_started=0.0,
        recorder=_FakeRecorder(),
        cfg=SimpleNamespace(sounds=False, note_hotkey="", language="uk"),
        terms={},
        tray=SimpleNamespace(notify=lambda m: notified.append(m),
                             set_state=lambda s: None),
        rec_state=SimpleNamespace(emit=lambda s: None),
        note_state=SimpleNamespace(emit=lambda s: states.append(s)),
        _notified=notified, _states=states)
    ctl._set_note_state = lambda s: (
        setattr(ctl, "_note_state", s), ctl.note_state.emit(s))
    from fronts.desktop.app import DesktopApp
    ctl.note_busy = lambda: DesktopApp.note_busy(ctl)   # реальна логіка гейта
    for k, v in over.items():
        setattr(ctl, k, v)
    return ctl


class NoteBufferTests(unittest.TestCase):
    def test_set_and_clear(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller()
        DesktopApp.note_set_buffer(ctl, "текст")
        self.assertEqual(DesktopApp.note_text(ctl), "текст")
        DesktopApp.note_clear(ctl)
        self.assertEqual(DesktopApp.note_text(ctl), "")

    def test_appended_updates_buffer(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(_note_buffer="перше")
        DesktopApp._on_note_appended(ctl, "друге")
        self.assertEqual(ctl._note_buffer, "перше\nдруге")

    def test_appended_refreshes_open_window(self):
        from fronts.desktop.app import DesktopApp
        shown = []
        ctl = _controller(_note_buffer="a",
                          _note_window=SimpleNamespace(
                              set_text=lambda t: shown.append(t)))
        DesktopApp._on_note_appended(ctl, "b")
        self.assertEqual(shown, ["a\nb"])


class NoteGateTests(unittest.TestCase):
    """Взаємне виключення з PTT/тестом/нарадою й старт запису в нотатку."""

    def test_note_record_start_sets_flag_and_records(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller()
        ok = DesktopApp.note_record_start(ctl)
        self.assertTrue(ok)
        self.assertTrue(ctl._note_dictating)
        self.assertTrue(ctl.recorder.recording)
        self.assertIn("recording", ctl._states)

    def test_note_record_start_blocked_when_busy(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(_busy=True)
        self.assertFalse(DesktopApp.note_record_start(ctl))
        self.assertFalse(ctl._note_dictating)
        self.assertFalse(ctl.recorder.recording)

    def test_note_record_start_no_mic_warns(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(recorder=_FakeRecorder(has_stream=False))
        self.assertFalse(DesktopApp.note_record_start(ctl))
        self.assertEqual(len(ctl._notified), 1)

    def test_ptt_press_ignored_while_note_dictating(self):
        from fronts.desktop.app import DesktopApp
        started = []
        ctl = _controller(_note_dictating=True, _key_down=False,
                          _cancel_guard=False, _mic_testing=False)
        ctl.cfg.ptt_mode = "hold"
        ctl._start_recording = lambda: started.append(1)
        DesktopApp.on_press(ctl)
        self.assertEqual(started, [])          # PTT не стартував запис

    def test_meeting_start_blocked_by_note(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(_note_dictating=True)
        ctl.tray.notify = lambda m: ctl._notified.append(m)
        # meeting_start повертає False на гейті зайнятості ще до будь-яких імпортів
        self.assertFalse(DesktopApp.meeting_start(ctl, "onlymic"))

    def test_note_busy_reflects_note_dictating(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(_note_dictating=False)
        # note_busy() дивиться на recorder.recording; окремо перевіримо mic_test_busy
        self.assertFalse(DesktopApp.note_busy(ctl))
        ctl.recorder.recording = True
        self.assertTrue(DesktopApp.note_busy(ctl))

    def test_mic_test_busy_includes_note(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(_note_dictating=True)
        self.assertTrue(DesktopApp.mic_test_busy(ctl))


class NoteWindowClosedTests(unittest.TestCase):
    def test_closing_during_record_stops_and_unblocks(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(_note_dictating=True)
        ctl.recorder.recording = True
        ctl.tray.set_state = lambda s: None
        DesktopApp.note_on_window_closed(ctl)
        self.assertIsNone(ctl._note_window)
        self.assertFalse(ctl._note_dictating)   # гейт знято → PTT знову вільний
        self.assertFalse(ctl.recorder.recording)
        self.assertIn("idle", ctl._states)

    def test_closing_while_idle_just_drops_window(self):
        from fronts.desktop.app import DesktopApp
        ctl = _controller(_note_window=object())
        DesktopApp.note_on_window_closed(ctl)
        self.assertIsNone(ctl._note_window)
        self.assertFalse(ctl.recorder.stopped)  # нічого не зупиняли


class _FakeNoteHotkey:
    instances = []

    def __init__(self, key):
        self.key = key
        self.started = False
        self.stopped = False
        self.triggered = SimpleNamespace(connect=lambda cb: None)
        _FakeNoteHotkey.instances.append(self)

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True


class ApplyNoteHotkeyTests(unittest.TestCase):
    def setUp(self):
        _FakeNoteHotkey.instances = []

    def _ctl(self, combo):
        # hotkey_backend="legacy": тести патчать саме note.NoteHotkey (legacy-клас);
        # native-гілка (NativeNoteHotkey) реєструвала б справжній RegisterHotKey
        return _controller(
            _note_hotkey=None,
            cfg=SimpleNamespace(note_hotkey=combo, sounds=False,
                                hotkey_backend="legacy"),
            show_note=lambda: None)

    def test_empty_registers_nothing(self):
        from fronts.desktop.app import DesktopApp
        ctl = self._ctl("")
        with patch("fronts.desktop.note.NoteHotkey", _FakeNoteHotkey):
            DesktopApp._apply_note_hotkey(ctl)
        self.assertIsNone(ctl._note_hotkey)
        self.assertEqual(_FakeNoteHotkey.instances, [])

    def test_combo_registers_hook(self):
        from fronts.desktop.app import DesktopApp
        ctl = self._ctl("ctrl+shift+n")
        with patch("fronts.desktop.note.NoteHotkey", _FakeNoteHotkey):
            DesktopApp._apply_note_hotkey(ctl)
        self.assertIsNotNone(ctl._note_hotkey)
        self.assertTrue(ctl._note_hotkey.started)
        self.assertEqual(ctl._note_hotkey.key, "ctrl+shift+n")

    def test_reapply_stops_previous(self):
        from fronts.desktop.app import DesktopApp
        ctl = self._ctl("ctrl+shift+n")
        with patch("fronts.desktop.note.NoteHotkey", _FakeNoteHotkey):
            DesktopApp._apply_note_hotkey(ctl)
            first = ctl._note_hotkey
            ctl.cfg.note_hotkey = "ctrl+alt+m"
            DesktopApp._apply_note_hotkey(ctl)
        self.assertTrue(first.stopped)          # старий хук знято
        self.assertEqual(ctl._note_hotkey.key, "ctrl+alt+m")


if __name__ == "__main__":
    unittest.main()
