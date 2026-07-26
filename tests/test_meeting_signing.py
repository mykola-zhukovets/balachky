"""feature/ed25519-journal: Ed25519-підпис журналу chain-of-custody.

Червоний перелік спеки _SPEC-ed25519.md §13.2 (ядро — критерії приймання
1-8, 10, 14). Ротація §5.6, крипто-«4 очі» §10, identity transfer §6.3 та
frozen-onedir §12.3 — окрема хвиля (stub із маркерами), тут не покриваються.

Модулі під тестом:
    whisper_core/meeting/signing.py    — ключі, підпис, перевірка
    whisper_core/meeting/audit_log.py  — signed append + verify_chain
    whisper_core/meeting/evidence.py   — evidence.json format 2 (підписаний)
    scripts/verify.py                  — незалежний перевіряч (stdlib + cryptography)
"""
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from whisper_core.meeting import audit_log, evidence, signing

# ── Golden vector (§13.3): фіксований seed → детермінований Ed25519-підпис. ──
# seed = 00 01 02 ... 1f; тіло: hash="a"*64, seq=0, фіксований log_id.
_GV_SEED = bytes(range(32))
_GV_PUB_B64 = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
_GV_KEY_ID = ("sha256:56475aa75463474c0285df5dbf2bcab7"
              "3da651358839e9b77481b2eab107708c")
_GV_LOG_ID = "00000000-0000-4000-8000-000000000000"
_GV_HASH = "a" * 64
_GV_SIG_B64 = ("cSZL4+IGEpEYc4b2bh6yxgjI8/fxPX7QQWhmpC1+P08g"
               "ua4nc4qWKnuGXVj2Sh8h+RqXW1mM7dXufDsxO8uLDQ==")


