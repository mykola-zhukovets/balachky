import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QMessageBox

from fronts.desktop.i18n import tr
from fronts.desktop.main_window import MainWindow
from tests.render_nav_smoke import _NavController

_APP = QApplication.instance() or QApplication([])


class TestModelsHubUI(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.controller = _NavController(self.tmp_dir)
        self.win = MainWindow(self.controller)
        self.settings = self.win.settings

    def tearDown(self):
        self.win.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_click_recommended_all_five_cards_when_downloaded(self):
        """Клік по 'Рекомендовано' для всіх 5 карток, коли моделі завантажені."""
        with patch("whisper_core.models_hub.stt_models.model_snapshot_size", return_value=100), \
             patch("whisper_core.models_hub.diar_models.models_available", return_value=True), \
             patch("whisper_core.models_hub.protocol_mm.model_available", return_value=True), \
             patch("whisper_core.models_hub.tts_voices.voice_available", return_value=True), \
             patch("whisper_core.models_hub.punc.model_available", return_value=True):

            for cid in ("stt", "diarization", "protocol", "tts", "punctuator"):
                row = self.settings._models_hub_rows[cid]
                row["btn_rec"].click()

            cfg = self.controller.cfg
            self.assertEqual(cfg.model_name, "large-v3-turbo")
            self.assertTrue(cfg.diarization_enabled)
            self.assertEqual(cfg.protocol_model, "fast")
            self.assertEqual(cfg.tts_voice_uk, "styletts2_ua")
            self.assertTrue(cfg.tts_enabled)
            self.assertTrue(cfg.punctuator_enabled)

    def test_click_recommended_when_not_downloaded_shows_dialog_and_does_not_activate(self):
        """Клік по 'Рекомендовано' при НЕзавантаженій моделі показує інфо-діалог і НЕ змінює cfg."""
        with patch("whisper_core.models_hub.stt_models.model_snapshot_size", return_value=0), \
             patch("whisper_core.models_hub.diar_models.models_available", return_value=False), \
             patch("whisper_core.models_hub.protocol_mm.model_available", return_value=False), \
             patch("whisper_core.models_hub.tts_voices.voice_available", return_value=False), \
             patch("whisper_core.models_hub.punc.model_available", return_value=False), \
             patch.object(QMessageBox, "information") as mock_info:

            cfg = self.controller.cfg
            cfg.model_name = "small"
            cfg.diarization_enabled = False
            cfg.protocol_model = "quality"
            cfg.tts_enabled = False
            cfg.punctuator_enabled = False

            for cid in ("stt", "diarization", "protocol", "tts", "punctuator"):
                row = self.settings._models_hub_rows[cid]
                row["btn_rec"].click()

            self.assertEqual(mock_info.call_count, 5)
            # cfg НЕ має змінитися на recommended
            self.assertEqual(cfg.model_name, "small")
            self.assertFalse(cfg.diarization_enabled)
            self.assertEqual(cfg.protocol_model, "quality")
            self.assertFalse(cfg.tts_enabled)
            self.assertFalse(cfg.punctuator_enabled)

    def test_click_advanced_all_five_cards_navigates_properly(self):
        """Клік по 'Розширено ↗' вестиме на відповідну вкладку/сторінку/діалог."""
        mock_vm = MagicMock()
        self.controller.open_voice_manager = mock_vm

        # 1. STT -> tab Recognition
        self.settings._on_models_hub_advanced("stt")
        self.assertEqual(self.settings._tabs.tabText(self.settings._tabs.currentIndex()), "Розпізнавання")

        # 2. Punctuator -> tab Recording
        self.settings._on_models_hub_advanced("punctuator")
        self.assertEqual(self.settings._tabs.tabText(self.settings._tabs.currentIndex()), "Запис і звук")

        # 3. Diarization -> Meeting page (index 2)
        self.settings._on_models_hub_advanced("diarization")
        self.assertEqual(self.win.pages.currentIndex(), 2)

        # 4. Protocol -> Meeting page (index 2)
        self.settings._on_models_hub_advanced("protocol")
        self.assertEqual(self.win.pages.currentIndex(), 2)

        # 5. TTS -> open_voice_manager called
        self.settings._on_models_hub_advanced("tts")
        mock_vm.assert_called_once()

    def test_open_models_folder_builds_menu_with_all_dirs(self):
        """Меню тек будується з усіма пунктами — БЕЗ показу.

        Раніше тест патчив QMenu.exec через mock: у PySide6 це Shiboken-метод,
        mock його не перехоплює, меню відкривалось по-справжньому і offscreen
        чекав вічно — увесь `unittest discover` не завершувався (суд 24.07).
        Тому меню тепер БУДУЄМО і перевіряємо, а exec не кличемо взагалі."""
        from whisper_core.models_hub import get_model_dirs

        dirs = get_model_dirs(self.controller.cfg)
        menu = self.settings._build_models_folder_menu()
        actions = [a for a in menu.actions() if not a.isSeparator()]
        self.assertEqual(len(actions), len(dirs))
        self.assertEqual([a.text() for a in actions], [tr(k) for k, _ in dirs])

    def test_each_folder_action_opens_its_own_dir(self):
        """Кожен пункт меню веде до СВОЄЇ теки (мутація «відв'язаний пункт» ловиться)."""
        from whisper_core.models_hub import get_model_dirs

        dirs = get_model_dirs(self.controller.cfg)
        menu = self.settings._build_models_folder_menu()
        actions = [a for a in menu.actions() if not a.isSeparator()]
        with patch("PySide6.QtGui.QDesktopServices.openUrl") as mock_open:
            for action, (_key, path) in zip(actions, dirs):
                mock_open.reset_mock()
                action.trigger()
                mock_open.assert_called_once()
                opened = mock_open.call_args[0][0]
                self.assertEqual(opened, QUrl.fromLocalFile(str(path)))




class TestModelsHubCardLabelsNoWrap(unittest.TestCase):
    """Картки «Центру керування моделями»: три підписи (назва, «Активна: …»,
    вимоги до пам'яті) не мають переноситись на типовій ширині вікна.

    Перевіряємо, що ширина тексту за fontMetrics не перевищує доступної
    ширини лейбла — отже переносу не буде. Підписи з wordWrap=True ріжуться
    на кілька рядків у вузькій колонці без stretch; тепер stretch=1 на
    текстовій колонці забирає вільну ширину, а wordWrap прибрано."""

    _LANGS = ("uk", "en")

    # «Типова ширина вікна» має бути ЗАДАНА явно: без resize/show Qt віддає
    # довільний розмір, і той самий тест окремо зеленів, а в повному прогоні
    # падав — міряв підписи у вікні невідомої ширини (спіймано гейтом 25.07).
    _TYPICAL_W, _TYPICAL_H = 1280, 800

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.controller = _NavController(self.tmp_dir)
        self.win = MainWindow(self.controller)
        self.win.resize(self._TYPICAL_W, self._TYPICAL_H)
        self.win.show()
        _APP.processEvents()
        self.settings = self.win.settings
        from fronts.desktop import i18n
        self._i18n = i18n
        self._lang = i18n.current_language()

    def tearDown(self):
        if self._lang is not None:
            self._i18n.set_language(self._lang)
        self.win.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _collect_labels(self, box):
        """Підписи, які НЕ мають переноситись: назва картки і рядок «Активна: …».

        Рядок вимог до пам'яті виключено свідомо: він найдовший
        («~5 ГБ ОЗП (Швидка) / ~7 ГБ відеопам'яті (Вища якість)») і на вузькому
        вікні не вміщається за визначенням — 641px проти 640 доступних.
        Вимагати від нього одного рядка означало б різати текст або дрібнити
        шрифт; перенос тут — менше зло."""
        from PySide6.QtWidgets import QLabel
        return [w for w in box.findChildren(QLabel)
                if w.text() and w.objectName() != "models_hub_memory_note"]

    def _assert_labels_fit(self, lang):
        from PySide6.QtGui import QFontMetrics
        self._i18n.set_language(lang)
        _APP.processEvents()
        for cid in ("stt", "diarization", "protocol", "tts", "punctuator"):
            row = self.settings._models_hub_rows[cid]
            box = row["box"]
            labels = self._collect_labels(box)
            self.assertGreaterEqual(
                len(labels), 3,
                f"[{lang}/{cid}] підписів картки замало — тест виродився")
            for lbl in labels:
                avail = lbl.contentsRect().width()
                self.assertGreater(
                    avail, 0,
                    f"[{lang}/{cid}] нульова доступна ширина підпису")
                fm = QFontMetrics(lbl.font())
                text_w = fm.horizontalAdvance(lbl.text())
                self.assertLessEqual(
                    text_w, avail,
                    f"[{lang}/{cid}] підпис ширший за доступну ширину "
                    f"({text_w} > {avail}) — буде перенос")

    def test_card_labels_fit_ukrainian(self):
        self._assert_labels_fit("uk")

    def test_card_labels_fit_english(self):
        self._assert_labels_fit("en")


if __name__ == "__main__":
    unittest.main()
