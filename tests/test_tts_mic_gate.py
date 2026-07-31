"""Хвиля 1: гейт before_microphone_start — порядок дій (§9.1, §11.2).

Ключова вимога: playback зупинено й TTS витіснено ДО повернення True (тобто ДО
recorder.start у викликачі). Мок фіксує порядок."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.tts_mic_gate import before_microphone_start
from whisper_core.heavy_models import HeavyModelCoordinator, TTS


class TestMicGateOrder(unittest.TestCase):
    def test_stop_and_yield_before_return(self):
        log = []
        coord = HeavyModelCoordinator(
            total_ram_provider=lambda: 8 * 1024 ** 3,
            tts_cancel=lambda: log.append("cancel"),
            tts_shutdown=lambda: log.append("shutdown"))
        coord.acquire(TTS, active=True)
        ok = before_microphone_start(
            "ptt",
            stop_playback=lambda: log.append("stop_playback"),
            coordinator=coord,
            confirm_stopped=lambda: (log.append("confirm") or True),
            on_step=lambda s: log.append(s))
        self.assertTrue(ok)
        # playback зупинено ПЕРШИМ, потім cancel+shutdown TTS, потім підтвердження
        self.assertLess(log.index("stop_playback"), log.index("cancel"))
        self.assertLess(log.index("cancel"), log.index("shutdown"))
        self.assertLess(log.index("shutdown"), log.index("confirm"))
        # lease звільнено (мікрофон переміг)
        self.assertIsNone(coord._leases.get(TTS))

    def test_returns_true_even_when_callbacks_raise(self):
        # мікрофон мусить стартувати навіть за збою TTS-приладдя
        def boom():
            raise RuntimeError("збій")
        coord = HeavyModelCoordinator(total_ram_provider=lambda: 8 * 1024 ** 3,
                                      tts_shutdown=boom)
        ok = before_microphone_start("meeting", stop_playback=boom, coordinator=coord)
        self.assertTrue(ok)

    def test_order_log_sequence(self):
        steps = []
        coord = HeavyModelCoordinator(total_ram_provider=lambda: 8 * 1024 ** 3)
        before_microphone_start("note", stop_playback=None, coordinator=coord,
                                on_step=steps.append)
        self.assertEqual(steps,
                         ["gate:note", "playback_stopped", "tts_yielded", "confirmed"])


class TestMicGateBarrier(unittest.TestCase):
    """CRITICAL §9.1: гейт — справжній БАР'ЄР. Не підтверджено Stopped → hard-kill →
    якщо все одно не зупинено, повертає False (запис НЕ стартує)."""

    def _coord(self):
        return HeavyModelCoordinator(total_ram_provider=lambda: 8 * 1024 ** 3)

    def test_confirmed_stopped_returns_true(self):
        ok = before_microphone_start("ptt", stop_playback=None, coordinator=self._coord(),
                                     confirm_stopped=lambda: True)
        self.assertTrue(ok)

    def test_not_stopped_triggers_hard_kill_then_false(self):
        killed = []
        # confirm_stopped завжди False (озвучка не зупиняється) → hard_kill → все одно
        # False → викликач мусить перервати старт запису
        ok = before_microphone_start(
            "ptt", stop_playback=None, coordinator=self._coord(),
            confirm_stopped=lambda: False, hard_kill=lambda: killed.append(1))
        self.assertFalse(ok)                     # запис НЕ стартує
        self.assertEqual(killed, [1])            # hard-kill TTS викликано

    def test_hard_kill_then_confirmed_returns_true(self):
        state = {"stopped": False}

        def confirm():
            return state["stopped"]

        def kill():
            state["stopped"] = True              # hard-kill примусово зупинив

        ok = before_microphone_start("ptt", stop_playback=None, coordinator=self._coord(),
                                     confirm_stopped=confirm, hard_kill=kill)
        self.assertTrue(ok)                      # після hard-kill підтверджено зупинено


class TestAppGateBlocksRecording(unittest.TestCase):
    """CRITICAL §9.1: у реальному app-шляху гейт False → recorder.start НЕ викликано
    (запис не стартує). Мутація (прибрати `if not: return`) червонить."""

    def test_note_record_start_aborts_when_gate_false(self):
        from fronts.desktop.app import DesktopApp
        started = []
        ctl = SimpleNamespace(
            note_busy=lambda: False,
            recorder=SimpleNamespace(has_stream=True,
                                     start=lambda *a, **k: started.append(1)),
            _note_dictating=False, _rec_started=0.0,
            _before_microphone_start=lambda reason: False,   # БАР'ЄР блокує
            tray=SimpleNamespace(notify=lambda *_: None))
        with patch("fronts.desktop.app._snapshot_processing_mode", return_value=None), \
                patch("fronts.desktop.app.diagnostic_event"):
            result = DesktopApp.note_record_start(ctl)
        self.assertFalse(result)
        self.assertEqual(started, [])            # recorder.start НЕ викликано
        self.assertFalse(ctl._note_dictating)    # стан відкочено

    def test_note_record_start_proceeds_when_gate_true(self):
        from fronts.desktop.app import DesktopApp
        started = []
        ctl = SimpleNamespace(
            note_busy=lambda: False,
            recorder=SimpleNamespace(has_stream=True,
                                     start=lambda *a, **k: started.append(1)),
            _note_dictating=False, _rec_started=0.0,
            _before_microphone_start=lambda reason: True,    # гейт пропускає
            tray=SimpleNamespace(notify=lambda *_: None, set_state=lambda *_: None),
            _set_note_state=lambda *_: None)
        with patch("fronts.desktop.app._snapshot_processing_mode", return_value=None), \
                patch("fronts.desktop.app.diagnostic_event"):
            try:
                DesktopApp.note_record_start(ctl)
            except Exception:                    # noqa: BLE001 — далі йдуть інші залежності
                pass
        self.assertEqual(started, [1])           # recorder.start ВИКЛИКАНО


class TestAllSixGatesGuarded(unittest.TestCase):
    """CRITICAL §9.1 (рецензія): УСІ 6 точок мік-гейту перевіряють результат (`if not …:
    return`). AST-структурний захист від регресу в БУДЬ-ЯКІЙ точці — прибирання
    `if not` у будь-якому сайті робить його негардженим → червонить."""

    def _app_ast(self):
        import ast
        src = (Path(__file__).resolve().parents[1] / "fronts" / "desktop"
               / "app.py").read_text(encoding="utf-8")
        return ast.parse(src)

    def test_every_gate_call_is_guarded(self):
        import ast
        tree = self._app_ast()
        # усі string-константи "_before_microphone_start" = кількість викликів гейту
        total = sum(1 for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and n.value == "_before_microphone_start")
        # виклики всередині `if not <...>:` (UnaryOp Not у тесті if)
        guarded = 0
        for n in ast.walk(tree):
            if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp) \
                    and isinstance(n.test.op, ast.Not):
                if any(isinstance(c, ast.Constant) and c.value == "_before_microphone_start"
                       for c in ast.walk(n.test)):
                    guarded += 1
        # 6 сайтів (кожен рядок getattr(...,"_before_microphone_start",...)) + метод
        # містить ще один string у власному getattr? Ні — метод визначений через def.
        self.assertGreaterEqual(total, 6, "очікувалось ≥6 викликів гейту")
        self.assertEqual(guarded, total,
                         "є негарджений виклик _before_microphone_start (не в `if not`)")


class TestCommandGateBlocks(unittest.TestCase):
    def test_command_record_start_aborts_when_gate_false(self):
        from fronts.desktop.app import DesktopApp
        started = []
        ctl = SimpleNamespace(
            note_busy=lambda: False,
            recorder=SimpleNamespace(has_stream=True,
                                     start=lambda *a, **k: started.append(1)),
            _command_dictating=False, _rec_started=0.0, _command_dialog=None,
            _before_microphone_start=lambda reason: False,   # БАР'ЄР блокує
            tray=SimpleNamespace(notify=lambda *_: None, set_state=lambda *_: None))
        DesktopApp._command_record_start(ctl)
        self.assertEqual(started, [])            # recorder.start НЕ викликано
        self.assertFalse(ctl._command_dictating)


if __name__ == "__main__":
    unittest.main()
