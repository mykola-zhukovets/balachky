"""Unit tests for reveal_in_explorer in fronts.desktop.links."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fronts.desktop.links import reveal_in_explorer


class RevealInExplorerTests(unittest.TestCase):
    def test_missing_path_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nonexistent" / "file.txt"
            self.assertFalse(reveal_in_explorer(missing))

    def test_existing_file_invokes_explorer_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "sample.txt"
            f.write_text("content", encoding="utf-8")
            with patch("subprocess.Popen") as mock_popen, patch("os.startfile"):
                res = reveal_in_explorer(f)
                self.assertTrue(res)
                if sys.platform.startswith("win"):
                    mock_popen.assert_called_once()
                    args = mock_popen.call_args[0][0]
                    self.assertEqual(args[0], "explorer")
                    self.assertEqual(args[1], "/select,")

    def test_existing_dir_invokes_startfile_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "subdir"
            d.mkdir()
            with patch("os.startfile") as mock_startfile:
                res = reveal_in_explorer(d)
                self.assertTrue(res)
                if sys.platform.startswith("win"):
                    mock_startfile.assert_called_once_with(str(d.resolve()))

    def test_oserror_in_startfile_is_safely_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "subdir"
            d.mkdir()
            with patch("os.startfile", side_effect=OSError("Access denied")):
                res = reveal_in_explorer(d)
                if sys.platform.startswith("win"):
                    self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
