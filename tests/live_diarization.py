"""Живий тест поза discover: потрібні локальні моделі і WAV.

Запуск (façade-шлях, суцільний масив):
    BALACHKY_DIAR_MODELS=C:\\... python tests/live_diarization.py

Семантика ``threshold`` — косинусна ВІДСТАНЬ (0.5 = дефолт sherpa, менше → більше
кластерів), НЕ схожість. Фіксований K=2 — надійний демо-режим; auto лише звітує
спостережену кількість/RTF, без «must be 2» до калібрування українським корпусом.
"""
import os
import time
from pathlib import Path

from whisper_core.meeting.diarize import DISTANCE_THRESHOLD, diarize, load_wav_f32_16k


if __name__ == "__main__":
    root = os.environ["BALACHKY_DIAR_MODELS"]
    wav = Path(r"<тека з еталонними записами>\2-two-speakers-en.wav")
    audio = load_wav_f32_16k(wav)
    seconds = len(audio) / 16000

    t0 = time.perf_counter()
    fixed = diarize(audio, num_speakers=2, configured_dir=root)
    fixed_rtf = (time.perf_counter() - t0) / seconds
    fixed_speakers = {spk for _s, _e, spk in fixed}
    assert len(fixed_speakers) == 2, fixed_speakers
    print(f"OK fixed K=2: 2 speakers, RTF={fixed_rtf:.3f}")

    t0 = time.perf_counter()
    auto = diarize(audio, num_speakers=None, threshold=DISTANCE_THRESHOLD,
                   configured_dir=root)
    auto_rtf = (time.perf_counter() - t0) / seconds
    auto_speakers = {spk for _s, _e, spk in auto}
    # auto — БЕЗ обов'язкового «== 2»: лише спостереження (distance 0.5 давав 3).
    print(f"auto @distance {DISTANCE_THRESHOLD}: {len(auto_speakers)} speakers, "
          f"RTF={auto_rtf:.3f}")
