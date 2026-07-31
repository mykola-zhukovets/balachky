"""Offscreen-рендер splash «Жук-медальйон» (компактна картка + анімований
медальйон-breakout) + смоук потоку.

ОКРЕМИЙ процес (як render_meeting_smoke.py): рендер живих QWidget із таймерами/
анімаціями у спільному процесі з рештою ~410 тестів дає флакі-краш на виході
offscreen-Qt. Тож файл НЕ підхоплюється `unittest discover` (патерн test*.py).

Прогін:
    python -m unittest tests.render_splash_smoke
    python tests/render_splash_smoke.py

Скріншоти кадрів — у %TEMP%/balachky-diag/splash/.

Кадри рендеримо детерміновано: замість реального таймера прямо виставляємо
атрибути фази (_beetle_reveal / _progress / _line_alpha / _fade) і update().
"""
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tempfile
# Діагностичні скріни — у temp, не на Desktop (канон: не смітити робочий стіл)
_DIAG = Path(tempfile.gettempdir()) / "balachky-diag" / "splash"


class SplashBrandLayoutTests(unittest.TestCase):
    def tearDown(self):
        from fronts.desktop.i18n import set_language
        set_language("uk")

    def _layout(self, language):
        from fronts.desktop import splash
        from fronts.desktop.i18n import set_language, tr

        self.assertTrue(
            hasattr(splash, "_brand_layout"),
            "splash має надавати чисту логіку розкладки бренду",
        )
        set_language(language)
        return splash._brand_layout(tr("app_title"))

    def test_english_brand_has_one_centered_row(self):
        self.assertEqual(
            self._layout("en"),
            (("Balachky", 225, 28),),
        )

    def test_ukrainian_brand_keeps_two_rows(self):
        self.assertEqual(
            self._layout("uk"),
            (
                ("Балачки", 214, 28),
                ("у Коростені", 246, 18),
            ),
        )


class SplashRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop.theme import QSS, load_fonts
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()                         # як у main(): ДО QSS
        cls._app.setStyleSheet(QSS)
        _DIAG.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._flush(cls._app)

    @staticmethod
    def _flush(app):
        from PySide6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    def setUp(self):
        self._live = []

    def tearDown(self):
        for s in self._live:
            try:
                s._stop_motion()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
            s.deleteLater()
        self._live = []
        self._flush(self._app)

    def _splash(self, animated=True, greet=False):
        from fronts.desktop import motion
        from fronts.desktop.splash import SplashScreen
        motion.init_config(SimpleNamespace(animations=animated))
        with mock.patch.object(motion, "_system_animations_ok",
                               return_value=animated):
            s = SplashScreen(greet=greet)
        self._live.append(s)
        return s

    def _medallion_render(self, splash):
        """Композит grab-а splash на ТЕМНУ підкладку + кількість «непорожніх»
        пікселів у зоні медальйона (над карткою — там лишень жук; порожнеча була б
        прозорою). Прозорий grab (стара сліпа зона) → 0 пікселів, тест впаде."""
        from PySide6.QtGui import QImage, QPainter, QColor
        from fronts.desktop.splash import _W, _CARD_TOP
        grab = splash.grab()
        self.assertFalse(grab.isNull())
        img = QImage(grab.size(), QImage.Format_RGB32)     # RGB32 = без альфи, темна
        img.fill(QColor(12, 12, 14))
        p = QPainter(img)
        p.drawPixmap(0, 0, grab)                           # накладаємо grab поверх
        p.end()
        scale = img.width() / _W                            # логічні → пікселі (DPI)
        mr = splash._medallion_rect()
        left, right = int((mr.left() + 12) * scale), int((mr.right() - 12) * scale)
        top, bottom = int(12 * scale), int((_CARD_TOP - 6) * scale)   # лише навіс над карткою
        xstep = max(1, (right - left) // 40)
        ystep = max(1, (bottom - top) // 40)
        content = 0
        for y in range(top, min(bottom, img.height()), ystep):
            for x in range(left, min(right, img.width()), xstep):
                c = img.pixelColor(x, y)
                if abs(c.red() - 12) + abs(c.green() - 12) + abs(c.blue() - 14) > 24:
                    content += 1
        return img, content

    def _save(self, splash, name):
        img, content = self._medallion_render(splash)
        # НЕ «сліпий» grab: у зоні медальйона реально намальований жук
        self.assertGreater(content, 4, f"{name}: зона медальйона порожня (сліпий grab)")
        img.save(str(_DIAG / name))

    def test_render_default_animated_emblem(self):
        """Звичайний запуск при увімкненому русі: медальйон АНІМОВАНИЙ (жук грає),
        жук на місці без одноразового «прокидання», лінії прогресу ще нема."""
        s = self._splash(animated=True, greet=False)
        self.assertIsNotNone(s._movie)               # не статика — живий QMovie
        self.assertEqual(s._beetle_reveal, 1.0)
        self.assertFalse(s._progress_shown)
        self._save(s, "01-default-emblem.png")

    def test_medallion_frames_differ_over_animation(self):
        """Медальйон СПРАВДІ грає: два кадри з інтервалом дають РІЗНИЙ рендер зони
        медальйона (сліпа зона / статичний кадр цього не пройшли б)."""
        s = self._splash(animated=True, greet=False)
        mv = s._movie
        self.assertIsNotNone(mv)
        self.assertGreater(mv.frameCount(), 1)
        mv.jumpToFrame(0)
        self._app.processEvents()
        img_a, ca = self._medallion_render(s)
        mv.jumpToFrame(mv.frameCount() // 2)
        self._app.processEvents()
        img_b, cb = self._medallion_render(s)
        self.assertGreater(ca, 4)                    # обидва кадри мають жука
        self.assertGreater(cb, 4)
        self.assertNotEqual(img_a, img_b)            # РІЗНІ кадри → медальйон живий
        img_b.save(str(_DIAG / "08-frame-b.png"))

    def test_render_first_run_greeting(self):
        """Перший запуск: жук у процесі «прокидання» (mid) і на місці (end)."""
        s = self._splash(animated=True, greet=True)
        self.assertTrue(s._greet)
        s._on_wake(0.4)                      # середина появи
        self._save(s, "02-greeting-mid.png")
        s._on_wake(1.0)                      # жук на місці
        self._save(s, "03-greeting-end.png")

    def test_render_progress_line(self):
        """Затяжний старт: тонка золота лінія прогресу під статусом."""
        s = self._splash(animated=True, greet=False)
        s._reveal_progress()
        s._on_creep(0.5)                     # заповнення ~50%
        self.assertTrue(s._progress_shown)
        self._save(s, "04-progress-line.png")

    def test_render_final_fade(self):
        """Фінальне затихання: напівпрозорий кадр заставки."""
        s = self._splash(animated=True, greet=False)
        s._fade = 0.5
        s.update()
        self._save(s, "05-final-fade.png")

    def test_render_static_anim_off(self):
        """Гейт руху: анімації вимкнено → статичний повний кадр, без таймерів."""
        s = self._splash(animated=False, greet=True)
        self.assertFalse(s._greet)
        self.assertIsNone(s._gate_timer)
        self.assertEqual(s._beetle_reveal, 1.0)
        self._save(s, "06-static-anim-off.png")

    def test_medallion_movie_animates(self):
        """Рух увімкнено: медальйон — живий QMovie з >1 кадром (жук справді грає),
        поточний кадр рендериться (не порожній), статичний кадр не потрібен."""
        s = self._splash(animated=True, greet=False)
        self.assertIsNotNone(s._movie)
        self.assertTrue(s._movie.isValid())
        self.assertGreater(s._movie.frameCount(), 1)
        self.assertIsNone(s._static_pm)
        self.assertFalse(s._current_medallion().isNull())
        self._save(s, "07-medallion-frame.png")

    def test_medallion_static_when_motion_off(self):
        """Reduce-motion: медальйон — статичний перший кадр, QMovie не створюється."""
        s = self._splash(animated=False, greet=False)
        self.assertIsNone(s._movie)
        self.assertIsNotNone(s._static_pm)
        self.assertFalse(s._static_pm.isNull())
        self.assertFalse(s._current_medallion().isNull())


class SplashThreadSmokeTests(unittest.TestCase):
    """Живий (offscreen) смоук: рушій у _EngineLoadThread НЕ морозить GUI-петлю,
    а splash.finish_to коректно показує вікно без винятків. Engine замокано —
    3 ГБ у тест не тягнемо."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_threaded_load_keeps_event_loop_alive(self):
        import fronts.desktop.app as appmod
        from PySide6.QtCore import QEventLoop, QTimer
        sentinel = object()

        def slow_engine(cfg):
            time.sleep(0.2)                  # імітація повільного завантаження
            return sentinel

        ticks = []
        with mock.patch.object(appmod, "Engine", side_effect=slow_engine):
            box = {}
            loop = QEventLoop()
            t = QTimer()
            t.setInterval(20)
            t.timeout.connect(lambda: ticks.append(1))
            t.start()
            thr = appmod._EngineLoadThread(SimpleNamespace())
            thr.ready.connect(lambda e: (box.__setitem__("e", e), loop.quit()))
            thr.needs_recovery.connect(lambda err: loop.quit())
            thr.start()
            loop.exec()                      # GUI-петля жива, поки потік вантажить
            thr.wait(2000)
            t.stop()
        self.assertIs(box.get("e"), sentinel)
        self.assertGreater(len(ticks), 2)    # петля тікала під час load → не морожено

    def test_finish_to_shows_window(self):
        from PySide6.QtWidgets import QWidget
        from PySide6.QtCore import QEventLoop, QTimer
        from fronts.desktop import motion
        from fronts.desktop.splash import SplashScreen
        motion.init_config(SimpleNamespace(animations=True))
        with mock.patch.object(motion, "_system_animations_ok", return_value=True):
            s = SplashScreen()
            s.show()
            w = QWidget()
            s.finish_to(w)                   # затихання → показати вікно
            loop = QEventLoop()
            QTimer.singleShot(400, loop.quit)   # дочекатись завершення 200мс затихання
            loop.exec()
        self.assertTrue(w.isVisible())
        self.assertIsNone(s._fade_anim)      # затихання завершено, ref знято
        w.close()
        w.deleteLater()
        s.deleteLater()


if __name__ == "__main__":
    suite = unittest.TestSuite()
    load = unittest.TestLoader().loadTestsFromTestCase
    suite.addTests(load(SplashBrandLayoutTests))
    suite.addTests(load(SplashRenderTests))
    suite.addTests(load(SplashThreadSmokeTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
