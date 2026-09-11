"""Пакети моделей другого рушія розпізнавання (sherpa-onnx): дані, завантаження,
перевірка. feature/stt-sherpa-parakeet.

Той самий підхід, що й у моделей мовців (``meeting/diarization_models.py``):
файли тягнемо з НЕЗМІННОЇ ревізії HuggingFace (resolve-URL містить повний
коміт, ніколи ``main``), докачуємо перерване через HTTP Range у персистентний
``.part``, звіряємо РІВНО ті байти, що споживає рушій (точний розмір + SHA-256),
і активуємо теку атомарно з READY-маркером. Байти, що не пройшли звірку, не
активуються і не лишаються в кеші.

Межа модуля: мережа тут дозволена (єдиний виняток для цього рушія), Qt — ні,
sherpa-onnx не імпортується (він потрібен лише рушію ``stt_sherpa``).

Тека пакета — ``paths.components_dir()/stt/<id>``: поза інсталяцією, переживає
перевстановлення, як пунктуатор і моделі протоколу.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import netlog, paths

_HF = "https://huggingface.co"
READY_NAME = "READY.json"
READY_SCHEMA = 1
_CACHE_DIR_NAME = ".stt-download"
_CHUNK = 1024 * 256


class SherpaModelDownloadError(RuntimeError):
    """Завантаження або перевірка пакета моделі не вдалися."""


@dataclass(frozen=True)
class SherpaAsset:
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SherpaPackage:
    """Один пакет моделі для sherpa-onnx: звідки тягнути, що саме і чия ліцензія."""
    id: str                      # = ім'я пресета (whisper_core.stt_presets), безпечне для шляху
    repo_id: str                 # репозиторій HuggingFace з готовими ONNX-файлами
    revision: str                # повний коміт (незмінна ревізія)
    assets: tuple                # SherpaAsset у порядку завантаження
    license_name: str            # ліцензія ВАГ моделі (не конвертації)
    page_url: str                # сторінка оригінальної моделі (атрибуція)
    source_url: str = ""         # сторінка конвертованого пакета (джерело файлів)

    @property
    def total_bytes(self) -> int:
        return sum(a.size for a in self.assets)


# Звірено 06.09.2026 з HuggingFace API (lfs.oid для ONNX-файлів, X-Linked-Size) і
# локальним SHA-256 tokens.txt. Ваги: NVIDIA Parakeet-TDT-0.6B-v3, CC-BY-4.0
# (атрибуція обов'язкова). Файли: int8-конвертація спільноти sherpa-onnx.
PACKAGES: "dict[str, SherpaPackage]" = {
    "parakeet-tdt-0.6b-v3": SherpaPackage(
        id="parakeet-tdt-0.6b-v3",
        repo_id="csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        revision="2bda32ec70b097a55adaa07d9a7173915b43cc78",
        assets=(
            SherpaAsset("encoder.int8.onnx", 652184281,
                        "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247"),
            SherpaAsset("decoder.int8.onnx", 11845275,
                        "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e"),
            SherpaAsset("joiner.int8.onnx", 6355277,
                        "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3"),
            SherpaAsset("tokens.txt", 93939,
                        "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
        ),
        license_name="CC-BY-4.0",
        page_url="https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
        source_url="https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
    ),
}


def package_for(preset_name) -> "SherpaPackage | None":
    """Пакет за ім'ям пресета; None — не sherpa-пресет (Whisper або власна модель)."""
    return PACKAGES.get(str(preset_name or "").strip())


def asset_url(package: SherpaPackage, asset: SherpaAsset) -> str:
    """Прямий resolve-URL файла на закріпленому коміті (ніколи main)."""
    return f"{_HF}/{package.repo_id}/resolve/{package.revision}/{asset.filename}"


def stt_components_dir() -> Path:
    """База пакетів другого рушія: components/stt (поза інсталяцією)."""
    return paths.components_dir() / "stt"


def model_dir(preset_name, base=None) -> Path:
    """Тека пакета пресета. ``base`` — підміна кореня (тести, перенесення)."""
    package = package_for(preset_name)
    if package is None:
        raise ValueError(f"Не sherpa-пресет: {preset_name!r}")
    root = Path(base) if base else stt_components_dir()
    return root / package.id


def model_provenance() -> list:
    """Джерела, ліцензії й хеші для екрана згоди, нотаток і реліз-чеклиста."""
    return [{
        "id": p.id, "repo_url": p.page_url, "source_url": p.source_url,
        "license": p.license_name, "revision": p.revision,
        "files": [{"filename": a.filename, "size": a.size, "sha256": a.sha256}
                  for a in p.assets],
    } for p in PACKAGES.values()]


# --- перевірка на диску -------------------------------------------------------

