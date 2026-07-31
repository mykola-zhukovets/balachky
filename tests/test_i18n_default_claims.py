"""Ловить розбіжність між заявленим у тексті інтерфейсу значенням "за
замовчуванням" (наприклад "За замовчуванням вимкнено.") і фактичним
дефолтом відповідного поля в whisper_core.config.Config.

Причина: 30.07.2026 користувач помітив, що чекбокс "Виправляти помилки в
словах" на екрані Налаштувань був увімкнений, тоді як підпис стверджував
"За замовчуванням вимкнено." Перевірка коду (Config.autocorrect_enabled =
False) підтвердила, що ТЕКСТ каже правду, а розбіжність — у збереженому
стані конкретного користувача, не в коді. Цей тест лишається як гейт: якщо
хтось змінить дефолт у Config, не оновивши підпис (або навпаки), тест
почервоніє.

Еталон — літеральні bool-значення, скопійовані з config.py на момент
написання тесту (НЕ читання того самого поля, яке перевіряється, і не
порівняння tr(key) з tr(key))."""
import dataclasses
import unittest

from whisper_core.config import Config
from fronts.desktop.i18n import STRINGS

UK = STRINGS["uk"]

# (ключ i18n з підписом, поле Config, очікуваний дефолт "на зараз")
CHECKS = [
    ("set_autocorrect_hint", "autocorrect_enabled", False),
    ("set_paste_typing_hint", "paste_typing_fallback", False),
    ("set_paste_preview_hint", "paste_preview", False),
    ("set_gate_hint", "noise_gate_enabled", False),
    ("set_agc_hint", "agc_enabled", False),
    ("set_highlight_uncertain_hint", "highlight_uncertain_words", True),
    ("set_voice_punct_hint", "voice_punctuation", False),
    ("set_paste_history_hint", "paste_history_enabled", True),
    ("set_transcript_edit_hint", "transcript_editing_enabled", False),
    ("set_punctuator_hint", "punctuator_enabled", False),
    ("set_review_changes_hint", "review_text_changes", False),
]

_OFF_MARKERS = ("вимкнено",)
_ON_MARKERS = ("ввімкнено", "увімкнено")


class TestI18nDefaultClaimsMatchConfig(unittest.TestCase):
    def test_config_defaults_match_hardcoded_expectations(self):
        """Страхувальний трос: якщо хтось змінить дефолт у Config і забуде
        оновити цей список (або підпис), нехай тест впаде тут, а не мовчки."""
        fields = {f.name: f.default for f in dataclasses.fields(Config)}
        for i18n_key, field_name, expected_default in CHECKS:
            with self.subTest(field=field_name):
                self.assertEqual(
                    fields[field_name], expected_default,
                    f"Config.{field_name} default changed — онови CHECKS і "
                    f"перевір текст ключа {i18n_key!r}.")

    def test_hint_text_matches_actual_default(self):
        for i18n_key, field_name, expected_default in CHECKS:
            with self.subTest(key=i18n_key, field=field_name):
                # Частина пояснень має два рівні: коротке видиме завжди і
                # довше за знаком питання (перебудова налаштувань 31.07).
                # Заява про стан за замовчуванням може жити в будь-якому з
                # них — важливо, щоб вона БУЛА і не суперечила коду. Тому
                # звіряємо обидва рівні разом.
                text = UK[i18n_key] + " " + UK.get(i18n_key + "_full", "")
                actual_default = getattr(Config(), field_name)
                self.assertEqual(
                    actual_default, expected_default,
                    f"{field_name}: тест застарів, актуальний дефолт "
                    f"{actual_default!r} != очікуваний {expected_default!r}")
                has_off = any(m in text for m in _OFF_MARKERS)
                has_on = any(m in text for m in _ON_MARKERS)
                self.assertTrue(
                    has_off or has_on,
                    f"{i18n_key}: підпис не заявляє стан за замовчуванням "
                    f"явно — {text!r}")
                if actual_default:
                    self.assertTrue(
                        has_on,
                        f"{i18n_key}: код вмикає це за замовчуванням "
                        f"(Config.{field_name}=True), але текст каже "
                        f"'вимкнено': {text!r}")
                    self.assertFalse(
                        has_off,
                        f"{i18n_key}: підпис одночасно каже і "
                        f"'вимкнено' — суперечність: {text!r}")
                else:
                    self.assertTrue(
                        has_off,
                        f"{i18n_key}: код вимикає це за замовчуванням "
                        f"(Config.{field_name}=False), але текст не каже "
                        f"'вимкнено': {text!r}")


if __name__ == "__main__":
    unittest.main()
