"""Вартовий: жоден тест не ходить у справжню мережу непомічено.

Ідея (issue #36): CI-раннер може бути офлайн або мати нестабільну мережу —
тест, що насправді відкриває сокет чи HTTP-з'єднання, стає недетермінованим.
Скануємо AST усіх ``tests/*.py`` і шукаємо прямі мережеві виклики
(``socket.create_connection``, ``socket.gethostbyname``,
``urllib.request.urlopen``, ``requests.get``/``requests.post``,
``http.client.HTTPConnection``) у тілі БУДЬ-ЯКОЇ функції файла — тестів,
хелперів, ``setUp`` (сама причина issue #36 сиділа в хелпері). Імена
приводяться до канонічних через імпорти файла: ``import requests as rq`` і
``from urllib.request import urlopen as uo`` не ховають виклик.

Виклик не порушення, якщо його прикрито підміною: ``patch("…urlopen")``,
``patch.object(<модуль>, "urlopen")`` чи присвоєння ``…urlopen = fake`` — у
тій самій функції (декоратор, ``with``, тіло) або на рівні класу
(``setUp``/``setUpClass``/декоратори класу). Прикриттям вважається лише ціль,
що закінчується на те саме ім'я (``patch("app.get_config")`` не прикриває
``requests.get``).

ALLOWLIST — файли, де пряма мережа виправдана (з поясненням кожного). Зараз
порожній: усі відомі мережеві виклики в тестах уже підмінені."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

#: канонічне dotted-ім'я мережевого виклику -> коротке ім'я для звірки з підміною
_NETWORK_SIGNATURES = {
    "socket.create_connection": "create_connection",
    "socket.gethostbyname_ex": "gethostbyname_ex",
    "socket.gethostbyname": "gethostbyname",
    "urllib.request.urlopen": "urlopen",
    "requests.get": "get",
    "requests.post": "post",
    "http.client.HTTPConnection": "HTTPConnection",
}

#: файл -> обґрунтування, чому пряма мережа тут виправдана. Порожній —
#: жодного такого місця не знайдено (кращий стан, ніж allowlist “про запас”).
ALLOWLIST: dict[str, str] = {}

_PATCH_NAMES = ("patch", "mock.patch", "unittest.mock.patch")
_PATCH_OBJECT_NAMES = tuple(n + ".object" for n in _PATCH_NAMES)


def _dotted_name(node: ast.AST) -> str | None:
    """``a.b.c`` для ланцюжка Name/Attribute, інакше None (виклик результату
    іншого виклику, індекс тощо)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Локальне ім'я -> канонічне dotted-ім'я за імпортами файла (усі рівні)."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _canonical(dotted: str, aliases: dict[str, str]) -> str:
    head, _, rest = dotted.partition(".")
    base = aliases.get(head, head)
    return f"{base}.{rest}" if rest else base


def _signature_for(canonical: str) -> str | None:
    for signature in _NETWORK_SIGNATURES:
        if canonical == signature or canonical.endswith("." + signature):
            return signature
    return None


def _coverage(node: ast.AST) -> set[str]:
    """Короткі імена, які підміняє піддерево: ``patch("a.b.name")`` ->
    ``name``; ``patch.object(x, "name")`` -> ``name``; ``a.b.name = fake`` ->
    ``name``."""
    covered: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            dotted = _dotted_name(sub.func)
            if dotted in _PATCH_NAMES and sub.args \
                    and isinstance(sub.args[0], ast.Constant) \
                    and isinstance(sub.args[0].value, str):
                covered.add(sub.args[0].value.rsplit(".", 1)[-1])
            elif dotted in _PATCH_OBJECT_NAMES and len(sub.args) >= 2 \
                    and isinstance(sub.args[1], ast.Constant) \
                    and isinstance(sub.args[1].value, str):
                covered.add(sub.args[1].value)
        elif isinstance(sub, ast.Assign):
            for target in sub.targets:
                dotted = _dotted_name(target)
                if dotted and "." in dotted:
                    covered.add(dotted.rsplit(".", 1)[-1])
    return covered


