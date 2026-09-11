"""«Спробувати іншою моделлю» для наради — issue #16 (за судом: BLOCK усунено).

Повторно розпізнати збережений запис наради ІНШОЮ моделлю, не втративши
правок людини (write_meeting_transcript пише лише transcript.txt і НЕ чіпає
transcript.json — саме тому проста заміна файлів знищила б правку). Результат
живе В ОКРЕМИХ артефактах transcript-<модель>.json/.txt поруч з оригіналом.

Блокер 1 (суд): важкий ASR тепер у ФОНОВОМУ потоці
(DesktopApp.start_retranscribe_meeting → _retranscribe_meeting_worker), а не
синхронно зі слота GUI-потоку. Фінал (ok/vault/missing/busy/fail) картка
дізнається ВИКЛЮЧНО через сигнал meeting_retranscribe_done.

Блокер 2 (суд): тимчасова тека повторного прогону починається з
MEETING_TEMP_PREFIX ("balachky-meeting-") — того самого префікса, що прибирає
й планова чистка, і панічне блокування, і деінсталятор.

Блокер 3 (суд): перемикач версій на картці — не декоративний: вибір
(``body._version``/``body._text``) читають копіювання, УСІ формати експорту
(через ``stem``) і панель редагування.

Чотири шари, як tests/test_meeting_subtitles.py: контролер (_retranscribe_
meeting_worker/start_retranscribe_meeting читають аудіо й пишуть результат
через read_artifact/write_artifact — шифрована сесія не розпаковується на
диск), сторінка (MeetingPage._retranscribe_menu/_start_retranscribe/
_switch_version/_on_retranscribe_done) і i18n.
"""
import json
import threading
import unittest
import wave
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.app import DesktopApp, MEETING_TEMP_PREFIX
from fronts.desktop.i18n import STRINGS, tr
from fronts.desktop.pages import meeting as meeting_page
from whisper_core.meeting import meeting_pipeline as pipeline
from whisper_core.meeting.session import MeetingMeta
from whisper_core.meeting.storage_crypto import VaultPasswordRequired


SESSION_ID = "2026-07-19_12-00-00"


def _wav_bytes(word: str = "x", *, seconds: float = 0.5, rate: int = 16000) -> bytes:
    buf = BytesIO()
    frames = max(1, int(seconds * rate))
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


class _FakeEngine:
    """Рушій, що завжди повертає ОДНЕ й те саме слово — досить, щоб відрізнити
    другий прогін від першого. Форма кортежу — як у справжнього рушія."""

    def __init__(self, word: str):
        self.word = word
        self.closed = False

    def transcribe(self, path, terms, *, include_word_timestamps=False):
        w = self.word
        timed = [{"start": 0.0, "end": 0.4, "word": w}]
        return (w, w, 0.4, [(w, 0.9)], [(0.0, 0.4, w)], timed)

    def close(self):
        self.closed = True


class _FakeStore:
    """Мінімальне сховище артефактів у пам'яті — так само мокає read_artifact/
    write_artifact, як ControllerTests у test_meeting_subtitles.py, але тут ще
    й ЗАПИС потрібно перевірити (нові артефакти не мають зачепити старі)."""

    def __init__(self, initial: dict):
        self.data = dict(initial)
        self.writes = {}

    def read(self, _session_dir, name):
        try:
            return self.data[str(name)]
        except KeyError:
            raise FileNotFoundError(name)

    def write(self, session_dir, name, data):
        self.data[str(name)] = data
        self.writes[str(name)] = data
        return Path(session_dir) / str(name)


class _Signal:
    """Дублер Qt Signal — записує кожен emit() для перевірки в тестах."""

    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def _meta(audio_files, *, speaker_names=None) -> MeetingMeta:
    return MeetingMeta(
        schema=2, id=SESSION_ID, created=1700000000, status="done",
        preset="onlymic", sources=list(audio_files),
        audio_files=audio_files, speaker_names=speaker_names or {})


def _base_store(word="перший-прогін", *, audio_files=None, model_name="large-v3-turbo"):
    audio_files = audio_files or {"mic": ["mic-0001.wav"]}
    meta = _meta(audio_files)
    original = [
        {"start": 0.0, "end": 0.4, "speaker": "single", "source": "mic", "text": word},
    ]
    store = _FakeStore({
        "meeting.json": meta.to_json().encode("utf-8"),
        "transcript.json": json.dumps(original, ensure_ascii=False).encode("utf-8"),
        "transcript.txt": (word + "\n").encode("utf-8"),
        **{name: _wav_bytes() for names in audio_files.values() for name in names},
    })
    return store, meta


