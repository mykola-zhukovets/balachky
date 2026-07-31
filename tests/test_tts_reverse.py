"""Фікс-хвиля 5.1 (рецензія хвилі 5): reverse-звʼязка ЛИШЕ через панель «Прослухати».

РЕАЛЬНІ шляхи (не ізольовані моки wrapper): спільний voice_edit_selection НЕ чіпає
TTS (Б1); cancel діалогу не лишає застарілий стан (Б2); часткова правка — чесний
стан, не тиха смерть панелі (Б3). CommandEditDialog патчиться (модальний exec)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop.app import DesktopApp
from fronts.desktop.i18n import tr

_APP = QApplication.instance() or QApplication([])


class _FakeDialog:
    """Замість CommandEditDialog: за режимом або застосовує правку, або скасовує."""
    apply_text = None                # None → cancel (apply_fn не викликається)

    def __init__(self, selected_text, preset_id, apply_fn, **kw):
        self._apply = apply_fn

    def exec(self):
        if _FakeDialog.apply_text is not None:
            self._apply(_FakeDialog.apply_text)
        return 0


def _base_ctl(**extra):
    ctl = SimpleNamespace(
        cfg=SimpleNamespace(protocol_model="fast"),
        _command_dialog=None, _command_dictating=False,
        recorder=SimpleNamespace(recording=False, stop=lambda: None),
        _command_record_toggle=lambda: None,
        tray=SimpleNamespace(notify=lambda *a: None))
    for k, v in extra.items():
        setattr(ctl, k, v)
    return ctl


def _bind(ctl, name):
    import types
    setattr(ctl, name, types.MethodType(getattr(DesktopApp, name), ctl))


class TestSharedPathDoesNotTouchTts(unittest.TestCase):
    """БЛОКЕР 1: спільний voice_edit_selection (Command Mode / AI-edit головного вікна /
    наради / зворотне диктування Історії) НЕ чіпає TTS — не паузить і не озвучує чужий
    текст. Реальний шлях через voice_edit_selection."""

    def _run_shared(self, apply_text):
        marks = []
        applied = []
        ctl = _base_ctl(_tts_controller=SimpleNamespace(
            mark_reverse_pause=lambda: marks.append(1) or True,
            consume_reverse_index=lambda: marks.append("consume") or 2,
            play_text=lambda t: marks.append(("play", t))))
        _bind(ctl, "voice_edit_selection")
        _FakeDialog.apply_text = apply_text
        with patch("fronts.desktop.pages.command_edit_ui.CommandEditDialog", _FakeDialog), \
                patch("whisper_core.config.protocol_custom_models", return_value=[]):
            ctl.voice_edit_selection("виділене", applied.append)
        return marks, applied

    def test_ai_edit_of_other_text_does_not_touch_tts(self):
        # правка ІНШОГО тексту (нарада/головне вікно) → TTS не паузиться, не грає
        marks, applied = self._run_shared("виправлений фрагмент наради")
        self.assertEqual(applied, ["виправлений фрагмент наради"])   # правка застосована
        self.assertEqual(marks, [])                 # TTS НЕ торкнуто (немає mark/consume/play)

    def test_cancel_shared_does_not_touch_tts(self):
        marks, applied = self._run_shared(None)      # cancel
        self.assertEqual(applied, [])
        self.assertEqual(marks, [])


class TestPanelReverseFix(unittest.TestCase):
    """Reverse-звʼязка ЛИШЕ через listen_panel_voice_fix (правка тексту САМОЇ панелі)."""

    def _panel_ctl(self, *, playing_idx=2, apply_text="виправлено"):
        from fronts.desktop.tts_panel import ListenPanel
        panel = ListenPanel(None, has_voice=True, text="Перше слово тут. Друге речення там.")
        state = {"reverse": {"idx": playing_idx}}
        events = []

        def mark():
            events.append("mark")
            return True

        def consume():
            events.append("consume")
            s = state["reverse"]
            state["reverse"] = None
            return s["idx"] if s else None

        ctrl = SimpleNamespace(
            mark_reverse_pause=mark, consume_reverse_index=consume,
            # рецензія 5.3: play_text повертає "playing"; arm прив'язується до last_generation()
            play_text=lambda t: (events.append(("play", t)), "playing")[1],
            last_generation=lambda: 1)
        notes = []
        ctl = _base_ctl(_tts_controller=ctrl, _tts_panel=panel,
                        tray=SimpleNamespace(notify=lambda k: notes.append(k)))
        _bind(ctl, "voice_edit_selection")
        _bind(ctl, "listen_panel_voice_fix")
        _bind(ctl, "_tts_resume_panel")
        _FakeDialog.apply_text = apply_text
        return ctl, panel, events, notes

    def test_cancel_clears_reverse_state(self):
        # БЛОКЕР 2: пауза → cancel діалогу → consume викликано (стан чистий), тост
        ctl, panel, events, notes = self._panel_ctl(apply_text=None)   # cancel
        with patch("fronts.desktop.pages.command_edit_ui.CommandEditDialog", _FakeDialog), \
                patch("whisper_core.config.protocol_custom_models", return_value=[]):
            ctl.listen_panel_voice_fix()
        self.assertIn("mark", events)
        self.assertIn("consume", events)             # стан СКИНУТО (не лишили індекс)
        self.assertFalse([e for e in events if isinstance(e, tuple) and e[0] == "play"])
        self.assertIn(tr("tts_reverse_cancelled"), notes)   # тост показано

    def test_partial_edit_honest_state_not_silent(self):
        # БЛОКЕР 3: часткова правка → короткий текст < збереженого речення → чесний
        # стан (тост «текст змінено» + resume=0), НЕ тиха смерть панелі
        ctl, panel, events, notes = self._panel_ctl(playing_idx=5, apply_text="ок")
        # виділяємо перше слово в редакторі панелі
        cur = panel._editor.textCursor()
        cur.select(cur.SelectionType.WordUnderCursor)
        panel._editor.setTextCursor(cur)
        with patch("fronts.desktop.pages.command_edit_ui.CommandEditDialog", _FakeDialog), \
                patch("whisper_core.config.protocol_custom_models", return_value=[]):
            ctl.listen_panel_voice_fix()
        # правку застосовано (ресинтез викликано) — не тиша
        self.assertTrue(any(isinstance(e, tuple) and e[0] == "play" for e in events))
        # позиція недосяжна → чесний тост + resume з початку
        self.assertIn(tr("tts_text_changed"), notes)
        self.assertEqual(panel._resume_index, 0)

    def test_valid_edit_resumes_at_sentence(self):
        # правка з валідним індексом → продовження з того ж речення (не з 0), без тосту
        ctl, panel, events, notes = self._panel_ctl(playing_idx=1, apply_text="виправлене")
        cur = panel._editor.textCursor()
        cur.select(cur.SelectionType.WordUnderCursor)
        panel._editor.setTextCursor(cur)
        with patch("fronts.desktop.pages.command_edit_ui.CommandEditDialog", _FakeDialog), \
                patch("whisper_core.config.protocol_custom_models", return_value=[]):
            ctl.listen_panel_voice_fix()
        self.assertTrue(any(isinstance(e, tuple) and e[0] == "play" for e in events))
        self.assertNotIn("tts_text_changed", notes)
        self.assertEqual(panel._resume_index, 1)     # продовження з речення 1


class TestStaleResumeIndex(unittest.TestCase):
    """БЛОКЕР 1 (рецензія 5.2): застарілий resume-індекс не може вистрілити пізніше —
    прив'язка до генерації synth-запиту. Реальний шлях через ListenPanel.enqueue_chunk."""

    def _panel(self):
        from fronts.desktop.tts_panel import ListenPanel
        return ListenPanel(None, has_voice=True, text="Перше. Друге. Третє. Четверте.")

    def _wav(self):
        # реальний тимчасовий wav-файл, щоб InlinePlayer.set_source мав що читати
        import tempfile, wave
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 1600)
        def _rm():
            try:                       # плеєр може ще тримати handle (Windows-лок) — best-effort
                os.path.exists(path) and os.remove(path)
            except OSError:
                pass
        self.addCleanup(_rm)
        return path

    def test_shorter_resynth_starts_not_silent(self):
        # Правка робить текст КОРОТШИМ: попередній потік (armed resume=3) обірвано ДО
        # чанка 3 → resume лишився 3; новий КОРОТШИЙ потік має 2 чанки. Старий код
        # мовчить (0>=3, 1>=3 — жоден чанк не грає); фікс — грає з початку.
        panel = self._panel()
        panel.set_resume_index(3)                      # armed після reverse-паузи
        panel.enqueue_chunk(self._wav(), [], is_first=True)   # потік A (обірвано)
        # новий (інший, коротший) потік
        panel.enqueue_chunk(self._wav(), [], is_first=True)   # потік B, чанк 0
        panel.enqueue_chunk(self._wav(), [], is_first=False)  # потік B, чанк 1
        self.assertNotEqual(panel._cur_index, -1)      # НЕ мовчить: щось грає

    def test_resume_index_does_not_survive_new_synth(self):
        # resume-індекс не переживає новий (не-armed) synth-запит іншого тексту
        panel = self._panel()
        panel.set_resume_index(3)                      # armed для потоку A
        panel.enqueue_chunk(self._wav(), [], is_first=True)   # A споживає arm
        panel.enqueue_chunk(self._wav(), [], is_first=True)   # B: новий, не armed
        self.assertEqual(panel._resume_index, 0)       # застарілий індекс скинуто

    def test_armed_resume_honored_for_its_stream(self):
        # легітимний armed resume дійсний РІВНО для наступного потоку (не зламали фічу)
        panel = self._panel()
        panel.set_resume_index(1)
        panel.enqueue_chunk(self._wav(), [], is_first=True)   # чанк 0 — до resume, скіп
        self.assertEqual(panel._cur_index, -1)         # ще не дійшли до resume=1
        panel.enqueue_chunk(self._wav(), [], is_first=False)  # чанк 1 == resume → грає
        self.assertEqual(panel._cur_index, 1)

    # --- рецензія 5.3: прив'язка arm до generation-токена наскрізно ------------------
    def test_disarm_before_first_chunk_next_stream_from_start(self):
        # (а) arm для генерації 1 → потік 1 СКАСОВАНО ДО першого чанка (disarm) →
        # наступний НЕЗАЛЕЖНИЙ потік 2 грає З ПОЧАТКУ, не успадковує чужу позицію.
        panel = self._panel()
        panel.set_resume_index(3, generation=1)               # armed для генерації 1
        panel.disarm(1)                                       # потік 1 обірвано ДО чанка
        panel.enqueue_chunk(self._wav(), [], is_first=True, generation=2)   # потік 2
        self.assertEqual(panel._resume_index, 0)              # позицію НЕ успадковано
        self.assertEqual(panel._cur_index, 0)                 # грає з початку (чанк 0)

    def test_stale_generation_chunk_ignored(self):
        # (б) чанк СТАРОЇ генерації, що прийшов ПІСЛЯ скасування (симуляція черги Qt),
        # ігнорується повністю: не глушить і не зсуває нове відтворення.
        panel = self._panel()
        panel.enqueue_chunk(self._wav(), [], is_first=True, generation=2)   # генерація 2 грає
        self.assertEqual(panel._cur_index, 0)
        # застряглий у черзі Qt чанк скасованої генерації 1
        panel.enqueue_chunk(self._wav(), [], is_first=False, generation=1)  # чужий → ігнор
        self.assertEqual(len(panel._chunks), 1)               # не додано
        self.assertEqual(panel._cur_index, 0)                 # не зсунуто
        # навіть повторний is_first старої генерації відсіюється
        panel.enqueue_chunk(self._wav(), [], is_first=True, generation=1)
        self.assertEqual(panel._active_gen, 2)                # активна генерація незмінна
        self.assertEqual(len(panel._chunks), 1)

    def test_resume_honored_for_own_generation(self):
        # (в) щасливий resume СВОЄЇ генерації працює як у 5.2 (arm споживає лише її чанк).
        panel = self._panel()
        panel.set_resume_index(1, generation=7)
        panel.enqueue_chunk(self._wav(), [], is_first=True, generation=7)   # чанк 0 < resume, скіп
        self.assertEqual(panel._cur_index, -1)
        panel.enqueue_chunk(self._wav(), [], is_first=False, generation=7)  # чанк 1 == resume → грає
        self.assertEqual(panel._cur_index, 1)

    def test_late_foreign_drop_preserves_pending_resume(self):
        # Суд 5.5: disarm СТРОГО генераційний. Запізнілий drop ЧУЖОЇ (обірваної) генерації,
        # що прийшов ПІСЛЯ старту доставки нашої генерації, НЕ обнуляє її ще не спожитий
        # resume_index. ЧЕРВОНИЙ на 5.4 (там _resume_gen is None матчив будь-який drop).
        panel = self._panel()
        panel.set_resume_index(2, generation=5)               # ціль — речення 2, генерація 5
        panel.enqueue_chunk(self._wav(), [], is_first=True, generation=5)   # чанк 0 < 2 → скіп; arm спожито
        self.assertEqual(panel._cur_index, -1)
        self.assertEqual(panel._resume_index, 2)              # ще чекаємо на речення 2
        panel.disarm(4)                                       # запізнілий drop ЧУЖОЇ генерації 4
        self.assertEqual(panel._resume_index, 2)              # НЕ скинуто (на 5.4 було б 0)
        panel.enqueue_chunk(self._wav(), [], is_first=False, generation=5)  # чанк 1 < 2 → скіп
        panel.enqueue_chunk(self._wav(), [], is_first=False, generation=5)  # чанк 2 == 2 → грає
        self.assertEqual(panel._cur_index, 2)


