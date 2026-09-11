"""Tests for fronts.desktop.telegram_controller."""

import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fronts.desktop.telegram_controller import (
    TelegramController,
)
from fronts.telegram.service import (
    TelegramIdentity,
    TelegramRuntimeState,
)
from whisper_core import telegram_secrets


class FakeRuntime:
    def __init__(
        self,
        token="123456:FAKE_TOKEN",
        bot_username="balachky_test_bot",
        cancel_event=None,
        identity=None,
        on_paired=None,
        transcribe=None,
        audit=None,
        shutdown_timeout_seconds=5.0,
    ):
        self.token = token
        self.bot_username = bot_username
        self.cancel_event = cancel_event or threading.Event()
        self.identity = identity
        self.on_paired = on_paired
        self.transcribe = transcribe
        self.audit = audit
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.stop_called = False
        self.polling_ready = threading.Event()
        self._stopped_event = asyncio.Event()
        self._pairing_secret = ""

    async def wait_until_stopped(self):
        self.polling_ready.set()
        await self._stopped_event.wait()

    async def start_normal_polling(self):
        self.polling_ready.set()

    async def verify_token(self, token: str):
        if not token or ":" not in token:
            raise ValueError("invalid token")
        return SimpleNamespace(username=self.bot_username, id=999)

    async def begin_pairing(self, ttl_seconds: float = 600.0):
        self._pairing_secret = "test_secret_12345"
        self.polling_ready.set()
        return f"https://t.me/{self.bot_username}?start={self._pairing_secret}"

    def feed_deep_link_start(self, user_id: int, chat_id: int, url: str):
        if self.on_paired:
            self.on_paired(TelegramIdentity(user_id, chat_id))

    async def stop(self):
        self.stop_called = True
        self._stopped_event.set()


def make_controller(
    runtime=None,
    telegram_enabled=False,
    telegram_user_id=0,
    telegram_chat_id=0,
    state_callback=None,
):
    cfg = SimpleNamespace(
        telegram_enabled=telegram_enabled,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        save=Mock(),
    )
    fake_runtime = runtime or FakeRuntime()

    def factory(**kwargs):
        fake_runtime.token = kwargs.get("token", fake_runtime.token)
        fake_runtime.on_paired = kwargs.get("on_paired", fake_runtime.on_paired)
        fake_runtime.transcribe = kwargs.get("transcribe", fake_runtime.transcribe)
        return fake_runtime

    controller = TelegramController(
        cfg=cfg,
        transcribe=lambda _audio, _cancel: ("raw", "final", 1.0, [], []),
        state_callback=state_callback,
        runtime_factory=factory,
    )
    return controller


class TelegramControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_path = Path(self.tmp.name) / "telegram-token.json"
        self.secrets_patch = patch.object(
            telegram_secrets, "_default_path", return_value=self.token_path
        )
        self.protect_patch = patch.object(
            telegram_secrets, "_protect", side_effect=lambda b: b[::-1]
        )
        self.unprotect_patch = patch.object(
            telegram_secrets, "_unprotect", side_effect=lambda b: b[::-1]
        )
        self.secrets_patch.start()
        self.protect_patch.start()
        self.unprotect_patch.start()

    def tearDown(self):
        self.unprotect_patch.stop()
        self.protect_patch.stop()
        self.secrets_patch.stop()
        self.tmp.cleanup()

    def test_disabled_config_does_not_start_thread(self):
        controller = TelegramController(
            cfg=SimpleNamespace(telegram_enabled=False),
            transcribe=lambda _audio, _cancel: None,
            state_callback=lambda _state: None,
            runtime_factory=Mock(),
        )
        controller.start_if_enabled()
        self.assertFalse(controller.is_running())

    def test_stop_joins_background_thread(self):
        runtime = FakeRuntime()
        controller = make_controller(runtime=runtime)
        controller.ensure_loop_ready("123456:TEST_TOKEN")
        self.assertTrue(controller.is_running())
        self.assertTrue(controller.stop(timeout=3.0))
        self.assertFalse(controller.is_running())
        self.assertTrue(runtime.stop_called)

    def test_fresh_config_can_verify_start_pairing_and_persist_identity(self):
        runtime = FakeRuntime(bot_username="balachky_test_bot")
        controller = make_controller(
            runtime=runtime,
            telegram_enabled=False,
            telegram_user_id=0,
            telegram_chat_id=0,
        )
        controller.verify_and_save_token("123456:TEST_TOKEN")
        time.sleep(0.1)  # wait for callback
        self.assertEqual(telegram_secrets.load_token(), "123456:TEST_TOKEN")
        url = controller.begin_pairing()
        self.assertTrue(runtime.polling_ready.wait(2.0))
        self.assertTrue(url.startswith("https://t.me/balachky_test_bot?start="))
        runtime.feed_deep_link_start(user_id=77, chat_id=77, url=url)
        self.assertEqual(controller.cfg.telegram_user_id, 77)
        self.assertEqual(controller.cfg.telegram_chat_id, 77)
        self.assertTrue(controller.cfg.telegram_enabled)
        self.assertTrue(controller.stop())

    def test_disconnect_clears_state_and_token(self):
        telegram_secrets.save_token("123456:TEST_TOKEN")
        runtime = FakeRuntime()
        controller = make_controller(
            runtime=runtime,
            telegram_enabled=True,
            telegram_user_id=88,
            telegram_chat_id=88,
        )
        controller.start_if_enabled()
        self.assertTrue(controller.is_running())
        self.assertTrue(controller.disconnect())
        self.assertFalse(controller.is_running())
        self.assertIsNone(telegram_secrets.load_token())
        self.assertFalse(controller.cfg.telegram_enabled)
        self.assertEqual(controller.cfg.telegram_user_id, 0)
        self.assertEqual(controller.cfg.telegram_chat_id, 0)

    def test_state_callback_receives_updates(self):
        events = []
        runtime = FakeRuntime()
        controller = make_controller(
            runtime=runtime,
            state_callback=lambda st: events.append(st.copy()),
        )
        controller.ensure_loop_ready("123456:TEST_TOKEN")
        self.assertTrue(controller.is_running())
        controller.set_enabled(False)
        self.assertFalse(controller.is_running())
        self.assertTrue(any(ev.get("state") == TelegramRuntimeState.DISABLED.value for ev in events))


if __name__ == "__main__":
    unittest.main()