def _fake_app(cfg_model="large-v3-turbo"):
    return SimpleNamespace(
        cfg=SimpleNamespace(model_name=cfg_model, language="uk", device="cpu",
                            compute_type="int8"),
        terms=None,
        _engine_lock=threading.Lock(),
        _model_lifecycle=None,
        _meeting_session_dir=lambda sid: Path("не-використовується") / sid,
        meeting_vault_needed=_Signal(),
        meeting_retranscribe_done=_Signal(),
        meeting_retranscribe_progress=_Signal(),
        _clear_meeting_plain_cache=lambda sid: None,
        _meeting_retranscribe_jobs={},
        _meeting_processing_jobs={},
        _meeting_active=False,
        has_model=True,
        tray=SimpleNamespace(notify=lambda *a, **k: None),
        installed_model_names=lambda: ["large-v3-turbo", "друга-модель"],
    )


def _run_worker(store, model_name="друга-модель", app=None):
    """Викликає _retranscribe_meeting_worker СИНХРОННО (без потоку) — саме
    тіло worker-логіки, ізольоване від threading.Thread/start_retranscribe_
    meeting, які тестуються окремо (StartRetranscribeMeetingTests)."""
    app = app or _fake_app()
    engine = _FakeEngine("слово-нової-моделі")
    session_dir = app._meeting_session_dir(SESSION_ID)
    token = pipeline.CancelToken()
    # _meeting_session_dir тут навмисно вказує на неіснуючу теку (як і в
    # решті тестів файлу) — N1 (журнал цілісності) інакше писав би СПРАВЖНІЙ
    # audit-лок/файл під цим відносним шляхом у робочій директорії тестового
    # прогону. _audit_event тестуємо ОКРЕМО (test_n1_audit_event...).
    with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read), \
         patch("whisper_core.meeting.session.write_artifact", side_effect=store.write), \
         patch("fronts.desktop.app.make_engine", return_value=engine), \
         patch("fronts.desktop.app._audit_event"):
        DesktopApp._retranscribe_meeting_worker(
            app, SESSION_ID, model_name, session_dir, token)
    return app, engine


