"""E2: менеджер завантажуваних GGUF-моделей для AI-протоколу наради.

Два пресети на вибір (сімейство Gemma 4):
  • "fast"    — Gemma 4 E4B Q4_K_M (~5 ГБ, Apache 2.0). За замовчуванням: CPU/слабке залізо.
  • "quality" — Gemma 4 12B QAT UD-Q4_K_XL (~6.3 ГБ, ліцензія Gemma). Якісніша; на GPU
                ~8 ГБ VRAM або на потужному CPU (повільніше). Моделі — окремий
                компонент, качаються на вимогу; у білд НЕ вшиваються.
Токенізатор той самий, решта коду модельно-агностична: щоб додати/замінити модель —
досить правки пресета (URL/розмір/SHA), не логіки.

Патерн — як діаризація й пунктуатор: у білд НЕ вшивається, качається за явним
натиском кнопки з consent-діалогом (де вказано розмір). Прямий resolve-URL із
HuggingFace через urllib (без huggingface_hub → без symlink-пастки frozen-exe,
наш урок HF_HUB_DISABLE_SYMLINKS). Після завантаження — звірка розміру (і SHA, коли
відомий) + маркер READY, як пунктуатор.

УВАГА (звірити перед живим завантаженням): точні repo-id/імена файлів/SHA моделей
Gemma 4 GGUF слід підтвердити на HuggingFace. Тут — best-effort за конвенцією unsloth;
код їх бере лише з пресета, тож виправлення — точкове, без зміни логіки. Живий тест із
реальною моделлю (3-8 ГБ) робить Микола.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import netlog   # доказова офлайновість: журнал вихідних з'єднань

# Маркер локальної готовності поруч із model.gguf (перевірка без хешування щоразу).
_READY_MARKER = "READY"
MODEL_FILENAME = "model.gguf"
_SAFE_ID_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class ModelPreset:
    """Опис завантажуваної моделі. label_key/hint_key — i18n-ключі для UI."""
    id: str
    url: str                      # прямий resolve-URL GGUF на HuggingFace
    approx_size_bytes: int        # для consent-діалогу («буде завантажено ~N ГБ»)
    min_bytes: int                # захист від недокачаного/битого файлу
    sha256: "str | None"          # звірка після завантаження (None → лише розмір)
    label_key: str                # назва пресета в UI
    hint_key: str                 # підказка про залізо (RAM/VRAM)
    license_name: str = ""        # ліцензія, як вказано на сторінці моделі
    page_url: str = ""            # сторінка моделі на HuggingFace (клік у consent)


# Верифіковано на HuggingFace 17.07.2026, ПЕРЕВІРЕНО ЗАНОВО 22.07.2026 (HEAD
# X-Linked-Size + X-Linked-Etag): після липневого перезаливу Unsloth GGUF
# (оновлений офіційний chat template Gemma 4) розмір і sha256 обох файлів НЕ
# змінились — пінований SHA досі збігається з живим файлом, докачка й звірка
# коректні. Обидва репо публічні (без gate), Apache 2.0; repo-id чутливий до регістру!
PRESETS: "dict[str, ModelPreset]" = {
    "fast": ModelPreset(
        id="fast",
        url=("https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/"
             "resolve/main/gemma-4-E4B-it-Q4_K_M.gguf"),
        approx_size_bytes=4_977_171_584,
        min_bytes=4_900_000_000,
        sha256="85a896a047553e842f25297ee5b031d64ff30147d9c4af17b1e4b394cd1fab87",
        label_key="protocol_model_fast",
        hint_key="protocol_model_fast_hint",
        # сторінка моделі вказує ліцензію «apache-2.0» (звірено 18.07.2026)
        license_name="Apache 2.0",
        page_url="https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF",
    ),
    "quality": ModelPreset(
        id="quality",
        # 12B QAT UD-Q4_K_XL: QAT-складання (quantization-aware) відчутно якісніше за
        # звичайний 12B Q4 і водночас на ~0.8 ГБ менше (живий тест 24.07 на реальній
        # нараді). Регістр repo-id критичний (12B, qat) — інакше 404. SHA-256 і розмір
        # звірено з живим файлом ТА з HF (HEAD X-Linked-ETag/-Size, 24.07.2026).
        url=("https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF/"
             "resolve/main/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"),
        approx_size_bytes=6_716_356_800,
        min_bytes=6_600_000_000,
        sha256="90fd44e29e0d7cffeb0fd00dc73cfdab9ed0b0e95306ecf7821ea634c940c370",
        label_key="protocol_model_quality",
        hint_key="protocol_model_quality_hint",
        # УВАГА: QAT-модель — під ліцензією Gemma (general.license=gemma у метаданих
        # GGUF), НЕ Apache 2.0 (на відміну від звичайної E4B). Завантажується
        # компонентом на вимогу; ліцензію показуємо в consent-діалозі чесно.
        license_name="Gemma",
        page_url="https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF",
    ),
}

DEFAULT_PRESET = "fast"


class ModelDownloadError(RuntimeError):
    pass


def safe_preset_id(preset_id: str) -> str:
    """Валідний id пресета для побудови шляху (лише [a-z0-9_]). Невідомий →
    DEFAULT_PRESET: підкладене «..\\інша-тека» ніколи не стане шляхом."""
    pid = str(preset_id or "").strip().lower()
    if pid in PRESETS and _SAFE_ID_RE.match(pid):
        return pid
    return DEFAULT_PRESET


def get_preset(preset_id: str) -> ModelPreset:
    return PRESETS[safe_preset_id(preset_id)]


def _is_reparse_point(path: Path) -> bool:
    """True для symlink/junction (frozen exe не ходить по reparse-точках)."""
    try:
        info = path.lstat()
    except OSError:
        return True
    return (path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                      getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def model_file(model_dir) -> Path:
    return Path(model_dir) / MODEL_FILENAME


def _verify(path: Path, min_bytes: int, sha256: "str | None") -> bool:
    """Файл цілий: не reparse, ≥ min_bytes, і SHA збігається (якщо відомий).
    SHA-гілка вмикається ЛИШЕ під час встановлення (звірка щойно завантаженого
    файлу) — не при рутинній перевірці готовності."""
    if _is_reparse_point(path):
        return False
    try:
        if not path.is_file() or path.stat().st_size < min_bytes:
            return False
    except OSError:
        return False
    if not sha256:
        return True
    try:
        checksum = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                checksum.update(block)
        return checksum.hexdigest() == sha256
    except OSError:
        return False


def _available_dir(model_dir: Path, min_bytes: int) -> bool:
    """ШВИДКА перевірка готовності теки (БЕЗ хешування): є READY-маркер і файл
    цілий за розміром. SHA звіряється ОДИН раз при встановленні (наявність READY
    — свідоцтво тієї звірки); повторне хешування 3-8 ГБ при кожному рендері
    морозило б UI (урок судді — freeze на вкладці «Нарада»)."""
    model_dir = Path(model_dir)
    try:
        if not (model_dir / _READY_MARKER).is_file():
            return False
    except OSError:
        return False
    return _verify(model_file(model_dir), min_bytes, None)


def model_available(model_dir, preset_id: str) -> bool:
    """Модель пресета готова у теці: є маркер READY і файл проходить звірку."""
    return _available_dir(Path(model_dir), get_preset(preset_id).min_bytes)


def _download(url: str, destination: Path, progress_cb=None, cancel_check=None) -> None:
    netlog.record_url(url, kind=netlog.MODEL, detail="llm")
    try:
        with urllib.request.urlopen(url, timeout=30) as response, destination.open("wb") as out:
            total, received = int(response.headers.get("Content-Length") or 0), 0
            while True:
                if cancel_check and cancel_check():
                    raise InterruptedError()
                part = response.read(1024 * 256)
                if not part:
                    break
                out.write(part)
                received += len(part)
                if progress_cb:
                    progress_cb(received, total)
    except InterruptedError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise ModelDownloadError(f"Не вдалося завантажити модель: {exc}") from exc


def _install_from_url(model_dir, url: str, min_bytes: int, sha256: "str | None",
                      progress_cb=None, cancel_check=None, force: bool = False) -> None:
    """Докачати GGUF за URL у stage-теку, звірити розмір/SHA, атомарно активувати
    + покласти маркер READY. Ідемпотентно: наявну валідну модель не чіпає.
    Спільне ядро для пресетів (відомий SHA) і кастомних інтернет-моделей (SHA
    невідомий → лише розмір).

    force=True («Завантажити заново»): пропускаємо idempotent-ранній вихід і
    ЗАВЖДИ качаємо свіжий файл у stage. Старий target ЖИВИЙ увесь час докачки —
    його підмінюємо лише ПІСЛЯ успішної звірки нового (backup-гілка нижче), тож
    скасування чи збій verify лишають стару модель на місці."""
    target = Path(model_dir)
    if not force and _available_dir(target, min_bytes):
        return
    if target.exists() and _is_reparse_point(target):
        raise ModelDownloadError("Тека моделі не може бути symlink або reparse point")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="llm-", dir=target.parent))
    try:
        staged_file = stage / MODEL_FILENAME
        _download(url, staged_file, progress_cb, cancel_check)
        if _is_reparse_point(staged_file):
            raise ModelDownloadError("Завантажений файл виявився reparse point")
        if not _verify(staged_file, min_bytes, sha256):
            raise ModelDownloadError(
                "Розмір або контрольна сума моделі не збігаються — файл неповний")
        (stage / _READY_MARKER).write_text("ok", encoding="utf-8")

        # Windows не має portable atomic directory-exchange: обидва rename
        # атомарні в межах тому; за невдалого другого відновлюємо старий набір.
        backup = None
        if target.exists():
            backup = stage.parent / f".{target.name}.previous-{next(tempfile._get_candidate_names())}"
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if backup is not None and backup.exists():
                os.replace(backup, target)
            raise
        stage = None                     # успішно перенесено — finally не чистить
        if backup is not None:
            import shutil
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if stage is not None:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)


def download_and_install(model_dir, preset_id: str, progress_cb=None,
                         cancel_check=None, force: bool = False) -> None:
    """Докачати GGUF пресета, звірити (розмір+SHA), атомарно активувати.
    force=True — перекачати навіть наявну модель (staged, старий файл живий)."""
    preset = get_preset(preset_id)
    _install_from_url(model_dir, preset.url, preset.min_bytes, preset.sha256,
                      progress_cb, cancel_check, force=force)


def delete_model(model_dir) -> bool:
    """Видалити завантажену модель (звільнити ГБ). True — видалено."""
    import shutil
    model_dir = Path(model_dir)
    if not model_dir.exists():
        return False
    shutil.rmtree(model_dir, ignore_errors=True)
    return not model_dir.exists()


# --- Кастомні моделі (feature/llm-model-picker) ------------------------------
# Понад два пресети користувач може додати СВОЮ модель: (а) локальний GGUF-файл
# на диску, (б) модель із інтернету за ідентифікатором репозиторію (repo_id +
# ім'я файлу). Якість і сумісність чужих моделей ми не гарантуємо — про це чесно
# попереджаємо в UI. Кастомні моделі зберігаються в конфізі списком (JSON-рядки)
# і переживають перезапуск.

CUSTOM_KIND_LOCAL = "local"
CUSTOM_KIND_HF = "hf"
# Мінімальний розмір валідного GGUF: рятує від порожнього/битого файлу, але не
# вимагає точного розміру (він у чужих моделей невідомий наперед).
CUSTOM_MIN_BYTES = 1024
_ID_PREFIX = "custom_"
# Ідентифікатор репозиторію в інтернеті: «власник/назва» (як на HuggingFace).
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_gguf_name(name) -> bool:
    """Ім'я/шлях закінчується на .gguf (єдиний формат, який ми підтримуємо)."""
    return str(name or "").strip().lower().endswith(".gguf")


