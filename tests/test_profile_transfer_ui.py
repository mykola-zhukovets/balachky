import os
import unittest

# Віджети без екрана: тесту потрібен QApplication, не реальний екран.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop.pages.settings import ProfileImportConfirmDialog, SettingsPage
# Той самий харнес, що будує живе MainWindow у render_nav_smoke.
from tests.render_nav_smoke import _NavController, _make_sandbox


class ProfileTransferUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_confirm_dialog_creation_and_accessibility(self):
        info = {
            "app_version": "2.1.0",
            "created_at": "2026-07-23 22:00:00",
            "files": ["config.toml", "profiles/default/terms.toml"],
            "missing_components": ["context_profiles.toml"],
            "is_newer_version": True,
        }
        dlg = ProfileImportConfirmDialog(None, info, "backup-2026-07-23")

        self.assertEqual(dlg.windowTitle(), "Підтвердження імпорту профілю")
        self.assertEqual(dlg.btn_confirm.accessibleName(), "btn_confirm_import")
        self.assertEqual(dlg.btn_cancel.accessibleName(), "btn_cancel_import")

    def test_settings_page_really_builds_with_backup_methods(self):
        """Регресія блокера Т42: ProfileImportConfirmDialog був вставлений
        ПОСЕРЕД тіла SettingsPage з відступом 0 → ~39 методів «хвоста» (зокрема
        _on_sounds) перетекли в діалог, і SettingsPage.__init__ падав з
        AttributeError. Тут РЕАЛЬНО будуємо сторінку через той самий харнес, що
        й render_nav_smoke — якщо метод втрачено, конструктор впаде тут."""
        page = SettingsPage(_NavController(_make_sandbox()))

        # методи фічі T42 мають належати саме SettingsPage
        self.assertTrue(hasattr(page, "_backup_group"))
        self.assertTrue(hasattr(page, "_on_backup_export"))
        self.assertTrue(hasattr(page, "_on_backup_import"))
        # а «хвіст» SettingsPage не повинен був перетекти в діалог
        self.assertTrue(hasattr(page, "_on_paste_sound"))
        self.assertFalse(hasattr(ProfileImportConfirmDialog, "_on_paste_sound"))
        self.assertFalse(hasattr(ProfileImportConfirmDialog, "_on_night_mode"))

        page.deleteLater()


if __name__ == "__main__":
    unittest.main()
