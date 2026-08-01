"""Unit tests for bilingual help functionality in fronts/desktop/help.py."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QUrl

from fronts.desktop.help import open_user_guide, _LOCAL, _REMOTE


class HelpI18nTests(unittest.TestCase):
    def test_remote_and_local_urls_for_ukrainian(self):
        self.assertEqual(_LOCAL["uk"], "README.uk.md")
        self.assertIn("README.uk.md#використання", _REMOTE["uk"])

        with patch("fronts.desktop.help.current_language", return_value="uk"), \
             patch("fronts.desktop.help.QDesktopServices.openUrl") as mock_open:
            mock_open.return_value = True
            res = open_user_guide()
            self.assertTrue(res)
            mock_open.assert_called_once()
            called_url = mock_open.call_args[0][0].toString()
            self.assertIn("README.uk.md#використання", called_url)

    def test_remote_and_local_urls_for_english(self):
        self.assertEqual(_LOCAL["en"], "README.md")
        self.assertIn("README.md#usage", _REMOTE["en"])

        with patch("fronts.desktop.help.current_language", return_value="en"), \
             patch("fronts.desktop.help.QDesktopServices.openUrl") as mock_open:
            mock_open.return_value = True
            res = open_user_guide()
            self.assertTrue(res)
            mock_open.assert_called_once()
            called_url = mock_open.call_args[0][0].toString()
            self.assertIn("README.md#usage", called_url)

    def test_local_fallback_when_remote_fails_uk(self):
        with patch("fronts.desktop.help.current_language", return_value="uk"), \
             patch("fronts.desktop.help.QDesktopServices.openUrl", side_effect=[False, True]) as mock_open, \
             patch("whisper_core.paths.bundled_doc", return_value="C:/path/to/README.uk.md"):
            res = open_user_guide()
            self.assertTrue(res)
            self.assertEqual(mock_open.call_count, 2)
            fallback_url = mock_open.call_args_list[1][0][0].toString()
            self.assertIn("README.uk.md", fallback_url)


if __name__ == "__main__":
    unittest.main()