def is_repo_id(value) -> bool:
    """Валідний ідентифікатор репозиторію «власник/назва»."""
    return bool(_REPO_ID_RE.match(str(value or "").strip()))


def new_custom_id() -> str:
    """Унікальний БЕЗПЕЧНИЙ id кастомної моделі (лише [a-z0-9_] → не втече за
    межі теки моделей при побудові шляху)."""
    return _ID_PREFIX + secrets.token_hex(6)


@dataclass(frozen=True)
class CustomModel:
    """Додана користувачем модель. kind визначає джерело: local (файл на диску)
    або hf (репозиторій в інтернеті)."""
    id: str
    label: str
    kind: str
    path: str = ""                # local: абсолютний шлях до .gguf
    repo_id: str = ""             # hf: власник/назва репозиторію
    filename: str = ""            # hf: ім'я GGUF-файлу в репозиторії
    approx_size_bytes: int = 0    # для інформації в UI (0 → невідомо)

    def valid(self) -> bool:
        if not _SAFE_ID_RE.match(self.id) or self.id in PRESETS:
            return False
        if self.kind == CUSTOM_KIND_LOCAL:
            return bool(self.path) and is_gguf_name(self.path)
        if self.kind == CUSTOM_KIND_HF:
            return is_repo_id(self.repo_id) and is_gguf_name(self.filename)
        return False

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id, "label": self.label, "kind": self.kind,
            "path": self.path, "repo_id": self.repo_id,
            "filename": self.filename, "approx_size_bytes": self.approx_size_bytes,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw) -> "CustomModel | None":
        """Розпарсити запис із конфігу. Невалідний (битий JSON, небезпечний id,
        невідомий kind, не-.gguf) → None: у список потраплять лише валідні."""
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            size = int(data.get("approx_size_bytes", 0) or 0)
        except (ValueError, TypeError):
            size = 0
        cm = cls(
            id=str(data.get("id", "")).strip().lower(),
            label=str(data.get("label", "")).strip(),
            kind=str(data.get("kind", "")).strip().lower(),
            path=str(data.get("path", "")),
            repo_id=str(data.get("repo_id", "")).strip(),
            filename=str(data.get("filename", "")).strip(),
            approx_size_bytes=max(0, size),
        )
        if not cm.label:
            cm = cls(id=cm.id, label=cm.id, kind=cm.kind, path=cm.path,
                     repo_id=cm.repo_id, filename=cm.filename,
                     approx_size_bytes=cm.approx_size_bytes)
        return cm if cm.valid() else None


