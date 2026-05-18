"""/start и deeplink-обработчики."""

import os
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ..config import TG_MESSAGE_LIMIT, TG_CAPTION_LIMIT, logger
from ..storage import cache_get, cache_set
from ..soundcloud import (
    get_client_id, fetch_track_fresh, download_track,
    download_artwork, hires_artwork, fetch_album_tracks,
)
from ..utils import get_bot_username, upload_to_storage, safe_remove

WELCOME_TEXT = (
    "Привет! Я инлайн-бот для скачивания треков с SoundCloud.\n\n"
    "Используй меня в любом чате:\n"
    "@pliserloadbot название трека — скачать с SoundCloud\n"
    "@pliserloadbot название text — получить текст песни\n\n"
    "Выбери трек — он появится прямо в чате."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — либо приветствие, либо разбор deeplink-а."""
    args = context.args or []
    if args:
        arg = args[0]
        if arg.startswith("track_"):
            await _handle_track(update, context, arg[6:])
            return
        elif arg.startswith("album_"):
            await _handle_album(update, context, arg[6:])
            return
        elif arg.startswith("dlall_"):
            await _handle_download_all(update, context, arg[6:])
            return

    await update.message.reply_text(WELCOME_TEXT)


async def _handle_track(update: Update, context: ContextTypes.DEFAULT_TYPE, track_id_str: str) -> None:
    try:
        track_id = int(track_id_str)
    except ValueError:
        await update.message.reply_text("❌ Неверный ID трека.")
        return

    chat_id = update.effective_chat.id

    cached = cache_get(track_id)
    if cached:
        try:
            await context.bot.send_audio(chat_id=chat_id, audio=cached)
            return
        except Exception:
            pass

    status = await update.message.reply_text("⏳ Скачиваю трек...")

    client_id = await get_client_id()
    if not client_id:
        await status.edit_text("❌ Не удалось подключиться к SoundCloud.")
        return

    track = await fetch_track_fresh(track_id, client_id)
    if not track:
        await status.edit_text("❌ Трек не найден.")
        return

    title = track.get("title", "track")
    artist = track.get("user", {}).get("username", "Unknown")
    duration = track.get("duration", 0) // 1000

    file_path = await download_track(track)
    if not file_path:
        await status.edit_text(f"❌ Не удалось скачать: {title}")
        return

    thumb = await download_artwork(hires_artwork(track.get("artwork_url")))
    file_id = await upload_to_storage(context, file_path, title, artist, duration, thumb)

    try:
        if file_id:
            cache_set(track_id, file_id)
            await context.bot.send_audio(chat_id=chat_id, audio=file_id, title=title, performer=artist, duration=duration)
        else:
            with open(file_path, "rb") as f:
                kwargs: dict[str, Any] = {"chat_id": chat_id, "audio": f, "title": title, "performer": artist, "duration": duration}
                if thumb:
                    kwargs["thumbnail"] = thumb
                await context.bot.send_audio(**kwargs)
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[DEEPLINK] отправка упала: {e}")
        await status.edit_text(f"❌ Ошибка отправки: {title}")
    finally:
        safe_remove(file_path)


async def _handle_album(update: Update, context: ContextTypes.DEFAULT_TYPE, album_id_str: str) -> None:
    try:
        album_id = int(album_id_str)
    except ValueError:
        await update.message.reply_text("❌ Неверный ID альбома.")
        return

    album = await fetch_album_tracks(album_id)
    if not album:
        await update.message.reply_text("❌ Альбом не найден.")
        return

    title = album.get("title", "Album")
    artist = album.get("user", {}).get("username", "Unknown")
    tracks = album.get("tracks") or []
    if not tracks:
        await update.message.reply_text("❌ В альбоме нет треков.")
        return

    artwork = album.get("artwork_url") or tracks[0].get("artwork_url") or ""
    bot_username = await get_bot_username(context)

    lines = [f"📀 {artist} – {title}\n"]
    for i, t in enumerate(tracks, 1):
        t_title = t.get("title", "?")
        t_id = t.get("id", 0)
        lines.append(f'{i} · <a href="https://t.me/{bot_username}?start=track_{t_id}">{t_title}</a>')
    text = "\n".join(lines)
    if len(text) > TG_MESSAGE_LIMIT:
        text = text[:TG_MESSAGE_LIMIT - 6] + "..."

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬇️ Скачать все", url=f"https://t.me/{bot_username}?start=dlall_{album_id}")
    ]])

    chat_id = update.effective_chat.id
    thumb = await download_artwork(hires_artwork(artwork)) if artwork else None

    if thumb and len(text) <= TG_CAPTION_LIMIT:
        await context.bot.send_photo(chat_id=chat_id, photo=thumb, caption=text, parse_mode="HTML", reply_markup=keyboard)
        return
    if thumb:
        await context.bot.send_photo(chat_id=chat_id, photo=thumb)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _handle_download_all(update: Update, context: ContextTypes.DEFAULT_TYPE, album_id_str: str) -> None:
    try:
        album_id = int(album_id_str)
    except ValueError:
        await update.message.reply_text("❌ Неверный ID альбома.")
        return

    album = await fetch_album_tracks(album_id)
    if not album:
        await update.message.reply_text("❌ Альбом не найден.")
        return

    title = album.get("title", "Album")
    tracks = album.get("tracks") or []
    if not tracks:
        await update.message.reply_text("❌ В альбоме нет треков.")
        return

    chat_id = update.effective_chat.id
    status = await update.message.reply_text(f"⏳ Скачиваю альбом: {title} ({len(tracks)} треков)...")

    success = 0
    for t in tracks:
        if await _send_album_track(context, chat_id, t):
            success += 1

    try:
        await status.edit_text(f"✅ Альбом: {title}\nОтправлено: {success}/{len(tracks)} треков")
    except Exception:
        pass


async def _send_album_track(context: ContextTypes.DEFAULT_TYPE, chat_id: int, track: dict) -> bool:
    track_id = track.get("id")
    title = track.get("title", "?")
    artist = track.get("user", {}).get("username", "Unknown")
    duration = track.get("duration", 0) // 1000

    cached = cache_get(track_id)
    if cached:
        try:
            await context.bot.send_audio(chat_id=chat_id, audio=cached)
            return True
        except Exception:
            pass

    file_path = await download_track(track)
    if not file_path:
        return False

    thumb = await download_artwork(hires_artwork(track.get("artwork_url")))
    try:
        with open(file_path, "rb") as f:
            kwargs: dict[str, Any] = {"chat_id": chat_id, "audio": f, "title": title, "performer": artist, "duration": duration}
            if thumb:
                kwargs["thumbnail"] = thumb
            msg = await context.bot.send_audio(**kwargs)
            if msg.audio and track_id:
                cache_set(track_id, msg.audio.file_id)
            return True
    except Exception as e:
        logger.error(f"[DLALL] не отправил {title}: {e}")
        return False
    finally:
        safe_remove(file_path)
