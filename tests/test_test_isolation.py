import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fronts.desktop import i18n, theme
from tests._isolation import reset_process_caches
from whisper_core import autocorrect, updater
from whisper_core.meeting import storage_crypto
from whisper_core.protocol import model_manager
from whisper_core.tts import voices


class TestProcessIsolationReset(unittest.TestCase):
    def setUp(self):
        reset_process_caches()

    def tearDown(self):
        reset_process_caches()

    def test_reset_forces_rehash_for_same_path_size_and_mtime(self):
        root = (
            Path.cwd()
            / ".test-artifacts"
            / f"isolation-{next(tempfile._get_candidate_names())}"
        )
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root, True)
        final = root / "setup.exe"
        valid = b"trusted payload"
        corrupt = b"broken! payload"
        self.assertEqual(len(valid), len(corrupt))
        fixed_mtime_ns = 1_700_000_000_000_000_000
        expected_sha256 = hashlib.sha256(valid).hexdigest()
        url = "https://example.invalid/setup.exe"

        final.write_bytes(valid)
        os.utime(final, ns=(fixed_mtime_ns, fixed_mtime_ns))
        self.assertEqual(
            updater.installer_ready(url, expected_sha256, root),
            final,
        )

        final.write_bytes(corrupt)
        os.utime(final, ns=(fixed_mtime_ns, fixed_mtime_ns))
        reset_process_caches()

        self.assertIsNone(
            updater.installer_ready(url, expected_sha256, root),
            "reset не очистив cached-valid: пошкоджений файл не перехешовано",
        )

    def test_reset_clears_all_mutable_process_state(self):
        integrity_modules = (
            model_manager,
            voices,
            updater,
            autocorrect,
        )
        for module in integrity_modules:
            module._INTEGRITY_CACHE["sentinel"] = object()
        storage_crypto._PASSWORD_CACHE["sentinel"] = b"secret"
        i18n.set_language("en")
        theme.set_ui_color("red")

        reset_process_caches()

        for module in integrity_modules:
            self.assertEqual(module._INTEGRITY_CACHE, {})
        self.assertEqual(storage_crypto._PASSWORD_CACHE, {})
        self.assertEqual(i18n.current_language(), "uk")
        self.assertEqual(theme.current_ui_color(), "classic")


if __name__ == "__main__":
    unittest.main()
