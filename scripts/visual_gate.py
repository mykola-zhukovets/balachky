"""Візуальний гейт «Балачок»: детектор обрізаного тексту й ламаної верстки.

Вимога Миколи: «я бачу обрізані кнопки, а автоматика — ні; зробити, щоб бачила».
Скрипт будує ЖИВЕ головне вікно (сайдбар-хром + усі сторінки навігації +
досяжні діалоги), обходить кожен видимий текстовий контрол і геометрично
перевіряє, чи вміщається його текст. Сайдбар (nav-кнопки, слоган, рядок версії) —
СУСІД стека сторінок, не «сторінка», тож скануємо його окремим прогоном
(run_language). Обидві мови UI (uk+en). Вивід — JSON-звіт + людський підсумок.

ЧОМУ НЕ offscreen: offscreen-QPA на Windows не бере системні шрифти (Segoe UI) —
метрики брешуть, детектор давав би хибні вердикти. Тому ФІКСУЄМО платформу
`windows` ДО будь-яких Qt-імпортів (той самий канон, що в scripts/screenshots.py).
Модулі tests/render_*_smoke на імпорті роблять setdefault("QT_QPA_PLATFORM",
"offscreen") — наш ранній set їх переважує (setdefault не чіпає вже задане).

Формули перевірок — з розвідки 2026-07-22-RESEARCH-Qt-візуальний-контроль:
  • text_clipped  — QFontMetrics.horizontalAdvance(text) > доступна ширина
                    (для стокових кнопок — style().subElementRect(SE_*Contents),
                     QSS-padding уже враховано проксі-стилем QStyleSheetStyle);
  • text_clipped_v — QLabel(wordWrap) нижчий за heightForWidth (текст зрізано);
  • smaller_than_hint — size < minimumSizeHint (підхід Squish, грубіший сигнал);
  • zero_size     — видимий контрол із текстом, але 0 по ширині/висоті;
  • overlap       — два текстові контроли-сусіди накладаються геометрично.

ШИРИНИ: прохід іде по КІЛЬКОХ ширинах вікна (GATE_WIDTHS — див. коментар там),
а не по одній. До 25.07 гейт міряв лише 1856 і законно писав «0 порушень» там,
де на вузькому вікні підписи різались. Ширина є в кожному порушенні (поле width),
у людському підсумку (w=…) і в ключі базлайна — інакше порушення не відтворити,
а прийняте на 1000 маскувало б нове на 1856.

Запуск (з кореня worktree):
  .venv\\Scripts\\python scripts\\visual_gate.py            # звіт, exit 0
  .venv\\Scripts\\python scripts\\visual_gate.py --strict   # нові порушення → exit 1
  .venv\\Scripts\\python scripts\\visual_gate.py --selfcheck # самоперевірка детектора
  ... --widths 1000        # лише одна ширина (швидка розвідка під час правки)

КОЛІР ІНТЕРФЕЙСУ (персоналізація вигляду, рішення власника 25.07): гейт завжди
ганяє АКТИВНИЙ колір теми — його задає середовище, ЩЕ ДО старту процесу, бо
fronts/desktop/theme.py читає його на імпорті модуля (одна перевірка на весь
прогін, а не перемикання наживо):
  BALACHKY_UI_COLOR=classic  .venv\\Scripts\\python scripts\\visual_gate.py --strict
  BALACHKY_UI_COLOR=red      .venv\\Scripts\\python scripts\\visual_gate.py --strict
  BALACHKY_UI_COLOR=teal     .venv\\Scripts\\python scripts\\visual_gate.py --strict
  BALACHKY_UI_COLOR=210      ... (довільний тон 0-359)
Без змінної — 'classic' (як і завжди). BALACHKY_FORCE_NIGHT=1 лишається
робочим (сумісність зі старими скриптами) і дає 'red', якщо BALACHKY_UI_COLOR
не задано. Звіт класичного прогону — scripts\\visual_gate_report.json (старий
шлях, без регресу), інших кольорів — visual_gate_report_<колір>.json (кожен
варіант зі своїм звітом, щоб прогони по черзі не затирали один одного).
Геометрія в усіх кольорах однакова (theme.py) — той самий базлайн (без
кольору в ключі) охоплює всі варіанти.
"""
import os
import sys
# Платформа — ДО імпорту PySide6 і ДО tests.render_*: див. докстрінг вище.
# ЖОРСТКО (не setdefault): якщо середовище вже має offscreen — переважуємо, бо
# offscreen бреше про шрифти й дає хибні вердикти обрізання.
os.environ["QT_QPA_PLATFORM"] = "windows"

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_PATH = ROOT / "scripts" / "visual_gate_report.json"
BASELINE_PATH = ROOT / "scripts" / "visual_gate_baseline.json"


def _active_color_label() -> str:
    """Варіант кольору цього прогону — для підпису в консолі й в імені звіту.
    Читає theme.current_ui_color() (сам theme.py вирішує BALACHKY_UI_COLOR
    проти старого BALACHKY_FORCE_NIGHT на імпорті — джерело правди ОДНЕ)."""
    from fronts.desktop.theme import current_ui_color
    color = current_ui_color()
    return f"hue{color}" if isinstance(color, int) else str(color)


def _report_path_for(label: str) -> Path:
    """Звіт класичного варіанту лишається на СТАРОМУ шляху (visual_gate_report.json —
    на нього посилаються qa_gate.ps1 і звички), решта — окремий файл на колір,
    інакше другий прогін мовчки затер би звіт першого і «цифри по кожному
    варіанту окремо» стали б неможливі."""
    if label == "classic":
        return REPORT_PATH
    return REPORT_PATH.with_name(f"visual_gate_report_{label}.json")

# Допуск у пікселях: horizontalAdvance трохи різниться з фактичним рендером
# (kerning/overhang). Дрібні різниці — не обрізання; ловимо помітні.
TOL = 3
VTOL = 3
# Окремий, ВУЖЧИЙ допуск для гліф-кліпу іконки: справжня ширина гліфа рівняється
# з канвою іконки. Гліф, що ТОЧНО заповнює канву (repeat/circle-info, over=0), —
# норма; реальний баг (volume-high у квадраті 16×16) дає over=+2. Поріг 1 їх
# розділяє (виміряно у 2026-07-22, див. _glyph_true_width).
ICON_GLYPH_TOL = 1

# ─────────────────────── ШИРИНИ ПРОГОНУ (єдине місце) ───────────────────────
# Гейт до 25.07 міряв РІВНО одну ширину (1856) — і законно писав «0 порушень»
# там, де на вузькому вікні текст різався. Через цю сліпоту власник ДВІЧІ за добу
# знаходив обрізання раніше за автоматику (Центр моделей, група доказовості
# наради), а рецензія 25.07 — ще й ряд кнопок картки диктування. Тепер ширин три:
#   1000 — MainWindow.minimumWidth(): найгірший ДОСЯЖНИЙ мишею випадок. Вужче
#          вікно Qt не дасть, тож усе, що чисто тут, чисто на будь-якій ширині;
#   1280 — типовий ноутбук (найпоширеніша реальна ширина в користувачів);
#   1856 — «особистий перегляд 1920» Миколи, історична ширина гейта й скрінів.
# Висота стала: вертикальні порушення ловить перенос по ширині, не висота вікна.
GATE_WIDTHS = (1000, 1280, 1856)
GATE_HEIGHT = 1044
# Ширина, на якій ганяємо ще й ширинонезалежні проходи (діалоги, майстер
# першого запуску, probe помилки компонента — усі вони самі виставляють собі
# розмір). Ганяти їх на кожній ширині — втроє довший гейт без жодної нової
# інформації.
PRIMARY_WIDTH = 1856


def _lazy_qt():
    """Qt-класи одним словником (імпорт відкладено до наявного QT_QPA_PLATFORM).
    Явний dict, а не locals(), — щоб лінтер не вважав імпорти невживаними."""
    from PySide6.QtWidgets import (
        QApplication, QWidget, QFrame, QPushButton, QToolButton, QLabel,
        QCheckBox, QRadioButton, QComboBox, QLineEdit, QAbstractButton,
        QTabWidget, QStackedWidget, QStyle, QStyleOptionButton,
        QStyleOptionComboBox)
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtCore import Qt, QTimer, QRect
    return {
        "QApplication": QApplication, "QWidget": QWidget, "QFrame": QFrame,
        "QRect": QRect,
        "QPushButton": QPushButton, "QToolButton": QToolButton,
        "QLabel": QLabel, "QCheckBox": QCheckBox, "QRadioButton": QRadioButton,
        "QComboBox": QComboBox, "QLineEdit": QLineEdit,
        "QAbstractButton": QAbstractButton, "QTabWidget": QTabWidget,
        "QStackedWidget": QStackedWidget, "QStyle": QStyle,
        "QStyleOptionButton": QStyleOptionButton,
        "QStyleOptionComboBox": QStyleOptionComboBox,
        "QFontMetrics": QFontMetrics, "Qt": Qt, "QTimer": QTimer,
    }


