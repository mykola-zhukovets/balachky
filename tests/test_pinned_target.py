"""feature/paste-safety — закріплення цілі вставки (pinned target).

Зміна активного вікна від старту диктування детектується, і вставка НЕ робиться
наосліп (військовий кейс: краще не вставити, ніж потрапити у чужий чат) — текст
лишається в буфері + тост. Той самий фокус — вставка як раніше. Гейт конфігу
вимикає перевірку. Реальний SendInput/буфер не чіпаємо. Стиль — test_cascade_paste.
"""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from fronts.desktop.paste import target_changed


class TargetChangedTests(unittest.TestCase):
    def test_no_pin_never_blocks(self):
        # ціль не закріплювали (напр. «повторити вставку» з трею)
        self.assertFalse(target_changed(None, 123))
        self.assertFalse(target_changed(0, 123))

    def test_same_hwnd_is_unchanged(self):
        self.assertFalse(target_changed(555, 555))

    def test_different_hwnd_is_changed(self):
        self.assertTrue(target_changed(555, 777))
        self.assertTrue(target_changed(555, None))   # фокус зник → теж «змінилось»

    def test_reused_hwnd_treated_as_same_documented_tradeoff(self):
        # СВІДОМИЙ КОМПРОМІС (LOW, рецензія): звірка лише за HWND. Якщо Windows
        # перевикористає той самий числовий HWND для нового вікна, ми трактуємо
        # його як «те саме» — крихітне вікно гонки, ловити дорожче за користь.
        # Тест фіксує контракт: зміна на звірку класу/PID оновить і його, і
        # коментар у target_changed.
        self.assertFalse(target_changed(555, 555))


class DeliverPasteGuardTests(unittest.TestCase):
    """_deliver_paste: закріплена ціль звіряється перед фактичною вставкою."""

    @staticmethod
    def _app(confirm=True):
        messages = []

        class Err:
            def emit(self, msg):
                messages.append(msg)

        undo = MagicMock()
        from whisper_core.qol import PasteHistory
        history = PasteHistory()
        app = SimpleNamespace(
            cfg=SimpleNamespace(
                restore_clipboard=False,
                paste_typing_fallback=False,
                paste_confirm_sound=False,
                paste_confirm_on_window_change=confirm),
            transcription_error=Err(),
            _undo=undo,
            _paste_history=history,
        )
        return app, messages, undo, history

    def _deliver(self, app, pinned, current_target, paste_result="ctrl_v"):
        from fronts.desktop import app as desktop_app
        from fronts.desktop import wininput
        clip = MagicMock()
        with patch.object(desktop_app, "paste_text",
                          return_value=paste_result) as pt, \
             patch.object(desktop_app, "cancel_clipboard_restore") as cancel, \
             patch.object(wininput, "capture_paste_target",
                          return_value=current_target), \
             patch.dict(sys.modules, {"pyperclip": clip}):
            desktop_app._deliver_paste(app, "текст", False, pinned_target=pinned)
        return pt, clip, cancel

    def test_window_changed_blocks_paste(self):
        app, messages, undo, history = self._app(confirm=True)
        pt, clip, cancel = self._deliver(app, pinned=(111, "Поле"),
                                         current_target=(999, "Чужий чат"))
        pt.assert_not_called()                       # наосліп НЕ вставляємо
        cancel.assert_called_once_with()             # старий timer не затре текст
        clip.copy.assert_called_once_with("текст")   # текст лишається в буфері
        self.assertEqual(len(messages), 1)
        self.assertIn("Чужий чат", messages[0])      # у тості — нове вікно
        self.assertFalse(history.has_items())        # у буфер вставок не пишемо
        undo.record.assert_not_called()

    def test_same_window_pastes_as_before(self):
        app, messages, undo, history = self._app(confirm=True)
        pt, clip, _ = self._deliver(app, pinned=(111, "Поле"),
                                    current_target=(111, "Поле"))
        pt.assert_called_once_with("текст", typing_fallback=False)
        self.assertEqual(messages, [])
        self.assertEqual(history.recent(), ["текст"])   # вставку записано
        undo.record.assert_called_once_with("текст")

    def test_gate_off_pastes_even_if_changed(self):
        app, messages, undo, history = self._app(confirm=False)
        pt, _, _ = self._deliver(app, pinned=(111, "Поле"),
                                 current_target=(999, "Інше"))
        pt.assert_called_once_with("текст", typing_fallback=False)
        self.assertEqual(messages, [])

    def test_no_pin_skips_guard(self):
        # «повторити вставку» з трею: закріплення нема → перевірку не робимо
        app, messages, undo, history = self._app(confirm=True)
        pt, _, _ = self._deliver(app, pinned=None, current_target=(999, "Інше"))
        pt.assert_called_once_with("текст", typing_fallback=False)
        self.assertEqual(messages, [])


