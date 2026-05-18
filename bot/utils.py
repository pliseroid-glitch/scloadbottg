"""Мелкие утилиты, общие для нескольких модулей."""

import os
from typing import Any

from telegram.ext import ContextTypes

from .config import (
    STORAGE_CHAT_ID, PENDING_CACHE_MAX, PENDING_CACHE_TRIM,
    LYRICS_SUFFIXES, LYRICS_SHORT_SUFFIXES, logger,
)

# Имя бота — кэшируем после первого вызова
_bot_username: str | None = None


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await context.bot.get_me()).username
    return _bot_username


def trim_pending(pending: dict) -> None:
    """Чистим LRU-словарь pending-запросов."""
    if len(pending) > PENDING_CACHE_MAX:
        for k in list(pending.keys())[:PENDING_CACHE_TRIM]:
            pending.pop(k, None)


def is_lyrics_query(query: str) -> tuple[bool, str]:
    """Определяем, хочет ли пользователь текст."""
    lower = query.lower()
    for suf in LYRICS_SUFFIXES:
        if lower.endswith(suf):
            return True, query[: -len(suf)].strip()

    for suf in LYRICS_SHORT_SUFFIXES:
        if lower.endswith(suf):
            head = query[: -len(suf)].strip()
            if head and any(len(w) >= 2 for w in head.split()):
                return True, head
    return False, query


def format_duration(ms: int) -> str:
    """Миллисекунды → "M:SS"."""
    return f"{ms // 60000}:{(ms % 60000) // 1000:02d}"


async def upload_to_storage(
    context: ContextTypes.DEFAULT_TYPE,
    file_path: str,
    title: str,
    artist: str,
    duration: int,
    thumb: bytes | None,
) -> str | None:
    """Заливаем mp3 в приватный канал-хранилище и возвращаем file_id."""
    if not STORAGE_CHAT_ID:
        logger.error("[STORAGE] STORAGE_CHAT_ID не задан")
        return None

    try:
        with open(file_path, "rb") as f:
            kwargs: dict[str, Any] = {
                "chat_id": STORAGE_CHAT_ID,
                "audio": f,
                "title": title,
                "performer": artist,
                "duration": duration,
                "disable_notification": True,
            }
            if thumb:
                kwargs["thumbnail"] = thumb
            msg = await context.bot.send_audio(**kwargs)
            return msg.audio.file_id if msg.audio else None
    except Exception as e:
        logger.error(f"[STORAGE] загрузка упала: {type(e).__name__}: {e}")
        return None


def safe_remove(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
