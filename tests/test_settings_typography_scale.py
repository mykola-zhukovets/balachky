"""Тести шкали групування сторінки Налаштувань (спека вигляду 30.07.2026).

Ловлять САМЕ те, що скаржився власник: злиття рівнів шкали (назва параметра
візуально не відрізняється від пояснення). Перевіряємо ФАКТИЧНІ числа з
build_qss() — не tr(ключ) сам із собою (тавтологія нічого не ловить, бо
ключ i18n не змінюється при поламаній шкалі).
"""
import re

import pytest

from fronts.desktop import theme


def _rule(qss: str, selector: str) -> str:
    """Тіло одного QSS-правила (без фігурних дужок) за селектором."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", qss)
    assert m, f"правило {selector!r} не знайдено у build_qss()"
    return m.group(1)


def _font_size(rule_body: str) -> int:
    m = re.search(r"font-size:\s*(\d+)px", rule_body)
    assert m, f"font-size не знайдено у {rule_body!r}"
    return int(m.group(1))


def _font_weight(rule_body: str) -> int:
    m = re.search(r"font-weight:\s*(\d+)", rule_body)
    assert m, f"font-weight не знайдено у {rule_body!r}"
    return int(m.group(1))


def _color(rule_body: str) -> str:
    m = re.search(r"(?<!background: )color:\s*(#[0-9A-Fa-f]{6}|rgba?\([^)]*\))", rule_body)
    assert m, f"color не знайдено у {rule_body!r}"
    return m.group(1)


@pytest.fixture(autouse=True)
def reset_theme():
    """Скинути тему до classic після кожного тесту (як у test_ui_color_picker)."""
    yield
    theme.set_ui_color("classic")


def test_option_label_strictly_bigger_than_helper():
    """Рівень 3 (назва параметра) МУСИТЬ бути кеглем більшим за рівень 4
    (пояснення) — інакше вони зливаються (скарга власника 30.07)."""
    qss = theme.build_qss()
    option_size = _font_size(_rule(qss, 'QLabel[level="option_label"]'))
    helper_size = _font_size(_rule(qss, 'QLabel[level="helper"]'))
    assert option_size > helper_size, (
        f"option_label ({option_size}px) має бути більшим за helper "
        f"({helper_size}px) — інакше назва параметра зливається з поясненням")


def test_option_label_and_helper_differ_in_color():
    """Розрив 3→4 — потрійний (порада §1): серед іншого колір МУСИТЬ
    відрізнятись, не лише кегль."""
    qss = theme.build_qss()
    option_color = _color(_rule(qss, 'QLabel[level="option_label"]'))
    helper_color = _color(_rule(qss, 'QLabel[level="helper"]'))
    assert option_color != helper_color, (
        "option_label і helper мають РІЗНИЙ колір — інакше різниця лише в "
        "кеглі й на мілких дисплеях знову зливається")


def test_group_title_is_bolder_and_smaller_than_card_title():
    """Рівень 2 (назва групи) не повинен бути ідентичним рівню картки
    (16/600) — саме цей збіг спричиняв злиття «_subhead» у старому коді
    (спека §1.3.2: _subhead мав level="block", той самий, що й заголовок
    картки)."""
    qss = theme.build_qss()
    card_body = _rule(qss, 'QLabel[level="block"]')
    group_body = _rule(qss, 'QLabel[level="group_title"]')
    card_size = _font_size(card_body)
    group_size = _font_size(group_body)
    assert group_size != card_size, (
        "group_title і card title (level=block) мають РІЗНИЙ кегль — інакше "
        "назва групи всередині картки зливається із заголовком картки")


def test_group_gap_bigger_than_row_gap():
    """Повітря нерівне навмисно (порада §2): між групами (картками) МАЄ бути
    помітно більше повітря, ніж усередині групи (між параметрами) — інакше
    групи не читаються як окремі плями."""
    assert theme.GROUP_GAP > theme.ROW_GAP * 2, (
        f"GROUP_GAP ({theme.GROUP_GAP}) має бути щонайменше вдвічі більшим за "
        f"ROW_GAP ({theme.ROW_GAP}), інакше груп не видно")


def test_nested_card_has_left_border_not_bare_indent():
    """Вкладені елементи (кнопки завантаження) тримає КОНТЕЙНЕР — QFrame з
    border-left, а не голий правий відступ (порада §4)."""
    qss = theme.build_qss()
    body = _rule(qss, 'QFrame[nestedCard="true"]')
    assert "border-left" in body, "nestedCard мусить мати border-left — зв'язок-гребінець з батьком"


def test_make_group_title_uppercases_text():
    """make_group_title() форсує UPPERCASE (Qt QSS не має text-transform,
    тож перевіряємо реальний QFont.capitalization())."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont
    QApplication.instance() or QApplication([])
    lbl = theme.make_group_title("тест")
    assert lbl.font().capitalization() == QFont.AllUppercase
    assert lbl.property("level") == "group_title"


