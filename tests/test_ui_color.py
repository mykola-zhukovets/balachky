"""feature/ui-color — персоналізація кольору інтерфейсу (рішення Миколи 25.07):
«зроби червоний режим. І так само, щоб людина могла з палітри будь-який інший
колір вибрати» / «Тільки не називається нічним режимом. А просто вибір
кольору». _NIGHT лишається еталоном («червоний»); решта відтінків — той самий
_NIGHT зі зсунутим ТОНОМ (HSV), з підтягуванням яскравості там, де WCAG-
контраст після зсуву не дотягує.

Перевіряємо:
  1. 'classic' і 'red' пресети — наявні _DAY/_NIGHT БЕЗ ЗМІН (жодного регресу).
  2. Зсув тону зберігає альфу байт-у-байт і не чіпає нейтральні кольори.
  3. Контраст WCAG 2.1 AA (>=4.5:1) виконаний для КОЖНОГО готового пресету і
     для 36 довільних тонів (через 10 градусів) — доказ, що будь-який вибір
     користувача читабельний.
  4. Міграція старого конфігу night_mode=true -> 'red'.
  5. Іконки контролів перефарбовуються й кешуються; помилка запису не валить
     застосунок (тихий відкат на червоні іконки).
  6. Мутаційні гарантії: поріг контрасту зафіксований на 4.5; підтягування
     яскравості справді потрібне (без нього конкретний тон не проходить).
"""
import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fronts.desktop import theme


# ─────────────── незалежна (від theme.py) перевірка контрасту ───────────────
# Навмисно НЕ використовуємо theme._contrast_ratio/_relative_luminance — якщо
# в реалізації зламається формула, цей окремий розрахунок має спіймати це.
def _rgb(value):
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


def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb):
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast(fg, bg):
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


_PAIRS = [
    ("TEXT_BODY", "CARD"), ("TEXT_BODY", "SURFACE"),
    ("TEXT_STRONG", "CARD"), ("TEXT_STRONG", "SURFACE"),
    ("TEXT_MUTED", "CARD"), ("TEXT_MUTED", "SURFACE"),
    ("TEXT_ON_GOLD", "GOLD"),
]


def _assert_palette_contrast(test, palette, label):
    for fg_key, bg_key in _PAIRS:
        ratio = _contrast(_rgb(palette[fg_key]), _rgb(palette[bg_key]))
        test.assertGreaterEqual(
            round(ratio, 2), 4.5,
            f"{label}: {fg_key} на {bg_key} — {ratio:.2f}:1 (< 4.5:1 WCAG AA)")


class PresetsUnchangedTests(unittest.TestCase):
    """Класичний і червоний — наявні _DAY/_NIGHT без жодних змін."""

    def test_classic_is_day_byte_for_byte(self):
        self.assertEqual(theme.PRESETS["classic"], theme._DAY)

    def test_red_is_night_byte_for_byte(self):
        self.assertEqual(theme.PRESETS["red"], theme._NIGHT)

    def test_all_named_presets_present(self):
        expected = {"classic", "red", "amber", "green", "teal", "blue", "purple", "pink"}
        self.assertEqual(expected, set(theme.PRESETS.keys()))


