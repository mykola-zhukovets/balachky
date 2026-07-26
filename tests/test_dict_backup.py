"""Резервні копії словників користувача + автовідновлення побитого файла
(whisper_core.dict_backup, застосовано в whisper_core.terms до terms.auto.toml).

Сценарій, що доводиться мутацією: файл побито, ціла копія є → дані
повертаються з копії, а не губляться."""
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from whisper_core import dict_backup
from whisper_core.terms import _read_auto, _write_auto, add_term, read_terms_dict


class TestDictBackupRotation(unittest.TestCase):
    def _auto(self, root: Path) -> Path:
        return root / "terms.auto.toml"

    def test_rotate_before_write_moves_current_to_bak1(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            auto = self._auto(root)
            auto.write_text("v1", encoding="utf-8")
            dict_backup.rotate_before_write(auto)
            self.assertFalse(auto.exists())
            self.assertEqual((root / "terms.auto.toml.bak1").read_text(), "v1")

    def test_rotate_keeps_only_three_generations(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            auto = self._auto(root)
            # той самий порядок, що в terms._write_auto: rotate ПЕРЕД записом
            # нового вмісту, 5 послідовних "збережень" v0..v4
            for i in range(5):
                dict_backup.rotate_before_write(auto)
                auto.write_text(f"v{i}", encoding="utf-8")
            # не більше MAX_BACKUPS файлів .bakN на диску — найстаріші (v0)
            # прибрано
            baks = sorted(root.glob("terms.auto.toml.bak*"))
            self.assertEqual(len(baks), dict_backup.MAX_BACKUPS)
            self.assertEqual((root / "terms.auto.toml.bak1").read_text(), "v3")
            self.assertEqual((root / "terms.auto.toml.bak2").read_text(), "v2")
            self.assertEqual((root / "terms.auto.toml.bak3").read_text(), "v1")
            self.assertFalse((root / "terms.auto.toml.bak4").exists())

    def test_rotate_noop_when_nothing_to_backup(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            auto = self._auto(root)               # файла ще нема
            dict_backup.rotate_before_write(auto)  # не має кинути
            self.assertFalse(auto.exists())
            self.assertFalse((root / "terms.auto.toml.bak1").exists())

    def test_recover_promotes_newest_good_backup(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            auto = self._auto(root)
            (root / "terms.auto.toml.bak1").write_text('[terms]\nfoo = ["fu"]\n',
                                                         encoding="utf-8")
            auto.write_text("{{{ побитий toml", encoding="utf-8")   # поточний файл битий

            def loads(text):
                return dict(tomllib.loads(text).get("terms", {}))

            data, recovered = dict_backup.recover(auto, loads)
            self.assertTrue(recovered)
            self.assertEqual(data, {"foo": ["fu"]})
            self.assertEqual(auto.read_text(encoding="utf-8"),
                              '[terms]\nfoo = ["fu"]\n')   # копію піднято в основний файл

    def test_recover_skips_broken_copies_to_first_good_one(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            auto = self._auto(root)
            (root / "terms.auto.toml.bak1").write_text("теж битий", encoding="utf-8")
            (root / "terms.auto.toml.bak2").write_text('[terms]\nbar = ["ba"]\n',
                                                         encoding="utf-8")
            auto.write_text("побитий", encoding="utf-8")

            def loads(text):
                return dict(tomllib.loads(text).get("terms", {}))

            data, recovered = dict_backup.recover(auto, loads)
            self.assertTrue(recovered)
            self.assertEqual(data, {"bar": ["ba"]})

    def test_recover_no_good_copy_returns_false(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            auto = self._auto(root)
            auto.write_text("побитий", encoding="utf-8")   # копій узагалі нема

            data, recovered = dict_backup.recover(
                auto, lambda t: dict(tomllib.loads(t).get("terms", {})))
            self.assertFalse(recovered)
            self.assertIsNone(data)


class TestTermsAutoBackupIntegration(unittest.TestCase):
    """Наскрізний сценарій через реальні виклики словника термінів (не лише
    примітиви dict_backup): add_term пише, файл б'ється, читання відновлює."""

    def test_write_then_corrupt_then_read_recovers(self):
        with TemporaryDirectory() as d:
            terms_path = Path(d) / "terms.toml"
            add_term(terms_path, "почта", "почту")     # запис 1 — бекапу ще нема
            add_term(terms_path, "гортаті", "гортати")  # запис 2 → запис 1 стає .bak1

            auto = terms_path.with_name("terms.auto.toml")
            bak1 = auto.with_name("terms.auto.toml.bak1")
            bak1_content = bak1.read_text(encoding="utf-8")   # цілий стан після запису 1
            auto.write_text("{{{ не toml взагалі", encoding="utf-8")   # імітуємо збій диска

            recovered_paths = []
            data = _read_auto(terms_path, on_recovered=recovered_paths.append)

            self.assertEqual(recovered_paths, [auto])          # повідомлення про відновлення пішло
            self.assertIn("почта", data)                        # дані з копії повернулись, не втрачені
            self.assertEqual(auto.read_text(encoding="utf-8"), bak1_content)  # копію піднято в основний файл

    def test_read_terms_dict_surfaces_recovered_data_and_callback(self):
        with TemporaryDirectory() as d:
            terms_path = Path(d) / "terms.toml"
            add_term(terms_path, "почта", "почту")      # запис 1 — бекапу ще нема
            add_term(terms_path, "гортаті", "гортати")   # запис 2 → запис 1 стає .bak1

            auto = terms_path.with_name("terms.auto.toml")
            auto.write_text("побитий toml {{{", encoding="utf-8")   # б'ємо ОСТАННІЙ (2-й) запис

            calls = []
            merged = read_terms_dict(terms_path, on_recovered=calls.append)
            self.assertEqual(len(calls), 1)
            self.assertIn("почта", merged)   # це і є вміст .bak1 (стан після запису 1)

    def test_write_auto_backs_up_before_clearing(self):
        """Навіть повне очищення словника (усі терміни видалено) не нищить
        останню версію без сліду — вона лишається в .bak1."""
        with TemporaryDirectory() as d:
            terms_path = Path(d) / "terms.toml"
            _write_auto(terms_path, {"foo": ["fu"]})
            _write_auto(terms_path, {})              # очищення
            bak1 = terms_path.with_name("terms.auto.toml.bak1")
            self.assertIn("foo", bak1.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
