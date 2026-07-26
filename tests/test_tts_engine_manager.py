"""Тести менеджера завантаження, перевірки підпису, сумісності, безпеки архіву та життєвого циклу рушія TTS."""
from __future__ import annotations

import base64
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from whisper_core.tts.engine_manager import (
    ArchiveValidationError, EngineSelfTestError,
    IncompatibleVersionError, InsufficientDiskSpaceError, NoPublicKeyError,
    SignatureError, check_disk_space, compute_key_id,
    delete_engine, fetch_engine_manifest, install_engine_from_manifest,
    run_engine_selftest, safe_extract_zip, set_test_public_key,
    signing_message, validate_manifest_compatibility,
    verify_archive_hash, verify_manifest_signature
)


class TestTtsEngineManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test-tts-engine-"))
        # Створення тестової пари ключів Ed25519
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        self.private_key = Ed25519PrivateKey.generate()
        self.pub_bytes = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.pub_b64 = base64.b64encode(self.pub_bytes).decode("ascii")
        self.key_id = compute_key_id(self.pub_bytes)

        set_test_public_key(self.pub_b64)

        self.valid_manifest = {
            "schema_version": 1,
            "engine_version": "1.0.0",
            "protocol_version": 1,
            "min_app_version": "1.2.3",
            "platform": "win-x64",
            "download_url": "https://example.com/engine.zip",
            "archive_size_bytes": 1000,
            "extracted_size_bytes": 5000,
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "executable_relative_path": "balachky-tts-worker.exe",
        }
        # Підпис канонічного повідомлення
        msg = signing_message(self.valid_manifest)
        sig = self.private_key.sign(msg)
        self.valid_manifest["signature"] = base64.b64encode(sig).decode("ascii")
        self.valid_manifest["signature_key_id"] = self.key_id

    def tearDown(self):
        set_test_public_key("")
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. Відсутній відкритий ключ -> завантаження заборонено
    def test_no_public_key_rejected(self):
        set_test_public_key("")
        with self.assertRaises(NoPublicKeyError):
            verify_manifest_signature(self.valid_manifest)

    # 2. Маніфест із битим підписом -> відхилено
    def test_broken_signature_rejected(self):
        broken_manifest = dict(self.valid_manifest)
        broken_manifest["signature"] = base64.b64encode(b"0" * 64).decode("ascii")
        with self.assertRaises(SignatureError):
            verify_manifest_signature(broken_manifest)

    # 3. Маніфест із чужим key_id -> відхилено
    def test_foreign_key_id_rejected(self):
        foreign_manifest = dict(self.valid_manifest)
        foreign_manifest["signature_key_id"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        with self.assertRaises(SignatureError):
            verify_manifest_signature(foreign_manifest)

    # 4. Невідповідність sha256 архіву -> відхилено та видалено
    def test_sha256_mismatch_removes_file(self):
        archive_path = self.temp_dir / "bad.zip"
        archive_path.write_bytes(b"corrupt data")
        wrong_hash = "0" * 64
        with self.assertRaises(ArchiveValidationError):
            verify_archive_hash(archive_path, wrong_hash)
        self.assertFalse(archive_path.exists())

    # 5. Архів з ".." в імені -> відкинуто (Zip Slip)
    def test_malicious_zip_path_rejected(self):
        archive_path = self.temp_dir / "malicious.zip"
        with zipfile.ZipFile(archive_path, "w") as z:
            z.writestr("../evil.exe", b"malicious code")

        stage_dir = self.temp_dir / "stage"
        with self.assertRaises(ArchiveValidationError):
            safe_extract_zip(archive_path, stage_dir)

    # БЛОКЕР 1: Відносний шлях виконуваного файла з '..' відхиляється ДО запуску (без subprocess.run)
    @patch("subprocess.run")
    def test_malicious_executable_relative_path_rejected_before_execution(self, mock_run):
        archive_path = self.temp_dir / "valid.zip"
        with zipfile.ZipFile(archive_path, "w") as z:
            z.writestr("balachky-tts-worker.exe", b"fake exe")

        bad_manifest = dict(self.valid_manifest)
        bad_manifest["executable_relative_path"] = r"..\..\..\..\Windows\System32\cmd.exe"

        with self.assertRaises(ArchiveValidationError):
            install_engine_from_manifest(bad_manifest, archive_path)

        # ДОКАЗ БЛОКERА 1: subprocess.run ЖОДНОГО РАЗУ не викликався!
        mock_run.assert_not_called()

    # БЛОКЕР 2: Перевірка сумісності викликається у робочому шляху install_engine_from_manifest
    def test_install_engine_rejects_incompatible_manifest_in_working_path(self):
        archive_path = self.temp_dir / "test.zip"
        archive_path.write_bytes(b"fake")

        incompatible_manifest = dict(self.valid_manifest)
        incompatible_manifest["protocol_version"] = 999

        with self.assertRaises(IncompatibleVersionError):
            install_engine_from_manifest(incompatible_manifest, archive_path)

    # БЛОКЕР 3: Проміжний випадок — вивід містить лише synth=ok без torch=present -> ВІДМОВА
    @patch("subprocess.run")
    def test_selftest_rejects_synth_ok_without_torch_present(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="balachky-tts-worker v1.0.0: synth=ok compute=cpu",
            stderr=""
        )
        fake_exe = self.temp_dir / "balachky-tts-worker.exe"
        fake_exe.write_bytes(b"exe")

        # Обов'язкова відмова з EngineSelfTestError
        with self.assertRaises(EngineSelfTestError) as cm:
            run_engine_selftest(fake_exe)
        self.assertIn("torch=present", str(cm.exception))

    # БЛОКЕР 4: Мережеве завантаження маніфесту
    @patch("urllib.request.urlopen")
    def test_fetch_engine_manifest_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"schema_version": 1, "engine_version": "1.0.0"}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        manifest = fetch_engine_manifest("https://example.com/manifest.json")
        self.assertEqual(manifest["engine_version"], "1.0.0")

    # БЛОКЕР 5: Перевірка вільного місця враховує архів + розпаковане (піковий обсяг)
    @patch("shutil.disk_usage")
    def test_check_disk_space_includes_archive_and_extracted(self, mock_usage):
        # Доступно 5500 байтів. Розпаковано = 5000, Архів = 1000. Піковий = 6000.
        mock_usage.return_value = MagicMock(free=5500)

        # З лише extracted (5000 < 5500) пройшло б помилково, але з archive (6000 > 5500) дає InsufficientDiskSpaceError
        with self.assertRaises(InsufficientDiskSpaceError) as cm:
            check_disk_space(extracted_size_bytes=5000, archive_size_bytes=1000, target_dir=self.temp_dir)
        self.assertEqual(cm.exception.required_bytes, 6000)

    # min_app_version вище за поточну -> зрозуміла відмова
    def test_incompatible_min_app_version(self):
        future_manifest = dict(self.valid_manifest)
        future_manifest["min_app_version"] = "99.0.0"
        with self.assertRaises(IncompatibleVersionError):
            validate_manifest_compatibility(future_manifest, app_version="1.2.3")

    # Успішний selftest із torch=present
    @patch("subprocess.run")
    def test_selftest_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="balachky-tts-worker v1.0.0: torch=present compute=cpu synth=ok",
            stderr=""
        )
        fake_exe = self.temp_dir / "balachky-tts-worker.exe"
        fake_exe.write_bytes(b"exe")

        # Не має піднімати винятків
        run_engine_selftest(fake_exe)

    # Видалення рушія зупиняє процес першим
    @patch("whisper_core.tts.sidecar.shutdown_all")
    def test_delete_engine_shuts_down_process_first(self, mock_shutdown_all):
        mock_sidecar_shutdown = MagicMock()
        with patch("whisper_core.paths.tts_engine_dir", return_value=self.temp_dir / "engine"):
            engine_dir = self.temp_dir / "engine"
            engine_dir.mkdir(parents=True, exist_ok=True)
            (engine_dir / "balachky-tts-worker.exe").write_bytes(b"exe")

            delete_engine(sidecar_shutdown_fn=mock_sidecar_shutdown)

            mock_sidecar_shutdown.assert_called_once()
            mock_shutdown_all.assert_called_once()
            self.assertFalse(engine_dir.exists())


if __name__ == "__main__":
    unittest.main()
