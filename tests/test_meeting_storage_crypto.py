"""Криптографічні інваріанти сховища нарад.

AES-тести умовно пропускаються лише в dev-середовищі без ``cryptography``;
release requirements містить залежність, тож у CI/збірці вони обов'язкові.
"""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from whisper_core.meeting import storage_crypto as crypto

_HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None


class DPAPITests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "DPAPI існує лише у Windows")
    def test_wrap_unwrap_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            first = crypto.ensure_dek(root)
            second = crypto.ensure_dek(root)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 32)
            self.assertTrue((root / crypto.KEY_FILE).is_file())


@unittest.skipUnless(_HAS_CRYPTO, "cryptography не встановлена у цьому dev venv")
class StreamingAesGcmTests(unittest.TestCase):
    def _round_trip(self, payload: bytes):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, encrypted, restored = root / "in", root / "in.enc", root / "out"
            source.write_bytes(payload)
            dek = os.urandom(32)
            crypto.encrypt_file(source, encrypted, dek)
            crypto.decrypt_file(encrypted, restored, dek)
            self.assertEqual(restored.read_bytes(), payload)
            self.assertEqual(crypto.decrypt_to_memory(encrypted, dek), payload)

    def test_byte_identical_sizes_including_empty_and_many_chunks(self):
        for size in (0, 1, crypto.CHUNK_SIZE - 1, crypto.CHUNK_SIZE,
                     crypto.CHUNK_SIZE + 1, crypto.CHUNK_SIZE * 3 + 123):
            with self.subTest(size=size):
                self._round_trip(os.urandom(size))

    def test_tamper_raises_authenticated_decryption_error(self):
        from cryptography.exceptions import InvalidTag
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, encrypted = root / "in", root / "in.enc"
            source.write_bytes(os.urandom(crypto.CHUNK_SIZE + 9))
            dek = os.urandom(32)
            crypto.encrypt_file(source, encrypted, dek)
            data = bytearray(encrypted.read_bytes())
            header = 1 + len(crypto._MAGIC) + crypto._FILE_ID_BYTES
            data[header + 4] ^= 0x80        # ciphertext першого чанка, не заголовок
            encrypted.write_bytes(data)
            with self.assertRaises(InvalidTag):
                crypto.decrypt_to_memory(encrypted, dek)

    def test_password_rewrap_same_dek_and_wrong_password_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            original = crypto.ensure_dek(root)
            crypto.set_password(root, "correct horse battery staple")
            crypto._PASSWORD_CACHE.clear()  # імітуємо новий процес без кешу
            # стабільний виняток для UI (без імпорту cryptography у віджетах)
            with self.assertRaises(crypto.VaultWrongPassword):
                crypto.ensure_dek(root, "wrong password")
            self.assertEqual(crypto.ensure_dek(root, "correct horse battery staple"), original)
            crypto.remove_password(root, "correct horse battery staple")
            self.assertEqual(crypto.ensure_dek(root), original)

    def test_password_wrap_unwrap_uses_current_scrypt_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            original = crypto.ensure_dek(root)
            crypto.set_password(root, "correct horse battery staple")
            blob = json.loads((root / crypto.KEY_FILE).read_text(encoding="utf-8"))
            self.assertEqual((blob["n"], blob["r"], blob["p"]), (2**17, 8, 1))
            crypto._PASSWORD_CACHE.clear()
            self.assertEqual(crypto.ensure_dek(root, "correct horse battery staple"), original)

    def test_password_unwraps_legacy_scrypt_parameters(self):
        password = "legacy password"
        dek, salt, nonce = os.urandom(32), os.urandom(16), os.urandom(12)
        kek = crypto._derive_kek(password, salt, n=2**15, r=8, p=1)
        wrapped = nonce + crypto._aesgcm_class()(kek).encrypt(
            nonce, dek, b"Balachky vault key")
        legacy_blob = {"version": 1, "mode": "password", "salt": crypto._b64(salt),
                       "wrapped_dek": crypto._b64(wrapped), "kdf": "scrypt",
                       "n": 2**15, "r": 8, "p": 1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            root.mkdir()
            crypto._write_vault(root, legacy_blob)
            self.assertEqual(crypto.ensure_dek(root, password), dek)
            crypto._PASSWORD_CACHE.clear()
            legacy_blob.pop("n")
            legacy_blob.pop("r")
            legacy_blob.pop("p")
            crypto._write_vault(root, legacy_blob)
            self.assertEqual(crypto.ensure_dek(root, password), dek)

    def test_password_rewrap_writes_current_scrypt_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            crypto.ensure_dek(root)
            crypto.set_password(root, "first password")
            crypto.set_password(root, "second password")
            blob = json.loads((root / crypto.KEY_FILE).read_text(encoding="utf-8"))
            self.assertEqual((blob["n"], blob["r"], blob["p"]), (2**17, 8, 1))

    def test_200mb_file_is_processed_in_chunks(self):
        """Практичний регресійний тест: 200 MiB не передається одним буфером AESGCM."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, encrypted, restored = root / "large.wav", root / "large.wav.enc", root / "out.wav"
            with source.open("wb") as f:
                block = b"0123456789abcdef" * 4096  # 64 KiB
                for _ in range(3200):               # рівно 200 MiB
                    f.write(block)
            dek = os.urandom(32)
            crypto.encrypt_file(source, encrypted, dek)
            crypto.decrypt_file(encrypted, restored, dek)
            self.assertEqual(restored.stat().st_size, 200 * 1024 * 1024)
            with restored.open("rb") as f:
                self.assertEqual(f.read(32), b"0123456789abcdef" * 2)

    def test_identical_plaintext_uses_distinct_file_keys_and_rejects_splice(self):
        from cryptography.exceptions import InvalidTag
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_a, source_b = root / "a", root / "b"
            encrypted_a, encrypted_b = root / "a.enc", root / "b.enc"
            payload = b"same plaintext" * (crypto.CHUNK_SIZE // 14 + 2)
            source_a.write_bytes(payload)
            source_b.write_bytes(payload)
            dek = os.urandom(32)
            crypto.encrypt_file(source_a, encrypted_a, dek)
            crypto.encrypt_file(source_b, encrypted_b, dek)
            encrypted_a_bytes = encrypted_a.read_bytes()
            encrypted_b_bytes = bytearray(encrypted_b.read_bytes())
            self.assertEqual(encrypted_a_bytes[0], crypto._FORMAT_VERSION)
            self.assertNotEqual(encrypted_a_bytes, bytes(encrypted_b_bytes))

            header = 1 + len(crypto._MAGIC) + crypto._FILE_ID_BYTES
            chunk_size = int.from_bytes(encrypted_a_bytes[header:header + 4], "big")
            encrypted_b_bytes[header:header + 4 + chunk_size] = (
                encrypted_a_bytes[header:header + 4 + chunk_size])
            encrypted_b.write_bytes(encrypted_b_bytes)
            with self.assertRaises(InvalidTag):
                crypto.decrypt_to_memory(encrypted_b, dek)


class ScryptKdfTests(unittest.TestCase):
    def test_current_scrypt_default_fits_maxmem(self):
        self.assertEqual(len(crypto._derive_kek("password", os.urandom(16))), 32)


class VaultKeySafetyTests(unittest.TestCase):
    def test_parallel_first_creation_returns_one_dek(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            start = threading.Barrier(8)
            results, errors = [], []

            def worker():
                try:
                    start.wait()
                    results.append(crypto.ensure_dek(root))
                except Exception as exc:  # assertions below keep thread errors visible
                    errors.append(exc)

            with mock.patch.object(crypto, "_dpapi_protect", side_effect=lambda dek: dek), \
                 mock.patch.object(crypto, "_dpapi_unprotect", side_effect=lambda dek: dek):
                threads = [threading.Thread(target=worker) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 8)
            self.assertEqual(len(set(results)), 1)

    def test_missing_vaultkey_with_encrypted_artifact_is_explicit_key_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            root.mkdir()
            (root / "meeting.json.enc").write_bytes(b"encrypted artifact")
            with self.assertRaises(crypto.VaultKeyLost) as raised:
                crypto.ensure_dek(root)
            self.assertIn(".vaultkey", str(raised.exception))
            self.assertFalse((root / crypto.KEY_FILE).exists())

if __name__ == "__main__":
    unittest.main()

@unittest.skipUnless(_HAS_CRYPTO, "cryptography unavailable")
class EncryptionRegressionTests(unittest.TestCase):
    def test_file_swap_is_rejected_by_v2_context(self):
        from cryptography.exceptions import InvalidTag
        with tempfile.TemporaryDirectory() as tmp:
            d, dek = Path(tmp), os.urandom(32)
            (d / "mic").write_bytes(b"m"); (d / "sys").write_bytes(b"s")
            crypto.encrypt_file(d / "mic", d / "mic.enc", dek, context="S/mic")
            (d / "sys.enc").write_bytes((d / "mic.enc").read_bytes())
            with self.assertRaises(InvalidTag): crypto.decrypt_to_memory(d / "sys.enc", dek, context="S/sys")

    def test_vault_mode_and_lock_state(self):
        """Примітиви для UI пароля: режим сховища і стан розблокування."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            self.assertIsNone(crypto.vault_mode(root))       # ключа ще немає
            self.assertTrue(crypto.is_unlocked(root))        # нічого не замкнено
            crypto.ensure_dek(root)
            self.assertEqual(crypto.vault_mode(root), "dpapi")
            self.assertTrue(crypto.is_unlocked(root))        # DPAPI не потребує пароля
            crypto.set_password(root, "correct horse battery staple")
            self.assertEqual(crypto.vault_mode(root), "password")
            self.assertTrue(crypto.is_unlocked(root))        # set_password кешує DEK
            crypto.lock_vault(root)                          # «Заблокувати зараз»
            self.assertFalse(crypto.is_unlocked(root))
            with self.assertRaises(crypto.VaultPasswordRequired):
                crypto.ensure_dek(root)
            crypto.ensure_dek(root, "correct horse battery staple")
            self.assertTrue(crypto.is_unlocked(root))

    def test_vault_mode_corrupted_key_raises_key_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "meetings"
            root.mkdir()
            (root / crypto.KEY_FILE).write_text("junk", encoding="utf-8")
            with self.assertRaises(crypto.VaultKeyLost):
                crypto.vault_mode(root)

    def test_stale_lock_dead_pid_reclaimed_live_pid_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); root.mkdir(exist_ok=True); lock = root / crypto._KEY_LOCK_FILE
            lock.write_text('{"pid":999999,"created":0}', encoding="ascii")
            with mock.patch.object(crypto._VaultCreationLock, "_pid_alive", return_value=False), mock.patch.object(crypto, "_dpapi_protect", side_effect=lambda x: x):
                self.assertEqual(len(crypto.ensure_dek(root)), 32)
            lock.write_text('{"pid":1,"created":0}', encoding="ascii")
            holder = crypto._VaultCreationLock(root)
            with mock.patch.object(holder, "_pid_alive", return_value=True): self.assertFalse(holder._stale())


