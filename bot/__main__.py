"""Точка входа: python -m bot"""

import aiohttp
from telegram.ext import (
    Application,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    CommandHandler,
    CallbackQueryHandler,
)

from .config import BOT_TOKEN, DEFAULT_HEADERS, logger
from .storage import load_cache, load_history
from .soundcloud import init_session
from .handlers import start_command, inline_query, chosen_inline_result, noop_callback


async def _on_startup(app: Application) -> None:
    session = aiohttp.ClientSession(headers=DEFAULT_HEADERS)
    init_session(session)
    app.bot_data["http_session"] = session  # чтобы закрыть при shutdown
    load_cache()
    load_history()
    logger.info("Бот запущен!")


async def _on_shutdown(app: Application) -> None:
    session = app.bot_data.get("http_session")
    if session:
        await session.close()


def main() -> None:
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN не задан в .env")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(InlineQueryHandler(inline_query, block=False))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result, block=False))
    app.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))

    app.run_polling(
        allowed_updates=["message", "inline_query", "chosen_inline_result", "callback_query"]
    )


if __name__ == "__main__":
    main()
