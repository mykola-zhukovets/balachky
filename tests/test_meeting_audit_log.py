"""Юніти журналу цілісності (chain-of-custody) наради — audit_log.py.

Без Qt, без реального аудіо: артефакти — звичайні файли у tempfile. Перевіряємо
хеш-ланцюг, детекцію підміни запису/аудіо, roundtrip і міграцію старих нарад.
"""
import builtins
from contextlib import contextmanager
import errno
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

from whisper_core.meeting import audit_log


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@contextmanager
def _workspace_temp_dir(prefix):
    path = Path.cwd() / f"{prefix}{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class AppendReadTests(unittest.TestCase):
    def test_append_creates_jsonl_and_reads_back(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED, note={"preset": "onlymic"})
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            events = audit_log.read_events(sess)
            self.assertEqual([e["type"] for e in events],
                             [audit_log.EVENT_CREATED, audit_log.EVENT_STOPPED])
            self.assertEqual([e["seq"] for e in events], [0, 1])
            self.assertEqual(events[0]["note"]["preset"], "onlymic")
            self.assertEqual(
                events[0]["note"]["_audit_head_policy"], 1)
            self.assertTrue((sess / "audit.jsonl").is_file())

    def test_each_record_carries_previous_hash(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            e0, e1 = audit_log.read_events(sess)
            self.assertEqual(e0["prev"], "")               # генезис
            self.assertEqual(e1["prev"], e0["hash"])       # ланцюг
            self.assertNotEqual(e0["hash"], e1["hash"])

    def test_append_is_pure_append_only(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            first = (sess / "audit.jsonl").read_text(encoding="utf-8")
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            second = (sess / "audit.jsonl").read_text(encoding="utf-8")
            self.assertTrue(second.startswith(first))       # старий рядок недоторканий


class VerifyChainTests(unittest.TestCase):
    def test_intact_chain_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)
            self.assertTrue(res.ok)
            self.assertEqual(res.event_count, 2)
            self.assertIsNone(res.broken_seq)

    def test_tampered_record_breaks_chain(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED, note={"preset": "onlymic"})
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            # Підміна вмісту першого запису БЕЗ перерахунку хеша.
            path = sess / "audit.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            rec = json.loads(lines[0])
            rec["note"] = {"preset": "both"}               # хтось «підправив» дані
            lines[0] = json.dumps(rec, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)
            self.assertFalse(res.ok)
            self.assertEqual(res.broken_seq, 0)

    def test_deleted_middle_record_breaks_chain(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            audit_log.append_event(sess, audit_log.EVENT_EXPORTED)
            path = sess / "audit.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            del lines[1]                                    # вирізали середину
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)

    def test_missing_journal_reports_absent(self):
        with tempfile.TemporaryDirectory() as d:
            res = audit_log.verify_chain(Path(d))       # стара нарада без audit.jsonl
            self.assertEqual(res.status, audit_log.STATUS_ABSENT)
            self.assertFalse(res.ok)
            self.assertEqual(res.event_count, 0)
            self.assertEqual(audit_log.read_events(Path(d)), [])

    def test_deleted_last_complete_row_is_reported_as_truncated(self):
        with _workspace_temp_dir(".audit-truncated-row-") as sess:
            audit_log.append_event(
                sess, audit_log.EVENT_CREATED, ts=1.0)
            audit_log.append_event(
                sess, audit_log.EVENT_STOPPED, ts=2.0)
            log = sess / "audit.jsonl"
            complete_rows = log.read_bytes().splitlines(keepends=True)
            self.assertEqual(len(complete_rows), 2)

            log.write_bytes(complete_rows[0])

            result = audit_log.verify_chain(sess)
            self.assertEqual(result.status, audit_log.STATUS_BROKEN)
            self.assertEqual(result.checkpoint_status, "truncated")
            self.assertIn("checkpoint seq 1", result.parse_error)

    def test_journal_ahead_of_head_is_verified_but_reported(self):
        with _workspace_temp_dir(".audit-head-behind-") as sess:
            first = audit_log.append_event(
                sess, audit_log.EVENT_CREATED, ts=1.0)
            audit_log.append_event(
                sess, audit_log.EVENT_STOPPED, ts=2.0)
            (sess / ".audit.head").write_text(
                json.dumps({
                    "version": 1,
                    "seq": first["seq"],
                    "hash": first["hash"],
                }) + "\n",
                encoding="utf-8",
            )

            result = audit_log.verify_chain(sess)
            self.assertEqual(result.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(
                getattr(result, "checkpoint_status", None),
                "journal_ahead",
            )
            self.assertIn(
                "journal seq 1",
                getattr(result, "checkpoint_error", ""),
            )

    def test_missing_head_for_new_policy_is_an_explicit_warning(self):
        with _workspace_temp_dir(".audit-head-missing-") as sess:
            audit_log.append_event(
                sess, audit_log.EVENT_CREATED, ts=1.0)
            (sess / ".audit.head").unlink(missing_ok=True)

            result = audit_log.verify_chain(sess)
            self.assertEqual(result.status, audit_log.STATUS_UNVERIFIED)
            self.assertEqual(result.checkpoint_status, "missing")
            self.assertIn("possible deletion", result.checkpoint_error)

    def test_legacy_journal_without_head_stays_compatible_and_is_labelled(self):
        with _workspace_temp_dir(".audit-head-legacy-") as sess:
            record = {
                "seq": 0,
                "type": audit_log.EVENT_CREATED,
                "ts": 1.0,
                "artifacts": {},
                "note": None,
                "prev": "",
            }
            record["hash"] = audit_log._record_hash(
                record["seq"], record["type"], record["ts"],
                record["artifacts"], record["note"], record["prev"],
            )
            (sess / "audit.jsonl").write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            result = audit_log.verify_chain(sess)
            self.assertEqual(result.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(
                getattr(result, "checkpoint_status", None),
                "missing_legacy",
            )


class ArtifactHashTests(unittest.TestCase):
    def test_finalize_records_audio_and_transcript_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            _write(sess / "mic.wav", b"RIFFaudio-bytes")
            _write(sess / "transcript.txt", b"hello world")
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.finalize(sess, ["mic.wav", "transcript.txt"])
            events = audit_log.read_events(sess)
            fin = events[-1]
            self.assertEqual(fin["type"], audit_log.EVENT_FINALIZED)
            self.assertIn("mic.wav", fin["artifacts"])
            self.assertIn("transcript.txt", fin["artifacts"])
            self.assertEqual(fin["artifacts"]["mic.wav"], audit_log.sha256_of(sess / "mic.wav"))
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(res.audio_sha, fin["artifacts"]["mic.wav"])

    def test_tampered_audio_breaks_verification(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            _write(sess / "mic.wav", b"original-audio")
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.finalize(sess, ["mic.wav"])
            # Ланцюг журналу цілий, але саме АУДІО підмінили після фіналізації.
            (sess / "mic.wav").write_bytes(b"forged-audio!!")
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)

    def test_logged_edit_keeps_chain_valid(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            _write(sess / "transcript.txt", b"draft text")
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.finalize(sess, ["transcript.txt"])
            # Легітимна правка транскрипту, ЗАФІКСОВАНА подією edited з новим хешем.
            (sess / "transcript.txt").write_bytes(b"corrected text")
            audit_log.append_event(sess, audit_log.EVENT_EDITED,
                                   artifacts=audit_log.hash_artifacts(sess, ["transcript.txt"]))
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)

    def test_unlogged_transcript_edit_breaks_verification(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            _write(sess / "transcript.txt", b"draft text")
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.finalize(sess, ["transcript.txt"])
            # Правка транскрипту БЕЗ події edited (обхід застосунку) → розрив.
            (sess / "transcript.txt").write_bytes(b"secretly changed")
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)


class LazyMetaTests(unittest.TestCase):
    """Лінива перевірка (fix продуктивності): рендер картки має читати статус
    журналу БЕЗ перехешування артефактів — інакше вкладка «Нарада» морозиться на
    2-годинних записах × багато сесій. read_chain_meta НЕ хешує; verify_chain —
    хешує лише за явним запитом."""

    def test_read_chain_meta_never_hashes_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            _write(sess / "mic.wav", b"RIFF" + b"x" * 100000)
            _write(sess / "transcript.txt", b"hello world")
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.finalize(sess, ["mic.wav", "transcript.txt"])
            calls = {"n": 0}
            real = audit_log.sha256_of

            def _counting(path, *a, **k):
                calls["n"] += 1
                return real(path, *a, **k)

            audit_log.sha256_of = _counting
            try:
                meta = audit_log.read_chain_meta(sess)
                # Дешеве читання: НУЛЬ звернень до sha256_of.
                self.assertEqual(calls["n"], 0)
                # Але статус і записана SHA доступні (з подій, без хешування).
                self.assertEqual(meta.status, audit_log.STATUS_UNVERIFIED)
                self.assertEqual(meta.event_count, 2)
                self.assertTrue(meta.audio_sha)
                self.assertEqual(len(meta.events), 2)
                # Контраст: повна верифікація ЗВЕРТАЄТЬСЯ до sha256_of.
                calls["n"] = 0
                audit_log.verify_chain(sess)
                self.assertGreater(calls["n"], 0)
            finally:
                audit_log.sha256_of = real

    def test_read_chain_meta_missing_journal_is_absent(self):
        with tempfile.TemporaryDirectory() as d:
            meta = audit_log.read_chain_meta(Path(d))
            self.assertEqual(meta.status, audit_log.STATUS_ABSENT)
            self.assertEqual(meta.event_count, 0)


class ReviewedEventTests(unittest.TestCase):
    """feature/evidence-plus: подія reviewed (принцип «чотирьох очей») — другий
    офіцер підтверджує цілісність. Той самий append-only механізм, ланцюг лишається
    цілим."""

    def test_reviewed_event_appends_and_keeps_chain_valid(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            audit_log.append_event(sess, audit_log.EVENT_REVIEWED,
                                   note={"reviewer": "Другий офіцер"})
            events = audit_log.read_events(sess)
            self.assertEqual(events[-1]["type"], audit_log.EVENT_REVIEWED)
            self.assertEqual(events[-1]["note"], {"reviewer": "Другий офіцер"})
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)


class BookmarkEventTests(unittest.TestCase):
    """feature/bookmarks-stage1: ручна мітка моменту наради (кнопка/хоткей під
    час запису) — та сама append-only подія, що робить закладку частиною
    доказового ланцюга (§4.1 спеки)."""

    def test_bookmark_added_event_keeps_chain_verified(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(
                sess, audit_log.EVENT_BOOKMARK_ADDED,
                note={"timestamp": 452.5, "title": "Обговорення кошторису",
                      "source": "live_hotkey"})
            events = audit_log.read_events(sess)
            self.assertEqual(events[-1]["type"], "bookmark_added")
            self.assertEqual(events[-1]["note"]["timestamp"], 452.5)
            self.assertEqual(events[-1]["note"]["source"], "live_hotkey")
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)

    def test_tampered_bookmark_note_breaks_chain(self):
        """МУТАЦІЯ-стиль перевірка: підміна timestamp у записаній події мітки
        мусить ловитись verify_chain, як і будь-яка інша подія журналу."""
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(
                sess, audit_log.EVENT_BOOKMARK_ADDED,
                note={"timestamp": 10.0, "title": "", "source": "live_button"})
            path = sess / "audit.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            tampered = json.loads(lines[-1])
            tampered["note"]["timestamp"] = 9999.0
            lines[-1] = json.dumps(tampered, ensure_ascii=False)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)


class CreatedNoteTests(unittest.TestCase):
    """feature/evidence-plus: created_note підставляє «хто зафіксував», якщо ім'я
    задано; порожнє → поле відсутнє (даних не вигадуємо, стара нарада сумісна)."""

    def test_created_note_includes_recorder_when_given(self):
        note = audit_log.created_note("both", ["mic", "sys"], recorded_by="  Ковальчук  ")
        self.assertEqual(note["preset"], "both")
        self.assertEqual(note["sources"], ["mic", "sys"])
        self.assertEqual(note["recorded_by"], "Ковальчук")       # обрізані пробіли

    def test_created_note_omits_recorder_when_empty(self):
        self.assertNotIn("recorded_by", audit_log.created_note("mic", ["mic"]))
        self.assertNotIn("recorded_by",
                         audit_log.created_note("mic", ["mic"], recorded_by="   "))

    def test_created_note_verifies_in_chain(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED,
                                   note=audit_log.created_note("mic", ["mic"],
                                                               recorded_by="Мельник"))
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(audit_log.read_events(sess)[0]["note"]["recorded_by"],
                             "Мельник")


class CorruptJournalTests(unittest.TestCase):
    def test_corrupt_line_in_middle_returns_broken(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            log = sess / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            # Вставляємо битий рядок JSON посередині
            lines.insert(1, '{"seq": 99, "type": "corrupt_json_line_bad_syntax...')
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)

    def test_corrupt_line_at_end_returns_broken(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            log = sess / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            # Додаємо битий рядок наприкінці
            lines.append('BAD_JSON_LINE_AT_END')
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)

    def test_valid_journal_returns_verified(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)


class AppendRefusesCorruptTests(unittest.TestCase):
    """Битий рядок посередині лишається AuditLogCorrupt; лише останній
    обірваний рядок стає явною recovery-подією перед наступним append."""

    def test_append_after_corrupt_tail_records_recovery_event(self):
        with _workspace_temp_dir(".audit-tail-") as sess:
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            log = sess / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            lines.append("BAD_JSON_TAIL")                   # битий хвіст журналу
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            discarded = log.read_bytes().splitlines(keepends=True)[-1]

            audit_log.append_event(sess, audit_log.EVENT_STOPPED)

            events = audit_log.read_events(sess)
            self.assertEqual(
                [event["type"] for event in events],
                [
                    audit_log.EVENT_CREATED,
                    audit_log.EVENT_RECOVERED,
                    audit_log.EVENT_STOPPED,
                ],
            )
            self.assertEqual(events[1]["note"], {
                "discarded_bytes": len(discarded),
                "discarded_sha256": hashlib.sha256(discarded).hexdigest(),
            })
            self.assertEqual(
                audit_log.verify_chain(sess).status,
                audit_log.STATUS_VERIFIED,
            )

    def test_partial_enospc_write_is_recovered_on_next_append(self):
        class PartialWriter:
            def __init__(self, wrapped, byte_limit):
                self._wrapped = wrapped
                self._byte_limit = byte_limit

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self._wrapped.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

            def write(self, data):
                partial = data[:self._byte_limit]
                self._wrapped.write(partial)
                self._wrapped.flush()
                raise OSError(errno.ENOSPC, "disk full")

        with _workspace_temp_dir(".audit-enospc-") as sess:
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            log = sess / "audit.jsonl"
            real_open = builtins.open

            def partial_open(path, mode="r", *args, **kwargs):
                wrapped = real_open(path, mode, *args, **kwargs)
                if Path(path) == log and "a" in mode:
                    return PartialWriter(wrapped, 17)
                return wrapped

            with patch("builtins.open", partial_open), self.assertRaises(OSError) as raised:
                audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            torn_tail = log.read_bytes().split(b"\n")[-1]
            self.assertTrue(torn_tail)

            audit_log.append_event(sess, audit_log.EVENT_EXPORTED)

            events = audit_log.read_events(sess)
            self.assertEqual(
                [event["type"] for event in events],
                [
                    audit_log.EVENT_CREATED,
                    audit_log.EVENT_RECOVERED,
                    audit_log.EVENT_EXPORTED,
                ],
            )
            self.assertEqual(
                events[1]["note"]["discarded_sha256"],
                hashlib.sha256(torn_tail).hexdigest(),
            )
            self.assertEqual(
                events[1]["note"]["discarded_bytes"],
                len(torn_tail),
            )
            self.assertEqual(
                audit_log.verify_chain(sess).status,
                audit_log.STATUS_VERIFIED,
            )

    def test_append_after_corrupt_middle_raises_audit_log_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            log = sess / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            lines.insert(1, '{"seq": 99, "type": "broken...')  # битий рядок посередині
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(audit_log.AuditLogCorrupt):
                audit_log.append_event(sess, audit_log.EVENT_EXPORTED)

    def test_valid_journal_append_still_works(self):
        """Регресія: неушкоджений журнал дописується як раніше (seq/prev-ланцюг)."""
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            rec = audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            self.assertEqual(rec["seq"], 1)
            events = audit_log.read_events(sess)
            self.assertEqual([e["seq"] for e in events], [0, 1])
            self.assertEqual(events[1]["prev"], events[0]["hash"])


class Sha256Tests(unittest.TestCase):
    def test_sha256_of_matches_hashlib(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            p = _write(Path(d) / "f.bin", b"a" * 100000)
            self.assertEqual(audit_log.sha256_of(p), hashlib.sha256(b"a" * 100000).hexdigest())


class AuditCorruptWarnPerMeetingTests(unittest.TestCase):
    def test_warn_audit_corrupt_logs_always_but_notifies_once_per_session(self):
        import logging
        from fronts.desktop import app
        notifications = []
        log_warnings = []

        def dummy_notify(msg):
            notifications.append(msg)

        app._set_audit_corrupt_notifier(dummy_notify)
        if hasattr(app, "_audit_corrupt_warned_sessions"):
            app._audit_corrupt_warned_sessions.clear()
        elif hasattr(app, "_audit_corrupt_warned"):
            app._audit_corrupt_warned = False

        class WarningFilter(logging.Handler):
            def emit(self, record):
                if record.levelno == logging.WARNING and "пошкоджено" in record.getMessage():
                    log_warnings.append(record.getMessage())

        handler = WarningFilter()
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            # Нарада 1 - перший виклик
            app._warn_audit_corrupt("session_1")
            self.assertEqual(len(notifications), 1)
            self.assertEqual(len(log_warnings), 1)

            # Нарада 1 - повторний виклик (повинно залогувати warning, але НЕ надсилати тост повторно)
            app._warn_audit_corrupt("session_1")
            self.assertEqual(len(notifications), 1)
            self.assertEqual(len(log_warnings), 2)

            # Нарада 2 - перший виклик (повинно надсилати тост для нової наради + залогувати warning)
            app._warn_audit_corrupt("session_2")
            self.assertEqual(len(notifications), 2)
            self.assertEqual(len(log_warnings), 3)
        finally:
            root_logger.removeHandler(handler)
            app._set_audit_corrupt_notifier(None)

    def test_audit_log_deleted_is_visible_to_user_and_logged_as_warning(self):
        from fronts.desktop import app
        notifications = []
        app._set_audit_corrupt_notifier(notifications.append)
        if hasattr(app, "_audit_deleted_warned_sessions"):
            app._audit_deleted_warned_sessions.clear()
        try:
            with patch(
                    "whisper_core.meeting.audit_log.append_event",
                    side_effect=audit_log.AuditLogDeleted("deleted")), \
                    self.assertLogs(level="WARNING") as captured:
                app._audit_event(
                    "2026-07-15_10-00-00",
                    audit_log.EVENT_STOPPED,
                )

            self.assertEqual(len(notifications), 1)
            self.assertIn("журнал", notifications[0].lower())
            self.assertTrue(any(
                "позначено видаленою" in message
                for message in captured.output
            ))
        finally:
            app._set_audit_corrupt_notifier(None)

    def test_audit_timeout_notifies_once_per_session(self):
        from fronts.desktop import app
        notifications = []
        app._set_audit_corrupt_notifier(notifications.append)
        if hasattr(app, "_audit_timeout_warned_sessions"):
            app._audit_timeout_warned_sessions.clear()
        try:
            with patch(
                    "whisper_core.meeting.audit_log.append_event",
                    side_effect=TimeoutError("busy")):
                app._audit_event("session_1", audit_log.EVENT_STOPPED)
                app._audit_event("session_1", audit_log.EVENT_EXPORTED)
                app._audit_event("session_2", audit_log.EVENT_STOPPED)

            self.assertEqual(len(notifications), 2)
            self.assertTrue(all(
                "іншим процесом" in message
                for message in notifications
            ))
        finally:
            app._set_audit_corrupt_notifier(None)

    def test_audit_timeout_uses_english_message(self):
        from fronts.desktop import app, i18n
        notifications = []
        previous_language = i18n.current_language()
        app._set_audit_corrupt_notifier(notifications.append)
        if hasattr(app, "_audit_timeout_warned_sessions"):
            app._audit_timeout_warned_sessions.clear()
        i18n.set_language("en")
        try:
            with patch(
                    "whisper_core.meeting.audit_log.append_event",
                    side_effect=TimeoutError("busy")):
                app._audit_event("session_en", audit_log.EVENT_STOPPED)

            self.assertEqual(len(notifications), 1)
            self.assertIn("another process", notifications[0])
            self.assertIn("try again", notifications[0].lower())
        finally:
            i18n.set_language(previous_language)
            app._set_audit_corrupt_notifier(None)

    def test_audit_os_errors_are_visible_to_user(self):
        from fronts.desktop import app
        for error in (PermissionError("read-only"), OSError("disk full")):
            with self.subTest(error_type=type(error).__name__):
                notifications = []
                app._set_audit_corrupt_notifier(notifications.append)
                if hasattr(app, "_audit_unavailable_warned_sessions"):
                    app._audit_unavailable_warned_sessions.clear()
                try:
                    with patch(
                            "whisper_core.meeting.audit_log.append_event",
                            side_effect=error):
                        app._audit_event("session_1", audit_log.EVENT_STOPPED)

                    self.assertEqual(len(notifications), 1)
                    self.assertIn("недоступний", notifications[0])
                finally:
                    app._set_audit_corrupt_notifier(None)


class TrashDeletionBarrierTests(unittest.TestCase):
    """Кошик (м'яке видалення) + tombstone deletion_barrier.

    Рецензент впіймав живим відтворенням: soft_delete БЕЗ барʼєра дозволяв
    straggler append_event створити фантомну теку на місці видаленої наради
    (append_event робить mkdir(parents=True), якщо тека не існує й немає
    маркера). Тут — та сама перевірка без subprocess-гонки (є в
    tests/test_meeting_audit_locking.py): прямий виклик деталей барʼєра до й
    після soft_delete/restore."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "sessions"
        self.root.mkdir()
        self.session_dir = self.root / "2026-07-15_10-00-00"
        audit_log.append_event(self.session_dir, audit_log.EVENT_CREATED)

    def test_soft_delete_under_barrier_refuses_straggler_without_phantom(self):
        from whisper_core import trash as mtrash
        with audit_log.deletion_barrier(self.session_dir):
            trashed = mtrash.soft_delete(self.session_dir, self.root)
        self.assertFalse(self.session_dir.exists())

        # Straggler-writer, що спізнився з подією про вже видалену нараду —
        # чесна відмова, а НЕ мовчазне відродження фантомної теки.
        with self.assertRaises(audit_log.AuditLogDeleted):
            audit_log.append_event(self.session_dir, audit_log.EVENT_STOPPED)
        self.assertFalse(
            self.session_dir.exists(),
            "append_event не має воскрешати видалену нараду mkdir-ом")
        self.assertTrue((trashed / "audit.jsonl").is_file())

    def test_restore_clears_barrier_and_append_event_succeeds_again(self):
        from whisper_core import trash as mtrash
        with audit_log.deletion_barrier(self.session_dir):
            trashed = mtrash.soft_delete(self.session_dir, self.root)

        restored = mtrash.restore(trashed, self.root)
        audit_log.clear_deletion_barrier(restored)

        # Журнал відновленої наради знову приймає події — ланцюг продовжується
        # (seq=1), а не тільки не кидає AuditLogDeleted.
        record = audit_log.append_event(restored, audit_log.EVENT_STOPPED)
        self.assertEqual(record["seq"], 1)
        events = audit_log.read_events(restored)
        self.assertEqual(
            [e["type"] for e in events],
            [audit_log.EVENT_CREATED, audit_log.EVENT_STOPPED])

    def test_restore_via_app_clears_barrier(self):
        """Той самий сценарій, але через DesktopApp.delete_meeting/restore_meeting
        (реальний виклик, не лише whisper_core.trash напряму)."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from fronts.desktop.app import DesktopApp

        controller = SimpleNamespace(
            profile=SimpleNamespace(),
            _meetings_root=lambda: self.root,
            _last_trashed_meeting=None,
        )
        with patch(
                "whisper_core.meeting.voice_memory.delete_pending_centroids",
                return_value=True):
            failures = DesktopApp.delete_meeting(
                controller, self.session_dir.name)
        self.assertEqual(failures, ())
        self.assertFalse(self.session_dir.exists())

        restored = DesktopApp.restore_meeting(controller)
        self.assertEqual(restored, self.session_dir)

        record = audit_log.append_event(self.session_dir, audit_log.EVENT_STOPPED)
        self.assertEqual(record["seq"], 1)

    def test_restore_with_name_conflict_clears_original_tombstone_too(self):
        """Осиротілий tombstone (хвіст рецензента): видалили "X", завели нову живу
        "X", відновили з кошика під конфліктом ("X (2)") — маркер
        .X.audit.deleted не має лишатись висіти вічно на оригінальній назві,
        і нова жива "X" далі мусить писати журнал."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from fronts.desktop.app import DesktopApp

        controller = SimpleNamespace(
            profile=SimpleNamespace(),
            _meetings_root=lambda: self.root,
            _last_trashed_meeting=None,
        )
        with patch(
                "whisper_core.meeting.voice_memory.delete_pending_centroids",
                return_value=True):
            failures = DesktopApp.delete_meeting(
                controller, self.session_dir.name)
        self.assertEqual(failures, ())
        self.assertFalse(self.session_dir.exists())

        # Нова жива нарада з тим самим ім'ям, поки стара лежить у кошику
        # (тека фізично на місці — так само, як реальний запис створює її
        # перед першою подією журналу).
        self.session_dir.mkdir()
        audit_log.append_event(self.session_dir, audit_log.EVENT_CREATED)

        trashed_dirs = list((self.root / ".trash").iterdir())
        self.assertEqual(len(trashed_dirs), 1)
        restored = DesktopApp.restore_meeting(controller, trashed_dirs[0])

        self.assertEqual(restored.name, self.session_dir.name + " (2)")
        # Осиротілий маркер оригінальної назви прибрано.
        self.assertFalse(
            audit_log._deleted_marker_path(self.session_dir).exists())
        # Нова жива "X" далі пише журнал.
        record = audit_log.append_event(self.session_dir, audit_log.EVENT_STOPPED)
        self.assertEqual(record["seq"], 1)
        # Відновлена "X (2)" теж пише журнал.
        record2 = audit_log.append_event(restored, audit_log.EVENT_STOPPED)
        self.assertEqual(record2["seq"], 1)


if __name__ == "__main__":
    unittest.main()


