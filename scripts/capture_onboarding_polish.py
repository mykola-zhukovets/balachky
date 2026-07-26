"""Скрипт знімання живих скріншотів полірованого майстра першого запуску та меню підтримки.

Платформа: QT_QPA_PLATFORM=windows (справжні шрифти Segoe UI, теми Balachky).
"""
import os
os.environ["QT_QPA_PLATFORM"] = "windows"

import sys
import time
from pathlib import Path
from PySide6.QtWidgets import QApplication, QToolButton, QMenu, QPushButton
from PySide6.QtCore import QTimer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fronts.desktop import i18n, theme
from fronts.desktop.onboarding import FirstRunWizard
from fronts.desktop.pages.settings import show_support_menu

out_dir = ROOT / "docs" / "screenshots" / "onboarding_polish"
out_dir.mkdir(parents=True, exist_ok=True)

app = QApplication.instance() or QApplication([])
i18n.set_language("uk")
theme.load_fonts()
theme.apply_theme(app, night=False)

# 1. Знімати кроки майстра з живою темою
wiz = FirstRunWizard()
wiz.show()
app.processEvents()

for step in range(wiz._stack.count()):
    wiz._stack.setCurrentIndex(step)
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()
    pix = wiz.grab()
    path = out_dir / f"onboarding_step_{step+1}_uk.png"
    pix.save(str(path))
    print(f"Saved step {step+1}: {path}")

# 1б. Знімати фокус-стан кнопки майстра
wiz._stack.setCurrentIndex(1)
app.processEvents()
btn_change = wiz.findChild(QPushButton, "common_change")
if not btn_change:
    for b in wiz.findChildren(QPushButton):
        if b.text():
            btn_change = b
            break
if btn_change:
    btn_change.setFocus()
    app.processEvents()
    time.sleep(0.1)
    app.processEvents()
    pix_focus = wiz.grab()
    path_focus = out_dir / "onboarding_step_2_focused_uk.png"
    pix_focus.save(str(path_focus))
    print(f"Saved focus state: {path_focus}")

wiz.close()
wiz.deleteLater()
app.processEvents()

# 2. Знімати відкрите меню підтримки (реальний show_support_menu + grab QMenu)
wiz2 = FirstRunWizard()
wiz2.show()
app.processEvents()
wiz2._stack.setCurrentIndex(0)
app.processEvents()

support_btn = wiz2.findChild(QToolButton, "authorSupportLink")
if support_btn:
    def _capture_open_menu():
        pop = app.activePopupWidget()
        if not pop:
            menus = [w for w in app.topLevelWidgets() if isinstance(w, QMenu)]
            if menus:
                pop = menus[0]
        if pop:
            pix_menu = pop.grab()
            path_menu = out_dir / "onboarding_support_menu_open_uk.png"
            pix_menu.save(str(path_menu))
            print(f"Saved real open support menu ({pix_menu.width()}x{pix_menu.height()}): {path_menu}")
            pop.close()

    QTimer.singleShot(200, _capture_open_menu)
    show_support_menu(support_btn)
    app.processEvents()

wiz2.close()
wiz2.deleteLater()
app.processEvents()
print("Всі скріншоти успішно згенеровано.")
