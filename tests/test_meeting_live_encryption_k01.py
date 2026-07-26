"""К-01: чи шифрується запис наради ПІД ЧАС запису, чи лише в кінці.

Послідовність з реального коду (fronts/desktop/app.py + whisper_core/meeting/session.py):

  1. app._toggle_meeting (app.py ~4135) викликає ensure_dek(root) — це лише
     гарантує, що ключ сховища існує/розблокований. На диск сесії це НІЧОГО
     не пише.
  2. create_session(...) → MeetingSession.__init__ (session.py:311) створює
     теку сесії й одразу пише meeting.json — АЛЕ через ІНСТАНСНИЙ метод
     MeetingSession._write_meta (session.py:489-492), який кличе
     atomic_write_json НАПРЯМУ, в обхід write_artifact/write_artifact_file і
     без жодної перевірки шифрування. Позначки "сесія зашифрована"
     (meeting.json.enc) на цьому кроці не з'являється.
  3. Кожен байт аудіо йде через MeetingSession._write → _open_segment
     (session.py:385-400): `open(tdir / f"{index:04d}.f32", "wb")` — це
     СИРИЙ файловий дескриптор, той самий шлях що й meeting.json: жодного
     виклику write_artifact_file, жодної перевірки маркера. Перший фрагмент
     (0000.f32) лягає на диск відкритим текстом.
  4. Позначка "сесія зашифрована" (session_dir/meeting.json.enc), яку читає
     write_artifact/write_artifact_file (encrypted_session, session.py:613),
     з'являється ЛИШЕ всередині encrypt_session() (session.py:782), а її
     викликає app._secure_meeting_finish (app.py:343-361) ПІСЛЯ sess.finalize()
     — тобто після того, як запис уже зупинено.

  Висновок доведений тестами нижче: це не помилка позиціонування одного
  маркера відносно одного запису — шифрування "на льоту" під час запису
  просто не існує в коді. Це свідомо задокументована поведінка:
  i18n.STRINGS["uk"]["set_meeting_encrypt_hint"] прямо каже "Під час запису
  аудіо, відео й службові файли лежать на цьому комп'ютері відкрито", а
  set_meeting_encrypt_password_warn попереджає саме про крах живлення під
  час запису (див. tests/test_security_beta.py::HonestIntegrityCopyTests).

  Test A фіксує факт (перший фрагмент лежить відкритим, поки триває запис —
  це очікувано і задокументовано, НЕ дефект для фіксу).
  Test B — реальний тест-замок безпеки: коли нарада зупиняється штатно,
  encrypt_session() зобов'язаний забрати геть УСІ файли з диска сесії, разом
  із найпершим сегментом, записаним задовго до finalize. Перевірено на
  закомміченому коді: тимчасова зміна рекурсивного обходу session.py
  (rglob → glob) валила саме цей тест (0000.f32 лишався в mic/ незачепленим),
  зміну повернуто одразу після перевірки — session.py в цьому коміті чистий.
"""
import tempfile
import unittest
from pathlib import Path

from whisper_core.meeting import session
from whisper_core.meeting.session import create_session
from whisper_core.meeting.storage_crypto import decrypt_to_memory


def _frames(n_frames: int, channels: int = 1) -> bytes:
    return b"\x00" * (n_frames * channels * 4)


class LiveRecordingIsPlaintextUntilStopTests(unittest.TestCase):
    """Test A: документує реальний стан на диску, поки нарада ще триває."""

    def test_first_fragment_and_meta_are_plaintext_while_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sess = create_session(root, ["mic"], rate=100, channels=1,
                                  segment_seconds=1000)
            first_chunk = _frames(10)
            sess.mic_sink(first_chunk)
            # finalize() лише закриває/флашить дескриптор сегмента — жодного
            # виклику шифрування тут немає (те окремо робить encrypt_session,
            # яку кличе лише app._secure_meeting_finish, app.py:343-361).
            sess.finalize()

            first_fragment = sess.dir / "mic" / "0000.f32"
            meta_file = sess.dir / "meeting.json"

            # Найперший фрагмент наради лежить на диску відкритим текстом.
            self.assertTrue(first_fragment.exists())
            self.assertEqual(first_fragment.read_bytes(), first_chunk)
            self.assertFalse(Path(str(first_fragment) + ".enc").exists())

            # Так само meeting.json — без жодної позначки шифрування.
            self.assertTrue(meta_file.exists())
            self.assertFalse((sess.dir / "meeting.json.enc").exists())


class EncryptSessionSealsEveryLiveArtifactTests(unittest.TestCase):
    """Test B: тест-замок. Після штатної зупинки жодного відкритого фрагмента
    не мусить лишитись — включно з найпершим, записаним на старті наради."""

    DEK = b"k" * 32

    def _record_multi_segment_session(self, root: Path):
        # segment_seconds=1 при rate=100 → кожні 100 кадрів нова ротація:
        # 0000.f32 закривається задовго до фіналізації, як і в реальному записі.
        sess = create_session(root, ["mic"], rate=100, channels=1,
                              segment_seconds=1)
        first_chunk = _frames(100)  # рівно один сегмент → форсує ротацію 0000→0001
        second_chunk = _frames(50)
        sess.mic_sink(first_chunk)
        sess.mic_sink(second_chunk)
        return sess, first_chunk

    def _leftover_plaintext(self, session_dir: Path):
        return [
            p for p in session_dir.rglob("*")
            if p.is_file() and not p.name.endswith(".enc")
            and p.name != "encrypting.marker"
        ]

    def test_first_fragment_is_sealed_with_zero_plaintext_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sess, first_chunk = self._record_multi_segment_session(root)
            session_dir = sess.dir

            # Найперший сегмент справді вже ротовано (закрито) до фіналізації.
            first_fragment = session_dir / "mic" / "0000.f32"
            self.assertTrue(first_fragment.exists())

            # Штатна зупинка наради: сама послідовність із
            # app._secure_meeting_finish (app.py:343-361).
            sess.finalize(session.STATUS_STOPPED)
            session.encrypt_session(session_dir, self.DEK, status=session.STATUS_DONE)

            # Жодного відкритого файла з вмістом наради не лишилось на диску.
            leftover = self._leftover_plaintext(session_dir)
            self.assertEqual(
                leftover, [],
                f"на диску лишились незашифровані файли після encrypt_session: {leftover}")

            # Найперший фрагмент саме зашифрований і відновлюється байт-у-байт
            # (пряме розшифрування DEK — без ensure_dek/.vaultkey, бо тест не
            # піднімає справжнє DPAPI-сховище; те саме API, що read_artifact
            # застосував би, якби DEK прийшов зі сховища).
            sealed_first = session_dir / "mic" / "0000.f32.enc"
            self.assertTrue(sealed_first.exists())
            context = session._session_context(session_dir, "mic/0000.f32")
            self.assertEqual(
                decrypt_to_memory(sealed_first, self.DEK, context=context), first_chunk)


if __name__ == "__main__":
    unittest.main()
