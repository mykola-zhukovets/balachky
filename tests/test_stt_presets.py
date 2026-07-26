"""fix/stt-models: ширший вибір моделі розпізнавання (пресети + власна модель).

Раніше в комбо було лише 2 пресети (turbo, large-v3). Тут регресії на:
  • пресети з даних (small/medium для слабких ПК) реально зʼявляються в комбо;
  • ДЕФОЛТ НЕ ЗМІНЕНО (turbo лишається активним/рекомендованим);
  • i18n-парність нових видимих рядків (uk+en);
  • власна модель (тека/HF-id) round-trip’иться в комбо як окремий пункт;
  • repo_for не падає на не-пресетному імені (recovery-шлях власної моделі).
"""
import os
import types
import unittest

# Віджети без екрана: комбо будуємо в offscreen QApplication
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLabel

from whisper_core import stt_presets
from whisper_core.models import repo_for
from fronts.desktop.i18n import STRINGS, tr
from fronts.desktop.pages.settings import SettingsPage


def _app():
    return QApplication.instance() or QApplication([])


def _build_combo():
    """Побудувати комбо точно як SettingsPage._model_group: пункти з PRESETS."""
    combo = QComboBox()
    for preset in stt_presets.PRESETS:
        combo.addItem(tr(preset.label_key), preset.name)
    return combo


class PresetDataTests(unittest.TestCase):
    def test_small_and_medium_available(self):
        names = [p.name for p in stt_presets.PRESETS]
        self.assertIn("small", names)      # слабкі ПК
        self.assertIn("medium", names)     # середні ПК

    def test_existing_two_still_present(self):
        names = [p.name for p in stt_presets.PRESETS]
        self.assertIn("large-v3-turbo", names)
        self.assertIn("large-v3", names)

    def test_more_than_two_presets(self):
        # суть задачі: вибір ширший за колишні два
        self.assertGreater(len(stt_presets.PRESETS), 2)

    def test_is_preset(self):
        self.assertTrue(stt_presets.is_preset("small"))
        self.assertTrue(stt_presets.is_preset("large-v3-turbo"))
        self.assertFalse(stt_presets.is_preset("Systran/whatever"))
        self.assertFalse(stt_presets.is_preset(""))

    def test_is_repo_id(self):
        self.assertTrue(stt_presets.is_repo_id("Systran/faster-whisper-small"))
        self.assertTrue(stt_presets.is_repo_id("arampacha/whisper-large-uk-2"))
        self.assertFalse(stt_presets.is_repo_id("no-slash"))
        self.assertFalse(stt_presets.is_repo_id("C:/local/path"))
        self.assertFalse(stt_presets.is_repo_id(""))

    def test_page_urls_present(self):
        # лінк «Про модель» має бути в кожного офіційного пресета
        for preset in stt_presets.PRESETS:
            self.assertTrue(preset.page_url.startswith("https://"),
                            f"{preset.name}: нема page_url")


class ComboTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def test_all_presets_appear_in_combo(self):
        combo = _build_combo()
        data = {combo.itemData(i) for i in range(combo.count())}
        self.assertEqual(data, {p.name for p in stt_presets.PRESETS})

    def test_default_selection_unchanged(self):
        # дефолт (turbo) досі є пресетом і вибирається без підміни
        combo = _build_combo()
        idx = combo.findData("large-v3-turbo")
        self.assertGreaterEqual(idx, 0)
        combo.setCurrentIndex(idx)
        self.assertEqual(combo.currentData(), "large-v3-turbo")

    def test_onboarding_default_still_turbo(self):
        # канон: дефолт НЕ міняти до A/B-тесту точності
        from fronts.desktop.onboarding import FirstRunWizard
        import inspect
        src = inspect.getsource(FirstRunWizard.__init__)
        self.assertIn('model_name or "large-v3-turbo"', src)


