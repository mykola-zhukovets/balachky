"""Хвиля 1: оркестрація TtsController — latest-wins (playback) / reject-busy (export),
resolve-вихід, STT-зайнятий, збереження. Без Qt (плеєр не інжектимо — лише колбеки)."""
import os
import sys
import tempfile
import threading
import time
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core.tts import (MSG_ACCEPTED, MSG_CHUNK_READY, MSG_RESULT, voices)
from whisper_core.tts.sidecar import TtsSidecarError
from fronts.desktop.tts_controller import TtsController


class FakeSidecar:
    def __init__(self):
        self.loaded = []
        self.cancelled = []
        self.synth_calls = 0

    def load_voice(self, vid, *, engine, manifest_path, **k):
        self.loaded.append(vid)

    def synthesize_stream(self, *, text, voice_id, wav_dir, on_event=None, **k):
        self.synth_calls += 1
        rid = f"req{self.synth_calls}"
        if on_event:
            on_event({"type": MSG_ACCEPTED, "id": rid})
        p = os.path.join(wav_dir, "s0.wav")
        with open(p, "wb") as f:
            f.write(b"\x00" * 8)
        if on_event:
            on_event({"type": MSG_CHUNK_READY, "wav_path": p})
            on_event({"type": MSG_RESULT})
        return rid

    def cancel(self, rid, **k):
        self.cancelled.append(rid)
        return True

    def shutdown(self):
        pass


_TEMP_REGISTRY = []


class FakeTemp:
    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="ctl-")
        self.cleaned = False
        _TEMP_REGISTRY.append(self)

    def cleanup(self):
        self.cleaned = True
        import shutil
        shutil.rmtree(self.path, ignore_errors=True)


def _live_temps():
    return [t for t in _TEMP_REGISTRY if not t.cleaned]


def _rv(available=True, langs=("uk",)):
    return SimpleNamespace(id="styletts2_ua", engine_kind="styletts2",
                           manifest_path=tempfile.mkdtemp(prefix="voice-"),
                           languages=langs, available=lambda: available,
                           integrity_available=lambda: available)


def _combine(wavs, out):
    with open(out, "wb") as f:
        f.write(b"\x00" * 16)
    return out


class _Coord:
    def __init__(self, allow=True):
        self.allow = allow

    def acquire_tts(self):
        return object() if self.allow else None


def _make_controller(*, enabled=True, resolve=None, coord=None, toast=None):
    cfg = SimpleNamespace(tts_enabled=enabled, tts_voice_uk="styletts2_ua",
                          tts_voice_en="kokoro_en", ui_language="uk")
    sc = FakeSidecar()
    events = {"played": [], "exported": [], "chunks": []}
    ctrl = TtsController(
        cfg=cfg, coordinator=coord or _Coord(),
        resolve_voice=resolve or (lambda vid, lang: _rv()),
        sidecar_factory=lambda: sc, temp_factory=FakeTemp, combine=_combine,
        on_playable=events["played"].append,
        on_chunk_playable=lambda tok, p, t, first: events["chunks"].append((p, first)),
        on_export_done=events["exported"].append,
        toast=toast or (lambda k: None))
    return ctrl, sc, events


def _wait(ctrl, timeout=5):
    w = ctrl._worker
    if w is not None:
        w.join(timeout)


