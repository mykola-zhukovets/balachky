"""Хвиля 4: діалог виправлення вимови (§6.4) — валідація/save/прев'ю/відмінки."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fronts.desktop.tts_pron import PronunciationDialog
from whisper_core.tts import lexicon as L

_APP = QApplication.instance() or QApplication([])


class TestPronDialog(unittest.TestCase):
    def test_save_text_replace(self):
        saved = []
        d = PronunciationDialog(None, word="Коростень",
                                on_save=lambda **kw: saved.append(kw))
        d._simple.setText("Коро́стень")
        d._do_save()
        self.assertTrue(saved)
        self.assertEqual(saved[0]["correction_type"], L.CORRECTION_TEXT_REPLACE)
        self.assertIn("Коро́стень", saved[0]["value"])

    def test_empty_word_keeps_input_shows_error(self):
        # порожнє слово → криве правило НЕ зникає, показано помилку (NVDA #11407)
        saved = []
        d = PronunciationDialog(None, word="",
                                on_save=lambda **kw: saved.append(kw) or "learned")
        d._simple.setText("щось")
        d._do_save()
        self.assertEqual(saved, [])              # НЕ збережено (порожній match)
        self.assertTrue(d._error.text())         # показано помилку

    def test_updated_status_shown(self):
        # БЛОКЕР 1: on_save повертає "updated" → діалог показує «Оновили», не «Запам'ятали»
        from fronts.desktop.i18n import current_language, set_language
        language = current_language()
        self.addCleanup(set_language, language)
        set_language("uk")
        d = PronunciationDialog(None, word="замок",
                                on_save=lambda **kw: "updated")
        d._simple.setText("за́мок")
        d._do_save()
        self.assertEqual(d._error.text(), "Оновили вимову")

    def test_undo_list_and_delete(self):
        # БЛОКЕР 2: список збережених + видалення (undo)
        from whisper_core.tts.lexicon import PronRule
        store = [PronRule(id="r1", match="замок", value="за́мок")]
        deleted = []
        d = PronunciationDialog(None, word="замок",
                                on_save=lambda **kw: "learned",
                                on_list=lambda: list(store),
                                on_delete=lambda rid: deleted.append(rid))
        self.assertEqual(d._rules_list.count(), 1)   # правило у списку
        d._rules_list.setCurrentRow(0)
        store.clear()                                 # імітуємо revoke на боці app
        d._do_delete()
        self.assertEqual(deleted, ["r1"])            # revoke викликано
        self.assertEqual(d._rules_list.count(), 0)   # список оновлено

    def test_forms_not_propagated_without_confirmation(self):
        # мутація (e): форми поширюються ЛИШЕ за підтвердженням. Заповнюємо список
        # (toggle on), тоді ЗНІМАЄМО згоду (toggle off) — save НЕ має поширювати forms.
        saved = []
        d = PronunciationDialog(None, word="Коростень",
                                on_save=lambda **kw: saved.append(kw) or "learned",
                                on_forms=lambda w: ["Коростень", "Коростеня"])
        d._simple.setText("Коро́стень")
        d._forms_ask.setChecked(True)                # заповнити список форм
        self.assertEqual(d._forms_list.count(), 2)
        d._forms_ask.setChecked(False)               # ЗНЯТИ згоду (список лишається)
        self.assertEqual(d._forms_list.count(), 2)   # список ще заповнений
        d._do_save()
        self.assertEqual(saved[0]["forms"], [])      # БЕЗ згоди — НЕ поширюємо (мутація→червоно)

    def test_forms_propagated_with_confirmation(self):
        saved = []
        d = PronunciationDialog(None, word="Коростень",
                                on_save=lambda **kw: saved.append(kw) or "learned",
                                on_forms=lambda w: ["Коростень", "Коростеня"])
        d._simple.setText("Коро́стень")
        d._forms_ask.setChecked(True)                # підтверджено
        d._do_save()
        self.assertEqual(saved[0]["forms"], ["Коростень", "Коростеня"])

    def test_preview_callback(self):
        previews = []
        d = PronunciationDialog(None, word="замок",
                                on_preview=lambda m, v, c: previews.append((m, v, c)))
        d._simple.setText("за́мок")
        d._do_preview()
        self.assertTrue(previews)

    def test_stress_tab_click_places_accent(self):
        d = PronunciationDialog(None, word="замок")
        # поставити наголос на другій голосній (о, індекс 3 у «замок»)
        d._place_stress(3)
        self.assertIn(L._STRESS if hasattr(L, "_STRESS") else "́", d._stress_value)
        self.assertTrue(d._stress_value.startswith("замо"))

    def test_forms_toggle_populates(self):
        forms_called = []
        d = PronunciationDialog(None, word="Коростень",
                                on_forms=lambda w: (forms_called.append(w) or
                                                    ["Коростень", "Коростеня"]))
        d._forms_ask.setChecked(True)
        self.assertFalse(d._forms_list.isHidden())   # розкрито (offscreen: не isVisible)
        self.assertEqual(d._forms_list.count(), 2)
        self.assertEqual(forms_called, ["Коростень"])


if __name__ == "__main__":
    unittest.main()
