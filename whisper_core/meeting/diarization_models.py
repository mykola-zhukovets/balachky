"""Завантаження моделей діаризації з IMMUTABLE Hugging Face ревізій.

Замість tar-архівів з GitHub Releases тягнемо ONNX-файли напряму з незмінних
ревізій HF (resolve-URL містить повний commit, ніколи ``main``). Це:
  • прибирає розпакування tar;
  • дає докачку (HTTP Range → 206) перерваного завантаження;
  • перевіряє РІВНО ті байти, що споживаються (точний розмір + SHA-256).

Межа модуля: мережа тут дозволена (єдиний виняток), Qt — ні. Кеш докачки
персистентний (``<target>/../.diarization-download/<sha>.part``), а не temp,
який зітре ``finally``; частковий файл переживає скасування/обрив мережі.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .diarize import (EMBEDDING_NAME, MODEL_MANIFEST, SEGMENTATION_RELATIVE,
                      _is_reparse_point, models_available)
from .. import netlog   # доказова офлайновість: журнал вихідних з'єднань

_HF = "https://huggingface.co"
_SEG_REV = "9403a6902bb58e3d5ae8c7e77c3422de279db2e0"
_EMB_REV = "8be2a75c9ed7a590538b268e46fbb65e1aa9d208"


@dataclass(frozen=True)
class ModelAsset:
    id: str
    url: str
    relative_path: Path          # шлях усередині теки моделей (= ключ MODEL_MANIFEST)
    size: int
    sha256: str
    repo_url: str
    license_name: str


ASSETS = (
    ModelAsset(
        id="segmentation",
        url=(f"{_HF}/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0/resolve/"
             f"{_SEG_REV}/model.onnx"),
        relative_path=SEGMENTATION_RELATIVE,
        size=MODEL_MANIFEST[SEGMENTATION_RELATIVE][0],
        sha256=MODEL_MANIFEST[SEGMENTATION_RELATIVE][1],
        repo_url="https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0",
        license_name="pyannote segmentation — MIT",
    ),
    ModelAsset(
        id="embedding",
        url=(f"{_HF}/csukuangfj/speaker-embedding-models/resolve/"
             f"{_EMB_REV}/{EMBEDDING_NAME}"),
        relative_path=Path(EMBEDDING_NAME),
        size=MODEL_MANIFEST[Path(EMBEDDING_NAME)][0],
        sha256=MODEL_MANIFEST[Path(EMBEDDING_NAME)][1],
        repo_url="https://huggingface.co/csukuangfj/speaker-embedding-models",
        license_name="3D-Speaker CampPlus — Apache-2.0",
    ),
)

TOTAL_DOWNLOAD_BYTES = sum(asset.size for asset in ASSETS)
READY_NAME = "READY.json"
READY_SCHEMA = 1


class DiarizationDownloadError(RuntimeError):
    pass


def model_provenance() -> list[dict]:
    """Публічні джерела/ліцензії/хеші для екрана згоди й реліз-чеклиста."""
    return [{
        "id": a.id, "repo_url": a.repo_url, "license": a.license_name,
        "size": a.size, "sha256": a.sha256,
    } for a in ASSETS]


def _sha256_of(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _part_valid_size(part: Path, expected: int) -> int:
    """Розмір валідного .part для докачки, або 0 (перезапуск). >expected → 0."""
    if not part.is_file() or _is_reparse_point(part):
        return 0
    try:
        size = part.stat().st_size
    except OSError:
        return 0
    if size > expected:
        # Частковий більший за очікуваний — структурно биті дані, стираємо.
        part.unlink(missing_ok=True)
        return 0
    return size


def _download_asset(asset: ModelAsset, part: Path, *, received_before: int,
                    progress_cb=None, cancel_check=None) -> None:
    """Докачати один asset у ``part`` з підтримкою HTTP Range (206).

    Зберігає валідний .part при скасуванні/обриві; стирає структурно биті/хибний
    SHA. Прогрес агрегатний: ``(received_before + поточне, TOTAL_DOWNLOAD_BYTES)``.
    """
    existing = _part_valid_size(part, asset.size)
    headers = {"User-Agent": "Balachky/diarization"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    netlog.record_url(asset.url, kind=netlog.MODEL, detail="voices")
    request = urllib.request.Request(asset.url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = getattr(response, "status", response.getcode())
            append = False
            if existing:
                content_range = response.headers.get("Content-Range", "")
                expected_cr = f"bytes {existing}-{asset.size - 1}/{asset.size}"
                if status == 206 and content_range == expected_cr:
                    append = True
                else:
                    existing = 0        # 200 або розбіжність — рестарт з нуля
            part.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if append else "wb"
            received = existing
            with part.open(mode) as out:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise InterruptedError()
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                    if received > asset.size:
                        raise DiarizationDownloadError(
                            "Сервер віддав більше байтів, ніж очікувалось")
                    if progress_cb:
                        progress_cb(received_before + received, TOTAL_DOWNLOAD_BYTES)
    except InterruptedError:
        raise                                   # .part лишаємо для докачки
    except DiarizationDownloadError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:
        # Обрив мережі: .part валідний → лишаємо; далі перевіримо розмір/SHA.
        raise DiarizationDownloadError(
            f"Не вдалося завантажити моделі мовців: {exc}") from exc

    if part.stat().st_size != asset.size:
        raise DiarizationDownloadError(
            f"Розмір {asset.id} не збігається після завантаження")
    if _sha256_of(part) != asset.sha256:
        part.unlink(missing_ok=True)            # биті байти не тримаємо
        raise DiarizationDownloadError(
            f"Контрольна сума {asset.id} не збіглася")


def _write_ready(payload_dir: Path) -> None:
    ready = {
        "schema": READY_SCHEMA,
        "created": int(time.time()),
        "assets": [{
            "relative": asset.relative_path.as_posix(),
            "size": asset.size, "sha256": asset.sha256,
        } for asset in ASSETS],
    }
    (payload_dir / READY_NAME).write_text(
        json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")


def download_and_install(target_dir, progress_cb=None, cancel_check=None) -> None:
    """Докачати, перевірити SHA і атомарно активувати весь набір моделей.

    Кеш докачки персистентний: перерваний .part докачується наступним запуском.
    Активна тека підміняється атомарно з відкатом на попередній набір.
    """
    target = Path(target_dir)
    if models_available(target):
        return
    if target.exists() and _is_reparse_point(target):
        raise DiarizationDownloadError("Тека моделей не може бути symlink або reparse point")
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = target.parent / ".diarization-download"
    if cache.exists() and _is_reparse_point(cache):
        raise DiarizationDownloadError("Кеш докачки не може бути reparse point")
    cache.mkdir(parents=True, exist_ok=True)

    received_before = 0
    part_paths = {}
    for asset in ASSETS:
        part = cache / f"{asset.sha256}.part"
        part_paths[asset.id] = part
        _download_asset(asset, part, received_before=received_before,
                        progress_cb=progress_cb, cancel_check=cancel_check)
        received_before += asset.size

    # Збираємо payload з правильним layout (= ключі MODEL_MANIFEST).
    payload = Path(tempfile.mkdtemp(prefix="diarization-", dir=target.parent))
    try:
        for asset in ASSETS:
            dest = payload / asset.relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(part_paths[asset.id], dest)
        if not models_available(payload):
            raise DiarizationDownloadError("Контрольна сума або розмір моделей не збігаються")
        _write_ready(payload)

        # Windows не має portable atomic directory-exchange. Обидва rename
        # атомарні в межах тому; за невдалого другого відновлюємо старий набір.
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
    # Успіх — прибираємо кеш докачки; він потрібен лише між спробами.
    for part in part_paths.values():
        part.unlink(missing_ok=True)


__all__ = [
    "ModelAsset", "ASSETS", "TOTAL_DOWNLOAD_BYTES", "DiarizationDownloadError",
    "download_and_install", "models_available", "model_provenance",
]
