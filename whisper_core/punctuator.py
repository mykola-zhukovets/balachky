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

import hashlib
import importlib.util
import logging
import os
import shutil
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import netlog   # доказова офлайновість: журнал вихідних з'єднань

_log = logging.getLogger(__name__)

# Публічний ідентифікатор моделі для сумісності з наявними налаштуваннями.
MODEL_NAME = "pcs_47lang"
MODEL_REVISION = "1b9d51fc7989ebc61e844d407d9dadd08ff4ba28"
MODEL_REPO = "1-800-BAD-CODE/punct_cap_seg_47_language"


@dataclass(frozen=True)
class ModelAsset:
    url: str
    filename: str
    size: int
    sha256: str


MODEL_ASSETS = (
    ModelAsset(
        url=(f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/"
             "punct_cap_seg_47lang.onnx"),
        filename="punct_cap_seg_47lang.onnx",
        size=232986305,
        sha256=(
            "640d91c06b7cc5b3e065c12a7097188378aad3bc11568ff1d72c4c0a2acb0df4"),
    ),
    ModelAsset(
        url=(f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/"
             "spe_unigram_64k_lowercase_47lang.model"),
        filename="spe_unigram_64k_lowercase_47lang.model",
        size=1186519,
        sha256=(
            "1bc15b6e5fd80dfac9999582ce3efcad2ac1f7cf4e0e9769b329f5de9ca5af47"),
    ),
)

# Конфіг малий і стабільний; тримаємо його в застосунку, а не створюємо третю
# мережеву точку. Файли моделі й токенізатора лишаються пінованими та хешованими.
_CONFIG_TEXT = """languages:
  [af, am, ar, bg, bn, de, el, en, es, et, fa, fi, fr, gu, hi, hr, hu, id,
   is, it, ja, kk, kn, ko, ky, lt, lv, mk, ml, mr, nl, or, pa, pl, ps, pt,
   ro, ru, rw, so, sr, sw, ta, te, tr, uk, zh]
max_length: 128
pre_labels: ["<NULL>", "¿"]
post_labels:
  ["<NULL>", ".", ",", "?", "？", "，", "。", "、", "・", "।", "؟", "،",
   ";", "።", "፣", "፧"]
"""
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
    """Швидка UI-перевірка: маркер і файли очікуваного розміру на місці."""
    try:
        target = Path(model_dir)
        return ((target / _READY_MARKER).is_file()
                and all((target / asset.filename).is_file()
                        and (target / asset.filename).stat().st_size == asset.size
                        for asset in MODEL_ASSETS))
    except OSError:
        return False


def available(model_dir) -> bool:
    """Крок готовий: є і пакет punctuators, і завантажена модель."""
    return punctuators_available() and model_available(model_dir)


def load_model(model_dir):
    """Завантажити ONNX-модель пунктуатора з локальної теки. → None, якщо пакета
    немає або модель недоступна (виклик безпечний навіть без пакета)."""
    target = Path(model_dir)
    _recover_previous(target)
    if not available(target) or not _assets_valid(target):
        return None
    try:
        from punctuators.models.punc_cap_seg_model import (
            PunctCapSegConfigONNX, PunctCapSegModelONNX)
        config = PunctCapSegConfigONNX(
            directory=str(model_dir),
            spe_filename="spe_unigram_64k_lowercase_47lang.model",
            model_filename="punct_cap_seg_47lang.onnx",
            config_filename="config.yaml",
        )
        return PunctCapSegModelONNX(config)
    except Exception:
        return None


class PunctuatorDownloadError(RuntimeError):
    pass


def _sha256_of(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _asset_valid(target: Path, asset: ModelAsset) -> bool:
    path = target / asset.filename
    try:
        return (path.is_file() and path.stat().st_size == asset.size
                and _sha256_of(path) == asset.sha256)
    except OSError:
        return False


def _assets_valid(target: Path) -> bool:
    return all(_asset_valid(target, asset) for asset in MODEL_ASSETS)


def _replace_with_retry(source: Path, destination: Path, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05)


def _recover_previous(target: Path) -> bool:
    """Відновити найновіший цілісний backup, якщо активація лишила target відсутнім."""
    if target.exists():
        return False
    try:
        candidates = sorted(
            target.parent.glob(".punctuator.prev-*"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True)
    except OSError:
        return False
    for backup in candidates:
        if not model_available(backup) or not _assets_valid(backup):
            continue
        try:
            _replace_with_retry(backup, target)
        except OSError:
            _log.warning(
                "Не вдалося відновити попередню модель пунктуатора з %s",
                backup, exc_info=True)
            continue
        _log.warning("Відновлено попередню модель пунктуатора з %s", backup)
        return True
    return False


def _download_asset(asset: ModelAsset, destination: Path, progress_cb=None,
                    cancel_check=None) -> None:
    netlog.record_url(asset.url, kind=netlog.MODEL, detail="punctuation")
    request = urllib.request.Request(
        asset.url, headers={"User-Agent": "Balachky/punctuator"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, \
                destination.open("wb") as output:
            received = 0
            while True:
                if cancel_check and cancel_check():
                    raise InterruptedError()
                block = response.read(256 * 1024)
                if not block:
                    break
                output.write(block)
                received += len(block)
                if progress_cb:
                    progress_cb(received, asset.size)
    except InterruptedError:
        raise
    except Exception as exc:
        raise PunctuatorDownloadError(
            f"Не вдалося завантажити модель пунктуатора: {exc}") from exc


def download_and_install(model_dir, progress_cb=None, cancel_check=None) -> None:
    """Докачати ONNX-модель пунктуатора у задану теку й позначити готовність.

    Завантажуємо рівно два файли закріпленої ревізії, перевіряємо точний розмір
    і SHA-256 кожного, тоді атомарно активуємо теку."""
    target = Path(model_dir)
    if model_available(target) and _assets_valid(target):
        return
    if not punctuators_available():
        raise PunctuatorDownloadError(
            "Пакет punctuators не встановлено — компонент недоступний")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(
        prefix="punctuator-", dir=target.parent))
    backup = None
    try:
        for asset in MODEL_ASSETS:
            destination = stage / asset.filename
            _download_asset(
                asset, destination, progress_cb, cancel_check)
            if not _asset_valid(stage, asset):
                raise PunctuatorDownloadError(
                    f"Розмір або контрольна сума не збігаються: {asset.filename}")
        (stage / "config.yaml").write_text(_CONFIG_TEXT, encoding="utf-8")
        (stage / _READY_MARKER).write_text("ok", encoding="utf-8")
        if target.exists():
            backup = target.parent / (
                f".punctuator.prev-{next(tempfile._get_candidate_names())}")
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup is not None and backup.exists():
                _replace_with_retry(backup, target)
            raise
        stage = None
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


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
