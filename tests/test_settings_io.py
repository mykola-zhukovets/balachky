import json
import tempfile
import zipfile
import unittest
from pathlib import Path

from whisper_core import settings_io as sio


def _seed_user_dir(base: Path, *, config="model_name = \"large-v3\"\n",
                   snippets="[t]\n", terms="[terms]\n",
                   history='{"ts": 1, "final": "секрет"}\n'):
    """Створити типову теку користувача з config/snippets/профілем default."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "config.toml").write_text(config, encoding="utf-8")
    (base / "snippets.toml").write_text(snippets, encoding="utf-8")
    (base / "context_profiles.toml").write_text("# ctx\n", encoding="utf-8")
    pdir = base / "profiles" / "default"
    pdir.mkdir(parents=True)
    (base / "profiles" / "state.json").write_text('{"active": "default"}',
                                                  encoding="utf-8")
    (pdir / "terms.toml").write_text(terms, encoding="utf-8")
    (pdir / "profile.json").write_text('{"memory": true}', encoding="utf-8")
    (pdir / "history.jsonl").write_text(history, encoding="utf-8")   # приватне
    return base


class ExportTests(unittest.TestCase):
    def test_archive_has_settings_and_excludes_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _seed_user_dir(Path(tmp))
            zip_path = Path(tmp) / "out.zip"
            sio.export_settings(
                zip_path, config_path=base / "config.toml",
                snippets_path=base / "snippets.toml",
                context_profiles_path=base / "context_profiles.toml",
                profiles_root=base)
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            self.assertIn(sio.MANIFEST_NAME, names)
            self.assertIn("manifest.json", names)
            self.assertIn("config.toml", names)
            self.assertIn("snippets.toml", names)
            self.assertIn("context_profiles.toml", names)
            self.assertIn("profiles/default/terms.toml", names)
            self.assertIn("profiles/default/profile.json", names)
            self.assertIn("profiles/state.json", names)
            # приватна історія та бекапи НЕ входять
            self.assertNotIn("profiles/default/history.jsonl", names)
            self.assertTrue(sio.is_valid_archive(zip_path))

    def test_manifest_json_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _seed_user_dir(Path(tmp))
            zip_path = Path(tmp) / "out.zip"
            sio.export_settings(
                zip_path, config_path=base / "config.toml",
                snippets_path=base / "snippets.toml",
                context_profiles_path=base / "context_profiles.toml",
                profiles_root=base, version="1.5.0")
            with zipfile.ZipFile(zip_path) as zf:
                manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest_data.get("app_version"), "1.5.0")
            self.assertIn("created_at", manifest_data)
            self.assertIn("contents", manifest_data)
            self.assertIn("config.toml", manifest_data["contents"])

    def test_export_excludes_encryption_secrets_and_meetings_guarantee(self):
        """Окремий тест-гарантія: секрети шифрування (DPAPI .vaultkey), наради,
        аудіозаписи, історія та кеші моделей СУРОВО НЕ потрапляють у zip."""
        with tempfile.TemporaryDirectory() as tmp:
            base = _seed_user_dir(Path(tmp))
            # Створюємо чутливі та побічні файли, які не мають експортуватись
            (base / ".vaultkey").write_text('{"mode": "dpapi", "wrapped_dek": "SECRET"}', encoding="utf-8")
            (base / "meetings").mkdir()
            (base / "meetings" / ".vaultkey").write_text('{"mode": "dpapi", "wrapped_dek": "SECRET"}', encoding="utf-8")
            (base / "meetings" / "session1.enc").write_bytes(b"SECRET_ENCRYPTED_MEETING")
            (base / "recordings").mkdir()
            (base / "recordings" / "rec.wav").write_bytes(b"AUDIO_DATA")
            (base / "components").mkdir()
            (base / "components" / "model.onnx").write_bytes(b"MODEL_CACHE")

            zip_path = Path(tmp) / "export_secret_check.zip"
            sio.export_settings(
                zip_path, config_path=base / "config.toml",
                snippets_path=base / "snippets.toml",
                context_profiles_path=base / "context_profiles.toml",
                profiles_root=base)

            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()

            for name in names:
                self.assertNotIn(".vaultkey", name)
                self.assertNotIn("meetings", name)
                self.assertNotIn("recordings", name)
                self.assertNotIn("components", name)
                self.assertNotIn("history.jsonl", name)
                self.assertNotIn(".enc", name)
                self.assertNotIn(".wav", name)

    def test_export_includes_extended_dictionaries(self):
        """Перевіряємо експорт додаткових словників (phrases.toml, self-learning.jsonl, learned terms/phrases)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = _seed_user_dir(Path(tmp))
            pdir = base / "profiles" / "default"
            (pdir / "phrases.toml").write_text("[phrases]\n", encoding="utf-8")
            (pdir / "self-learning.jsonl").write_text('{"correction": 1}\n', encoding="utf-8")
            (pdir / "terms.learned.toml").write_text("[learned]\n", encoding="utf-8")
            (pdir / "phrases.learned.toml").write_text("[learned_phrases]\n", encoding="utf-8")
            (pdir / "macros.toml").write_text("[macros]\n", encoding="utf-8")

            zip_path = Path(tmp) / "ext_export.zip"
            sio.export_settings(
                zip_path, config_path=base / "config.toml",
                snippets_path=base / "snippets.toml",
                context_profiles_path=base / "context_profiles.toml",
                profiles_root=base)

            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())

            self.assertIn("profiles/default/phrases.toml", names)
            self.assertIn("profiles/default/self-learning.jsonl", names)
            self.assertIn("profiles/default/terms.learned.toml", names)
            self.assertIn("profiles/default/phrases.learned.toml", names)
            self.assertIn("profiles/default/macros.toml", names)


