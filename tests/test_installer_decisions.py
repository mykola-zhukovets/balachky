"""Три рішення власника з живого тесту (2026-07-31): український вступ перед
ліцензією, «Я приймаю» позначено за замовчуванням, питання про очищення
реєстру наприкінці видалення.
"""
import re
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LICENSE = _ROOT / "LICENSE"
_INSTALLER_COPY = _ROOT / "installer" / "LICENSE.txt"
_ISS = _ROOT / "installer" / "balachky.iss"


def _code_section() -> str:
    iss = _ISS.read_text(encoding="utf-8")
    start = iss.index("[Code]")
    return iss[start:]


def _custom_messages(language: str) -> dict[str, str]:
    iss = _ISS.read_text(encoding="utf-8")
    start = iss.index("[CustomMessages]")
    end = iss.index("\n[", start + 1)
    section = iss[start:end]
    prefix = f"{language}."
    result = {}
    for line in section.splitlines():
        if line.startswith(prefix) and "=" in line:
            key, value = line.split("=", 1)
            result[key.removeprefix(prefix)] = value
    return result


class UkrainianLicenseIntroTests(unittest.TestCase):
    """Рішення 1: короткий український вступ перед англійським текстом
    ліцензії; коротший рядок роздільника; кирилиця не має перетворитись на
    кракозябри (UTF-8 з BOM)."""

    def test_installer_copy_still_starts_with_bom(self):
        head = _INSTALLER_COPY.read_bytes()[:3]
        self.assertEqual(head, b"\xef\xbb\xbf")

    def test_installer_copy_decodes_cleanly_as_utf8(self):
        # Якби кодування було не те, byte-послідовності кирилиці або впали б
        # з UnicodeDecodeError, або дали б "?"/кракозябри після round-trip.
        text = _INSTALLER_COPY.read_text(encoding="utf-8-sig")
        self.assertIn("Збройних Силах", text)
        self.assertNotIn("�", text)  # U+FFFD REPLACEMENT CHARACTER

    def test_ukrainian_explanation_precedes_the_english_license_text(self):
        text = _LICENSE.read_text(encoding="utf-8")
        ukr_pos = text.find("КОРОТКЕ ПОЯСНЕННЯ")
        license_heading_pos = text.find("# PolyForm Noncommercial License 1.0.0")
        self.assertNotEqual(ukr_pos, -1, "немає українського вступу")
        self.assertNotEqual(license_heading_pos, -1)
        self.assertLess(
            ukr_pos, license_heading_pos,
            "вступ має стояти ПЕРЕД повним текстом ліцензії, не після",
        )

    def test_explanation_does_not_promise_more_than_the_license(self):
        text = _LICENSE.read_text(encoding="utf-8")
        intro_end = text.find("# PolyForm Noncommercial License 1.0.0")
        intro = text[:intro_end]
        # має пояснювати, а не підміняти — явно каже "не замінює"
        self.assertIn("не замінює", intro)
        # некомерційне використання і комерційна угода згадані чесно
        self.assertIn("некомерційного використання", intro)
        self.assertIn("COMMERCIAL-LICENSE.md", intro)

    def test_separator_lines_are_shortened_from_eighty_to_fifty(self):
        text = _LICENSE.read_text(encoding="utf-8")
        self.assertNotIn("=" * 80, text)
        self.assertIn("=" * 50, text)
        # рівно 50, не більше (наступний символ не '=')
        for match in re.finditer(r"=+", text):
            self.assertLessEqual(len(match.group()), 50)

    def test_installer_copy_still_matches_license_verbatim(self):
        original = _LICENSE.read_text(encoding="utf-8")
        copy = _INSTALLER_COPY.read_text(encoding="utf-8-sig")
        self.assertEqual(original, copy)


class LicenseAcceptedByDefaultTests(unittest.TestCase):
    """Рішення 2: «Я приймаю» позначено одразу, не «Я не приймаю»."""

    def test_initialize_wizard_checks_license_accepted_radio(self):
        code = _code_section()
        self.assertIn("procedure InitializeWizard();", code)
        body_start = code.index("procedure InitializeWizard();")
        body_end = code.index("end;", body_start)
        body = code[body_start:body_end]
        self.assertIn("WizardForm.LicenseAcceptedRadio.Checked := True;", body)


class UninstallRegistryPromptTests(unittest.TestCase):
    """Рішення 3: питання про очищення реєстру наприкінці видалення для тих,
    хто НЕ позначив повне видалення даних на початковому екрані."""

    def test_custom_messages_exist_both_languages(self):
        uk = _custom_messages("ukrainian")
        en = _custom_messages("english")
        self.assertIn("UninstCleanRegistryPrompt", uk)
        self.assertIn("UninstCleanRegistryPrompt", en)

    def test_prompt_names_the_actual_registry_key(self):
        uk = _custom_messages("ukrainian")
        en = _custom_messages("english")
        self.assertIn(r"HKCU\Software\Balachky", uk["UninstCleanRegistryPrompt"])
        self.assertIn(r"HKCU\Software\Balachky", en["UninstCleanRegistryPrompt"])

    def test_prompt_honestly_excludes_user_data_and_models(self):
        # Перевірено фактично (QSettings("Balachky","Balachky") у app.py/
        # main_window.py пише лише onboarded*, splash_greeted, geometry,
        # close_hint_shown, update_*) — жодних записів чи моделей у реєстрі.
        # Текст питання не повинен це заперечувати.
        uk = _custom_messages("ukrainian")
        en = _custom_messages("english")
        for term in ("записів", "розшифровок", "словників", "моделей"):
            with self.subTest(language="uk", term=term):
                self.assertIn(term, uk["UninstCleanRegistryPrompt"])
        for term in ("recordings", "transcripts", "dictionaries", "models"):
            with self.subTest(language="en", term=term):
                self.assertIn(term, en["UninstCleanRegistryPrompt"])

    def test_curuninstallstepchanged_keeps_the_original_data_checkbox_path(self):
        # Не чіпаємо гілку, яку вже стереже tests/test_installer_upgrade_matrix.py
        code = _code_section()
        self.assertIn(
            "if (CurUninstallStep = usPostUninstall) and RemoveUserData then",
            code,
        )

    def test_curuninstallstepchanged_asks_only_when_data_checkbox_was_off(self):
        code = _code_section()
        proc_start = code.index("procedure CurUninstallStepChanged")
        proc_end = code.index("\nend;", proc_start)
        body = code[proc_start:proc_end]
        self.assertIn("not RemoveUserData", body)
        self.assertIn("not UninstallSilent()", body)
        self.assertIn("RegKeyExists(HKEY_CURRENT_USER, 'Software\\Balachky')", body)
        self.assertIn("{cm:UninstCleanRegistryPrompt}", body)
        self.assertIn("MB_YESNO", body)
        # видаляє лише сам ключ, за згодою — не чіпає {localappdata}\Balachky
        self.assertNotIn(r"{localappdata}\Balachky", body)

    def test_silent_uninstall_never_shows_the_registry_msgbox(self):
        code = _code_section()
        proc_start = code.index("procedure CurUninstallStepChanged")
        proc_end = code.index("\nend;", proc_start)
        body = code[proc_start:proc_end]
        guard_pos = body.index("not UninstallSilent()")
        msgbox_pos = body.index("MsgBox(")
        self.assertLess(guard_pos, msgbox_pos)


if __name__ == "__main__":
    unittest.main()
