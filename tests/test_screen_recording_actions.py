"""RecordActionBar на Записі екрана: реальні
файлові наслідки перейменування/видалення, не виклик мока. Тест перевіряє
логіку контролера (DesktopApp.rename_screen_recording / delete_screen_recording
/ show_screen_recording_in_folder) напряму на справжніх файлах у тимчасовій
теці — без побудови повного QApplication/DesktopApp (важке з реальним
мікрофоном/рушієм), методи викликаються unbound на легкому дублері з тими
самими атрибутами, які вони фактично читають (self.cfg, self._screen_recordings_root()).
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fronts.desktop.app import DesktopApp
from fronts.desktop.record_action_bar import is_safe_display_name


class _StubApp:
    """Дублер: несе лише те, що читають методи екрана запису."""

    def __init__(self, root: Path):
        self._root = root
        self.cfg = SimpleNamespace(screen_recordings_dir=str(root))
        self.opened_folder = 0

    def _screen_recordings_root(self):
        return self._root

    def open_screen_recordings_folder(self):
        self.opened_folder += 1


class RenameScreenRecordingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.app = _StubApp(self.root)

    def _make_file(self, name="screen-20260730-1200.webm", content=b"video-bytes"):
        p = self.root / name
        p.write_bytes(content)
        return p

    def test_rename_actually_renames_file_on_disk(self):
        src = self._make_file()
        new_path = DesktopApp.rename_screen_recording(self.app, src, "нарада-липень")
        self.assertIsNotNone(new_path)
        self.assertFalse(src.exists(), "старий файл має зникнути")
        self.assertTrue(new_path.exists(), "новий файл має існувати на диску")
        self.assertEqual(new_path.name, "нарада-липень.webm")
        self.assertEqual(new_path.read_bytes(), b"video-bytes")

    def test_rename_refuses_when_target_already_exists(self):
        src = self._make_file()
        self._make_file("зайнято.webm")
        result = DesktopApp.rename_screen_recording(self.app, src, "зайнято")
        self.assertIsNone(result)
        self.assertTrue(src.exists(), "відмова — оригінал лишається на місці")

    def test_rename_refuses_path_traversal_via_dots(self):
        src = self._make_file()
        result = DesktopApp.rename_screen_recording(self.app, src, "..")
        self.assertIsNone(result)
        self.assertTrue(src.exists())

    def test_rename_refuses_separators_in_new_name(self):
        src = self._make_file()
        result = DesktopApp.rename_screen_recording(self.app, src, "a/b")
        self.assertIsNone(result)
        self.assertTrue(src.exists())

    def test_rename_refuses_file_outside_recordings_root(self):
        outside_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: outside_dir.rmdir() if outside_dir.exists() else None)
        outside = outside_dir / "screen-elsewhere.webm"
        outside.write_bytes(b"x")
        try:
            result = DesktopApp.rename_screen_recording(self.app, outside, "нове")
            self.assertIsNone(result)
            self.assertTrue(outside.exists())
        finally:
            outside.unlink(missing_ok=True)


class DeleteScreenRecordingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.app = _StubApp(self.root)

    def test_delete_actually_removes_file_from_disk(self):
        p = self.root / "screen-20260730-1200.webm"
        p.write_bytes(b"video-bytes")
        ok = DesktopApp.delete_screen_recording(self.app, p)
        self.assertTrue(ok)
        self.assertFalse(p.exists(), "файл має фізично зникнути з диска")

    def test_delete_refuses_file_outside_recordings_root(self):
        outside_dir = Path(tempfile.mkdtemp())
        outside = outside_dir / "screen-elsewhere.webm"
        outside.write_bytes(b"x")
        try:
            ok = DesktopApp.delete_screen_recording(self.app, outside)
            self.assertFalse(ok)
            self.assertTrue(outside.exists(), "файл поза текою записів не мав постраждати")
        finally:
            outside.unlink(missing_ok=True)
            outside_dir.rmdir()

    def test_delete_of_already_missing_file_is_a_safe_no_op(self):
        p = self.root / "screen-not-there.webm"
        ok = DesktopApp.delete_screen_recording(self.app, p)
        self.assertTrue(ok, "повторний виклик (файл уже видалено) — не помилка")

    def test_no_orphaned_journal_entries_because_there_is_no_journal(self):
        """Запис екрана — плоский файл без журналу цілісності/шифрування (на
        відміну від Наради, де є meeting/audit_log.py + storage_crypto.py).
        list_screen_recordings глобить файли напряму з диска — після видалення
        файлу перелік просто не побачить його знову, жодного стороннього
        джерела правди (БД/журналу) звірити нема з чим."""
        p = self.root / "screen-20260730-1200.webm"
        p.write_bytes(b"video-bytes")
        self.assertTrue(DesktopApp.delete_screen_recording(self.app, p))
        remaining = sorted(self.root.glob("*.webm"))
        self.assertEqual(remaining, [])


class ShowInFolderFallbackTests(unittest.TestCase):
    def test_falls_back_to_open_folder_off_windows_or_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = _StubApp(root)
            missing = root / "screen-gone.webm"   # не існує → не Windows-select-гілка
            DesktopApp.show_screen_recording_in_folder(app, missing)
            self.assertEqual(app.opened_folder, 1)


class SafeDisplayNameTests(unittest.TestCase):
    def test_rejects_dot_only_and_separators(self):
        for bad in (".", "..", "", "   ", "a/b", "a\\b", "a:b", 'a"b'):
            self.assertFalse(is_safe_display_name(bad), repr(bad))

    def test_accepts_ordinary_names(self):
        for ok in ("нарада-липень", "meeting 2026", "запис (1)"):
            self.assertTrue(is_safe_display_name(ok), repr(ok))


if __name__ == "__main__":
    unittest.main()
