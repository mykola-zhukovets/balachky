"""Вартовий проти «мертвих» Qt-властивостей на GlassButton.

``GlassButton`` (fronts/desktop/glass.py) повністю малює себе сам у
``paintEvent`` і НІКОЛИ не викликає ``super().paintEvent`` — тобто звичайний
QSS-механізм (``QPushButton[accent="true"] {...}``) на нього фізично не
лягає. Єдина властивість, яку GlassButton насправді читає в себе через
``self.property(...)``, — це ``ghost`` (замінник style-класу, який він сам
відтворює руками у малюванні).

Живий тест власника 30.07.2026 (базовий коміт e092484) знайшов, що кнопка
«Обробити нараду» мала ``setProperty("accent", True)``, але лишалась
візуально нерозрізненою — властивість ставилась, та жоден код її не читав.
Незалежний рецензент того ж дня знайшов ще два такі випадки (screen.py,
reverse_dictation.py). Щоб такий дефект наступного разу ловила машина, а не
власник під час живого тесту, цей вартовий:

1. дістає з ``theme.py`` перелік властивостей, які QSS реально стилізує для
   ``QPushButton`` (``accent``, ``ghost``, ``danger``, ``kbfocus``,
   ``primaryAction`` тощо);
2. дістає з ``glass.py`` перелік властивостей, які сам клас ``GlassButton``
   читає через ``self.property(...)`` у своєму тілі (наразі — лише
   ``ghost``);
3. статично (AST, за функціями-скоупами) знаходить усі місця коду, де
   змінній присвоюється ``GlassButton(...)``, а потім ЦІЙ ЖЕ змінній у тому
   самому скоупі викликається ``.setProperty("<стильова-властивість>", ...)``
   для властивості, яку GlassButton не читає.

Властивості поза QSS-переліком (напр. ``mode``, ``kind`` — вони лише кладуть
дані для власного ``.property()``-читання в коді сторінки, а не для QSS) тест
навмисно НЕ чіпає: GlassButton не зобов'язаний їх «розуміти», бо стилю вони
не несуть.
"""
import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTS = ROOT / "fronts" / "desktop"
GLASS_PY = FRONTS / "glass.py"
THEME_PY = FRONTS / "theme.py"

# Директорії/файли, які свідомо не проходимо (тести, скрипти рендеру самого
# glass.py тощо) — лише продакшн-код застосунку.
_SKIP_DIRS = {"__pycache__"}


def _qss_styled_button_properties() -> set[str]:
    """Властивості, які theme.py реально прив'язує до `[prop="true"]`-селектора
    QPushButton/QAbstractButton — тобто мають ефект ЛИШЕ через живий QSS-рушій."""
    text = THEME_PY.read_text(encoding="utf-8")
    props = set(re.findall(
        r'Q(?:PushButton|AbstractButton)\[(\w+)=', text))
    return props


def _glassbutton_read_properties() -> set[str]:
    """Властивості, які клас GlassButton сам читає через self.property(...)."""
    tree = ast.parse(GLASS_PY.read_text(encoding="utf-8"), filename=str(GLASS_PY))
    read = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.ClassDef) and node.name == "GlassButton"):
            for n in ast.walk(node):
                if (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "property"
                        and n.args
                        and isinstance(n.args[0], ast.Constant)):
                    read.add(n.args[0].value)
    return read


def _varname(node: ast.AST):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _varname(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _dead_property_hits(path: Path):
    """(lineno, varname, prop) для кожного GlassButton у файлі, на якому
    поставлено QSS-стильову властивість, яку GlassButton не читає."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return
    styled = _qss_styled_button_properties()
    read_by_glass = _glassbutton_read_properties()

    # Скоуп = тіло функції/методу: GlassButton, присвоєний змінній у одній
    # функції, і .setProperty(...) на тій самій змінній у тій самій функції.
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            glass_vars = set()
            calls = []
            for n in ast.walk(node):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                        and isinstance(n.value.func, ast.Name) \
                        and n.value.func.id == "GlassButton":
                    for t in n.targets:
                        vn = _varname(t)
                        if vn:
                            glass_vars.add(vn)
                if (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "setProperty"
                        and len(n.args) >= 1
                        and isinstance(n.args[0], ast.Constant)):
                    vn = _varname(n.func.value)
                    if vn:
                        calls.append((n.lineno, vn, n.args[0].value))
            for lineno, vn, prop in calls:
                if vn in glass_vars and prop in styled and prop not in read_by_glass:
                    hits.append((lineno, vn, prop))
    return hits


class GlassButtonDeadPropertyTests(unittest.TestCase):
    def test_qss_styled_properties_include_known_examples(self):
        # Захист від тихого зламу самого вартового (напр. якщо theme.py
        # перепишуть без [prop="true"]-нотації): якщо цей набір спорожніє,
        # решта тесту мовчки нічого не перевіряє.
        styled = _qss_styled_button_properties()
        self.assertIn("accent", styled)
        self.assertIn("ghost", styled)

    def test_glassbutton_reads_ghost_only(self):
        # Документує поточний контракт: якщо GlassButton навчиться читати
        # більше властивостей — цей тест підкаже оновити докстрінг вище.
        self.assertEqual(_glassbutton_read_properties(), {"ghost"})

    def test_no_dead_qss_property_on_glassbutton_instances(self):
        """Жоден GlassButton у продакшн-коді не має QSS-стильової властивості
        (accent/danger/kbfocus/primaryAction/...), яку сам клас не читає —
        інакше вона мовчки ігнорується (як було з screen.py й
        reverse_dictation.py до фікса)."""
        offenders = []
        for path in sorted(FRONTS.rglob("*.py")):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path == GLASS_PY:
                continue
            for lineno, varname, prop in (_dead_property_hits(path) or []):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{lineno} "
                    f"{varname}.setProperty({prop!r}, ...) — GlassButton "
                    f"ігнорує цю властивість (paintEvent її не читає)")
        self.assertEqual(
            offenders, [],
            "GlassButton малює себе сам і НЕ бере ці властивості з QSS. "
            "Заміни на звичайний QPushButton зі стилем головної дії "
            "(як зроблено в meeting.py, e092484), інакше кнопка виглядає "
            "як другорядна:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