class CustomModelHelperTests(unittest.TestCase):
    """Хелпери власної моделі, викликані на легкому SimpleNamespace-page."""
    @classmethod
    def setUpClass(cls):
        _app()

    def _page(self):
        page = types.SimpleNamespace()
        page._stt_presets = stt_presets
        page._model = _build_combo()
        page._model_hint = QLabel()
        page._model_about = QLabel()
        # звʼязати потрібні методи як bound
        for name in ("_custom_model_label", "_refresh_model_meta",
                     "_select_custom_model", "_hardware_hint",
                     "_selected_device", "_selected_compute"):
            setattr(page, name, types.MethodType(getattr(SettingsPage, name), page))
        return page

    def test_custom_model_roundtrips_into_combo(self):
        page = self._page()
        page._select_custom_model("arampacha/whisper-large-uk-2")
        self.assertEqual(page._model.currentData(),
                         "arampacha/whisper-large-uk-2")
        # повторний вибір тієї ж моделі не дублює пункт
        n = page._model.count()
        page._select_custom_model("arampacha/whisper-large-uk-2")
        self.assertEqual(page._model.count(), n)

    def test_refresh_meta_sets_hint_for_preset(self):
        page = self._page()
        page._model.setCurrentIndex(page._model.findData("small"))
        page._refresh_model_meta()
        self.assertTrue(page._model_hint.text())
        self.assertIn("small", page._model_hint.text())
        self.assertIn("href", page._model_about.text())   # лінк «Про модель»

    def test_refresh_meta_clears_for_custom(self):
        page = self._page()
        page._select_custom_model("owner/custom-model")
        page._refresh_model_meta()
        self.assertEqual(page._model_hint.text(), "")
        self.assertEqual(page._model_about.text(), "")


class I18nTests(unittest.TestCase):
    def test_preset_keys_parity(self):
        uk, en = STRINGS["uk"], STRINGS["en"]
        keys = []
        for preset in stt_presets.PRESETS:
            keys += [preset.label_key, preset.hint_key]
        keys += ["stt_model_about", "stt_model_custom_prefix",
                 "stt_model_add_folder", "stt_model_add_folder_tip",
                 "stt_model_add_hf", "stt_model_add_hf_tip",
                 "stt_model_hf_prompt", "stt_model_hf_invalid",
                 "stt_model_folder_invalid"]
        for k in keys:
            self.assertIn(k, uk, f"нема UK: {k}")
            self.assertIn(k, en, f"нема EN: {k}")


class RepoForToleranceTests(unittest.TestCase):
    def test_preset_names_map(self):
        self.assertEqual(repo_for("small"), "Systran/faster-whisper-small")
        self.assertEqual(repo_for("medium"), "Systran/faster-whisper-medium")

    def test_unknown_name_returns_itself(self):
        # власна HF-модель: repo_for не падає KeyError, віддає репо як є
        self.assertEqual(repo_for("owner/custom-model"), "owner/custom-model")


class ModelPinningTests(unittest.TestCase):
    """SUPPLY-CHAIN: пресети слабких ПК мусять бути пінованими, як turbo/large-v3,
    інакше качаються з плаваючого HF-main і не потрапляють під known_repos()."""

    def test_small_and_medium_are_pinned(self):
        from whisper_core.engine import MODEL_REVISIONS
        for name in ("small", "medium"):
            self.assertIn(name, MODEL_REVISIONS, f"{name} не пінований")
            self.assertRegex(MODEL_REVISIONS[name], r"^[0-9a-f]{40}$",
                             f"{name}: ревізія не 40-символьний sha")

    def test_pinned_presets_are_managed_repos(self):
        # пінований пресет → known_repos() ним керує (видалення/детекція)
        from whisper_core.models import known_repos, repo_for
        repos = known_repos()
        self.assertIn(repo_for("small"), repos)
        self.assertIn(repo_for("medium"), repos)


class InstalledModelNamesTests(unittest.TestCase):
    """РЕГРЕС: кнопка «Спробувати іншою моделлю». Раніше installed_model_names()
    перебирала лише ключі MODEL_REVISIONS → на активній small/medium/власній моделі
    казала «немає моделей», хоча модель щойно розшифрувала (б'є по слабких ПК)."""

    def _names_for(self, model_name):
        # порожня тека моделей: жодного знімка на диску → лишається тільки активна
        import tempfile
        from fronts.desktop.app import DesktopApp
        with tempfile.TemporaryDirectory() as d:
            fake = types.SimpleNamespace(
                cfg=types.SimpleNamespace(model_name=model_name, model_dir=d))
            return DesktopApp.installed_model_names(fake)

    def test_active_small_is_listed(self):
        self.assertIn("small", self._names_for("small"))

    def test_active_medium_is_listed(self):
        self.assertIn("medium", self._names_for("medium"))

    def test_active_custom_model_not_in_revisions_is_listed(self):
        # власна модель (HF-id) поза MODEL_REVISIONS — активна все одно доступна
        names = self._names_for("owner/custom-model")
        self.assertIn("owner/custom-model", names)
        self.assertEqual(names[0], "owner/custom-model")  # активна — першою


if __name__ == "__main__":
    unittest.main()
