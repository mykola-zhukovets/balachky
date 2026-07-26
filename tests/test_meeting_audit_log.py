"""Юніти журналу цілісності (chain-of-custody) наради — audit_log.py.

Без Qt, без реального аудіо: артефакти — звичайні файли у tempfile. Перевіряємо
хеш-ланцюг, детекцію підміни запису/аудіо, roundtrip і міграцію старих нарад.
"""
import json
import tempfile
import unittest
from pathlib import Path

from whisper_core.meeting import audit_log


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


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
            self.assertEqual(events[0]["note"], {"preset": "onlymic"})
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
    """Блокер Т56: після фікса _read_raw битий ХВІСТ журналу повертається
    маркером {"_corrupt": True}. append_event раніше робив events[-1]["seq"] →
    KeyError, який мовчки ковтав широкий except у UI, і ВСІ наступні події
    наради більше не дописувалися. Тепер append_event на пошкодженні кидає
    спеціалізований AuditLogCorrupt (не KeyError, не тихий дозапис поверх)."""

    def test_append_after_corrupt_tail_raises_audit_log_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d)
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            log = sess / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            lines.append("BAD_JSON_TAIL")                   # битий хвіст журналу
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(audit_log.AuditLogCorrupt):
                audit_log.append_event(sess, audit_log.EVENT_STOPPED)

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


if __name__ == "__main__":
    unittest.main()


