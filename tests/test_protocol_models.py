"""E2: менеджер GGUF-моделей — пресети, безпечний id, звірка, завантаження.

Живе завантаження (3-8 ГБ) не ганяємо; підміняємо PRESETS тестовим пресетом із
file://-URL на маленький локальний файл — реальний шлях download→verify→install
покрито без мережі й гігабайтів."""
import hashlib
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._isolation import reset_process_caches
from whisper_core.protocol import model_manager as mm


class TestPresets(unittest.TestCase):
    def test_two_presets_of_gemma_family(self):
        self.assertIn("fast", mm.PRESETS)
        self.assertIn("quality", mm.PRESETS)
        # обидві — Gemma (сімейство не міняти)
        for p in mm.PRESETS.values():
            self.assertIn("gemma", p.url.lower())

    def test_default_is_fast(self):
        self.assertEqual(mm.DEFAULT_PRESET, "fast")

    def test_quality_bigger_than_fast(self):
        self.assertGreater(mm.PRESETS["quality"].approx_size_bytes,
                           mm.PRESETS["fast"].approx_size_bytes)

    def test_safe_preset_id_valid(self):
        self.assertEqual(mm.safe_preset_id("quality"), "quality")

    def test_safe_preset_id_unknown_falls_back(self):
        self.assertEqual(mm.safe_preset_id("../evil"), "fast")
        self.assertEqual(mm.safe_preset_id(""), "fast")
        self.assertEqual(mm.safe_preset_id(None), "fast")

    def test_get_preset_unknown_falls_back(self):
        self.assertEqual(mm.get_preset("nope").id, "fast")


class TestDownloadInstall(unittest.TestCase):
    def setUp(self):
        reset_process_caches()
        self.tmp = Path(tempfile.mkdtemp(prefix="mmtest-"))
        # Джерело «моделі» — маленький файл; рахуємо його SHA для звірки.
        self.src = self.tmp / "src.gguf"
        payload = b"GGUF" + b"\x00" * 5000
        self.src.write_bytes(payload)
        self.sha = hashlib.sha256(payload).hexdigest()
        self.url = self.src.resolve().as_uri()          # file:// URL
        self.preset = mm.ModelPreset(
            id="fast", url=self.url, approx_size_bytes=5004, min_bytes=100,
            sha256=self.sha, label_key="x", hint_key="y")
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = self.preset

    def tearDown(self):
        mm.PRESETS.clear()
        mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_not_available_before_download(self):
        target = self.tmp / "fast"
        self.assertFalse(mm.model_available(target, "fast"))

    def test_download_installs_and_verifies(self):
        target = self.tmp / "fast"
        mm.download_and_install(target, "fast")
        self.assertTrue(mm.model_available(target, "fast"))
        self.assertTrue(mm.model_file(target).is_file())
        self.assertTrue((target / mm._READY_MARKER).is_file())

    def test_download_idempotent(self):
        target = self.tmp / "fast"
        mm.download_and_install(target, "fast")
        mm.download_and_install(target, "fast")     # вдруге — no-op, без винятку
        self.assertTrue(mm.model_available(target, "fast"))

    def test_truncated_file_fails_verify(self):
        target = self.tmp / "fast"
        # min_bytes більший за розмір джерела → звірка провалює встановлення
        mm.PRESETS["fast"] = replace(self.preset, min_bytes=999999, sha256=None)
        with self.assertRaises(mm.ModelDownloadError):
            mm.download_and_install(target, "fast")
        self.assertFalse(mm.model_available(target, "fast"))

    def test_wrong_sha_fails_verify(self):
        target = self.tmp / "fast"
        mm.PRESETS["fast"] = replace(self.preset, sha256="0" * 64)
        with self.assertRaises(mm.ModelDownloadError):
            mm.download_and_install(target, "fast")

    def test_cancel_aborts(self):
        target = self.tmp / "fast"
        with self.assertRaises(InterruptedError):
            mm.download_and_install(target, "fast", cancel_check=lambda: True)
        self.assertFalse(mm.model_available(target, "fast"))

    def test_delete_model(self):
        target = self.tmp / "fast"
        mm.download_and_install(target, "fast")
        self.assertTrue(mm.delete_model(target))
        self.assertFalse(target.exists())
        self.assertFalse(mm.delete_model(target))    # вже нема → False


