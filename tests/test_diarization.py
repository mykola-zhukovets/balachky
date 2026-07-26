"""Юніти діаризації без onnx: лише зшивання та збереження."""
import hashlib
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from whisper_core.config import Config
from whisper_core.meeting import postprocess
from whisper_core.meeting.session import MeetingMeta, set_speaker_name
from whisper_core.meeting import diarize
from whisper_core.meeting import diarization_models


class FakeDiarizer:
    def __call__(self, _audio, **_kwargs):
        return [(0.0, 1.0, "raw-a"), (1.0, 2.0, "raw-b")]


class SpeakerStitchTests(unittest.TestCase):
    def test_words_choose_largest_overlap_and_keep_boundary_word(self):
        words = [{"start": .1, "end": .4, "word": "one"},
                 {"start": .9, "end": 1.1, "word": "two"},
                 {"start": 1.2, "end": 1.5, "word": "three"}]
        segments, labels = postprocess.speaker_segments_from_words(
            words, [(0, 1, "a"), (1, 2, "b")])
        self.assertEqual([x[3] for x in segments], ["speaker_1", "speaker_2"])
        self.assertEqual(labels, {"a": "speaker_1", "b": "speaker_2"})

    def test_words_without_diarization_overlap_are_kept_unlabeled(self):
        segments, labels = postprocess.speaker_segments_from_words(
            [{"start": 0, "end": .5, "word": "kept"}], [(1, 2, "raw-a")])
        self.assertEqual(segments, [(0.0, .5, "kept", postprocess.SPK_SINGLE)])
        self.assertEqual(labels, {})

    def test_empty_diarization_keeps_all_words_unlabeled(self):
        words = [{"start": 0, "end": .5, "word": "all"}, {"start": .5, "end": 1, "word": "text"}]
        segments, labels = postprocess.speaker_segments_from_words(words, [])
        self.assertEqual(segments, [(0.0, 1.0, "all text", postprocess.SPK_SINGLE)])
        self.assertEqual(labels, {})

    def test_segments_without_words_are_absent(self):
        segments, labels = postprocess.speaker_segments_from_words(
            [], [(0, 1, "a")])
        self.assertEqual(segments, [])
        self.assertEqual(labels, {})

    def test_overlapping_speaker_segments_keep_word_once_by_max_overlap(self):
        """Накладання голосів: слово перекриває ДВА мовці одночасно → мітка за
        більшим перекриттям, слово лишається рівно раз (не дублюється, не зникає)."""
        words = [{"start": .4, "end": .7, "word": "overlap"}]
        diar = [(0.0, 0.55, "a"), (0.45, 1.0, "b")]   # сегменти перетинаються
        segments, labels = postprocess.speaker_segments_from_words(words, diar)
        self.assertEqual([(s, e, t) for s, e, t, _ in segments], [(.4, .7, "overlap")])
        self.assertEqual(segments[0][3], labels["b"])  # b дає 0.25 проти 0.15 у a

    def test_uncertain_clustering_gap_keeps_every_word(self):
        """Невпевнена кластеризація лишає дірки між сегментами: слова у дірці
        мусять лишитись у транскрипті (без мовця), жодне слово не губиться."""
        words = [{"start": .1, "end": .4, "word": "alpha"},   # у сегменті a
                 {"start": .6, "end": .7, "word": "gap"},      # у дірці 0.5–0.8
                 {"start": .9, "end": 1.1, "word": "beta"}]    # у сегменті b
        diar = [(0.0, 0.5, "a"), (0.8, 1.3, "b")]
        segments, _ = postprocess.speaker_segments_from_words(words, diar)
        joined = " ".join(t for _, _, t, _ in segments)
        for token in ("alpha", "gap", "beta"):
            self.assertIn(token, joined)
        self.assertEqual(
            [seg[3] for seg in segments if "gap" in seg[2]], [postprocess.SPK_SINGLE])

    def test_diarized_sys_track_with_gap_words_survives_stitch(self):
        """Наскрізь: діаризований sys (мовці + слова-без-мовця) через stitch з mic —
        увесь текст sys присутній у фінальному транскрипті, нічого не випадає."""
        words = [{"start": .1, "end": .4, "word": "hi"},
                 {"start": .6, "end": .7, "word": "unmatched"}]
        sys_segs, _ = postprocess.speaker_segments_from_words(words, [(0.0, 0.5, "a")])
        utterances = postprocess.stitch([(0.0, 0.5, "me line")], sys_segs)
        text = postprocess.to_transcript_text(utterances, me_label="Me", others_label="Others")
        for token in ("me line", "hi", "unmatched"):
            self.assertIn(token, text)

    def test_disabled_or_failed_diarization_keeps_untagged_others(self):
        utterances = postprocess.stitch([(0, 1, "me")], [(0, 1, "other")])
        self.assertIn("Others", postprocess.to_transcript_text(
            utterances, me_label="Me", others_label="Others"))


