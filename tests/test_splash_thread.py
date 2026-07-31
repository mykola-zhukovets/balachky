"""Юніти виносу рушія в потік + гейтів splash (feature/splash-thread).

Логіка _EngineLoadThread перевіряється БЕЗ QThread.start()/event-loop: run() —
звичайний метод, кличемо його напряму, сигнали з DirectConnection (той самий
потік) спрацьовують синхронно. Так тест детермінований і не тягне ~3 ГБ моделі
(Engine замокано).
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class _QtBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])


class EngineLoadThreadTests(_QtBase):
    def test_run_emits_ready_on_success(self):
        import fronts.desktop.app as appmod
        sentinel = object()
        with mock.patch.object(appmod, "Engine", return_value=sentinel) as eng:
            thr = appmod._EngineLoadThread(SimpleNamespace(name="cfg"))
            ready, rec = [], []
            thr.ready.connect(ready.append)
            thr.needs_recovery.connect(rec.append)
            thr.run()                       # синхронно, у цьому ж потоці
        eng.assert_called_once()
        self.assertEqual(ready, [sentinel])
        self.assertEqual(rec, [])

    def test_run_logs_with_empty_config_without_breaking_ready_signal(self):
        """Routine-лог не вимагає від часткового cfg model/device/compute полів."""
        import fronts.desktop.app as appmod
        sentinel = object()
        with mock.patch.object(appmod, "Engine", return_value=sentinel):
            thr = appmod._EngineLoadThread(SimpleNamespace())
            ready = []
            thr.ready.connect(ready.append)
            thr.run()
        self.assertEqual(ready, [sentinel])

    def test_run_emits_recovery_on_revision_unavailable(self):
        import fronts.desktop.app as appmod
        from whisper_core.engine import ModelRevisionUnavailable
        err = ModelRevisionUnavailable("m", None, "abc", False)
        with mock.patch.object(appmod, "Engine", side_effect=err):
            thr = appmod._EngineLoadThread(SimpleNamespace())
            ready, rec = [], []
            thr.ready.connect(ready.append)
            thr.needs_recovery.connect(rec.append)
            thr.run()
        self.assertEqual(ready, [])         # діалог у потоці НЕ відкривається
        self.assertEqual(rec, [err])        # лише сигнал у GUI

    def test_start_emits_failed_on_raw_error_no_hang(self):
        """РЕГРЕСІЯ hang: сирий виняток рушія (ctranslate2 «Unable to open file»,
        CUDA OOM тощо) через РЕАЛЬНИЙ .start() → емітиться failed, петля
        очікування завершується САМА (не по таймауту-запобіжнику) → не зависає.
        Синхронний run() цю гонку не ловить — потрібен саме .start()-шлях."""
        import fronts.desktop.app as appmod
        from PySide6.QtCore import QEventLoop, QTimer
        boom = RuntimeError("ctranslate2: Unable to open file model.bin")
        box = {}
        loop = QEventLoop()

        def _ready(e):
            box["ready"] = e
            loop.quit()

        def _recovery(e):
            box["recovery"] = e
            loop.quit()

        def _failed(e):
            box["fatal"] = e
            loop.quit()

        def _timeout():
            box["timeout"] = True    # петля НЕ мала дожити до цього — це був би hang
            loop.quit()

        with mock.patch.object(appmod, "Engine", side_effect=boom):
            thr = appmod._EngineLoadThread(SimpleNamespace())
            thr.ready.connect(_ready)
            thr.needs_recovery.connect(_recovery)
            thr.failed.connect(_failed)
            guard = QTimer()
            guard.setSingleShot(True)
            guard.timeout.connect(_timeout)
            guard.start(4000)        # запобіжник від вічного hang у самому тесті
            thr.start()
            loop.exec()
            thr.wait(4000)
            guard.stop()

        self.assertIn("fatal", box)          # сирий виняток → failed (не тихе зникнення)
        self.assertIs(box["fatal"], boom)
        self.assertNotIn("ready", box)
        self.assertNotIn("recovery", box)
        self.assertNotIn("timeout", box)     # завершилась сама → НЕ hang

    def test_run_emits_failed_on_raw_error(self):
        """Синхронна перевірка каналу: сирий виняток → failed, не ready/recovery."""
        import fronts.desktop.app as appmod
        boom = OSError("Unable to open file model.bin")
        with mock.patch.object(appmod, "Engine", side_effect=boom):
            thr = appmod._EngineLoadThread(SimpleNamespace())
            ready, rec, fail = [], [], []
            thr.ready.connect(ready.append)
            thr.needs_recovery.connect(rec.append)
            thr.failed.connect(fail.append)
            thr.run()
        self.assertEqual(ready, [])
        self.assertEqual(rec, [])
        self.assertEqual(fail, [boom])


class SplashMinTimeTests(unittest.TestCase):
    def test_remaining_pads_up_to_min(self):
        from fronts.desktop.app import _splash_min_remaining
        self.assertEqual(_splash_min_remaining(0, 800), 800)
        self.assertEqual(_splash_min_remaining(200, 800), 600)

    def test_remaining_never_negative(self):
        """Реальне завантаження довше за мінімум → 0 (жодного штучного sleep)."""
        from fronts.desktop.app import _splash_min_remaining
        self.assertEqual(_splash_min_remaining(800, 800), 0)
        self.assertEqual(_splash_min_remaining(5000, 800), 0)


class SplashAnimGateTests(_QtBase):
    """Контракт splash (медальйон-breakout, рецензія №2): при УВІМКНЕНОМУ русі
    медальйон грає ЗАВЖДИ (QMovie), і на звичайному запуску теж — «статичною
    емблемою» він стає лише в reduce-motion. Одноразові додатки поверх руху йдуть
    ЗА ПОДІЄЮ: привітальне «прокидання» жука (перший запуск, greet=True) і лінія
    прогресу (затяжний старт, гейт). Вимкнені анімації → абсолютна статика."""

    def _splash(self, greet=False):
        from fronts.desktop.splash import SplashScreen
        return SplashScreen(greet=greet)

    def test_default_animates_medallion_without_greeting(self):
        """Звичайний запуск (greet=False) при увімкненому русі: медальйон — живий
        QMovie, що ГРАЄ; одноразового привітання нема, лінія прогресу схована,
        гейт зведений. «Статики» тут немає — жук усе одно циклиться."""
        from fronts.desktop import motion
        from PySide6.QtGui import QMovie
        motion.init_config(SimpleNamespace(animations=True))
        with mock.patch.object(motion, "_system_animations_ok", return_value=True):
            s = self._splash(greet=False)
            try:
                self.assertFalse(s._greet)
                self.assertIsNotNone(s._movie)                 # медальйон анімований
                self.assertEqual(s._movie.state(), QMovie.MovieState.Running)  # і грає
                self.assertIsNone(s._static_pm)                # статичний кадр не потрібен
                self.assertEqual(s._beetle_reveal, 1.0)        # жук на місці (без «прокидання»)
                self.assertFalse(s._progress_shown)            # лінії ще нема
                self.assertIsNone(s._wake_anim)                # без одноразового привітання
                self.assertIsNotNone(s._gate_timer)            # гейт лінії зведено
            finally:
                s._stop_motion()
                s.close()

    def test_first_run_greeting_wakes_beetle(self):
        """Перший запуск (greet=True) + рух → жук «прокидається» (reveal < 1)."""
        from fronts.desktop import motion
        motion.init_config(SimpleNamespace(animations=True))
        with mock.patch.object(motion, "_system_animations_ok", return_value=True):
            s = self._splash(greet=True)
            try:
                self.assertTrue(s._greet)
                self.assertIsNotNone(s._wake_anim)        # жива поява жука
                self.assertLess(s._beetle_reveal, 1.0)    # ще прокидається
            finally:
                s._stop_motion()
                s.close()

    def test_progress_line_reveals_only_on_gate(self):
        """Лінія прогресу з'являється лише коли зведений гейт спрацював."""
        from fronts.desktop import motion
        motion.init_config(SimpleNamespace(animations=True))
        with mock.patch.object(motion, "_system_animations_ok", return_value=True):
            s = self._splash(greet=False)
            try:
                self.assertFalse(s._progress_shown)
                s._reveal_progress()                      # імітуємо спрацювання гейта
                self.assertTrue(s._progress_shown)
                self.assertIsNotNone(s._progress_anim)
            finally:
                s._stop_motion()
                s.close()

    def test_static_when_disabled(self):
        """Рух вимкнено → жодних анімацій/таймерів навіть на першому запуску."""
        from fronts.desktop import motion
        motion.init_config(SimpleNamespace(animations=False))
        s = self._splash(greet=True)
        try:
            self.assertFalse(s._greet)                    # гейт руху скасував привітання
            self.assertIsNone(s._movie)                   # QMovie не створюється зовсім
            self.assertIsNotNone(s._static_pm)            # лише статичний перший кадр
            self.assertEqual(s._beetle_reveal, 1.0)       # статичний повний кадр
            self.assertFalse(s._progress_shown)
            self.assertIsNone(s._wake_anim)
            self.assertIsNone(s._progress_anim)
            self.assertIsNone(s._gate_timer)              # гейт не зводиться
        finally:
            s.close()


