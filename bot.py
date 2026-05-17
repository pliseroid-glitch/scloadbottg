import os
import re
import json
import uuid
import asyncio
import logging
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    ContextTypes,
    CommandHandler,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

CACHE_FILE = Path("cache.json")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Cache the client_id between requests
_client_id_cache: dict = {"value": None}

# Cache: SoundCloud track_id -> Telegram file_id
# Persisted to cache.json so survives restarts
_file_id_cache: dict[str, str] = {}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


def load_cache():
    global _file_id_cache
    if CACHE_FILE.exists():
        try:
            _file_id_cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            logger.info(f"[CACHE] Loaded {len(_file_id_cache)} cached tracks")
        except Exception as e:
            logger.warning(f"[CACHE] Failed to load: {e}")
            _file_id_cache = {}


def save_cache():
    try:
        CACHE_FILE.write_text(json.dumps(_file_id_cache), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[CACHE] Failed to save: {e}")


def cache_get(track_id: int) -> str | None:
    return _file_id_cache.get(str(track_id))


def cache_set(track_id: int, file_id: str):
    _file_id_cache[str(track_id)] = file_id
    save_cache()


async def get_soundcloud_client_id(session: aiohttp.ClientSession, force: bool = False) -> str | None:
    if _client_id_cache["value"] and not force:
        return _client_id_cache["value"]

    try:
        async with session.get("https://soundcloud.com/") as resp:
            html = await resp.text()

        scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)
        for script_url in reversed(scripts):
            async with session.get(script_url) as resp:
                js = await resp.text()
                match = re.search(r'client_id\s*[:=]\s*"([a-zA-Z0-9]{30,})"', js)
                if match:
                    cid = match.group(1)
                    _client_id_cache["value"] = cid
                    logger.info(f"[CLIENT_ID] Got: {cid[:8]}...")
                    return cid
    except Exception as e:
        logger.error(f"[CLIENT_ID] Failed: {e}")
    return None


async def search_soundcloud(query: str) -> list[dict]:
    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        client_id = await get_soundcloud_client_id(session)
        if not client_id:
            return []

        params = {"q": query, "client_id": client_id, "limit": 10, "offset": 0}
        url = "https://api-v2.soundcloud.com/search/tracks"

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                logger.error(f"[SEARCH] HTTP {resp.status}")
                return []
            data = await resp.json()
            tracks = data.get("collection", [])
            logger.info(f"[SEARCH] '{query}' -> {len(tracks)} tracks")
            return tracks


def pick_transcoding(transcodings: list[dict], prefer: str = "progressive") -> dict | None:
    if not transcodings:
        return None
    # First pass — preferred protocol
    for t in transcodings:
        if t.get("format", {}).get("protocol") == prefer:
            return t
    # Fallback to any
    for t in transcodings:
        if t.get("format", {}).get("protocol") in ("progressive", "hls"):
            return t
    return transcodings[0]


async def fetch_track_fresh(session: aiohttp.ClientSession, track_id: int, client_id: str) -> dict | None:
    """Fetch fresh track data — track_authorization from search results expires fast."""
    url = f"https://api-v2.soundcloud.com/tracks/{track_id}"
    try:
        async with session.get(url, params={"client_id": client_id}) as resp:
            if resp.status != 200:
                logger.warning(f"[REFRESH] HTTP {resp.status} for track {track_id}")
                return None
            return await resp.json()
    except Exception as e:
        logger.warning(f"[REFRESH] Failed: {e}")
        return None


