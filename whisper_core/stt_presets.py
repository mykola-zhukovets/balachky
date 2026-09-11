"""Пресети моделі розпізнавання мовлення (STT) для UI вибору.

Раніше вибір був зашитий двома радіо (turbo / large-v3). Слабкі ПК потребують
менших моделей — рушій faster-whisper підтримує їх «з коробки» (ті самі
офіційні Systran-репо, ліцензія MIT). Тут — ЧИСТО ДАНІ: id пресета збігається з
іменем моделі faster-whisper (те, що йде у WhisperModel і в cfg.model_name),
плюс i18n-ключі назви/підказки про залізо та сторінка моделі для лінка «Про
модель». Логіка (repo_for/revision_for, докачка, детекція наявності) — модельно
агностична й лежить у whisper_core.models; додати/прибрати пресет = правка цього
списку, не коду.

ДЕФОЛТ НЕ МІНЯЄМО (канон: до A/B-тесту точності лишається turbo) — тут лише
розширюємо доступний вибір.

Розміри/VRAM — стандартні, добре задокументовані значення faster-whisper (не
специфічні для української). int8 на CPU суттєво менший за VRAM fp16.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Ідентифікатор репозиторію моделі «власник/назва» (як на HuggingFace) — для
# опції «власна модель за HF-id». Локальна тека визначається окремо (шлях на диску).
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class SttPreset:
    """Опис пресета STT. name — ім'я моделі faster-whisper (== cfg.model_name).
    label_key/hint_key — i18n-ключі; page_url — сторінка моделі (лінк «Про модель»)."""
    name: str
    label_key: str
    hint_key: str
    license_name: str = "MIT"
    page_url: str = ""
    # feature/stt-sherpa-parakeet: яким рушієм працює пресет —
    # "whisper" (faster-whisper/CTranslate2, кеш HuggingFace) або
    # "sherpa" (sherpa-onnx, пакет у components/stt). Власна модель
    # користувача завжди трактується як whisper.
    kind: str = "whisper"


# Порядок у комбо: від найлегшої (слабкі ПК) до найточнішої. Turbo лишається
# рекомендованим дефолтом; порядок не змінює того, яка модель активна (її задає
# cfg.model_name). Усі — офіційні multilingual-репо Systran/mobiuslabs, MIT.
PRESETS: "list[SttPreset]" = [
    SttPreset(
        name="small",
        label_key="stt_preset_small",
        hint_key="stt_preset_small_hint",
        page_url="https://huggingface.co/Systran/faster-whisper-small",
    ),
    SttPreset(
        name="medium",
        label_key="stt_preset_medium",
        hint_key="stt_preset_medium_hint",
        page_url="https://huggingface.co/Systran/faster-whisper-medium",
    ),
    SttPreset(
        name="large-v3-turbo",
        label_key="stt_preset_turbo",
        hint_key="stt_preset_turbo_hint",
        page_url="https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    ),
    # feature/stt-preset-large-v2: попередниця large-v3 як АЛЬТЕРНАТИВА, не
    # заміна. Публічний бенчмарк на українській (egorsmkv/speech-recognition-uk,
    # Common Voice 10) дає large-v2 13,72 % WER проти 20,53 % у large-v3 — тож
    # людям, яким large-v3 «домислює» слова, є з чого обрати. Дефолт (turbo) і
    # підпис «Найточніша» у large-v3 лишаються до власного A/B-заміру.
    SttPreset(
        name="large-v2",
        label_key="stt_preset_large_v2",
        hint_key="stt_preset_large_v2_hint",
        page_url="https://huggingface.co/Systran/faster-whisper-large-v2",
    ),
    SttPreset(
        name="large-v3",
        label_key="stt_preset_large_v3",
        hint_key="stt_preset_large_v3_hint",
        page_url="https://huggingface.co/Systran/faster-whisper-large-v3",
    ),
    # feature/stt-sherpa-parakeet: другий рушій (sherpa-onnx), не Whisper.
    # NVIDIA Parakeet-TDT-0.6B-v3 — 25 європейських мов, на українській за
    # публічними вимірами (FLEURS uk 6,79 % WER) точніша за Whisper і в рази
    # швидша на процесорі. Ваги CC-BY-4.0 (атрибуція в екрані згоди й нотатках);
    # пакет і маніфест — whisper_core/stt_sherpa_models.py.
    SttPreset(
        name="parakeet-tdt-0.6b-v3",
        label_key="stt_preset_parakeet",
        hint_key="stt_preset_parakeet_hint",
        license_name="CC-BY-4.0",
        page_url="https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
        kind="sherpa",
    ),
]

_BY_NAME = {p.name: p for p in PRESETS}


def get_preset(name: str) -> "SttPreset | None":
    """Пресет за іменем моделі; None — не пресет (напр. власна модель)."""
    return _BY_NAME.get(str(name or "").strip())


def is_preset(name) -> bool:
    return str(name or "").strip() in _BY_NAME


def engine_kind(name) -> str:
    """Вид рушія для імені моделі: "sherpa" лише для відповідного пресета;
    усе інше (пресети Whisper, власна тека чи HF-id) — "whisper"."""
    preset = get_preset(name)
    return preset.kind if preset is not None else "whisper"


def is_repo_id(value) -> bool:
    """Валідний ідентифікатор репозиторію «власник/назва» (для власної HF-моделі)."""
    return bool(_REPO_ID_RE.match(str(value or "").strip()))
