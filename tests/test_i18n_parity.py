"""Парність словників UK/EN у i18n.

Кожен видимий рядок мусить бути в обох мовах (канон розробки §1: i18n парність
uk+en). Ловить забутий переклад і розсинхрон плейсхолдерів {name}, через які
`tr(..., name=...)` мовчки віддав би сирий рядок однією з мов.
"""
import re
import unittest

from fronts.desktop.i18n import STRINGS

UK = STRINGS["uk"]
EN = STRINGS["en"]

# Іменовані плейсхолдери {foo}; позиційні {} / {0} не використовуємо в UI-рядках.
_FIELD = re.compile(r"{(\w+)}", re.UNICODE)

# Локалізовані ЛІТЕРАЛЬНІ токени макросів (whisper_core/macros.py приймає обидві
# мови незалежно від UI): {дата}/{date}, {час}/{time}. Це показовий текст, а не
# аргументи tr(...).format() — тому вони законно різняться між uk/en.
_MACRO_LITERALS = frozenset({"дата", "час", "date", "time"})


def _named_fields(value: str) -> set:
    return set(_FIELD.findall(value)) - _MACRO_LITERALS


class Parity(unittest.TestCase):
    def test_same_keys(self):
        only_uk = sorted(set(UK) - set(EN))
        only_en = sorted(set(EN) - set(UK))
        self.assertEqual(only_uk, [], f"ключі лише в UK (нема EN-перекладу): {only_uk}")
        self.assertEqual(only_en, [], f"ключі лише в EN (нема UK-оригіналу): {only_en}")

    def test_named_placeholders_match(self):
        mismatched = {
            k: (sorted(_named_fields(str(UK[k]))), sorted(_named_fields(str(EN[k]))))
            for k in set(UK) & set(EN)
            if _named_fields(str(UK[k])) != _named_fields(str(EN[k]))
        }
        self.assertEqual(mismatched, {}, f"розсинхрон плейсхолдерів uk/en: {mismatched}")

    def test_en_typographic_quotes_only(self):
        # house-style поширюється й на EN: лише “ ”, не «ялинки»/„лапки-низом“.
        bad = "«»„"
        hits = sorted(k for k, v in EN.items()
                      if any(ch in str(v) for ch in bad))
        self.assertEqual(hits, [], f"EN має лише “ ”: {hits}")

    def test_no_empty_values_except_intentional_english_brand_bottom(self):
        empty = sorted(
            (lang, key)
            for lang, strings in STRINGS.items()
            for key, value in strings.items()
            if isinstance(value, str) and not value.strip()
        )
        self.assertEqual(
            empty,
            [("en", "brand_bottom")],
            f"unexpected empty strings: {empty}",
        )


if __name__ == "__main__":
    unittest.main()
