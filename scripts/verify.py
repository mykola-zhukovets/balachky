#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Балачки — незалежний перевіряч цілісності запису наради.

ЦЕЙ ФАЙЛ САМОДОСТАТНІЙ. Він не потребує встановлених «Балачок».
Залежності: Python 3, cryptography (для Ed25519-підписів).

Як запустити (у теці з файлами наради):
    python verify.py .
    python verify.py C:\\шлях\\до\\теки-наради
    python verify.py C:\\шлях\\до\\audit.jsonl
    python verify.py . --expect-key-id sha256:<64hex>
    python verify.py . --trusted-key commander-public-key.json

Коди виходу:
    0 — усе ціле, підпис валідний, ключ підтверджений
    1 — порушено журнал, файл, manifest, підпис
    2 — журналу немає або аргумент непридатний
    3 — підпис математично валідний, але ключ не підтверджено
    4 — валідний старий hash-журнал без Ed25519
    5 — немає cryptography або алгоритм недоступний
    6 — ви заявили очікуваний ключ (--expect-key-id / --trusted-key),
        а журнал непідписаний: підпису під цим ключем НЕМАЄ
"""
import hashlib
import json
import sys
from pathlib import Path

_LOG_NAME = "audit.jsonl"
_CHUNK = 1 << 20  # 1 МіБ
_SIGNING_PREFIX = b"Balachky audit event v1\x00"
_EVIDENCE_PREFIX = b"Balachky evidence manifest v2\x00"

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_ABSENT = 2
EXIT_UNTRUSTED = 3
EXIT_LEGACY = 4
EXIT_NO_CRYPTO = 5
EXIT_SIGNATURE_EXPECTED = 6


def sha256_of_file(path, chunk: int = _CHUNK) -> str:
    """SHA-256 файлу потоково."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def record_hash(seq, event_type, ts, artifacts, note, prev) -> str:
    """SHA-256 канонічного вмісту запису — дзеркало audit_log._record_hash."""
    content = {"seq": seq, "type": event_type, "ts": ts,
               "artifacts": artifacts, "note": note, "prev": prev}
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_json(obj: dict) -> bytes:
    """Канонічний JSON для підпису."""
    return json.dumps(
        obj, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("ascii")


def _b64decode(s: str) -> bytes:
    import base64
    return base64.b64decode(s.encode("ascii"), validate=True)


def _compute_key_id(pub_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(pub_bytes).hexdigest()


def read_events(log_path: Path):
    """Розпарсити audit.jsonl → список записів. Битий рядок = _corrupt."""
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
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


def _extract_public_keys(events):
    """Витягти публічні ключі з подій журналу: {key_id: raw_bytes}."""
    keys = {}
    for ev in events:
        auth = ev.get("auth")
        if not isinstance(auth, dict):
            continue
        key_id = auth.get("key_id", "")
        pub_b64 = auth.get("public_key", "")
        if key_id and pub_b64:
            try:
                pub_bytes = _b64decode(pub_b64)
                if len(pub_bytes) == 32 and _compute_key_id(pub_bytes) == key_id:
                    keys[key_id] = pub_bytes
            except (ValueError, TypeError):
                pass
    return keys


def _verify_signature(pub_bytes, signature, message):
    """Перевірити Ed25519-підпис. Повертає True/False."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        public_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        public_key.verify(signature, message)
        return True
    except ImportError:
        raise
    except Exception:
        return False


def _check_crypto():
    """Перевірити доступність cryptography."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        return Ed25519PublicKey is not None
    except ImportError:
        return False


def verify(session_dir: Path, log_path: Path, *,
           expect_key_id=None, trusted_keys=None):
    """Повернути (status, details).

    status: 'verified'|'broken'|'absent'|'untrusted'|'legacy'|'no_crypto'
            |'signature_expected'
    """
    events = read_events(log_path)
    if not events:
        return "absent", {"event_count": 0}

    # ── 1. Hash-ланцюг ──
    expected_artifacts = {}
    prev = ""
    for i, rec in enumerate(events):
        if (not isinstance(rec, dict) or rec.get("_corrupt")
                or "seq" not in rec or "type" not in rec or "ts" not in rec):
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": (rec.get("seq") if isinstance(rec, dict) else None),
                "reason": "malformed"}
        # seq має точно дорівнювати індексу рядка (§7.2): жодних пропусків,
        # дублікатів чи перестановок seq.
        if not isinstance(rec["seq"], int) or isinstance(rec["seq"], bool) \
                or rec["seq"] != i:
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec.get("seq"),
                "reason": "seq_index_mismatch"}
        digest = record_hash(rec["seq"], rec["type"], rec["ts"],
                             rec.get("artifacts") or {}, rec.get("note"),
                             rec.get("prev", ""))
        if digest != rec.get("hash") or rec.get("prev", "") != prev:
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec["seq"], "broken_type": rec["type"],
                "expected_hash": digest, "actual_hash": rec.get("hash"),
                "reason": "chain"}
        prev = rec["hash"]
        for rel, sha in (rec.get("artifacts") or {}).items():
            expected_artifacts[rel] = sha

    # ── 2. Артефакти ──
    for rel, sha in sorted(expected_artifacts.items()):
        p = session_dir / rel
        if not p.is_file():
            return "broken", {
                "event_count": len(events), "bad_file": rel,
                "expected_file_sha": sha, "actual_file_sha": None,
                "reason": "file_missing"}
        actual = sha256_of_file(p)
        if actual != sha:
            return "broken", {
                "event_count": len(events), "bad_file": rel,
                "expected_file_sha": sha, "actual_file_sha": actual,
                "reason": "file_changed"}

    # ── 3. Ed25519 підписи ──
    first_auth = events[0].get("auth") if events else None
    is_signed = isinstance(first_auth, dict)

    if not is_signed:
        # ЗМІШАНИЙ ЖУРНАЛ (§7.2, follow-up крипто-суду): нульова подія без
        # ``auth``, а пізніші події підписані. Справжній legacy-журнал таким
        # бути не може — це атака «зняти auth лише з events[0]», щоб згасити
        # сигнал «журнал підписаний» і отримати м'який код 4 замість BROKEN.
        for i, rec in enumerate(events[1:], start=1):
            if rec.get("auth") is not None:
                return "broken", {
                    "event_count": len(events), "broken_index": i,
                    "broken_seq": rec.get("seq", i),
                    "reason": "mixed_auth_journal"}
        # Заявлений очікуваний ключ + непідписаний журнал = ВІДМОВА, не «legacy».
        # Той, хто вказав --expect-key-id / --trusted-key, стверджує «тут має
        # бути підпис ключем X». Чесна відповідь — «підпису немає», окремим
        # ненульовим кодом 6.
        if expect_key_id or trusted_keys:
            return "signature_expected", {
                "event_count": len(events),
                "expected_key_ids": ([expect_key_id] if expect_key_id
                                     else sorted(trusted_keys.keys())),
                "reason": "journal_unsigned_but_key_expected"}
        return "legacy", {"event_count": len(events)}

    # Потрібна cryptography
    if not _check_crypto():
        return "no_crypto", {
            "event_count": len(events),
            "reason": "cryptography library unavailable"}

    known_keys = _extract_public_keys(events)
    if trusted_keys:
        known_keys.update(trusted_keys)
    log_id = ""
    seen_key_ids = set()

    for i, rec in enumerate(events):
        auth = rec.get("auth")
        if not isinstance(auth, dict):
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec.get("seq", i),
                "reason": "unsigned_in_signed_journal"}

        ev_log_id = auth.get("log_id", "")
        if i == 0:
            log_id = ev_log_id
        elif ev_log_id != log_id:
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec.get("seq", i),
                "reason": "log_id_mismatch"}

        key_id = auth.get("key_id", "")
        seen_key_ids.add(key_id)

        try:
            signature = _b64decode(auth.get("signature", ""))
        except (ValueError, TypeError):
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec.get("seq", i),
                "reason": "invalid_signature_encoding"}

        if len(signature) != 64:
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec.get("seq", i),
                "reason": "invalid_signature_length"}

        pub_bytes = known_keys.get(key_id)
        if pub_bytes is None:
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec.get("seq", i),
                "reason": "key_not_found",
                "key_id": key_id}

        body = {
            "algorithm": "Ed25519",
            "hash": rec["hash"],
            "key_id": key_id,
            "log_id": ev_log_id,
            "seq": rec["seq"],
            "version": 1,
        }
        message = _SIGNING_PREFIX + _canonical_json(body)

        if not _verify_signature(pub_bytes, signature, message):
            return "broken", {
                "event_count": len(events), "broken_index": i,
                "broken_seq": rec.get("seq", i),
                "reason": "invalid_signature"}

    # Підписи валідні — перевіряємо trust
    is_trusted = False
    if expect_key_id:
        is_trusted = expect_key_id in seen_key_ids
    elif trusted_keys:
        is_trusted = bool(seen_key_ids & set(trusted_keys.keys()))

    if is_trusted:
        return "verified", {
            "event_count": len(events), "log_id": log_id,
            "key_ids": sorted(seen_key_ids)}
    else:
        return "untrusted", {
            "event_count": len(events), "log_id": log_id,
            "key_ids": sorted(seen_key_ids),
            "reason": "key not independently confirmed"}


