"""Юніти оркестрації диктофона у DesktopApp (feature/player-recordings).

Той самий підхід, що test_meeting_ui: SimpleNamespace-контролер зі спаями, на
якому викликаємо unbound-методи DesktopApp. Без Qt-подій, без реального аудіо —
recorder і tray фейкові, ядро recordings пише у tempfile."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from fronts.desktop.app import DesktopApp


class _Tray:
    def __init__(self):
        self.states = []
        self.msgs = []

    def set_state(self, s, text=None):
        self.states.append(s)

    def notify(self, text):
        self.msgs.append(text)


class _Recorder:
    """Фейковий recorder: тримає прапорець recording; заданий у start() sink
    (стрімінг диктофона) одразу отримує задане аудіо — імітація callback'а."""

    def __init__(self, audio=None, has_stream=True):
        self.recording = False
        self._audio = audio
        self.has_stream = has_stream
        self.started = False

    def start(self, sink=None):
        self.started = True
        self.recording = True
        if sink is not None and self._audio is not None:
            sink(self._audio)            # «callback» віддав увесь запис одним блоком

    def stop(self):
        self.recording = False
        return []

    def take_meter(self):
        return (0.3, 0.5)


def _controller(**overrides):
    tmp = overrides.pop("root", None) or tempfile.mkdtemp()
    audio = overrides.pop("audio", np.zeros(16000, dtype=np.float32))
    c = SimpleNamespace(
        cfg=SimpleNamespace(recordings_dir=None, sample_rate=16000, sounds=False),
        tray=_Tray(),
        recorder=_Recorder(audio),
        _busy=False,
        _capturing=False,
        _mic_testing=False,
        _meeting_active=False,
        _dictaphone_active=False,
        _dictaphone_started=0.0,
        _dictaphone_writer=None,
        window=SimpleNamespace(files=SimpleNamespace(added=[])),
    )
    c.window.files.add_files = lambda paths: c.window.files.added.extend(paths)
    c._recordings_root = lambda: Path(tmp)
    c.dictaphone_busy = lambda: DesktopApp.dictaphone_busy(c)
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class BusyGateTests(unittest.TestCase):
    def test_busy_reflects_flags(self):
        c = _controller()
        self.assertFalse(DesktopApp.dictaphone_busy(c))
        c._meeting_active = True
        self.assertTrue(DesktopApp.dictaphone_busy(c))
        c._meeting_active = False
        c.recorder.recording = True
        self.assertTrue(DesktopApp.dictaphone_busy(c))


class StartStopTests(unittest.TestCase):
    def test_start_sets_active_and_records(self):
        c = _controller()
        ok = DesktopApp.dictaphone_start(c)
        self.assertTrue(ok)
        self.assertTrue(c._dictaphone_active)
        self.assertTrue(c.recorder.started)
        self.assertEqual(c.tray.states, ["recording"])

    def test_start_refused_when_busy(self):
        c = _controller(_busy=True)
        self.assertFalse(DesktopApp.dictaphone_start(c))
        self.assertFalse(c._dictaphone_active)
        self.assertTrue(c.tray.msgs)          # попередив тостом

    def test_start_refused_without_mic(self):
        c = _controller()
        c.recorder.has_stream = False
        self.assertFalse(DesktopApp.dictaphone_start(c))
        self.assertFalse(c._dictaphone_active)

    def test_stop_saves_file_and_resets(self):
        c = _controller()
        DesktopApp.dictaphone_start(c)
        path = DesktopApp.dictaphone_stop(c)
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).exists())
        self.assertFalse(c._dictaphone_active)
        self.assertIn("idle", c.tray.states)
        # файл видно у переліку
        recs = DesktopApp.list_recordings(c)
        self.assertEqual(len(recs), 1)

    def test_stop_empty_audio_saves_nothing(self):
        c = _controller(audio=None)
        DesktopApp.dictaphone_start(c)
        self.assertIsNone(DesktopApp.dictaphone_stop(c))
        self.assertEqual(len(DesktopApp.list_recordings(c)), 0)

    def test_stop_without_start_is_noop(self):
        c = _controller()
        self.assertIsNone(DesktopApp.dictaphone_stop(c))

    def test_cancel_discards(self):
        c = _controller()
        DesktopApp.dictaphone_start(c)
        DesktopApp.dictaphone_cancel(c)
        self.assertFalse(c._dictaphone_active)
        self.assertEqual(len(DesktopApp.list_recordings(c)), 0)


class QueueTests(unittest.TestCase):
    def test_transcribe_recording_enqueues_to_files(self):
        c = _controller()
        DesktopApp.transcribe_recording(c, "C:/x/rec.wav")
        self.assertEqual(c.window.files.added, ["C:/x/rec.wav"])

    def test_delete_recording(self):
        c = _controller()
        DesktopApp.dictaphone_start(c)
        path = DesktopApp.dictaphone_stop(c)
        self.assertTrue(DesktopApp.delete_recording(c, Path(path).name))
        self.assertEqual(len(DesktopApp.list_recordings(c)), 0)


class ReverseGateTests(unittest.TestCase):
    """Активний диктофон блокує решту споживачів мікрофона/моделі."""

    def test_ptt_press_ignored_while_dictaphone_active(self):
        c = _controller(_dictaphone_active=True)
        c._key_down = False
        c._cancel_guard = False
        DesktopApp.on_press(c)
        self.assertFalse(c.recorder.started)     # запис диктування не стартував

    def test_meeting_start_refused_while_dictaphone_active(self):
        c = _controller(_dictaphone_active=True)
        self.assertFalse(DesktopApp.meeting_start(c, "onlymic"))
        self.assertTrue(c.tray.msgs)             # тост «зачекай»

    def test_mic_test_busy_while_dictaphone_active(self):
        c = _controller(_dictaphone_active=True)
        self.assertTrue(DesktopApp.mic_test_busy(c))

    def test_record_start_refused_while_dictaphone_active(self):
        c = _controller(_dictaphone_active=True)
        DesktopApp.record_start(c)
        self.assertFalse(c.recorder.started)


class MeetingAudioPathsTests(unittest.TestCase):
    def test_returns_existing_wavs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            sid = "2026-07-16_14-30-05"
            d = Path(tmp) / sid
            d.mkdir()
            (d / "mic.wav").write_bytes(b"RIFF")
            c = SimpleNamespace(
                _meeting_session_dir=lambda s: Path(tmp) / s,
                _meeting_tracks=lambda s: ["mic", "sys"])
            # meeting_audio_paths тепер бере media з materialized-теки; для
            # незашифрованої сесії реальний метод віддає саму теку сесії.
            c._materialized_meeting_dir = lambda s: DesktopApp._materialized_meeting_dir(c, s)
            out = DesktopApp.meeting_audio_paths(c, sid)
            self.assertEqual(set(out), {"mic"})
            self.assertEqual(out["mic"], d / "mic.wav")


if __name__ == "__main__":
    unittest.main()
