"""Ed25519-підпис журналу цілісності наради — ключі, підпис, перевірка.

БЕЗ Qt, БЕЗ мережі (правило меж пакета whisper_core.meeting).
Залежність: cryptography (hazmat.primitives) — вже у requirements.txt.
Точка інтеграції зі сховищем: ``storage_crypto.ensure_dek()`` — єдиний шлях
до DEK. Внутрішні ``_dpapi_protect()`` і ``_PASSWORD_CACHE`` напряму НЕ
викликаються (правило спеки §5.3).

Формат підпису:
    Кожен запис ``audit.jsonl`` отримує блок ``auth`` із Ed25519-підписом.
    Підписується канонічний JSON (``algorithm``, ``hash``, ``key_id``,
    ``log_id``, ``seq``, ``version``), префіксований
    ``b"Balachky audit event v1\\x00"``.

Ключ зберігається у ``<meetings_root>/.audit-signing-key.json``, зашифрований
AES-256-GCM із wrapping key, виведеним із DEK через HKDF-SHA256.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_SIGNING_KEY_FILE = ".audit-signing-key.json"
_SIGNING_PREFIX = b"Balachky audit event v1\x00"
_EVIDENCE_PREFIX = b"Balachky evidence manifest v2\x00"
_AUTH_VERSION = 1
_ALGORITHM = "Ed25519"
_HKDF_INFO = b"balachky-audit-private-key-v1"


class SigningKeyMissing(RuntimeError):
    """Ключ підпису не знайдено; підписати або дописати у підписаний журнал неможливо."""


class SigningKeyCorrupt(RuntimeError):
    """Контейнер ключа підпису пошкоджено або неможливо розшифрувати."""


@dataclass
class SigningIdentity:
    """Активна пара Ed25519 для підпису журналу."""
    key_id: str                      # "sha256:<64hex>"
    public_key_b64: str              # Base64 of 32-byte raw public key
    _private_key: object             # Ed25519PrivateKey (cryptography)
    rotation_serial: int = 0

    @property
    def public_key_bytes(self) -> bytes:
        return base64.b64decode(self.public_key_b64)


@dataclass
class SignatureResult:
    """Результат перевірки Ed25519-підпису запису журналу."""
    valid: bool
    key_id: str = ""
    error: str = ""


# ── Утиліти ──────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"), validate=True)


def _canonical_json(obj: dict) -> bytes:
    """Канонічний JSON для підпису: ASCII, sort_keys, compact, no NaN."""
    return json.dumps(
        obj, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("ascii")


def _compute_key_id(public_key_bytes: bytes) -> str:
    """``key_id = "sha256:" + lowercase_hex(SHA256(raw_public_key))``."""
    return "sha256:" + hashlib.sha256(public_key_bytes).hexdigest()


def _make_signing_body(record_hash: str, key_id: str,
                       log_id: str, seq: int) -> dict:
    """Тіло підпису (§4.2 спеки)."""
    return {
        "algorithm": _ALGORITHM,
        "hash": record_hash,
        "key_id": key_id,
        "log_id": log_id,
        "seq": seq,
        "version": _AUTH_VERSION,
    }


def _protection_aad(version: int, key_id: str,
                    public_key_b64: str, algorithm: str) -> bytes:
    """AAD для AES-256-GCM обгортки seed (§5.3 спеки)."""
    return _canonical_json({
        "algorithm": algorithm,
        "key_id": key_id,
        "public_key": public_key_b64,
        "version": version,
    })


def _atomic_write(path: Path, data: bytes) -> None:
    """Атомарний запис файлу: temp → flush → fsync → replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent)
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


def new_log_id() -> str:
    """Згенерувати UUIDv4 для нового журналу (§4.2 спеки)."""
    return str(uuid.uuid4())


# ── Керування ключем ─────────────────────────────────────────────────────

def ensure_signing_identity(meetings_root) -> SigningIdentity:
    """Створити або завантажити signing identity.

    Якщо контейнера ще нема — генерує Ed25519 ключ, створює ``.vaultkey``
    (через ``ensure_dek``) і зберігає зашифрований seed. Якщо контейнер є —
    завантажує існуючий ключ.
    """
    root = Path(meetings_root)
    if (root / _SIGNING_KEY_FILE).exists():
        return load_signing_identity(root)
    return _create_signing_identity(root)


def load_signing_identity(meetings_root) -> SigningIdentity:
    """Завантажити існуючу signing identity. Кидає ``SigningKeyMissing``, якщо
    контейнера немає, або ``SigningKeyCorrupt``, якщо він пошкоджений."""
    from .storage_crypto import ensure_dek
    root = Path(meetings_root)
    key_path = root / _SIGNING_KEY_FILE
    if not key_path.exists():
        raise SigningKeyMissing(
            "Ключ підпису журналу не знайдено; "
            "створіть identity або відновіть із резервної копії")
    dek = ensure_dek(root)
    return _load_identity(key_path, dek)


