"""Лінт КАНОНу текстів UI: заборонені слова у видимих UK-рядках.

Ловить регрес до жаргону й не-канонних формулювань, які Микола вже відхилив:
«тека» (→ папка), «докачка» (→ завантаження), «застосунок» (→ програма),
internal-назви large-v3 / Mica / Hugging Face поза дозволеними дужковими
винятками, а також «ялинки» «» і „лапки-низом“ (house-style — лише “ ”).

Джерело правди: канон текстів інтерфейсу (робочий документ поза репозиторієм).

Тест бере лише ЗНАЧЕННЯ STRINGS["uk"] (видимі рядки), не коментарі коду.
"""
import re
import unittest

from fronts.desktop.i18n import STRINGS

UK = STRINGS["uk"]
EN = STRINGS["en"]

# Технічну назву моделі КАНОН дозволяє як довідку В ДУЖКАХ (принцип 3:
# «Технічні назви (large-v3-turbo, .srt) лишаємо тільки в дужках»). Ці ключі —
# прямий вибір моделі, де назва в дужках доречна; решта рядків її містити не має.
_LARGE_V3_ALLOWED = frozenset({
    "set_model_fast", "set_model_precise",
    "hint_model",
    # fix/stt-models: пресети вибору моделі — назва large-v3(-turbo) доречна
    "stt_preset_turbo", "stt_preset_turbo_hint",
    "stt_preset_large_v3", "stt_preset_large_v3_hint",
    "models_hub_preset_turbo",
    "models_hub_preset_large_v3",
    "models_hub_download_stt_hint",
})


class CanonForbiddenWords(unittest.TestCase):
    def _hits(self, pattern, allowed=frozenset()):
        rx = re.compile(pattern, re.IGNORECASE)
        return sorted(k for k, v in UK.items()
                      if k not in allowed and rx.search(str(v)))

    def test_no_teka(self):
        # «тека»/«теку»/«теки»…; лукбехайнд відсікає «бібліотека», «картотека» тощо.
        hits = self._hits(r"(?<![а-яіїєґ'’-])тек[аиуеоюі]")
        self.assertEqual(hits, [], f"«тека» → «папка» (канон А): {hits}")

    def test_no_dokachka(self):
        hits = self._hits(r"докач")
        self.assertEqual(hits, [], f"«докачка» → «завантаження» (канон В): {hits}")

    def test_no_zastosunok(self):
        # застосунок/застосунків/застосунку — не «застосувати»/«застосовується».
        hits = self._hits(r"застосунк?[аиуові]")
        self.assertEqual(hits, [], f"«застосунок» → «програма» (канон Ґ): {hits}")

    def test_no_large_v3_outside_model_pick(self):
        hits = self._hits(r"large-v3", allowed=_LARGE_V3_ALLOWED)
        self.assertEqual(hits, [], f"«large-v3» лише в дужках вибору моделі: {hits}")

    def test_no_mica(self):
        hits = self._hits(r"mica")
        self.assertEqual(hits, [], f"«Mica» — прибрати internal-назву: {hits}")

    def test_no_hugging_face(self):
        hits = self._hits(r"hugging")
        self.assertEqual(hits, [], f"«Hugging Face» → «інтернет»: {hits}")

    def test_no_korpus(self):
        # «корпус»/«корпусу»… — жаргон (Микола 1.2.1) → «приклади».
        hits = self._hits(r"корпус")
        self.assertEqual(hits, [], f"«корпус» → «приклади» (жаргон 1.2.1): {hits}")

    def test_no_zrazok(self):
        # «зразок»/«зразки»/«зразків» → «приклад».
        hits = self._hits(r"зразк[аиуові]|зразок")
        self.assertEqual(hits, [], f"«зразок» → «приклад» (жаргон 1.2.1): {hits}")

    def test_no_diarizatsiya(self):
        # «діаризація»/«діаризації»… → «розрізнення голосів».
        hits = self._hits(r"діаризац")
        self.assertEqual(hits, [], f"«діаризація» → «розрізнення голосів»: {hits}")

    def test_only_typographic_quotes(self):
        # house-style: скрізь “ ”; жодних «ялинок» чи „лапок-низом“.
        bad = "«»„"
        hits = sorted(k for k, v in UK.items()
                      if any(ch in str(v) for ch in bad))
        self.assertEqual(hits, [], f"лише “ ”, не «» чи „: {hits}")

    def test_vault_locked_mentions_recovery_way_out(self):
        """meeting_error_vault_locked: пароль забуто — найчастіший реальний випадок.
        Код відновлення (unlock_with_recovery у storage_crypto.py) справді відкриває
        сховище в усіх режимах, що ведуть до цієї помилки (VaultPasswordRequired) —
        текст мусить прямо казати про цей вихід, а не лишати читача без ради."""
        uk_text = UK["meeting_error_vault_locked"]
        self.assertIn("код відновлення", uk_text.lower(),
                       "UK-текст має згадувати код відновлення як вихід")
        self.assertIn("забув пароль", uk_text.lower(),
                       "UK-текст має вказати посилання «Забув пароль»")

        en_text = EN["meeting_error_vault_locked"]
        self.assertIn("recovery code", en_text.lower(),
                       "EN text must mention the recovery code as a way out")
        self.assertIn("forgot", en_text.lower(),
                       "EN text must point to the “Forgot password” link")

    def test_author_signature_styling(self):
        """Підпис автора на вітальному кроці: один рядок та розмір шрифту level=body (>= 15px)."""
        from PySide6.QtWidgets import QApplication, QLabel
        from fronts.desktop.onboarding import FirstRunWizard
        QApplication.instance() or QApplication([])
        wiz = FirstRunWizard()
        lbl = wiz.findChild(QLabel, "authorLabel")
        self.assertIsNotNone(lbl, "Лейбл підпису автора не знайдено в FirstRunWizard")
        self.assertFalse(lbl.wordWrap(), "Підпис автора має бути в один рядок (wordWrap=False)")
        self.assertEqual(lbl.property("level"), "body", "Підпис автора має використовувати level='body'")
        wiz.close()
        wiz.deleteLater()


if __name__ == "__main__":
    unittest.main()
