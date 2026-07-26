"""Онбординг: лічильник кроків рахує умовний крок відеокарти динамічно, а на
цьому кроці загальний «Назад» ховається (а не висить сірим неактивним)."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop.onboarding import FirstRunWizard


class OnboardingStepCounterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _wizard(self, gpu_possible):
        with patch.object(FirstRunWizard, "_gpu_step_possible",
                          return_value=gpu_possible):
            wiz = FirstRunWizard()
        self.addCleanup(wiz.deleteLater)
        self.addCleanup(lambda: wiz.done(0))
        self.addCleanup(lambda: wiz.done(0))
        return wiz

    def test_total_is_six_without_gpu_step(self):
        wiz = self._wizard(gpu_possible=False)
        self.assertEqual(wiz._total_steps, 6)
        self.assertIn("/6", wiz._eyebrow(1, "onb_sec_welcome"))

    def test_total_is_seven_with_gpu_step(self):
        wiz = self._wizard(gpu_possible=True)
        self.assertEqual(wiz._total_steps, 7)
        # усі кроки рахуються «/7», а крок відеокарти — останній (7/7)
        self.assertIn("/7", wiz._eyebrow(1, "onb_sec_welcome"))
        gpu_label = wiz._eyebrow(wiz._total_steps, "onb_sec_gpu")
        self.assertIn("7/7", gpu_label)

    def test_back_hidden_on_gpu_step(self):
        wiz = self._wizard(gpu_possible=True)
        # крок завантаження моделі (4): «Назад» присутній
        wiz._stack.setCurrentIndex(4)
        wiz._sync_nav()
        self.assertFalse(wiz._back.isHidden())
        # крок відеокарти: загальний «Назад» схований (веде власна навігація)
        wiz._stack.setCurrentIndex(wiz._gpu_index)
        wiz._sync_nav()
        self.assertTrue(wiz._back.isHidden())


class OnboardingVoiceStepTests(unittest.TestCase):
    """Тести кроку «Озвучення»: герметичні перевірки без OR-ассертів,
    мокування model_present та перевірка відновлення кнопок при «Назад»."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _wizard(self, gpu_possible=False):
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=gpu_possible):
            wiz = FirstRunWizard()
        def _cleanup():
            wiz._detach_worker()
            wiz._detach_gpu_worker()
            wiz._detach_voice_worker()
            wiz.done(0)
            if getattr(wiz, "_tray", None) and getattr(wiz._tray, "icon", None):
                try:
                    wiz._tray.icon.hide()
                    wiz._tray.icon.deleteLater()
                except Exception:
                    pass
            wiz.deleteLater()
        self.addCleanup(_cleanup)
        return wiz

    def test_voice_step_skip_no_network(self):
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(3)       # крок «Озвучення»
        wiz._update_voice_page_state()

        with patch("fronts.desktop.onboarding.model_present", return_value=False), \
                patch("fronts.desktop.onboarding.DownloadWorker.start"), \
                patch("whisper_core.tts.voices.download_and_install") as mock_dl:
            wiz._voice_skip_btn.click()
            mock_dl.assert_not_called()
        self.assertIsNone(wiz._voice_worker)
        # за відсутності моделі перейшло на крок завантаження (індекс 4)
        self.assertEqual(wiz._stack.currentIndex(), 4)

    def test_voice_step_download_calls_handler(self):
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(3)
        wiz._update_voice_page_state()

        from fronts.desktop.onboarding import VoiceDownloadWorker
        with patch.object(VoiceDownloadWorker, "start") as mock_start:
            wiz._voice_dl_btn.click()
            self.assertIsNotNone(wiz._voice_worker)
            mock_start.assert_called_once()
            wiz._detach_voice_worker()

    def test_wizard_not_blocked_model_present(self):
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(3)
        wiz._update_voice_page_state()

        wiz._on_voice_failed("Connection timed out")
        self.assertFalse(wiz._voice_skip_btn.isHidden())
        self.assertIn("Не вдалося", wiz._voice_status.text())

        # gpu_present мокаємо свідомо: _finish_or_gpu питає стан CUDA-рантайму
        # НА МАШИНІ, тож на комп'ютері без завантаженого рантайму майстер ішов
        # на крок GPU і accept не викликався — тест падав від чужого оточення,
        # а не від коду (спіймано 24.07 після зачистки машини під живий тест).
        # Так само mock model_snapshot_usable: model_present бачить лише імена,
        # а от model_snapshot_usable відкриває реальні файли на диску — на машині
        # без моделі він поверне False й майстер піде на сторінку «модель пошкоджено»
        # замість _finish_or_gpu → accept не кличе.
        with patch("fronts.desktop.onboarding.model_present", return_value=True), \
                patch("fronts.desktop.onboarding.model_snapshot_usable",
                      return_value=True), \
                patch("whisper_core.cuda_runtime.gpu_present", return_value=False), \
                patch.object(wiz, "accept") as mock_accept:
            wiz._voice_skip_btn.click()
            mock_accept.assert_called_once()

    def test_wizard_not_blocked_model_absent(self):
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(3)
        wiz._update_voice_page_state()

        wiz._on_voice_failed("Connection timed out")
        self.assertFalse(wiz._voice_skip_btn.isHidden())

        with patch("fronts.desktop.onboarding.model_present", return_value=False), \
                patch("fronts.desktop.onboarding.DownloadWorker.start"):
            wiz._voice_skip_btn.click()
            self.assertEqual(wiz._stack.currentIndex(), 4)

    def test_voice_step_go_back_restores_buttons(self):
        # РЕАЛЬНИЙ сценарій глухого кута (суд-2): завантаження успішне ->
        # авто-перехід на крок 4 -> «Назад». Стрибок setCurrentIndex(4) на
        # свіжому майстрі багу не відтворює (кнопки ще в дефолтному стані).
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(3)
        with patch("fronts.desktop.onboarding.VoiceDownloadWorker.start"), \
                patch("fronts.desktop.onboarding.DownloadWorker.start"), \
                patch("fronts.desktop.onboarding.model_present",
                   return_value=False):
            wiz._start_voice_download()
            wiz._on_voice_done()            # ховає кнопки, веде на крок 4
            self.assertEqual(wiz._stack.currentIndex(), 4)
            wiz._go_back()                  # повернення на крок 3 (Озвучення)

        self.assertEqual(wiz._stack.currentIndex(), 3)
        visible = [b for b in (wiz._voice_dl_btn, wiz._voice_next_btn,
                               wiz._voice_skip_btn)
                   if not b.isHidden()]
        self.assertTrue(
            visible, "глухий кут: після Назад з кроку 4 жодної видимої кнопки")

    def test_go_back_no_tts_engine_no_voice_buttons(self):
        # полегшена збірка без рушія озвучення: «Назад» зі сторінки моделі
        # (крок 4) на крок голосу (3) НЕ мусить знову пропонувати завантажити
        # непридатний голос. Раніше _go_back сліпо кликав _update_voice_page_state,
        # яке показувало кнопку завантаження 714 МБ голосу без змоги його
        # відтворити (суд-3, знахідка 24.07).
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(4)
        with patch("fronts.desktop.onboarding._tts_engine_available",
                   return_value=False):
            wiz._go_back()
        self.assertEqual(wiz._stack.currentIndex(), 3)
        self.assertTrue(
            wiz._voice_dl_btn.isHidden(),
            "без рушія озвучення Назад не показує кнопку завантаження голосу")
        self.assertTrue(
            wiz._voice_retry_btn.isHidden(),
            "без рушія озвучення Назад не показує кнопку повтору голосу")
        # «Пропустити» лишається — не глухий кут, користувач може піти далі.
        self.assertFalse(
            wiz._voice_skip_btn.isHidden(),
            "без рушія озвучення «Пропустити» лишається видимою (не глухий кут)")




