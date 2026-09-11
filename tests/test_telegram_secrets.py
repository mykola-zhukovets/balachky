import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_core import telegram_secrets


class TelegramSecretsTests(unittest.TestCase):
    TOKEN = "123456:ABC_secret"

    def test_roundtrip_never_writes_plain_token(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(telegram_secrets, "_protect",
                             side_effect=lambda value: value[::-1]), \
                patch.object(telegram_secrets, "_unprotect",
                             side_effect=lambda value: value[::-1]):
            path = Path(tmp) / "telegram-token.json"
            telegram_secrets.save_token(self.TOKEN, path=path)
            self.assertNotIn(self.TOKEN.encode("utf-8"), path.read_bytes())
            self.assertEqual(
                telegram_secrets.load_token(path=path), self.TOKEN)

    def test_missing_token_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telegram-token.json"
            self.assertIsNone(telegram_secrets.load_token(path=path))

    def test_delete_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telegram-token.json"
            path.write_text("x", encoding="utf-8")
            telegram_secrets.delete_token(path=path)
            telegram_secrets.delete_token(path=path)
            self.assertFalse(path.exists())

    def test_corrupt_secret_shapes_raise_typed_error(self):
        payloads = (
            [],
            {"version": 2, "ciphertext": "YQ=="},
            {"version": 1, "ciphertext": "%%%"},
            {"version": 1, "ciphertext": 123},
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "telegram-token.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(telegram_secrets.TelegramSecretError):
                    telegram_secrets.load_token(path=path)

    def test_save_uses_fsync_uuid_temp_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(telegram_secrets, "_protect",
                             return_value=b"ciphertext"), \
                patch.object(telegram_secrets.os, "fsync") as fsync, \
                patch.object(telegram_secrets.os, "replace",
                             wraps=os.replace) as replace:
            path = Path(tmp) / "telegram-token.json"
            telegram_secrets.save_token(self.TOKEN, path=path)

            fsync.assert_called_once()
            replace.assert_called_once()
            source, destination = map(Path, replace.call_args.args)
            self.assertEqual(destination, path)
            self.assertEqual(source.parent, path.parent)
            self.assertTrue(source.name.startswith(path.name + "."))
            self.assertTrue(source.name.endswith(".tmp"))
            uuid_hex = source.name[len(path.name) + 1:-4]
            self.assertEqual(len(uuid_hex), 32)
            int(uuid_hex, 16)

    def test_replace_failure_preserves_existing_and_hides_token(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(telegram_secrets, "_protect",
                             return_value=b"ciphertext"), \
                patch.object(telegram_secrets.os, "replace",
                             side_effect=OSError(f"failed {self.TOKEN}")):
            path = Path(tmp) / "telegram-token.json"
            original = b"existing-secret-envelope"
            path.write_bytes(original)

            with self.assertRaises(telegram_secrets.TelegramSecretError) as raised:
                telegram_secrets.save_token(self.TOKEN, path=path)

            self.assertNotIn(self.TOKEN, str(raised.exception))
            rendered = "".join(traceback.format_exception(raised.exception))
            self.assertNotIn(self.TOKEN, rendered)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_token_error_does_not_echo_value(self):
        value = "not-a-valid-secret"
        with self.assertRaises(telegram_secrets.TelegramSecretError) as raised:
            telegram_secrets.save_token(value)
        self.assertNotIn(value, str(raised.exception))

    @unittest.skipUnless(os.name == "nt", "DPAPI існує лише у Windows")
    def test_real_windows_dpapi_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telegram-token.json"
            telegram_secrets.save_token(self.TOKEN, path=path)
            self.assertEqual(
                telegram_secrets.load_token(path=path), self.TOKEN)
            self.assertNotIn(self.TOKEN.encode("utf-8"), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