class RetranscribeWorkerTests(unittest.TestCase):
    def test_new_run_does_not_overwrite_existing_transcript(self):
        store, _meta_obj = _base_store("перший-прогін")
        before_json = store.data["transcript.json"]
        before_txt = store.data["transcript.txt"]
        app, _engine = _run_worker(store, "друга-модель")
        self.assertEqual(app.meeting_retranscribe_done.calls,
                         [(SESSION_ID, "друга-модель", "ok")])
        self.assertEqual(store.data["transcript.json"], before_json)
        self.assertEqual(store.data["transcript.txt"], before_txt)
        self.assertIn("transcript-друга-модель.json", store.data)
        self.assertIn("transcript-друга-модель.txt", store.data)

    def test_human_edit_stays_readable_after_second_run(self):
        store, _meta_obj = _base_store("перший-прогін")
        # Людина відредагувала transcript.txt руками — текст уже НЕ збігається
        # з тим, що дав би відбудований transcript.json (write_meeting_transcript
        # чіпає лише .txt, точнісінько так само).
        edited = "Це моя відредагована версія тексту.\n"
        store.data["transcript.txt"] = edited.encode("utf-8")
        app, _engine = _run_worker(store, "друга-модель")
        self.assertEqual(app.meeting_retranscribe_done.calls[-1][2], "ok")
        self.assertEqual(store.data["transcript.txt"].decode("utf-8"), edited)

    def test_switching_versions_gives_different_text(self):
        store, _meta_obj = _base_store("перший-прогін")
        app, engine = _run_worker(store, "друга-модель")
        self.assertEqual(app.meeting_retranscribe_done.calls[-1][2], "ok")
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read):
            original = DesktopApp.meeting_transcript_text(app, SESSION_ID, None)
            other = DesktopApp.meeting_transcript_text(app, SESSION_ID, "друга-модель")
        self.assertIn("перший-прогін", original)
        self.assertIn(engine.word, other)
        self.assertNotEqual(original, other)

    def test_provenance_of_second_run_has_finished_at_and_different_model(self):
        """N3: часова позначка прогону в provenance-<модель>.json — версії
        впорядковуються за нею (мутація: видалити рядок
        ``provenance["finished_at"] = time.time()`` — тест падає з KeyError)."""
        store, _meta_obj = _base_store("перший-прогін", model_name="large-v3-turbo")
        app, _engine = _run_worker(store, "друга-модель")
        self.assertEqual(app.meeting_retranscribe_done.calls[-1][2], "ok")
        provenance = json.loads(store.data["provenance-друга-модель.json"].decode("utf-8"))
        self.assertEqual(provenance["model"], "друга-модель")
        self.assertNotEqual(provenance["model"], "large-v3-turbo")
        self.assertEqual(provenance["engine"], "faster-whisper")
        self.assertIn("finished_at", provenance)

    def test_n1_audit_event_and_cache_clear_on_success(self):
        """N1: подія журналу цілісності "retranscribed" (за зразком "edited")
        і скидання plaintext-кешу картки — інакше картка після успішного
        повторного прогону й далі показувала б застарілий стан (мутація:
        прибрати виклик _audit_event/_clear_meeting_plain_cache — тест падає,
        бо жоден з моків не отримує виклику)."""
        store, _meta_obj = _base_store("перший-прогін")
        app = _fake_app()
        cache_cleared = []
        app._clear_meeting_plain_cache = lambda sid: cache_cleared.append(sid)
        session_dir = app._meeting_session_dir(SESSION_ID)
        token = pipeline.CancelToken()
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read), \
             patch("whisper_core.meeting.session.write_artifact", side_effect=store.write), \
             patch("fronts.desktop.app.make_engine", return_value=_FakeEngine("х")), \
             patch("fronts.desktop.app._audit_event") as audit_event:
            DesktopApp._retranscribe_meeting_worker(
                app, SESSION_ID, "друга-модель", session_dir, token)
        self.assertEqual(app.meeting_retranscribe_done.calls[-1][2], "ok")
        audit_event.assert_called_once()
        call_args = audit_event.call_args
        self.assertEqual(call_args.args[1], "retranscribed")
        self.assertEqual(call_args.kwargs["note"]["model"], "друга-модель")
        self.assertEqual(call_args.kwargs["note"]["pipeline"], pipeline.PIPELINE_VERSION)
        self.assertEqual(cache_cleared, [SESSION_ID])

    def test_missing_audio_gives_honest_reason_not_crash(self):
        """Мутація (перевірено вручну байтовою копією файлу, див. звіт):
        насправді цей сценарій (один WAV зник з диска) ловить
        ``except FileNotFoundError: reason = "missing"; return`` навколо
        копіювання в тимчасову теку — саме там read_artifact падає для
        зниклого relative-шляху. Прибравши ЦЕЙ except (замінивши на
        ``pass``) РАЗОМ із наступним ``if not scratch_audio``, тест падає з
        AttributeError у make_engine замість чесної причини "missing" —
        обидва рядки-запобіжники підтверджено потрібними. Сам по собі
        ``if not scratch_audio`` тут НЕ є вирішальним (заготовлений про
        запас на випадок audio_files з порожнім списком relatives для
        доріжки — сценарій, не покритий жодним тестом окремо)."""
        store, _meta_obj = _base_store("перший-прогін")
        del store.data["mic-0001.wav"]     # аудіо «зникло» з диска
        app, _engine = _run_worker(store, "друга-модель")
        self.assertEqual(app.meeting_retranscribe_done.calls,
                         [(SESSION_ID, "друга-модель", "missing")])
        self.assertNotIn("transcript-друга-модель.json", store.data)

    def test_no_audio_files_at_all_gives_honest_reason(self):
        meta = _meta({})
        store = _FakeStore({"meeting.json": meta.to_json().encode("utf-8")})
        app, _engine = _run_worker(store, "друга-модель")
        self.assertEqual(app.meeting_retranscribe_done.calls,
                         [(SESSION_ID, "друга-модель", "missing")])

    def test_encrypted_style_session_uses_read_and_write_artifact_only(self):
        """Вимога звіту п.4: читання й запис ідуть ТІЛЬКИ через read_artifact/
        write_artifact (ті самі шляхи, що meeting_subtitles) — жодного прямого
        читання оригінальної (умовно зашифрованої) сесії з диска в обхід."""
        store, _meta_obj = _base_store("перший-прогін")
        app = _fake_app()
        session_dir = app._meeting_session_dir(SESSION_ID)
        token = pipeline.CancelToken()
        with patch("whisper_core.meeting.session.read_artifact",
                   side_effect=store.read) as read_mock, \
             patch("whisper_core.meeting.session.write_artifact",
                   side_effect=store.write) as write_mock, \
             patch("fronts.desktop.app.make_engine",
                   return_value=_FakeEngine("нова-модель-слово")), \
             patch("fronts.desktop.app._audit_event"):
            DesktopApp._retranscribe_meeting_worker(
                app, SESSION_ID, "друга-модель", session_dir, token)
        self.assertEqual(app.meeting_retranscribe_done.calls[-1][2], "ok")
        self.assertGreater(read_mock.call_count, 0)
        written_names = {call.args[1] for call in write_mock.call_args_list}
        self.assertIn("transcript-друга-модель.json", written_names)
        self.assertIn("transcript-друга-модель.txt", written_names)
        self.assertIn("provenance-друга-модель.json", written_names)

    def test_locked_vault_emits_vault_signal_no_raise(self):
        app = _fake_app()
        session_dir = app._meeting_session_dir(SESSION_ID)
        token = pipeline.CancelToken()
        with patch("whisper_core.meeting.session.read_artifact",
                   side_effect=VaultPasswordRequired()):
            DesktopApp._retranscribe_meeting_worker(
                app, SESSION_ID, "друга-модель", session_dir, token)
        self.assertEqual(app.meeting_vault_needed.calls, [()])
        self.assertEqual(app.meeting_retranscribe_done.calls,
                         [(SESSION_ID, "друга-модель", "vault")])

    def test_asr_failure_gives_fail_reason(self):
        """process_meeting повернув неуспішний статус → чесна причина "fail",
        а не мовчазний крах чи "ok" (мутація: змінити
        ``if result.status not in ("complete", "partial")`` на ``if False``
        — тест падає, бо реєструється "ok" попри cancelled-статус)."""
        store, _meta_obj = _base_store("перший-прогін")
        app = _fake_app()
        session_dir = app._meeting_session_dir(SESSION_ID)
        token = pipeline.CancelToken()
        fake_result = SimpleNamespace(status="cancelled", word_count=0)
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read), \
             patch("whisper_core.meeting.session.write_artifact", side_effect=store.write), \
             patch("fronts.desktop.app.make_engine", return_value=_FakeEngine("х")), \
             patch("whisper_core.meeting.meeting_pipeline.process_meeting",
                   return_value=fake_result):
            DesktopApp._retranscribe_meeting_worker(
                app, SESSION_ID, "друга-модель", session_dir, token)
        self.assertEqual(app.meeting_retranscribe_done.calls,
                         [(SESSION_ID, "друга-модель", "fail")])
        self.assertNotIn("transcript-друга-модель.json", store.data)

    def test_finally_emits_done_even_after_early_return(self):
        """Регресія: emit() живе у ``finally``, а не одразу після try/except —
        інакше ранні ``return`` (vault/missing) обривали б worker ще ДО
        сигналу, і картка НІКОЛИ не дізналась би про фінал (кнопка "Спробувати
        іншою моделлю" лишалась би вимкненою назавжди). Перевірено тут через
        сценарій "missing" — done.calls мусить бути НЕПОРОЖНІМ."""
        meta = _meta({})
        store = _FakeStore({"meeting.json": meta.to_json().encode("utf-8")})
        app, _engine = _run_worker(store, "друга-модель")
        self.assertTrue(app.meeting_retranscribe_done.calls)

    def test_temp_dir_prefix_matches_panic_cleanup_prefix(self):
        """Блокер 2 (суд): тимчасова тека повторного прогону мусить починатись
        із MEETING_TEMP_PREFIX — САМЕ той префікс, що прибирають і планова
        чистка, і панічне блокування (_cleanup_panic_plaintext_temps), і
        деінсталятор (installer/balachky.iss:130). Літерал НЕ дублюємо:
        константу читаємо з app.py, щоб майбутня розсинхронізація префіксів
        червонила саме тут."""
        store, _meta_obj = _base_store("перший-прогін")
        seen = {}
        real_tempdir = __import__("tempfile").TemporaryDirectory

        class _Recording:
            def __init__(self, prefix=None, **kw):
                seen["prefix"] = prefix
                self._inner = real_tempdir(prefix=prefix, **kw)

            def __enter__(self):
                return self._inner.__enter__()

            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        with patch("fronts.desktop.app.tempfile.TemporaryDirectory", _Recording):
            _run_worker(store, "друга-модель")
        self.assertIsNotNone(seen.get("prefix"))
        self.assertTrue(seen["prefix"].startswith(MEETING_TEMP_PREFIX))


