"""feature/evidence-plus: доказовий пакет (evidence.py) + незалежний перевіряч
(scripts/verify.py).

Перевіряємо: (1) export_evidence кладе у zip усі 4 частини й коректний REPORT;
(2) standalone verify.py, запущений ЧИСТИМ системним python БЕЗ репозиторію на
PYTHONPATH, дає VERIFIED на цілому ланцюгу й BROKEN із правильним індексом на
підміненому — доказ самодостатності (лише stdlib).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from whisper_core.meeting import audit_log, evidence


def _make_session(sess: Path, *, recorded_by="Ковальчук"):
    (sess / "mic.wav").write_bytes(b"RIFF-audio-bytes")
    (sess / "transcript.txt").write_bytes(b"hello world")
    audit_log.append_event(sess, audit_log.EVENT_CREATED,
                           note=audit_log.created_note("mic", ["mic"], recorded_by=recorded_by))
    audit_log.finalize(sess, ["mic.wav", "transcript.txt"])


class ExportEvidenceTests(unittest.TestCase):
    def test_zip_contains_all_four_parts(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d) / "sess"
            sess.mkdir()
            _make_session(sess)
            out = Path(d) / "evidence.zip"
            pkg = evidence.export_evidence(sess, out, app_version="1.2.3")
            self.assertEqual(pkg.status, audit_log.STATUS_VERIFIED)
            self.assertTrue(pkg.has_verifier)
            self.assertTrue(out.is_file())
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
            # (а) файли наради
            self.assertIn("mic.wav", names)
            self.assertIn("transcript.txt", names)
            # (б) журнал цілісності
            self.assertIn("audit.jsonl", names)
            # (в) незалежний перевіряч
            self.assertIn("verify.py", names)
            # (г) людино-читний звіт
            self.assertIn("REPORT.txt", names)

    def test_report_reflects_status_recorder_and_hashes(self):
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d) / "sess"
            sess.mkdir()
            _make_session(sess, recorded_by="Олена Ковальчук")
            audit_log.append_event(sess, audit_log.EVENT_REVIEWED,
                                   note={"reviewer": "Андрій Мельник"})
            out = Path(d) / "e.zip"
            evidence.export_evidence(sess, out, app_version="1.2.3")
            with zipfile.ZipFile(out) as z:
                report = z.read("REPORT.txt").decode("utf-8")
            self.assertIn("ПІДТВЕРДЖЕНО", report)              # статус ланцюга
            self.assertIn("Олена Ковальчук", report)          # хто зафіксував
            self.assertIn("Андрій Мельник", report)           # хто переглянув
            self.assertIn("1.2.3", report)                     # версія
            # реальний SHA-256 аудіо присутній у переліку файлів
            self.assertIn(audit_log.sha256_of(sess / "mic.wav"), report)

    def test_missing_session_dir_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                evidence.export_evidence(Path(d) / "nope", Path(d) / "x.zip")


class StandaloneVerifierTests(unittest.TestCase):
    """verify.py має працювати БЕЗ застосунку: чистий python, репозиторій НЕ на
    PYTHONPATH. Копіюємо verify.py поряд із сесією і запускаємо `python verify.py .`."""

    def _verify_py(self) -> Path:
        p = evidence.verifier_source()
        self.assertIsNotNone(p, "scripts/verify.py не знайдено")
        return p

    def _run(self, workdir: Path):
        env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "verify.py", "."],
            cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env)

    def test_intact_unsigned_chain_reports_legacy(self):
        # Журнал без Ed25519-підписів (старий стиль) → код 4 «unsigned legacy»,
        # НЕ код 0 і НЕ «VERIFIED» (§9.2, критерій приймання 8).
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "mic.wav").write_bytes(b"RIFF-audio")
            audit_log.append_event(work, audit_log.EVENT_CREATED)
            audit_log.finalize(work, ["mic.wav"])
            shutil.copyfile(self._verify_py(), work / "verify.py")
            r = self._run(work)
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("UNSIGNED LEGACY", r.stdout)
            self.assertNotIn("VERIFIED — журнал цілий, підписи", r.stdout)

    def test_tampered_record_reports_broken_with_index(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            audit_log.append_event(work, audit_log.EVENT_CREATED, note={"preset": "mic"})
            audit_log.append_event(work, audit_log.EVENT_STOPPED)
            # підмінити ПЕРШИЙ запис без перерахунку хешу
            log = work / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            rec = json.loads(lines[0])
            rec["note"] = {"preset": "both"}
            lines[0] = json.dumps(rec, ensure_ascii=False)
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            shutil.copyfile(self._verify_py(), work / "verify.py")
            r = self._run(work)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)
            self.assertIn("seq=0", r.stdout)                   # правильний індекс

    def test_tampered_audio_reports_broken(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            (work / "mic.wav").write_bytes(b"original-audio")
            audit_log.append_event(work, audit_log.EVENT_CREATED)
            audit_log.finalize(work, ["mic.wav"])
            (work / "mic.wav").write_bytes(b"forged!!")        # підміна файлу
            shutil.copyfile(self._verify_py(), work / "verify.py")
            r = self._run(work)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)
            self.assertIn("mic.wav", r.stdout)

    def test_corrupt_line_in_middle_reports_broken(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            audit_log.append_event(work, audit_log.EVENT_CREATED)
            audit_log.append_event(work, audit_log.EVENT_STOPPED)
            log = work / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            lines.insert(1, '{"bad_json": corrupt...')
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            shutil.copyfile(self._verify_py(), work / "verify.py")
            r = self._run(work)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)

    def test_corrupt_line_at_end_reports_broken(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            audit_log.append_event(work, audit_log.EVENT_CREATED)
            log = work / "audit.jsonl"
            lines = log.read_text(encoding="utf-8").splitlines()
            lines.append("BAD_TAIL_LINE")
            log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            shutil.copyfile(self._verify_py(), work / "verify.py")
            r = self._run(work)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)

    def test_absent_journal_reports_absent(self):
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            shutil.copyfile(self._verify_py(), work / "verify.py")
            r = self._run(work)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("НЕМАЄ ЖУРНАЛУ", r.stdout)


if __name__ == "__main__":
    unittest.main()

