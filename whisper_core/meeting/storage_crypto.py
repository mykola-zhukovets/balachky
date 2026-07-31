"""Криптографія локального сховища нарад (без Qt і без мережі).

DEK генерується один раз на сховище. Малий ``.vaultkey`` містить лише його
обгортку: типово DPAPI поточного Windows-користувача; парольний режим замінює
обгортку, а не перешифровує аудіо. Дані мають власний STREAM-формат: AES-256-GCM
для кожного 64 KiB чанка, nonce = counter(11 байт) || final(1 байт).

Кожен контейнер отримує 16-байтний випадковий ``file_id`` з ``os.urandom``.
Під одним DEK формат розрахований не більш як на 2**32 файлів, а один файл —
не більш як на 2**32 чанків (256 TiB). Межа чанків контролюється під час читання
і запису. Межа файлів документована, але не має runtime-лічильника: надійний
лічильник вимагав би постійного реєстру або зміни публічного API.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

CHUNK_SIZE = 64 * 1024
KEY_FILE = ".vaultkey"
_KEY_LOCK_FILE = ".vaultkey.lock"
_FORMAT_VERSION = 1                 # legacy: file_id + chunk AAD only
_CONTEXT_FORMAT_VERSION = 2         # binds a container to its session/path
_MAGIC = b"BME"
_FILE_ID_BYTES = 16
_MAX_FILES_PER_DEK = 2**32
_MAX_CHUNKS_PER_FILE = 2**32
_MAX_PLAINTEXT_BYTES_PER_FILE = _MAX_CHUNKS_PER_FILE * CHUNK_SIZE
_TAG_BYTES = 16
_DEK_BYTES = 32
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_PASSWORD_CACHE: dict[str, bytes] = {}
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_LEGACY_SCRYPT_N = 2**15
_SCRYPT_MAXMEM = 256 * 1024 * 1024
# Код відновлення: base32 без неоднозначних символів (без 0/1/I/O).
_RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_RECOVERY_GROUP = 4
_RECOVERY_SYMBOLS = 28              # 28×5 = 140 біт ентропії (> 128)
_VAULT_KEY_AAD = b"Balachky vault key"
_KEYFILE_BYTES = 64                # файл-ключ: 64 випадкові байти на знімному носії
_KEYFILE_MODES = ("keyfile", "password+keyfile")
_VAULT_MODES = ("dpapi", "password", "keyfile", "password+keyfile")
_VAULT_READ_ATTEMPTS = 20
_VAULT_READ_RETRY_SECONDS = 0.05
_VAULT_CREATION_TIMEOUT_SECONDS = 60
_WINDOWS_SHARING_VIOLATIONS = {5, 32}


class CryptoUnavailable(RuntimeError):
    """cryptography не встановлена: незашифрований застосунок лишається робочим."""


class VaultPasswordRequired(RuntimeError):
    """Сховище переведено у парольний режим, але пароль не надано."""

class VaultWrongPassword(RuntimeError):
    """Пароль не підійшов до сховища (AEAD-обгортка DEK не автентифікується)."""

class VaultWrongRecovery(RuntimeError):
    """Код відновлення не підійшов до сховища (обгортка DEK не автентифікується)."""

class VaultWrongKeyfile(RuntimeError):
    """Файл-ключ (або пароль у двофакторному режимі) не підійшов до сховища."""

class VaultKeyLost(RuntimeError):
    """Encrypted artifacts exist but their vault key is missing."""

    def __init__(self):
        super().__init__(
            "Ключ сховища втрачено; відновіть оригінальний .vaultkey з резервної копії.")


def _aesgcm_class():
    """Лінивий імпорт: залежність потрібна лише після opt-in шифрування."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise CryptoUnavailable(
            "Для шифрування нарад встановіть залежність cryptography.") from exc
    return AESGCM

