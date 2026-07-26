"""Опційний CUDA-рантайм: докачка cuBLAS для прискорення на NVIDIA GPU.

Frozen-збірка публікується CPU-only (без важких CUDA-DLL у інсталяторі). Хто має
NVIDIA і хоче в кілька разів швидше, докачує РІВНО два DLL — cublas64_12.dll +
cublasLt64_12.dll (разом ~752 МБ) — з офіційного PyPI-wheel самої NVIDIA у
%LOCALAPPDATA%\\Balachky\\cuda. cudnn для ctranslate2 4.7.2 НЕ потрібен (згортки
whisper-енкодера йдуть через cuBLAS/GEMM) — валідовано наживо на RTX 4070:
int8_float16 ~1 c проти ~15 c на CPU (~14x), пік VRAM ~3.7 ГБ.

Жодного Qt: цим модулем користуються і ядро (engine), і desktop-фронт.
ПРИВАТНІСТЬ: у мережу ходить лише download_and_install — свідома дія користувача
(кнопка/крок майстра). Решта функцій суто локальні (перевірка файлів/пристрою).

Застереження: набір DLL валідний саме для ctranslate2 4.7.2. При апгрейді ct2
перевірити мінімальний набір повторно (можливо повернеться потреба в cudnn).
"""
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import netlog   # доказова офлайновість: журнал вихідних з'єднань

log = logging.getLogger(__name__)

# --- піни (supply-chain): точна версія wheel + sha256, звіряємо байти самі ---
# Джерело — офіційний PyPI-пакет NVIDIA. URL wheel беремо з PyPI JSON API на
# льоту (він може мінятись між дзеркалами), а цілісність гарантує саме sha256.
CUBLAS_WHEEL_VERSION = "12.8.4.1"
CUBLAS_WHEEL_SHA256 = (
    "47e9b82132fa8d2b4944e708049229601448aaad7e6f296f630f2d1a32de35af")
CUBLAS_WHEEL_MB = 541                    # ~розмір трафіку (для повідомлень UI)
_PYPI_JSON = "https://pypi.org/pypi/nvidia-cublas-cu12/{ver}/json"
_UA = "Balachky (+https://github.com/mykola-zhukovets/balachky)"

#: рівно ці DLL потрібні ctranslate2 4.7.2 на CUDA; порядок = порядок preload
#: (cublasLt залежить від уже завантаженого cublas)
RUNTIME_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")

# preload-хендли тримаємо весь процес: delay-load усередині ctranslate2.dll
# шукає DLL за іменем, і os.add_dll_directory на нього НЕ діє — рятує лише
# завантажена в пам'ять копія з абсолютним шляхом (+ тека в PATH).
_loaded_handles = []
_activated = False


class CudaDownloadError(Exception):
    """Докачка CUDA-рантайму не вдалась; повідомлення придатне для логу/показу."""


class CudaDownloadCancelled(Exception):
    """Користувач скасував докачку CUDA-рантайму (штатне переривання, не помилка)."""


def cuda_dir() -> Path:
    """Тека завантаженого CUDA-рантайму: %LOCALAPPDATA%\\Balachky\\cuda.

    Стабільне per-user місце в ОБОХ режимах (dev і frozen) — так само, як тека
    моделей (onboarding.default_model_dir). Важкі DLL не кладемо в репозиторій, і
    шлях переживає перевстановлення. НЕ paths.user_dir(): у dev він = корінь репо.
    Тека не створюється тут (лише шлях); mkdir — у download_and_install."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Balachky" / "cuda"


def gpu_present() -> bool:
    """Чи бачить ctranslate2 бодай один CUDA-пристрій. Безпечно навіть без
    cublas (перевіряє драйвер/пристрій, не delay-loaded бібліотеки)."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() >= 1
    except Exception:
        return False


def runtime_ready() -> bool:
    """Чи готовий докачаний рантайм: обидва DLL на місці І реально
    завантажуються. Лише Windows (єдина ціль frozen-збірки)."""
    if sys.platform != "win32":
        return False
    dlls = [cuda_dir() / name for name in RUNTIME_DLLS]
    if not all(p.is_file() for p in dlls):
        return False
    try:
        import ctypes
        for p in dlls:                       # cublas → cublasLt (порядок важливий)
            ctypes.WinDLL(str(p))
    except OSError:
        return False
    return True


