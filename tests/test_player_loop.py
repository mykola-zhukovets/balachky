import unittest
from fronts.desktop.audio_editor import should_loop_seek

class LoopPlaybackTests(unittest.TestCase):
    def test_should_loop_seek(self):
        # loop active, pos inside range -> False
        self.assertFalse(should_loop_seek(1500, 1000, 2000, True))
        
        # loop active, pos at or past end -> True
        self.assertTrue(should_loop_seek(2000, 1000, 2000, True))
        self.assertTrue(should_loop_seek(2100, 1000, 2000, True))
        
        # loop not active, pos at or past end -> False
        self.assertFalse(should_loop_seek(2000, 1000, 2000, False))
        
        # loop active, but pos before start -> False
        self.assertFalse(should_loop_seek(500, 1000, 2000, True))

if __name__ == "__main__":
    unittest.main()