class StartRetranscribeMeetingTests(unittest.TestCase):
    """Блокер 1 (суд): важкий ASR у фоновому потоці, не в GUI-потоці."""

    def test_returns_before_worker_finishes_then_done_signal_arrives(self):
        """Тест з зубами: worker блокується на threading.Event, поки тест не
        відпустить його. start_retranscribe_meeting МАЄ повернутись ДО того,
        як подія відбулась (перевіряємо прапорець worker_started, а НЕ сам
        факт завершення) — інакше це й досі синхронний виклик у GUI-потоці
        (мутація: замінити start_retranscribe_meeting на прямий виклик
        _retranscribe_meeting_worker без Thread — цей тест зависає/падає,
        бо started_flag ще не встановлено на момент return)."""
        app = _fake_app()
        gate = threading.Event()
        started_flag = threading.Event()

        # SimpleNamespace, не інстанс DesktopApp — self._retranscribe_meeting_
        # worker(...) шукає атрибут ПРЯМО на app, тож підміняємо саме його
        # (не клас DesktopApp), функцією без параметра self.
        def blocking_worker(session_id, model_name, session_dir, token):
            started_flag.set()
            gate.wait(timeout=5)
            app.meeting_retranscribe_done.emit(session_id, model_name, "ok")
            app._meeting_retranscribe_jobs.pop(session_id, None)

        app._retranscribe_meeting_worker = blocking_worker
        started = DesktopApp.start_retranscribe_meeting(app, SESSION_ID, "друга-модель")
        self.assertTrue(started)
        # Ключова перевірка: worker МІГ ще не встигнути стартувати взагалі,
        # але виклик start_... уже повернувся — доказ фонового потоку.
        self.assertFalse(app.meeting_retranscribe_done.calls)
        started_flag.wait(timeout=5)
        self.assertTrue(started_flag.is_set())
        self.assertTrue(DesktopApp.retranscribe_active(app, SESSION_ID))
        gate.set()
        # дочекатись worker-потоку без сну в циклі
        for _ in range(200):
            if app.meeting_retranscribe_done.calls:
                break
            threading.Event().wait(0.01)
        self.assertEqual(app.meeting_retranscribe_done.calls,
                         [(SESSION_ID, "друга-модель", "ok")])

    def test_busy_second_start_gives_busy_reason_without_second_thread(self):
        """Другий старт тієї ж наради, поки перший ще триває, — чесна причина
        "busy" (а не друга спроба запустити рушій паралельно)."""
        app = _fake_app()
        gate = threading.Event()
        thread_starts = []

        def blocking_worker(session_id, model_name, session_dir, token):
            thread_starts.append(1)
            gate.wait(timeout=5)
            app.meeting_retranscribe_done.emit(session_id, model_name, "ok")
            app._meeting_retranscribe_jobs.pop(session_id, None)

        app._retranscribe_meeting_worker = blocking_worker
        first = DesktopApp.start_retranscribe_meeting(app, SESSION_ID, "друга-модель")
        self.assertTrue(first)
        second = DesktopApp.start_retranscribe_meeting(app, SESSION_ID, "третя-модель")
        self.assertFalse(second)
        gate.set()
        for _ in range(200):
            if app.meeting_retranscribe_done.calls:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(thread_starts), 1)   # рушій запущено ОДИН раз
        self.assertIn((SESSION_ID, "третя-модель", "busy"),
                      app.meeting_retranscribe_done.calls)

    def test_model_absent_refuses_without_thread(self):
        app = _fake_app()
        app.has_model = False
        notified = []
        app.tray = SimpleNamespace(notify=lambda msg: notified.append(msg))
        with patch.object(DesktopApp, "_retranscribe_meeting_worker") as worker:
            started = DesktopApp.start_retranscribe_meeting(app, SESSION_ID, "друга-модель")
        self.assertFalse(started)
        worker.assert_not_called()
        self.assertTrue(notified)

    def test_retranscribe_active_reflects_jobs(self):
        app = _fake_app()
        self.assertFalse(DesktopApp.retranscribe_active(app, SESSION_ID))
        app._meeting_retranscribe_jobs[SESSION_ID] = object()
        self.assertTrue(DesktopApp.retranscribe_active(app, SESSION_ID))


