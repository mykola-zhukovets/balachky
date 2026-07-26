"""feature/video-player-mvp — чиста логіка відеоплеєра (без QtMultimedia).

Формат тайм-коду й підписів швидкості перевіряємо без побудови QMediaPlayer/
QVideoWidget, тож ці тести безпечно йдуть у спільному `unittest discover`.
Рендер контролів і обробку відсутнього файлу перевіряє render_video_smoke.py
(окремий процес — живе Qt-вікно).
"""
import unittest

from fronts.desktop.video_player import _VIDEO_SPEEDS, fmt_speed, fmt_time


class VideoSpeedTests(unittest.TestCase):
    def test_speed_cycle_order(self):
        # 1× → 1,25× → 1,5× → 2× → 0,5× → 0,75× → 1× (0,5× — повільний перегляд)
        self.assertEqual(_VIDEO_SPEEDS, (1.0, 1.25, 1.5, 2.0, 0.5, 0.75))

    def test_speed_labels_ukrainian_decimal_comma(self):
        labels = [fmt_speed(s) for s in _VIDEO_SPEEDS]
        self.assertEqual(labels, ["1×", "1,25×", "1,5×", "2×", "0,5×", "0,75×"])


class TimeCodeTests(unittest.TestCase):
    def test_under_a_minute(self):
        self.assertEqual(fmt_time(0), "0:00")
        self.assertEqual(fmt_time(5_000), "0:05")

    def test_minutes_seconds(self):
        self.assertEqual(fmt_time(65_000), "1:05")
        self.assertEqual(fmt_time(599_000), "9:59")

    def test_over_an_hour_shows_hours(self):
        self.assertEqual(fmt_time(3_725_000), "1:02:05")

    def test_negative_clamped(self):
        self.assertEqual(fmt_time(-1), "0:00")


if __name__ == "__main__":
    unittest.main()
