"""FIX 4: load_corrector не має валити диктування, якщо словник пошкоджений.

available() перевіряє лише наявність пакета + непорожній файл. Але сам файл
може бути пошкоджений так, що SymSpell().load_dictionary кидає виняток уже під
час читання. Раніше import symspellpy і SymSpell() будувались ПОЗА try, а
load ловив тільки OSError — інші винятки валили весь конвеєр диктування.
Тепер будь-який збій побудови коректора → тихо None (фолбек на сирий текст).
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_core import autocorrect


class LoadCorrectorResilienceTests(unittest.TestCase):
    def _fake_symspellpy(self, *, load_side_effect=None, ctor_side_effect=None):
        """Підставний модуль symspellpy з керованою поведінкою SymSpell."""
        module = types.ModuleType("symspellpy")

        class FakeSymSpell:
            def __init__(self, *a, **k):
                if ctor_side_effect is not None:
                    raise ctor_side_effect

            def load_dictionary(self, *a, **k):
                if load_side_effect is not None:
                    raise load_side_effect
                return True

        module.SymSpell = FakeSymSpell
        module.Verbosity = types.SimpleNamespace(CLOSEST=1)
        return module

    def test_load_raises_non_oserror_returns_none_not_crash(self):
        """load_dictionary кидає RuntimeError (пошкоджений вміст) → None, не виняток."""
        fake = self._fake_symspellpy(load_side_effect=RuntimeError("пошкоджено"))
        with patch.object(autocorrect, "symspell_available", return_value=True), \
             patch.dict(sys.modules, {"symspellpy": fake}):
            with tempfile.TemporaryDirectory() as tmp:
                dic = Path(tmp) / "freq.txt"
                dic.write_text("слово 1\n", encoding="utf-8")
                # НЕ має піднятись — тихий фолбек
                self.assertIsNone(autocorrect.load_corrector(dic))

    def test_symspell_ctor_failure_returns_none(self):
        """Збій конструктора SymSpell (раніше ПОЗА try) → None, не виняток."""
        fake = self._fake_symspellpy(ctor_side_effect=MemoryError("нема памʼяті"))
        with patch.object(autocorrect, "symspell_available", return_value=True), \
             patch.dict(sys.modules, {"symspellpy": fake}):
            with tempfile.TemporaryDirectory() as tmp:
                dic = Path(tmp) / "freq.txt"
                dic.write_text("слово 1\n", encoding="utf-8")
                self.assertIsNone(autocorrect.load_corrector(dic))

    def test_valid_dictionary_still_builds_corrector(self):
        """Happy-path не зламано: справний словник → робочий Corrector."""
        fake = self._fake_symspellpy()   # load повертає True
        with patch.object(autocorrect, "symspell_available", return_value=True), \
             patch.dict(sys.modules, {"symspellpy": fake}):
            with tempfile.TemporaryDirectory() as tmp:
                dic = Path(tmp) / "freq.txt"
                dic.write_text("слово 1\n", encoding="utf-8")
                corr = autocorrect.load_corrector(dic)
                self.assertIsInstance(corr, autocorrect.Corrector)


if __name__ == "__main__":
    unittest.main()
