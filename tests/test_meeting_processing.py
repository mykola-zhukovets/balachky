import json
import tempfile
import unittest
import wave
from collections import Counter
from dataclasses import FrozenInstanceError
from pathlib import Path

from whisper_core.meeting.meeting_pipeline import CancelToken, process_meeting
from whisper_core.meeting.session import MeetingMeta, atomic_write_json, load_meta
from whisper_core.meeting.word_ledger import (
    ImmutableLedgerError,
    SpeakerAssignment,
    WordRecord,
    read_word_ledger,
    write_word_ledger,
)


def _write_wav(path: Path, *, seconds: float = 1.0, rate: int = 16000) -> None:
    frames = max(1, int(seconds * rate))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * frames)


def _make_session(
        root: Path, tracks=("mic", "sys"), blocks=1, *,
        preset="both") -> Path:
    session_dir = root / "2026-07-19_12-00-00"
    session_dir.mkdir()
    audio_files = {}
    for track in tracks:
        paths = []
        for index in range(blocks):
            path = session_dir / f"{track}-{index + 1:04d}.wav"
            _write_wav(path)
            paths.append(path.name)
        audio_files[track] = paths
    meta = MeetingMeta(
        schema=2, id=session_dir.name, created=1, status="done",
        preset=preset, sources=list(tracks), audio_files=audio_files,
    )
    atomic_write_json(session_dir / "meeting.json", json.loads(meta.to_json()))
    return session_dir


def _asr_result(word: str, *, start=0.1, end=0.4):
    timed = [{"start": start, "end": end, "word": word}]
    return word, word, 1.0, [(word, 0.9)], [(start, end, word)], timed


class _FakeDiarRuntime:
    """Класифікує кліп за середньою амплітудою: >0.15→B, >0.05→A (як slice3-юніт)."""

    def __init__(self):
        self.embedding_dim = 2

    def diarize_window(self, samples, *, cancel_check=None):
        import numpy as np
        samples = np.asarray(samples, dtype=np.float32)
        hop = 8000
        spans = []
        for start in range(0, samples.size, hop):
            clip = samples[start:start + hop]
            if clip.size == 0:
                continue
            mean = float(np.mean(np.abs(clip)))
            label = "B" if mean > 0.15 else ("A" if mean > 0.05 else None)
            if label is None:
                continue
            if spans and spans[-1][2] == label and start - spans[-1][1] <= hop:
                spans[-1] = (spans[-1][0], start + clip.size, label)
            else:
                spans.append((start, start + clip.size, label))
        return spans

    def embed(self, samples):
        import numpy as np
        mean = float(np.mean(np.abs(np.asarray(samples, dtype=np.float32))))
        if mean > 0.15:
            return np.array([0.0, 1.0], dtype=np.float32)
        if mean > 0.05:
            return np.array([1.0, 0.0], dtype=np.float32)
        return None

    def cluster(self, features, *, num_speakers, distance_threshold=0.5):
        from whisper_core.meeting.diarize import cluster_embeddings
        return cluster_embeddings(features, num_speakers=num_speakers,
                                  distance_threshold=distance_threshold)


def _two_speaker_sys_wav(path, *, rate=16000):
    import math
    import numpy as np
    freq, slot, gap = 200.0, 3 * rate, rate // 3
    blocks = []
    for i in range(6):
        amp = 0.3 if i % 2 == 0 else 0.7
        t = np.arange(slot) / rate
        blocks.append((amp * np.sin(2 * math.pi * freq * t)).astype(np.float32))
        blocks.append(np.zeros(gap, dtype=np.float32))
    mono = np.clip(np.concatenate(blocks), -1, 1)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.round(mono * 32767).astype("<i2").tobytes())
    return mono.size


class _ProfileStub:
    """Мінімальний профіль для voice_memory: тека профілю (ПОЗА текою сесії)."""

    def __init__(self, d):
        self.dir = Path(d)

    @property
    def voice_memory_path(self):
        return self.dir / "voices.json"