class StorageTests(unittest.TestCase):
    def test_name_map_round_trip_and_export(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            meta = MeetingMeta(1, "2026-01-01_00-00-00", 0, "done", "both", ["mic", "sys"])
            (root / "meeting.json").write_text(meta.to_json(), encoding="utf-8")
            saved = set_speaker_name(root, "speaker_1", "Alice")
            self.assertEqual(saved.speaker_names["speaker_1"], "Alice")
            utterances = [postprocess.Utterance(0, 1, "speaker_1", "hello")]
            postprocess.write_transcript(root, utterances, me_label="Me", others_label="Others",
                                         speaker_names=saved.speaker_names)
            self.assertIn("Alice: hello", (root / "transcript.txt").read_text(encoding="utf-8"))

    def test_clearing_name_keeps_editable_speaker_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            meta = MeetingMeta(1, "2026-01-01_00-00-00", 0, "done", "both", ["mic", "sys"],
                               speaker_names={"speaker_1": "Speaker 1"})
            (root / "meeting.json").write_text(meta.to_json(), encoding="utf-8")
            saved = set_speaker_name(root, "speaker_1", "")
            self.assertIn("speaker_1", saved.speaker_names)

    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            cfg = Config(diarization_enabled=True, diarization_num_speakers=2)
            cfg.save(path)
            loaded = Config.load(path)
            self.assertTrue(loaded.diarization_enabled)
            self.assertEqual(loaded.diarization_num_speakers, 2)


class ValidateSpeakerCountTests(unittest.TestCase):
    def test_validate_speaker_count_valid(self):
        from whisper_core.meeting.diarize import validate_speaker_count
        self.assertEqual(validate_speaker_count(2), 2)
        self.assertEqual(validate_speaker_count(10), 10)
        self.assertEqual(validate_speaker_count("5"), 5)

    def test_validate_speaker_count_invalid(self):
        from whisper_core.meeting.diarize import validate_speaker_count
        self.assertIsNone(validate_speaker_count(1))
        self.assertIsNone(validate_speaker_count(11))
        self.assertIsNone(validate_speaker_count("invalid"))
        self.assertIsNone(validate_speaker_count(None))


if __name__ == "__main__":
    unittest.main()



class ModelIntegrityTests(unittest.TestCase):
    def test_bad_size_or_hash_is_not_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for relative, (size, _digest) in diarize.MODEL_MANIFEST.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * size)
            self.assertFalse(diarize.models_available(root))

    def test_reparse_point_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(diarize, "_is_reparse_point", return_value=True):
                self.assertFalse(diarize.models_available(root))


