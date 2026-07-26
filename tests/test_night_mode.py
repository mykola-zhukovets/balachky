"""feature/night-mode — нічний/червоний МОНО-режим інтерфейсу.

Перевіряємо три речі:
  1. Тумблер персиститься (Config round-trip) і перемикає активну тему.
  2. Нічна палітра — СПРАВДІ моно-червона: кожен видимий колір червоно-домінантний
     з нейтральним відтінком (G≈B), без білого/оливкового/помаранчевого/синього/
     зеленого. Той самий чек ВАЛИТЬ денну палітру (доказ, що не пустушка).
  3. Контраст тексту (тіло/приглушений) на нічному тлі ≥ 4.5:1 (WCAG AA).
"""
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fronts.desktop import theme
from whisper_core.config import Config


# ─────────────────────── розбір кольору → (r,g,b) ───────────────────────
def _rgb(value):
    """Рядок #RRGGBB / rgba(r,g,b,a) або кортеж → (r,g,b). None — не колір."""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return int(value[0]), int(value[1]), int(value[2])
    if not isinstance(value, str):
        return None
    v = value.strip()
    m = re.fullmatch(r"#([0-9A-Fa-f]{6})", v)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)", v)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


# Токени, що НЕ є кольорами (шляхи до svg/png, розміри, градієнт, url).
_NON_COLOR = {"_CHECK", "_CHEVRON", "_RADIO_OFF", "_RADIO_ON", "_RADIO_HOV",
              "_RADIO_DIS", "_R_CARD", "_R_CTRL", "_TILE_IMG", "_GLASS_FILL"}

_DARK_MAX = 40      # макс. канал ≤ цього → «майже-чорне» тло (дозволене)
_RED_MARGIN = 40    # R має домінувати над G/B хоча б на стільки (не білий/сірий)
_HUE_NEUTRAL = 24   # |G−B| ≤ цього → нейтральний червоний (не помаранч./синьо-зсув)


def _is_red_only(rgb) -> bool:
    """Колір придатний для нічного (моно-червоного) режиму?

    Або майже-чорний (усі канали низькі, R не менший за G/B), або яскраво-
    червоний: R — найбільший, домінує над G/B (не білий/сірий) і відтінок
    нейтральний (G≈B — не зсунутий у помаранч/жовте чи в синє/пурпур)."""
    r, g, b = rgb
    m = max(r, g, b)
    if m <= _DARK_MAX:
        return r >= g and r >= b          # темне тло без зелено/синьо-домінанти
    return (r == m
            and (r - max(g, b)) >= _RED_MARGIN
            and abs(g - b) <= _HUE_NEUTRAL)


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast(fg, bg) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


class NightModeConfigTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(Config().night_mode)

    def test_roundtrip_persists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            c = Config()
            c.night_mode = True
            c.save(p)
            self.assertTrue(Config.load(p).night_mode)
            # і назад
            c.night_mode = False
            c.save(p)
            self.assertFalse(Config.load(p).night_mode)


