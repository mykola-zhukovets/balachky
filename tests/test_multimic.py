"""Мультимік Наради: N треків, зшивання, конфіг і перейменування."""
import json
import tempfile
import unittest
from pathlib import Path

from whisper_core.config import (Config, MEETING_PRESET_BOTH, MEETING_PRESET_MULTIMIC,
                                 MEETING_PRESET_ONLYMIC, meeting_preset_for_cfg,
                                 meeting_source_set, meeting_sources_for_preset)
from whisper_core.meeting.postprocess import stitch_tracks, to_transcript_text
from whisper_core.meeting.session import MeetingSession, load_meta, set_speaker_name


class MultiMicConfigTests(unittest.TestCase):
    def test_multimic_round_trip_and_legacy_presets(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            cfg = Config(meeting_sources="multimic", meeting_mic_devices=["M1", "M2", "M3"])
            cfg.save(path)
            loaded = Config.load(path)
        self.assertEqual(loaded.meeting_mic_devices, ["M1", "M2", "M3"])
        self.assertEqual(meeting_preset_for_cfg(loaded), MEETING_PRESET_MULTIMIC)
        self.assertEqual(meeting_source_set(loaded), {"mic1", "mic2", "mic3"})
        self.assertEqual(meeting_sources_for_preset(MEETING_PRESET_ONLYMIC), "mic")
        self.assertEqual(meeting_sources_for_preset(MEETING_PRESET_BOTH), "mic+sys")


class MultiMicSessionTests(unittest.TestCase):
    def test_three_tracks_write_three_files_and_meta(self):
        with tempfile.TemporaryDirectory() as td:
            session = MeetingSession(Path(td), ["mic1", "mic2", "mic3"], rate=100,
                                     channels=1, track_devices={"mic1": "M1", "mic2": "M2", "mic3": "M3"})
            for track, value in zip(("mic1", "mic2", "mic3"), (b"\x01", b"\x02", b"\x03")):
                session.sink(track)(value * 16)
            session.finalize()
            meta = load_meta(session.dir)
            self.assertEqual(meta.sources, ["mic1", "mic2", "mic3"])
            self.assertEqual(meta.track_devices, {"mic1": "M1", "mic2": "M2", "mic3": "M3"})
            self.assertEqual(len(list(session.dir.glob("mic*/*.f32"))), 3)

    def test_track_rename_persists_in_meeting_json(self):
        with tempfile.TemporaryDirectory() as td:
            session = MeetingSession(Path(td), ["mic1", "mic2"])
            set_speaker_name(session.dir, "mic2", "Олена")
            data = json.loads((session.dir / "meeting.json").read_text(encoding="utf-8"))
            self.assertEqual(data["speaker_names"]["mic2"], "Олена")


class MultiMicPostprocessTests(unittest.TestCase):
    def test_stitch_tracks_orders_and_labels_microphones(self):
        utterances = stitch_tracks({
            "mic1": [(2.0, 3.0, "друга")],
            "mic2": [(1.0, 1.5, "перша")],
            "mic3": [(4.0, 5.0, "третя")],
        })
        self.assertEqual([u.speaker for u in utterances], ["mic2", "mic1", "mic3"])
        text = to_transcript_text(utterances, me_label="Я", others_label="Співрозмовники",
                                  speaker_names={"mic1": "Мікрофон 1", "mic2": "Олена", "mic3": "Мікрофон 3"})
        self.assertIn("Олена: перша", text)
        self.assertIn("Мікрофон 1: друга", text)


if __name__ == "__main__":
    unittest.main()