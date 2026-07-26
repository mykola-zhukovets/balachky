"""Доказовий пакет наради (evidence export) — zip для передачі комісії/слідчому.

БЕЗ Qt, БЕЗ мережі (правило меж пакета whisper_core.meeting). Складає самодостатній
zip: файли наради + журнал цілісності ``audit.jsonl`` + незалежний перевіряч
``verify.py`` + людино-читний ``REPORT.txt`` (українською) + evidence.json manifest
(format 2 з Ed25519-підписом) + PUBLIC-KEYS.json + requirements-verify.txt.

Легітимна мета: офіцер має віддати на розслідування пакет, який приймають як доказ
(маніфест із хешами + статус ланцюга + хто зафіксував), а не сирий набір файлів.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import audit_log

_VERIFIER_NAME = "verify.py"
_REPORT_NAME = "REPORT.txt"
_EVIDENCE_JSON = "evidence.json"
_PUBLIC_KEYS_JSON = "PUBLIC-KEYS.json"
_REQUIREMENTS_VERIFY = "requirements-verify.txt"

# Людські назви подій для звіту (модуль whisper_core — без i18n фронта; звіт
# українською як юридичний документ для комісії).
_EVENT_LABELS = {
    audit_log.EVENT_CREATED: "Створено запис",
    audit_log.EVENT_STOPPED: "Зупинено запис",
    audit_log.EVENT_FINALIZED: "Розшифровано (зафіксовано коди аудіо й тексту)",
    audit_log.EVENT_DECRYPTED: "Розшифровано із захищеного сховища",
    audit_log.EVENT_EDITED: "Змінено текст (зафіксовано новий код)",
    audit_log.EVENT_EXPORTED: "Експортовано назовні",
    audit_log.EVENT_REVIEWED: "Перегляд підтверджено",
}

_STATUS_LABELS = {
    audit_log.STATUS_VERIFIED: "ПІДТВЕРДЖЕНО",
    audit_log.STATUS_BROKEN: "ПОРУШЕНО",
    audit_log.STATUS_ABSENT: "ЖУРНАЛУ НЕМАЄ",
    audit_log.STATUS_UNVERIFIED: "НЕ ПЕРЕВІРЕНО",
}

_STATUS_EXPLAIN = {
    audit_log.STATUS_VERIFIED: "Журнал цілий і всі файли збігаються — запис не змінювали після фіксації.",
    audit_log.STATUS_BROKEN: "Виявлено підміну запису журналу або зміну/зникнення файлу наради.",
    audit_log.STATUS_ABSENT: "Журналу цілісності немає — перевірити нічим (стара нарада).",
    audit_log.STATUS_UNVERIFIED: "Журнал є, але повну цілісність не перевіряли.",
}


@dataclass
class EvidencePackage:
    out_zip: Path
    status: str                # STATUS_* із verify_chain
    file_count: int            # скільки файлів наради вкладено
    has_verifier: bool         # чи вклали verify.py
    event_count: int = 0
    auth_status: str = ""      # signed_valid | unsigned_legacy
    log_id: str = ""


def verifier_source() -> "Path | None":
    """Знайти ``scripts/verify.py`` — у dev-дереві або у frozen-збірці (sys._MEIPASS)."""
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "scripts" / _VERIFIER_NAME)
    candidates.append(Path(__file__).resolve().parents[2] / "scripts" / _VERIFIER_NAME)
    for c in candidates:
        if c.is_file():
            return c
    return None


def _session_files(session_dir: Path) -> dict:
    """Усі файли теки сесії (рекурсивно): relpath(posix) → абсолютний шлях.
    Наші доважки і evidence-артефакти не включаємо — їх додаємо окремо."""
    exclude = {_VERIFIER_NAME, _REPORT_NAME, _EVIDENCE_JSON,
               _PUBLIC_KEYS_JSON, _REQUIREMENTS_VERIFY}
    out: dict[str, Path] = {}
    for p in sorted(session_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(session_dir).as_posix()
        if rel in exclude:
            continue
        out[rel] = p
    return out


def _who(events) -> tuple:
    """Витягти з подій «хто зафіксував» (created.note.recorded_by) і список
    переглядів (reviewed): [(ts, reviewer), ...]."""
    recorded_by = ""
    reviews = []
    for ev in events:
        note = ev.get("note")
        if not isinstance(note, dict):
            continue
        if ev.get("type") == audit_log.EVENT_CREATED and note.get("recorded_by"):
            recorded_by = str(note["recorded_by"])
        if ev.get("type") == audit_log.EVENT_REVIEWED:
            who = note.get("reviewer") or note.get("recorded_by") or ""
            reviews.append((ev.get("ts", 0), str(who)))
    return recorded_by, reviews


def _stamp(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts or 0)))
    except (TypeError, ValueError, OSError):
        return "?"


def build_report(session_dir, result, files: dict, *, app_version: str = "") -> str:
    """Людино-читний REPORT.txt (українською) за результатом verify_chain."""
    session_dir = Path(session_dir)
    status = result.status
    events = list(getattr(result, "events", []) or [])
    recorded_by, reviews = _who(events)
    auth_status = getattr(result, "auth_status", "")

    lines = []
    lines.append("БАЛАЧКИ — ДОКАЗОВИЙ ПАКЕТ НАРАДИ")
    lines.append("=" * 48)
    lines.append("")
    lines.append("Нарада (тека): {}".format(session_dir.name))
    lines.append("Пакет сформовано: {}".format(_stamp(time.time())))
    lines.append("Версія застосунку: {}".format(app_version or "—"))
    lines.append("")
    lines.append("СТАТУС ЦІЛІСНОСТІ: {}".format(_STATUS_LABELS.get(status, status)))
    lines.append(_STATUS_EXPLAIN.get(status, ""))
    if auth_status == "signed_valid":
        lines.append("Криптографічний підпис: ВАЛІДНИЙ (Ed25519).")
    elif auth_status == "unsigned_legacy":
        lines.append("Криптографічний підпис: ВІДСУТНІЙ (legacy-журнал).")
    lines.append("")
    lines.append("ХТО ЗАФІКСУВАВ / ПЕРЕГЛЯНУВ")
    lines.append("-" * 48)
    lines.append("Зафіксував: {}".format(recorded_by or "не вказано"))
    if reviews:
        lines.append("Підтвердження перегляду (принцип \"чотирьох очей\"):")
        for ts, who in reviews:
            lines.append("  [{}] {}".format(_stamp(ts), who or "без імені"))
    else:
        lines.append("Підтверджень перегляду немає.")
    lines.append("")
    lines.append("ПОДІЇ ЖУРНАЛУ (що і коли)")
    lines.append("-" * 48)
    if events:
        for ev in events:
            label = _EVENT_LABELS.get(ev.get("type"), ev.get("type", ""))
            lines.append("  [{}] {}".format(_stamp(ev.get("ts")), label))
    else:
        lines.append("  журналу немає")
    lines.append("")
    lines.append("ФАЙЛИ НАРАДИ ТА ЇХНІ КОДИ SHA-256")
    lines.append("-" * 48)
    width = max((len(rel) for rel in files), default=0)
    for rel in sorted(files):
        try:
            sha = audit_log.sha256_of(files[rel])
        except OSError:
            sha = "<не вдалося прочитати>"
        lines.append("  {}  {}".format(rel.ljust(width), sha))
    lines.append("")
    # §11, §15 спеки: обмеження
    lines.append("ОБМЕЖЕННЯ ТА ЗАСТЕРЕЖЕННЯ")
    lines.append("-" * 48)
    lines.append("Час зі системного годинника пристрою; зовнішньо не засвідчено.")
    lines.append("DPAPI не є доказом апаратної ізоляції.")
    lines.append("Підпис не захищає від active malware у розблокованій Windows-сесії.")
    lines.append("Rollback до старішого стану можливий без зовнішнього checkpoint.")
    lines.append("Підпис не доводить, що пакет узагалі підписували: без зовнішнього якоря")
    lines.append("(незалежно збережений key_id або голова журналу) сторона, яка тримає")
    lines.append("пакет, може подати повністю розпідписану копію — усі підписи зняті,")
    lines.append("хеш-ланцюг цілий — і вона виглядатиме як стара нарада без підпису.")
    lines.append("Тому звіряйте key_id з незалежного джерела")
    lines.append("(команда: python {} . --expect-key-id <ключ>).".format(_VERIFIER_NAME))
    lines.append("")
    lines.append("ЯК ПЕРЕВІРИТИ САМОСТІЙНО (без встановлення Балачок)")
    lines.append("-" * 48)
    lines.append("1. Встановіть Python 3 (python.org), якщо його ще немає.")
    lines.append("2. Встановіть залежність: pip install -r requirements-verify.txt")
    lines.append("3. Розпакуйте цей пакет і відкрийте його теку в терміналі.")
    lines.append("4. Виконайте команду:  python {} .".format(_VERIFIER_NAME))
    lines.append("")
    lines.append("Перевіряч звірить журнал, підписи й файли.")
    lines.append("Він читає лише коди й дати — не розкриває вміст запису.")
    lines.append("")
    return "\n".join(lines)


def _build_manifest(result, events, manifest_files, *,
                    app_version: str = "", signer=None) -> dict:
    """Побудувати (і за наявності signer підписати) evidence.json format 2 (§8.1)."""
    auth_status = getattr(result, "auth_status", "")
    log_id_val = getattr(result, "log_id", "") or ""
    head_hash_val = getattr(result, "head_hash", "") or ""
    head_seq = events[-1]["seq"] if events else -1
    journal_auth = ("signed_events" if auth_status == "signed_valid"
                    else "legacy_unsigned")

    manifest = {
        "kind": "balachky-evidence",
        "format": 2,
        "journal_auth": journal_auth,
        "log_id": log_id_val,
        "event_count": len(events),
        "head": {
            "seq": head_seq,
            "hash": head_hash_val or (events[-1]["hash"] if events else ""),
        },
        "created_at": time.time(),
        "app_version": app_version or "",
    }
    if signer is not None:
        manifest["signer"] = {"algorithm": "Ed25519", "key_id": signer.key_id}
    manifest["files"] = manifest_files
    if signer is not None:
        from .signing import sign_evidence_manifest
        manifest["signature"] = sign_evidence_manifest(manifest, signer)
    else:
        manifest["signature"] = ""
    return manifest


def export_evidence(session_dir, out_zip, *, app_version: str = "",
                    signer=None, meetings_root=None) -> EvidencePackage:
    """Скласти доказовий zip-пакет наради у ``out_zip``.

    Вкладає: (а) усі файли наради; (б) журнал ``audit.jsonl``;
    (в) незалежний ``verify.py``; (г) людино-читний ``REPORT.txt``;
    (д) ``evidence.json`` manifest format 2; (е) ``PUBLIC-KEYS.json``;
    (є) ``requirements-verify.txt``.

    Manifest (format 2) фіксує точну голову журналу та SHA-256 усіх вкладень;
    за наявності ``signer`` він підписаний Ed25519 (§8.1). ZIP пишеться атомарно
    через temp-файл у теці призначення + ``os.replace`` (§8.3).
    """
    session_dir = Path(session_dir)
    out_zip = Path(out_zip)
    if not session_dir.is_dir():
        raise FileNotFoundError("Теки наради немає: {}".format(session_dir))

    # Encrypted session: materialize for ZIP construction. Ключ береться з
    # оригінального meetings_root, а не з тимчасової теки (§8.3).
    if (session_dir / "meeting.json.enc").is_file():
        from .session import materialize_session
        with materialize_session(session_dir) as plain_session:
            return export_evidence(
                plain_session, out_zip, app_version=app_version,
                signer=signer, meetings_root=meetings_root)

    result = audit_log.verify_chain(session_dir)
    files = _session_files(session_dir)
    report = build_report(session_dir, result, files, app_version=app_version)
    verifier = verifier_source()
    events = list(getattr(result, "events", []) or [])

    public_keys_json = ""
    if events:
        from .signing import build_public_keys_json
        public_keys_json = build_public_keys_json(events)
    requirements_verify = "cryptography>=44,<47\n"

    # Файли сесії лишаються потоковими з диска (WAV наради буває великий).
    disk_files = sorted(files.items())              # [(rel, Path), ...]
    # Наші доважки маленькі — тримаємо в пам'яті.
    mem_files: "list[tuple[str, bytes]]" = []
    if verifier is not None:
        mem_files.append((_VERIFIER_NAME, verifier.read_bytes()))
    mem_files.append((_REPORT_NAME, report.encode("utf-8")))
    if public_keys_json:
        mem_files.append((_PUBLIC_KEYS_JSON, public_keys_json.encode("utf-8")))
    mem_files.append((_REQUIREMENTS_VERIFY, requirements_verify.encode("utf-8")))

    # Manifest перелічує всі файли пакета крім самого evidence.json.
    manifest_files = []
    for rel, path in disk_files:
        manifest_files.append({
            "path": rel,
            "size": path.stat().st_size,
            "sha256": audit_log.sha256_of(path),
        })
    for arcname, data in mem_files:
        manifest_files.append({
            "path": arcname,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    manifest = _build_manifest(result, events, manifest_files,
                               app_version=app_version, signer=signer)
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, indent=2).encode("utf-8")

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=out_zip.name + ".", suffix=".tmp", dir=out_zip.parent)
    try:
        with os.fdopen(fd, "wb") as raw:
            with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
                for rel, path in disk_files:
                    z.write(path, rel)
                for arcname, data in mem_files:
                    z.writestr(arcname, data)
                z.writestr(_EVIDENCE_JSON, manifest_bytes)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp_name, out_zip)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return EvidencePackage(
        out_zip=out_zip, status=result.status, file_count=len(files),
        has_verifier=verifier is not None, event_count=result.event_count,
        auth_status=getattr(result, "auth_status", ""),
        log_id=getattr(result, "log_id", "") or "")
