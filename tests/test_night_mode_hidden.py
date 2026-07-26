"""feature/ui-color — рішення Миколи 25.07 СКАСОВУЄ Т50 (23.07): «нічний/
червоний режим ТИМЧАСОВО прихований» більше не діє. Власник: «зроби червоний
режим. І так само, щоб людина могла з палітри будь-який інший колір вибрати»
/ «Тільки не називається нічним режимом. А просто вибір кольору» — фіча
повертається у збірку як персоналізація вигляду (не мілітарі-функція).

Перевіряємо:
  1. NIGHT_MODE_AVAILABLE знову True (заслінку знято в ядрі theme.py) —
     тумблер кольору знову зʼявляється в Налаштуваннях, БЕЗ жодної зміни в
     самому settings.py (він і раніше читав саме цей прапорець).
  2. Старий конфіг night_mode=true піднімається як 'red' (theme.night_enabled_for
     / theme.resolve_ui_color), а не форситься на день і не падає помилкою.
  3. i18n-рядки тумблера лишаються на місці (паритет не ламаємо).

Повне покриття нового ядра кольору (зсув тону, контраст, пресети, іконки,
міграція конфігу) — у tests/test_ui_color.py. Інфраструктура старої бінарної
теми (set_mode/is_night/night-палітра) — у tests/test_night_mode.py.
"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox   # noqa: E402

from fronts.desktop import motion, theme                # noqa: E402
from fronts.desktop.i18n import tr, STRINGS             # noqa: E402
from fronts.desktop.pages.settings import SettingsPage  # noqa: E402
from tests.render_nav_smoke import _NavController, _make_sandbox  # noqa: E402


class UiColorUnhiddenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        motion.init_config(SimpleNamespace(animations=False))
        cls.sandbox = _make_sandbox()

    def setUp(self):
        theme.set_mode(False)                 # почати з денної; не лишати нічну іншим

    def tearDown(self):
        theme.set_mode(False)

    def test_feature_flag_on(self):
        self.assertTrue(theme.NIGHT_MODE_AVAILABLE,
                         "рішення 25.07 скасовує Т50 — фіча знову в збірці")

    def test_toggle_present_in_settings(self):
        page = SettingsPage(_NavController(self.sandbox))
        combo_names = [cb.accessibleName() for cb in page.findChildren(QComboBox)]
        self.assertIn(tr("set_ui_color_title"), combo_names,
                       "розділ вибору кольору інтерфейсу має бути у Налаштуваннях")

    def test_startup_migrates_old_night_mode_true(self):
        cfg = SimpleNamespace(night_mode=True)
        # дзеркалить рішення старту (app.main): night_enabled_for тепер читає
        # resolve_ui_color, старий night_mode=true піднімається як 'red'
        self.assertTrue(theme.night_enabled_for(cfg))
        if theme.night_enabled_for(cfg):
            theme.set_mode(True)
        self.assertTrue(theme.is_night(), "старе night=true має піднятись як червоний")

    def test_startup_stays_classic_by_default(self):
        cfg = SimpleNamespace()
        self.assertFalse(theme.night_enabled_for(cfg))

    def test_i18n_strings_preserved(self):
        for key in ("set_ui_color_title", "set_ui_color_hint"):
            self.assertIn(key, STRINGS["uk"], key)
            self.assertIn(key, STRINGS["en"], key)


if __name__ == "__main__":
    unittest.main()
