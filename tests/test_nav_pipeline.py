"""feature/office-voice-nav — гейт голосової навігації у PTT-конвеєрі (_work).

Розпізнана команда споживається (клавіші у закріплену ціль через send_nav) і далі
конвеєром НЕ йде (картка/історія не пишуться); невпізнане лишається текстом; при
вимкненому режимі фраза-команда — звичайний текст. Стиль — як PipelineGateTests у
test_macros.py."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fronts.desktop import app as desktop_app
from fronts.desktop import paste


class NavPipelineGateTests(unittest.TestCase):
    @staticmethod
    def _controller(final, voice_nav_enabled=True, formfill_capturing=False):
        recorded = {"emitted": False}

        class Transcribed:
            def emit(self, raw, fin, words, ts):
                recorded["emitted"] = True
                recorded["final"] = fin

        class Signal:
            def __init__(self):
                self.count = 0

            def emit(self, *args):
                self.count += 1
                self.last = args

        controller = SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: ("raw", final, 1.0, [], []),
            output_mode="show",
            transcription_error=Signal(),
            transcribed=Transcribed(),
            formfill_text=Signal(),
            finished=Signal(),
            macros={},
            formfill_capturing=formfill_capturing,
            _nav_aliases={"далі": "next_field"},
            _nav_target_hwnd=111,
            cfg=SimpleNamespace(sounds=False, voice_punctuation=False,
                                voice_nav_enabled=voice_nav_enabled, language="uk"),
            _busy=True,
        )
        return controller, recorded

    def _run(self, controller):
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())

    def test_command_consumed_not_pasted(self):
        controller, recorded = self._controller("наступне поле")
        with patch.object(desktop_app, "send_nav",
                          return_value=paste.NAV_OK) as sn:
            self._run(controller)
        sn.assert_called_once()
        self.assertEqual(sn.call_args.args[0], ("key", "tab"))
        self.assertFalse(recorded["emitted"])  # картка НЕ показана — команда з'їдена

    def test_pinned_target_passed_to_send_nav(self):
        controller, _ = self._controller("наступне поле")
        with patch.object(desktop_app, "send_nav",
                          return_value=paste.NAV_OK) as sn:
            self._run(controller)
        self.assertEqual(sn.call_args.kwargs.get("target_hwnd"), 111)

    def test_cell_goto_command(self):
        controller, recorded = self._controller("комірка Б7")
        with patch.object(desktop_app, "send_nav",
                          return_value=paste.NAV_OK) as sn:
            self._run(controller)
        self.assertEqual(sn.call_args.args[0], ("goto", "B7"))
        self.assertFalse(recorded["emitted"])

    def test_user_alias_command(self):
        controller, recorded = self._controller("далі")
        with patch.object(desktop_app, "send_nav",
                          return_value=paste.NAV_OK) as sn:
            self._run(controller)
        self.assertEqual(sn.call_args.args[0], ("key", "tab"))
        self.assertFalse(recorded["emitted"])

    def test_plain_text_passes_through(self):
        controller, recorded = self._controller("командиру роти доповідаю")
        with patch.object(desktop_app, "send_nav") as sn:
            self._run(controller)
        sn.assert_not_called()
        self.assertTrue(recorded["emitted"])
        self.assertEqual(recorded["final"], "командиру роти доповідаю")

    def test_disabled_mode_phrase_is_text(self):
        controller, recorded = self._controller("наступне поле",
                                                voice_nav_enabled=False)
        with patch.object(desktop_app, "send_nav") as sn:
            self._run(controller)
        sn.assert_not_called()
        self.assertTrue(recorded["emitted"])
        self.assertEqual(recorded["final"], "наступне поле")

    def test_formfill_capturing_takes_precedence(self):
        # у режимі заповнення шаблону диктант іде в поле діалогу, не в навігацію
        controller, recorded = self._controller("наступне поле",
                                                formfill_capturing=True)
        with patch.object(desktop_app, "send_nav") as sn:
            self._run(controller)
        sn.assert_not_called()
        self.assertEqual(controller.formfill_text.count, 1)
        self.assertFalse(recorded["emitted"])

    def test_blocked_still_consumes_command(self):
        # send_nav заблокував (напр., вікно змінилось) — команда все одно з'їдена,
        # текст «наступне поле» не вставляється як зміст
        controller, recorded = self._controller("наступне поле")
        with patch.object(desktop_app, "send_nav",
                          return_value=desktop_app.PASTE_BLOCKED):
            self._run(controller)
        self.assertFalse(recorded["emitted"])
        self.assertEqual(controller.transcription_error.count, 1)


if __name__ == "__main__":
    unittest.main()