def _derive_file_key(dek: bytes, file_id: bytes) -> bytes:
    """Derive the independent AES key used by one encrypted file."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise CryptoUnavailable("cryptography is required for meeting encryption.") from exc
    return HKDF(algorithm=hashes.SHA256(), length=_DEK_BYTES, salt=file_id,
                info=b"balachky-meeting-file-v1").derive(dek)


def _password_bytes(password) -> bytes:
    if isinstance(password, str):
        return password.encode("utf-8")
    if isinstance(password, bytes):
        return password
    raise TypeError("password має бути str або bytes")


def _derive_kek(password, salt: bytes, n: int = _SCRYPT_N,
                r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> bytes:
    return hashlib.scrypt(_password_bytes(password), salt=salt,
                          n=n, r=r, p=p, dklen=_DEK_BYTES,
                          maxmem=_SCRYPT_MAXMEM)


def _new_recovery_code() -> str:
    """Одноразовий людський код: групи по 4 символи base32 без 0/1/I/O.

    Молодші 5 біт випадкового байта рівномірні над 32-символьним алфавітом
    (256 = 8×32), тож без bias і без rejection-циклу."""
    chars = [_RECOVERY_ALPHABET[b & 0x1F] for b in os.urandom(_RECOVERY_SYMBOLS)]
    groups = ["".join(chars[i:i + _RECOVERY_GROUP])
              for i in range(0, len(chars), _RECOVERY_GROUP)]
    return "-".join(groups)


def _normalize_recovery(code) -> str:
    """Канонічна форма коду для KDF: без роздільників, у верхньому регістрі."""
    if isinstance(code, bytes):
        code = code.decode("utf-8", "ignore")
    return "".join(ch for ch in str(code).upper() if ch in _RECOVERY_ALPHABET)


def _wrap_dek_with_secret(dek: bytes, secret) -> dict:
    """Обгорнути DEK секретом (пароль або код) через scrypt + AES-256-GCM."""
    salt = os.urandom(16)
    n, r, p = _SCRYPT_N, _SCRYPT_R, _SCRYPT_P
    kek = _derive_kek(secret, salt, n=n, r=r, p=p)
    nonce = os.urandom(12)
    wrapped = nonce + _aesgcm_class()(kek).encrypt(nonce, dek, _VAULT_KEY_AAD)
    return {"salt": _b64(salt), "wrapped_dek": _b64(wrapped), "kdf": "scrypt",
            "n": n, "r": r, "p": p}


def _unwrap_dek_with_secret(slot: dict, secret) -> bytes:
    """Розгорнути DEK зі слота (кидає ValueError на биті поля, InvalidTag — на
    невірний секрет). Приймає і legacy-параметри scrypt (як парольний слот)."""
    salt = _unb64(slot["salt"])
    n = slot.get("n", _LEGACY_SCRYPT_N)
    r = slot.get("r", _SCRYPT_R)
    p = slot.get("p", _SCRYPT_P)
    kek = _derive_kek(secret, salt, n=n, r=r, p=p)
    packed = _unb64(slot["wrapped_dek"])
    if len(packed) < 12 + _TAG_BYTES:
        raise ValueError("short wrapped DEK")
    return _aesgcm_class()(kek).decrypt(packed[:12], packed[12:], _VAULT_KEY_AAD)


def generate_keyfile(path) -> Path:
    """Створити файл-ключ: 64 випадкові байти (os.urandom) атомарним записом.

    Вміст файла і є секретом; його треба тримати на знімному носії — файл-ключ
    поряд із записами на тому ж диску не додає захисту."""
    path = Path(path)
    _atomic_bytes(path, os.urandom(_KEYFILE_BYTES))
    return path


def _read_keyfile(path) -> bytes:
    """Прочитати вміст файла-ключа (рівно 64 байти) або чітко відмовити."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise VaultWrongKeyfile("Не вдалося прочитати файл-ключ") from exc
    if len(data) != _KEYFILE_BYTES:
        raise VaultWrongKeyfile("Це не схоже на файл-ключ Balachky")
    return data