def _load_verify_module():
    """Імпортувати scripts/verify.py як модуль (він standalone, не в пакеті)."""
    p = Path(__file__).resolve().parents[1] / "scripts" / "verify.py"
    spec = importlib.util.spec_from_file_location("balachky_verify_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_lines(log: Path):
    return log.read_text(encoding="utf-8").splitlines()


def _write_lines(log: Path, lines):
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _signed_session(root: Path, name="sess", n_extra=1):
    """Створити identity в ``root`` і підписану сесію ``root/name`` з
    ``created`` + ``n_extra`` подій ``stopped``. Повертає (sess, identity)."""
    ident = signing.ensure_signing_identity(root)
    sess = root / name
    sess.mkdir()
    audit_log.append_event(sess, audit_log.EVENT_CREATED,
                           signer=ident, require_signature=True)
    for _ in range(n_extra):
        audit_log.append_event(sess, audit_log.EVENT_STOPPED, signer=ident)
    return sess, ident


# ── 1. Одиничні тести signing.py ─────────────────────────────────────────

class SigningUnitTests(unittest.TestCase):
    def test_golden_vector_signature_is_stable(self):
        # §13.3: signing.py має видати рівно очікуваний підпис для фіксованих
        # seed/hash/log_id/seq (Ed25519 детермінований).
        sk = Ed25519PrivateKey.from_private_bytes(_GV_SEED)
        ident = signing.SigningIdentity(
            key_id=_GV_KEY_ID, public_key_b64=_GV_PUB_B64, _private_key=sk)
        auth = signing.sign_audit_record(
            {"hash": _GV_HASH, "seq": 0}, ident, _GV_LOG_ID)
        self.assertEqual(auth["signature"], _GV_SIG_B64)
        self.assertEqual(auth["key_id"], _GV_KEY_ID)
        self.assertEqual(auth["public_key"], _GV_PUB_B64)  # seq==0 → ключ вкладено

    def test_golden_vector_verified_by_both_signing_and_verify_py(self):
        # §13.3: той самий fixture перевіряють signing.py і standalone verify.py.
        record = {"hash": _GV_HASH, "seq": 0, "auth": {
            "version": 1, "algorithm": "Ed25519", "log_id": _GV_LOG_ID,
            "key_id": _GV_KEY_ID, "public_key": _GV_PUB_B64,
            "certificate_chain": [], "signature": _GV_SIG_B64}}
        pub = base64.b64decode(_GV_PUB_B64)
        res = signing.verify_audit_record(record, lambda kid: pub)
        self.assertTrue(res.valid, res.error)

        vmod = _load_verify_module()
        body = {"algorithm": "Ed25519", "hash": _GV_HASH, "key_id": _GV_KEY_ID,
                "log_id": _GV_LOG_ID, "seq": 0, "version": 1}
        msg = vmod._SIGNING_PREFIX + vmod._canonical_json(body)
        self.assertTrue(vmod._verify_signature(
            pub, base64.b64decode(_GV_SIG_B64), msg))

    def test_key_id_is_sha256_of_public_key(self):
        self.assertEqual(signing._compute_key_id(base64.b64decode(_GV_PUB_B64)),
                         _GV_KEY_ID)

    def test_flip_one_signature_bit_fails(self):
        pub = base64.b64decode(_GV_PUB_B64)
        sig = bytearray(base64.b64decode(_GV_SIG_B64))
        sig[0] ^= 0x01
        record = {"hash": _GV_HASH, "seq": 0, "auth": {
            "version": 1, "algorithm": "Ed25519", "log_id": _GV_LOG_ID,
            "key_id": _GV_KEY_ID, "public_key": _GV_PUB_B64,
            "certificate_chain": [], "signature": base64.b64encode(bytes(sig)).decode()}}
        res = signing.verify_audit_record(record, lambda kid: pub)
        self.assertFalse(res.valid)

    def test_manifest_sign_verify_roundtrip(self):
        sk = Ed25519PrivateKey.from_private_bytes(_GV_SEED)
        ident = signing.SigningIdentity(
            key_id=_GV_KEY_ID, public_key_b64=_GV_PUB_B64, _private_key=sk)
        manifest = {"kind": "balachky-evidence", "format": 2,
                    "event_count": 3, "head": {"seq": 2, "hash": "b" * 64}}
        sig = signing.sign_evidence_manifest(manifest, ident)
        manifest["signature"] = sig
        self.assertTrue(signing.verify_evidence_manifest(
            manifest, base64.b64decode(_GV_PUB_B64)))
        # Змінене поле → підпис недійсний.
        manifest["event_count"] = 4
        self.assertFalse(signing.verify_evidence_manifest(
            manifest, base64.b64decode(_GV_PUB_B64)))

    def test_identity_persists_and_reloads_same_key(self):
        # Крит. 11 (частково): той самий DEK відкриває той самий signing key.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a = signing.ensure_signing_identity(root)
            b = signing.load_signing_identity(root)
            self.assertEqual(a.key_id, b.key_id)
            # Приватний ключ у контейнері зашифрований — не лежить у відкритому.
            raw = (root / ".audit-signing-key.json").read_bytes()
            self.assertNotIn(a.public_key_bytes, raw)  # тільки b64, не raw seed

    def test_load_missing_identity_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(signing.SigningKeyMissing):
                signing.load_signing_identity(Path(d))


# ── 2. Signed append + verify_chain (audit_log.py) ───────────────────────

class AuditLogSignedTests(unittest.TestCase):
    def test_first_event_has_log_id_key_id_signature(self):
        # Крит. 1.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=0)
            rec = audit_log.read_events(sess)[0]
            auth = rec["auth"]
            self.assertTrue(auth["log_id"])
            self.assertTrue(auth["key_id"].startswith("sha256:"))
            self.assertEqual(len(base64.b64decode(auth["signature"])), 64)
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(res.auth_status, "signed_valid")

    def test_full_chain_recompute_detected(self):
        # Крит. 2: змінити стару подію й перерахувати весь hash-ланцюг →
        # signed_invalid (підпис над старим hash не збігається).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=2)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            events[0]["note"] = {"preset": "TAMPERED"}
            prev = ""
            for e in events:
                e["prev"] = prev
                e["hash"] = audit_log._record_hash(
                    e["seq"], e["type"], e["ts"],
                    e.get("artifacts") or {}, e.get("note"), prev)
                prev = e["hash"]
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)
            self.assertEqual(res.auth_status, "signed_invalid")

    def test_flip_signature_bit_broken(self):
        # Крит. 14 / §13.2.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=1)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            sig = bytearray(base64.b64decode(events[0]["auth"]["signature"]))
            sig[10] ^= 0x02
            events[0]["auth"]["signature"] = base64.b64encode(bytes(sig)).decode()
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_replace_public_key_broken(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=0)
            other = Ed25519PrivateKey.generate()
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PublicFormat)
            other_pub = other.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            events[0]["auth"]["public_key"] = base64.b64encode(other_pub).decode()
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            # key_id більше не збігається з public_key → ключ не резолвиться →
            # signed_key_missing (public_keys_for_journal відкидає невідповідні).
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)

    def test_replace_key_id_broken(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=0)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            events[0]["auth"]["key_id"] = "sha256:" + "0" * 64
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_delete_middle_event_broken(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=2)
            log = sess / "audit.jsonl"
            lines = _read_lines(log)
            del lines[1]
            _write_lines(log, lines)
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_reorder_events_broken(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=2)
            log = sess / "audit.jsonl"
            lines = _read_lines(log)
            lines[1], lines[2] = lines[2], lines[1]
            _write_lines(log, lines)
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_duplicate_seq_broken(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=2)
            log = sess / "audit.jsonl"
            lines = _read_lines(log)
            lines.insert(2, lines[1])  # дубль події
            _write_lines(log, lines)
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_unsigned_event_in_signed_journal_broken(self):
        # Крит. 3 / §7.2.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=1)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            # Додати структурно валідну, але непідписану подію в кінець ланцюга.
            prev = events[-1]["hash"]
            seq = len(events)
            note = None
            digest = audit_log._record_hash(
                seq, audit_log.EVENT_STOPPED, events[-1]["ts"], {}, note, prev)
            events.append({"seq": seq, "type": audit_log.EVENT_STOPPED,
                           "ts": events[-1]["ts"], "artifacts": {},
                           "note": note, "prev": prev, "hash": digest})
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_remove_auth_from_one_event_broken(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=1)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            del events[1]["auth"]
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_mixed_auth_journal_broken(self):
        # Follow-up крипто-суду (§7.2, дзеркало verify.py): auth прибрано лише
        # з нульової події (гасить сигнал «журнал підписаний»), решта подій
        # підписані. Це структурно неможливий legacy-журнал → BROKEN.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=1)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            del events[0]["auth"]
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)
            self.assertIn("mixed_auth_journal", res.parse_error)

    def test_mixed_auth_journal_null_first_auth_broken(self):
        # Та сама атака, але auth=null замість видалення поля.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=1)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            events[0]["auth"] = None
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)
            self.assertIn("mixed_auth_journal", res.parse_error)

    def test_broken_tail_line_is_broken_not_prefix(self):
        # Крит. 14: битий хвіст після валідних подій → BROKEN, не «verified prefix».
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=1)
            log = sess / "audit.jsonl"
            lines = _read_lines(log)
            lines.append("{ this is not valid json")
            _write_lines(log, lines)
            self.assertEqual(audit_log.verify_chain(sess).status,
                             audit_log.STATUS_BROKEN)

    def test_remove_public_key_reports_key_missing(self):
        # Крит. 8: без public key → signed_key_missing, НЕ legacy і НЕ verified.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=1)
            log = sess / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            events[0]["auth"]["public_key"] = ""  # прибрати вкладений ключ
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_BROKEN)
            self.assertEqual(res.auth_status, "signed_key_missing")

    def test_append_without_signer_to_signed_journal_raises(self):
        # Крит. 3: signed-сесія не переходить тихо в unsigned.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ = _signed_session(root, n_extra=0)
            with self.assertRaises(signing.SigningKeyMissing):
                audit_log.append_event(sess, audit_log.EVENT_STOPPED)

    def test_legacy_unsigned_journal_status(self):
        # Крит. 8: старий журнал без auth → unsigned_legacy, не broken/signed.
        with tempfile.TemporaryDirectory() as d:
            sess = Path(d) / "sess"
            sess.mkdir()
            audit_log.append_event(sess, audit_log.EVENT_CREATED)
            audit_log.append_event(sess, audit_log.EVENT_STOPPED)
            res = audit_log.verify_chain(sess)
            self.assertEqual(res.status, audit_log.STATUS_VERIFIED)
            self.assertEqual(res.auth_status, "unsigned_legacy")


