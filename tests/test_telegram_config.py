import tempfile
import unittest
from pathlib import Path

from whisper_core.config import Config


class TelegramConfigTests(unittest.TestCase):
    MAX_ID = 2**63 - 1

    def _load(self, text: str) -> Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return Config.load(path)

    def assert_reset(self, cfg: Config):
        self.assertFalse(cfg.telegram_enabled)
        self.assertEqual(cfg.telegram_user_id, 0)
        self.assertEqual(cfg.telegram_chat_id, 0)

    def test_defaults_are_disabled_and_unpaired(self):
        self.assert_reset(Config())

    def test_non_secret_state_roundtrips_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = Config()
            cfg.telegram_enabled = True
            cfg.telegram_user_id = 1234567890123
            cfg.telegram_chat_id = 1234567890123
            cfg.bot_token = "123456:must-never-be-saved"

            cfg.save(path)

            text = path.read_text(encoding="utf-8")
            self.assertNotIn("bot_token", text)
            self.assertNotIn("123456:must-never-be-saved", text)
            loaded = Config.load(path)
            self.assertTrue(loaded.telegram_enabled)
            self.assertEqual(loaded.telegram_user_id, 1234567890123)
            self.assertEqual(loaded.telegram_chat_id, 1234567890123)
            self.assertFalse(hasattr(loaded, "bot_token"))

    def test_enabled_requires_exact_bool(self):
        for raw in ('"false"', "1"):
            with self.subTest(raw=raw):
                cfg = self._load(
                    f"telegram_enabled = {raw}\n"
                    "telegram_user_id = 101\n"
                    "telegram_chat_id = 202\n")
                self.assert_reset(cfg)

    def test_ids_require_exact_int_in_signed_64_bit_range(self):
        invalid_values = ('"101"', "101.0", "true", "-1",
                          str(self.MAX_ID + 1))
        for field in ("telegram_user_id", "telegram_chat_id"):
            for raw in invalid_values:
                with self.subTest(field=field, raw=raw):
                    values = {
                        "telegram_user_id": "101",
                        "telegram_chat_id": "202",
                    }
                    values[field] = raw
                    cfg = self._load(
                        "telegram_enabled = false\n"
                        f"telegram_user_id = {values['telegram_user_id']}\n"
                        f"telegram_chat_id = {values['telegram_chat_id']}\n")
                    self.assert_reset(cfg)

    def test_partial_pair_resets_all_fields_atomically(self):
        for user_id, chat_id in ((101, 0), (0, 202)):
            with self.subTest(user_id=user_id, chat_id=chat_id):
                cfg = self._load(
                    "telegram_enabled = false\n"
                    f"telegram_user_id = {user_id}\n"
                    f"telegram_chat_id = {chat_id}\n")
                self.assert_reset(cfg)

    def test_enabled_without_positive_pair_normalizes_to_disabled(self):
        cfg = self._load(
            "telegram_enabled = true\n"
            "telegram_user_id = 0\n"
            "telegram_chat_id = 0\n")
        self.assert_reset(cfg)

    def test_valid_disabled_pair_is_preserved(self):
        cfg = self._load(
            "telegram_enabled = false\n"
            f"telegram_user_id = {self.MAX_ID}\n"
            f"telegram_chat_id = {self.MAX_ID}\n")
        self.assertFalse(cfg.telegram_enabled)
        self.assertEqual(cfg.telegram_user_id, self.MAX_ID)
        self.assertEqual(cfg.telegram_chat_id, self.MAX_ID)


if __name__ == "__main__":
    unittest.main()
