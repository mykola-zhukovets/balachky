"""Render-smoke вкладки «Історія» (ПОЗА unittest discover — живий QWidget).

Перевіряє два виправлення з живого тесту Миколи:
  - зауваж. 8: панель статистики (зведення + економія) ПРИХОВАНА при вході;
    кнопка «Статистика» показує/ховає її (toggle).
  - зауваж. 9: пошук по історії матчить і ДАТУ запису (як показано в картці),
    а не лише текст розшифровки.

Запуск: QT_QPA_PLATFORM=offscreen python tests/render_history_smoke.py
(так само підхоплює dev/qa_gate.ps1 як render_*_smoke).
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402


class MockProfile:
    def __init__(self, p):
        self.history_path = p
        self.memory_enabled = True


class MockCtl:
    def __init__(self, p):
        self.profile = MockProfile(p)


def _write_history(path, records):
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


def main() -> int:
    QApplication.instance() or QApplication([])
    from fronts.desktop.i18n import set_language
    from fronts.desktop.pages.history import HistoryPage

    set_language("uk")

    # дві дати, як відрендерить картка: "%d.%m.%Y %H:%M"
    ts_new = time.mktime(time.strptime("2026-07-17 10:00", "%Y-%m-%d %H:%M"))
    ts_old = time.mktime(time.strptime("2026-07-12 09:30", "%Y-%m-%d %H:%M"))
    date_new = time.strftime("%d.%m.%Y", time.localtime(ts_new))   # 17.07.2026
    date_new_short = time.strftime("%d.%m", time.localtime(ts_new))  # 17.07

    with tempfile.TemporaryDirectory() as tmp:
        hp = Path(tmp) / "history.jsonl"
        _write_history(hp, [
            {"ts": ts_old, "final": "стара розшифровка про яблука"},
            {"ts": ts_new, "final": "нова розшифровка про груші"},
        ])
        page = HistoryPage(MockCtl(hp))

        # --- зауваж. 8: статистика прихована при вході ---
        assert page._stats_panel.isHidden(), "статистика має бути прихована на старті"
        page._toggle_stats()
        assert not page._stats_panel.isHidden(), "клік «Статистика» має показати панель"
        page._toggle_stats()
        assert page._stats_panel.isHidden(), "повторний клік має сховати панель"

        # --- зауваж. 9: пошук по даті ---
        page.refresh()
        assert len(page._cards) == 2, f"очікувалось 2 картки, є {len(page._cards)}"

        def _visible_texts(query):
            page._search.setText(query)
            return [t for card, t in page._cards if not card.isHidden()]

        vis = _visible_texts(date_new)               # повна дата 17.07.2026
        assert len(vis) == 1, f"дата {date_new}: очікувалась 1 картка, {len(vis)}"
        assert "груші" in vis[0], "по даті знайшлась не та картка"

        vis_short = _visible_texts(date_new_short)   # коротка дата 17.07
        assert len(vis_short) == 1, f"дата {date_new_short}: {len(vis_short)} карток"

        vis_text = _visible_texts("яблука")          # пошук по тексту ще працює
        assert len(vis_text) == 1 and "яблука" in vis_text[0], "пошук по тексту зламано"

        vis_all = _visible_texts("")                 # порожній запит → усі видимі
        assert len(vis_all) == 2, "порожній запит має показати всі картки"

        page.deleteLater()

    print("RENDER HISTORY SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
