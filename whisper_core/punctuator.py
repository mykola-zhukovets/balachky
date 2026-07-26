"""Пунктуатор/ITN після STT (punctuators pcs_47lang, ONNX, Apache-2.0).

Опційний (opt-in), ЕКСПЕРИМЕНТАЛЬНИЙ крок постобробки ДИКТУВАННЯ. Розставляє
розділові знаки, великі літери й (частково) числа у «сирому» потоці слів від
Whisper. Модель — завантажуваний компонент (ONNX ~234 МБ): у білд не вшивається,
качається за явним натиском кнопки в Налаштуваннях (як моделі діаризації).

Порядок у конвеєрі (feature/punctuation-plus): ПІСЛЯ словників профілю і ПІСЛЯ
автокорекції одруків, ПЕРЕД вставкою — пунктуацію ставимо на вже виправлених
словах.

punctuators — ОПЦІЙНА залежність (тягне onnxruntime, який у стеку вже є, але сам
пакет — ні). Якщо пакета немає, модуль лишається імпортовним, а available()
повертає False (UI вимикає чекбокс із поясненням, build не падає).

УВАГА: живий шлях інференсу (load_model/apply_punctuation) написано за
документованим API punctuators, але його НЕ перевірено вживу в цьому venv (пакет
не встановлено). Логіку доступності/гейтів конвеєра покрито тестами; сам інференс
слід звірити при встановленні пакета й завантаженні моделі.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from . import netlog   # доказова офлайновість: журнал вихідних з'єднань

# Ідентифікатор моделі в HuggingFace-хабі (punctuators.from_pretrained).
MODEL_NAME = "pcs_47lang"
# Маркер локальної готовності: кладемо його поруч зі скачаною моделлю, щоб
# перевіряти доступність без імпорту важкого пакета й без мережі.
_READY_MARKER = "READY"


def punctuators_available() -> bool:
    """Чи встановлено пакет punctuators (опційна залежність)."""
    try:
        return importlib.util.find_spec("punctuators") is not None
    except (ImportError, ValueError):
        return False


def model_available(model_dir) -> bool:
    """Чи завантажено модель у задану теку (за маркером готовності)."""
    try:
        return (Path(model_dir) / _READY_MARKER).is_file()
    except OSError:
        return False


def available(model_dir) -> bool:
    """Крок готовий: є і пакет punctuators, і завантажена модель."""
    return punctuators_available() and model_available(model_dir)


def load_model(model_dir):
    """Завантажити ONNX-модель пунктуатора з локальної теки. → None, якщо пакета
    немає або модель недоступна (виклик безпечний навіть без пакета)."""
    if not available(model_dir):
        return None
    try:
        from punctuators.models import PunctCapSegModelONNX
        # HF-кеш моделі спрямовуємо в нашу локальну теку (без окремого кешу
        # HuggingFace у профілі користувача).
        return PunctCapSegModelONNX.from_pretrained(
            MODEL_NAME, cache_dir=str(model_dir), local_files_only=True)
    except Exception:
        return None


class PunctuatorDownloadError(RuntimeError):
    pass


def download_and_install(model_dir, progress_cb=None, cancel_check=None) -> None:
    """Докачати ONNX-модель пунктуатора у задану теку й позначити готовність.

    Завантаження робить сам пакет punctuators (from_pretrained тягне модель у
    cache_dir через huggingface_hub). progress_cb/cancel_check приймаємо заради
    єдиного інтерфейсу з рештою завантажень, але HF-хаб не дає покрокового
    прогресу тут, тож вони наразі не використовуються. Уже готова модель — no-op."""
    target = Path(model_dir)
    if model_available(target):
        return
    if not punctuators_available():
        raise PunctuatorDownloadError(
            "Пакет punctuators не встановлено — компонент недоступний")
    target.mkdir(parents=True, exist_ok=True)
    netlog.record("huggingface.co", kind=netlog.MODEL, allowed=True,
                  detail="punctuation")
    try:
        from punctuators.models import PunctCapSegModelONNX
        PunctCapSegModelONNX.from_pretrained(MODEL_NAME, cache_dir=str(target))
    except Exception as exc:
        raise PunctuatorDownloadError(
            f"Не вдалося завантажити модель пунктуатора: {exc}") from exc
    (target / _READY_MARKER).write_text("ok", encoding="utf-8")


def apply_punctuation(text: str, model, language: str = "uk") -> str:
    """text → text з розставленою пунктуацією й великими літерами. Порожній текст
    або відсутня модель → повертаємо вхід незмінним.

    punctuators.infer приймає список рядків і повертає список списків речень
    (одне речення → один рядок); склеюємо їх пробілом в один абзац."""
    if not text or model is None:
        return text
    try:
        results = model.infer([text])
    except Exception:
        return text                       # інференс упав — краще сирий текст, ніж краш
    if not results:
        return text
    sentences = results[0]
    if not sentences:
        return text
    return " ".join(s.strip() for s in sentences if s and s.strip()) or text
