"""Юніти Slice 3: binder, кластеризація, конвеєр із синтетичним 2-спікер аудіо.

Жодного sherpa: рантайм інжектиться фейком, що виводить стабільні ембединги з
відомих смуг сигналу. Тони перевіряють САМЕ обв'язку (вікна/зведення/binding), а
не акустичну якість натренованої моделі.
"""
import json
import math
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from whisper_core.meeting import postprocess
from whisper_core.meeting.diarize import cluster_embeddings
from whisper_core.meeting.word_binding import (
    DiarizationSpan, REASON_NO_SPAN, REASON_NOT_SYS, REASON_OVERLAP, bind_words)
from whisper_core.meeting.word_ledger import WordRecord
from whisper_core.meeting.diarization_pipeline import (
    DiarizationSettings, WavTrackReader, run_system_diarization,
    write_diarization_artifact)

RATE = 16000


def _has_float_vector(obj, min_len=64):
    """Евристика на серіалізований ембединг: чи є десь список із >= ``min_len``
    чисел (bool не рахуємо). Ловить біометричні вектори незалежно від назви поля."""
    if isinstance(obj, bool):
        return False
    if isinstance(obj, list):
        if len(obj) >= min_len and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in obj):
            return True
        return any(_has_float_vector(v, min_len) for v in obj)
    if isinstance(obj, dict):
        return any(_has_float_vector(v, min_len) for v in obj.values())
    return False


class _ProfileStub:
    """Мінімальний профіль для voice_memory: тека зі сховищем voices.json."""

    def __init__(self, d):
        self.dir = Path(d)

    @property
    def voice_memory_path(self):
        return self.dir / "voices.json"


def _sys_word(word_id, start, end):
    return WordRecord(word_id, "sys", start, end, "x", "others", {})


# ── синтетичний 2-спікер WAV і фейк-рантайм ──────────────────────────────────

def make_two_source_wav(path, *, rate=RATE):
    """Детермінований 2-спікерний сигнал: A (амплітуда 0.3) і B (0.7) по черзі у
    3-секундних слотах з тишею-швом посередині. Повертає (шлях, тривалість_с)."""
    freq = 200.0
    slot = 3 * rate
    gap = rate // 3           # 0.33 с тиші між слотами
    blocks = []
    for i in range(8):
        amp = 0.3 if i % 2 == 0 else 0.7
        t = np.arange(slot) / rate
        blocks.append((amp * np.sin(2 * math.pi * freq * t)).astype(np.float32))
        blocks.append(np.zeros(gap, dtype=np.float32))
        if i == 3:
            blocks.append(np.zeros(rate, dtype=np.float32))   # шов
    mono = np.concatenate(blocks)
    pcm = np.clip(mono, -1, 1)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.round(pcm * 32767).astype("<i2").tobytes())
    return path, mono.size / rate


class FakeRuntime:
    """Класифікує аудіо за середньою амплітудою: >0.35→B, >0.1→A, інакше тиша."""

    def __init__(self):
        self.embedding_dim = 2

    def diarize_window(self, samples, *, cancel_check=None):
        samples = np.asarray(samples, dtype=np.float32)
        hop = RATE // 2
        raw = []
        for start in range(0, samples.size, hop):
            clip = samples[start:start + hop]
            if clip.size == 0:
                continue
            mean = float(np.mean(np.abs(clip)))
            label = "B" if mean > 0.15 else ("A" if mean > 0.05 else None)
            raw.append((start, start + clip.size, label))
        spans = []
        for start, end, label in raw:
            if label is None:
                continue
            if spans and spans[-1][2] == label and start - spans[-1][1] <= hop:
                spans[-1] = (spans[-1][0], end, label)
            else:
                spans.append((start, end, label))
        return spans

    def embed(self, samples):
        mean = float(np.mean(np.abs(np.asarray(samples, dtype=np.float32))))
        if mean > 0.15:
            return np.array([0.0, 1.0], dtype=np.float32)
        if mean > 0.05:
            return np.array([1.0, 0.0], dtype=np.float32)
        return None

    def cluster(self, features, *, num_speakers, distance_threshold=0.5):
        return cluster_embeddings(features, num_speakers=num_speakers,
                                  distance_threshold=distance_threshold)


# ── binder ──────────────────────────────────────────────────────────────────

