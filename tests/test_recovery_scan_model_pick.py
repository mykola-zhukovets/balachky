"""Регресія відновлення: скан «стандартних тек» на frozen exe (WinError 448).

Дефект: RecoveryDialog._scan_computer брав ПЕРШУ теку зі стандартного списку,
де є повний знімок моделі, навіть якщо це symlink-копія HF-кешу — а поруч
могла лежати РЕАЛЬНА копія (наприклад, тека Балачок після онбордингу).
Frozen exe symlink-и не читає (WinError 448), тож вибір такого кандидата знову
кидав би у цей самий діалог. Тут: скан віддає перевагу кандидату з РЕАЛЬНИМИ
файлами — та сама логіка, що онбординг (_find_existing), див.
tests/test_onboarding_model_pick.py.
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
from fronts.desktop import recovery


MODEL_NAME = "large-v3-turbo"
REPO_ID = repo_for(MODEL_NAME)
REVISION = MODEL_REVISIONS[MODEL_NAME]
FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.json")


def _snapshot_dir(hub: Path) -> Path:
    return (hub / ("models--" + REPO_ID.replace("/", "--"))
            / "snapshots" / REVISION)


def _make_real_snapshot(hub: Path) -> Path:
    """Знімок зі звичайними файлами (як тека Балачок після онбордингу)."""
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


class FakeConfig:
    """Мінімальний конфіг для RecoveryDialog: без реального config.toml —
    жодного запису на диск (._cfg.save() лише позначає прапорець)."""

    def __init__(self, model_name):
        self.model_name = model_name
        self.model_dir = None
        self.saved = False

    def save(self, config_path=None):
        self.saved = True


class RecoveryScanModelPickTests(unittest.TestCase):
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

    def _dialog(self):
        cfg = FakeConfig(MODEL_NAME)
        dlg = recovery.RecoveryDialog(cfg, err=None)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_scan_prefers_real_file_candidate_over_symlinks(self):
        real_hub = str(self.tmp / "real" / "hub")
        _make_real_snapshot(Path(real_hub))
        dlg = self._dialog()
        # symlink-кандидат ПЕРШИЙ у списку — старий код узяв би саме його
        with patch.object(recovery, "_model_search_dirs",
                          return_value=[self.link_hub, real_hub]):
            dlg._scan_computer()
        self.assertEqual(dlg._cfg.model_dir, real_hub)
        self.assertTrue(dlg._cfg.saved)
        self.assertEqual(dlg.result(), recovery.QDialog.DialogCode.Accepted)

    def test_scan_falls_back_to_symlink_candidate_when_no_real_one(self):
        dlg = self._dialog()
        with patch.object(recovery, "_model_search_dirs",
                          return_value=[self.link_hub]):
            dlg._scan_computer()
        self.assertEqual(dlg._cfg.model_dir, self.link_hub)
        self.assertTrue(dlg._cfg.saved)

    def test_scan_none_found_leaves_config_untouched(self):
        dlg = self._dialog()
        with patch.object(recovery, "_model_search_dirs", return_value=[]):
            dlg._scan_computer()
        self.assertIsNone(dlg._cfg.model_dir)
        self.assertFalse(dlg._cfg.saved)


if __name__ == "__main__":
    unittest.main()
