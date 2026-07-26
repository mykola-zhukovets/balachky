"""У дистрибутив не потрапляють російські словники pymorphy3.

Знахідка 25.07: `pymorphy3` тягне `pymorphy3_dicts_ru` як транзитивну залежність,
і збирач клав її в дистрибутив — 16 МБ даних, яких програма ніколи не читає
(розбір відмінків кличеться лише з lang="uk", lexicon.py:139).

ВАЖЛИВИЙ УРОК ЦЬОГО ФАЙЛА. Перша версія тесту перевіряла лише наявність назви в
`excludes` — і була ЗЕЛЕНОЮ, тоді як після справжньої збірки 16 МБ усе одно
лежали в `dist`. Тобто тест підтверджував намір, а не результат: `excludes`
блокує ІМПОРТ модуля, але дані пакета приходять власним hook-ом pymorphy3 уже
всередині Analysis. Тому тепер перевіряємо саме фільтр ПІСЛЯ Analysis, а якщо
поруч лежить готова збірка — ще й її вміст.
"""
import unittest
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


class BuildExcludesRuDictsTests(unittest.TestCase):
    def setUp(self):
        self.spec = (_ROOT / "balachky.spec").read_text(encoding="utf-8")

    def test_datas_filtered_after_analysis(self):
        """Дані російських словників викидаються з a.datas — саме там вони й з'являлись."""
        self.assertIn("a.datas = [entry for entry in a.datas", self.spec)
        self.assertIn("pymorphy3_dicts_ru", self.spec)
        # фільтр мусить стояти ДО пакування, інакше він ні на що не впливає
        cut = self.spec.index("a.datas = [entry for entry in a.datas")
        self.assertLess(cut, self.spec.index("pyz = PYZ(a.pure)"),
                        "фільтр мусить виконуватись до PYZ, інакше він марний")

    def test_pure_python_filtered_too(self):
        """Сам модуль словників теж не потрібен — інакше він лишиться в архіві."""
        self.assertIn("a.pure = [entry for entry in a.pure", self.spec)

    def test_uk_dicts_still_collected(self):
        """Українські словники, натомість, збираються явно — без них розбір мертвий."""
        self.assertIn('collect_data_files("pymorphy3_dicts_uk")', self.spec)
        self.assertIn('"pymorphy3_dicts_uk"', self.spec)

    def test_built_dist_has_no_ru_dicts(self):
        """Якщо збірка вже є поруч — у ній не мусить бути російських словників.

        Це єдина перевірка, що дивиться на РЕЗУЛЬТАТ, а не на намір. Коли dist
        відсутній (звичайний прогін на чистій машині) — тест пропускається.
        """
        dist = _ROOT / "dist" / "Balachky" / "_internal"
        if not dist.is_dir():
            self.skipTest("готової збірки поруч немає — перевіряти нічого")
        found = list(dist.glob("pymorphy3_dicts_ru*"))
        self.assertEqual(found, [],
                         f"у дистрибутиві лежать російські словники: {found}")

    def test_built_dist_has_uk_dicts(self):
        """Дзеркальна перевірка: українські словники в збірці МУСЯТЬ бути."""
        dist = _ROOT / "dist" / "Balachky" / "_internal"
        if not dist.is_dir():
            self.skipTest("готової збірки поруч немає — перевіряти нічого")
        self.assertTrue(list(dist.glob("pymorphy3_dicts_uk*")),
                        "українські словники зникли зі збірки — розбір відмінків мертвий")


if __name__ == "__main__":
    unittest.main()