def verify_evidence(evidence_dir: Path, *,
                    expect_key_id=None, trusted_keys=None):
    """Перевірити evidence-пакет format 2 (§8 спеки)."""
    manifest_path = evidence_dir / "evidence.json"
    if not manifest_path.is_file():
        # Немає manifest — fallback до звичайної перевірки журналу
        return verify(evidence_dir, evidence_dir / _LOG_NAME,
                      expect_key_id=expect_key_id, trusted_keys=trusted_keys)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return "broken", {"reason": "invalid evidence.json"}

    if manifest.get("kind") != "balachky-evidence" or manifest.get("format") != 2:
        return "broken", {"reason": "unsupported evidence format"}

    # 1. Перевірити файли manifest
    for finfo in manifest.get("files", []):
        fpath = evidence_dir / finfo["path"]
        if not fpath.is_file():
            return "broken", {
                "reason": "manifest_file_missing",
                "bad_file": finfo["path"]}
        actual_sha = sha256_of_file(fpath)
        if actual_sha != finfo.get("sha256"):
            return "broken", {
                "reason": "manifest_file_changed",
                "bad_file": finfo["path"],
                "expected": finfo.get("sha256"),
                "actual": actual_sha}
        actual_size = fpath.stat().st_size
        if actual_size != finfo.get("size"):
            return "broken", {
                "reason": "manifest_file_size",
                "bad_file": finfo["path"]}

    # 2. Перевірити підпис manifest.
    #
    # КРИТИЧНО (блокер крипто-суду): поле signer.algorithm КОНТРОЛЮЄ АТАКЕР —
    # воно НЕ сміє вирішувати, чи взагалі перевіряти підпис. Раніше блок
    # перевірки виконувався лише коли manifest сам оголошував
    # algorithm == "Ed25519"; атакер без приватного ключа прибирав signer
    # (або ставив "none"/інший регістр) → підпис не перевірявся → verify.py
    # видавав VERIFIED на усіченому/розпідписаному журналі (атаки A7, A9).
    #
    # Прив'язка до самого журналу: якщо журнал у пакеті підписаний (events[0]
    # має auth) АБО manifest оголошує journal_auth="signed_events" АБО присутній
    # блок signer — підпис manifest ОБОВ'ЯЗКОВИЙ і має бути валідним Ed25519 із
    # валідним key_id. Інакше — BROKEN, не verified і не legacy (§8.1, §14.4,
    # критерії приймання 3/4/8).
    log_path = evidence_dir / _LOG_NAME
    journal_events = read_events(log_path)
    journal_signed = (bool(journal_events)
                      and isinstance(journal_events[0].get("auth"), dict))

    signer_info = manifest.get("signer")
    signer_present = (isinstance(signer_info, dict)
                      and signer_info.get("algorithm") is not None)
    manifest_claims_signed = (manifest.get("journal_auth") == "signed_events")

    must_verify_manifest = (
        journal_signed or manifest_claims_signed or signer_present)

    if must_verify_manifest:
        # Алгоритм — регістронезалежно рівно "ed25519"; будь-що інше (відсутній,
        # "none", неочікуваний) при підписаному/оголошено-підписаному пакеті =
        # BROKEN. Атакер не може вимкнути перевірку підміною цього поля.
        algorithm = signer_info.get("algorithm") if isinstance(
            signer_info, dict) else None
        if not isinstance(algorithm, str) or algorithm.strip().lower() != "ed25519":
            return "broken", {
                "reason": "manifest_signature_required",
                "journal_signed": journal_signed,
                "algorithm": algorithm}

        if not _check_crypto():
            return "no_crypto", {"reason": "cryptography library unavailable"}

        # Знайти публічний ключ
        manifest_key_id = signer_info.get("key_id", "")
        pub_bytes = None

        # З PUBLIC-KEYS.json
        pk_path = evidence_dir / "PUBLIC-KEYS.json"
        if pk_path.is_file():
            try:
                pk_data = json.loads(pk_path.read_text(encoding="utf-8"))
                for entry in pk_data.get("keys", []):
                    if entry.get("key_id") == manifest_key_id:
                        pub_bytes = _b64decode(entry["public_key"])
                        break
            except (OSError, json.JSONDecodeError, ValueError, KeyError):
                pass

        # З trusted keys
        if pub_bytes is None and trusted_keys:
            pub_bytes = trusted_keys.get(manifest_key_id)

        if pub_bytes is None:
            return "broken", {
                "reason": "manifest_signer_key_not_found",
                "key_id": manifest_key_id}

        # Перевірити підпис
        try:
            signature = _b64decode(manifest.get("signature", ""))
            body = {k: v for k, v in manifest.items() if k != "signature"}
            message = _EVIDENCE_PREFIX + _canonical_json(body)
            if not _verify_signature(pub_bytes, signature, message):
                return "broken", {"reason": "invalid_manifest_signature"}
        except (ValueError, TypeError):
            return "broken", {"reason": "invalid_manifest_signature_encoding"}

        # Крос-звірка узгодженості: валідно підписаний manifest, що оголошує
        # signed_events, МУСИТЬ пиновати підписаний журнал; і навпаки —
        # підписаний журнал МУСИТЬ бути оголошений signed_events. Розбіжність =
        # спроба тихого переходу signed→unsigned (атака A9).
        if manifest_claims_signed and not journal_signed:
            return "broken", {"reason": "manifest_signed_journal_unsigned"}
        if journal_signed and not manifest_claims_signed:
            return "broken", {"reason": "manifest_journal_auth_mismatch"}

    # 3. Перевірити журнал у пакеті
    journal_status, journal_details = verify(
        evidence_dir, log_path,
        expect_key_id=expect_key_id, trusted_keys=trusted_keys)

    if journal_status == "broken":
        return journal_status, journal_details

    # 4. Перевірити голову журналу vs manifest
    head = manifest.get("head", {})
    events = journal_events
    if events:
        last = events[-1]
        if (last.get("seq") != head.get("seq")
                or last.get("hash") != head.get("hash")):
            return "broken", {
                "reason": "manifest_head_mismatch",
                "manifest_seq": head.get("seq"),
                "journal_seq": last.get("seq")}
    if manifest.get("event_count") != len(events):
        return "broken", {
            "reason": "manifest_event_count_mismatch",
            "manifest_count": manifest.get("event_count"),
            "journal_count": len(events)}

    return journal_status, journal_details