class SplashColorPersonalizationTests(_QtBase):
    """Рішення Миколи 25.07 (аудит мономи theme.py): картка/рамка/смужка
    прогресу сплеша перефарбовуються під активний колір інтерфейсу (усі
    пресети, не лише 'red'). Жук-медальйон — КАНОНІЧНИЙ бренд-маскот
    (balachky-mascot-canon), персоналізація його НЕ перефарбовує; єдиний
    виняток — 'red' (вимога сумісності з нічним баченням, не естетика)."""

    def setUp(self):
        from fronts.desktop import motion
        motion.init_config(SimpleNamespace(animations=True))
        self._patcher = mock.patch.object(motion, "_system_animations_ok", return_value=True)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        from fronts.desktop import theme
        theme.set_ui_color("classic")     # не протікати активний колір між тестами

    def _splash(self, greet=False):
        from fronts.desktop.splash import SplashScreen
        return SplashScreen(greet=greet)

    def test_classic_stays_original_card_and_animated_mascot(self):
        from fronts.desktop import theme, splash
        theme.set_ui_color("classic")
        s = self._splash(greet=False)
        try:
            self.assertIsNotNone(s._movie)                # жук живий, оригінальний
            self.assertIsNone(s._static_pm)
            self.assertEqual(splash._card_bg(), "#221D12")
            self.assertEqual(splash._card_border(), "#4A442F")
            self.assertEqual(splash._track_color(), "#3B3526")
        finally:
            s._stop_motion()
            s.close()

    def test_red_recolors_card_and_mascot_byte_identical_to_pre_25_07(self):
        from fronts.desktop import theme, splash
        theme.set_ui_color("red")
        s = self._splash(greet=False)
        try:
            self.assertIsNone(s._movie)                   # нічне бачення: статичний тінт
            self.assertIsNotNone(s._static_pm)
            self.assertEqual(splash._card_bg(), "#1A0606")
            self.assertEqual(splash._card_border(), "#5A2626")
            self.assertEqual(splash._track_color(), "#2A1010")
        finally:
            s._stop_motion()
            s.close()

    def test_teal_recolors_card_but_leaves_mascot_untouched(self):
        from fronts.desktop import theme, splash
        theme.set_ui_color("teal")
        s = self._splash(greet=False)
        try:
            self.assertIsNotNone(s._movie)                 # бренд-маскот лишається живим
            self.assertIsNone(s._static_pm)
            bg = splash._card_bg()
            self.assertNotEqual(bg, "#221D12")             # картка ЗМІНИЛАСЬ під колір...
            self.assertNotEqual(bg, "#1A0606")              # ...і це не еталонний червоний
        finally:
            s._stop_motion()
            s.close()


