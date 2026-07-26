"""Юніт-тест AST-сканера імпортів (dev/check_lazy_imports.py).

Синтетичний пакет у tmp: метод із ЛІНИВИМ (in-function) імпортом неіснуючого
імені має бути спійманий; чистий пакет — ні. Це відтворює клас історичного
багу `from .settings import DiarizationDownloadWorker` (зникле ім'я всередині
методу, повз pyflakes і юніт-тести).
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCANNER = _ROOT / "dev" / "check_lazy_imports.py"


def _load_scanner():
    """Завантажити dev/check_lazy_imports.py як модуль (він не в пакеті)."""
    spec = importlib.util.spec_from_file_location("check_lazy_imports", _SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CheckLazyImportsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = _load_scanner()

    def _make_pkg(self, tmp: Path, lazy_import_line: str):
        """Пакет `synthpkg` з good.py (ціль імпорту) і user.py (лінивий імпорт)."""
        pkg = tmp / "synthpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "good.py").write_text(
            "EXISTING = 1\n\n\ndef helper():\n    return EXISTING\n",
            encoding="utf-8")
        (pkg / "user.py").write_text(
            "def action():\n"
            f"    {lazy_import_line}\n"
            "    return True\n",
            encoding="utf-8")
        return pkg

    def _check(self, pkg: Path, syspath_root: Path):
        records = self.scanner.collect_imports([pkg], syspath_root)
        return self.scanner.check_records(records)

    def test_catches_missing_lazy_name(self):
        """Лінивий `from .good import MISSING` (нема такого імені) → проблема."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pkg = self._make_pkg(tmp, "from .good import MISSING")
            if str(tmp) not in sys.path:
                sys.path.insert(0, str(tmp))
            try:
                problems, _ = self._check(pkg, tmp)
            finally:
                sys.path.remove(str(tmp))
            self.assertTrue(problems, "сканер мусив спіймати зникле ім'я MISSING")
            joined = " ".join(m for _, _, m in problems)
            self.assertIn("MISSING", joined)

    def test_clean_package_no_problems(self):
        """Лінивий `from .good import EXISTING` (ім'я є) → жодних проблем."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pkg = self._make_pkg(tmp, "from .good import EXISTING")
            if str(tmp) not in sys.path:
                sys.path.insert(0, str(tmp))
            try:
                problems, _ = self._check(pkg, tmp)
            finally:
                sys.path.remove(str(tmp))
            self.assertEqual(problems, [], f"неочікувані проблеми: {problems}")

    def test_optional_import_is_skipped_not_flagged(self):
        """import у try/except ImportError — у 'пропущено', НЕ у проблемах."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            pkg = tmp / "optpkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "opt.py").write_text(
                "try:\n"
                "    import totally_missing_module_xyz\n"
                "except ImportError:\n"
                "    totally_missing_module_xyz = None\n",
                encoding="utf-8")
            if str(tmp) not in sys.path:
                sys.path.insert(0, str(tmp))
            try:
                problems, skipped = self._check(pkg, tmp)
            finally:
                sys.path.remove(str(tmp))
            self.assertEqual(problems, [], f"optional не мав стати проблемою: {problems}")
            self.assertTrue(any("totally_missing_module_xyz" in s
                                for _, _, s in skipped),
                            "optional-імпорт мав потрапити у 'пропущено'")


if __name__ == "__main__":
    unittest.main()