def _combine_secret(password, keyfile_bytes: bytes) -> bytes:
    """Двофакторний секрет: HKDF-SHA256 від (пароль || вміст файла-ключа).

    Обидва фактори потрібні одночасно — без будь-якого з них похідний секрет
    інший, і AEAD-обгортка DEK не автентифікується."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise CryptoUnavailable("cryptography is required for meeting encryption.") from exc
    ikm = _password_bytes(password) + keyfile_bytes
    return HKDF(algorithm=hashes.SHA256(), length=_DEK_BYTES, salt=None,
                info=b"balachky-2fa-v1").derive(ikm)


def _nonce(index: int, final: bool) -> bytes:
    if not 0 <= index < _MAX_CHUNKS_PER_FILE:
        raise ValueError(
            f"Зашифрований файл перевищує межу "
            f"{_MAX_CHUNKS_PER_FILE} чанків")
    return index.to_bytes(11, "big") + (b"\x01" if final else b"\x00")

def _context_bytes(context) -> bytes:
    """Stable external binding for v2 containers (never taken from their header)."""
    if isinstance(context, str):
        context = context.encode("utf-8")
    if not isinstance(context, bytes) or not context:
        raise ValueError("v2 encrypted file requires a non-empty storage context")
    return context


def _aad(version: int, file_id: bytes, index: int, final: bool,
         context: bytes = b"") -> bytes:
    base = (bytes((version,)) + file_id + index.to_bytes(8, "big")
            + (b"\x01" if final else b"\x00"))
    return base if version == _FORMAT_VERSION else base + context


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


def _dpapi_call(data: bytes, *, protect: bool) -> bytes:
    """Виклик DPAPI ctypes; тільки per-user, UI примусово вимкнений."""
    if os.name != "nt":
        raise OSError("DPAPI доступний лише у Windows")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    raw = ctypes.create_string_buffer(data)
    source = DATA_BLOB(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_byte)))
    target = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
    kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(source), "Balachky meeting vault", None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target))
    else:
        ok = fn(ctypes.byref(source), None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(data, protect=True)


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call(data, protect=False)


def _is_transient_windows_file_error(exc: OSError) -> bool:
    """Whether Windows has temporarily denied access during a file swap."""
    return (isinstance(exc, PermissionError)
            or getattr(exc, "winerror", None) in _WINDOWS_SHARING_VIOLATIONS)


def _read_vault(root: Path) -> dict:
    path = root / KEY_FILE
    for attempt in range(_VAULT_READ_ATTEMPTS):
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            if blob.get("version") != 1 or blob.get("mode") not in _VAULT_MODES:
                raise ValueError("невідомий формат .vaultkey")
            return blob
        except OSError as exc:
            if (not _is_transient_windows_file_error(exc)
                    or attempt == _VAULT_READ_ATTEMPTS - 1):
                raise VaultKeyLost() from exc
            time.sleep(_VAULT_READ_RETRY_SECONDS)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise VaultKeyLost() from exc
    raise AssertionError("unreachable")


def _write_vault(root: Path, blob: dict) -> None:
    _atomic_bytes(root / KEY_FILE,
                  json.dumps(blob, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _has_encrypted_artifacts(root: Path) -> bool:
    """Return whether a missing vault key would make existing data unrecoverable."""
    return root.is_dir() and any(path.is_file() for path in root.rglob("*.enc"))


class _VaultCreationLock:
    """Short-lived interprocess lock used only when creating the first vault key."""
    def __init__(self, root: Path):
        self.root = root
        self.path = root / _KEY_LOCK_FILE
        self._reclaim_path = root / (_KEY_LOCK_FILE + ".reclaim")
        self.acquired = False

    @staticmethod
    def _pid_alive(pid) -> bool:
        if not isinstance(pid, int) or pid <= 0: return False
        try: os.kill(pid, 0)
        except ProcessLookupError: return False
        except PermissionError: return True
        except OSError: return False
        return True

    def _stale(self) -> bool:
        try:
            record = json.loads(self.path.read_text(encoding="ascii"))
            created, pid = float(record["created"]), record["pid"]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            try: created, pid = self.path.stat().st_mtime, None
            except OSError: return False
        return time.time() - created >= 1 and not self._pid_alive(pid)

    def _reclaim_stale(self) -> None:
        """Serialize reclaimers and re-check before unlinking the main lock."""
        try:
            with self._reclaim_path.open("x", encoding="ascii") as guard:
                guard.write(str(os.getpid()))
            try:
                if self.path.exists() and self._stale(): self._unlink_with_retry(self.path)
            finally:
                self._reclaim_path.unlink(missing_ok=True)
        except FileExistsError:
            pass

    def _unlink_with_retry(self, path: Path) -> None:
        for attempt in range(_VAULT_READ_ATTEMPTS):
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except OSError as exc:
                if (not _is_transient_windows_file_error(exc)
                        or attempt == _VAULT_READ_ATTEMPTS - 1):
                    raise
                time.sleep(_VAULT_READ_RETRY_SECONDS)

    def __enter__(self):
        deadline = time.monotonic() + _VAULT_CREATION_TIMEOUT_SECONDS
        while True:
            try:
                with self.path.open("x", encoding="ascii") as lock:
                    json.dump({"pid": os.getpid(), "created": time.time()}, lock,
                              separators=(",", ":"))
                    lock.flush(); os.fsync(lock.fileno())
                self.acquired = True; return self
            except OSError as exc:
                if (not isinstance(exc, FileExistsError)
                        and not _is_transient_windows_file_error(exc)):
                    raise
                if (self.root / KEY_FILE).is_file():
                    try:
                        _read_vault(self.root)
                    except VaultKeyLost as exc:
                        if not _is_transient_windows_file_error(exc.__cause__):
                            raise
                    else:
                        return self
                self._reclaim_stale()
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for .vaultkey creation")
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            self._unlink_with_retry(self.path)

def _load_dek(root: Path, password=None) -> bytes:
    """Read and unwrap an existing vault key."""
    blob = _read_vault(root)
    if blob["mode"] == "dpapi":
        try: dek = _dpapi_unprotect(_unb64(blob["wrapped_dek"]))
        except (OSError, ValueError, TypeError, KeyError) as exc: raise VaultKeyLost() from exc
    elif blob["mode"] in _KEYFILE_MODES:
        # Файл-ключ не вводиться текстом: без кешу потрібне unlock_with_keyfile().
        dek = _PASSWORD_CACHE.get(str(root.resolve()))
        if dek is None: raise VaultPasswordRequired("Для цього сховища потрібен файл-ключ")
    else:
        cache_key = str(root.resolve())
        if password is None:
            dek = _PASSWORD_CACHE.get(cache_key)
            if dek is None: raise VaultPasswordRequired("Для цього сховища потрібен пароль")
        else:
            try:
                salt = _unb64(blob["salt"])
                n = blob.get("n", _LEGACY_SCRYPT_N)
                r = blob.get("r", _SCRYPT_R)
                p = blob.get("p", _SCRYPT_P)
                kek = _derive_kek(password, salt, n=n, r=r, p=p)
                packed = _unb64(blob["wrapped_dek"])
                if len(packed) < 12 + _TAG_BYTES: raise ValueError("short wrapped DEK")
            except (ValueError, TypeError, KeyError) as exc: raise VaultKeyLost() from exc
            try:
                dek = _aesgcm_class()(kek).decrypt(packed[:12], packed[12:], b"Balachky vault key")
            except CryptoUnavailable:
                raise
            except Exception as exc:      # InvalidTag: стабільний тип для UI
                raise VaultWrongPassword("Пароль не підходить до цього сховища") from exc
            _PASSWORD_CACHE[cache_key] = dek
    if len(dek) != _DEK_BYTES: raise VaultKeyLost()
    return dek

def vault_mode(meetings_root) -> "str | None":
    """"dpapi" | "password" | None (ключа ще немає). Битий ключ → VaultKeyLost."""
    root = Path(meetings_root)
    if not (root / KEY_FILE).exists():
        return None
    return _read_vault(root)["mode"]


def is_unlocked(meetings_root) -> bool:
    """Чи можна читати сховище без пароля прямо зараз (DPAPI або DEK у кеші)."""
    root = Path(meetings_root)
    try:
        mode = vault_mode(root)
    except VaultKeyLost:
        return False
    if mode in (None, "dpapi"):
        return True
    return _PASSWORD_CACHE.get(str(root.resolve())) is not None


def lock_vault(meetings_root) -> None:
    """«Заблокувати зараз»: викинути розшифрований DEK з пам'яті процесу."""
    _PASSWORD_CACHE.pop(str(Path(meetings_root).resolve()), None)