class TestGuards(unittest.TestCase):
    def test_integrity_check_runs_only_in_worker_thread(self):
        caller_thread = threading.get_ident()
        integrity_threads = []
        integrity_checked = threading.Event()

        def check_integrity():
            integrity_threads.append(threading.get_ident())
            integrity_checked.set()
            return True

        rv = SimpleNamespace(
            id="styletts2_ua", engine_kind="styletts2",
            manifest_path="manifest.json",
            languages=("uk",), available=lambda: True,
            integrity_available=check_integrity)
        ctrl, _sc, _events = _make_controller(
            resolve=lambda vid, lang: rv)

        self.assertEqual(
            ctrl.play_text("Привіт світ українською тут"), "playing")
        self.assertNotIn(caller_thread, integrity_threads)
        self.assertTrue(integrity_checked.wait(2), "worker не перевірив голос")
        _wait(ctrl)
        self.assertTrue(integrity_threads)
        self.assertNotIn(caller_thread, integrity_threads)

    def test_disabled_blocks(self):
        toasts = []
        ctrl, _sc, _ = _make_controller(enabled=False, toast=toasts.append)
        self.assertEqual(ctrl.play_text("Привіт світ українською"), "disabled")
        self.assertIn("tts_disabled_note", toasts)

    def test_empty_text(self):
        ctrl, _sc, _ = _make_controller()
        self.assertEqual(ctrl.play_text("   "), "empty")

    def test_no_voice(self):
        toasts = []
        ctrl, _sc, _ = _make_controller(
            resolve=lambda vid, lang: _rv(available=False), toast=toasts.append)
        self.assertEqual(ctrl.play_text("Привіт світ українською тут"), "no_voice")
        self.assertIn("tts_no_voice_hint", toasts)

    def test_pre_use_gate_rejects_incomplete_and_corrupt_voice_directory(self):
        root = Path(tempfile.mkdtemp(prefix="controller-integrity-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        good_a, good_b = b"trusted-a", b"trusted-b"
        preset = voices.VoicePreset(
            id="styletts2_ua", engine_kind="styletts2", languages=("uk",),
            files=(
                ("https://example.invalid/a", "a.bin", len(good_a),
                 sha256(good_a).hexdigest()),
                ("https://example.invalid/b", "b.bin", len(good_b),
                 sha256(good_b).hexdigest()),
            ),
            approx_size_bytes=len(good_a) + len(good_b),
            label_key="k", hint_key="k")

        def populate(case, voice_dir):
            if case == "empty":
                return
            (voice_dir / "READY").write_text("ok", encoding="utf-8")
            (voice_dir / "a.bin").write_bytes(good_a)
            if case == "corrupt_sha":
                (voice_dir / "b.bin").write_bytes(b"x" * len(good_b))

        with mock.patch.dict(
                voices.VOICE_PRESETS, {preset.id: preset}, clear=False):
            for case in ("empty", "missing_file", "corrupt_sha"):
                with self.subTest(case=case):
                    voice_dir = root / preset.id
                    __import__("shutil").rmtree(voice_dir, ignore_errors=True)
                    voice_dir.mkdir()
                    populate(case, voice_dir)
                    toasts = []
                    ctrl, sidecar, _events = _make_controller(
                        resolve=lambda vid, lang: voices.resolve(
                            vid, lang, root=root),
                        toast=toasts.append)

                    result = ctrl.play_text("Прочитай це українське речення")
                    _wait(ctrl)
                    self.assertEqual(result, "playing")
                    self.assertIn("tts_no_voice_hint", toasts)
                    self.assertEqual(sidecar.loaded, [])

    def test_language_mismatch(self):
        toasts = []
        ctrl, _sc, _ = _make_controller(
            resolve=lambda vid, lang: voices.LANGUAGE_MISMATCH, toast=toasts.append)
        self.assertEqual(ctrl.play_text("The quick brown fox jumps high"),
                         "lang_mismatch")
        self.assertIn("tts_lang_mismatch", toasts)

    def test_unknown_mixed_multiple_voices_asks(self):
        # §7.2: непевна мова + кілька завантажених голосів → просимо вибір, не синтез
        toasts = []
        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        sc = FakeSidecar()
        ctrl = TtsController(
            cfg=cfg, coordinator=_Coord(),
            resolve_voice=lambda vid, lang: _rv(),
            sidecar_factory=lambda: sc, temp_factory=FakeTemp, combine=_combine,
            available_langs=lambda: {"uk", "en"},   # два голоси → неоднозначно
            toast=toasts.append)
        # російські маркери → detect_language = unknown
        self.assertEqual(ctrl.play_text("Электрический сыр объективный"), "lang_pick")
        self.assertIn("tts_lang_pick", toasts)

    def test_unknown_single_voice_uses_it(self):
        # непевна мова + РІВНО один голос → беремо його (неоднозначності нема)
        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        sc = FakeSidecar()
        ctrl = TtsController(
            cfg=cfg, coordinator=_Coord(),
            resolve_voice=lambda vid, lang: _rv(),
            sidecar_factory=lambda: sc, temp_factory=FakeTemp, combine=_combine,
            available_langs=lambda: {"uk"})
        self.assertEqual(ctrl.play_text("Электрический сыр объективный"), "playing")


class TestLatestWinsRejectBusy(unittest.TestCase):
    def test_play_streams_first_chunk(self):
        # СТРІМІНГ (§3.2 TTFS): перший чанк віддано плеєру негайно (не combined-WAV)
        ctrl, sc, events = _make_controller()
        self.assertEqual(ctrl.play_text("Прочитай це речення українською мовою"),
                         "playing")
        _wait(ctrl)
        self.assertTrue(events["chunks"])            # чанк(и) віддані плеєру
        self.assertTrue(events["chunks"][0][1])      # перший is_first=True
        self.assertTrue(os.path.isfile(events["chunks"][0][0]))

    def test_latest_wins_cancels_previous(self):
        ctrl, sc, _ = _make_controller()
        ctrl.play_text("Перше довге речення для озвучення тут")
        _wait(ctrl)
        # другий запит під час/після першого — latest-wins має скасувати попередній
        ctrl._active_req = "req1"               # імітуємо активний перший
        ctrl.play_text("Друге зовсім інше речення для озвучення")
        _wait(ctrl)
        self.assertIn("req1", sc.cancelled)     # cancel попереднього викликано

    def test_export_reject_busy(self):
        toasts = []
        ctrl, _sc, _ = _make_controller(toast=toasts.append)
        ctrl._busy = True                        # синтез у польоті
        out = str(Path(tempfile.mkdtemp()) / "o.wav")
        self.assertEqual(ctrl.export_text("Будь-який текст", out), "busy")
        self.assertIn("tts_busy", toasts)

    def test_export_writes_file(self):
        ctrl, _sc, events = _make_controller()
        out = str(Path(tempfile.mkdtemp()) / "озвучення.wav")
        self.assertEqual(ctrl.export_text("Текст для збереження у файл", out),
                         "exporting")
        _wait(ctrl)
        self.assertTrue(os.path.isfile(out))
        self.assertEqual(events["exported"], [out])

    def test_export_freespace_gate_blocks(self):
        # СУД п.5 §8.7: free-space gate РЕАЛЬНО викликається в _do_export. Немає місця
        # → файл НЕ пишеться, тост tts_save_nospace. Мутація (прибрати gate) → червонить.
        from unittest.mock import patch
        toasts = []
        ctrl, _sc, events = _make_controller(toast=toasts.append)
        out = str(Path(tempfile.mkdtemp()) / "великий.wav")
        with patch("fronts.desktop.tts_controller.save.enough_free_space",
                   return_value=False):
            ctrl.export_text("Завеликий текст для диска", out)
            _wait(ctrl)
        self.assertIn("tts_save_nospace", toasts)
        self.assertFalse(os.path.isfile(out))    # файл НЕ створено (gate спрацював)
        self.assertEqual(events["exported"], [])

    def test_no_plaintext_temp_leak(self):
        # БЛОКЕР рецензії §8.9: 3 послідовні «Прослухати» → максимум 1 жива temp-тека
        # одночасно; після stop() → 0. (регрес блокера 6 Хвилі 1)
        _TEMP_REGISTRY.clear()
        ctrl, _sc, _ = _make_controller()
        for i in range(3):
            ctrl.play_text(f"Речення номер {i} для послідовного озвучення тут")
            _wait(ctrl)
            self.assertLessEqual(len(_live_temps()), 1,
                                 f"витік plaintext-temp після play #{i}")
        ctrl.stop()
        self.assertEqual(len(_live_temps()), 0, "temp не прибрано після stop()")

    def test_overlapping_requests_old_cleans_only_own_temp(self):
        # СУД п.4 §8.9: старий воркер, що ЗАТРИМАВСЯ, після витіснення чистить ЛИШЕ
        # СВОЮ теку (request-local token), НЕ теку нового запиту. Мутація на глобальний
        # cleanup (self._active_temp замість переданого temp) → червонить.
        import threading as _th
        _TEMP_REGISTRY.clear()

        class BlockingSidecar(FakeSidecar):
            def __init__(self):
                super().__init__()
                self.release = _th.Event()
                self.hard_killed = False

            def synthesize_stream(self, *, text, voice_id, wav_dir, on_event=None, **k):
                from whisper_core.tts import MSG_ACCEPTED
                from whisper_core.tts.sidecar import TtsSidecarError
                if on_event:
                    on_event({"type": MSG_ACCEPTED, "id": "r"})
                self.release.wait(timeout=5)
                raise TtsSidecarError("урвано після витіснення")   # error-гілка → _finish

            def cancel(self, rid, **k):
                return True

            def hard_kill(self):
                self.hard_killed = True

        sidecars = []

        def factory():
            s = BlockingSidecar()
            sidecars.append(s)
            return s

        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        ctrl = TtsController(cfg=cfg, coordinator=_Coord(),
                             resolve_voice=lambda vid, lang: _rv(),
                             sidecar_factory=factory, temp_factory=FakeTemp,
                             combine=_combine)
        ctrl.play_text("Перший довгий запит для озвучення тут")
        time.sleep(0.2)                          # воркер1 дійшов до block
        worker1 = ctrl._worker
        temp1 = ctrl._active_temp
        self.assertIsNotNone(temp1)
        # витіснення: join воркера1 таймаутить (він у block) → hard-kill → новий запит
        ctrl.play_text("Другий інший запит для озвучення знову")
        temp2 = ctrl._active_temp
        self.assertIsNotNone(temp2)
        self.assertIsNot(temp1, temp2)
        # відпускаємо воркер1 → він падає в error-гілку → _finish(token1, temp1)
        sidecars[0].release.set()
        if worker1 is not None:
            worker1.join(5)
        # СВОЯ тека (temp1) прибрана; тека НОВОГО запиту (temp2) — НЕ прибрана воркером1
        self.assertFalse(temp2.cleaned, "старий воркер прибрав ЧУЖУ (нову) теку!")

    def test_error_cleans_temp(self):
        # збій синтезу → plaintext-temp прибрано (не лишати конфіденційне аудіо)
        _TEMP_REGISTRY.clear()

        class BoomSidecar(FakeSidecar):
            def synthesize_stream(self, **k):
                from whisper_core.tts.sidecar import TtsSidecarError
                raise TtsSidecarError("вибух")

        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        sc = BoomSidecar()
        ctrl = TtsController(cfg=cfg, coordinator=_Coord(),
                             resolve_voice=lambda vid, lang: _rv(),
                             sidecar_factory=lambda: sc, temp_factory=FakeTemp,
                             combine=_combine)
        ctrl.play_text("Текст, що впаде на синтезі")
        _wait(ctrl)
        self.assertEqual(len(_live_temps()), 0)   # temp прибрано на error

    def test_ttfs_first_chunk_before_full_synth(self):
        # CRITICAL §3.2: перший звук НЕ чекає синтезу всього тексту. Повільний sidecar
        # (0.2 c/чанк) → перший chunk_playable набагато раніше завершення всіх чанків.
        import time as _t

        class SlowSidecar(FakeSidecar):
            def synthesize_stream(self, *, text, voice_id, wav_dir, on_event=None, **k):
                from whisper_core.tts import (MSG_ACCEPTED, MSG_CHUNK_READY,
                                              MSG_RESULT)
                if on_event:
                    on_event({"type": MSG_ACCEPTED, "id": "r"})
                for i in range(3):
                    _t.sleep(0.2)
                    p = os.path.join(wav_dir, f"s{i}.wav")
                    with open(p, "wb") as f:
                        f.write(b"\x00" * 8)
                    if on_event:
                        on_event({"type": MSG_CHUNK_READY, "wav_path": p,
                                  "timings": []})
                if on_event:
                    on_event({"type": MSG_RESULT})
                return "r"

        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        times = {}
        t0 = _t.perf_counter()
        ctrl = TtsController(
            cfg=cfg, coordinator=_Coord(), resolve_voice=lambda vid, lang: _rv(),
            sidecar_factory=lambda: SlowSidecar(), temp_factory=FakeTemp,
            combine=_combine,
            on_chunk_playable=lambda tok, p, t, first: times.setdefault(
                "first" if first else "x", _t.perf_counter()))
        ctrl.play_text("Довге перше речення. Друге речення. Третє речення тут.")
        _wait(ctrl, timeout=5)
        total = _t.perf_counter() - t0
        self.assertIn("first", times)
        first_at = times["first"] - t0
        # перший звук набагато раніше повного синтезу (~0.6 c): TTFS збережено
        self.assertLess(first_at, total * 0.6)

    def test_rejects_fake_marker(self):
        # CRITICAL §3.2: вихід FakeTtsEngine ([fake-tts]) НЕ видається за успіх —
        # користувач бачить помилку, не тишу (відсутня модель ≠ відтворення).
        from whisper_core.tts import FAKE_ENGINE_MARKER

        class FakeMarkerSidecar(FakeSidecar):
            def synthesize_stream(self, *, text, voice_id, wav_dir, on_event=None, **k):
                from whisper_core.tts import (MSG_ACCEPTED, MSG_CHUNK_READY,
                                              MSG_RESULT)
                if on_event:
                    on_event({"type": MSG_ACCEPTED, "id": "r"})
                    p = os.path.join(wav_dir, "s0.wav")
                    with open(p, "wb") as f:
                        f.write(b"\x00" * 8)
                    on_event({"type": MSG_CHUNK_READY, "wav_path": p, "timings": [],
                              "normalized_text": f"{FAKE_ENGINE_MARKER} привіт"})
                    on_event({"type": MSG_RESULT})
                return "r"

        toasts = []
        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        chunks = []
        ctrl = TtsController(
            cfg=cfg, coordinator=_Coord(), resolve_voice=lambda vid, lang: _rv(),
            sidecar_factory=lambda: FakeMarkerSidecar(), temp_factory=FakeTemp,
            combine=_combine, toast=toasts.append,
            on_chunk_playable=lambda tok, p, t, first: chunks.append(p))
        ctrl.play_text("Текст без реальної моделі")
        _wait(ctrl)
        self.assertIn("tts_engine_error", toasts)    # чесна помилка
        self.assertEqual(chunks, [])                 # НЕ віддано плеєру як «успіх»

    def test_reverse_pause_saves_position_and_stops(self):
        # §9.2: зворотне диктування під час playback → позиція збережена + TTS зупинено
        stopped = []
        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        ctrl = TtsController(cfg=cfg, coordinator=_Coord(),
                             resolve_voice=lambda vid, lang: _rv(),
                             sidecar_factory=lambda: FakeSidecar(), temp_factory=FakeTemp,
                             combine=_combine, stop_playback=lambda: stopped.append(1),
                             position_provider=lambda: 2)   # грає речення 2
        self.assertTrue(ctrl.mark_reverse_pause())
        self.assertTrue(ctrl.has_reverse_pending())
        self.assertTrue(stopped)                     # playback зупинено
        self.assertEqual(ctrl.consume_reverse_index(), 2)   # позиція збережена
        self.assertFalse(ctrl.has_reverse_pending())        # спожито

    def test_reverse_pause_noop_when_not_playing(self):
        # нічого не грає → нема reverse-паузи (нема що відновлювати)
        ctrl, _sc, _ = _make_controller()
        ctrl._position_provider = lambda: None
        self.assertFalse(ctrl.mark_reverse_pause())
        self.assertFalse(ctrl.has_reverse_pending())

    def test_ordinary_record_does_not_resume(self):
        # §9.1/§9.2: без mark_reverse_pause (ordinary-запис) consume → None (не відновлює)
        ctrl, _sc, _ = _make_controller()
        self.assertIsNone(ctrl.consume_reverse_index())
        self.assertFalse(ctrl.has_reverse_pending())

    def test_stt_busy_rejects(self):
        toasts = []
        ctrl, _sc, _ = _make_controller(coord=_Coord(allow=False), toast=toasts.append)
        # координатор не дає lease (STT зайнятий записом) → чесна відмова
        self.assertEqual(ctrl.play_text("Текст під час активного запису тут"),
                         "rejected")
        self.assertIn("tts_muted_recording", toasts)


class TestDropDetection(unittest.TestCase):
    """Суд 5.3–5.5: drop-детекція за ФАКТОМ доставки чанка плеєру (не за порожнечею wavs),
    перевірена РЕАЛЬНИМ шляхом — справжня ListenPanel, справжній _preempt_previous() під
    локом, доставка чанка/drop через чергу «сигналів» як у проді (_on_tts_chunk/
    _on_tts_synth_dropped). panel.disarm перевіряється фактичним ефектом, не лічильником."""

    def _panel(self, text="Перше речення. Друге речення."):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.tts_panel import ListenPanel
        QApplication.instance() or QApplication([])
        return ListenPanel(None, has_voice=True, text=text)

    @staticmethod
    def _drain(gui, panel):
        # головний «GUI-потік»: злити чергу сигналів у РЕАЛЬНУ панель, як _on_tts_* у проді
        for ev in gui:
            if ev[0] == "chunk":
                panel.enqueue_chunk(ev[1], ev[2], ev[3], ev[4])
            else:
                panel.disarm(ev[1])

    def _ctrl(self, sidecar, gui):
        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")
        return TtsController(
            cfg=cfg, coordinator=_Coord(),
            resolve_voice=lambda vid, lang: _rv(),
            sidecar_factory=lambda: sidecar, temp_factory=FakeTemp, combine=_combine,
            on_chunk_playable=lambda tok, p, t, first: gui.append(("chunk", p, t, first, tok)),
            on_synth_dropped=lambda tok: gui.append(("drop", tok)),
            toast=lambda k: None)

    def test_real_preempt_after_chunk_disarms_panel(self):
        # ЧЕРВОНИЙ до 5.4: РЕАЛЬНИЙ _preempt_previous() знеактивнює token (під локом),
        # worker ПОТІМ віддає чанк при НЕактивному token → чанк НЕ доставлено плеєру →
        # generation «впала» → drop → panel.disarm(1) реально роззброює панель.
        panel = self._panel()
        panel.set_resume_index(2, generation=1)      # armed для генерації 1 (перший play → token 1)
        gui = []
        gate = threading.Event()

        class RaceSidecar(FakeSidecar):
            def synthesize_stream(self, *, text, voice_id, wav_dir, on_event=None, **k):
                self.synth_calls += 1
                rid = f"req{self.synth_calls}"
                if on_event:
                    on_event({"type": MSG_ACCEPTED, "id": rid})
                gate.wait(3)                          # чекаємо, поки головний потік зробить preempt
                p = os.path.join(wav_dir, "s0.wav")
                with open(p, "wb") as f:
                    f.write(b"\x00" * 8)
                if on_event:
                    on_event({"type": MSG_CHUNK_READY, "wav_path": p})   # token уже неактивний
                    on_event({"type": MSG_RESULT})
                return rid

        sc = RaceSidecar()
        ctrl = self._ctrl(sc, gui)
        ctrl.play_text("Перше речення. Друге речення.")   # token=1; worker блокується на gate
        # РЕАЛЬНИЙ preempt в окремому потоці (його join не блокує головний — worker прокинемо нижче)
        t = threading.Thread(target=ctrl._preempt_previous)
        t.start()
        while ctrl._active_token is not None:         # дочекатись, поки preempt зняв активність (під локом)
            time.sleep(0.001)
        gate.set()                                    # тепер worker віддає чанк при НЕактивному token
        t.join(5)
        _wait(ctrl)
        self._drain(gui, panel)
        # чанк НЕ доставлено (token неактивний), у черзі лише drop генерації 1
        self.assertNotIn("chunk", [e[0] for e in gui])
        self.assertIn(("drop", 1), gui)
        # РЕАЛЬНА панель роззброєна саме цією генерацією
        self.assertFalse(panel._resume_armed)
        self.assertEqual(panel._resume_index, 0)

    def test_delivered_chunk_no_drop_real_panel(self):
        # НЕГАТИВ: on_chunk_playable РЕАЛЬНО доставляє в панель (не no-op) → drop НЕ виникає
        panel = self._panel(text="Одне речення тут.")
        gui = []
        sc = FakeSidecar()                             # штатно: чанк при активному token
        ctrl = self._ctrl(sc, gui)
        ctrl.play_text("Одне речення тут.")
        _wait(ctrl)
        self._drain(gui, panel)
        self.assertTrue(any(e[0] == "chunk" for e in gui))   # чанк реально доставлено
        self.assertFalse(any(e[0] == "drop" for e in gui))   # generation НЕ впала
        self.assertNotEqual(panel._cur_index, -1)            # панель реально грає

    def test_toast_failure_does_not_swallow_drop(self):
        # Суд 5.5 п.1: виняток тосту НЕ зриває drop — notify_dropped ПЕРЕД тостом, тост
        # безпечний. ЧЕРВОНИЙ на 5.4 (там toast перед notify_dropped і його виняток
        # пропускав drop + _finish). Помилка синтезу з state["first"]==True → drop завжди.
        gui = []

        class BoomSidecar(FakeSidecar):
            def synthesize_stream(self, *, text, voice_id, wav_dir, on_event=None, **k):
                raise TtsSidecarError("boom")

        cfg = SimpleNamespace(tts_enabled=True, tts_voice_uk="styletts2_ua",
                              tts_voice_en="kokoro_en", ui_language="uk")

        def boom_toast(k):
            raise RuntimeError("трей недоступний")

        ctrl = TtsController(
            cfg=cfg, coordinator=_Coord(), resolve_voice=lambda vid, lang: _rv(),
            sidecar_factory=lambda: BoomSidecar(), temp_factory=FakeTemp, combine=_combine,
            on_chunk_playable=lambda tok, p, t, first: None,
            on_synth_dropped=lambda tok: gui.append(("drop", tok)),
            toast=boom_toast)
        ctrl.play_text("Текст для озвучення тут")
        _wait(ctrl)
        self.assertIn(("drop", 1), gui)


if __name__ == "__main__":
    unittest.main()