class TranscriptEditedTests(unittest.TestCase):
    def test_detects_human_edit(self):
        store, _meta_obj = _base_store("перший-прогін")
        store.data["transcript.txt"] = "інший текст, не той, що в json\n".encode("utf-8")
        app = _fake_app()
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read):
            self.assertTrue(DesktopApp.meeting_transcript_edited(app, SESSION_ID))

    def test_no_edit_when_txt_matches_json(self):
        store, meta = _base_store("текст-без-правок")
        from whisper_core.meeting import postprocess as mpost
        data = json.loads(store.data["transcript.json"].decode("utf-8"))
        utterances = [mpost.Utterance(**item) for item in data]
        rebuilt = mpost.to_transcript_text(
            utterances, me_label=tr("meeting_speaker_me"),
            others_label=tr("meeting_speaker_others"), speaker_names=meta.speaker_names)
        store.data["transcript.txt"] = rebuilt.encode("utf-8")
        app = _fake_app()
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read):
            self.assertFalse(DesktopApp.meeting_transcript_edited(app, SESSION_ID))


class MeetingAudioAvailableTests(unittest.TestCase):
    def test_true_when_wav_present(self):
        store, _meta_obj = _base_store("x")
        app = _fake_app()
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read):
            self.assertTrue(DesktopApp.meeting_audio_available(app, SESSION_ID))

    def test_false_when_wav_missing(self):
        store, _meta_obj = _base_store("x")
        del store.data["mic-0001.wav"]
        app = _fake_app()
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read):
            self.assertFalse(DesktopApp.meeting_audio_available(app, SESSION_ID))


