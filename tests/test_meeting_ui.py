"""Тести режиму «Нарада» (feature/meeting-ui, Б2).

Логіка вкладки й інтеграції в app.py — з МОКнутими ядровими модулями Б1/Б3
(whisper_core.meeting.capture/session/postprocess ще не існують): агрегатор
доріжок, стани тумблера, взаємне блокування з PTT, видалення сесії. Стиль —
як DictationSilenceTests: unbound-виклики методів DesktopApp на SimpleNamespace.

Offscreen-рендер вкладки (show()/grab() живих QWidget із таймерами) винесено
в ОКРЕМИЙ процес — tests/render_meeting_smoke.py (integration wave-2): у
спільному процесі з рештою тестів недобитий Qt-таймер давав флакі-краш на
виході (0xC000041D). Тут лишається лише логіка на моках — без живих віджетів.
"""
import contextlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Віджети без екрана: рендер-тесту потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.app import DesktopApp


# --- дрібні спаї ------------------------------------------------------------
class _Sig:
    """Мінімальний дублер Qt-сигналу: пише emit-и, connect — no-op."""

    def __init__(self):
        self.emits = []

    def emit(self, *args):
        self.emits.append(args if len(args) != 1 else args[0])

    def connect(self, *a, **k):
        pass


class _Tray:
    def __init__(self):
        self.states = []
        self.notes = []

    def set_state(self, state, text=None):
        self.states.append(state)

    def notify(self, text):
        self.notes.append(text)


class _RunThread:
    """Підміна threading.Thread: .start() виконує ціль синхронно (детермінізм)."""

    def __init__(self, target=None, args=(), daemon=None, **kw):
        self._target = target
        self._args = args

    def start(self):
        if self._target:
            self._target(*self._args)


class _FakeStream:
    def __init__(self, *, kind, device_index, channels, rate, sink,
                 on_stall, on_device_lost, on_sink_error=None, on_audio=None):
        self.kind = kind
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def take_level(self):
        return (0.5, 0.7)


class _FakeSession:
    def __init__(self, sid, root):
        self.id = sid
        self.dir = Path(root) / sid
        self.finalized = []

    def mic_sink(self, pcm):
        pass

    def sys_sink(self, pcm):
        pass

    def close_segment(self, track):
        pass

    def finalize(self, status):
        self.finalized.append(status)
        return SimpleNamespace(id=self.id, status=status)


@contextlib.contextmanager
def fake_meeting(*, wavs=None, session_factory=None, stitch=None,
                 write_transcript=None):
    """Вставити фейкові whisper_core.meeting.{capture,session,postprocess} у
    sys.modules на час тесту (ядро Б1/Б3 ще не існує)."""
    pkg = types.ModuleType("whisper_core.meeting")
    cap = types.ModuleType("whisper_core.meeting.capture")
    ses = types.ModuleType("whisper_core.meeting.session")
    post = types.ModuleType("whisper_core.meeting.postprocess")
    # audit_log (chain-of-custody) — РЕАЛЬНИЙ модуль: він самодостатній (без Qt,
    # без capture/session), тож підмінений пакет має віддавати справжній журнал.
    # Інакше лінивий `from whisper_core.meeting import audit_log` у _audit_event
    # мовчки падав ImportError'ом (тека сесії — throwaway tempdir, запис безпечний).
    from whisper_core.meeting import audit_log as _audit_log

    cap.NATIVE_RATE = 48000
    cap.NATIVE_CHANNELS = 2
    cap.default_input = lambda name=None: {"index": 1, "name": "Мікрофон (тест)"}
    cap.default_loopback = lambda: {"index": 2, "name": "Динаміки (loopback)"}
    cap.CaptureStream = _FakeStream

    for name in ("RECORDING", "STOPPED", "PROCESSING", "DONE", "ERROR",
                 "INTERRUPTED"):
        setattr(ses, "STATUS_" + name, name.lower())
    ses.create_session = session_factory or (
        lambda root, sources, **kw: _FakeSession("2026-07-15_14-30-05", root))
    ses.load_meta = lambda session_dir: None
    # audit_log читає/пише журнал через session.{read,write}_artifact. Без них
    # лінивий `from .session import read_artifact` у audit_log падав ImportError
    # (глушився в _audit_event) → chain-of-custody сліпий у десятках тестів. Теки
    # тут незашифровані tempdir'и, тож реальні функції пишуть звичайний plaintext.
    from whisper_core.meeting.session import (read_artifact as _read_artifact,
                                              write_artifact as _write_artifact)
    ses.read_artifact = _read_artifact
    ses.write_artifact = _write_artifact
    # реальний гейт id тестується в test_meeting_session; тут пускаємо все, аби
    # контролер дійшов до delete_session (сам гейт — рубіж 2 захисту від traversal)
    ses.is_safe_session_id = lambda sid: True
    # finalize_dir НЕ задаємо за замовчуванням: guard-тест перевіряє деградацію
    # на старому ядрі без цієї функції (Б1 раунд 2 додав її).

    post.build_session_wavs = lambda d: dict(wavs or {})
    post.stitch = stitch or (lambda mic, sysx, **kw: ["<stitched>", mic, sysx])
    post.write_transcript = write_transcript or (
        lambda d, utt, *, me_label, others_label: (d / "transcript.txt",
                                                    d / "transcript.json"))

    mods = {
        "whisper_core.meeting": pkg,
        "whisper_core.meeting.capture": cap,
        "whisper_core.meeting.session": ses,
        "whisper_core.meeting.postprocess": post,
        "whisper_core.meeting.audit_log": _audit_log,
    }
    pkg.capture, pkg.session, pkg.postprocess = cap, ses, post
    pkg.audit_log = _audit_log
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield SimpleNamespace(capture=cap, session=ses, postprocess=post)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _controller(**overrides):
    """SimpleNamespace-контролер зі спаями під методи наради DesktopApp."""
    tmp = overrides.pop("root", None) or tempfile.mkdtemp()
    c = SimpleNamespace(
        cfg=SimpleNamespace(meeting_dir=None, meeting_sources="mic",
                            sounds=False, input_device=None),
        tray=_Tray(),
        _busy=False,
        _capturing=False,
        _mic_testing=False,
        _meeting_active=False,
        _meeting_session=None,
        _meeting_streams={},
        _meeting_pending={},
        _meeting_postprocessing=set(),
        _meeting_processing_jobs={},
        _live_meeting=None,
        _live_dictation=None,
        _start_live_meeting=lambda: None,
        _stop_live_meeting=lambda: None,
        recorder=SimpleNamespace(recording=False),
        meeting_state=_Sig(),
        meeting_audio_ready=_Sig(),
        meeting_session_done=_Sig(),
        meeting_error=_Sig(),
        meeting_storage_warning=_Sig(),
        meeting_track_done=_Sig(),
        meeting_screen_error=_Sig(),
        meeting_processing_progress=_Sig(),
        meeting_processing_done=_Sig(),
        transcription_error=_Sig(),
        terms=SimpleNamespace(),
        _meetings_root=lambda: Path(tmp),
        enqueue_meeting_track=None,   # заповнимо нижче
    )
    # реальні методи, які тестуємо, викликаємо unbound; але enqueue має
    # складати у справжню чергу-спай
    c._queued = []
    c.enqueue_meeting_track = lambda sid, track, wav: c._queued.append(
        (sid, track, str(wav)))
    c._meeting_session_dir = lambda sid: Path(tmp) / sid
    # внутрішні методи, які тестований метод кличе на self — прив'язуємо справжні
    c._detach_meeting = lambda: DesktopApp._detach_meeting(c)
    c._track_level = lambda track: DesktopApp._track_level(c, track)
    c._finish_meeting = lambda sid: DesktopApp._finish_meeting(c, sid)
    # feature/obsidian-channel: хук після фіналізації; cfg-фейк без obsidian_enabled
    # → реальний метод повертається одразу (канал вимкнено), фіналізації не заважає
    c._auto_obsidian = lambda sid: DesktopApp._auto_obsidian(c, sid)
    c._meeting_postprocess = lambda sid, d, sess: DesktopApp._meeting_postprocess(
        c, sid, d, sess)
    c._recover_meeting_recording = lambda sid, d: DesktopApp._recover_meeting_recording(
        c, sid, d)
    c._live_meeting_ids = lambda: DesktopApp._live_meeting_ids(c)
    c._await_screen_close = lambda sess, screen, timeout=10.0: DesktopApp._await_screen_close(c, sess, screen, timeout)
    c._complete_meeting_stop = lambda sess, screen: DesktopApp._complete_meeting_stop(c, sess, screen)
    c._complete_meeting_cancel = lambda sess, screen: DesktopApp._complete_meeting_cancel(c, sess, screen)
    c._discard_meeting_session = lambda sess, status: DesktopApp._discard_meeting_session(c, sess, status)
    c._process_meeting_worker = lambda sid, d, token: DesktopApp._process_meeting_worker(
        c, sid, d, token)
    c._mark_screen_started = lambda sess, started, monitor: DesktopApp._mark_screen_started(c, sess, started, monitor)
    c._mark_screen_failed = lambda sess, exc: DesktopApp._mark_screen_failed(c, sess, exc)
    c._on_meeting_state_tray = lambda state: DesktopApp._on_meeting_state_tray(
        c, state)
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


