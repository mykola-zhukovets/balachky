"""Адаптер StyleTTS2-UA (дефолтний укр-голос) — за bench.make_styletts2().

torch/styletts2_inference/ipa_uk/ukrainian_word_stress імпортуються ЛИШЕ у методах
(живуть у воркер-EXE, не в GUI). Тривалості — forward-hook на приватному
`model.predictor.duration_proj`: КАНАРКА DURATION_HOOK_PATH ловить зникнення цього
приватного імені після апдейту пакета (§4.2, §12.7)."""
from __future__ import annotations

from .base import EngineCapabilities, EngineLoadError, SynthResult

SAMPLE_RATE = 24000
FRAME_HOP_MS = 25.0
# Приватний шлях, на який чіпляємо duration-hook. Змінюй лише разом із канаркою.
DURATION_HOOK_PATH = ("predictor", "duration_proj")


def durations_from_proj(proj_out, speed: float) -> list:
    """Вихід duration_proj форми (batch=1, seq, max_dur) → ПЛАСКИЙ список per-token
    тривалостей (int). Чиста функція (numpy, БЕЗ torch), щоб форму можна було
    покривати офлайн — torch-гілку synthesize() тестує лише real-engine golden, який
    скіпається без стека воркера, тож зникнення reshape(-1)/clamp тут інакше не
    ловиться. sigmoid→сума по max_dur→/speed→round→clamp(≥1)→РОЗПЛЮЩИТИ batch."""
    import numpy as np
    arr = np.asarray(proj_out, dtype=np.float64)
    sig = 1.0 / (1.0 + np.exp(-arr))
    summed = sig.sum(axis=-1) / float(speed)
    dur = np.maximum(np.round(summed), 1.0).reshape(-1)   # reshape: (1,seq)→(seq,)
    return [int(x) for x in dur.tolist()]


