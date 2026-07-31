"""Звірка №6 (18.07): set_note_hotkey має контракт command_edit/bookmark —
конфлікт з іншою комбінацією відхиляється з тостом і bool=False, щоб UI не
показував комбінацію, яка фізично не працює."""
import unittest
from types import SimpleNamespace

from fronts.desktop.app import DesktopApp


class _FakeTray:
    def __init__(self):
        self.notices = []

    def notify(self, text):
        self.notices.append(text)


class NoteHotkeyConflictTests(unittest.TestCase):
    def _app(self, **cfg_fields):
        # Мінімальний фейк-self: методи класу викликаємо незв'язано (DesktopApp —
        # QObject, тож навіть __new__ без __init__ небезпечний; фейку досить
        # атрибутів, які реально читає set_note_hotkey)
        app = SimpleNamespace()
        defaults = dict(ptt_key="ctrl+shift+space", undo_paste_key="",
                        insert_last_key="", command_edit_hotkey="",
                        meeting_bookmark_hotkey="", note_hotkey="")
        defaults.update(cfg_fields)
        saved = {"n": 0}
        app.cfg = SimpleNamespace(**defaults, save=lambda: saved.__setitem__("n", saved["n"] + 1))
        app.tray = _FakeTray()
        app._applied = []
        app._apply_note_hotkey = lambda: app._applied.append(app.cfg.note_hotkey)
        return app

    def test_conflict_with_ptt_rejected_with_toast(self):
        app = self._app()
        ok = DesktopApp.set_note_hotkey(app, "ctrl+shift+space")
        self.assertFalse(ok)
        self.assertEqual(app.cfg.note_hotkey, "")     # не записано
        self.assertEqual(len(app.tray.notices), 1)    # тост показано
        self.assertEqual(app._applied, [])            # реєстрація не пробувалась

    def test_conflict_with_command_edit_rejected(self):
        app = self._app(command_edit_hotkey="ctrl+alt+e")
        self.assertFalse(DesktopApp.set_note_hotkey(app, "ctrl+alt+e"))

    def test_free_combo_accepted(self):
        app = self._app()
        ok = DesktopApp.set_note_hotkey(app, "ctrl+alt+n")
        self.assertTrue(ok)
        self.assertEqual(app.cfg.note_hotkey, "ctrl+alt+n")
        self.assertEqual(app._applied, ["ctrl+alt+n"])
        self.assertEqual(app.tray.notices, [])

    def test_register_failure_reported_honestly(self):
        # RegisterHotKey відхилив (комбінацію зайняла ІНША програма):
        # _apply_note_hotkey скидає cfg.note_hotkey → set повертає False + тост
        app = self._app()

        def apply_and_reset():
            app.cfg.note_hotkey = ""      # як робить реальний _apply_note_hotkey
        app._apply_note_hotkey = apply_and_reset
        ok = DesktopApp.set_note_hotkey(app, "ctrl+alt+q")
        self.assertFalse(ok)
        self.assertEqual(len(app.tray.notices), 1)

    def test_clear_always_ok(self):
        app = self._app(note_hotkey="ctrl+alt+n")
        self.assertTrue(DesktopApp.set_note_hotkey(app, ""))
        self.assertEqual(app.cfg.note_hotkey, "")

    def test_bookmark_now_checks_command_edit(self):
        app = self._app(command_edit_hotkey="ctrl+alt+e")
        app.bookmark_hotkey = SimpleNamespace(apply=lambda *a: None)
        self.assertFalse(DesktopApp.set_meeting_bookmark_hotkey(app, "ctrl+alt+e"))

    def test_action_hotkey_checks_note_and_command_edit(self):
        # Звірка №7: undo/insert теж мають бачити note/command_edit/bookmark
        app = self._app(note_hotkey="ctrl+alt+n",
                        command_edit_hotkey="ctrl+alt+e",
                        meeting_bookmark_hotkey="ctrl+alt+b")
        app.action_hotkeys = SimpleNamespace(apply=lambda *a: None)
        self.assertFalse(DesktopApp.set_action_hotkey(app, "undo", "ctrl+alt+n"))
        self.assertFalse(DesktopApp.set_action_hotkey(app, "insert", "ctrl+alt+e"))
        self.assertFalse(DesktopApp.set_action_hotkey(app, "undo", "ctrl+alt+b"))
        self.assertTrue(DesktopApp.set_action_hotkey(app, "undo", "ctrl+alt+u"))

    def test_apply_key_rejects_conflict_and_failed_rebind(self):
        # Звірка №8: PTT теж чесний — конфлікт або відмова RegisterHotKey →
        # False, cfg.ptt_key НЕ змінюється, тост показано, «успіху» нема
        app = self._app(note_hotkey="ctrl+alt+n")
        ok = DesktopApp._apply_key(app, "ctrl+alt+n")     # конфлікт з нотаткою
        self.assertFalse(ok)
        self.assertEqual(app.cfg.ptt_key, "ctrl+shift+space")
        self.assertEqual(len(app.tray.notices), 1)

        app2 = self._app()
        app2.hotkey = SimpleNamespace(rebind=lambda k: False)  # система відхилила
        self.assertFalse(DesktopApp._apply_key(app2, "ctrl+alt+q"))
        self.assertEqual(app2.cfg.ptt_key, "ctrl+shift+space") # cfg недоторканий
        self.assertEqual(len(app2.tray.notices), 1)

    def test_apply_key_success_path(self):
        app = self._app()
        app.hotkey = SimpleNamespace(rebind=lambda k: True)
        app.action_hotkeys = SimpleNamespace(reapply=lambda: None)
        app.bookmark_hotkey = SimpleNamespace(reapply=lambda: None)
        app.command_edit_hotkey = SimpleNamespace(reapply=lambda: None)
        app._apply_note_hotkey = lambda: None
        # Звірка рецензії №7: legacy-rebind знімає ВСІ хоткеї (keyboard.unhook_all) —
        # _apply_key має перевісити й панік-хоткей, інакше після ребайнду PTT
        # panic-lock тихо мертвий. Мутація (прибрати _apply_panic_hotkey) валить це.
        panic_reapplied = {"n": 0}
        app._apply_panic_hotkey = lambda: panic_reapplied.__setitem__("n", panic_reapplied["n"] + 1)
        shortcuts = []
        app.window = SimpleNamespace(dictation=SimpleNamespace(
            set_shortcut=shortcuts.append))
        self.assertTrue(DesktopApp._apply_key(app, "ctrl+alt+r"))
        self.assertEqual(app.cfg.ptt_key, "ctrl+alt+r")
        self.assertEqual(shortcuts, ["ctrl+alt+r"])
        self.assertEqual(panic_reapplied["n"], 1, "панік-хоткей має перевіситись після ребайнду PTT")


if __name__ == "__main__":
    unittest.main()