class TestPanelTextRaceGate(unittest.TestCase):
    """БЛОКЕР 2 (рецензія 5.2): гонка підміни тексту панелі під час модального діалогу
    правки — ревізія-гейт скасовує застосування правки до чужого тексту."""

    def _panel_ctl(self):
        from fronts.desktop.tts_panel import ListenPanel
        panel = ListenPanel(None, has_voice=True, text="Оригінальний текст панелі.")
        events = []
        state = {"reverse": {"idx": 0}}

        def mark():
            events.append("mark")
            return True

        def consume():
            events.append("consume")
            state["reverse"] = None
            return 0

        ctrl = SimpleNamespace(
            mark_reverse_pause=mark, consume_reverse_index=consume,
            play_text=lambda t: events.append(("play", t)))
        notes = []
        ctl = _base_ctl(_tts_controller=ctrl, _tts_panel=panel,
                        tray=SimpleNamespace(notify=lambda k: notes.append(k)))
        _bind(ctl, "voice_edit_selection")
        _bind(ctl, "listen_panel_voice_fix")
        _bind(ctl, "_tts_resume_panel")
        return ctl, panel, events, notes

    def test_foreign_text_swap_aborts_edit(self):
        ctl, panel, events, notes = self._panel_ctl()

        class _HijackDialog:
            def __init__(self, selected, preset, apply_fn, **kw):
                self._apply = apply_fn

            def exec(self):
                # гонка: інший «Прослухати» з хоткея підмінив текст панелі
                panel.set_text("Зовсім інший текст.")
                self._apply("правка оригіналу")
                return 0

        with patch("fronts.desktop.pages.command_edit_ui.CommandEditDialog", _HijackDialog), \
                patch("whisper_core.config.protocol_custom_models", return_value=[]):
            ctl.listen_panel_voice_fix()
        # правку НЕ застосовано до підміненого тексту
        self.assertEqual(panel._editor.toPlainText(), "Зовсім інший текст.")
        self.assertIn(tr("tts_panel_text_changed"), notes)
        self.assertIn("consume", events)               # reverse-стан скинуто
        # ресинтез правки НЕ запускали
        self.assertFalse([e for e in events if isinstance(e, tuple) and e[0] == "play"])

    def test_hotkey_listen_blocked_during_panel_fix(self):
        # простіший запобіжник: новий «Прослухати» з хоткея не перехоплює панель,
        # поки відкритий діалог правки панелі
        ctl, panel, events, notes = self._panel_ctl()
        _bind(ctl, "listen_selection_from_hotkey")
        opened = []
        ctl.open_listen_panel = lambda t: opened.append(t)

        class _DuringDialog:
            def __init__(self, selected, preset, apply_fn, **kw):
                self._apply = apply_fn

            def exec(self):
                ctl.listen_selection_from_hotkey()     # спроба перехопити панель
                return 0                                # cancel правки

        with patch("fronts.desktop.pages.command_edit_ui.CommandEditDialog", _DuringDialog), \
                patch("whisper_core.config.protocol_custom_models", return_value=[]), \
                patch("fronts.desktop.app.capture_selection", return_value="щось інше"):
            ctl.listen_panel_voice_fix()
        self.assertEqual(opened, [])                    # панель не перехоплено
        self.assertIn(tr("tts_busy_editing"), notes)


if __name__ == "__main__":
    unittest.main()
