"""Вартовий існування цілей для patch та patch.object у наборі тестів.

Перевіряє, що всі рядкові та об'єктні цілі моків (patch, mock.patch, patch.object)
вказують на реальні модулі та атрибути у кодовій базі. Запобігає мовчазній деградації
тестів після рефакторингу або перейменування функцій і класів.
"""
from __future__ import annotations

import ast
import importlib
import logging
import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# PySide6-модулі фронту потребують offscreen-платформи без живого дисплея
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BALACHKY_INSTANCE_SUFFIX", f"-test-{os.getpid()}")

_LOGGER = logging.getLogger("test_patch_targets_exist")
# Пакети самого репозиторію: їхня відсутність під час імпорту — поламка, не опція.
_REPO_PACKAGES = frozenset({"fronts", "whisper_core", "tests", "scripts", "dev"})


class _PatchTargetVisitor(ast.NodeVisitor):
    """AST-обхідник для збору цілей patch і patch.object з локальною таблицею імпортів."""

    def __init__(self, filename: Path) -> None:
        self.filename = filename
        self.scopes: list[dict[str, str]] = [{}]
        # Елементи: (рядок, dotted_target, kind, original_repr)
        self.targets: list[tuple[int, str, str, str]] = []
        # Елементи: (рядок, reason, original_repr)
        self.skipped: list[tuple[int, str, str]] = []

    def _collect_scope_imports(self, body: list[ast.stmt]) -> dict[str, str]:
        """Імпорти поточної області видимості, включно з тими, що стоять усередині
        try/with/if (у вкладені def/class не заходимо — там своя область).

        ``import a.b`` прив'язує ім'я ``a`` до пакета ``a`` (не до ``a.b``);
        ``import a.b as ab`` — ``ab`` до ``a.b``; ``from a import b`` — ``b`` до ``a.b``."""
        imports: dict[str, str] = {}

        def visit(node: ast.AST) -> None:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        imports[alias.asname] = alias.name
                    else:
                        top = alias.name.split(".")[0]
                        imports[top] = top
                return
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    imports[alias.asname or alias.name] = (
                        f"{mod}.{alias.name}" if mod else alias.name)
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            for child in ast.iter_child_nodes(node):
                visit(child)

        for stmt in body:
            visit(stmt)
        return imports

    def visit_Module(self, node: ast.Module) -> None:
        self.scopes = [self._collect_scope_imports(node.body)]
        for stmt in node.body:
            self.visit(stmt)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        new_scope = dict(self.scopes[-1])
        new_scope.update(self._collect_scope_imports(node.body))
        self.scopes.append(new_scope)
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        new_scope = dict(self.scopes[-1])
        new_scope.update(self._collect_scope_imports(node.body))
        self.scopes.append(new_scope)
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for dec in node.decorator_list:
            self.visit(dec)
        new_scope = dict(self.scopes[-1])
        new_scope.update(self._collect_scope_imports(node.body))
        self.scopes.append(new_scope)
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    @staticmethod
    def _dotted_chain(node: ast.AST) -> "list[str] | None":
        """``a.b.c`` -> ["a", "b", "c"]; виклики, індекси тощо -> None."""
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        parts.reverse()
        return parts

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_direct = False
        is_obj = False

        if isinstance(func, ast.Name) and func.id == "patch":
            is_direct = True
        elif isinstance(func, ast.Attribute) and func.attr == "patch":
            # mock.patch або unittest.mock.patch
            is_direct = True
        elif isinstance(func, ast.Attribute) and func.attr == "object":
            # patch.object, mock.patch.object або unittest.mock.patch.object
            val = func.value
            if isinstance(val, ast.Name) and val.id == "patch":
                is_obj = True
            elif isinstance(val, ast.Attribute) and val.attr == "patch":
                is_obj = True

        # Якщо вказано create=True, mock створює атрибут динамічно
        has_create_true = any(
            kw.arg == "create" and isinstance(kw.value, ast.Constant) and bool(kw.value.value)
            for kw in node.keywords
        )

        if is_direct and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if has_create_true:
                    self.skipped.append((node.lineno, "виклик із create=True", first.value))
                else:
                    self.targets.append((node.lineno, first.value, "patch", first.value))
            else:
                self.skipped.append((node.lineno, "нелітеральна ціль patch", ast.unparse(first)))

        if is_obj and len(node.args) >= 2:
            target = node.args[0]
            attr = node.args[1]
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                attr_name = attr.value
                orig_desc = f"{ast.unparse(target)}.{attr_name}"
                if has_create_true:
                    self.skipped.append((node.lineno, "виклик із create=True", orig_desc))
                else:
                    chain = self._dotted_chain(target)
                    if chain and chain[0] in self.scopes[-1]:
                        base = ".".join([self.scopes[-1][chain[0]], *chain[1:]])
                        self.targets.append(
                            (node.lineno, f"{base}.{attr_name}", "patch.object", orig_desc))
                    elif chain:
                        self.skipped.append(
                            (node.lineno, f"ціль “{chain[0]}” не є імпортованим ім'ям", orig_desc))
                    else:
                        self.skipped.append(
                            (node.lineno, f"ціль “{ast.unparse(target)}” не є ланцюжком імен",
                             orig_desc))

        self.generic_visit(node)


