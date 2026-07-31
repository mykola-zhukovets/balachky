"""Доказові межі формату криптосховища нарад."""
import importlib.util
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from whisper_core.meeting import storage_crypto as crypto

_HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
_PROPERTY_SEED = 0x504F53543531


@unittest.skipUnless(_HAS_CRYPTO, "cryptography не встановлена у цьому dev venv")
class FormatLimitTests(unittest.TestCase):
    def test_file_id_is_16_random_bytes_from_os_urandom(self):
        file_id = bytes.fromhex("00112233445566778899aabbccddeeff")
        dek = os.urandom(32)
        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "payload.enc"
            with mock.patch.object(crypto.os, "urandom", return_value=file_id) as urandom:
                crypto.encrypt_bytes(b"payload", encrypted, dek,
                                     context=b"session/payload")

            urandom.assert_called_once_with(16)
            header = encrypted.read_bytes()
            self.assertEqual(header[1:1 + len(crypto._MAGIC)], crypto._MAGIC)
            start = 1 + len(crypto._MAGIC)
            self.assertEqual(header[start:start + 16], file_id)

    def test_file_id_collision_bound_for_maximum_files_per_dek(self):
        self.assertEqual(crypto._FILE_ID_BYTES, 16)
        self.assertEqual(crypto._MAX_FILES_PER_DEK, 2**32)
        identifier_space = 2 ** (8 * crypto._FILE_ID_BYTES)
        collision_upper_numerator = (
            crypto._MAX_FILES_PER_DEK * (crypto._MAX_FILES_PER_DEK - 1)
        )
        # Birthday union bound: n(n-1)/(2*2**128) < 2**-65.
        self.assertLess(collision_upper_numerator * 2**65,
                        2 * identifier_space)

    def test_chunk_and_plaintext_limits_are_exactly_256_tib(self):
        self.assertEqual(crypto._MAX_CHUNKS_PER_FILE, 2**32)
        self.assertEqual(
            crypto._MAX_PLAINTEXT_BYTES_PER_FILE,
            crypto._MAX_CHUNKS_PER_FILE * crypto.CHUNK_SIZE,
        )
        self.assertEqual(crypto._MAX_PLAINTEXT_BYTES_PER_FILE, 256 * 2**40)

    def test_nonce_is_unique_for_seeded_indices_and_rejects_limit(self):
        rng = random.Random(_PROPERTY_SEED)
        indices = rng.sample(range(100_000), 256)
        nonces = {
            crypto._nonce(index, final)
            for index in indices
            for final in (False, True)
        }
        self.assertEqual(len(nonces), len(indices) * 2)
        self.assertEqual(len(crypto._nonce(crypto._MAX_CHUNKS_PER_FILE - 1, True)),
                         12)
        with self.assertRaisesRegex(ValueError, "меж.*чанків"):
            crypto._nonce(crypto._MAX_CHUNKS_PER_FILE, False)

    def test_encryption_refuses_more_than_maximum_chunks(self):
        payload = b"x" * (2 * crypto.CHUNK_SIZE + 1)
        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "payload.enc"
            with mock.patch.object(crypto, "_MAX_CHUNKS_PER_FILE", 2):
                with self.assertRaisesRegex(ValueError, "меж.*чанків"):
                    crypto.encrypt_bytes(payload, encrypted, os.urandom(32),
                                         context=b"session/payload")
            self.assertFalse(encrypted.exists())
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_encryption_refuses_plaintext_over_maximum_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "payload.enc"
            with mock.patch.object(
                    crypto, "_MAX_PLAINTEXT_BYTES_PER_FILE", 2):
                with self.assertRaisesRegex(ValueError, "максимальний розмір"):
                    crypto.encrypt_bytes(b"abc", encrypted, os.urandom(32),
                                         context=b"session/payload")
            self.assertFalse(encrypted.exists())

    def test_decryption_refuses_more_than_maximum_chunks(self):
        payload = b"x" * (2 * crypto.CHUNK_SIZE + 1)
        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "payload.enc"
            decrypted = Path(tmp) / "payload"
            original = b"existing destination"
            decrypted.write_bytes(original)
            dek = os.urandom(32)
            crypto.encrypt_bytes(payload, encrypted, dek,
                                 context=b"session/payload")
            with mock.patch.object(crypto, "_MAX_CHUNKS_PER_FILE", 2):
                with self.assertRaisesRegex(ValueError, "меж.*чанків"):
                    crypto.decrypt_file(
                        encrypted, decrypted, dek, context=b"session/payload")
            self.assertEqual(decrypted.read_bytes(), original)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


