"""Блокер релізу: слабкий інтернет замикав людину на кроці завантаження моделі.

Тут перевіряємо:
  1) Б1: DownloadWorker.run реально викликає resumable_download_file (без hf_constants),
     а вимкнення цієї функції повертає помилку.
  2) Шлях «пропустив» у main() показує повідомлення і СТАРТУЄ далі без мовного пакета
     (вихід із програми відхилено власником — feature/no-model-state).
  3) Б2: Підказка про можливість продовження завантаження ВИДИМА на кроці завантаження.
  4) Б3: Докачка файлів підтримує HTTP Range (206) і відновлюється з місця обриву без витоку
     глобального стану, а перевірка вільного місця спрацьовує ДО старту.
  5) Ч2: Новий крок «Додаткові можливості» з 4 компонентами, сторожами інтерфейсу, пропуском та підказками.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ДО імпорту fronts.desktop.app: цей модуль викликає main(), яке піднімає
# реальний QLocalServer single-instance. _isolation виставляє
# BALACHKY_INSTANCE_SUFFIX, щоб цей offscreen-екземпляр не займав робочий
# канал "balachky-single" (кейс 31.07 — відбило живий запуск власника).
from tests._isolation import reset_process_caches
reset_process_caches()

from PySide6.QtWidgets import QApplication, QLabel
from PySide6.QtCore import QSettings

from fronts.desktop.app import _apply_onboarding_result
from fronts.desktop.onboarding import FirstRunWizard

_DOWNLOAD_STEP = 5          # індекс сторінки завантаження моделі у _stack (після _page_extra на 4)


class DownloadWorkerUsesResumableDownloadTests(unittest.TestCase):
    """Б1: Перевіряємо, що DownloadWorker.run реально викликає resumable_download_file."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_download_worker_uses_resumable_download_file(self):
        from fronts.desktop.onboarding import DownloadWorker
        tmp_dir = tempfile.mkdtemp(prefix="balachky-dw-test-")
        worker = DownloadWorker("mobiuslabsgmbh/faster-whisper-large-v3-turbo", tmp_dir)

        calls = []
        def dummy_download(url, dest, **kwargs):
            calls.append((url, dest))

        with patch("fronts.desktop.onboarding.check_free_space"), \
             patch("fronts.desktop.onboarding.resumable_download_file", side_effect=dummy_download):
            worker.run()

        self.assertGreater(len(calls), 0, "DownloadWorker.run мусить викликати resumable_download_file")
        self.assertTrue(any("config.json" in str(dest) for url, dest in calls))
        self.assertTrue(any("model.bin" in str(dest) for url, dest in calls))

    def test_disabling_resumable_download_fails_worker(self):
        from fronts.desktop.onboarding import DownloadWorker
        tmp_dir = tempfile.mkdtemp(prefix="balachky-dw-test2-")
        worker = DownloadWorker("mobiuslabsgmbh/faster-whisper-large-v3-turbo", tmp_dir)

        failed_messages = []
        worker.failed.connect(failed_messages.append)

        with patch("fronts.desktop.onboarding.check_free_space"), \
             patch("fronts.desktop.onboarding.resumable_download_file", side_effect=RuntimeError("download disabled")):
            worker.run()

        self.assertEqual(len(failed_messages), 1)
        self.assertIn("download disabled", failed_messages[0])