class OnboardingTrayTests(unittest.TestCase):
    """Трей має жити вже під час майстра (п.1 фідбеку 24.07). Вартовий проти
    мертвого коду: хибний імпорт у блоці трея мовчки ковтався except-ом і
    self._tray лишався None назавжди (вердикт повторного суду 24.07)."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_tray_alive_during_wizard(self):
        with patch.object(FirstRunWizard, "_gpu_step_possible",
                          return_value=False):
            wiz = FirstRunWizard()
        self.addCleanup(wiz.deleteLater)
        self.addCleanup(lambda: wiz.done(0))
        # offscreen трей конструюється і show() працює (перевірено 24.07) —
        # skip не потрібен, інакше вартовий беззубий саме в гейті
        self.assertIsNotNone(
            wiz._tray, "трей не створився під час майстра — імпорти/конструктор")
        self.assertTrue(wiz._tray.icon.isVisible())




class FocusVisibilityPixelTests(unittest.TestCase):
    """Фокус має бути ВИДИМИМ на всіх 4 класах кнопок в обох темах.
    Піксельний вартовий: рендер до/після setFocus мусить відрізнятись
    (суд-3 24.07: accent-фокус у денній темі був невидимий, бо FOCUS==GOLD;
    visual_gate фокус-стани не сканує взагалі)."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        # тест ганяє обидві теми і закінчує нічною — ГЛОБАЛЬНИЙ стан теми треба
        # повернути до денної, інакше наступні тести процесу (splash та ін.)
        # флейкають порядкозалежно (гейт 24.07 після злиття)
        import fronts.desktop.theme as theme
        theme.apply_theme(cls._app, night=False)

    def _render_pair(self, night, props):
        from PySide6.QtWidgets import QPushButton, QWidget, QHBoxLayout
        import fronts.desktop.theme as theme
        theme.apply_theme(self._app, night=night)
        host = QWidget()
        lay = QHBoxLayout(host)
        # друга кнопка забирає фокус «до», щоб стан був чистим
        other = QPushButton("інша", host)
        btn = QPushButton("Далі", host)
        for k, v in props.items():
            btn.setProperty(k, v)
        lay.addWidget(other); lay.addWidget(btn)
        host.show()
        other.setFocus()
        self._app.processEvents()
        before = btn.grab().toImage()
        btn.setFocus()
        self._app.processEvents()
        after = btn.grab().toImage()
        host.deleteLater()
        return before, after

    def test_focus_visible_all_classes_both_themes(self):
        cases = [
            ("base", {}), ("accent", {"accent": True}),
            ("ghost", {"ghost": True}), ("danger", {"danger": True}),
        ]
        for night in (False, True):
            for name, props in cases:
                with self.subTest(theme="night" if night else "day", cls=name):
                    before, after = self._render_pair(night, props)
                    self.assertEqual(before.size(), after.size())
                    diff = sum(
                        1
                        for y in range(before.height())
                        for x in range(before.width())
                        if before.pixel(x, y) != after.pixel(x, y)
                    )
                    self.assertGreater(
                        diff, 0,
                        f"фокус невидимий: {name} у "
                        f"{'нічній' if night else 'денній'} темі")


