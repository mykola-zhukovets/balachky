import unittest
from fronts.desktop.audio_editor import mark_x_position

class WaveformMarksTests(unittest.TestCase):
    def test_mark_x_position(self):
        # 50% of the total time should be at 50% of the width
        self.assertEqual(mark_x_position(500, 1000, 100), 50)
        
        # 25%
        self.assertEqual(mark_x_position(250, 1000, 100), 25)
        
        # 0 total time should return 0 (safe fallback)
        self.assertEqual(mark_x_position(500, 0, 100), 0)
        
        # past end should be capped at width
        self.assertEqual(mark_x_position(1500, 1000, 100), 100)
        
        # negative time should be capped at 0
        self.assertEqual(mark_x_position(-100, 1000, 100), 0)

if __name__ == "__main__":
    unittest.main()
