"""Регрес: ``meeting_start`` мусить працювати з реальним модулем capture.

Баг живого тесту 19.07 (balachky.log): app.py:2898 у meeting_start звертався до
capture.NATIVE_CHANNELS, а модуль його не мав — коментар обіцяв імпорт, самого
рядка import не було (хвіст автозлиття). Наслідок: AttributeError → лог «Не
вдалося відкрити аудіо-потік наради» → режим «Нарада» був мертвий на старті.

Важливо: QA-гейт запускає ``unittest discover``, тому перевірки тут оформлені як
``unittest.TestCase``. Модульна pytest-функція в цьому файлі раніше була зеленою
лише на папері: штатний гейт її не знаходив.
"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import whisper_core.meeting as meeting
from whisper_core.meeting import capture
from whisper_core.meeting import session
from fronts.desktop.app import DesktopApp


class _SignalSpy:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _TraySpy:
    def __init__(self):
        self.notifications = []

    def notify(self, text):
        self.notifications.append(text)


class _Session:
    def __init__(self):
        self.dir = Path("meeting-start-regression")

    def mic_sink(self, _pcm):
        pass

    def close_segment(self, _track):
        pass


class _CaptureStream:
    opened = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        type(self).opened.append(self)

    def start(self):
        self.started = True

    def stop(self):
        pass


def _controller():
    return SimpleNamespace(
        cfg=SimpleNamespace(input_device=None, sounds=False,
                            meeting_screen_enabled=False),
        recorder=SimpleNamespace(recording=False),
        tray=_TraySpy(),
        meeting_state=_SignalSpy(),
        _busy=False,
        _capturing=False,
        _mic_testing=False,
        _meeting_active=False,
        _meeting_session=None,
        _meeting_streams={},
        _start_live_meeting=lambda: None,
        _stop_live_meeting=lambda: None,
        _meetings_root=lambda: Path("meeting-start-regression"),
        _on_meeting_device_lost=lambda *_a: None,
        _on_meeting_storage_error=lambda *_a: None,
    )


class MeetingCaptureReexportTests(unittest.TestCase):
    def test_capture_reexports_native_format(self):
        self.assertTrue(hasattr(capture, "NATIVE_CHANNELS"),
                        "capture.NATIVE_CHANNELS відсутній — нарада впаде")
        self.assertTrue(hasattr(capture, "NATIVE_RATE"),
                        "capture.NATIVE_RATE відсутній — нарада впаде")
        self.assertEqual(capture.NATIVE_CHANNELS, meeting.NATIVE_CHANNELS)
        self.assertEqual(capture.NATIVE_RATE, meeting.NATIVE_RATE)

    def test_meeting_start_uses_real_capture_exports_without_audio(self):
        """Дійти реальним ``DesktopApp.meeting_start`` до створення потоку.

        Підмінено лише межу з PortAudio та файлову сесію; сам імпорт
        ``whisper_core.meeting.capture`` і читання його NATIVE_* — продакшн-шлях.
        Якби ре-експорт знову зник, ``meeting_start`` упіймав би AttributeError і
        повернув False, тобто цей тест відтворив би саме живий регрес запуску.
        """
        ctl = _controller()
        fake_session = _Session()
        _CaptureStream.opened = []
        device = {"index": 7, "name": "Мікрофон (тест)"}

        with (patch.object(capture, "default_input", return_value=device),
              patch.object(capture, "CaptureStream", _CaptureStream),
              patch.object(session, "create_session", return_value=fake_session),
              patch("fronts.desktop.app._audit_event"),
              patch("fronts.desktop.app.diagnostic_event")):
            ok = DesktopApp.meeting_start(ctl, "onlymic")

        self.assertTrue(ok, ctl.tray.notifications)
        self.assertTrue(ctl._meeting_active)
        self.assertEqual(ctl.meeting_state.values, ["recording"])
        self.assertEqual(len(_CaptureStream.opened), 1)
        stream = _CaptureStream.opened[0]
        self.assertTrue(stream.started)
        self.assertEqual(stream.kwargs["channels"], meeting.NATIVE_CHANNELS)
        self.assertEqual(stream.kwargs["rate"], meeting.NATIVE_RATE)


if __name__ == "__main__":
    unittest.main()