@unittest.skipUnless(_HAS_CRYPTO, "cryptography не встановлена у цьому dev venv")
class RecoveryCodeTests(unittest.TestCase):
    """Код відновлення: третій слот-обгортка DEK у .vaultkey (scrypt від коду)."""

    PW = "correct horse battery staple"

    def setUp(self):
        crypto._PASSWORD_CACHE.clear()

    def _fresh_vault(self, tmp):
        root = Path(tmp) / "meetings"
        crypto.ensure_dek(root)
        return root

    def test_code_format_groups_and_unambiguous_alphabet(self):
        code = crypto._new_recovery_code()
        groups = code.split("-")
        self.assertTrue(all(len(g) == crypto._RECOVERY_GROUP for g in groups))
        symbols = code.replace("-", "")
        self.assertGreaterEqual(len(symbols) * 5, 128)          # ≥128 біт ентропії
        self.assertTrue(set(symbols) <= set(crypto._RECOVERY_ALPHABET))
        self.assertFalse(set("O0I1") & set(crypto._RECOVERY_ALPHABET))
        self.assertNotEqual(code, crypto._new_recovery_code())  # одноразовий/випадковий

    def test_set_password_returns_code_and_recovery_unlocks_same_dek(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            code = crypto.set_password(root, self.PW)
            self.assertIsInstance(code, str)
            blob = json.loads((root / crypto.KEY_FILE).read_text(encoding="utf-8"))
            self.assertIn("recovery", blob)
            crypto._PASSWORD_CACHE.clear()
            # ввід із зайвими пробілами й у нижньому регістрі теж має підходити
            noisy = "  " + code.lower().replace("-", " ") + " "
            self.assertEqual(crypto.unlock_with_recovery(root, noisy), dek)

    def test_wrong_recovery_code_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            crypto.set_password(root, self.PW)
            crypto._PASSWORD_CACHE.clear()
            with self.assertRaises(crypto.VaultWrongRecovery):
                crypto.unlock_with_recovery(root, "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG")

    def test_recovery_survives_password_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            code = crypto.set_password(root, self.PW)
            # зміна пароля НЕ видає новий код і НЕ інвалідовує старий
            self.assertIsNone(crypto.set_password(root, "second password"))
            crypto._PASSWORD_CACHE.clear()
            self.assertEqual(crypto.unlock_with_recovery(root, code), dek)

    def test_recovery_removed_with_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            code = crypto.set_password(root, self.PW)
            crypto.remove_password(root, self.PW)
            blob = json.loads((root / crypto.KEY_FILE).read_text(encoding="utf-8"))
            self.assertNotIn("recovery", blob)
            with self.assertRaises(crypto.VaultKeyLost):
                crypto.unlock_with_recovery(root, code)

    def test_regenerate_invalidates_old_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            old = crypto.set_password(root, self.PW)
            new = crypto.regenerate_recovery(root, self.PW)
            self.assertNotEqual(old, new)
            crypto._PASSWORD_CACHE.clear()
            self.assertEqual(crypto.unlock_with_recovery(root, new), dek)
            crypto._PASSWORD_CACHE.clear()
            with self.assertRaises(crypto.VaultWrongRecovery):
                crypto.unlock_with_recovery(root, old)

    def test_regenerate_requires_password_when_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            crypto.set_password(root, self.PW)
            crypto.lock_vault(root)
            with self.assertRaises(crypto.VaultPasswordRequired):
                crypto.regenerate_recovery(root)

    def test_unlock_with_recovery_lets_user_set_new_password(self):
        """Після входу кодом можна задати новий пароль; старий код лишається."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            code = crypto.set_password(root, self.PW)
            crypto._PASSWORD_CACHE.clear()
            self.assertEqual(crypto.unlock_with_recovery(root, code), dek)
            self.assertIsNone(crypto.set_password(root, "brand new password"))
            self.assertEqual(crypto.ensure_dek(root, "brand new password"), dek)
            crypto._PASSWORD_CACHE.clear()


@unittest.skipUnless(_HAS_CRYPTO, "cryptography недоступна")
class KeyfileTests(unittest.TestCase):
    """Файл-ключ: четвертий слот-обгортка DEK у .vaultkey (scrypt від вмісту
    файла), плюс двофакторний режим «пароль+файл» (HKDF від пароля||файла)."""

    PW = "correct horse battery staple"

    def setUp(self):
        crypto._PASSWORD_CACHE.clear()

    def _fresh_vault(self, tmp):
        root = Path(tmp) / "meetings"
        crypto.ensure_dek(root)
        return root

    def test_generate_keyfile_writes_64_random_distinct_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.vaultkey", Path(tmp) / "b.vaultkey"
            crypto.generate_keyfile(a)
            crypto.generate_keyfile(b)
            self.assertEqual(len(a.read_bytes()), crypto._KEYFILE_BYTES)
            self.assertEqual(len(b.read_bytes()), crypto._KEYFILE_BYTES)
            self.assertNotEqual(a.read_bytes(), b.read_bytes())

    def test_keyfile_only_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            kf = crypto.generate_keyfile(Path(tmp) / "key.vaultkey")
            self.assertIsInstance(crypto.set_keyfile(root, kf), str)  # код відновлення
            self.assertEqual(crypto.vault_mode(root), "keyfile")
            crypto._PASSWORD_CACHE.clear()
            self.assertFalse(crypto.is_unlocked(root))
            self.assertEqual(crypto.unlock_with_keyfile(root, kf), dek)
            self.assertTrue(crypto.is_unlocked(root))

    def test_wrong_keyfile_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            kf = crypto.generate_keyfile(Path(tmp) / "key.vaultkey")
            crypto.set_keyfile(root, kf)
            crypto._PASSWORD_CACHE.clear()
            other = crypto.generate_keyfile(Path(tmp) / "other.vaultkey")
            with self.assertRaises(crypto.VaultWrongKeyfile):
                crypto.unlock_with_keyfile(root, other)
            # файл неправильної довжини теж відхиляється (не схоже на файл-ключ)
            junk = Path(tmp) / "junk.bin"
            junk.write_bytes(b"not a keyfile")
            with self.assertRaises(crypto.VaultWrongKeyfile):
                crypto.unlock_with_keyfile(root, junk)

    def test_two_factor_requires_both_password_and_keyfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            kf = crypto.generate_keyfile(Path(tmp) / "key.vaultkey")
            crypto.set_keyfile(root, kf, password=self.PW)
            self.assertEqual(crypto.vault_mode(root), "password+keyfile")
            crypto._PASSWORD_CACHE.clear()
            # обидва → успіх
            self.assertEqual(
                crypto.unlock_with_keyfile(root, kf, password=self.PW), dek)
            crypto._PASSWORD_CACHE.clear()
            # лише файл (без пароля) → відмова
            with self.assertRaises(crypto.VaultPasswordRequired):
                crypto.unlock_with_keyfile(root, kf)
            # правильний файл, неправильний пароль → відмова
            with self.assertRaises(crypto.VaultWrongKeyfile):
                crypto.unlock_with_keyfile(root, kf, password="wrong pass")
            # правильний пароль, неправильний файл → відмова
            other = crypto.generate_keyfile(Path(tmp) / "other.vaultkey")
            with self.assertRaises(crypto.VaultWrongKeyfile):
                crypto.unlock_with_keyfile(root, other, password=self.PW)

    def test_recovery_works_in_keyfile_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            kf = crypto.generate_keyfile(Path(tmp) / "key.vaultkey")
            code = crypto.set_keyfile(root, kf)
            crypto._PASSWORD_CACHE.clear()
            self.assertEqual(crypto.unlock_with_recovery(root, code), dek)

    def test_recovery_works_in_two_factor_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            kf = crypto.generate_keyfile(Path(tmp) / "key.vaultkey")
            code = crypto.set_keyfile(root, kf, password=self.PW)
            crypto._PASSWORD_CACHE.clear()
            self.assertEqual(crypto.unlock_with_recovery(root, code), dek)

    def test_recovery_survives_switch_from_password_to_keyfile(self):
        """Перехід пароль→файл-ключ не інвалідовує наявний код відновлення."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            dek = crypto.ensure_dek(root)
            code = crypto.set_password(root, self.PW)
            kf = crypto.generate_keyfile(Path(tmp) / "key.vaultkey")
            self.assertIsNone(crypto.set_keyfile(root, kf))  # код не перевидається
            crypto._PASSWORD_CACHE.clear()
            self.assertEqual(crypto.unlock_with_recovery(root, code), dek)

    def test_remove_keyfile_returns_to_dpapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._fresh_vault(tmp)
            kf = crypto.generate_keyfile(Path(tmp) / "key.vaultkey")
            crypto.set_keyfile(root, kf)
            crypto.remove_keyfile(root)
            self.assertEqual(crypto.vault_mode(root), "dpapi")
            blob = json.loads((root / crypto.KEY_FILE).read_text(encoding="utf-8"))
            self.assertNotIn("recovery", blob)
            crypto._PASSWORD_CACHE.clear()
            with self.assertRaises(crypto.VaultKeyLost):
                crypto.unlock_with_keyfile(root, kf)
