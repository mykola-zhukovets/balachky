"""Адаптер RAD-TTS++ (резервний укр-голос) — за bench.make_radtts().

`tts_uk.synthesis(...)` викликається з УСІМА 13 позиційними/іменованими аргументами
(README-приклад із трьома застарілий!) — інакше TypeError. Тривалості — через
обгортку навколо `ti.radtts.infer` (ключ 'dur', який synthesis() відкидає)."""
from __future__ import annotations

from .base import EngineCapabilities, EngineLoadError, SynthResult

SAMPLE_RATE = 44100
FRAME_HOP_MS = 11.6            # 512 семплів / 44100

# Повний перелік аргументів synthesis() — канарка проти застарілого README (§12.4).
# Значення (крім text/voice/token_dur_scaling) — дефолти з бенчмарку.
SYNTHESIS_DEFAULTS = dict(
    n_takes=1, use_latest_take=False,
    f0_mean=0.0, f0_std=0.0, energy_mean=0.0, energy_std=0.0,
    sigma_decoder=0.8, sigma_token_duration=0.666, sigma_f0=1.0, sigma_energy=1.0)

# tts_uk.inference вантажить моделі на РІВНІ МОДУЛЯ (import), і Python кешує його в
# sys.modules НА ВЕСЬ ПРОЦЕС. Тож load() іншої теки після першого import — тихий
# no-op (той самий модуль, ті самі ваги). Фіксуємо теку першого import ТУТ (процес-
# рівень, бо кожен load_voice робить НОВИЙ екземпляр рушія), щоб чесно відхиляти
# гарячу заміну голосу. _ORIG_INFER — справжній radtts.infer до наших обгорток
# (перевʼязуємо ЗАВЖДИ від нього, щоб не нашаровувати обгортки при повторних load).
_LOADED_MODEL_PATH = None
_ORIG_INFER = None


def _norm_path(p: str) -> str:
    import os
    return os.path.normcase(os.path.abspath(str(p or "")))


def _restore_cwd():
    """Контекст: зберегти CWD на вході, відновити на виході (навіть при винятку).
    tts_uk читає модель CWD-відносно ЛИШЕ на import (synthesis() відносних шляхів
    НЕ читає — звірено по сирцях), тож chdir потрібен тільки навколо import, а
    permanent-мутація CWD процесу неприпустима (ламає сусідні рушії)."""
    import contextlib
    import os

    @contextlib.contextmanager
    def _cm():
        prev = os.getcwd()
        try:
            yield
        finally:
            try:
                os.chdir(prev)
            except OSError:
                pass
    return _cm()


def _prepare_offline_hf(model_path: str) -> str:
    """Підготувати офлайн-оточення для `import tts_uk.inference`, щоб вокодер vocos
    знайшовся hf_hub_download-ом БЕЗ мережі. Кеш вокодера лежить у теці голосу як
    hf/models--…/snapshots/<commit>/…; тут ствердимо refs/main (revision 'main' →
    commit) — інакше офлайн-lookup не знайде snapshot. Виставляємо HF_HUB_CACHE на
    цю теку (і оновлюємо константу, якщо huggingface_hub уже імпортовано в процесі).
    Повертає model_path (майбутній CWD). Мережа НЕ потрібна; тестується без torch."""
    import os
    import sys
    hub_cache = os.path.join(model_path, "hf")
    if os.path.isdir(hub_cache):
        for repo_dir in os.listdir(hub_cache):
            snaps = os.path.join(hub_cache, repo_dir, "snapshots")
            if not os.path.isdir(snaps):
                continue
            commits = [d for d in os.listdir(snaps)
                       if os.path.isdir(os.path.join(snaps, d))]
            if len(commits) != 1:                    # неоднозначно — не чіпаємо
                continue
            refs_dir = os.path.join(hub_cache, repo_dir, "refs")
            os.makedirs(refs_dir, exist_ok=True)
            ref_main = os.path.join(refs_dir, "main")
            if not os.path.isfile(ref_main):
                with open(ref_main, "w", encoding="utf-8") as fh:
                    fh.write(commits[0])
    os.environ["HF_HUB_CACHE"] = hub_cache
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    const = sys.modules.get("huggingface_hub.constants")
    if const is not None:                            # уже імпортовано → env зчитано на імпорті
        const.HF_HUB_CACHE = hub_cache
    return model_path