def _check_target(target: str) -> tuple[str, str | None, str | None]:
    """Перевіряє dotted-ціль: імпортує найдовший префікс і перевіряє hasattr для решти.

    Повертає кортеж (статус, повідомлення, деталь):
      - ("ok", None, None)
      - ("missing_attr", повідомлення, ім'я_атрибута)
      - ("no_module_imported", повідомлення, префікс)
      - ("optional_import_error", повідомлення, префікс)
    """
    parts = target.split(".")
    longest_mod = None
    remaining: list[str] = []

    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        try:
            mod = importlib.import_module(prefix)
            longest_mod = mod
            remaining = parts[i:]
            break
        except ModuleNotFoundError as e:
            missing = e.name or ""
            if missing == prefix or prefix.startswith(missing + "."):
                continue                      # такого модуля нема — можливо, це атрибут
            # Модуль є, але сам не імпортує залежність: внутрішню (fronts/whisper_core…)
            # — це поламка, третьосторонню — опційна залежність, пропуск із поміткою.
            if missing.split(".")[0] in _REPO_PACKAGES:
                return ("broken_module", f"{prefix} -> {e}", prefix)
            return ("optional_import_error", f"{prefix} -> {e}", prefix)
        except Exception as e:                # ImportError, SyntaxError, помилка на рівні модуля
            return ("broken_module", f"{prefix} -> {type(e).__name__}: {e}", prefix)

    if longest_mod is None:
        return ("no_module_imported", f"жоден модуль за префіксом “{parts[0]}” не імпортується", parts[0])

    cur = longest_mod
    for part in remaining:
        if not hasattr(cur, part):
            return ("missing_attr", f"атрибут “{part}” відсутній у “{getattr(cur, '__name__', type(cur).__name__)}”", part)
        cur = getattr(cur, part)

    return ("ok", None, None)


