"""Тести логіки тумблера довіри «Вимкнути обробку зараз» у Нараді
(fronts.desktop.app.DesktopApp.set_meeting_live_processing + guard
_start_live_meeting). Стиль — виклик незв'язаних методів на фейк-контролері,
як у PipelineGateTests test_macros.py."""
import unittest
from types import SimpleNamespace

from fronts.desktop import app as desktop_app


def _controller(*, meeting_active=True, live_disabled=False,
                live_transcription=True):
    calls = {"stop": 0, "start": 0}
    ctrl = SimpleNamespace(
        _meeting_active=meeting_active,
        _meeting_live_disabled=live_disabled,
        _live_meeting=None,
        cfg=SimpleNamespace(live_transcription=live_transcription),
    )
    ctrl._stop_live_meeting = lambda: calls.__setitem__("stop", calls["stop"] + 1)
    ctrl._start_live_meeting = lambda: calls.__setitem__("start", calls["start"] + 1)
    return ctrl, calls


class SetLiveProcessingTests(unittest.TestCase):
    def test_disable_stops_live_and_sets_flag(self):
        ctrl, calls = _controller()
        desktop_app.DesktopApp.set_meeting_live_processing(ctrl, False)
        self.assertTrue(ctrl._meeting_live_disabled)
        self.assertEqual(calls["stop"], 1)
        self.assertEqual(calls["start"], 0)   # вимкнення не піднімає живу обробку

    def test_reenable_starts_live_when_meeting_active(self):
        ctrl, calls = _controller(live_disabled=True)
        desktop_app.DesktopApp.set_meeting_live_processing(ctrl, True)
        self.assertFalse(ctrl._meeting_live_disabled)
        self.assertEqual(calls["start"], 1)

    def test_reenable_without_active_meeting_does_not_start(self):
        ctrl, calls = _controller(meeting_active=False, live_disabled=True)
        desktop_app.DesktopApp.set_meeting_live_processing(ctrl, True)
        self.assertFalse(ctrl._meeting_live_disabled)
        self.assertEqual(calls["start"], 0)


class StartLiveMeetingGuardTests(unittest.TestCase):
    """Guard _start_live_meeting поважає прапорець тумблера й конфіг: у обох
    випадках метод повертається ДО створення LiveTranscriber (не чіпає движок)."""

    def _make(self, *, live_transcription, live_disabled):
        calls = {"stop": 0}
        ctrl = SimpleNamespace(
            cfg=SimpleNamespace(live_transcription=live_transcription),
            _meeting_live_disabled=live_disabled,
            _live_meeting="sentinel",
        )
        ctrl._stop_live_meeting = lambda: calls.__setitem__("stop", calls["stop"] + 1)
        return ctrl, calls

    def test_disabled_flag_short_circuits_before_stop(self):
        # прапорець вимкнено → вихід до _stop_live_meeting і створення движка
        ctrl, calls = self._make(live_transcription=True, live_disabled=True)
        desktop_app.DesktopApp._start_live_meeting(ctrl)
        self.assertEqual(calls["stop"], 0)
        self.assertEqual(ctrl._live_meeting, "sentinel")   # не перезаписано

    def test_live_off_short_circuits(self):
        ctrl, calls = self._make(live_transcription=False, live_disabled=False)
        desktop_app.DesktopApp._start_live_meeting(ctrl)
        self.assertEqual(calls["stop"], 0)


if __name__ == "__main__":
    unittest.main()