# ─────────────────────── пісочниця профілів (без даних користувача) ───────────────────────
def _make_sandbox() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="visgate-"))
    proot = tmp / "profiles"
    default = proot / "default"
    default.mkdir(parents=True)
    (default / "terms.toml").write_text('[terms]\nGitHub = ["гітхаб"]\n',
                                        encoding="utf-8")
    now = round(time.time())
    (default / "history.jsonl").write_text(
        json.dumps({"ts": now, "raw": "тест", "final": "тест",
                    "source": "desktop"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (default / "profile.json").write_text('{"memory": true}', encoding="utf-8")
    (proot / "state.json").write_text('{"active": "default"}', encoding="utf-8")
    return tmp


# ─────────────────────── геометричні перевірки одного віджета ───────────────────────
def _is_richish(text: str) -> bool:
    low = text.lower()
    return "</" in low or "<a " in low or "<b>" in low or "<span" in low \
        or "<img" in low or "<br" in low


def _widget_text(w) -> str:
    getter = getattr(w, "text", None)
    if callable(getter):
        try:
            return getter() or ""
        except TypeError:
            return ""
    return ""


def _widget_path(w) -> str:
    """Стабільний людський шлях: ланцюг класів + objectName/accessibleName."""
    parts = []
    cur = w
    depth = 0
    while cur is not None and depth < 8:
        name = cur.objectName() or ""
        parts.append(f"{type(cur).__name__}" + (f"#{name}" if name else ""))
        cur = cur.parentWidget()
        depth += 1
    return "/".join(reversed(parts))


def _smaller_than_hint(w, qt) -> dict:
    """Сигнал Squish: контрол вужчий/нижчий за свій мінімальний хінт → вміст
    (зазвичай текст) імовірно зрізаний. Грубіший за text_clipped, тож рахуємо
    його лише коли точна перевірка мовчить (у check_widget). Для StatusTag хінт —
    sizeHint (мінімальний у нього дозволяє стиск), для решти — minimumSizeHint."""
    from fronts.desktop.glass import StatusTag
    QAbstractButton = qt["QAbstractButton"]
    QComboBox = qt["QComboBox"]
    if isinstance(w, StatusTag):
        hint = w.sizeHint()
    elif isinstance(w, (QAbstractButton, QComboBox)):
        hint = w.minimumSizeHint()
    else:
        return None
    if w.width() + TOL < hint.width():
        return {"type": "smaller_than_hint", "text": _widget_text(w) or getattr(w, "_text", ""),
                "needed": hint.width(), "avail": w.width()}
    if w.height() + TOL < hint.height():
        return {"type": "smaller_than_hint", "text": _widget_text(w) or getattr(w, "_text", ""),
                "needed": hint.height(), "avail": w.height()}
    return None


def _glyph_true_width(icon, iw, ih, qt):
    """Справжня (НЕобрізана) ширина гліфа при тій самій висоті, з якою його
    малює кнопка. qtawesome вписує гліф у квадрат за меншим боком канви
    (em = min(iconSize)), тож малюємо ту саму іконку у ШИРОКУ DPR-1 канву
    (em×6 × em), де гліф не впирається у край, і міряємо bbox альфа-каналу.
    Повертає ширину гліфа в ЛОГІЧНИХ пікселях або None (порожній гліф).
    Це — фактичний рендер тих самих пікселів, що бачить користувач, тож ловить
    саме баг «гліф ширший за канву» (а не геометрію iconSize vs кнопка)."""
    from PySide6.QtGui import QImage, QPainter
    Qt = qt["Qt"]
    QRect = qt["QRect"]
    em = min(int(iw), int(ih))
    if em <= 0:
        return None
    width, height = em * 6, em
    img = QImage(width, height, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        icon.paint(p, QRect(0, 0, width, height))
    finally:
        p.end()
    left, right = width, -1
    for x in range(width):                       # крайні НЕпорожні стовпці по альфі
        for y in range(height):
            if img.pixelColor(x, y).alpha() > 10:
                if x < left:
                    left = x
                right = x
                break
    if right < 0:
        return None
    return right - left + 1


def _icon_violation(w, qt) -> list:
    """icon_clipped — ДВА незалежні класи обрізання іконкової кнопки. Ціль —
    кнопки з іконкою і БЕЗ тексту (або QToolButton у режимі ІКОНКА-ОНЛІ);
    text+icon ділять простір і покриваються text-перевіркою.

    (а) «канва>кнопка»: iconSize() НЕ вміщається у contentsRect кнопки — гліф
        ріжеться КРАЄМ КНОПКИ. Ловить, напр., синтетичну кнопку 10×10 з іконкою
        24×24. (Це геометрія iconSize vs contentsRect — сам гліф може бути цілим.)

    (б) «гліф>канва»: сам ГЛІФ ширший за КВАДРАТНУ канву іконки. qtawesome
        вписує гліф за меншим боком (за висотою), тож ширші-за-квадрат гліфи
        (fa6s.volume-high, viewBox 640×512, співвідношення 1,25) втрачають праві
        пікселі у боксі 16×16 — саме цей баг фіксить icon_w у _IconButton
        (player.py/video_player.py). Міряємо СПРАВЖНЮ ширину гліфа рендером
        (_glyph_true_width) і рівняємо з iconSize().width() — на БУДЬ-ЯКІЙ
        іконковій кнопці, яку обходить гейт.

    Продакшн-кнопки гучності InlinePlayer/VideoPlayerDialog у live-обхід гейта не
    потрапляють (плеєр будується лише під наявний запис), тож реінтродукцію icon_w
    на них ловить окремий регрес tests/render_tracks_smoke.py::
    VolumeGlyphRegressionTests (прибрати icon_w → гліф ріжеться → тест червоніє).
    Клас (б) детектора підтверджено селфчеком на РЕАЛЬНОМУ гліфі volume-high."""
    QAbstractButton = qt["QAbstractButton"]
    if not isinstance(w, QAbstractButton):
        return []
    try:
        icon = w.icon()
        if icon.isNull():
            return []
    except Exception:
        return []
    if _widget_text(w):                          # текст ділить простір з іконкою —
        Qt = qt["Qt"]                            # то вже text_clipped, не icon
        QToolButton = qt["QToolButton"]
        if not (isinstance(w, QToolButton)
                and w.toolButtonStyle() == Qt.ToolButtonIconOnly):
            return []
    iw, ih = w.iconSize().width(), w.iconSize().height()
    out = []
    # (а) канва іконки більша за кнопку
    cr = w.contentsRect()
    dw, dh = iw - cr.width(), ih - cr.height()
    if dw > TOL or dh > VTOL:
        needed, avail = (iw, cr.width()) if dw >= dh else (ih, cr.height())
        out.append({"type": "icon_clipped",
                    "text": (w.accessibleName() or w.objectName() or "(icon)")
                            + " · канва>кнопка",
                    "needed": needed, "avail": avail})
    # (б) гліф ширший за канву іконки — справжній рендер ширини гліфа
    true_w = _glyph_true_width(icon, iw, ih, qt)
    if true_w is not None and true_w > iw + ICON_GLYPH_TOL:
        out.append({"type": "icon_clipped",
                    "text": (w.accessibleName() or w.objectName() or "(icon)")
                            + " · гліф>канва",
                    "needed": true_w, "avail": iw})
    return out


def check_widget(w, qt) -> list:
    """Усі порушення віджета: zero_size / точний text_clipped / (як фолбек)
    smaller_than_hint / icon_clipped. overlapping_siblings — на рівні контейнера.
    """
    out = _text_violation(w, qt)
    if not out:
        sh = _smaller_than_hint(w, qt)
        if sh:
            out.append(sh)
    out.extend(_icon_violation(w, qt))
    return out


def _text_violation(w, qt) -> list:
    """Точна перевірка обрізання тексту за типом віджета (формули RESEARCH).
    Порожній список = текст вміщається. Кожне порушення — dict: type/text/needed/avail.
    """
    Qt = qt["Qt"]
    QFontMetrics = qt["QFontMetrics"]
    QStyle = qt["QStyle"]
    QPushButton = qt["QPushButton"]
    QToolButton = qt["QToolButton"]
    QLabel = qt["QLabel"]
    QCheckBox = qt["QCheckBox"]
    QRadioButton = qt["QRadioButton"]
    QComboBox = qt["QComboBox"]
    QLineEdit = qt["QLineEdit"]
    QStyleOptionButton = qt["QStyleOptionButton"]
    QStyleOptionComboBox = qt["QStyleOptionComboBox"]

    from fronts.desktop.glass import GlassButton, RecButton, StatusTag

    out = []
    text = _widget_text(w)

    # zero-size із текстом — контрол «є», але не видно
    if text and (w.width() == 0 or w.height() == 0):
        out.append({"type": "zero_size", "text": text,
                    "needed": 0, "avail": 0})
        return out            # решта перевірок безглузда на 0×0

    # RecButton — кругла іконка без тексту; StatusTag — обробляється загальним
    # smaller_than_hint (у check_widget), точного text-виміру не має
    if isinstance(w, (RecButton, StatusTag)):
        return out

    # GlassButton — текст малюється власним paintEvent (див. glass.py)
    if isinstance(w, GlassButton):
        if not text:
            return out
        fm = QFontMetrics(w.font())
        needed = fm.horizontalAdvance(text)
        if getattr(w, "_nav", False):
            from fronts.desktop import glass as _g
            # nav: p.drawText(rect.adjusted(14+_ICON+10, 0, -8, 0), ...)
            avail = w.width() - (14 + _g._ICON + 10) - 8
        else:
            avail = w.width()          # не-nav: текст по центру повного rect
        if needed > avail + TOL:
            out.append({"type": "text_clipped", "text": text,
                        "needed": needed, "avail": avail})
        return out

    # QLabel
    if isinstance(w, QLabel):
        if not text:
            return out
        try:
            if not w.pixmap().isNull():
                return out              # лейбл-картинка, не текст
        except Exception:
            pass
        if w.textFormat() == Qt.RichText or _is_richish(text) \
                or w.openExternalLinks():
            return out                  # rich/HTML — метрики ненадійні, пропускаємо
        fm = QFontMetrics(w.font())
        cr = w.contentsRect()
        if w.wordWrap():
            avail = cr.width()
            if avail <= 0:
                return out
            # Висота під перенос — РІВНО як QLabel малює: QFontMetrics.boundingRect
            # із шириною contentsRect і прапорами вирівнювання (те саме, що йде в
            # QPainter.drawText). НЕ heightForWidth: у Qt-реалізації QLabel він для
            # деяких шрифтів ЗАВИЩУЄ на рядок і дає ХИБНИЙ вердикт — доведено на
            # слогані сайдбара (heightForWidth=48/3рядки, а рендер і boundingRect=
            # 32/2рядки, текст цілий). boundingRect збігається з реальним рендером.
            QRect = qt["QRect"]
            flags = int(Qt.TextWordWrap) | int(w.alignment())
            needed_h = fm.boundingRect(
                QRect(0, 0, avail, 1 << 20), flags, text).height()
            if needed_h > 0 and w.height() + VTOL < needed_h:
                out.append({"type": "text_clipped_v", "text": text,
                            "needed": needed_h, "avail": w.height()})
        else:
            needed = fm.horizontalAdvance(text)
            if needed > cr.width() + TOL:
                out.append({"type": "text_clipped", "text": text,
                            "needed": needed, "avail": cr.width()})
        return out

    # QCheckBox / QRadioButton — текст поруч з індикатором
    if isinstance(w, (QCheckBox, QRadioButton)):
        if not text:
            return out
        opt = QStyleOptionButton()
        opt.initFrom(w)
        opt.text = text
        se = (QStyle.SE_CheckBoxContents if isinstance(w, QCheckBox)
              else QStyle.SE_RadioButtonContents)
        avail = w.style().subElementRect(se, opt, w).width()
        needed = QFontMetrics(w.font()).horizontalAdvance(text)
        if needed > avail + TOL:
            out.append({"type": "text_clipped", "text": text,
                        "needed": needed, "avail": avail})
        return out

    # QComboBox — поточний текст проти поля вводу
    if isinstance(w, QComboBox):
        opt = QStyleOptionComboBox()
        opt.initFrom(w)
        field = w.style().subControlRect(
            QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, w)
        avail = field.width()
        fm = QFontMetrics(w.font())
        cur = w.currentText()
        if cur:
            needed = fm.horizontalAdvance(cur)
            if needed > avail + TOL:
                out.append({"type": "text_clipped", "text": cur,
                            "needed": needed, "avail": avail})
        return out

    # Стокові QPushButton / QToolButton
    if isinstance(w, QPushButton):
        if not text:
            return out
        opt = QStyleOptionButton()
        opt.initFrom(w)
        opt.text = text
        rect = w.style().subElementRect(QStyle.SE_PushButtonContents, opt, w)
        avail = rect.width()
        if not w.icon().isNull():
            avail -= w.iconSize().width() + 4
        needed = QFontMetrics(w.font()).horizontalAdvance(text)
        if needed > avail + TOL:
            out.append({"type": "text_clipped", "text": text,
                        "needed": needed, "avail": avail})
        return out

    if isinstance(w, QToolButton):
        if not text or w.toolButtonStyle() == Qt.ToolButtonIconOnly:
            return out
        needed = QFontMetrics(w.font()).horizontalAdvance(text)
        avail = w.width() - 8
        if needed > avail + TOL:
            out.append({"type": "text_clipped", "text": text,
                        "needed": needed, "avail": avail})
        return out

    # QLineEdit — placeholder (порожнє поле) або текст read-only поля. Редаговане
    # поле з текстом прокручується — не «обрізане». Метод — той самий elidedText,
    # що Qt застосовує до плейсхолдера (ElideRight), тож вердикт збігається з
    # реальним рендером. inner = contentsRect − внутрішні поля Qt (horizontalMargin
    # 2px з боку) − textMargins. Ловить занадто вузьке поле з підказкою.
    if isinstance(w, QLineEdit):
        cr = w.contentsRect()
        tm = w.textMargins()
        inner = cr.width() - 4 - tm.left() - tm.right()
        if inner <= 0:
            return out
        fm = QFontMetrics(w.font())
        probe = None
        if not w.text() and w.placeholderText():
            probe = w.placeholderText()
        elif w.isReadOnly() and w.text():
            probe = w.text()
        if probe:
            needed = fm.horizontalAdvance(probe)
            if fm.elidedText(probe, Qt.ElideRight, inner) != probe:
                out.append({"type": "placeholder_clipped", "text": probe,
                            "needed": needed, "avail": inner})
        return out

    return out


# ─────────────────────── обхід дерева видимих віджетів ───────────────────────
def _is_text_control(w, qt) -> bool:
    QAbstractButton = qt["QAbstractButton"]
    QComboBox = qt["QComboBox"]
    QLabel = qt["QLabel"]
    if isinstance(w, (QAbstractButton, QComboBox)):
        return True
    if isinstance(w, QLabel):
        try:
            if not w.pixmap().isNull():
                return False
        except Exception:
            pass
        return bool(w.text())
    return False


def _check_overlaps(controls, page, lang, results, seen, qt):
    """overlapping_siblings: два текстові контроли-сусіди (спільний батько)
    геометрично перетинаються більш ніж на 40% меншого — верстка «наїхала».
    Значний поріг відсікає легітимні дрібні дотики (тіні/бордюри)."""
    from collections import defaultdict
    by_parent = defaultdict(list)
    for w in controls:
        by_parent[id(w.parentWidget())].append(w)
    for group in by_parent.values():
        for i, a in enumerate(group):
            ga = a.geometry()
            area_a = ga.width() * ga.height()
            for b in group[i + 1:]:
                gb = b.geometry()
                inter = ga.intersected(gb)
                if inter.isEmpty():
                    continue
                area_b = gb.width() * gb.height()
                smaller = min(area_a, area_b) or 1
                if inter.width() * inter.height() < 0.40 * smaller:
                    continue
                key = ("overlap", min(id(a), id(b)), max(id(a), id(b)))
                if key in seen:
                    continue
                seen.add(key)
                results.append({
                    "type": "overlapping_siblings", "lang": lang, "page": page,
                    "widget": _widget_path(a),
                    "text": f"{_widget_text(a)!r} ⨯ {_widget_text(b)!r}",
                    "needed": inter.width() * inter.height(), "avail": smaller,
                    "size": [ga.width(), ga.height()]})


def _scan_visible(root, page, lang, results, seen, qt):
    QWidget = qt["QWidget"]
    controls = []
    for w in root.findChildren(QWidget):
        if not w.isVisible():
            continue
        wid = id(w)
        for v in check_widget(w, qt):
            key = (wid, v["type"])
            if key in seen:
                continue
            seen.add(key)
            v.update({"lang": lang, "page": page, "widget": _widget_path(w),
                      "size": [w.width(), w.height()]})
            results.append(v)
        if _is_text_control(w, qt):
            controls.append(w)
    _check_overlaps(controls, page, lang, results, seen, qt)


def _process(app, times=3):
    for _ in range(times):
        app.processEvents()


def _flush_deferred(app):
    """Виконати deleteLater зараз, а не лишати C++-об'єкти до teardown Qt."""
    from PySide6.QtCore import QCoreApplication, QEvent
    _process(app)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def scan_container(root, page, lang, results, seen, app, qt):
    """Сканує root і, послідовно активуючи кожну вкладку QTabWidget та кожен
    індекс вкладеного QStackedWidget, добирає приховані підсторінки."""
    QTabWidget = qt["QTabWidget"]
    QStackedWidget = qt["QStackedWidget"]
    _process(app)
    _scan_visible(root, page, lang, results, seen, qt)
    for tabw in root.findChildren(QTabWidget):
        for i in range(tabw.count()):
            try:
                tabw.setCurrentIndex(i)
            except RuntimeError:
                continue
            _process(app)
            _scan_visible(root, f"{page}/tab[{i}:{tabw.tabText(i)}]",
                          lang, results, seen, qt)
    for st in root.findChildren(QStackedWidget):
        for i in range(st.count()):
            try:
                st.setCurrentIndex(i)
            except RuntimeError:
                continue
            _process(app)
            _scan_visible(root, page, lang, results, seen, qt)


# ─────────────────────── реєстр досяжних діалогів (без побічних ефектів) ───────────────────────
def _dialog_factories(win, lang):
    """(ім'я, фабрика) для діалогів, які будуються без мережі/файлів/воркерів.

    Кожен показуємо не-модально (show, не exec), скануємо, закриваємо. Важкі
    діалоги (докачка GPU/моделі, протокол наради, онбординг, захоплення клавіші
    з grabKeyboard, редактор команди з резолвом моделі) свідомо пропущені —
    їхня побудова має побічні ефекти. Пропущені перелічені у звіті (skipped)."""
    from fronts.desktop.corpus_dialog import CorpusReportDialog
    from fronts.desktop.navcommands_dialog import NavCommandsDialog
    from fronts.desktop.diff_review import DiffReviewDialog
    from fronts.desktop.pages.vocab import (
        _BulkImportDialog, _MacroAddDialog, _PhraseAddDialog)
    from fronts.desktop.pages.settings import NetworkLogDialog
    from fronts.desktop.main_window import TextLogReminderDialog

    # заповнений журнал мережі — щоб гейт перевірив і таблицю, і довідку
    _net_rows = [
        {"ts": 1_700_000_000.0, "host": "huggingface.co", "kind": "model",
         "allowed": True, "detail": "large"},
        {"ts": 1_700_000_100.0, "host": "api.github.com", "kind": "update",
         "allowed": True, "detail": "check"},
    ]
    long_before = ("Це початковий варіант надиктованого тексту, який користувач "
                   "хоче переформатувати у більш стриманому тоні перед надсиланням.")
    long_after = ("Це відредагований варіант того самого тексту — рівний тон, "
                  "без зайвих емоцій, готовий до відправлення адресату.")
    return [
        ("crash", _build_crash_box),
        ("CorpusReportDialog",
         lambda: CorpusReportDialog("нейромережа впала на другому кроці", win)),
        ("NavCommandsDialog", lambda: NavCommandsDialog(lang, None, win)),
        ("DiffReviewDialog",
         lambda: DiffReviewDialog(long_before, long_after, win)),
        ("_BulkImportDialog", lambda: _BulkImportDialog(win)),
        ("_MacroAddDialog", lambda: _MacroAddDialog(win)),
        ("_PhraseAddDialog", lambda: _PhraseAddDialog(win)),
        ("VideoPlayerDialog", lambda: _make_video_dialog(win)),
        ("MultiTrackPlayer", lambda: _make_tracks_dialog(win)),
        ("VideoMixerDialog", lambda: _make_video_mixer_dialog(win)),
        ("NetworkLogDialog", lambda: NetworkLogDialog(win, entries=_net_rows)),
        ("TextLogReminderDialog",
         lambda: TextLogReminderDialog(win.controller, win)),
        # feature/tts-listen (Хвиля 1): панель «Прослухати» — транспорт+Зберегти й
        # порожній стан «завантажте голос» (найтекстовіший). Джерела ледачі
        # (InlinePlayer не грає без файлу), без мережі/воркерів — скан безпечний.
        ("ListenPanel", lambda: _make_listen_panel(win, has_voice=True)),
        ("ListenPanelEmpty", lambda: _make_listen_panel(win, has_voice=False)),
        ("ListenPanelKaraoke", lambda: _make_listen_panel(win, karaoke=True)),
        # feature/tts-listen (Хвиля 3): менеджер голосів (картки за мовою, стан «не
        # завантажено» — найтекстовіший) + підказка невідповідності мови.
        ("VoiceManagerDialog", lambda: _make_voice_manager(win)),
        ("TtsLangMismatch", lambda: _make_lang_mismatch(win)),
        # feature/tts-listen (Хвиля 4): діалог виправлення вимови (два таби, поле,
        # наголос по кліку, прев'ю).
        ("PronunciationDialog", lambda: _make_pron_dialog(win)),
    ]


def _make_pron_dialog(win):
    from fronts.desktop.tts_pron import PronunciationDialog
    return PronunciationDialog(win, word="Коростень")


def _make_voice_manager(win):
    from fronts.desktop.tts_voices import VoiceManagerDialog
    # root=None → усі картки у стані «не завантажено» (найтекстовіший: назва+рушій+
    # мови+розмір+ліцензія+кнопки Завантажити/Почути зразок), згруповані за мовою.
    return VoiceManagerDialog(win, root=None)


def _make_lang_mismatch(win):
    from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout
    from fronts.desktop.glass import GlassButton
    from fronts.desktop.i18n import tr
    dlg = QDialog(win)
    lay = QVBoxLayout(dlg)
    lbl = QLabel(tr("tts_lang_mismatch"))
    lbl.setWordWrap(True)
    lay.addWidget(lbl)
    lay.addWidget(GlassButton(tr("tts_voice_download")))
    return dlg


def _make_listen_panel(win, *, has_voice=True, karaoke=False):
    from fronts.desktop.tts_panel import ListenPanel
    from fronts.desktop.i18n import tr
    text = tr("tts_listen_selection")
    panel = ListenPanel(win, has_voice=has_voice, text=text)
    if karaoke and panel.highlighter() is not None:
        # стан із підсвіченим словом: синтетичні тривалості + оновлення на 50 мс
        words = [{"start_ms": 0, "raw_start": 0, "raw_end": 4},
                 {"start_ms": 50, "raw_start": 5, "raw_end": 10}]
        panel.set_timings(words, [0])
        panel.highlighter().update(50)
    return panel


def _make_tracks_dialog(win):
    """Багатодоріжковий аудіоплеєр (панель мікшера: чекбокс+гучність+Соло на
    доріжку) у діалозі-обгортці — щоб детектор бачив рядки панелі в обох мовах.
    Джерела ледачі (не грають без файлів), тож сканування безпечне."""
    from PySide6.QtWidgets import QDialog, QVBoxLayout
    from fronts.desktop.player_tracks import MultiTrackPlayer
    from fronts.desktop.i18n import tr
    dlg = QDialog(win)
    lay = QVBoxLayout(dlg)
    specs = [("mic", tr("meeting_track_mic"), str(ROOT / "tests" / "_mic.wav")),
             ("sys", tr("meeting_track_sys"), str(ROOT / "tests" / "_sys.wav"))]
    lay.addWidget(MultiTrackPlayer(specs, dlg))
    return dlg


def _make_video_dialog(win):
    """Відеоплеєр у стані «людська помилка» — найтекстовіший екран (банер про
    відсутній кодек + повний ряд контролів), щоб детектор обрізань бачив і його.
    Гейт без реальних відеофайлів, тож форсуємо банер напряму (без відтворення)."""
    from fronts.desktop.i18n import tr
    from fronts.desktop.video_player import VideoPlayerDialog
    dlg = VideoPlayerDialog(None, win)
    dlg._show_error(tr("video_error"))
    return dlg


def _make_video_mixer_dialog(win):
    """Відеоплеєр у РЕЖИМІ НАРАДИ (audio_tracks) — усередині нього ЖИВЕ та сама
    панель мікшера (TrackMixerPanel), АЛЕ з окремим рядком «Звук екрана» і власною
    логікою показу/приховання (reveal_channel). _make_video_dialog будує відео БЕЗ
    audio_tracks, тож `if self._audio_tracks:` не спрацьовує і ця панель НЕ існує
    під час гейта — обрізання тексту/іконки саме тут проходило б --strict непомітно
    (сліпа зона, знахідка рецензії 22.07). Тут вмикаємо режим наради й РОЗКРИВАЄМО
    «Звук екрана», щоб детектор бачив ПОВНИЙ набір рядків (екран+mic+sys) в обох
    мовах. Джерела ледачі (без файлів не грають) — сканування безпечне."""
    from fronts.desktop.i18n import tr
    from fronts.desktop.video_player import VideoPlayerDialog
    specs = [("mic", tr("meeting_track_mic"), str(ROOT / "tests" / "_mic.wav")),
             ("sys", tr("meeting_track_sys"), str(ROOT / "tests" / "_sys.wav"))]
    dlg = VideoPlayerDialog(None, win, audio_tracks=specs)
    if dlg._panel is not None:              # показати «Звук екрана» — інакше рядок схований
        dlg._panel.reveal_channel("screen")
    return dlg


SKIPPED_DIALOGS = [
    "FirstRunWizard (онбординг: перевірка/докачка моделі)",
    "GpuDownloadDialog (запускає воркер докачки CUDA)",
    "ProtocolModelDownloadDialog / HFModelDialog (мережа HuggingFace)",
    "ProtocolDialog / QADialog / RewriteDialog (фонова генерація LLM)",
    "KeyCaptureDialog (grabKeyboard на показі)",
    "CommandEditDialog (резолв моделі мовної генерації)",
    "ContextProfileDialog / AutoProfileRuleDialog (потрібен resolver вікна)",
    "RecoveryDialog / FormFillDialog (можливий воркер/сесія — пропущено обережно)",
]

STATE_PROBES = [
    "MeetingPage: банери незашифрованих нарад і пошкодженого config.toml",
    "SettingsPage: довга помилка завантаження компонента",
    "DictationPage: заповнена стрічка (картка з raw ≠ final — увесь ряд кнопок)",
]


def _build_crash_box():
    """Справжній краш-діалог продукту (fronts.desktop.crash._build_crash_dialog,
    вертикальний стек кнопок) — сканувати треба те, що бачить користувач."""
    from fronts.desktop.crash import _build_crash_dialog
    dlg, _buttons, _result = _build_crash_dialog(
        "Traceback (демо для гейта)\n  File ...\nRuntimeError: x")
    return dlg


def _scan_one_dialog(name, factory, lang, results, app, qt):
    """Збудувати, показати, просканувати й закрити ОДИН діалог. Фікс 3:
    якщо фабрика КИДАЄ виняток — це ПОРУШЕННЯ (dialog_broken), а не тихий
    warning+continue. Раніше зламаний діалог (фабрика падала) давав зелений
    --strict: гейт мовчки пропускав бите вікно, яке саме й покликаний ловити.
    Тепер виняток фабрики летить у results і валить --strict (нового ключа нема
    в базлайні). Реєстр SKIPPED_DIALOGS лишається для СВІДОМО пропущених важких
    діалогів — їх у _dialog_factories просто немає, тож сюди вони не доходять."""
    try:
        dlg = factory()
    except Exception as e:                     # фабрика впала — це порушення
        print(f"  ! діалог {name} НЕ збудувався (порушення): {e}")
        results.append({"type": "dialog_broken", "lang": lang,
                        "page": f"dialog:{name}", "widget": f"dialog:{name}",
                        "text": f"{name}: {e}", "needed": 0, "avail": 0,
                        "size": [0, 0]})
        return
    try:
        dlg.setAttribute(qt["Qt"].WA_DontShowOnScreen, True)
        dlg.show()
        _process(app)
        seen = set()
        scan_container(dlg, f"dialog:{name}", lang, results, seen, app, qt)
    finally:
        try:
            dlg.close()
            dlg.deleteLater()
        except Exception:
            pass
        # processEvents() поза app.exec() не доставляє DeferredDelete сам:
        # діалог мусить померти тут, поки QApplication ще жива.
        _flush_deferred(app)


def scan_dialogs(win, lang, results, app, qt):
    for name, factory in _dialog_factories(win, lang):
        _scan_one_dialog(name, factory, lang, results, app, qt)


# ─────────────────────── побудова живого вікна (спільне з visual_sweep) ───────────────────────
def open_main_window(lang, app, sandbox, width=PRIMARY_WIDTH):
    """Виставити мову, вимкнути анімації, збудувати й показати MainWindow заданої
    ширини (типово «особистий перегляд 1920» Миколи). Повертає (win, ctrl).
    Спільне для детектора й скрін-проходу, щоб обидва бачили ідентичну верстку."""
    from fronts.desktop import i18n, motion
    from types import SimpleNamespace
    i18n.set_language(lang)
    motion.init_config(SimpleNamespace(animations=False))   # без живих таймерів

    from tests.render_nav_smoke import _NavController
    from fronts.desktop.main_window import MainWindow
    from PySide6.QtCore import Qt

    ctrl = _NavController(sandbox)
    ctrl.cfg.ui_language = lang
    win = MainWindow(ctrl)
    win.setWindowState(Qt.WindowNoState)
    # ДЕТЕРМІНОВАНІСТЬ: не мапимо на екран. Показане вікно менеджер вікон обрізає
    # до логічного розміру монітора (на масштабованому дисплеї 1856 «стискається»
    # до ~1391), і тоді вердикти гейта залежали б від екрана виконавця — базлайн
    # флакав би між машинами. WA_DontShowOnScreen тримає ФІКСОВАНІ 1856×1044
    # логічних будь-де (і на CI), а платформа лишається windows → шрифт Segoe UI
    # справжній (offscreen тут ні до чого — це інша штука). Бонус: нічого не
    # блимає на екрані під час гейта.
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.resize(width, GATE_HEIGHT)
    win.show()
    _process(app, 5)
    return win, ctrl


def close_main_window(win, app):
    """Спинити всі таймери й знести вікно (канон nav-smoke проти 0xC000041D)."""
    from PySide6.QtCore import QTimer
    for t in win.findChildren(QTimer):
        try:
            t.stop()
        except RuntimeError:
            pass
    win.close()
    win.deleteLater()
    _flush_deferred(app)


# ─────── пост-фейл стан: довга помилка завантаження компонента ───────
# Синтетичний мережевий виняток із неперервним URL-токеном (55+ символів без
# пробілу) — саме такий рядок failed-колбек кладе в статус, і саме він раніше
# різався по горизонталі (QLabel[wordWrap] переносить лише по пробілах).
_COMPONENT_ERR = (
    "HTTPSConnectionPool(host='huggingface.co', port=443): Max retries exceeded "
    "with url: /api/models/sberbank-ai-ruRoberta-large-punctuation-model/resolve/"
    "main/pytorch_model.bin (Caused by NewConnectionError: [Errno 11001] "
    "getaddrinfo failed)")


def scan_component_error_state(win, lang, results, app, qt):
    """Скан СТАНУ, який дефолтний обхід не бачить: довга помилка завантаження
    компонента постобробки в _autocorrect_status/_punctuator_status.

    Звичайний скан читає лише початковий текст статусу («Готово»/«Спочатку
    завантажте…»), а не пост-фейл setText() з рядком винятку. Тут кладемо довгий
    виняток РЕАЛЬНИМ шляхом коду (SettingsPage._set_component_error) за НАЙВУЖЧОЇ
    реальної колонки (мінімум вікна — найгірший випадок переносу) і перевіряємо
    обидва статуси на два класи обрізання:
      • text_clipped   — неперервний прогін (URL/клас винятку) ширший за лейбл;
      • text_clipped_v — рядок QGridLayout не виріс під перенесений текст.
    Викликається останнім у run_language (вікно далі закривається), тож зміна
    розміру нікому не заважає. М'яко фейлить у no-op, якщо сторінки/поля немає —
    гейт не має падати через відсутній probe."""
    from PySide6.QtCore import QRect
    Qt = qt["Qt"]
    QFontMetrics = qt["QFontMetrics"]
    try:
        from fronts.desktop.pages.settings import SettingsPage
        idx = next((i for i in range(win.pages.count())
                    if isinstance(win.pages.widget(i), SettingsPage)), None)
        if idx is None:
            return
        page = win.pages.widget(idx)
        win.resize(win.minimumWidth(), win.minimumHeight())   # найвужча колонка
        win.set_page(idx)
        from fronts.desktop.i18n import tr
        rec_title = tr("set_tab_recording")
        rec_tab_idx = next((i for i in range(page._tabs.count())
                            if page._tabs.tabText(i) == rec_title), None)
        if rec_tab_idx is not None:
            page._tabs.setCurrentIndex(rec_tab_idx)
        else:
            # needed/avail обов'язкові: без них підсумок падає з KeyError уже
            # ПІСЛЯ чесного червоного — і причина губиться за трасуванням
            results.append({"type": "missing_tab", "text": f"вкладка '{rec_title}' відсутня",
                            "lang": lang, "page": "SettingsPage/component-error",
                            "widget": "tabs", "size": [0, 0],
                            "needed": 0, "avail": 0})
            return
        _process(app, 6)
        zwsp = "​"
        for name in ("_autocorrect_status", "_punctuator_status"):
            lbl = getattr(page, name, None)
            if lbl is None or not lbl.isVisible():
                results.append({"type": "missing_status_label", "text": f"лейбл '{name}' відсутній або невидимий",
                                "lang": lang, "page": "SettingsPage/component-error",
                                "widget": name, "size": [0, 0],
                                "needed": 0, "avail": 0})
                continue
            page._set_component_error(lbl, _COMPONENT_ERR)
            _process(app, 8)
            fm = QFontMetrics(lbl.font())
            avail = lbl.contentsRect().width()
            if avail <= 0:
                continue
            shown = lbl.text()
            meta = {"lang": lang, "page": "SettingsPage/component-error",
                    "widget": name, "size": [lbl.width(), lbl.height()]}
            # горизонталь: найширший неперервний прогін (розрив = пробіл або ZWSP)
            runs = shown.replace(zwsp, " ").split()
            maxrun = max((fm.horizontalAdvance(r) for r in runs), default=0)
            if maxrun > avail + TOL:
                results.append({"type": "text_clipped", "text": "помилка завантаження",
                                "needed": maxrun, "avail": avail, **meta})
            # вертикаль: та сама формула boundingRect, що в _text_violation
            flags = int(Qt.TextWordWrap) | int(lbl.alignment())
            needed_h = fm.boundingRect(
                QRect(0, 0, avail, 1 << 20), flags, shown).height()
            if needed_h > 0 and lbl.height() + VTOL < needed_h:
                results.append({"type": "text_clipped_v", "text": "помилка завантаження",
                                "needed": needed_h, "avail": lbl.height(), **meta})
        # відновити дефолтний статус (гігієна, якщо код колись переставлять)
        try:
            page._refresh_textproc_controls()
        except Exception:
            pass
        _process(app, 2)
    except Exception as exc:   # probe не має валити гейт власною крихкістю
        print(f"  ⚠ probe component-error пропущено: {exc}")


def scan_security_banner_state(win, lang, results, app, qt):
    """Явний visual-gate стан для двох нових стійких security-банерів."""
    try:
        page = win.meeting
        idx = win.pages.indexOf(page)
        cfg = page.controller.cfg
        old_corrupt = getattr(cfg, "_config_corrupt", False)
        old_recovered = getattr(cfg, "_config_recovered_from_backup", False)
        win.set_page(idx)
        cfg._config_corrupt = True
        cfg._config_recovered_from_backup = False
        page._refresh_config_warning()
        page._on_pending_plaintext(123)
        _process(app, 4)
        # Перевіряємо РІВНО дві мітки банерів, а не всю MeetingPage: скан цілої
        # сторінки затягував сторонні підписи (вибір моделі ШІ) і приписував
        # цьому пробнику чужі порушення. Сторінку загалом гейт обходить окремо.
        for lbl in (page._config_corrupt_warning, page._pending_plaintext_warning):
            if not lbl.isVisible():
                print(f"  ⚠ probe security-banners: {_widget_path(lbl)} не показався")
                continue
            for v in check_widget(lbl, qt):
                v.update({"lang": lang, "page": "MeetingPage/security-banners",
                          "widget": _widget_path(lbl),
                          "size": [lbl.width(), lbl.height()]})
                results.append(v)
        page._on_pending_plaintext(0)
        cfg._config_corrupt = old_corrupt
        cfg._config_recovered_from_backup = old_recovered
        page._refresh_config_warning()
    except Exception as exc:
        print(f"  ⚠ probe security-banners пропущено: {exc}")


def scan_onboarding_wizard(lang, results, app, qt):
    """Обхід усіх кроків майстра першого запуску (FirstRunWizard) для обох мов."""
    try:
        from fronts.desktop import i18n
        from fronts.desktop.onboarding import FirstRunWizard
        i18n.set_language(lang)

        wiz = FirstRunWizard()
        # Гарантуємо, що і крок GPU також у стекі
        if hasattr(wiz, "_gpu_index") and wiz._gpu_index is not None:
            pass
        wiz.setAttribute(qt["Qt"].WA_DontShowOnScreen, True)
        wiz.resize(620, 460)
        wiz.show()
        _process(app, 4)

        for step in range(wiz._stack.count()):
            wiz._stack.setCurrentIndex(step)
            if step == 3:
                wiz._update_voice_page_state()
            _process(app, 4)
            seen = set()
            scan_container(wiz, f"FirstRunWizard/step{step+1}", lang, results, seen, app, qt)

        wiz.close()
        wiz.deleteLater()
        _process(app, 3)
    except Exception as exc:
        print(f"  ! FirstRunWizard НЕ збудувався (порушення): {exc}")
        results.append({"type": "onboarding_broken", "lang": lang,
                        "page": "FirstRunWizard", "widget": "FirstRunWizard",
                        "text": f"FirstRunWizard: {exc}", "needed": 0, "avail": 0,
                        "size": [0, 0]})


def _check_horizontal_overflow(page, page_name, lang, results, qt):
    """Порушення page_horizontal_overflow: вміст QScrollArea сторінки ширший за
    видиму область (viewport) — ознака ряду без переносу, що розпирає картку
    за межі вікна горизонтальною прокруткою (діагноз 2026-07-30 №1). До фіксу
    гейт це не бачив: мок read_meeting_utterances повертав [], тож ряд чіпів
    узагалі не будувався (сліпа зона, не хиба самої формули перевірки)."""
    scroll = getattr(page, "_scroll", None)
    if scroll is None:
        return
    content = scroll.widget()
    if content is None:
        return
    viewport_w = scroll.viewport().width()
    needed_w = max(content.width(), content.minimumSizeHint().width())
    if needed_w > viewport_w + TOL:
        results.append({
            "type": "page_horizontal_overflow",
            "lang": lang, "page": page_name, "widget": _widget_path(content),
            "text": "вміст стрічки ширший за вікно (горизонтальна прокрутка)",
            "needed": needed_w, "avail": viewport_w,
            "size": [content.width(), content.height()],
        })


def scan_done_meeting_state(win, lang, results, app, qt):
    """Скан стану завершеної наради (готова картка з текстом і кнопками дій).
    Якщо побудова або оновлення стану завершеної наради зазнає збою (виняток) —
    це МУСИТЬ реєструватися як ПОРУШЕННЯ (meeting_state_broken), а не мовчазний
    пропуск/continue (за каноном).
    """
    from types import SimpleNamespace
    import tempfile
    tmpdir = None
    try:
        page = win.meeting
        idx = win.pages.indexOf(page)
        win.set_page(idx)

        meta = SimpleNamespace(
            id="2026-07-15_14-30-05",
            status="done",
            title="Тестова завершена нарада",
            preset="both",
            audio_files={"mic": ["audio/mic/0001.wav"]},
            processing={"status": "complete"},
            bookmarks=[],
            speaker_names={},
        )

        ctrl = page.controller
        orig_list = getattr(ctrl, "list_meetings", None)
        orig_read = getattr(ctrl, "read_meeting_transcript", None)
        orig_utts = getattr(ctrl, "read_meeting_utterances", None)
        orig_meta = getattr(ctrl, "meeting_integrity_meta", None)
        orig_ready = getattr(ctrl, "protocol_model_ready", None)
        orig_audio_paths = getattr(ctrl, "meeting_audio_paths", None)

        # Чіпи часових позначок (segrow) додаються лише коли є і utterances,
        # і вбудований плеєр (`if utterances and player is not None`) — плеєр
        # будується лише коли meeting_audio_paths() дає реальний WAV-файл,
        # інакше _add_audio_player() тихо повертає None і ряд чіпів НІКОЛИ не
        # будується, скільки завгодно utterances не давай (те саме сліпе
        # місце, що й порожній read_meeting_utterances — діагноз №4).
        tmpdir = tempfile.mkdtemp(prefix="balachky_gate_meeting_")
        fake_wav = Path(tmpdir) / "mic.wav"
        fake_wav.write_bytes(b"")
        ctrl.meeting_audio_paths = lambda sid: {"mic": str(fake_wav)}

        ctrl.list_meetings = lambda: [meta]
        ctrl.read_meeting_transcript = lambda sid: (
            "[00:00] Я: Доброго дня, шановні колеги.\n"
            "[00:05] Співрозмовники: Доброго дня, розпочинаємо обговорення."
        )
        # 12 реплік — реальний максимум чіпів часових позначок (meeting.py
        # segrow бере utterances[:12]). Порожній список ХОВАВ увесь ряд чіпів
        # від гейта (if utterances and player: ... узагалі не виконувався) —
        # саме тому переповнення ряду на 1000px не ловилось (діагноз №4).
        mock_utterances = [
            SimpleNamespace(start=float(i * 5), end=float(i * 5 + 4),
                            text=f"Репліка {i + 1} тестової наради.")
            for i in range(12)
        ]
        ctrl.read_meeting_utterances = lambda sid: mock_utterances
        ctrl.meeting_integrity_meta = lambda sid: SimpleNamespace(
            status="unverified", audio_sha="abc123def45678901234567890123456", events=[]
        )
        ctrl.protocol_model_ready = lambda: True

        try:
            page.refresh()
            _process(app, 4)
            seen = set()
            scan_container(page, "MeetingPage/done-card", lang, results, seen, app, qt)
            _check_horizontal_overflow(page, "MeetingPage/done-card", lang, results, qt)
        finally:
            if orig_list is not None: ctrl.list_meetings = orig_list
            if orig_read is not None: ctrl.read_meeting_transcript = orig_read
            if orig_utts is not None: ctrl.read_meeting_utterances = orig_utts
            if orig_meta is not None: ctrl.meeting_integrity_meta = orig_meta
            if orig_ready is not None: ctrl.protocol_model_ready = orig_ready
            if orig_audio_paths is not None:
                ctrl.meeting_audio_paths = orig_audio_paths
    except Exception as exc:
        print(f"  ❌ visual_gate failure: scan_done_meeting_state error: {exc}")
        results.append({
            "type": "meeting_state_broken",
            "lang": lang,
            "page": "MeetingPage/done-card",
            "widget": "MeetingPage",
            "text": f"Не вдалося побудувати стан готової наради: {exc}",
            "needed": 0,
            "avail": 0,
            "size": [0, 0],
        })
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def scan_dictation_feed_state(win, lang, results, app, qt):
    """Скан заповненої стрічки диктування. Дефолтний обхід сторінок бачить лише
    ПОРОЖНІЙ стан («ще нічого не надиктовано»), тож увесь мета-ряд картки —
    «Копіювати», «Копіювати дослівно», «Переформатувати…», ✕ — до цього пробника
    гейт не бачив узагалі (сліпа зона, знахідка рецензії 25.07). Подаємо raw ≠ final,
    щоб кнопка дослівного копіювання теж була видима: саме на цьому ряду тісно.
    Збій побудови картки — ПОРУШЕННЯ, а не мовчазний пропуск."""
    try:
        page = win.dictation
        win.set_page(win.pages.indexOf(page))
        page.add_entry("прошу підготувати зведення особового складу до 15,5 години",
                       "Прошу підготувати зведення особового складу до 15,5 години.")
        _process(app, 4)
        seen = set()
        scan_container(page, "DictationPage/feed-card", lang, results, seen, app, qt)
    except Exception as exc:
        print(f"  ❌ visual_gate failure: scan_dictation_feed_state error: {exc}")
        results.append({
            "type": "dictation_state_broken",
            "lang": lang,
            "page": "DictationPage/feed-card",
            "widget": "DictationPage",
            "text": f"Не вдалося побудувати картку стрічки: {exc}",
            "needed": 0,
            "avail": 0,
            "size": [0, 0],
        })


# ─────────────────────── прогін для однієї мови ───────────────────────
def run_language(lang, app, sandbox, qt, results, width=PRIMARY_WIDTH):
    """Повний обхід для однієї мови на ОДНІЙ ширині вікна. Кожне порушення
    отримує поле width (ставиться одним місцем у main) — без нього порушення
    неможливо відтворити, а базлайн не розрізняв би ширини."""
    win, _ctrl = open_main_window(lang, app, sandbox, width)
    # Сайдбар — завжди-видима хром-панель (QFrame#sidebar): бренд, слоган, РЯДОК
    # ВЕРСІЇ (аудит 1.2.1 №5 колись обрізався знизу), режим-тестування-мітка й
    # 8 nav-кнопок. Структурно він СУСІД self.pages у центральному layout, тож
    # жоден обхід сторінок (win.pages.widget(i)) його не бачить — скануємо окремим
    # прогоном. Якщо панель зникла (перейменували objectName) — валимо ГУЧНО, а не
    # мовчки пропускаємо: тихий пропуск завжди-видимого UI — сама вада, яку гейт
    # покликаний ловити.
    sidebar = win.findChild(qt["QFrame"], "sidebar")
    if sidebar is None:
        raise RuntimeError(
            "visual_gate: QFrame#sidebar не знайдено — скан хром-панелі "
            "(nav-кнопки/версія/слоган) неможливий; перевір main_window.py")
    seen = set()
    scan_container(sidebar, "sidebar", lang, results, seen, app, qt)
    for i in range(win.pages.count()):
        win.set_page(i)
        _process(app, 4)
        page = win.pages.widget(i)
        pname = type(page).__name__
        # Згорнуті розкривачки ховають цілі панелі від isVisible-скану (сліпа
        # зона зі злиття 22.07: панель налаштувань Наради). Розкриваємо всі
        # QToolButton-checkable перед виміром, щоб сканувати ПОВНИЙ вміст.
        for toggle in page.findChildren(qt["QToolButton"]):
            if toggle.isCheckable() and not toggle.isChecked():
                toggle.setChecked(True)
        _process(app, 4)
        seen = set()
        scan_container(page, pname, lang, results, seen, app, qt)
    scan_security_banner_state(win, lang, results, app, qt)
    scan_done_meeting_state(win, lang, results, app, qt)
    scan_dictation_feed_state(win, lang, results, app, qt)
    # Ширинонезалежні проходи — лише на PRIMARY_WIDTH: діалоги й майстер першого
    # запуску самі виставляють собі розмір, а probe помилки компонента сам
    # стискає вікно до мінімуму. На кожній ширині вони дали б ті самі вердикти.
    if width == PRIMARY_WIDTH:
        scan_dialogs(win, lang, results, app, qt)
        scan_onboarding_wizard(lang, results, app, qt)
        # СТАН, невидимий дефолтному обходу: довга помилка завантаження
        # компонента. ОСТАННІМ — змінює розмір вікна, яке далі закривається.
        scan_component_error_state(win, lang, results, app, qt)
    close_main_window(win, app)



# ─────────────────────── базлайн / порівняння ───────────────────────
def _match_key(v) -> tuple:
    """Ключ збігу з базлайном: ШИРИНА+мова+сторінка+тип+текст+WIDGET-шлях. БЕЗ пікселів
    (ширини дрейфують від DPI/шрифта — інакше базлайн ламався б на кожному
    масштабі). Фікс 5: widget-шлях (_widget_path: ланцюг класів+objectName) у
    ключі — інакше один забазлайнений напис (напр. «Delete» на сторінці) «прощав»
    би ВСІ однакові написи там само, і нове обрізання іншого контролю з тим самим
    текстом проходило б --strict непомітно.

    ШИРИНА в ключі (25.07) — обов'язкова: тісний ряд на 1000, який свідомо
    прийняли в базлайн, інакше «прощав» би НОВЕ обрізання того самого контролю
    на 1856, де місця вдосталь і виправдання немає."""
    return (v.get("width"), v.get("lang"), v.get("page"), v.get("type"),
            (v.get("text") or "").strip(), v.get("widget") or "")


def load_baseline() -> set:
    if not BASELINE_PATH.exists():
        return set()
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {_match_key(v) for v in data.get("entries", [])}


# ─────────────────────── самоперевірка детектора (TDD-доказ) ───────────────────────
def selfcheck(app, qt) -> int:
    """Синтетичне вікно: НАВМИСНО обрізана кнопка (30px, довгий текст) + нормальна,
    плюс ДВА класи icon_clipped: (а) геометрія — кнопка 10×10 з іконкою 24×24
    (канва>кнопка) і нормальна іконкова; (б) гліф-кліп — РЕАЛЬНИЙ гліф
    fa6s.volume-high (співвідношення 1,25) у КВАДРАТНІЙ канві 16×16 (правий край
    зникає) проти прямокутної 20×16 (фікс icon_w — цілий). Детектор МУСИТЬ зловити
    обрізані й НЕ чіпати нормальні. Доказ, що не пустушка."""
    from PySide6.QtWidgets import (
        QPushButton, QToolButton, QWidget, QVBoxLayout)
    from PySide6.QtGui import QIcon, QPixmap
    from PySide6.QtCore import QSize
    import qtawesome as qta
    from fronts.desktop.theme import QSS, load_fonts
    from PySide6.QtCore import Qt
    load_fonts()
    app.setStyleSheet(QSS)

    host = QWidget()
    lay = QVBoxLayout(host)
    clipped = QPushButton("Дуже довгий підпис кнопки, що не влізе у тридцять пікселів")
    clipped.setObjectName("clipped_btn")
    clipped.setFixedWidth(30)
    lay.addWidget(clipped)
    normal = QPushButton("ОК")
    normal.setObjectName("normal_btn")
    normal.setFixedWidth(160)
    lay.addWidget(normal)

    # синтетична іконка (біле коло на прозорому) для icon_clipped-кейсів
    pm = QPixmap(24, 24)
    pm.fill(Qt.white)
    ico = QIcon(pm)
    icon_clip = QToolButton()
    icon_clip.setObjectName("icon_clip_btn")
    icon_clip.setIcon(ico)
    icon_clip.setIconSize(QSize(24, 24))       # іконка більша за кнопку → ріжеться
    icon_clip.setFixedSize(10, 10)
    lay.addWidget(icon_clip)
    icon_ok = QToolButton()
    icon_ok.setObjectName("icon_ok_btn")
    icon_ok.setIcon(ico)
    icon_ok.setIconSize(QSize(16, 16))         # іконка з запасом вміщається
    icon_ok.setFixedSize(34, 30)
    lay.addWidget(icon_ok)

    # (б) РЕАЛЬНИЙ гліф-кліп: fa6s.volume-high (широкий, viewBox 640×512) у
    # КВАДРАТНІЙ канві 16×16 → правий край хвиль зникає; у прямокутній 20×16
    # (фікс icon_w) — цілий. Той самий гліф і розміри, що у _IconButton.
    vol_ico = qta.icon("fa6s.volume-high", color="white")
    glyph_clip = QToolButton()
    glyph_clip.setObjectName("glyph_clip_btn")
    glyph_clip.setIcon(vol_ico)
    glyph_clip.setIconSize(QSize(16, 16))      # КВАДРАТ → гліф ширший за канву
    glyph_clip.setFixedSize(30, 28)
    lay.addWidget(glyph_clip)
    glyph_ok = QToolButton()
    glyph_ok.setObjectName("glyph_ok_btn")
    glyph_ok.setIcon(vol_ico)
    glyph_ok.setIconSize(QSize(20, 16))        # icon_w=20 → гліф вміщається
    glyph_ok.setFixedSize(34, 28)
    lay.addWidget(glyph_ok)

    host.setAttribute(Qt.WA_DontShowOnScreen, True)
    host.resize(240, 220)
    host.show()
    _process(app, 4)

    v_clipped = check_widget(clipped, qt)
    v_normal = check_widget(normal, qt)
    v_icon_clip = check_widget(icon_clip, qt)
    v_icon_ok = check_widget(icon_ok, qt)
    v_glyph_clip = check_widget(glyph_clip, qt)
    v_glyph_ok = check_widget(glyph_ok, qt)
    host.close()

    ok = True
    if not any(v["type"] == "text_clipped" for v in v_clipped):
        print("SELFCHECK FAIL: обрізану кнопку НЕ зловлено")
        ok = False
    else:
        print("selfcheck: обрізану кнопку зловлено ✓")
    if v_normal:
        print(f"SELFCHECK FAIL: нормальну кнопку хибно позначено: {v_normal}")
        ok = False
    else:
        print("selfcheck: нормальну кнопку не чіпано ✓")
    if not any(v["type"] == "icon_clipped" for v in v_icon_clip):
        print("SELFCHECK FAIL: обрізану іконку (10×10 / 24, канва>кнопка) НЕ зловлено")
        ok = False
    else:
        print("selfcheck: обрізану іконку (канва>кнопка) зловлено ✓")
    if any(v["type"] == "icon_clipped" for v in v_icon_ok):
        print(f"SELFCHECK FAIL: нормальну іконку хибно позначено: {v_icon_ok}")
        ok = False
    else:
        print("selfcheck: нормальну іконку не чіпано ✓")
    if not any(v["type"] == "icon_clipped" for v in v_glyph_clip):
        print("SELFCHECK FAIL: гліф-кліп (volume-high у 16×16) НЕ зловлено")
        ok = False
    else:
        print("selfcheck: гліф-кліп (volume-high у квадраті) зловлено ✓")
    if any(v["type"] == "icon_clipped" for v in v_glyph_ok):
        print(f"SELFCHECK FAIL: цілий гліф (20×16) хибно позначено: {v_glyph_ok}")
        ok = False
    else:
        print("selfcheck: цілий гліф (20×16, icon_w) не чіпано ✓")

    # Фікс 3: зламана фабрика діалогу МУСИТЬ ловитись як dialog_broken (валить
    # --strict), а не тихо пропускатись. Синтетична фабрика, що кидає RuntimeError.
    broken_results = []

    def _broken_factory():
        raise RuntimeError("синтетична бита фабрика (selfcheck)")

    _scan_one_dialog("broken_selfcheck", _broken_factory, "uk",
                     broken_results, app, qt)
    if any(v["type"] == "dialog_broken" for v in broken_results):
        print("selfcheck: зламану фабрику діалогу зловлено (dialog_broken) ✓")
    else:
        print("SELFCHECK FAIL: зламану фабрику діалогу НЕ зловлено")
        ok = False
    return 0 if ok else 1


# ─────────────────────── головний потік ───────────────────────
def _init_app(qt):
    import ctypes
    from PySide6.QtGui import QGuiApplication, QIcon
    from PySide6.QtCore import Qt
    QApplication = qt["QApplication"]
    if sys.platform == "win32":
        try:
            shcore = ctypes.windll.shcore
            shcore.SetProcessDpiAwareness.argtypes = (ctypes.c_int,)
            shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from fronts.desktop.theme import QSS, load_fonts
    load_fonts()
    app.setStyleSheet(QSS)
    try:
        app.setWindowIcon(QIcon(str(ROOT / "assets" / "balachky.ico")))
    except Exception:
        pass
    return app


def _print_summary(results, new_only=None):
    by_type = {}
    for v in results:
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1
    print("\n──────────── ВІЗУАЛЬНИЙ ГЕЙТ: ПІДСУМОК ────────────")
    print(f"  усього порушень: {len(results)}")
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        print(f"    {t}: {n}")
    shown = new_only if new_only is not None else results
    if new_only is not None:
        print(f"  НОВИХ поза базлайном: {len(new_only)}")
    for v in shown[:60]:
        txt = (v.get("text") or "").replace("\n", " ")
        if len(txt) > 54:
            txt = txt[:51] + "…"
        # ширина — ПЕРШОЮ: без неї порушення неможливо відтворити руками
        print(f"    [w={v.get('width', '?')} {v['lang']}] {v['page']} · {v['type']} · "
              f"needed={v.get('needed', 0)} avail={v.get('avail', 0)} · «{txt}»")
    if len(shown) > 60:
        print(f"    … і ще {len(shown) - 60}")
    print("───────────────────────────────────────────────────")


def main():
    # Консоль Windows часто cp1251 — україномовний вивід і галочки/хрестики
    # інакше валять UnicodeEncodeError (відома грабля CLI). Перемикаємо на utf-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Візуальний гейт Балачок")
    parser.add_argument("--strict", action="store_true",
                        help="нові порушення поза базлайном → exit 1")
    parser.add_argument("--selfcheck", action="store_true",
                        help="самоперевірка детектора (синтетична кнопка)")
    parser.add_argument("--langs", default="uk,en",
                        help="мови через кому (типово uk,en)")
    parser.add_argument("--widths",
                        default=",".join(str(w) for w in GATE_WIDTHS),
                        help="ширини вікна через кому (типово %(default)s: "
                             "мінімум вікна, ноутбук, 1920-перегляд)")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("Візуальний гейт розрахований на Windows (Segoe UI/DWM).")
        # не валимо гейт на не-Windows — крок і руками йде
        return 0

    qt = _lazy_qt()
    app = _init_app(qt)

    if args.selfcheck:
        code = selfcheck(app, qt)
        os_exit(code)
        return code

    # Колір інтерфейсу для цього прогону — задає ЛИШЕ середовище (BALACHKY_UI_COLOR,
    # старий BALACHKY_FORCE_NIGHT — сумісність), theme.py читає його на імпорті.
    # Тут лише читаємо, ЩО вийшло, для підпису звіту — імпорт theme (перший тут,
    # до run_language) фіксує активний колір на весь прогін.
    color_label = _active_color_label()
    print(f"=== ВІЗУАЛЬНИЙ ГЕЙТ: варіант кольору = {color_label} ===")
    report_path = _report_path_for(color_label)

    from whisper_core import profiles
    sandbox = _make_sandbox()
    _orig_list = profiles.list_profiles
    profiles.list_profiles = lambda root=None: _orig_list(sandbox)

    results = []
    widths = [int(x.strip()) for x in args.widths.split(",") if x.strip()]
    try:
        for width in widths:
            for lang in [x.strip() for x in args.langs.split(",") if x.strip()]:
                print(f"── прогін: ширина {width} · мова {lang} · колір {color_label} ──")
                mark = len(results)
                run_language(lang, app, sandbox, qt, results, width)
                # ОДНЕ місце, де ширина потрапляє в порушення: інакше кожен із
                # десятка append-ів (сторінки, діалоги, стан-проби) мусив би
                # пам'ятати про поле, і будь-який новий probe тихо лишався б без
                # ширини — тобто без можливості відтворення.
                for v in results[mark:]:
                    v.setdefault("width", width)
    finally:
        profiles.list_profiles = _orig_list
        shutil.rmtree(sandbox, ignore_errors=True)

    report_path.write_text(
        json.dumps({"generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "color": color_label,
                    "widths": widths, "height": GATE_HEIGHT,
                    "count": len(results),
                    "skipped_dialogs": SKIPPED_DIALOGS,
                    "state_probes": STATE_PROBES,
                    "entries": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"\nЗвіт: {report_path}")

    baseline = load_baseline()
    new_only = [v for v in results if _match_key(v) not in baseline]
    _print_summary(results, new_only if args.strict else None)

    if args.strict:
        if new_only:
            print(f"\n❌ ВІЗУАЛЬНИЙ ГЕЙТ [{color_label}]: {len(new_only)} НОВИХ "
                  f"порушень поза базлайном ({BASELINE_PATH.name}).")
            os_exit(1)
        print(f"\n✅ ВІЗУАЛЬНИЙ ГЕЙТ [{color_label}]: нових порушень немає "
              f"(базлайн покриває {len(baseline)} відомих).")
        os_exit(0)
    os_exit(0)


def os_exit(code):
    """Завершити Qt/DXGI штатно, потім вийти без Python static teardown."""
    from PySide6.QtWidgets import QApplication
    import shiboken6

    sys.stdout.flush()
    sys.stderr.flush()
    app = QApplication.instance()
    if app is not None:
        _flush_deferred(app)
        app.quit()
        shiboken6.delete(app)
    os._exit(code)


if __name__ == "__main__":
    main()
