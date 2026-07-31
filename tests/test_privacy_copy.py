"""Honesty locks for the Privacy & offline copy."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.i18n import STRINGS


class PrivacyCopy(unittest.TestCase):
    def test_network_cases_are_named_in_both_languages(self):
        expected = {
            "uk": ("майстер налаштування", "компонент", "оновл"),
            "en": ("setup wizard", "component", "update"),
        }
        for language, terms in expected.items():
            text = STRINGS[language]["set_offline_text"].lower()
            with self.subTest(language=language):
                for term in terms:
                    self.assertIn(term, text)

    def test_absolute_network_claims_are_absent(self):
        forbidden = {
            "uk": ("нічого не виходить", "єдиний виняток", "кожну спробу"),
            "en": ("nothing leaves", "only exception", "every attempt"),
        }
        keys = ("set_offline_badge", "set_offline_text", "set_offline_log_intro")
        for language, phrases in forbidden.items():
            text = " ".join(STRINGS[language][key] for key in keys).lower()
            with self.subTest(language=language):
                for phrase in phrases:
                    self.assertNotIn(phrase, text)

    def test_automatic_update_hint_is_conditional(self):
        self.assertIn("якщо", STRINGS["uk"]["set_upd_hint"].lower())
        self.assertIn("if", STRINGS["en"]["set_upd_hint"].lower())

    def test_uk_privacy_copy_uses_typographic_apostrophes(self):
        values = (
            value for key, value in STRINGS["uk"].items()
            if key.startswith("set_offline_") or key == "set_upd_hint"
        )
        self.assertNotIn("'", " ".join(values))

    def test_setup_wizard_probe_is_not_described_as_first_run_only(self):
        expected = {
            "uk": "майстер налаштування",
            "en": "setup wizard",
        }
        forbidden = {
            "uk": "першого запуску",
            "en": "first-time setup",
        }
        for language in ("uk", "en"):
            text = " ".join(
                STRINGS[language][key]
                for key in ("set_offline_text", "set_offline_verify")
            ).lower()
            with self.subTest(language=language):
                self.assertIn(expected[language], text)
                self.assertNotIn(forbidden[language], text)

    def test_each_setup_wizard_instance_runs_the_connection_probe(self):
        from PySide6.QtWidgets import QApplication
        from fronts.desktop import onboarding

        app = QApplication.instance() or QApplication([])
        with patch.object(
            onboarding.FirstRunWizard,
            "_gpu_step_possible",
            return_value=False,
        ), patch.object(onboarding, "_has_network", return_value=True) as probe:
            first = onboarding.FirstRunWizard()
            repeat = onboarding.FirstRunWizard(repeat=True)
        self.assertEqual(probe.call_count, 2)
        first.close()
        repeat.close()
        app.processEvents()

    def test_about_and_onboarding_copy_avoid_absolute_network_promises(self):
        keys = (
            "set_about_body",
            "about_net_note",
            "onb_welcome_body",
            "onb_dl_intro",
            "onb_extra_proto_info",
            "dl_consent_body",
        )
        forbidden = {
            "uk": (
                "нікуди не відправляються",
                "нікуди не надсилається",
                "інтернет не потрібен",
                "лише один раз",
                "нічого не надсилає",
            ),
            "en": (
                "not sent anywhere",
                "needs no internet",
                "downloads once",
                "sends nothing",
            ),
        }
        for language, phrases in forbidden.items():
            text = " ".join(STRINGS[language][key] for key in keys).lower()
            with self.subTest(language=language):
                for phrase in phrases:
                    self.assertNotIn(phrase, text)

    def test_about_copy_names_the_main_network_cases(self):
        expected = {
            "uk": ("майстер", "модел", "компонент", "оновл"),
            "en": ("wizard", "model", "component", "update"),
        }
        for language, terms in expected.items():
            text = STRINGS[language]["set_about_body"].lower()
            with self.subTest(language=language):
                for term in terms:
                    self.assertIn(term, text)

    def test_changed_uk_copy_uses_typographic_apostrophes(self):
        keys = (
            "set_about_body",
            "about_net_note",
            "onb_welcome_body",
            "onb_dl_intro",
            "onb_extra_proto_info",
            "dl_consent_body",
        )
        self.assertNotIn("'", " ".join(STRINGS["uk"][key] for key in keys))


if __name__ == "__main__":
    unittest.main()
