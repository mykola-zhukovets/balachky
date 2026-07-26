# -*- coding: utf-8 -*-
"""Генератор тла «Б з жуком» → assets/ui/bg-tile.png (480×480).

Джерело істини вигляду — затверджений Миколою макет (тло Б): база #2c2718 +
радіальний градієнт, у нього вплетено притишений патерн — мікрофончики, справжній
маскот mascot-512 (3 розміри), навушники, осцилограми, документи, лапки, хвилі,
барси. Кремові елементи (мікрофони/навушники/документи/лапки) — альфа AL=0.085;
темні (хвилі/барси/осцилограми) — AD=0.16; жук — 0.11–0.13. Тайл безшовний за
рахунок повного повторення бази під патерном (елементи не торкаються країв).

Тайл КОМІТИТЬСЯ разом зі скриптом — для відтворюваності; PyInstaller підхоплює
його наявним datas=[("assets","assets")]. Перегенерувати:
    .venv\\Scripts\\python scripts\\gen_bg_tile.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

ROOT = Path(__file__).resolve().parents[1]
MASCOT = ROOT / "assets" / "mascot-512.png"
OUT = ROOT / "assets" / "ui" / "bg-tile.png"

BASE = (44, 39, 24)          # #2c2718
CREAM = (255, 244, 214)
DARK = (0, 0, 0)
AL, AD = 0.085, 0.16         # альфи кремових / темних елементів (тло Б)


def build_tile() -> Image.Image:
    tile = Image.new("RGBA", (480, 480), BASE + (255,))
    # м'який радіальний градієнт зверху-зліва
    grad = Image.new("L", (480, 480), 0)
    gd = ImageDraw.Draw(grad)
    for r in range(480, 0, -4):
        a = int(28 * (1 - r / 480))
        gd.ellipse([144 - r, 96 - r, 144 + r, 96 + r], fill=a)
    tile = Image.alpha_composite(
        tile,
        Image.merge("RGBA", [Image.new("L", (480, 480), 58),
                             Image.new("L", (480, 480), 52),
                             Image.new("L", (480, 480), 34), grad]))

    mascot = Image.open(MASCOT).convert("RGBA")

    def mic(x, y, rot, alpha, s=1.0):
        layer = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        c = CREAM + (int(alpha * 255),)
        ld.rounded_rectangle([25, 8, 39, 34], 7, outline=c, width=3)
        ld.arc([18, 20, 46, 46], 0, 180, fill=c, width=3)
        ld.line([32, 46, 32, 54], fill=c, width=3)
        ld.line([25, 54, 39, 54], fill=c, width=3)
        layer = layer.rotate(rot, expand=True).resize((int(layer.width * s),) * 2)
        tile.alpha_composite(layer, (x, y))

    def waves(x, y, rot, alpha, s=1.0):
        layer = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        c = DARK + (int(alpha * 255),)
        for i, r in enumerate([10, 18, 26]):
            ld.arc([32 - 4 - r // 2, 32 - r, 32 - 4 + r + r // 2, 32 + r],
                   -55, 55, fill=c, width=3)
        layer = layer.rotate(rot, expand=True).resize((int(layer.width * s),) * 2)
        tile.alpha_composite(layer, (x, y))

    def bars(x, y, alpha):
        # ЧЕРЕЗ ЛАЙЕР + alpha_composite (як решта елементів): пряме малювання з
        # альфою по RGBA-тайлу ЗАМІНює пікселі напівпрозорим чорним (пробиває
        # дірки в опуклому тлі), а не змішує. У вебмакеті дірки маскував темніший
        # фон сторінки; для тла застосунку тайл мусить бути повністю непрозорим.
        layer = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        c = DARK + (int(alpha * 255),)
        for i, (dx, h) in enumerate([(0, 8), (8, 18), (16, 12), (24, 22)]):
            ld.rounded_rectangle([dx, 16 - h // 2, dx + 4, 16 + h // 2], 2, fill=c)
        tile.alpha_composite(layer, (x, y - 16))

    def beetle(x, y, size, alpha, rot=0):
        # справжній маскот як водяний знак: зменшений, притишений, знебарвлений
        m = mascot.resize((size, size), Image.LANCZOS)
        m = ImageEnhance.Color(m).enhance(0.55)      # прибрати строкатість
        m = ImageEnhance.Brightness(m).enhance(1.08)
        if rot:
            m = m.rotate(rot, expand=True, resample=Image.BICUBIC)
        a = m.getchannel("A").point(lambda v: int(v * alpha))
        m.putalpha(a)
        tile.alpha_composite(m, (x, y))

    def headphones(x, y, rot, alpha, s=1.0):
        layer = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        c = CREAM + (int(alpha * 255),)
        ld.arc([14, 12, 50, 48], 180, 360, fill=c, width=3)
        ld.rounded_rectangle([11, 30, 21, 46], 4, outline=c, width=3)
        ld.rounded_rectangle([43, 30, 53, 46], 4, outline=c, width=3)
        layer = layer.rotate(rot, expand=True).resize((int(layer.width * s),) * 2)
        tile.alpha_composite(layer, (x, y))

    def wavemeter(x, y, alpha, s=1.0):
        layer = Image.new("RGBA", (72, 32), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        c = DARK + (int(alpha * 255),)
        pts = [(0, 16), (8, 16), (13, 6), (19, 26), (25, 10), (31, 22),
               (37, 14), (43, 18), (50, 16), (72, 16)]
        ld.line(pts, fill=c, width=3, joint="curve")
        layer = layer.resize((int(72 * s), int(32 * s)))
        tile.alpha_composite(layer, (x, y))

    def doc(x, y, rot, alpha):
        layer = Image.new("RGBA", (48, 56), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        c = CREAM + (int(alpha * 255),)
        ld.rounded_rectangle([6, 4, 42, 52], 6, outline=c, width=3)
        for i, w in enumerate([22, 26, 18, 24]):
            ld.line([13, 16 + i * 9, 13 + w, 16 + i * 9], fill=c, width=3)
        layer = layer.rotate(rot, expand=True)
        tile.alpha_composite(layer, (x, y))

    def quotes(x, y, alpha, s=1.0):
        layer = Image.new("RGBA", (56, 40), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        c = DARK + (int(alpha * 255),)
        for dx in (0, 22):
            ld.ellipse([dx + 4, 6, dx + 16, 18], outline=c, width=3)
            ld.arc([dx - 2, 6, dx + 16, 34], 20, 110, fill=c, width=3)
        layer = layer.resize((int(56 * s), int(40 * s)))
        tile.alpha_composite(layer, (x, y))

    # розкладка тла Б (координати/оберти/масштаби — з макета)
    mic(50, 55, -12, AL); waves(330, 70, 0, AD); bars(120, 180, AD)
    beetle(196, 26, 96, 0.13, 8)
    headphones(122, 96, 10, AL)
    mic(270, 190, 10, AL); beetle(392, 210, 78, 0.11, -10)
    wavemeter(196, 260, AD, 1.1)
    waves(40, 300, 8, AD); mic(180, 350, -8, AL); bars(320, 330, AD)
    doc(410, 96, -8, AL)
    quotes(60, 216, AD, 0.9)
    beetle(64, 396, 88, 0.12, 12); waves(238, 440, -6, AD); mic(396, 420, 6, AL)
    headphones(300, 420, -12, AL, 0.9)
    doc(150, 260, 10, AL)
    wavemeter(360, 20, AD, 0.9)
    quotes(250, 120, AD, 0.8)
    return tile.convert("RGBA")


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tile = build_tile()
    tile.save(OUT, "PNG", optimize=True)
    print(f"{OUT.relative_to(ROOT)}  {tile.size[0]}x{tile.size[1]}  "
          f"{OUT.stat().st_size // 1024} КБ")


if __name__ == "__main__":
    main()