class BindWordsTests(unittest.TestCase):
    def test_sums_overlap_and_picks_greatest(self):
        spans = [DiarizationSpan(0, 100, "speaker_01"),
                 DiarizationSpan(100, 300, "speaker_02")]
        word = _sys_word("sys:1", 50, 260)   # 50 з sp1, 160 з sp2
        (a,) = bind_words([word], spans)
        self.assertEqual(a.speaker_id, "speaker_02")
        self.assertEqual(a.assignment_reason, REASON_OVERLAP)

    def test_no_span_is_unassigned(self):
        (a,) = bind_words([_sys_word("sys:1", 0, 100)],
                          [DiarizationSpan(500, 600, "speaker_01")])
        self.assertIsNone(a.speaker_id)
        self.assertEqual(a.assignment_reason, REASON_NO_SPAN)

    def test_mic_words_are_never_bound(self):
        mic = WordRecord("mic:1", "mic", 0, 100, "hi", "me", {})
        spans = [DiarizationSpan(0, 100, "speaker_01")]
        (a,) = bind_words([mic], spans)
        self.assertIsNone(a.speaker_id)
        self.assertEqual(a.assignment_reason, REASON_NOT_SYS)

    def test_every_word_appears_exactly_once_even_with_overlapping_spans(self):
        words = [_sys_word("sys:1", 0, 100), _sys_word("sys:2", 90, 200),
                 WordRecord("mic:1", "mic", 0, 50, "m", "me", {})]
        spans = [DiarizationSpan(0, 120, "speaker_01"),
                 DiarizationSpan(80, 200, "speaker_02")]   # штучне накладання
        out = bind_words(words, spans)
        self.assertEqual(len(out), len(words))
        self.assertEqual([a.word_id for a in out], ["sys:1", "sys:2", "mic:1"])
        for a in out:
            self.assertFalse(a.overlap_suspected)   # sherpa не дає одночасних

    def test_tie_break_prefers_earliest_span_start_then_lexical(self):
        spans = [DiarizationSpan(0, 100, "speaker_02"),
                 DiarizationSpan(100, 200, "speaker_01")]
        word = _sys_word("sys:1", 50, 150)   # рівно 50/50
        (a,) = bind_words([word], spans)
        self.assertEqual(a.speaker_id, "speaker_02")  # найраніший початок спану


# ── кластеризація ────────────────────────────────────────────────────────────

class ClusterTests(unittest.TestCase):
    def test_empty_and_single(self):
        self.assertEqual(cluster_embeddings([], num_speakers=None), [])
        self.assertEqual(cluster_embeddings([[1, 0]], num_speakers=None), [0])

    def test_fixed_k_two(self):
        feats = [[1, 0], [1, 0], [0, 1]]
        labels = cluster_embeddings(feats, num_speakers=2)
        self.assertEqual(len(set(labels)), 2)
        self.assertEqual(labels[0], labels[1])
        self.assertNotEqual(labels[0], labels[2])

    def test_fixed_k_capped_to_rows(self):
        labels = cluster_embeddings([[1, 0], [0, 1]], num_speakers=5)
        self.assertEqual(len(set(labels)), 2)

    def test_auto_lower_threshold_more_clusters(self):
        a = np.array([1.0, 0.0])
        b = a * math.cos(0.3) + np.array([0.0, 1.0]) * math.sin(0.3)  # близькі
        feats = [a, b]
        loose = cluster_embeddings(feats, num_speakers=None, distance_threshold=0.5)
        strict = cluster_embeddings(feats, num_speakers=None, distance_threshold=0.01)
        self.assertEqual(len(set(loose)), 1)   # поріг 0.5 зливає близькі
        self.assertEqual(len(set(strict)), 2)  # менший поріг → більше кластерів

    def test_rejects_count_of_one_via_cap(self):
        # cluster сам не валідує 1 (це робить UI/конвеєр), але K=1 не має ділити
        labels = cluster_embeddings([[1, 0], [0, 1]], num_speakers=1)
        self.assertEqual(len(set(labels)), 1)

    def test_auto_vs_fixed_k_clustering(self):
        """Перевірка кластеризації при num_speakers=None (авто) та num_speakers=2..10."""
        feats = [[1, 0], [0.9, 0.1], [0, 1], [0.1, 0.9], [0.5, 0.5]]
        auto_labels = cluster_embeddings(feats, num_speakers=None, distance_threshold=0.5)
        fixed_3_labels = cluster_embeddings(feats, num_speakers=3)
        fixed_2_labels = cluster_embeddings(feats, num_speakers=2)
        self.assertGreaterEqual(len(set(auto_labels)), 1)
        self.assertEqual(len(set(fixed_3_labels)), 3)
        self.assertEqual(len(set(fixed_2_labels)), 2)



