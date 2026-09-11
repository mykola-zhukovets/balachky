import asyncio
import inspect
import io
import logging
import secrets
import threading
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import (
    TelegramBadRequest as AiogramTelegramBadRequest,
    TelegramConflictError as AiogramTelegramConflictError,
    TelegramNetworkError as AiogramTelegramNetworkError,
    TelegramUnauthorizedError as AiogramTelegramUnauthorizedError,
)
from aiogram.methods import EditMessageText, GetMe, GetUpdates
from aiogram.types import BufferedInputFile

from fronts.telegram import bot
from fronts.telegram import service as telegram_service

try:
    from fronts.telegram.service import (
        CappedBytesIO,
        TelegramApiAdapter,
        TelegramFileTooLarge,
        TelegramIdentity,
        TelegramLogRecordFactory,
        TelegramOfflineError,
        TelegramRuntimeState,
        TelegramService,
    )
except ImportError:
    CappedBytesIO = TelegramApiAdapter = TelegramFileTooLarge = None
    TelegramIdentity = TelegramLogRecordFactory = TelegramOfflineError = None
    TelegramRuntimeState = TelegramService = None


class TelegramServiceSurfaceTests(unittest.TestCase):
    def test_service_exports_security_boundaries(self):
        # Сторож імпорту, не перевірка поведінки: вище при ImportError імена
        # стають None (опційна залежність aiogram), і тоді решта тестів файла
        # падала б з TypeError замість зрозумілого "сервіс не імпортується".
        # Поведінку кожного класу перевіряють тести нижче.
        self.assertIsNotNone(CappedBytesIO)
        self.assertIsNotNone(TelegramApiAdapter)
        self.assertIsNotNone(TelegramLogRecordFactory)
        self.assertIsNotNone(TelegramService)

    def test_runtime_states_are_fixed_non_secret_values(self):
        self.assertEqual(
            {state.name: state.value for state in TelegramRuntimeState},
            {
                "DISABLED": "disabled",
                "CHECKING": "checking",
                "PAIRING": "pairing",
                "ACTIVE": "active",
                "OFFLINE": "offline",
                "TOKEN_INVALID": "token-invalid",
                "BOT_IN_USE": "bot-in-use",
                "WEBHOOK_ACTIVE": "webhook-active",
                "SHUTDOWN_FAILED": "shutdown-failed",
            },
        )


class CappedBytesIOTests(unittest.TestCase):
    def test_rejects_chunk_before_actual_bytes_cross_limit(self):
        try:
            target = CappedBytesIO(max_bytes=4)
        except TypeError:
            self.fail("CappedBytesIO must accept an explicit byte limit")

        self.assertEqual(target.write(b"1234"), 4)
        with self.assertRaises(TelegramFileTooLarge):
            target.write(b"5")

        self.assertEqual(target.getvalue(), b"1234")

    def test_counts_actual_buffer_size_instead_of_cumulative_writes(self):
        target = CappedBytesIO(max_bytes=4)

        self.assertEqual(target.write(b"1234"), 4)
        target.seek(0)
        self.assertEqual(target.write(b"abcd"), 4)

        self.assertEqual(target.getvalue(), b"abcd")

    def test_writelines_cannot_bypass_actual_buffer_cap(self):
        target = CappedBytesIO(max_bytes=4)

        with self.assertRaises(TelegramFileTooLarge):
            target.writelines((b"12", b"345"))

        self.assertLessEqual(len(target.getvalue()), 4)


class TelegramApiAdapterTests(unittest.IsolatedAsyncioTestCase):
    TOKEN = "123456:SENTINEL_ADAPTER_TOKEN"

    def make_bot(self):
        return SimpleNamespace(
            get_me=AsyncMock(),
            get_webhook_info=AsyncMock(),
            get_updates=AsyncMock(),
            get_file=AsyncMock(),
            download_file=AsyncMock(),
            send_message=AsyncMock(),
            edit_message_text=AsyncMock(),
            send_document=AsyncMock(),
        )

    async def test_every_wrapper_audits_fixed_action_before_bot_call(self):
        cases = (
            ("get_me", (), {}, "verify-token"),
            ("get_webhook_info", (), {}, "polling"),
            (
                "get_updates",
                (),
                {"offset": 7, "timeout": 5, "allowed_updates": ["message"]},
                "polling",
            ),
            ("get_file", ("file-1",), {}, "voice-download"),
            ("download_file", ("opaque-path", io.BytesIO()), {}, "voice-download"),
            ("send_message", (10, "result"), {}, "send-result"),
            (
                "edit_message_text",
                ("done",),
                {"chat_id": 10, "message_id": 20},
                "send-result",
            ),
            ("send_document", (10, object()), {}, "send-result"),
        )

        for method_name, args, kwargs, expected_action in cases:
            with self.subTest(method=method_name):
                audit = []
                bot_api = self.make_bot()
                expected_result = object()

                async def operation(*_args, **_kwargs):
                    self.assertEqual(audit, [expected_action])
                    return expected_result

                getattr(bot_api, method_name).side_effect = operation
                adapter = TelegramApiAdapter(bot_api, audit.append)

                result = await getattr(adapter, method_name)(*args, **kwargs)

                self.assertIs(result, expected_result)
                self.assertEqual(audit, [expected_action])
                getattr(bot_api, method_name).assert_awaited_once_with(*args, **kwargs)

    async def test_underreported_download_is_stopped_by_actual_buffer_cap(self):
        audit = []
        bot_api = self.make_bot()
        destination = CappedBytesIO(max_bytes=4)

        async def download(_file_path, target):
            target.write(b"123")
            target.write(b"45")

        bot_api.download_file.side_effect = download
        adapter = TelegramApiAdapter(bot_api, audit.append)

        with self.assertRaises(TelegramFileTooLarge):
            await adapter.download_file("metadata-said-three-bytes", destination)

        self.assertEqual(destination.getvalue(), b"123")
        self.assertEqual(audit, ["voice-download"])

    async def test_aiogram_exceptions_map_to_fixed_typed_errors(self):
        cases = (
            (
                AiogramTelegramUnauthorizedError,
                "TelegramTokenInvalidError",
                TelegramRuntimeState.TOKEN_INVALID,
            ),
            (
                AiogramTelegramConflictError,
                "TelegramBotInUseError",
                TelegramRuntimeState.BOT_IN_USE,
            ),
            (
                AiogramTelegramNetworkError,
                "TelegramOfflineError",
                TelegramRuntimeState.OFFLINE,
            ),
        )

        for aiogram_error, mapped_name, expected_state in cases:
            with self.subTest(error=aiogram_error.__name__):
                audit = []
                bot_api = self.make_bot()
                bot_api.get_me.side_effect = aiogram_error(
                    GetMe(),
                    f"request failed at /bot{self.TOKEN}/getMe",
                )
                adapter = TelegramApiAdapter(bot_api, audit.append)
                mapped_type = getattr(telegram_service, mapped_name)

                with self.assertRaises(mapped_type) as caught:
                    await adapter.get_me()

                self.assertEqual(caught.exception.state, expected_state)
                self.assertNotIn(self.TOKEN, str(caught.exception))
                self.assertNotIn(
                    self.TOKEN,
                    "".join(traceback.format_exception(caught.exception)),
                )
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)
                self.assertEqual(audit, ["verify-token"])


