"""Offscreen-рендер потоку «Розпізнано погано…» (feature/accuracy-corpus +
feature/selflearn-dict).

Поза `unittest discover` (патерн test*.py) — тримаємо окремим процесом, як
render_reverse_dictation_smoke. Перевіряє ІЗОЛЯЦІЮ САМОНАВЧАННЯ по словниках на
рівні UI для потоку звіту точності (report_bad):
  - звичайний потік: виправлення в діалозі вчить безпечне правило в активний
    словник і зберігає зразок корпусу;
  - перемикання активного словника з трею, ПОКИ модалка відкрита, НЕ переадресує
    навчання — воно йде в ЗАХОПЛЕНИЙ на відкритті профіль (дзеркало сценарію
    зворотного диктування test_switch_profile_mid_dialog_learns_captured_profile).

    python -m unittest tests.render_corpus_report_smoke
    python tests/render_corpus_report_smoke.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from whisper_core import self_learning                 # noqa: E402
from whisper_core.profiles import Profile              # noqa: E402


class FakeController:
    """Мінімальна поверхня контролера, яку кличе report_bad. learn_from_report
    ДЗЕРКАЛИТЬ DesktopApp.learn_from_report: навчання йде в ЗАХОПЛЕНИЙ профіль
    (параметр), а не в поточно-активний self.profile."""

    def __init__(self, d):
        self.profile = Profile("default", Path(d))
        self.corpus_saved = []
        self.reloaded = 0
        self.model_name = ""

    def save_corpus_sample(self, recognized, corrected, *, ts=None,
                           src_wav=None, source="desktop", profile=None):
        # feature/selflearn-dict: зразок корпусу теж прив'язується до ЗАХОПЛЕНОГО
        # словника — фіксуємо ім'я, щоб перевірити ізоляцію й для цього шляху.
        self.corpus_saved.append((recognized, corrected,
                                  getattr(profile or self.profile, "name", "") or ""))
        return {"recognized": recognized, "corrected": corrected}

    def reload_terms(self):
        self.reloaded += 1

    def learn_from_report(self, recognized, corrected, *, source="desktop",
                          profile=None):
        profile = profile or self.profile
        result = self_learning.learn_from_correction(
            profile, recognized or "", corrected or "", source=source or "history")
        if result.status == "learned" and profile.name == self.profile.name:
            self.reload_terms()
        return result

    def revoke_learned(self, profile, entry_id):
        return self_learning.revoke(profile, entry_id)


class CorpusReportRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from fronts.desktop.i18n import set_language
        set_language("uk")

    def setUp(self):
        from PySide6.QtWidgets import QWidget
        self._tmp = tempfile.TemporaryDirectory()
        self._ctl = FakeController(self._tmp.name)
        self._page = QWidget()
        self._page.resize(480, 360)
        self._widgets = [self._page]

    def tearDown(self):
        for w in self._widgets:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self.app.processEvents()
        self._tmp.cleanup()

    def _patch_exec(self, corrected, *, switch_to=None):
        """Підмінити CorpusReportDialog.exec: (опц.) перемкнути активний словник
        із трею ПОКИ модалка відкрита, виставити виправлення, повернути Accepted."""
        from PySide6.QtWidgets import QDialog
        from fronts.desktop import corpus_dialog
        ctl = self._ctl

        def fake_exec(dlg_self):
            if switch_to is not None:
                ctl.profile = switch_to        # імітуємо перемикання словника з трею
            dlg_self.corrected = corrected
            return QDialog.Accepted

        orig = corpus_dialog.CorpusReportDialog.exec
        corpus_dialog.CorpusReportDialog.exec = fake_exec
        self.addCleanup(setattr, corpus_dialog.CorpusReportDialog, "exec", orig)

    # ---- звичайний потік: вчить активний словник + зберігає зразок ----
    def test_report_bad_learns_active_profile(self):
        from fronts.desktop.corpus_dialog import report_bad
        self._patch_exec("сирий worktree")
        ok = report_bad(self._page, self._ctl, "сирий ворктрі", source="desktop")
        self.assertTrue(ok)
        self.assertEqual(len(self._ctl.corpus_saved), 1)          # зразок у корпусі
        self.assertTrue(self_learning.list_learned(self._ctl.profile))  # правило вивчено

    # ---- ІЗОЛЯЦІЯ: перемикання словника поки діалог відкритий не тече ----
    def test_switch_profile_mid_dialog_learns_captured_profile(self):
        from fronts.desktop.corpus_dialog import report_bad
        captured = self._ctl.profile
        other = Profile("work", Path(self._tmp.name) / "work_profile")
        other.dir.mkdir(parents=True, exist_ok=True)
        self._patch_exec("сирий worktree", switch_to=other)
        report_bad(self._page, self._ctl, "сирий ворктрі", source="desktop")
        # навчання пішло в ЗАХОПЛЕНИЙ профіль, а не в новоактивний
        self.assertTrue(self_learning.list_learned(captured))
        self.assertEqual(self_learning.list_learned(other), [])
        # і зразок корпусу позначено ЗАХОПЛЕНИМ словником, не новоактивним
        self.assertEqual(self._ctl.corpus_saved[0][2], captured.name)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(CorpusReportRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
