"""Контракт CI-воркфлоу `.github/workflows/tests.yml` (issue #25).

CI має відтворювати автоматичні рівні `dev/qa_gate.ps1`. Тест звіряє кроки
воркфлоу з кроками гейта (кожен вартовий `dev/check_*.py`, який запускає гейт,
має бути і в CI), тримає безголове середовище, мінімальні права токена і
Windows-раннер, і стежить, щоб у CI не потрапили кроки, які потребують живого
екрана: візуальний гейт, живий запуск застосунку, заморожена збірка.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
GATE = ROOT / "dev" / "qa_gate.ps1"

# Кроки гейта, які CI зобов'язаний повторювати (підрядок у команді кроку).
REQUIRED_STEPS = [
    "-m pyflakes",
    "dev/check_lazy_imports.py",
    "dev/check_ai_trailers.py",
    "-m compileall",
    "-m unittest discover -s tests",
    "render_*_smoke.py",
    "dev/pytest_only_modules.txt",
    "dev/check_gate_coverage.py",
]
# Те, що свідомо лишається поза CI (живий екран або складальна машина).
FORBIDDEN_IN_STEPS = ["visual_gate.py", "run_app.py", "build_app.py", "pyinstaller"]


class CiWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.data = yaml.safe_load(cls.text)
        cls.job = cls.data["jobs"]["tests"]
        cls.runs = "\n".join(step.get("run", "") for step in cls.job["steps"])

    def test_yaml_parses_into_a_single_windows_job(self):
        self.assertIsInstance(self.data, dict)
        self.assertEqual(list(self.data["jobs"]), ["tests"])
        self.assertEqual(self.job["runs-on"], "windows-latest")
        self.assertIsInstance(self.job.get("timeout-minutes"), int)

    def test_headless_environment(self):
        env = {**self.data.get("env", {}), **self.job.get("env", {})}
        self.assertEqual(env.get("QT_QPA_PLATFORM"), "offscreen")
        self.assertEqual(str(env.get("PYTHONUTF8")), "1")

    def test_least_privilege_token(self):
        permissions = self.data.get("permissions") or self.job.get("permissions")
        self.assertEqual(permissions, {"contents": "read"})

    def test_python_312_and_only_first_party_actions(self):
        uses = [step["uses"] for step in self.job["steps"] if "uses" in step]
        self.assertTrue(uses)
        for action in uses:
            self.assertTrue(action.startswith("actions/"), action)
        setup = next(s for s in self.job["steps"]
                     if s.get("uses", "").startswith("actions/setup-python"))
        self.assertEqual(str(setup["with"]["python-version"]), "3.12")

    def test_every_required_gate_step_is_reproduced(self):
        for needle in REQUIRED_STEPS:
            with self.subTest(step=needle):
                self.assertIn(needle, self.runs)

    def test_screen_dependent_steps_stay_out(self):
        for needle in FORBIDDEN_IN_STEPS:
            with self.subTest(step=needle):
                self.assertNotIn(needle.lower(), self.runs.lower())

    def test_gate_watchdogs_are_all_covered(self):
        """Кожен вартовий `dev/check_*.py` з гейта має бути і в CI — щоб новий
        крок гейта не з'явився повз CI непоміченим."""
        watchdogs = set(re.findall(r"dev.(check_[a-z_]+\.py)",
                                   GATE.read_text(encoding="utf-8")))
        self.assertTrue(watchdogs, "у гейті не знайдено жодного вартового dev/check_*.py")
        for name in sorted(watchdogs):
            with self.subTest(watchdog=name):
                self.assertIn(name, self.runs)

    def test_no_secrets_referenced(self):
        self.assertNotIn("secrets.", self.text)


if __name__ == "__main__":
    unittest.main()