class MainAppSkippedBranchTests(unittest.TestCase):
    """Б1: Суддя заміняв `if model_skipped:` на `if False:`. Перевіряємо main() у разі пропуску."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_main_skipped_model_warns_and_keeps_running(self):
        """Пропуск завантаження: кажемо стан і СТАРТУЄМО далі, а не виходимо.

        Раніше тут стояв sys.exit(0) — програма виганяла людину, якщо та на
        слабкому інтернеті натиснула «Пропустити». Власник це відхилив, і
        feature/no-model-state дала чесний старт без мовного пакета. Тест
        переписано під фактичну поведінку: налаштування збережені, повідомлення
        показане, виходу НЕМА, виконання доходить до підняття рушія.

        Обриваємо main() контрольовано на створенні потоку завантаження рушія:
        сам факт, що ми туди дійшли, і є доказом, що виходу не сталося. Без
        обриву main() пішов би піднімати справжнє вікно й тест би завис.

        ІЗОЛЯЦІЯ (знахідка 31.07): main() у dev-режимі читає config.toml із
        КОРЕНЯ РЕПО (whisper_core.paths.USER_DIR == APP_ROOT поза frozen-збіркою) —
        це той самий файл, яким користується власник, коли сам запускає
        run_app.py. Без ізоляції тест читав РЕАЛЬНИЙ dev config.toml (одного разу
        там був ui_language="en") і виставляв i18n у "en" на решту процесу —
        14 тестів після цього в алфавітному порядку падали з англійськими
        текстами. Тут підміняємо paths.config_path() і QSettings("Balachky",
        "Balachky") на тимчасові файли, щоб main() нічого не читав і не писав
        за межами tmp-теки цього тесту."""
        from fronts.desktop.app import main

        class _ReachedEngine(RuntimeError):
            """Мітка: виконання дійшло до підняття рушія, тобто виходу не було."""

        tmp_dir = Path(tempfile.mkdtemp(prefix="balachky-main-isolated-"))
        tmp_config_path = tmp_dir / "config.toml"
        tmp_settings_path = tmp_dir / "settings.ini"

        def _isolated_settings(*_a, **_kw):
            return QSettings(str(tmp_settings_path), QSettings.IniFormat)

        self.addCleanup(reset_process_caches)

        with patch("PySide6.QtNetwork.QLocalSocket.waitForConnected", return_value=False), \
             patch("fronts.desktop.app._should_show_onboarding", return_value=True), \
             patch("fronts.desktop.onboarding.FirstRunWizard") as mock_wiz_cls, \
             patch("fronts.desktop.app._apply_onboarding_result", return_value=True) as mock_apply, \
             patch("PySide6.QtWidgets.QMessageBox") as mock_box_cls, \
             patch("sys.exit", side_effect=AssertionError("програма НЕ мусить виходити при пропуску")), \
             patch("fronts.desktop.app._EngineLoadThread", side_effect=_ReachedEngine), \
             patch("whisper_core.paths.config_path", return_value=tmp_config_path), \
             patch("fronts.desktop.app.QSettings", side_effect=_isolated_settings):

            wiz_inst = mock_wiz_cls.return_value
            wiz_inst.exec.return_value = True

            with self.assertRaises(_ReachedEngine):
                main()

            mock_apply.assert_called_once()
            mock_box_cls.return_value.exec.assert_called_once()


class SkipButtonOnDownloadStepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _wizard(self):
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=False):
            wiz = FirstRunWizard()

        def _cleanup():
            wiz._detach_worker()
            wiz._detach_gpu_worker()
            wiz._detach_voice_worker()
            wiz._detach_extra_worker()
            wiz.done(0)
            wiz.deleteLater()

        self.addCleanup(_cleanup)
        return wiz

    def test_skip_visible_while_download_runs(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(_DOWNLOAD_STEP)
        with patch("fronts.desktop.onboarding.model_present", return_value=False), \
             patch("fronts.desktop.onboarding.DownloadWorker.start"):
            wiz._start_download()
        self.assertIsNotNone(wiz._worker, "воркер завантаження мусить бути живим")
        self.assertFalse(wiz._dl_skip.isHidden(),
                         "«Пропустити» мусить бути видимою ПІД ЧАС завантаження")
        self.assertTrue(wiz._dl_skip.isEnabled())

    def test_skip_visible_after_cancel(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(_DOWNLOAD_STEP)
        with patch("fronts.desktop.onboarding.model_present", return_value=False), \
             patch("fronts.desktop.onboarding.DownloadWorker.start"):
            wiz._start_download()
        wiz._cancel_download()
        self.assertFalse(wiz._dl_skip.isHidden(),
                         "«Пропустити» мусить лишатись видимою після скасування")
        self.assertTrue(wiz._dl_skip.isEnabled())

    def test_skip_click_sets_flag_and_accepts(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(_DOWNLOAD_STEP)
        with patch("fronts.desktop.onboarding.model_present", return_value=False), \
             patch("fronts.desktop.onboarding.DownloadWorker.start"):
            wiz._start_download()
        with patch.object(FirstRunWizard, "accept") as accept:
            wiz._dl_skip.click()
        self.assertTrue(wiz.model_skipped)
        accept.assert_called_once()
        self.assertIsNone(wiz._worker, "воркер мусить бути від'єднаний")


class SkippedPathSavesConfigTests(unittest.TestCase):
    """Шлях «пропустив» зберігає конфіг так само, як звичайний."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _cfg(self):
        saved = {"count": 0}
        cfg = SimpleNamespace(
            model_name="", model_dir="", language="", ui_language="",
            ptt_key="", device="cpu", compute_type="int8",
            save=lambda: saved.__setitem__("count", saved["count"] + 1),
        )
        return cfg, saved

    def _settings(self):
        path = str(Path(tempfile.mkdtemp(prefix="balachky-skip-")) / "settings.ini")
        return QSettings(path, QSettings.IniFormat)

    def test_skipped_wizard_saves_everything(self):
        cfg, saved = self._cfg()
        settings = self._settings()
        wizard = SimpleNamespace(
            model_name="large-v3", model_dir="X:/models", language="en",
            ptt_key="ctrl+alt+r", use_gpu=False, model_skipped=True)
        skipped = _apply_onboarding_result(cfg, wizard, settings)
        self.assertTrue(skipped, "прапорець «пропущено» мусить дійти до main()")
        self.assertEqual(cfg.model_name, "large-v3")
        self.assertEqual(cfg.model_dir, "X:/models")
        self.assertEqual(cfg.language, "en")
        self.assertEqual(cfg.ui_language, "en")
        self.assertEqual(cfg.ptt_key, "ctrl+alt+r")
        self.assertEqual(saved["count"], 1, "конфіг мусить бути збережений")
        self.assertIsNotNone(settings.value("onboarded"),
                             "onboarded мусить бути виставлений і на шляху «пропустив»")

    def test_normal_path_unchanged(self):
        cfg, saved = self._cfg()
        settings = self._settings()
        wizard = SimpleNamespace(
            model_name="large-v3-turbo", model_dir="X:/models", language="uk",
            ptt_key="ctrl+shift+space", use_gpu=False)
        self.assertFalse(_apply_onboarding_result(cfg, wizard, settings))
        self.assertEqual(cfg.model_name, "large-v3-turbo")
        self.assertEqual(saved["count"], 1)
        self.assertIsNotNone(settings.value("onboarded"))


