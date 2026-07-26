"""feature/mono-themes — параметричний прохід візуального гейта по кольору
інтерфейсу (задача 25.07: повернути й параметризувати прохід гейта поза
класикою, замінивши BALACHKY_FORCE_NIGHT так, щоб старий спосіб теж працював).

Перевіряємо ДВІ речі, які логіка теми/інтерфейсу (theme.py) сама по собі не
покриває — вони належать саме гейту:
  1. `theme._startup_color_from_env` — яке середовище дає який колір НА
     ІМПОРТІ модуля (BALACHKY_UI_COLOR має пріоритет; BALACHKY_FORCE_NIGHT —
     стара сумісність; без обох — 'classic'). Через subprocess, бо це рішення
     ухвалюється РІВНО раз, на рівні модуля, і повторний імпорт у тому самому
     процесі його не відтворить.
  2. `scripts/visual_gate.py` — підпис активного кольору й шлях звіту
     (`_active_color_label`/`_report_path_for`): класика лишається на
     старому шляху (жоден наявний скрипт/звичка не зламані), інші кольори —
     окремий файл на варіант, інакше прогони по черзі затирали б звіт
     одне одного і «цифри по кожному кольору окремо» стали б неможливі.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _color_in_subprocess(env_overrides: dict) -> str:
    """Свіжий процес python: імпортувати theme із заданим середовищем і
    надрукувати theme.current_ui_color(). Ізольовано від кешу модулів цього
    тестового процесу — інакше другий виклик theme.py в тому самому процесі
    просто повернув би вже імпортований модуль, а не перечитав середовище."""
    env = dict(os.environ)
    env.pop("BALACHKY_UI_COLOR", None)
    env.pop("BALACHKY_FORCE_NIGHT", None)
    env.update(env_overrides)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    code = (
        "import sys; sys.path.insert(0, r'" + str(_ROOT) + "');"
        "from fronts.desktop import theme;"
        "print(repr(theme.current_ui_color()))"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"stderr:\n{out.stderr}"
    return out.stdout.strip().splitlines()[-1]


class StartupColorFromEnvTests(unittest.TestCase):
    """theme._startup_color_from_env: джерело правди для гейта і для будь-якого
    ручного BALACHKY_UI_COLOR=... запуску."""

    def test_no_env_is_classic(self):
        self.assertEqual(_color_in_subprocess({}), "'classic'")

    def test_ui_color_named_preset(self):
        self.assertEqual(_color_in_subprocess({"BALACHKY_UI_COLOR": "teal"}), "'teal'")

    def test_ui_color_arbitrary_hue_becomes_int(self):
        self.assertEqual(_color_in_subprocess({"BALACHKY_UI_COLOR": "210"}), "210")

    def test_legacy_force_night_still_works(self):
        self.assertEqual(_color_in_subprocess({"BALACHKY_FORCE_NIGHT": "1"}), "'red'")

    def test_ui_color_takes_priority_over_legacy_force_night(self):
        """Обидві задані — новий спосіб виграє (старий не тихо переможе)."""
        color = _color_in_subprocess(
            {"BALACHKY_UI_COLOR": "amber", "BALACHKY_FORCE_NIGHT": "1"})
        self.assertEqual(color, "'amber'")

    def test_empty_ui_color_falls_back_to_legacy(self):
        """BALACHKY_UI_COLOR="" (задана, але порожня) — не валить, а поводиться
        як незадана: старий BALACHKY_FORCE_NIGHT далі рішає."""
        color = _color_in_subprocess(
            {"BALACHKY_UI_COLOR": "", "BALACHKY_FORCE_NIGHT": "1"})
        self.assertEqual(color, "'red'")


class VisualGateColorLabelTests(unittest.TestCase):
    """_active_color_label / _report_path_for з scripts/visual_gate.py —
    підпис прогону й окремий звіт на колір (не JSON-детектор обрізань, той
    покритий --selfcheck самого скрипта)."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.path.insert(0, str(_ROOT / "scripts"))
        import visual_gate
        cls.visual_gate = visual_gate

    def test_report_path_classic_is_legacy_path(self):
        path = self.visual_gate._report_path_for("classic")
        self.assertEqual(path, self.visual_gate.REPORT_PATH)
        self.assertEqual(path.name, "visual_gate_report.json")

    def test_report_path_other_color_is_suffixed(self):
        path = self.visual_gate._report_path_for("teal")
        self.assertEqual(path.name, "visual_gate_report_teal.json")
        self.assertNotEqual(path, self.visual_gate.REPORT_PATH)

    def test_report_path_hue_label_is_suffixed(self):
        path = self.visual_gate._report_path_for("hue210")
        self.assertEqual(path.name, "visual_gate_report_hue210.json")


if __name__ == "__main__":
    unittest.main()
