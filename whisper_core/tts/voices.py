"""Менеджер голосів TTS (§7) — дзеркало protocol/model_manager, але МОВНО-СВІДОМЕ.

Первинна ознака вибору голосу — мова ТЕКСТУ, який озвучуємо (не мова інтерфейсу):
розшифровка англомовної наради при укр-UI має читатись англ-голосом. Дефолти —
per-мова (`DEFAULT_VOICE`). resolve() БЕЗ тихого фолбеку; невідповідність мови — окремий
сигнал LANGUAGE_MISMATCH (не None, не тиха погана вимова).

БЕЗ Qt. Завантаження голосу (кілька файлів) — за патерном model_manager (urllib, SHA,
атомарна активація); SHA-піни ЗВІРИТИ ЖИВО перед впровадженням (як Gemma)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .. import netlog, paths

_SAFE_ID_RE = re.compile(r"^[a-z0-9_]+$")
_READY = "READY"
MANIFEST_NAME = "voice.json"
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


class VoiceDownloadError(RuntimeError):
    pass

#: сигнал «активний голос не підходить під мову тексту» (§7.4) — щоб UI запропонував
#: завантажити відповідний, а не показав загальну помилку чи мовчазну каліч.
LANGUAGE_MISMATCH = "language_mismatch"


@dataclass(frozen=True)
class VoicePreset:
    id: str
    engine_kind: str                  # ключ ENGINE_REGISTRY
    languages: tuple                  # ISO-коди, що голос ОЗВУЧУЄ
    files: tuple                      # ((url, filename, min_bytes, sha256), ...)
    approx_size_bytes: int
    label_key: str
    hint_key: str
    license_name: str = ""
    page_url: str = ""
    recommended: bool = False


# SHA-256-піни ЗВІРЕНО ЖИВО 2026-07-24 (HfApi files_metadata + завантаження малих
# файлів). Розкладка файлів у теці голосу диктується тим, як рушій їх ЧИТАЄ:
#   • styletts2 — плоско (config.yml + pytorch_model.bin + style.pt поруч);
#   • radtts — модель у ./models/... (tts_uk качає CWD-відносно), а вокодер у
#     HF-кеш-структурі hf/models--…/snapshots/<commit>/… (hf_hub_download офлайн).
# Розмір/SHA беруться лише з пресета — виправлення точкове.
_VOCOS_COMMIT = "e7d50512f731887429abfa9ba1e82d1a76f2360d"
_VOCOS_SNAP = ("hf/models--patriotyk--vocos-mel-hifigan-compat-44100khz/"
               "snapshots/" + _VOCOS_COMMIT)
VOICE_PRESETS: "dict[str, VoicePreset]" = {
    "styletts2_ua": VoicePreset(
        id="styletts2_ua", engine_kind="styletts2", languages=("uk",),
        files=(
            ("https://huggingface.co/patriotyk/styletts2_ukrainian_single/"
             "resolve/main/config.yml", "config.yml", 1_000,
             "5c426957b2d5578e00869330c1949003092933cac2c42a2dcbbbea84c6774463"),
            ("https://huggingface.co/patriotyk/styletts2_ukrainian_single/"
             "resolve/main/pytorch_model.bin", "pytorch_model.bin", 700_000_000,
             "25e78d882ec4ee5a8a361749004edf6914137760f2be33a71ea24ce22da1a24a"),
            ("https://huggingface.co/patriotyk/styletts2_ukrainian_single/"
             "resolve/main/style.pt", "style.pt", 1_500,
             "f181646626df52fdcf749e93a311686ffb2eaeae8112be0005a8d6efa7dc5cc9"),
        ),
        approx_size_bytes=748_852_000,
        label_key="tts_voice_styletts2", hint_key="tts_voice_styletts2",
        license_name="MIT",
        page_url="https://huggingface.co/patriotyk/styletts2_ukrainian_single",
        recommended=True),
    "radtts_uk": VoicePreset(
        id="radtts_uk", engine_kind="radtts", languages=("uk",),
        files=(
            ("https://huggingface.co/Yehor/radtts-uk/resolve/main/"
             "radtts-pp-dap-model/model_dap_84000_state.pt",
             "models/radtts-pp-dap-model/model_dap_84000_state.pt", 900_000_000,
             "affc1a3f4f59b864c7e19711a802a0a613955f7fe411046c48e08235fa45ead2"),
            ("https://huggingface.co/patriotyk/vocos-mel-hifigan-compat-44100khz/"
             "resolve/main/config.yaml", _VOCOS_SNAP + "/config.yaml", 400,
             "0d970f5cdc1913730c417b49e476bb09bb8b874583d113f71a7f10c8bb3a4b7d"),
            ("https://huggingface.co/patriotyk/vocos-mel-hifigan-compat-44100khz/"
             "resolve/main/pytorch_model.bin", _VOCOS_SNAP + "/pytorch_model.bin",
             50_000_000,
             "310df58f31cd77e56c494df8389f7ebd57bf7b594c585d9221eb9fe888785572"),
        ),
        approx_size_bytes=986_053_000,
        label_key="tts_voice_radtts", hint_key="tts_voice_radtts",
        license_name="Apache 2.0",
        page_url="https://huggingface.co/Yehor/radtts-uk"),
    "kokoro_en": VoicePreset(
        id="kokoro_en", engine_kind="sherpa", languages=("en",),
        files=(),                     # реальні файли — Хвиля 3 (sherpa-onnx)
        approx_size_bytes=90_000_000,
        label_key="tts_voice_kokoro", hint_key="tts_voice_kokoro",
        license_name="Apache 2.0", recommended=True),
    "supertonic": VoicePreset(
        id="supertonic", engine_kind="sherpa",
        languages=("uk", "en", "de", "fr", "es"),
        files=(),                     # Хвиля 3
        approx_size_bytes=120_000_000,
        label_key="tts_voice_supertonic", hint_key="tts_voice_supertonic",
        license_name="OpenRAIL-M"),
}

#: дефолтний голос за мовою тексту (§7.1)
DEFAULT_VOICE = {"uk": "styletts2_ua", "en": "kokoro_en"}
#: універсальний охват для мов без спеціалізованого голосу
FALLBACK_MULTILINGUAL = "supertonic"


def safe_voice_id(voice_id: str) -> "str | None":
    vid = str(voice_id or "").strip().lower()
    return vid if _SAFE_ID_RE.match(vid) else None


def _safe_rel_filename(filename: str) -> bool:
    """filename пресета — лише ВІДНОСНИЙ шлях у межах теки голосу (вкладені підтеки —
    ок для HF-кеш-структури), БЕЗ '..'-сегментів, абсолютних шляхів чи диска
    (захист від запису поза текою при stage)."""
    fn = str(filename or "")
    if not fn:
        return False
    if fn[0] in "/\\" or (len(fn) >= 2 and fn[1] == ":"):
        return False                              # абсолютний / диск (C:)
    segments = re.split(r"[\\/]", fn)
    return all(seg not in ("", "..") for seg in segments)


def default_voice_for(lang: str) -> "str | None":
    return DEFAULT_VOICE.get(str(lang or "").lower(), FALLBACK_MULTILINGUAL)


def voices_for_language(lang: str) -> list:
    """Пресети, що озвучують `lang`, рекомендований — першим."""
    lang = str(lang or "").lower()
    matched = [p for p in VOICE_PRESETS.values() if lang in p.languages]
    return sorted(matched, key=lambda p: (not p.recommended, p.id))


@dataclass(frozen=True)
class ResolvedVoice:
    id: str
    engine_kind: str
    languages: tuple
    manifest_path: str                # абсолютний ЛОКАЛЬНИЙ шлях теки голосу (§3.2)
    downloadable: bool
    label: str = ""
    label_key: str = ""

    def available(self) -> bool:
        from pathlib import Path
        return Path(self.manifest_path).exists()


def resolve(active_voice_id, lang, root=None, custom_list=None):
    """active_voice_id + мова тексту → ResolvedVoice | None | LANGUAGE_MISMATCH.

    None — невідомий id (виклик підіймає чесну помилку, БЕЗ тихого фолбеку).
    LANGUAGE_MISMATCH — голос знайдено, але `lang` не серед його мов (UI пропонує
    завантажити відповідний, §7.2). Порожній lang → перевірку мови не робимо."""
    vid = safe_voice_id(active_voice_id)
    if vid is None:
        return None
    root = paths.tts_voices_dir() if root is None else root
    preset = VOICE_PRESETS.get(vid)
    if preset is not None:
        from pathlib import Path
        voice_dir = Path(root) / vid
        if not paths.safe_under(root, voice_dir):
            return None
        if lang and str(lang).lower() not in preset.languages:
            return LANGUAGE_MISMATCH
        return ResolvedVoice(
            id=vid, engine_kind=preset.engine_kind, languages=preset.languages,
            manifest_path=str(voice_dir), downloadable=True,
            label_key=preset.label_key)
    for cm in custom_list or []:
        if getattr(cm, "id", None) != vid:
            continue
        if lang and str(lang).lower() not in tuple(cm.languages):
            return LANGUAGE_MISMATCH
        return ResolvedVoice(
            id=vid, engine_kind=cm.kind, languages=tuple(cm.languages),
            manifest_path=cm.manifest_path, downloadable=False, label=cm.label)
    return None


# --- завантаження/встановлення голосу (§7.1, патерн model_manager) -----------

def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return (path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                      getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _verify_file(path: Path, min_bytes: int, sha256) -> bool:
    if _is_reparse(path):
        return False
    try:
        if not path.is_file() or path.stat().st_size < int(min_bytes or 0):
            return False
    except OSError:
        return False
    if not sha256:
        return True
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest() == sha256
    except OSError:
        return False


def voice_manifest_dict(preset: "VoicePreset") -> dict:
    """voice.json для встановленого голосу-пресета (читають адаптери, зокрема sherpa)."""
    return {
        "schema": 1, "kind": preset.engine_kind, "label": preset.label_key,
        "languages": list(preset.languages),
        "files": {Path(fn).stem: fn for (_u, fn, _m, _s) in preset.files},
    }


def voice_available(voice_id, root=None) -> bool:
    """Голос готовий: є READY-маркер і всі файли пресета проходять розмір-звірку
    (SHA звіряється РАЗ при встановленні — наявність READY це свідчить)."""
    vid = safe_voice_id(voice_id)
    if vid is None or vid not in VOICE_PRESETS:
        return False
    root = paths.tts_voices_dir() if root is None else root
    vdir = Path(root) / vid
    try:
        if not (vdir / _READY).is_file():
            return False
    except OSError:
        return False
    for (_url, filename, min_bytes, _sha) in VOICE_PRESETS[vid].files:
        if not _verify_file(vdir / filename, min_bytes, None):
            return False
    return True


def _download_file(url: str, dest: Path, progress_cb=None, cancel_check=None) -> None:
    netlog.record_url(url, kind=netlog.MODEL, detail="tts-voice")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            received = 0
            while True:
                if cancel_check and cancel_check():
                    raise InterruptedError()
                part = resp.read(1 << 18)
                if not part:
                    break
                out.write(part)
                received += len(part)
                if progress_cb:
                    progress_cb(received, total)
    except InterruptedError:
        dest.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise VoiceDownloadError(f"Не вдалося завантажити голос: {exc}") from exc


def download_and_install(voice_id, root=None, progress_cb=None, cancel_check=None,
                         force: bool = False) -> None:
    """Докачати ВСІ файли голосу у stage-теку, звірити кожен (розмір+SHA), написати
    voice.json + READY, атомарно активувати (os.replace). Порожній files (sherpa-
    пресети без пінів) → VoiceDownloadError: файли ще не запіновано (жива звірка)."""
    vid = safe_voice_id(voice_id)
    if vid is None or vid not in VOICE_PRESETS:
        raise VoiceDownloadError(f"Невідомий голос: {voice_id!r}")
    preset = VOICE_PRESETS[vid]
    if not preset.files:
        raise VoiceDownloadError(
            "Файли цього голосу ще не запіновано (потрібна жива SHA-звірка)")
    root = paths.tts_voices_dir() if root is None else root
    target = Path(root) / vid
    if not paths.safe_under(root, target):
        raise VoiceDownloadError("Небезпечний шлях голосу")
    if not force and voice_available(vid, root):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="tts-voice-", dir=target.parent))
    try:
        for (url, filename, min_bytes, sha256) in preset.files:
            if not _safe_rel_filename(filename):
                raise VoiceDownloadError(f"Небезпечне ім'я файлу голосу: {filename!r}")
            staged = stage / filename
            staged.parent.mkdir(parents=True, exist_ok=True)   # вкладені шляхи (HF-кеш)
            _download_file(url, staged, progress_cb, cancel_check)
            if not _verify_file(staged, min_bytes, sha256):
                raise VoiceDownloadError(
                    f"Розмір або контрольна сума не збігаються: {filename}")
        (stage / MANIFEST_NAME).write_text(
            json.dumps(voice_manifest_dict(preset), ensure_ascii=False),
            encoding="utf-8")
        (stage / _READY).write_text("ok", encoding="utf-8")
        backup = None
        if target.exists():
            backup = stage.parent / f".{vid}.prev-{next(tempfile._get_candidate_names())}"
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup is not None and backup.exists():
                os.replace(backup, target)
            raise
        stage = None
        if backup is not None:
            import shutil
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if stage is not None:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)


def delete_voice(voice_id, root=None) -> bool:
    """Видалити завантажений голос (звільнити ГБ). True — видалено."""
    vid = safe_voice_id(voice_id)
    if vid is None:
        return False
    root = paths.tts_voices_dir() if root is None else root
    vdir = Path(root) / vid
    if not paths.safe_under(root, vdir) or not vdir.exists():
        return False
    import shutil
    shutil.rmtree(vdir, ignore_errors=True)
    return not vdir.exists()


# --- «Почути зразок»: попередньо згенеровані демо-WAV (§7.6) ------------------

SAMPLES_SUBDIR = "tts-samples"
PROVENANCE_NAME = "PROVENANCE.json"


def sample_wav_path(voice_id) -> "Path | None":
    """Шлях демо-WAV пресета у комплекті (assets/tts-samples/<id>.wav). None, якщо
    файлу ще нема (демо генеруються скриптом на білді з torch — до того картка
    показує чесну заглушку «зразок зʼявиться після збірки», не мовчазну кнопку)."""
    vid = safe_voice_id(voice_id)
    if vid is None:
        return None
    p = paths.assets_dir() / SAMPLES_SUBDIR / f"{vid}.wav"
    return p if p.is_file() else None


def has_sample(voice_id) -> bool:
    return sample_wav_path(voice_id) is not None


# --- «Інша модель»: власний voice-pack (local / HF), безпека §4.4 -------------

CUSTOM_KIND_LOCAL = "local"
CUSTOM_KIND_HF = "hf"
_CUSTOM_PREFIX = "custom_"

# ВЛАСНІ голоси (voice-pack) дозволені ЛИШЕ безпечних рушіїв: sherpa вантажить ONNX
# через валідований combat-path (єдиний вхід). styletts2/radtts делегують tensor-load
# сторонній бібліотеці БЕЗ гарантованого weights_only=True — для власних паків заборонено
# (§4.4, суд хвилі 3). Пресетні styletts2/radtts (SHA-довірений перелік) — як є.
SAFE_CUSTOM_KINDS = frozenset({"sherpa"})


def new_custom_voice_id() -> str:
    import secrets
    return _CUSTOM_PREFIX + secrets.token_hex(6)


@dataclass(frozen=True)
class CustomVoice:
    """Доданий користувачем голос: local voice-pack (тека з voice.json) або HF-repo.
    engine kind + languages беруться з валідованого маніфесту (§4.4)."""
    id: str
    label: str
    kind: str                         # engine kind (ENGINE_REGISTRY), напр. "sherpa"
    source: str                       # CUSTOM_KIND_LOCAL | CUSTOM_KIND_HF
    manifest_path: str = ""           # local: абсолютний шлях теки pack
    repo_id: str = ""                 # hf
    revision: str = ""                # hf immutable commit (TOFU інакше)
    languages: tuple = field(default_factory=tuple)

    def valid(self) -> bool:
        if not _SAFE_ID_RE.match(self.id) or self.id in VOICE_PRESETS:
            return False
        # ВЛАСНІ голоси — лише безпечні рушії (не довільний ENGINE_REGISTRY): styletts2/
        # radtts обходять weights_only через бібліотеку (суд хвилі 3).
        if self.kind not in SAFE_CUSTOM_KINDS:
            return False
        if self.source == CUSTOM_KIND_LOCAL:
            return bool(self.manifest_path)
        if self.source == CUSTOM_KIND_HF:
            return bool(_REPO_ID_RE.match(self.repo_id or ""))
        return False

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id, "label": self.label, "kind": self.kind,
            "source": self.source, "manifest_path": self.manifest_path,
            "repo_id": self.repo_id, "revision": self.revision,
            "languages": list(self.languages)}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw) -> "CustomVoice | None":
        try:
            d = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(d, dict):
            return None
        cv = cls(
            id=str(d.get("id", "")).strip().lower(),
            label=str(d.get("label", "")).strip(),
            kind=str(d.get("kind", "")).strip().lower(),
            source=str(d.get("source", "")).strip().lower(),
            manifest_path=str(d.get("manifest_path", "")),
            repo_id=str(d.get("repo_id", "")).strip(),
            revision=str(d.get("revision", "")).strip(),
            languages=tuple(d.get("languages") or ()))
        return cv if cv.valid() else None


def hf_download_action(revision, resolved_commit, file_shas, existing_lock_json=None):
    """TOFU-consent для HF-голосу (§4.4) — підключення security.VoiceLock до
    завантаження/згоди. Повертає (action, lock_json):
      • "reconsent" — mutable revision без lock АБО зміна commit/SHA після lock →
        потрібна ПОВТОРНА явна згода користувача (виявлення підміни);
      • "lock" — перше довіряння (TOFU): створюємо lock зі resolved commit+SHA;
      • "proceed" — lock збігається, можна вантажити без нової згоди.
    Мережа НЕ потрібна (тести дають commit/SHA моком)."""
    from .security import VoiceLock, hf_needs_lock
    existing = VoiceLock.from_json(existing_lock_json) if existing_lock_json else None
    new_lock = VoiceLock(commit=str(resolved_commit),
                         file_shas={str(k): str(v) for k, v in (file_shas or {}).items()})
    if existing is None:
        # немає lock: mutable revision → потрібна перша явна згода (TOFU), потім lock
        if hf_needs_lock(revision):
            return "reconsent", new_lock.to_json()
        return "lock", new_lock.to_json()
    if existing.changed(commit=resolved_commit, file_shas=file_shas or {}):
        return "reconsent", new_lock.to_json()   # підміна після lock → повторна згода
    return "proceed", existing.to_json()


def custom_voice_from_pack(pack_dir, *, label="") -> "CustomVoice":
    """Створити CustomVoice з ЛОКАЛЬНОГО voice-pack ПІСЛЯ валідації безпеки (§4.4).
    Кидає VoicePackError, якщо pack невалідний/небезпечний."""
    from .security import validate_voice_pack, VoicePackError
    manifest = validate_voice_pack(pack_dir)      # безпекова перевірка (weights_only/YAML/шляхи)
    if manifest.kind not in SAFE_CUSTOM_KINDS:
        # styletts2/radtts власним паком → відхилити на ДОДАВАННІ (не на пізнішому
        # load), чесний текст (§4.4, суд хвилі 3)
        raise VoicePackError("tts_voice_custom_engine",
                             f"тип {manifest.kind!r} не дозволений для власних голосів")
    return CustomVoice(
        id=new_custom_voice_id(), label=label or manifest.label or "Мій голос",
        kind=manifest.kind, source=CUSTOM_KIND_LOCAL,
        manifest_path=str(Path(pack_dir).resolve()), languages=manifest.languages)


# --- легкий детектор мови тексту (§7.2) — НЕ агресивний ------------------------

_UK_MARK = set("ґєїіҐЄЇІ")
_RU_MARK = set("ыъэЫЪЭ")
_PL_MARK = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def detect_language(text: str) -> str:
    """Мова тексту за спаданням певності. Повертає 'uk'|'en'|'unknown'|'mixed'.

    НЕ агресивний «кирилиця=uk, латиниця=en»: російський текст не піде в укр-голос,
    польський — в англ. Короткий/змішаний/непевний → 'unknown'/'mixed' (§7.2 — далі
    UI спитає користувача, а не синтезує наосліп)."""
    text = text or ""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 2:
        return "unknown"
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c in _UK_MARK)
    lat = sum(1 for c in letters if "a" <= c.lower() <= "z")
    if cyr and lat and min(cyr, lat) / max(cyr, lat) > 0.25:
        return "mixed"
    if cyr > lat:
        if any(c in _RU_MARK for c in text):
            return "unknown"                 # російські маркери — не вгадуємо uk
        if any(c in _UK_MARK for c in text):
            return "uk"
        return "unknown"                     # кирилиця без укр-маркерів — непевно
    if lat > cyr:
        if any(c in _PL_MARK for c in text):
            return "unknown"                 # польські діакритики — не en
        return "en"
    return "unknown"
