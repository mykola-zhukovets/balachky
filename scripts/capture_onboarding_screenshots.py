"""Скрипт зйомок живих скріншотів усіх кроків FirstRunWizard (uk) у реальному вікні."""
import os
os.environ["QT_QPA_PLATFORM"] = "windows"

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from fronts.desktop import i18n
from fronts.desktop.onboarding import FirstRunWizard
import fronts.desktop.theme as theme

OUT_DIR = ROOT / "docs" / "screenshots" / "onboarding"
OUT_DIR.mkdir(parents=True, exist_ok=True)

def capture_steps():
    app = QApplication.instance() or QApplication([])
    i18n.set_language("uk")
    theme.apply_theme(app, night=False)

    wiz = FirstRunWizard()
    wiz.resize(620, 460)
    wiz.show()

    # Даємо Qt час на рендер
    for _ in range(10):
        app.processEvents()
        time.sleep(0.05)

    step_names = [
        "step1_welcome",
        "step2_model",
        "step3_language",
        "step4_voice",
        "step5_download",
        "step6_gpu",
    ]

    captured_paths = []
    total = wiz._stack.count()
    for idx in range(total):
        wiz._stack.setCurrentIndex(idx)
        if idx == 3:
            wiz._update_voice_page_state()
        wiz._sync_nav()
        for _ in range(5):
            app.processEvents()
            time.sleep(0.03)

        name = step_names[idx] if idx < len(step_names) else f"step{idx+1}"
        img = wiz.grab()
        out_path = OUT_DIR / f"{name}_uk.png"
        img.save(str(out_path))
        captured_paths.append(out_path)
        print(f"Збережено скріншот: {out_path}")

    # --- Спеціальні кадри 3-х станів кроку Озвучення ---
    # 1) Стан 1: нема голосу (дефолт)
    wiz._stack.setCurrentIndex(3)
    wiz._update_voice_page_state()
    wiz._sync_nav()
    for _ in range(5):
        app.processEvents(); time.sleep(0.03)
    p1 = OUT_DIR / "step4_voice_novoice_uk.png"
    wiz.grab().save(str(p1))
    captured_paths.append(p1)

    # 2) Стан 2: у процесі завантаження
    wiz._voice_status.setText("З'єднання...")
    wiz._voice_bar.show()
    wiz._voice_bar.setRange(0, 0)
    wiz._voice_dl_btn.hide()
    wiz._voice_skip_btn.hide()
    wiz._voice_cancel_btn.show()
    for _ in range(5):
        app.processEvents(); time.sleep(0.03)
    p2 = OUT_DIR / "step4_voice_downloading_uk.png"
    wiz.grab().save(str(p2))
    captured_paths.append(p2)

    # 3) Стан 3: офлайн / збій мережі
    wiz._on_voice_failed("Помилка з'єднання з мережею")
    for _ in range(5):
        app.processEvents(); time.sleep(0.03)
    p3 = OUT_DIR / "step4_voice_offline_uk.png"
    wiz.grab().save(str(p3))
    captured_paths.append(p3)

    # 4) Шлях «завантажено / перехід дальше -> Назад -> кнопки живі»
    wiz._stack.setCurrentIndex(4)     # крок 5 (завантаження)
    wiz._sync_nav()
    wiz._go_back()                    # повернення Назад на крок 4 (Озвучення)
    for _ in range(5):
        app.processEvents(); time.sleep(0.03)
    p4 = OUT_DIR / "step4_voice_back_from_step5_uk.png"
    wiz.grab().save(str(p4))
    captured_paths.append(p4)

    wiz.close()
    wiz.deleteLater()
    return captured_paths

if __name__ == "__main__":
    capture_steps()
