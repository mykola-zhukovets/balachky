"""Кнопка «Ліцензії третіх сторін» у «Про програму»: файл ліцензій резолвиться
поруч із програмою (paths.bundled_doc), а підписи є в обох мовах."""
import unittest

from whisper_core import paths
from fronts.desktop.i18n import STRINGS


class ThirdPartyNoticesTests(unittest.TestCase):
    def test_notices_file_resolvable(self):
        p = paths.bundled_doc("THIRD-PARTY-NOTICES.txt")
        self.assertIsNotNone(p, "THIRD-PARTY-NOTICES.txt має резолвитись у dev/збірці")
        self.assertTrue(p.exists())

    def test_button_labels_both_languages(self):
        for lang in ("uk", "en"):
            self.assertIn("set_third_party_btn", STRINGS[lang])
            self.assertIn("set_third_party_missing", STRINGS[lang])


if __name__ == "__main__":
    unittest.main()