class TestPreUseIntegrityCache(unittest.TestCase):
    def setUp(self):
        reset_process_caches()

    def test_integrity_cache_isolated_by_model_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted_dir = root / "trusted"
            corrupt_dir = root / "corrupt"
            trusted_dir.mkdir()
            corrupt_dir.mkdir()
            for model_dir in (trusted_dir, corrupt_dir):
                (model_dir / mm._READY_MARKER).write_text(
                    "ok", encoding="utf-8")

            trusted = b"trusted model payload"
            corrupt = trusted[::-1]
            trusted_path = trusted_dir / mm.MODEL_FILENAME
            corrupt_path = corrupt_dir / mm.MODEL_FILENAME
            trusted_path.write_bytes(trusted)
            corrupt_path.write_bytes(corrupt)
            stamp = 1_700_000_000_000_000_000
            os.utime(trusted_path, ns=(stamp, stamp))
            os.utime(corrupt_path, ns=(stamp, stamp))
            self.assertEqual(
                mm._integrity_fingerprint(trusted_path, 1),
                mm._integrity_fingerprint(corrupt_path, 1))
            expected = hashlib.sha256(trusted).hexdigest()

            self.assertTrue(mm._available_dir(trusted_dir, 1, expected))
            self.assertFalse(mm._available_dir(corrupt_dir, 1, expected))

    def test_repeated_integrity_check_rehashes_only_after_mtime_change(self):
        tmp = (Path.cwd() / ".test-artifacts" /
               f"mm-integrity-{next(tempfile._get_candidate_names())}")
        tmp.mkdir(parents=True)
        self.addCleanup(
            lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        model_dir = tmp / "fast"
        model_dir.mkdir()
        payload = b"GGUF" + b"\x00" * 5000
        model_path = model_dir / mm.MODEL_FILENAME
        model_path.write_bytes(payload)
        (model_dir / mm._READY_MARKER).write_text("ok", encoding="utf-8")
        resolved = mm.ResolvedModel(
            id="fast", kind="preset", model_path=model_path,
            downloadable=True, _model_dir=model_dir, _min_bytes=100,
            _sha256=hashlib.sha256(payload).hexdigest())
        real_hasher = mm.hashlib.sha256
        calls = []

        def counted_hasher(*args, **kwargs):
            calls.append(True)
            return real_hasher(*args, **kwargs)

        with mock.patch.object(
                mm.hashlib, "sha256", side_effect=counted_hasher):
            self.assertTrue(resolved.integrity_available())
            self.assertEqual(len(calls), 1)
            self.assertTrue(resolved.integrity_available())
            self.assertEqual(len(calls), 1)
            old_stat = model_path.stat()
            os.utime(
                model_path,
                ns=(old_stat.st_atime_ns,
                    old_stat.st_mtime_ns + 2_000_000_000))
            self.assertTrue(resolved.integrity_available())
            self.assertEqual(len(calls), 2)


class TestForcedRedownload(unittest.TestCase):
    """Фікс 2: «Завантажити заново» = staged-заміна. force=True докачує
    свіжий файл у stage й атомарно підміняє — старий ЖИВИЙ, доки нова версія не
    завантажилась повністю; скасування/збій = старий файл лишається на місці."""

    def setUp(self):
        reset_process_caches()
        self.tmp = Path(tempfile.mkdtemp(prefix="mmforce-"))
        self.src = self.tmp / "src.gguf"
        self._write_src(b"GGUF-v1" + b"\x00" * 5000)
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = self.preset

    def _write_src(self, payload):
        self.src.write_bytes(payload)
        self.sha = hashlib.sha256(payload).hexdigest()
        self.preset = mm.ModelPreset(
            id="fast", url=self.src.resolve().as_uri(),
            approx_size_bytes=len(payload), min_bytes=100, sha256=self.sha,
            label_key="x", hint_key="y")

    def tearDown(self):
        mm.PRESETS.clear()
        mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_force_replaces_cache_when_expected_sha_changes(self):
        target = self.tmp / "fast"
        mm.download_and_install(target, "fast")
        # Інший закріплений артефакт має іншу очікувану SHA: кеш v1 більше
        # не валідний і перекачується навіть без force.
        self._write_src(b"GGUF-v2-DIFFERENT" + b"\x00" * 6000)
        mm.PRESETS["fast"] = self.preset
        mm.download_and_install(target, "fast")
        self.assertTrue(mm.model_file(target).read_bytes().startswith(b"GGUF-v2"))

    def test_force_replaces_existing_model(self):
        target = self.tmp / "fast"
        mm.download_and_install(target, "fast")
        self.assertTrue(mm.model_file(target).read_bytes().startswith(b"GGUF-v1"))
        # автор перезалив ваги → нове джерело + force
        self._write_src(b"GGUF-v2-DIFFERENT" + b"\x00" * 6000)
        mm.PRESETS["fast"] = self.preset
        mm.download_and_install(target, "fast", force=True)
        self.assertTrue(mm.model_file(target).read_bytes().startswith(b"GGUF-v2"))
        self.assertTrue(mm.model_available(target, "fast"))

    def test_force_keeps_old_model_when_new_fails_verify(self):
        target = self.tmp / "fast"
        mm.download_and_install(target, "fast")
        # свіже джерело з НЕПРАВИЛЬНОЮ очікуваною SHA → verify падає ПІСЛЯ докачки,
        # але ДО підміни target → старий файл недоторканий
        self._write_src(b"GGUF-v2-DIFFERENT" + b"\x00" * 6000)
        broken = replace(self.preset, sha256="0" * 64)
        mm.PRESETS["fast"] = broken
        with self.assertRaises(mm.ModelDownloadError):
            mm.download_and_install(target, "fast", force=True)
        self.assertTrue(mm.model_available(target, "fast"))          # старий живий
        self.assertTrue(mm.model_file(target).read_bytes().startswith(b"GGUF-v1"))

    def test_force_keeps_old_model_when_cancelled(self):
        target = self.tmp / "fast"
        mm.download_and_install(target, "fast")
        self._write_src(b"GGUF-v2-DIFFERENT" + b"\x00" * 6000)
        mm.PRESETS["fast"] = self.preset
        with self.assertRaises(InterruptedError):
            mm.download_and_install(target, "fast", force=True,
                                    cancel_check=lambda: True)
        self.assertTrue(mm.model_available(target, "fast"))          # старий живий
        self.assertTrue(mm.model_file(target).read_bytes().startswith(b"GGUF-v1"))


if __name__ == "__main__":
    unittest.main()
