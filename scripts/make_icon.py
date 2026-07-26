"""Генерація assets/balachky.ico — ГІБРИД (вердикт Миколи 11.07 по 99-icon16-compare):
16/24 px = флет-мікрофон (маскот на цих розмірах пливе у пляму),
32-256 px = жук-маскот з assets/mascot-512.png (LANCZOS).

Фолбек (mascot-512.png відсутній): старий флет-рендер — золотий мікрофон
на круглій підложці «Balachky» через Qt. Pillow — dev-залежність ЛИШЕ цього
скрипта, у runtime-requirements її нема.

Запуск:  .venv\\Scripts\\python scripts\\make_icon.py
"""
import io
import os
import sys
from pathlib import Path

# offscreen: без вікон і без DPI-масштабування (інакше гліф пливе на 125/150%)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "balachky.ico"
MASCOT = ROOT / "assets" / "mascot-512.png"
SIZES = [16, 24, 32, 48, 64, 128, 256]
BG = "#2E2A1F"      # theme.DEEP — глибша панель «Balachky»
GOLD = "#F39200"    # theme.GOLD — єдиний акцент


def render_mascot(src: Image.Image, size: int) -> Image.Image:
    """Кадр із маскота. 16/24: підсилити читабельність — UnsharpMask + легкий
    autocontrast ДО LANCZOS-даунскейла і делікатний UnsharpMask після (підібрано
    візуально по сітці варіантів). Обробка лише по RGB — альфу не чіпаємо,
    інакше autocontrast «роз'їдає» прозорі кути."""
    img = src
    if size <= 24:
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        rgb = ImageOps.autocontrast(rgb, cutoff=1)
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=4, percent=160, threshold=2))
        img = Image.merge("RGBA", (*rgb.split(), a))
    img = img.resize((size, size), Image.LANCZOS)
    if size <= 24:
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b)).filter(
            ImageFilter.UnsharpMask(radius=1, percent=80, threshold=0))
        img = Image.merge("RGBA", (*rgb.split(), a))
    return img


def render_flat(size: int) -> Image.Image:
    """Фолбек-кадр: кругла підложка + мікрофон по центру (Qt-рендер)."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QPainter, QColor, QPixmap
    from PySide6.QtCore import Qt, QBuffer
    import qtawesome as qta

    _app = QApplication.instance() or QApplication(sys.argv)
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(BG))
    p.setPen(Qt.NoPen)
    p.drawEllipse(0, 0, size, size)
    glyph = max(1, round(size * 0.56))
    ipm = qta.icon("fa6s.microphone", color=GOLD).pixmap(glyph, glyph)
    p.drawPixmap((size - ipm.width()) // 2, (size - ipm.height()) // 2, ipm)
    p.end()
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.ReadWrite)
    pm.save(buf, "PNG")
    img = Image.open(io.BytesIO(bytes(buf.data())))
    img.load()
    return img


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if MASCOT.exists():
        src = Image.open(MASCOT).convert("RGBA")
        # тісніший кроп ДЛЯ ІКОНКИ: жук заповнює кадр помітніше (у таскбарі
        # був дрібний через поля кола-монети). mascot-512.png лишається цілим —
        # у шапці сайдбара повна «монета» виглядає добре
        w, h = src.size
        icon_src = src.crop((int(w * 0.11), int(h * 0.08),
                             int(w * 0.89), int(h * 0.96)))
        frames = [render_flat(s) if s <= 24 else render_mascot(icon_src, s)
                  for s in SIZES]
        mode = "гібрид: флет 16/24 + маскот 32-256 (тісний кроп)"
    else:
        frames = [render_flat(s) for s in SIZES]
        mode = "флет-фолбек (нема mascot-512.png)"
    base = frames[-1]  # 256 — базовий кадр .ico
    base.save(OUT, format="ICO",
              sizes=[(s, s) for s in SIZES],
              append_images=frames[:-1])
    print(f"OK ({mode}): {OUT} ({OUT.stat().st_size} байт)")


if __name__ == "__main__":
    main()
