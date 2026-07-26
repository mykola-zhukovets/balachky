"""Абстракція рушія синтезу (§4.1). БЕЗ Qt, БЕЗ важких залежностей на рівні модуля.

Конкретні адаптери (styletts2/radtts/sherpa) імпортують torch/onnx ЛИШЕ у своїх
методах — щоб реєстр і сам GUI-процес не тягли важкі дерева (torch живе у воркері).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EngineCapabilities:
    """Можливості рушія — керують конвеєром словника й караоке."""
    sample_rate: int                    # 24000 (StyleTTS2) | 44100 (RAD-TTS)
    supported_languages: tuple          # ISO-коди, напр. ("uk",)
    native_word_timings: bool           # чи дає пофонемні тривалості (караоке)
    stress_override: bool = False       # чи приймає U+0301-наголос
    phonetic_override: bool = False     # чи приймає готовий IPA
    sentence_split_internal: bool = False  # чи ріже на речення сам


@dataclass
class SynthResult:
    """Результат синтезу одного речення/чанку."""
    wav: object                         # np.ndarray float32 mono
    sample_rate: int
    normalized_text: str
    token_durations: "list | None" = None   # кадри декодера (караоке); None → без таймінгів
    frame_hop_ms: float = 0.0                # StyleTTS2 ~25.0; RAD-TTS 11.6
    phoneme_to_word: list = field(default_factory=list)  # токен → індекс слова (§8)


@runtime_checkable
class TtsEngine(Protocol):
    """Контракт рушія. KIND — стабільний ключ реєстру (НЕ довільний рядок від
    користувача)."""
    KIND: str

    def load(self, model_path: str) -> None: ...

    def capabilities(self) -> EngineCapabilities: ...

    def synthesize(self, text: str, *, speed: float, want_timings: bool,
                   lexicon=None) -> SynthResult: ...

    def unload(self) -> None: ...


class EngineLoadError(RuntimeError):
    """Рушій не вдалося завантажити (немає torch, битий файл голосу тощо)."""