class _FakeResp:
    """Мінімальний HTTP-response як контекст-менеджер для _download_asset."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body
        self._pos = 0

    def getcode(self):
        return self.status

    def read(self, n):
        chunk = self._body[self._pos:self._pos + n]
        self._pos += n
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_assets(seg=b"segmentation-bytes", emb=b"embedding-bytes"):
    assets = (
        diarization_models.ModelAsset(
            "segmentation", "http://x/seg", diarize.SEGMENTATION_RELATIVE,
            len(seg), hashlib.sha256(seg).hexdigest(), "repo", "MIT"),
        diarization_models.ModelAsset(
            "embedding", "http://x/emb", Path(diarize.EMBEDDING_NAME),
            len(emb), hashlib.sha256(emb).hexdigest(), "repo", "Apache-2.0"),
    )
    manifest = {
        diarize.SEGMENTATION_RELATIVE: (len(seg), hashlib.sha256(seg).hexdigest()),
        Path(diarize.EMBEDDING_NAME): (len(emb), hashlib.sha256(emb).hexdigest()),
    }
    return assets, manifest, {"segmentation": seg, "embedding": emb}


class ModelInstallTests(unittest.TestCase):
    def test_install_verifies_writes_ready_and_activates(self):
        assets, manifest, data = _fake_assets()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(diarize, "MODEL_MANIFEST", manifest), \
                mock.patch.object(diarization_models, "ASSETS", assets):
            target = Path(td) / "diarization"
            target.mkdir()
            (target / "partial").write_text("old", encoding="utf-8")

            def fake_dl(asset, part, *, received_before, progress_cb=None, cancel_check=None):
                part.parent.mkdir(parents=True, exist_ok=True)
                part.write_bytes(data[asset.id])
                if progress_cb:
                    progress_cb(received_before + asset.size, sum(a.size for a in assets))

            with mock.patch.object(diarization_models, "_download_asset", side_effect=fake_dl):
                diarization_models.download_and_install(target)
            self.assertTrue(diarize.models_available(target))
            self.assertTrue((target / diarization_models.READY_NAME).is_file())
            self.assertFalse((target / "partial").exists())

    def test_download_asset_resumes_valid_partial(self):
        assets, _manifest, data = _fake_assets(seg=b"HELLOWORLD")
        asset = assets[0]                       # 10 байтів
        with tempfile.TemporaryDirectory() as td:
            part = Path(td) / f"{asset.sha256}.part"
            part.write_bytes(b"HELLO")          # 5 байтів уже є
            resp = _FakeResp(206, {"Content-Range": "bytes 5-9/10"}, b"WORLD")

            def fake_urlopen(request, timeout=None):
                self.assertEqual(request.headers.get("Range"), "bytes=5-")
                return resp

            with mock.patch.object(diarization_models.urllib.request, "urlopen", fake_urlopen):
                diarization_models._download_asset(asset, part, received_before=0)
            self.assertEqual(part.read_bytes(), b"HELLOWORLD")

    def test_download_asset_restarts_on_200(self):
        assets, _m, _d = _fake_assets(seg=b"HELLOWORLD")
        asset = assets[0]
        with tempfile.TemporaryDirectory() as td:
            part = Path(td) / f"{asset.sha256}.part"
            part.write_bytes(b"XX")             # биті 2 байти
            resp = _FakeResp(200, {}, b"HELLOWORLD")   # сервер ігнорує Range
            with mock.patch.object(diarization_models.urllib.request, "urlopen",
                                   lambda request, timeout=None: resp):
                diarization_models._download_asset(asset, part, received_before=0)
            self.assertEqual(part.read_bytes(), b"HELLOWORLD")

    def test_download_asset_cancel_preserves_partial(self):
        assets, _m, _d = _fake_assets(seg=b"HELLOWORLD")
        asset = assets[0]
        with tempfile.TemporaryDirectory() as td:
            part = Path(td) / f"{asset.sha256}.part"
            resp = _FakeResp(200, {}, b"HELLOWORLD")
            with mock.patch.object(diarization_models.urllib.request, "urlopen",
                                   lambda request, timeout=None: resp):
                with self.assertRaises(InterruptedError):
                    diarization_models._download_asset(
                        asset, part, received_before=0, cancel_check=lambda: True)
            self.assertTrue(part.exists())      # .part лишається для докачки

    def test_download_asset_bad_sha_removes_partial(self):
        assets, _m, _d = _fake_assets(seg=b"HELLOWORLD")
        asset = assets[0]                       # sha очікує HELLOWORLD
        with tempfile.TemporaryDirectory() as td:
            part = Path(td) / f"{asset.sha256}.part"
            resp = _FakeResp(200, {}, b"GOODBYEXXX")   # 10 байтів, але не ті
            with mock.patch.object(diarization_models.urllib.request, "urlopen",
                                   lambda request, timeout=None: resp):
                with self.assertRaises(diarization_models.DiarizationDownloadError):
                    diarization_models._download_asset(asset, part, received_before=0)
            self.assertFalse(part.exists())     # биті байти не тримаємо

    def test_oversized_partial_is_discarded(self):
        assets, _m, _d = _fake_assets(seg=b"HELLOWORLD")
        asset = assets[0]
        with tempfile.TemporaryDirectory() as td:
            part = Path(td) / f"{asset.sha256}.part"
            part.write_bytes(b"X" * 99)         # більше за очікувані 10
            self.assertEqual(diarization_models._part_valid_size(part, asset.size), 0)
            self.assertFalse(part.exists())
