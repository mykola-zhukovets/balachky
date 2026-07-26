"""Тести бекенду зберігання тригер→текст (whisper_core.snippets) — тепер це
спільна TOML-логіка для голосових макросів (feature/voice-macros). Фіча
«сніпетів» злита в «Макроси»; апплай-логіка й PTT-гейт живуть у test_macros.py."""
import tempfile
import unittest
from pathlib import Path

from whisper_core.snippets import (
    normalize_trigger, load_snippets,
    save_snippets, add_snippet, delete_snippet,
)


class NormalizeTests(unittest.TestCase):
    def test_lowercase(self):
        self.assertEqual(normalize_trigger("Встав ПІДПИС"), "встав підпис")

    def test_trim_and_collapse_spaces(self):
        self.assertEqual(normalize_trigger("  встав   підпис  "), "встав підпис")

    def test_strip_trailing_punctuation(self):
        self.assertEqual(normalize_trigger("встав підпис."), "встав підпис")
        self.assertEqual(normalize_trigger("готово?!"), "готово")
        self.assertEqual(normalize_trigger("список,"), "список")

    def test_cyrillic_preserved(self):
        # кирилиця не губиться й не транслітерується
        self.assertEqual(normalize_trigger("Їжак Ґудзик"), "їжак ґудзик")

    def test_internal_punctuation_kept(self):
        # зрізаємо лише КІНЦЕВУ пунктуацію, не всередині
        self.assertEqual(normalize_trigger("моя.пошта"), "моя.пошта")


class LoadTests(unittest.TestCase):
    def _write(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / "snippets.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_missing_file_is_empty(self):
        self.assertEqual(load_snippets(Path(tempfile.mkdtemp()) / "nope.toml"), {})

    def test_empty_file_is_empty(self):
        self.assertEqual(load_snippets(self._write("")), {})

    def test_keys_are_normalized(self):
        p = self._write('"Встав Підпис" = "текст"\n')
        self.assertEqual(load_snippets(p), {"встав підпис": "текст"})

    def test_broken_toml_is_empty_dict(self):
        # незакрита лапка — битий TOML → порожньо + warning (не крах)
        p = self._write('"тригер = незакрито\n')
        self.assertEqual(load_snippets(p), {})

    def test_non_string_value_skipped(self):
        p = self._write('"добрий" = ["масив", "помилково"]\n"вітаю" = "привіт"\n')
        self.assertEqual(load_snippets(p), {"вітаю": "привіт"})

    def test_multiline_value(self):
        p = self._write('"шапка" = """\nрядок один\nрядок два"""\n')
        self.assertEqual(load_snippets(p), {"шапка": "рядок один\nрядок два"})


class WriteRoundTripTests(unittest.TestCase):
    """Програмний запис переформатовує файл, але значення round-trip'иться."""

    def _path(self):
        return Path(tempfile.mkdtemp()) / "snippets.toml"

    def test_save_load_single_line(self):
        p = self._path()
        save_snippets(p, {"моя пошта": 'a@b.com "x"'})
        self.assertEqual(load_snippets(p), {"моя пошта": 'a@b.com "x"'})

    def test_save_load_multiline(self):
        p = self._path()
        text = "рядок один\nрядок два\nрядок три"
        save_snippets(p, {"шапка": text})
        self.assertEqual(load_snippets(p), {"шапка": text})

    def test_save_load_backslash(self):
        p = self._path()
        save_snippets(p, {"шлях": r"C:\Users\ня"})
        self.assertEqual(load_snippets(p), {"шлях": r"C:\Users\ня"})

    def test_add_normalizes_trigger(self):
        p = self._path()
        add_snippet(p, "Встав Підпис", "текст")
        self.assertEqual(load_snippets(p), {"встав підпис": "текст"})

    def test_add_empty_trigger_is_noop(self):
        p = self._path()
        add_snippet(p, "   ", "текст")
        self.assertEqual(load_snippets(p), {})

    def test_delete_removes(self):
        p = self._path()
        add_snippet(p, "один", "1")
        add_snippet(p, "два", "2")
        self.assertTrue(delete_snippet(p, "Один."))   # м'яка звірка тригера
        self.assertEqual(load_snippets(p), {"два": "2"})

    def test_delete_missing_returns_false(self):
        p = self._path()
        add_snippet(p, "один", "1")
        self.assertFalse(delete_snippet(p, "нема"))


if __name__ == "__main__":
    unittest.main()