def _resolve_paths(arg: str):
    """arg → (session_dir, log_path)."""
    p = Path(arg)
    if p.is_dir():
        return p, p / _LOG_NAME
    if p.name == _LOG_NAME:
        return p.parent, p
    return p.parent, p


def _load_trusted_key_file(path_str):
    """Завантажити публічний ключ із JSON-файла."""
    try:
        data = json.loads(Path(path_str).read_text(encoding="utf-8"))
        keys = {}
        if isinstance(data, dict):
            if "keys" in data:
                for entry in data["keys"]:
                    kid = entry.get("key_id", "")
                    pub = entry.get("public_key", "")
                    if kid and pub:
                        keys[kid] = _b64decode(pub)
            elif "key_id" in data and "public_key" in data:
                keys[data["key_id"]] = _b64decode(data["public_key"])
        return keys
    except Exception as exc:
        print("Помилка читання файла ключа: {}".format(exc), file=sys.stderr)
        return {}


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Балачки — перевірка цілісності запису наради",
        epilog=("Коди виходу: 0 ціле й ключ підтверджено; 1 порушено; "
                "2 немає журналу; 3 підпис валідний, ключ не підтверджено; "
                "4 старий журнал без підпису; 5 немає cryptography; "
                "6 ви заявили очікуваний ключ, а журнал непідписаний."))
    parser.add_argument("target", nargs="?", default=".",
                        help="Тека наради або шлях до audit.jsonl")
    parser.add_argument("--expect-key-id", default=None,
                        help=("Очікуваний key_id (sha256:<64hex>). "
                              "Якщо журнал непідписаний — код 6 (відмова), "
                              "а не код 4"))
    parser.add_argument("--trusted-key", default=None,
                        help=("JSON-файл з публічним ключем. Так само: "
                              "непідписаний журнал → код 6 (відмова)"))
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    session_dir, log_path = _resolve_paths(args.target)
    trusted_keys = None
    if args.trusted_key:
        trusted_keys = _load_trusted_key_file(args.trusted_key)

    print("Балачки — перевірка цілісності запису наради")
    print("Тека: {}".format(session_dir.resolve()))
    print("Журнал: {}".format(log_path.name))

    # Якщо є evidence.json — перевіряємо evidence-пакет
    evidence_json = session_dir / "evidence.json"
    if evidence_json.is_file():
        status, d = verify_evidence(
            session_dir, expect_key_id=args.expect_key_id,
            trusted_keys=trusted_keys)
    else:
        status, d = verify(
            session_dir, log_path, expect_key_id=args.expect_key_id,
            trusted_keys=trusted_keys)

    print("Подій у журналі: {}".format(d.get("event_count", 0)))
    print("")

    if status == "verified":
        key_ids = d.get("key_ids", [])
        print("VERIFIED — журнал цілий, підписи валідні, ключ підтверджений.")
        if key_ids:
            print("Ключі: {}".format(", ".join(key_ids)))
        return EXIT_OK

    if status == "untrusted":
        key_ids = d.get("key_ids", [])
        print("SIGNATURE VALID — підписи математично валідні.")
        print("УВАГА: ключ не підтверджено незалежним каналом.")
        if key_ids:
            print("Ключі: {}".format(", ".join(key_ids)))
        print("Для повної перевірки вкажіть --expect-key-id або --trusted-key.")
        return EXIT_UNTRUSTED

    if status == "signature_expected":
        expected = d.get("expected_key_ids") or []
        print("ПІДПИСУ НЕМАЄ — ви заявили очікуваний ключ, а журнал непідписаний.")
        if expected:
            print("Очікувався ключ: {}".format(", ".join(expected)))
        print("Хеш-ланцюг цілий, але Ed25519-підпису в журналі немає взагалі.")
        print("Це або справді стара нарада (тоді запускайте без --expect-key-id")
        print("та --trusted-key), або з пакета зняли всі підписи.")
        print("Підпис заявленим ключем НЕ підтверджено.")
        return EXIT_SIGNATURE_EXPECTED

    if status == "legacy":
        print("UNSIGNED LEGACY — валідний hash-журнал без Ed25519-підписів.")
        print("Це стара нарада; хеш-ланцюг цілий, але криптографічного")
        print("підпису немає.")
        return EXIT_LEGACY

    if status == "absent":
        print("НЕМАЄ ЖУРНАЛУ — у цій теці немає {}.".format(_LOG_NAME))
        print("Це стара нарада без журналу цілісності або невірна тека.")
        return EXIT_ABSENT

    if status == "no_crypto":
        print("CRYPTOGRAPHY UNAVAILABLE — бібліотека cryptography не встановлена.")
        print("Встановіть: pip install 'cryptography>=44,<47'")
        print("Або див. requirements-verify.txt у пакеті.")
        return EXIT_NO_CRYPTO

    # broken
    reason = d.get("reason", "")
    print("BROKEN — цілісність ПОРУШЕНО.")
    if reason in ("chain", "malformed"):
        print("Перший зламаний запис: №{} (порядковий seq={}).".format(
            d.get("broken_index"), d.get("broken_seq")))
        if d.get("broken_type"):
            print("  тип події: {}".format(d.get("broken_type")))
        if d.get("expected_hash") is not None:
            print("  очікуваний хеш: {}".format(d.get("expected_hash")))
            print("  фактичний хеш:  {}".format(d.get("actual_hash")))
        else:
            print("  запис пошкоджено (немає обов'язкових полів).")
    elif reason == "file_missing":
        print("Файл наради зник: {}".format(d.get("bad_file")))
        print("  очікуваний код SHA-256: {}".format(d.get("expected_file_sha")))
    elif reason == "file_changed":
        print("Файл наради змінили: {}".format(d.get("bad_file")))
        print("  очікуваний код SHA-256: {}".format(d.get("expected_file_sha")))
        print("  фактичний код SHA-256:  {}".format(d.get("actual_file_sha")))
    elif reason == "invalid_signature":
        print("Недійсний Ed25519-підпис запису seq={}.".format(
            d.get("broken_seq")))
    elif reason == "unsigned_in_signed_journal":
        print("Непідписана подія в підписаному журналі: seq={}.".format(
            d.get("broken_seq")))
    elif reason in ("invalid_manifest_signature", "invalid_manifest_signature_encoding"):
        print("Недійсний підпис evidence.json.")
    elif reason == "manifest_signature_required":
        print("Пакет містить підписаний журнал, але evidence.json НЕ підписаний")
        print("валідним Ed25519 (підпис прибрано або підмінено алгоритм).")
        print("Це спроба обійти перевірку підпису — доказовість НЕ підтверджено.")
    elif reason == "manifest_signed_journal_unsigned":
        print("evidence.json оголошує підписаний журнал, але події журналу")
        print("не мають підписів — журнал розпідписали після експорту.")
    elif reason == "manifest_journal_auth_mismatch":
        print("Журнал підписаний, але evidence.json це приховує")
        print("(journal_auth не 'signed_events').")
    elif reason == "seq_index_mismatch":
        print("Порядковий seq не збігається з позицією запису: №{} seq={}.".format(
            d.get("broken_index"), d.get("broken_seq")))
    elif reason == "manifest_head_mismatch":
        print("Голова журналу не збігається з manifest.")
        print("  manifest seq={}, журнал seq={}".format(
            d.get("manifest_seq"), d.get("journal_seq")))
    elif reason == "manifest_event_count_mismatch":
        print("Кількість подій не збігається з manifest.")
    elif reason in ("manifest_file_missing", "manifest_file_changed", "manifest_file_size"):
        print("Файл пакета не збігається з manifest: {}".format(d.get("bad_file")))
    elif reason == "key_not_found":
        print("Публічний ключ не знайдено: {}".format(d.get("key_id")))
    elif reason == "mixed_auth_journal":
        print("Змішаний журнал: перша подія без підпису, а подія seq={}".format(
            d.get("broken_seq")))
        print("підписана. Справжній старий журнал таким бути не може —")
        print("підпис зняли з першої події, щоб приховати, що журнал підписаний.")
    elif reason == "log_id_mismatch":
        print("log_id не збігається між подіями: seq={}.".format(
            d.get("broken_seq")))
    else:
        print("Причина: {}".format(reason))
    return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