class NightModeThemeTests(unittest.TestCase):
    def tearDown(self):
        theme.set_mode(False)      # не лишати глобальну тему нічною для інших тестів

    def test_set_mode_swaps_palette(self):
        theme.set_mode(False)
        self.assertFalse(theme.is_night())
        day_gold = theme.GOLD
        theme.set_mode(True)
        self.assertTrue(theme.is_night())
        self.assertNotEqual(theme.GOLD, day_gold)
        theme.set_mode(False)
        self.assertEqual(theme.GOLD, day_gold)

    def test_night_palette_is_red_only(self):
        """Кожен колірний токен нічної палітри — червоно-домінантний / майже-чорний."""
        bad = []
        for name, value in theme._NIGHT.items():
            if name in _NON_COLOR:
                continue
            rgb = _rgb(value)
            if rgb is None:
                continue
            if not _is_red_only(rgb):
                bad.append((name, value, rgb))
        self.assertEqual(bad, [], f"не-червоні токени в нічній палітрі: {bad}")

    def test_checker_has_teeth_day_fails(self):
        """Той самий чек МУСИТЬ завалити денну палітру (оливка/золото/білий),
        інакше він нічого не доводить."""
        offenders = [n for n, v in theme._DAY.items()
                     if n not in _NON_COLOR and _rgb(v) and not _is_red_only(_rgb(v))]
        # золото, білий текст, оливкове тло тощо — мають не пройти
        self.assertIn("GOLD", offenders)
        self.assertIn("TEXT_STRONG", offenders)
        self.assertIn("SURFACE", offenders)

    def test_night_qss_has_no_nonred_light(self):
        """Зібрана нічна таблиця стилів не містить білого/оливкового/помаранчевого
        світла: жодних rgba(255,255,255…), #FFFFFF, оливкових hex чи 243,146,0."""
        theme.set_mode(True)
        css = theme.build_qss() + theme.build_qss_mica()
        theme.set_mode(False)
        for forbidden in ("rgba(255,255,255", "#FFFFFF", "#FFFFFF".lower(),
                          "243,146,0", "#4D4634", "#3A3528", "#2E2A1F",
                          "#2c2718", "#FFC766", "#E6E5D1", "#D6CDB8"):
            self.assertNotIn(forbidden, css,
                             f"нічний QSS містить не-червоне світло: {forbidden}")
        # Тайл-тло малює _TiledStack.paintEvent (main_window), НЕ QSS; уночі він
        # малює лише червоно-темну базу (is_night → без тайла). Тож у нічному QSS
        # оливкового bg-tile.png бути не має — guard проти регресу з поверненням
        # тайла в QSS нічної теми.
        self.assertNotIn("bg-tile", css)

    def test_night_text_contrast_wcag_aa(self):
        """Тіло й приглушений текст на нічному тлі ≥ 4.5:1 (WCAG AA)."""
        night = theme._NIGHT
        bg = _rgb(night["SURFACE"])
        body = _contrast(_rgb(night["TEXT_BODY"]), bg)
        muted = _contrast(_rgb(night["TEXT_MUTED"]), bg)
        strong = _contrast(_rgb(night["TEXT_STRONG"]), bg)
        self.assertGreaterEqual(round(body, 2), 4.5, f"тіло: {body:.2f}:1")
        self.assertGreaterEqual(round(muted, 2), 4.5, f"приглушений: {muted:.2f}:1")
        self.assertGreaterEqual(round(strong, 2), 4.5, f"заголовок: {strong:.2f}:1")

    def test_night_day_geometry_identical(self):
        """Нічна тема — суто колірний своп: жодних змін розмірів/відступів/радіусів,
        тож візуальний гейт (обрізання) зелений в ОБОХ темах за побудовою."""
        theme.set_mode(False)
        day = theme.build_qss()
        theme.set_mode(True)
        night = theme.build_qss()
        theme.set_mode(False)
        # прибираємо всі значення кольорів → лишається структура + геометрія
        def skeleton(css):
            css = re.sub(r"#[0-9A-Fa-f]{6}", "#", css)
            css = re.sub(r"rgba?\([^)]*\)", "rgba()", css)
            css = re.sub(r"qradialgradient\([^)]*\)", "grad()", css)
            # svg/png-шляхи (день і ніч ведуть на різні файли) → єдиний токен;
            # _TILE_IMG день=url("…"), ніч=none — після обох замін теж збігаються.
            css = re.sub(r'url\("[^"]*"\)', "URL", css)
            css = css.replace("none", "URL")
            return css
        self.assertEqual(skeleton(day), skeleton(night))


