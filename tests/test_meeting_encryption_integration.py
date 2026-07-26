"""Live Windows integration coverage for the meeting vault (no DPAPI mocks)."""
import importlib.util
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from whisper_core.meeting import session
from whisper_core.meeting import audit_log
from whisper_core.meeting import evidence
from whisper_core.meeting import postprocess
from whisper_core.meeting import storage_crypto as crypto


_WINDOWS_CRYPTO = os.name == "nt" and importlib.util.find_spec("cryptography") is not None


@unittest.skipUnless(_WINDOWS_CRYPTO, "requires live Windows DPAPI and cryptography")
class LiveMeetingVaultIntegrationTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"
    PASSWORD_2 = "second correct horse battery staple"

    def setUp(self):
        crypto._PASSWORD_CACHE.clear()

    def _legacy_session(self, root: Path):
        meeting = root / "2026-07-22_20-00-00"
        (meeting / "mic1").mkdir(parents=True)
        (meeting / "sys").mkdir()
        (meeting / "mic1" / "0000.f32").write_bytes(b"mic-track")
        (meeting / "sys" / "0000.f32").write_bytes(b"system-track")
        (meeting / "screen.webm").write_bytes(b"webm-video")
        (meeting / "transcript-redacted.txt").write_text("[redacted]", encoding="utf-8")
        meta = session.MeetingMeta(
            schema=2, id=meeting.name, created=1, status="done",
            preset="multimic", sources=["mic1", "sys"])
        (meeting / "meeting.json").write_text(meta.to_json(), encoding="utf-8")
        return meeting

    def test_live_dpapi_password_recovery_keyfile_and_corruption_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            removable = Path(tmp) / "simulated-removable-drive"
            removable.mkdir()
            meeting = self._legacy_session(root)

            # Real current-user DPAPI envelope round-trip.
            dek = crypto.ensure_dek(root)
            self.assertEqual(crypto.ensure_dek(root), dek)
            self.assertEqual(crypto.vault_mode(root), "dpapi")
            self.assertEqual(session.migrate_unencrypted_sessions(root, dek), 1)
            self.assertFalse((meeting / "screen.webm").exists())
            self.assertTrue((meeting / "screen.webm.enc").exists())
            self.assertEqual(session.read_artifact(meeting, "screen.webm"), b"webm-video")
            self.assertEqual(session.read_artifact(
                meeting, "transcript-redacted.txt"), b"[redacted]")

            recovery = crypto.set_password(root, self.PASSWORD)
            crypto.lock_vault(root)
            with self.assertRaises(crypto.VaultWrongPassword):
                crypto.ensure_dek(root, "wrong password")
            self.assertEqual(crypto.ensure_dek(root, self.PASSWORD), dek)
            self.assertIsNone(crypto.set_password(root, self.PASSWORD_2))
            crypto.lock_vault(root)
            self.assertEqual(crypto.unlock_with_recovery(root, recovery), dek)
            crypto.lock_vault(root)
            self.assertEqual(crypto.ensure_dek(root, self.PASSWORD_2), dek)

            keyfile = crypto.generate_keyfile(removable / "balachky.key")
            self.assertIsNone(crypto.set_keyfile(root, keyfile))
            crypto.lock_vault(root)
            wrong = crypto.generate_keyfile(removable / "wrong.key")
            with self.assertRaises(crypto.VaultWrongKeyfile):
                crypto.unlock_with_keyfile(root, wrong)
            self.assertEqual(crypto.unlock_with_keyfile(root, keyfile), dek)

            self.assertIsNone(crypto.set_keyfile(root, keyfile, self.PASSWORD))
            crypto.lock_vault(root)
            with self.assertRaises(crypto.VaultPasswordRequired):
                crypto.unlock_with_keyfile(root, keyfile)
            with self.assertRaises(crypto.VaultWrongKeyfile):
                crypto.unlock_with_keyfile(root, keyfile, "wrong password")
            self.assertEqual(crypto.unlock_with_keyfile(root, keyfile, self.PASSWORD), dek)

            envelope = root / crypto.KEY_FILE
            original_envelope = envelope.read_bytes()
            encrypted_before = (meeting / "screen.webm.enc").read_bytes()
            envelope.write_text("{corrupted envelope", encoding="utf-8")
            crypto.lock_vault(root)
            with self.assertRaises(crypto.VaultKeyLost):
                crypto.ensure_dek(root)
            self.assertEqual((meeting / "screen.webm.enc").read_bytes(), encrypted_before)
            self.assertFalse((meeting / "screen.webm").exists())
            envelope.write_bytes(original_envelope)
            self.assertEqual(crypto.unlock_with_keyfile(root, keyfile, self.PASSWORD), dek)

    def test_legacy_migration_remains_readable_and_cleans_temps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            meeting = self._legacy_session(root)
            (meeting / ".transcript-orphan.txt.tmp").write_text(
                "sensitive temporary text", encoding="utf-8")
            dek = crypto.ensure_dek(root)
            self.assertEqual(session.migrate_unencrypted_sessions(root, dek), 1)
            self.assertEqual(session.load_meta(meeting).status, "done")
            self.assertFalse(any(meeting.rglob("*.tmp")))
            with session.materialize_session(meeting) as plain:
                self.assertEqual((plain / "mic1" / "0000.f32").read_bytes(), b"mic-track")
                plain_root = plain.parent
            self.assertFalse(plain_root.exists())

    def test_completed_encrypted_session_mutations_stay_sealed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            meeting = self._legacy_session(root)
            dek = crypto.ensure_dek(root)
            self.assertEqual(session.migrate_unencrypted_sessions(root, dek), 1)

            session.set_title(meeting, "Private title")
            postprocess.write_transcript_text(meeting, "Private transcript")
            # append_event несе actor у note (реальний підпис не має окремого actor).
            audit_log.append_event(meeting, "edited", note={"actor": "integration-test"})

            self.assertEqual(session.load_meta(meeting).title, "Private title")
            self.assertEqual(
                session.read_artifact(meeting, "transcript.txt"),
                b"Private transcript",
            )
            self.assertEqual(
                audit_log.verify_chain(meeting).status,
                audit_log.STATUS_VERIFIED,
            )
            self.assertFalse((meeting / "meeting.json").exists())
            self.assertFalse((meeting / "transcript.txt").exists())
            self.assertFalse((meeting / audit_log._LOG_NAME).exists())
            self.assertTrue((meeting / "meeting.json.enc").exists())
            self.assertTrue((meeting / "transcript.txt.enc").exists())
            self.assertTrue((meeting / (audit_log._LOG_NAME + ".enc")).exists())

            package = evidence.export_evidence(
                meeting, Path(tmp) / "evidence.zip", app_version="integration")
            self.assertEqual(package.status, audit_log.STATUS_VERIFIED)
            with zipfile.ZipFile(package.out_zip) as archive:
                names = set(archive.namelist())
                self.assertIn("meeting.json", names)
                self.assertIn("transcript.txt", names)
                self.assertIn(audit_log._LOG_NAME, names)
                self.assertFalse(any(name.endswith(".enc") for name in names))


if __name__ == "__main__":
    unittest.main()