class _RecordCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TelegramLogRecordFactoryTests(unittest.TestCase):
    TOKEN = "123456:SENTINEL_LOG_TOKEN"

    def setUp(self):
        self.previous_factory = logging.getLogRecordFactory()
        self.root = logging.getLogger()
        self.previous_root_level = self.root.level
        self.collector = _RecordCollector()
        self.collector.setFormatter(logging.Formatter("%(message)s"))
        self.root.addHandler(self.collector)
        self.root.setLevel(logging.DEBUG)
        self.logger = logging.getLogger("aiogram.client.session.security.child")
        self.previous_logger_level = self.logger.level
        self.previous_logger_propagate = self.logger.propagate
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = True

    def tearDown(self):
        logging.setLogRecordFactory(self.previous_factory)
        self.root.removeHandler(self.collector)
        self.root.setLevel(self.previous_root_level)
        self.logger.setLevel(self.previous_logger_level)
        self.logger.propagate = self.previous_logger_propagate

    def install_factory(self):
        factory = TelegramLogRecordFactory(self.TOKEN)
        factory.install()
        return factory

    def formatted_records(self):
        return "\n".join(self.collector.format(record) for record in self.collector.records)

    def test_child_logger_redacts_token_from_message_args_and_bot_url(self):
        self.install_factory()

        self.logger.error(f"direct token={self.TOKEN}")
        self.logger.error(
            "request token=%s url=%s",
            self.TOKEN,
            f"https://api.telegram.org/bot{self.TOKEN}/getMe",
        )

        rendered = self.formatted_records()
        self.assertNotIn(self.TOKEN, rendered)
        self.assertNotIn(f"/bot{self.TOKEN}", rendered)
        self.assertIn("[REDACTED]", rendered)
        for record in self.collector.records:
            self.assertNotIn(self.TOKEN, str(record.msg))
            self.assertNotIn(self.TOKEN, repr(record.args))

    def test_synthetic_exception_keeps_type_but_clears_raw_exc_info(self):
        self.install_factory()

        try:
            raise RuntimeError(
                f"synthetic request /bot{self.TOKEN}/getUpdates failed"
            )
        except RuntimeError:
            self.logger.exception("Telegram transport failed for %s", self.TOKEN)

        record = self.collector.records[-1]
        rendered = self.formatted_records()
        self.assertIsNone(record.exc_info)
        self.assertIn("RuntimeError", record.exc_text)
        self.assertIn("[REDACTED]", record.exc_text)
        self.assertNotIn(self.TOKEN, rendered)
        self.assertNotIn(self.TOKEN, record.exc_text)

    def test_materializes_and_redacts_object_values_and_dict_keys(self):
        token = self.TOKEN

        class TokenBearingObject:
            def __str__(self):
                return f"object={token}"

            def __repr__(self):
                return f"TokenBearingObject({token})"

        self.install_factory()

        self.logger.error(TokenBearingObject())
        self.logger.error("argument=%s", TokenBearingObject())
        self.logger.error("mapping=%s", {self.TOKEN: "safe-value"})

        rendered = self.formatted_records()
        self.assertNotIn(self.TOKEN, rendered)
        for record in self.collector.records:
            self.assertIsInstance(record.msg, str)
            self.assertEqual(record.args, ())
            self.assertNotIn(self.TOKEN, record.msg)

    def test_redacts_token_from_entire_chained_exception(self):
        self.install_factory()

        try:
            try:
                raise ValueError(f"inner request /bot{self.TOKEN}/getMe")
            except ValueError as inner:
                raise RuntimeError("outer transport failure") from inner
        except RuntimeError:
            self.logger.exception("Telegram transport failed")

        record = self.collector.records[-1]
        rendered = self.formatted_records()
        self.assertIsNone(record.exc_info)
        self.assertNotIn(self.TOKEN, rendered)
        self.assertNotIn(self.TOKEN, record.exc_text)

    def test_factory_chains_previous_factory_and_restores_it(self):
        calls = []

        def previous(*args, **kwargs):
            calls.append(True)
            record = self.previous_factory(*args, **kwargs)
            record.preexisting_marker = "kept"
            return record

        logging.setLogRecordFactory(previous)
        factory = TelegramLogRecordFactory(self.TOKEN)
        factory.install()

        self.logger.info("safe")
        factory.restore()

        self.assertTrue(calls)
        self.assertEqual(self.collector.records[-1].preexisting_marker, "kept")
        self.assertIs(logging.getLogRecordFactory(), previous)

    def test_nested_factories_restore_out_of_order_without_resurrection(self):
        original = logging.getLogRecordFactory()
        outer = TelegramLogRecordFactory("outer-token")
        outer.install()
        inner = TelegramLogRecordFactory("inner-token")
        inner.install()

        try:
            outer.restore()
            self.assertIs(logging.getLogRecordFactory(), inner)

            inner.restore()

            self.assertIs(logging.getLogRecordFactory(), original)
        finally:
            logging.setLogRecordFactory(original)

    def test_factory_install_is_idempotent_only_while_active(self):
        original = logging.getLogRecordFactory()
        factory = TelegramLogRecordFactory("single-use-token")

        try:
            factory.install()
            factory.install()

            self.assertIs(logging.getLogRecordFactory(), factory)
        finally:
            factory.restore()
            logging.setLogRecordFactory(original)

    def test_restored_factory_cannot_reinstall_into_its_descendant(self):
        original = logging.getLogRecordFactory()
        outer = TelegramLogRecordFactory("outer-token")
        outer.install()
        inner = TelegramLogRecordFactory("inner-token")
        inner.install()

        try:
            outer.restore()

            with self.assertRaises(telegram_service.TelegramServiceError) as caught:
                outer.install()

            self.assertEqual(
                type(caught.exception).__name__,
                "TelegramLogFactoryReinstallError",
            )
            self.assertEqual(
                str(caught.exception),
                "telegram-log-factory-reinstall",
            )
            self.logger.info("safe after rejected reinstall")
            inner.restore()
            self.assertIs(logging.getLogRecordFactory(), original)
        finally:
            logging.setLogRecordFactory(original)


class TelegramPairingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        original_factory = logging.getLogRecordFactory()
        self.addCleanup(logging.setLogRecordFactory, original_factory)

    def make_runtime(self, **kwargs):
        kwargs.setdefault("token", "123456:TEST_TOKEN")
        kwargs.setdefault("transcribe", lambda _audio, _cancel: None)
        bot_api = kwargs.pop("bot", None)
        if bot_api is None:
            bot_api = _FakeBot()

            async def block_updates(**_kwargs):
                await asyncio.Event().wait()

            bot_api.get_updates.side_effect = block_updates
        kwargs.setdefault("bot_factory", Mock(return_value=bot_api))
        kwargs.setdefault("dispatcher_factory", Mock(return_value=_FakeDispatcher()))
        kwargs.setdefault("text", _test_text)
        kwargs.setdefault("audit", Mock())
        runtime = TelegramService(**kwargs)
        self.addAsyncCleanup(runtime.stop)
        return runtime

    async def test_token_redactor_is_installed_before_bot_construction(self):
        previous = logging.getLogRecordFactory()
        factory_seen_by_bot = []

        def bot_factory(_token):
            factory_seen_by_bot.append(logging.getLogRecordFactory())
            return SimpleNamespace()

        try:
            TelegramService(
                token="123456:TEST_TOKEN",
                transcribe=lambda _audio, _cancel: None,
                bot_factory=bot_factory,
                audit=Mock(),
            )
        except TypeError:
            self.fail("TelegramService must install redaction before Bot construction")
        finally:
            logging.setLogRecordFactory(previous)

        self.assertIsInstance(factory_seen_by_bot[0], TelegramLogRecordFactory)

    async def test_service_starts_fail_closed_without_complete_identity(self):
        bot_factory = unittest.mock.Mock(return_value=SimpleNamespace())

        try:
            runtime = TelegramService(
                token="123456:TEST_TOKEN",
                transcribe=lambda _audio, _cancel: None,
                paired_user_id=10,
                paired_chat_id=0,
                bot_factory=bot_factory,
                audit=Mock(),
            )
        except TypeError:
            self.fail("TelegramService must expose the injected runtime boundary")

        self.assertIsNone(runtime.identity)
        self.assertEqual(runtime.state, TelegramRuntimeState.DISABLED)
        bot_factory.assert_called_once_with("123456:TEST_TOKEN")

    async def test_service_accepts_only_complete_positive_64_bit_identity(self):
        invalid_pairs = (
            (10, 0),
            (0, 10),
            (True, 10),
            (10, False),
            (-1, 10),
            (10, -1),
            (2**63, 10),
            (10, 2**63),
            ("10", 10),
            (10, None),
        )

        for user_id, chat_id in invalid_pairs:
            with self.subTest(user_id=user_id, chat_id=chat_id):
                runtime = self.make_runtime(
                    paired_user_id=user_id,
                    paired_chat_id=chat_id,
                )
                self.assertIsNone(runtime.identity)
                self.assertFalse(runtime._is_authorized(10, 10))

        runtime = self.make_runtime(paired_user_id=10, paired_chat_id=20)
        self.assertEqual(runtime.identity, TelegramIdentity(10, 20))
        self.assertTrue(runtime._is_authorized(10, 20))
        self.assertFalse(runtime._is_authorized(10, 21))
        self.assertFalse(runtime._is_authorized(11, 20))

    async def test_pairing_secret_uses_128_bits_and_is_single_use(self):
        paired = []
        try:
            runtime = self.make_runtime(on_paired=paired.append)
        except TypeError:
            self.fail("TelegramService must accept an on_paired callback")

        with patch(
            "fronts.telegram.service.secrets.token_urlsafe",
            return_value="A" * 22,
        ) as make_secret:
            secret = await runtime.begin_pairing(ttl_seconds=600)

        make_secret.assert_called_once_with(16)
        self.assertEqual(secret, "A" * 22)
        self.assertTrue(runtime.accept_pair(10, 10, "private", secret))
        self.assertFalse(runtime.accept_pair(11, 11, "private", secret))
        self.assertEqual(paired, [TelegramIdentity(10, 10)])
        runtime.bot.get_me.assert_awaited_once_with()
        self.assertEqual(runtime.state, TelegramRuntimeState.ACTIVE)

    async def test_new_pairing_secret_invalidates_previous_secret(self):
        with patch(
            "fronts.telegram.service.secrets.token_urlsafe",
            side_effect=("A" * 22, "B" * 22),
        ):
            runtime = self.make_runtime()
            old_secret = await runtime.begin_pairing(ttl_seconds=600)
            new_secret = await runtime.begin_pairing(ttl_seconds=600)

        self.assertFalse(runtime.accept_pair(10, 10, "private", old_secret))
        self.assertTrue(runtime.accept_pair(10, 10, "private", new_secret))

    async def test_reserved_pairing_rejects_concurrent_begin_until_accept_finishes(self):
        callback_entered = threading.Event()
        release_callback = threading.Event()
        accept_finished = threading.Event()
        persisted = []
        accept_results = []
        accept_errors = []

        def persist(identity):
            callback_entered.set()
            if not release_callback.wait(timeout=1):
                raise RuntimeError("pairing callback was not released")
            persisted.append(identity)

        runtime = self.make_runtime(on_paired=persist)
        old_secret = await runtime.begin_pairing(ttl_seconds=600)
        expected_identity = TelegramIdentity(10, 10)

        def accept():
            try:
                accept_results.append(
                    runtime.accept_pair(10, 10, "private", old_secret)
                )
            except BaseException as error:
                accept_errors.append(error)
            finally:
                accept_finished.set()

        worker = threading.Thread(target=accept, daemon=True)
        worker.start()

        self.assertTrue(
            callback_entered.wait(timeout=0.5),
            "pairing callback did not reserve the session",
        )
        reserved_pairing = runtime._pairing
        self.assertEqual(reserved_pairing.reserved_identity, expected_identity)

        try:
            with self.assertRaises(
                telegram_service.TelegramServiceError
            ) as caught:
                await runtime.begin_pairing(ttl_seconds=600)

            self.assertEqual(
                type(caught.exception).__name__,
                "TelegramPairingBusyError",
            )
            self.assertEqual(str(caught.exception), "telegram-pairing-busy")
            self.assertEqual(runtime.bot.get_me.await_count, 2)
            self.assertIs(runtime._pairing, reserved_pairing)
        finally:
            release_callback.set()
            accept_finished.wait(timeout=0.5)
            worker.join(timeout=0.5)

        self.assertTrue(
            accept_finished.is_set(),
            "accept_pair did not finish after callback release",
        )

        self.assertEqual(accept_errors, [])
        self.assertEqual(accept_results, [True])
        self.assertEqual(persisted, [expected_identity])
        self.assertEqual(runtime.identity, persisted[0])

        next_secret = await runtime.begin_pairing(ttl_seconds=600)

        self.assertIsInstance(next_secret, str)
        self.assertEqual(runtime.bot.get_me.await_count, 3)

    async def test_pairing_ttl_must_be_finite_and_positive(self):
        invalid_ttls = (
            float("nan"),
            float("inf"),
            float("-inf"),
            0.0,
            -1.0,
        )

        for ttl_seconds in invalid_ttls:
            with self.subTest(ttl_seconds=ttl_seconds):
                runtime = self.make_runtime()

                with self.assertRaises(telegram_service.TelegramServiceError) as caught:
                    await runtime.begin_pairing(ttl_seconds=ttl_seconds)

                self.assertEqual(
                    type(caught.exception).__name__,
                    "TelegramInvalidPairingTtlError",
                )
                self.assertEqual(str(caught.exception), "telegram-pairing-ttl-invalid")
                self.assertEqual(runtime.state, TelegramRuntimeState.DISABLED)
                self.assertIsNone(runtime._pairing)
                runtime.bot.get_me.assert_not_awaited()

    async def test_pairing_expires_at_exact_ttl_boundary(self):
        now = [100.0]
        runtime = self.make_runtime(clock=lambda: now[0])
        secret = await runtime.begin_pairing(ttl_seconds=600)

        now[0] = 700.0

        self.assertFalse(runtime.accept_pair(10, 10, "private", secret))
        self.assertIsNone(runtime.identity)

    async def test_pairing_is_valid_immediately_before_expiry(self):
        now = [100.0]
        runtime = self.make_runtime(clock=lambda: now[0])
        secret = await runtime.begin_pairing(ttl_seconds=600)

        now[0] = 699.999

        self.assertTrue(runtime.accept_pair(10, 10, "private", secret))

    async def test_compare_digest_runs_while_pairing_lock_is_held(self):
        runtime = self.make_runtime()
        secret = await runtime.begin_pairing(ttl_seconds=600)
        original_compare = secrets.compare_digest

        def compare(left, right):
            self.assertTrue(runtime._pairing_lock.locked())
            return original_compare(left, right)

        with patch("fronts.telegram.service.secrets.compare_digest", side_effect=compare):
            self.assertTrue(runtime.accept_pair(10, 10, "private", secret))

    async def test_non_private_or_invalid_identity_does_not_consume_secret(self):
        runtime = self.make_runtime()
        secret = await runtime.begin_pairing(ttl_seconds=600)

        self.assertFalse(runtime.accept_pair(10, 10, "group", secret))
        self.assertFalse(runtime.accept_pair(True, 10, "private", secret))
        self.assertFalse(runtime.accept_pair(10, 10, "private", None))
        self.assertTrue(runtime.accept_pair(10, 10, "private", secret))

    async def test_pairing_callback_runs_without_holding_pairing_lock(self):
        callback_lock_states = []
        runtime = self.make_runtime(
            on_paired=lambda _identity: callback_lock_states.append(
                runtime._pairing_lock.locked()
            )
        )
        secret = await runtime.begin_pairing(ttl_seconds=600)

        self.assertTrue(runtime.accept_pair(10, 10, "private", secret))
        self.assertEqual(callback_lock_states, [False])

    async def test_pairing_callback_can_reenter_without_second_winner(self):
        callback_results = []
        accept_results = []
        accept_finished = threading.Event()

        def persist(_identity):
            callback_results.append(
                runtime.accept_pair(20, 20, "private", secret)
            )

        runtime = self.make_runtime(on_paired=persist)
        secret = await runtime.begin_pairing(ttl_seconds=600)

        def accept():
            accept_results.append(
                runtime.accept_pair(10, 10, "private", secret)
            )
            accept_finished.set()

        worker = threading.Thread(target=accept, daemon=True)
        worker.start()

        self.assertTrue(
            accept_finished.wait(timeout=0.5),
            "reentrant accept_pair did not return promptly",
        )
        self.assertEqual(callback_results, [False])
        self.assertEqual(accept_results, [True])
        self.assertEqual(runtime.identity, TelegramIdentity(10, 10))

    async def test_pairing_callback_failure_rolls_back_and_allows_retry(self):
        callback_calls = []

        def persist(identity):
            callback_calls.append(identity)
            if len(callback_calls) == 1:
                raise RuntimeError("persistence failed")

        runtime = self.make_runtime(on_paired=persist)
        secret = await runtime.begin_pairing(ttl_seconds=600)

        with self.assertRaisesRegex(RuntimeError, "persistence failed"):
            runtime.accept_pair(10, 10, "private", secret)

        self.assertEqual(runtime.state, TelegramRuntimeState.PAIRING)
        self.assertIsNone(runtime.identity)
        self.assertFalse(runtime._is_authorized(10, 10))
        self.assertIsNotNone(runtime._pairing)
        self.assertFalse(runtime._pairing.used)

        self.assertTrue(runtime.accept_pair(10, 10, "private", secret))
        self.assertEqual(callback_calls, [TelegramIdentity(10, 10)] * 2)
        self.assertEqual(runtime.identity, TelegramIdentity(10, 10))
        self.assertEqual(runtime.state, TelegramRuntimeState.ACTIVE)

    async def test_concurrent_pairing_has_one_winner_and_one_callback(self):
        paired = []
        runtime = self.make_runtime(on_paired=paired.append)
        secret = await runtime.begin_pairing(ttl_seconds=600)
        contenders = 8
        barrier = threading.Barrier(contenders)

        def accept(user_id):
            barrier.wait()
            return runtime.accept_pair(user_id, user_id, "private", secret)

        results = await asyncio.gather(
            *(asyncio.to_thread(accept, user_id) for user_id in range(1, contenders + 1))
        )

        self.assertEqual(sum(results), 1)
        self.assertEqual(len(paired), 1)
        self.assertEqual(runtime.identity, paired[0])
        self.assertFalse(runtime.accept_pair(99, 99, "private", secret))


class TelegramLegacyContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_five_value_transcription_is_returned(self):
        message = AsyncMock()
        message.chat.id = 101
        message.from_user.id = 101
        message.voice.file_id = "voice-1"
        message.audio = None
        note = AsyncMock()
        message.reply.return_value = note

        async def fake_transcribe(_bot, _file_id):
            return "raw", "готовий текст", 1.0, [], []

        with patch.object(bot, "transcribe_voice", side_effect=fake_transcribe), \
                patch.object(bot, "ALLOWED", {101}):
            await bot.on_voice(message, AsyncMock())

        note.edit_text.assert_awaited_once_with("готовий текст")


def _test_text(key, **kwargs):
    if key == "telegram_transcript_filename":
        return f"transcript-{kwargs['timestamp']}.txt"
    if kwargs:
        rendered = ",".join(f"{name}={value}" for name, value in sorted(kwargs.items()))
        return f"{key}({rendered})"
    return key


class _FakeMessageObserver:
    def __init__(self):
        self.handlers = []

    def register(self, callback, *filters):
        self.handlers.append((callback, filters))


class _FakeDispatcher:
    def __init__(self):
        self.message = _FakeMessageObserver()
        self.feed_calls = []
        self.start_polling = Mock(side_effect=AssertionError("opaque polling used"))

    async def feed_update(self, bot_api, update):
        self.feed_calls.append((bot_api, update))
        message = update.message
        text = getattr(message, "text", None) or ""
        if text.startswith("/start "):
            callback_name = "_handle_pairing_start"
            extra = {"command": SimpleNamespace(args=text.split(maxsplit=1)[1])}
        elif text == "/start":
            callback_name = "_handle_start"
            extra = {}
        elif text == "/help":
            callback_name = "_handle_help"
            extra = {}
        elif text == "/status":
            callback_name = "_handle_status"
            extra = {}
        elif text == "/privacy":
            callback_name = "_handle_privacy"
            extra = {}
        elif getattr(message, "voice", None) is not None:
            callback_name = "handle_media"
            extra = {}
        else:
            return None

        for callback, _filters in self.message.handlers:
            if callback.__name__ == callback_name:
                return await callback(message, **extra)
        return None


class _FakeBot:
    def __init__(self):
        self.get_me = AsyncMock(return_value=SimpleNamespace(username="test_bot"))
        self.get_webhook_info = AsyncMock(return_value=SimpleNamespace(url=""))
        self.get_updates = AsyncMock()
        self.get_file = AsyncMock(return_value=SimpleNamespace(file_path="opaque-file"))
        self.download_file = AsyncMock(side_effect=self._download)
        self.send_message = AsyncMock(side_effect=self._send_message)
        self.edit_message_text = AsyncMock()
        self.send_document = AsyncMock()
        self.session = SimpleNamespace(close=AsyncMock())
        self._message_id = 1000

    async def _download(self, _file_path, destination):
        destination.write(b"voice")

    async def _send_message(self, _chat_id, _text):
        self._message_id += 1
        return SimpleNamespace(message_id=self._message_id)


