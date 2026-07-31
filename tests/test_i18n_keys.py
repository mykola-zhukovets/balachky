"""Вартові повноти ключів локалізації, які використовує UI.

Статичний вартовий знаходить усі виклики ``tr("literal")`` у Python-файлах
``fronts/``. Виклики з обчислюваним першим аргументом він навмисно не вважає
статичними: їхній набір значень загалом неможливо довести лише з AST виклику.

Дві наявні конкатенації з обмеженим доменом перевіряються окремо:
``set_model_idle_`` бере суфікси з кортежу ``keys`` у ``_model_group()``, а
``set_mouse_`` — з whitelist кнопок у ``DesktopApp.hotkey_bindings()``.
Тест дістає обидва домени з AST робочого коду, тому зміна допустимих значень
без відповідного перекладу теж почервонить вартового.
"""

import ast
import tokenize
import unittest
from pathlib import Path

from fronts.desktop.i18n import STRINGS


ROOT = Path(__file__).resolve().parents[1]
FRONTS = ROOT / "fronts"


def _parse(path: Path) -> ast.AST:
    with tokenize.open(path) as source:
        return ast.parse(source.read(), filename=str(path))


def _is_tr_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name) and node.func.id == "tr"
            or isinstance(node.func, ast.Attribute) and node.func.attr == "tr"
        )
        and bool(node.args)
    )


def _literal_tr_calls():
    for path in sorted(FRONTS.rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if (
                _is_tr_call(node)
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                yield path.relative_to(ROOT).as_posix(), node.lineno, node.args[0].value


def _dynamic_tr_call(node: ast.AST, prefix: str, variable: str) -> bool:
    if not _is_tr_call(node) or not isinstance(node.args[0], ast.BinOp):
        return False
    arg = node.args[0]
    return (
        isinstance(arg.op, ast.Add)
        and isinstance(arg.left, ast.Constant)
        and arg.left.value == prefix
        and isinstance(arg.right, ast.Name)
        and arg.right.id == variable
    )


def _function_with_dynamic_call(path: Path, prefix: str, variable: str) -> ast.FunctionDef:
    tree = _parse(path)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(_dynamic_tr_call(child, prefix, variable) for child in ast.walk(node))
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one function building {prefix!r} + {variable}, got {len(matches)}"
        )
    return matches[0]


def _string_tuple(node: ast.AST):
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    values = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        values.append(item.value)
    return tuple(values)


def _idle_suffixes() -> tuple:
    path = FRONTS / "desktop" / "pages" / "settings.py"
    function = _function_with_dynamic_call(path, "set_model_idle_", "key")
    matches = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "keys"
               for target in node.targets):
            values = _string_tuple(node.value)
            if values is not None:
                matches.append(values)
    if len(matches) != 1:
        raise AssertionError(f"expected one literal idle keys tuple, got {len(matches)}")
    return matches[0]


def _mouse_suffixes() -> tuple:
    path = FRONTS / "desktop" / "app.py"
    function = _function_with_dynamic_call(path, "set_mouse_", "btn")
    matches = []
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "btn"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and len(node.comparators) == 1
        ):
            values = _string_tuple(node.comparators[0])
            if values is not None:
                matches.append(values)
    if len(matches) != 1:
        raise AssertionError(f"expected one literal mouse-button whitelist, got {len(matches)}")
    return matches[0]


def _missing_languages(keys):
    return {
        key: sorted(lang for lang, strings in STRINGS.items() if key not in strings)
        for key in sorted(set(keys))
        if any(key not in strings for strings in STRINGS.values())
    }


class I18nKeyCoverageTests(unittest.TestCase):
    def test_every_static_tr_key_exists_in_all_languages(self):
        calls = list(_literal_tr_calls())
        missing = _missing_languages(key for _path, _line, key in calls)
        locations = {
            key: [f"{path}:{line}" for path, line, found in calls if found == key]
            for key in missing
        }
        self.assertEqual(
            missing,
            {},
            f"static tr() keys missing by language: {missing}; calls: {locations}",
        )

    def test_bounded_dynamic_tr_keys_exist_in_all_languages(self):
        dynamic_keys = {
            *("set_model_idle_" + suffix for suffix in _idle_suffixes()),
            *("set_mouse_" + suffix for suffix in _mouse_suffixes()),
        }
        self.assertEqual(
            _missing_languages(dynamic_keys),
            {},
            "bounded dynamic tr() combinations are missing translations",
        )


if __name__ == "__main__":
    unittest.main()
