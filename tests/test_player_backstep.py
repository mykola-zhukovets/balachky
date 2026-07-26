"""Авто-відкат плеєра після паузи: при відновленні відмотати на N секунд назад."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class ResumeBackstepTests(unittest.TestCase):
    def test_position_rewound_by_backstep(self):
        from fronts.desktop.player import resume_position_ms
        self.assertEqual(resume_position_ms(5000, 1500), 3500)

    def test_never_below_zero(self):
        from fronts.desktop.player import resume_position_ms
        self.assertEqual(resume_position_ms(800, 1500), 0)

    def test_zero_backstep_keeps_position(self):
        from fronts.desktop.player import resume_position_ms
        self.assertEqual(resume_position_ms(5000, 0), 5000)

    def test_setter_updates_module_value(self):
        from fronts.desktop import player
        old = player._resume_backstep_ms
        try:
            player.set_resume_backstep_ms(500)
            self.assertEqual(player._resume_backstep_ms, 500)
            player.set_resume_backstep_ms(-10)   # ≥0
            self.assertEqual(player._resume_backstep_ms, 0)
        finally:
            player._resume_backstep_ms = old


class BackstepConfigTests(unittest.TestCase):
    def test_default_is_1_5s(self):
        from whisper_core.config import Config
        self.assertEqual(Config().player_resume_backstep_s, 1.5)

    def test_round_trip(self):
        import tempfile
        from pathlib import Path
        from whisper_core.config import Config
        c = Config()
        c.player_resume_backstep_s = 3.0
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            c.save(p)
            c2 = Config.load(p)
        self.assertEqual(c2.player_resume_backstep_s, 3.0)


if __name__ == "__main__":
    unittest.main()
