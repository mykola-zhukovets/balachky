"""feature/processing-slider — пер-профільний персист рівня обробки (спека §5, §9).

Значення живе у profile.json (не глобальний config.toml), окремо для диктування
й наради; merge зберігає memory та невідомі ключі; переноситься експортом.
"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from whisper_core import processing, settings_io
from whisper_core.profiles import Profile


class ProfileProcessingModes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name) / "default"
        d.mkdir(parents=True)
        self.p = Profile("default", d)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_is_verbatim_and_absent(self):
        self.assertFalse(self.p.has_processing())
        self.assertEqual(self.p.processing_mode(processing.DICTATION), "verbatim")
        self.assertEqual(self.p.processing_mode(processing.MEETING), "verbatim")

    def test_roundtrip_per_surface(self):
        self.p.set_processing_mode(processing.DICTATION, "fillers")
        self.p.set_processing_mode(processing.MEETING, "document")
        # свіжий Profile тієї самої теки читає з диска
        reread = Profile("default", self.p.dir)
        self.assertTrue(reread.has_processing())
        self.assertEqual(reread.processing_mode(processing.DICTATION), "fillers")
        self.assertEqual(reread.processing_mode(processing.MEETING), "document")

    def test_surfaces_are_independent(self):
        self.p.set_processing_mode(processing.DICTATION, "document")
        # нарада не зачеплена вибором диктування
        self.assertEqual(self.p.processing_mode(processing.MEETING), "verbatim")

    def test_merge_preserves_memory_and_unknown_keys(self):
        self.p._meta_path.write_text(
            json.dumps({"memory": False, "custom_x": 42}), encoding="utf-8")
        self.p.set_processing_mode(processing.DICTATION, "fillers")
        meta = json.loads(self.p._meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["memory"], False)      # збережено
        self.assertEqual(meta["custom_x"], 42)       # невідомий ключ збережено
        self.assertEqual(meta["processing"]["dictation"], "fillers")
        # і сам об'єкт бачить збережену пам'ять
        self.assertFalse(self.p.memory_enabled)

    def test_invalid_surface_ignored(self):
        self.p.set_processing_mode("bogus", "document")
        self.assertFalse(self.p.has_processing())

    def test_unknown_mode_normalizes(self):
        self.p.set_processing_mode(processing.DICTATION, "СМІТТЯ")
        self.assertEqual(self.p.processing_mode(processing.DICTATION), "verbatim")

    def test_atomic_write_leaves_no_tmp(self):
        self.p.set_processing_mode(processing.DICTATION, "fillers")
        self.assertFalse((self.p.dir / "profile.json.tmp").exists())
        # валідний JSON
        json.loads(self.p._meta_path.read_text(encoding="utf-8"))


class ExportCarriesProcessing(unittest.TestCase):
    def test_profile_json_is_exported(self):
        # profile.json (з блоком processing) входить у перенос налаштувань
        self.assertIn("profile.json", settings_io._PROFILE_INCLUDE)

    def test_export_roundtrip_includes_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "profiles" / "default"
            pdir.mkdir(parents=True)
            Profile("default", pdir).set_processing_mode(processing.DICTATION, "fillers")
            (root / "config.toml").write_text("", encoding="utf-8")
            zip_path = root / "out.zip"
            settings_io.export_settings(zip_path, config_path=root / "config.toml",
                                        profiles_root=root)
            with zipfile.ZipFile(zip_path) as zf:
                data = json.loads(zf.read("profiles/default/profile.json"))
            self.assertEqual(data["processing"]["dictation"], "fillers")


if __name__ == "__main__":
    unittest.main()