@unittest.skipUnless(_HAS_CRYPTO, "cryptography не встановлена у цьому dev venv")
class FormatVersionTests(unittest.TestCase):
    def test_version_1_legacy_container_round_trips_without_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "payload"
            encrypted = Path(tmp) / "payload.enc"
            payload = b"legacy payload"
            dek = os.urandom(32)
            source.write_bytes(payload)

            crypto.encrypt_file(source, encrypted, dek)

            self.assertEqual(encrypted.read_bytes()[0], crypto._FORMAT_VERSION)
            self.assertEqual(crypto.decrypt_to_memory(encrypted, dek), payload)
            with self.assertRaisesRegex(ValueError, "v1 container"):
                crypto.decrypt_to_memory(
                    encrypted, dek, context=b"session/payload")

    def test_version_2_container_round_trips_with_matching_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "payload.enc"
            payload = b"context-bound payload"
            context = b"session/payload"
            dek = os.urandom(32)

            crypto.encrypt_bytes(payload, encrypted, dek, context=context)

            self.assertEqual(
                encrypted.read_bytes()[0], crypto._CONTEXT_FORMAT_VERSION)
            self.assertEqual(
                crypto.decrypt_to_memory(encrypted, dek, context=context),
                payload,
            )

    def test_unknown_future_version_is_rejected_before_decryption(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "payload"
            encrypted = Path(tmp) / "payload.enc"
            source.write_bytes(b"payload")
            dek = os.urandom(32)
            crypto.encrypt_file(source, encrypted, dek)
            container = bytearray(encrypted.read_bytes())
            container[0] = 3
            encrypted.write_bytes(container)

            with self.assertRaisesRegex(
                    ValueError, "невідома версія формату"):
                crypto.decrypt_to_memory(encrypted, dek)

    def test_version_2_cannot_be_downgraded_to_version_1(self):
        from cryptography.exceptions import InvalidTag

        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "payload.enc"
            dek = os.urandom(32)
            crypto.encrypt_bytes(
                b"payload", encrypted, dek, context=b"session/payload")
            container = bytearray(encrypted.read_bytes())
            container[0] = crypto._FORMAT_VERSION
            encrypted.write_bytes(container)

            with self.assertRaises(InvalidTag):
                crypto.decrypt_to_memory(encrypted, dek)

    def test_version_2_rejects_substituted_context(self):
        from cryptography.exceptions import InvalidTag

        with tempfile.TemporaryDirectory() as tmp:
            encrypted = Path(tmp) / "payload.enc"
            dek = os.urandom(32)
            crypto.encrypt_bytes(
                b"payload", encrypted, dek, context=b"session/original")

            with self.assertRaises(InvalidTag):
                crypto.decrypt_to_memory(
                    encrypted, dek, context=b"session/substituted")


@unittest.skipUnless(_HAS_CRYPTO, "cryptography не встановлена у цьому dev venv")
class OfficialPrimitiveVectorTests(unittest.TestCase):
    def test_nist_aes_256_gcm_test_case_14(self):
        # Source: NIST, "The Galois/Counter Mode of Operation (GCM)",
        # revised specification, Test Case 14 (AES-256), the vector document
        # accompanying the work standardized as SP 800-38D:
        # https://csrc.nist.gov/groups/ST/toolkit/BCM/documents/proposedmodes/gcm/gcm-revised-spec.pdf
        key = bytes.fromhex("00" * 32)
        nonce = bytes.fromhex("00" * 12)
        plaintext = bytes.fromhex("00" * 16)
        expected = bytes.fromhex(
            "cea7403d4d606b6e074ec5d3baf39d18"
            "d0d1c8a799996bf0265b98b5d48ab919"
        )

        aesgcm = crypto._aesgcm_class()(key)

        self.assertEqual(aesgcm.encrypt(nonce, plaintext, b""), expected)
        self.assertEqual(aesgcm.decrypt(nonce, expected, b""), plaintext)

    def test_rfc_7914_scrypt_first_vector_prefix(self):
        # Source: RFC 7914, section 12, first scrypt vector:
        # https://www.rfc-editor.org/rfc/rfc7914.html#section-12
        # The RFC asks for dkLen=64; Balachky's KEK wrapper deliberately returns
        # 32 bytes, so the comparison uses the first 32 reference bytes.
        expected = bytes.fromhex(
            "77d6576238657b203b19ca42c18a0497"
            "f16b4844e3074ae8dfdffa3fede21442"
        )

        actual = crypto._derive_kek(b"", b"", n=16, r=1, p=1)

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
