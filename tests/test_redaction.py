"""feature/redaction — інтеграційні гарантії доказовості.

КЛЮЧОВИЙ інваріант: редакція заглушує аудіо в ОКРЕМІЙ «-redacted»-копії й
пише відредагований транскрипт в ОКРЕМИЙ файл (transcript-redacted.*).
ОРИГІНАЛЬНІ transcript.txt/transcript.json лишаються байт-у-байт незмінними —
початкові слова не знищуються (доказовість запису).

Плюс: збій запису транскрипту → помилка (а не тихе «готово»); у підтвердженні
редакції явна примітка, що чіпається лише відкрита доріжка наради.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core.meeting import postprocess as pp


def _make_session(tmp) -> Path:
    """Сесія з ОРИГІНАЛЬНИМИ transcript.txt/transcript.json (репліка «чутливе»
    перетинає діапазон [4,5), який редагуватимемо)."""
    session = Path(tmp) / "2026-07-17_10-00-00"
    session.mkdir(parents=True, exist_ok=True)
    (session / "meeting.json").write_text(
        json.dumps({"rate": 48000, "channels": 2}), encoding="utf-8")
    utts = [
        pp.Utterance(0.0, 2.0, pp.SPK_SINGLE, "до діапазону"),
        pp.Utterance(3.0, 6.0, pp.SPK_SINGLE, "чутливе"),
        pp.Utterance(7.0, 9.0, pp.SPK_SINGLE, "після діапазону"),
    ]
    (session / "transcript.json").write_text(
        json.dumps(pp.to_transcript_json(utts), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (session / "transcript.txt").write_text(
        "\n".join(u.text for u in utts) + "\n", encoding="utf-8")
    return session


def _redact_ns(session):
    """Стенд контролера для redact_transcript. Контролер тепер мапить теку через
    _meeting_session_dir (для шифрованих сесій — назад із materialized-теки) і
    чистить plain-кеш. Без шифрування materialized-тека == тека сесії, тож мапимо
    id назад на неї; кеш-очистка — no-op."""
    return SimpleNamespace(
        _meeting_session_dir=lambda sid: session.parent / sid,
        _clear_meeting_plain_cache=lambda *a, **k: None,
    )


class WriteTranscriptStemTests(unittest.TestCase):
    """postprocess: stem пише в окремий файл, не чіпаючи transcript.*."""

    def test_stem_writes_separate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            orig_txt = (session / "transcript.txt").read_bytes()
            orig_json = (session / "transcript.json").read_bytes()
            utts = [pp.Utterance(0.0, 1.0, pp.SPK_SINGLE, "[вилучено]")]
            txt_path, json_path = pp.write_transcript(
                session, utts, me_label="Я", others_label="Співрозмовники",
                stem="transcript-redacted")
            self.assertEqual(txt_path, session / "transcript-redacted.txt")
            self.assertEqual(json_path, session / "transcript-redacted.json")
            self.assertTrue(txt_path.is_file() and json_path.is_file())
            # оригінали байт-у-байт незмінні
            self.assertEqual((session / "transcript.txt").read_bytes(), orig_txt)
            self.assertEqual((session / "transcript.json").read_bytes(), orig_json)

    def test_append_note_stem_targets_separate_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            orig_txt = (session / "transcript.txt").read_bytes()
            (session / "transcript-redacted.txt").write_text("рядок\n", encoding="utf-8")
            pp.append_transcript_note(session, "фрагмент 0:04–0:05 вилучено",
                                      stem="transcript-redacted")
            red = (session / "transcript-redacted.txt").read_text(encoding="utf-8")
            self.assertIn("фрагмент 0:04–0:05 вилучено", red)
            self.assertEqual((session / "transcript.txt").read_bytes(), orig_txt)


class RedactTranscriptControllerTests(unittest.TestCase):
    """DesktopApp.redact_transcript (unbound на SimpleNamespace) — оригінал
    недоторканий, редакція в окремому файлі, збій запису піднімається."""

    def _call(self, session, ns=None):
        from fronts.desktop.app import DesktopApp
        wav = session / "recording.wav"
        return DesktopApp.redact_transcript(
            ns or _redact_ns(session), str(wav), 4.0, 5.0,
            marker="[вилучено]", note="фрагмент 0:04–0:05 вилучено")

    def test_originals_untouched_and_redacted_copy_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            orig_txt = (session / "transcript.txt").read_bytes()
            orig_json = (session / "transcript.json").read_bytes()

            result = self._call(session)

            # 1) оригінали байт-у-байт незмінні
            self.assertEqual((session / "transcript.txt").read_bytes(), orig_txt)
            self.assertEqual((session / "transcript.json").read_bytes(), orig_json)
            # 2) редакція — в ОКРЕМОМУ файлі
            red_json = session / "transcript-redacted.json"
            red_txt = session / "transcript-redacted.txt"
            self.assertTrue(red_json.is_file() and red_txt.is_file())
            self.assertEqual(result, red_txt)
            # 3) у редакції — маркер (перекрита репліка), решта слів на місці
            data = json.loads(red_json.read_text(encoding="utf-8"))
            texts = [it["text"] for it in data]
            self.assertEqual(texts, ["до діапазону", "[вилучено]", "після діапазону"])
            # 4) примітка дописана
            self.assertIn("фрагмент 0:04–0:05 вилучено",
                          red_txt.read_text(encoding="utf-8"))
            # 5) початкове слово «чутливе» ще є в ОРИГІНАЛІ (відновлюваність)
            orig = json.loads((session / "transcript.json").read_text(encoding="utf-8"))
            self.assertIn("чутливе", [it["text"] for it in orig])

    def test_nested_track_audio_resolves_session_and_redacts(self):
        """FIX 2(a): реальне аудіо доріжки лежить у <session>/audio/<track>/0000.wav.
        parent аудіо = назва доріжки («mic»), НЕ id сесії. Резолв мусить піднятись
        до теки сесії й СТВОРИТИ transcript-redacted, а не тихо повернути None."""
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_two_source_session(tmp)
            nested = session / "audio" / "mic" / "0000.wav"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"wav")
            from fronts.desktop.app import DesktopApp
            result = DesktopApp.redact_transcript(
                _redact_ns(session), str(nested), 4.0, 5.0,
                marker="[вилучено]", note="фрагмент 0:04–0:05 вилучено",
                source=pp.SPK_ME)
            red = session / "transcript-redacted.json"
            self.assertTrue(red.is_file())    # створено, не None-фейк
            self.assertEqual(result, session / "transcript-redacted.txt")
            texts = [it["text"] for it in json.loads(red.read_text(encoding="utf-8"))]
            self.assertEqual(texts, ["[вилучено]", "чужий голос", "після"])

    def test_no_transcript_returns_none_without_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "plain"
            session.mkdir()
            result = self._call(session)
            self.assertIsNone(result)
            self.assertFalse((session / "transcript-redacted.txt").exists())

    def test_write_failure_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)
            orig_txt = (session / "transcript.txt").read_bytes()
            # Запис транскрипту тепер іде через session.write_artifact (зберігає
            # шифрування), а не через postprocess.os.replace. Патчимо новий шлях.
            with patch("whisper_core.meeting.session.write_artifact",
                       side_effect=OSError("диск повний")):
                with self.assertRaises(OSError):
                    self._call(session)
            # оригінал усе одно недоторканий після збою
            self.assertEqual((session / "transcript.txt").read_bytes(), orig_txt)


def _make_two_source_session(tmp) -> Path:
    """Сесія mic+sys: репліки ОБОХ джерел перекривають діапазон [4,5).
    Редакція mic-доріжки мусить зачепити ЛИШЕ ``me``-репліку."""
    session = Path(tmp) / "2026-07-22_11-00-00"
    session.mkdir(parents=True, exist_ok=True)
    (session / "meeting.json").write_text(
        json.dumps({"rate": 48000, "channels": 2}), encoding="utf-8")
    utts = [
        pp.Utterance(3.5, 4.5, pp.SPK_ME, "мій голос", source=pp.SPK_ME),
        pp.Utterance(4.0, 5.0, pp.SPK_OTHERS, "чужий голос", source=pp.SPK_OTHERS),
        pp.Utterance(7.0, 9.0, pp.SPK_ME, "після", source=pp.SPK_ME),
    ]
    (session / "transcript.json").write_text(
        json.dumps(pp.to_transcript_json(utts), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (session / "transcript.txt").write_text(
        "\n".join(u.text for u in utts) + "\n", encoding="utf-8")
    return session


class RedactUtterancesSourceScopeTests(unittest.TestCase):
    """postprocess.redact_utterances: ``source`` звужує редакцію до однієї доріжки."""

    def _utts(self):
        return [
            pp.Utterance(3.5, 4.5, pp.SPK_ME, "мій голос", source=pp.SPK_ME),
            pp.Utterance(4.0, 5.0, pp.SPK_OTHERS, "чужий голос", source=pp.SPK_OTHERS),
        ]

    def test_source_scopes_redaction_to_one_track(self):
        out = pp.redact_utterances(self._utts(), 4.0, 5.0,
                                   marker="[вилучено]", source=pp.SPK_ME)
        texts = [u.text for u in out]
        # затерта лише me-репліка; репліка іншого джерела недоторкана
        self.assertEqual(texts, ["[вилучено]", "чужий голос"])

    def test_source_none_redacts_all_overlapping(self):
        out = pp.redact_utterances(self._utts(), 4.0, 5.0, marker="[вилучено]")
        texts = [u.text for u in out]
        self.assertEqual(texts, ["[вилучено]", "[вилучено]"])


class RedactTranscriptSourceScopeTests(unittest.TestCase):
    """DesktopApp.redact_transcript зі source: у -redacted лишаються чужі репліки."""

    def test_redact_by_mic_leaves_sys_utterances(self):
        from fronts.desktop.app import DesktopApp
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_two_source_session(tmp)
            wav = session / "mic.wav"
            result = DesktopApp.redact_transcript(
                _redact_ns(session), str(wav), 4.0, 5.0,
                marker="[вилучено]", note="фрагмент 0:04–0:05 вилучено",
                source=pp.SPK_ME)
            data = json.loads((session / "transcript-redacted.json").read_text(encoding="utf-8"))
            texts = [it["text"] for it in data]
            # me-репліка затерта; репліка «Співрозмовників» ЦІЛА
            self.assertEqual(texts, ["[вилучено]", "чужий голос", "після"])
            self.assertEqual(result, session / "transcript-redacted.txt")


class RedactUiTests(unittest.TestCase):
    """audio_editor._redact (unbound на SimpleNamespace): збій не видається за
    успіх; у підтвердженні є мультитрек-примітка."""

    def _fake_panel(self, redact_transcript):
        import numpy as np
        label = SimpleNamespace(text=None, setText=lambda t: setattr(label, "text", t))
        return SimpleNamespace(
            _range=(4.0, 5.0),
            _need_range=lambda: (4.0, 5.0),
            _audio=np.zeros(16000, dtype=np.float32),
            _rate=16000,
            _path="C:/x/recording.wav",
            _save=lambda audio, suffix: "C:/x/recording-redacted.wav",
            _range_label=label,
            _source=None,
            _controller=SimpleNamespace(redact_transcript=redact_transcript),
        )

    def test_write_failure_shows_error_not_success(self):
        from fronts.desktop.audio_editor import AudioEditorPanel
        from fronts.desktop.i18n import tr

        def boom(*a, **k):
            raise OSError("немає доступу")

        panel = self._fake_panel(boom)
        with patch("fronts.desktop.audio_editor.QInputDialog.getItem",
                   return_value=("Тиша", True)), \
             patch("fronts.desktop.audio_editor.QMessageBox.question",
                   return_value=__import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes), \
             patch("fronts.desktop.audio_editor.QMessageBox.warning") as warn:
            AudioEditorPanel._redact(panel)
            self.assertTrue(warn.called)          # помилку показано
        # мітка — про збій, НЕ фальшиве «готово»
        self.assertEqual(panel._range_label.text, tr("audioedit_redact_failed"))

    def test_none_result_shows_error_not_fake_success(self):
        """FIX 2(b): redact_transcript повернув None (транскрипт НЕ заредаговано) —
        _redact НЕ має показувати фальшиве «вилучено». Інакше чутливі слова
        лишаються в transcript.json і потрапляють в evidence, а користувач думає,
        що їх прибрано."""
        from fronts.desktop.audio_editor import AudioEditorPanel
        from fronts.desktop.i18n import tr
        from PySide6.QtWidgets import QMessageBox

        panel = self._fake_panel(lambda *a, **k: None)   # нічого не заредаговано
        with patch("fronts.desktop.audio_editor.QInputDialog.getItem",
                   return_value=("Тиша", True)), \
             patch("fronts.desktop.audio_editor.QMessageBox.question",
                   return_value=QMessageBox.Yes), \
             patch("fronts.desktop.audio_editor.QMessageBox.warning") as warn:
            AudioEditorPanel._redact(panel)
            self.assertTrue(warn.called)          # помилку показано
        # мітка — про збій, НЕ фальшиве «фрагмент вилучено»
        self.assertEqual(panel._range_label.text, tr("audioedit_redact_failed"))

    def test_confirm_includes_multitrack_note(self):
        from fronts.desktop.audio_editor import AudioEditorPanel
        from fronts.desktop.i18n import tr
        from PySide6.QtWidgets import QMessageBox

        panel = self._fake_panel(lambda *a, **k: None)
        seen = {}

        def capture_question(parent, title, text, *a, **k):
            seen["text"] = text
            return QMessageBox.No           # скасувати — далі не йдемо

        with patch("fronts.desktop.audio_editor.QInputDialog.getItem",
                   return_value=("Тиша", True)), \
             patch("fronts.desktop.audio_editor.QMessageBox.question",
                   side_effect=capture_question):
            AudioEditorPanel._redact(panel)
        self.assertIn(tr("audioedit_redact_multitrack_note"), seen["text"])


class EvidenceIncludesRedactedTests(unittest.TestCase):
    """FIX 2(c): доказовий пакет мусить містити transcript-redacted, якщо він є."""

    def test_evidence_package_includes_transcript_redacted(self):
        import zipfile
        from whisper_core.meeting import evidence
        with tempfile.TemporaryDirectory() as tmp:
            session = _make_session(tmp)          # має transcript.json/.txt
            (session / "transcript-redacted.txt").write_text(
                "[вилучено]\n", encoding="utf-8")
            (session / "transcript-redacted.json").write_text(
                "[]", encoding="utf-8")
            out = Path(tmp) / "evidence.zip"
            evidence.export_evidence(session, out)
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
            self.assertIn("transcript-redacted.txt", names)
            self.assertIn("transcript-redacted.json", names)


if __name__ == "__main__":
    unittest.main()