class HueShiftMechanicsTests(unittest.TestCase):
    def test_neutral_colors_unaffected(self):
        for neutral in ("#FFFFFF", "#000000", "#808080", "#2C2C2C"):
            for hue in (0, 45, 120, 200, 300):
                self.assertEqual(theme._shift_token(neutral, hue), neutral,
                                  f"{neutral} @ {hue} мусив лишитись нейтральним")

    def test_alpha_preserved_byte_for_byte(self):
        cases = ["rgba(255,77,77,0.283)", "rgba(0,0,0,0.22)", "rgba(255,90,90,0.06)"]
        for original in cases:
            m = re.search(r",\s*([\d.]+)\)", original)
            alpha_src = m.group(1)
            for hue in (10, 130, 260):
                shifted = theme._shift_token(original, hue)
                m2 = re.search(r",\s*([\d.]+)\)", shifted)
                self.assertEqual(m2.group(1), alpha_src,
                                  f"альфа змінилась: {original} -> {shifted}")

    def test_rgb_tuple_shift_roundtrips_format(self):
        shifted = theme._shift_token((255, 77, 77), 180)
        self.assertIsInstance(shifted, tuple)
        self.assertEqual(len(shifted), 3)

    def test_non_color_token_untouched(self):
        self.assertEqual(theme._shift_token('url("x.png")', 180), 'url("x.png")')
        self.assertEqual(theme._shift_token("none", 180), "none")

    def test_tile_img_token_not_touched_by_hue_build(self):
        for hue in (0, 88, 200):
            p = theme.build_palette_for_hue(hue) if hue else theme.PRESETS["red"]
            self.assertEqual(p["_TILE_IMG"], theme._NIGHT["_TILE_IMG"])

    def test_hue_zero_reproduces_red_palette(self):
        """Тон 0 (еталонний червоний тон _NIGHT) — зсув тотожний (S/V ті самі,
        H той самий => той самий колір, лише через round-trip HSV)."""
        built = theme.build_palette_for_hue(0)
        for key, value in theme._NIGHT.items():
            if key == "_TILE_IMG":
                continue
            rgb_a, rgb_b = _rgb(value), _rgb(built[key])
            if rgb_a is None:
                continue
            # округлення HSV<->RGB може дати відхилення ±1 на канал
            self.assertTrue(all(abs(a - b) <= 1 for a, b in zip(rgb_a, rgb_b)),
                             f"{key}: {value} -> {built[key]}")

    def test_invalid_hue_raises(self):
        for bad in (-1, 360, 400, 1.5, "45", True):
            with self.assertRaises(ValueError, msg=f"hue={bad!r} мав кинути ValueError"):
                theme.build_palette_for_hue(bad)


class ContrastAllPresetsTests(unittest.TestCase):
    """Доказ, що БУДЬ-ЯКИЙ вибір користувача читабельний: усі готові пресети
    і 36 довільних тонів (крок 10 градусів) проходять WCAG 2.1 AA."""

    def test_every_preset_meets_wcag_aa(self):
        for name, palette in theme.PRESETS.items():
            _assert_palette_contrast(self, palette, name)

    def test_36_arbitrary_hues_meet_wcag_aa(self):
        for hue in range(0, 360, 10):
            palette = theme.build_palette_for_hue(hue)
            _assert_palette_contrast(self, palette, f"hue={hue}")


class ContrastMutationGuardTests(unittest.TestCase):
    """Доказ, що перевірка контрасту МАЄ ЗУБИ (а не завжди зелена)."""

    def test_threshold_is_wcag_aa_4_5(self):
        # занижений поріг у коді (напр. 3.0) не зловився б жодним іншим тестом
        # у цьому файлі — пришпилюємо саме число.
        self.assertEqual(theme._CONTRAST_MIN, 4.5)

    def test_raw_shift_without_boost_fails_on_some_hue(self):
        """Без підтягування яскравості (лише зсув тону) конкретний тон НЕ
        проходить поріг — отже _fix_contrast справді щось виправляє, а не
        працює на порожньому місці. Якщо хтось приберe виклик _fix_contrast
        усередині build_palette_for_hue, test_36_arbitrary_hues_meet_wcag_aa
        (і цей тест побічно) почервоніє."""
        hue = 240   # синьо-фіолетовий — навіть чорний текст ледь дотягує
        raw = {
            key: (value if key in theme._NON_COLOR_TOKENS else theme._shift_token(value, hue))
            for key, value in theme._NIGHT.items()
        }
        offenders = [
            f"{fg}/{bg}" for fg, bg in _PAIRS
            if _contrast(_rgb(raw[fg]), _rgb(raw[bg])) < 4.5
        ]
        self.assertTrue(offenders, "цей тест мав знайти хоч один недотягнутий "
                                    "токен на hue=210 без підтягування яскравості")
        fixed = theme.build_palette_for_hue(hue)
        _assert_palette_contrast(self, fixed, f"hue={hue} (виправлено)")

    def test_boost_contrast_raises_when_unreachable(self):
        """Контраст, недосяжний навіть на крайньому V — чесна помилка з назвою
        токена й числом, а не мовчазне повернення нечитного кольору."""
        # той самий відтінок і насиченість фону й тексту -> контраст 1:1
        # завжди, недосяжний навіть на протилежному краю V, якщо порогом
        # поставити щось нереальне.
        with self.assertRaises(RuntimeError) as ctx:
            theme._boost_contrast("#808080", (128, 128, 128), target=21.0, label="тест")
        self.assertIn("тест", str(ctx.exception))


