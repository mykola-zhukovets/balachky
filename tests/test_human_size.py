"""Тести канонічної функції human_size: одиниці та роздільники за мовою UI (UK/EN)."""
import unittest

from fronts.desktop.i18n import set_language, human_size


class TestHumanSize(unittest.TestCase):

    def setUp(self):
        set_language("uk")

    def tearDown(self):
        set_language("uk")

    def test_human_size_uk_gb(self):
        set_language("uk")
        bytes_val = int(18.1 * (1024 ** 3))
        res = human_size(bytes_val)
        self.assertEqual(res, "18,1 ГБ")

    def test_human_size_en_gb(self):
        set_language("en")
        bytes_val = int(18.1 * (1024 ** 3))
        res = human_size(bytes_val)
        self.assertEqual(res, "18.1 GB")

    def test_human_size_uk_mb(self):
        set_language("uk")
        bytes_val = 749 * (1024 ** 2)
        res = human_size(bytes_val)
        self.assertEqual(res, "749 МБ")

    def test_human_size_en_mb(self):
        set_language("en")
        bytes_val = 749 * (1024 ** 2)
        res = human_size(bytes_val)
        self.assertEqual(res, "749 MB")

    def test_human_size_mutation_catches_dot_in_uk(self):
        """Мутаційний тест: якщо у мові uk зародиться крапка замість коми (18.1 ГБ),
        тест має впасти."""
        set_language("uk")
        bytes_val = int(18.1 * (1024 ** 3))
        res = human_size(bytes_val)
        # Перевірка, що кома присутня і крапки немає у роздільнику дробової частини
        self.assertIn(",", res)
        self.assertNotIn(".", res)
        with self.assertRaises(AssertionError):
            # Імітація мутації: вимога "18.1 ГБ" при мові uk повертає розбіжність з дійсним "18,1 ГБ"
            self.assertEqual(res, "18.1 ГБ")


if __name__ == "__main__":
    unittest.main()