def activate() -> bool:
    """Підготувати процес до device=cuda: додати cuda_dir у PATH і preload обох
    DLL з АБСОЛЮТНИМ шляхом, тримаючи хендли на рівні модуля весь процес.
    Ідемпотентно (повторний виклик — no-op). Має відпрацювати ДО створення
    рушія з device=cuda. Повертає True, якщо рантайм активовано/вже активний."""
    global _activated
    if _activated:
        return True
    if sys.platform != "win32":
        return False
    dlls = [cuda_dir() / name for name in RUNTIME_DLLS]
    if not all(p.is_file() for p in dlls):
        return False
    d = str(cuda_dir())
    if d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    try:
        import ctypes
        for p in dlls:
            _loaded_handles.append(ctypes.WinDLL(str(p)))
    except OSError:
        log.exception("Не вдалося активувати CUDA-рантайм (preload DLL)")
        return False
    _activated = True
    log.info("CUDA-рантайм активовано (%s)", d)
    return True


def _wheel_url() -> str:
    """URL win_amd64-wheel потрібної версії з PyPI JSON API (на льоту)."""
    req = urllib.request.Request(
        _PYPI_JSON.format(ver=CUBLAS_WHEEL_VERSION), headers={"User-Agent": _UA})
    netlog.record("pypi.org", kind=netlog.MODEL, allowed=True, detail="gpu-runtime")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except Exception as e:
        raise CudaDownloadError(
            "Не вдалося отримати посилання на файл прискорення з PyPI. "
            "Перевір інтернет і спробуй ще раз.") from e
    for f in data.get("urls", []):
        if f.get("filename", "").endswith("win_amd64.whl"):
            return f["url"]
    raise CudaDownloadError("PyPI не повернув потрібний файл прискорення "
                            "(win_amd64) для цієї версії.")


def _download_file(url, dest, progress_cb, cancel_check) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    netlog.record_url(url, kind=netlog.MODEL, detail="gpu-runtime")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as fh:
                while True:
                    if cancel_check():
                        raise CudaDownloadCancelled()
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
    except CudaDownloadCancelled:
        raise
    except CudaDownloadError:
        raise
    except Exception as e:
        raise CudaDownloadError(
            "Завантаження файлу прискорення перервалось. Перевір інтернет "
            "і спробуй ще раз.") from e


def _verify_sha256(path) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    got = h.hexdigest()
    if got != CUBLAS_WHEEL_SHA256:
        raise CudaDownloadError(
            "Контрольна сума завантаженого файлу не збіглася "
            f"(очікувалось {CUBLAS_WHEEL_SHA256[:12]}…, отримано {got[:12]}…). "
            "Файл пошкоджено — спробуй ще раз.")


def _extract_dlls(wheel_path, dest) -> None:
    """Розпакувати з wheel ЛИШЕ потрібні DLL (за іменем файлу, незалежно від
    внутрішньої теки) у dest. Пишемо строго dest/<ім'я DLL> — жодного path
    traversal із записів архіву."""
    wanted = set(RUNTIME_DLLS)
    found = set()
    with zipfile.ZipFile(wheel_path) as zf:
        for info in zf.infolist():
            base = os.path.basename(info.filename)
            if base in wanted and base not in found:
                with zf.open(info) as src, open(Path(dest) / base, "wb") as out:
                    shutil.copyfileobj(src, out)
                found.add(base)
    missing = wanted - found
    if missing:
        raise CudaDownloadError(
            "У завантаженому пакеті бракує потрібних файлів: "
            + ", ".join(sorted(missing)))


def download_and_install(progress_cb=None, cancel_check=None) -> None:
    """Докачати й встановити CUDA-рантайм (cuBLAS) у cuda_dir().

    Кроки: URL з PyPI JSON → тимчасовий файл (з прогресом і скасуванням) →
    звірка sha256 з піном → розпакувати лише два DLL → wheel видалити.
    progress_cb(done_bytes, total_bytes); cancel_check() -> bool (True = стоп).
    Помилки — CudaDownloadError (людське повідомлення); скасування —
    CudaDownloadCancelled."""
    cancel_check = cancel_check or (lambda: False)
    d = cuda_dir()
    d.mkdir(parents=True, exist_ok=True)
    url = _wheel_url()
    fd, tmp = tempfile.mkstemp(suffix=".whl", dir=str(d))
    os.close(fd)
    try:
        _download_file(url, tmp, progress_cb, cancel_check)
        if cancel_check():
            raise CudaDownloadCancelled()
        _verify_sha256(tmp)
        _extract_dlls(tmp, d)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    log.info("CUDA-рантайм встановлено у %s", d)