class DiarizationIntegrationTests(unittest.TestCase):
    def _session_with_real_sys(self, root):
        from whisper_core.meeting.diarization_pipeline import DiarizationSettings
        session_dir = root / "2026-07-22_10-00-00"
        session_dir.mkdir()
        _write_wav(session_dir / "mic-0001.wav", seconds=1.0)
        frames = _two_speaker_sys_wav(session_dir / "sys-0001.wav")
        meta = MeetingMeta(
            schema=2, id=session_dir.name, created=1, status="done",
            preset="both", sources=["mic", "sys"],
            audio_files={"mic": ["mic-0001.wav"], "sys": ["sys-0001.wav"]},
        )
        atomic_write_json(session_dir / "meeting.json", json.loads(meta.to_json()))
        return session_dir, frames, DiarizationSettings

    def test_diarization_labels_sys_and_persists_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _frames, Settings = self._session_with_real_sys(Path(tmp))
            result = process_meeting(
                session_dir,
                transcribe=lambda path, **kw: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники",
                diarization=Settings(enabled=True, num_speakers=2),
                diarization_runtime_loader=_FakeDiarRuntime,
                speaker_label="Спікер {number}")
            self.assertEqual(result.status, "complete")
            meta = load_meta(session_dir)
            diar = meta.processing["diarization"]
            self.assertEqual(diar["status"], "complete")
            self.assertEqual(diar["num_speakers"], 2)
            self.assertIn("speaker_01", meta.speaker_names)
            self.assertEqual(meta.speaker_names["speaker_01"], "Спікер 1")
            self.assertTrue((session_dir / "diarization.final.json").is_file())
            self.assertTrue((session_dir / "speaker-assignments.jsonl").is_file())
            # sys-репліки отримали speaker_id; mic лишився без нього
            exported = json.loads(
                (session_dir / "transcript.json").read_text(encoding="utf-8"))
            sys_rows = [r for r in exported if r.get("source") == "others"]
            self.assertTrue(any(r.get("speaker_id") for r in sys_rows))
            mic_rows = [r for r in exported if r.get("source") == "me"]
            self.assertTrue(all("speaker_id" not in r for r in mic_rows))

    # ── voice_pending (Т41, варіант B): центроїди поза текою сесії ────────────
    def _process_with_consent(self, session_dir, Settings, profile, *, consent):
        return process_meeting(
            session_dir,
            transcribe=lambda path, **kw: _asr_result(Path(path).stem),
            asr_provenance={"engine": "faster-whisper"},
            me_label="Я", others_label="Співрозмовники",
            diarization=Settings(enabled=True, num_speakers=2,
                                 voice_memory_enabled=consent, profile=profile),
            diarization_runtime_loader=_FakeDiarRuntime,
            speaker_label="Спікер {number}")

    def test_consent_off_writes_no_voice_pending(self):
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as prof_tmp:
            session_dir, _f, Settings = self._session_with_real_sys(Path(tmp))
            profile = _ProfileStub(prof_tmp)
            self._process_with_consent(session_dir, Settings, profile, consent=False)
            pending_dir = Path(prof_tmp) / "voice_pending"
            self.assertFalse(pending_dir.exists() and any(pending_dir.iterdir()))
            art = json.loads(
                (session_dir / "diarization.final.json").read_text(encoding="utf-8"))
            self.assertNotIn("speaker_centroids", art)

    def test_consent_on_pending_enables_rename_bootstrap_after_restart(self):
        from whisper_core.meeting import voice_memory
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as prof_tmp:
            session_dir, _f, Settings = self._session_with_real_sys(Path(tmp))
            profile = _ProfileStub(prof_tmp)
            self._process_with_consent(session_dir, Settings, profile, consent=True)
            # артефакт БЕЗ центроїдів (біометрія не в доказовому пакеті)
            art = json.loads(
                (session_dir / "diarization.final.json").read_text(encoding="utf-8"))
            self.assertNotIn("speaker_centroids", art)
            # pending-файл існує ПОЗА текою сесії
            pending = Path(prof_tmp) / "voice_pending" / (session_dir.name + ".json")
            self.assertTrue(pending.is_file())
            self.assertFalse(str(pending.resolve()).startswith(str(session_dir.resolve())))
            spk = next(iter(json.loads(pending.read_text(encoding="utf-8"))["centroids"]))
            # СИМУЛЯЦІЯ РЕСТАРТУ: свіже читання зі сховища (новий доступ до диска)
            centroid = voice_memory.take_pending_centroid(profile, session_dir.name, spk)
            self.assertIsNotNone(centroid)
            voice_memory.add_or_update_voice(profile, "Олена", centroid)
            self.assertIn("Олена", voice_memory.load_voices(profile))
            # enroll-once: запис спожито
            self.assertIsNone(
                voice_memory.take_pending_centroid(profile, session_dir.name, spk))

    def test_delete_meeting_clears_pending(self):
        from whisper_core.meeting import voice_memory
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as prof_tmp:
            session_dir, _f, Settings = self._session_with_real_sys(Path(tmp))
            profile = _ProfileStub(prof_tmp)
            self._process_with_consent(session_dir, Settings, profile, consent=True)
            pending = Path(prof_tmp) / "voice_pending" / (session_dir.name + ".json")
            self.assertTrue(pending.is_file())
            voice_memory.delete_pending_centroids(profile, session_dir.name)
            self.assertFalse(pending.exists())

    def test_evidence_package_excludes_biometrics(self):
        import zipfile
        from whisper_core.meeting.evidence import export_evidence
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as prof_tmp:
            session_dir, _f, Settings = self._session_with_real_sys(Path(tmp))
            profile = _ProfileStub(prof_tmp)
            self._process_with_consent(session_dir, Settings, profile, consent=True)
            out_zip = Path(tmp) / "evidence.zip"
            export_evidence(session_dir, out_zip)
            with zipfile.ZipFile(out_zip) as z:
                names = z.namelist()
                self.assertFalse(any("voice_pending" in n for n in names))
                diar = next(n for n in names if n.endswith("diarization.final.json"))
                payload = json.loads(z.read(diar).decode("utf-8"))
                self.assertNotIn("speaker_centroids", payload)
                self.assertNotIn("speaker_centroids", payload.get("diagnostics", {}))

    def test_zero_loss_after_diarization(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _frames, Settings = self._session_with_real_sys(Path(tmp))
            process_meeting(
                session_dir,
                transcribe=lambda path, **kw: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники",
                diarization=Settings(enabled=True, num_speakers=2),
                diarization_runtime_loader=_FakeDiarRuntime,
                speaker_label="Спікер {number}")
            ledger_ids = [w.word_id for w in
                          read_word_ledger(session_dir / "words.sys.jsonl")
                          + read_word_ledger(session_dir / "words.mic.jsonl")]
            exported = json.loads(
                (session_dir / "transcript.json").read_text(encoding="utf-8"))
            exported_ids = [wid for u in exported for wid in u["word_ids"]]
            self.assertEqual(Counter(exported_ids), Counter(ledger_ids))
            # speaker-assignments.jsonl — sys-only: рівно один рядок на sys-слово
            sys_ids = [w.word_id for w in
                       read_word_ledger(session_dir / "words.sys.jsonl")]
            lines = [l for l in (session_dir / "speaker-assignments.jsonl")
                     .read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), len(sys_ids))

    def test_runtime_missing_degrades_to_plain_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _frames, Settings = self._session_with_real_sys(Path(tmp))

            def loader():
                raise RuntimeError("sherpa відсутній")

            result = process_meeting(
                session_dir,
                transcribe=lambda path, **kw: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники",
                diarization=Settings(enabled=True, num_speakers=2),
                diarization_runtime_loader=loader,
                speaker_label="Спікер {number}")
            self.assertEqual(result.status, "complete")   # ASR не зламано
            meta = load_meta(session_dir)
            self.assertEqual(meta.processing["diarization"]["status"], "unavailable")
            self.assertIn("Співрозмовники",
                          (session_dir / "transcript.txt").read_text(encoding="utf-8"))
            self.assertFalse((session_dir / "diarization.final.json").exists())

    def test_republish_preserves_speaker_labels_from_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _frames, Settings = self._session_with_real_sys(Path(tmp))
            process_meeting(
                session_dir,
                transcribe=lambda path, **kw: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники",
                diarization=Settings(enabled=True, num_speakers=2),
                diarization_runtime_loader=_FakeDiarRuntime,
                speaker_label="Спікер {number}")
            # Симулюємо крах export: транскрипти зникли, леджери й асайнменти є.
            for name in ("transcript.txt", "transcript.md", "transcript.json"):
                (session_dir / name).unlink()

            def must_not_run(path, **kw):
                raise AssertionError("ASR/sherpa не має запускатися при доопублікуванні")

            result = process_meeting(
                session_dir, transcribe=must_not_run,
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники")
            self.assertEqual(result.status, "complete")
            exported = json.loads(
                (session_dir / "transcript.json").read_text(encoding="utf-8"))
            sys_rows = [r for r in exported if r.get("source") == "others"]
            self.assertTrue(any(r.get("speaker_id") for r in sys_rows),
                            "мітки мовців мають пережити доопублікування")

    def test_disabled_diarization_leaves_no_diar_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir, _frames, _Settings = self._session_with_real_sys(Path(tmp))
            result = process_meeting(
                session_dir,
                transcribe=lambda path, **kw: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники")
            self.assertEqual(result.status, "complete")
            meta = load_meta(session_dir)
            self.assertEqual(meta.processing["diarization"]["status"], "disabled")
            self.assertFalse((session_dir / "diarization.final.json").exists())


class WordLedgerContractTests(unittest.TestCase):
    def test_word_and_future_assignment_are_frozen_separate_layers(self):
        word = WordRecord(
            "mic:00000001", "mic", 10, 20, "Привіт", "me",
            {"engine": "faster-whisper"},
        )
        assignment = SpeakerAssignment(
            word_id=word.word_id, speaker_id=None,
            assignment_reason="track_source", candidates=(),
            overlap_suspected=False, confidence_class="deterministic",
        )
        with self.assertRaises(FrozenInstanceError):
            word.text = "змінено"
        with self.assertRaises(FrozenInstanceError):
            assignment.speaker_id = "speaker_01"
        self.assertEqual(word.source, "me")
        self.assertEqual(assignment.word_id, word.word_id)

    def test_existing_ledger_is_idempotent_but_cannot_be_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.mic.jsonl"
            first = WordRecord("mic:00000001", "mic", 0, 10, "раз", "me", {})
            changed = WordRecord("mic:00000001", "mic", 0, 10, "два", "me", {})
            write_word_ledger(path, [first])
            write_word_ledger(path, [first])
            with self.assertRaises(ImmutableLedgerError):
                write_word_ledger(path, [changed])
            self.assertEqual(read_word_ledger(path), [first])


class MeetingPipelineTests(unittest.TestCase):
    def test_multimic_tracks_keep_distinct_speakers_in_ledger_and_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = _make_session(
                Path(tmp), tracks=("mic1", "mic2", "sys"),
                preset="multimic",
            )

            result = process_meeting(
                session_dir,
                transcribe=lambda path, **kwargs: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я",
                others_label="Співрозмовники",
                microphone_label="Мікрофон {number}",
            )

            self.assertEqual(result.status, "complete")
            mic1 = read_word_ledger(session_dir / "words.mic1.jsonl")
            mic2 = read_word_ledger(session_dir / "words.mic2.jsonl")
            system = read_word_ledger(session_dir / "words.sys.jsonl")
            self.assertEqual(mic1[0].source, "mic1")
            self.assertEqual(mic2[0].source, "mic2")
            self.assertEqual(system[0].source, "others")

            exported = json.loads(
                (session_dir / "transcript.json").read_text(encoding="utf-8"))
            by_source = {item["source"]: item for item in exported}
            self.assertEqual(by_source["mic1"]["speaker"], "mic1")
            self.assertEqual(by_source["mic2"]["speaker"], "mic2")
            self.assertEqual(by_source["others"]["speaker"], "others")

            transcript = (session_dir / "transcript.txt").read_text(
                encoding="utf-8")
            self.assertIn("Мікрофон 1: mic1-0001", transcript)
            self.assertIn("Мікрофон 2: mic2-0001", transcript)
            self.assertIn("Співрозмовники: sys-0001", transcript)
            self.assertNotIn("Я:", transcript)

            markdown = (session_dir / "transcript.md").read_text(
                encoding="utf-8")
            self.assertIn("## Мікрофон 1", markdown)
            self.assertIn("## Мікрофон 2", markdown)
            self.assertIn("## Співрозмовники", markdown)

    def test_processes_every_track_block_with_word_timestamps_and_exports_all_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = _make_session(Path(tmp), blocks=2)
            calls = []

            def transcribe(path, *, include_word_timestamps=False):
                calls.append((Path(path).name, include_word_timestamps))
                return _asr_result(Path(path).stem)

            progress = []
            result = process_meeting(
                session_dir,
                transcribe=transcribe,
                asr_provenance={"engine": "faster-whisper", "model": "small"},
                me_label="Я",
                others_label="Співрозмовники",
                progress=progress.append,
            )
            self.assertEqual(result.status, "complete")
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(flag for _name, flag in calls))
            mic_words = read_word_ledger(session_dir / "words.mic.jsonl")
            sys_words = read_word_ledger(session_dir / "words.sys.jsonl")
            self.assertEqual([w.source for w in mic_words], ["me", "me"])
            self.assertEqual([w.source for w in sys_words], ["others", "others"])
            self.assertGreaterEqual(mic_words[1].start_sample, 16000)
            self.assertEqual(len({w.word_id for w in mic_words + sys_words}), 4)
            self.assertEqual(progress[-1]["completed_chunks"], 4)
            self.assertEqual(progress[-1]["total_chunks"], 4)

            transcript = (session_dir / "transcript.txt").read_text(encoding="utf-8")
            self.assertIn("Я:", transcript)
            self.assertIn("Співрозмовники:", transcript)
            self.assertTrue((session_dir / "transcript.md").is_file())
            exported = json.loads(
                (session_dir / "transcript.json").read_text(encoding="utf-8"))
            exported_ids = [
                wid for utterance in exported for wid in utterance["word_ids"]]
            ledger_ids = [w.word_id for w in mic_words + sys_words]
            self.assertEqual(Counter(exported_ids), Counter(ledger_ids))

            meta = load_meta(session_dir)
            self.assertEqual(meta.processing["status"], "complete")
            self.assertEqual(meta.processing["stage"], "complete")
            self.assertEqual(meta.processing["progress"], 1.0)

    def test_noncanonical_session_dir_still_publishes_transcripts(self):
        """Б2: теку сесії передано лексично неканонічним шляхом (як junction чи
        OneDrive-перенаправлення). _safe_audio_path резолвить WAV, тож без
        симетричного .resolve() тут provenance-виклик relative_to кинув би
        ValueError → уся нарада «failed» із нулем слів попри вдалий ASR."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = _make_session(root)          # реальна тека з WAV
            (root / "nested").mkdir()                  # існуючий проміжок для «..»
            # той самий каталог, але не в канонічній формі: .resolve() його
            # змінює, а суто лексичний Path.relative_to без фіксу впав би.
            spooky = root / "nested" / ".." / session_dir.name
            self.assertNotEqual(str(spooky), str(session_dir))

            result = process_meeting(
                spooky,
                transcribe=lambda path, **kwargs: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я",
                others_label="Співрозмовники",
            )

            self.assertEqual(result.status, "complete")
            self.assertGreater(result.word_count, 0)
            mic = read_word_ledger(session_dir / "words.mic.jsonl")
            self.assertTrue(mic)
            # provenance тримає ВІДНОСНИЙ шлях WAV усередині сесії, не абсолютний
            self.assertFalse(
                Path(mic[0].asr_provenance["audio_file"]).is_absolute())

    def test_track_failure_keeps_other_track_and_marks_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = _make_session(Path(tmp))

            def transcribe(path, *, include_word_timestamps=False):
                if Path(path).name.startswith("sys-"):
                    raise RuntimeError("sys ASR failed")
                return _asr_result("мої слова")

            result = process_meeting(
                session_dir,
                transcribe=transcribe,
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я",
                others_label="Співрозмовники",
            )
            self.assertEqual(result.status, "partial")
            self.assertEqual(
                len(read_word_ledger(session_dir / "words.mic.jsonl")), 1)
            self.assertEqual(
                read_word_ledger(session_dir / "words.sys.jsonl"), [])
            self.assertIn(
                "мої слова",
                (session_dir / "transcript.txt").read_text(encoding="utf-8"),
            )
            meta = load_meta(session_dir)
            self.assertEqual(
                meta.processing["tracks"]["mic"]["status"], "complete")
            self.assertEqual(
                meta.processing["tracks"]["sys"]["status"], "error")

    def test_retry_republishes_from_ledgers_without_reasr(self):
        # С5: краш після запису леджерів, але до транскриптів. Ретрай доопубліковує
        # текст з леджерів БЕЗ повторного ASR (інакше immutable-гейт → глухий кут).
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = _make_session(Path(tmp))
            process_meeting(
                session_dir,
                transcribe=lambda path, **kwargs: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники")
            for name in ("transcript.txt", "transcript.md", "transcript.json"):
                (session_dir / name).unlink()

            def must_not_run(path, **kwargs):
                raise AssertionError("ASR не має запускатися при доопублікуванні")

            result = process_meeting(
                session_dir, transcribe=must_not_run,
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники")

            self.assertEqual(result.status, "complete")
            self.assertGreater(result.word_count, 0)
            self.assertTrue((session_dir / "transcript.json").is_file())
            self.assertTrue((session_dir / "transcript.txt").is_file())
            self.assertTrue(load_meta(session_dir).processing.get("republished"))

    def test_text_without_word_timestamps_degrades_not_kills_track(self):
        # С4: блок повернув текст, але порожній список пословних таймкодів — доріжка
        # НЕ падає; слова синтезуються з меж сегмента, хвіст доріжки зберігається.
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = _make_session(Path(tmp), tracks=("mic",), blocks=2)

            def transcribe(path, *, include_word_timestamps=False):
                if Path(path).name.endswith("0002.wav"):
                    return ("тихо гра", "тихо гра", 1.0,
                            [("тихо", 0.5), ("гра", 0.5)],
                            [(0.0, 0.8, "тихо гра")], [])
                return _asr_result("привіт")

            result = process_meeting(
                session_dir, transcribe=transcribe,
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я", others_label="Співрозмовники")

            self.assertEqual(result.status, "complete")
            mic = read_word_ledger(session_dir / "words.mic.jsonl")
            texts = [w.text for w in mic]
            self.assertIn("привіт", texts)
            self.assertIn("тихо", texts)
            self.assertIn("гра", texts)
            synth = [w for w in mic if w.text in ("тихо", "гра")]
            self.assertTrue(synth)
            self.assertTrue(all(
                w.asr_provenance.get("word_timestamps")
                == "synthesized_from_segments" for w in synth))

    def test_cancel_between_blocks_preserves_last_valid_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = _make_session(
                Path(tmp), tracks=("mic",), blocks=2)
            old = session_dir / "transcript.txt"
            old.write_text("попередній валідний текст", encoding="utf-8")
            token = CancelToken()

            def on_progress(state):
                if state["completed_chunks"] == 1:
                    token.cancel()

            result = process_meeting(
                session_dir,
                transcribe=lambda path, **kwargs: _asr_result(Path(path).stem),
                asr_provenance={"engine": "faster-whisper"},
                me_label="Я",
                others_label="Співрозмовники",
                cancel=token,
                progress=on_progress,
            )
            self.assertEqual(result.status, "cancelled")
            self.assertEqual(
                old.read_text(encoding="utf-8"),
                "попередній валідний текст",
            )
            self.assertFalse((session_dir / "words.mic.jsonl").exists())
            self.assertEqual(
                load_meta(session_dir).processing["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