class StyleTTS2Engine:
    KIND = "styletts2"

    def __init__(self):
        self._model = None
        self._style = None
        self._stressify = None
        self._ipa = None
        self._stress_symbol = None
        self._torch = None
        self._dur_store = {}

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            sample_rate=SAMPLE_RATE, supported_languages=("uk",),
            native_word_timings=True, stress_override=True,
            phonetic_override=True, sentence_split_internal=False)

    def load(self, model_path: str) -> None:
        """`model_path` — тека голосу з вагами + style.pt + config (абсолютний
        локальний шлях, §3.2). БЕЗ прихованого завантаження з мережі."""
        try:
            import os
            import torch
            from ipa_uk import ipa
            from styletts2_inference.models import StyleTTS2
            from ukrainian_word_stress import Stressifier, StressSymbol
        except ImportError as exc:                     # torch/пакети лише у воркері
            raise EngineLoadError(f"Рушій StyleTTS2 недоступний: {exc}") from exc

        torch.set_num_threads(os.cpu_count() or 1)
        self._torch = torch                      # збережений з guarded-load для synthesize
        self._stressify = Stressifier()
        self._ipa = ipa
        self._stress_symbol = StressSymbol
        # StyleTTS2(hf_path=...) трактує аргумент як HF repo-id і КАЧАЄ з мережі —
        # локальна тека дала б HFValidationError. Тож вантажимо напряму з файлів теки
        # голосу: config_path + weights_path (config.yml + pytorch_model.bin), БЕЗ
        # жодного мережевого виклику.
        self._model = StyleTTS2(
            config_path=os.path.join(model_path, "config.yml"),
            weights_path=os.path.join(model_path, "pytorch_model.bin"),
            device="cpu")
        style_file = os.path.join(model_path, "style.pt")
        if os.path.isfile(style_file):
            # ВСІ tensor-файли (вкл. style embeddings) — weights_only=True (§4.4):
            # ніколи не виконуємо pickle-код чужого голосу.
            self._style = torch.load(style_file, weights_only=True)
        node = self._model
        for attr in DURATION_HOOK_PATH:
            node = getattr(node, attr)          # канарка: AttributeError → пакет змінив API
        node.register_forward_hook(
            lambda m, i, o: self._dur_store.__setitem__("out", o.detach()))

    def _prep(self, text: str, lexicon=None) -> str:
        import re
        from unicodedata import normalize as unorm
        t = (text or "").strip().replace('"', '')
        t = t.replace('+', self._stress_symbol.CombiningAcuteAccent)  # «+» після складу
        t = unorm('NFKC', t)
        t = re.sub(r'[᠆‐‑‒–—―⁻₋−⸺⸻]', '-', t)
        if t and t[-1] not in '.?!:-':
            t += '.'
        t = re.sub(r' - ', ': ', t)
        t = self._stressify(t)
        # §6.2 рівень 2: stress-override зі словника ПІСЛЯ Stressifier, ДО ipa()
        # (перекриває словникову неоднозначність ukrainian-word-stress — розвідка §1а)
        if lexicon is not None and not getattr(lexicon, "is_empty", lambda: True)():
            t = lexicon.apply_stress(t)
        ipa = self._ipa(t)
        # §6.2 рівень 3: phonetic-override ПЕРЕД акустичною моделлю (у сховищі/конвеєрі,
        # НЕ в acceptance Хвилі 4 — §6.4)
        if lexicon is not None and not getattr(lexicon, "is_empty", lambda: True)():
            ipa = lexicon.apply_phonetic(ipa)
        return ipa

    def synthesize(self, text: str, *, speed: float, want_timings: bool,
                   lexicon=None) -> SynthResult:
        if self._model is None or self._torch is None:
            raise EngineLoadError("StyleTTS2 не завантажено")
        self._dur_store.clear()
        phonemes = self._prep(text, lexicon)
        tok = self._model.tokenizer.encode(phonemes)
        wav = self._model(tok, speed=speed, s_prev=self._style)
        durations = None
        p2w = []
        if want_timings and "out" in self._dur_store:
            # hook ловить вихід duration_proj форми (batch=1, seq, max_dur); чиста
            # durations_from_proj сумує по max_dur і РОЗПЛЮЩУЄ batch → (seq,).
            durations = durations_from_proj(
                self._dur_store["out"].cpu().numpy(), speed)
            p2w = self._phoneme_to_word(text, len(durations))
        return SynthResult(
            wav=wav.detach().cpu().numpy(), sample_rate=SAMPLE_RATE,
            normalized_text=text, token_durations=durations,
            frame_hop_ms=FRAME_HOP_MS, phoneme_to_word=p2w)

    def _phoneme_to_word(self, text: str, n_tokens: int) -> list:
        """Best-effort token→індекс-слова (§8.2): кодуємо IPA КОЖНОГО слова окремо,
        щоб дістати per-word к-ть токенів, і розкладаємо n_tokens по словах. Модель
        рахується на ПОВНОМУ ipa (якість); тут — лише мапа для караоке.

        КАНАРКА (жива звірка з torch): сума per-word токенів може трохи різнитись від
        n_tokens через стики; вирівнюємо пропорційно й клампимо, щоб len == n_tokens.
        Точний збіг перевіряє real-engine golden (test_tts_timings)."""
        import re
        words = re.findall(r"\S+", text or "")
        if not words or n_tokens <= 0:
            return [0] * max(0, n_tokens)
        counts = []
        for w in words:
            try:
                counts.append(max(1, len(self._model.tokenizer.encode(self._prep(w)))))
            except Exception:                    # noqa: BLE001
                counts.append(1)
        total = sum(counts) or 1
        p2w = []
        for wi, c in enumerate(counts):
            share = max(1, round(c * n_tokens / total))
            p2w.extend([wi] * share)
        if len(p2w) > n_tokens:
            p2w = p2w[:n_tokens]
        while len(p2w) < n_tokens:
            p2w.append(len(words) - 1)
        return p2w

    def unload(self) -> None:
        self._model = None
        self._style = None
        self._dur_store.clear()
