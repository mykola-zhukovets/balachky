"""Регресійні тести виправлень стиків UI-хвиль.

Фікс 3: visual_gate.scan_dialogs — зламана фабрика діалогу = ПОРУШЕННЯ
        (dialog_broken, валить --strict), а не тихий warning+exit 0.
Фікс 4: підказка вимкнених кнопок протоколу вказує на РОЗКРИВАЧКУ
        “Налаштування наради” на сторінці Нарада, а не на Налаштування → Нарада.
Фікс 5: visual_gate._match_key містить widget-ідентичність — один
        забазлайнений напис не «прощає» всі однакові написи на сторінці.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ─────────────────────── Фікс 4: маршрут підказки протоколу ───────────────────────
class ProtocolHintRouteTests(unittest.TestCase):
    def test_uk_points_to_meeting_settings_disclosure(self):
        from fronts.desktop.i18n import STRINGS
        hint = STRINGS["uk"]["meeting_protocol_need_model"]
        # старий помилковий маршрут «Налаштування → Нарада» прибрано
        self.assertNotIn("Налаштування → Нарада", hint)
        # новий маршрут — розкривачка на цій самій сторінці
        self.assertIn("Налаштування наради", hint)
        self.assertIn("сторінці", hint)

    def test_en_points_to_meeting_settings_disclosure(self):
        from fronts.desktop.i18n import STRINGS
        hint = STRINGS["en"]["meeting_protocol_need_model"]
        self.assertNotIn("Settings → Meeting", hint)
        self.assertIn("Meeting settings", hint)
        self.assertIn("this page", hint)


# ─────────────────────── Фікс 5: widget-ідентичність у ключі збігу ───────────────────────
class MatchKeyWidgetIdentityTests(unittest.TestCase):
    def test_same_text_different_widget_gives_different_key(self):
        import visual_gate
        a = {"lang": "uk", "page": "MeetingPage", "type": "text_clipped",
             "text": "Видалити", "widget": "MainWindow/MeetingPage/QPushButton#del_a"}
        b = {"lang": "uk", "page": "MeetingPage", "type": "text_clipped",
             "text": "Видалити", "widget": "MainWindow/MeetingPage/QPushButton#del_b"}
        self.assertNotEqual(visual_gate._match_key(a), visual_gate._match_key(b))

    def test_identical_widget_gives_equal_key(self):
        import visual_gate
        a = {"lang": "uk", "page": "P", "type": "text_clipped",
             "text": "Видалити", "widget": "W/QPushButton#x"}
        b = dict(a)
        self.assertEqual(visual_gate._match_key(a), visual_gate._match_key(b))


# ─────────────────────── Фікс 3: зламана фабрика діалогу = порушення ───────────────────────
class BrokenDialogFactoryTests(unittest.TestCase):
    def test_raising_factory_records_dialog_broken(self):
        import visual_gate
        qt = visual_gate._lazy_qt()
        results = []

        def _boom():
            raise RuntimeError("синтетична бита фабрика")

        # app не потрібен: гілка винятку повертається до будь-якого _process(app)
        visual_gate._scan_one_dialog("broken_test", _boom, "uk", results, None, qt)
        self.assertTrue(
            any(v["type"] == "dialog_broken" for v in results),
            f"зламана фабрика не дала dialog_broken: {results}")
        # порушення має widget — інакше _match_key (фікс 5) впав би на None
        broken = next(v for v in results if v["type"] == "dialog_broken")
        self.assertIn("widget", broken)


class ObsidianFormalCopyTests(unittest.TestCase):
    def test_obsidian_hint_already_uses_formal_vy(self):
        from fronts.desktop.i18n import STRINGS
        self.assertIn("якщо Ви нею не користуєтеся",
                      STRINGS["uk"]["set_obsidian_hint"])


if __name__ == "__main__":
    unittest.main()
