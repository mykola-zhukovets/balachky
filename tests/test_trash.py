"""Тести кошика (soft-delete) — whisper_core/trash.py.

Справжня файлова система через tempfile (не моки shutil): доводимо, що дані
реально переїжджають/повертаються/остаточно зникають на диску.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from whisper_core import trash


class TrashSoftDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "sessions"
        self.root.mkdir()
        self.session_dir = self.root / "2026-07-31_10-00-00"
        self.session_dir.mkdir()
        (self.session_dir / "meeting.json").write_text("{}", encoding="utf-8")
        (self.session_dir / "audit.jsonl").write_text(
            '{"seq":0,"type":"created"}\n', encoding="utf-8")

    def test_soft_delete_moves_dir_into_trash_under_same_root(self):
        dest = trash.soft_delete(self.session_dir, self.root, now=1000.0)
        self.assertFalse(self.session_dir.exists())
        self.assertTrue(dest.is_dir())
        self.assertTrue(str(dest).startswith(str(trash.trash_root(self.root))))

    def test_soft_delete_preserves_audit_log_untouched(self):
        original = (self.session_dir / "audit.jsonl").read_text(encoding="utf-8")
        dest = trash.soft_delete(self.session_dir, self.root, now=1000.0)
        self.assertEqual(
            (dest / "audit.jsonl").read_text(encoding="utf-8"), original)
        # НЕ дописано нову подію про перенесення — журнал лишається як був.
        self.assertEqual(original.count("\n"), 1)

    def test_soft_delete_missing_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            trash.soft_delete(self.root / "nope", self.root)

    def test_restore_moves_dir_back_to_original_name(self):
        dest = trash.soft_delete(self.session_dir, self.root, now=1000.0)
        restored = trash.restore(dest, self.root)
        self.assertEqual(restored, self.session_dir)
        self.assertTrue(restored.is_dir())
        self.assertFalse(dest.exists())
        self.assertEqual(
            (restored / "meeting.json").read_text(encoding="utf-8"), "{}")

    def test_restore_conflict_gets_suffix_and_does_not_clobber(self):
        dest = trash.soft_delete(self.session_dir, self.root, now=1000.0)
        # Нова тека з тим самим ім'ям з'явилась на місці, поки старе лежало в кошику.
        self.session_dir.mkdir()
        (self.session_dir / "meeting.json").write_text(
            "LIVE", encoding="utf-8")
        restored = trash.restore(dest, self.root)
        self.assertNotEqual(restored, self.session_dir)
        self.assertEqual(restored.name, self.session_dir.name + " (2)")
        # Наявна тека НЕ затерта.
        self.assertEqual(
            (self.session_dir / "meeting.json").read_text(encoding="utf-8"),
            "LIVE")
        self.assertEqual(
            (restored / "meeting.json").read_text(encoding="utf-8"), "{}")

    def test_restore_with_poisoned_original_name_stays_inside_root(self):
        r"""Аудит релізу 31.07: trash_info.json — дані з диска, і отруєне
        original_name виду "..\\..\\чужа-тека" тягнуло теку ЗА МЕЖІ root
        (root / name без перевірки). Тепер підозріле імʼя → фолбек на назву
        теки в кошику, все лишається всередині root."""
        import json
        outside = self.root.parent / "втеча"
        self.assertFalse(outside.exists())
        for poison in (r"..\..\втеча", "../втеча", r"C:\втеча",
                       "..", ".", 42, r"під\тека"):
            dest = trash.soft_delete(self.session_dir, self.root, now=1000.0)
            (dest / "trash_info.json").write_text(json.dumps(
                {"original_name": poison}), encoding="utf-8")
            restored = trash.restore(dest, self.root)
            self.assertEqual(restored.parent, self.root,
                             f"отруєне {poison!r} вивело за межі root")
            self.assertFalse(outside.exists(),
                             f"отруєне {poison!r} створило теку поза root")
            # повернути стан для наступної ітерації
            shutil.move(str(restored), str(self.session_dir))

    def test_purge_expired_removes_only_old_entries(self):
        now = 1_800_000_000.0   # довільна "поточна" епоха, зручна для арифметики
        day = 86400.0
        old_dest = trash.soft_delete(self.session_dir, self.root, now=now - 8 * day)
        session_dir2 = self.root / "2026-07-31_11-00-00"
        session_dir2.mkdir()
        (session_dir2 / "meeting.json").write_text("{}", encoding="utf-8")
        new_dest = trash.soft_delete(session_dir2, self.root, now=now - 1 * day)

        purged = trash.purge_expired(self.root, max_age_days=7, now=now)

        self.assertIn(old_dest, purged)
        self.assertFalse(old_dest.exists())
        self.assertTrue(new_dest.exists())

    def test_purge_expired_no_trash_dir_is_noop(self):
        self.assertEqual(trash.purge_expired(self.root), [])


if __name__ == "__main__":
    unittest.main()