class QueueForcedGuardTests(unittest.TestCase):
    """feature/dictation-queue (рекомендація рецензії): для джоба ЧЕРГИ (pinned_pid
    відомий → відкладена вставка) звірка вікна ПРИМУСОВА, навіть коли тумблер
    paste_confirm_on_window_change вимкнено. Причина: між записом і вставкою з
    черги минає значно більше часу (фонова розшифровка попередніх фраз), тож
    фокус устигає піти в чуже вікно набагато ймовірніше — відкладеність робить
    захист ВАЖЛИВІШИМ. Звірка за HWND І PID (target_changed_ex)."""

    @staticmethod
    def _app(confirm):
        messages = []

        class Err:
            def emit(self, msg):
                messages.append(msg)

        from whisper_core.qol import PasteHistory
        undo = MagicMock()
        history = PasteHistory()
        app = SimpleNamespace(
            cfg=SimpleNamespace(
                restore_clipboard=False,
                paste_typing_fallback=False,
                paste_confirm_sound=False,
                paste_confirm_on_window_change=confirm),
            transcription_error=Err(),
            _undo=undo,
            _paste_history=history,
        )
        return app, messages, undo, history

    def _deliver(self, app, pinned, pinned_pid, current_target, current_pid,
                 paste_result="ctrl_v"):
        from fronts.desktop import app as desktop_app
        from fronts.desktop import wininput
        clip = MagicMock()
        with patch.object(desktop_app, "paste_text",
                          return_value=paste_result) as pt, \
             patch.object(wininput, "capture_paste_target",
                          return_value=current_target), \
             patch.object(wininput, "get_window_pid",
                          return_value=current_pid), \
             patch.dict(sys.modules, {"pyperclip": clip}):
            desktop_app._deliver_paste(app, "текст", False,
                                       pinned_target=pinned, pinned_pid=pinned_pid)
        return pt, clip

    def test_queue_blocks_changed_window_even_with_gate_off(self):
        # головний кейс рецензії: тумблер ВИМКНЕНО, але це черга → звірка все одно діє
        app, messages, undo, history = self._app(confirm=False)
        pt, clip = self._deliver(app, pinned=(111, "Поле"), pinned_pid=1000,
                                 current_target=(999, "Чужий чат"),
                                 current_pid=2000)
        pt.assert_not_called()                        # наосліп у чуже вікно НЕ йдемо
        clip.copy.assert_called_once_with("текст")    # текст лишається в буфері
        self.assertEqual(len(messages), 1)
        self.assertIn("Чужий чат", messages[0])
        self.assertFalse(history.has_items())
        undo.record.assert_not_called()

    def test_queue_blocks_reused_hwnd_by_pid_with_gate_off(self):
        # той самий числовий HWND, але ІНШИЙ процес (Windows перевикористав HWND
        # між записом і вставкою) → PID ловить підміну навіть із вимкненим тумблером
        app, messages, _, _ = self._app(confirm=False)
        pt, clip = self._deliver(app, pinned=(111, "Поле"), pinned_pid=1000,
                                 current_target=(111, "Поле"), current_pid=2000)
        pt.assert_not_called()
        clip.copy.assert_called_once_with("текст")
        self.assertEqual(len(messages), 1)

    def test_queue_same_window_and_pid_pastes(self):
        # ціль не мінялась → черга вставляє як звичайно (гейт не заважає)
        app, messages, undo, history = self._app(confirm=False)
        pt, _ = self._deliver(app, pinned=(111, "Поле"), pinned_pid=1000,
                              current_target=(111, "Поле"), current_pid=1000)
        pt.assert_called_once_with("текст", typing_fallback=False)
        self.assertEqual(messages, [])
        self.assertEqual(history.recent(), ["текст"])
        undo.record.assert_called_once_with("текст")

    def test_immediate_gate_off_still_pastes_into_changed_window(self):
        # контраст: НЕгайне диктування (pinned_pid=None) з вимкненим тумблером —
        # поведінка як була: вставляємо навіть у змінене вікно (черга ≠ негайне)
        app, messages, _, _ = self._app(confirm=False)
        pt, _ = self._deliver(app, pinned=(111, "Поле"), pinned_pid=None,
                              current_target=(999, "Інше"), current_pid=2000)
        pt.assert_called_once_with("текст", typing_fallback=False)
        self.assertEqual(messages, [])


class _FakeSignal:
    """Мінімальний сурогат PySide-сигналу: connect зберігає, emit кличе."""
    def __init__(self):
        self._cb = None

    def connect(self, cb):
        self._cb = cb

    def emit(self, *args):
        if self._cb is not None:
            self._cb(*args)


class _FakeCard:
    """Сурогат PreviewCard: не чіпає Qt, лише несе сигнали й no-op показ."""
    def __init__(self, text=""):
        self.text = text
        self.accepted = _FakeSignal()
        self.copied = _FakeSignal()
        self.cancelled = _FakeSignal()

    def show_near_cursor(self):
        pass


