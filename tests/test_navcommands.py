"""feature/office-voice-nav — тести чистої логіки розбору команд голосової
навігації (whisper_core.navcommands): точний-збіг фраз, парсер адреси комірки з
кириличними гомогліфами, адресна команда, користувацькі аліаси, довідка. Без Qt.
Стиль — як test_formfill.py / test_macros.py."""
import tempfile
import unittest
from pathlib import Path

from whisper_core import navcommands


class MatchExactPhraseTests(unittest.TestCase):
    def test_uk_next_prev_field(self):
        self.assertEqual(navcommands.match("наступне поле", "uk"), ("key", "tab"))
        self.assertEqual(navcommands.match("попереднє поле", "uk"),
                         ("key", "shift_tab"))

    def test_uk_soft_normalized(self):
        # регістр і кінцева пунктуація не заважають (як тригери сніпетів)
        self.assertEqual(navcommands.match("Наступне поле.", "uk"), ("key", "tab"))

    def test_uk_cells(self):
        self.assertEqual(navcommands.match("комірка нижче", "uk"), ("key", "down"))
        self.assertEqual(navcommands.match("комірка вище", "uk"), ("key", "up"))
        self.assertEqual(navcommands.match("комірка ліворуч", "uk"),
                         ("key", "left"))
        self.assertEqual(navcommands.match("наступна комірка", "uk"),
                         ("key", "tab"))

    def test_uk_confirm(self):
        self.assertEqual(navcommands.match("підтвердити", "uk"), ("key", "enter"))
        self.assertEqual(navcommands.match("готово", "uk"), ("key", "enter"))

    def test_en(self):
        self.assertEqual(navcommands.match("next field", "en"), ("key", "tab"))
        self.assertEqual(navcommands.match("previous field", "en"),
                         ("key", "shift_tab"))
        self.assertEqual(navcommands.match("cell below", "en"), ("key", "down"))

    def test_partial_phrase_not_a_command(self):
        # безпека: команда лише при точному збігу ВСІЄЇ фрази (як макроси/formfill)
        self.assertIsNone(navcommands.match("а тепер наступне поле форми", "uk"))
        self.assertIsNone(navcommands.match("наступне поле буде складним", "uk"))

    def test_plain_text_none(self):
        self.assertIsNone(navcommands.match("командиру роти", "uk"))

    def test_empty_and_unknown_language(self):
        self.assertIsNone(navcommands.match("", "uk"))
        self.assertIsNone(navcommands.match("наступне поле", "xx"))


class ParseCellAddressTests(unittest.TestCase):
    def test_latin_plain(self):
        self.assertEqual(navcommands.parse_cell_address("B7"), "B7")
        self.assertEqual(navcommands.parse_cell_address("aa12"), "AA12")

    def test_spaces_removed(self):
        self.assertEqual(navcommands.parse_cell_address("б 7"), "B7")

    def test_cyrillic_homoglyphs_mapped(self):
        # кириличні двійники латинських літер стовпців Excel
        self.assertEqual(navcommands.parse_cell_address("Б7"), "B7")
        self.assertEqual(navcommands.parse_cell_address("А1"), "A1")
        self.assertEqual(navcommands.parse_cell_address("С3"), "C3")
        self.assertEqual(navcommands.parse_cell_address("Н9"), "H9")

    def test_invalid(self):
        self.assertIsNone(navcommands.parse_cell_address(""))
        self.assertIsNone(navcommands.parse_cell_address("привіт"))   # лише літери
        self.assertIsNone(navcommands.parse_cell_address("7"))        # лише цифри
        self.assertIsNone(navcommands.parse_cell_address("B0"))       # рядок 0 нема
        self.assertIsNone(navcommands.parse_cell_address("B07"))      # провідний нуль


class CellGotoCommandTests(unittest.TestCase):
    def test_uk_goto(self):
        self.assertEqual(navcommands.match("комірка Б7", "uk"), ("goto", "B7"))
        self.assertEqual(navcommands.match("клітинка A1", "uk"), ("goto", "A1"))

    def test_en_goto(self):
        self.assertEqual(navcommands.match("cell B7", "en"), ("goto", "B7"))

    def test_exact_cell_phrase_wins_over_goto(self):
        # «комірка нижче» — точна команда-клавіша, НЕ спроба адреси
        self.assertEqual(navcommands.match("комірка нижче", "uk"), ("key", "down"))

    def test_goto_with_bad_address_is_none(self):
        self.assertIsNone(navcommands.match("комірка привіт", "uk"))

    def test_bare_prefix_is_text(self):
        self.assertIsNone(navcommands.match("комірка", "uk"))


class AliasesTests(unittest.TestCase):
    def _file(self, text):
        d = Path(tempfile.mkdtemp())
        p = d / "navcommands.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_load_valid_aliases(self):
        p = self._file('[aliases]\n"далі" = "next_field"\n"назад" = "prev_field"\n')
        aliases = navcommands.load_aliases(p)
        self.assertEqual(aliases, {"далі": "next_field", "назад": "prev_field"})

    def test_alias_extends_matching(self):
        aliases = {"далі": "next_field"}
        self.assertEqual(navcommands.match("далі", "uk", aliases), ("key", "tab"))

    def test_alias_overrides_builtin(self):
        # користувацький аліас перекриває вбудовану фразу з тим самим текстом
        aliases = {"наступне поле": "confirm"}
        self.assertEqual(navcommands.match("наступне поле", "uk", aliases),
                         ("key", "enter"))

    def test_unknown_action_id_ignored(self):
        p = self._file('[aliases]\n"хтозна" = "не_існує"\n"далі" = "next_field"\n')
        self.assertEqual(navcommands.load_aliases(p), {"далі": "next_field"})

    def test_missing_file_empty(self):
        self.assertEqual(navcommands.load_aliases(Path("nope.toml")), {})

    def test_broken_toml_empty(self):
        p = self._file("це = не [ валідний toml")
        self.assertEqual(navcommands.load_aliases(p), {})


class ReferenceTests(unittest.TestCase):
    def test_contains_core_commands(self):
        rows = navcommands.command_reference("uk")
        phrases = [p for p, _ in rows]
        self.assertIn("наступне поле", phrases)
        self.assertIn("попереднє поле", phrases)
        # адресна команда присутня як приклад
        self.assertTrue(any("B7" in p for p in phrases))

    def test_effect_keys_are_i18n_keys(self):
        rows = navcommands.command_reference("uk")
        for _phrase, effect_key in rows:
            self.assertTrue(effect_key.startswith("nav_ref_"))

    def test_user_aliases_appended(self):
        rows = navcommands.command_reference("uk", {"давай далі": "next_field"})
        self.assertIn("давай далі", [p for p, _ in rows])


if __name__ == "__main__":
    unittest.main()
