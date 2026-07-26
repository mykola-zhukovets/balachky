"""Офлайн-пакет компонентів: збирання на машині з мережею й імпорт на машині без неї.

Створення пакета компонентів (моделі ASR, LLM, CUDA, голоси TTS, розрізнення
голосів, пунктуатор) для автономного перенесення на комп'ютери без інтернету
та встановлення їх з носія назад у теки даних програми.

Специфікація пакета:
- BALACHKY_COMPONENTS.marker (створюється ОСТАННІМ)
- manifest.json та manifest.sha256
- payload/... (реальні файли без символьних посилань)
- checksums/... (файли SHA-256 по компонентах)
- README-UK.txt

Імпорт вважає пакет НЕДОВІРЕНИМ джерелом: шлях кожного файла перевіряється на
вихід за межі призначення, символьні посилання й reparse-points відкидаються,
сума кожного файла звіряється вже ПІСЛЯ переносу в тимчасову теку, і лише
повністю перевірений компонент займає своє місце.

БЕЗ залежностей від PySide6 і від fronts/ (канон: ядро не знає про інтерфейс).
"""
from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import logging
import ntpath
import os
import re
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable, Any

import whisper_core.cuda_runtime as cuda_runtime
import whisper_core.meeting.diarization_models as diar_models
import whisper_core.models as stt_models
import whisper_core.paths as paths
import whisper_core.protocol.model_manager as protocol_mm

_log = logging.getLogger("balachky.offline_package")
import whisper_core.punctuator as punc
import whisper_core.tts.voices as tts_voices

if TYPE_CHECKING:
    from whisper_core.config import Config


PACKAGE_FORMAT_VERSION = 1
PRODUCT_NAME = "Balachky"
REQUIRED_APP_VERSION = "1.2.0"
FOUR_GIB = 4 * 1024 * 1024 * 1024
OFFLINE_IMPORT_BUTTON_TEXT_UK = "Встановити моделі з флешки…"


class OfflinePackageError(Exception):
    """Базовий клас помилок створення/читання офлайн-пакета."""


class OfflinePackageCancelled(Exception):
    """Операцію створення пакета скасовано користувачем."""


class InsufficientSpaceError(OfflinePackageError):
    """Недостатньо вільного місця на диску призначення."""


class FilesystemLimitError(OfflinePackageError):
    """Файлова система носія не приймає файл такого розміру (FAT32 і 4 ГБ)."""


class UnsafePackageError(OfflinePackageError):
    """Пакет намагається записати файл поза межами призначення (або через посилання)."""


class PackageFormatError(OfflinePackageError):
    """Формат пакета не читається цією версією програми."""


@dataclass
class ComponentExportInfo:
    id: str
    type: str                   # "asr_model", "llm", "cuda_runtime", "voice", "diarization", "punctuator"
    display_name: str
    size_bytes: int
    file_count: int
    source_dir: Path
    payload_rel_path: str
    checksum_file: str
    details: dict[str, Any]


def get_filesystem_type(target_dir: str | Path) -> str:
    """Визначення типу файлової системи для шляху (Windows: GetVolumeInformationW)."""
    p = Path(target_dir).resolve()
    if sys.platform == "win32":
        drive = p.anchor
        if not drive.endswith("\\"):
            drive += "\\"
        volume_name_buf = ctypes.create_unicode_buffer(1024)
        fs_name_buf = ctypes.create_unicode_buffer(1024)
        serial_num = ctypes.c_ulong()
        max_len = ctypes.c_ulong()
        flags = ctypes.c_ulong()
        try:
            res = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(drive),
                volume_name_buf,
                1024,
                ctypes.byref(serial_num),
                ctypes.byref(max_len),
                ctypes.byref(flags),
                fs_name_buf,
                1024,
            )
            if res:
                return fs_name_buf.value
        except Exception:
            pass
    return "NTFS"


def check_fat32_warning(target_dir: str | Path, components: list[ComponentExportInfo]) -> str | None:
    """Ключ попередження, якщо носій у форматі FAT (FAT32/FAT16) і є файл >= 4 ГБ; інакше None.

    Кличеться і з UI (щоб попередити ДО вибору компонентів), і з export_package
    (щоб не починати копіювання, яке фізично не завершиться)."""
    fs_type = get_filesystem_type(target_dir).upper()
    if "FAT" in fs_type and "EXFAT" not in fs_type:
        for comp in components:
            if comp.source_dir and comp.source_dir.exists():
                for fp in _iter_files(comp.source_dir):
                    try:
                        # stat() слідує за посиланням: важить сам файл кешу,
                        # а не 0 байтів посилання на нього.
                        if fp.stat().st_size >= FOUR_GIB:
                            return "offline_pkg_fat32_warning"
                    except OSError:
                        pass
    return None