# ---------------------------------------------------------------- конфіг/шляхи
class ConfigMeetingTests(unittest.TestCase):
    def test_source_set(self):
        from whisper_core.config import meeting_source_set
        self.assertEqual(meeting_source_set(SimpleNamespace(meeting_sources="mic")),
                         {"mic"})
        self.assertEqual(
            meeting_source_set(SimpleNamespace(meeting_sources="mic+sys")),
            {"mic", "sys"})
        # порожній/битий → безпечний дефолт
        self.assertEqual(meeting_source_set(SimpleNamespace(meeting_sources="")),
                         {"mic"})
        self.assertEqual(meeting_source_set(SimpleNamespace(meeting_sources="junk")),
                         {"mic"})

    def test_preset_mapping(self):
        from whisper_core.config import (meeting_sources_for_preset,
                                         meeting_preset_for_cfg)
        self.assertEqual(meeting_sources_for_preset("both"), "mic+sys")
        self.assertEqual(meeting_sources_for_preset("onlymic"), "mic")
        self.assertEqual(
            meeting_preset_for_cfg(SimpleNamespace(meeting_sources="mic+sys")),
            "both")
        self.assertEqual(
            meeting_preset_for_cfg(SimpleNamespace(meeting_sources="mic")),
            "onlymic")

    def test_save_roundtrip(self):
        from whisper_core.config import Config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            cfg = Config()
            cfg.meeting_sources = "mic+sys"
            cfg.meeting_dir = str(Path(d) / "meets")
            cfg.save(p)
            loaded = Config.load(p)
            self.assertEqual(loaded.meeting_sources, "mic+sys")
            self.assertEqual(loaded.meeting_dir, str(Path(d) / "meets"))

    def test_meeting_dir_not_written_when_none(self):
        from whisper_core.config import Config
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            Config().save(p)   # meeting_dir=None
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("meeting_dir", text)
            self.assertIn("meeting_sources", text)   # пресет пишемо завжди


