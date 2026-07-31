"""Журнал цілісності наради (chain-of-custody) — незмінний append-only лог подій
із хеш-ланцюгом. БЕЗ Qt, БЕЗ мережі (правило меж пакета whisper_core.meeting).

Легітимна мета: офіцер має довести, що запис наради не змінювали. Журнал фіксує
життєвий цикл сесії (створено / зупинено / розшифровано-фіналізовано / розшифровано
з-під шифру / відредаговано / експортовано) з часовими мітками. Кожен запис несе
хеш попереднього (hash-chain, як у блокчейні): будь-яка підміна чи видалення запису
посередині ламає ланцюг, а ``verify_chain`` це детектує.

Формат: ``audit.jsonl`` поряд із записом — по одному JSON-об'єкту на рядок. Файл
append-only; кожен рядок flush+fsync. Якщо процес урвав останній write, наступний
append відкидає лише незавершений хвіст і включає його розмір та SHA-256 в окрему
подію відновлення. Пошкодження посередині не виправляється автоматично.

Запис:
    {"seq": 0, "type": "created", "ts": 1700000000.0,
     "artifacts": {"mic.wav": "<sha256>", ...},   # опційно (файли сесії)
     "note": {...},                                 # опційно (довільні метадані)
     "prev": "<hex хеша попереднього запису або ''>",
     "hash": "<sha256 канонічного вмісту цього запису>"}

Хеш запису рахується за канонічним JSON полів (seq, type, ts, artifacts, note,
prev) — тими самими, що серіалізуються, тож перерахунок під час верифікації
відтворює ту саму цифру. ``prev`` входить у вміст, тому зміна раннього запису
міняє його hash і рве ``prev`` усіх наступних.

SHA-256 файлів рахуємо потоково (як whisper_core.updater.sha256_of), не тримаючи
файл у пам'яті — ті самі 2-годинні WAV наради.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..history import history_lock

_LOG_NAME = "audit.jsonl"
_HEAD_NAME = ".audit.head"
_HEAD_POLICY_VERSION = 1
_HEAD_POLICY_NOTE_KEY = "_audit_head_policy"
_TOMBSTONE_VERSION = 2
_CHUNK = 1 << 20  # 1 MiB
_APPEND_LOCK_TIMEOUT_SECONDS = 10.0


class AuditLogCorrupt(Exception):
    """append_event відмовляється дописувати у пошкоджений журнал.

    Блокер Т56: після фікса _read_raw битий рядок повертається маркером
    {"_corrupt": True}. Мовчазний дозапис поверх пошкодження приховав би
    порушення цілісності (а events[-1]["seq"]/["hash"] на маркері кидали
    KeyError, який німо ковтав широкий except у UI → усі наступні події
    наради мовчки не дописувалися). Тому append_event на пошкодженні кидає
    цей спеціалізований виняток: фронт ловить його ОКРЕМО, чесно попереджає
    користувача й зупиняє дозапис. verify_chain однаково покаже BROKEN."""


class AuditLogDeleted(Exception):
    """Нараду позначено видаленою: пізній append відхилено без відтворення теки."""


# Типи подій — ПОРІВНЮЄМО в коді, показуємо через tr (фронт).
EVENT_CREATED = "created"        # сесію створено (старт запису)
EVENT_STOPPED = "stopped"        # запис зупинено користувачем
EVENT_FINALIZED = "finalized"    # розшифровка завершена: зафіксовано SHA аудіо+транскрипту
EVENT_DECRYPTED = "decrypted"    # сесію розшифровано (feature/meeting-encryption)
EVENT_EDITED = "edited"          # транскрипт відредаговано (з новим SHA)
EVENT_EXPORTED = "exported"      # аудіо/транскрипт експортовано назовні
EVENT_REVIEWED = "reviewed"      # цілісність підтверджено другим офіцером (принцип «чотирьох очей»)
EVENT_RECOVERED = "journal_recovered"  # відкинуто лише обірваний хвіст; digest є в note
EVENT_BOOKMARK_ADDED = "bookmark_added"  # feature/bookmarks-stage1: ручна мітка моменту наради

# Статус верифікації — ПОРІВНЮЄМО в коді, показуємо через tr.
STATUS_VERIFIED = "verified"     # ланцюг цілий і всі артефакти збігаються
STATUS_BROKEN = "broken"         # підміна запису або файлу — ланцюг порушено
STATUS_ABSENT = "absent"         # журналу немає (стара нарада до цієї фічі)
STATUS_UNVERIFIED = "unverified"  # журнал є, але цілісність ще не перевіряли (лінива перевірка)

def sha256_of(path, chunk: int = _CHUNK) -> str:
    """SHA-256 файлу потоково (не тримаючи його в пам'яті) — як updater.sha256_of."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _record_hash(seq: int, event_type: str, ts: float,
                 artifacts: dict, note: object, prev: str) -> str:
    """SHA-256 канонічного вмісту запису. sort_keys + фіксовані роздільники →
    та сама цифра при перерахунку під час верифікації, незалежно від порядку
    ключів у dict."""
    content = {"seq": seq, "type": event_type, "ts": ts,
               "artifacts": artifacts, "note": note, "prev": prev}
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def hash_artifacts(session_dir, relpaths) -> dict:
    """{relpath: sha256} для наявних файлів сесії. Відсутні тихо пропускаємо —
    у журнал іде лише те, що реально є на диску."""
    session_dir = Path(session_dir)
    out: dict[str, str] = {}
    for rel in relpaths:
        try:
            out[str(rel)] = _artifact_sha(session_dir, rel)
        except FileNotFoundError:
            pass
    return out


