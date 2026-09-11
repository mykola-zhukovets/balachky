"""Ізольоване per-user DPAPI-сховище Telegram bot token."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import uuid
from pathlib import Path

from whisper_core import paths


class TelegramSecretError(Exception):
    """Telegram token не можна безпечно зберегти або відкрити."""


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _default_path() -> Path:
    return paths.telegram_token_path()


def _dpapi_call(data: bytes, *, protect: bool) -> bytes:
    """Виклик Windows DPAPI у per-user режимі без UI."""
    if os.name != "nt":
        raise TelegramSecretError("Захист Telegram token доступний лише у Windows")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    raw = ctypes.create_string_buffer(data)
    source = DATA_BLOB(
        len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_byte)))
    target = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
    kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = fn(
            ctypes.byref(source), "Balachky Telegram bot token", None, None,
            None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target))
    else:
        ok = fn(
            ctypes.byref(source), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target))
    if not ok:
        raise TelegramSecretError("Windows не відкрив захищений Telegram token")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _protect(data: bytes) -> bytes:
    return _dpapi_call(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    return _dpapi_call(data, protect=False)


def save_token(token: str, *, path=None) -> None:
    """Захистити token і атомарно записати його окремо від config.toml."""
    value = (token or "").strip()
    if not value or ":" not in value:
        raise TelegramSecretError("Некоректний Telegram bot token")

    target = Path(path) if path is not None else _default_path()
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = _protect(value.encode("utf-8"))
        payload = {
            "version": 1,
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except TelegramSecretError:
        raise
    except Exception:
        raise TelegramSecretError(
            "Не вдалося безпечно зберегти Telegram token") from None
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def load_token(*, path=None) -> str | None:
    """Відкрити token або повернути ``None``, якщо сховища ще немає."""
    target = Path(path) if path is not None else _default_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict)
                or payload.get("version") != 1
                or not isinstance(payload.get("ciphertext"), str)):
            raise ValueError("schema")
        raw = base64.b64decode(
            payload["ciphertext"].encode("ascii"), validate=True)
        value = _unprotect(raw).decode("utf-8")
        if not value or ":" not in value:
            raise ValueError("token")
        return value
    except FileNotFoundError:
        return None
    except TelegramSecretError:
        raise
    except Exception:
        raise TelegramSecretError(
            "Telegram token пошкоджений або недоступний") from None


def delete_token(*, path=None) -> None:
    """Ідемпотентно видалити локальну DPAPI-обгортку token."""
    target = Path(path) if path is not None else _default_path()
    try:
        target.unlink(missing_ok=True)
    except OSError:
        raise TelegramSecretError(
            "Не вдалося видалити захищений Telegram token") from None