def _iter_files(p: Path):
    """Усі файли теки рекурсивно (включно з символьними посиланнями кешу) або сам файл."""
    if p.is_file():
        yield p
        return
    for root, _, files in os.walk(p):
        for name in files:
            yield Path(root) / name


def _get_dir_file_count_and_size(p: Path) -> tuple[int, int]:
    """Повертає (кількість_файлів, сумарний_розмір_байт).

    Файли кешу моделей — це посилання на blobs, тому розмір беремо через
    ``stat()`` (слідує за посиланням), а не пропускаємо такі файли: інакше вага
    моделі рахувалася б як 0 і перевірка вільного місця нічого не важила."""
    if not p.exists():
        return 0, 0
    if p.is_file():
        return 1, p.stat().st_size
    count = 0
    size = 0
    for fp in _iter_files(p):
        try:
            fsize = fp.stat().st_size
        except OSError:
            continue
        count += 1
        size += fsize
    return count, size


def get_available_components(cfg: Config) -> list[ComponentExportInfo]:
    """Збір переліку завантажених компонентів, доступних для створення пакета."""
    result: list[ComponentExportInfo] = []

    # 1. STT Model
    stt_name = cfg.model_name or "large-v3-turbo"
    stt_repo = stt_models.repo_for(stt_name)
    stt_rev = stt_models.revision_for(stt_name)
    cache_dir = stt_models.resolve_cache_dir(cfg.model_dir)
    if stt_models.model_present(cache_dir, stt_repo, stt_rev):
        rev_to_use = stt_rev or stt_models.local_snapshot_revision(cache_dir, stt_repo)
        if rev_to_use:
            snap_dir = (Path(cache_dir) / ("models--" + stt_repo.replace("/", "--"))
                        / "snapshots" / rev_to_use)
            if snap_dir.exists():
                fc, sz = _get_dir_file_count_and_size(snap_dir)
                safe_repo = stt_repo.replace("/", "--")
                result.append(ComponentExportInfo(
                    id=f"asr_{safe_repo}",
                    type="asr_model",
                    display_name=f"Модель розпізнавання ({stt_name})",
                    size_bytes=sz,
                    file_count=fc,
                    source_dir=snap_dir,
                    payload_rel_path=f"payload/asr/{safe_repo}/snapshots/{rev_to_use}",
                    checksum_file=f"checksums/asr-{safe_repo}.sha256",
                    details={
                        "repo_id": stt_repo,
                        "revision": rev_to_use,
                        "model_name": stt_name,
                        "dereferenced": True,
                    }
                ))

    # 2. CUDA Runtime
    if cuda_runtime.runtime_ready():
        c_dir = cuda_runtime.cuda_dir()
        fc, sz = _get_dir_file_count_and_size(c_dir)
        result.append(ComponentExportInfo(
            id="cuda_runtime",
            type="cuda_runtime",
            display_name="CUDA Runtime (NVIDIA)",
            size_bytes=sz,
            file_count=fc,
            source_dir=c_dir,
            payload_rel_path="payload/cuda/runtime",
            checksum_file="checksums/cuda-runtime.sha256",
            details={
                "version": cuda_runtime.CUBLAS_WHEEL_VERSION,
                "dlls": list(cuda_runtime.RUNTIME_DLLS),
            }
        ))

    # 3. Protocol LLM (Gemma)
    proto_dir = paths.protocol_models_dir()
    proto_preset = cfg.protocol_model or "fast"
    if protocol_mm.model_available(proto_dir, proto_preset):
        preset_dir = paths.protocol_model_dir(proto_preset)
        if preset_dir.exists():
            fc, sz = _get_dir_file_count_and_size(preset_dir)
            result.append(ComponentExportInfo(
                id=f"llm_{proto_preset}",
                type="llm",
                display_name=f"Модель протоколу ({proto_preset})",
                size_bytes=sz,
                file_count=fc,
                source_dir=preset_dir,
                payload_rel_path=f"payload/llm/{proto_preset}",
                checksum_file=f"checksums/llm-{proto_preset}.sha256",
                details={
                    "preset": proto_preset,
                }
            ))

    # 4. Diarization
    diar_dir = paths.diarization_models_dir()
    if diar_models.models_available(cfg.diarization_model_dir) and diar_dir.exists():
        fc, sz = _get_dir_file_count_and_size(diar_dir)
        result.append(ComponentExportInfo(
            id="diarization",
            type="diarization",
            display_name="Модель діаризації (Pyannote)",
            size_bytes=sz,
            file_count=fc,
            source_dir=diar_dir,
            payload_rel_path="payload/diarization",
            checksum_file="checksums/diarization.sha256",
            details={}
        ))

    # 5. TTS Voices
    tts_dir = paths.tts_voices_dir()
    tts_voice = cfg.tts_voice_uk or "styletts2_ua"
    if tts_voices.voice_available(tts_voice) and tts_dir.exists():
        voice_sub = tts_dir / tts_voice
        target_src = voice_sub if voice_sub.exists() else tts_dir
        fc, sz = _get_dir_file_count_and_size(target_src)
        result.append(ComponentExportInfo(
            id=f"voice_{tts_voice}",
            type="voice",
            display_name=f"Голос озвучення ({tts_voice})",
            size_bytes=sz,
            file_count=fc,
            source_dir=target_src,
            payload_rel_path=f"payload/voices/{tts_voice}",
            checksum_file=f"checksums/voice-{tts_voice}.sha256",
            details={
                "voice_id": tts_voice,
            }
        ))

    # 6. Punctuator
    punc_dir = paths.punctuator_model_dir()
    if punc.model_available(punc_dir) and punc_dir.exists():
        fc, sz = _get_dir_file_count_and_size(punc_dir)
        result.append(ComponentExportInfo(
            id="punctuator",
            type="punctuator",
            display_name="Пунктуатор (pcs_47lang)",
            size_bytes=sz,
            file_count=fc,
            source_dir=punc_dir,
            payload_rel_path="payload/punctuator",
            checksum_file="checksums/punctuator.sha256",
            details={}
        ))

    return result


