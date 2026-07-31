"""Менеджер завантаження, перевірки та підготовки рушія синтезу мовлення (balachky-tts-engine).

Вимоги безпеки та надійності:
  • Перевірка Ed25519-підпису маніфесту (канонічний JSON, prefix, key_id);
  • Заборона завантаження за відсутності відкритого ключа;
  • Перевірка сумісності min_app_version та protocol_version (validate_manifest_compatibility);
  • Перевірка пікового вільного місця на диску (архів + розпакований вміст);
  • Відновлення перерваного завантаження через HTTP Range;
  • Перевірка SHA-256 контрольної суми архіву;
  • Захист від Zip Slip та виконання довільного коду (перевірка executable_relative_path та архіву через safe_under);
  • Перевірка роботи завантаженого рушія через `--selftest` (обов'язковий рядок `torch=present`);
  • Безпечне видалення з попередньою зупинкою процесу.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from packaging.version import InvalidVersion, Version

from whisper_core import netlog, paths
from whisper_core.paths import safe_under
from whisper_core.version import DISPLAY_VERSION

_log = logging.getLogger("balachky.tts.engine_manager")

SIGNING_PREFIX = b"Balachky tts engine manifest v1\x00"
UNSIGNED_FIELDS = ("signature", "signature_key_id")
CURRENT_APP_VERSION = DISPLAY_VERSION
EXPECTED_PROTOCOL_VERSION = 1
DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/mykola-zhukovets/balachky-tts-engine/main/engine_manifest.json"

#: Публічний ключ розробника (base64, 32 байти). Власник додасть його у випуск.
_PUBLIC_KEY_BASE64: str = ""


class EngineDownloadError(RuntimeError):
    """Базовий виняток помилок завантаження та встановлення рушія."""


class SignatureError(EngineDownloadError):
    """Помилка перевірки Ed25519 підпису маніфесту."""


class NoPublicKeyError(SignatureError):
    """Відкритий ключ підпису відсутній у програмі."""


class IncompatibleVersionError(EngineDownloadError):
    """Несумісність версій програми або протоколу."""


class InsufficientDiskSpaceError(EngineDownloadError):
    """Недостатньо вільного місця на диску."""

    def __init__(self, message: str, required_bytes: int = 0, available_bytes: int = 0):
        super().__init__(message)
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes


class ArchiveValidationError(EngineDownloadError):
    """Помилка валідації zip-архіву (SHA-256 або небеспечні шляхи)."""


class EngineSelfTestError(EngineDownloadError):
    """Рушій не пройшов перевірку --selftest (мертвий бінарник або fake torch)."""


def set_test_public_key(pub_key_b64: str | None) -> None:
    """Встановити відкритий ключ підпису (використовується в тестах)."""
    global _PUBLIC_KEY_BASE64
    _PUBLIC_KEY_BASE64 = pub_key_b64 or ""


def get_public_key() -> str:
    """Отримати поточний відкритий ключ підпису у base64."""
    return _PUBLIC_KEY_BASE64


def canonical_json(obj: dict) -> bytes:
    """Канонічний JSON для підпису: ASCII, ключі відсортовані, компактно, без NaN."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def signing_message(manifest: dict) -> bytes:
    """Формування підписуваного повідомлення з маніфесту."""
    body = {k: v for k, v in manifest.items() if k not in UNSIGNED_FIELDS}
    return SIGNING_PREFIX + canonical_json(body)


def compute_key_id(public_key_bytes: bytes) -> str:
    """Обчислення key_id від байтів відкритого ключа."""
    return "sha256:" + hashlib.sha256(public_key_bytes).hexdigest()