class PreviewPinTests(unittest.TestCase):
    """feature/paste-safety: шлях перегляду (paste_preview=True) мусить
    використовувати пін зі СТАРТУ диктування, а не перезахоплювати ціль після
    розшифровки. Регрес: перемикання вікна під час розшифровки → вставка в чуже
    вікно без попередження (саме той сценарій, від якого фіча захищає)."""

    @staticmethod
    def _self_ns(pinned, confirm=True):
        deliver = MagicMock()
        ns = SimpleNamespace(
            _paste_target=pinned,
            cfg=SimpleNamespace(paste_confirm_on_window_change=confirm),
            _preview_card=None,
            _close_preview=lambda: None,
            _preview_copy=lambda t: None,
            _preview_deliver=deliver,
        )
        return ns, deliver

    def _run_preview_ready(self, ns, current_target):
        from fronts.desktop import app as desktop_app
        from fronts.desktop import preview, wininput
        card = _FakeCard("текст")
        with patch.object(preview, "PreviewCard", return_value=card), \
             patch.object(wininput, "capture_paste_target",
                          return_value=current_target):
            desktop_app.DesktopApp._on_preview_ready(ns, "текст", False)
        return card

    def test_switch_during_transcription_uses_start_pin_and_flags_desync(self):
        # старт диктування закріпив A=(111); під час розшифровки фокус на B=(999)
        ns, deliver = self._self_ns(pinned=(111, "Поле A"))
        card = self._run_preview_ready(ns, current_target=(999, "Чужий чат"))
        card.accepted.emit("текст")                  # користувач тисне «Вставити»
        deliver.assert_called_once()
        args = deliver.call_args.args
        self.assertEqual(args[2], (111, "Поле A"))    # ціль — пін СТАРТУ, не (999)
        self.assertFalse(args[3])                     # focus_ok=False — розсинхрон
        self.assertEqual(args[4], "Чужий чат")        # для тосту — нове вікно

    def test_same_window_keeps_focus_ok(self):
        ns, deliver = self._self_ns(pinned=(111, "Поле A"))
        card = self._run_preview_ready(ns, current_target=(111, "Поле A"))
        card.accepted.emit("текст")
        args = deliver.call_args.args
        self.assertEqual(args[2], (111, "Поле A"))
        self.assertTrue(args[3])                      # фокус той самий → вставляємо

    def test_gate_off_never_flags_desync(self):
        ns, deliver = self._self_ns(pinned=(111, "Поле A"), confirm=False)
        card = self._run_preview_ready(ns, current_target=(999, "Інше"))
        card.accepted.emit("текст")
        self.assertTrue(deliver.call_args.args[3])    # гейт вимкнено → не блокуємо


class PreviewDeliverGuardTests(unittest.TestCase):
    """_preview_deliver: розсинхрон фокуса блокує вставку (буфер+тост), той самий
    фокус — повертає ціль СТАРТУ й доставляє наявним шляхом."""

    @staticmethod
    def _app(confirm=True):
        messages = []

        class Err:
            def emit(self, msg):
                messages.append(msg)

        return SimpleNamespace(
            cfg=SimpleNamespace(paste_confirm_on_window_change=confirm),
            transcription_error=Err(),
            _preview_card=object(),
        ), messages

    def test_desync_blocks_without_paste(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop import wininput
        app, messages = self._app(confirm=True)
        clip = MagicMock()
        with patch.dict(sys.modules, {"pyperclip": clip}), \
             patch.object(desktop_app.threading, "Thread") as thread, \
             patch.object(wininput, "set_foreground_window") as sfw:
            desktop_app.DesktopApp._preview_deliver(
                app, "текст", False, (111, "A"), focus_ok=False,
                changed_window="Чужий чат")
        clip.copy.assert_called_once_with("текст")   # текст лишається в буфері
        thread.assert_not_called()                    # вставку НЕ запускаємо
        sfw.assert_not_called()                       # і фокус не смикаємо
        self.assertEqual(len(messages), 1)
        self.assertIn("Чужий чат", messages[0])

    def test_focus_ok_restores_start_pin_and_delivers(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop import wininput
        app, messages = self._app(confirm=True)
        with patch.object(desktop_app.threading, "Thread") as thread, \
             patch.object(wininput, "set_foreground_window") as sfw:
            desktop_app.DesktopApp._preview_deliver(
                app, "текст", True, (111, "A"), focus_ok=True)
        sfw.assert_called_once_with(111)              # фокус повертаємо піну СТАРТУ
        thread.assert_called_once()
        kw = thread.call_args.kwargs
        self.assertIs(kw["target"], desktop_app._deliver_paste)
        self.assertEqual(kw["args"], (app, "текст", True, (111, "A")))
        self.assertEqual(messages, [])


class ConfigGateTests(unittest.TestCase):
    def test_default_is_true(self):
        # безпека «з коробки»: за замовчуванням підтверджуємо зміну вікна
        from whisper_core.config import Config
        self.assertTrue(Config().paste_confirm_on_window_change)

    def test_save_load_roundtrip(self):
        import os
        import tempfile
        from whisper_core.config import Config
        c = Config()
        c.paste_confirm_on_window_change = False   # не-дефолт → має персиститись
        fd, path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            c.save(path)
            self.assertFalse(Config.load(path).paste_confirm_on_window_change)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
