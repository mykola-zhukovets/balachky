"""FIX 1: імена мовців наради пишуться encryption-aware (write_artifact), НЕ
через plaintext atomic_write_json.

На запечатаній сесії (meeting.json.enc) прямий atomic_write_json клав
НЕЗАШИФРОВАНИЙ meeting.json (хто-що-казав) поруч із .enc; краш між цим і
finalize лишав плейнтекст назавжди. Перевіряємо через unbound
DesktopApp._persist_speaker_names на SimpleNamespace (метод не залежить від self).
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from whisper_core.meeting import session
from whisper_core.meeting import storage_crypto as crypto

_WINDOWS_CRYPTO = os.name == "nt" and importlib.util.find_spec("cryptography") is not None


def _make_session(root: Path) -> Path:
    d = root / "2026-07-22_21-00-00"
    (d / "mic").mkdir(parents=True)
    (d / "mic" / "0000.f32").write_bytes(b"mic-track")
    meta = session.MeetingMeta(
        schema=2, id=d.name, created=1, status="done",
        preset="both", sources=["mic", "sys"],
        speaker_names={"speaker_1": "Спікер 1"})
    (d / "meeting.json").write_text(meta.to_json(), encoding="utf-8")
    return d


class PersistSpeakerNamesPlaintextTests(unittest.TestCase):
    """Незашифрована сесія: звичайний запис meeting.json оновлюється як завжди."""

    def test_unencrypted_session_updates_meeting_json(self):
        from fronts.desktop.app import DesktopApp
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_session(Path(tmp))
            DesktopApp._persist_speaker_names(
                SimpleNamespace(), d, {"speaker_1": "Директор"})
            self.assertTrue((d / "meeting.json").is_file())
            self.assertEqual(
                session.load_meta(d).speaker_names["speaker_1"], "Директор")

    def test_missing_meta_is_noop(self):
        from fronts.desktop.app import DesktopApp
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "no-such-session"
            d.mkdir()
            # немає meeting.json → тихо None, без падіння
            self.assertIsNone(DesktopApp._persist_speaker_names(
                SimpleNamespace(), d, {"speaker_1": "Директор"}))


@unittest.skipUnless(_WINDOWS_CRYPTO, "requires live Windows DPAPI and cryptography")
class PersistSpeakerNamesEncryptedTests(unittest.TestCase):
    """Запечатана сесія: оновлення імен НЕ лишає плейнтекст meeting.json."""

    def setUp(self):
        crypto._PASSWORD_CACHE.clear()

    def test_sealed_session_update_leaves_no_plaintext(self):
        from fronts.desktop.app import DesktopApp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            d = _make_session(root)
            dek = crypto.ensure_dek(root)
            session.encrypt_session(d, dek)
            # запечатано: лише .enc, без плейнтексту
            self.assertTrue((d / "meeting.json.enc").is_file())
            self.assertFalse((d / "meeting.json").exists())

            DesktopApp._persist_speaker_names(
                SimpleNamespace(), d, {"speaker_1": "Директор"})

            # ключове: плейнтекст meeting.json НЕ зʼявився; оновлення дійшло в .enc
            self.assertFalse(
                (d / "meeting.json").exists(),
                "плейнтекст meeting.json зʼявився поруч із .enc — витік хто-що-казав")
            self.assertTrue((d / "meeting.json.enc").is_file())
            self.assertEqual(
                session.load_meta(d).speaker_names["speaker_1"], "Директор")


if __name__ == "__main__":
    unittest.main()
