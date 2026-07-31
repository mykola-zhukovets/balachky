"""Semantic invariants for the public English product name."""
import unittest
from pathlib import Path

from fronts.desktop.i18n import STRINGS


ROOT = Path(__file__).resolve().parent.parent
STALE_ENGLISH_BRAND = ("Balachky u Korosteni", "u Korosteni", "Chats in Korosten")


class BrandNames(unittest.TestCase):
    def test_english_runtime_brand_is_canonical(self):
        en = STRINGS["en"]
        self.assertEqual(en["app_title"], "Balachky")
        self.assertEqual(en["brand_top"], "Balachky")
        self.assertEqual(en["brand_bottom"], "")
        self.assertEqual(
            en["set_about_lead"],
            "“Balachky” turns your voice into text.",
        )
        self.assertEqual(
            en["brand_slogan"],
            "What’s said in Korosten stays in Korosten",
        )
        joined = "\n".join(str(value) for value in en.values())
        for stale in STALE_ENGLISH_BRAND:
            self.assertNotIn(stale, joined)

    def test_ukrainian_runtime_brand_stays_localized(self):
        uk = STRINGS["uk"]
        self.assertEqual(uk["app_title"], "Балачки у Коростені")
        self.assertEqual(uk["brand_top"], "Балачки")
        self.assertEqual(uk["brand_bottom"], "у Коростені")
        self.assertEqual(
            uk["set_about_lead"],
            "“Балачки у Коростені” перетворюють Ваш голос на текст.",
        )

    def test_english_readme_uses_canonical_heading_and_keeps_origin(self):
        readme = (ROOT / "README.en.md").read_text(encoding="utf-8")
        self.assertIn('<h1 align="center">Balachky</h1>', readme)
        self.assertNotIn("Chats in Korosten", readme)
        self.assertIn("Korosten is the town in Ukraine where the app is made", readme)

    def test_installer_localizes_public_product_name(self):
        installer = (ROOT / "installer" / "balachky.iss").read_text(encoding="utf-8-sig")
        required_lines = (
            "AppName={cm:AppDisplayName}",
            "AppVerName={cm:AppDisplayName} {#AppVersion}",
            "ukrainian.AppDisplayName=Балачки у Коростені",
            "english.AppDisplayName=Balachky",
            "english.UninstTitle=Uninstall Balachky",
            'Name: "{autoprograms}\\{cm:AppDisplayName}";',
            'Name: "{autodesktop}\\{cm:AppDisplayName}";',
            'Description: "{cm:LaunchProgram,{cm:AppDisplayName}}";',
        )
        for line in required_lines:
            self.assertIn(line, installer)
        for stale in STALE_ENGLISH_BRAND:
            self.assertNotIn(stale, installer)

    def test_installer_version_info_fields_are_exact_compile_time_values(self):
        installer = (ROOT / "installer" / "balachky.iss").read_text(encoding="utf-8-sig")
        self.assertIn("VersionInfoDescription=Balachky Setup", installer)
        self.assertIn("VersionInfoProductName=Balachky", installer)

    def test_installer_version_info_rejects_localized_custom_message(self):
        installer = (ROOT / "installer" / "balachky.iss").read_text(encoding="utf-8-sig")
        localized_version_info = [
            line
            for line in installer.splitlines()
            if line.startswith("VersionInfo") and "{cm:AppDisplayName}" in line
        ]
        self.assertEqual(localized_version_info, [])

    def test_english_pe_string_table_fields_are_exact(self):
        spec = (ROOT / "balachky.spec").read_text(encoding="utf-8")
        self.assertIn('StringTable("040904B0"', spec)
        self.assertEqual(
            spec.count('StringStruct("FileDescription", "Balachky")'),
            1,
        )
        self.assertEqual(
            spec.count('StringStruct("ProductName", "Balachky")'),
            1,
        )

    def test_english_changelog_and_notices_use_canonical_brand(self):
        paths = (
            ROOT / "CHANGELOG.md",
            ROOT / "THIRD-PARTY-NOTICES.txt",
            ROOT / "licenses" / "PERMISSIVE-LICENSES.txt",
        )
        for path in paths:
            text = path.read_text(encoding="utf-8-sig")
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn("Balachky", text)
                for stale in STALE_ENGLISH_BRAND:
                    self.assertNotIn(stale, text)


if __name__ == "__main__":
    unittest.main()
