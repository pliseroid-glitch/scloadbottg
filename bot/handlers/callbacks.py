"""Callback query обработчики."""

from telegram import Update
from telegram.ext import ContextTypes


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопки-индикатора '⏳ Загрузка...' — иначе у пользователя крутится колесо."""
    try:
        await update.callback_query.answer()
    except Exception:
        pass