# ------------------------------------------------------------------- старт/стоп
class MeetingStartTests(unittest.TestCase):
    def test_start_onlymic_opens_one_stream(self):
        live_starts = []
        c = _controller(_start_live_meeting=lambda: live_starts.append(True))
        with fake_meeting():
            ok = DesktopApp.meeting_start(c, "onlymic")
        self.assertTrue(ok)
        self.assertTrue(c._meeting_active)
        self.assertEqual(set(c._meeting_streams), {"mic"})
        self.assertTrue(c._meeting_streams["mic"].started)
        self.assertEqual(c.meeting_state.emits, ["recording"])
        self.assertEqual(live_starts, [])  # capture phase не запускає ASR
        # трей оновлює ЛИШЕ слот _on_meeting_state_tray (GUI-потік) — прямих
        # викликів tray.set_state з методів наради нема (раунд 2, фікс 2)
        self.assertEqual(c.tray.states, [])

    def test_start_both_opens_two_streams(self):
        c = _controller(cfg=SimpleNamespace(meeting_dir=None,
                                            meeting_sources="mic+sys", sounds=False,
                                            input_device=None))
        with fake_meeting():
            ok = DesktopApp.meeting_start(c, "both")
        self.assertTrue(ok)
        self.assertEqual(set(c._meeting_streams), {"mic", "sys"})

    def test_start_refused_when_busy(self):
        from fronts.desktop.i18n import tr
        c = _controller(_busy=True)
        with fake_meeting():
            ok = DesktopApp.meeting_start(c, "onlymic")
        self.assertFalse(ok)
        self.assertFalse(c._meeting_active)
        self.assertEqual(c.tray.notes, [tr("meeting_busy_wait")])

    def test_start_refused_when_recording_dictation(self):
        c = _controller(recorder=SimpleNamespace(recording=True))
        with fake_meeting():
            ok = DesktopApp.meeting_start(c, "onlymic")
        self.assertFalse(ok)

    def test_start_both_refused_without_loopback(self):
        from fronts.desktop.i18n import tr
        c = _controller()
        with fake_meeting() as fk:
            fk.capture.default_loopback = lambda: None
            ok = DesktopApp.meeting_start(c, "both")
        self.assertFalse(ok)
        self.assertEqual(c.tray.notes, [tr("meeting_no_loopback")])

    def test_start_device_enumeration_failure_is_toast_not_crash(self):
        """Раунд 2, фікс 3: збій енумерації пристроїв (PortAudio впав) → чиста
        відмова з тостом, а не виняток у GUI-слот."""
        from fronts.desktop.i18n import tr
        c = _controller()

        def boom(name=None):
            raise RuntimeError("PortAudio down")

        with fake_meeting() as fk:
            fk.capture.default_input = boom
            with self.assertLogs(level="ERROR"):
                ok = DesktopApp.meeting_start(c, "onlymic")
        self.assertFalse(ok)
        self.assertFalse(c._meeting_active)
        self.assertEqual(c.tray.notes, [tr("meeting_error_generic")])

    def test_start_loopback_enumeration_failure_is_toast_not_crash(self):
        from fronts.desktop.i18n import tr
        c = _controller()

        def boom():
            raise RuntimeError("WASAPI down")

        with fake_meeting() as fk:
            fk.capture.default_loopback = boom
            with self.assertLogs(level="ERROR"):
                ok = DesktopApp.meeting_start(c, "both")
        self.assertFalse(ok)
        self.assertEqual(c.tray.notes, [tr("meeting_error_generic")])

    def test_stop_exports_audio_without_enqueuing_transcription(self):
        c = _controller()
        with fake_meeting(wavs={"mic": Path("m.wav")}):
            DesktopApp.meeting_start(c, "onlymic")
            sess = c._meeting_session
            with patch("fronts.desktop.app.threading.Thread", _RunThread):
                DesktopApp.meeting_stop(c)
        self.assertFalse(c._meeting_active)
        self.assertIn("stopped", sess.finalized)
        self.assertEqual(c.meeting_state.emits, ["recording", "processing", "idle"])
        # Record phase лише готує WAV; ASR-черга не запускається автоматично.
        self.assertEqual(c._queued, [])
        self.assertEqual(c.meeting_audio_ready.emits, ["2026-07-15_14-30-05"])

    def test_canonical_sources_open_two_microphones_and_system(self):
        from whisper_core.config import MEETING_SYSTEM_SOURCE, meeting_microphone_token
        c = _controller(cfg=SimpleNamespace(
            meeting_dir=None, meeting_sources="multimic", sounds=False,
            input_device=None, meeting_export_segment_minutes=10,
            meeting_record_sources=[meeting_microphone_token("M1"),
                                    meeting_microphone_token("M2"),
                                    MEETING_SYSTEM_SOURCE],
            meeting_mic_devices=["M1", "M2"]))
        created = {}

        def factory(root, sources, **kwargs):
            created.update(sources=list(sources), kwargs=kwargs)
            return _FakeSession("2026-07-15_14-30-05", root)

        with fake_meeting(session_factory=factory):
            ok = DesktopApp.meeting_start(c, "multimic")
        self.assertTrue(ok)
        self.assertEqual(created["sources"], ["mic1", "mic2", "sys"])
        self.assertEqual(set(c._meeting_streams), {"mic1", "mic2", "sys"})
        self.assertEqual(created["kwargs"]["export_segment_seconds"], 600)

    def test_cancel_deletes_session(self):
        deleted = []
        c = _controller()
        with fake_meeting() as fk:
            # delete_session тепер приймає й корінь сховища (рубіж 3 захисту)
            fk.session.delete_session = lambda d, root=None: deleted.append(d) or True
            DesktopApp.meeting_start(c, "onlymic")
            sess = c._meeting_session
            # _RunThread виконує завершення скасування синхронно в межах fake_meeting
            with patch("fronts.desktop.app.threading.Thread", _RunThread):
                DesktopApp.meeting_cancel(c)
        self.assertFalse(c._meeting_active)
        self.assertEqual(deleted, [sess.dir])
        self.assertEqual(c.meeting_state.emits, ["recording", "idle"])


class MeetingCancelConfirmTests(unittest.TestCase):
    """Б3: «Скасувати запис» знищує живу нараду безповоротно — тепер, як і
    видалення завершеної, вимагає підтвердження. Тонкий UI-обробник _on_cancel
    перевіряємо unbound-викликом на стабі, з фейковим QMessageBox у неймспейсі
    сторінки (без живого QWidget/Qt-діалогу)."""

    def _page(self):
        calls = []
        controller = SimpleNamespace(meeting_cancel=lambda: calls.append(True))
        return SimpleNamespace(controller=controller), calls

    def test_no_answer_keeps_the_recording(self):
        from fronts.desktop.pages import meeting as meeting_page
        page, calls = self._page()
        asked = []

        def _confirm(_parent, title, text):
            asked.append((title, text))
            return False
        with patch.object(meeting_page, "_meeting_confirm", _confirm):
            meeting_page.MeetingPage._on_cancel(page)
        self.assertEqual(len(asked), 1)       # спитали ПЕРЕД знищенням
        self.assertEqual(calls, [])           # «Ні» → нараду не чіпаємо

    def test_yes_answer_cancels_only_after_confirmation(self):
        from fronts.desktop.pages import meeting as meeting_page
        page, calls = self._page()
        asked = []

        def _confirm(_parent, title, text):
            asked.append((title, text))
            return True
        with patch.object(meeting_page, "_meeting_confirm", _confirm):
            meeting_page.MeetingPage._on_cancel(page)
        self.assertEqual(len(asked), 1)
        self.assertEqual(calls, [True])       # лише по «Так» скасовуємо


# ---------------------------------------------------------------- вихід
class MeetingExitTests(unittest.TestCase):
    def test_exit_waits_at_most_three_seconds_and_marks_timeout_failed(self):
        from whisper_core.meeting.session import (MeetingSession, STATUS_INTERRUPTED,
                                                  load_meta)

        waits = []
        screen = SimpleNamespace(
            request_stop=lambda: None,
            wait_finished=lambda timeout: waits.append(timeout) or False,
        )
        with tempfile.TemporaryDirectory() as root:
            session = MeetingSession(Path(root), ["mic"])
            c = _controller(_meeting_active=True, _meeting_screen_recorder=screen)
            c._meeting_session = session
            DesktopApp._shutdown_meeting_for_exit(c)

            self.assertEqual(waits, [3.0])
            meta = load_meta(session.dir)
            self.assertEqual(meta.screen_status, "failed")
            self.assertEqual(meta.status, STATUS_INTERRUPTED)


