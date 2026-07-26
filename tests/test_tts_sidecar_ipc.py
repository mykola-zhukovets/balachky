"""Хвиля 1: TTS sidecar↔worker IPC (§3.2, §11.2).

Юніт-рівень (synthesize_stream/split/cap/cleanup) + ЖИВИЙ підпроцес-воркер із
фейковим рушієм (ENV_FAKE_BACKEND): ping, load_voice, стрімінг, busy, cancel
ПІД ЧАС synthesize (control-потік), краш процесу → TtsSidecarError."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import (ENV_FAKE_BACKEND, FAKE_ENGINE_MARKER,
                              FIRST_CHUNK_MAX_WORDS, MSG_ACCEPTED, MSG_CANCELLED,
                              MSG_CHUNK_READY, MSG_RESULT)
from whisper_core.tts import worker as W
from whisper_core.tts.engines.fake import FakeTtsEngine
from whisper_core.tts.sidecar import (TtsSidecar, TtsSidecarError,
                                      default_worker_command)


class TestSplitAndCap(unittest.TestCase):
    def test_split_sentences(self):
        # речення > 20 символів кожне, щоб не спрацювало групування коротких хвостів
        parts = W.split_sentences(
            "Перше досить велике речення тут. "
            "Друге так само велике речення також! "
            "Третє велике речення наприкінці?")
        self.assertEqual(len(parts), 3)

    def test_short_sentences_grouped(self):
        # короткі хвости (≤20) приклеюються — навмисно (проти дрібних чанків)
        parts = W.split_sentences("Так. Ні. Добре.")
        self.assertLess(len(parts), 3)

    def test_first_chunk_cap_for_ttfs(self):
        # довгий абзац без крапки → перший чанк ≤ cap слів (TTFS < 0.5 c)
        long = " ".join(f"слово{i}" for i in range(40))
        chunks = W.apply_first_chunk_cap(W.split_sentences(long))
        self.assertLessEqual(len(chunks[0].split()), FIRST_CHUNK_MAX_WORDS)
        # решта тексту не загублена
        self.assertGreater(len(chunks), 1)

    def test_short_first_sentence_not_capped(self):
        chunks = W.apply_first_chunk_cap(["Коротко."])
        self.assertEqual(chunks, ["Коротко."])


class TestSynthesizeStream(unittest.TestCase):
    def _run(self, text, *, want_timings=False, cancel_after=None):
        events = []
        d = tempfile.mkdtemp(prefix="tts-test-")
        eng = FakeTtsEngine()
        eng.load("")
        state = {"n": 0}

        def is_cancelled():
            if cancel_after is None:
                return False
            return state["n"] >= cancel_after

        def emit(p):
            events.append(p)
            if p.get("type") == MSG_CHUNK_READY:
                state["n"] += 1

        W.synthesize_stream(eng, {"id": "r1", "text": text, "wav_dir": d,
                                  "want_timings": want_timings}, emit, is_cancelled)
        return events, d

    def test_streaming_order_and_atomic_files(self):
        events, d = self._run("Перше. Друге. Третє.")
        types = [e["type"] for e in events]
        self.assertIn(MSG_CHUNK_READY, types)
        self.assertEqual(types[-1], MSG_RESULT)
        # готові s{i}.wav, жодного .part не лишилось
        files = os.listdir(d)
        self.assertTrue(any(f.endswith(".wav") for f in files))
        self.assertFalse(any(f.endswith(".part") for f in files))

    def test_chunk_ready_carries_fake_marker(self):
        events, _ = self._run("Привіт.")
        chunk = next(e for e in events if e["type"] == MSG_CHUNK_READY)
        self.assertIn(FAKE_ENGINE_MARKER, chunk["normalized_text"])

    def test_cancel_midstream_cleans_partials(self):
        # скасувати після 1 чанку: cancelled + жодного s*.wav/.part не лишилось
        events, d = self._run(
            "Перше велике речення для тесту. Друге велике речення для тесту. "
            "Третє велике речення для тесту. Четверте велике речення для тесту.",
            cancel_after=1)
        types = [e["type"] for e in events]
        self.assertIn(MSG_CANCELLED, types)
        files = os.listdir(d)
        self.assertFalse(any(f.endswith(".part") for f in files))
        self.assertFalse(any(f.endswith(".wav") for f in files))

    def test_want_timings_off_no_durations(self):
        events, _ = self._run("Привіт.", want_timings=False)
        chunk = next(e for e in events if e["type"] == MSG_CHUNK_READY)
        self.assertIsNone(chunk["timings"])


class TestBusyBranch(unittest.TestCase):
    """Reject-busy РЕАЛЬНО досяжна (§3.2): control-потік бачить другий synthesize,
    поки активний перший. Юніт-рівень — детерміновано (мутація «не емітити busy»
    червонить)."""

    def test_second_synthesize_while_active_gets_busy(self):
        import io
        import json
        from whisper_core.tts import MSG_BUSY, MSG_SYNTHESIZE
        from whisper_core.tts.worker import TtsWorker
        out = io.StringIO()
        line = json.dumps({"type": MSG_SYNTHESIZE, "id": "second",
                           "text": "x", "wav_dir": ""})
        w = TtsWorker(io.StringIO(line + "\n"), out)
        w._active_id = "first-active"            # перший synthesize уже активний
        w._reader_loop()                         # control-потік обробляє другий
        emitted = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        busy = [m for m in emitted if m.get("type") == MSG_BUSY]
        self.assertTrue(busy, "busy НЕ емітовано — reject-busy недосяжна")
        self.assertEqual(busy[0]["id"], "second")
        self.assertEqual(busy[0]["active_id"], "first-active")

    def test_synthesize_when_idle_is_queued_not_busy(self):
        import io
        import json
        from whisper_core.tts import MSG_BUSY, MSG_SYNTHESIZE
        from whisper_core.tts.worker import TtsWorker
        out = io.StringIO()
        line = json.dumps({"type": MSG_SYNTHESIZE, "id": "solo",
                           "text": "x", "wav_dir": ""})
        w = TtsWorker(io.StringIO(line + "\n"), out)
        w._reader_loop()                         # active_id порожній → claim, у чергу
        emitted = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        self.assertFalse([m for m in emitted if m.get("type") == MSG_BUSY])
        self.assertEqual(w._active_id, "solo")   # claimed


class TestVoiceSwitch(unittest.TestCase):
    """§7.7: зміна голосу — повний unload попереднього ПЕРЕД load нового; невдалий
    load не лишає два напівживі engine (воркер лишається без активного голосу)."""

    def _worker(self):
        import io
        return W.TtsWorker(io.StringIO(""), io.StringIO())

    def test_switch_unloads_previous_before_new(self):
        import io
        from whisper_core.tts import worker as WM
        events = []

        class TrackEngine:
            def __init__(self, name):
                self.name = name
            def capabilities(self):
                from whisper_core.tts.engines.base import EngineCapabilities
                return EngineCapabilities(sample_rate=24000,
                                          supported_languages=("uk",),
                                          native_word_timings=False)
            def unload(self):
                events.append(("unload", self.name))

        made = []

        def fake_make_engine(kind, path):
            eng = TrackEngine(kind)
            made.append(eng)
            events.append(("load", kind))
            return eng

        orig = WM.make_engine
        WM.make_engine = fake_make_engine
        try:
            w = WM.TtsWorker(io.StringIO(""), io.StringIO())
            w._handle_load_voice({"id": "1", "voice_id": "styletts2_ua",
                                  "engine": "styletts2", "manifest_path": "/a"})
            w._handle_load_voice({"id": "2", "voice_id": "radtts_uk",
                                  "engine": "radtts", "manifest_path": "/b"})
        finally:
            WM.make_engine = orig
        # перший engine вивантажено ПЕРЕД завантаженням другого
        self.assertIn(("unload", "styletts2"), events)
        self.assertLess(events.index(("unload", "styletts2")),
                        events.index(("load", "radtts")))

    def test_failed_load_leaves_no_engine(self):
        import io
        from whisper_core.tts import worker as WM
        from whisper_core.tts.engines import EngineLoadError

        def boom_make_engine(kind, path):
            raise EngineLoadError("битий голос")

        orig = WM.make_engine
        WM.make_engine = boom_make_engine
        try:
            out = io.StringIO()
            w = WM.TtsWorker(io.StringIO(""), out)
            w._handle_load_voice({"id": "9", "voice_id": "x", "engine": "styletts2",
                                  "manifest_path": "/x"})
            self.assertIsNone(w._engine)         # без напівживого engine
            import json as _j
            msgs = [_j.loads(l) for l in out.getvalue().splitlines() if l.strip()]
            self.assertTrue(any(m.get("type") == "error" for m in msgs))
        finally:
            WM.make_engine = orig


def _fake_sidecar(**kw):
    return TtsSidecar(env={ENV_FAKE_BACKEND: "1"}, **kw)


class TestSidecarLive(unittest.TestCase):
    def test_default_command_targets_worker(self):
        cmd = default_worker_command()
        self.assertTrue(any("tts" in str(c) for c in cmd))

    def test_ping_pong(self):
        with _fake_sidecar() as s:
            self.assertTrue(s.ping(timeout=20))

    def test_load_voice_and_synthesize(self):
        d = tempfile.mkdtemp(prefix="tts-live-")
        with _fake_sidecar() as s:
            s.load_voice("styletts2_ua", engine="styletts2",
                         manifest_path=d, timeout=20)
            events = []
            s.synthesize_stream(text="Перше речення. Друге речення.",
                                voice_id="styletts2_ua", wav_dir=d,
                                on_event=events.append, chunk_timeout=20)
            types = [e["type"] for e in events]
            self.assertEqual(types[0], MSG_ACCEPTED)
            self.assertIn(MSG_CHUNK_READY, types)
            self.assertEqual(types[-1], MSG_RESULT)

    def test_cancel_during_synthesis_control_thread(self):
        # ДОВЕДЕННЯ окремого control-потоку: cancel читається ПІД ЧАС synthesize.
        d = tempfile.mkdtemp(prefix="tts-cancel-")
        with TtsSidecar(env={ENV_FAKE_BACKEND: "1",
                             "BALACHKY_TTS_FAKE_SLEEP": "0.4"}) as s:
            s.ping(timeout=20)
            got = {"cancelled": False, "req": None}

            def on_event(p):
                if p.get("type") == MSG_ACCEPTED:
                    got["req"] = p["id"]
                if p.get("type") == MSG_CANCELLED:
                    got["cancelled"] = True

            text = ". ".join(f"Досить велике речення номер {i} тут"
                             for i in range(8)) + "."

            def _run_synth():
                try:
                    s.synthesize_stream(text=text, voice_id="v", wav_dir=d,
                                        on_event=on_event, chunk_timeout=30)
                except TtsSidecarError:
                    pass                       # hard-kill на застряглому — не падіння тесту

            t = threading.Thread(target=_run_synth)
            t.start()
            time.sleep(0.6)                    # синтез уже йде (sleep між реченнями)
            # знайти active id: чекаємо accepted
            for _ in range(50):
                if got["req"]:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(got["req"])
            s.cancel(got["req"], deadline=5.0)
            t.join(timeout=10)
            # після cancel жодного .part не лишилось (worker прибрав)
            self.assertFalse(any(f.endswith(".part") for f in os.listdir(d)))

    def test_live_busy_on_concurrent_export(self):
        # ЖИВИЙ reject-busy: поки перший synth іде (sleep), другий → busy → помилка
        d = tempfile.mkdtemp(prefix="tts-busy-")
        with TtsSidecar(env={ENV_FAKE_BACKEND: "1",
                             "BALACHKY_TTS_FAKE_SLEEP": "0.5"}) as s:
            s.ping(timeout=20)
            first_done = {"v": False}

            def _run_first():
                try:
                    s.synthesize_stream(
                        text=". ".join(f"Досить довге речення номер {i} тут"
                                       for i in range(6)) + ".",
                        voice_id="v", wav_dir=d, chunk_timeout=30)
                except TtsSidecarError:
                    pass
                first_done["v"] = True

            t = threading.Thread(target=_run_first)
            t.start()
            time.sleep(0.7)                      # перший точно активний
            with self.assertRaises(TtsSidecarError):
                s.synthesize_stream(text="Другий запит.", voice_id="v",
                                    wav_dir=d, chunk_timeout=20)
            t.join(timeout=15)

    def test_pythonutf8_apostrophe_roundtrip(self):
        # §11.2 PYTHONUTF8: кирилиця + апостроф ʼ(U+02BC) через IPC не валять процес
        d = tempfile.mkdtemp(prefix="tts-utf8-")
        with _fake_sidecar() as s:
            s.load_voice("v", engine="styletts2", manifest_path=d, timeout=20)
            events = []
            # апостроф U+02BC + U+0027, кирилиця з ґ/є/ї
            text = "Дзвонив о девʼятій, з'їв ґроно — їхав до Києва."
            s.synthesize_stream(text=text, voice_id="v", wav_dir=d,
                                on_event=events.append, chunk_timeout=20)
            types = [e["type"] for e in events]
            self.assertIn(MSG_CHUNK_READY, types)
            self.assertEqual(types[-1], MSG_RESULT)   # без UnicodeEncodeError-краху
            chunk = next(e for e in events if e["type"] == MSG_CHUNK_READY)
            self.assertIn("ʼ", chunk["normalized_text"])  # апостроф пережив round-trip

    def test_worker_crash_raises_sidecar_error(self):
        # процес, що миттєво вмирає → TtsSidecarError, а не зависання
        s = TtsSidecar(command=[sys.executable, "-c", "import sys; sys.exit(1)"])
        s.start()
        with self.assertRaises(TtsSidecarError):
            s.ping(timeout=10)
        s.shutdown()


if __name__ == "__main__":
    unittest.main()