class RadTtsEngine:
    KIND = "radtts"

    def __init__(self):
        self._ti = None
        self._dur_store = {}
        self._voice = "tetiana"

    def capabilities(self) -> EngineCapabilities:
        # stress/phonetic override — ВІДКРИТИЙ пункт §6.3 (G2P tts_uk не з'ясовано);
        # доки не з'ясовано — лише text_replace.
        return EngineCapabilities(
            sample_rate=SAMPLE_RATE, supported_languages=("uk",),
            native_word_timings=True, stress_override=False,
            phonetic_override=False, sentence_split_internal=False)

    def load(self, model_path: str) -> None:
        """`model_path` — тека голосу. `import tts_uk.inference` на РІВНІ МОДУЛЯ
        жорстко вантажить дві речі БЕЗ параметрів шляху:
          • модель RAD-TTS — hf_hub_download(local_dir="./models/") + torch.load(
            Path("models/...")) → CWD-відносно, тож перемикаємо CWD на теку голосу
            ЛИШЕ на час import (потім відновлюємо — permanent-chdir заборонено);
          • вокодер vocos — hf_hub_download у HF-кеш → готуємо кеш у теці голосу
            (hf/…/snapshots/<commit>) і виставляємо HF_HUB_CACHE + refs/main.
        Усе офлайн (HF_HUB_OFFLINE=1): жодного прихованого завантаження.

        Повторний load() ІНШОЇ теки в тому самому процесі неможливий (import
        закешовано в sys.modules): чесно кидаємо EngineLoadError замість тихого
        no-op зі старими вагами (воркер поверне це як IPC error — деградація чесна,
        не краш). Той самий шлях — дозволений no-op (перевʼязуємо кешований модуль)."""
        global _LOADED_MODEL_PATH
        import os
        import sys
        norm = _norm_path(model_path)
        if "tts_uk.inference" in sys.modules:
            # модуль уже завантажено в цьому процесі (перший голос)
            if _LOADED_MODEL_PATH is not None and norm != _LOADED_MODEL_PATH:
                raise EngineLoadError(
                    "Рушій RAD-TTS не підтримує гарячу заміну голосу в одному "
                    "процесі — потрібен перезапуск воркера")
            ti = sys.modules["tts_uk.inference"]     # той самий шлях → no-op
        else:
            try:
                import contextlib
                _prepare_offline_hf(model_path)      # HF-кеш вокодера + env (до import!)
                with _restore_cwd():                 # chdir лише на час import
                    os.chdir(model_path)             # ./models/... → у теку голосу
                    # tts_uk.inference сам імпортує torch і ДРУКУЄ у stdout на import
                    # ("Loaded checkpoint…") — у воркері stdout це JSON-канал IPC, тож
                    # глушимо у stderr (ImportError без torch спливе як EngineLoadError).
                    with contextlib.redirect_stdout(sys.stderr):
                        import tts_uk.inference as ti  # module-level import вантажить моделі
            except ImportError as exc:
                raise EngineLoadError(f"Рушій RAD-TTS недоступний: {exc}") from exc
            _LOADED_MODEL_PATH = norm
        try:
            import torch
            torch.set_num_threads(os.cpu_count() or 1)
        except ImportError:                          # кешований модуль без torch у тесті
            pass
        self._ti = ti
        self._install_infer_wrap(ti)

    def _install_infer_wrap(self, ti) -> None:
        """Обгорнути radtts.infer, щоб ловити тривалості ('dur') у сховище ЦЬОГО
        екземпляра. Перевʼязуємо ЗАВЖДИ від справжнього original (_ORIG_INFER),
        тож повторний load не нашаровує обгортку на обгортку."""
        global _ORIG_INFER
        if _ORIG_INFER is None:
            _ORIG_INFER = ti.radtts.infer
        orig_infer = _ORIG_INFER
        dur_store = self._dur_store

        def _wrap(*a, **k):
            out = orig_infer(*a, **k)
            try:
                dur_store["dur"] = out["dur"].detach()
            except Exception:                        # noqa: BLE001
                dur_store["dur"] = None
            return out

        ti.radtts.infer = _wrap

    def synthesize(self, text: str, *, speed: float, want_timings: bool,
                   lexicon=None) -> SynthResult:
        if self._ti is None:
            raise EngineLoadError("RAD-TTS не завантажено")
        import contextlib
        import sys
        self._dur_store.clear()
        # tts_uk.inference.synthesis() ДРУКУЄ "Inferencing take N" у stdout на КОЖНОМУ
        # виклику (не лише на import) — у воркері stdout це JSON-канал IPC, тож глушимо
        # кожен synth у stderr (інакше не-JSON-рядок псує потік; сьогодні рятує лише
        # те, що sidecar._read_loop мовчки ковтає не-JSON — випадкова страховка).
        with contextlib.redirect_stdout(sys.stderr):
            _mels, wave, _stats = self._ti.synthesis(
                text=text, voice=self._voice,
                token_dur_scaling=1.0 / speed, **SYNTHESIS_DEFAULTS)
        durations = None
        p2w = []
        if want_timings and self._dur_store.get("dur") is not None:
            dd = self._dur_store["dur"].squeeze()
            durations = [int(x) for x in dd.tolist()]
            p2w = _proportional_phoneme_to_word(text, len(durations))
        return SynthResult(
            wav=wave.squeeze().cpu().numpy(), sample_rate=SAMPLE_RATE,
            normalized_text=text, token_durations=durations,
            frame_hop_ms=FRAME_HOP_MS, phoneme_to_word=p2w)

    def unload(self) -> None:
        self._ti = None
        self._dur_store.clear()


def _proportional_phoneme_to_word(text: str, n_tokens: int) -> list:
    """Best-effort token→індекс-слова (§8.2): tts_uk не дає прямого мапінгу, тож
    розкладаємо n_tokens по словах пропорційно довжині слова (кадри йдуть по фонемах,
    фонем ≈ довжині слова). КАНАРКА: точність доводить golden на build stage з реальним
    рушієм; без мапінгу караоке RAD-TTS взагалі не працювало б (ревізія §8.1)."""
    import re
    words = re.findall(r"\S+", text or "")
    if not words or n_tokens <= 0:
        return [0] * max(0, n_tokens)
    counts = [max(1, len(w)) for w in words]
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
