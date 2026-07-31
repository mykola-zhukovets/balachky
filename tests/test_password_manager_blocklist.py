"""Regression locks for the shared password-manager process blocklist."""

import unittest

from fronts.desktop.context import SecurityGate
from fronts.desktop.wininput import PASSWORD_MANAGERS, is_password_manager


EXPECTED_PASSWORD_MANAGERS = frozenset({
    "keepass.exe",
    "keepassxc.exe",
    "keepassxc-proxy.exe",
    "bitwarden.exe",
    "1password.exe",
    "credentialuibroker.exe",
    "robotaskbaricon.exe",
    "enpass.exe",
})


class PasswordManagerBlocklistTests(unittest.TestCase):
    def test_explicit_expected_set_is_the_single_shared_source(self):
        self.assertEqual(PASSWORD_MANAGERS, EXPECTED_PASSWORD_MANAGERS)

        gate = SecurityGate()
        for exe in sorted(EXPECTED_PASSWORD_MANAGERS):
            with self.subTest(exe=exe):
                self.assertTrue(is_password_manager(exe.upper()))
                self.assertTrue(gate.is_blocked(exe.upper()))

    def test_unrelated_process_is_allowed_by_both_gates(self):
        self.assertFalse(is_password_manager("notepad.exe"))
        self.assertFalse(SecurityGate().is_blocked("notepad.exe"))


if __name__ == "__main__":
    unittest.main()