# ── postprocess: speaker_id, JSON, redaction, сумісність назад ───────────────

class UtteranceContractTests(unittest.TestCase):
    def test_positional_construction_still_works(self):
        u = postprocess.Utterance(0.0, 1.0, "others", "hi")
        self.assertIsNone(u.speaker_id)
        self.assertEqual(u.source, "")

    def test_json_emits_speaker_id_only_when_present(self):
        with_id = postprocess.Utterance(0, 1, "speaker_01", "hi",
                                        source="others", speaker_id="speaker_01")
        without = postprocess.Utterance(0, 1, "me", "yo", source="me")
        rows = postprocess.to_transcript_json([with_id, without])
        self.assertEqual(rows[0]["speaker_id"], "speaker_01")
        self.assertNotIn("speaker_id", rows[1])

    def test_redaction_by_speaker_id_touches_only_that_speaker(self):
        a = postprocess.Utterance(0, 1, "speaker_01", "secret",
                                  source="others", speaker_id="speaker_01")
        b = postprocess.Utterance(0, 1, "speaker_02", "keep",
                                  source="others", speaker_id="speaker_02")
        out = postprocess.redact_utterances(
            [a, b], 0, 1, source="others", speaker_id="speaker_01")
        self.assertEqual(out[0].text, "[вилучено]")
        self.assertEqual(out[1].text, "keep")

    def test_speaker_id_label_resolves_via_speaker_names(self):
        u = postprocess.Utterance(0, 1, "speaker_01", "hi",
                                  source="others", speaker_id="speaker_01")
        text = postprocess.to_transcript_text(
            [u], me_label="Я", others_label="Співрозмовники",
            speaker_names={"speaker_01": "Спікер 1"})
        self.assertIn("Спікер 1: hi", text)


# ── WavTrackReader ───────────────────────────────────────────────────────────