async def download_track(track: dict) -> str | None:
    file_id = str(uuid.uuid4())[:8]
    title = track.get("title", "track")
    track_id = track.get("id")

    async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
        client_id = await get_soundcloud_client_id(session)
        if not client_id:
            return None

        # Refresh track data — track_authorization from search expires fast
        if track_id:
            fresh = await fetch_track_fresh(session, track_id, client_id)
            if fresh:
                track = fresh

        media = track.get("media") or {}
        transcoding = pick_transcoding(media.get("transcodings") or [])
        if not transcoding:
            logger.error(f"[DOWNLOAD] No transcodings for '{title}'")
            return None

        stream_api_url = transcoding.get("url")
        protocol = transcoding.get("format", {}).get("protocol")
        track_auth = track.get("track_authorization", "")
        logger.info(f"[DOWNLOAD] '{title}' protocol={protocol} auth={'yes' if track_auth else 'NO'}")

        params = {"client_id": client_id}
        if track_auth:
            params["track_authorization"] = track_auth

        async def fetch_stream_url(cid: str) -> str | None:
            params["client_id"] = cid
            async with session.get(stream_api_url, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"[DOWNLOAD] Stream URL fetch: {resp.status} {body[:200]}")
                    return None
                return (await resp.json()).get("url")

        stream_url = await fetch_stream_url(client_id)
        if not stream_url:
            client_id = await get_soundcloud_client_id(session, force=True)
            if client_id:
                stream_url = await fetch_stream_url(client_id)
        if not stream_url:
            return None

        output_path = DOWNLOADS_DIR / f"{file_id}.mp3"

        if protocol == "progressive":
            try:
                async with session.get(stream_url) as resp:
                    if resp.status != 200:
                        logger.error(f"[DOWNLOAD] Stream HTTP {resp.status}")
                        return None
                    total = 0
                    with open(output_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1 << 15):
                            f.write(chunk)
                            total += len(chunk)
                logger.info(f"[DOWNLOAD] {output_path.name} ({total / 1024 / 1024:.1f} MB)")
                return str(output_path)
            except Exception as e:
                logger.error(f"[DOWNLOAD] Failed: {e}")
                return None
        else:
            # HLS via ffmpeg
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", stream_url,
                    "-c:a", "libmp3lame", "-b:a", "256k",
                    str(output_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    logger.error(f"[DOWNLOAD] ffmpeg: {stderr.decode(errors='ignore')[:300]}")
                    return None
                if output_path.exists():
                    logger.info(f"[DOWNLOAD] {output_path.name}")
                    return str(output_path)
            except FileNotFoundError:
                logger.error("[DOWNLOAD] ffmpeg not found")
            except Exception as e:
                logger.error(f"[DOWNLOAD] HLS error: {e}")
    return None


async def download_artwork(url: str) -> bytes | None:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession(headers=DEFAULT_HEADERS) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.warning(f"[ARTWORK] {e}")
    return None


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я инлайн-бот для скачивания треков с SoundCloud.\n\n"
        "Используй меня в любом чате:\n"
        "@pliserloadbot название трека — скачать с SoundCloud\n"
        "@pliserloadbot название text — получить текст песни\n\n"
        "Выбери трек — он появится прямо в чате."
    )


def search_lyrics(query: str) -> list[dict]:
    """Search for song lyrics using Genius. Runs in thread."""
    import lyricsgenius
    logger.info(f"[LYRICS] Searching: '{query}', token={'yes' if GENIUS_TOKEN else 'NO'}")
    if not GENIUS_TOKEN:
        logger.error("[LYRICS] GENIUS_TOKEN not set!")
        return []
    try:
        genius = lyricsgenius.Genius(GENIUS_TOKEN, verbose=False, remove_section_headers=False)
        genius.timeout = 10
        # Search for songs
        response = genius.search_songs(query)
        logger.info(f"[LYRICS] Raw response keys: {list(response.keys()) if response else 'None'}")

        # Try different response structures
        hits = []
        if response:
            # Structure 1: response -> sections -> hits
            sections = response.get("sections", [])
            if sections:
                hits = sections[0].get("hits", [])
            # Structure 2: response -> hits
            if not hits:
                hits = response.get("hits", [])

        logger.info(f"[LYRICS] Found {len(hits)} hits")

        results = []
        for hit in hits[:5]:
            song_info = hit.get("result", {})
            results.append({
                "title": song_info.get("title") or song_info.get("full_title", "Unknown"),
                "artist": song_info.get("primary_artist", {}).get("name", "Unknown"),
                "url": song_info.get("url", ""),
                "thumbnail": song_info.get("song_art_image_thumbnail_url", ""),
                "id": song_info.get("id"),
            })
        return results
    except Exception as e:
        logger.error(f"[LYRICS] Search failed: {e}")
        return []


