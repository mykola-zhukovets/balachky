"""Дешевий статичний лінт дизайн-канону (docs/DESIGN-SYSTEM.md §5.2).

Стереже два інваріанти, які сьогодні чисті й мають лишитись чистими:
  1. QSS у theme.py визначає рівно чотири варіанти кнопки — accent / ghost /
     danger / базовий (жодного нового QPushButton[primary|secondary|…]).
  2. У коді fronts/desktop не з'являються ad-hoc варіанти кнопок через
     setProperty("primary"/"secondary"/"destructive"/…).

`danger` додано за рішенням дизайн-власника (аудит Миколи 22.07, DESIGN-SYSTEM.md
§1.5): вторинна деструктив-кнопка з тонкою рамкою і СТРИМАНИМ теракотом
(DANGER_MUTED #CF7B62) — НЕ зарезервованим ALERT-червоним і завжди в парі з
QMessageBox-підтвердженням. Це не «кричуща червона кнопка», якої канон уникав.

Суто рядковий скан, без Qt і без імпорту застосунку — швидко й без побічних дій.
Запуск вручну:  python -m unittest tests.test_design_lint
"""
import re
import unittest
from pathlib import Path

_DESKTOP = Path(__file__).resolve().parent.parent / "fronts" / "desktop"
_THEME = _DESKTOP / "theme.py"
_MEETING = _DESKTOP / "pages" / "meeting.py"

# Канонічні варіанти-property кнопки (базовий = без property).
ALLOWED_BUTTON_VARIANTS = {"accent", "ghost", "danger"}

# Динамічні СТАНИ (не варіанти вигляду): їх виставляє код у рантаймі, вони
# комбінуються з будь-яким варіантом і нового вигляду кнопки не вводять.
# kbfocus — рамка фокуса лише при клавіатурній навігації (не після кліку мишею).
STATE_PROPS = {"kbfocus"}

# Property-літерали, що означали б заборонений «новий варіант кнопки».
FORBIDDEN_BUTTON_PROPS = {"primary", "secondary", "destructive"}


class DesignLint(unittest.TestCase):
    def test_qss_defines_only_canonical_button_variants(self):
        """theme.py QSS не вводить нових QPushButton[<variant>] поза каноном."""
        qss = _THEME.read_text(encoding="utf-8")
        variants = set(re.findall(r'QPushButton\[([a-z]+)="true"\]', qss)) - STATE_PROPS
        self.assertEqual(
            variants, ALLOWED_BUTTON_VARIANTS,
            "theme.py має визначати рівно {accent, ghost, danger} варіанти кнопки; "
            f"знайдено: {sorted(variants)}. Новий варіант → онови DESIGN-SYSTEM.md §1.",
        )

    def test_no_adhoc_button_variant_properties(self):
        """Ніде в fronts/desktop немає setProperty('danger'/'primary'/…)."""
        offenders = []
        for py in _DESKTOP.rglob("*.py"):
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                m = re.search(r'setProperty\(\s*"([a-z]+)"', line)
                if m and m.group(1) in FORBIDDEN_BUTTON_PROPS:
                    offenders.append(f"{py.name}:{i}: {m.group(1)}")
        self.assertEqual(
            offenders, [],
            "Заборонений ad-hoc варіант кнопки через property (канон — accent/ghost/"
            f"danger/базовий, DESIGN-SYSTEM.md §1): {offenders}",
        )


class MeetingDialogLint(unittest.TestCase):
    """Сторінка «Нарада» показує повідомлення лише через кастомні хелпери з
    золотою іконкою й локалізованими кнопками (_meeting_confirm/_meeting_warn/
    _meeting_info поверх _show_meeting_box) — не голими QMessageBox.question/
    warning/information із системними іконками й англійськими «Yes/No/OK».

    Виняток — визначення самих хелперів (_show_meeting_box будує QMessageBox
    вручну): скануємо лише прямі виклики .question(/.warning(/.information(.
    """

    def test_no_bare_qmessagebox_calls_in_meeting(self):
        offenders = []
        pat = re.compile(r'QMessageBox\.(question|warning|information)\(')
        for i, line in enumerate(_MEETING.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                offenders.append(f"meeting.py:{i}: {line.strip()[:60]}")
        self.assertEqual(
            offenders, [],
            "Голий QMessageBox на сторінці «Нарада» — переведи на _meeting_confirm/"
            f"_meeting_warn/_meeting_info (золота іконка, локалізовані кнопки): {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
