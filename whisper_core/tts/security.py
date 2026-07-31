"""Безпека «власного модуля користувача» (§4.4, закриває блокер 5).

Категорична межа: «власний голос» = МОДЕЛЬ (ваги + конфіг) ВІДОМОГО формату рушія,
а НЕ довільний Python-код. Ніколи не імпортуємо й не виконуємо код користувача.
Перевірка розширення `.pt` НЕ робить файл безпечним — тому нижче суворі правила:

  • schema маніфесту відома; kind ∈ ENGINE_REGISTRY;
  • кожен відносний шлях канонізується й лишається ВСЕРЕДИНІ теки pack (safe_under);
    ../, абсолютні шляхи, reparse-точки/junction → відхилити;
  • ВСІ tensor-файли (вкл. style embeddings) — лише torch.load(weights_only=True);
  • config через yaml.safe_load + allowlist полів; ніякого yaml.load;
  • жодних .py/.pyc/.dll/.so у pack;
  • ONNX external-data за межами pack → відхилити;
  • HF — immutable revision або TOFU-lock; зміна SHA після lock → повторна згода.

Модуль-межа: БЕЗ Qt. Причини відмови — i18n-ключами (UI показує людською мовою)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from .engines import ENGINE_REGISTRY

MANIFEST_NAME = "voice.json"
KNOWN_SCHEMA = 1
_FORBIDDEN_EXT = {".py", ".pyc", ".pyo", ".pyd", ".dll", ".so", ".dylib",
                  ".exe", ".bat", ".cmd", ".sh"}
# дозволені верхньорівневі поля config.yml рушія (allowlist; невідомі → ігноруємо)
_CONFIG_ALLOWED = {"model_params", "preprocess_params", "sample_rate", "n_mels",
                   "hop_length", "win_length", "n_fft", "tokens", "languages"}


class VoicePackError(ValueError):
    """Voice-pack невалідний/небезпечний (reason_key — i18n-ключ причини)."""

    def __init__(self, reason_key: str, detail: str = ""):
        super().__init__(detail or reason_key)
        self.reason_key = reason_key


def _is_reparse_point(path: Path) -> bool:
    import stat
    try:
        info = path.lstat()
    except OSError:
        return True
    return (path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                      getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


_KNOWN_SHERPA_TYPES = {"vits", "kokoro", "matcha", ""}
_MAX_PACK_BYTES = 3 * (1024 ** 3)             # ліміт розміру voice-pack (§4.4)


@dataclass(frozen=True)
class VoicePackManifest:
    schema: int
    kind: str
    label: str
    languages: tuple
    files: dict
    sample_rate: int
    model_type: str = ""                      # sherpa: vits|kokoro|matcha (валідований)


def _load_manifest(pack_dir: Path) -> dict:
    mpath = pack_dir / MANIFEST_NAME
    if not mpath.is_file():
        raise VoicePackError("tts_voice_custom_invalid", "немає voice.json")
    try:
        with mpath.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError) as exc:
        raise VoicePackError("tts_voice_custom_invalid", str(exc)) from exc
    if not isinstance(data, dict):
        raise VoicePackError("tts_voice_custom_invalid", "маніфест не об'єкт")
    return data


def validate_voice_pack(pack_dir) -> VoicePackManifest:
    """Валідувати локальний voice-pack. Успіх → VoicePackManifest; будь-яке порушення
    безпеки → VoicePackError(reason_key). Дзеркало CustomModel.valid()."""
    pack = Path(pack_dir)
    if not pack.is_dir() or _is_reparse_point(pack):
        raise VoicePackError("tts_voice_custom_invalid", "тека pack недоступна/reparse")
    data = _load_manifest(pack)

    if data.get("schema") != KNOWN_SCHEMA:
        raise VoicePackError("tts_voice_custom_invalid", "невідома версія маніфесту")
    kind = str(data.get("kind", ""))
    if kind not in ENGINE_REGISTRY:
        raise VoicePackError("tts_voice_custom_invalid", f"невідомий рушій: {kind!r}")

    # жодних виконуваних файлів ніде в pack + ліміт сумарного розміру (§4.4)
    total_size = 0
    for root, _dirs, names in os.walk(pack):
        for name in names:
            if os.path.splitext(name)[1].lower() in _FORBIDDEN_EXT:
                raise VoicePackError("tts_voice_custom_invalid",
                                     f"заборонений файл у pack: {name}")
            try:
                total_size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    if total_size > _MAX_PACK_BYTES:
        raise VoicePackError("tts_voice_custom_invalid",
                             "voice-pack завеликий (перевищує ліміт)")

    mtype = str(data.get("model_type") or data.get("sherpa_type") or "").lower()
    if mtype not in _KNOWN_SHERPA_TYPES:
        raise VoicePackError("tts_voice_custom_invalid",
                             f"невідомий тип моделі: {mtype!r}")

    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise VoicePackError("tts_voice_custom_invalid", "немає files у маніфесті")

    for _role, rel in files.items():
        rel = str(rel)
        # ЄДИНА лінія оборони шляху — КАНОНІЗАЦІЯ (paths.safe_under робить realpath і
        # звіряє is_relative_to). Рядковий `.. in parts` тут НЕ дублюємо: він
        # маскував би мутацію канонізації (обхід через regчутливість тому/8.3-імена/
        # відсутність `..` у literal). safe_under ловить абсолютні шляхи, `../`,
        # символьні лінки й reparse однаково — незалежно від існування файлу.
        target = (pack / rel)                     # abs rel → Path бере абсолютний
        if not paths.safe_under(pack, target):
            raise VoicePackError("tts_voice_custom_invalid",
                                 f"шлях виходить за межі pack: {rel}")
        if not target.exists() or _is_reparse_point(target):
            raise VoicePackError("tts_voice_custom_invalid",
                                 f"файл відсутній або reparse: {rel}")
        if target.suffix.lower() == ".onnx":
            check_onnx_external_data(target, pack)     # external-data лише в межах pack

    # config.yml — лише safe_load + allowlist
    cfg = pack / "config.yml"
    if cfg.is_file():
        safe_yaml_config(cfg)

    langs = data.get("languages") or []
    if not isinstance(langs, list):
        raise VoicePackError("tts_voice_custom_invalid", "languages не список")
    try:
        sr = int(data.get("sample_rate", 0))
    except (ValueError, TypeError):
        raise VoicePackError("tts_voice_custom_invalid", "sample_rate не число")

    return VoicePackManifest(
        schema=KNOWN_SCHEMA, kind=kind, label=str(data.get("label", "")),
        languages=tuple(str(x) for x in langs), files=dict(files), sample_rate=sr,
        model_type=mtype)


def load_tensor_file(path, torch_module=None):
    """Завантажити tensor-файл (вкл. style.pt) ВИКЛЮЧНО через weights_only=True —
    ніколи не покладаючись на стороннню бібліотеку. Виконуваний pickle не запускається.

    torch_module — інжекція для тестів (без реального torch)."""
    if torch_module is None:
        try:
            import torch as torch_module
        except ImportError as exc:               # torch лише у воркер-EXE
            raise VoicePackError("tts_voice_corrupt",
                                 "torch недоступний для читання ваг") from exc
    return torch_module.load(str(path), weights_only=True)


def safe_yaml_config(path) -> dict:
    """config.yml → dict через yaml.safe_load (НЕ yaml.load) + allowlist полів.
    YAML-теги/невідомі конструктори → SafeLoader їх відхиляє (raise)."""
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)          # tag-конструктори (!!python/...) → помилка
        except yaml.YAMLError as exc:
            raise VoicePackError("tts_voice_custom_invalid", f"config.yml: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise VoicePackError("tts_voice_custom_invalid", "config.yml не об'єкт")
    # невідомі верхньорівневі поля — ігноруємо (лишаємо лише allowlist)
    return {k: v for k, v in data.items() if k in _CONFIG_ALLOWED}


def check_onnx_external_data(onnx_path, pack_root) -> None:
    """ONNX external-data (.data), що посилається за межі pack, → VoicePackError.
    Якщо пакет `onnx` недоступний — best-effort перевіряємо лише сусідні .data
    файли на канонізацію в межах pack (path-safety — головна лінія оборони §4.4)."""
    pack_root = Path(pack_root)
    try:
        import onnx
        from onnx.external_data_helper import _get_all_tensors
        model = onnx.load(str(onnx_path), load_external_data=False)
        for tensor in _get_all_tensors(model):
            for ext in tensor.external_data:
                if ext.key == "location":
                    ref = (Path(onnx_path).parent / ext.value)
                    if os.path.isabs(ext.value) or ".." in Path(ext.value).parts \
                            or not paths.safe_under(pack_root, ref):
                        raise VoicePackError(
                            "tts_voice_custom_invalid",
                            f"ONNX external-data за межами pack: {ext.value}")
    except ImportError:
        # FAIL-CLOSED (рецензія): без onnx-lib не можемо прочитати внутрішні
        # external-data посилання. Якщо поруч є докази external-data (.data/.onnx_data/
        # .bin, які не самодостатня модель), відхиляємо — не можемо гарантувати межі.
        parent = Path(onnx_path).parent
        try:
            siblings = [p for p in parent.iterdir()
                        if p.suffix.lower() in (".data", ".onnx_data", ".bin")]
        except OSError:
            siblings = []
        if siblings:
            raise VoicePackError(
                "tts_voice_custom_invalid",
                "ONNX external-data неможливо перевірити без onnx (fail-closed)")


# --- HF immutable revision / TOFU-lock (§4.4) --------------------------------

def hf_needs_lock(revision) -> bool:
    """True — revision mutable (немає або 'main'/'master'/'HEAD') → потрібен TOFU-lock,
    а не тихе довіряння. Immutable commit (40-hex або ≥7-hex) locking не потребує."""
    rev = str(revision or "").strip().lower()
    if not rev or rev in ("main", "master", "head", "latest"):
        return True
    return not bool(_looks_like_commit(rev))


def _looks_like_commit(rev: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", rev))


@dataclass(frozen=True)
class VoiceLock:
    """TOFU-lock: зафіксований commit + per-file SHA. Будь-яка зміна → повторна згода."""
    commit: str
    file_shas: dict                           # filename → sha256

    def to_json(self) -> str:
        return json.dumps({"commit": self.commit, "file_shas": self.file_shas},
                          ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw) -> "VoiceLock | None":
        try:
            d = json.loads(raw) if isinstance(raw, str) else dict(raw)
            return cls(commit=str(d["commit"]),
                       file_shas={str(k): str(v) for k, v in d["file_shas"].items()})
        except (ValueError, TypeError, KeyError):
            return None

    def changed(self, *, commit: str, file_shas: dict) -> bool:
        """True — commit або будь-який per-file SHA відрізняється (виявлення підміни)
        → UI вимагає повторної явної згоди (tts_voice_sha_changed)."""
        if str(commit) != self.commit:
            return True
        for name, sha in (file_shas or {}).items():
            if self.file_shas.get(name) != str(sha):
                return True
        return False
