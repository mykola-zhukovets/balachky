"""feature/audio-qol: тест мікрофона + чутливість VAD.

Три групи:
  * серіалізація нових VAD-полів конфігу (round-trip save/load);
  * VAD-параметри з конфігу доходять до faster_whisper.transcribe (мок рушія);
  * пороги вердикту тесту мікрофона (чиста функція рівень→код).
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Тести регресії нижче імпортують DesktopApp — Qt без реального екрана.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from whisper_core.config import (
    Config, VAD_THRESHOLD_DEFAULT, VAD_MIN_SILENCE_MS_DEFAULT,
)
from whisper_core.engine import Engine
from fronts.desktop.recorder import classify_mic_level, peak_dbfs



class RecorderLiveSinkTests(unittest.TestCase):
    def test_callback_without_live_sink_does_not_raise(self):
        stream = Mock()
        with patch("fronts.desktop.recorder.sd.InputStream", return_value=stream):
            from fronts.desktop.recorder import Recorder
            recorder = Recorder(16000)
        recorder.start()
        recorder._cb(np.ones((8, 1), dtype=np.float32), 8, None, None)
        self.assertEqual(len(recorder._frames), 1)
        self.assertIsNone(recorder._live_sink)

    def test_set_live_sink_assigns_and_clears(self):
        # set_live_sink(fn) виставляє _live_sink, set_live_sink(None) — очищає
        stream = Mock()
        with patch("fronts.desktop.recorder.sd.InputStream", return_value=stream):
            from fronts.desktop.recorder import Recorder
            recorder = Recorder(16000)

        def sink(_block):
            pass

        recorder.set_live_sink(sink)
        self.assertIs(recorder._live_sink, sink)
        recorder.set_live_sink(None)
        self.assertIsNone(recorder._live_sink)


class StopLiveDictationCrashTests(unittest.TestCase):
    """Регресія крашу живого диктування: _stop_live_dictation зверталося до
    recorder.set_live_sink(), якого в Recorder не існувало (app.py:1715) —
    AttributeError валив зупинку кожного live-диктування. Unbound-виклик методу
    DesktopApp на SimpleNamespace, як у DictationSilenceTests (test_meeting_ui)."""

    def test_stop_live_dictation_does_not_raise(self):
        from fronts.desktop.app import DesktopApp
        from fronts.desktop.recorder import Recorder
        stream = Mock()
        with patch("fronts.desktop.recorder.sd.InputStream", return_value=stream):
            recorder = Recorder(16000)
        ns = SimpleNamespace(recorder=recorder, _live_dictation=None)
        DesktopApp._stop_live_dictation(ns)      # не має кидати AttributeError
        self.assertIsNone(recorder._live_sink)
        self.assertIsNone(ns._live_dictation)


class VadConfigTests(unittest.TestCase):
    def test_defaults_match_shared_constants(self):
        cfg = Config()
        self.assertEqual(cfg.vad_threshold, VAD_THRESHOLD_DEFAULT)
        self.assertEqual(cfg.vad_min_silence_ms, VAD_MIN_SILENCE_MS_DEFAULT)

    def test_vad_fields_survive_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.vad_threshold = 0.3
            cfg.vad_min_silence_ms = 900
            cfg.save(path)
            loaded = Config.load(path)
            self.assertEqual(loaded.vad_threshold, 0.3)
            self.assertEqual(loaded.vad_min_silence_ms, 900)


class VadTranscribeTests(unittest.TestCase):
    def test_config_vad_params_reach_transcribe(self):
        cfg = Config()
        cfg.vad_threshold = 0.42
        cfg.vad_min_silence_ms = 750
        with patch("whisper_core.engine.WhisperModel") as model:
            model.return_value.transcribe.return_value = (
                [], SimpleNamespace(duration=1.0))
            engine = Engine(cfg)
            engine.transcribe("audio.wav")
        kwargs = model.return_value.transcribe.call_args.kwargs
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(kwargs["vad_parameters"]["threshold"], 0.42)
        self.assertEqual(kwargs["vad_parameters"]["min_silence_duration_ms"], 750)

    def test_defaults_reach_transcribe_unchanged(self):
        cfg = Config()
        with patch("whisper_core.engine.WhisperModel") as model:
            model.return_value.transcribe.return_value = (
                [], SimpleNamespace(duration=1.0))
            Engine(cfg).transcribe("audio.wav")
        params = model.return_value.transcribe.call_args.kwargs["vad_parameters"]
        self.assertEqual(params["threshold"], VAD_THRESHOLD_DEFAULT)
        self.assertEqual(params["min_silence_duration_ms"], VAD_MIN_SILENCE_MS_DEFAULT)


class MicTestButtonTests(unittest.TestCase):
    """Кнопка «Перевірити мікрофон» не залипає. Без жодного мікрофона
    (recorder.has_stream == False) start_mic_test емітить вердикт СИНХРОННО
    (той самий GUI-потік → DirectConnection): слот результату відпрацьовує
    всередині виклику, і якщо «Записую…» ставиться після — воно перезатирає
    фінальний стан назавжди (воркер не стартував, сигналу більше не буде).
    Тому busy-стан має ставитись ДО start_mic_test."""

    class _Btn:
        def __init__(self):
            self.enabled = True
            self.text = ""

        def setEnabled(self, on):
            self.enabled = bool(on)

        def setText(self, text):
            self.text = text

    class _Note:
        def __init__(self):
            self.text = ""
            self.hidden = False

        def hide(self):
            self.hidden = True

        def setText(self, text):
            self.text = text

    def _page(self, start_mic_test):
        return SimpleNamespace(
            _mic_test_btn=self._Btn(),
            _mic_test_note=self._Note(),
            controller=SimpleNamespace(start_mic_test=start_mic_test),
        )

    def test_no_input_device_leaves_button_enabled_with_verdict(self):
        from fronts.desktop.pages.settings import SettingsPage
        from fronts.desktop.i18n import tr

        page = self._page(None)

        def sync_emit_silence():
            # як у app.start_mic_test без потоку: емітимо вердикт синхронно
            # ВСЕРЕДИНІ виклику (DirectConnection у тому самому потоці)
            SettingsPage._on_mic_test_result(page, "silence")
            return True

        page.controller.start_mic_test = sync_emit_silence
        with patch("fronts.desktop.pages.settings.motion"):
            SettingsPage._on_mic_test(page)
        # фінальний стан: кнопка УВІМКНЕНА, текст звичайний, вердикт «Тиша…»
        self.assertTrue(page._mic_test_btn.enabled)
        self.assertEqual(page._mic_test_btn.text, tr("set_mic_test"))
        self.assertEqual(page._mic_test_note.text, tr("set_mic_silence"))

    def test_worker_path_disables_until_result_arrives(self):
        from fronts.desktop.pages.settings import SettingsPage
        from fronts.desktop.i18n import tr

        page = self._page(lambda: True)          # воркер стартував, вердикт пізніше
        with patch("fronts.desktop.pages.settings.motion"):
            SettingsPage._on_mic_test(page)
            # поки тест триває — кнопка заглушена з «Записую…»
            self.assertFalse(page._mic_test_btn.enabled)
            self.assertEqual(page._mic_test_btn.text, tr("set_mic_testing"))
            SettingsPage._on_mic_test_result(page, "good")   # сигнал з воркера
        self.assertTrue(page._mic_test_btn.enabled)
        self.assertEqual(page._mic_test_btn.text, tr("set_mic_test"))
        self.assertEqual(page._mic_test_note.text, tr("set_mic_good"))

    def test_busy_gate_restores_button(self):
        from fronts.desktop.pages.settings import SettingsPage
        from fronts.desktop.i18n import tr

        page = self._page(lambda: False)         # зайнято — тест не стартував
        with patch("fronts.desktop.pages.settings.motion"):
            SettingsPage._on_mic_test(page)
        self.assertTrue(page._mic_test_btn.enabled)
        self.assertEqual(page._mic_test_btn.text, tr("set_mic_test"))


class MicTestClassificationTests(unittest.TestCase):
    """Пороги: < -50 dBFS → silence, -50..-24 → quiet, >= -24 → good."""

    @staticmethod
    def _const(peak: float, n: int = 16000):
        return np.full(n, peak, dtype=np.float32)

    def test_none_and_empty_are_silence(self):
        self.assertEqual(classify_mic_level(None), "silence")
        self.assertEqual(classify_mic_level(np.zeros(0, dtype=np.float32)), "silence")

    def test_digital_silence_is_silence(self):
        # заглушений мікрофон дає цифрову тишу (усі нулі) — гілка «тиша»
        self.assertEqual(classify_mic_level(np.zeros(48000, dtype=np.float32)),
                         "silence")
        self.assertEqual(peak_dbfs(np.zeros(10, dtype=np.float32)), float("-inf"))

    def test_below_silence_floor_is_silence(self):
        self.assertEqual(classify_mic_level(self._const(0.002)), "silence")  # -54 dBFS

    def test_quiet_band(self):
        self.assertEqual(classify_mic_level(self._const(0.01)), "quiet")     # -40 dBFS
        self.assertEqual(classify_mic_level(self._const(0.02)), "quiet")     # -34 dBFS

    def test_good_band(self):
        self.assertEqual(classify_mic_level(self._const(0.1)), "good")       # -20 dBFS
        self.assertEqual(classify_mic_level(self._const(0.25)), "good")      # -12 dBFS


class CleanupTeardownTests(unittest.TestCase):
    """Integration wave-2 (teardown): _cleanup від'єднує ВСІ крос-потокові
    сигнали, ВКЛЮЧНО з mic_test_result — він емітиться з daemon-потоку
    _mic_test_work (finally), і пропуск у розконект-списку = use-after-free
    при виході застосунку під час 3-секундного тесту мікрофона."""

    class _Sig:
        def __init__(self):
            self.disconnected = False

        def disconnect(self):
            self.disconnected = True

    def test_cleanup_disconnects_mic_test_result_and_joins_thread(self):
        from fronts.desktop.app import DesktopApp

        names = ("transcribed", "finished", "file_status", "file_done",
                 "rec_state", "transcription_error", "cpu_fallback",
                 "watch_ready", "mic_test_result", "preview_ready",
                 "meeting_state", "meeting_track_done", "meeting_session_done",
                 "meeting_error", "meeting_audio_ready",
                 "meeting_storage_warning", "meeting_screen_error",
                 "meeting_processing_progress", "meeting_processing_done",
                 "live_dictation_segment", "live_meeting_segment", "live_error",
                 "live_disable_requested", "dictation_audio_state",
                 "meeting_audio_state",
                 "screen_record_state", "screen_record_error",
                 "screen_record_finished")
        sigs = {n: self._Sig() for n in names}
        joined, stopped = [], []
        thread = SimpleNamespace(is_alive=lambda: True,
                                 join=lambda timeout: joined.append(timeout))
        c = SimpleNamespace(**sigs, _update_thread=None,
                            _mic_test_thread=thread, _meeting_streams={},
                            _shutdown_meeting_for_exit=lambda: None,
                            _clear_meeting_plain_cache=lambda: None,
                            _stop_live_dictation=lambda: stopped.append("dictation"),
                            _stop_live_meeting=lambda: stopped.append("meeting"))
        DesktopApp._cleanup(c)
        self.assertTrue(sigs["mic_test_result"].disconnected)   # головний guard
        self.assertTrue(all(s.disconnected for s in sigs.values()))
        self.assertEqual(stopped, ["dictation", "meeting"])
        self.assertEqual(len(joined), 1)   # потік тесту джойниться (з таймаутом)


if __name__ == "__main__":
    unittest.main()