def ensure_dek(meetings_root, password=None) -> bytes:
    """Return the DEK, creating a DPAPI wrapper only for an empty vault."""
    root = Path(meetings_root)
    key_path = root / KEY_FILE
    if key_path.exists():
        return _load_dek(root, password)
    root.mkdir(parents=True, exist_ok=True)
    if _has_encrypted_artifacts(root):
        raise VaultKeyLost()

    # Re-check after taking the interprocess lock: another caller may have won.
    with _VaultCreationLock(root):
        if key_path.exists():
            return _load_dek(root, password)
        if _has_encrypted_artifacts(root):
            raise VaultKeyLost()
        dek = os.urandom(_DEK_BYTES)
        _write_vault(root, {"version": 1, "mode": "dpapi",
                            "wrapped_dek": _b64(_dpapi_protect(dek))})
    if password is not None:
        set_password(root, password)
    return dek

def set_password(meetings_root, password) -> "str | None":
    """Переобгорнути той самий DEK через scrypt + AES-256-GCM, без зміни файлів.

    Повертає одноразовий код відновлення рядком, ЯКЩО цей виклик його щойно
    створив (перше задання пароля для сховища); при зміні пароля наявний слот
    відновлення лишається чинним, а функція повертає None."""
    root = Path(meetings_root)
    if not _password_bytes(password):
        raise ValueError("Пароль не може бути порожнім")
    dek = ensure_dek(root)
    recovery_slot = _read_vault(root).get("recovery")
    recovery_code = None
    if recovery_slot is None:                       # перше задання пароля
        recovery_code = _new_recovery_code()
        recovery_slot = _wrap_dek_with_secret(dek, _normalize_recovery(recovery_code))
    blob = {"version": 1, "mode": "password", **_wrap_dek_with_secret(dek, password),
            "recovery": recovery_slot}
    _write_vault(root, blob)
    _PASSWORD_CACHE[str(root.resolve())] = dek
    return recovery_code