def _is_reparse_point(path: Path) -> bool:
    """symlink/junction: заморожений exe не ходить за ними (WinError 448)."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _sha256_of(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _sized_regular_file(path: Path, size: int) -> bool:
    if _is_reparse_point(path):
        return False
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def models_present_fast(target_dir, package: SherpaPackage) -> bool:
    """Дешева проба для UI: реальні файли з точним розміром, БЕЗ SHA."""
    root = Path(target_dir)
    if _is_reparse_point(root):
        return False
    return all(_sized_regular_file(root / a.filename, a.size) for a in package.assets)


def models_available(target_dir, package: SherpaPackage) -> bool:
    """Повна звірка: кожен файл — реальний, точного розміру і з тим самим SHA-256."""
    root = Path(target_dir)
    if not models_present_fast(root, package):
        return False
    try:
        return all(_sha256_of(root / a.filename) == a.sha256 for a in package.assets)
    except OSError:
        return False


# --- завантаження -------------------------------------------------------------

def _part_valid_size(part: Path, expected: int) -> int:
    """Розмір валідного .part для докачки, або 0 (перезапуск). >expected → 0."""
    if not part.is_file() or _is_reparse_point(part):
        return 0
    try:
        size = part.stat().st_size
    except OSError:
        return 0
    if size > expected:
        part.unlink(missing_ok=True)      # структурно биті дані, стираємо
        return 0
    return size


def _download_asset(package: SherpaPackage, asset: SherpaAsset, part: Path, *,
                    received_before: int, progress_cb=None, cancel_check=None,
                    opener=None) -> None:
    """Докачати один файл у ``part`` з підтримкою HTTP Range (206).

    Валідний .part лишається при скасуванні або обриві; структурно битий або з
    хибним SHA — стирається. Прогрес агрегатний по пакету.
    """
    opener = opener or urllib.request.urlopen
    url = asset_url(package, asset)
    existing = _part_valid_size(part, asset.size)
    headers = {"User-Agent": "Balachky/stt"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    # доказова офлайновість: факт виходу в мережу — ДО першого запиту
    netlog.record_url(url, kind=netlog.MODEL, detail="stt")
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener(request, timeout=60) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            append = False
            if existing:
                content_range = response.headers.get("Content-Range", "")
                expected_cr = f"bytes {existing}-{asset.size - 1}/{asset.size}"
                if status == 206 and content_range == expected_cr:
                    append = True
                else:
                    existing = 0          # 200 або розбіжність — рестарт з нуля
            part.parent.mkdir(parents=True, exist_ok=True)
            received = existing
            with part.open("ab" if append else "wb") as out:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise InterruptedError()
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                    if received > asset.size:
                        raise SherpaModelDownloadError(
                            "Сервер віддав більше байтів, ніж очікувалось")
                    if progress_cb:
                        progress_cb(received_before + received, package.total_bytes)
    except InterruptedError:
        raise                                       # .part лишаємо для докачки
    except SherpaModelDownloadError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        # обрив мережі: .part валідний → лишаємо; далі перевіримо розмір/SHA
        raise SherpaModelDownloadError(
            f"Не вдалося завантажити модель розпізнавання: {exc}") from exc

    if part.stat().st_size != asset.size:
        raise SherpaModelDownloadError(
            f"Розмір {asset.filename} не збігається після завантаження")
    if _sha256_of(part) != asset.sha256:
        part.unlink(missing_ok=True)                # биті байти не тримаємо
        raise SherpaModelDownloadError(
            f"Контрольна сума {asset.filename} не збіглася")


def _write_ready(payload_dir: Path, package: SherpaPackage) -> None:
    ready = {
        "schema": READY_SCHEMA,
        "created": int(time.time()),
        "package": package.id,
        "repo_id": package.repo_id,
        "revision": package.revision,
        "license": package.license_name,
        "assets": [{"filename": a.filename, "size": a.size, "sha256": a.sha256}
                   for a in package.assets],
    }
    (payload_dir / READY_NAME).write_text(
        json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")


def download_and_install(target_dir, package: SherpaPackage, progress_cb=None,
                         cancel_check=None, opener=None) -> None:
    """Докачати, перевірити SHA і атомарно активувати пакет у ``target_dir``.

    Кеш докачки (``<batьки>/.stt-download/<sha>.part``) персистентний: перервана
    спроба докачується наступною. Активна тека підміняється атомарно з відкатом
    на попередню. Уже встановлений пакет — нічого не робить і не ходить у мережу.
    """
    target = Path(target_dir)
    if models_available(target, package):
        return
    if target.exists() and _is_reparse_point(target):
        raise SherpaModelDownloadError("Тека моделі не може бути symlink або reparse point")
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = target.parent / _CACHE_DIR_NAME
    if cache.exists() and _is_reparse_point(cache):
        raise SherpaModelDownloadError("Кеш докачки не може бути reparse point")
    cache.mkdir(parents=True, exist_ok=True)

    received_before = 0
    parts = {}
    for asset in package.assets:
        part = cache / f"{asset.sha256}.part"
        parts[asset.filename] = part
        _download_asset(package, asset, part, received_before=received_before,
                        progress_cb=progress_cb, cancel_check=cancel_check,
                        opener=opener)
        received_before += asset.size

    payload = Path(tempfile.mkdtemp(prefix="stt-", dir=target.parent))
    try:
        for asset in package.assets:
            dest = payload / asset.filename
            shutil.copy2(parts[asset.filename], dest)
            # байти мають лягти на диск ДО атомарної підміни теки — інакше
            # після збою живлення READY-маркер може пережити порожній encoder
            with dest.open("rb+") as handle:
                os.fsync(handle.fileno())
        if not models_available(payload, package):
            raise SherpaModelDownloadError("Контрольна сума або розмір моделі не збігаються")
        _write_ready(payload, package)
        # Windows не має атомарного обміну тек: два rename у межах тому, з відкатом.
        backup = None
        if target.exists():
            backup = target.parent / f".{target.name}.previous-{next(tempfile._get_candidate_names())}"
            os.replace(target, backup)
        try:
            os.replace(payload, target)
        except Exception:
            if backup is not None and backup.exists():
                os.replace(backup, target)
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(payload, ignore_errors=True)
    for part in parts.values():
        part.unlink(missing_ok=True)                # кеш потрібен лише між спробами


__all__ = [
    "SherpaAsset", "SherpaPackage", "SherpaModelDownloadError", "PACKAGES",
    "READY_NAME", "READY_SCHEMA", "package_for", "asset_url", "model_dir",
    "stt_components_dir", "model_provenance", "models_available",
    "models_present_fast", "download_and_install",
]
