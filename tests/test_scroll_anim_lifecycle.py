"""Регрес краху 21.07: RuntimeError «Internal C++ object (QPropertyAnimation)
already deleted» на КОЖНОМУ додаванні картки диктування (main_window.add_entry →
motion.smooth_scroll_to_end).

Корінь: smooth_scroll_to_end зберігав scrollarea._scroll_anim й стартував із
DeleteWhenStopped; коли анімація добігала, Qt видаляв C++ об'єкт, а Python-
посилання звисало. Наступний виклик діставав мертвий wrapper і old.stop() кидав
RuntimeError. Фікс: обнуляти посилання по finished + shiboken6.isValid перед
зняттям старої анімації + косметичний рубіж (плавний скрол не має валити
застосунок — вставка тексту вже успішна).

Живі QWidget із анімацією-таймером: анімацію доводимо до кінця в кожному кроці
(DeleteWhenStopped самознищує), а teardown знімає будь-яку недобиту — інакше
таймер, живий під час GC, дає флакі-краш на виході (як у test_meeting_ui).
"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import (
    QAbstractAnimation, QCoreApplication, QEvent, QPropertyAnimation,
)
from PySide6.QtWidgets import (
    QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from fronts.desktop import motion


def _app():
    return QApplication.instance() or QApplication([])


class ScrollAnimLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        # детермінізм: не залежати від системного перемикача анімацій машини
        motion.init_config(SimpleNamespace(animations=True))
        motion._system_ok = True
        self.area = QScrollArea()
        host = QWidget()
        lay = QVBoxLayout(host)
        for i in range(60):
            lay.addWidget(QLabel(f"рядок {i}"))
        self.area.setWidget(host)
        self.area.setWidgetResizable(True)
        self.area.resize(200, 150)
        self.area.show()
        self.app.processEvents()

    def tearDown(self):
        old = getattr(self.area, "_scroll_anim", None)
        if old is not None and shiboken6.isValid(old):
            old.stop()
        self.area.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def _finish_pending_anim(self):
        """Прогнати заплановану через singleShot(0) анімацію й довести її до
        кінця, щоб спрацював DeleteWhenStopped (Qt видаляє C++ об'єкт)."""
        self.app.processEvents()                 # виконати singleShot _run
        anim = getattr(self.area, "_scroll_anim", None)
        if anim is not None and shiboken6.isValid(anim):
            anim.setCurrentTime(anim.duration())  # добігти → finished → delete
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_ref_cleared_after_animation_finishes(self):
        """По finished посилання _scroll_anim обнуляється — не лишається
        звисаючого wrapper на видалений C++ об'єкт (корінь бага)."""
        sb = self.area.verticalScrollBar()
        self.assertGreater(sb.maximum(), 0, "контент має бути прокручуваним")
        motion.smooth_scroll_to_end(self.area)
        self._finish_pending_anim()
        self.assertIsNone(getattr(self.area, "_scroll_anim", None))

    def test_repeated_scroll_never_crashes(self):
        """Сценарій живого тесту: кожне додавання картки кличе
        smooth_scroll_to_end, а попередня анімація вже добігла й Qt її видалив.
        Друге+ спрацювання не сміє впасти на old.stop() (мертвий об'єкт)."""
        for _ in range(4):
            motion.smooth_scroll_to_end(self.area)
            self._finish_pending_anim()

    def test_burst_calls_leave_single_running_animation(self):
        """Репро швидкої серії диктувань: кілька викликів за ОДНУ ітерацію event
        loop, потім ще один поверх ЖИВОЇ анімації. Не сміє впасти, а скролбар
        має ганяти лише ОДНА анімація. Інваріант тримається на двох речах:
        Qt сам зупиняє попередню QPropertyAnimation тієї ж властивості того ж
        об'єкта (same-property dedup у updateState), а фікс 21.07 (обнулення по
        finished + isValid) гарантує, що зупинена/видалена стара не лишає
        звисаючого посилання. Тест ловить регрес будь-якої з половинок —
        напр., якщо анімацію створять на інший target або знімуть _done."""
        sb = self.area.verticalScrollBar()
        for _ in range(5):                       # burst: без processEvents між
            motion.smooth_scroll_to_end(self.area)
        self.app.processEvents()                 # усі _run виконуються поспіль

        anim = getattr(self.area, "_scroll_anim", None)
        self.assertIsNotNone(anim)
        self.assertTrue(shiboken6.isValid(anim))
        anim.setCurrentTime(anim.duration() // 2)   # добігла до середини
        motion.smooth_scroll_to_end(self.area)      # переривання на льоту
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

        running = [a for a in sb.findChildren(QPropertyAnimation)
                   if shiboken6.isValid(a)
                   and a.state() == QAbstractAnimation.Running]
        self.assertEqual(len(running), 1,
                         f"скролбар ганяють {len(running)} анімацій — старі не зупинені")
        self._finish_pending_anim()              # добити хвіст перед teardown

    def test_stop_survives_dead_previous_animation(self):
        """Прямий регрес: у _scroll_anim лежить УЖЕ видалений об'єкт (як після
        DeleteWhenStopped). Наступний виклик не сміє кинути RuntimeError."""
        sb = self.area.verticalScrollBar()
        dead = QPropertyAnimation(sb, b"value", sb)
        dead.setDuration(200)
        dead.setStartValue(0)
        dead.setEndValue(sb.maximum())
        dead.start(QAbstractAnimation.DeleteWhenStopped)
        dead.setCurrentTime(dead.duration())     # добігти → самознищення
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.assertFalse(shiboken6.isValid(dead), "передумова: об'єкт мертвий")
        self.area._scroll_anim = dead            # звисаюче посилання
        motion.smooth_scroll_to_end(self.area)   # НЕ повинно кинути RuntimeError
        self._finish_pending_anim()


if __name__ == "__main__":
    unittest.main()