# Мінімальний контраст галочка/пояснення (колірна вісь), окремо на КОЖЕН
# пресет — рецензія 30.07 (друга ніч) виміряла, що ця вісь має ФІЗИЧНУ стелю:
# HELPER_TEXT не може стати темнішим за межу WCAG 4.5:1 до власного тла
# (CARD/SURFACE), а ця межа й визначає, наскільки далеко колір пояснення
# може відійти від кольору назви. У холодних/фіолетових тонах (мала вага
# синього каналу у формулі яскравості) стеля НИЗЬКА (фіолетовий — 1.32:1) —
# це не помилка підбору, а математична межа за незмінного порогу 4.5:1 до
# тла (перевірено емпірично: theme._darkest_for_contrast уже шукає
# найтемніше можливе значення). Тому кожен поріг нижче — конкретне число,
# зняте з живих віджетів ПІСЛЯ фіксу (з невеликим запасом), а не єдина
# планка для всіх кольорів: якщо колір не може нести розрив сам, це замість
# нього несе вага шрифту (перевірено окремо нижче, WEIGHT_GAP_MIN).
_MIN_CB_HELPER_CONTRAST = {
    "classic": 1.55,
    "red":     1.45,
    "amber":   2.55,
    "green":   2.95,
    "teal":    3.05,
    "blue":    1.75,
    "purple":  1.25,   # найгірший випадок — фізична стеля кольору тут ~1.32:1
    "pink":    1.55,
}

# Мінімальна різниця font-weight() галочка/пояснення. Це РЕЗЕРВНИЙ канал:
# там, де колір фізично не може розійтись достатньо (фіолетовий/червоний/
# рожевий/класика/синій — усі нижче за "явно різне"), саме вага несе розрив
# 3→4 на око. 700 (назва) - 400 (пояснення) = 300 — навмисний запас, не
# найменше можливе число.
_WEIGHT_GAP_MIN = 300

_BG_READABILITY_MIN = 4.5  # WCAG 2.1 AA — ЦЮ межу пояснення до тла ЖОДЕН колір не сміє порушувати


