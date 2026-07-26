"""Recording-only contracts for the first meeting pipeline slice."""
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from whisper_core import config
from whisper_core.meeting import capture, postprocess
from whisper_core.meeting.session import MeetingSession, load_meta, record_audio_exports


class RecordingSourceConfigTests(unittest.TestCase):
    def test_arbitrary_microphones_plus_system_round_trip(self):
        tokens = [
            config.meeting_microphone_token("USB Conference"),
            config.meeting_microphone_token("Headset Mic"),
            config.MEETING_SYSTEM_SOURCE,
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = config.Config(meeting_record_sources=tokens,
                                meeting_export_segment_minutes=10)
            cfg.save(path)
            loaded = config.Config.load(path)
        self.assertEqual(config.meeting_record_source_specs(loaded), [
            config.MeetingSourceSpec("microphone", "USB Conference"),
            config.MeetingSourceSpec("microphone", "Headset Mic"),
            config.MeetingSourceSpec("system", None),
        ])
        self.assertEqual(loaded.meeting_export_segment_minutes, 10)

    def test_legacy_preset_remains_a_compatible_fallback(self):
        cfg = SimpleNamespace(meeting_record_sources=[], meeting_sources="mic+sys",
                              meeting_mic_devices=[], input_device="Built-in Mic")
        self.assertEqual(config.meeting_record_source_specs(cfg), [
            config.MeetingSourceSpec("microphone", "Built-in Mic"),
            config.MeetingSourceSpec("system", None),
        ])

    def test_canonical_sources_cap_microphones_but_keep_system_loopback(self):
        tokens = [config.meeting_microphone_token(f"Mic {number}")
                  for number in range(1, config.MEETING_MULTIMIC_MAX + 3)]
        tokens.append(config.MEETING_SYSTEM_SOURCE)
        cfg = SimpleNamespace(meeting_record_sources=tokens)

        specs = config.meeting_record_source_specs(cfg)

        self.assertEqual(
            [spec.device_name for spec in specs if spec.kind == "microphone"],
            [f"Mic {number}"
             for number in range(1, config.MEETING_MULTIMIC_MAX + 1)])
        self.assertEqual(sum(spec.kind == "system" for spec in specs), 1)


class CaptureDeviceListTests(unittest.TestCase):
    def test_lists_only_unique_wasapi_microphones(self):
        devices = [
            {"index": 0, "name": "USB Mic", "hostApi": 1,
             "maxInputChannels": 2, "isLoopbackDevice": False},
            {"index": 1, "name": "USB Mic", "hostApi": 7,
             "maxInputChannels": 2, "isLoopbackDevice": False},
            {"index": 2, "name": "Speakers [Loopback]", "hostApi": 7,
             "maxInputChannels": 2, "isLoopbackDevice": True},
            {"index": 3, "name": "Room Mic", "hostApi": 7,
             "maxInputChannels": 1, "isLoopbackDevice": False},
        ]

        class FakePA:
            def get_host_api_info_by_type(self, _kind):
                return {"index": 7}
            def get_device_count(self):
                return len(devices)
            def get_device_info_by_index(self, index):
                return devices[index]
            def terminate(self):
                pass

        with patch.object(capture, "_pa", return_value=FakePA()), \
                patch.object(capture, "pyaudio", SimpleNamespace(paWASAPI=13)):
            found = capture.list_input_devices()
        self.assertEqual([(item["index"], item["name"]) for item in found], [
            (1, "USB Mic"), (3, "Room Mic"),
        ])


class SegmentedWhisperAudioTests(unittest.TestCase):
    @staticmethod
    def _wav_info(path):
        with wave.open(str(path), "rb") as wav:
            return (wav.getnchannels(), wav.getsampwidth(),
                    wav.getframerate(), wav.getnframes())

    def test_each_track_exports_independent_ten_minute_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = MeetingSession(Path(tmp), ["mic1", "mic2", "sys"],
                                     rate=80, channels=1,
                                     export_segment_seconds=10)
            for track, frequency in (("mic1", 2), ("mic2", 3), ("sys", 5)):
                x = np.arange(80 * 25, dtype=np.float32) / 80
                audio = (0.25 * np.sin(2 * np.pi * frequency * x)).astype(np.float32)
                session.sink(track)(audio.tobytes())
            session.finalize()
            exports = postprocess.build_segmented_wavs(
                session.dir, segment_seconds=10, out_rate=16)
            self.assertEqual(set(exports), {"mic1", "mic2", "sys"})
            self.assertEqual([len(exports[track]) for track in exports], [3, 3, 3])
            for paths in exports.values():
                self.assertEqual([self._wav_info(path) for path in paths], [
                    (1, 2, 16, 160), (1, 2, 16, 160), (1, 2, 16, 80),
                ])

    def test_all_silent_tracks_keep_wavs_not_discarded(self):
        # С2: коли ВСІ доріжки тихі — WAV не викидаємо, інакше нарада стане
        # «done без аудіо» й UI ніколи не дасть її обробити чи прослухати.
        with tempfile.TemporaryDirectory() as tmp:
            session = MeetingSession(Path(tmp), ["mic", "sys"], rate=80,
                                     channels=1, export_segment_seconds=10)
            for track in ("mic", "sys"):
                session.sink(track)(np.zeros(80 * 5, dtype=np.float32).tobytes())
            session.finalize()
            exports = postprocess.build_segmented_wavs(
                session.dir, segment_seconds=10, out_rate=16)
            self.assertTrue(exports)
            for paths in exports.values():
                self.assertTrue(paths and all(p.exists() for p in paths))

    def test_silent_track_dropped_when_another_has_sound(self):
        # Тиша серед звучних доріжок і далі відкидається (шум/зайвий мік).
        with tempfile.TemporaryDirectory() as tmp:
            session = MeetingSession(Path(tmp), ["mic", "sys"], rate=80,
                                     channels=1, export_segment_seconds=10)
            x = np.arange(80 * 5, dtype=np.float32) / 80
            session.sink("mic")(
                (0.25 * np.sin(2 * np.pi * 3 * x)).astype(np.float32).tobytes())
            session.sink("sys")(np.zeros(80 * 5, dtype=np.float32).tobytes())
            session.finalize()
            exports = postprocess.build_segmented_wavs(
                session.dir, segment_seconds=10, out_rate=16)
            self.assertIn("mic", exports)
            self.assertNotIn("sys", exports)


class RecordingMetadataTests(unittest.TestCase):
    def test_sources_gaps_and_exports_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_snapshot = [
                {"track": "mic1", "kind": "microphone", "device_name": "USB Mic"},
                {"track": "sys", "kind": "system", "device_name": "Speakers"},
            ]
            session = MeetingSession(Path(tmp), ["mic1", "sys"], rate=80, channels=1,
                                     export_segment_seconds=600,
                                     recording_sources=source_snapshot)
            session.record_audio_gap("sys", 1.25, reason="device_reconnect")
            session.finalize()
            record_audio_exports(session.dir, {
                "mic1": [session.dir / "audio" / "mic1" / "0001.wav"],
                "sys": [session.dir / "audio" / "sys" / "0001.wav"],
            })
            meta = load_meta(session.dir)
            raw = json.loads((session.dir / "meeting.json").read_text(encoding="utf-8"))
        self.assertEqual(meta.recording_sources, source_snapshot)
        self.assertEqual(meta.export_segment_seconds, 600)
        self.assertEqual(meta.audio_discontinuities[0]["track"], "sys")
        self.assertEqual(meta.audio_discontinuities[0]["duration_seconds"], 1.25)
        self.assertEqual(meta.audio_discontinuities[0]["reason"], "device_reconnect")
        self.assertEqual(raw["audio_files"]["mic1"], ["audio/mic1/0001.wav"])


if __name__ == "__main__":
    unittest.main()
