"""Керування словниками й термінами (feature/vocab-manage).

Ядро без Qt: delete_profile / rename_profile (файли на диску, активний профіль,
валідація) і правка машинної частини словника (terms.auto.toml), яка НЕ чіпає
людський terms.toml з його коментарями. Стиль — як tests/test_backend_regressions.
"""
import tempfile
import unittest
from pathlib import Path

from whisper_core import profiles
from whisper_core.terms import (
    add_term, delete_term, rename_term, editable_canons, read_terms_dict,
)


class ProfileTraversalTests(unittest.TestCase):
    """profiles.get не має вислизнути за profiles/ на traversal-назві (той самий
    вхід приходить від MCP-агента під недовіреним контекстом)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        profiles.get_active(self.root)          # створити profiles/default

    def test_get_rejects_parent_traversal(self):
        evil = self.root / "evilprofile"
        evil.mkdir()
        self.assertIsNone(profiles.get(self.root, "../evilprofile"))

    def test_get_rejects_absolute_path(self):
        self.assertIsNone(profiles.get(self.root, str(self.root)))

    def test_get_rejects_empty_and_root_itself(self):
        self.assertIsNone(profiles.get(self.root, ""))
        self.assertIsNone(profiles.get(self.root, "."))

    def test_get_accepts_legitimate_profile(self):
        self.assertIsNotNone(profiles.get(self.root, "default"))


class ProfileDeleteTests(unittest.TestCase):
    def test_delete_removes_dir_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.create_profile(tmp, "робота")
            d = Path(tmp) / "profiles" / "робота"
            self.assertTrue(d.is_dir())
            profiles.delete_profile(tmp, "робота")
            self.assertFalse(d.exists())

    def test_deleting_active_switches_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.create_profile(tmp, "робота")
            profiles.set_active(tmp, "робота")
            self.assertEqual(profiles.get_active(tmp).name, "робота")
            profiles.delete_profile(tmp, "робота")
            self.assertEqual(profiles.get_active(tmp).name, "default")

    def test_deleting_other_keeps_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.create_profile(tmp, "робота")
            profiles.create_profile(tmp, "дім")
            profiles.set_active(tmp, "робота")
            profiles.delete_profile(tmp, "дім")
            self.assertEqual(profiles.get_active(tmp).name, "робота")
            self.assertFalse((Path(tmp) / "profiles" / "дім").exists())

    def test_delete_default_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.list_profiles(tmp)                 # створити default
            with self.assertRaises(profiles.ProfileValidationError) as cm:
                profiles.delete_profile(tmp, "default")
            self.assertEqual(cm.exception.code, "is_default")
            self.assertTrue((Path(tmp) / "profiles" / "default").is_dir())

    def test_delete_missing_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.list_profiles(tmp)
            with self.assertRaises(profiles.ProfileValidationError) as cm:
                profiles.delete_profile(tmp, "нема")
            self.assertEqual(cm.exception.code, "not_found")


class ProfileRenameTests(unittest.TestCase):
    def test_rename_moves_dir_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = profiles.create_profile(tmp, "робота")
            (p.dir / "terms.toml").write_text(
                '[terms]\nCoworking = ["ко ворк"]\n', encoding="utf-8")
            profiles.rename_profile(tmp, "робота", "офіс")
            old = Path(tmp) / "profiles" / "робота"
            new = Path(tmp) / "profiles" / "офіс"
            self.assertFalse(old.exists())
            self.assertTrue(new.is_dir())
            self.assertIn("Coworking",
                          (new / "terms.toml").read_text(encoding="utf-8"))

    def test_renaming_active_keeps_it_active_under_new_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.create_profile(tmp, "робота")
            profiles.set_active(tmp, "робота")
            profiles.rename_profile(tmp, "робота", "офіс")
            self.assertEqual(profiles.get_active(tmp).name, "офіс")

    def test_renaming_other_keeps_active_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.create_profile(tmp, "робота")
            profiles.create_profile(tmp, "дім")
            profiles.set_active(tmp, "робота")
            profiles.rename_profile(tmp, "дім", "хата")
            self.assertEqual(profiles.get_active(tmp).name, "робота")
            self.assertTrue((Path(tmp) / "profiles" / "хата").is_dir())

    def test_rename_default_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.list_profiles(tmp)
            with self.assertRaises(profiles.ProfileValidationError) as cm:
                profiles.rename_profile(tmp, "default", "інше")
            self.assertEqual(cm.exception.code, "is_default")
            self.assertTrue((Path(tmp) / "profiles" / "default").is_dir())

    def test_rename_to_invalid_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.create_profile(tmp, "робота")
            with self.assertRaises(profiles.ProfileValidationError) as cm:
                profiles.rename_profile(tmp, "робота", "з пробілом")
            self.assertEqual(cm.exception.code, "invalid_name")
            self.assertTrue((Path(tmp) / "profiles" / "робота").is_dir())

    def test_rename_to_existing_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.create_profile(tmp, "робота")
            profiles.create_profile(tmp, "дім")
            with self.assertRaises(profiles.ProfileValidationError) as cm:
                profiles.rename_profile(tmp, "робота", "дім")
            self.assertEqual(cm.exception.code, "already_exists")
            self.assertTrue((Path(tmp) / "profiles" / "робота").is_dir())

    def test_rename_missing_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            profiles.list_profiles(tmp)
            with self.assertRaises(profiles.ProfileValidationError) as cm:
                profiles.rename_profile(tmp, "нема", "будь")
            self.assertEqual(cm.exception.code, "not_found")


class TermsEditTests(unittest.TestCase):
    """Межа безпечного редагування: лише машинний terms.auto.toml. Людський
    terms.toml (з коментарями) лишається байт-у-байт недоторканним."""

    @staticmethod
    def _terms_path(tmp):
        return profiles.create_profile(tmp, "робота").terms_path

    def test_only_auto_terms_are_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            tp.write_text('[terms]\nHuman = ["людина"]\n', encoding="utf-8")
            add_term(tp, "Machine", "машина")
            self.assertEqual(editable_canons(tp), {"Machine"})

    def test_canon_in_both_files_is_not_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            tp.write_text('[terms]\nShared = ["людський"]\n', encoding="utf-8")
            add_term(tp, "Shared", "машинний")
            self.assertNotIn("Shared", editable_canons(tp))

    def test_delete_term_removes_from_auto_and_keeps_human_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            human = "# коментар користувача\n[terms]\nHuman = [\"людина\"]\n"
            tp.write_text(human, encoding="utf-8")
            add_term(tp, "Machine", "машина")
            self.assertTrue(delete_term(tp, "Machine"))
            merged = read_terms_dict(tp)
            self.assertNotIn("Machine", merged)
            self.assertIn("Human", merged)
            self.assertEqual(tp.read_text(encoding="utf-8"), human)

    def test_delete_missing_term_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            add_term(tp, "Machine", "машина")
            self.assertFalse(delete_term(tp, "Нема"))

    def test_delete_last_auto_term_removes_auto_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            add_term(tp, "Machine", "машина")
            auto = tp.with_name("terms.auto.toml")
            self.assertTrue(auto.exists())
            delete_term(tp, "Machine")
            self.assertFalse(auto.exists())

    def test_rename_term_preserves_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            add_term(tp, "Coworking", "ко ворк")
            add_term(tp, "Coworking", "коворкінг")
            self.assertTrue(rename_term(tp, "Coworking", "Коворкінг"))
            merged = read_terms_dict(tp)
            self.assertNotIn("Coworking", merged)
            self.assertEqual(set(merged["Коворкінг"]), {"ко ворк", "коворкінг"})

    def test_rename_term_leaves_human_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            human = "# мій коментар\n[terms]\nHuman = [\"людина\"]\n"
            tp.write_text(human, encoding="utf-8")
            add_term(tp, "Machine", "машина")
            rename_term(tp, "Machine", "Механізм")
            self.assertEqual(tp.read_text(encoding="utf-8"), human)
            self.assertIn("Механізм", read_terms_dict(tp))

    def test_rename_term_noop_on_same_or_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._terms_path(tmp)
            add_term(tp, "Machine", "машина")
            self.assertFalse(rename_term(tp, "Machine", "Machine"))
            self.assertFalse(rename_term(tp, "Machine", "   "))
            self.assertIn("Machine", read_terms_dict(tp))


if __name__ == "__main__":
    unittest.main()
