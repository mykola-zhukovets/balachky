"""Regression tests for honest panic/delete semantics and privacy copy."""

import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from types import MethodType
import unittest
from unittest.mock import MagicMock, patch
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.app import DesktopApp
from fronts.desktop.i18n import STRINGS
from fronts.desktop.pages import meeting as meeting_page
from whisper_core.meeting import voice_memory

ROOT = Path(__file__).resolve().parents[1]


class DeleteMeetingTruthfulness(unittest.TestCase):
    SESSION_ID = "2026-07-15_10-00-00"

    def setUp(self):
        self.temp_root = Path.cwd() / f".post49-delete-{uuid.uuid4().hex}"
        self.temp_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _controller(self):
        return SimpleNamespace(
            profile="default",
            _meetings_root=lambda: self.temp_root,
        )

    def test_controller_returns_failed_file_deletion(self):
        # Семантика кошика (31.07): «видалити» = перенести теку в .trash.
        # Перенос упав (тека зайнята іншим процесом) — контролер мусить
        # чесно повернути невдалий крок, а не змовчати.
        app = self._controller()
        (self.temp_root / self.SESSION_ID).mkdir()

        with patch(
                "whisper_core.meeting.session.is_safe_session_id",
                return_value=True), patch(
                    "whisper_core.trash.soft_delete",
                    side_effect=OSError("тека зайнята")), patch(
                        "whisper_core.meeting.voice_memory.delete_pending_centroids",
                        return_value=True):
            failures = DesktopApp.delete_meeting(app, self.SESSION_ID)

        self.assertEqual(failures, ("meeting_delete_step_files",))

    def test_controller_treats_already_absent_files_as_deleted(self):
        app = self._controller()

        with patch(
                "whisper_core.meeting.session.is_safe_session_id",
                return_value=True), patch(
                    "whisper_core.meeting.session.delete_session",
                    return_value=False), patch(
                        "whisper_core.meeting.voice_memory.delete_pending_centroids",
                        return_value=True):
            failures = DesktopApp.delete_meeting(app, self.SESSION_ID)

        self.assertEqual(failures, ())

    def test_controller_does_not_trust_success_while_files_remain(self):
        # soft_delete БРЕШЕ про успіх (повертає шлях у кошику), але тека
        # насправді лишилась — контролер вірить лише фактичному стану диска.
        app = self._controller()
        (self.temp_root / self.SESSION_ID).mkdir()

        with patch(
                "whisper_core.meeting.session.is_safe_session_id",
                return_value=True), patch(
                    "whisper_core.trash.soft_delete",
                    return_value=self.temp_root / ".trash" / self.SESSION_ID), patch(
                        "whisper_core.meeting.voice_memory.delete_pending_centroids",
                        return_value=True):
            failures = DesktopApp.delete_meeting(app, self.SESSION_ID)

        self.assertEqual(failures, ("meeting_delete_step_files",))

    def test_controller_returns_failed_voice_data_deletion(self):
        app = self._controller()

        with patch(
                "whisper_core.meeting.session.is_safe_session_id",
                return_value=True), patch(
                    "whisper_core.meeting.session.delete_session",
                    return_value=True), patch(
                        "whisper_core.meeting.voice_memory.delete_pending_centroids",
                        return_value=False):
            failures = DesktopApp.delete_meeting(app, self.SESSION_ID)

        self.assertEqual(failures, ("meeting_delete_step_voice_memory",))

    def test_page_shows_partial_deletion_warning(self):
        controller = SimpleNamespace(
            delete_meeting=MagicMock(
                return_value=("meeting_delete_step_voice_memory",)),
        )
        page = SimpleNamespace(
            controller=controller,
            _stop_players=MagicMock(),
            refresh=MagicMock(),
        )

        with patch.object(meeting_page, "_meeting_confirm", return_value=True), \
                patch.object(meeting_page, "_meeting_warn") as warn:
            meeting_page.MeetingPage._confirm_delete(page, self.SESSION_ID)

        controller.delete_meeting.assert_called_once_with(self.SESSION_ID)
        page.refresh.assert_called_once()
        warning = warn.call_args.args[2]
        # Жорсткі укр. літерали — assertEqual/assertIn з tr(того самого
        # ключа) не ловлять видалений/зламаний ключ meeting_delete_partial
        # чи meeting_delete_step_voice_memory.
        self.assertIn("видалити дані розрізнення голосів", warning)
        self.assertEqual(
            warning,
            "Нараду видалено не повністю: частина файлів лишилася на диску, "
            "бо ще відкрита в іншій програмі (видалити дані розрізнення "
            "голосів). Закрийте ці програми й повторіть видалення, щоб "
            "прибрати все.")

    def test_locked_plaintext_temp_is_retained_and_reported_after_delete(self):
        app = self._controller()
        session_dir = self.temp_root / self.SESSION_ID
        session_dir.mkdir()
        plain_dir = self.temp_root / "plain"
        plain_dir.mkdir()
        locked_plaintext = plain_dir / "mic.wav"
        locked_plaintext.write_bytes(b"decrypted")

        owner = SimpleNamespace(cleanup=lambda: shutil.rmtree(plain_dir))
        app._meeting_plain_cache = {
            self.SESSION_ID: (owner, str(plain_dir)),
        }
        app._clear_meeting_plain_cache = MethodType(
            DesktopApp._clear_meeting_plain_cache, app)
        app.delete_meeting = MethodType(DesktopApp.delete_meeting, app)
        page = SimpleNamespace(
            controller=app,
            _players=[],
            refresh=MagicMock(),
        )
        page._stop_players = MethodType(meeting_page.MeetingPage._stop_players, page)

        with open(locked_plaintext, "rb"), patch.object(
                meeting_page, "_meeting_confirm", return_value=True), patch.object(
                    meeting_page, "_meeting_warn") as warn, patch(
                        "whisper_core.meeting.voice_memory.delete_pending_centroids",
                        return_value=True):
            meeting_page.MeetingPage._confirm_delete(page, self.SESSION_ID)

            self.assertFalse(session_dir.exists())
            self.assertTrue(locked_plaintext.exists())
            self.assertIn(self.SESSION_ID, app._meeting_plain_cache)
            warning = warn.call_args.args[2]
            # Жорсткий укр. літерал — те саме обґрунтування, що й вище.
            self.assertIn("видалити тимчасову розшифровану копію", warning)