def _voice_message(
    user_id=10,
    chat_id=20,
    *,
    chat_type="private",
    file_size=100,
    duration=30,
    file_id="voice-1",
    message_id=301,
):
    from_user = None if user_id is None else SimpleNamespace(id=user_id)
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=from_user,
        voice=SimpleNamespace(
            file_id=file_id,
            file_size=file_size,
            duration=duration,
        ),
        audio=None,
        document=None,
        message_id=message_id,
        text=None,
    )


def _command_message(text, user_id=10, chat_id=20, chat_type="private"):
    message = _voice_message(
        user_id=user_id,
        chat_id=chat_id,
        chat_type=chat_type,
    )
    message.voice = None
    message.text = text
    return message


class TelegramTask4BCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_factory = logging.getLogRecordFactory()
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in reversed(self.runtimes):
            try:
                await runtime.stop()
            except telegram_service.TelegramServiceError:
                pass
        logging.setLogRecordFactory(self.previous_factory)

    def make_runtime(self, **kwargs):
        fake_bot = kwargs.pop("bot", None) or _FakeBot()
        dispatcher = kwargs.pop("dispatcher", None) or _FakeDispatcher()
        kwargs.setdefault("token", "123456:TASK4B_TOKEN")
        kwargs.setdefault(
            "transcribe",
            lambda _audio, _cancelled: ("raw", "final", 1.0, [], []),
        )
        kwargs.setdefault("audit", Mock())
        kwargs.setdefault("text", _test_text)
        kwargs.setdefault("bot_factory", Mock(return_value=fake_bot))
        kwargs.setdefault("dispatcher_factory", Mock(return_value=dispatcher))
        try:
            runtime = TelegramService(**kwargs)
        except TypeError as error:
            self.fail(f"Task 4B injection boundary is missing: {error}")
        self.runtimes.append(runtime)
        return runtime, fake_bot, dispatcher

    async def wait_for(self, predicate, timeout=1.0):
        async def waiter():
            while not predicate():
                await asyncio.sleep(0)

        await asyncio.wait_for(waiter(), timeout=timeout)


class TelegramPollingAndRoutingTests(TelegramTask4BCase):
    async def test_clean_config_verifies_polls_and_deep_link_sets_exact_identity(self):
        paired = asyncio.Event()
        dispatcher = _FakeDispatcher()
        fake_bot = _FakeBot()
        updates_blocked = asyncio.Event()
        secret_seen = []

        async def get_updates(**_kwargs):
            if not secret_seen:
                await self.wait_for(lambda: runtime._pairing is not None)
                secret_seen.append(runtime._pairing.secret)
                return [
                    SimpleNamespace(
                        update_id=77,
                        message=_command_message(f"/start {secret_seen[0]}"),
                    )
                ]
            updates_blocked.set()
            await asyncio.Event().wait()

        fake_bot.get_updates.side_effect = get_updates
        runtime, _fake_bot, _dispatcher = self.make_runtime(
            bot=fake_bot,
            dispatcher=dispatcher,
            paired_user_id=0,
            paired_chat_id=0,
            on_paired=lambda _identity: paired.set(),
        )

        secret = await runtime.begin_pairing(ttl_seconds=600)
        await asyncio.wait_for(paired.wait(), timeout=1)
        await asyncio.wait_for(updates_blocked.wait(), timeout=1)

        self.assertEqual(secret, secret_seen[0])
        self.assertEqual(runtime.identity, TelegramIdentity(10, 20))
        self.assertEqual(runtime.state, TelegramRuntimeState.ACTIVE)
        fake_bot.get_me.assert_awaited_once_with()
        fake_bot.get_webhook_info.assert_awaited_once_with()
        first_poll = fake_bot.get_updates.await_args_list[0]
        self.assertEqual(first_poll.kwargs["timeout"], 5)
        self.assertEqual(first_poll.kwargs["allowed_updates"], ["message"])
        self.assertEqual(dispatcher.feed_calls[0][1].update_id, 77)
        self.assertEqual(runtime._poll_offset, 78)
        sent_texts = [call.args[1] for call in fake_bot.send_message.await_args_list]
        self.assertIn("telegram_reply_pair_success", sent_texts)
        dispatcher.start_polling.assert_not_called()

    async def test_registered_surface_has_commands_and_private_voice_only(self):
        runtime, _fake_bot, dispatcher = self.make_runtime()

        names = [callback.__name__ for callback, _ in dispatcher.message.handlers]

        self.assertEqual(
            names,
            [
                "_handle_pairing_start",
                "_handle_start",
                "_handle_help",
                "_handle_status",
                "_handle_privacy",
                "handle_media",
            ],
        )
        source = inspect.getsource(telegram_service.TelegramService._register_handlers)
        self.assertIn("F.voice", source)
        self.assertNotIn("F.audio", source)
        self.assertNotIn("F.document", source)
        self.assertNotIn("start_polling", inspect.getsource(telegram_service))
        self.assertNotIn("start_webhook", inspect.getsource(telegram_service))
        self.assertNotIn("run_webhook", inspect.getsource(telegram_service))
        self.assertIs(runtime.dispatcher, dispatcher)

    async def test_plain_commands_use_injected_text_and_status_requires_identity(self):
        text = Mock(side_effect=_test_text)
        runtime, fake_bot, _dispatcher = self.make_runtime(text=text)
        messages = (
            (_command_message("/start"), runtime._handle_start),
            (_command_message("/help"), runtime._handle_help),
            (_command_message("/privacy"), runtime._handle_privacy),
            (_command_message("/status"), runtime._handle_status),
        )

        for message, handler in messages:
            await handler(message)

        self.assertEqual(
            [call.args[0] for call in text.call_args_list],
            [
                "telegram_reply_start",
                "telegram_reply_help",
                "telegram_reply_privacy",
                "telegram_error_unauthorized",
            ],
        )
        self.assertEqual(fake_bot.send_message.await_count, 4)

    async def test_webhook_is_typed_terminal_and_starts_no_inbound_server(self):
        fake_bot = _FakeBot()
        fake_bot.get_webhook_info.return_value = SimpleNamespace(
            url="https://service.invalid/hook"
        )
        runtime, _fake_bot, dispatcher = self.make_runtime(bot=fake_bot)

        with self.assertRaises(
            telegram_service.TelegramWebhookActiveError
        ) as caught:
            await runtime.start()

        self.assertEqual(caught.exception.state, TelegramRuntimeState.WEBHOOK_ACTIVE)
        self.assertEqual(runtime.state, TelegramRuntimeState.WEBHOOK_ACTIVE)
        fake_bot.get_me.assert_awaited_once_with()
        fake_bot.get_webhook_info.assert_awaited_once_with()
        fake_bot.get_updates.assert_not_awaited()
        dispatcher.start_polling.assert_not_called()