def unlock_with_recovery(meetings_root, code) -> bytes:
    """Розблокувати парольне сховище кодом відновлення → DEK (і кешувати його).

    Код лишається чинним і після використання: далі UI пропонує задати новий
    пароль. Немає слота відновлення → VaultKeyLost; невірний код → VaultWrongRecovery."""
    root = Path(meetings_root)
    blob = _read_vault(root)
    slot = blob.get("recovery")
    if blob.get("mode") not in ("password",) + _KEYFILE_MODES or not isinstance(slot, dict):
        raise VaultKeyLost()
    normalized = _normalize_recovery(code)
    if not normalized:
        raise VaultWrongRecovery("Код відновлення порожній")
    try:
        dek = _unwrap_dek_with_secret(slot, normalized)
    except CryptoUnavailable:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise VaultKeyLost() from exc
    except Exception as exc:                          # InvalidTag: невірний код
        raise VaultWrongRecovery("Код відновлення не підходить") from exc
    if len(dek) != _DEK_BYTES:
        raise VaultKeyLost()
    _PASSWORD_CACHE[str(root.resolve())] = dek
    return dek


def regenerate_recovery(meetings_root, password=None) -> str:
    """Створити НОВИЙ код відновлення (старий перестає діяти). Потрібен відкритий
    DEK: якщо сховище замкнене — передай чинний пароль."""
    root = Path(meetings_root)
    dek = ensure_dek(root, password)
    blob = _read_vault(root)
    if blob.get("mode") not in ("password",) + _KEYFILE_MODES:
        raise ValueError("Код відновлення існує лише для захищеного паролем/файлом сховища")
    code = _new_recovery_code()
    blob["recovery"] = _wrap_dek_with_secret(dek, _normalize_recovery(code))
    _write_vault(root, blob)
    return code


def remove_password(meetings_root, password=None) -> None:
    """Повернути DPAPI; після перезапуску передай чинний пароль явно."""
    root = Path(meetings_root)
    dek = ensure_dek(root, password)
    _write_vault(root, {"version": 1, "mode": "dpapi",
                        "wrapped_dek": _b64(_dpapi_protect(dek))})
    _PASSWORD_CACHE.pop(str(root.resolve()), None)