# ── 3. Evidence format 2 (evidence.py) ───────────────────────────────────

class EvidenceSignedTests(unittest.TestCase):
    def _signed_meeting(self, root: Path):
        ident = signing.ensure_signing_identity(root)
        sess = root / "sess"
        sess.mkdir()
        (sess / "mic.wav").write_bytes(b"RIFF-audio-bytes")
        (sess / "transcript.txt").write_bytes(b"hello")
        audit_log.append_event(sess, audit_log.EVENT_CREATED,
                               signer=ident, require_signature=True)
        audit_log.finalize(sess, ["mic.wav", "transcript.txt"], signer=ident)
        return sess, ident

    def test_manifest_signs_head_and_files(self):
        # Крит. 4.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, ident = self._signed_meeting(root)
            out = root / "e.zip"
            pkg = evidence.export_evidence(sess, out, app_version="1.2.4",
                                           signer=ident)
            self.assertEqual(pkg.auth_status, "signed_valid")
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                manifest = json.loads(z.read("evidence.json"))
            self.assertIn("evidence.json", names)
            self.assertIn("PUBLIC-KEYS.json", names)
            self.assertIn("requirements-verify.txt", names)
            self.assertEqual(manifest["format"], 2)
            self.assertEqual(manifest["journal_auth"], "signed_events")
            self.assertEqual(manifest["signer"]["key_id"], ident.key_id)
            self.assertTrue(manifest["signature"])
            # head = остання подія журналу.
            evs = audit_log.read_events(sess)
            self.assertEqual(manifest["head"]["seq"], evs[-1]["seq"])
            self.assertEqual(manifest["head"]["hash"], evs[-1]["hash"])
            # Підпис manifest валідний під публічним ключем автора.
            self.assertTrue(signing.verify_evidence_manifest(
                manifest, ident.public_key_bytes))
            # У manifest перелічені audit.jsonl, verify.py, REPORT.txt.
            paths = {f["path"] for f in manifest["files"]}
            for req in ("audit.jsonl", "verify.py", "REPORT.txt",
                        "PUBLIC-KEYS.json", "requirements-verify.txt"):
                self.assertIn(req, paths)
            self.assertNotIn("evidence.json", paths)  # сам себе не перелічує

    def test_report_states_strip_to_legacy_limitation(self):
        # Follow-up крипто-суду (§3.2/§3.3): REPORT.txt чесно попереджає, що
        # підпис не рятує від повного розпідписування пакета без зовнішнього
        # якоря (checkpoint).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, _ident = self._signed_meeting(root)
            report = evidence.build_report(
                sess, audit_log.verify_chain(sess), {})
            self.assertIn("розпідпис", report)
            self.assertIn("зовнішнього якоря", report)

    def test_private_key_never_in_package(self):
        # Крит. 10.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, ident = self._signed_meeting(root)
            out = root / "e.zip"
            evidence.export_evidence(sess, out, signer=ident, meetings_root=root)
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                blob = b"".join(z.read(n) for n in names)
            self.assertNotIn(".audit-signing-key.json", names)
            self.assertNotIn(".vaultkey", names)
            # Сирий seed не витікає у жоден файл пакета.
            seed = ident._private_key.private_bytes(
                *_raw_priv_args())
            self.assertNotIn(seed, blob)