class MeetingOriginalModelTests(unittest.TestCase):
    """N4: exclusion of the already-used model from the retry menu relies on
    reading it back out of the first word-ledger record."""

    def _store_with_ledger(self, model="large-v3-turbo"):
        store, meta = _base_store("x")
        record = {"word": "x", "start": 0.0, "end": 0.1,
                  "asr_provenance": {"model": model}}
        store.data["words.mic.jsonl"] = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        return store

    def test_reads_model_from_ledger(self):
        store = self._store_with_ledger("large-v3-turbo")
        app = _fake_app()
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read):
            self.assertEqual(
                DesktopApp.meeting_original_model(app, SESSION_ID), "large-v3-turbo")

    def test_none_when_ledger_missing(self):
        store, _meta_obj = _base_store("x")
        app = _fake_app()
        with patch("whisper_core.meeting.session.read_artifact", side_effect=store.read):
            self.assertIsNone(DesktopApp.meeting_original_model(app, SESSION_ID))


class _Body:
    """Заміна TranscriptViewer для тестів сторінки — лише setText потрібен."""

    def __init__(self):
        self.text = None
        self._text = None
        self._version = None

    def setText(self, text):
        self.text = text


class _FakeWidget:
    """Дублер QLabel/GlassButton — лише setVisible/setEnabled потрібні."""

    def __init__(self):
        self.visible = True
        self.enabled = True

    def setVisible(self, value):
        self.visible = value

    def setEnabled(self, value):
        self.enabled = value


class _RecordingTriggered:
    """На відміну від _FakeAction у test_dictation_card_copy.py (connect —
    no-op): тут ЗБЕРІГАЄМО підключений колбек, щоб N2 міг фактично викликати
    trigger() другого пункту меню й перевірити, ЯКУ модель він передає."""

    def __init__(self):
        self._slot = None

    def connect(self, slot):
        self._slot = slot

    def emit(self, checked=False):
        if self._slot is not None:
            self._slot(checked)


class _FakeAction:
    """Дублер QAction — як _FakeAction у test_dictation_card_copy.py, плюс
    справжній trigger() (див. _RecordingTriggered) для N2."""

    def __init__(self, text=""):
        self._text = text
        self._enabled = True
        self.triggered = _RecordingTriggered()

    def setEnabled(self, value):
        self._enabled = value

    def isEnabled(self):
        return self._enabled

    def text(self):
        return self._text

    def trigger(self):
        self.triggered.emit(False)


class _FakeMenu:
    """Дублер QMenu (як у test_dictation_card_copy.py/test_processing_recovery):
    справжній QMenu.exec() блокує прогін тесту на показі меню (модальний
    цикл подій, що нічим не закривається під offscreen-платформою) — тож
    підміняємо весь клас QMenu у модулі сторінки, а не патчимо exec точково."""
    last = None

    def __init__(self, *a, **k):
        self._actions = []
        _FakeMenu.last = self

    def addAction(self, text=""):
        a = _FakeAction(text)
        self._actions.append(a)
        return a

    def actions(self):
        return self._actions

    def exec(self, *a, **k):
        pass


class PageMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication, QWidget
        if QApplication.instance() is None:
            cls.app = QApplication([])
        cls.QWidget = QWidget

    def test_empty_model_list_gives_disabled_menu_item(self):
        controller = SimpleNamespace(
            meeting_audio_available=lambda sid: True,
            installed_model_names=lambda: [],
            meeting_original_model=lambda sid: None)
        page = SimpleNamespace(controller=controller)
        anchor = self.QWidget()
        with patch.object(meeting_page, "QMenu", _FakeMenu):
            meeting_page.MeetingPage._retranscribe_menu(page, anchor, SESSION_ID)
        actions = _FakeMenu.last.actions()
        self.assertEqual(len(actions), 1)
        self.assertFalse(actions[0].isEnabled())
        # Жорсткий літерал, не tr(...) — вартовий тавтології (test_i18n_
        # tautology_lint.py) забороняє звіряти widget-вивід з tr(того самого
        # ключа): відсутній переклад зробив би обидві сторони однаково
        # «сирим» ключем і тест лишився б зеленим. Ключ і мова синхронізовані
        # I18nTests.test_keys_in_both_languages нижче.
        self.assertEqual(actions[0].text(), "Інших встановлених моделей немає")

    def test_missing_audio_shows_toast_without_building_menu(self):
        controller = SimpleNamespace(meeting_audio_available=lambda sid: False)
        page = SimpleNamespace(controller=controller)
        anchor = self.QWidget()
        with patch.object(meeting_page, "QMenu") as menu_cls, \
             patch.object(meeting_page.motion, "toast") as toast:
            meeting_page.MeetingPage._retranscribe_menu(page, anchor, SESSION_ID)
        menu_cls.assert_not_called()
        toast.assert_called_once()
        # Порівнювати аргумент з tr(...) напряму заборонено вартовим лінту
        # тавтології — перевіряємо, ЩО тост стався, а не буквальний рядок.
        self.assertEqual(toast.call_args.args[0], page)

    def test_two_models_give_two_menu_items_and_trigger_second(self):
        """N2 (мутація m6 вижила за судом): два пункти меню, trigger()
        другого викликає _start_retranscribe(sid, <друге ім'я>) — не перше й
        не якесь фіксоване."""
        calls = []
        controller = SimpleNamespace(
            meeting_audio_available=lambda sid: True,
            installed_model_names=lambda: ["модель-А", "модель-Б"],
            meeting_original_model=lambda sid: None)
        page = SimpleNamespace(
            controller=controller, _MODEL_LABEL_KEYS={},
            _start_retranscribe=lambda sid, model: calls.append((sid, model)))
        anchor = self.QWidget()
        with patch.object(meeting_page, "QMenu", _FakeMenu):
            meeting_page.MeetingPage._retranscribe_menu(page, anchor, SESSION_ID)
        actions = _FakeMenu.last.actions()
        self.assertEqual(len(actions), 2)
        actions[1].trigger()
        self.assertEqual(calls, [(SESSION_ID, "модель-Б")])

    def test_original_model_excluded_from_menu(self):
        """N4: активну модель (провенанс оригіналу) меню не пропонує."""
        controller = SimpleNamespace(
            meeting_audio_available=lambda sid: True,
            installed_model_names=lambda: ["модель-А", "модель-Б"],
            meeting_original_model=lambda sid: "модель-А")
        page = SimpleNamespace(controller=controller, _MODEL_LABEL_KEYS={})
        anchor = self.QWidget()
        with patch.object(meeting_page, "QMenu", _FakeMenu):
            meeting_page.MeetingPage._retranscribe_menu(page, anchor, SESSION_ID)
        actions = _FakeMenu.last.actions()
        labels = [a.text() for a in actions]
        self.assertEqual(labels, ["модель-Б"])

    def test_original_model_undeterminable_leaves_all_models(self):
        def boom(sid):
            raise Exception("boom")
        controller = SimpleNamespace(
            meeting_audio_available=lambda sid: True,
            installed_model_names=lambda: ["модель-А", "модель-Б"],
            meeting_original_model=boom)
        page = SimpleNamespace(controller=controller, _MODEL_LABEL_KEYS={})
        anchor = self.QWidget()
        with patch.object(meeting_page, "QMenu", _FakeMenu):
            meeting_page.MeetingPage._retranscribe_menu(page, anchor, SESSION_ID)
        actions = _FakeMenu.last.actions()
        self.assertEqual(len(actions), 2)

    def test_switch_version_sets_body_text_and_version(self):
        controller = SimpleNamespace(
            meeting_transcript_text=lambda sid, version: f"текст:{version}")
        page = SimpleNamespace(controller=controller)
        body = _Body()
        meeting_page.MeetingPage._switch_version(page, SESSION_ID, body, "друга-модель")
        self.assertEqual(body.text, "текст:друга-модель")
        self.assertEqual(body._text, "текст:друга-модель")
        self.assertEqual(body._version, "друга-модель")

    def test_switch_version_toggles_edit_panel_and_note(self):
        controller = SimpleNamespace(
            meeting_transcript_text=lambda sid, version: f"текст:{version}")
        page = SimpleNamespace(controller=controller)
        body = _Body()
        body._edit_panel = _FakeWidget()
        body._edit_button = _FakeWidget()
        body._edit_note = _FakeWidget()
        meeting_page.MeetingPage._switch_version(page, SESSION_ID, body, "друга-модель")
        self.assertFalse(body._edit_panel.visible)
        self.assertFalse(body._edit_button.enabled)
        self.assertTrue(body._edit_note.visible)
        meeting_page.MeetingPage._switch_version(page, SESSION_ID, body, None)
        self.assertTrue(body._edit_panel.visible)
        self.assertTrue(body._edit_button.enabled)
        self.assertFalse(body._edit_note.visible)

    def test_switch_version_failure_shows_toast_not_crash(self):
        def boom(sid, version):
            raise OSError("диск недоступний")
        controller = SimpleNamespace(meeting_transcript_text=boom)
        page = SimpleNamespace(controller=controller)
        body = _Body()
        with patch.object(meeting_page.motion, "toast") as toast:
            meeting_page.MeetingPage._switch_version(page, SESSION_ID, body, "друга-модель")
        toast.assert_called_once()
        self.assertIsNone(body.text)
        self.assertIsNone(body._version)   # не змінено при помилці

    def test_export_stem_matches_selected_version(self):
        page = SimpleNamespace(controller=None)
        body = _Body()
        self.assertEqual(meeting_page.MeetingPage._export_stem(page, body), "transcript")
        body._version = "друга-модель"
        self.assertEqual(meeting_page.MeetingPage._export_stem(page, body),
                         "transcript-друга-модель")

    def test_start_retranscribe_calls_controller_and_toasts_running(self):
        controller = SimpleNamespace(
            meeting_transcript_edited=lambda sid: False,
            start_retranscribe_meeting=lambda sid, model: True)
        page = SimpleNamespace(controller=controller, refresh=lambda: None)
        with patch.object(meeting_page.motion, "toast") as toast:
            meeting_page.MeetingPage._start_retranscribe(page, SESSION_ID, "друга-модель")
        toast.assert_called_once()   # лише "триває" — busy/fail приходять сигналом

    def test_start_retranscribe_busy_return_shows_no_running_toast(self):
        """start_retranscribe_meeting повернув False (напр. busy-guard) —
        _start_retranscribe НЕ показує "триває": причину дасть done-сигнал."""
        controller = SimpleNamespace(
            meeting_transcript_edited=lambda sid: False,
            start_retranscribe_meeting=lambda sid, model: False)
        page = SimpleNamespace(controller=controller, refresh=lambda: None)
        with patch.object(meeting_page.motion, "toast") as toast:
            meeting_page.MeetingPage._start_retranscribe(page, SESSION_ID, "друга-модель")
        toast.assert_not_called()

    def test_start_retranscribe_edits_note_shown_when_edited(self):
        controller = SimpleNamespace(
            meeting_transcript_edited=lambda sid: True,
            start_retranscribe_meeting=lambda sid, model: True)
        page = SimpleNamespace(controller=controller, refresh=lambda: None)
        with patch.object(meeting_page.motion, "toast") as toast:
            meeting_page.MeetingPage._start_retranscribe(page, SESSION_ID, "друга-модель")
        # "маєте правки" + "триває" = 2 тости, коли є ручні правки.
        self.assertEqual(toast.call_count, 2)

    _REASON_KEYS = meeting_page.MeetingPage._RETRANSCRIBE_REASON_KEYS

    def test_on_retranscribe_done_ok_toasts_and_refreshes(self):
        refreshed = []
        page = SimpleNamespace(refresh=lambda: refreshed.append(1),
                               _RETRANSCRIBE_REASON_KEYS=self._REASON_KEYS)
        with patch.object(meeting_page.motion, "toast") as toast:
            meeting_page.MeetingPage._on_retranscribe_done(
                page, SESSION_ID, "друга-модель", "ok")
        toast.assert_called_once()
        self.assertEqual(refreshed, [1])

    def test_on_retranscribe_done_vault_no_toast_but_refreshes(self):
        refreshed = []
        page = SimpleNamespace(refresh=lambda: refreshed.append(1),
                               _RETRANSCRIBE_REASON_KEYS=self._REASON_KEYS)
        with patch.object(meeting_page.motion, "toast") as toast:
            meeting_page.MeetingPage._on_retranscribe_done(
                page, SESSION_ID, "друга-модель", "vault")
        toast.assert_not_called()
        self.assertEqual(refreshed, [1])

    def test_on_retranscribe_done_busy_and_missing_and_fail_toast(self):
        for reason in ("busy", "missing", "fail"):
            page = SimpleNamespace(refresh=lambda: None,
                                   _RETRANSCRIBE_REASON_KEYS=self._REASON_KEYS)
            with patch.object(meeting_page.motion, "toast") as toast:
                meeting_page.MeetingPage._on_retranscribe_done(
                    page, SESSION_ID, "друга-модель", reason)
            toast.assert_called_once()


class I18nTests(unittest.TestCase):
    def test_keys_in_both_languages(self):
        keys = (
            "meeting_retry_model_menu", "meeting_retry_model_none",
            "meeting_retry_model_missing", "meeting_retry_model_busy",
            "meeting_retry_model_running", "meeting_retry_model_fail",
            "meeting_retry_model_done", "meeting_retry_model_edits_note",
            "meeting_version_menu", "meeting_version_original",
            "meeting_version_edit_locked",
        )
        for key in keys:
            for lang in ("uk", "en"):
                value = STRINGS[lang][key]
                self.assertTrue(value.strip())
                self.assertNotIn("«", value)
                self.assertNotIn("»", value)


if __name__ == "__main__":
    unittest.main()