def _artifact_sha(session_dir: Path, relative_path) -> str:
    """Hash plaintext content whether the durable artifact is plain or sealed."""
    session_dir = Path(session_dir)
    path = session_dir / relative_path
    if path.is_file():
        return sha256_of(path)
    encrypted = Path(str(path) + ".enc")
    if not encrypted.is_file():
        raise FileNotFoundError(path)
    from . import session as msession
    from .storage_crypto import _decrypt_chunks, ensure_dek
    context = msession._session_context(session_dir, Path(relative_path).as_posix())
    h = hashlib.sha256()
    for block in _decrypt_chunks(
            encrypted, ensure_dek(session_dir.parent),
            context=context if msession._has_v2_artifact(session_dir) else None):
        h.update(block)
    return h.hexdigest()


def created_note(preset, sources, recorded_by: str = "") -> dict:
    """Note для події ``created`` наради. ``recorded_by`` — вільний текст «хто
    зафіксував» (імʼя оператора з налаштувань, не привʼязка до акаунта). Порожній
    → поле не додається: даних не вигадуємо, старі наради лишаються сумісними."""
    note = {"preset": preset, "sources": list(sources)}
    recorded_by = (recorded_by or "").strip()
    if recorded_by:
        note["recorded_by"] = recorded_by
    return note


def _barrier_lock_path(session_dir: Path) -> Path:
    """Один стабільний lock сховища: delete не стирає його під append-ом."""
    session_dir = Path(session_dir)
    return session_dir.parent / ".audit-barrier"


def _deleted_marker_path(session_dir: Path) -> Path:
    session_dir = Path(session_dir)
    return session_dir.parent / f".{session_dir.name}.audit.deleted"