def _create_signing_identity(root: Path) -> SigningIdentity:
    """Згенерувати нову пару Ed25519 і зберегти під захистом DEK."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat)
    from .storage_crypto import ensure_dek

    dek = ensure_dek(root)
    private_key = Ed25519PrivateKey.generate()

    seed = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)

    public_key_b64 = _b64(pub_bytes)
    key_id = _compute_key_id(pub_bytes)

    _save_identity_container(root, seed, key_id, public_key_b64, dek)

    return SigningIdentity(
        key_id=key_id,
        public_key_b64=public_key_b64,
        _private_key=private_key,
        rotation_serial=0,
    )


def _save_identity_container(root: Path, seed: bytes, key_id: str,
                              public_key_b64: str, dek: bytes) -> None:
    """Зберегти зашифрований контейнер ключа (§5.3, §5.4 спеки)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    salt = os.urandom(16)
    wrapping_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        info=_HKDF_INFO,
    ).derive(dek)

    nonce = os.urandom(12)
    aad = _protection_aad(_AUTH_VERSION, key_id, public_key_b64, _ALGORITHM)
    ciphertext = AESGCM(wrapping_key).encrypt(nonce, seed, aad)

    container = {
        "kind": "balachky-audit-signing-key",
        "version": 1,
        "active_key_id": key_id,
        "public_key": public_key_b64,
        "rotation_serial": 0,
        "protection": {
            "mode": "vault-dek",
            "kdf": "HKDF-SHA256",
            "salt": _b64(salt),
            "nonce": _b64(nonce),
            "ciphertext": _b64(ciphertext),
        },
        "certificates": [],
    }

    data = json.dumps(
        container, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_write(root / _SIGNING_KEY_FILE, data)


def _load_identity(key_path: Path, dek: bytes) -> SigningIdentity:
    """Прочитати і розшифрувати контейнер ключа."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)

    try:
        blob = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise SigningKeyCorrupt(
            "Контейнер ключа підпису пошкоджено") from exc

    if (blob.get("kind") != "balachky-audit-signing-key"
            or blob.get("version") != 1):
        raise SigningKeyCorrupt(
            "Невідомий формат контейнера ключа підпису")

    try:
        key_id = blob["active_key_id"]
        public_key_b64 = blob["public_key"]
        prot = blob["protection"]
        salt = _unb64(prot["salt"])
        nonce = _unb64(prot["nonce"])
        ciphertext = _unb64(prot["ciphertext"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SigningKeyCorrupt(
            "Поля контейнера ключа підпису відсутні або пошкоджені"
        ) from exc

    wrapping_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt,
        info=_HKDF_INFO,
    ).derive(dek)

    aad = _protection_aad(1, key_id, public_key_b64, _ALGORITHM)
    try:
        seed = AESGCM(wrapping_key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise SigningKeyCorrupt(
            "Ключ підпису не розшифровується поточним DEK") from exc

    private_key = Ed25519PrivateKey.from_private_bytes(seed)

    # Верифікація: key_id має збігатися з фактичним публічним ключем
    pub_bytes = private_key.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)
    actual_key_id = _compute_key_id(pub_bytes)
    if actual_key_id != key_id:
        raise SigningKeyCorrupt(
            "key_id контейнера не збігається з фактичним ключем")

    return SigningIdentity(
        key_id=key_id,
        public_key_b64=public_key_b64,
        _private_key=private_key,
        rotation_serial=blob.get("rotation_serial", 0),
    )


# ── Підпис і перевірка подій журналу ─────────────────────────────────────

def sign_audit_record(record: dict, identity: SigningIdentity,
                      log_id: str) -> dict:
    """Підписати запис журналу. Повертає блок ``auth`` для вбудовування в запис.

    ``public_key`` вставляється лише при першому використанні ключа (seq == 0)
    або за наявності certificate_chain (§4.2 спеки). В інших подіях —
    порожній рядок (economія місця, ключ уже відомий з ранішої події).
    """
    record_hash = record["hash"]
    seq = record["seq"]

    body = _make_signing_body(record_hash, identity.key_id, log_id, seq)
    message = _SIGNING_PREFIX + _canonical_json(body)

    signature = identity._private_key.sign(message)

    # public_key включається при seq==0 (перше використання ключа в журналі)
    include_pubkey = (seq == 0)

    auth = {
        "version": _AUTH_VERSION,
        "algorithm": _ALGORITHM,
        "log_id": log_id,
        "key_id": identity.key_id,
        "public_key": identity.public_key_b64 if include_pubkey else "",
        "certificate_chain": [],
        "signature": _b64(signature),
    }
    return auth


def verify_audit_record(record: dict,
                        key_resolver: Callable[[str], Optional[bytes]]
                        ) -> SignatureResult:
    """Перевірити Ed25519-підпис запису журналу.

    ``key_resolver(key_id) -> bytes | None``: повертає 32 байти raw public key
    для відповідного ``key_id`` або ``None``, якщо ключ невідомий.
    """
    auth = record.get("auth")
    if not isinstance(auth, dict):
        return SignatureResult(valid=False, error="no auth block")

    try:
        key_id = auth["key_id"]
        signature = _unb64(auth["signature"])
        log_id = auth["log_id"]
        version = auth["version"]
        algorithm = auth["algorithm"]
    except (KeyError, TypeError, ValueError) as exc:
        return SignatureResult(
            valid=False, error="malformed auth: {}".format(exc))

    if version != _AUTH_VERSION:
        return SignatureResult(
            valid=False, key_id=key_id, error="unsupported auth version")
    if algorithm != _ALGORITHM:
        return SignatureResult(
            valid=False, key_id=key_id, error="unsupported algorithm")
    if len(signature) != 64:
        return SignatureResult(
            valid=False, key_id=key_id, error="invalid signature length")

    pub_bytes = key_resolver(key_id)
    if pub_bytes is None:
        return SignatureResult(
            valid=False, key_id=key_id, error="key not found")
    if len(pub_bytes) != 32:
        return SignatureResult(
            valid=False, key_id=key_id, error="invalid public key length")

    body = _make_signing_body(
        record["hash"], key_id, log_id, record["seq"])
    message = _SIGNING_PREFIX + _canonical_json(body)

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        public_key.verify(signature, message)
    except Exception:
        return SignatureResult(
            valid=False, key_id=key_id, error="invalid signature")

    return SignatureResult(valid=True, key_id=key_id)


# ── Evidence manifest (§8 спеки) ─────────────────────────────────────────

def sign_evidence_manifest(manifest: dict,
                           identity: SigningIdentity) -> str:
    """Підписати evidence manifest. Повертає Base64-підпис.

    Підписується канонічний JSON manifest без поля ``signature`` (§8.1 спеки):
    ``b"Balachky evidence manifest v2\\x00" + canonical_manifest_body``.
    """
    body = {k: v for k, v in manifest.items() if k != "signature"}
    message = _EVIDENCE_PREFIX + _canonical_json(body)
    signature = identity._private_key.sign(message)
    return _b64(signature)


def verify_evidence_manifest(manifest: dict, pub_bytes: bytes) -> bool:
    """Перевірити підпис evidence manifest. True — підпис валідний."""
    try:
        signature = _unb64(manifest["signature"])
        body = {k: v for k, v in manifest.items() if k != "signature"}
        message = _EVIDENCE_PREFIX + _canonical_json(body)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        public_key.verify(signature, message)
        return True
    except Exception:
        return False


# ── Допоміжні функції для верифікації ────────────────────────────────────

def public_keys_for_journal(events: list) -> dict:
    """Витягти публічні ключі з подій журналу.

    Повертає ``{key_id: raw_public_key_bytes}``. Ключ включається в першу
    подію, де він використовується (§4.2 спеки).
    """
    keys: dict[str, bytes] = {}
    for ev in events:
        auth = ev.get("auth")
        if not isinstance(auth, dict):
            continue
        key_id = auth.get("key_id", "")
        pub_b64 = auth.get("public_key", "")
        if key_id and pub_b64:
            try:
                pub_bytes = _unb64(pub_b64)
                if len(pub_bytes) == 32:
                    # Верифікуємо key_id
                    expected_id = _compute_key_id(pub_bytes)
                    if expected_id == key_id:
                        keys[key_id] = pub_bytes
            except (ValueError, TypeError):
                pass
    return keys


def build_public_keys_json(events: list) -> str:
    """Побудувати ``PUBLIC-KEYS.json`` для evidence-пакету (§9.3 спеки)."""
    keys = public_keys_for_journal(events)
    entries = []
    for key_id, pub_bytes in sorted(keys.items()):
        entries.append({
            "key_id": key_id,
            "algorithm": _ALGORITHM,
            "public_key": _b64(pub_bytes),
            "fingerprint": key_id.replace("sha256:", ""),
        })
    return json.dumps(
        {"keys": entries, "note": "Публічний ключ із пакета не є доказом "
         "особи. Для юридично значущої перевірки підтвердіть fingerprint "
         "незалежним каналом (§9.3 спеки)."},
        ensure_ascii=False, indent=2,
    )


# ── Stub для майбутніх хвиль ─────────────────────────────────────────────

def rotate_signing_identity(meetings_root, reason: str = ""):
    """TODO(T54-wave2 §5.6): штатна ротація identity зі старим→новим certificate."""
    raise NotImplementedError(
        "Ротація ключа підпису буде реалізована окремою хвилею")


def export_signing_identity(meetings_root, out_path, transfer_password):
    """TODO(T54-wave2 §6.3): експорт identity для перенесення на інший комп'ютер."""
    raise NotImplementedError(
        "Експорт identity буде реалізований окремою хвилею")


def import_signing_identity(meetings_root, archive_path, transfer_password):
    """TODO(T54-wave2 §6.3): імпорт identity з transfer-файлу."""
    raise NotImplementedError(
        "Імпорт identity буде реалізований окремою хвилею")
