"""Telegram-бот: надішли голосове → повертає локальну розшифровку.

Запуск:
    set WHISPER_TYPER_BOT_TOKEN=<токен від @BotFather>
    set WHISPER_TYPER_BOT_ALLOWED=<твій chat_id>   (опційно; порожньо = усі)
    python -m fronts.telegram.bot

Модель вантажиться локально (офлайн), голосові не зберігаються.
"""
import asyncio
import io
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message

from whisper_core import profiles
from whisper_core.config import Config
from whisper_core.engine import Engine
from whisper_core.terms import load_terms

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("WHISPER_TYPER_BOT_TOKEN", "")
_allowed = os.environ.get("WHISPER_TYPER_BOT_ALLOWED", "")
ALLOWED = {int(x) for x in _allowed.replace(" ", "").split(",")
           if x.strip().lstrip("-").isdigit()}

dp = Dispatcher()
_cfg = Config.load()
# Словник — з активного профілю; пам'ять бот НЕ веде (чужі голосові не логуються).
_terms = load_terms(profiles.get_active(ROOT).terms_path)
_engine = None  # лінива ініціалізація — щоб імпорт модуля лишався легким
_engine_lock = asyncio.Lock()  # WhisperModel не потоко-безпечний: транскрипції строго по черзі
TG_LIMIT = 4096  # ліміт довжини повідомлення Telegram


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine(_cfg)
    return _engine


async def transcribe_voice(bot: Bot, file_id: str):
    """Завантажити голосове у пам'ять (BytesIO) і розпізнати. → (raw, final, duration, words)."""
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    buf.seek(0)
    async with _engine_lock:
        return await asyncio.to_thread(get_engine().transcribe, buf, _terms)


@dp.message(CommandStart())
async def on_start(message: Message):
    await message.reply(
        f"Балачки — бот. Надішли голосове чи аудіо — комп'ютер із ботом "
        f"розпізнає його локально (модель {_cfg.model_name}, мова {_cfg.language}). "
        f"Саме повідомлення проходить через сервери Telegram.\n"
        f"Твій chat_id: {message.chat.id}"
    )


@dp.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot):
    if ALLOWED and message.chat.id not in ALLOWED:
        return
    media = message.voice or message.audio
    note = await message.reply("Розпізнаю…")
    try:
        _raw, final, _dur, _words = await transcribe_voice(bot, media.file_id)
        final = final or "(тиша або нерозбірливо)"
        # довша за ліміт Telegram → шматками, щоб не втратити готову розшифровку
        await note.edit_text(final[:TG_LIMIT])
        for i in range(TG_LIMIT, len(final), TG_LIMIT):
            await message.reply(final[i:i + TG_LIMIT])
    except Exception:
        log.exception("Не вдалося розпізнати повідомлення Telegram")
        await note.edit_text(
            "Не вдалося розпізнати це повідомлення. Перевір формат аудіо "
            "та спробуй ще раз."
        )


async def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Задай WHISPER_TYPER_BOT_TOKEN (від @BotFather). "
            "Опційно WHISPER_TYPER_BOT_ALLOWED=<твій chat_id>."
        )
    get_engine()  # прогріти модель до старту опитування
    bot = Bot(BOT_TOKEN)
    print(f"Bot запущено. Whitelist: {ALLOWED or 'усі'}", flush=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
