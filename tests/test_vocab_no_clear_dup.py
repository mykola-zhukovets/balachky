"""Регресія: «Очистити історію» не дублюється на сторінці Словників. Дім цієї
дії — сторінка «Історія»; у Словниках лишається лише підказка-перехід."""
import inspect
import unittest

from fronts.desktop.pages import vocab
from fronts.desktop.i18n import STRINGS


class VocabNoClearDuplicateTests(unittest.TestCase):
    def test_no_reset_memory_action_on_vocab(self):
        # метод-обробник кнопки очищення прибрано разом із кнопкою
        self.assertFalse(hasattr(vocab.VocabPage, "_reset_memory"))
        src = inspect.getsource(vocab)
        # жодної кнопки «Очистити історію…» (common_clear_mem) на Словниках
        self.assertNotIn("common_clear_mem", src)
        # натомість є підказка-перехід на сторінку Історія
        self.assertIn("vocab_clear_hint", src)

    def test_clear_hint_present_both_languages(self):
        for lang in ("uk", "en"):
            self.assertIn("vocab_clear_hint", STRINGS[lang])


if __name__ == "__main__":
    unittest.main()