class PatchTargetsExistTests(unittest.TestCase):
    """Вартовий актуальності цілей patch у тестових файлах."""

    def test_all_patch_targets_exist(self) -> None:
        tests_dir = _REPO_ROOT / "tests"

        failures: list[str] = []
        skipped_optional: list[str] = []

        for test_file in sorted(tests_dir.glob("*.py")):
            try:
                tree = ast.parse(test_file.read_text(encoding="utf-8"), filename=str(test_file))
            except Exception as e:
                failures.append(f"{test_file.name}:1 -> не вдалося розібрати AST: {e}")
                continue

            visitor = _PatchTargetVisitor(test_file)
            visitor.visit(tree)

            rel_path = test_file.relative_to(_REPO_ROOT)

            for lineno, target, _kind, orig_repr in visitor.targets:
                status, msg, _detail = _check_target(target)
                if status in ("missing_attr", "no_module_imported", "broken_module"):
                    failures.append(f"{rel_path}:{lineno} -> {target} [{orig_repr}] ({msg})")
                elif status == "optional_import_error":
                    skipped_optional.append(f"{rel_path}:{lineno} -> {target} ({msg})")

        if skipped_optional:
            # WARNING, а не INFO: без налаштованого logging його друкує запасний
            # обробник у stderr — пропуск видно у виводі гейта.
            _LOGGER.warning(
                "Пропущено перевірку %d цілей через відсутні опційні залежності:\n%s",
                len(skipped_optional),
                "\n".join(skipped_optional),
            )

        if failures:
            self.fail(
                f"Знайдено {len(failures)} неіснуючих цілей patch:\n"
                + "\n".join(failures)
            )

    def test_sentinel_detects_nonexistent_attribute(self) -> None:
        """Перевірка самодіагностики вартового: неіснуючий атрибут мусить ловитися."""
        # Синтетичні цілі, не продуктові: тест не має залежати від того, чи app.py
        # колись знову імпортує ім'я Engine.
        status, _msg, detail = _check_target("unittest.mock.no_such_attribute_zz")
        self.assertEqual(status, "missing_attr")
        self.assertEqual(detail, "no_such_attribute_zz")
        self.assertEqual(_check_target("unittest.mock.patch")[0], "ok")
        self.assertEqual(_check_target("no_such_pkg_zz.thing")[0], "no_module_imported")

    def test_broken_module_vs_optional_dependency(self) -> None:
        """Модуль, що не імпортує внутрішній пакет репозиторію, — провал; модуль без
        сторонньої бібліотеки — пропуск із поміткою."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for name, line in (("zz_broken_internal", "import whisper_core.no_such_internal_zz"),
                               ("zz_missing_thirdparty", "import no_such_thirdparty_zz")):
                pkg = Path(tmp) / name
                pkg.mkdir()
                (pkg / "__init__.py").write_text(line + "\n", encoding="utf-8")
            sys.path.insert(0, tmp)
            try:
                self.assertEqual(_check_target("zz_broken_internal.thing")[0], "broken_module")
                self.assertEqual(_check_target("zz_missing_thirdparty.thing")[0],
                                 "optional_import_error")
            finally:
                sys.path.remove(tmp)
                for name in ("zz_broken_internal", "zz_missing_thirdparty"):
                    sys.modules.pop(name, None)

    def test_import_binding_rules(self) -> None:
        """``import a.b`` прив'язує ``a`` до ``a``; імпорти всередині try/with видно;
        ланцюжок атрибутів від імпортованого імені стає повною ціллю."""
        source = (
            "import os.path\n"
            "try:\n"
            "    from fronts.desktop import app as appmod\n"
            "except ImportError:\n"
            "    appmod = None\n"
            "def test_x():\n"
            "    with patch.object(os, 'getcwd'):\n"
            "        pass\n"
            "    patch.object(appmod, 'make_engine')\n"
            "    patch.object(os.path, 'join')\n"
        )
        visitor = _PatchTargetVisitor(Path("synthetic.py"))
        visitor.visit(ast.parse(source))
        self.assertEqual([t for _, t, _, _ in visitor.targets],
                         ["os.getcwd", "fronts.desktop.app.make_engine", "os.path.join"])
        self.assertEqual(visitor.skipped, [])

    def test_non_literal_patch_target_is_counted(self) -> None:
        """``patch(f"{MOD}.x")`` не перевіриш статично — але й не губимо мовчки."""
        visitor = _PatchTargetVisitor(Path("synthetic.py"))
        visitor.visit(ast.parse("MOD = 'os'\npatch(f'{MOD}.getcwd')\n"))
        self.assertEqual(visitor.targets, [])
        self.assertEqual(len(visitor.skipped), 1)
        self.assertIn("нелітеральна", visitor.skipped[0][1])


if __name__ == "__main__":
    unittest.main()
