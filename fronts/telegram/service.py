"""Shared, memory-only Telegram transcription service."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import secrets
import threading
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramAPIError as AiogramTelegramAPIError,
    TelegramConflictError as AiogramTelegramConflictError,
    TelegramNetworkError as AiogramTelegramNetworkError,
    TelegramUnauthorizedError as AiogramTelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile


class TelegramRuntimeState(str, Enum):
    DISABLED = "disabled"
    CHECKING = "checking"
    PAIRING = "pairing"
    ACTIVE = "active"
    OFFLINE = "offline"
    TOKEN_INVALID = "token-invalid"
    BOT_IN_USE = "bot-in-use"
    WEBHOOK_ACTIVE = "webhook-active"
    SHUTDOWN_FAILED = "shutdown-failed"


class TelegramServiceError(Exception):
    """Base class for fixed, non-secret service errors."""


class TelegramFileTooLarge(TelegramServiceError):
    """The downloaded stream exceeded the in-memory byte cap."""


class TelegramInvalidPairingTtlError(TelegramServiceError):
    """The requested pairing lifetime is invalid."""

    def __init__(self):
        super().__init__("telegram-pairing-ttl-invalid")


class TelegramPairingBusyError(TelegramServiceError):
    """The current pairing identity is being persisted."""

    def __init__(self):
        super().__init__("telegram-pairing-busy")


class TelegramLogFactoryReinstallError(TelegramServiceError):
    """A restored Telegram log factory cannot be installed again."""

    def __init__(self):
        super().__init__("telegram-log-factory-reinstall")


class TelegramOfflineError(TelegramServiceError):
    """Telegram could not be reached."""

    state = TelegramRuntimeState.OFFLINE

    def __init__(self):
        super().__init__("telegram-offline")


class TelegramTokenInvalidError(TelegramServiceError):
    """Telegram rejected the configured bot token."""

    state = TelegramRuntimeState.TOKEN_INVALID

    def __init__(self):
        super().__init__("telegram-token-invalid")


class TelegramBotInUseError(TelegramServiceError):
    """Another consumer already owns this bot's update stream."""

    state = TelegramRuntimeState.BOT_IN_USE

    def __init__(self):
        super().__init__("telegram-bot-in-use")


class TelegramWebhookActiveError(TelegramServiceError):
    """A webhook already owns the bot's inbound update stream."""

    state = TelegramRuntimeState.WEBHOOK_ACTIVE

    def __init__(self):
        super().__init__("telegram-webhook-active")


class TelegramModelMissingError(TelegramServiceError):
    """The local recognition model is unavailable."""

    def __init__(self):
        super().__init__("telegram-model-missing")


class TelegramShutdownFailedError(TelegramServiceError):
    """The active transcription did not stop within the owned timeout."""

    state = TelegramRuntimeState.SHUTDOWN_FAILED

    def __init__(self):
        super().__init__("telegram-shutdown-failed")


def _key_text(key: str, **_kwargs: Any) -> str:
    return key


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: int
    chat_id: int


@dataclass
class PairingSession:
    secret: str
    expires_at: float
    used: bool = False
    reserved_identity: TelegramIdentity | None = None


@dataclass(frozen=True)
class TelegramJob:
    chat_id: int
    message_id: int
    file_id: str
    file_size: int
    duration_seconds: int
    progress_message_id: int


class CappedBytesIO(io.BytesIO):
    def __init__(self, max_bytes: int):
        super().__init__()
        self._max_bytes = max_bytes

    def write(self, data: bytes) -> int:
        resulting_size = max(len(self.getbuffer()), self.tell() + len(data))
        if resulting_size > self._max_bytes:
            raise TelegramFileTooLarge()
        return super().write(data)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)


