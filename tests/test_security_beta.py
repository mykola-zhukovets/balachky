"""Регресії безпекової фікс-хвилі перед бетою."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop import app as app_module
from fronts.desktop.i18n import STRINGS
from fronts.desktop.pages import meeting as meeting_page
from whisper_core import punctuator
from whisper_core.config import Config
from whisper_core.history import log_history
from whisper_core.meeting import session
from whisper_core.profiles import Profile

MeetingPage = meeting_page.MeetingPage


class _Signal:
    def __init__(self):
        self.emits = []

    def emit(self, value):
        self.emits.append(value)


class _Tray:
    def __init__(self):
        self.notes = []

    def notify(self, text):
        self.notes.append(text)


class _Label:
    def __init__(self):
        self.text = ""
        self.visible = False
        self.accessible_name = ""

    def setText(self, text):
        self.text = text

    def setVisible(self, visible):
        self.visible = bool(visible)

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def setAccessibleName(self, name):
        self.accessible_name = name


class CorruptProfileFailClosedTests(unittest.TestCase):
    def test_corrupt_profile_json_disables_memory_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Profile("default", Path(tmp))
            profile._meta_path.write_text("{ broken", encoding="utf-8")
            self.assertFalse(profile.memory_enabled)
            self.assertTrue(profile.meta_corrupt)
            self.assertIsNone(log_history(
                profile.history_path, "таємний текст", "таємний текст",
                enabled=profile.memory_enabled))
            self.assertFalse(profile.history_path.exists())

    def test_missing_profile_json_keeps_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Profile("default", Path(tmp))
            self.assertTrue(profile.memory_enabled)
            self.assertFalse(profile.meta_corrupt)

    def test_set_memory_keeps_old_json_when_atomic_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Profile("default", Path(tmp))
            profile._meta_path.write_text(
                json.dumps({"memory": False}), encoding="utf-8")
            with patch("whisper_core.profiles.os.replace",
                       side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    profile.set_memory(True)
            self.assertEqual(
                json.loads(profile._meta_path.read_text(encoding="utf-8")),
                {"memory": False})

    def test_profile_warning_is_visible_beside_memory_switch(self):
        label = _Label()
        page = SimpleNamespace(
            controller=SimpleNamespace(
                profile=SimpleNamespace(meta_corrupt=True)),
            _profile_meta_warning=label)
        from fronts.desktop.pages.vocab import VocabPage
        VocabPage._refresh_profile_meta_warning(page)
        self.assertTrue(label.visible)
        # Warning must not send an untechnical user to hand-edit the JSON file;
        # it must point at the in-app switch that safely rewrites it instead.
        self.assertNotIn("profile.json", label.text)
        self.assertIn("Зберігати нові розшифровки в історії", label.text)
        self.assertTrue(label.accessible_name)


class CorruptConfigFailClosedTests(unittest.TestCase):
    def test_config_corruption_warning_is_visible(self):
        label = _Label()
        page = SimpleNamespace(
            controller=SimpleNamespace(cfg=SimpleNamespace(
                _config_corrupt=True,
                _config_recovered_from_backup=False)),
            _config_corrupt_warning=label)
        MeetingPage._refresh_config_warning(page)
        self.assertTrue(label.visible)
        self.assertIn("безпечне", label.text)
        self.assertTrue(label.accessible_name)

    def test_corrupt_config_without_backup_uses_safe_encryption_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('meeting_encrypt = "unterminated', encoding="utf-8")
            loaded = Config.load(path)
            self.assertTrue(loaded._config_corrupt)
            self.assertFalse(loaded._config_recovered_from_backup)
            self.assertTrue(loaded.meeting_encrypt)

    def test_config_backup_roundtrip_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            Config(meeting_encrypt=True).save(path)
            Config(meeting_encrypt=False).save(path)
            backup = Path(str(path) + ".bak")
            self.assertTrue(backup.exists())
            self.assertTrue(
                Config.load(backup).meeting_encrypt,
                "backup must retain the previous valid security policy")

            path.write_text("{ broken", encoding="utf-8")
            loaded = Config.load(path)
            self.assertTrue(loaded._config_corrupt)
            self.assertTrue(loaded._config_recovered_from_backup)
            self.assertTrue(loaded.meeting_encrypt)

    def _config_with_backup(self, path):
        """config.toml + .bak, обидва з meeting_encrypt = true."""
        Config(meeting_encrypt=True).save(path)   # .bak ще нема
        Config(meeting_encrypt=True).save(path)   # тепер попередній файл → .bak
        backup = Path(str(path) + ".bak")
        self.assertTrue(backup.exists())
        return backup

    def test_empty_config_with_backup_is_treated_as_corrupt(self):
        # Обірваний запис лишає ВАЛІДНИЙ TOML (порожній файл) — парсер мовчить,
        # а політика шифрування тихо падає в дефолт. Це і є fail-open В-05.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            self._config_with_backup(path)
            path.write_text("", encoding="utf-8")
            loaded = Config.load(path)
            self.assertTrue(loaded.meeting_encrypt)
            self.assertTrue(loaded._config_corrupt)
            self.assertTrue(loaded._config_recovered_from_backup)

    def test_truncated_but_valid_config_with_backup_is_treated_as_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            self._config_with_backup(path)
            lines = path.read_text(encoding="utf-8").splitlines()
            cut = next(i for i, ln in enumerate(lines)
                       if ln.startswith("meeting_encrypt"))
            path.write_text("\n".join(lines[:cut]) + "\n", encoding="utf-8")
            import tomllib
            tomllib.loads(path.read_text(encoding="utf-8"))  # файл валідний!
            loaded = Config.load(path)
            self.assertTrue(loaded.meeting_encrypt)
            self.assertTrue(loaded._config_corrupt)
            self.assertTrue(loaded._config_recovered_from_backup)

    def test_first_run_without_config_is_not_punished(self):
        # Легітимний перший запуск: config.toml НЕ існує — дефолти, без банера.
        with tempfile.TemporaryDirectory() as tmp:
            loaded = Config.load(Path(tmp) / "config.toml")
            self.assertEqual(loaded.meeting_encrypt, Config().meeting_encrypt)
            self.assertFalse(loaded._config_corrupt)
            self.assertFalse(loaded._config_recovered_from_backup)

    def test_config_save_keeps_old_toml_when_atomic_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            old = "meeting_encrypt = true\n"
            path.write_text(old, encoding="utf-8")
            with patch("whisper_core.config.os.replace",
                       side_effect=OSError("simulated crash")):
                Config(meeting_encrypt=False).save(path)
            self.assertEqual(path.read_text(encoding="utf-8"), old)


class MeetingPlaintextRecoveryTests(unittest.TestCase):
    def _crashed_session(self, root: Path) -> Path:
        meeting = root / "2026-07-23_10-11-12"
        (meeting / "mic").mkdir(parents=True)
        (meeting / "mic" / "0000.f32").write_bytes(b"plaintext audio")
        meta = session.MeetingMeta(
            schema=2, id=meeting.name, created=1,
            status=session.STATUS_RECORDING,
            preset=session.PRESET_ONLYMIC, sources=["mic"])
        session.atomic_write_json(
            meeting / "meeting.json", json.loads(meta.to_json()))
        return meeting

    def test_ui_never_claims_encrypted_while_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            meeting = self._crashed_session(Path(tmp))
            self.assertEqual(
                meeting_page.classify_meeting_security(
                    meeting, session.STATUS_RECORDING),
                "open")

    def test_classifier_requires_sealed_metadata_and_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            meeting = Path(tmp)
            (meeting / "meeting.json.enc").write_bytes(b"sealed")
            self.assertEqual(
                meeting_page.classify_meeting_security(
                    meeting, session.STATUS_DONE),
                "encrypted")
            (meeting / "encrypting.marker").write_text("{}", encoding="utf-8")
            self.assertEqual(
                meeting_page.classify_meeting_security(
                    meeting, session.STATUS_DONE),
                "open")
            self.assertEqual(
                meeting_page.classify_meeting_security(
                    meeting, session.STATUS_DONE, materialized=True),
                "open_view")

    def test_pending_plaintext_counted_then_cleared_after_encryption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meeting = self._crashed_session(root)
            session.mark_encryption_pending(
                meeting, session.STATUS_INTERRUPTED)
            self.assertEqual(session.count_pending_encryption(root), 1)
            session.encrypt_session(meeting, b"k" * 32)
            self.assertEqual(session.count_pending_encryption(root), 0)

    def test_crash_restart_encrypts_orphan_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meeting = self._crashed_session(root)
            signal = _Signal()
            app = SimpleNamespace(
                cfg=SimpleNamespace(meeting_encrypt=True),
                _meetings_root=lambda: root,
                meeting_plaintext_pending=signal,
                tray=_Tray())
            with patch(
                    "whisper_core.meeting.storage_crypto.ensure_dek",
                    return_value=b"k" * 32):
                app_module._resume_meeting_encryption(app)

            self.assertFalse((meeting / "meeting.json").exists())
            self.assertFalse((meeting / "mic" / "0000.f32").exists())
            self.assertTrue((meeting / "meeting.json.enc").exists())
            self.assertEqual(app._meeting_plaintext_count, 0)
            self.assertEqual(signal.emits[-1], 0)

    def test_resume_locked_vault_sets_warning_flag_and_banner(self):
        from whisper_core.meeting.storage_crypto import VaultPasswordRequired
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._crashed_session(root)
            signal = _Signal()
            tray = _Tray()
            app = SimpleNamespace(
                cfg=SimpleNamespace(meeting_encrypt=True),
                _meetings_root=lambda: root,
                meeting_plaintext_pending=signal,
                tray=tray)
            with patch(
                    "whisper_core.meeting.storage_crypto.ensure_dek",
                    side_effect=VaultPasswordRequired("locked")):
                app_module._resume_meeting_encryption(app)

            self.assertEqual(app._meeting_plaintext_count, 1)
            self.assertEqual(signal.emits[-1], 1)
            self.assertTrue(tray.notes)

            label = _Label()
            page = SimpleNamespace(_pending_plaintext_warning=label)
            MeetingPage._on_pending_plaintext(page, 1)
            self.assertTrue(label.visible)
            self.assertIn("1", label.text)
            self.assertTrue(label.accessible_name)
            MeetingPage._on_pending_plaintext(page, 0)
            self.assertFalse(label.visible)


class HonestIntegrityCopyTests(unittest.TestCase):
    def test_recording_copy_says_files_are_unencrypted(self):
        self.assertIn("завершені", STRINGS["uk"]["set_meeting_encrypt"])
        self.assertIn("під час запису", STRINGS["uk"]["set_meeting_encrypt_hint"].lower())
        self.assertIn("відкрито", STRINGS["uk"]["set_meeting_encrypt_hint"].lower())
        self.assertIn("completed", STRINGS["en"]["set_meeting_encrypt"].lower())
        self.assertIn("during recording", STRINGS["en"]["set_meeting_encrypt_hint"].lower())
        self.assertIn("unencrypted", STRINGS["en"]["set_meeting_encrypt_hint"].lower())

    def test_integrity_copy_calls_it_checksum_not_signature(self):
        uk = " ".join(
            STRINGS["uk"][key]
            for key in ("meeting_integrity_hint", "meeting_integrity_ok",
                        "meeting_evidence_warn")).lower()
        en = " ".join(
            STRINGS["en"][key]
            for key in ("meeting_integrity_hint", "meeting_integrity_ok",
                        "meeting_evidence_warn")).lower()
        self.assertIn("контрольн", uk)
        self.assertIn("цілісн", uk)
        self.assertIn("не є електронним підписом", uk)
        self.assertNotIn("автентично", uk)
        self.assertNotIn("засвідчено", uk)
        self.assertIn("checksum", en)
        self.assertIn("integrity", en)
        self.assertIn("not an electronic signature", en)
        self.assertNotIn("authentic", en)
        self.assertNotIn("certified", en)


class OfflinePunctuatorTests(unittest.TestCase):
    def test_load_model_is_offline_and_does_not_log_download(self):
        config_type = Mock(return_value=object())
        model_type = Mock(return_value=object())
        model_module = types.ModuleType(
            "punctuators.models.punc_cap_seg_model")
        model_module.PunctCapSegConfigONNX = config_type
        model_module.PunctCapSegModelONNX = model_type
        package = types.ModuleType("punctuators")
        models = types.ModuleType("punctuators.models")
        package.models = models
        with patch.object(punctuator, "available", return_value=True), \
                patch.object(punctuator, "_assets_valid", return_value=True), \
                patch.dict(sys.modules, {
                    "punctuators": package,
                    "punctuators.models": models,
                    "punctuators.models.punc_cap_seg_model": model_module}), \
                patch.object(punctuator.netlog, "record") as net_record:
            model = punctuator.load_model("C:/local/punctuator")

        self.assertIsNotNone(model)
        config_type.assert_called_once_with(
            directory="C:/local/punctuator",
            spe_filename="spe_unigram_64k_lowercase_47lang.model",
            model_filename="punct_cap_seg_47lang.onnx",
            config_filename="config.yaml")
        model_type.assert_called_once_with(config_type.return_value)
        net_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
