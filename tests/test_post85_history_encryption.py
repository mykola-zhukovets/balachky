import os
import io
import json
import shutil
import time
import unittest
import uuid
from contextlib import contextmanager
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cryptography.exceptions import InvalidTag
from PySide6.QtWidgets import QApplication, QCheckBox

import learn
from fronts.desktop import app as desktop_app
from fronts.desktop.i18n import STRINGS, tr
from fronts.desktop.pages.settings import SettingsPage
from tests.render_nav_smoke import _NavController
from whisper_core import history, profiles
from whisper_core.config import Config
from whisper_core.meeting import storage_crypto
from whisper_core.profiles import Profile
from whisper_core.search_index import SearchIndex


_WORKTREE_TMP = Path(__file__).resolve().parents[1] / "dev"


@contextmanager
def _temporary_directory():
    root = (_WORKTREE_TMP / "post85-test-work" / uuid.uuid4().hex).resolve()
    allowed = (_WORKTREE_TMP / "post85-test-work").resolve()
    if not root.is_relative_to(allowed):
        raise RuntimeError("test path escaped the worktree")
    root.mkdir(parents=True)
    try:
        yield str(root)
    finally:
        shutil.rmtree(root)


def _make_sandbox(root: Path) -> Path:
    default = root / "profiles" / "default"
    default.mkdir(parents=True)
    (default / "terms.toml").write_text("[terms]\n", encoding="utf-8")
    (default / "history.jsonl").write_text(
        json.dumps(
            {"ts": round(time.time()), "raw": "тест", "final": "тест",
             "source": "desktop"},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    (default / "profile.json").write_text('{"memory": true}', encoding="utf-8")
    (root / "profiles" / "state.json").write_text(
        '{"active": "default"}', encoding="utf-8")
    return root


class HistoryEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.dpapi = [
            patch.object(storage_crypto, "_dpapi_protect",
                         side_effect=lambda data: data),
            patch.object(storage_crypto, "_dpapi_unprotect",
                         side_effect=lambda data: data),
        ]
        for item in self.dpapi:
            item.start()

    def tearDown(self):
        for item in reversed(self.dpapi):
            item.stop()

    def test_disabled_keeps_plaintext_and_reads_existing_jsonl(self):
        with _temporary_directory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text(
                '{"ts": 1, "raw": "старий", "final": "старий", '
                '"source": "desktop"}\n',
                encoding="utf-8",
            )

            history.log_history(path, "новий секрет", "новий секрет")

            self.assertIn("старий", path.read_text(encoding="utf-8"))
            self.assertIn("новий секрет", path.read_text(encoding="utf-8"))
            self.assertFalse(history.encrypted_path(path).exists())
            self.assertEqual(
                [record["final"] for _, record in history.read_recent(path)],
                ["новий секрет", "старий"],
            )

    def test_enabled_hides_plaintext_and_reads_and_appends(self):
        with _temporary_directory() as tmp:
            path = Path(tmp) / "history.jsonl"
            history.log_history(path, "перша таємниця", "перша таємниця")

            history.set_encryption(path, True)
            history.log_history(path, "друга таємниця", "друга таємниця")

            encrypted = history.encrypted_path(path)
            self.assertTrue(encrypted.exists())
            self.assertFalse(path.exists())
            disk = encrypted.read_bytes()
            self.assertNotIn("перша таємниця".encode("utf-8"), disk)
            self.assertNotIn("друга таємниця".encode("utf-8"), disk)
            self.assertEqual(
                [record["final"] for _, record in history.read_recent(path)],
                ["друга таємниця", "перша таємниця"],
            )

    def test_enabled_from_first_write_creates_only_encrypted_file(self):
        with _temporary_directory() as tmp:
            path = Path(tmp) / "history.jsonl"

            history.log_history(
                path, "відразу таємно", "відразу таємно", encrypt=True)

            self.assertFalse(path.exists())
            self.assertTrue(history.encrypted_path(path).exists())
            self.assertNotIn(
                "відразу таємно".encode("utf-8"),
                history.encrypted_path(path).read_bytes(),
            )

    def test_encrypted_history_supports_update_delete_and_search(self):
        with _temporary_directory() as tmp:
            path = Path(tmp) / "history.jsonl"
            first = history.log_history(path, "сирий", "перший", encrypt=True)
            history.log_history(path, "другий", "другий", encrypt=True)

            self.assertTrue(
                history.update_final_by_id(path, first["id"], "оновлений"))
            line = next(
                line for line, record in history.read_recent(path)
                if record["final"] == "другий"
            )
            history.delete_line(path, line)
            index = SearchIndex.build(history_paths=[path])

            self.assertEqual(
                [record["final"] for _, record in history.read_recent(path)],
                ["оновлений"],
            )
            self.assertEqual(index.search("оновлений")[0].snippet, "оновлений")

    def test_switching_both_ways_preserves_records(self):
        with _temporary_directory() as tmp:
            path = Path(tmp) / "history.jsonl"
            history.log_history(path, "до", "до")

            history.set_encryption(path, True)
            history.log_history(path, "під шифром", "під шифром")
            history.set_encryption(path, False)
            history.log_history(path, "після", "після")

            self.assertTrue(path.exists())
            self.assertFalse(history.encrypted_path(path).exists())
            self.assertEqual(
                [record["final"] for _, record in history.read_recent(path)],
                ["після", "під шифром", "до"],
            )

    def test_clear_history_archives_encrypted_copy(self):
        with _temporary_directory() as tmp:
            profile = Profile("default", Path(tmp))
            history.log_history(
                profile.history_path, "секрет", "секрет", encrypt=True)

            backup = profile.reset_memory()

            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            self.assertTrue(backup.name.endswith(".bak.jsonl.enc"))
            self.assertFalse(history.encrypted_path(profile.history_path).exists())
            self.assertEqual(history.read_recent(profile), [])

    def test_clear_history_archives_both_copies_after_interrupted_migration(self):
        with _temporary_directory() as tmp:
            profile = Profile("default", Path(tmp))
            history.log_history(
                profile.history_path, "канонічний секрет", "канонічний секрет",
                encrypt=True,
            )
            profile.history_path.write_text(
                '{"ts": 1, "raw": "залишок", "final": "залишок", '
                '"source": "desktop"}\n',
                encoding="utf-8",
            )

            backup = profile.reset_memory()

            self.assertIsNotNone(backup)
            self.assertFalse(profile.history_path.exists())
            self.assertFalse(history.encrypted_path(profile.history_path).exists())
            self.assertEqual(history.read_recent(profile), [])
            backups = list(profile.dir.glob("history.*.bak.jsonl*"))
            self.assertEqual(len(backups), 2)
            self.assertTrue(any(path.name.endswith(".enc") for path in backups))
            self.assertTrue(any(".stale." in path.name for path in backups))

    def test_encrypted_history_is_used_by_learning_and_profile_count(self):
        with _temporary_directory() as tmp:
            profile = Profile("default", Path(tmp))
            history.log_history(
                profile.history_path,
                "коростеняни коростеняни",
                "коростеняни коростеняни",
                encrypt=True,
            )

            candidates = learn.analyze(
                profile.history_path, stopwords=set(), min_count=2)
            output = io.StringIO()
            with patch.object(
                    profiles, "get_active", return_value=profile), patch.object(
                        profiles, "list_profiles",
                        return_value=[profile]), redirect_stdout(output):
                profiles._main(["list"])

            self.assertEqual(candidates, [("коростеняни", 2)])
            self.assertIn("записів: 1", output.getvalue())

    def test_each_profile_uses_its_own_key(self):
        with _temporary_directory() as tmp:
            first = Path(tmp) / "first" / "history.jsonl"
            second = Path(tmp) / "second" / "history.jsonl"
            first.parent.mkdir()
            second.parent.mkdir()
            history.log_history(first, "перший секрет", "перший секрет",
                                encrypt=True)
            history.log_history(second, "другий секрет", "другий секрет",
                                encrypt=True)

            first_key = storage_crypto.ensure_dek(first.parent)
            second_key = storage_crypto.ensure_dek(second.parent)

            self.assertNotEqual(first_key, second_key)
            self.assertNotEqual(
                (first.parent / ".vaultkey").read_bytes(),
                (second.parent / ".vaultkey").read_bytes(),
            )
            with self.assertRaises(InvalidTag):
                storage_crypto.decrypt_to_memory(
                    history.encrypted_path(first),
                    second_key,
                    context="balachky-dictation-history-v1",
                )


class HistoryEncryptionConfigTests(unittest.TestCase):
    def test_default_is_off_and_roundtrip_preserves_choice(self):
        self.assertFalse(Config().history_encrypt)
        with _temporary_directory() as tmp:
            path = Path(tmp) / "config.toml"
            Config(history_encrypt=True).save(path)
            self.assertTrue(Config.load(path).history_encrypt)

    def test_save_reports_atomic_write_failure(self):
        with _temporary_directory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("history_encrypt = false\n", encoding="utf-8")
            with patch(
                    "whisper_core.config._atomic_write_text",
                    side_effect=OSError("disk unavailable")):
                saved = Config(history_encrypt=True).save(path)

            self.assertIs(saved, False)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "history_encrypt = false\n",
            )


class HistoryEncryptionControllerTests(unittest.TestCase):
    def test_failed_multi_profile_migration_rolls_back_previous_profiles(self):
        first = SimpleNamespace(history_path=Path("first/history.jsonl"))
        second = SimpleNamespace(history_path=Path("second/history.jsonl"))
        config = SimpleNamespace(history_encrypt=True, save=Mock())
        controller = SimpleNamespace(
            cfg=config, tray=SimpleNamespace(notify=Mock()))
        calls = []

        def migrate(path, enabled):
            calls.append((path, enabled))
            if path == second.history_path and not enabled:
                raise OSError("second profile is unavailable")

        with patch.object(
                desktop_app.profiles, "list_profiles",
                return_value=[first, second]), patch.object(
                desktop_app, "set_history_file_encryption",
                    side_effect=migrate), patch.object(
                        desktop_app, "history_file_is_encrypted",
                        side_effect=[True, True]), patch.object(
                            desktop_app.logging, "exception"):
            result = desktop_app.DesktopApp.set_history_encryption(
                controller, False)

        self.assertFalse(result)
        self.assertEqual(
            calls,
            [
                (first.history_path, False),
                (second.history_path, False),
                (first.history_path, True),
            ],
        )
        self.assertTrue(config.history_encrypt)
        config.save.assert_not_called()

    def test_config_save_failure_rolls_back_histories_and_in_memory_flag(self):
        first = SimpleNamespace(history_path=Path("first/history.jsonl"))
        second = SimpleNamespace(history_path=Path("second/history.jsonl"))
        config = SimpleNamespace(history_encrypt=False, save=Mock(return_value=False))
        controller = SimpleNamespace(
            cfg=config, tray=SimpleNamespace(notify=Mock()))
        calls = []

        with patch.object(
                desktop_app.profiles, "list_profiles",
                return_value=[first, second]), patch.object(
                    desktop_app, "set_history_file_encryption",
                    side_effect=lambda path, enabled: calls.append(
                        (path, enabled))), patch.object(
                            desktop_app, "history_file_is_encrypted",
                            side_effect=[False, False]), patch.object(
                                desktop_app.logging, "exception"):
            result = desktop_app.DesktopApp.set_history_encryption(
                controller, True)

        self.assertFalse(result)
        self.assertEqual(
            calls,
            [
                (first.history_path, True),
                (second.history_path, True),
                (second.history_path, False),
                (first.history_path, False),
            ],
        )
        self.assertFalse(config.history_encrypt)
        controller.tray.notify.assert_called_once_with(
            tr("history_encrypt_error"))


class HistoryEncryptionSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_privacy_toggle_is_localized_and_accessible(self):
        with _temporary_directory() as tmp:
            controller = _NavController(_make_sandbox(Path(tmp)))
            toggled = []
            controller.set_history_encryption = (
                lambda enabled: toggled.append(enabled) or True)
            page = SettingsPage(controller)
            checkboxes = {
                checkbox.accessibleName(): checkbox
                for checkbox in page.findChildren(QCheckBox)
            }

            self.assertIn(tr("set_history_encrypt"), checkboxes)
            checkbox = checkboxes[tr("set_history_encrypt")]
            self.assertFalse(checkbox.isChecked())
            checkbox.click()
            self.assertEqual(toggled, [True])
            self.assertIn("текст", STRINGS["uk"]["set_history_encrypt"].lower())
            self.assertIn("text", STRINGS["en"]["set_history_encrypt"].lower())
            for lang in ("uk", "en"):
                self.assertTrue(STRINGS[lang]["set_history_encrypt"])
                self.assertTrue(STRINGS[lang]["set_history_encrypt_hint"])


if __name__ == "__main__":
    unittest.main()
