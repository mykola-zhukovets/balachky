"""feature/office-voice-nav — тести надсилання навігаційних клавіш (wininput) і
доставки з безпекою (paste.send_nav). SendInput підмінюємо, щоб перевірити зібрані
події; вікно/гейти — моками. Стиль — як test_cascade_paste.py."""
import unittest
from unittest.mock import patch

from fronts.desktop import wininput
from fronts.desktop import paste


def _capture(store):
    def _fn(inputs):
        store.append(list(inputs))
        return len(inputs)
    return _fn


class SendNavKeyTests(unittest.TestCase):
    def test_tab_down_up(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            self.assertTrue(wininput.send_nav_key("tab"))
        # одна клавіша = 2 події (down, up)
        self.assertEqual(len(captured[0]), 2)
        self.assertEqual(captured[0][0].u.ki.wVk, wininput.VK_TAB)

    def test_shift_tab_is_chord(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            self.assertTrue(wininput.send_nav_key("shift_tab"))
        seq = captured[0]
        self.assertEqual(len(seq), 4)  # shift down, tab down, tab up, shift up
        self.assertEqual(seq[0].u.ki.wVk, wininput.VK_SHIFT)
        self.assertEqual(seq[1].u.ki.wVk, wininput.VK_TAB)

    def test_arrows_and_enter(self):
        for name, vk in (("down", wininput.VK_DOWN), ("up", wininput.VK_UP),
                         ("left", wininput.VK_LEFT), ("right", wininput.VK_RIGHT),
                         ("enter", wininput.VK_RETURN)):
            captured = []
            with patch.object(wininput, "_call_sendinput",
                              side_effect=_capture(captured)):
                self.assertTrue(wininput.send_nav_key(name))
            self.assertEqual(captured[0][0].u.ki.wVk, vk)

    def test_unknown_name_no_send(self):
        with patch.object(wininput, "_call_sendinput") as m:
            self.assertFalse(wininput.send_nav_key("nope"))
            m.assert_not_called()

    def test_ctrl_g_sequence(self):
        captured = []
        with patch.object(wininput, "_call_sendinput", side_effect=_capture(captured)):
            self.assertTrue(wininput.send_ctrl_g())
        seq = captured[0]
        self.assertEqual([e.u.ki.wVk for e in seq],
                         [wininput.VK_CONTROL, wininput.VK_G,
                          wininput.VK_G, wininput.VK_CONTROL])


class OwnProcessTests(unittest.TestCase):
    def test_own_process_matches_self(self):
        with patch.object(wininput, "own_exe_name", return_value="balachky.exe"):
            self.assertTrue(wininput.is_own_process("Balachky.exe"))
            self.assertFalse(wininput.is_own_process("winword.exe"))

    def test_empty_is_not_own(self):
        self.assertFalse(wininput.is_own_process(""))


class SendNavDeliveryTests(unittest.TestCase):
    """paste.send_nav: гейти безпеки й доставка key/goto."""

    def _fg(self, cls="OpusApp", exe="winword.exe"):
        return patch.object(wininput, "get_foreground_info", return_value=(cls, exe))

    def test_key_delivered(self):
        with self._fg(), \
             patch.object(wininput, "is_own_process", return_value=False), \
             patch.object(wininput, "send_nav_key", return_value=True) as k:
            self.assertEqual(paste.send_nav(("key", "tab")), paste.NAV_OK)
            k.assert_called_once_with("tab")

    def test_password_manager_blocked(self):
        with self._fg(exe="keepass.exe"), \
             patch.object(wininput, "send_nav_key") as k:
            self.assertEqual(paste.send_nav(("key", "tab")), paste.PASTE_BLOCKED)
            k.assert_not_called()

    def test_own_window_blocked(self):
        with self._fg(exe="balachky.exe"), \
             patch.object(wininput, "is_own_process", return_value=True), \
             patch.object(wininput, "send_nav_key") as k:
            self.assertEqual(paste.send_nav(("key", "tab")), paste.PASTE_BLOCKED)
            k.assert_not_called()

    def test_pinned_target_mismatch_blocked(self):
        # ціль закріплена (target_hwnd=111), але фокус уже на іншому вікні (222)
        with self._fg(), \
             patch.object(wininput, "is_own_process", return_value=False), \
             patch.object(wininput, "get_foreground_window", return_value=222), \
             patch.object(wininput, "send_nav_key") as k:
            self.assertEqual(paste.send_nav(("key", "tab"), target_hwnd=111),
                             paste.PASTE_BLOCKED)
            k.assert_not_called()

    def test_pinned_target_match_delivers(self):
        with self._fg(), \
             patch.object(wininput, "is_own_process", return_value=False), \
             patch.object(wininput, "get_foreground_window", return_value=111), \
             patch.object(wininput, "send_nav_key", return_value=True):
            self.assertEqual(paste.send_nav(("key", "tab"), target_hwnd=111),
                             paste.NAV_OK)

    def test_goto_sequence(self):
        calls = []
        with self._fg(), \
             patch.object(wininput, "is_own_process", return_value=False), \
             patch.object(wininput, "send_ctrl_g", return_value=True) as g, \
             patch.object(wininput, "type_unicode",
                          side_effect=lambda a: calls.append(("type", a)) or True), \
             patch.object(wininput, "send_nav_key",
                          side_effect=lambda n: calls.append(("key", n)) or True), \
             patch("fronts.desktop.paste.time.sleep"):
            self.assertEqual(paste.send_nav(("goto", "B7")), paste.NAV_OK)
            g.assert_called_once()
            self.assertEqual(calls, [("type", "B7"), ("key", "enter")])

    def test_goto_ctrl_g_fails(self):
        with self._fg(), \
             patch.object(wininput, "is_own_process", return_value=False), \
             patch.object(wininput, "send_ctrl_g", return_value=False), \
             patch.object(wininput, "type_unicode") as tu, \
             patch("fronts.desktop.paste.time.sleep"):
            self.assertIsNone(paste.send_nav(("goto", "B7")))
            tu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
