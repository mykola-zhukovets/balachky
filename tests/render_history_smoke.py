"""Render-smoke вкладки «Історія» (живий QWidget сторінки історії).

Перевіряє:
  - зауваження 8: панель статистики (зведення + економія) прихована при вході;
    кнопка «Статистика» показує/ховає її (toggle).
  - зауваження 9: пошук по історії матчить і дату запису (як показано в картці),
    а не лише текст розшифровки.
  - аудит 31.07: поведінка порожнього стану за увімкненої та вимкненої історії,
    кнопка увімкнення пам'яті та зникнення порожнього стану після появи першого запису.
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
from fronts.desktop.i18n import set_language  # noqa: E402
from fronts.desktop.pages.history import HistoryPage  # noqa: E402


class MockProfile:
    def __init__(self, p: Path | str) -> None:
        self.history_path = p
        self.memory_enabled = True


class MockCtl:
    def __init__(self, p: Path | str) -> None:
        self.profile = MockProfile(p)
        self.toggled: list[bool] = []

    def toggle_memory(self, on: bool) -> None:
        self.toggled.append(on)
        self.profile.memory_enabled = on


def _write_history(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


class RenderHistorySmokeTests(unittest.TestCase):
    """Димові тести рендерингу та взаємодії зі сторінкою історії."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        set_language("uk")

    def tearDown(self) -> None:
        set_language("uk")

    def test_stats_panel_toggle_and_date_search(self) -> None:
        """Перевірка перемикання панелі статистики та пошуку за датою і текстом."""
        # дві дати, як відрендерить картка: "%d.%m.%Y %H:%M"
        ts_new = time.mktime(time.strptime("2026-07-17 10:00", "%Y-%m-%d %H:%M"))
        ts_old = time.mktime(time.strptime("2026-07-12 09:30", "%Y-%m-%d %H:%M"))
        date_new = time.strftime("%d.%m.%Y", time.localtime(ts_new))  # 17.07.2026
        date_new_short = time.strftime("%d.%m", time.localtime(ts_new))  # 17.07

        with tempfile.TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(
                hp,
                [
                    {"ts": ts_old, "final": "стара розшифровка про яблука"},
                    {"ts": ts_new, "final": "нова розшифровка про груші"},
                ],
            )
            page = HistoryPage(MockCtl(hp))
            try:
                # --- зауваження 8: статистика прихована при вході ---
                self.assertTrue(page._stats_panel.isHidden(), "статистика має бути прихована на старті")
                page._toggle_stats()
                self.assertFalse(page._stats_panel.isHidden(), "клік «Статистика» має показати панель")
                page._toggle_stats()
                self.assertTrue(page._stats_panel.isHidden(), "повторний клік має сховати панель")

                # --- зауваження 9: пошук по даті ---
                page.refresh()
                self.assertEqual(len(page._cards), 2, f"очікувалось 2 картки, є {len(page._cards)}")

                def _visible_texts(query: str) -> list[str]:
                    page._search.setText(query)
                    return [t for card, t, *_ in page._cards if not card.isHidden()]

                vis = _visible_texts(date_new)  # повна дата 17.07.2026
                self.assertEqual(len(vis), 1, f"дата {date_new}: очікувалась 1 картка, {len(vis)}")
                self.assertIn("груші", vis[0], "по даті знайшлась не та картка")

                vis_short = _visible_texts(date_new_short)  # коротка дата 17.07
                self.assertEqual(len(vis_short), 1, f"дата {date_new_short}: {len(vis_short)} карток")

                vis_text = _visible_texts("яблука")  # пошук по тексту ще працює
                self.assertEqual(len(vis_text), 1, "пошук по тексту зламано")
                self.assertIn("яблука", vis_text[0], "пошук по тексту повернув не той запис")

                vis_all = _visible_texts("")  # порожній запит → усі видимі
                self.assertEqual(len(vis_all), 2, "порожній запит має показати всі картки")
            finally:
                page.deleteLater()
                self._app.processEvents()

    def test_empty_state_and_memory_toggle(self) -> None:
        """Перевірка порожнього стану та активації пам'яті через кнопку."""
        ts_new = time.mktime(time.strptime("2026-07-17 10:00", "%Y-%m-%d %H:%M"))

        with tempfile.TemporaryDirectory() as tmp:
            empty_hp = Path(tmp) / "history-empty.jsonl"
            _write_history(empty_hp, [])
            ctl = MockCtl(empty_hp)
            empty_page = HistoryPage(ctl)
            try:
                empty_page.refresh()
                self.assertEqual(
                    empty_page._stack.currentIndex(), 0, "0 записів мали показати порожній стан"
                )
                self.assertEqual(
                    empty_page._empty.button.text(), "", "історія УВІМКНЕНА: кнопки в порожньому стані бути не мусить"
                )

                # вимкнена історія — реальна кнопка «Увімкнути історію», не відсилання
                # у трей (аудит: раніше текст вказував на невірне місце дії)
                ctl.profile.memory_enabled = False
                empty_page.refresh()
                self.assertEqual(empty_page._stack.currentIndex(), 0)
                self.assertTrue(
                    bool(empty_page._empty.button.text()),
                    "історія ВИМКНЕНА: кнопка «Увімкнути історію» мусить бути видима",
                )
                empty_page._empty.button.click()
                self.assertEqual(ctl.toggled, [True], "кнопка мала увімкнути пам'ять через controller")
                self.assertIs(ctl.profile.memory_enabled, True)

                # після увімкнення й появи запису — порожній стан зникає:
                # дописуємо один запис і оновлюємо сторінку.
                _write_history(empty_hp, [{"ts": ts_new, "final": "перший запис"}])
                empty_page.refresh()
                self.assertEqual(
                    empty_page._stack.currentIndex(), 1, "перший запис мав прибрати порожній стан і показати стрічку"
                )
            finally:
                empty_page.deleteLater()
                self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