def _find_violations(source: str, filename: str) -> list[tuple[str, int, str]]:
    """Список (файл, рядок, dotted-ім'я) для непідмінених мережевих викликів
    у будь-якій функції ``source``."""
    tree = ast.parse(source, filename=filename)
    aliases = _import_aliases(tree)
    violations: list[tuple[str, int, str]] = []

    def scan_function(func: ast.AST, inherited: set[str]) -> None:
        covered = set(inherited) | _coverage(func)
        for sub in ast.walk(func):
            if not isinstance(sub, ast.Call):
                continue
            dotted = _dotted_name(sub.func)
            if dotted is None:
                continue
            signature = _signature_for(_canonical(dotted, aliases))
            if signature and _NETWORK_SIGNATURES[signature] not in covered:
                violations.append((filename, sub.lineno, dotted))

    def scan_body(body: list[ast.stmt], inherited: set[str]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan_function(node, inherited)
            elif isinstance(node, ast.ClassDef):
                class_cover = set(inherited)
                for dec in node.decorator_list:
                    class_cover |= _coverage(dec)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and item.name in ("setUp", "setUpClass"):
                        class_cover |= _coverage(item)
                scan_body(node.body, class_cover)

    scan_body(tree.body, set())
    return violations


class NoLiveNetworkInTestsTests(unittest.TestCase):
    """Перевіряє реальні файли tests/*.py та саму логіку сканера."""

    def test_no_unmocked_network_calls_in_test_files(self) -> None:
        all_violations: list[tuple[str, int, str]] = []
        for path in sorted(TESTS_DIR.glob("*.py")):
            if path.name in ALLOWLIST:
                continue
            source = path.read_text(encoding="utf-8")
            all_violations.extend(_find_violations(source, path.name))
        self.assertEqual(
            [], all_violations,
            "непідмінені мережеві виклики в тестах (файл, рядок, виклик): "
            f"{all_violations}")

    def test_direct_unmocked_call_is_a_violation(self) -> None:
        source = (
            "import socket\n"
            "def test_x():\n"
            "    socket.create_connection((\"1.1.1.1\", 53))\n"
        )
        self.assertEqual([("fake.py", 3, "socket.create_connection")],
                         _find_violations(source, "fake.py"))

    def test_patched_call_is_allowed(self) -> None:
        source = (
            "from unittest.mock import patch\n"
            "def test_x():\n"
            "    with patch(\"socket.create_connection\"):\n"
            "        socket.create_connection((\"1.1.1.1\", 53))\n"
        )
        self.assertEqual([], _find_violations(source, "fake.py"))

    def test_helper_and_setup_are_scanned_too(self) -> None:
        # Причина issue #36 сиділа в хелпері, не в тесті.
        source = (
            "import urllib.request\n"
            "def _helper():\n"
            "    return urllib.request.urlopen(\"http://example.com\")\n"
            "class T:\n"
            "    def setUp(self):\n"
            "        urllib.request.urlopen(\"http://example.com\")\n"
        )
        self.assertEqual([("fake.py", 3, "urllib.request.urlopen"),
                          ("fake.py", 6, "urllib.request.urlopen")],
                         _find_violations(source, "fake.py"))

    def test_import_aliases_are_resolved(self) -> None:
        source = (
            "import requests as rq\n"
            "from urllib.request import urlopen as uo\n"
            "from urllib import request\n"
            "def test_x():\n"
            "    rq.get(\"http://a\")\n"
            "    uo(\"http://b\")\n"
            "    request.urlopen(\"http://c\")\n"
        )
        self.assertEqual([("fake.py", 5, "rq.get"), ("fake.py", 6, "uo"),
                          ("fake.py", 7, "request.urlopen")],
                         _find_violations(source, "fake.py"))

    def test_bare_local_get_is_not_network(self) -> None:
        source = (
            "def get(key):\n"
            "    return {}[key]\n"
            "def test_x():\n"
            "    get(\"a\")\n"
            "    store.post(\"b\")\n"
        )
        self.assertEqual([], _find_violations(source, "fake.py"))

    def test_patch_object_and_assignment_cover_the_call(self) -> None:
        source = (
            "from unittest import mock\n"
            "import mod\n"
            "def test_x():\n"
            "    with mock.patch.object(mod.urllib.request, \"urlopen\", fake):\n"
            "        mod.urllib.request.urlopen(\"http://a\")\n"
            "def test_y():\n"
            "    mod.urllib.request.urlopen = fake\n"
            "    mod.urllib.request.urlopen(\"http://a\")\n"
        )
        self.assertEqual([], _find_violations(source, "fake.py"))

    def test_class_level_patch_in_setup_covers_methods(self) -> None:
        source = (
            "from unittest.mock import patch\n"
            "import socket\n"
            "class T:\n"
            "    def setUp(self):\n"
            "        self.p = patch(\"socket.create_connection\")\n"
            "        self.p.start()\n"
            "    def test_x(self):\n"
            "        socket.create_connection((\"1.1.1.1\", 53))\n"
        )
        self.assertEqual([], _find_violations(source, "fake.py"))

    def test_unrelated_patch_does_not_cover(self) -> None:
        source = (
            "from unittest.mock import patch\n"
            "import requests\n"
            "def test_x():\n"
            "    with patch(\"app.helpers.get_config\"):\n"
            "        requests.get(\"http://a\")\n"
        )
        self.assertEqual([("fake.py", 5, "requests.get")],
                         _find_violations(source, "fake.py"))


if __name__ == "__main__":
    unittest.main()
