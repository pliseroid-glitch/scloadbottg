"""Inline-режим: поиск, выбор результата, скачивание."""

import os
import uuid
import asyncio

from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaAudio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes

from ..config import TG_MESSAGE_LIMIT, INLINE_RESULTS_LIMIT, logger
from ..storage import cache_get, cache_set, history_get, history_push
from ..soundcloud import (
    search_tracks, download_track, download_artwork,
    hires_artwork, get_track_album, is_track_blocked, DRM_MARKER,
)
from ..lyrics import search_lyrics, fetch_lyrics
from ..utils import (
    get_bot_username, trim_pending, is_lyrics_query,
    format_duration, upload_to_storage, safe_remove,
)


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главная точка входа в инлайн-режим."""
    query = update.inline_query.query.strip()

    if not query:
        await _handle_empty(update, context)
        return

    is_lyrics, cleaned = is_lyrics_query(query)
    if is_lyrics:
        if not cleaned:
            return
        logger.info(f"[INLINE] режим текстов: '{cleaned}'")
        await _handle_lyrics(update, context, cleaned)
        return

    await _handle_search(update, context, query)


async def _handle_empty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пустой запрос — приветствие + история пользователя."""
    results: list[InlineQueryResultArticle] = [
        InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="💛 PliserLoad",
            description="Напиши название трека или артиста для поиска",
            thumbnail_url="https://upload.wikimedia.org/wikipedia/commons/c/c0/Manul_Timofey_in_April_2025_%281%2C_cropped%29.jpg",
            input_message_content=InputTextMessageContent(
                message_text=(
                    "🎵 PliserLoad Bot\n\n"
                    "Ищи треки с SoundCloud прямо в чате!\n"
                    "Добавь «t» в конце запроса для поиска текста песни."
                ),
            ),
        )
    ]

    pending = context.bot_data.setdefault("pending_downloads", {})
    user_id = update.inline_query.from_user.id
    history = history_get(user_id)

    for item in history[-9:]:
        result_id = str(uuid.uuid4())
        artwork = item.get("artwork") or ""
        results.append(
            InlineQueryResultArticle(
                id=result_id,
                title=f"🕐 {item['title']}",
                description=item["artist"],
                thumbnail_url=artwork.replace("-large", "-t300x300") if artwork else None,
                input_message_content=InputTextMessageContent(
                    message_text=f"🎵 {item['title']}\n👤 {item['artist']}\n\n⏳ Скачиваю...",
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏳ Загрузка...", callback_data="noop")
                ]]),
            )
        )
        pending[result_id] = item

    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def _handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    """Поиск треков на SoundCloud."""
    tracks = await search_tracks(query)
    pending = context.bot_data.setdefault("pending_downloads", {})

    results: list[InlineQueryResultArticle] = []
    for track in tracks[:INLINE_RESULTS_LIMIT]:
        title = track.get("title", "Unknown")
        artist = track.get("user", {}).get("username", "Unknown")
        duration_ms = track.get("duration", 0)
        duration_str = format_duration(duration_ms)
        artwork = track.get("artwork_url") or ""
        blocked = is_track_blocked(track)

        result_id = str(uuid.uuid4())
        results.append(_build_track_card(result_id, title, artist, duration_str, artwork, blocked))

        pending[result_id] = {
            "track": track,
            "title": title,
            "artist": artist,
            "duration": duration_ms // 1000,
            "artwork": artwork,
            "blocked": blocked,
        }

    trim_pending(pending)
    logger.info(f"[INLINE] '{query}' → {len(results)} карточек")
    await update.inline_query.answer(results, cache_time=10)


def _build_track_card(result_id, title, artist, duration_str, artwork, blocked):
    if blocked:
        display_title = f"🔒 {title}"
        description = f"🔒 Go+ • {artist} • {duration_str}"
        message_tail = "🔒 Это платный трек (Go+), полная версия недоступна."
        keyboard = None
    else:
        display_title = title
        description = f"{artist} • {duration_str}"
        message_tail = "⏳ Скачиваю..."
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏳ Загрузка...", callback_data="noop")
        ]])

    return InlineQueryResultArticle(
        id=result_id,
        title=display_title,
        description=description,
        thumbnail_url=artwork.replace("-large", "-t300x300") if artwork else None,
        input_message_content=InputTextMessageContent(
            message_text=f"🎵 {title}\n👤 {artist}\n⏱ {duration_str}\n\n{message_tail}",
        ),
        reply_markup=keyboard,
    )


async def _handle_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    """Инлайн-поиск текстов через Genius."""
    loop = asyncio.get_event_loop()
    songs = await loop.run_in_executor(None, search_lyrics, query)

    if not songs:
        await update.inline_query.answer(
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="Ничего не найдено",
                description="Попробуй другой запрос",
                input_message_content=InputTextMessageContent(message_text="❌ Текст не найден."),
            )],
            cache_time=30,
        )
        return

    pending = context.bot_data.setdefault("pending_lyrics", {})
    results: list[InlineQueryResultArticle] = []

    for song in songs:
        result_id = str(uuid.uuid4())
        results.append(
            InlineQueryResultArticle(
                id=result_id,
                title=f"📝 {song['title']}",
                description=song["artist"],
                thumbnail_url=song.get("thumbnail") or None,
                input_message_content=InputTextMessageContent(
                    message_text=f"📝 {song['title']}\n👤 {song['artist']}\n\n⏳ Загружаю текст...",
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏳ Загрузка текста...", callback_data="noop")
                ]]),
            )
        )
        pending[result_id] = {"title": song["title"], "artist": song["artist"], "url": song["url"]}

    if len(pending) > 200:
        for k in list(pending.keys())[:50]:
            pending.pop(k, None)

    logger.info(f"[LYRICS] '{query}' → {len(results)} результатов")
    await update.inline_query.answer(results, cache_time=30)