def custom_hf_url(cm: "CustomModel") -> str:
    """Прямий resolve-URL GGUF у репозиторії (гілка main)."""
    return f"https://huggingface.co/{cm.repo_id}/resolve/main/{cm.filename}"


def download_custom_hf(model_dir, cm: "CustomModel", progress_cb=None,
                       cancel_check=None, force: bool = False) -> None:
    """Завантажити GGUF кастомної інтернет-моделі у власну підтеку (як пресет, але
    без відомого розміру/SHA — лише мінімальна звірка цілості).
    force=True — перекачати навіть наявну модель (staged, старий файл живий)."""
    _install_from_url(model_dir, custom_hf_url(cm), CUSTOM_MIN_BYTES, None,
                      progress_cb, cancel_check, force=force)


def _safe_child(root: Path, target: Path) -> bool:
    """target фізично в межах root (захист від path-traversal у id).
    Спільна канонічна перевірка paths.safe_under (лінивий імпорт — без циклу)."""
    from .. import paths
    return paths.safe_under(root, target)


@dataclass(frozen=True)
class ResolvedModel:
    """Активна модель, зведена до конкретного файлу + перевірки готовності.
    Пресети/hf — качаються у теку моделей; local — уже лежить на диску."""
    id: str
    kind: str                     # "preset" | CUSTOM_KIND_LOCAL | CUSTOM_KIND_HF
    model_path: Path
    downloadable: bool
    approx_size_bytes: int = 0
    label: str = ""               # кастомні: людська назва
    label_key: str = ""           # пресети: i18n-ключ назви
    hint_key: str = ""            # пресети: i18n-ключ підказки про залізо
    _model_dir: "Path | None" = None
    _min_bytes: int = CUSTOM_MIN_BYTES

    def available(self) -> bool:
        if self.kind == CUSTOM_KIND_LOCAL:
            return _verify(self.model_path, self._min_bytes, None)
        if self._model_dir is None:
            return False
        return _available_dir(self._model_dir, self._min_bytes)


