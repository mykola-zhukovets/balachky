"""Тести UI-кнопок експорту та імпорту офлайн-пакета моделей.

Перевіряємо наявність кнопок у Налаштуваннях, доступність мишкою, accessibility,
підказки, з'єднання з ядром export_package / import_package та стійкість до мутацій.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from fronts.desktop.i18n import tr, set_language
from fronts.desktop.main_window import MainWindow
from fronts.desktop.pages.settings import OfflineExportDialog, OfflineImportDialog
from whisper_core import paths
from whisper_core.offline_package import ComponentExportInfo
from tests.render_nav_smoke import _NavController

_APP = QApplication.instance() or QApplication([])


class TestOfflinePackageUI(unittest.TestCase):
    def setUp(self):
        set_language("uk")
        self.tmp_dir = tempfile.mkdtemp()
        user_dir_patch = patch.object(
            paths, "USER_DIR", Path(self.tmp_dir) / "userdata"
        )
        user_dir_patch.start()
        self.addCleanup(user_dir_patch.stop)
        self.controller = _NavController(self.tmp_dir)
        self.win = MainWindow(self.controller)
        self.win.show()
        self.settings = self.win.settings

    def tearDown(self):
        self.win.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_offline_buttons_exist_and_have_a11y_and_tooltips(self):
        """Кнопки експорту та імпорту є в розділі моделей, мають AccessibleName та ToolTip."""
        btn_export = self.settings._btn_export_pkg
        btn_import = self.settings._btn_import_pkg

        self.assertIsNotNone(btn_export)
        self.assertIsNotNone(btn_import)

        self.assertFalse(btn_export.isHidden())
        self.assertFalse(btn_import.isHidden())

        self.assertTrue(bool(btn_export.accessibleName().strip()))
        self.assertTrue(bool(btn_export.toolTip().strip()))
        self.assertTrue(bool(btn_import.accessibleName().strip()))
        self.assertTrue(bool(btn_import.toolTip().strip()))

    def test_export_button_when_no_components_shows_info_message(self):
        """Клік по вивантаженню при відсутності завантажених моделей показує сповіщення."""
        with patch("whisper_core.offline_package.get_available_components", return_value=[]), \
             patch.object(QMessageBox, "information") as mock_info:
            self.settings._btn_export_pkg.click()
            mock_info.assert_called_once()
            self.assertIn(tr("offline_pkg_export_no_components"), mock_info.call_args[0][2])

    def test_export_button_connected_to_core(self):
        """Натискання кнопки вивантаження викликає export_package з ядра."""
        comp_dir = Path(self.tmp_dir) / "dummy_comp"
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / "model.bin").write_bytes(b"DATA")

        dummy_comp = ComponentExportInfo(
            id="punctuator",
            type="punctuator",
            display_name="Пунктуатор",
            size_bytes=4,
            file_count=1,
            source_dir=comp_dir,
            payload_rel_path="payload/punctuator",
            checksum_file="checksums/punctuator.sha256",
            details={},
        )
        target_dir = Path(self.tmp_dir) / "export_target"

        with patch("whisper_core.offline_package.get_available_components", return_value=[dummy_comp]), \
             patch.object(QFileDialog, "getExistingDirectory", return_value=str(target_dir)), \
             patch("whisper_core.offline_package.export_package") as mock_export:
            
            mock_export.return_value = target_dir / "Balachky-components-test"
            dlg_mock = MagicMock()
            with patch("fronts.desktop.pages.settings.OfflineExportDialog", return_value=dlg_mock) as mock_dlg_cls:
                self.settings._btn_export_pkg.click()
                mock_dlg_cls.assert_called_once_with(str(target_dir), [dummy_comp], self.controller.cfg, parent=self.settings)
                dlg_mock.exec.assert_called_once()

    def test_import_button_connected_to_core(self):
        """Натискання кнопки завантаження відкриває діалог і після виконання оновлює Центр моделей."""
        src_dir = Path(self.tmp_dir) / "import_src"
        src_dir.mkdir(parents=True, exist_ok=True)

        dlg_mock = MagicMock()
        dlg_mock.exec.return_value = OfflineImportDialog.Accepted
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(src_dir)), \
             patch("fronts.desktop.pages.settings.OfflineImportDialog", return_value=dlg_mock) as mock_dlg_cls, \
             patch.object(self.settings, "_refresh_models_hub") as mock_refresh:
            
            self.settings._btn_import_pkg.click()
            mock_dlg_cls.assert_called_once_with(str(src_dir), self.controller.cfg, parent=self.settings)
            dlg_mock.exec.assert_called_once()
            mock_refresh.assert_called_once()


    def test_export_dialog_worker_executes_core_export(self):
        """OfflineExportDialog запускає OfflineExportWorker і виконує export_package з ядра."""
        comp_dir = Path(self.tmp_dir) / "source_comp"
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / "model.bin").write_bytes(b"DATA_1234")

        comp = ComponentExportInfo(
            id="punctuator",
            type="punctuator",
            display_name="Пунктуатор",
            size_bytes=9,
            file_count=1,
            source_dir=comp_dir,
            payload_rel_path="payload/punctuator",
            checksum_file="checksums/punctuator.sha256",
            details={},
        )
        target_dir = Path(self.tmp_dir) / "export_out"

        dlg = OfflineExportDialog(target_dir, [comp], self.controller.cfg, parent=self.settings)
        self.assertIsNone(dlg.pkg_result)

        # Діалог передає воркеру лише ІДЕНТИФІКАТОРИ, а склад пакета ядро бере
        # саме через get_available_components. Без підміни штучний "punctuator"
        # ядру невідомий, воркер віддає порожній пакет — і тест мовчки перевіряє
        # не з'єднання кнопки з ядром, а власну фікстуру. Підміняємо джерело
        # складових, щоб діалог і ядро дивились на ті самі дані.
        with patch("whisper_core.offline_package.get_available_components",
                   return_value=[comp]):
            dlg._start_export()
            dlg._worker.wait(15000)
            _APP.processEvents()

        self.assertIsNotNone(
            dlg.pkg_result,
            "воркер не віддав теку пакета — кнопка не доходить до ядра")
        self.assertTrue(dlg.pkg_result.exists())
        self.assertTrue((dlg.pkg_result / "BALACHKY_COMPONENTS.marker").exists())

    def test_export_button_fails_when_core_mutated(self):
        """Зламане ядро вивантаження ламає сценарій кнопки, а не тихо мовчить."""
        comp_dir = Path(self.tmp_dir) / "source_comp"
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / "model.bin").write_bytes(b"DATA")

        comp = ComponentExportInfo(
            id="punctuator",
            type="punctuator",
            display_name="Пунктуатор",
            size_bytes=4,
            file_count=1,
            source_dir=comp_dir,
            payload_rel_path="payload/punctuator",
            checksum_file="checksums/punctuator.sha256",
            details={},
        )
        target_dir = Path(self.tmp_dir) / "export_out"

        with patch("whisper_core.offline_package.export_package", side_effect=RuntimeError("ядро вивантаження зламано - імітація для тесту")):
            dlg = OfflineExportDialog(target_dir, [comp], self.controller.cfg, parent=self.settings)
            dlg._start_export()
            dlg._worker.wait(5000)
            _APP.processEvents()

            self.assertIsNone(dlg.pkg_result)
            self.assertIn("ядро вивантаження зламано", dlg._status.text())

    def test_import_dialog_worker_executes_core_import(self):
        """OfflineImportDialog запускає OfflineImportWorker і розгортає моделі через import_package."""
        from whisper_core.offline_package import export_package
        comp_dir = Path(self.tmp_dir) / "source_comp"
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / "model.bin").write_bytes(b"DATA_1234")

        comp = ComponentExportInfo(
            id="punctuator",
            type="punctuator",
            display_name="Пунктуатор",
            size_bytes=9,
            file_count=1,
            source_dir=comp_dir,
            payload_rel_path="payload/punctuator",
            checksum_file="checksums/punctuator.sha256",
            details={},
        )
        target_dir = Path(self.tmp_dir) / "export_out"
        with patch("whisper_core.offline_package.get_available_components", return_value=[comp]):
            pkg_dir = export_package(target_dir, self.controller.cfg)

        dlg = OfflineImportDialog(pkg_dir, self.controller.cfg, parent=self.settings)
        self.assertIsNone(dlg.import_result)

        dlg._start_import()
        dlg._worker.wait(5000)
        _APP.processEvents()

        self.assertIsNotNone(dlg.import_result)
        self.assertIn("punctuator", dlg.import_result.installed)

    def test_import_dialog_broken_package_logs_and_shows_warning_not_silence(self):
        """Аудит 'тихі відмови' №3 (settings.py:1867-1870): невалідний офлайн-
        пакет (немає маркера) не має мовчки нічого не робити. Конструктор
        діалогу вже показує власне попередження — перевіряємо, що воно
        реально з'явилось І подія лишилась у журналі, а не просто return."""
        src_dir = Path(self.tmp_dir) / "not_a_package"
        src_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(src_dir)), \
             patch.object(QMessageBox, "warning") as mock_warn, \
             self.assertLogs(level="WARNING") as logs:
            self.settings._btn_import_pkg.click()
        mock_warn.assert_called_once()
        self.assertTrue(
            any("імпорт" in rec.lower() or "import" in rec.lower() for rec in logs.output),
            f"немає запису в журналі про відхилений імпорт: {logs.output}")

    def test_import_dialog_unexpected_error_logs_and_shows_message_not_silence(self):
        """Аудит №3: НЕочікуваний виняток (не OfflinePackageError) у конструкторі
        діалогу раніше проковтувався голим 'except Exception: return' — без
        вікна й без журналу. Тепер людина повинна побачити повідомлення."""
        src_dir = Path(self.tmp_dir) / "import_src"
        src_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(src_dir)), \
             patch("fronts.desktop.pages.settings.OfflineImportDialog",
                   side_effect=RuntimeError("несподівана поломка - імітація для тесту")), \
             patch.object(QMessageBox, "critical") as mock_crit, \
             self.assertLogs(level="ERROR") as logs:
            self.settings._btn_import_pkg.click()
        mock_crit.assert_called_once()
        self.assertIn(tr("offline_pkg_import_unexpected_error"), mock_crit.call_args[0][2])
        self.assertTrue(
            any("несподівана поломка" in rec for rec in logs.output) or
            any("import" in rec.lower() or "імпорт" in rec.lower() for rec in logs.output),
            f"виняток мав потрапити в журнал: {logs.output}")

    def test_import_button_fails_when_core_mutated(self):
        """Мутація в import_package ламає UI-сценарій завантаження."""
        from whisper_core.offline_package import export_package
        comp_dir = Path(self.tmp_dir) / "source_comp"
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / "model.bin").write_bytes(b"DATA_1234")

        comp = ComponentExportInfo(
            id="punctuator",
            type="punctuator",
            display_name="Пунктуатор",
            size_bytes=9,
            file_count=1,
            source_dir=comp_dir,
            payload_rel_path="payload/punctuator",
            checksum_file="checksums/punctuator.sha256",
            details={},
        )
        target_dir = Path(self.tmp_dir) / "export_out"
        with patch("whisper_core.offline_package.get_available_components", return_value=[comp]):
            pkg_dir = export_package(target_dir, self.controller.cfg)

        with patch("whisper_core.offline_package.import_package", side_effect=RuntimeError("ядро встановлення зламано - імітація для тесту")):
            dlg = OfflineImportDialog(pkg_dir, self.controller.cfg, parent=self.settings)
            dlg._start_import()
            dlg._worker.wait(5000)
            _APP.processEvents()

            self.assertIsNone(dlg.import_result)
            self.assertIn("ядро встановлення зламано", dlg._status.text())


if __name__ == "__main__":
    unittest.main()
