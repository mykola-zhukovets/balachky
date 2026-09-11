"""feature/stt-sherpa-parakeet: інтеграція другого рушія в решту програми.

Перевіряємо стики, які тести рушія не бачать: детекція стану моделі за видом
пресета, діалог відновлення з докачкою пакета замість HF-кешу, Центр моделей,
офлайн-пакет, перелік встановлених моделей, i18n-парність і те, що жоден фронт
не будує Engine напряму повз фабрику.
"""
import os
import re
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core import stt_presets
from whisper_core.config import Config
from whisper_core.models import resolve_model_state, PINNED_OK, ABSENT
from fronts.desktop.i18n import STRINGS

PRESET = "parakeet-tdt-0.6b-v3"
ROOT = Path(__file__).resolve().parents[1]


class ModelStateTests(unittest.TestCase):
    def test_sherpa_preset_state_comes_from_component_dir(self):
        cfg = Config(model_name=PRESET)
        with patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=True):
            state = resolve_model_state(cfg)
        self.assertEqual(state.state, PINNED_OK)
        self.assertIsNone(state.revision)
        with patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=False):
            state = resolve_model_state(cfg)
        self.assertEqual(state.state, ABSENT)

    def test_whisper_preset_state_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(model_name="large-v3-turbo", model_dir=tmp)
            self.assertEqual(resolve_model_state(cfg).state, ABSENT)


class ModelsHubTests(unittest.TestCase):
    def test_hub_reports_sherpa_package(self):
        from whisper_core.models_hub import get_models_hub_status
        cfg = Config(model_name=PRESET)
        with patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=True), \
                patch("whisper_core.models_hub.get_dir_size", return_value=1234):
            items = get_models_hub_status(cfg)
        stt = next(i for i in items if i.component_id == "stt")
        self.assertTrue(stt.is_downloaded)
        self.assertEqual(stt.size_bytes, 1234)
        self.assertEqual(stt.active_name_key, "models_hub_preset_parakeet")
        self.assertFalse(stt.is_recommended_active)

    def test_hub_sherpa_missing_is_not_downloaded(self):
        from whisper_core.models_hub import get_models_hub_status
        cfg = Config(model_name=PRESET)
        with patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=False):
            items = get_models_hub_status(cfg)
        stt = next(i for i in items if i.component_id == "stt")
        self.assertFalse(stt.is_downloaded)
        self.assertEqual(stt.size_bytes, 0)


class OfflinePackageTests(unittest.TestCase):
    def test_import_destination_for_sherpa_component(self):
        from whisper_core.offline_package import import_destination
        cfg = Config()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("whisper_core.paths.user_dir", return_value=Path(tmp)):
                dest = import_destination(
                    {"type": "asr_sherpa", "details": {"preset": PRESET}}, cfg)
                self.assertEqual(dest, Path(tmp) / "components" / "stt" / PRESET)
                self.assertIsNone(import_destination(
                    {"type": "asr_sherpa", "details": {"preset": "large-v3"}}, cfg))
                self.assertIsNone(import_destination(
                    {"type": "asr_sherpa", "details": {"preset": "..\\evil"}}, cfg))

    def test_export_lists_installed_sherpa_package(self):
        from whisper_core.offline_package import get_available_components
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(model_dir=tmp)
            pkg_dir = Path(tmp) / "components" / "stt" / PRESET
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "tokens.txt").write_bytes(b"x" * 10)
            with patch("whisper_core.paths.user_dir", return_value=Path(tmp)), \
                    patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=True):
                comps = get_available_components(cfg)
        match = [c for c in comps if c.type == "asr_sherpa"]
        self.assertEqual(len(match), 1)
        comp = match[0]
        self.assertEqual(comp.id, f"asr_sherpa_{PRESET}")
        self.assertEqual(comp.details, {"preset": PRESET})
        self.assertEqual(comp.source_dir, pkg_dir)
        self.assertEqual(comp.size_bytes, 10)


class InstalledModelsTests(unittest.TestCase):
    def test_installed_model_names_includes_sherpa_package_on_disk(self):
        from fronts.desktop.app import DesktopApp
        fake_self = types.SimpleNamespace(cfg=Config(model_name="large-v3-turbo", model_dir=""))
        with patch("whisper_core.models.model_snapshot_size", return_value=0), \
                patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=True):
            names = DesktopApp.installed_model_names(fake_self)
        self.assertEqual(names[0], "large-v3-turbo")
        self.assertIn(PRESET, names)
        with patch("whisper_core.models.model_snapshot_size", return_value=0), \
                patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=False):
            names = DesktopApp.installed_model_names(fake_self)
        self.assertNotIn(PRESET, names)


class RecoveryDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _dialog(self, model_name, tmp):
        from fronts.desktop.recovery import RecoveryDialog
        from whisper_core.engine import ModelRevisionUnavailable
        cfg = Config(model_name=model_name, model_dir=tmp)
        err = ModelRevisionUnavailable(model_name, tmp, None, False)
        return RecoveryDialog(cfg, err)

    def test_sherpa_mode_hides_hf_only_actions_and_shows_license(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=False):
            dlg = self._dialog(PRESET, tmp)
            self.assertIsNone(dlg._pick_btn.parentWidget())      # «Вказати папку» не для пакета
            self.assertIsNone(dlg._scan_btn.parentWidget())      # «Перевірити папки» так само
            self.assertIsNotNone(dlg._dl_btn.parentWidget())     # докачка лишається
            self.assertIn("CC-BY-4.0", dlg._license_note.text())
            self.assertIn("nvidia/parakeet-tdt-0.6b-v3", dlg._license_note.text())
            dlg.deleteLater()

    def test_sherpa_mode_restore_buttons_does_not_spawn_windows(self):
        """Після збою/скасування докачки _restore_buttons() не має показувати
        HF-кнопки без батька — інакше Qt малює їх окремими вікнами (суд 07.09)."""
        with tempfile.TemporaryDirectory() as tmp,                 patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=False):
            dlg = self._dialog(PRESET, tmp)
            dlg._restore_buttons()
            self.assertFalse(dlg._pick_btn.isVisible())
            self.assertFalse(dlg._scan_btn.isVisible())
            self.assertIsNone(dlg._pick_btn.parentWidget())
            self.assertTrue(dlg._dl_btn.isVisibleTo(dlg))
            dlg.deleteLater()

    def test_whisper_mode_keeps_offline_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            dlg = self._dialog("large-v3-turbo", tmp)
            self.assertIsNotNone(dlg._pick_btn.parentWidget())
            self.assertIsNotNone(dlg._scan_btn.parentWidget())
            self.assertIsNone(getattr(dlg, "_license_note", None))
            dlg.deleteLater()

    def test_sherpa_download_uses_package_worker(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("whisper_core.stt_sherpa_models.models_present_fast", return_value=False), \
                patch("fronts.desktop.recovery.SherpaDownloadWorker") as worker_cls, \
                patch("fronts.desktop.recovery.DownloadWorker") as hf_worker_cls:
            dlg = self._dialog(PRESET, tmp)
            dlg._start_download()
            worker_cls.assert_called_once_with(PRESET)
            worker_cls.return_value.start.assert_called_once()
            hf_worker_cls.assert_not_called()
            dlg._worker = None
            dlg.deleteLater()


class I18nTests(unittest.TestCase):
    KEYS = ("stt_preset_parakeet", "stt_preset_parakeet_hint", "stt_preset_parakeet_cpu",
            "stt_sherpa_note", "models_hub_preset_parakeet", "rec_license_line")

    def test_keys_present_and_clean(self):
        for key in self.KEYS:
            for lang in ("uk", "en"):
                self.assertIn(key, STRINGS[lang], f"{key} відсутній у {lang}")
                value = STRINGS[lang][key]
                self.assertTrue(value.strip())
                self.assertNotIn("«", value)
                self.assertNotIn("»", value)
        self.assertIn("{license}", STRINGS["uk"]["rec_license_line"])
        self.assertIn("{url}", STRINGS["uk"]["rec_license_line"])


class FactoryDisciplineTests(unittest.TestCase):
    def test_no_front_constructs_engine_directly(self):
        """Усі фронти будують рушій ЛИШЕ через make_engine — інакше новий вид
        пресета мовчки отримає Whisper-рушій і впаде на завантаженні."""
        pattern = re.compile(r"(=|return)\s*Engine\(")
        offenders = []
        for base in (ROOT / "fronts", ROOT / "whisper_core"):
            for path in base.rglob("*.py"):
                if path.name == "engine.py":
                    continue
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(line) and not line.lstrip().startswith("#"):
                        offenders.append(f"{path.relative_to(ROOT)}:{n}")
        self.assertEqual(offenders, [])

    def test_settings_hint_mentions_language_note_for_sherpa(self):
        from fronts.desktop.pages.settings import SettingsPage
        from fronts.desktop.i18n import tr
        page = types.SimpleNamespace(_selected_device=lambda: "cuda",
                                     _selected_compute=lambda: "int8")
        hint = SettingsPage._hardware_hint(page, stt_presets.get_preset(PRESET))
        self.assertIn(tr("stt_sherpa_note"), hint)
        self.assertIn(tr("stt_preset_parakeet_hint"), hint)


if __name__ == "__main__":
    unittest.main()