if __name__ == "__main__":
    unittest.main()


class OnboardingPresenceChecksTests(unittest.TestCase):
    """Тести перевірок наявності та придатності компонентів (голос, модель STT, CUDA-рантайм).
    Всі перевірки мокуються — тести герметичні й не залежать від заліза/диска."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def _wizard(self, gpu_possible=True):
        with patch.object(FirstRunWizard, "_gpu_step_possible", return_value=gpu_possible):
            wiz = FirstRunWizard()
        def _cleanup():
            wiz._detach_worker()
            wiz._detach_gpu_worker()
            wiz._detach_voice_worker()
            wiz.done(0)
            if getattr(wiz, "_tray", None) and getattr(wiz._tray, "icon", None):
                try:
                    wiz._tray.icon.hide()
                    wiz._tray.icon.deleteLater()
                except Exception:
                    pass
            wiz.deleteLater()
        self.addCleanup(_cleanup)
        return wiz

    def test_voice_present_shows_ready_and_hides_download_btn(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(3)
        with patch("whisper_core.tts.voices.voice_available", return_value=True):
            wiz._update_voice_page_state()

        self.assertTrue(wiz._voice_dl_btn.isHidden())
        self.assertFalse(wiz._voice_next_btn.isHidden())
        self.assertIn("готово", wiz._voice_status.text().lower())

    def test_voice_absent_offers_download(self):
        wiz = self._wizard()
        wiz._stack.setCurrentIndex(3)
        with patch("whisper_core.tts.voices.voice_available", return_value=False):
            wiz._update_voice_page_state()

        self.assertFalse(wiz._voice_dl_btn.isHidden())
        self.assertFalse(wiz._voice_skip_btn.isHidden())
        self.assertTrue(wiz._voice_next_btn.isHidden())

    def test_stt_model_present_and_usable_skips_download(self):
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(3)
        with patch("fronts.desktop.onboarding.model_present", return_value=True), \
                patch("fronts.desktop.onboarding.model_snapshot_usable", return_value=True), \
                patch.object(wiz, "_finish_or_gpu") as mock_finish:
            wiz._advance_from_voice()
            mock_finish.assert_called_once()

    def test_stt_model_corrupted_shows_corrupted_status(self):
        wiz = self._wizard(gpu_possible=False)
        wiz._stack.setCurrentIndex(3)
        with patch("fronts.desktop.onboarding.model_present", return_value=True), \
                patch("fronts.desktop.onboarding.model_snapshot_usable", return_value=False):
            wiz._advance_from_voice()

        self.assertEqual(wiz._stack.currentIndex(), 4)
        self.assertIn("пошкоджено", wiz._dl_status.text().lower())
        self.assertFalse(wiz._dl_retry.isHidden())

    def test_cuda_runtime_ready_shows_ready_and_hides_download_btn(self):
        wiz = self._wizard(gpu_possible=True)
        with patch("whisper_core.cuda_runtime.gpu_present", return_value=True), \
                patch("whisper_core.cuda_runtime.runtime_ready", return_value=True):
            wiz._update_gpu_page_state()

        self.assertTrue(wiz._gpu_yes.isHidden())
        self.assertTrue(wiz._gpu_no.isHidden())
        self.assertFalse(wiz._gpu_next_btn.isHidden())
        self.assertIn("готове", wiz._gpu_intro.text().lower())
        self.assertTrue(wiz.use_gpu)

    def test_cuda_runtime_absent_offers_download(self):
        wiz = self._wizard(gpu_possible=True)
        with patch("whisper_core.cuda_runtime.gpu_present", return_value=True), \
                patch("whisper_core.cuda_runtime.runtime_ready", return_value=False):
            wiz._update_gpu_page_state()

        self.assertFalse(wiz._gpu_yes.isHidden())
        self.assertFalse(wiz._gpu_no.isHidden())
        self.assertTrue(wiz._gpu_next_btn.isHidden())