class NightModeAssetTests(unittest.TestCase):
    """Статичні SVG-іконки контролів (радіо/чекбокс/шеврон) — окрема сліпа зона:
    їхні кольори живуть у файлі, а не в палітрі, тож `test_night_palette_is_red_only`
    (що читає лише _NIGHT) їх НЕ бачить (шляхи сидять у _NON_COLOR). Раніше саме
    через це золота крапка радіо світилася при 1596 зелених тестах. Тут перевіряємо
    самі АКТИВНІ файли: у нічному режимі QSS має вести на `*-night.svg`, і кожен
    колір у них — червоно-домінантний."""

    def tearDown(self):
        theme.set_mode(False)

    def test_night_icons_exist_and_are_red_only(self):
        theme.set_mode(True)
        for tok, base in theme._ICON_TOKENS.items():
            path = Path(theme._P[tok])
            self.assertTrue(
                path.name.endswith("-night.svg"),
                f"{tok}: нічний QSS має вести на *-night.svg, а веде на {path.name}")
            self.assertTrue(path.exists(), f"немає нічного асета: {path}")
            svg = path.read_text(encoding="utf-8")
            for hexval in re.findall(r"#[0-9A-Fa-f]{6}", svg):
                rgb = _rgb(hexval)
                self.assertTrue(
                    _is_red_only(rgb),
                    f"{path.name}: не-червоний колір {hexval} у нічній іконці")

    def test_day_icons_used_in_day_mode(self):
        """Дзеркало: у денному режимі QSS веде на денні файли (без регресу дня)."""
        theme.set_mode(False)
        for tok, base in theme._ICON_TOKENS.items():
            self.assertEqual(Path(theme._P[tok]).name, f"{base}.svg")

    def test_line_hilite_is_red_only(self):
        """Підсвіт активного рядка розшифровки (meeting.py) у нічній палітрі —
        червоний (денний #523A1E — теплий бурштин — валив би моно-червоне)."""
        self.assertTrue(_is_red_only(_rgb(theme._NIGHT["_LINE_HILITE"])))


# ─── піксельний скан растра (для живих віджетів; толерантний до антиаліасу) ───
def _scan_lit(pm, alpha_min=170, dark=48, margin=26, hue=42):
    """(світлих_пікселів, не-червоних) у растрі. «Не-червоний світлий» = яскравий
    піксель (max-канал > dark), що НЕ червоно-домінантний: R не найбільший, або R
    майже не переважає G/B (margin), або відтінок зсунутий (|G−B|>hue — золото/
    синє). Антиаліас червоного (G≈B) проходить. Дзеркалить перевірку live-скрипта."""
    from PySide6.QtGui import QImage
    img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
    n_lit = n_bad = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() < alpha_min:
                continue
            r, g, b = c.red(), c.green(), c.blue()
            m = max(r, g, b)
            if m <= dark:
                continue
            n_lit += 1
            if not (r == m and (r - max(g, b)) >= margin and abs(g - b) <= hue):
                n_bad += 1
    return n_lit, n_bad


