"""Тести відсутності звукових сигналів запису та збереження звуку вставки тексту.

Перевіряють, що:
1. Конвеєри запису (диктування, нотатки, нарада, файли, деталі) не викликають
   програвання сигналів "done"/старт/стоп запису.
2. Функція `sounds.play` або `_play_chime` з "done" не грає жодних гудків.
3. Звук підтвердження вставки тексту (`paste_confirm_sound` / "paste") залишається
   працездатним.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fronts.desktop import sounds
from fronts.desktop.app import _play_chime


class NoRecordingSoundsTests(unittest.TestCase):

    def test_sounds_tones_contains_only_paste(self):
        """Звуки запису вилучені — у _TONES залишається лише підтвердження вставки 'paste'."""
        self.assertIn("paste", sounds._TONES)
        self.assertNotIn("done", sounds._TONES)

    def test_sounds_play_done_does_nothing(self):
        """Виклик sounds.play('done') є бездіяльним (no-op)."""
        with patch("threading.Thread") as mock_thread:
            sounds.play("done")
            mock_thread.assert_not_called()

    def test_sounds_play_paste_launches_thread(self):
        """Виклик sounds.play('paste') запускає потік відтворення."""
        with patch("threading.Thread") as mock_thread:
            mock_inst = MagicMock()
            mock_thread.return_value = mock_inst
            sounds.play("paste")
            mock_thread.assert_called_once()
            mock_inst.start.assert_called_once()

    def test_play_chime_paste_respects_config_and_quiet_hours(self):
        """_play_chime реагує на 'paste' за налаштуваннями `sounds` / `paste_confirm_sound`."""
        cfg = SimpleNamespace(sounds=True, paste_confirm_sound=True, quiet_hours_enabled=False)
        with patch("fronts.desktop.sounds.play") as mock_play:
            _play_chime(cfg, "paste")
            mock_play.assert_called_once_with("paste")

        with patch("fronts.desktop.sounds.play") as mock_play:
            _play_chime(cfg, "done")
            mock_play.assert_not_called()

    def test_app_codebase_has_no_done_chime_calls(self):
        """Шляхом статичного аналізу перевіряємо, що в app.py немає викликів _play_chime з 'done' чи sounds.play('done')."""
        from pathlib import Path
        app_path = Path(__file__).resolve().parent.parent / "fronts" / "desktop" / "app.py"
        content = app_path.read_text(encoding="utf-8")
        self.assertNotIn('sounds.play("done")', content)
        self.assertNotIn('_play_chime(self.cfg, "done")', content)
        self.assertNotIn('_play_chime(app.cfg, "done")', content)


if __name__ == "__main__":
    unittest.main()


class LegacySoundsMigrationTests(unittest.TestCase):
    """sounds=False у старому конфігу глушив УСЕ, включно зі вставкою.
    Після видалення сигналів запису воля «тихо» не має вмикати вставку
    (вердикт рецензії 24.07: регрес для sounds=False & paste_confirm_sound=True)."""

    def _load(self, toml_text):
        import tempfile, os
        from whisper_core.config import Config
        fd, path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(toml_text)
            return Config.load(path)
        finally:
            os.unlink(path)

    def test_legacy_sounds_false_silences_paste(self):
        c = self._load('meeting_encrypt = true\nsounds = false\n'
                       'paste_confirm_sound = true\n')
        self.assertFalse(c.paste_confirm_sound)

    def test_sounds_true_keeps_paste_choice(self):
        c = self._load('meeting_encrypt = true\nsounds = true\n'
                       'paste_confirm_sound = true\n')
        self.assertTrue(c.paste_confirm_sound)

    def test_absent_legacy_key_keeps_default(self):
        c = self._load('meeting_encrypt = true\n')
        self.assertTrue(c.paste_confirm_sound)

    def test_migration_is_one_time(self):
        # після споживання legacy-прапорця він гаситься: користувач, що
        # ЗНОВУ ввімкнув звук вставки в UI, не втрачає вибір на рестарті
        import tempfile, os
        from whisper_core.config import Config
        fd, path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write('meeting_encrypt = true\nsounds = false\n'
                        'paste_confirm_sound = true\n')
            c = Config.load(path)
            self.assertFalse(c.paste_confirm_sound)  # міграція разова
            self.assertTrue(c.sounds)                # прапорець погашено
            c.paste_confirm_sound = True             # явний вибір у UI
            c.save(path)
            c2 = Config.load(path)                   # рестарт
            self.assertTrue(c2.paste_confirm_sound,
                            "рестарт мовчки скинув явний вибір користувача")
        finally:
            os.unlink(path)