@pytest.mark.parametrize("color", sorted(theme.PRESETS.keys()))
def test_real_checkbox_row_has_triple_gap_not_just_style_rules(color):
    """Ловить САМЕ те, що пропустили test_option_label_*: ~13 параметрів на
    вкладці «Запис і звук» — це нативний текст QCheckBox (SettingsPage._opt_row),
    а НЕ make_option_label(). Правила `QLabel[level="option_label"]` vs
    `QLabel[level="helper"]` можуть бути ідеальними, а фактичний рядок
    "галочка + пояснення" — все одно зливатися, якщо чекбокс не отримав тієї ж
    ваги/кольору. Тому будуємо СПРАВЖНІЙ рядок через реальну фабрику
    (SettingsPage._opt_row), стилюємо його СПРАВЖНІМ build_qss() і читаємо
    ФАКТИЧНІ font()/palette() віджетів — а не tr(ключ) сам із собою.

    Прогонятись мусить по ВСІХ 8 пресетах кольору (не лише дефолтному
    classic): conftest.py._reset_process_state автоматично повертає тему на
    classic у СЕТАПІ кожного тесту (fixture autouse), АЛЕ це відбувається ДО
    тіла тесту — виклик theme.set_ui_color(color) тут, усередині тіла,
    виконується ПІСЛЯ того скидання і тому чесно перемикає активну палітру
    (перевірено: рецензія 30.07 спіймала версію без цього виклику — вона мовчки
    гоняла лише classic і не бачила, що фіолетовий валить власний поріг)."""
    from PySide6.QtWidgets import QApplication, QCheckBox, QWidget

    from fronts.desktop.pages.settings import SettingsPage

    theme.set_ui_color(color)
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.build_qss())

    checkbox = QCheckBox("тестовий параметр")
    row = SettingsPage._opt_row(checkbox, "тестове пояснення")
    host = QWidget()
    host.setLayout(row)
    host.show()
    app.processEvents()

    helper = row.itemAt(row.count() - 1).widget()
    assert helper.property("level") == "helper", (
        "_opt_row мусить додавати make_helper_label() останнім віджетом рядка")

    checkbox.ensurePolished()
    helper.ensurePolished()
    app.processEvents()

    cb_font, helper_font = checkbox.font(), helper.font()
    cb_color = checkbox.palette().color(checkbox.foregroundRole())
    helper_color = helper.palette().color(helper.foregroundRole())

    assert cb_font.pixelSize() > helper_font.pixelSize(), (
        f"[{color}] кегль галочки ({cb_font.pixelSize()}px) має бути більшим за "
        f"пояснення ({helper_font.pixelSize()}px)")

    weight_gap = cb_font.weight() - helper_font.weight()
    assert weight_gap >= _WEIGHT_GAP_MIN, (
        f"[{color}] вага галочки ({cb_font.weight()}) мінус вага пояснення "
        f"({helper_font.weight()}) = {weight_gap} — менше за резервний поріг "
        f"{_WEIGHT_GAP_MIN}. Там, де колір фізично слабкий (напр. фіолетовий), "
        "саме вага мусить нести розрив 3→4.")
    assert cb_color.name() != helper_color.name(), (
        f"[{color}] колір галочки ({cb_color.name()}) і пояснення "
        f"({helper_color.name()}) ОДНАКОВІ")

    contrast = theme._contrast_ratio(
        (cb_color.red(), cb_color.green(), cb_color.blue()),
        (helper_color.red(), helper_color.green(), helper_color.blue()))
    min_required = _MIN_CB_HELPER_CONTRAST[color]
    assert contrast >= min_required, (
        f"[{color}] контраст галочка/пояснення ({contrast:.2f}:1) нижчий за "
        f"поріг цього кольору ({min_required}:1) — саме на це скаржився "
        "власник 30.07, і саме тут рецензія 30.07 (друга ніч) спіймала фіолетовий")

    # Читабельність пояснення до ВЛАСНОГО тла — червона лінія, якою НЕ можна
    # жертвувати заради розрізнюваності назва/пояснення (вимога рецензії).
    card_rgb = theme._token_rgb(theme._P["CARD"])
    surface_rgb = theme._token_rgb(theme._P["SURFACE"])
    helper_rgb = (helper_color.red(), helper_color.green(), helper_color.blue())
    helper_vs_card = theme._contrast_ratio(helper_rgb, card_rgb)
    helper_vs_surface = theme._contrast_ratio(helper_rgb, surface_rgb)
    assert helper_vs_card >= _BG_READABILITY_MIN, (
        f"[{color}] пояснення на картці ({helper_vs_card:.2f}:1) нижче за "
        f"WCAG AA {_BG_READABILITY_MIN}:1")
    assert helper_vs_surface >= _BG_READABILITY_MIN, (
        f"[{color}] пояснення на тлі вікна ({helper_vs_surface:.2f}:1) нижче за "
        f"WCAG AA {_BG_READABILITY_MIN}:1")

    host.close()


def test_indent_row_actually_offsets_child_widget():
    """indent_row() мусить РЕАЛЬНО зсунути дитину вправо в живому layout —
    QFrame.setContentsMargins() НЕ робить цього (перевірено емпірично: та
    інша дитина мали однаковий x()), тож ловимо саме фактичну геометрію
    після polish/processEvents, а не намір коду."""
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    app = QApplication.instance() or QApplication([])
    outer = QWidget()
    lay = QVBoxLayout(outer)
    lay.setContentsMargins(0, 0, 0, 0)
    parent_label = QLabel("parent")
    lay.addWidget(parent_label)
    child = theme.wrap_nested(QLabel("child"))
    lay.addLayout(theme.indent_row(child))
    outer.resize(400, 200)
    outer.show()
    app.processEvents()
    assert child.x() >= theme.NESTED_INDENT, (
        f"вкладена мікрокартка має бути зсунута щонайменше на "
        f"NESTED_INDENT ({theme.NESTED_INDENT}px), фактично x()={child.x()}")
    outer.close()