class NightModeLiveWidgetTests(unittest.TestCase):
    """Раніше-пропущені елементи, які незалежний суд спіймав денними при ЖИВОМУ
    свопі (без перезапуску): ⓘ-іконка налаштувань (info_hint), круглий значок-
    посилання (round_social) і іконка вікна/таскбару (app_icon). Кожен ставив
    колір ОДИН раз при побудові → лишався денним. Тут будуємо РЕАЛЬНІ віджети,
    свопимо тему тим самим шляхом, що reapply_theme (theme.apply_theme запускає
    restyle-хуки), і перевіряємо ПІКСЕЛІ: удень серед світлого є не-червоне
    (золото/хакі — доказ, що чек має зуби), уночі — жодного не-червоного пікселя."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        theme.set_mode(False)
        # ізоляція: свопимо лише хуки, зареєстровані у ЦЬОМУ тесті (не чужі віджети)
        self._hook_backup = list(theme._restyle_hooks)
        theme._restyle_hooks.clear()

    def tearDown(self):
        theme.set_mode(False)
        theme._restyle_hooks[:] = self._hook_backup

    def _swap_to_night(self):
        theme.apply_theme(self._app, True)     # той самий рушій, що reapply_theme
        self.assertTrue(theme.is_night())

    def test_info_hint_icon_repaints_red_on_live_swap(self):
        from PySide6.QtCore import QSize
        from fronts.desktop.pages.settings import info_hint
        btn = info_hint("hint_model", clickable=True, title_key="hint_model_title")
        day_lit, day_bad = _scan_lit(btn.icon().pixmap(QSize(64, 64)))
        self.assertGreater(day_bad, 0, "денна ⓘ-іконка мусить бути не-червоною (хакі)")
        self._swap_to_night()
        night_lit, night_bad = _scan_lit(btn.icon().pixmap(QSize(64, 64)))
        self.assertGreater(night_lit, 0, "нічна ⓘ-іконка не намалювалась")
        self.assertEqual(night_bad, 0,
                         f"ⓘ-іконка лишилась з не-червоним світлом: {night_bad} пікс")

    def test_round_social_icon_repaints_red_on_live_swap(self):
        from PySide6.QtCore import QSize
        from fronts.desktop.pages.settings import round_social
        btn = round_social("fa6b.github", "https://example/x", name="GitHub")
        day_lit, day_bad = _scan_lit(btn.icon().pixmap(QSize(64, 64)))
        self.assertGreater(day_bad, 0, "денний значок-посилання мусить бути не-червоним")
        self._swap_to_night()
        night_lit, night_bad = _scan_lit(btn.icon().pixmap(QSize(64, 64)))
        self.assertGreater(night_lit, 0, "нічний значок-посилання не намалювався")
        self.assertEqual(night_bad, 0,
                         f"значок «Про автора» лишився не-червоним: {night_bad} пікс")

    def test_round_social_hover_qss_repaints_red_on_live_swap(self):
        # hover-QSS значка-посилання пік золото при побудові (rgba(243,146,0,...)).
        # QSS не перечитує тему сам — restyle-хук мусить перебудувати його на свопі,
        # інакше рамка/тло на hover світили б золотом уночі.
        from fronts.desktop.pages.settings import round_social
        btn = round_social("fa6b.github", "https://example/x", name="GitHub")
        self.assertIn("243,146,0", btn.styleSheet(), "денний hover-QSS має нести золото")
        self._swap_to_night()
        ss = btn.styleSheet()
        r, g, b = theme.ACCENT_RGB
        self.assertIn(f"{r},{g},{b}", ss, "нічний hover-QSS має нести акцент палітри")
        self.assertNotIn("243,146,0", ss, "hover-QSS лишив денне золото вночі")
        self.assertTrue(_is_red_only(theme.ACCENT_RGB),
                        "нічний акцент hover має бути моно-червоним")

    def test_window_icon_is_red_only_in_night(self):
        from PySide6.QtCore import QSize
        from fronts.desktop.main_window import app_icon
        from whisper_core.paths import asset_root
        ico = asset_root() / "assets" / "balachky.ico"
        # день: оригінальний золото-жовтий жук → серед світлого є не-червоне
        day_lit, day_bad = _scan_lit(app_icon(ico).pixmap(QSize(64, 64)))
        self.assertGreater(day_lit, 0, "денна іконка вікна не намалювалась")
        self.assertGreater(day_bad, 0, "денна .ico мусить нести не-червоне (золото)")
        # ніч: моно-червоний силует → жодного не-червоного пікселя
        theme.set_mode(True)
        night_lit, night_bad = _scan_lit(app_icon(ico).pixmap(QSize(64, 64)))
        self.assertGreater(night_lit, 0, "нічна іконка вікна не намалювалась")
        self.assertEqual(night_bad, 0,
                         f"іконка вікна лишилась не-червоною: {night_bad} пікс")


class NightModeAccentLinkTests(unittest.TestCase):
    """Rich-text лінки з inline-кольором акценту через токен %%ACCENT%% в i18n
    (ліцензія, «Releases», ліцензія моделі). QPalette.Link жива зміна теми НЕ
    перечитує, тож колір вшито в текст — і має свопитись на нічний акцент при
    re-run tr(). Удень — денне золото #FFC766; уночі — нічний акцент, і ЖОДНОГО
    денного золота (ні #FFC766, ні #F39200)."""

    def tearDown(self):
        theme.set_mode(False)

    def _accent_keys(self):
        from fronts.desktop.i18n import tr
        yield tr("set_license")
        yield tr("set_upd_hint")
        yield tr("dl_consent_license", url="https://example/x", license="X")

    def test_day_links_carry_day_gold(self):
        theme.set_mode(False)
        for s in self._accent_keys():
            self.assertIn("#FFC766", s, "денний лінк без денного золота")

    def test_night_links_have_no_day_gold(self):
        theme.set_mode(True)
        night_accent = theme.GOLD_EYEBROW
        self.assertTrue(_is_red_only(_rgb(night_accent)))
        for s in self._accent_keys():
            self.assertIn(night_accent, s, "нічний лінк без нічного акценту")
            self.assertNotIn("#FFC766", s, "нічний лінк лишив денне золото #FFC766")
            self.assertNotIn("#F39200", s, "нічний лінк лишив денне золото #F39200")


if __name__ == "__main__":
    unittest.main()
