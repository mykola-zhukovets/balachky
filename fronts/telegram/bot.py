"""Telegram-бот: надішли голосове → повертає локальну розшифровку.

Запуск:
    set WHISPER_TYPER_BOT_TOKEN=<токен від @BotFather>
    python -m fronts.telegram.bot

Підтримується рівно один прив'язаний chat_id (це особистий бот, не для
кількох користувачів одночасно):
    - без WHISPER_TYPER_BOT_ALLOWED бот виведе в консоль посилання для
      прив'язки — відкрий його в Telegram, щоб прив'язати свій chat_id;
    - WHISPER_TYPER_BOT_ALLOWED=<chat_id> прив'язує його одразу при старті.

Модель вантажиться локально (офлайн), голосові не зберігаються.
"""
import asyncio
import inspect
import io
import logging
import os
from pathlib import Path

from whisper_core import profiles
from whisper_core.config import Config
from whisper_core.engine import Engine, ModelAbsentError, make_engine
from whisper_core.terms import load_terms

from .service import (
    TelegramModelMissingError,
    TelegramService,
)

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("WHISPER_TYPER_BOT_TOKEN", "")
_allowed = os.environ.get("WHISPER_TYPER_BOT_ALLOWED", "")
ALLOWED = {int(x) for x in _allowed.replace(" ", "").split(",")
           if x.strip().lstrip("-").isdigit()}

_cfg = Config.load()
# Словник — з активного профілю; пам'ять бот НЕ веде (чужі голосові не логуються).
_terms = load_terms(profiles.get_active(ROOT).terms_path)
_engine = None  # лінива ініціалізація — щоб імпорт модуля лишався легким
TG_LIMIT = 4096  # ліміт довжини повідомлення Telegram


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine(_cfg)
    return _engine


def transcribe_voice(audio, should_cancel):
    """Run the shared service's synchronous local-STT callback."""
    try:
        return get_engine().transcribe(
            audio,
            _terms,
            should_cancel=should_cancel,
        )
    except ModelAbsentError:
        raise TelegramModelMissingError() from None


async def on_voice(message, bot_api):
    """Compatibility helper for the pre-service five-value contract."""
    if ALLOWED and message.chat.id not in ALLOWED:
        return
    media = message.voice or message.audio
    note = await message.reply("Розпізнаю…")
    try:
        telegram_file = await bot_api.get_file(media.file_id)
        buffer = io.BytesIO()
        await bot_api.download_file(telegram_file.file_path, buffer)
        buffer.seek(0)
        result = transcribe_voice(buffer, lambda: False)
        if inspect.isawaitable(result):
            result = await result
        _raw, final, _dur, _words, _segments = result[:5]
        final = final or "(тиша або нерозбірливо)"
        # довша за ліміт Telegram → шматками, щоб не втратити готову розшифровку
        await note.edit_text(final[:TG_LIMIT])
        for i in range(TG_LIMIT, len(final), TG_LIMIT):
            await message.reply(final[i:i + TG_LIMIT])
    except Exception:
        log.error("Не вдалося розпізнати повідомлення Telegram")
        await note.edit_text(
            "Не вдалося розпізнати це повідомлення. Перевір формат аудіо "
            "та спробуй ще раз."
        )


def _resolve_single_allowed_id(allowed: set) -> int:
    """Return the one supported paired id, or 0 for token-only pairing mode.

    TelegramService pairs exactly one identity — WHISPER_TYPER_BOT_ALLOWED
    listing more than one id used to be silently coerced to "nobody paired",
    a stuck bot that never explained why. Fail loudly instead.
    """
    if len(allowed) > 1:
        raise SystemExit(
            "WHISPER_TYPER_BOT_ALLOWED підтримує лише один chat_id. Лиши "
            "там один ID або взагалі не задавай його — бот сам запропонує "
            "посилання для прив'язки у консолі."
        )
    return next(iter(allowed), 0)


async def _announce_pairing(runtime: TelegramService) -> None:
    """Print how to finish setup: already paired, or a fresh pairing link."""
    if runtime.identity is not None:
        print("Бот запущено. Прив'язаний chat_id активний.", flush=True)
        return
    secret = await runtime.begin_pairing(
        ttl_seconds=TelegramService.PAIR_TTL_SECONDS
    )
    me = await runtime.api.get_me()
    print(
        "Бот запущено без прив'язаного chat_id. Прив'яжи себе: "
        f"https://t.me/{me.username}?start={secret}",
        flush=True,
    )


async def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Задай WHISPER_TYPER_BOT_TOKEN (від @BotFather). "
            "Опційно WHISPER_TYPER_BOT_ALLOWED=<твій chat_id>."
        )
    allowed_id = _resolve_single_allowed_id(ALLOWED)
    runtime = TelegramService(
        token=BOT_TOKEN,
        transcribe=transcribe_voice,
        audit=lambda action: log.info("Telegram action: %s", action),
        paired_user_id=allowed_id,
        paired_chat_id=allowed_id,
    )
    await runtime.start()
    try:
        await _announce_pairing(runtime)
        await asyncio.Event().wait()
    finally:
        await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