# -------------------------------------------------------------- агрегатор доріжок
class MeetingAggregatorTests(unittest.TestCase):
    def _pending(self, c, sid, expected, sess=None):
        c._meeting_pending[sid] = {
            "session": sess or _FakeSession(sid, tempfile.mkdtemp()),
            "dir": Path(tempfile.mkdtemp()),
            "expected": expected, "tracks": {}}
        return c._meeting_pending[sid]

    def test_two_tracks_stitch_and_finalize_done(self):
        c = _controller()
        sid = "S"
        p = self._pending(c, sid, expected=2)
        seen = {}
        with fake_meeting(
                stitch=lambda mic, sysx, **kw: seen.setdefault("stitch", (mic, sysx)),
                write_transcript=lambda d, utt, *, me_label, others_label:
                    seen.setdefault("wt", (me_label, others_label))):
            with patch("fronts.desktop.app.threading.Thread", _RunThread):
                DesktopApp._on_meeting_track_done(c, sid, "mic", "textM", ["mseg"])
                # одна доріжка ще не фінал
                self.assertNotIn("stitch", seen)
                DesktopApp._on_meeting_track_done(c, sid, "sys", "textS", ["sseg"])
        self.assertEqual(seen["stitch"], (["mseg"], ["sseg"]))
        self.assertIn("done", p["session"].finalized)
        self.assertEqual(c.meeting_session_done.emits and
                         c.meeting_session_done.emits[0][0], sid)
        self.assertNotIn(sid, c._meeting_pending)   # не зависає

    def test_single_track_finalizes_after_one(self):
        c = _controller()
        sid = "S1"
        p = self._pending(c, sid, expected=1)
        with fake_meeting() as fk:
            captured = {}
            fk.postprocess.stitch = lambda mic, sysx, **kw: captured.setdefault(
                "args", (mic, sysx))
            with patch("fronts.desktop.app.threading.Thread", _RunThread):
                DesktopApp._on_meeting_track_done(c, sid, "mic", "only", ["seg"])
        # одна доріжка: sys=None → зшивка без міток (speaker="single" у ядрі)
        self.assertEqual(captured["args"], (["seg"], None))
        self.assertIn("done", p["session"].finalized)
        self.assertNotIn(sid, c._meeting_pending)

    def test_live_session_done_written_via_finalize_dir(self):
        """Integration wave-2: жива сесія доходить до 'done' НА ДИСКУ через
        finalize_dir — sess.finalize після стопу вже no-op (ідемпотентність Б1
        заморожує 'stopped'), тож саме finalize_dir має підняти статус, а у
        session_done летить свіжа meta від нього."""
        c = _controller()
        sid = "S3"
        p = self._pending(c, sid, expected=1)
        calls = []
        fresh = SimpleNamespace(id=sid, status="done")
        with fake_meeting() as fk:
            fk.session.finalize_dir = (
                lambda sdir, status: calls.append((sdir, status)) or fresh)
            with patch("fronts.desktop.app.threading.Thread", _RunThread):
                DesktopApp._on_meeting_track_done(c, sid, "mic", "t", ["seg"])
        self.assertIn("done", p["session"].finalized)   # файли закрито/сумісність
        self.assertEqual(calls, [(p["dir"], "done")])   # статус на диску — finalize_dir
        self.assertEqual(c.meeting_session_done.emits, [(sid, fresh)])
        self.assertNotIn(sid, c._meeting_pending)

    def test_track_error_finalizes_error_no_hang(self):
        c = _controller()
        sid = "S2"
        p = self._pending(c, sid, expected=2)
        with fake_meeting():
            DesktopApp._on_meeting_error(c, sid, "boom")
        self.assertIn("error", p["session"].finalized)
        self.assertNotIn(sid, c._meeting_pending)          # очікування знято
        self.assertEqual(c.tray.notes, ["boom"])
        self.assertIn("idle", c.meeting_state.emits)

    def test_finish_error_when_stitch_raises(self):
        from fronts.desktop.i18n import tr
        c = _controller()
        sid = "S3"
        p = self._pending(c, sid, expected=1)

        def boom(*a, **k):
            raise RuntimeError("stitch fail")

        with fake_meeting(stitch=boom):
            DesktopApp._finish_meeting(c, sid)
        self.assertIn("error", p["session"].finalized)
        self.assertEqual(c.meeting_error.emits,
                         [(sid, tr("meeting_error_generic"))])

    def test_finish_never_touches_tray_directly(self):
        """_finish_meeting біжить у worker-потоці: трей — ЛИШЕ через сигнал
        meeting_state → GUI-слот (раунд 2, фікс 2). Спай-сигнал не диспатчить у
        слот, тож будь-який запис у tray.states = прямий (небезпечний) виклик."""
        c = _controller()
        sid = "S4"
        self._pending(c, sid, expected=1)
        c._meeting_pending[sid]["tracks"]["mic"] = ("text", ["seg"])
        with fake_meeting():
            DesktopApp._finish_meeting(c, sid)
        self.assertEqual(c.tray.states, [])
        self.assertIn("idle", c.meeting_state.emits)


class MeetingRecoveryTests(unittest.TestCase):
    """Раунд 2, фікс 1: відновлена (осиротіла) сесія має доходити до «готово»
    через session.finalize_dir (Б1 раунд 2) — живого MeetingSession при
    відновленні нема."""

    def _pending_recovered(self, c, sid):
        d = Path(tempfile.mkdtemp())
        c._meeting_pending[sid] = {"session": None, "dir": d,
                                   "expected": 1,
                                   "tracks": {"mic": ("text", ["seg"])}}
        return d

    def test_recovered_session_finalized_done_via_finalize_dir(self):
        c = _controller()
        sid = "R1"
        d = self._pending_recovered(c, sid)
        calls = []
        meta = SimpleNamespace(id=sid, status="done")
        with fake_meeting() as fk:
            fk.session.finalize_dir = (
                lambda sdir, status: calls.append((sdir, status)) or meta)
            DesktopApp._finish_meeting(c, sid)
        self.assertEqual(calls, [(d, "done")])                 # статус на диску
        self.assertEqual(c.meeting_session_done.emits, [(sid, meta)])
        self.assertNotIn(sid, c._meeting_pending)
        self.assertEqual(c.tray.states, [])   # трей — лише через слот

    def test_recovered_session_without_finalize_dir_degrades(self):
        """Старе ядро без finalize_dir: транскрипт записано, краху нема,
        session_done емітиться з meta=None (guard-шлях)."""
        c = _controller()
        sid = "R2"
        self._pending_recovered(c, sid)
        with fake_meeting():                   # finalize_dir відсутня у фейку
            DesktopApp._finish_meeting(c, sid)
        self.assertEqual(c.meeting_session_done.emits, [(sid, None)])
        self.assertIn("idle", c.meeting_state.emits)
        self.assertNotIn(sid, c._meeting_pending)

    def test_recover_meeting_rebuilds_audio_without_transcription(self):
        c = _controller()
        sid = "2026-07-15_08-00-00"
        recorded = []
        meta = SimpleNamespace(id=sid, status="done")
        with fake_meeting(wavs={"mic": Path("m.wav")}) as fk:
            fk.session.load_meta = (
                lambda d: SimpleNamespace(id=sid, status="interrupted"))
            fk.postprocess.build_segmented_wavs = lambda d: {"mic": [Path("m.wav")]}
            fk.session.record_audio_exports = (
                lambda d, exports: recorded.append((d, exports)))
            fk.session.finalize_dir = lambda d, status: meta
            with patch("fronts.desktop.app.threading.Thread", _RunThread):
                DesktopApp.recover_meeting(c, sid)
        self.assertEqual(c.meeting_state.emits, ["processing", "idle"])
        self.assertEqual(c._queued, [])
        self.assertTrue(recorded)
        self.assertEqual(c.meeting_audio_ready.emits, [sid])
        self.assertEqual(c.meeting_session_done.emits, [(sid, meta)])
        self.assertEqual(c.tray.states, [])   # трей — лише через слот

    def test_recover_meeting_no_meta_is_noop(self):
        c = _controller()
        with fake_meeting():                   # load_meta → None (дефолт фейку)
            DesktopApp.recover_meeting(c, "ghost")
        self.assertEqual(c.meeting_state.emits, [])
        self.assertEqual(c._queued, [])