def set_keyfile(meetings_root, keyfile_path, password=None) -> "str | None":
    """Переобгорнути той самий DEK файлом-ключем (без password) або двофакторно
    «пароль+файл» (з password). Сховище має бути відкрите (виклич ensure_dek із
    чинним секретом перед цим). Повертає одноразовий код відновлення, якщо цей
    виклик його щойно створив; інакше None (наявний код лишається чинним)."""
    root = Path(meetings_root)
    keyfile_bytes = _read_keyfile(keyfile_path)
    dek = ensure_dek(root)
    recovery_slot = _read_vault(root).get("recovery")
    recovery_code = None
    if recovery_slot is None:                       # перший перехід на секрет
        recovery_code = _new_recovery_code()
        recovery_slot = _wrap_dek_with_secret(dek, _normalize_recovery(recovery_code))
    if password is None:
        mode, secret = "keyfile", keyfile_bytes
    else:
        if not _password_bytes(password):
            raise ValueError("Пароль не може бути порожнім")
        mode, secret = "password+keyfile", _combine_secret(password, keyfile_bytes)
    blob = {"version": 1, "mode": mode, **_wrap_dek_with_secret(dek, secret),
            "recovery": recovery_slot}
    _write_vault(root, blob)
    _PASSWORD_CACHE[str(root.resolve())] = dek
    return recovery_code


def unlock_with_keyfile(meetings_root, keyfile_path, password=None) -> bytes:
    """Розблокувати сховище файлом-ключем (і паролем у двофакторному режимі) →
    DEK (кешується). Немає keyfile-режиму → VaultKeyLost; двофактор без пароля →
    VaultPasswordRequired; невірний файл/пароль → VaultWrongKeyfile."""
    root = Path(meetings_root)
    blob = _read_vault(root)
    mode = blob.get("mode")
    if mode not in _KEYFILE_MODES:
        raise VaultKeyLost()
    keyfile_bytes = _read_keyfile(keyfile_path)
    if mode == "password+keyfile":
        if not _password_bytes(password or b""):
            raise VaultPasswordRequired("Двофакторне сховище потребує і пароль, і файл-ключ")
        secret = _combine_secret(password, keyfile_bytes)
    else:
        secret = keyfile_bytes
    try:
        dek = _unwrap_dek_with_secret(blob, secret)
    except CryptoUnavailable:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise VaultKeyLost() from exc
    except Exception as exc:                          # InvalidTag: невірний файл/пароль
        raise VaultWrongKeyfile("Файл-ключ або пароль не підходять") from exc
    if len(dek) != _DEK_BYTES:
        raise VaultKeyLost()
    _PASSWORD_CACHE[str(root.resolve())] = dek
    return dek


def remove_keyfile(meetings_root) -> None:
    """Зняти файл-ключ (повернути захист ключем Windows-акаунта, DPAPI).
    Сховище має бути відкрите; код відновлення для DPAPI не потрібен і зникає."""
    root = Path(meetings_root)
    dek = ensure_dek(root)
    _write_vault(root, {"version": 1, "mode": "dpapi",
                        "wrapped_dek": _b64(_dpapi_protect(dek))})
    _PASSWORD_CACHE.pop(str(root.resolve()), None)


def _check_dek(dek: bytes) -> bytes:
    if not isinstance(dek, bytes) or len(dek) != _DEK_BYTES:
        raise ValueError("DEK має бути випадковим 32-байтним ключем")
    return dek


def _temp_output(dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=dst.parent)