def verify_manifest_signature(manifest: dict) -> None:
    """Перевірити Ed25519 підпис маніфесту.

    Якщо ключа немає — завантаження НЕ дозволяється.
    """
    pub_key_b64 = get_public_key().strip()
    if not pub_key_b64:
        raise NoPublicKeyError("Перевірка підпису недоступна: відсутній відкритий ключ у програмі")

    sig_b64 = manifest.get("signature")
    if not sig_b64 or not isinstance(sig_b64, str):
        raise SignatureError("У маніфесті відсутній або недійсний підпис")

    try:
        public_bytes = base64.b64decode(pub_key_b64, validate=True)
        signature_bytes = base64.b64decode(sig_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise SignatureError(f"Некоректний base64 підпису чи ключа: {exc}") from exc

    if len(public_bytes) != 32:
        raise SignatureError(f"Відкритий ключ має бути 32 байти, отримано {len(public_bytes)}")
    if len(signature_bytes) != 64:
        raise SignatureError(f"Підпис має бути 64 байти, отримано {len(signature_bytes)}")

    expected_key_id = compute_key_id(public_bytes)
    manifest_key_id = manifest.get("signature_key_id")
    if manifest_key_id and manifest_key_id != expected_key_id:
        raise SignatureError(
            f"key_id маніфесту ({manifest_key_id}) не збігається з очікуваним ({expected_key_id})")

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(public_bytes)
        key.verify(signature_bytes, signing_message(manifest))
    except Exception as exc:
        raise SignatureError(f"Недійсний підпис маніфесту: {exc}") from exc


def parse_version_tuple(v_str: str) -> tuple[int, ...]:
    """Числовий release-сегмент версії, незалежно від суфікса каналу."""
    try:
        return Version(str(v_str).strip()).release
    except InvalidVersion:
        return (0, 0, 0)


def validate_manifest_compatibility(manifest: dict, app_version: str = CURRENT_APP_VERSION) -> None:
    """Перевірка сумісності маніфесту з програмою."""
    schema_ver = manifest.get("schema_version", 1)
    if schema_ver != 1:
        raise IncompatibleVersionError(f"Непідтримувана версія схеми маніфесту: {schema_ver}")

    proto_ver = manifest.get("protocol_version", 1)
    if proto_ver != EXPECTED_PROTOCOL_VERSION:
        raise IncompatibleVersionError(
            f"Версія протоколу рушія ({proto_ver}) не збігається з очікуваною ({EXPECTED_PROTOCOL_VERSION})")

    min_app_v = manifest.get("min_app_version")
    if min_app_v:
        if parse_version_tuple(app_version) < parse_version_tuple(min_app_v):
            raise IncompatibleVersionError(
                f"Для цього рушія потрібна новіша версія програми ({min_app_v}). Поточна версія: {app_version}")

    platform = manifest.get("platform")
    if platform and platform != "win-x64" and os.name == "nt":
        raise IncompatibleVersionError(f"Платформа рушія ({platform}) не сумісна з поточним ОС")


#: Розміри опублікованого пакета рушія в байтах — ЄДИНЕ джерело чисел для
#: підписів у програмі, поки маніфест не завантажено з мережі. При перевипуску
#: пакета правити тільки тут: жоден екран не має власних літералів розміру,
#: інакше інтерфейс мовчки збреше про вагу завантаження (урок 25.07).
PACKAGE_ARCHIVE_SIZE_BYTES = 336909393
PACKAGE_EXTRACTED_SIZE_BYTES = 895247902


def expected_engine_sizes() -> tuple[int, int]:
    """(вага завантаження, місце на диску) в байтах для підписів у налаштуваннях.

    Якщо рушій уже встановлено, числа беруться з його власного маніфесту — тоді
    підпис описує саме той пакет, що лежить на диску. Поки рушія немає, беруться
    розміри опублікованого пакета з констант вище.
    """
    manifest_path = paths.tts_engine_dir() / "engine_manifest.json"
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        archive = int(data["archive_size_bytes"])
        extracted = int(data["extracted_size_bytes"])
        if archive > 0 and extracted > 0:
            return archive, extracted
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        pass
    return PACKAGE_ARCHIVE_SIZE_BYTES, PACKAGE_EXTRACTED_SIZE_BYTES


def check_disk_space(extracted_size_bytes: int, archive_size_bytes: int = 0, target_dir: Path | None = None) -> None:
    """Перевірити, чи вистачає вільного місця на диску з урахуванням сумісного перебування архіву та розпакованого всього вмісту (БЛОКЕР 5)."""
    if target_dir is None:
        target_dir = paths.user_dir()
    peak_required = extracted_size_bytes + archive_size_bytes
    try:
        usage = shutil.disk_usage(target_dir)
        free_bytes = usage.free
    except OSError:
        return  # Якщо не вдалося зміряти — не блокуємо жорстко

    if free_bytes < peak_required:
        raise InsufficientDiskSpaceError(
            f"Недостатньо місця на диску: потрібно {peak_required} байтів (архів {archive_size_bytes} + розпаковано {extracted_size_bytes}), є {free_bytes}",
            required_bytes=peak_required,
            available_bytes=free_bytes)


def fetch_engine_manifest(url: str = DEFAULT_MANIFEST_URL) -> dict:
    """Завантажити маніфест рушія з мережі за URL (БЛОКЕР 4)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": f"BalachkyApp/{DISPLAY_VERSION}"})
    netlog.record_url(url, kind=netlog.MODEL, detail="tts-engine-manifest")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            manifest = json.loads(data.decode("utf-8"))
            return manifest
    except Exception as exc:
        raise EngineDownloadError(f"Не вдалося завантажити маніфест рушія з мережі: {exc}") from exc


def download_file(url: str, dest_path: Path, expected_size: int | None = None,
                  progress_cb=None) -> Path:
    """Завантажити файл за URL із підтримкою докачування (HTTP Range)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    downloaded_bytes = 0
    mode = "wb"
    headers = {}

    if dest_path.exists():
        downloaded_bytes = dest_path.stat().st_size
        if expected_size and downloaded_bytes == expected_size:
            if progress_cb:
                progress_cb(downloaded_bytes, expected_size)
            return dest_path
        if downloaded_bytes > 0:
            headers["Range"] = f"bytes={downloaded_bytes}-"
            mode = "ab"

    req = urllib.request.Request(url, headers=headers)
    netlog.record_url(url, kind=netlog.MODEL, detail="tts-engine-archive")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            if status == 206:
                # Сервер підтримує HTTP Range — продовжуємо запис
                pass
            else:
                # Range не підтримано або новий файл — перезапис з нуля
                mode = "wb"
                downloaded_bytes = 0

            content_length = resp.headers.get("Content-Length")
            total_bytes = expected_size or (
                (downloaded_bytes + int(content_length)) if content_length else 0)

            with open(dest_path, mode) as f:
                block_size = 64 * 1024
                while True:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded_bytes += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded_bytes, total_bytes)

    except Exception as exc:
        raise EngineDownloadError(f"Помилка завантаження файлу рушія: {exc}") from exc

    return dest_path