class TelegramAdmissionTests(TelegramTask4BCase):
    async def test_acl_matrix_rejects_before_get_file(self):
        cases = (
            (None, 20, "private"),
            (11, 20, "private"),
            (10, 21, "private"),
            (10, 20, "group"),
        )

        for user_id, chat_id, chat_type in cases:
            with self.subTest(
                user_id=user_id,
                chat_id=chat_id,
                chat_type=chat_type,
            ):
                runtime, fake_bot, _dispatcher = self.make_runtime(
                    paired_user_id=10,
                    paired_chat_id=20,
                )
                await runtime.handle_media(
                    _voice_message(
                        user_id=user_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    )
                )

                fake_bot.get_file.assert_not_awaited()
                self.assertEqual(runtime.queue.qsize(), 0)
                self.assertIsNone(runtime._active_job)
                self.assertNotIn(
                    "telegram_progress_received",
                    [call.args[1] for call in fake_bot.send_message.await_args_list],
                )

    async def test_acl_precedes_metadata_and_queue_admission(self):
        runtime, fake_bot, _dispatcher = self.make_runtime(
            paired_user_id=10,
            paired_chat_id=20,
        )

        await runtime.handle_media(
            _voice_message(
                user_id=11,
                file_size=TelegramService.MAX_FILE_BYTES + 1,
                duration=TelegramService.MAX_DURATION_SECONDS + 1,
            )
        )

        self.assertEqual(
            [call.args[1] for call in fake_bot.send_message.await_args_list],
            ["telegram_error_unauthorized"],
        )
        fake_bot.get_file.assert_not_awaited()
        self.assertEqual(runtime.queue.qsize(), 0)
        self.assertIsNone(runtime._active_job)

    async def test_metadata_limits_accept_exact_boundaries_and_reject_plus_one(self):
        exact, exact_bot, _ = self.make_runtime(
            paired_user_id=10,
            paired_chat_id=20,
        )
        exact._ensure_worker_task()
        await exact.handle_media(
            _voice_message(
                file_size=TelegramService.MAX_FILE_BYTES,
                duration=TelegramService.MAX_DURATION_SECONDS,
            )
        )
        await self.wait_for(lambda: exact_bot.get_file.await_count == 1)

        for file_size, duration in (
            (TelegramService.MAX_FILE_BYTES + 1, 30),
            (100, TelegramService.MAX_DURATION_SECONDS + 1),
            (None, 30),
            (100, None),
        ):
            with self.subTest(file_size=file_size, duration=duration):
                runtime, fake_bot, _ = self.make_runtime(
                    paired_user_id=10,
                    paired_chat_id=20,
                )
                await runtime.handle_media(
                    _voice_message(file_size=file_size, duration=duration)
                )
                fake_bot.get_file.assert_not_awaited()
                self.assertEqual(runtime.queue.qsize(), 0)
                self.assertIsNone(runtime._active_job)
                self.assertNotIn(
                    "telegram_progress_received",
                    [call.args[1] for call in fake_bot.send_message.await_args_list],
                )

    async def test_underreported_stream_is_rejected_at_actual_byte_cap(self):
        fake_bot = _FakeBot()

        async def oversized_download(_file_path, destination):
            destination.write(b"x" * TelegramService.MAX_FILE_BYTES)
            destination.write(b"x")

        fake_bot.download_file.side_effect = oversized_download
        runtime, _fake_bot, _ = self.make_runtime(
            bot=fake_bot,
            paired_user_id=10,
            paired_chat_id=20,
        )
        runtime._ensure_worker_task()

        await runtime.handle_media(_voice_message(file_size=1))
        await self.wait_for(
            lambda: any(
                call.kwargs.get("text")
                == "telegram_error_file_too_large(limit_mib=10)"
                for call in fake_bot.edit_message_text.await_args_list
            )
        )

        runtime.transcribe.assert_not_called() if isinstance(runtime.transcribe, Mock) else None
        self.assertEqual(len(fake_bot.download_file.await_args.args[1].getvalue()), 10 * 1024 * 1024)

    async def test_atomic_budget_is_one_active_one_pending_and_third_rejected(self):
        entered = threading.Event()
        release = threading.Event()

        def transcribe(_audio, _cancelled):
            entered.set()
            release.wait(timeout=2)
            return "raw", "final", 1.0, [], []

        runtime, fake_bot, _ = self.make_runtime(
            transcribe=transcribe,
            paired_user_id=10,
            paired_chat_id=20,
        )
        runtime._ensure_worker_task()

        await asyncio.wait_for(runtime.handle_media(_voice_message(message_id=1)), 0.2)
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await asyncio.wait_for(runtime.handle_media(_voice_message(message_id=2)), 0.2)
        await asyncio.wait_for(runtime.handle_media(_voice_message(message_id=3)), 0.2)

        self.assertEqual(fake_bot.get_file.await_count, 1)
        self.assertEqual(runtime.queue.qsize(), 1)
        sent = [call.args[1] for call in fake_bot.send_message.await_args_list]
        self.assertEqual(sent.count("telegram_progress_received"), 2)
        self.assertEqual(sent.count("telegram_error_queue_full"), 1)
        release.set()


class TelegramWorkerOutputTests(TelegramTask4BCase):
    async def _run_transcript(self, final):
        runtime, fake_bot, _ = self.make_runtime(
            transcribe=lambda _audio, _cancelled: ("raw", final, 1.0, [], []),
            paired_user_id=10,
            paired_chat_id=20,
        )
        runtime._ensure_worker_task()
        await runtime.handle_media(_voice_message())
        await self.wait_for(
            lambda: fake_bot.send_document.await_count
            or any(call.args[1] == final for call in fake_bot.send_message.await_args_list)
        )
        return runtime, fake_bot

    async def test_progress_is_one_editable_message_and_short_result_uses_send_message(self):
        _runtime, fake_bot = await self._run_transcript("short result")

        self.assertEqual(
            [call.args[1] for call in fake_bot.send_message.await_args_list],
            ["telegram_progress_received", "short result"],
        )
        self.assertEqual(
            [call.kwargs["text"] for call in fake_bot.edit_message_text.await_args_list],
            ["telegram_progress_transcribing", "telegram_progress_done"],
        )
        progress_ids = {
            call.kwargs["message_id"]
            for call in fake_bot.edit_message_text.await_args_list
        }
        self.assertEqual(len(progress_ids), 1)
        fake_bot.send_document.assert_not_awaited()

    async def test_text_boundary_3800_is_message_and_3801_is_utf8_txt(self):
        exact = "а" * 3800
        _runtime, exact_bot = await self._run_transcript(exact)
        self.assertEqual(exact_bot.send_message.await_args_list[-1].args[1], exact)
        exact_bot.send_document.assert_not_awaited()

        long = "а" * 3801
        _runtime, long_bot = await self._run_transcript(long)
        long_bot.send_document.assert_awaited_once()
        document = long_bot.send_document.await_args.kwargs["document"]
        self.assertIsInstance(document, BufferedInputFile)
        self.assertEqual(document.data.decode("utf-8"), long)
        self.assertTrue(document.filename.startswith("transcript-"))
        self.assertTrue(document.filename.endswith(".txt"))
        result_messages = [
            call.args[1] for call in long_bot.send_message.await_args_list
        ]
        self.assertNotIn(long, result_messages)

    async def test_model_missing_and_generic_failures_use_fixed_text_without_raw_log(self):
        for error, expected_key in (
            (
                telegram_service.TelegramModelMissingError(),
                "telegram_error_model_missing",
            ),
            (RuntimeError("SENSITIVE RAW FAILURE"), "telegram_error_generic"),
        ):
            with self.subTest(error=type(error).__name__):
                records = []

                class Capture(logging.Handler):
                    def emit(self, record):
                        records.append(record.getMessage())

                logger = logging.getLogger("fronts.telegram.service")
                capture = Capture()
                logger.addHandler(capture)
                try:
                    def fail(_audio, _cancelled):
                        raise error

                    runtime, fake_bot, _ = self.make_runtime(
                        transcribe=fail,
                        paired_user_id=10,
                        paired_chat_id=20,
                    )
                    runtime._ensure_worker_task()
                    await runtime.handle_media(_voice_message())
                    await self.wait_for(
                        lambda: any(
                            call.kwargs.get("text") == expected_key
                            for call in fake_bot.edit_message_text.await_args_list
                        )
                    )
                finally:
                    logger.removeHandler(capture)

                self.assertNotIn("SENSITIVE RAW FAILURE", "\n".join(records))


