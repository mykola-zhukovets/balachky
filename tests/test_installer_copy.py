"""Truthfulness locks for the custom uninstaller copy."""

import unittest
from pathlib import Path


ISS_PATH = (
    Path(__file__).resolve().parents[1]
    / "installer"
    / "balachky.iss"
)
ISS = ISS_PATH.read_text(encoding="utf-8")


def custom_message(key: str) -> str:
    prefix = f"{key}="
    for line in ISS.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).lower()
    raise AssertionError(f"Missing custom message: {key}")


def section(name: str) -> str:
    marker = f"[{name}]"
    start = ISS.index(marker) + len(marker)
    end = ISS.find("\n[", start)
    return ISS[start:] if end == -1 else ISS[start:end]


class InstallerCopy(unittest.TestCase):
    def test_remove_data_checkbox_names_major_data_groups(self):
        uk = custom_message("ukrainian.UninstRemoveData")
        en = custom_message("english.UninstRemoveData")
        # Мовна ревізія 31.07: "моделі" з переліку прибрано СВІДОМО — великі
        # моделі живуть у спільному кеші поза %LOCALAPPDATA%\Balachky, і
        # прапорець їх НЕ видаляє; обіцянка була нечесною. Натомість чесне
        # "завантажені компоненти" (те, що справді лежить у теці даних).
        for term in ("записи", "розшифровки", "завантажені компоненти"):
            with self.subTest(language="uk", term=term):
                self.assertIn(term, uk)
        self.assertNotIn("моделі", uk)
        for term in ("recordings", "transcripts", "downloaded components"):
            with self.subTest(language="en", term=term):
                self.assertIn(term, en)
        self.assertNotIn("models", en)

    def test_prompt_explains_what_the_uninstaller_keeps(self):
        uk = custom_message("ukrainian.UninstPrompt")
        en = custom_message("english.UninstPrompt")
        self.assertIn(r"%localappdata%\balachky", uk)
        self.assertIn(r"%localappdata%\balachky", en)
        for term in ("власних папках", "спільний кеш"):
            with self.subTest(language="uk", term=term):
                self.assertIn(term, uk)
        for term in ("custom folders", "shared model cache"):
            with self.subTest(language="en", term=term):
                self.assertIn(term, en)

    def test_copy_matches_the_real_user_data_uninstall_target(self):
        uninstall_delete = section("UninstallDelete").lower()
        expected = (
            'type: filesandordirs; name: "{localappdata}\\balachky"; '
            "check: removeuserdatachecked"
        )
        self.assertIn(expected, uninstall_delete)

    def test_uninstall_delete_removes_all_plaintext_temp_patterns(self):
        uninstall_delete = section("UninstallDelete").lower()
        expected = (
            'type: filesandordirs; name: "{%temp}\\balachky-meeting-*"',
            'type: filesandordirs; name: "{%temp}\\balachky-meeting-media-*"',
            'type: filesandordirs; name: "{%temp}\\balachky-tts-plain-*"',
        )
        for rule in expected:
            with self.subTest(rule=rule):
                self.assertIn(rule, uninstall_delete)
        self.assertNotIn('name: "{tmp}\\balachky-', uninstall_delete)
        self.assertEqual(uninstall_delete.count("type:"), 4)

    def test_custom_form_has_room_for_complete_copy(self):
        self.assertIn(
            "Form := CreateCustomForm(ScaleX(840), ScaleY(365), False, True);",
            ISS,
        )


if __name__ == "__main__":
    unittest.main()