def _atomic_replace(path: Path, payload: bytes, temp_suffix: str) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + temp_suffix)
    try:
        with open(tmp, "wb") as f:
            written = f.write(payload)
            if written != len(payload):
                raise OSError(
                    f"Short atomic write: wrote {written} of {len(payload)} bytes")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_parent(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(Path(path).parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _journal_identity(events) -> "dict | None":
    if not events or not isinstance(events[0], dict) or events[0].get("_corrupt"):
        return None
    auth = events[0].get("auth")
    if isinstance(auth, dict) and isinstance(auth.get("log_id"), str):
        if auth["log_id"]:
            return {"log_id": auth["log_id"]}
    first_hash = events[0].get("hash")
    if isinstance(first_hash, str) and first_hash:
        return {"first_hash": first_hash}
    return None


def _valid_journal_identity(identity) -> bool:
    return (
        isinstance(identity, dict)
        and (
            isinstance(identity.get("log_id"), str) and identity["log_id"]
            or isinstance(identity.get("first_hash"), str)
            and identity["first_hash"]
        )
    )


def _read_deleted_identities(session_dir: Path) -> "list | None":
    marker = _deleted_marker_path(session_dir)
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != _TOMBSTONE_VERSION:
        return None
    if value.get("legacy_unknown"):
        return None
    identities = value.get("identities")
    if identities is None:
        identities = [value.get("identity")]
    if not isinstance(identities, list):
        return None
    if not all(_valid_journal_identity(identity) for identity in identities):
        return None
    return identities


@contextmanager
def deletion_barrier(session_dir):
    """Поставити durable tombstone й тримати append-barrier до завершення delete.

    Маркер лежить поруч, а не всередині папки наради, тому переживає rmtree.
    """
    session_dir = Path(session_dir)
    with history_lock(
            _barrier_lock_path(session_dir),
            timeout=_APPEND_LOCK_TIMEOUT_SECONDS):
        marker = _deleted_marker_path(session_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        identity = _journal_identity(read_events(session_dir))
        previous_identities = (
            _read_deleted_identities(session_dir)
            if marker.exists() else []
        )
        legacy_unknown = previous_identities is None
        identities = list(previous_identities or [])
        if identity is not None and identity not in identities:
            identities.append(identity)
        payload = json.dumps(
            {
                "version": _TOMBSTONE_VERSION,
                "identity": identity,
                "identities": identities,
                "legacy_unknown": legacy_unknown,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        _atomic_replace(marker, payload, ".tmp")
        yield


def clear_deletion_barrier(session_dir) -> None:
    """Зняти tombstone після повернення наради з кошика (Restore/Undo).

    Викликати ЛИШЕ коли ``session_dir`` фізично на місці зі своєю справжньою
    текою — інакше знімаємо бар'єр передчасно й straggler-writer знову зможе
    мовчки відродити фантомну теку (аудит чесності видалення / кошик). Під тим самим
    barrier-lock, що і deletion_barrier/append_event, тож не перетинається з
    паралельним delete чи append.
    """
    session_dir = Path(session_dir)
    with history_lock(
            _barrier_lock_path(session_dir),
            timeout=_APPEND_LOCK_TIMEOUT_SECONDS):
        marker = _deleted_marker_path(session_dir)
        try:
            marker.unlink()
        except FileNotFoundError:
            pass


def _raise_deleted(session_dir: Path) -> None:
    raise AuditLogDeleted(
        f"Нараду {session_dir.name} видалено: подію журналу не дописано")


def _check_deleted_identity(session_dir: Path, events, event_type: str,
                            candidate: dict) -> None:
    marker = _deleted_marker_path(session_dir)
    if not marker.exists():
        return
    deleted_identities = _read_deleted_identities(session_dir)
    if not deleted_identities:
        _raise_deleted(session_dir)
    current_identity = _journal_identity(events)
    if current_identity is not None:
        if current_identity in deleted_identities:
            _raise_deleted(session_dir)
        return
    if event_type != EVENT_CREATED:
        _raise_deleted(session_dir)
    if _journal_identity([candidate]) in deleted_identities:
        _raise_deleted(session_dir)


def _make_record(events, event_type: str, ts: float, artifacts: dict,
                 note: object, signer, log_id, require_signature: bool) -> dict:
    seq = (events[-1]["seq"] + 1) if events else 0
    prev = events[-1]["hash"] if events else ""
    if not events:
        if isinstance(note, dict):
            note = dict(note)
            note[_HEAD_POLICY_NOTE_KEY] = _HEAD_POLICY_VERSION
        else:
            note = {
                _HEAD_POLICY_NOTE_KEY: _HEAD_POLICY_VERSION,
                "value": note,
            }
    digest = _record_hash(seq, event_type, ts, artifacts, note, prev)
    record = {"seq": seq, "type": event_type, "ts": ts,
              "artifacts": artifacts, "note": note, "prev": prev, "hash": digest}

    is_signed_journal = (events and isinstance(events[0], dict)
                         and "auth" in events[0])
    if require_signature and signer is None:
        raise ValueError("require_signature=True, але signer не передано")
    if is_signed_journal and signer is None:
        from .signing import SigningKeyMissing
        raise SigningKeyMissing(
            "Журнал підписаний — дописати без signer неможливо (§7.1)")
    if signer is not None:
        from .signing import sign_audit_record, new_log_id
        if log_id is None:
            if is_signed_journal:
                for event in events:
                    auth = event.get("auth")
                    if isinstance(auth, dict) and auth.get("log_id"):
                        log_id = auth["log_id"]
                        break
            if log_id is None:
                log_id = new_log_id()
        record["auth"] = sign_audit_record(record, signer, log_id)
    return record


def _journal_bytes(path: Path) -> bytes:
    from .session import read_artifact
    return read_artifact(path.parent, path.name)


def _events_from_bytes(data: bytes) -> list:
    out = []
    parts = data.split(b"\n")
    last_unterminated = bool(data) and not data.endswith(b"\n")
    for index, raw_line in enumerate(parts):
        line = raw_line.strip()
        if not line:
            continue
        if last_unterminated and index == len(parts) - 1:
            out.append({"_corrupt": True})
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            out.append({"_corrupt": True})
            continue
        out.append(value if isinstance(value, dict) else {"_corrupt": True})
    return out


def _recoverable_tail(data: bytes):
    """(valid_prefix, discarded_tail) лише для останнього обірваного JSON-рядка.

    Битий рядок, після якого є ще один непорожній рядок, є серединою журналу й
    ніколи не відновлюється автоматично.
    """
    if not data:
        return None
    parts = data.split(b"\n")
    offset = 0
    for index, raw_line in enumerate(parts):
        has_newline = index < len(parts) - 1
        segment_size = len(raw_line) + (1 if has_newline else 0)
        line = raw_line.strip()
        if not line:
            offset += segment_size
            continue
        if not has_newline:
            return data[:offset], data[offset:]
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError):
            has_later_record = any(part.strip() for part in parts[index + 1:])
            if not has_later_record:
                return data[:offset], data[offset:]
            return None
        if not isinstance(value, dict):
            return None
        offset += segment_size
    return None


def _line_bytes(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def _append_plain(path: Path, payload: bytes) -> None:
    with open(path, "a+b") as f:
        written = f.write(payload)
        if written != len(payload):
            raise OSError(
                f"Short audit append: wrote {written} of {len(payload)} bytes")
        f.flush()
        os.fsync(f.fileno())


def _replace_plain(path: Path, payload: bytes) -> None:
    _atomic_replace(path, payload, ".recover.tmp")


def _write_head(session_dir: Path, record: dict) -> None:
    payload = json.dumps(
        {
            "version": _HEAD_POLICY_VERSION,
            "seq": record["seq"],
            "hash": record["hash"],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    _atomic_replace(Path(session_dir) / _HEAD_NAME, payload, ".tmp")


def append_event(session_dir, event_type: str, *, artifacts: dict = None,
                  note: object = None, ts: float = None,
                  signer=None, log_id=None, require_signature=False,
                  lock_timeout: float | None = None) -> dict:
    """Дописати подію у ``audit.jsonl`` із хешем попереднього запису.

    Читає останній запис, бере його ``hash`` за ``prev``, рахує власний ``hash``,
    дописує рядок (flush+fsync). Якщо останній write був обірваний, спершу
    атомарно замінює хвіст на окрему recovery-подію. Повертає запитаний запис.
    """
    import time
    session_dir = Path(session_dir)
    artifacts = dict(artifacts or {})
    ts = float(time.time() if ts is None else ts)
    lock_timeout = (
        _APPEND_LOCK_TIMEOUT_SECONDS
        if lock_timeout is None else lock_timeout
    )
    with history_lock(
            _barrier_lock_path(session_dir),
            timeout=lock_timeout):
        if (_deleted_marker_path(session_dir).exists()
                and not session_dir.exists()):
            _raise_deleted(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / _LOG_NAME
        # Сумісний lock лишається частиною транзакції; зовнішній sibling
        # barrier потрібен лише тому, що lock усередині rmtree не переживає delete.
        with history_lock(path, timeout=lock_timeout):
            try:
                previous = _journal_bytes(path)
            except FileNotFoundError:
                previous = b""
            recovery = _recoverable_tail(previous)
            valid_bytes = recovery[0] if recovery is not None else previous
            events = _events_from_bytes(valid_bytes)
            if any(isinstance(event, dict) and event.get("_corrupt")
                   for event in events):
                raise AuditLogCorrupt(
                    "Журнал цілісності наради пошкоджено: нові події не "
                    "дописуються, доказовий пакет покаже BROKEN")

            records = []
            if recovery is not None:
                discarded = recovery[1]
                recovery_note = {
                    "discarded_bytes": len(discarded),
                    "discarded_sha256": hashlib.sha256(discarded).hexdigest(),
                }
                recovered = _make_record(
                    events, EVENT_RECOVERED, float(time.time()), {},
                    recovery_note, signer, log_id, require_signature)
                records.append(recovered)
                events.append(recovered)

            record = _make_record(
                events, event_type, ts, artifacts, note,
                signer, log_id, require_signature)
            _check_deleted_identity(
                session_dir, events, event_type, record)
            records.append(record)
            payload = b"".join(_line_bytes(item) for item in records)

            encrypted = Path(str(path) + ".enc")
            encrypted_session = (session_dir / "meeting.json.enc").exists()
            if encrypted_session or (encrypted.exists() and not path.exists()):
                from .session import write_artifact
                write_artifact(session_dir, _LOG_NAME, valid_bytes + payload)
            elif recovery is not None:
                _replace_plain(path, valid_bytes + payload)
            else:
                _append_plain(path, payload)
            _write_head(session_dir, record)
            return record


def finalize(session_dir, artifact_relpaths, *, note: object = None,
             signer=None, log_id=None) -> dict:
    """Подія завершення розшифровки: зафіксувати SHA фінального аудіо + транскрипту.

    ``artifact_relpaths`` — імена файлів сесії (напр. ["mic.wav", "sys.wav",
    "transcript.txt"]). Хеші рахуються тут і лягають у незмінний запис.
    """
    session_dir = Path(session_dir)
    return append_event(session_dir, EVENT_FINALIZED,
                        artifacts=hash_artifacts(session_dir, artifact_relpaths), note=note,
                        signer=signer, log_id=log_id)


def _read_raw(path: Path) -> list:
    """Розпарсити audit.jsonl → список записів. Порожньо/нема файлу → []. Битий
    рядок маркується як _corrupt для гарантії виявлення порушення цілісності."""
    try:
        data = _journal_bytes(path)
    except (OSError, FileNotFoundError):
        return []
    return _events_from_bytes(data)


def read_events(session_dir) -> list:
    """Усі події журналу сесії (у порядку запису). Немає журналу → []."""
    return _read_raw(Path(session_dir) / _LOG_NAME)


@dataclass
class ChainResult:
    status: str                       # STATUS_VERIFIED | STATUS_BROKEN | STATUS_ABSENT
    event_count: int = 0
    broken_seq: "int | None" = None   # seq запису, на якому порушено ланцюг
    audio_sha: "str | None" = None    # SHA першої аудіо-доріжки (*.wav) для показу
    events: list = field(default_factory=list)
    # Ed25519 (T54)
    auth_status: str = ""              # signed_valid | signed_invalid | signed_key_missing | unsigned_legacy
    log_id: str = ""
    key_ids: list = field(default_factory=list)
    head_hash: str = ""
    parse_error: str = ""
    checkpoint_status: str = ""
    checkpoint_error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_VERIFIED


def _first_audio_sha(expected: dict) -> "str | None":
    """SHA першої аудіо-доріжки (для короткого показу «SHA аудіо» у картці)."""
    for rel in sorted(expected):
        if rel.lower().endswith(".wav"):
            return expected[rel]
    return None


def read_chain_meta(session_dir) -> ChainResult:
    """ДЕШЕВИЙ статус журналу БЕЗ хешування артефактів — безпечно кликати на
    рендері картки (на КОЖНОМУ showEvent, для кожної минулої наради).

    Лише читає й парсить ``audit.jsonl`` (маленький файл), НЕ читає й НЕ хешує
    аудіо/транскрипт із диска. Повертає:
    * ABSENT — журналу немає (стара нарада);
    * UNVERIFIED — журнал є; цілісність не перевірено (це робить ``verify_chain``
      за явним запитом користувача — «Журнал цілісності»).

    ``audio_sha`` — записаний у журналі хеш першої аудіо-доріжки (для показу),
    береться з подій без перехешування файлу.
    """
    events = read_events(session_dir)
    if not events:
        return ChainResult(status=STATUS_ABSENT)
    expected: dict[str, str] = {}
    for rec in events:
        for rel, sha in (rec.get("artifacts") or {}).items():
            expected[rel] = sha
    return ChainResult(status=STATUS_UNVERIFIED, event_count=len(events),
                       audio_sha=_first_audio_sha(expected), events=events)


def _checkpoint_state(session_dir: Path, events: list) -> tuple:
    """(status, explanation, result_status_override) для ``.audit.head``."""
    head_path = Path(session_dir) / _HEAD_NAME
    first_note = events[0].get("note") if events else None
    head_required = (
        isinstance(first_note, dict)
        and first_note.get(_HEAD_POLICY_NOTE_KEY) == _HEAD_POLICY_VERSION
    )
    if not head_path.exists():
        if head_required:
            return (
                "missing",
                "audit checkpoint is missing; possible deletion or rollback",
                STATUS_UNVERIFIED,
            )
        return (
            "missing_legacy",
            "legacy journal has no audit checkpoint",
            None,
        )
    try:
        head = json.loads(head_path.read_text(encoding="utf-8"))
        head_seq = head["seq"]
        head_hash = head["hash"]
        if (head.get("version") != _HEAD_POLICY_VERSION
                or not isinstance(head_seq, int)
                or isinstance(head_seq, bool)
                or head_seq < 0
                or not isinstance(head_hash, str)
                or not head_hash):
            raise ValueError("invalid audit checkpoint fields")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return (
            "invalid",
            "audit checkpoint cannot be validated: {}".format(exc),
            STATUS_UNVERIFIED,
        )

    journal_seq = events[-1]["seq"]
    if head_seq > journal_seq:
        return (
            "truncated",
            "checkpoint seq {} is ahead of journal seq {}; complete events "
            "were removed".format(head_seq, journal_seq),
            STATUS_BROKEN,
        )
    if events[head_seq].get("hash") != head_hash:
        return (
            "mismatch",
            "checkpoint hash does not match journal seq {}".format(head_seq),
            STATUS_BROKEN,
        )
    if head_seq < journal_seq:
        return (
            "journal_ahead",
            "journal seq {} is ahead of checkpoint seq {}; accepted as an "
            "interrupted checkpoint update".format(journal_seq, head_seq),
            None,
        )
    return ("current", "", None)


def verify_chain(session_dir) -> ChainResult:
    """Перевірити цілісність журналу й артефактів сесії.

    1. Немає журналу → ABSENT (стара нарада; не помилка, просто без гарантії).
    2. Хеш-ланцюг: для кожного запису перерахований hash має збігтися зі
       збереженим, а ``prev`` — з hash попереднього. Перший розрив → BROKEN.
    3. Артефакти: беремо ОСТАННІЙ зафіксований SHA кожного файлу (аудіо
       фіксується раз, транскрипт — щоразу при legitimate edit) і перехешовуємо
       файл на диску. Розбіжність або зникнення → BROKEN (підміна/видалення).

    Межа моделі загроз: локальний head ловить зрізання хвоста, доки лишається
    на диску. Зловмисник із повним доступом може видалити ``audit.jsonl`` і
    ``.audit.head`` разом — тоді результат не відрізняється від старої наради
    без журналу. Захист від повного видалення потребує Ed25519-підпису
    разом із незалежною зовнішньою копією або якорем; цей verifier цього не
    обіцяє.
    """
    session_dir = Path(session_dir)
    events = read_events(session_dir)
    if not events:
        return ChainResult(status=STATUS_ABSENT)

    expected_artifacts: dict[str, str] = {}
    prev = ""
    for i, rec in enumerate(events):
        if not isinstance(rec, dict) or rec.get("_corrupt") or "seq" not in rec or "type" not in rec or "ts" not in rec:
            return ChainResult(status=STATUS_BROKEN, event_count=len(events),
                               broken_seq=rec.get("seq", i) if isinstance(rec, dict) else i,
                               events=events)
        # seq має точно дорівнювати індексу рядка (§7.2): жодних пропусків,
        # дублікатів чи перестановок seq непомітно не пройде.
        if not isinstance(rec["seq"], int) or isinstance(rec["seq"], bool) or rec["seq"] != i:
            return ChainResult(status=STATUS_BROKEN, event_count=len(events),
                               broken_seq=rec.get("seq"), events=events,
                               parse_error="seq != index at line {}".format(i))
        try:
            seq = rec["seq"]
            digest = _record_hash(seq, rec["type"], rec["ts"],
                                  rec.get("artifacts") or {}, rec.get("note"),
                                  rec.get("prev", ""))
        except (KeyError, TypeError):
            return ChainResult(status=STATUS_BROKEN, event_count=len(events),
                               broken_seq=rec.get("seq", i) if isinstance(rec, dict) else i,
                               events=events)
        if digest != rec.get("hash") or rec.get("prev", "") != prev:
            return ChainResult(status=STATUS_BROKEN, event_count=len(events),
                               broken_seq=seq, events=events)
        prev = rec["hash"]
        for rel, sha in (rec.get("artifacts") or {}).items():
            expected_artifacts[rel] = sha

    checkpoint_status, checkpoint_error, checkpoint_result_status = (
        _checkpoint_state(session_dir, events)
    )
    checkpoint_fields = {
        "checkpoint_status": checkpoint_status,
        "checkpoint_error": checkpoint_error,
    }
    if checkpoint_result_status == STATUS_BROKEN:
        return ChainResult(
            status=STATUS_BROKEN,
            event_count=len(events),
            broken_seq=events[-1]["seq"],
            events=events,
            parse_error=checkpoint_error,
            **checkpoint_fields,
        )
    verified_status = checkpoint_result_status or STATUS_VERIFIED

    # Ланцюг цілий — тепер звірити самі файли з останнім зафіксованим хешем.
    for rel, sha in expected_artifacts.items():
        try:
            current = _artifact_sha(session_dir, rel)
        except FileNotFoundError:
            current = None
        if current != sha:
            return ChainResult(status=STATUS_BROKEN, event_count=len(events),
                               events=events,
                               audio_sha=_first_audio_sha(expected_artifacts),
                               **checkpoint_fields)

    # ── Ed25519 перевірка підписів (T54, §7.3 спеки) ──
    head_hash = events[-1]["hash"] if events else ""
    first_auth = events[0].get("auth") if events else None
    is_signed = isinstance(first_auth, dict)

    if not is_signed:
        # ЗМІШАНИЙ ЖУРНАЛ (§7.2): нульова подія без ``auth``, а пізніші події
        # підписані. Справжній legacy-журнал таким бути не може — це атака
        # «зняти auth лише з events[0]», щоб згасити сигнал «журнал підписаний»
        # і отримати м'який unsigned_legacy замість BROKEN.
        for i, rec in enumerate(events[1:], start=1):
            if rec.get("auth") is not None:
                return ChainResult(
                    status=STATUS_BROKEN, event_count=len(events),
                    broken_seq=rec.get("seq", i), events=events,
                    auth_status="signed_invalid", head_hash=head_hash,
                    parse_error="mixed_auth_journal: unsigned first event, "
                                "signed event at seq={}".format(i),
                    **checkpoint_fields)
        # Старий unsigned журнал — не broken і не signed
        return ChainResult(
            status=verified_status, event_count=len(events),
            audio_sha=_first_audio_sha(expected_artifacts), events=events,
            auth_status="unsigned_legacy", head_hash=head_hash,
            **checkpoint_fields)

    # Підписаний журнал — перевіряємо кожен підпис
    from .signing import verify_audit_record, public_keys_for_journal
    known_keys = public_keys_for_journal(events)
    key_resolver = lambda kid: known_keys.get(kid)
    log_id = ""
    seen_key_ids = set()

    for i, rec in enumerate(events):
        auth = rec.get("auth")
        if not isinstance(auth, dict):
            # unsigned подія в signed журналі — BROKEN (§7.2)
            return ChainResult(
                status=STATUS_BROKEN, event_count=len(events),
                broken_seq=rec.get("seq", i), events=events,
                auth_status="signed_invalid",
                parse_error="unsigned event in signed journal at seq={}".format(i),
                **checkpoint_fields)

        # log_id consistency
        ev_log_id = auth.get("log_id", "")
        if i == 0:
            log_id = ev_log_id
        elif ev_log_id != log_id:
            return ChainResult(
                status=STATUS_BROKEN, event_count=len(events),
                broken_seq=rec.get("seq", i), events=events,
                auth_status="signed_invalid",
                parse_error="log_id mismatch at seq={}".format(i),
                **checkpoint_fields)

        result = verify_audit_record(rec, key_resolver)
        seen_key_ids.add(auth.get("key_id", ""))
        if not result.valid:
            if result.error == "key not found":
                return ChainResult(
                    status=STATUS_BROKEN, event_count=len(events),
                    broken_seq=rec.get("seq", i), events=events,
                    auth_status="signed_key_missing",
                    log_id=log_id, key_ids=sorted(seen_key_ids),
                    head_hash=head_hash, **checkpoint_fields)
            return ChainResult(
                status=STATUS_BROKEN, event_count=len(events),
                broken_seq=rec.get("seq", i), events=events,
                auth_status="signed_invalid",
                log_id=log_id, key_ids=sorted(seen_key_ids),
                head_hash=head_hash,
                parse_error=result.error, **checkpoint_fields)

    return ChainResult(
        status=verified_status, event_count=len(events),
        audio_sha=_first_audio_sha(expected_artifacts), events=events,
        auth_status="signed_valid",
        log_id=log_id, key_ids=sorted(seen_key_ids),
        head_hash=head_hash, **checkpoint_fields)