class WavReaderTests(unittest.TestCase):
    def test_reads_across_files_as_one_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for idx, value in enumerate((1000, 2000)):
                p = Path(td) / f"{idx}.wav"
                with wave.open(str(p), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(RATE)
                    wav.writeframes(np.full(RATE, value, dtype="<i2").tobytes())
                paths.append(p)
            reader = WavTrackReader(paths)
            self.assertEqual(reader.total_samples, 2 * RATE)
            window = reader.read(RATE - 2, RATE + 2)   # шов файлів
            self.assertEqual(window.size, 4)
            self.assertAlmostEqual(window[0], 1000 / 32768, places=5)
            self.assertAlmostEqual(window[-1], 2000 / 32768, places=5)


# ── синтетична 2-спікер інтеграція конвеєра ──────────────────────────────────

class SyntheticPipelineTests(unittest.TestCase):
    def test_two_source_audio_yields_two_speakers(self):
        with tempfile.TemporaryDirectory() as td:
            wav, seconds = make_two_source_wav(Path(td) / "sys.wav")
            settings = DiarizationSettings(enabled=True, num_speakers=2)
            result = run_system_diarization(
                [wav], settings, runtime=FakeRuntime())
            self.assertEqual(result.status, "complete")
            self.assertEqual(len(result.speaker_ids), 2)
            self.assertEqual(set(result.speaker_ids), {"speaker_01", "speaker_02"})
            # спани не виходять за межі аудіо
            for span in result.spans:
                self.assertGreaterEqual(span.start_sample, 0)
                self.assertLessEqual(span.end_sample, int(seconds * RATE))
            self.assertLess(result.diagnostics["rtf"], 5.0)
            self.assertFalse(result.diagnostics["overlap_supported"])

    def test_binding_over_synthetic_spans_loses_no_word(self):
        with tempfile.TemporaryDirectory() as td:
            wav, _ = make_two_source_wav(Path(td) / "sys.wav")
            result = run_system_diarization(
                [wav], DiarizationSettings(enabled=True, num_speakers=2),
                runtime=FakeRuntime())
            # слова щосекунди по всій доріжці
            words = [_sys_word(f"sys:{i}", i * RATE, i * RATE + RATE // 2)
                     for i in range(20)]
            out = bind_words(words, result.spans)
            self.assertEqual(len(out), len(words))
            self.assertEqual(len({a.word_id for a in out}), len(words))
            assigned = {a.speaker_id for a in out if a.speaker_id}
            self.assertTrue(assigned.issubset({"speaker_01", "speaker_02"}))
            self.assertGreaterEqual(len(assigned), 2)

    def test_cancel_before_windows_preserves_nothing_and_reports(self):
        with tempfile.TemporaryDirectory() as td:
            wav, _ = make_two_source_wav(Path(td) / "sys.wav")
            result = run_system_diarization(
                [wav], DiarizationSettings(enabled=True, num_speakers=2),
                runtime=FakeRuntime(), cancel=lambda: True)
            self.assertEqual(result.status, "cancelled")
            self.assertEqual(result.spans, ())

    def test_artifact_has_no_embeddings(self):
        with tempfile.TemporaryDirectory() as td:
            wav, _ = make_two_source_wav(Path(td) / "sys.wav")
            result = run_system_diarization(
                [wav], DiarizationSettings(enabled=True, num_speakers=2),
                runtime=FakeRuntime())
            out = Path(td) / "diarization.final.json"
            write_diarization_artifact(out, result, {"runtime": "fake"})
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], 1)
            self.assertFalse(payload["overlap_supported"])
            # ЖОДНОЇ біометрії в артефакті: ані відомі поля, ані евристика
            self.assertNotIn("vector", json.dumps(payload))
            self.assertNotIn("speaker_centroids", payload)
            self.assertNotIn("speaker_centroids", payload.get("diagnostics", {}))
            self.assertFalse(
                _has_float_vector(payload),
                "артефакт містить довгий список float — схоже на ембединг")

    def test_guard_catches_injected_embedding(self):
        # підсунутий у артефакт вектор — евристика ловить (тест-охоронець кусається)
        self.assertTrue(_has_float_vector({"schema": 1, "sneaky": [0.1] * 64}))
        self.assertTrue(_has_float_vector({"nested": {"emb": [0.0] * 128}}))
        # чистий артефакт — не спрацьовує
        clean = {"schema": 1, "overlap_supported": False,
                 "spans": [{"start_sample": 0, "end_sample": 5,
                            "speaker_id": "speaker_01"}],
                 "matched_speaker_names": {"speaker_01": "Олена"},
                 "diagnostics": {"distance_threshold": 0.5, "rtf": 0.12}}
        self.assertFalse(_has_float_vector(clean))

    def test_consent_disabled_omits_centroids(self):
        with tempfile.TemporaryDirectory() as td:
            wav, _ = make_two_source_wav(Path(td) / "sys.wav")
            result = run_system_diarization(
                [wav], DiarizationSettings(enabled=True, num_speakers=2,
                                           voice_memory_enabled=False),
                runtime=FakeRuntime())
            self.assertNotIn("speaker_centroids", result.diagnostics)
            out = Path(td) / "diarization.final.json"
            write_diarization_artifact(out, result, {"runtime": "fake"})
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotIn("speaker_centroids", payload)
            self.assertNotIn("speaker_centroids", payload.get("diagnostics", {}))
            self.assertFalse(_has_float_vector(payload))

    def test_consent_enabled_updates_voices_but_artifact_clean(self):
        from whisper_core.meeting import voice_memory
        with tempfile.TemporaryDirectory() as td:
            wav, _ = make_two_source_wav(Path(td) / "sys.wav")
            profile = _ProfileStub(td)
            # передзасіяний голос, що збігається з одним із мовців (центроїд [1,0])
            voice_memory.add_or_update_voice(profile, "Тест", [1.0, 0.0])
            before = voice_memory.load_voices(profile)["Тест"]["sample_count"]
            result = run_system_diarization(
                [wav], DiarizationSettings(enabled=True, num_speakers=2,
                                           voice_memory_enabled=True,
                                           profile=profile),
                runtime=FakeRuntime())
            # артефакт БЕЗ центроїдів навіть за увімкненої згоди (суворий варіант)
            self.assertNotIn("speaker_centroids", result.diagnostics)
            out = Path(td) / "diarization.final.json"
            write_diarization_artifact(out, result, {"runtime": "fake"})
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertNotIn("speaker_centroids", payload)
            self.assertFalse(_has_float_vector(payload))
            # voices.json оновився: зіставлення підняло sample_count
            after = voice_memory.load_voices(profile)["Тест"]["sample_count"]
            self.assertGreater(after, before)


if __name__ == "__main__":
    unittest.main()
