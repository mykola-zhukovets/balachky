"""Release-version contracts shared by runtime, updater, and Windows builds."""
import unittest
from pathlib import Path
from unittest.mock import patch

import whisper_core
from whisper_core import offline_package, updates
from whisper_core.tts import engine_manager


ROOT = Path(__file__).resolve().parent.parent


class VersionConstantsTests(unittest.TestCase):
    def test_release_concepts_have_distinct_canonical_values(self):
        self.assertEqual(
            getattr(whisper_core, "DISPLAY_VERSION", None), "1.2.4.1-beta")
        self.assertEqual(
            getattr(whisper_core, "PEP440_VERSION", None), "1.2.4.1b0")
        self.assertEqual(
            getattr(whisper_core, "WINDOWS_FILE_VERSION", None), (1, 2, 4, 1))
        self.assertEqual(
            getattr(whisper_core, "RELEASE_CHANNEL", None), "beta")

    def test_tts_compatibility_uses_display_version_without_losing_release(self):
        self.assertEqual(
            engine_manager.CURRENT_APP_VERSION, whisper_core.DISPLAY_VERSION)
        self.assertEqual(
            engine_manager.parse_version_tuple(whisper_core.DISPLAY_VERSION),
            (1, 2, 4, 1),
        )

    def test_offline_package_fallback_uses_canonical_display_version(self):
        source = Path(offline_package.__file__).read_text(encoding="utf-8")
        self.assertIn(
            'getattr(cfg, "app_version", DISPLAY_VERSION)', source)


class UpdateVersionComparisonTests(unittest.TestCase):
    def test_stable_release_is_newer_than_same_base_beta(self):
        current = getattr(
            whisper_core, "PEP440_VERSION", whisper_core.__version__)
        self.assertTrue(updates.is_newer("1.2.5", current))

    def test_next_patch_beta_is_newer_than_previous_stable(self):
        self.assertTrue(updates.is_newer("1.2.4-beta", "1.2.3"))

    def test_same_stable_release_is_not_newer(self):
        self.assertFalse(updates.is_newer("1.2.3", "1.2.3"))


class WindowsVersionBuildTests(unittest.TestCase):
    def test_spec_uses_numeric_file_version_when_display_has_suffix(self):
        spec = (ROOT / "balachky.spec").read_text(encoding="utf-8")
        version_preamble = spec.split("# Вбиваємо коміт збірки", 1)[0]

        version_values = {
            "DISPLAY_VERSION": "1.2.4.1-beta",
            "PEP440_VERSION": "1.2.4.1b0",
            "WINDOWS_FILE_VERSION": (1, 2, 4, 1),
            "RELEASE_CHANNEL": "beta",
        }
        namespace = {"SPECPATH": str(ROOT)}
        with patch.object(
                Path, "read_text", return_value='__version__ = "1.2.4.1-beta"\n'), \
                patch("runpy.run_path", return_value=version_values):
            try:
                exec(compile(version_preamble, "balachky.spec", "exec"),
                     namespace)
            except ValueError:
                file_version = None
            else:
                file_version = namespace.get("_vtuple")

        self.assertEqual(file_version, (1, 2, 4, 1))

    def test_installer_uses_numeric_windows_file_version(self):
        installer = (
            ROOT / "installer" / "balachky.iss").read_text(encoding="utf-8-sig")
        self.assertIn("VersionInfoVersion={#WindowsFileVersion}", installer)

    def test_finalize_installer_does_not_append_release_channel_twice(self):
        script = (
            ROOT / "scripts" / "finalize_installer.ps1").read_text(
                encoding="utf-8-sig")
        self.assertIn('[string]$Suffix = ""', script)
        self.assertIn(
            'if ($Suffix -and -not $version.EndsWith('
            '"-$Suffix", [StringComparison]::OrdinalIgnoreCase))',
            script,
        )


if __name__ == "__main__":
    unittest.main()