# ─────────────────────────────────────────────────────────────────────────────
# Chosen inline result
# ─────────────────────────────────────────────────────────────────────────────

async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь выбрал результат из инлайн-выдачи."""
    chosen = update.chosen_inline_result
    result_id = chosen.result_id
    inline_message_id = chosen.inline_message_id
    user_id = chosen.from_user.id

    logger.info(f"[CHOSEN] result_id={result_id} user={user_id} ({chosen.from_user.username})")

    # Текст?
    pending_lyrics = context.bot_data.get("pending_lyrics", {})
    lyrics_info = pending_lyrics.pop(result_id, None)
    if lyrics_info:
        await _handle_chosen_lyrics(context, inline_message_id, lyrics_info)
        return

    # Скачивание трека?
    pending = context.bot_data.get("pending_downloads", {})
    info = pending.pop(result_id, None)
    if not info:
        logger.warning(f"[CHOSEN] нет данных для {result_id} — сессия истекла")
        await _try_edit(context, inline_message_id, "❌ Сессия истекла, попробуй снова.")
        return

    if info.get("blocked"):
        logger.info("[CHOSEN] трек заблокирован, скачивание не запускаем")
        return

    await _handle_chosen_track(context, inline_message_id, info, user_id)


async def _handle_chosen_track(context, inline_message_id, info, user_id):
    track = info["track"]
    title = info["title"]
    artist = info["artist"]
    duration = info["duration"]
    artwork = info["artwork"]
    track_id = track.get("id")

    file_id = cache_get(track_id)
    if file_id:
        logger.info(f"[CHOSEN] cache hit для трека {track_id}")
    else:
        file_path = await download_track(track)
        if file_path == DRM_MARKER:
            await _try_edit(context, inline_message_id, f"🔒 {title}\n\nЭтот трек защищён авторским правом и недоступен для скачивания.")
            return
        if not file_path or not os.path.exists(file_path):
            logger.error("[CHOSEN] download_track вернул пусто")
            await _try_edit(context, inline_message_id, f"❌ Не удалось скачать: {title}")
            return
        try:
            thumb = await download_artwork(hires_artwork(artwork))
            file_id = await upload_to_storage(context, file_path, title, artist, duration, thumb)
        finally:
            safe_remove(file_path)

        if not file_id:
            await _try_edit(context, inline_message_id, f"❌ Ошибка загрузки: {title}")
            return
        if track_id:
            cache_set(track_id, file_id)

    if not inline_message_id:
        return

    # Кнопка альбома
    album = await get_track_album(track)
    keyboard = None
    if album:
        bot_username = await get_bot_username(context)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"📀 {album['title']}", url=f"https://t.me/{bot_username}?start=album_{album['id']}")
        ]])

    try:
        await context.bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=InputMediaAudio(media=file_id, title=title, performer=artist, duration=duration),
            reply_markup=keyboard,
        )
        logger.info("[CHOSEN] ✅ инлайн-сообщение заменено на аудио")
        history_push(user_id, {"track": track, "title": title, "artist": artist, "duration": duration, "artwork": artwork})
    except Exception as e:
        logger.error(f"[CHOSEN] editMessageMedia упал: {type(e).__name__}: {e}")
        await _try_edit(context, inline_message_id, f"✅ {title} — {artist}\n\nТрек загружен, но не удалось вставить в чат.")


async def _handle_chosen_lyrics(context, inline_message_id, info):
    if not inline_message_id:
        return

    title = info["title"]
    artist = info["artist"]
    song_url = info["url"]

    loop = asyncio.get_event_loop()
    lyrics = await loop.run_in_executor(None, fetch_lyrics, song_url)

    if not lyrics:
        await _try_edit(context, inline_message_id, f"❌ Не удалось загрузить текст: {title}")
        return

    header = f"📝 {title}\n👤 {artist}\n\n"
    max_len = TG_MESSAGE_LIMIT - len(header) - 10
    if len(lyrics) > max_len:
        lyrics = lyrics[:max_len] + "..."

    try:
        await context.bot.edit_message_text(inline_message_id=inline_message_id, text=header + lyrics)
        logger.info(f"[LYRICS] ✅ текст для '{title}' отправлен")
    except Exception as e:
        logger.error(f"[LYRICS] edit упал: {e}")


async def _try_edit(context, inline_message_id, text):
    if not inline_message_id:
        return
    try:
        await context.bot.edit_message_text(inline_message_id=inline_message_id, text=text)
    except Exception:
        pass