class TelegramTransportTests(TelegramTask4BCase):
    async def test_only_offline_retries_with_bounded_backoff(self):
        sleeps = []
        fake_bot = _FakeBot()
        network_error = AiogramTelegramNetworkError(
            GetUpdates(timeout=5, allowed_updates=["message"]),
            "network secret",
        )
        token_error = AiogramTelegramUnauthorizedError(
            GetUpdates(timeout=5, allowed_updates=["message"]),
            "token secret",
        )
        fake_bot.get_updates.side_effect = [network_error, [], token_error]

        async def controlled_sleep(delay):
            sleeps.append(delay)

        runtime, _fake_bot, _ = self.make_runtime(
            bot=fake_bot,
            paired_user_id=10,
            paired_chat_id=20,
            sleep=controlled_sleep,
        )
        await runtime.start()
        await self.wait_for(lambda: runtime._poll_task.done())

        self.assertEqual(sleeps, [TelegramService.OFFLINE_BACKOFF_SECONDS[0]])
        self.assertEqual(runtime.state, TelegramRuntimeState.TOKEN_INVALID)
        self.assertEqual(fake_bot.get_updates.await_count, 3)

    async def test_conflict_is_terminal_without_backoff(self):
        sleeps = AsyncMock()
        fake_bot = _FakeBot()
        fake_bot.get_updates.side_effect = AiogramTelegramConflictError(
            GetUpdates(timeout=5, allowed_updates=["message"]),
            "conflict secret",
        )
        runtime, _fake_bot, _ = self.make_runtime(
            bot=fake_bot,
            paired_user_id=10,
            paired_chat_id=20,
            sleep=sleeps,
        )

        await runtime.start()
        await self.wait_for(lambda: runtime.state == TelegramRuntimeState.BOT_IN_USE)

        sleeps.assert_not_awaited()
        self.assertTrue(runtime._poll_task.done())


class TelegramShutdownTests(TelegramTask4BCase):
    async def test_wait_until_stopped_blocks_until_stop_even_before_start(self):
        runtime, _fake_bot, _ = self.make_runtime()
        waiter = asyncio.create_task(runtime.wait_until_stopped())

        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        await runtime.stop()
        await asyncio.wait_for(waiter, timeout=0.1)

    async def test_stop_cancels_inflight_poll_closes_session_and_is_idempotent(self):
        poll_started = asyncio.Event()
        poll_cancelled = asyncio.Event()
        fake_bot = _FakeBot()

        async def blocked_poll(**_kwargs):
            poll_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                poll_cancelled.set()
                raise

        fake_bot.get_updates.side_effect = blocked_poll
        runtime, _fake_bot, _ = self.make_runtime(
            bot=fake_bot,
            paired_user_id=10,
            paired_chat_id=20,
        )

        await runtime.start()
        await runtime.start()
        await asyncio.wait_for(poll_started.wait(), timeout=1)
        await runtime.stop()
        await runtime.stop()
        await asyncio.wait_for(runtime.wait_until_stopped(), timeout=0.1)

        self.assertTrue(poll_cancelled.is_set())
        fake_bot.get_me.assert_awaited_once_with()
        fake_bot.session.close.assert_awaited_once_with()
        self.assertIs(logging.getLogRecordFactory(), self.previous_factory)
        self.assertEqual(runtime.state, TelegramRuntimeState.DISABLED)

    async def test_stop_during_download_awaits_active_worker_without_transcribing(self):
        download_started = asyncio.Event()
        release_download = asyncio.Event()
        transcribe = Mock(return_value=("raw", "final", 1.0, [], []))
        fake_bot = _FakeBot()

        async def blocked_download(_path, destination):
            download_started.set()
            await release_download.wait()
            destination.write(b"voice")

        fake_bot.download_file.side_effect = blocked_download
        runtime, _fake_bot, _ = self.make_runtime(
            bot=fake_bot,
            transcribe=transcribe,
            paired_user_id=10,
            paired_chat_id=20,
        )
        await runtime.start()
        await runtime.handle_media(_voice_message())
        await asyncio.wait_for(download_started.wait(), timeout=1)

        stop_task = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        self.assertFalse(stop_task.done())
        release_download.set()
        await asyncio.wait_for(stop_task, timeout=1)

        transcribe.assert_not_called()
        fake_bot.session.close.assert_awaited_once_with()

    async def test_stop_drains_pending_and_cooperatively_cancels_active_stt(self):
        entered = threading.Event()

        def cooperative(_audio, should_cancel):
            entered.set()
            while not should_cancel():
                threading.Event().wait(0.001)
            raise RuntimeError("cooperative stop")

        runtime, fake_bot, _ = self.make_runtime(
            transcribe=cooperative,
            paired_user_id=10,
            paired_chat_id=20,
        )
        await runtime.start()
        await runtime.handle_media(_voice_message(message_id=1))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await runtime.handle_media(_voice_message(message_id=2))

        await asyncio.wait_for(runtime.stop(), timeout=1)

        self.assertEqual(runtime.queue.qsize(), 0)
        self.assertEqual(fake_bot.get_file.await_count, 1)
        self.assertTrue(
            any(
                call.kwargs.get("text") == "telegram_error_generic"
                for call in fake_bot.edit_message_text.await_args_list
            )
        )

    async def test_noncooperative_stt_raises_typed_shutdown_failure_then_can_finish(self):
        entered = threading.Event()
        release = threading.Event()

        def noncooperative(_audio, _should_cancel):
            entered.set()
            release.wait(timeout=2)
            return "raw", "final", 1.0, [], []

        runtime, fake_bot, _ = self.make_runtime(
            transcribe=noncooperative,
            paired_user_id=10,
            paired_chat_id=20,
            shutdown_timeout_seconds=0.01,
        )
        await runtime.start()
        await runtime.handle_media(_voice_message())
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))

        with self.assertRaises(
            telegram_service.TelegramShutdownFailedError
        ) as caught:
            await runtime.stop()

        self.assertEqual(caught.exception.state, TelegramRuntimeState.SHUTDOWN_FAILED)
        self.assertEqual(runtime.state, TelegramRuntimeState.SHUTDOWN_FAILED)
        fake_bot.session.close.assert_awaited_once_with()
        self.assertIs(logging.getLogRecordFactory(), self.previous_factory)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(runtime.wait_until_stopped(), timeout=0.01)

        release.set()
        await self.wait_for(lambda: runtime._worker_task.done())
        await runtime.stop()
        await asyncio.wait_for(runtime.wait_until_stopped(), timeout=0.1)

    async def test_begin_pairing_serializes_with_stop_and_leaves_no_zombie_poll_task(self):
        get_me_entered = asyncio.Event()
        release_get_me = asyncio.Event()
        poll_started = asyncio.Event()
        poll_cancelled = asyncio.Event()
        fake_bot = _FakeBot()

        async def blocking_get_me():
            get_me_entered.set()
            await release_get_me.wait()
            return SimpleNamespace(username="test_bot")

        async def blocked_poll(**_kwargs):
            poll_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                poll_cancelled.set()
                raise

        fake_bot.get_me.side_effect = blocking_get_me
        fake_bot.get_updates.side_effect = blocked_poll
        runtime, _fake_bot, _ = self.make_runtime(bot=fake_bot)

        pairing_task = asyncio.create_task(runtime.begin_pairing(ttl_seconds=600))
        await asyncio.wait_for(get_me_entered.wait(), timeout=1)

        stop_task = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        self.assertFalse(
            stop_task.done(),
            "stop() must wait for the in-flight begin_pairing() call",
        )

        release_get_me.set()
        secret = await asyncio.wait_for(pairing_task, timeout=1)
        self.assertIsInstance(secret, str)

        await asyncio.wait_for(poll_started.wait(), timeout=1)
        await asyncio.wait_for(stop_task, timeout=1)

        self.assertTrue(
            poll_cancelled.is_set(),
            "the poll task started by begin_pairing() must be cancelled by stop()",
        )
        self.assertTrue(runtime._closed)
        self.assertTrue(runtime._poll_task.done())
        self.assertTrue(runtime._stop_event.is_set())
        self.assertEqual(runtime.state, TelegramRuntimeState.DISABLED)

    async def test_stop_finishes_cleanup_when_pending_job_notification_fails(self):
        entered = threading.Event()

        def cooperative(_audio, should_cancel):
            entered.set()
            while not should_cancel():
                threading.Event().wait(0.001)
            raise RuntimeError("cooperative stop")

        runtime, fake_bot, _ = self.make_runtime(
            transcribe=cooperative,
            paired_user_id=10,
            paired_chat_id=20,
        )
        await runtime.start()
        await runtime.handle_media(_voice_message(message_id=1))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await runtime.handle_media(_voice_message(message_id=2))

        fake_bot.edit_message_text.side_effect = AiogramTelegramBadRequest(
            EditMessageText(chat_id=20, message_id=1002, text="x"),
            "message to edit not found",
        )

        await asyncio.wait_for(runtime.stop(), timeout=1)

        self.assertTrue(runtime._closed)
        self.assertEqual(runtime.state, TelegramRuntimeState.DISABLED)
        fake_bot.session.close.assert_awaited_once_with()
        self.assertIs(logging.getLogRecordFactory(), self.previous_factory)

    async def test_safe_error_lets_unexpected_bugs_propagate_instead_of_hiding_them(self):
        runtime, fake_bot, _ = self.make_runtime()
        job = telegram_service.TelegramJob(
            chat_id=20,
            message_id=1,
            file_id="f",
            file_size=1,
            duration_seconds=1,
            progress_message_id=1002,
        )
        fake_bot.edit_message_text.side_effect = AttributeError("formatter bug")

        with self.assertRaises(AttributeError):
            await runtime._safe_error(job, "telegram_error_generic")


