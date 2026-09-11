"""Render-smoke нових UI-віджетів feature/ux-center.

Перевіряє, що будуються без винятків обома мовами (uk, en):
  - плаваюча пілюля FloatingPill (стани, drag-колбеки, скидання позиції);
  - шпаргалка гарячих клавіш HotkeyCheatSheet;
  - дашборд статистики на сторінці історії HistoryPage.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402
from fronts.desktop.cheatsheet import HotkeyCheatSheet  # noqa: E402
from fronts.desktop.i18n import set_language  # noqa: E402
from fronts.desktop.pages.history import HistoryPage  # noqa: E402
from fronts.desktop.pill import FloatingPill  # noqa: E402


class MockProfile:
    def __init__(self, p: Path | str) -> None:
        self.history_path = p
        self.memory_enabled = True


class MockCtl:
    def __init__(self, p: Path | str) -> None:
        self.profile = MockProfile(p)


class RenderUxCenterSmokeTests(unittest.TestCase):
    """Димові тести віджетів UX-центру двома мовами."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        set_language("uk")

    def test_uxcenter_widgets_render_multilang(self) -> None:
        """Перевірка ініціалізації та оновлення віджетів для мов “uk” та “en”."""
        for lang in ("uk", "en"):
            with self.subTest(lang=lang):
                set_language(lang)
                moved: list[tuple[int, int] | str] = []
                pill = FloatingPill(
                    on_moved=lambda x, y: moved.append((x, y)),
                    on_reset=lambda: moved.append("reset"),
                )
                try:
                    pill.apply_saved_position((120, 120))
                    for state in ("recording", "busy", "idle"):
                        pill.set_state(state)
                    pill.reset_to_default()
                finally:
                    pill.deleteLater()

                sheet = HotkeyCheatSheet(
                    lambda: [
                        ("hotkeys_dictate", "Ctrl + Shift + Space"),
                        ("hotkeys_mode", "Hold"),
                        ("hotkeys_mouse", "X2"),
                    ]
                )
                try:
                    sheet.refresh()
                    self.assertEqual(sheet._rows_host.count(), 3,
                                     "шпаргалка не наповнилась рядками провайдера")
                finally:
                    sheet.deleteLater()

                with tempfile.TemporaryDirectory() as tmp:
                    hp = Path(tmp) / "history.jsonl"
                    now = time.time()
                    recs = [
                        {"ts": now - k * 86400, "final": "один два три"}
                        for k in range(3)
                    ]
                    hp.write_text(
                        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                        encoding="utf-8",
                    )
                    page = HistoryPage(MockCtl(hp))
                    try:
                        page._update_summary()
                        # 3 записи по 3 слова три дні поспіль. Порівнювати з tr(...) не можна
                        # (вартовий test_i18n_tautology_lint: зламаний ключ ламає обидві
                        # сторони однаково), тож перевіряємо ЧИСЛА і те, що плейсхолдер “—”
                        # з конструктора замінено.
                        from fronts.desktop.pages import history as history_mod
                        mins = round(history_mod.estimate_saved_minutes(9))
                        saved, streak = page._saved_num.text(), page._streak_num.text()
                        self.assertNotEqual(saved, "—", "дашборд не оновився")
                        self.assertIn(str(mins), saved)
                        self.assertNotEqual(streak, "—", "стрік не оновився")
                        self.assertIn("3", streak)
                    finally:
                        page.deleteLater()

                self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