def resolve(active_id, model_root, custom_list=None) -> "ResolvedModel | None":
    """active_id (пресет АБО кастомний id) → ResolvedModel; невідомий → None.

    БЕЗ тихого фолбеку на інший пресет: невідомий id повертає None, і виклик
    підіймає чесну помилку (вимога Миколи — не підміняти модель нишком)."""
    aid = str(active_id or "").strip()
    root = Path(model_root)
    if aid in PRESETS:
        preset = PRESETS[aid]
        model_dir = root / aid
        if not _safe_child(root, model_dir):
            return None
        return ResolvedModel(
            id=aid, kind="preset", model_path=model_dir / MODEL_FILENAME,
            downloadable=True, approx_size_bytes=preset.approx_size_bytes,
            label_key=preset.label_key, hint_key=preset.hint_key,
            _model_dir=model_dir, _min_bytes=preset.min_bytes)
    for cm in custom_list or []:
        if cm.id != aid:
            continue
        if cm.kind == CUSTOM_KIND_LOCAL:
            return ResolvedModel(
                id=aid, kind=CUSTOM_KIND_LOCAL, model_path=Path(cm.path),
                downloadable=False, approx_size_bytes=cm.approx_size_bytes,
                label=cm.label)
        model_dir = root / aid
        if not _safe_child(root, model_dir):
            return None
        return ResolvedModel(
            id=aid, kind=CUSTOM_KIND_HF, model_path=model_dir / MODEL_FILENAME,
            downloadable=True, approx_size_bytes=cm.approx_size_bytes,
            label=cm.label, _model_dir=model_dir)
    return None