def fetch_lyrics(song_url: str) -> str | None:
    """Fetch full lyrics for a song using Genius API + page scraping."""
    if not GENIUS_TOKEN:
        return None
    try:
        import lyricsgenius
        genius = lyricsgenius.Genius(GENIUS_TOKEN, verbose=False, remove_section_headers=False)
        genius.timeout = 15

        # Extract song path from URL, e.g. "Sqwore-ufo-lyrics"
        # Use genius.lyrics() which handles scraping internally
        lyrics = genius.lyrics(song_url=song_url)
        if not lyrics:
            logger.warning(f"[LYRICS] genius.lyrics() returned empty for {song_url}")
            return None

        # Clean up Genius junk
        lines = lyrics.strip().split("\n")
        # Remove first line if it contains "Lyrics" header junk
        if lines and ("Lyrics" in lines[0] or "Contributors" in lines[0]):
            lines = lines[1:]
        # Remove last line if it's embed junk
        while lines and any(x in lines[-1] for x in ["Embed", "URLCopy", "You might also like"]):
            lines.pop()
        result = "\n".join(lines).strip()
        logger.info(f"[LYRICS] Got {len(result)} chars of lyrics")
        return result if result else None
    except Exception as e:
        logger.error(f"[LYRICS] Fetch failed: {type(e).__name__}: {e}")
    return None


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    if not query:
        return

    # Check if user wants lyrics (suffix "text" or "текст")
    if query.lower().endswith(" text") or query.lower().endswith(" текст"):
        # Remove the suffix
        lyrics_query = query.rsplit(" ", 1)[0].strip()
        if not lyrics_query:
            return
        logger.info(f"[INLINE] Lyrics mode: '{lyrics_query}'")
        await handle_lyrics_inline(update, context, lyrics_query)
        return

    # Otherwise — SoundCloud search
    tracks = await search_soundcloud(query)
    results = []
    pending = context.bot_data.setdefault("pending_downloads", {})

    for track in tracks[:10]:
        title = track.get("title", "Unknown")
        artist = track.get("user", {}).get("username", "Unknown")
        duration_ms = track.get("duration", 0)
        duration_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
        artwork = track.get("artwork_url") or ""

        # Detect paid/Go+ tracks — only preview is available, can't download full
        is_blocked = (
            track.get("policy") in ("BLOCK", "SNIP")
            or track.get("monetization_model") in ("SUB_HIGH_TIER",)
        )
        # Detect that media transcodings only have /preview/ URLs
        media = track.get("media") or {}
        transcodings = media.get("transcodings") or []
        is_preview_only = bool(transcodings) and all(
            "/preview/" in (t.get("url") or "") for t in transcodings
        )

        if is_blocked or is_preview_only:
            description = f"🔒 Go+ • {artist} • {duration_str}"
            display_title = f"🔒 {title}"
        else:
            description = f"{artist} • {duration_str}"
            display_title = title

        result_id = str(uuid.uuid4())
        result = InlineQueryResultArticle(
            id=result_id,
            title=display_title,
            description=description,
            thumbnail_url=artwork.replace("-large", "-t300x300") if artwork else None,
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"🎵 {title}\n"
                    f"👤 {artist}\n"
                    f"⏱ {duration_str}\n\n"
                    + ("🔒 Это платный трек (Go+), полная версия недоступна."
                       if is_blocked or is_preview_only
                       else "⏳ Скачиваю...")
                ),
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏳ Загрузка...", callback_data="noop")
            ]]) if not (is_blocked or is_preview_only) else None,
        )
        results.append(result)

        pending[result_id] = {
            "track": track,
            "title": title,
            "artist": artist,
            "duration": duration_ms // 1000,
            "artwork": artwork,
            "blocked": is_blocked or is_preview_only,
        }

    if len(pending) > 500:
        for k in list(pending.keys())[:100]:
            pending.pop(k, None)

    logger.info(f"[INLINE] '{query}' -> {len(results)} results")
    await update.inline_query.answer(results, cache_time=10)


