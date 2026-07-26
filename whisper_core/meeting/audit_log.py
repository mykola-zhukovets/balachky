"""Журнал цілісності наради (chain-of-custody) — незмінний append-only лог подій
із хеш-ланцюгом. БЕЗ Qt, БЕЗ мережі (правило меж пакета whisper_core.meeting).

Легітимна мета: офіцер має довести, що запис наради не змінювали. Журнал фіксує
життєвий цикл сесії (створено / зупинено / розшифровано-фіналізовано / розшифровано
з-під шифру / відредаговано / експортовано) з часовими мітками. Кожен запис несе
хеш попереднього (hash-chain, як у блокчейні): будь-яка підміна чи видалення запису
посередині ламає ланцюг, а ``verify_chain`` це детектує.

Формат: ``audit.jsonl`` поряд із записом — по одному JSON-об'єкту на рядок. Файл
append-only; кожен рядок дописується атомарно (flush+fsync), старі рядки ніколи не
переписуються.

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
import threading
from dataclasses import dataclass, field
from pathlib import Path

_LOG_NAME = "audit.jsonl"
_CHUNK = 1 << 20  # 1 MiB


class AuditLogCorrupt(Exception):
    """append_event відмовляється дописувати у пошкоджений журнал.

    Блокер Т56: після фікса _read_raw битий рядок повертається маркером
    {"_corrupt": True}. Мовчазний дозапис поверх пошкодження приховав би
    порушення цілісності (а events[-1]["seq"]/["hash"] на маркері кидали
    KeyError, який німо ковтав широкий except у UI → усі наступні події
    наради мовчки не дописувалися). Тому append_event на пошкодженні кидає
    цей спеціалізований виняток: фронт ловить його ОКРЕМО, чесно попереджає
    користувача й зупиняє дозапис. verify_chain однаково покаже BROKEN."""

# Типи подій — ПОРІВНЮЄМО в коді, показуємо через tr (фронт).
EVENT_CREATED = "created"        # сесію створено (старт запису)
EVENT_STOPPED = "stopped"        # запис зупинено користувачем
EVENT_FINALIZED = "finalized"    # розшифровка завершена: зафіксовано SHA аудіо+транскрипту
EVENT_DECRYPTED = "decrypted"    # сесію розшифровано (feature/meeting-encryption)
EVENT_EDITED = "edited"          # транскрипт відредаговано (з новим SHA)
EVENT_EXPORTED = "exported"      # аудіо/транскрипт експортовано назовні
EVENT_REVIEWED = "reviewed"      # цілісність підтверджено другим офіцером (принцип «чотирьох очей»)

# Статус верифікації — ПОРІВНЮЄМО в коді, показуємо через tr.
STATUS_VERIFIED = "verified"     # ланцюг цілий і всі артефакти збігаються
STATUS_BROKEN = "broken"         # підміна запису або файлу — ланцюг порушено
STATUS_ABSENT = "absent"         # журналу немає (стара нарада до цієї фічі)
STATUS_UNVERIFIED = "unverified"  # журнал є, але цілісність ще не перевіряли (лінива перевірка)

# Серіалізуємо приписи журналу один раз на шлях: append читає останній хеш, тоді
# дописує — межа гонки та сама, що в session.atomic_write_json.
_APPEND_LOCKS: dict[str, threading.Lock] = {}
_APPEND_LOCKS_GUARD = threading.Lock()


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


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _APPEND_LOCKS_GUARD:
        return _APPEND_LOCKS.setdefault(key, threading.Lock())


def append_event(session_dir, event_type: str, *, artifacts: dict = None,
                 note: object = None, ts: float = None,
                 signer=None, log_id=None, require_signature=False) -> dict:
    """Дописати подію у ``audit.jsonl`` із хешем попереднього запису.

    Читає останній запис, бере його ``hash`` за ``prev``, рахує власний ``hash``,
    дописує один рядок (flush+fsync). Повертає доданий запис.
    """
    import time
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / _LOG_NAME
    artifacts = dict(artifacts or {})
    ts = float(time.time() if ts is None else ts)
    with _lock_for(path):
        events = _read_raw(path)
        # Битий рядок у прочитаному журналі → не дописувати поверх пошкодження
        # (інакше seq/prev рахувалися б з маркера _corrupt → KeyError). Чесно
        # сигналимо фронту; verify_chain усе одно покаже BROKEN.
        if any(isinstance(e, dict) and e.get("_corrupt") for e in events):
            raise AuditLogCorrupt(
                "Журнал цілісності наради пошкоджено: нові події не "
                "дописуються, доказовий пакет покаже BROKEN")
        seq = (events[-1]["seq"] + 1) if events else 0
        prev = events[-1]["hash"] if events else ""
        digest = _record_hash(seq, event_type, ts, artifacts, note, prev)
        record = {"seq": seq, "type": event_type, "ts": ts,
                  "artifacts": artifacts, "note": note, "prev": prev, "hash": digest}
        # ── Ed25519 підпис (§7.1 спеки) ──
        # TODO(T54-wave2 §12.1): міжпроцесний lock-файл на сесію
        # (поточний _APPEND_LOCKS покриває однопроцесний випадок).
        is_signed_journal = (events and isinstance(events[0], dict)
                             and "auth" in events[0])
        if require_signature and signer is None:
            raise ValueError(
                "require_signature=True, але signer не передано")
        if is_signed_journal and signer is None:
            from .signing import SigningKeyMissing
            raise SigningKeyMissing(
                "Журнал підписаний — дописати без signer неможливо (§7.1)")
        if signer is not None:
            from .signing import sign_audit_record, new_log_id
            if log_id is None:
                if is_signed_journal:
                    # Беремо log_id з існуючого підписаного журналу
                    for ev in events:
                        auth = ev.get("auth")
                        if isinstance(auth, dict) and auth.get("log_id"):
                            log_id = auth["log_id"]
                            break
                if log_id is None:
                    log_id = new_log_id()
            auth = sign_audit_record(record, signer, log_id)
            record["auth"] = auth
        line = json.dumps(record, ensure_ascii=False) + "\n"
        encrypted = Path(str(path) + ".enc")
        encrypted_session = (session_dir / "meeting.json.enc").exists()
        if encrypted_session or (encrypted.exists() and not path.exists()):
            from .session import read_artifact, write_artifact
            try:
                previous = read_artifact(session_dir, _LOG_NAME)
            except FileNotFoundError:
                previous = b""
            write_artifact(session_dir, _LOG_NAME, previous + line.encode("utf-8"))
        else:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
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
        from .session import read_artifact
        text = read_artifact(path.parent, path.name).decode("utf-8")
    except (OSError, FileNotFoundError, UnicodeError):
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            val = json.loads(line)
            if not isinstance(val, dict):
                out.append({"_corrupt": True})
            else:
                out.append(val)
        except ValueError:
            out.append({"_corrupt": True})
    return out


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


def verify_chain(session_dir) -> ChainResult:
    """Перевірити цілісність журналу й артефактів сесії.

    1. Немає журналу → ABSENT (стара нарада; не помилка, просто без гарантії).
    2. Хеш-ланцюг: для кожного запису перерахований hash має збігтися зі
       збереженим, а ``prev`` — з hash попереднього. Перший розрив → BROKEN.
    3. Артефакти: беремо ОСТАННІЙ зафіксований SHA кожного файлу (аудіо
       фіксується раз, транскрипт — щоразу при legitimate edit) і перехешовуємо
       файл на диску. Розбіжність або зникнення → BROKEN (підміна/видалення).
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
                                  rec.get("artifacts") or {}, rec.get("note"), rec.get("prev", ""))
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

    # Ланцюг цілий — тепер звірити самі файли з останнім зафіксованим хешем.
    for rel, sha in expected_artifacts.items():
        try:
            current = _artifact_sha(session_dir, rel)
        except FileNotFoundError:
            current = None
        if current != sha:
            return ChainResult(status=STATUS_BROKEN, event_count=len(events),
                               events=events, audio_sha=_first_audio_sha(expected_artifacts))

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
                                "signed event at seq={}".format(i))
        # Старий unsigned журнал — не broken і не signed
        return ChainResult(
            status=STATUS_VERIFIED, event_count=len(events),
            audio_sha=_first_audio_sha(expected_artifacts), events=events,
            auth_status="unsigned_legacy", head_hash=head_hash)

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
                parse_error="unsigned event in signed journal at seq={}".format(i))

        # log_id consistency
        ev_log_id = auth.get("log_id", "")
        if i == 0:
            log_id = ev_log_id
        elif ev_log_id != log_id:
            return ChainResult(
                status=STATUS_BROKEN, event_count=len(events),
                broken_seq=rec.get("seq", i), events=events,
                auth_status="signed_invalid",
                parse_error="log_id mismatch at seq={}".format(i))

        result = verify_audit_record(rec, key_resolver)
        seen_key_ids.add(auth.get("key_id", ""))
        if not result.valid:
            if result.error == "key not found":
                return ChainResult(
                    status=STATUS_BROKEN, event_count=len(events),
                    broken_seq=rec.get("seq", i), events=events,
                    auth_status="signed_key_missing",
                    log_id=log_id, key_ids=sorted(seen_key_ids),
                    head_hash=head_hash)
            return ChainResult(
                status=STATUS_BROKEN, event_count=len(events),
                broken_seq=rec.get("seq", i), events=events,
                auth_status="signed_invalid",
                log_id=log_id, key_ids=sorted(seen_key_ids),
                head_hash=head_hash,
                parse_error=result.error)

    return ChainResult(
        status=STATUS_VERIFIED, event_count=len(events),
        audio_sha=_first_audio_sha(expected_artifacts), events=events,
        auth_status="signed_valid",
        log_id=log_id, key_ids=sorted(seen_key_ids),
        head_hash=head_hash)