class TelegramLogRecordFactory:
    REDACTED = "[REDACTED]"
    _factory_lock = threading.RLock()

    def __init__(self, token: str):
        self._token = token
        self._previous = logging.getLogRecordFactory()
        self._active = False
        self._restored = False

    def __call__(self, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = self._previous(*args, **kwargs)
        if not self._active:
            return record
        try:
            record.msg = self._redact_text(record.getMessage())
        except BaseException:
            record.msg = f"Telegram log message {self.REDACTED}"
        record.args = ()
        if record.stack_info is not None:
            record.stack_info = self._safe_text(record.stack_info)
        if record.exc_info:
            try:
                exception_text = "".join(
                    traceback.format_exception(*record.exc_info)
                )
            except BaseException:
                exception_text = f"Telegram exception {self.REDACTED}"
            record.exc_info = None
            record.exc_text = self._redact_text(exception_text)
        elif record.exc_text is not None:
            record.exc_text = self._safe_text(record.exc_text)
        return record

    def _redact_text(self, value: str) -> str:
        if not self._token:
            return value
        value = value.replace(
            f"/bot{self._token}",
            f"/bot{self.REDACTED}",
        )
        return value.replace(self._token, self.REDACTED)

    def _safe_text(self, value: Any) -> str:
        try:
            return self._redact_text(str(value))
        except BaseException:
            return self.REDACTED

    def install(self) -> None:
        with self._factory_lock:
            if self._active:
                return
            if self._restored:
                raise TelegramLogFactoryReinstallError()
            self._previous = logging.getLogRecordFactory()
            self._active = True
            logging.setLogRecordFactory(self)

    def restore(self) -> None:
        with self._factory_lock:
            self._active = False
            self._restored = True
            if logging.getLogRecordFactory() is not self:
                return
            previous = self._previous
            while (
                isinstance(previous, TelegramLogRecordFactory)
                and not previous._active
            ):
                previous = previous._previous
            logging.setLogRecordFactory(previous)


class TelegramApiAdapter:
    VERIFY_TOKEN = "verify-token"
    POLLING = "polling"
    VOICE_DOWNLOAD = "voice-download"
    SEND_RESULT = "send-result"

    def __init__(self, bot: Any, audit: Callable[[str], None]):
        self.bot = bot
        self._audit = audit

    async def call(self, action: str, awaitable_factory: Callable[[], Any]) -> Any:
        self._audit(action)
        mapped_error = None
        try:
            return await awaitable_factory()
        except AiogramTelegramUnauthorizedError:
            mapped_error = TelegramTokenInvalidError()
        except AiogramTelegramConflictError:
            mapped_error = TelegramBotInUseError()
        except AiogramTelegramNetworkError:
            mapped_error = TelegramOfflineError()
        mapped_error.__suppress_context__ = True
        raise mapped_error

    async def get_me(self) -> Any:
        return await self.call(self.VERIFY_TOKEN, self.bot.get_me)

    async def get_webhook_info(self) -> Any:
        return await self.call(self.POLLING, self.bot.get_webhook_info)

    async def get_updates(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call(
            self.POLLING,
            lambda: self.bot.get_updates(*args, **kwargs),
        )

    async def get_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call(
            self.VOICE_DOWNLOAD,
            lambda: self.bot.get_file(*args, **kwargs),
        )

    async def download_file(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call(
            self.VOICE_DOWNLOAD,
            lambda: self.bot.download_file(*args, **kwargs),
        )

    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call(
            self.SEND_RESULT,
            lambda: self.bot.send_message(*args, **kwargs),
        )

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call(
            self.SEND_RESULT,
            lambda: self.bot.edit_message_text(*args, **kwargs),
        )

    async def send_document(self, *args: Any, **kwargs: Any) -> Any:
        return await self.call(
            self.SEND_RESULT,
            lambda: self.bot.send_document(*args, **kwargs),
        )


class TelegramService:
    MAX_TELEGRAM_ID = 2**63 - 1
    MAX_FILE_BYTES = 10 * 1024 * 1024
    MAX_DURATION_SECONDS = 10 * 60
    MAX_PENDING = 1
    PAIR_TTL_SECONDS = 600
    TEXT_MESSAGE_LIMIT = 3800
    OFFLINE_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __init__(
        self,
        *,
        token: str,
        transcribe: Callable[..., Any],
        audit: Callable[[str], None],
        paired_user_id: int = 0,
        paired_chat_id: int = 0,
        on_paired: Callable[[TelegramIdentity], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        bot_factory: Callable[[str], Any] = Bot,
        dispatcher_factory: Callable[[], Any] = Dispatcher,
        text: Callable[..., str] = _key_text,
        sleep: Callable[[float], Any] = asyncio.sleep,
        shutdown_timeout_seconds: float = 30.0,
    ):
        self.state = TelegramRuntimeState.DISABLED
        self.identity = (
            TelegramIdentity(paired_user_id, paired_chat_id)
            if self._valid_identity(paired_user_id, paired_chat_id)
            else None
        )
        self.transcribe = transcribe
        self.text = text
        self._clock = clock
        self._sleep = sleep
        self._on_paired = on_paired
        self._pairing = None
        self._pairing_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._poll_task = None
        self._poll_offset = None
        self.queue = asyncio.Queue(maxsize=self.MAX_PENDING)
        self._active_job = None
        self._work_event = asyncio.Event()
        self._worker_task = None
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._closed = False
        self._session_closed = False
        self._stopped_event = asyncio.Event()
        self.dispatcher = dispatcher_factory()
        self._register_handlers()
        self._log_factory = TelegramLogRecordFactory(token)
        self._log_factory.install()
        try:
            self.bot = bot_factory(token)
        except BaseException:
            self._log_factory.restore()
            raise
        self.api = TelegramApiAdapter(self.bot, audit)

    @classmethod
    def _valid_identity(cls, user_id: Any, chat_id: Any) -> bool:
        return all(
            type(value) is int and 0 < value <= cls.MAX_TELEGRAM_ID
            for value in (user_id, chat_id)
        )

    def _is_authorized(self, user_id: Any, chat_id: Any) -> bool:
        return (
            self.identity is not None
            and self._valid_identity(user_id, chat_id)
            and self.identity == TelegramIdentity(user_id, chat_id)
        )

    def _register_handlers(self) -> None:
        private_chat = F.chat.type == "private"
        self.dispatcher.message.register(
            self._handle_pairing_start,
            CommandStart(deep_link=True),
            private_chat,
        )
        self.dispatcher.message.register(
            self._handle_start,
            CommandStart(),
            private_chat,
        )
        self.dispatcher.message.register(
            self._handle_help,
            Command("help"),
            private_chat,
        )
        self.dispatcher.message.register(
            self._handle_status,
            Command("status"),
            private_chat,
        )
        self.dispatcher.message.register(
            self._handle_privacy,
            Command("privacy"),
            private_chat,
        )
        self.dispatcher.message.register(
            self.handle_media,
            private_chat,
            F.voice,
        )

    @staticmethod
    def _message_identity(message: Any) -> tuple[Any, Any]:
        from_user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        return getattr(from_user, "id", None), getattr(chat, "id", None)

    async def _send_text(self, message: Any, key: str, **kwargs: Any) -> Any:
        _user_id, chat_id = self._message_identity(message)
        if not self._valid_identity(chat_id, chat_id):
            return None
        return await self.api.send_message(chat_id, self.text(key, **kwargs))

    async def _handle_pairing_start(
        self,
        message: Any,
        command: Any = None,
    ) -> None:
        secret = getattr(command, "args", None)
        if not secret:
            text = getattr(message, "text", "") or ""
            _command, separator, secret = text.partition(" ")
            if not separator:
                secret = ""
        user_id, chat_id = self._message_identity(message)
        chat_type = getattr(getattr(message, "chat", None), "type", "")
        try:
            accepted = self.accept_pair(user_id, chat_id, chat_type, secret)
        except Exception:
            accepted = False
        await self._send_text(
            message,
            "telegram_reply_pair_success"
            if accepted
            else "telegram_error_pair_link",
        )

    async def _handle_start(self, message: Any) -> None:
        await self._send_text(message, "telegram_reply_start")

    async def _handle_help(self, message: Any) -> None:
        await self._send_text(
            message,
            "telegram_reply_help",
            limit_mib=self.MAX_FILE_BYTES // (1024 * 1024),
            limit_minutes=self.MAX_DURATION_SECONDS // 60,
        )

    async def _handle_status(self, message: Any) -> None:
        user_id, chat_id = self._message_identity(message)
        key = (
            "telegram_reply_status"
            if self._is_authorized(user_id, chat_id)
            else "telegram_error_unauthorized"
        )
        await self._send_text(message, key)

    async def _handle_privacy(self, message: Any) -> None:
        await self._send_text(message, "telegram_reply_privacy")

    async def handle_media(self, message: Any) -> None:
        chat = getattr(message, "chat", None)
        if getattr(chat, "type", None) != "private":
            await self._send_text(message, "telegram_error_private_only")
            return

        user_id, chat_id = self._message_identity(message)
        if not self._is_authorized(user_id, chat_id):
            await self._send_text(message, "telegram_error_unauthorized")
            return

        voice = getattr(message, "voice", None)
        if voice is None:
            await self._send_text(message, "telegram_reply_unsupported")
            return

        file_size = getattr(voice, "file_size", None)
        if (
            type(file_size) is not int
            or file_size < 0
            or file_size > self.MAX_FILE_BYTES
        ):
            await self._send_text(
                message,
                "telegram_error_file_too_large",
                limit_mib=self.MAX_FILE_BYTES // (1024 * 1024),
            )
            return

        duration = getattr(voice, "duration", None)
        if (
            type(duration) is not int
            or duration < 0
            or duration > self.MAX_DURATION_SECONDS
        ):
            await self._send_text(
                message,
                "telegram_error_voice_too_long",
                limit_minutes=self.MAX_DURATION_SECONDS // 60,
            )
            return

        async with self._admission_lock:
            if self._stop_event.is_set() or self._closed:
                await self._send_text(message, "telegram_error_generic")
                return
            if self._active_job is not None and self.queue.full():
                await self._send_text(message, "telegram_error_queue_full")
                return

            progress = await self.api.send_message(
                chat_id,
                self.text("telegram_progress_received"),
            )
            job = TelegramJob(
                chat_id=chat_id,
                message_id=getattr(message, "message_id", 0),
                file_id=voice.file_id,
                file_size=file_size,
                duration_seconds=duration,
                progress_message_id=progress.message_id,
            )
            if self._active_job is None:
                self._active_job = job
                self._work_event.set()
            else:
                self.queue.put_nowait(job)
            self._ensure_worker_task()

    def _ensure_worker_task(self) -> None:
        if self._closed:
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stopped_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def _edit_progress(
        self,
        job: TelegramJob,
        key: str,
        **kwargs: Any,
    ) -> None:
        await self.api.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.progress_message_id,
            text=self.text(key, **kwargs),
        )

    async def _safe_error(
        self,
        job: TelegramJob,
        key: str,
        **kwargs: Any,
    ) -> None:
        try:
            await self._edit_progress(job, key, **kwargs)
        except (AiogramTelegramAPIError, TelegramServiceError):
            # Best-effort notification: a Telegram/network failure here must
            # never block stop()'s own cleanup (session close, log-factory
            # restore) from running. Anything else (a bug in our own
            # formatting/keys) still propagates instead of vanishing silently.
            logging.getLogger(__name__).debug(
                "Could not deliver Telegram error notification", exc_info=True
            )

    async def _send_transcript(self, job: TelegramJob, final: str) -> None:
        if len(final) <= self.TEXT_MESSAGE_LIMIT:
            await self.api.send_message(job.chat_id, final)
            return
        filename = self.text(
            "telegram_transcript_filename",
            timestamp=time.strftime("%Y%m%d-%H%M%S"),
        )
        await self.api.send_document(
            chat_id=job.chat_id,
            document=BufferedInputFile(
                final.encode("utf-8"),
                filename=filename,
            ),
        )

    async def _process_job(self, job: TelegramJob) -> None:
        try:
            buffer = CappedBytesIO(max_bytes=self.MAX_FILE_BYTES)
            telegram_file = await self.api.get_file(job.file_id)
            await self.api.download_file(telegram_file.file_path, buffer)
            if self.cancel_event.is_set():
                await self._safe_error(job, "telegram_error_generic")
                return
            buffer.seek(0)
            await self._edit_progress(job, "telegram_progress_transcribing")
            result = await asyncio.to_thread(
                self.transcribe,
                buffer,
                self.cancel_event.is_set,
            )
            if self.cancel_event.is_set():
                return
            _raw, final, _duration, _words, _segments = result[:5]
            await self._send_transcript(job, str(final or ""))
            await self._edit_progress(job, "telegram_progress_done")
        except TelegramFileTooLarge:
            await self._safe_error(
                job,
                "telegram_error_file_too_large",
                limit_mib=self.MAX_FILE_BYTES // (1024 * 1024),
            )
        except TelegramModelMissingError:
            await self._safe_error(job, "telegram_error_model_missing")
        except Exception:
            logging.getLogger(__name__).error("Telegram transcription failed")
            await self._safe_error(job, "telegram_error_generic")

    async def _worker_loop(self) -> None:
        while True:
            await self._work_event.wait()
            job = self._active_job
            if job is None:
                self._work_event.clear()
                if self._stop_event.is_set():
                    return
                continue

            await self._process_job(job)
            async with self._admission_lock:
                if self._active_job is job:
                    if self._stop_event.is_set() or self.queue.empty():
                        self._active_job = None
                        self._work_event.clear()
                    else:
                        self._active_job = self.queue.get_nowait()
                        self.queue.task_done()
            if self._stop_event.is_set():
                return

    def _ready_state(self) -> TelegramRuntimeState:
        return (
            TelegramRuntimeState.ACTIVE
            if self.identity is not None
            else TelegramRuntimeState.PAIRING
        )

    async def _ensure_webhook_absent(self) -> None:
        webhook = await self.api.get_webhook_info()
        if getattr(webhook, "url", ""):
            raise TelegramWebhookActiveError()

    def _start_poll_task(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._stop_event.clear()
        self.cancel_event.clear()
        self._stopped_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise TelegramServiceError("telegram-service-closed")
            if self._poll_task is not None and not self._poll_task.done():
                return
            self.state = TelegramRuntimeState.CHECKING
            try:
                await self.api.get_me()
                await self._ensure_webhook_absent()
            except (
                TelegramOfflineError,
                TelegramTokenInvalidError,
                TelegramBotInUseError,
                TelegramWebhookActiveError,
            ) as error:
                self.state = error.state
                raise
            self.state = self._ready_state()
            self._start_poll_task()
            self._ensure_worker_task()

    async def _poll_loop(self) -> None:
        backoff_index = 0
        try:
            while not self._stop_event.is_set():
                try:
                    updates = await self.api.get_updates(
                        offset=self._poll_offset,
                        timeout=5,
                        allowed_updates=["message"],
                    )
                except TelegramOfflineError:
                    self.state = TelegramRuntimeState.OFFLINE
                    delay = self.OFFLINE_BACKOFF_SECONDS[backoff_index]
                    backoff_index = min(
                        backoff_index + 1,
                        len(self.OFFLINE_BACKOFF_SECONDS) - 1,
                    )
                    await self._sleep(delay)
                    continue
                except (TelegramTokenInvalidError, TelegramBotInUseError) as error:
                    self.state = error.state
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return

                backoff_index = 0
                if self.state == TelegramRuntimeState.OFFLINE:
                    self.state = self._ready_state()
                for update in updates:
                    self._poll_offset = update.update_id + 1
                    try:
                        await self.dispatcher.feed_update(self.bot, update)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise

    async def _close_session(self) -> None:
        if self._session_closed:
            return
        session = getattr(self.bot, "session", None)
        close = getattr(session, "close", None)
        if close is not None:
            await close()
        self._session_closed = True

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._stop_event.set()
            self.cancel_event.set()
            poll_task = self._poll_task
            if poll_task is not None and not poll_task.done():
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass

            pending = []
            async with self._admission_lock:
                while not self.queue.empty():
                    pending.append(self.queue.get_nowait())
                    self.queue.task_done()
            for job in pending:
                await self._safe_error(job, "telegram_error_generic")

            self._work_event.set()
            worker_task = self._worker_task
            try:
                if worker_task is not None and not worker_task.done():
                    await asyncio.wait_for(
                        asyncio.shield(worker_task),
                        timeout=self._shutdown_timeout_seconds,
                    )
            except asyncio.TimeoutError:
                await self._close_session()
                self._log_factory.restore()
                self.state = TelegramRuntimeState.SHUTDOWN_FAILED
                raise TelegramShutdownFailedError() from None

            await self._close_session()
            self._log_factory.restore()
            self.state = TelegramRuntimeState.DISABLED
            self._closed = True
            self._stopped_event.set()

    async def wait_until_stopped(self) -> None:
        await self._stopped_event.wait()

    async def begin_pairing(self, ttl_seconds: float) -> str:
        try:
            ttl_seconds = float(ttl_seconds)
        except (TypeError, ValueError, OverflowError):
            raise TelegramInvalidPairingTtlError() from None
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise TelegramInvalidPairingTtlError()
        # Serialized against start()/stop() via _lifecycle_lock: without this,
        # a stop() racing the awaits below can close the service and this
        # call would still resurrect polling afterwards (zombie runtime).
        async with self._lifecycle_lock:
            if self._closed:
                raise TelegramServiceError("telegram-service-closed")
            needs_poll_task = self._poll_task is None or self._poll_task.done()
            try:
                await self.api.get_me()
                if needs_poll_task:
                    await self._ensure_webhook_absent()
            except (
                TelegramOfflineError,
                TelegramTokenInvalidError,
                TelegramBotInUseError,
                TelegramWebhookActiveError,
            ) as error:
                self.state = error.state
                raise
            secret = secrets.token_urlsafe(16)
            with self._pairing_lock:
                if (
                    self._pairing is not None
                    and self._pairing.reserved_identity is not None
                ):
                    raise TelegramPairingBusyError()
                self._pairing = PairingSession(
                    secret=secret,
                    expires_at=self._clock() + ttl_seconds,
                )
                self.state = TelegramRuntimeState.PAIRING
            if needs_poll_task:
                self._start_poll_task()
            return secret

    def accept_pair(
        self,
        user_id: Any,
        chat_id: Any,
        chat_type: str,
        secret: Any,
    ) -> bool:
        if (
            chat_type != "private"
            or not self._valid_identity(user_id, chat_id)
            or type(secret) is not str
        ):
            return False

        identity = TelegramIdentity(user_id, chat_id)
        with self._pairing_lock:
            pairing = self._pairing
            if (
                pairing is None
                or pairing.used
                or pairing.reserved_identity is not None
            ):
                return False
            if self._clock() >= pairing.expires_at:
                self._pairing = None
                return False
            if not secrets.compare_digest(pairing.secret, secret):
                return False
            pairing.reserved_identity = identity

        try:
            if self._on_paired is not None:
                self._on_paired(identity)
        except BaseException:
            with self._pairing_lock:
                if (
                    self._pairing is pairing
                    and pairing.reserved_identity == identity
                ):
                    pairing.reserved_identity = None
            raise

        with self._pairing_lock:
            if (
                self._pairing is not pairing
                or pairing.reserved_identity != identity
            ):
                return False
            pairing.used = True
            self._pairing = None
            self.identity = identity
            self.state = TelegramRuntimeState.ACTIVE
        return True
