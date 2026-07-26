"""Адаптер sherpa-onnx TTS (§4.2, §4.5) — реальний рушій для Kokoro-82M/Supertonic 3
(англ/мультимовні кандидати). sherpa-onnx уже в стеку (діаризація), тож нових важких
DLL немає. Читає ONNX-моделі голосів через sherpa_onnx.OfflineTts API.

Тип моделі (vits|kokoro|matcha) береться з voice-pack маніфесту (§4.4). sherpa-onnx TTS
НЕ гарантує пофонемних тривалостей у публічному API → `native_word_timings=False`, і
для караоке цього голосу застосовується faster-whisper-фолбек (§4.5, whisper_core.tts.align).

Живий синтез перевіриться на білді з реальною моделлю; тут — коректний код за API +
чиста деградація (EngineLoadError), коли модель відсутня."""
from __future__ import annotations

import json
import os

from .base import EngineCapabilities, EngineLoadError, SynthResult

_DEFAULT_SR = 24000
MANIFEST_NAME = "voice.json"


class SherpaTtsEngine:
    KIND = "sherpa"

    def __init__(self):
        self._tts = None
        self._sample_rate = _DEFAULT_SR
        self._languages = ("en",)

    def capabilities(self) -> EngineCapabilities:
        # native_word_timings=False: sherpa-onnx TTS не дає пофонемних тривалостей у
        # публічному API → караоке через faster-whisper-фолбек (§4.5).
        return EngineCapabilities(
            sample_rate=self._sample_rate, supported_languages=self._languages,
            native_word_timings=False, stress_override=False,
            phonetic_override=False, sentence_split_internal=False)

    def load(self, model_path: str) -> None:
        """`model_path` — тека voice-pack з voice.json + ONNX-файлами (абсолютний
        локальний шлях). БЕЗ прихованого завантаження.

        БОЙОВИЙ ШЛЯХ БЕЗПЕКИ (§4.4, ревізія Sol CRITICAL): валідація voice-pack —
        ЄДИНИЙ вхід. Використовуємо САНІТИЗОВАНИЙ маніфест (не сирий re-read):
        канонізація шляхів, заборона виконуваних файлів, ONNX external-data
        fail-closed, ліміт розміру pack. Малформований/шкідливий → EngineLoadError.

        Порядок саме такий: спершу перевірка pack, і лише потім рушій. Шкідливий
        pack мусить бути відхилений незалежно від того, чи рушій узагалі є в цьому
        середовищі — інакше на машині без sherpa-onnx перевірка безпеки не
        відбувається зовсім, а тест на неї падає з чужої причини."""
        from ..security import VoicePackError, validate_voice_pack
        try:
            manifest = validate_voice_pack(model_path)   # безпека ПЕРЕД будь-яким load
        except VoicePackError as exc:
            raise EngineLoadError(f"voice-pack відхилено: {exc.reason_key}") from exc
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise EngineLoadError(f"sherpa-onnx недоступний: {exc}") from exc
        self._sample_rate = int(manifest.sample_rate or _DEFAULT_SR)
        self._languages = tuple(manifest.languages or ("en",))
        # будуємо config із САНІТИЗОВАНОГО manifest.files (сирий voice.json повторно
        # НЕ читаємо — уникаємо TOCTOU і небезпечних полів)
        config = build_tts_config(sherpa_onnx,
                                  {"model_type": manifest.model_type or "vits",
                                   "files": manifest.files}, model_path)
        if config is None:
            raise EngineLoadError("Невідомий тип sherpa-моделі у voice.json")
        try:
            self._tts = sherpa_onnx.OfflineTts(config)
        except Exception as exc:                 # noqa: BLE001 — битий/відсутній файл
            raise EngineLoadError(f"Не вдалося завантажити sherpa-голос: {exc}") from exc
        # перевірка фактичного sample_rate проти маніфесту (§4.4): не на віру
        actual = int(getattr(self._tts, "sample_rate", self._sample_rate))
        if actual and self._sample_rate and abs(actual - self._sample_rate) > 1:
            self._sample_rate = actual           # довіряємо ФАКТИЧНОМУ, не заявленому

    def synthesize(self, text: str, *, speed: float, want_timings: bool,
                   lexicon=None) -> SynthResult:
        if self._tts is None:
            raise EngineLoadError("sherpa-голос не завантажено")
        import numpy as np
        audio = self._tts.generate(text, sid=0, speed=float(speed))
        wav = np.asarray(audio.samples, dtype=np.float32)
        sr = int(getattr(audio, "sample_rate", self._sample_rate))
        # native_word_timings=False → без token_durations; караоке піде фолбеком (§4.5)
        return SynthResult(
            wav=wav, sample_rate=sr, normalized_text=text,
            token_durations=None, frame_hop_ms=0.0, phoneme_to_word=None)

    def unload(self) -> None:
        self._tts = None


def _read_manifest(model_path: str) -> dict:
    mpath = os.path.join(model_path, MANIFEST_NAME)
    if not os.path.isfile(mpath):
        raise EngineLoadError(f"немає {MANIFEST_NAME} у теці голосу")
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as exc:
        raise EngineLoadError(f"пошкоджений {MANIFEST_NAME}: {exc}") from exc


def build_tts_config(sherpa_onnx, manifest: dict, model_dir: str):
    """Побудувати OfflineTtsConfig за маніфестом (тип моделі vits|kokoro|matcha).
    Чиста функція (крім sherpa-типів) — тестується логіка вибору без OfflineTts.
    None — невідомий тип."""
    files = manifest.get("files") or {}
    mtype = str(manifest.get("model_type") or manifest.get("sherpa_type") or "vits").lower()
    threads = max(1, (os.cpu_count() or 1))

    def _p(name):
        return os.path.join(model_dir, files[name]) if name in files else ""

    if mtype == "kokoro":
        model_cfg = sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=_p("model"), voices=_p("voices"), tokens=_p("tokens"),
                data_dir=os.path.join(model_dir, files.get("data_dir", "espeak-ng-data")),
                lexicon=_p("lexicon")),
            num_threads=threads, provider="cpu")
    elif mtype == "matcha":
        model_cfg = sherpa_onnx.OfflineTtsModelConfig(
            matcha=sherpa_onnx.OfflineTtsMatchaModelConfig(
                acoustic_model=_p("model"), vocoder=_p("vocoder"),
                tokens=_p("tokens"),
                data_dir=os.path.join(model_dir, files.get("data_dir", "espeak-ng-data"))),
            num_threads=threads, provider="cpu")
    elif mtype == "vits":
        model_cfg = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=_p("model"), tokens=_p("tokens"), lexicon=_p("lexicon"),
                data_dir=os.path.join(model_dir, files.get("data_dir", ""))),
            num_threads=threads, provider="cpu")
    else:
        return None
    return sherpa_onnx.OfflineTtsConfig(model=model_cfg)