class ResumeHintTests(unittest.TestCase):
    """Б2: Текст кроку завантаження мусить бути ВИДИМИМ на кроці завантаження."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_hint_visible_on_download_step(self):
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=False):
            wiz = FirstRunWizard()
        self.addCleanup(lambda: (wiz.done(0), wiz.deleteLater()))

        wiz._stack.setCurrentIndex(_DOWNLOAD_STEP)
        self.assertTrue(hasattr(wiz, "_dl_resume_hint"), "контрол підказки мусить бути у майстрі")
        self.assertIsInstance(wiz._dl_resume_hint, QLabel)
        self.assertFalse(wiz._dl_resume_hint.isHidden(), "підказка докачки мусить бути ВИДИМА на кроці завантаження")
        self.assertIn("Завантаження можна перервати", wiz._dl_resume_hint.text())


class ResumableDownloadTests(unittest.TestCase):
    """Б3: Обрив мережі лишає .incomplete файл і друга спроба продовжує з HTTP Range (206),
    а замалий диск дає ранню помилку ДО старту завантаження.
    """

    def test_resumable_download_file_with_range(self):
        from fronts.desktop.onboarding import resumable_download_file

        tmp = Path(tempfile.mkdtemp(prefix="balachky-resumable-"))
        dest = tmp / "model.bin"
        incomplete = tmp / "model.bin.incomplete"

        incomplete.write_bytes(b"xxxx")

        requests_received = []

        class DummyResponse:
            status = 206
            headers = {"Content-Range": "bytes 4-7/8"}
            def getcode(self): return 206
            def read(self, amt):
                if getattr(self, "_read_done", False):
                    return b""
                self._read_done = True
                return b"yyyy"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def dummy_urlopen(req, timeout=30):
            requests_received.append(req.headers.get("Range"))
            return DummyResponse()

        with patch("urllib.request.urlopen", side_effect=dummy_urlopen):
            resumable_download_file(
                "https://example.invalid/model.bin",
                dest,
                expected_size=8,
                expected_sha256=__import__("hashlib").sha256(
                    b"xxxxyyyy").hexdigest(),
            )

        self.assertEqual(requests_received, ["bytes=4-"])
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"xxxxyyyy")

    def test_insufficient_disk_space_fails_early(self):
        from fronts.desktop.onboarding import check_free_space

        tmp = Path(tempfile.mkdtemp(prefix="balachky-disk-test-"))
        with patch("shutil.disk_usage", return_value=SimpleNamespace(total=1000, free=100, used=900)):
            with self.assertRaises(OSError) as ctx:
                check_free_space(tmp, required_bytes=5000)
            err_msg = str(ctx.exception)
            self.assertIn("потрібно", err_msg.lower())
            self.assertIn("є", err_msg.lower())


class OnboardingExtraFeaturesStepTests(unittest.TestCase):
    """Ч2: Сторожі екрана «Додаткові можливості»."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _wizard(self):
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=False):
            wiz = FirstRunWizard()
        self.addCleanup(lambda: (wiz.done(0), wiz.deleteLater()))
        return wiz

    def test_extra_defaults_none_checked(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(4)
        for comp_id in ("diarization", "protocol", "punctuator", "tts"):
            chk, sz, is_dl = wiz._extra_chks[comp_id]
            if not is_dl:
                self.assertFalse(chk.isChecked(), f"Чекбокс {comp_id} не повинен бути позначеним за умовчанням")

    def test_extra_sum_updates_on_toggle(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(4)
        self.assertIn("Нічого не обрано", wiz._extra_sum_label.text())

        chk, sz, is_dl = wiz._extra_chks["diarization"]
        if not is_dl:
            chk.setChecked(True)
            self.assertIn("Обрано для завантаження", wiz._extra_sum_label.text())
            self.assertNotIn("Нічого не обрано", wiz._extra_sum_label.text())

    def test_extra_skip_btn_visible_and_enabled(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(4)
        self.assertFalse(wiz._extra_skip_btn.isHidden(), "Кнопка пропуску додаткових компонентів мусить бути ВИДИМА")
        self.assertTrue(wiz._extra_skip_btn.isEnabled(), "Кнопка пропуску додаткових компонентів мусить бути АКТИВНА")

    def test_extra_hints_and_accessible_names_present(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(4)
        for comp_id in ("diarization", "protocol", "punctuator", "tts"):
            chk, sz, is_dl = wiz._extra_chks[comp_id]
            self.assertTrue(bool(chk.toolTip()), f"Чекбокс {comp_id} мусить мати setToolTip")
            self.assertTrue(bool(chk.accessibleName()), f"Чекбокс {comp_id} мусить мати setAccessibleName")

        self.assertTrue(bool(wiz._extra_skip_btn.toolTip()))
        self.assertTrue(bool(wiz._extra_skip_btn.accessibleName()))


if __name__ == "__main__":
    unittest.main()
