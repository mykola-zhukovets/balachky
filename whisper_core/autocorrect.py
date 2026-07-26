"""Автокорекція одруків після STT (symspellpy + завантажуваний частотний словник).

Опційний (opt-in), ЕКСПЕРИМЕНТАЛЬНИЙ крок постобробки ДИКТУВАННЯ. Без Qt і без
глобального стану: корректор будується один раз з частотного словника
(завантажуваний компонент), а сама корекція — чиста функція над текстом.

Порядок у конвеєрі (feature/punctuation-plus): ПІСЛЯ словників профілю
(apply_glossary у engine.transcribe), ПЕРЕД пунктуатором і вставкою. Спершу
чистимо одруки, тоді (окремим кроком) розставляємо пунктуацію на вже виправлених
словах.

Захист від псування тексту:
  • слова з профілю користувача (terms) НЕ «виправляємо» — вони можуть бути
    рідкісними власними назвами, яких немає в частотному словнику;
  • слова, вже відомі словнику (відстань 0), не чіпаємо;
  • короткі слова (< MIN_WORD_LEN) не чіпаємо — там надто легко «виправити» на
    інше коротке слово;
  • виправляємо лише в межах порога редагування MAX_EDIT_DISTANCE — невпевнені
    (далекі) кандидати відкидаємо.

symspellpy — ОПЦІЙНА залежність: якщо пакета немає, модуль лишається імпортовним,
а available() повертає False (UI вимикає чекбокс із поясненням, build не падає).
"""
from __future__ import annotations

import importlib.util
import logging
import re
from pathlib import Path

# Коротші слова «виправляти» надто ризиковано (легко перескочити на інше слово).
MIN_WORD_LEN = 4
# Поріг непевності: далі за стільки правок — це вже інше слово, а не одрук.
MAX_EDIT_DISTANCE = 2

# Послідовність літер, з'єднаних апострофом («комп'ютер», «п'ять») — один токен.
# Цифри та підкреслення не чіпаємо (це не слова природної мови).
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’ʼ][^\W\d_]+)*", re.UNICODE)


def symspell_available() -> bool:
    """Чи встановлено пакет symspellpy (опційна залежність)."""
    try:
        return importlib.util.find_spec("symspellpy") is not None
    except (ImportError, ValueError):
        return False


def dictionary_available(dict_path) -> bool:
    """Чи завантажено файл частотного словника (непорожній звичайний файл)."""
    try:
        p = Path(dict_path)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def available(dict_path) -> bool:
    """Крок готовий до роботи: є і пакет symspellpy, і завантажений словник."""
    return symspell_available() and dictionary_available(dict_path)


def _match_case(original: str, replacement: str) -> str:
    """Перенести регістр original на replacement: УСІ ВЕЛИКІ / Перша велика."""
    if len(original) > 1 and original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


class Corrector:
    """Обгортка над завантаженим SymSpell. Будується один раз (див.
    load_corrector), застосовується багато разів; захищений набір профільних
    слів передається на кожен виклик apply (профіль може змінюватись, а важкий
    словник — ні)."""

    def __init__(self, symspell):
        self._sym = symspell

    def _correct_word(self, word: str, protected: frozenset) -> str:
        from symspellpy import Verbosity
        lower = word.lower()
        if lower in protected or len(lower) < MIN_WORD_LEN:
            return word
        suggestions = self._sym.lookup(
            lower, Verbosity.CLOSEST, max_edit_distance=MAX_EDIT_DISTANCE)
        if not suggestions:
            return word                       # невідоме й без близького кандидата
        best = suggestions[0]
        if best.distance == 0:
            return word                       # слово відоме словнику — не чіпаємо
        return _match_case(word, best.term)

    def apply(self, text: str, protected: frozenset = frozenset()) -> str:
        """text → text з виправленими явними одруками. protected — множина слів
        (у нижньому регістрі), які не чіпати (слова профілю користувача).
        Порожній текст повертаємо як є."""
        if not text:
            return text
        return _WORD_RE.sub(lambda m: self._correct_word(m.group(0), protected), text)


def load_corrector(dict_path):
    """Побудувати Corrector із частотного словника. → None, якщо symspellpy нема
    або словник не читається (виклик безпечний навіть без встановленого пакета —
    саме тому імпорт symspellpy лінивий)."""
    if not symspell_available():
        return None
    # Увесь ланцюг (import → SymSpell() → load_dictionary) під одним try: словник
    # може бути фізично пошкоджений так, що load кидає НЕ OSError (напр. під час
    # парсингу), а конструктор/імпорт — впасти на браку памʼяті. Автокорекція —
    # opt-in косметика; будь-який її збій НЕ має валити диктування. Тихо None →
    # _run_autocorrect віддає сирий текст як є.
    try:
        from symspellpy import SymSpell
        sym = SymSpell(max_dictionary_edit_distance=MAX_EDIT_DISTANCE)
        loaded = sym.load_dictionary(
            str(dict_path), term_index=0, count_index=1, encoding="utf-8")
    except Exception:
        logging.warning("Автокорекцію вимкнено: словник не завантажився", exc_info=True)
        return None
    if not loaded:
        return None
    return Corrector(sym)