def _raw_priv_args():
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat)
    return (Encoding.Raw, PrivateFormat.Raw, NoEncryption())


# ── 4. Незалежний verify.py (рівень функцій) ─────────────────────────────

class VerifyPyFunctionTests(unittest.TestCase):
    def setUp(self):
        self.vmod = _load_verify_module()

    def _export_and_extract(self, root: Path, signer=None):
        ident = signer or signing.ensure_signing_identity(root)
        sess = root / "sess"
        sess.mkdir()
        (sess / "mic.wav").write_bytes(b"audio")
        audit_log.append_event(sess, audit_log.EVENT_CREATED,
                               signer=None if signer is False else ident,
                               require_signature=bool(signer is not False))
        audit_log.finalize(sess, ["mic.wav"],
                           signer=None if signer is False else ident)
        out = root / "e.zip"
        evidence.export_evidence(
            sess, out, signer=None if signer is False else ident)
        ext = root / "ext"
        with zipfile.ZipFile(out) as z:
            z.extractall(ext)
        return ext, ident

    def test_signed_journal_untrusted_without_key(self):
        # Крит. 7: public key із пакета не подається як довірений.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._export_and_extract(root)
            status, _det = self.vmod.verify_evidence(ext)
            self.assertEqual(status, "untrusted")

    def test_signed_journal_verified_with_expect_key(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, ident = self._export_and_extract(root)
            status, _det = self.vmod.verify_evidence(
                ext, expect_key_id=ident.key_id)
            self.assertEqual(status, "verified")

    def test_no_crypto_fails_closed(self):
        # Крит. 6: без cryptography verifier НЕ падає у hash-only VERIFIED.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._export_and_extract(root)
            orig = self.vmod._check_crypto
            self.vmod._check_crypto = lambda: False
            try:
                status, _det = self.vmod.verify(ext, ext / "audit.jsonl")
                self.assertEqual(status, "no_crypto")
            finally:
                self.vmod._check_crypto = orig

    def test_tampered_report_file_hash_mismatch(self):
        # Крит. 4.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._export_and_extract(root)
            (ext / "REPORT.txt").write_text("підмінено", encoding="utf-8")
            status, det = self.vmod.verify_evidence(ext)
            self.assertEqual(status, "broken")
            self.assertIn("manifest_file", det.get("reason", ""))

    def test_tampered_manifest_signature_invalid(self):
        # Крит. 4: псуємо САМЕ байти підпису (а не інше поле manifest) —
        # раніше цей тест чіпав event_count і шлях підпису не покривав.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._export_and_extract(root)
            manifest = json.loads((ext / "evidence.json").read_text("utf-8"))
            raw = base64.b64decode(manifest["signature"])
            flipped = bytes([raw[0] ^ 0x01]) + raw[1:]      # один біт підпису
            manifest["signature"] = base64.b64encode(flipped).decode("ascii")
            (ext / "evidence.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            status, det = self.vmod.verify_evidence(ext)
            self.assertEqual(status, "broken")
            self.assertEqual(det.get("reason"), "invalid_manifest_signature")

    def test_tampered_manifest_event_count_invalid(self):
        # Крит. 4: підміна підписаного поля тіла manifest.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._export_and_extract(root)
            manifest = json.loads((ext / "evidence.json").read_text("utf-8"))
            manifest["event_count"] = manifest["event_count"] + 99
            (ext / "evidence.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            status, det = self.vmod.verify_evidence(ext)
            self.assertEqual(status, "broken")
            self.assertIn("manifest", det.get("reason", ""))

    def test_truncated_journal_head_mismatch(self):
        # Крит. 4: усічений журнал у пакеті → head mismatch (unsigned manifest,
        # щоб ізолювати саме перевірку голови, а не файловий SHA).
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._export_and_extract(root, signer=False)
            manifest = json.loads((ext / "evidence.json").read_text("utf-8"))
            manifest["head"]["seq"] = manifest["head"]["seq"] + 5
            (ext / "evidence.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            status, det = self.vmod.verify_evidence(ext)
            self.assertEqual(status, "broken")
            self.assertEqual(det.get("reason"), "manifest_head_mismatch")

    def test_legacy_evidence_returns_legacy(self):
        # Крит. 8: signed manifest над legacy журналом → journal_auth legacy.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._export_and_extract(root, signer=False)
            manifest = json.loads((ext / "evidence.json").read_text("utf-8"))
            self.assertEqual(manifest["journal_auth"], "legacy_unsigned")
            status, _det = self.vmod.verify_evidence(ext)
            self.assertEqual(status, "legacy")


# ── 5. Standalone verify.py у підпроцесі (крит. 5) ───────────────────────

class StandaloneSignedVerifierTests(unittest.TestCase):
    def _verify_py(self) -> Path:
        p = evidence.verifier_source()
        self.assertIsNotNone(p)
        return p

    def _run(self, workdir: Path, *args):
        env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "verify.py", ".", *args],
            cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env)

    def test_signed_journal_verifies_standalone(self):
        # Крит. 5: verify.py працює без «Балачок» і без private key.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            sess, ident = _signed_session(root, n_extra=1)
            shutil.copyfile(self._verify_py(), sess / "verify.py")
            # Без ключа довіри → код 3 (математично валідний, ключ не підтверджено).
            r = self._run(sess)
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("SIGNATURE VALID", r.stdout)
            # З --expect-key-id → код 0.
            r2 = self._run(sess, "--expect-key-id", ident.key_id)
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertIn("VERIFIED", r2.stdout)


# ── 6. Standalone verify.py над evidence-пакетом: обхід підпису manifest ──

class StandaloneEvidenceTamperTests(unittest.TestCase):
    """Блокер крипто-суду: раніше блок перевірки підпису manifest виконувався
    ЛИШЕ якщо manifest сам оголошував ``signer.algorithm == "Ed25519"``. Атакер
    без приватного ключа прибирав ``signer`` — і verify.py віддавав код 0
    «VERIFIED» на усіченому журналі (A7) або код 4 «UNSIGNED LEGACY» на
    розпідписаному (A9).

    Ці тести б'ють САМЕ по standalone ``scripts/verify.py`` через підпроцес —
    тому мутація ``_verify_signature → return True`` їх валить (раніше жоден із
    31 тесту цього шляху не покривав)."""

    def _verify_py(self) -> Path:
        p = evidence.verifier_source()
        self.assertIsNotNone(p)
        return p

    def _package(self, root: Path, *, signed=True):
        """Експортувати й розпакувати evidence-пакет + покласти поряд verify.py.
        Повертає (ext_dir, identity|None)."""
        ident = signing.ensure_signing_identity(root) if signed else None
        sess = root / "sess"
        sess.mkdir()
        (sess / "mic.wav").write_bytes(b"audio-bytes")
        audit_log.append_event(sess, audit_log.EVENT_CREATED,
                               signer=ident, require_signature=signed)
        audit_log.append_event(sess, audit_log.EVENT_STOPPED, signer=ident)
        audit_log.finalize(sess, ["mic.wav"], signer=ident)
        out = root / "e.zip"
        evidence.export_evidence(sess, out, signer=ident)
        ext = root / "ext"
        with zipfile.ZipFile(out) as z:
            z.extractall(ext)
        shutil.copyfile(self._verify_py(), ext / "verify.py")
        return ext, ident

    def _run(self, workdir: Path, *args):
        env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "verify.py", ".", *args],
            cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env)

    def _manifest(self, ext: Path) -> dict:
        return json.loads((ext / "evidence.json").read_text(encoding="utf-8"))

    def _write_manifest(self, ext: Path, manifest: dict):
        (ext / "evidence.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _refresh_files(self, ext: Path, manifest: dict):
        """Перерахувати sha256/size у manifest['files'] — так робить атакер,
        щоб файловий гейт не спіймав змінений audit.jsonl."""
        for finfo in manifest.get("files", []):
            p = ext / finfo["path"]
            if p.is_file():
                finfo["sha256"] = audit_log.sha256_of(p)
                finfo["size"] = p.stat().st_size
        return manifest

    # ── (5) happy-path не зламано ──
    def test_valid_package_still_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("VERIFIED", r.stdout)

    # ── (6) legacy-пакет лишається кодом 4 ──
    def test_legacy_package_still_code_4(self):
        with tempfile.TemporaryDirectory() as d:
            ext, _ = self._package(Path(d), signed=False)
            r = self._run(ext)
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("UNSIGNED LEGACY", r.stdout)

    # ── (1) прибрати блок signer ──
    def test_stripped_signer_block_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            manifest = self._manifest(ext)
            manifest.pop("signer", None)
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)
            self.assertNotIn("VERIFIED — журнал цілий", r.stdout)

    # ── підпис manifest підмінено (мутаційний якір для standalone-шляху) ──
    def test_flipped_manifest_signature_bit_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            manifest = self._manifest(ext)
            raw = base64.b64decode(manifest["signature"])
            flipped = bytes([raw[0] ^ 0x01]) + raw[1:]
            manifest["signature"] = base64.b64encode(flipped).decode("ascii")
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)

    # ── (2) algorithm "none" / інший регістр ──
    def test_algorithm_none_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            manifest = self._manifest(ext)
            manifest["signer"]["algorithm"] = "none"
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)

    def test_algorithm_uppercase_is_broken(self):
        # Регістронезалежне порівняння НЕ дає обходу: тіло підпису змінилось,
        # тож підпис стає недійсним.
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            manifest = self._manifest(ext)
            manifest["signer"]["algorithm"] = "ED25519"
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)

    def test_algorithm_empty_string_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            manifest = self._manifest(ext)
            manifest["signer"]["algorithm"] = ""
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)

    # ── (3) атака A7: усічення підписаного журналу ──
    def test_a7_truncated_signed_journal_is_broken(self):
        """Обрізати audit.jsonl до 2 подій (їхні власні підписи валідні),
        перерахувати sha256 у files, підігнати head/event_count і прибрати
        signer. Раніше → код 0 VERIFIED на журналі, з якого вирізали пізні
        події. Тепер → BROKEN."""
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            log = ext / "audit.jsonl"
            lines = _read_lines(log)
            self.assertGreater(len(lines), 2, "потрібно >2 подій для усічення")
            _write_lines(log, lines[:2])                     # усічення
            last = json.loads(lines[1])
            manifest = self._manifest(ext)
            manifest["event_count"] = 2                      # підгонка
            manifest["head"] = {"seq": last["seq"], "hash": last["hash"]}
            manifest.pop("signer", None)                     # нейтралізація
            self._refresh_files(ext, manifest)               # перерахунок sha
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)
            self.assertNotIn("VERIFIED — журнал цілий", r.stdout)

    # ── (4) атака A9: розпідписання журналу ──
    def test_a9_stripped_auth_is_broken_not_legacy(self):
        """Прибрати ``auth`` з УСІХ подій підписаного журналу (hash-ланцюг це
        не ламає — auth не входить у hash) і нейтралізувати signer. Раніше →
        код 4 «UNSIGNED LEGACY», тобто signed-сесія тихо ставала unsigned
        (порушення критерію приймання 3). Тепер → BROKEN."""
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            log = ext / "audit.jsonl"
            stripped = []
            for line in _read_lines(log):
                rec = json.loads(line)
                rec.pop("auth", None)
                stripped.append(json.dumps(rec, ensure_ascii=False))
            _write_lines(log, stripped)
            manifest = self._manifest(ext)
            manifest.pop("signer", None)                     # нейтралізація
            self._refresh_files(ext, manifest)
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)
            self.assertNotIn("UNSIGNED LEGACY", r.stdout)

    # ── follow-up крипто-суду (а): явно заявлений ключ + непідписаний журнал ──
    def test_legacy_with_expect_key_is_refusal_not_code_4(self):
        """(1) Слідчий заявив «очікую підпис ключем X», а журнал непідписаний.
        Чесна відповідь — ВІДМОВА окремим кодом 6, а не «код 4 legacy»."""
        with tempfile.TemporaryDirectory() as d:
            ext, _ = self._package(Path(d), signed=False)
            r = self._run(ext, "--expect-key-id", "sha256:" + "0" * 64)
            self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
            self.assertNotIn("UNSIGNED LEGACY", r.stdout)
            self.assertIn("ПІДПИСУ НЕМАЄ", r.stdout)

    def test_legacy_with_trusted_key_file_is_refusal(self):
        """Те саме для --trusted-key (другий спосіб заявити очікуваний ключ)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ext, _ = self._package(root, signed=False)
            from cryptography.hazmat.primitives.serialization import (
                Encoding, PublicFormat)
            pub = Ed25519PrivateKey.generate().public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw)
            keyfile = root / "commander-public-key.json"
            keyfile.write_text(json.dumps({
                "key_id": signing._compute_key_id(pub),
                "public_key": base64.b64encode(pub).decode("ascii"),
            }), encoding="utf-8")
            r = self._run(ext, "--trusted-key", str(keyfile))
            self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
            self.assertIn("ПІДПИСУ НЕМАЄ", r.stdout)

    def test_legacy_without_expect_key_still_code_4(self):
        """(2) Без заявленого ключа поведінка legacy НЕ змінилась — код 4."""
        with tempfile.TemporaryDirectory() as d:
            ext, _ = self._package(Path(d), signed=False)
            r = self._run(ext)
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("UNSIGNED LEGACY", r.stdout)

    def test_signed_package_with_expect_key_still_ok(self):
        """(5) Happy-path не зачеплено: підписаний пакет + ключ → код 0."""
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("VERIFIED", r.stdout)

    # ── follow-up крипто-суду (б): змішаний журнал = BROKEN ──
    def _downgrade_to_legacy(self, ext: Path):
        """Атакер прибирає signer і оголошує пакет legacy — саме так гаситься
        гейт підпису manifest; лишається тільки перевірка журналу."""
        manifest = self._manifest(ext)
        manifest.pop("signer", None)
        manifest["journal_auth"] = "legacy_unsigned"
        self._refresh_files(ext, manifest)
        self._write_manifest(ext, manifest)

    def test_g1_auth_stripped_from_first_event_is_broken(self):
        """(3) G1: auth прибрано ЛИШЕ з events[0] — сигнал journal_signed
        згасає, решта подій лишаються підписаними. Раніше → код 4."""
        with tempfile.TemporaryDirectory() as d:
            ext, _ = self._package(Path(d))
            log = ext / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            self.assertGreater(len(events), 1)
            del events[0]["auth"]
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            self._downgrade_to_legacy(ext)
            r = self._run(ext)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)
            self.assertNotIn("UNSIGNED LEGACY", r.stdout)

    def test_g2_first_event_auth_null_is_broken(self):
        """(4) G2: events[0].auth = null замість видалення поля."""
        with tempfile.TemporaryDirectory() as d:
            ext, _ = self._package(Path(d))
            log = ext / "audit.jsonl"
            events = [json.loads(x) for x in _read_lines(log)]
            events[0]["auth"] = None
            _write_lines(log, [json.dumps(e, ensure_ascii=False) for e in events])
            self._downgrade_to_legacy(ext)
            r = self._run(ext)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)
            self.assertNotIn("UNSIGNED LEGACY", r.stdout)

    # ── журнал підписаний, але manifest це приховує ──
    def test_journal_auth_downgraded_in_manifest_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            ext, ident = self._package(Path(d))
            manifest = self._manifest(ext)
            manifest["journal_auth"] = "legacy_unsigned"
            self._write_manifest(ext, manifest)
            r = self._run(ext, "--expect-key-id", ident.key_id)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("BROKEN", r.stdout)


if __name__ == "__main__":
    unittest.main()
