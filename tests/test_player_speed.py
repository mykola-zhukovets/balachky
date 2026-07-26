"""feature/player-pack — варіативна швидкість плеєра зі станом на сесію.

Цикл кнопки: 1× → 1,25× → 1,5× → 2× → 0,75× → 1×. Обрана швидкість тримається
на сесію: новий InlinePlayer стартує з тією ж швидкістю. Qt offscreen; без
QtMultimedia — skip.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class SpeedLabelTests(unittest.TestCase):
    """Чисті перевірки, що не потребують QtMultimedia."""

    def test_speed_cycle_order(self):
        from fronts.desktop import player
        self.assertEqual(player._SPEEDS, (1.0, 1.25, 1.5, 2.0, 0.75))


class InlinePlayerSpeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            import PySide6.QtMultimedia  # noqa: F401  — перевірка наявності
        except Exception:                                  # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from fronts.desktop import player
        player._session_speed_idx = 0                      # ізолювати сесійний стан

    def tearDown(self):
        from fronts.desktop import player
        player._session_speed_idx = 0

    def test_labels_ukrainian_decimal_comma(self):
        from fronts.desktop.player import InlinePlayer, _SPEEDS
        p = InlinePlayer()
        labels = []
        for _ in _SPEEDS:
            labels.append(p._speed_label())
            p._cycle_speed()
        self.assertEqual(labels, ["1×", "1,25×", "1,5×", "2×", "0,75×"])
        p.deleteLater()

    def test_cycle_sets_playback_rate(self):
        from fronts.desktop.player import InlinePlayer
        p = InlinePlayer()
        self.assertEqual(p._player.playbackRate(), 1.0)
        p._cycle_speed()                                   # → 1,25×
        self.assertEqual(p._player.playbackRate(), 1.25)
        p._cycle_speed()                                   # → 1,5×
        self.assertEqual(p._player.playbackRate(), 1.5)
        p.deleteLater()

    def test_wraps_back_to_one(self):
        from fronts.desktop.player import InlinePlayer, _SPEEDS
        p = InlinePlayer()
        for _ in _SPEEDS:                                  # повний оберт
            p._cycle_speed()
        self.assertEqual(p._player.playbackRate(), 1.0)
        self.assertEqual(p._speed_label(), "1×")
        p.deleteLater()

    def test_speed_persists_across_players_in_session(self):
        from fronts.desktop.player import InlinePlayer
        p1 = InlinePlayer()
        p1._cycle_speed()                                  # 1× → 1,25×
        p1._cycle_speed()                                  # → 1,5×
        p2 = InlinePlayer()                                # новий плеєр цієї ж сесії
        self.assertEqual(p2._player.playbackRate(), 1.5)
        self.assertEqual(p2._speed_label(), "1,5×")
        p1.deleteLater()
        p2.deleteLater()


if __name__ == "__main__":
    unittest.main()