def _encrypt_stream(inp, dst: Path, dek: bytes, context=None) -> None:
    dst, dek = Path(dst), _check_dek(dek)
    version = _CONTEXT_FORMAT_VERSION if context is not None else _FORMAT_VERSION
    binding = _context_bytes(context) if context is not None else b""
    file_id = os.urandom(_FILE_ID_BYTES); aes = _aesgcm_class()(_derive_file_key(dek, file_id))
    fd, tmp_name = _temp_output(dst)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(bytes((version,)) + _MAGIC + file_id); index = 0; current = inp.read(CHUNK_SIZE)
            while True:
                following = inp.read(CHUNK_SIZE) if current else b""; final = not following
                ciphertext = aes.encrypt(_nonce(index, final), current, _aad(version, file_id, index, final, binding))
                out.write(len(ciphertext).to_bytes(4, "big")); out.write(ciphertext)
                if final: break
                current, index = following, index + 1
            out.flush(); os.fsync(out.fileno())
        os.replace(tmp_name, dst)
    except Exception:
        try: os.unlink(tmp_name)
        except OSError: pass
        raise


def encrypt_file(src, dst, dek, *, context=None) -> None:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve(): raise ValueError("src і dst мають бути різними файлами")
    if src.stat().st_size > _MAX_PLAINTEXT_BYTES_PER_FILE:
        raise ValueError(
            f"Відкритий файл перевищує максимальний розмір "
            f"{_MAX_PLAINTEXT_BYTES_PER_FILE} байтів")
    with src.open("rb") as inp: _encrypt_stream(inp, dst, dek, context)


def encrypt_bytes(data: bytes, dst, dek, *, context) -> None:
    import io
    if len(data) > _MAX_PLAINTEXT_BYTES_PER_FILE:
        raise ValueError(
            f"Відкритий файл перевищує максимальний розмір "
            f"{_MAX_PLAINTEXT_BYTES_PER_FILE} байтів")
    _encrypt_stream(io.BytesIO(data), Path(dst), dek, context)


def _decrypt_chunks(src: Path, dek: bytes, *, context=None):
    dek = _check_dek(dek)
    with src.open("rb") as inp:
        raw = inp.read(1)
        if len(raw) != 1: raise ValueError("це не зашифрований файл наради Balachky")
        version = raw[0]
        if version not in (_FORMAT_VERSION, _CONTEXT_FORMAT_VERSION): raise ValueError("невідома версія формату зашифрованого файла наради")
        if inp.read(len(_MAGIC)) != _MAGIC: raise ValueError("це не зашифрований файл наради Balachky")
        file_id = inp.read(_FILE_ID_BYTES)
        if len(file_id) != _FILE_ID_BYTES: raise ValueError("зашифрований файл обрізаний у заголовку")
        if version == _CONTEXT_FORMAT_VERSION: binding = _context_bytes(context)
        elif context is not None: raise ValueError("v1 container is not allowed in a v2 session")
        else: binding = b""
        aes = _aesgcm_class()(_derive_file_key(dek, file_id)); index = 0
        while True:
            size_raw = inp.read(4)
            if len(size_raw) != 4: raise ValueError("зашифрований файл обрізаний (немає фінального чанка)")
            size = int.from_bytes(size_raw, "big")
            if not _TAG_BYTES <= size <= CHUNK_SIZE + _TAG_BYTES: raise ValueError("некоректний розмір зашифрованого чанка")
            ciphertext = inp.read(size)
            if len(ciphertext) != size: raise ValueError("зашифрований файл обрізаний усередині чанка")
            final = False
            try: plain = aes.decrypt(_nonce(index, False), ciphertext, _aad(version, file_id, index, False, binding))
            except Exception as normal_error:
                try: plain = aes.decrypt(_nonce(index, True), ciphertext, _aad(version, file_id, index, True, binding)); final = True
                except Exception: raise normal_error
            yield plain
            if final:
                if inp.read(1): raise ValueError("дані після фінального чанка")
                return
            index += 1


def decrypt_file(src, dst, dek, *, context=None) -> None:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve(): raise ValueError("src і dst мають бути різними файлами")
    fd, tmp_name = _temp_output(dst)
    try:
        with os.fdopen(fd, "wb") as out:
            for plain in _decrypt_chunks(src, dek, context=context): out.write(plain)
            out.flush(); os.fsync(out.fileno())
        os.replace(tmp_name, dst)
    except Exception:
        try: os.unlink(tmp_name)
        except OSError: pass
        raise


def decrypt_to_memory(src, dek, *, context=None) -> bytes:
    return b"".join(_decrypt_chunks(Path(src), dek, context=context))
