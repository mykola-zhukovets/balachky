"""Звірка №5 (18.07): CLI не має падати на Windows-консолі з cp1251 —
main() перевлаштовує потоки на UTF-8 (як mcp_server/worker). Тест симулює
вузьку консоль cp1251-обгорткою і ганяє команду з типографікою у виводі."""
import io
import sys
import unittest


class CliUtf8ReconfigureTests(unittest.TestCase):
    def test_main_survives_cp1251_stdout(self):
        from fronts import cli
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(raw, encoding="cp1251", errors="strict")
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = narrow, narrow
        try:
            # dictionary list друкує канонічні “ ” і стрілки → поза cp1251;
            # без reconfigure це UnicodeEncodeError (відтворено звіркою №5)
            code = cli.main(["dictionary", "list"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertIn(code, (0, 1))     # будь-який чесний вихід, головне — без крашу

    def test_reconfigure_tolerates_streams_without_method(self):
        from fronts import cli
        old_out = sys.stdout
        sys.stdout = io.StringIO()      # StringIO не має reconfigure — не повинно падати
        try:
            code = cli.main([])
        finally:
            sys.stdout = old_out
        self.assertEqual(code, 1)       # порожній argv → help + код 1, без винятків


if __name__ == "__main__":
    unittest.main()