class MeetingTraySlotTests(unittest.TestCase):
    """Раунд 2, фікс 2: єдиний GUI-слот meeting_state → трей."""

    def test_tray_slot_maps_states(self):
        c = _controller()
        DesktopApp._on_meeting_state_tray(c, "recording")
        DesktopApp._on_meeting_state_tray(c, "processing")
        DesktopApp._on_meeting_state_tray(c, "postprocessing")
        DesktopApp._on_meeting_state_tray(c, "idle")
        self.assertEqual(c.tray.states, ["recording", "busy", "busy", "idle"])


# ------------------------------------------------------------ блокування з PTT
class MeetingGatingTests(unittest.TestCase):
    def test_on_press_ignored_during_meeting(self):
        calls = []
        c = SimpleNamespace(
            _key_down=False, _cancel_guard=False, _capturing=False,
            _meeting_active=True, _mic_testing=False, _busy=False,
            cfg=SimpleNamespace(ptt_mode="hold"),
            recorder=SimpleNamespace(recording=False),
            _start_recording=lambda: calls.append("start"))
        DesktopApp.on_press(c)
        self.assertEqual(calls, [])
        self.assertTrue(c._key_down)   # клавішу відстежуємо, але запис не стартує

    def test_record_start_ignored_during_meeting(self):
        calls = []
        c = SimpleNamespace(
            _busy=False, _capturing=False, _meeting_active=True,
            recorder=SimpleNamespace(recording=False), _mic_warned=True,
            _start_recording=lambda: calls.append("start"))
        DesktopApp.record_start(c)
        self.assertEqual(calls, [])

    # --- integration wave-2: взаємні гейти тест мікрофона ↔ нарада ---
    def test_mic_test_busy_during_meeting(self):
        """Йде нарада → тест мікрофона «зайнятий» (спільний мікрофон)."""
        c = SimpleNamespace(
            _busy=False, _capturing=False, _mic_testing=False,
            _meeting_active=True, recorder=SimpleNamespace(recording=False))
        self.assertTrue(DesktopApp.mic_test_busy(c))
        c._meeting_active = False
        self.assertFalse(DesktopApp.mic_test_busy(c))

    def test_meeting_start_refused_during_mic_test(self):
        """Триває тест мікрофона → нарада не стартує (спільний мікрофон)."""
        from fronts.desktop.i18n import tr
        c = _controller(_mic_testing=True)
        with fake_meeting():
            ok = DesktopApp.meeting_start(c, "onlymic")
        self.assertFalse(ok)
        self.assertFalse(c._meeting_active)
        self.assertEqual(c.tray.notes, [tr("meeting_busy_wait")])


# ----------------------------------------------------------- команди-хелпери
class MeetingCommandTests(unittest.TestCase):
    def test_delete_meeting_calls_core(self):
        deleted = []
        c = _controller()
        with fake_meeting() as fk:
            # delete_session тепер приймає й корінь сховища (рубіж 3 захисту)
            fk.session.delete_session = lambda d, root=None: deleted.append(d) or True
            DesktopApp.delete_meeting(c, "2026-07-15_10-00-00")
        self.assertEqual(deleted, [c._meeting_session_dir("2026-07-15_10-00-00")])

    def test_list_meetings_keeps_live_postprocess_stopped_session(self):
        marked = []
        c = _controller()
        c._meeting_pending["live"] = {"session": None}
        with fake_meeting() as fk:
            fk.session.find_orphans = lambda root: [
                SimpleNamespace(id="live"), SimpleNamespace(id="crashed")]
            fk.session.mark_interrupted = lambda d: marked.append(d.name)
            fk.session.list_sessions = lambda root: []
            DesktopApp.list_meetings(c)
        self.assertEqual(marked, ["crashed"])
    def test_list_meetings_marks_orphans(self):
        marked = []
        c = _controller()
        with fake_meeting() as fk:
            fk.session.find_orphans = lambda root: [SimpleNamespace(id="orph")]
            fk.session.mark_interrupted = lambda d: marked.append(d)
            fk.session.list_sessions = lambda root: ["<meta>"]
            out = DesktopApp.list_meetings(c)
        self.assertEqual(out, ["<meta>"])
        self.assertEqual(marked, [c._meetings_root() / "orph"])

    def test_list_meetings_recovers_orphan_processing_as_cancelled(self):
        c = _controller()
        meta = SimpleNamespace(
            id="2026-07-15_10-00-00",
            processing={"status": "running"},
        )
        updates = []
        with fake_meeting() as fk:
            fk.session.find_orphans = lambda root: []
            fk.session.list_sessions = lambda root: [meta]
            fk.session.update_processing = lambda path, **state: (
                updates.append((path, state))
                or SimpleNamespace(id=meta.id, processing=state)
            )
            out = DesktopApp.list_meetings(c)
        self.assertEqual(out[0].processing["status"], "cancelled")
        self.assertEqual(updates[0][0], c._meetings_root() / meta.id)

    def test_set_meeting_sources_writes_cfg(self):
        saved = []
        c = _controller()
        c.cfg.save = lambda: saved.append(c.cfg.meeting_sources)
        DesktopApp.set_meeting_sources(c, "both")
        self.assertEqual(c.cfg.meeting_sources, "mic+sys")
        self.assertEqual(saved, ["mic+sys"])

    def test_meeting_level_reads_stream(self):
        c = _controller(_meeting_streams={"mic": _FakeStream(
            kind="mic", device_index=1, channels=2, rate=48000, sink=None,
            on_stall=None, on_device_lost=None)})
        self.assertEqual(DesktopApp.meeting_mic_level(c), (0.5, 0.7))
        self.assertEqual(DesktopApp.meeting_sys_level(c), (0.0, 0.0))  # нема sys