class ImportTests(unittest.TestCase):
    def test_import_overwrites_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _seed_user_dir(tmp / "src", config="model_name = \"turbo\"\n")
            dst = _seed_user_dir(tmp / "dst", config="model_name = \"OLD\"\n")
            archive = tmp / "exp.zip"
            sio.export_settings(
                archive, config_path=src / "config.toml",
                snippets_path=src / "snippets.toml",
                context_profiles_path=src / "context_profiles.toml",
                profiles_root=src)
            backup = tmp / "backup.zip"
            made = sio.import_settings(archive, user_dir=dst,
                                       profiles_root=dst, backup_path=backup)
            # цільовий config перезаписано вмістом архіву
            self.assertIn("turbo", (dst / "config.toml").read_text(encoding="utf-8"))
            # бекап зроблено і містить СТАРИЙ стан
            self.assertEqual(made, backup)
            self.assertTrue(sio.is_valid_archive(backup))
            with zipfile.ZipFile(backup) as zf:
                self.assertIn("OLD", zf.read("config.toml").decode("utf-8"))
            # історія цілі не постраждала
            self.assertTrue((dst / "profiles" / "default" / "history.jsonl").exists())

    def test_import_creates_backup_directory_in_user_dir(self):
        """Імпорт із створенням теки backup-YYYY-MM-DD у теці даних користувача."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _seed_user_dir(tmp / "src", config="model_name = \"new_val\"\n")
            dst = _seed_user_dir(tmp / "dst", config="model_name = \"old_val\"\n")
            archive = tmp / "exp.zip"
            sio.export_settings(
                archive, config_path=src / "config.toml",
                snippets_path=src / "snippets.toml",
                context_profiles_path=src / "context_profiles.toml",
                profiles_root=src)

            backup_dir = sio.import_settings_with_dir_backup(archive, user_dir=dst, profiles_root=dst)
            self.assertTrue(backup_dir.exists())
            self.assertTrue(backup_dir.is_dir())
            self.assertTrue(backup_dir.name.startswith("backup-"))
            # Перевіряємо що всередині бекап-теки лежить старий config.toml
            self.assertIn("old_val", (backup_dir / "config.toml").read_text(encoding="utf-8"))
            # І що у dst застосовано новий конфіг
            self.assertIn("new_val", (dst / "config.toml").read_text(encoding="utf-8"))

    def test_corrupt_archive_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            junk = tmp / "junk.zip"
            junk.write_bytes(b"not a zip at all")
            with self.assertRaises(sio.SettingsArchiveError):
                sio.import_settings(junk, user_dir=tmp)

    def test_valid_zip_without_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            foreign = tmp / "foreign.zip"
            with zipfile.ZipFile(foreign, "w") as zf:
                zf.writestr("config.toml", "model_name = \"evil\"\n")
            self.assertFalse(sio.is_valid_archive(foreign))
            with self.assertRaises(sio.SettingsArchiveError):
                sio.import_settings(foreign, user_dir=tmp)

    def test_inspect_archive_returns_metadata_and_missing(self):
        """Перевіряє функцію inspect_archive для діалогу підтвердження імпорту."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _seed_user_dir(tmp / "src")
            # Видаляємо context_profiles.toml для перевірки відображення відсутніх компонентів
            (src / "context_profiles.toml").unlink()
            archive = tmp / "partial.zip"
            sio.export_settings(
                archive, config_path=src / "config.toml",
                snippets_path=src / "snippets.toml",
                context_profiles_path=src / "context_profiles.toml",
                profiles_root=src, version="2.0.0")

            info = sio.inspect_archive(archive, current_version="1.0.0")
            self.assertEqual(info["app_version"], "2.0.0")
            self.assertTrue(info["is_newer_version"])
            self.assertIn("config.toml", info["files"])
            self.assertIn("context_profiles.toml", info["missing_components"])


if __name__ == "__main__":
    unittest.main()

