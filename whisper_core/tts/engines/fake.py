"""Фейковий рушій: для тестів IPC і чесної деградації без torch/моделі.

Дзеркало whisper_core.protocol.worker.FakeBackend. `synthesize` повертає детерміновану
коротку тишу + позначку FAKE_ENGINE_MARKER у normalized_text — оркестрація ловить її,
щоб НЕ видати заглушку за реальний синтез (урок судді: без рушія показуй «завантажте
голос», а не тишу за успіх)."""
from __future__ import annotations

from .. import FAKE_ENGINE_MARKER
from .base import EngineCapabilities, SynthResult

_SR = 24000


class FakeTtsEngine:
    """Синтез без нейромережі: коротка тиша фіксованої частоти."""
    KIND = "fake"

    def __init__(self):
        self._loaded = False

    def load(self, model_path: str) -> None:
        self._loaded = True

    def capabilities(self) -> EngineCapabilities:
        # native_word_timings=True: фейк дає СИНТЕТИЧНІ (детерміновані) тривалості —
        # щоб караоке-шлях (Хвиля 2) тестувався end-to-end без torch (FakeBackend з
        # відомими тривалостями). Реальна якість — StyleTTS2/RAD-TTS у воркер-EXE.
        return EngineCapabilities(
            sample_rate=_SR, supported_languages=("uk",),
            native_word_timings=True)

    def synthesize(self, text: str, *, speed: float, want_timings: bool,
                   lexicon=None) -> SynthResult:
        import os
        import numpy as np
        # тест: BALACHKY_TTS_FAKE_SLEEP уповільнює синтез речення, щоб control-потік
        # устиг прочитати cancel ПІД ЧАС synthesize (доказ окремого потоку).
        sleep_s = float(os.environ.get("BALACHKY_TTS_FAKE_SLEEP", "0") or 0)
        if sleep_s > 0:
            import time
            time.sleep(sleep_s)
        # ~0.1 c тиші на кожні 10 слів (детерміновано, без залежності від контенту)
        words = max(1, len((text or "").split()))
        samples = int(_SR * 0.1 * (words / 10 + 1))
        wav = np.zeros(samples, dtype=np.float32)
        durations = None
        p2w = None
        hop = 0.0
        if want_timings:
            # 1 токен на слово; тривалість = довжина слова (кадри) — детерміновано.
            toks = (text or "").split()
            durations = [max(1, len(w)) for w in toks]
            p2w = list(range(len(toks)))         # токен i → слово i
            hop = 25.0
        return SynthResult(
            wav=wav, sample_rate=_SR,
            normalized_text=f"{FAKE_ENGINE_MARKER} {text}",
            token_durations=durations, frame_hop_ms=hop, phoneme_to_word=p2w)

    def unload(self) -> None:
        self._loaded = False