class MeetingExplicitProcessingTests(unittest.TestCase):
    class _Token:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    def test_explicit_command_runs_pipeline_in_background_with_progress(self):
        c = _controller()
        c._transcribe_with_fallback = lambda *args, **kwargs: ()
        meta = SimpleNamespace(processing={}, audio_files={"mic": ["audio/mic/0001.wav"]})
        calls = []

        def process(session_dir, **kwargs):
            calls.append((session_dir, kwargs))
            kwargs["progress"]({
                "status": "running", "completed_chunks": 1, "total_chunks": 2})
            return SimpleNamespace(status="complete", word_count=1, tracks={})

        with patch("whisper_core.meeting.session.is_safe_session_id", return_value=True), \
                patch("whisper_core.meeting.session.load_meta", return_value=meta), \
                patch("whisper_core.meeting.meeting_pipeline.CancelToken", self._Token), \
                patch("whisper_core.meeting.meeting_pipeline.process_meeting", process), \
                patch("fronts.desktop.app._audit_event"), \
                patch("fronts.desktop.app.threading.Thread", _RunThread):
            ok = DesktopApp.start_meeting_processing(
                c, "2026-07-15_10-00-00")

        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["asr_provenance"]["engine"] == "faster-whisper")
        self.assertEqual(
            c.meeting_processing_progress.emits[0][0],
            "2026-07-15_10-00-00",
        )
        self.assertEqual(
            c.meeting_processing_done.emits[-1][1].status, "complete")
        self.assertEqual(c.meeting_state.emits, ["postprocessing", "idle"])
        self.assertEqual(c._meeting_processing_jobs, {})

    def test_cancel_marks_token_without_deleting_audio(self):
        c = _controller()
        token = self._Token()
        c._meeting_processing_jobs["2026-07-15_10-00-00"] = token

        self.assertTrue(DesktopApp.cancel_meeting_processing(
            c, "2026-07-15_10-00-00"))
        self.assertTrue(token.cancelled)

    def test_processing_is_refused_while_recording(self):
        c = _controller(_meeting_active=True)
        self.assertFalse(DesktopApp.start_meeting_processing(
            c, "2026-07-15_10-00-00"))
        self.assertEqual(c._meeting_processing_jobs, {})

    def test_new_recording_is_refused_while_processing(self):
        c = _controller()
        c._meeting_processing_jobs["2026-07-15_10-00-00"] = self._Token()
        with fake_meeting():
            self.assertFalse(DesktopApp.meeting_start(c, "onlymic"))
        self.assertFalse(c._meeting_active)


# Offscreen-рендер вкладки «Нарада» винесено в tests/render_meeting_smoke.py
# (окремий процес) — див. модульний docstring вище.


# ------------------------------------------------- діаризація: воркер завантаження
class DiarizationDownloadWorkerTests(unittest.TestCase):
    """Регрес: реорг Налаштувань у вкладки (618f567) прибрав
    DiarizationDownloadWorker із settings.py, але лінивий імпорт у
    _download_diarization досі тягнув `from .settings import ...` → ImportError
    при кліку «завантажити моделі» на вкладці «Нарада». Лінивий імпорт усередині
    методу — тому юніт-тести його не бачили. Клас тепер живе в meeting.py."""

    def test_worker_importable_from_meeting_module(self):
        # ТОЙ САМИЙ символ, що його тягне _download_diarization (тепер локальний)
        from fronts.desktop.pages.meeting import DiarizationDownloadWorker
        # конструюємо без .start() — потік не запускаємо, лише перевіряємо API
        w = DiarizationDownloadWorker(Path("x"))
        self.assertTrue(hasattr(w, "finished_ok"))
        self.assertTrue(hasattr(w, "failed"))
        self.assertTrue(hasattr(w, "progress"))

    def test_meeting_has_no_stale_settings_import(self):
        # саме цей рядок був крашем — читаємо meeting.py як текст
        text = (Path(__file__).resolve().parent.parent
                / "fronts" / "desktop" / "pages" / "meeting.py"
                ).read_text(encoding="utf-8")
        self.assertNotIn("from .settings import DiarizationDownloadWorker", text)


class MeetingScreenRecordToggleTests(unittest.TestCase):
    """Виправлення 1: до цього meeting_screen_enabled НЕ вмикав ЖОДЕН продакшн-UI
    (лише тест-фейки), тож відео наради лишалось недосяжним для нового профілю.
    Тепер чекбокс «Записувати екран під час наради» у налаштуваннях наради
    вмикає його через контролер set_meeting_screen_enabled."""

    def test_toggle_on_enables_via_controller(self):
        from fronts.desktop.pages import meeting as mp
        calls = []
        page = SimpleNamespace(controller=SimpleNamespace(
            set_meeting_screen_enabled=lambda on: calls.append(on)))
        mp.MeetingPage._on_screen_record_toggle(page, True)
        self.assertEqual(calls, [True])

    def test_toggle_off_disables_via_controller(self):
        from fronts.desktop.pages import meeting as mp
        calls = []
        page = SimpleNamespace(controller=SimpleNamespace(
            set_meeting_screen_enabled=lambda on: calls.append(on)))
        mp.MeetingPage._on_screen_record_toggle(page, False)
        self.assertEqual(calls, [False])

    def test_production_ui_wires_screen_enable(self):
        """Регрес: у продакшн-коді сторінки має бути ЖИВИЙ виклик
        set_meeting_screen_enabled (а не лише визначення в app.py) — інакше відео
        наради знову стає недосяжним. Читаємо meeting.py як текст."""
        text = (Path(__file__).resolve().parent.parent
                / "fronts" / "desktop" / "pages" / "meeting.py"
                ).read_text(encoding="utf-8")
        self.assertIn("set_meeting_screen_enabled", text)


class RequestQuitTests(unittest.TestCase):
    """С7: вихід під час запису наради має спитати підтвердження."""

    def _stub(self, active):
        calls = []
        controller = SimpleNamespace(
            _meeting_active=active, window=None,
            app=SimpleNamespace(quit=lambda: calls.append(True)))
        return controller, calls

    def test_quit_without_recording_quits_immediately(self):
        c, calls = self._stub(False)
        DesktopApp.request_quit(c)
        self.assertEqual(calls, [True])

    def test_quit_during_recording_cancelled_does_not_quit(self):
        import PySide6.QtWidgets as W
        c, calls = self._stub(True)
        box = SimpleNamespace(Yes=1, No=0, question=lambda *a, **k: 0)
        with patch.object(W, "QMessageBox", box):
            DesktopApp.request_quit(c)
        self.assertEqual(calls, [])

    def test_quit_during_recording_confirmed_quits(self):
        import PySide6.QtWidgets as W
        c, calls = self._stub(True)
        box = SimpleNamespace(Yes=1, No=0, question=lambda *a, **k: 1)
        with patch.object(W, "QMessageBox", box):
            DesktopApp.request_quit(c)
        self.assertEqual(calls, [True])