def compute_sha256(file_path: Path, cancel_check: Callable[[], bool] | None = None) -> str:
    """Обчислення SHA-256 для файла з перевіркою скасування."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            if cancel_check and cancel_check():
                raise OfflinePackageCancelled("offline_pkg_export_cancelled")
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def has_symlinks_or_reparse(dir_path: Path) -> bool:
    """Перевірка відсутності символьних посилань та reparse points у теці."""
    if not dir_path.exists():
        return False
    for root, _, files in os.walk(dir_path):
        for f in files:
            fp = Path(root) / f
            try:
                if os.path.islink(fp):
                    return True
            except OSError:
                return True
    return False


def _copy_file_with_progress(
    src: Path,
    dst: Path,
    copied_ref: list[int],
    total_bytes: int,
    progress_cb: Callable[[int, int, str], None] | None,
    cancel_check: Callable[[], bool] | None,
) -> None:
    """Копіювання файла по блоках з прогресом та скасуванням."""
    if cancel_check and cancel_check():
        raise OfflinePackageCancelled("offline_pkg_export_cancelled")

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as rf, open(dst, "wb") as wf:
        while True:
            if cancel_check and cancel_check():
                raise OfflinePackageCancelled("offline_pkg_export_cancelled")
            chunk = rf.read(512 * 1024)
            if not chunk:
                break
            wf.write(chunk)
            copied_ref[0] += len(chunk)
            if progress_cb:
                progress_cb(copied_ref[0], total_bytes, src.name)


def export_package(
    target_dir: str | Path,
    cfg: Config,
    selected_component_ids: list[str] | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """Створення автономного пакета компонентів.

    Кроки:
    1. Перевірка компонентів та вільного місця.
    2. Створення staging-теки `.balachky-export-<uuid>`.
    3. Копіювання компонентів з dereference для HuggingFace ASR.
    4. Перевірка відсутності symlinks/reparse points.
    5. Обчислення SHA-256 для кожного файла й запис компонентних checksums.
    6. Формування manifest.json та manifest.sha256.
    7. Запис README-UK.txt.
    8. Запис BALACHKY_COMPONENTS.marker (ОСТАННІМ).
    9. Перейменування staging у фінальну теку.
    """
    cancel_check = cancel_check or (lambda: False)
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    available = get_available_components(cfg)
    if selected_component_ids is not None:
        components = [c for c in available if c.id in selected_component_ids]
    else:
        components = available

    if not components:
        raise OfflinePackageError("Не вибрано жодного наявного компонента для експорту.")

    total_bytes = sum(c.size_bytes for c in components)
    free_bytes = shutil.disk_usage(target_dir).free
    if free_bytes < total_bytes:
        raise InsufficientSpaceError("offline_pkg_export_space_error")

    # FAT32 не приймає файл >= 4 ГБ. Це ПОПЕРЕДЖЕННЯ, не заборона (рішення власника
    # 25.07: "чотири гігабайта це зараз мало, нехай просто буде попередження").
    # Інтерфейс показує його в діалозі перед початком; людина сама вирішує, чи
    # продовжувати - частина складових може поїхати, а великі впадуть на копіюванні
    # з чесною системною помилкою.
    fat_warning = check_fat32_warning(target_dir, components)
    if fat_warning:
        _log.warning("Носій розмічено як FAT32: файли понад 4 ГБ не поїдуть (%s)", fat_warning)

    staging_dir = target_dir / f".balachky-export-{uuid.uuid4().hex[:8]}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    copied_bytes_ref = [0]

    try:
        exported_components = []
        total_file_count = 0

        for comp in components:
            if cancel_check():
                raise OfflinePackageCancelled("offline_pkg_export_cancelled")

            payload_dst = staging_dir / comp.payload_rel_path
            payload_dst.mkdir(parents=True, exist_ok=True)

            # Копіювання файлів
            if comp.source_dir.is_file():
                _copy_file_with_progress(
                    comp.source_dir,
                    payload_dst / comp.source_dir.name,
                    copied_bytes_ref,
                    total_bytes,
                    progress_cb,
                    cancel_check,
                )
            else:
                for root, _, files in os.walk(comp.source_dir):
                    for name in files:
                        if cancel_check():
                            raise OfflinePackageCancelled("offline_pkg_export_cancelled")
                        src_file = Path(root) / name
                        # Дереференс symlink-ів джерела під час читання
                        rel_sub = src_file.relative_to(comp.source_dir)
                        dst_file = payload_dst / rel_sub
                        
                        # Якщо джерело symlink, резолвимо його в реальний файл
                        real_src = src_file.resolve()
                        _copy_file_with_progress(
                            real_src,
                            dst_file,
                            copied_bytes_ref,
                            total_bytes,
                            progress_cb,
                            cancel_check,
                        )

            # Для ASR-моделей застосовуємо перевірку dereference
            if comp.type == "asr_model":
                stt_models.dereference_snapshot(
                    model_dir=str(payload_dst.parents[1]),
                    repo_id=comp.details["repo_id"],
                    revision=comp.details["revision"],
                )

            # Перевірка відсутності symlink у payload
            if has_symlinks_or_reparse(payload_dst):
                raise OfflinePackageError("offline_pkg_symlink_error")

            # Обчислення SHA-256 для кожного файла
            checksum_lines = []
            comp_files_count = 0
            for root, _, files in os.walk(payload_dst):
                for name in files:
                    if cancel_check():
                        raise OfflinePackageCancelled("offline_pkg_export_cancelled")
                    fp = Path(root) / name
                    rel_to_pkg = fp.relative_to(staging_dir).as_posix()
                    file_sha = compute_sha256(fp, cancel_check)
                    checksum_lines.append(f"{file_sha}  {rel_to_pkg}")
                    comp_files_count += 1

            total_file_count += comp_files_count

            # Запис компонента у checksums/
            cs_path = staging_dir / comp.checksum_file
            cs_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cs_path, "w", encoding="utf-8") as cs_file:
                cs_file.write("\n".join(checksum_lines) + "\n")

            cs_sha256 = compute_sha256(cs_path, cancel_check)

            comp_dict = {
                "id": comp.id,
                "type": comp.type,
                "display_name": comp.display_name,
                "size_bytes": comp.size_bytes,
                "file_count": comp_files_count,
                "payload_path": comp.payload_rel_path,
                "checksum_file": comp.checksum_file,
                "checksum_file_sha256": cs_sha256,
                "details": comp.details,
            }
            exported_components.append(comp_dict)

        # 6. Запис README-UK.txt
        readme_content = (
            "=====================================================\n"
            " ПАКЕТ КОМПОНЕНТІВ «БАЛАЧКИ У КОРОСТЕНІ» (ОФЛАЙН)\n"
            "=====================================================\n\n"
            "Цей пакет містить готові моделі та компоненти для роботи\n"
            "застосунку на комп'ютері без підключення до мережі Інтернет.\n\n"
            "Інструкція зі встановлення:\n"
            "1. Встановіть «Балачки у Коростені» на ізольований ПК.\n"
            "2. Підключіть цей носій (флешку).\n"
            "3. Відкрийте застосунок «Балачки».\n"
            "4. У Налаштуваннях у розділі моделей натисніть «" + OFFLINE_IMPORT_BUTTON_TEXT_UK + "».\n\n"
            "Увага: пакет містить лише службові компоненти та моделі ШІ.\n"
            "Жодних особистих даних, записів чи налаштувань у пакеті немає.\n"
        )
        readme_path = staging_dir / "README-UK.txt"
        with open(readme_path, "w", encoding="utf-8") as rf:
            rf.write(readme_content)

        readme_sha256 = compute_sha256(readme_path, cancel_check)

        # 7. Запис manifest.json
        created_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        manifest_data = {
            "package_format": PACKAGE_FORMAT_VERSION,
            "product": PRODUCT_NAME,
            "created_at": created_at_iso,
            "created_by_app_version": getattr(cfg, "app_version", "1.2.4"),
            "required_app_version": REQUIRED_APP_VERSION,
            "components": exported_components,
            "package_checksums": {
                "README-UK.txt": readme_sha256,
            },
            "total_size_bytes": total_bytes,
            "total_file_count": total_file_count,
        }
        manifest_path = staging_dir / "manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest_data, mf, ensure_ascii=False, indent=2)

        manifest_sha = compute_sha256(manifest_path, cancel_check)
        manifest_sha_path = staging_dir / "manifest.sha256"
        with open(manifest_sha_path, "w", encoding="utf-8") as msf:
            msf.write(f"{manifest_sha}  manifest.json\n")

        # 8. Запис BALACHKY_COMPONENTS.marker (ОСТАННІМ)
        marker_data = {
            "product": PRODUCT_NAME,
            "package_format": PACKAGE_FORMAT_VERSION,
            "manifest": "manifest.json",
            "manifest_sha256": manifest_sha,
            "created_at": created_at_iso,
        }
        marker_path = staging_dir / "BALACHKY_COMPONENTS.marker"
        with open(marker_path, "w", encoding="utf-8") as mpf:
            json.dump(marker_data, mpf, ensure_ascii=False, indent=2)

        # 9. Перейменування staging у фінальну теку
        pkg_folder_name = f"Balachky-components-{datetime.datetime.now().strftime('%Y-%m-%d-%H%M%S')}"
        final_pkg_dir = target_dir / pkg_folder_name
        staging_dir.rename(final_pkg_dir)

        return final_pkg_dir

    except Exception:
        # Очищення staging-теки при скасуванні або помилці
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise


# ======================================================================
# Етап 2: імпорт пакета на комп'ютері без мережі
# ======================================================================

#: Формати пакета, які ця версія вміє прочитати (пакет із чужим номером — відмова).
SUPPORTED_PACKAGE_FORMATS = frozenset({1})
MARKER_NAME = "BALACHKY_COMPONENTS.marker"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class ImportResult:
    """Підсумок імпорту: що встановлено, що відкинуто й чому."""
    installed: list[str] = field(default_factory=list)          # id компонентів
    installed_names: list[str] = field(default_factory=list)    # показові назви
    bad_files: list[str] = field(default_factory=list)          # шляхи, що не пройшли суму
    skipped: list[str] = field(default_factory=list)            # id, які ця версія не вміє
    total_bytes: int = 0


def _is_link_or_reparse(p: Path) -> bool:
    """Символьне посилання, junction або будь-який reparse-point (Windows)."""
    try:
        st = p.lstat()
    except OSError:
        return True                      # не читається — вважаємо небезпечним
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_member(base: Path, rel: str) -> Path:
    """Шлях ``rel`` із пакета, приведений до ``base``, або відмова.

    Відкидається все, чим зловмисно зібраний пакет може вилізти за межі
    призначення: порожні й службові сегменти, "..", абсолютні шляхи (у т.ч.
    з літерою диска та UNC-виглядом), нульовий байт."""
    raw = str(rel or "")
    if not raw.strip() or "\x00" in raw:
        raise UnsafePackageError("offline_pkg_import_unsafe_path")
    norm = raw.replace("\\", "/")
    if norm.startswith("/") or ntpath.isabs(raw) or ":" in norm:
        raise UnsafePackageError("offline_pkg_import_unsafe_path")
    # сегменти беремо з СИРОГО рядка: PurePosixPath сам ковтає "." і подвійні
    # слеші, а нам треба відкинути такі імена, а не тихо їх виправити
    for part in norm.split("/"):
        if part in ("", ".", "..") or part.strip() != part:
            raise UnsafePackageError("offline_pkg_import_unsafe_path")
    pure = PurePosixPath(norm)
    if pure.is_absolute() or not pure.parts:
        raise UnsafePackageError("offline_pkg_import_unsafe_path")
    target = base / pure
    if not paths.safe_under(base, target):
        raise UnsafePackageError("offline_pkg_import_unsafe_path")
    return target


def _check_no_links(base: Path, target: Path) -> None:
    """Ні сам файл, ні жодна проміжна папка не є посиланням чи reparse-point."""
    node = Path(target)
    base = Path(base)
    while True:
        if node.exists() or node.is_symlink():
            if _is_link_or_reparse(node):
                raise UnsafePackageError("offline_pkg_import_unsafe_path")
        if node == base or node.parent == node:
            break
        node = node.parent


def read_manifest(src_dir: str | Path) -> dict:
    """Опис вмісту пакета: позначка на місці, JSON цілий, сума збігається, формат наш."""
    src = Path(src_dir)
    marker_path = src / MARKER_NAME
    if not marker_path.is_file() or _is_link_or_reparse(marker_path):
        raise OfflinePackageError("offline_pkg_import_not_a_package")

    manifest_path = src / "manifest.json"
    if not manifest_path.is_file() or _is_link_or_reparse(manifest_path):
        raise OfflinePackageError("offline_pkg_import_no_manifest")

    sha_path = src / "manifest.sha256"
    if not sha_path.is_file() or _is_link_or_reparse(sha_path):
        raise OfflinePackageError("offline_pkg_import_manifest_broken")
    try:
        expected_sha = sha_path.read_text("utf-8").split()[0].strip().lower()
    except (OSError, IndexError, UnicodeDecodeError):
        raise OfflinePackageError("offline_pkg_import_manifest_broken")
    if not _SHA256_RE.match(expected_sha) or compute_sha256(manifest_path) != expected_sha:
        raise OfflinePackageError("offline_pkg_import_manifest_broken")

    try:
        marker_data = json.loads(marker_path.read_text("utf-8"))
        if marker_data.get("manifest_sha256") and marker_data.get("manifest_sha256") != expected_sha:
            raise OfflinePackageError("offline_pkg_import_manifest_broken")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise OfflinePackageError("offline_pkg_import_manifest_broken")

    try:
        data = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise OfflinePackageError("offline_pkg_import_manifest_broken")
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        raise OfflinePackageError("offline_pkg_import_manifest_broken")
    if data.get("product") != PRODUCT_NAME:
        raise OfflinePackageError("offline_pkg_import_not_a_package")
    if data.get("package_format") not in SUPPORTED_PACKAGE_FORMATS:
        raise PackageFormatError("offline_pkg_import_format_unsupported")
    return data


def _safe_segment(value) -> str | None:
    """Один сегмент імені з опису пакета, придатний для шляху; інакше None."""
    seg = str(value or "").strip()
    return seg if _SAFE_SEGMENT_RE.match(seg) else None


def import_destination(comp: dict, cfg: Config):
    """Куди лягає компонент на цій машині; None — ця версія його не встановлює."""
    ctype = comp.get("type")
    details = comp.get("details") or {}

    if ctype == "asr_model":
        repo = str(details.get("repo_id") or "")
        revision = _safe_segment(details.get("revision"))
        repo_parts = repo.split("/")
        if not revision or len(repo_parts) != 2 or any(_safe_segment(p) is None for p in repo_parts):
            return None
        cache_root = Path(stt_models.resolve_cache_dir(cfg.model_dir))
        dest = cache_root / ("models--" + repo.replace("/", "--")) / "snapshots" / revision
        return dest if paths.safe_under(cache_root, dest) else None

    if ctype == "cuda_runtime":
        return cuda_runtime.cuda_dir()

    if ctype == "llm":
        preset = _safe_segment(details.get("preset"))
        if not preset or preset not in protocol_mm.PRESETS:
            return None
        return paths.protocol_model_dir(preset)

    if ctype == "diarization":
        return paths.diarization_models_dir()

    if ctype == "voice":
        voice_id = _safe_segment(details.get("voice_id"))
        if not voice_id:
            return None
        return paths.tts_voices_dir() / voice_id

    if ctype == "punctuator":
        return paths.punctuator_model_dir()

    return None


def _read_checksums(src: Path, comp: dict) -> dict:
    """{шлях_у_пакеті: sha256} з файла сум компонента. Порожній чи битий — відмова."""
    cs_path = _safe_member(src, comp.get("checksum_file") or "")
    _check_no_links(src, cs_path)
    if not cs_path.is_file():
        raise OfflinePackageError("offline_pkg_import_manifest_broken")

    expected_cs_sha = comp.get("checksum_file_sha256")
    if expected_cs_sha and compute_sha256(cs_path) != expected_cs_sha.lower():
        raise OfflinePackageError("offline_pkg_import_manifest_broken")

    try:
        lines = cs_path.read_text("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise OfflinePackageError("offline_pkg_import_manifest_broken")

    sums: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not _SHA256_RE.match(parts[0].lower()):
            raise OfflinePackageError("offline_pkg_import_manifest_broken")
        rel_path = parts[1].strip()
        try:
            _safe_member(src, rel_path)
        except UnsafePackageError:
            raise UnsafePackageError("offline_pkg_import_unsafe_path")
        sums[rel_path.replace("\\", "/")] = parts[0].lower()
    if not sums:
        raise OfflinePackageError("offline_pkg_import_manifest_broken")
    return sums


def _swap_into_place(staging: Path, dest: Path) -> None:
    """Поставити готову папку на місце; попередню тримаємо, поки не вийшло."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if dest.exists():
        backup = dest.with_name(f"{dest.name}.old-{uuid.uuid4().hex[:8]}")
        dest.rename(backup)
    try:
        staging.rename(dest)
    except OSError:
        if backup is not None:
            backup.rename(dest)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def import_package(
    src_dir: str | Path,
    cfg: Config,
    progress: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ImportResult:
    """Встановити моделі з носія в папки даних програми.

    Порядок для кожного компонента: перевірити шляхи всередині пакета →
    перенести файли в тимчасову папку ПОРУЧ із призначенням → звірити суму
    кожного перенесеного файла → лише тоді поставити папку на місце.
    Обірваний імпорт (висмикнули носій, скінчилось місце) прибирає тимчасову
    папку й лишає попередній стан недоторканим. Файли з невідповідною сумою
    не встановлюються, їхні шляхи повертаються в ``ImportResult.bad_files``.
    """
    cancel_check = cancel_check or (lambda: False)
    if cancel_check():
        raise OfflinePackageCancelled("offline_pkg_import_cancelled")

    src = Path(src_dir).resolve()
    manifest = read_manifest(src)
    result = ImportResult()

    components = [c for c in manifest["components"] if isinstance(c, dict)]
    total_bytes = sum(int(c.get("size_bytes") or 0) for c in components)
    done_bytes_ref = [0]

    # 1. Перевірка вільного місця та FAT32-обмежень на всіх дисках призначення ДО копіювання
    required_space_per_dest: dict[Path, int] = {}
    for comp in components:
        dest = import_destination(comp, cfg)
        payload_rel = comp.get("payload_path") or ""
        if dest is not None and payload_rel:
            dest_parent = Path(dest).parent
            need_bytes = int(comp.get("size_bytes") or 0)
            required_space_per_dest[dest_parent] = required_space_per_dest.get(dest_parent, 0) + need_bytes

            # Те саме для цільової теки: попередження, не заборона. Тека даних на
            # FAT32-носії - рідкість, а якщо великий файл справді не влізе,
            # копіювання впаде з чесною системною помилкою, а не з нашою забороною.
            fs_type = get_filesystem_type(dest_parent).upper()
            if "FAT" in fs_type and "EXFAT" not in fs_type:
                payload_dir = _safe_member(src, payload_rel)
                if payload_dir.is_dir():
                    for fp in _iter_files(payload_dir):
                        if fp.stat().st_size >= FOUR_GIB:
                            _log.warning("Цільову теку розмічено як FAT32: %s важить понад 4 ГБ", fp.name)
                            break

    for dest_parent, req_bytes in required_space_per_dest.items():
        dest_parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(dest_parent).free < req_bytes:
            raise InsufficientSpaceError("offline_pkg_import_space_error")

    # 2. Звіряємо контрольні суми службових/пакетних файлів (наприклад, README-UK.txt)
    pkg_checksums = manifest.get("package_checksums") or {}
    for rel_pkg_file, expected_pkg_sha in pkg_checksums.items():
        pkg_file_path = _safe_member(src, rel_pkg_file)
        _check_no_links(src, pkg_file_path)
        if not pkg_file_path.is_file() or compute_sha256(pkg_file_path, cancel_check) != expected_pkg_sha.lower():
            raise OfflinePackageError("offline_pkg_import_manifest_broken")

    # 3. Збір переліку відомих файлів для виявлення зайвих файлів у пакеті
    known_all_files: set[str] = {
        MARKER_NAME,
        "manifest.json",
        "manifest.sha256",
    }
    for rel_f in pkg_checksums:
        known_all_files.add(rel_f.replace("\\", "/"))
    for comp in components:
        if comp.get("checksum_file"):
            known_all_files.add(comp["checksum_file"].replace("\\", "/"))

    for comp in components:
        if cancel_check():
            raise OfflinePackageCancelled("offline_pkg_import_cancelled")

        comp_id = str(comp.get("id") or "?")
        dest = import_destination(comp, cfg)
        payload_rel = comp.get("payload_path") or ""
        if dest is None or not payload_rel:
            result.skipped.append(comp_id)
            continue

        payload_dir = _safe_member(src, payload_rel)
        _check_no_links(src, payload_dir)
        if not payload_dir.is_dir():
            result.skipped.append(comp_id)
            continue

        sums = _read_checksums(src, comp)
        for rel_f in sums:
            known_all_files.add(rel_f.replace("\\", "/"))

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        need_bytes = int(comp.get("size_bytes") or 0)
        staging = dest.parent / f".balachky-import-{uuid.uuid4().hex[:8]}"
        bad: list[str] = []
        seen: set[str] = set()
        try:
            staging.mkdir(parents=True)
            copied = 0
            for src_file in _iter_files(payload_dir):
                if cancel_check():
                    raise OfflinePackageCancelled("offline_pkg_import_cancelled")

                rel_in_pkg = src_file.relative_to(src).as_posix()
                rel_in_comp = src_file.relative_to(payload_dir).as_posix()
                dst_file = _safe_member(staging, rel_in_comp)
                _check_no_links(payload_dir, src_file)
                seen.add(rel_in_pkg)

                expected = sums.get(rel_in_pkg)
                if expected is None:
                    bad.append(rel_in_pkg)          # файла немає в переліку сум
                    continue

                _copy_file_with_progress(
                    src_file,
                    dst_file,
                    done_bytes_ref,
                    total_bytes,
                    progress,
                    cancel_check,
                )

                # сума рахується з КОПІЇ: ловить і підміну, і обрив копіювання
                if compute_sha256(dst_file, cancel_check) != expected:
                    bad.append(rel_in_pkg)
                    continue
                copied += 1

            bad.extend(sorted(set(sums) - seen))     # обіцяний, але відсутній файл

            if bad or copied == 0:
                result.bad_files.extend(sorted(set(bad)))
                shutil.rmtree(staging, ignore_errors=True)
                continue

            _swap_into_place(staging, dest)
            result.installed.append(comp_id)
            result.installed_names.append(str(comp.get("display_name") or comp_id))
            result.total_bytes += need_bytes
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    # Перевірка на зайві невідомі файли у корені та не-payload теках пакета
    payload_dirs = set()
    for comp in components:
        prel = comp.get("payload_path")
        if prel:
            try:
                payload_dirs.add(_safe_member(src, prel))
            except UnsafePackageError:
                pass

    for fp in _iter_files(src):
        if any(paths.safe_under(pdir, fp) for pdir in payload_dirs):
            continue
        rel_fp = fp.relative_to(src).as_posix()
        if rel_fp not in known_all_files:
            raise OfflinePackageError("offline_pkg_import_manifest_broken")

    return result
