"""Render-smoke нових UI-віджетів feature/ux-center (ПОЗА unittest discover).

Запуск: QT_QPA_PLATFORM=offscreen python tests/render_uxcenter_smoke.py
Перевіряє, що будуються без винятків обома мовами: плаваюча пілюля (стани,
drag-колбеки, скидання позиції), шпаргалка гарячих клавіш, дашборд Статистики.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    from fronts.desktop.i18n import set_language
    from fronts.desktop.pill import FloatingPill
    from fronts.desktop.cheatsheet import HotkeyCheatSheet
    from fronts.desktop.pages.history import HistoryPage

    class MockProfile:
        def __init__(self, p):
            self.history_path = p
            self.memory_enabled = True

    class MockCtl:
        def __init__(self, p):
            self.profile = MockProfile(p)

    for lang in ("uk", "en"):
        set_language(lang)
        moved = []
        pill = FloatingPill(on_moved=lambda x, y: moved.append((x, y)),
                            on_reset=lambda: moved.append("reset"))
        pill.apply_saved_position((120, 120))
        for state in ("recording", "busy", "idle"):
            pill.set_state(state)
        pill.reset_to_default()

        sheet = HotkeyCheatSheet(lambda: [
            ("hotkeys_dictate", "Ctrl + Shift + Space"),
            ("hotkeys_mode", "Hold"),
            ("hotkeys_mouse", "X2"),
        ])
        sheet.refresh()

        with tempfile.TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            now = time.time()
            recs = [{"ts": now - k * 86400, "final": "один два три"}
                    for k in range(3)]
            hp.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                    for r in recs) + "\n", encoding="utf-8")
            page = HistoryPage(MockCtl(hp))
            page._update_summary()
            assert page._saved_num.text(), "дашборд не заповнився"
            assert page._streak_num.text(), "стрік не заповнився"
        print(f"[{lang}] pill + cheatsheet + stats dashboard OK")
    print("RENDER SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