class RawAudioCleanupTests(unittest.TestCase):
    """С10: прибирання сирих .f32 після успішного WAV-export."""

    def _make(self, tmp, *, with_wavs):
        from dataclasses import asdict
        from whisper_core.meeting.session import MeetingMeta, atomic_write_json
        sid = "2026-07-15_14-30-05"
        d = Path(tmp) / sid
        (d / "mic").mkdir(parents=True)
        (d / "mic" / "0001.f32").write_bytes(b"\x00" * 4096)
        (d / "mic" / "0002.f32").write_bytes(b"\x00" * 2048)
        meta = MeetingMeta(
            schema=2, id=sid, created=1, status="done", preset="onlymic",
            sources=["mic"],
            audio_files={"mic": ["audio/mic/0001.wav"]} if with_wavs else {})
        atomic_write_json(d / "meeting.json", asdict(meta))
        c = SimpleNamespace()
        c._meeting_session_dir = lambda s, dd=d: dd
        c._meeting_raw_f32_paths = lambda s: DesktopApp._meeting_raw_f32_paths(c, s)
        return c, sid, d

    def test_free_raw_audio_deletes_f32_when_wavs_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, sid, d = self._make(tmp, with_wavs=True)
            self.assertEqual(DesktopApp.meeting_raw_audio_bytes(c, sid), 4096 + 2048)
            freed = DesktopApp.meeting_free_raw_audio(c, sid)
            self.assertEqual(freed, 4096 + 2048)
            self.assertEqual(list((d / "mic").glob("*.f32")), [])

    def test_free_raw_audio_refuses_without_wavs(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, sid, d = self._make(tmp, with_wavs=False)
            self.assertEqual(DesktopApp.meeting_free_raw_audio(c, sid), 0)
            self.assertEqual(len(list((d / "mic").glob("*.f32"))), 2)


class MeetingScreenVideoLookupTests(unittest.TestCase):
    """Ліцензійна хвиля: нові наради пишуть screen.webm (VP9), старі — screen.mp4
    (H.264). meeting_screen_video має знаходити ОБИДВА розширення (сумісність зі
    старими нарадами), віддаючи перевагу webm, і повертати None, коли відео немає
    або запис позначено збійним."""

    def _controller(self, d):
        c = SimpleNamespace()
        c._meeting_session_dir = lambda s, dd=d: dd
        # meeting_screen_video тепер бере materialized-теку. Реальний метод для
        # незашифрованої сесії (без meeting.json.enc) віддає саму теку сесії, тож
        # прив'язуємо справжній — так тест і перевіряє коректний фолбек.
        c._materialized_meeting_dir = lambda s: DesktopApp._materialized_meeting_dir(c, s)
        return c

    def _write_meta(self, d, status="ok"):
        from dataclasses import asdict
        from whisper_core.meeting.session import MeetingMeta, atomic_write_json
        meta = MeetingMeta(schema=2, id="s", created=1, status="done",
                           preset="onlymic", sources=["mic"], audio_files={},
                           screen_status=status)
        atomic_write_json(d / "meeting.json", asdict(meta))

    def _video(self, d):
        return DesktopApp.meeting_screen_video(self._controller(d), "s")

    def test_finds_new_webm(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); (d / "screen.webm").write_bytes(b"x"); self._write_meta(d)
            self.assertEqual(self._video(d).name, "screen.webm")

    def test_finds_legacy_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); (d / "screen.mp4").write_bytes(b"x"); self._write_meta(d)
            self.assertEqual(self._video(d).name, "screen.mp4")

    def test_prefers_webm_over_legacy_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "screen.webm").write_bytes(b"x"); (d / "screen.mp4").write_bytes(b"x")
            self._write_meta(d)
            self.assertEqual(self._video(d).name, "screen.webm")

    def test_none_when_no_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); self._write_meta(d)
            self.assertIsNone(self._video(d))

    def test_none_when_screen_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp); (d / "screen.webm").write_bytes(b"x")
            self._write_meta(d, status="failed")
            self.assertIsNone(self._video(d))


class ModelCardDestructiveTests(unittest.TestCase):
    """Аудит Миколи 22.07: видалення завантаженої моделі й повторне завантаження —
    деструктив, тож із QMessageBox-підтвердженням (канон DESIGN-SYSTEM §1.5).
    Тонкі обробники перевіряємо unbound-викликом на стабі; підтвердження —
    через патч кастомного хелпера _meeting_confirm (True=«Так» / False=«Ні»)."""

    def _resolved(self):
        return SimpleNamespace(id="fast",
                               model_path=Path("/models/fast/model.gguf"))

    def _confirm(self, answer):
        return patch("fronts.desktop.pages.meeting._meeting_confirm",
                     lambda *a, **k: answer)

    def test_delete_skips_on_no(self):
        from fronts.desktop.pages import meeting as mp
        from whisper_core.protocol import model_manager as mm
        deleted, refreshed = [], []
        page = SimpleNamespace(_refresh_model_list=lambda: refreshed.append(True))
        with self._confirm(False), \
                patch.object(mm, "delete_model", lambda d: deleted.append(d)):
            mp.MeetingPage._delete_downloaded(page, self._resolved(), "Швидка")
        self.assertEqual(deleted, [])           # «Ні» → нічого не стерли
        self.assertEqual(refreshed, [])

    def test_delete_removes_on_yes(self):
        from fronts.desktop.pages import meeting as mp
        from whisper_core.protocol import model_manager as mm
        deleted, refreshed = [], []
        page = SimpleNamespace(_refresh_model_list=lambda: refreshed.append(True))
        with self._confirm(True), \
                patch.object(mm, "delete_model", lambda d: deleted.append(d)):
            mp.MeetingPage._delete_downloaded(page, self._resolved(), "Швидка")
        self.assertEqual(deleted, [Path("/models/fast")])
        self.assertEqual(refreshed, [True])

    def test_redownload_skips_on_no(self):
        from fronts.desktop.pages import meeting as mp
        from whisper_core.protocol import model_manager as mm
        deleted, opened = [], []
        page = SimpleNamespace(
            _open_download_dialog=lambda r, c, force: opened.append((r, c, force)))
        with self._confirm(False), \
                patch.object(mm, "delete_model", lambda d: deleted.append(d)):
            mp.MeetingPage._redownload_model(page, self._resolved(), None, "Швидка")
        self.assertEqual(deleted, [])           # «Ні» → ні видалення, ні докачки
        self.assertEqual(opened, [])

    def test_redownload_staged_replaces_without_deleting_on_yes(self):
        """Виправлення 2: «Так» НЕ стирає старий файл наперед — запускає staged-докачку
        (force=True), яка підмінить модель лише ПІСЛЯ успіху. Другого consent нема."""
        from fronts.desktop.pages import meeting as mp
        from whisper_core.protocol import model_manager as mm
        deleted, opened = [], []
        resolved = self._resolved()
        page = SimpleNamespace(
            _open_download_dialog=lambda r, c, force: opened.append((r, c, force)))
        with self._confirm(True), \
                patch.object(mm, "delete_model", lambda d: deleted.append(d)):
            mp.MeetingPage._redownload_model(page, resolved, None, "Швидка")
        self.assertEqual(deleted, [])                       # старий файл НЕ стерто
        self.assertEqual(opened, [(resolved, None, True)])  # staged-докачка force=True


