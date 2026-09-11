"""Desktop lifecycle bridge between Qt and the Telegram service."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

from whisper_core import telegram_secrets
from fronts.telegram.service import (
    TelegramIdentity,
    TelegramRuntimeState,
    TelegramService,
)

log = logging.getLogger(__name__)


class TelegramControllerError(Exception):
    """Exception raised for Telegram controller failures."""


class TelegramController:
    """Manages the background asyncio event loop and TelegramService runtime."""

    def __init__(
        self,
        cfg: Any,
        transcribe: Callable[..., Any],
        state_callback: Callable[[dict[str, Any]], None] | None = None,
        runtime_factory: Callable[..., Any] | None = None,
    ):
        self.cfg = cfg
        self.transcribe = transcribe
        self.state_callback = state_callback
        self.runtime_factory = runtime_factory or self._default_runtime_factory
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runtime: Any = None
        self.ready = threading.Event()
        self.cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._last_state: dict[str, Any] = {
            "state": TelegramRuntimeState.DISABLED.value,
            "bot_name": "",
            "pairing_url": "",
            "error": "",
        }

    def _default_runtime_factory(
        self,
        token: str,
        cancel_event: threading.Event,
        identity: TelegramIdentity | None,
        on_paired: Callable[[TelegramIdentity], None],
        transcribe: Callable[..., Any],
        audit: Callable[[str], None] | None = None,
        shutdown_timeout_seconds: float = 5.0,
    ) -> TelegramService:
        from whisper_core import netlog

        def default_audit(action: str) -> None:
            try:
                netlog.record(
                    "api.telegram.org",
                    kind=getattr(netlog, "TELEGRAM", "telegram"),
                    allowed=True,
                    detail=action,
                )
            except Exception:
                pass

        return TelegramService(
            token=token,
            cancel_event=cancel_event,
            identity=identity,
            on_paired=on_paired,
            transcribe=transcribe,
            audit=audit or default_audit,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )

    def _thread_main(self, candidate_token: str | None) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        token = candidate_token or telegram_secrets.load_token() or ""
        paired_user_id = getattr(self.cfg, "telegram_user_id", 0)
        paired_chat_id = getattr(self.cfg, "telegram_chat_id", 0)
        identity = (
            TelegramIdentity(paired_user_id, paired_chat_id)
            if paired_user_id and paired_chat_id
            else None
        )

        try:
            self.runtime = self.runtime_factory(
                token=token,
                cancel_event=self.cancel_event,
                identity=identity,
                on_paired=self._handle_paired,
                transcribe=self.transcribe,
            )
        except Exception as exc:
            log.exception("Ne vdalosya initializuvaty TelegramService")
            self._update_state(
                state=TelegramRuntimeState.TOKEN_INVALID.value,
                error=str(exc),
            )
            self.ready.set()
            return


        self.ready.set()
        try:
            wait_fn = getattr(self.runtime, "wait_until_stopped", None)
            if callable(wait_fn):
                self.loop.run_until_complete(wait_fn())
        except Exception:
            log.exception("Pomylka u tsykliTelegram runtime")
        finally:
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                self.loop.close()

    def _handle_paired(self, identity: TelegramIdentity) -> None:
        self.cfg.telegram_user_id = identity.user_id
        self.cfg.telegram_chat_id = identity.chat_id
        self.cfg.telegram_enabled = True
        save_fn = getattr(self.cfg, "save", None)
        if callable(save_fn):
            try:
                save_fn()
            except Exception:
                log.exception("Ne vdalosya zberedty konfikuratsiyu pislya sparyuvannya")
        self._update_state(
            state=TelegramRuntimeState.ACTIVE.value,
            bot_name=getattr(self.runtime, "bot_username", ""),
        )

    def _update_state(self, **kwargs: Any) -> None:
        with self._state_lock:
            self._last_state.update(kwargs)
            state_copy = dict(self._last_state)
        if self.state_callback:
            try:
                self.state_callback(state_copy)
            except Exception:
                log.exception("Pomylka u state_callback TelegramController")


    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._last_state)

    def ensure_loop_ready(self, candidate_token: str | None = None) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.ready.clear()
        self.cancel_event.clear()
        self.thread = threading.Thread(
            target=self._thread_main,
            args=(candidate_token,),
            name="TelegramControllerThreadas",
            daemon=True,
        )
        self.thread.start()
        if not self.ready.wait(5.0):
            raise TelegramControllerError("Telegram runtime ne zapustyvsya")


    def start_if_enabled(self) -> None:
        token = telegram_secrets.load_token()
        if (
            getattr(self.cfg, "telegram_enabled", False)
            and token
            and getattr(self.cfg, "telegram_user_id", 0)
            and getattr(self.cfg, "telegram_chat_id", 0)
        ):
            self.ensure_loop_ready(token)
            if self.loop and self.runtime:
                start_polling = getattr(self.runtime, "start_normal_polling", None)
                if callable(start_polling):
                    asyncio.run_coroutine_threadsafe(
                        start_polling(), self.loop
                    )
                self._update_state(
                    state=TelegramRuntimeState.ACTIVE.value,
                    bot_name=getattr(self.runtime, "bot_username", ""),
                )
        else:
            self._update_state(state=TelegramRuntimeState.DISABLED.value)

    def set_enabled(self, enabled: bool) -> None:
        self.cfg.telegram_enabled = bool(enabled)
        save_fn = getattr(self.cfg, "save", None)
        if callable(save_fn):
            save_fn()
        if enabled:
            self.start_if_enabled()
        else:
            self.stop()
            self._update_state(state=TelegramRuntimeState.DISABLED.value)

    def verify_and_save_token(self, token: str) -> None:
        token = (token or "").strip()
        self.ensure_loop_ready(token)
        self._update_state(state=TelegramRuntimeState.CHECKING.value)
        future = asyncio.run_coroutine_threadsafe(
            self.runtime.verify_token(token), self.loop
        )

        def _on_verified(fut: Any) -> None:
            try:
                bot_info = fut.result()
                telegram_secrets.save_token(token)
                bot_username = getattr(bot_info, "username", "") or ""
                self._update_state(
                    state=TelegramRuntimeState.CHECKING.value,
                    bot_name=bot_username,
                    error="",
                )
            except Exception as exc:
                self._update_state(
                    state=TelegramRuntimeState.TOKEN_INVALID.value,
                    error=str(exc),
                )

        future.add_done_callback(_on_verified)

    def begin_pairing(self) -> str:
        token = telegram_secrets.load_token()
        if not token:
            raise TelegramControllerError("Balachky Telegram token vidsutniy")
        self.ensure_loop_ready(token)
        future = asyncio.run_coroutine_threadsafe(
            self.runtime.begin_pairing(ttl_seconds=600), self.loop
        )
        url = future.result(timeout=5.0)
        self._update_state(
            state=TelegramRuntimeState.PAIRING.value,
            pairing_url=url,
        )
        return url

    def disconnect(self) -> bool:
        if not self.stop(timeout=15.0):
            self._update_state(
                state=TelegramRuntimeState.SHUTDOWN_FAILED.value
            )
            return False
        telegram_secrets.delete_token()
        self.cfg.telegram_enabled = False
        self.cfg.telegram_user_id = 0
        self.cfg.telegram_chat_id = 0
        save_fn = getattr(self.cfg, "save", None)
        if callable(save_fn):
            save_fn()
        self._update_state(
            state=TelegramRuntimeState.DISABLED.value,
            bot_name="",
            pairing_url="",
            error="",
        )
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        if not self.is_running():
            return True
        self.cancel_event.set()
        if self.loop and self.runtime:
            stop_fn = getattr(self.runtime, "stop", None)
            if callable(stop_fn):
                future = asyncio.run_coroutine_threadsafe(
                    stop_fn(), self.loop
                )
                try:
                    future.result(timeout=timeout)
                except Exception:
                    pass

        if self.thread:
            self.thread.join(timeout)
        is_stopped = not (self.thread and self.thread.is_alive())
        if is_stopped:
            self.thread = None
            self.loop = None
            self.runtime = None
        return is_stopped

    def is_running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())