async def handle_lyrics_inline(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Handle inline query for lyrics search."""
    loop = asyncio.get_event_loop()
    songs = await loop.run_in_executor(None, search_lyrics, query)

    if not songs:
        results = [
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="Ничего не найдено",
                description="Попробуй другой запрос",
                input_message_content=InputTextMessageContent(
                    message_text="❌ Текст не найден."
                ),
            )
        ]
        await update.inline_query.answer(results, cache_time=30)
        return

    results = []
    pending_lyrics = context.bot_data.setdefault("pending_lyrics", {})

    for song in songs:
        result_id = str(uuid.uuid4())
        title = song["title"]
        artist = song["artist"]

        result = InlineQueryResultArticle(
            id=result_id,
            title=f"📝 {title}",
            description=artist,
            thumbnail_url=song.get("thumbnail") or None,
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"📝 {title}\n"
                    f"👤 {artist}\n\n"
                    f"⏳ Загружаю текст..."
                ),
            ),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏳ Загрузка текста...", callback_data="noop")
            ]]),
        )
        results.append(result)

        pending_lyrics[result_id] = {
            "title": title,
            "artist": artist,
            "url": song["url"],
        }

    # Trim
    if len(pending_lyrics) > 200:
        for k in list(pending_lyrics.keys())[:50]:
            pending_lyrics.pop(k, None)

    logger.info(f"[LYRICS] '{query}' -> {len(results)} results")
    await update.inline_query.answer(results, cache_time=30)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a result — handle both audio downloads and lyrics."""
    chosen = update.chosen_inline_result
    result_id = chosen.result_id
    inline_message_id = chosen.inline_message_id
    user = chosen.from_user

    logger.info(f"[CHOSEN] result_id={result_id} user={user.id} ({user.username})")
    logger.info(f"[CHOSEN] inline_message_id={inline_message_id}")

    # Check if it's a lyrics request
    pending_lyrics = context.bot_data.get("pending_lyrics", {})
    lyrics_info = pending_lyrics.pop(result_id, None)
    if lyrics_info:
        await handle_chosen_lyrics(context, inline_message_id, lyrics_info)
        return

    # Otherwise it's a SoundCloud download
    pending = context.bot_data.get("pending_downloads", {})
    info = pending.pop(result_id, None)
    if not info:
        logger.warning(f"[CHOSEN] No info for {result_id}")
        if inline_message_id:
            try:
                await context.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text="❌ Сессия истекла, попробуй снова.",
                )
            except Exception:
                pass
        return

    if info.get("blocked"):
        logger.info("[CHOSEN] Blocked track, skipping download")
        return

    track = info["track"]
    title = info["title"]
    artist = info["artist"]
    duration = info["duration"]
    artwork = info["artwork"]
    track_id = track.get("id")

    # Check cache — maybe we already have this track uploaded
    cached_file_id = cache_get(track_id) if track_id else None
    if cached_file_id:
        logger.info(f"[CHOSEN] Cache hit for track {track_id}!")
        audio_file_id = cached_file_id
    else:
        # Download the audio file
        file_path = await download_track(track)
        if not file_path or not os.path.exists(file_path):
            logger.error("[CHOSEN] Download failed")
            if inline_message_id:
                try:
                    await context.bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=f"❌ Не удалось скачать: {title}",
                    )
                except Exception:
                    pass
            return

        # Get artwork
        thumb_bytes = None
        if artwork:
            thumb_url = artwork.replace("-large", "-t500x500")
            thumb_bytes = await download_artwork(thumb_url)

        # Upload to storage chat to get file_id
        storage_chat = os.getenv("STORAGE_CHAT_ID")
        if not storage_chat:
            logger.error("[CHOSEN] STORAGE_CHAT_ID not set")
            try:
                os.remove(file_path)
            except OSError:
                pass
            return

        audio_file_id = None
        try:
            with open(file_path, "rb") as f:
                kwargs = {
                    "chat_id": storage_chat,
                    "audio": f,
                    "title": title,
                    "performer": artist,
                    "duration": duration,
                    "disable_notification": True,
                }
                if thumb_bytes:
                    kwargs["thumbnail"] = thumb_bytes
                msg = await context.bot.send_audio(**kwargs)
                audio_file_id = msg.audio.file_id
                logger.info(f"[CHOSEN] Uploaded to storage: file_id={audio_file_id[:20]}...")
                # Save to cache
                if track_id:
                    cache_set(track_id, audio_file_id)
        except Exception as e:
            logger.error(f"[CHOSEN] Storage upload failed: {type(e).__name__}: {e}")
        finally:
            try:
                os.remove(file_path)
            except OSError:
                pass

        if not audio_file_id:
            if inline_message_id:
                try:
                    await context.bot.edit_message_text(
                        inline_message_id=inline_message_id,
                        text=f"❌ Ошибка загрузки: {title}",
                    )
                except Exception:
                    pass
            return

    # Replace inline message with audio using editMessageMedia
    if inline_message_id:
        from telegram import InputMediaAudio
        try:
            await context.bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaAudio(
                    media=audio_file_id,
                    title=title,
                    performer=artist,
                    duration=duration,
                ),
            )
            logger.info(f"[CHOSEN] ✅ Inline message replaced with audio!")
        except Exception as e:
            logger.error(f"[CHOSEN] editMessageMedia failed: {type(e).__name__}: {e}")
            # Fallback — just update text
            try:
                await context.bot.edit_message_text(
                    inline_message_id=inline_message_id,
                    text=f"✅ {title} — {artist}\n\nТрек загружен, но не удалось вставить в чат.",
                )
            except Exception:
                pass