class ConfigMigrationTests(unittest.TestCase):
    def test_default_is_classic(self):
        cfg = SimpleNamespace()
        self.assertEqual(theme.resolve_ui_color(cfg), "classic")
        self.assertFalse(theme.night_enabled_for(cfg))

    def test_old_night_mode_true_migrates_to_red(self):
        cfg = SimpleNamespace(night_mode=True)
        self.assertEqual(theme.resolve_ui_color(cfg), "red")
        self.assertTrue(theme.night_enabled_for(cfg))

    def test_old_night_mode_false_stays_classic(self):
        cfg = SimpleNamespace(night_mode=False)
        self.assertEqual(theme.resolve_ui_color(cfg), "classic")

    def test_new_field_takes_precedence_over_old(self):
        cfg = SimpleNamespace(night_mode=True, ui_color="teal")
        self.assertEqual(theme.resolve_ui_color(cfg), "teal")
        self.assertTrue(theme.night_enabled_for(cfg))

    def test_new_field_arbitrary_hue(self):
        cfg = SimpleNamespace(ui_color=210)
        self.assertEqual(theme.resolve_ui_color(cfg), 210)


class SetUiColorLiveSwapTests(unittest.TestCase):
    def tearDown(self):
        theme.set_mode(False)

    def test_set_ui_color_swaps_active_palette(self):
        theme.set_ui_color("classic")
        self.assertEqual(theme.current_ui_color(), "classic")
        self.assertFalse(theme.is_night())
        theme.set_ui_color("green")
        self.assertEqual(theme.current_ui_color(), "green")
        self.assertFalse(theme.is_night(), "зелений — не еталонний червоний")
        self.assertEqual(theme.GOLD, theme.PRESETS["green"]["GOLD"])
        theme.set_ui_color("red")
        self.assertTrue(theme.is_night())

    def test_set_ui_color_arbitrary_hue(self):
        theme.set_ui_color(200)
        self.assertEqual(theme.current_ui_color(), 200)
        self.assertEqual(theme.GOLD, theme.build_palette_for_hue(200)["GOLD"])

    def test_set_mode_compat_wrapper(self):
        theme.set_mode(True)
        self.assertEqual(theme.current_ui_color(), "red")
        theme.set_mode(False)
        self.assertEqual(theme.current_ui_color(), "classic")


class IconRecolorTests(unittest.TestCase):
    def tearDown(self):
        theme.set_mode(False)

    def test_classic_uses_day_icon_file(self):
        path = theme._icon_path_for("check", None)
        self.assertTrue(path.endswith("check.svg"))
        self.assertFalse(path.endswith("-night.svg"))

    def test_red_uses_night_icon_file_unchanged(self):
        path = theme._icon_path_for("check", 0)
        self.assertTrue(path.endswith("check-night.svg"))

    def test_other_hue_generates_recolored_cached_file(self):
        path = theme._icon_path_for("check", 180)
        self.assertTrue(Path(path).exists(), "перефарбована іконка не створилась")
        svg = Path(path).read_text(encoding="utf-8")
        hexes = re.findall(r"#[0-9A-Fa-f]{6}", svg)
        self.assertTrue(hexes)
        src_svg = (theme._UI / "check-night.svg").read_text(encoding="utf-8")
        src_hex = re.findall(r"#[0-9A-Fa-f]{6}", src_svg)[0]
        expected = theme._rgb_to_hex(theme._shift_rgb(theme._hex_to_rgb(src_hex[1:]), 180))
        self.assertIn(expected, svg)
        # прибираємо за собою — це кеш, не бандл-ресурс (ui_icons/ у .gitignore)
        Path(path).unlink(missing_ok=True)

    def test_cache_not_regenerated_if_fresh(self):
        path1 = theme._icon_path_for("check", 155)
        mtime1 = Path(path1).stat().st_mtime
        path2 = theme._icon_path_for("check", 155)
        mtime2 = Path(path2).stat().st_mtime
        self.assertEqual(path1, path2)
        self.assertEqual(mtime1, mtime2, "кеш перегенерувався хоча джерело не змінилось")
        Path(path1).unlink(missing_ok=True)

    def test_write_error_falls_back_to_red_silently(self):
        """Помилка запису (тут: user_dir недоступний) НЕ валить застосунок —
        тихий відкат на еталонний червоний файл."""
        import whisper_core.paths as paths_mod

        def _boom():
            raise OSError("диск недоступний (симуляція)")

        original = paths_mod.user_dir
        paths_mod.user_dir = _boom
        try:
            path = theme._icon_path_for("check", 155)
        finally:
            paths_mod.user_dir = original
        self.assertTrue(path.endswith("check-night.svg"),
                         "помилка запису мала тихо відкотитись на червону іконку")


if __name__ == "__main__":
    unittest.main()
