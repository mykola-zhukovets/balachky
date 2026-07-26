import unittest

from whisper_core import pill_pos


class PillPosTests(unittest.TestCase):
    _SCREENS = [(0, 0, 1920, 1080), (1920, 0, 1280, 1024)]  # два монітори
    _DEFAULT = (860, 980)

    def test_saved_within_screen_is_kept(self):
        pos = pill_pos.resolved_position((2000, 500), self._SCREENS, self._DEFAULT)
        self.assertEqual(pos, (2000, 500))

    def test_saved_off_all_screens_falls_back_to_default(self):
        # монітор №2 від'єднали → координата (2500, 500) більше не видима
        pos = pill_pos.resolved_position((2500, 500), [(0, 0, 1920, 1080)],
                                         self._DEFAULT)
        self.assertEqual(pos, self._DEFAULT)

    def test_none_saved_uses_default(self):
        self.assertEqual(
            pill_pos.resolved_position(None, self._SCREENS, self._DEFAULT),
            self._DEFAULT)
        self.assertEqual(
            pill_pos.resolved_position((None, None), self._SCREENS, self._DEFAULT),
            self._DEFAULT)

    def test_visibility_boundary(self):
        self.assertTrue(pill_pos.is_visible(0, 0, self._SCREENS))
        self.assertFalse(pill_pos.is_visible(-1, 0, self._SCREENS))


if __name__ == "__main__":
    unittest.main()