class TelegramSharedServiceAndCliContractTests(unittest.TestCase):
    def test_shared_service_does_not_import_or_construct_engine(self):
        source = inspect.getsource(telegram_service)
        self.assertNotIn("from whisper_core.engine import Engine", source)
        self.assertNotIn("Engine(", source)

    def test_cli_callback_preserves_five_value_tuple_and_cancel_contract(self):
        result = ("raw", "final", 1.0, [("word", 0.9)], [(0.0, 1.0, "final")])
        engine = SimpleNamespace(transcribe=Mock(return_value=result))
        audio = io.BytesIO(b"voice")
        cancelled = Mock(return_value=False)

        with patch.object(bot, "get_engine", return_value=engine):
            actual = bot.transcribe_voice(audio, cancelled)

        self.assertEqual(actual, result)
        engine.transcribe.assert_called_once()
        args, kwargs = engine.transcribe.call_args
        self.assertIs(args[0], audio)
        self.assertIs(kwargs["should_cancel"], cancelled)

    def test_cli_is_thin_service_entry_and_keeps_environment_contract(self):
        source = inspect.getsource(bot)
        self.assertIn("WHISPER_TYPER_BOT_TOKEN", source)
        self.assertIn("WHISPER_TYPER_BOT_ALLOWED", source)
        self.assertIn("TelegramService(", source)
        self.assertNotIn("Dispatcher(", source)
        self.assertNotIn("start_polling", source)

    def test_resolve_single_allowed_id_rejects_multiple_ids(self):
        with self.assertRaises(SystemExit):
            bot._resolve_single_allowed_id({10, 20, 30})

    def test_resolve_single_allowed_id_defaults_to_pairing_mode(self):
        self.assertEqual(bot._resolve_single_allowed_id(set()), 0)

    def test_resolve_single_allowed_id_keeps_single_preset_id(self):
        self.assertEqual(bot._resolve_single_allowed_id({42}), 42)

    def test_main_stops_runtime_even_if_pairing_announcement_fails(self):
        source = inspect.getsource(bot.main)
        try_index = source.index("try:")
        announce_index = source.index("_announce_pairing(runtime)")
        finally_index = source.index("finally:")
        self.assertTrue(
            try_index < announce_index < finally_index,
            "_announce_pairing() must run inside the try/finally that stops "
            "the runtime, so a pairing failure still closes the session "
            "instead of leaking the poll task and bot session",
        )


class TelegramCliPairingAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def test_announces_pairing_link_when_no_identity_preset(self):
        runtime = SimpleNamespace(
            identity=None,
            begin_pairing=AsyncMock(return_value="SECRET123"),
            api=SimpleNamespace(
                get_me=AsyncMock(return_value=SimpleNamespace(username="my_bot"))
            ),
        )

        await bot._announce_pairing(runtime)

        runtime.begin_pairing.assert_awaited_once_with(
            ttl_seconds=telegram_service.TelegramService.PAIR_TTL_SECONDS
        )
        runtime.api.get_me.assert_awaited_once_with()

    async def test_skips_pairing_when_identity_already_preset(self):
        runtime = SimpleNamespace(
            identity=TelegramIdentity(10, 10),
            begin_pairing=AsyncMock(),
        )

        await bot._announce_pairing(runtime)

        runtime.begin_pairing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
