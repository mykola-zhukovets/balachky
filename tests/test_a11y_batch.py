"""Пачка a11y-фіксів 30.07 (worktree a11y-batch, гілка fix/a11y-quick-batch).

Чотири незалежні знахідки аудиту, кожна ЧЕРВОНА на master 4db101d:
1. Кнопки/таблиця з Qt.NoFocus випадали з клавіатурної навігації
   (settings.py: NetworkLogDialog._build_table, KeyCaptureDialog,
   ContextProfileDialog, AutoProfileRuleDialog).
2. Клікабельні цілі 22×22 (main_window.py del_btn, settings.py info_hint)
   нижче рекомендованого мінімуму 24×24.
3. DANGER_MUTED (#CF7B62) на CARD (#3A3528) — 3.87:1, нижче AA 4.5:1.
4. Повзунок плеєра (_Slider) без фокуса/доступного імені/стрілок.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ───────────────────────── 1. фокус-навігація ─────────────────────────

class NetworkLogTableFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_table_accepts_focus(self):
        from PySide6.QtCore import Qt
        from fronts.desktop.pages.settings import NetworkLogDialog
        dlg = NetworkLogDialog(entries=[
            {"ts": 1721300000, "host": "example.com", "kind": "model"},
        ])
        table = dlg.findChildren(__import__(
            "PySide6.QtWidgets", fromlist=["QTableWidget"]).QTableWidget)
        self.assertTrue(table, "таблиця журналу не побудувалась")
        self.assertNotEqual(table[0].focusPolicy(), Qt.NoFocus,
                             "таблиця журналу недосяжна з клавіатури")
        dlg.deleteLater()


class KeyCaptureDialogFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def _dialog(self):
        from fronts.desktop.pages.settings import KeyCaptureDialog
        dlg = KeyCaptureDialog()
        self.addCleanup(dlg._finish_capture)   # звільнити grabKeyboard
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_cancel_and_save_accept_focus(self):
        from PySide6.QtCore import Qt
        dlg = self._dialog()
        # приватні на назву, публічні на призначення: пошук за текстом кнопки
        from PySide6.QtWidgets import QPushButton
        buttons = {b.text(): b for b in dlg.findChildren(QPushButton)}
        self.assertEqual(len(buttons), 2, buttons)
        for text, btn in buttons.items():
            self.assertNotEqual(btn.focusPolicy(), Qt.NoFocus,
                                 f"кнопка «{text}» недосяжна з клавіатури")

    def test_bare_enter_confirms_already_captured_combo_without_mouse(self):
        """Захоплення клавіш лишається недоторканим (grabKeyboard і далі
        перехоплює все), але гола Enter (без модифікатора) — яка і без цього
        невалідна як хоткей (need_mod) — тепер підтверджує вже captured
        комбінацію. Клавіатурний шлях до Save без миші."""
        dlg = self._dialog()
        dlg._on_event("down", "ctrl")
        dlg._on_event("down", "k")
        dlg._on_event("up", "k")
        dlg._on_event("up", "ctrl")
        self.assertEqual(dlg._pending, "ctrl+k")
        self.assertIsNone(dlg.result_key)
        dlg._on_event("down", "enter")
        self.assertEqual(dlg.result_key, "ctrl+k")

    def test_enter_held_as_modifier_partner_still_capturable(self):
        """Ctrl+Enter лишається бажаною комбінацією — хапаємо лише ГОЛУ
        Enter (без затиснутих модифікаторів), не Enter узагалі."""
        dlg = self._dialog()
        dlg._on_event("down", "ctrl")
        dlg._on_event("down", "enter")
        self.assertEqual(dlg._pending, "ctrl+enter")
        self.assertIsNone(dlg.result_key, "Enter-з-модифікатором не мала автосейвитись")


class TakeButtonFocusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_context_profile_take_button_accepts_focus(self):
        from PySide6.QtCore import Qt
        from fronts.desktop.pages.settings import ContextProfileDialog
        dlg = ContextProfileDialog(resolver=None, dict_names=[])
        self.assertNotEqual(dlg._take.focusPolicy(), Qt.NoFocus)
        dlg.deleteLater()

    def test_auto_profile_take_button_accepts_focus(self):
        from PySide6.QtCore import Qt
        from fronts.desktop.pages.settings import AutoProfileRuleDialog
        dlg = AutoProfileRuleDialog(resolver=None, profile_names=["default"])
        self.assertNotEqual(dlg._take.focusPolicy(), Qt.NoFocus)
        dlg.deleteLater()


# ───────────────────────── 2. розмір клікабельних цілей ─────────────────────────

class ClickTargetSizeTests(unittest.TestCase):
    _MIN = 24

    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_info_hint_button_at_least_24px(self):
        from fronts.desktop.pages.settings import info_hint
        btn = info_hint("hint_mica")
        self.assertGreaterEqual(btn.width(), self._MIN)
        self.assertGreaterEqual(btn.height(), self._MIN)
        btn.deleteLater()

    def test_dictation_card_delete_button_at_least_24px(self):
        from types import SimpleNamespace
        from tests.render_nav_smoke import _NavController, _make_sandbox
        from fronts.desktop.main_window import MainWindow
        from fronts.desktop import motion
        from fronts.desktop.glass import TipToolButton
        from fronts.desktop.i18n import tr
        from whisper_core import profiles

        motion.init_config(SimpleNamespace(animations=False))
        sandbox = _make_sandbox()
        orig_list = profiles.list_profiles
        profiles.list_profiles = lambda root=None: orig_list(sandbox)
        win = None
        try:
            win = MainWindow(_NavController(sandbox))
            win.dictation.add_entry("тест", "тест")
            del_btns = [b for b in win.findChildren(TipToolButton)
                        if b.accessibleName() == tr("dict_card_delete")]
            self.assertTrue(del_btns, "кнопка видалення картки не знайдена")
            btn = del_btns[0]
            self.assertGreaterEqual(btn.width(), self._MIN)
            self.assertGreaterEqual(btn.height(), self._MIN)
        finally:
            profiles.list_profiles = orig_list
            if win is not None:
                from PySide6.QtCore import QTimer
                for t in win.findChildren(QTimer):
                    try:
                        t.stop()
                    except RuntimeError:
                        pass
                try:
                    win.close()
                except Exception:
                    pass
                win.deleteLater()
            for _ in range(3):
                self.app.processEvents()


# ───────────────────────── 3. контраст деструктив-кнопки ─────────────────────────

class ThemeContrastTests(unittest.TestCase):
    """Загальна перевірка WCAG AA (4.5:1) для пар «текст на тлі» теми.

    Пари — куратований, а НЕ автоматично зіскреблений із QSS список токенів,
    які РЕАЛЬНО виступають одне як color:, інше як фонова поверхня в
    build_qss (перевірено вручну 30.07). Додаси новий текстовий токен —
    допиши пару сюди, і вона теж почне перевірятись.
    """

    _MIN = 4.5
    _PAIRS = [
        ("TEXT_BODY", "CARD"), ("TEXT_BODY", "SURFACE"),
        ("TEXT_STRONG", "CARD"), ("TEXT_STRONG", "SURFACE"),
        ("TEXT_MUTED", "CARD"), ("TEXT_MUTED", "SURFACE"),
        ("TEXT_ON_GOLD", "GOLD"),
        ("GOLD_EYEBROW", "CARD"), ("GOLD_EYEBROW", "SURFACE"),
        ("ALERT_TEXT", "CARD"),
        ("IDLE", "CARD"),
        ("SUCCESS", "CARD"),
        ("SUCCESS_EYEBROW", "CARD"),
        ("DANGER_MUTED", "CARD"),   # 30.07: був 3.87:1 у DAY — виправлено
    ]

    @staticmethod
    def _contrast(fg_value, bg_value) -> float:
        """Контраст (WCAG relative luminance) для двох значень токена теми
        (hex / rgba-рядок) — рахує наживо, не хардкодить число."""
        from fronts.desktop import theme
        fg_rgb = theme._token_rgb(fg_value)
        bg_rgb = theme._token_rgb(bg_value)
        return theme._contrast_ratio(fg_rgb, bg_rgb)

    def _check_palette(self, palette: dict, label: str):
        failures = []
        for fg, bg in self._PAIRS:
            c = self._contrast(palette[fg], palette[bg])
            if c < self._MIN:
                failures.append(f"{fg} on {bg}: {c:.2f}:1 (<{self._MIN})")
        self.assertEqual(failures, [], f"{label}: {failures}")

    def test_day_palette_pairs_meet_aa(self):
        from fronts.desktop import theme
        self._check_palette(theme._DAY, "DAY")

    def test_night_palette_pairs_meet_aa(self):
        from fronts.desktop import theme
        self._check_palette(theme._NIGHT, "NIGHT")

    def test_danger_muted_on_card_day_at_least_aa(self):
        """Мутаційний якір: повернення DANGER_MUTED на #CF7B62 має
        зчервонити саме цей тест (3.87:1 < 4.5:1)."""
        from fronts.desktop import theme
        c = self._contrast(theme._DAY["DANGER_MUTED"], theme._DAY["CARD"])
        self.assertGreaterEqual(c, self._MIN)


# ───────────────────────── 4. повзунок плеєра ─────────────────────────

class SliderAccessibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def test_slider_accepts_focus(self):
        from PySide6.QtCore import Qt
        from fronts.desktop.player import _Slider
        s = _Slider()
        self.assertNotEqual(s.focusPolicy(), Qt.NoFocus)
        s.deleteLater()

    def test_seek_and_volume_have_accessible_names(self):
        from fronts.desktop.player import InlinePlayer
        p = InlinePlayer()
        if p._player is None:
            self.skipTest("QtMultimedia недоступний — фолбек без повзунків")
        self.assertTrue(p._seek.accessibleName())
        self.assertTrue(p._vol.accessibleName())
        p.deleteLater()

    def test_arrow_keys_seek(self):
        from PySide6.QtCore import Qt, QEvent
        from PySide6.QtGui import QKeyEvent
        from fronts.desktop.player import _Slider
        s = _Slider()
        s.set_fraction(0.5)
        moved = []
        s.moved.connect(moved.append)

        right = QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier)
        s.keyPressEvent(right)
        self.assertGreater(s.fraction(), 0.5)
        self.assertTrue(moved and moved[-1] == s.fraction())

        left = QKeyEvent(QEvent.KeyPress, Qt.Key_Left, Qt.NoModifier)
        s.keyPressEvent(left)
        s.keyPressEvent(left)
        self.assertLess(s.fraction(), 0.5)
        s.deleteLater()


if __name__ == "__main__":
    unittest.main()
