"""Реєстр рушіїв TTS (§4.3).

Ключ KIND НЕ приходить від користувача довільним рядком — лише з цього реєстру
(voice_id → engine kind визначає preset/voice-pack manifest, §7). Значення реєстру —
ЛІНИВІ фабрики (zero-arg → клас рушія), а не імпортовані класи: sherpa-адаптера ще
нема до Хвилі 3, тож реєстр не має падати на його імпорті (§10 sherpa-stub)."""
from __future__ import annotations

from .base import (EngineCapabilities, EngineLoadError, SynthResult,  # noqa: F401
                   TtsEngine)


def _styletts2_cls():
    from .styletts2 import StyleTTS2Engine
    return StyleTTS2Engine


def _radtts_cls():
    from .radtts import RadTtsEngine
    return RadTtsEngine


def _fake_cls():
    from .fake import FakeTtsEngine
    return FakeTtsEngine


def _sherpa_cls():
    """Лінива фабрика sherpa (§4.5). До Хвилі 3 адаптера нема — чесна помилка,
    а не ImportError на побудові реєстру."""
    try:
        from .sherpa import SherpaTtsEngine
    except ImportError as exc:
        raise EngineLoadError(
            "Рушій sherpa-onnx ще не реалізовано (Хвиля 3)") from exc
    return SherpaTtsEngine


#: kind → фабрика класу рушія
ENGINE_REGISTRY = {
    "styletts2": _styletts2_cls,
    "radtts": _radtts_cls,
    "sherpa": _sherpa_cls,
    "fake": _fake_cls,
}


def create_engine(kind: str):
    """Створити екземпляр рушія за KIND із реєстру. Невідомий kind → EngineLoadError
    (БЕЗ тихого фолбеку на інший рушій)."""
    factory = ENGINE_REGISTRY.get(str(kind or ""))
    if factory is None:
        raise EngineLoadError(f"Невідомий рушій синтезу: {kind!r}")
    return factory()()


__all__ = ["ENGINE_REGISTRY", "create_engine", "EngineCapabilities",
           "EngineLoadError", "SynthResult", "TtsEngine"]