def verify_archive_hash(archive_path: Path, expected_sha256: str) -> None:
    """Перевірка SHA-256 контрольної суми завантаженого zip-архіву."""
    if not archive_path.exists():
        raise ArchiveValidationError(f"Файл архіву не існує: {archive_path}")

    h = hashlib.sha256()
    with open(archive_path, "rb") as f:
        while chunk := f.read(128 * 1024):
            h.update(chunk)
    digest = h.hexdigest().lower()
    expected = expected_sha256.strip().lower()

    if digest != expected:
        try:
            archive_path.unlink()
        except OSError:
            pass
        raise ArchiveValidationError(
            f"Невірна контрольна сума SHA-256 архіву: очікувалося {expected}, отримано {digest}")


def safe_extract_zip(archive_path: Path, stage_dir: Path) -> None:
    """Безпечне розпакування zip-архіву із захистом від Zip Slip та символьних посилань."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "r") as z:
        for member in z.infolist():
            name = member.filename
            # Відкидаємо абсолютні шляхи
            if name.startswith("/") or name.startswith("\\") or (len(name) > 1 and name[1] == ":"):
                raise ArchiveValidationError(f"Заборонений абсолютний шлях в архіві: {name}")

            parts = Path(name).parts
            if ".." in parts:
                raise ArchiveValidationError(f"Заборонений відносний перехід '..' в архіві: {name}")

            # Перевірка символьних посилань у unix permissions (0o120000 == S_IFLNK)
            if (member.external_attr >> 16) & 0o120000 == 0o120000:
                raise ArchiveValidationError(f"Символьні посилання в архіві заборонені: {name}")

            target_path = stage_dir / name
            if not safe_under(stage_dir, target_path):
                raise ArchiveValidationError(f"Шлях архіву виходить за межі теки розпакування: {name}")

        # Якщо всі файли безпечні — виконуємо розпакування
        z.extractall(stage_dir)


def validate_executable_path(base_dir: Path, relative_path_str: str) -> Path:
    """Валідація відносного шляху виконуваного файла з захистом від Path Traversal (БЛОКЕР 1)."""
    rel = str(relative_path_str or "").strip()
    if not rel:
        raise ArchiveValidationError("Відносний шлях виконуваного файла порожній")

    # Перевірка на диск-префікси, абсолютні шляхи та '..'
    if rel.startswith("/") or rel.startswith("\\") or (len(rel) > 1 and rel[1] == ":"):
        raise ArchiveValidationError(f"Шлях виконуваного файла не може бути абсолютним: {rel}")

    p = Path(rel)
    if ".." in p.parts:
        raise ArchiveValidationError(f"Шлях виконуваного файла містить заборонений перехід '..': {rel}")

    target = (base_dir / p).resolve()
    if not safe_under(base_dir, target):
        raise ArchiveValidationError(f"Шлях виконуваного файла виходить за межі цільової теки: {rel}")

    return target


def run_engine_selftest(exe_path: Path) -> None:
    """Перевірити роботи рушія через `balachky-tts-worker.exe --selftest`.

    БЛОКЕР 3: Вимагає ОБОВ'ЯЗКОВОЇ явної згадки `torch=present`.
    Відсутність `torch=present` або наявність `torch=absent`/`fake` — це відмова,
    навіть якщо `synth=ok` або returncode == 0.
    """
    if not exe_path.exists():
        raise EngineSelfTestError(f"Виконуваний файл рушія не знайдено: {exe_path}")

    try:
        proc = subprocess.run(
            [str(exe_path), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except Exception as exc:
        raise EngineSelfTestError(f"Не вдалося запустити --selftest рушія: {exc}") from exc

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    out_lower = output.lower()

    if proc.returncode != 0:
        raise EngineSelfTestError(f"Командний код виходу --selftest не нульовий ({proc.returncode}): {output.strip()}")

    if "torch=absent" in out_lower or "fake" in out_lower or "mock" in out_lower or "fake_engine" in out_lower:
        raise EngineSelfTestError(
            f"Рушій повернув незадовільний стан self-test (torch=absent чи підставний рушій): {output.strip()}")

    # СТРОГА перевірка БЛОКЕРА 3: synth=ok без torch=present НЕ проходить!
    if "torch=present" not in out_lower:
        raise EngineSelfTestError(
            f"Вивід --selftest не містить підтвердження наявності обчислювальної бібліотеки torch=present: {output.strip()}")


def install_engine_from_manifest(manifest: dict, archive_path: Path, progress_cb=None) -> Path:
    """Повний цикл встановлення завантаженого маніфесту та архіву.

    Виконує:
      1. БЛОКЕР 2: Перевірка сумісності (validate_manifest_compatibility)
      2. БЛОКЕР 5: Перевірка пікового місця (check_disk_space)
      3. БЛОКЕР 1: Перевірка executable_relative_path перед розпакуванням та запуском
      4. Атомарне переміщення у %LOCALAPPDATA%\\Balachky\\tts-engine
      5. БЛОКЕР 3: Перевірка --selftest з обов'язковим torch=present
    """
    # БЛОКЕР 2: Виклик перевірки сумісності в робочому шляху!
    validate_manifest_compatibility(manifest)

    extracted_size = manifest.get("extracted_size_bytes", 0)
    archive_size = manifest.get("archive_size_bytes", 0)
    # БЛОКЕР 5: Пікове місце (архів + розпаковане)
    check_disk_space(extracted_size, archive_size)

    exe_rel = manifest.get("executable_relative_path", "balachky-tts-worker.exe")

    target_dir = paths.tts_engine_dir()
    temp_stage = Path(tempfile.mkdtemp(prefix="tts-engine-stage-", dir=target_dir.parent))

    try:
        # БЛОКЕР 1: Валідація відносного шляху виконуваного файла ДО запуску чи переміщення
        stage_exe = validate_executable_path(temp_stage, exe_rel)

        safe_extract_zip(archive_path, temp_stage)

        if not stage_exe.exists():
            raise ArchiveValidationError(f"У розпакованому архіві відсутній виконуваний файл {exe_rel}")

        # Очистити наявний рушій перед атомарною підстановкою
        delete_engine()

        target_dir.parent.mkdir(parents=True, exist_ok=True)
        # Атомарне переміщення stage у target_dir
        shutil.move(str(temp_stage), str(target_dir))

        target_exe = validate_executable_path(target_dir, exe_rel)

        # БЛОКЕР 1 & 3: Перевірка через --selftest
        try:
            run_engine_selftest(target_exe)
        except EngineSelfTestError as exc:
            _log.error("Самоперевірка рушія не пройшла, видаляємо встановлені файли: %s", exc)
            delete_engine()
            raise

    finally:
        if temp_stage.exists():
            shutil.rmtree(temp_stage, ignore_errors=True)

    return target_dir


def delete_engine(sidecar_shutdown_fn=None) -> None:
    """Видалити рушій озвучення.

    Спершу вивантажує активний процес рушія (щоб Windows не залокував файли),
    потім видаляє теку.
    """
    if sidecar_shutdown_fn:
        try:
            sidecar_shutdown_fn()
        except Exception:
            pass

    try:
        from whisper_core.tts.sidecar import shutdown_all
        shutdown_all()
    except Exception:
        pass

    target_dir = paths.tts_engine_dir()
    if target_dir.exists():
        try:
            shutil.rmtree(target_dir, ignore_errors=True)
        except OSError as exc:
            _log.warning("Не вдалося видалити теку рушія повністю: %s", exc)
