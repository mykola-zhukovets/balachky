"""Регресії онбордингу: вибір знайденої моделі на frozen exe (WinError 448).

Дефект: майстер першого запуску пропонував symlink-знімки HF-кешу і НЕ лікував
їх після вибору → перший старт після онбордингу падав у діалог відновлення
(зайвий цикл + тихе дублювання блобів само-лікуванням app.py). Тут:
  • скан _find_existing віддає перевагу кандидату з РЕАЛЬНИМИ файлами;
  • підтвердження symlink-кандидата (_go_next) дереференсить знімок —
    те саме лікування, що RecoveryDialog._heal_if_symlinks.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Віджети без екрана: тестам потрібен QApplication, не рендеринг
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from whisper_core.engine import MODEL_REVISIONS
from whisper_core.models import repo_for
from fronts.desktop import onboarding


MODEL_NAME = "large-v3-turbo"        # дефолтний вибір радіо у майстрі
REPO_ID = repo_for(MODEL_NAME)
REVISION = MODEL_REVISIONS[MODEL_NAME]
FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.json")


def _snapshot_dir(hub: Path) -> Path:
    return (hub / ("models--" + REPO_ID.replace("/", "--"))
            / "snapshots" / REVISION)


def _make_real_snapshot(hub: Path) -> Path:
    """Знімок зі звичайними файлами (як після докачки майстром)."""
    snap = _snapshot_dir(hub)
    snap.mkdir(parents=True)
    for name in FILES:
        (snap / name).write_bytes(b"x")
    return snap


def _make_symlink_snapshot(hub: Path) -> Path:
    """Знімок у форматі HF-кешу: snapshots/<rev>/файл → ../../blobs/<hash>."""
    snap = _snapshot_dir(hub)
    blobs = snap.parent.parent / "blobs"
    snap.mkdir(parents=True)
    blobs.mkdir(parents=True)
    for i, name in enumerate(FILES):
        blob = blobs / f"blob{i}"
        blob.write_bytes(b"x")
        os.symlink(os.path.relpath(blob, snap), snap / name)
    return snap


class OnboardingModelPickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        try:
            _make_symlink_snapshot(self.tmp / "linked" / "hub")
        except OSError:
            self.skipTest("symlink-и недоступні (потрібен Developer Mode)")
        self.link_hub = str(self.tmp / "linked" / "hub")

    def _wizard(self):
        wizard = onboarding.FirstRunWizard()
        self.addCleanup(wizard.deleteLater)
        return wizard

    def test_scan_prefers_real_file_candidate_over_symlinks(self):
        real_hub = str(self.tmp / "real" / "hub")
        _make_real_snapshot(Path(real_hub))
        wizard = self._wizard()
        # symlink-кандидат ПЕРШИЙ у списку — старий код узяв би саме його
        with patch.object(onboarding, "_model_search_dirs",
                          return_value=[self.link_hub, real_hub]):
            wizard._find_existing()
        self.assertEqual(wizard.model_dir, real_hub)

    def test_confirming_symlink_candidate_dereferences_snapshot(self):
        wizard = self._wizard()
        with patch.object(onboarding, "_model_search_dirs",
                          return_value=[self.link_hub]):
            wizard._find_existing()
        self.assertEqual(wizard.model_dir, self.link_hub)

        wizard._stack.setCurrentIndex(3)      # крок «Озвучення»: пропуск/далі підтверджує вибір
        # feature/gpu: ізолюємо від реального GPU — тест про лікування моделі, не
        # про докачку прискорення (інакше на «NVIDIA без рантайму» показався б
        # GPU-крок замість accept).
        with patch.object(onboarding, "dereference_snapshot",
                          wraps=onboarding.dereference_snapshot) as deref, \
                patch("whisper_core.cuda_runtime.gpu_present", return_value=False):
            wizard._go_next()
        deref.assert_called_once_with(self.link_hub, REPO_ID, REVISION)
        # знімок вилікувано: всі файли реальні, докачка не стартувала
        snap = _snapshot_dir(Path(self.link_hub))
        for name in FILES:
            self.assertFalse(os.path.islink(snap / name), name)
        self.assertEqual(wizard.result(), onboarding.QDialog.DialogCode.Accepted)


if __name__ == "__main__":
    unittest.main()