async def handle_chosen_lyrics(context: ContextTypes.DEFAULT_TYPE, inline_message_id: str | None, info: dict):
    """Fetch lyrics and update the inline message."""
    title = info["title"]
    artist = info["artist"]
    song_url = info["url"]

    if not inline_message_id:
        logger.warning("[LYRICS] No inline_message_id")
        return

    loop = asyncio.get_event_loop()
    lyrics = await loop.run_in_executor(None, fetch_lyrics, song_url)

    if not lyrics:
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"❌ Не удалось загрузить текст: {title}",
            )
        except Exception:
            pass
        return

    # Telegram message limit is 4096 chars
    header = f"📝 {title}\n👤 {artist}\n\n"
    max_lyrics_len = 4096 - len(header) - 10
    if len(lyrics) > max_lyrics_len:
        lyrics = lyrics[:max_lyrics_len] + "..."

    try:
        await context.bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=header + lyrics,
        )
        logger.info(f"[LYRICS] ✅ Sent lyrics for '{title}'")
    except Exception as e:
        logger.error(f"[LYRICS] edit failed: {e}")


def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not set in .env")
        return

    load_cache()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(InlineQueryHandler(inline_query, block=False))
    app.add_handler(ChosenInlineResultHandler(chosen_inline_result, block=False))

    logger.info("Bot started!")
    app.run_polling(
        allowed_updates=[
            "message",
            "inline_query",
            "chosen_inline_result",
        ]
    )


if __name__ == "__main__":
    main()
