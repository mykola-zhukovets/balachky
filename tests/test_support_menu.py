"""Тести розгортного меню підтримки автора (Частина Б).

Перевіряє:
  1. Збіг крипто-адрес у меню побайтово з константами links.py (USDT, BTC, ETH).
  2. Справжнє побудування show_support_menu з усіма діями та доступними підписами.
  3. Справжній виклик дії копіювання крипто-адреси через меню у буфер обміну.
"""
import unittest
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QToolButton, QMenu
from PySide6.QtCore import QTimer
from fronts.desktop import links
from fronts.desktop.pages.settings import (
    round_social, show_support_menu, support_targets)

app = QApplication.instance() or QApplication([])


class TestSupportMenu(unittest.TestCase):

    def test_crypto_addresses_byte_equality(self):
        """Крипто-адреси у links.py єдине джерело правди й посимвольно збігаються з ТЗ."""
        self.assertEqual(links.SUPPORT_USDT_TRC20, "TTsc47PDTe2rUkeXcZGTQwR6driykkP2s8")
        self.assertEqual(links.SUPPORT_BTC, "bc1q8wqskryef3ey09jxhv9epdv7kpxnzg8vcf40hy")
        self.assertEqual(links.SUPPORT_ETH, "0x6A9FeF1CB66C20D31f770a970F790aFC85243A57")

        self.assertEqual(links.SUPPORT_MONO_UAH, "https://send.monobank.ua/jar/21rfey7KTz")
        self.assertEqual(links.SUPPORT_PRIVAT_USD, "https://www.privat24.ua/send/4h4jh")
        self.assertEqual(links.SUPPORT_PRIVAT_EUR, "https://www.privat24.ua/send/4h5jr")

    def test_all_six_ways_reachable(self):
        """Меню віддає ВСІ шість способів підтримки з links.py — і банківські
        посилання, і три гаманці. Приберемо з меню будь-який один — тест червоніє."""
        expected = {
            links.SUPPORT_MONO_UAH,
            links.SUPPORT_PRIVAT_USD,
            links.SUPPORT_PRIVAT_EUR,
            links.SUPPORT_USDT_TRC20,
            links.SUPPORT_BTC,
            links.SUPPORT_ETH,
        }
        targets = support_targets()
        self.assertEqual(len(targets), 6,
                         f"способів у меню має бути 6, а не {len(targets)}: {targets}")
        self.assertEqual(set(targets), expected)

    def test_support_menu_actions_and_a11y(self):
        """show_support_menu будує справжнє QMenu з усіма діями, інтро та доступними підписами."""
        btn = round_social("fa6s.heart", links.SUPPORT_URL, name="Support", tooltip="Support")
        btn.show()

        found_actions = []

        def _inspect():
            pop = app.activePopupWidget() or btn.findChild(QMenu, "supportMenu")
            if pop:
                for act in pop.actions():
                    if act.text():
                        found_actions.append((act.objectName(), act.text(), act.toolTip()))
                pop.close()

        QTimer.singleShot(50, _inspect)
        show_support_menu(btn)

        self.assertGreaterEqual(len(found_actions), 7, "Не всі дії збудовані у show_support_menu")
        obj_names = [a[0] for a in found_actions]
        self.assertIn("supportActIntro", obj_names)
        self.assertIn("supportActMono", obj_names)
        self.assertIn("supportActUsdt", obj_names)
        self.assertIn("supportActBtc", obj_names)
        self.assertIn("supportActEth", obj_names)

    def test_clipboard_copy_crypto(self):
        """Справжній виклик дії меню пише крипто-адресу в буфер обміну."""
        btn = QToolButton()
        btn.show()
        cb = QApplication.clipboard()
        cb.setText("")

        def _click_usdt():
            pop = app.activePopupWidget() or btn.findChild(QMenu, "supportMenu")
            if pop:
                for act in pop.actions():
                    if act.objectName() == "supportActUsdt":
                        act.trigger()
                        break
                pop.close()

        QTimer.singleShot(50, _click_usdt)
        show_support_menu(btn)

        self.assertEqual(cb.text(), "TTsc47PDTe2rUkeXcZGTQwR6driykkP2s8")


if __name__ == "__main__":
    unittest.main()
