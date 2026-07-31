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
import hashlib
import logging
import os
import re
import threading
from pathlib import Path

# Коротші слова «виправляти» надто ризиковано (легко перескочити на інше слово).
MIN_WORD_LEN = 4
# Поріг непевності: далі за стільки правок — це вже інше слово, а не одрук.
MAX_EDIT_DISTANCE = 2

# Послідовність літер, з'єднаних апострофом («комп'ютер», «п'ять») — один токен.
# Цифри та підкреслення не чіпаємо (це не слова природної мови).
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’ʼ][^\W\d_]+)*", re.UNICODE)
_INTEGRITY_CACHE = {}
_INTEGRITY_CACHE_LOCK = threading.Lock()


def symspell_available() -> bool:
    """Чи встановлено пакет symspellpy (опційна залежність)."""
    try:
        return importlib.util.find_spec("symspellpy") is not None
    except (ImportError, ValueError):
        return False


def _integrity_fingerprint(path: Path):
    """Метадані всіх asset-ів: кеш вважає файл незмінним, доки між перевірками
    збігаються size і mtime_ns. Атакер із правом запису в теку голосу може зберегти
    ці значення після підміни; повний захист вимагав би свідомо відкинутого заради
    швидкості робочого шляху повторного SHA-хешування на кожне використання."""
    try:
        stat_result = path.stat()
        if not path.is_file() or stat_result.st_size <= 0:
            return None
    except OSError:
        return None
    return stat_result.st_size, stat_result.st_mtime_ns


def dictionary_available(dict_path, expected_sha256=None) -> bool:
    """Чи завантажено файл частотного словника (непорожній звичайний файл)."""
    p = Path(dict_path)
    if not expected_sha256:
        return _integrity_fingerprint(p) is not None
    expected = expected_sha256.lower()
    scope = (os.path.abspath(os.fspath(p)), expected)
    with _INTEGRITY_CACHE_LOCK:
        fingerprint = _integrity_fingerprint(p)
        if fingerprint is None:
            _INTEGRITY_CACHE.pop(scope, None)
            return False
        cached = _INTEGRITY_CACHE.get(scope)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        try:
            checksum = hashlib.sha256()
            with p.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    checksum.update(block)
            valid = checksum.hexdigest() == expected
        except OSError:
            return False
        if _integrity_fingerprint(p) != fingerprint:
            return False
        _INTEGRITY_CACHE[scope] = (fingerprint, valid)
        return valid


def available(dict_path, expected_sha256=None) -> bool:
    """Крок готовий до роботи: є і пакет symspellpy, і завантажений словник."""
    return (symspell_available()
            and dictionary_available(dict_path, expected_sha256))


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


def load_corrector(dict_path, expected_sha256=None):
    """Побудувати Corrector із частотного словника. → None, якщо symspellpy нема
    або словник не читається (виклик безпечний навіть без встановленого пакета —
    саме тому імпорт symspellpy лінивий)."""
    if (not symspell_available()
            or not dictionary_available(dict_path, expected_sha256)):
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