class HueTintTests(_QtBase):
    """Пряма перевірка _hue_tint (обходить QMovie/static-гілку _build_medallion,
    щоб гарантовано зловити регрес у самій формулі перефарбування пікселя)."""

    def test_non_red_hue_leaves_pixel_unchanged(self):
        from fronts.desktop import splash
        from PySide6.QtGui import QPixmap, QColor
        pm = QPixmap(2, 2)
        pm.fill(QColor(30, 60, 200, 255))
        for hue in (None, 178):
            out = splash._hue_tint(pm, hue)
            c = out.toImage().pixelColor(0, 0)
            self.assertEqual((c.red(), c.green(), c.blue(), c.alpha()), (30, 60, 200, 255))

    def test_red_hue_recolors_pixel_to_monochrome_red(self):
        from fronts.desktop import splash
        from PySide6.QtGui import QPixmap, QColor
        pm = QPixmap(2, 2)
        pm.fill(QColor(30, 60, 200, 255))
        out = splash._hue_tint(pm, 0)
        c = out.toImage().pixelColor(0, 0)
        self.assertNotEqual((c.red(), c.green(), c.blue()), (30, 60, 200))
        self.assertGreater(c.red(), c.green())
        self.assertGreater(c.red(), c.blue())


if __name__ == "__main__":
    unittest.main()