class StaleMeetingTempCleanupTests(unittest.TestCase):
    """Blocker: краш під час наради лишає РОЗШИФРОВАНІ media/worker-теки у %TEMP%.
    DPAPI-сховище авто-розблоковується (ensure_dek без діалогу), тож прибирання,
    прив'язане до unlock-діалогу, їх не зачепить — старт застосунку мусить чистити
    стухлі balachky-meeting-* теки БЕЗУМОВНО."""

    def test_cleanup_removes_stale_but_keeps_fresh(self):
        import os as _os
        import shutil
        import time as _time
        from fronts.desktop import app as app_module
        stale = Path(tempfile.mkdtemp(prefix="balachky-meeting-media-"))
        fresh = Path(tempfile.mkdtemp(prefix="balachky-meeting-"))
        (stale / "transcript.txt").write_text("чутливе", encoding="utf-8")
        try:
            old = _time.time() - 7200            # 2 год — старше за поріг (1 год)
            _os.utime(stale, (old, old))
            app_module._cleanup_stale_meeting_temps()
            self.assertFalse(stale.exists())     # стуха прибрана
            self.assertTrue(fresh.exists())      # свіжа (щойно створена) лишилась
        finally:
            shutil.rmtree(stale, ignore_errors=True)
            shutil.rmtree(fresh, ignore_errors=True)

    def test_startup_wires_unconditional_cleanup(self):
        """Прибирання стоїть у стартовій ініціалізації (поряд із watch-config), а не
        ЛИШЕ в unlock-діалозі — інакше DPAPI-дефолт лишає plaintext-темпи після краху."""
        text = (Path(__file__).resolve().parent.parent
                / "fronts" / "desktop" / "app.py").read_text(encoding="utf-8")
        anchor = "self._apply_watch_config()"     # унікальний рядок __init__
        self.assertIn(anchor, text)
        idx = text.index(anchor)
        self.assertIn("_cleanup_stale_meeting_temps()", text[idx:idx + 400])


class IntegrityJournalLinesTests(unittest.TestCase):
    """Блокер Т56, косметика: рядок-маркер {"_corrupt": True} у журналі НЕ має
    рендеритися як подія 1970 року (ts=0). Замість нього — один чесний рядок
    «Пошкоджений запис». Юніт на функцію підготовки рядків (без живого діалогу)."""

    def _labels(self):
        from whisper_core.meeting import audit_log
        return {audit_log.EVENT_CREATED: "Created", audit_log.EVENT_STOPPED: "Stopped"}

    def test_corrupt_marker_not_rendered_as_1970(self):
        from fronts.desktop.pages import meeting as mpage
        from whisper_core.meeting import audit_log
        events = [
            {"seq": 0, "type": audit_log.EVENT_CREATED, "ts": 1_700_000_000.0},
            {"_corrupt": True},
        ]
        lines = mpage._audit_journal_lines(events, self._labels())
        joined = "\n".join(lines)
        self.assertNotIn("1970", joined)               # маркер не показано як 1970-рік
        self.assertEqual(len(lines), 2)
        from fronts.desktop.i18n import tr
        self.assertIn(tr("meeting_audit_corrupt_row"), lines)

    def test_valid_events_render_with_timestamp(self):
        from fronts.desktop.pages import meeting as mpage
        from whisper_core.meeting import audit_log
        events = [{"seq": 0, "type": audit_log.EVENT_CREATED, "ts": 1_700_000_000.0}]
        lines = mpage._audit_journal_lines(events, self._labels())
        self.assertEqual(len(lines), 1)
        self.assertIn("Created", lines[0])
        self.assertIn("[", lines[0])                    # мітка часу присутня


class MeetingExportMenuAndDialogsTests(unittest.TestCase):
    """Тести для згрупованого випадаючого меню «Експортувати в…», доступності (accessibleName)
    та кастомних діалогів повідомлень без системних іконок Qt."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            cls.app = QApplication([])

    def test_export_menu_and_dialog_helpers(self):
        import unittest.mock
        from fronts.desktop.pages import meeting as mpage
        from fronts.desktop import i18n
        i18n.set_language("uk")

        # 1. Перевірка наявності ключів i18n
        self.assertEqual(i18n.tr("meeting_export_menu"), "Експортувати в…")
        self.assertEqual(i18n.tr("meeting_evidence_group_title"), "ДОКАЗОВІСТЬ ТА ЦІЛІСНІСТЬ")

        # 2. Хелпери ЗАВЖДИ йдуть через кастомний box.exec(), НІКОЛИ через статичні
        # QMessageBox.question/warning/information (ті показали б системний модальний
        # діалог із нелокалізованими кнопками — і зависли б в offscreen назавжди).
        # Статичні методи замокані на виняток: якщо регресія поверне їх — тест
        # ВПАДЕ (а не зависне на реальному модальному вікні).
        from PySide6.QtWidgets import QMessageBox

        def _forbidden(*a, **k):
            raise AssertionError("статичний QMessageBox викликано замість _show_meeting_box")

        with unittest.mock.patch.object(QMessageBox, 'question', _forbidden), \
                unittest.mock.patch.object(QMessageBox, 'warning', _forbidden), \
                unittest.mock.patch.object(QMessageBox, 'information', _forbidden):
            with unittest.mock.patch.object(
                    QMessageBox, 'exec',
                    return_value=QMessageBox.StandardButton.Yes):
                res = mpage._meeting_confirm(None, "Тест", "Текст підтвердження")
                self.assertTrue(res)
            with unittest.mock.patch.object(
                    QMessageBox, 'exec',
                    return_value=QMessageBox.StandardButton.No):
                self.assertFalse(
                    mpage._meeting_confirm(None, "Тест", "Текст підтвердження"))
            with unittest.mock.patch.object(
                    QMessageBox, 'exec',
                    return_value=QMessageBox.StandardButton.Ok):
                mpage._meeting_warn(None, "Увага", "Текст попередження")
                mpage._meeting_info(None, "Інфо", "Інформаційний текст")

        # 3. Локалізовані підписи кнопок діалогу існують в обох словниках.
        self.assertEqual(i18n.tr("dialog_yes"), "Так")
        self.assertEqual(i18n.tr("dialog_no"), "Ні")
        self.assertEqual(i18n.tr("dialog_ok"), "Гаразд")


if __name__ == "__main__":
    unittest.main()