class VoicePendingDeleteTruthfulness(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path.cwd() / f".post49-voice-{uuid.uuid4().hex}"
        self.temp_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def test_locked_pending_file_returns_false(self):
        pending = self.temp_root / "pending.json"
        pending.write_text("{}", encoding="utf-8")
        real_unlink = Path.unlink

        def deny_pending(path, *args, **kwargs):
            if path == pending:
                raise PermissionError("file is locked")
            return real_unlink(path, *args, **kwargs)

        with self.assertLogs(
                "whisper_core.meeting.voice_memory",
                level="ERROR"), patch.object(
                    voice_memory,
                    "_pending_file",
                    return_value=pending), patch.object(
                        Path,
                        "unlink",
                        deny_pending):
            result = voice_memory.delete_pending_centroids(
                SimpleNamespace(), "meeting-id")

        self.assertIs(result, False)
        self.assertTrue(pending.exists())


class DeleteCopyTruthfulness(unittest.TestCase):
    def test_meeting_delete_copy_names_physical_limits_in_both_languages(self):
        expected = {
            "uk": ("цього диска", "фізично сектори", "резервні копії",
                   "системні файли підкачки"),
            "en": ("this disk", "physically erase disk sectors", "backups",
                   "system paging files"),
        }
        for language, phrases in expected.items():
            copy = STRINGS[language]["meeting_delete_confirm"].lower()
            for phrase in phrases:
                with self.subTest(language=language, phrase=phrase):
                    self.assertIn(phrase, copy)


class PrivacyDocumentationTruthfulness(unittest.TestCase):
    def test_document_names_temp_patterns_and_deletion_limits(self):
        privacy = (ROOT / "docs" / "DATA-PRIVACY.md").read_text(
            encoding="utf-8",
        ).lower()
        expected = (
            "balachky-meeting-*",
            "balachky-meeting-media-*",
            "balachky-tts-plain-*",
            "pagefile.sys",
            "swapfile.sys",
            "hiberfil.sys",
            "$recycle.bin",
            "wear-leveling",
        )
        for phrase in expected:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, privacy)


if __name__ == "__main__":
    unittest.main()
