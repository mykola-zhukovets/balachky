"""feature/stt-preset-large-v2: п'ятий пресет розпізнавання — large-v2.

Навіщо: за публічним бенчмарком на українській (egorsmkv/speech-recognition-uk,
Common Voice 10) ванільний large-v2 робить менше помилок за large-v3
(13,72 % проти 20,53 % WER). Пресет додається як АЛЬТЕРНАТИВА, дефолт (turbo)
не міняється (канон: до власного A/B-тесту точності).

Регресії тут:
  • пресет є в даних і стоїть між turbo і large-v3 (порядок комбо);
  • ревізія пінована, маніфест повний (розмір + SHA-256 кожного файла);
  • i18n-парність усіх нових видимих рядків (uk+en), без лапок-ялинок;
  • Центр моделей знає підпис пресета;
  • докачка з майстра першого запуску резолвить ревізію для КОЖНОГО пресета
    (а не для захардкодженого списку) і перевіряє місце за фактичним маніфестом;
  • таблиця VRAM у Налаштуваннях має рядки для нового пресета.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core import stt_presets
from whisper_core.engine import MODEL_REVISIONS
from whisper_core.models import (model_download_manifest, repo_for,
                                 revision_for)
from fronts.desktop.i18n import STRINGS

REPO = "Systran/faster-whisper-large-v2"
REVISION = "f0fe81560cb8b68660e564f55dd99207059c092e"
EXPECTED_FILES = {
    "config.json": (
        2796,
        "d86b7a7664a12559d644aa210a32ce9a7e03913e794b7ea4fb7182de69e273a7"),
    "tokenizer.json": (
        2203239,
        "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "vocabulary.txt": (
        459861,
        "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
    "model.bin": (
        3086912962,
        "bf2a9746382e1aa7ffff6b3a0d137ed9edbd9670c3b87e5d35f5e85e70d0333a"),
}


class PresetDataTests(unittest.TestCase):
    def test_large_v2_present_between_turbo_and_large_v3(self):
        names = [p.name for p in stt_presets.PRESETS]
        self.assertIn("large-v2", names)
        self.assertLess(names.index("large-v3-turbo"), names.index("large-v2"))
        self.assertLess(names.index("large-v2"), names.index("large-v3"))

    def test_large_v2_page_and_license(self):
        preset = stt_presets.get_preset("large-v2")
        self.assertIsNotNone(preset)
        self.assertEqual(preset.page_url, "https://huggingface.co/" + REPO)
        self.assertEqual(preset.license_name, "MIT")

    def test_default_untouched(self):
        # канон: дефолт лишається turbo, large-v2 — лише додатковий вибір
        self.assertEqual(stt_presets.PRESETS[2].name, "large-v3-turbo")


class PinTests(unittest.TestCase):
    def test_revision_pinned(self):
        self.assertEqual(MODEL_REVISIONS["large-v2"], REVISION)
        self.assertEqual(revision_for("large-v2"), REVISION)

    def test_repo_mapping(self):
        self.assertEqual(repo_for("large-v2"), REPO)

    def test_manifest_exact_sizes_and_sha(self):
        manifest = model_download_manifest(REPO, REVISION)
        by_name = {a.filename: (a.size, a.sha256) for a in manifest}
        self.assertEqual(by_name, EXPECTED_FILES)


class I18nTests(unittest.TestCase):
    KEYS = ("stt_preset_large_v2", "stt_preset_large_v2_hint",
            "stt_preset_large_v2_cpu", "models_hub_preset_large_v2")

    def test_keys_present_in_both_languages(self):
        for key in self.KEYS:
            for lang in ("uk", "en"):
                self.assertIn(key, STRINGS[lang], f"{key} відсутній у {lang}")
                self.assertTrue(STRINGS[lang][key].strip(), f"{key} порожній у {lang}")

    def test_no_guillemets_and_honest_wording(self):
        for key in self.KEYS:
            for lang in ("uk", "en"):
                value = STRINGS[lang][key]
                self.assertNotIn("«", value)
                self.assertNotIn("»", value)
        # підпис не обіцяє «найточнішу» — це лишається за large-v3, доки нема A/B
        self.assertNotIn("Найточніша", STRINGS["uk"]["stt_preset_large_v2"])
        self.assertIn("large-v2", STRINGS["uk"]["stt_preset_large_v2"])
        self.assertIn("large-v2", STRINGS["en"]["stt_preset_large_v2"])


class ModelsHubTests(unittest.TestCase):
    def test_hub_labels_large_v2(self):
        from whisper_core.config import Config
        from whisper_core.models_hub import get_models_hub_status
        cfg = Config(model_name="large-v2")
        with patch("whisper_core.models.model_snapshot_size", return_value=0):
            items = get_models_hub_status(cfg)
        stt = next(i for i in items if i.component_id == "stt")
        self.assertEqual(stt.active_name_key, "models_hub_preset_large_v2")
        self.assertEqual(stt.active_name_param, "")
        self.assertFalse(stt.is_recommended_active)


class DownloadWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_every_preset_repo_resolves_pinned_revision_and_real_size(self):
        """revision=None (шлях майстра першого запуску) має знайти піновану
        ревізію для КОЖНОГО пресета й перевірити місце за сумою маніфесту,
        а не за евристикою "large-v3 → 3,1 ГБ, інакше 1,6 ГБ"."""
        from fronts.desktop.onboarding import DownloadWorker
        for preset in stt_presets.PRESETS:
            if getattr(preset, "kind", "whisper") != "whisper":
                continue        # пакети другого рушія — не HF-кеш, свій завантажувач
            repo = repo_for(preset.name)
            revision = revision_for(preset.name)
            manifest = model_download_manifest(repo, revision)
            expected_size = sum(a.size for a in manifest)
            with tempfile.TemporaryDirectory() as tmp:
                worker = DownloadWorker(repo, tmp)          # без revision
                failures = []
                worker.failed.connect(failures.append)
                with patch("fronts.desktop.onboarding.check_free_space") as free, \
                        patch("fronts.desktop.onboarding.resumable_download_file") as dl:
                    worker.run()
            self.assertEqual(failures, [], f"{preset.name}: {failures}")
            self.assertEqual(dl.call_count, len(manifest), preset.name)
            for call in dl.call_args_list:
                self.assertIn(f"/resolve/{revision}/", call.args[0], preset.name)
            free.assert_called_once()
            self.assertEqual(free.call_args.args[1], expected_size, preset.name)


class SettingsVramTableTests(unittest.TestCase):
    def test_gpu_vram_rows_for_large_v2(self):
        from fronts.desktop.pages import settings
        for compute in ("int8", "int8_float16", "float16"):
            self.assertIn(("large-v2", compute), settings._GPU_VRAM)


if __name__ == "__main__":
    unittest.main()
