"""Offscreen-рендер зворотного диктування (feature/reverse-dictation).

Поза `unittest discover` (патерн test*.py) — живі QWidget з show()/exec тримаємо
окремим процесом (як render_editing_smoke). Перевіряє:
  - картка Історії має кнопки «Переслухати» (неактивна без збереженого аудіо) і
    «Виправити»; обидві з accessibleName;
  - діалог зі збереженим аудіо показує плеєр; збереження виправлення переписує
    final (raw ЦІЛИЙ) і ставить позначку edited;
  - діалог без аудіо показує підказку замість плеєра;
  - «Виправити голосом виділене» заміняє виділення результатом Command Mode.

    python -m unittest tests.render_reverse_dictation_smoke
    python tests/render_reverse_dictation_smoke.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from whisper_core import history, recordings, self_learning   # noqa: E402
from whisper_core.profiles import Profile                       # noqa: E402


class FakeController:
    """Мінімальна поверхня контролера, яку кличуть картка Історії й діалог."""

    def __init__(self, d):
        # справжній Profile: має всі шляхи (history/terms/phrases/self-learning)
        self.profile = Profile("default", Path(d))
        self.saved = []
        self.voiced = []

    def _dictation_audio_dir(self):
        return self.profile.dir / "dictation_audio"

    def dictation_audio_path(self, rec):
        name = rec.get("audio") if isinstance(rec, dict) else None
        if not name or not recordings.is_safe_recording_name(name):
            return None
        p = self._dictation_audio_dir() / name
        return p if p.is_file() else None

    def delete_dictation_audio(self, rec):
        name = rec.get("audio") if isinstance(rec, dict) else None
        if name:
            recordings.delete_recording(self._dictation_audio_dir(), name)

    def reload_terms(self):
        pass

    def apply_correction(self, rec, new_final, *, profile=None, save_corpus=False,
                         recognized=None):
        # дзеркалить DesktopApp.apply_correction: оновити запис (за id, інакше ts)
        # + вивести безпечне правило самонавчання
        profile = profile or self.profile
        self.saved.append((rec.get("ts"), new_final))
        rid, ts = rec.get("id"), rec.get("ts")
        if rid:
            updated = history.update_final_by_id(
                profile.history_path, rid, new_final,
                fallback=(ts, rec.get("raw"), rec.get("final"), rec.get("source")))
        else:
            updated = history.update_record(profile.history_path, ts,
                                            final=new_final, mark_edited=True)
        before = (rec.get("final") or rec.get("raw") or "")
        result = self_learning.learn_from_correction(
            profile, before, new_final, history_id=rid or "", source="history")
        result.history_updated = updated
        return result

    def revoke_learned(self, profile, entry_id):
        return self_learning.revoke(profile, entry_id)

    def voice_edit_selection(self, selected, apply_fn, parent=None):
        self.voiced.append(selected)
        apply_fn("ГОЛОСОВЕ ВИПРАВЛЕННЯ")     # імітуємо застосований результат


def _save_audio(controller):
    audio = np.zeros(16000, dtype=np.float32)
    audio[100:400] = 0.1
    out = recordings.save_recording(controller._dictation_audio_dir(), audio, 16000)
    return out.name


class ReverseDictationRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from fronts.desktop.i18n import set_language
        set_language("uk")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ctl = FakeController(self._tmp.name)
        self._audio_name = _save_audio(self._ctl)
        # log_history сам виставляє ts — беремо повернуті записи (як у застосунку
        # картка отримує їх із read_recent), щоб ts збігався для update_record
        self._rec_plain = history.log_history(
            self._ctl.profile.history_path, "старий запис без аудіо",
            "старий запис без аудіо", source="file")
        self._rec_audio = history.log_history(
            self._ctl.profile.history_path, "сирий ворктрі", "сирий ворктрі",
            source="desktop", audio=self._audio_name)
        self._widgets = []

    def tearDown(self):
        for w in self._widgets:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self.app.processEvents()
        self._tmp.cleanup()

    # ---- картка Історії ----
    def test_card_replay_button_state(self):
        from fronts.desktop.i18n import tr
        from fronts.desktop.glass import GlassButton
        from fronts.desktop.pages.history import HistoryPage
        page = HistoryPage(self._ctl)
        self._widgets.append(page)
        page.refresh()
        self.app.processEvents()
        buttons = page.findChildren(GlassButton)
        replays = [b for b in buttons if b.text() == tr("revdict_replay")]
        corrects = [b for b in buttons if b.text() == tr("revdict_correct")]
        # два записи → по кнопці на картку
        self.assertEqual(len(replays), 2)
        self.assertEqual(len(corrects), 2)
        # рівно одна «Переслухати» активна (запис зі збереженим аудіо)
        enabled = [b for b in replays if b.isEnabled()]
        self.assertEqual(len(enabled), 1)
        # accessibleName на нових кнопках
        for b in replays + corrects:
            self.assertTrue(b.accessibleName())

    # ---- діалог: аудіо є, збереження виправлення ----
    def test_dialog_with_audio_saves_correction(self):
        from fronts.desktop.pages.reverse_dictation import ReverseDictationDialog
        wav = self._ctl.dictation_audio_path(self._rec_audio)
        self.assertIsNotNone(wav)
        dlg = ReverseDictationDialog(self._ctl, self._rec_audio, audio_path=wav,
                                     autoplay=False)
        self._widgets.append(dlg)
        self.assertIsNotNone(dlg._player)             # плеєр «Переслухати» є
        dlg._editor.setPlainText("сирий worktree")    # правка клавіатурою
        dlg._on_save()
        self.assertTrue(dlg.saved)
        self.assertEqual(len(self._ctl.saved), 1)
        # у файлі: final переписано, raw дослівний ЦІЛИЙ, позначка edited
        records = history.read_recent(self._ctl.profile.history_path)
        rec = next(r for _line, r in records
                   if r.get("ts") == self._rec_audio["ts"])
        self.assertEqual(rec["final"], "сирий worktree")
        self.assertEqual(rec["raw"], "сирий ворктрі")
        self.assertTrue(rec["edited"])

    # ---- діалог: аудіо немає → підказка, плеєра нема ----
    def test_dialog_without_audio_has_no_player(self):
        from fronts.desktop.pages.reverse_dictation import ReverseDictationDialog
        dlg = ReverseDictationDialog(self._ctl, self._rec_plain, audio_path=None,
                                     autoplay=False)
        self._widgets.append(dlg)
        self.assertIsNone(dlg._player)
        # текст усе одно правиться
        dlg._editor.setPlainText("старий запис виправлено")
        dlg._on_save()
        self.assertTrue(dlg.saved)

    # ---- одна дія Save оновлює запис І вчить правило (feature/selflearn-dict) ----
    def test_saving_correction_learns_rule(self):
        from fronts.desktop.pages.reverse_dictation import ReverseDictationDialog
        wav = self._ctl.dictation_audio_path(self._rec_audio)
        dlg = ReverseDictationDialog(self._ctl, self._rec_audio, audio_path=wav,
                                     autoplay=False)
        self._widgets.append(dlg)
        profile = dlg._profile
        dlg._editor.setPlainText("сирий worktree")
        dlg._on_save()
        # запис оновлено ТА виведено правило самонавчання (без окремого запиту)
        self.assertIsNotNone(dlg._result)
        self.assertEqual(dlg._result.status, "learned")
        self.assertTrue(self_learning.list_learned(profile))   # правило в цьому словнику

    def test_switch_profile_mid_dialog_learns_captured_profile(self):
        # ІЗОЛЯЦІЯ на рівні UI: перемикання активного словника поки діалог відкритий
        # НЕ переадресує навчання — воно йде в ЗАХОПЛЕНИЙ на відкритті профіль.
        from fronts.desktop.pages.reverse_dictation import ReverseDictationDialog
        wav = self._ctl.dictation_audio_path(self._rec_audio)
        dlg = ReverseDictationDialog(self._ctl, self._rec_audio, audio_path=wav,
                                     autoplay=False)
        self._widgets.append(dlg)
        captured = dlg._profile
        other = Profile("work", Path(self._tmp.name) / "work_profile")
        other.dir.mkdir(parents=True, exist_ok=True)
        self._ctl.profile = other                     # імітуємо перемикання словника
        dlg._editor.setPlainText("сирий worktree")
        dlg._on_save()
        self.assertTrue(self_learning.list_learned(captured))     # вивчив захоплений
        self.assertEqual(self_learning.list_learned(other), [])   # інший — цілий

    # ---- голосове виправлення виділеного ----
    def test_voice_fix_replaces_selection(self):
        from fronts.desktop.pages.reverse_dictation import ReverseDictationDialog
        dlg = ReverseDictationDialog(self._ctl, self._rec_plain, audio_path=None,
                                     autoplay=False)
        self._widgets.append(dlg)
        self.assertTrue(hasattr(dlg, "_voice_btn"))
        dlg._voice_fix()
        self.assertTrue(self._ctl.voiced)             # Command Mode викликано
        self.assertIn("ГОЛОСОВЕ ВИПРАВЛЕННЯ", dlg._editor.toPlainText())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.TestLoader().loadTestsFromTestCase(ReverseDictationRenderTests))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
