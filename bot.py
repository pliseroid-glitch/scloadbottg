"""
PliserLoad — Telegram inline-бот для скачивания треков с SoundCloud
и поиска текстов песен через Genius.

Как это работает:
  1. Пользователь набирает @bot <запрос> в любом чате.
  2. Мы ищем треки через api-v2.soundcloud.com и возвращаем карточки.
  3. Пользователь выбирает карточку — Telegram присылает chosen_inline_result.
  4. Качаем трек через yt-dlp (он сам резолвит стрим и конвертит в mp3),
     заливаем в приватный канал-хранилище (STORAGE_CHAT_ID), получаем
     file_id и через editMessageMedia подменяем заглушку на аудио.
  5. Пара (track_id → file_id) кладётся в cache.json — на следующий раз
     загрузка пропускается, сразу подставляется готовый file_id.

Если в конце запроса есть " t", " text", " т", " текст" — переключаемся
в режим поиска текстов через Genius (search_songs + парсинг страницы).
"""

import os
import re
import json
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp
import requests
import yt_dlp
from bs4 import BeautifulSoup
import lyricsgenius
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaAudio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    InlineQueryHandler,
    ChosenInlineResultHandler,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)


# ─────────────────────────────────────────────────────────────────────────────
# Конфигурация
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GENIUS_TOKEN = os.getenv("GENIUS_TOKEN")
STORAGE_CHAT_ID = os.getenv("STORAGE_CHAT_ID")

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

CACHE_FILE = Path("cache.json")

# Лимиты Telegram, к которым привязываемся в нескольких местах
TG_MESSAGE_LIMIT = 4096
TG_CAPTION_LIMIT = 1024

# Сколько результатов отдавать в инлайн-выдаче
INLINE_RESULTS_LIMIT = 10
LYRICS_RESULTS_LIMIT = 5

# Размер in-memory кэша pending-запросов (между inline_query и chosen_inline_result)
PENDING_CACHE_MAX = 500
PENDING_CACHE_TRIM = 100  # сколько удалять при переполнении

# Браузерный UA — без него SoundCloud иногда отдаёт 403
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {"User-Agent": BROWSER_UA}

# Триггеры для переключения в режим поиска текстов
LYRICS_SUFFIXES = (" text", " текст")
# Однобуквенные суффиксы — отдельно: ловим только если перед ними длинное слово
LYRICS_SHORT_SUFFIXES = (" t", " т")


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Состояние процесса
# ─────────────────────────────────────────────────────────────────────────────

# SoundCloud периодически выкатывает новый client_id. Держим текущий в памяти
# и обновляем при первой же ошибке авторизации.
_client_id: str | None = None
_client_id_lock = asyncio.Lock()

# Кэш Telegram file_id (track_id → file_id), переживает рестарты — пишется в cache.json
_file_id_cache: dict[str, str] = {}

# Один общий aiohttp.ClientSession на весь процесс — заводим в main()
_http: aiohttp.ClientSession | None = None

# Имя бота (нужно для t.me/<bot>?start=...) — кэшируем после первого вызова
_bot_username: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Кэш file_id (track_id → telegram file_id)
# ─────────────────────────────────────────────────────────────────────────────

def load_cache() -> None:
    """Подгружаем cache.json при старте. Если файла нет или он битый — начинаем с пустого."""
    if not CACHE_FILE.exists():
        return
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _file_id_cache.update(data)
            logger.info(f"[CACHE] загружено {len(_file_id_cache)} записей")
    except Exception as e:
        logger.warning(f"[CACHE] не смог прочитать cache.json: {e}")


def save_cache() -> None:
    """Сохраняем кэш на диск. Дёргается на каждое cache_set — для нашего объёма ок."""
    try:
        CACHE_FILE.write_text(
            json.dumps(_file_id_cache, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"[CACHE] не смог сохранить cache.json: {e}")


def cache_get(track_id: int | None) -> str | None:
    if track_id is None:
        return None
    return _file_id_cache.get(str(track_id))


def cache_set(track_id: int, file_id: str) -> None:
    _file_id_cache[str(track_id)] = file_id
    save_cache()


# ─────────────────────────────────────────────────────────────────────────────
# SoundCloud: client_id
# ─────────────────────────────────────────────────────────────────────────────

async def get_client_id(force: bool = False) -> str | None:
    """
    Достаём публичный client_id со страницы soundcloud.com.
    Он зашит в один из JS-бандлов на a-v2.sndcdn.com, ищем там по регулярке.

    force=True — выбросить текущий и перечитать (например, если SoundCloud
    в ответ на запрос вернул 401/403 — значит, ключ протух).
    """
    global _client_id

    async with _client_id_lock:
        if _client_id and not force:
            return _client_id

        try:
            async with _http.get("https://soundcloud.com/") as resp:
                html = await resp.text()
        except Exception as e:
            logger.error(f"[CLIENT_ID] не открыл главную SoundCloud: {e}")
            return None

        # Бандлов несколько, нужный обычно последний — идём с конца
        scripts = re.findall(
            r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html
        )
        for script_url in reversed(scripts):
            try:
                async with _http.get(script_url) as resp:
                    js = await resp.text()
            except Exception:
                continue

            match = re.search(r'client_id\s*[:=]\s*"([a-zA-Z0-9]{30,})"', js)
            if match:
                _client_id = match.group(1)
                logger.info(f"[CLIENT_ID] обновлён: {_client_id[:8]}...")
                return _client_id

        logger.error("[CLIENT_ID] не нашёл ключ ни в одном из бандлов")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SoundCloud: поиск и метаданные
# ─────────────────────────────────────────────────────────────────────────────

async def search_soundcloud(query: str) -> list[dict]:
    """Поиск треков. Возвращает массив объектов track из api-v2."""
    client_id = await get_client_id()
    if not client_id:
        return []

    url = "https://api-v2.soundcloud.com/search/tracks"
    params = {"q": query, "client_id": client_id, "limit": INLINE_RESULTS_LIMIT, "offset": 0}

    try:
        async with _http.get(url, params=params) as resp:
            if resp.status != 200:
                logger.error(f"[SEARCH] HTTP {resp.status}")
                return []
            data = await resp.json()
    except Exception as e:
        logger.error(f"[SEARCH] {e}")
        return []

    tracks = data.get("collection", [])
    logger.info(f"[SEARCH] '{query}' → {len(tracks)} треков")
    return tracks


async def fetch_track_fresh(track_id: int, client_id: str) -> dict | None:
    """
    Берём свежие данные трека по id. Используется в местах, где у нас есть
    только id (например, в deeplink-ах и при догрузке метаданных).
    """
    url = f"https://api-v2.soundcloud.com/tracks/{track_id}"
    try:
        async with _http.get(url, params={"client_id": client_id}) as resp:
            if resp.status != 200:
                logger.warning(f"[REFRESH] HTTP {resp.status} для трека {track_id}")
                return None
            return await resp.json()
    except Exception as e:
        logger.warning(f"[REFRESH] {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SoundCloud: скачивание (через yt-dlp)
# ─────────────────────────────────────────────────────────────────────────────

# Параметры yt-dlp общие для всех загрузок. Прячем шум в логах,
# просим mp3 на 256 kbps — это потолок того, что отдаёт SoundCloud.
_YTDLP_OPTS_BASE: dict[str, Any] = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "nocheckcertificate": True,
    "http_headers": {"User-Agent": BROWSER_UA},
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "256",
        }
    ],
}


def _ytdlp_download_sync(track_url: str, output_template: str) -> str | None:
    """
    Синхронная обёртка над yt-dlp — выполняется в executor.
    Возвращает путь до итогового mp3 или None.
    """
    opts = {**_YTDLP_OPTS_BASE, "outtmpl": output_template}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track_url, download=True)
            # После постпроцессора файл всегда .mp3 — берём базу из template
            path = ydl.prepare_filename(info)
            mp3 = Path(path).with_suffix(".mp3")
            return str(mp3) if mp3.exists() else None
    except yt_dlp.utils.DownloadError as e:
        # Самые частые случаи: трек удалён, гео-блок, Go+
        logger.error(f"[DOWNLOAD] yt-dlp: {e}")
    except Exception as e:
        logger.error(f"[DOWNLOAD] yt-dlp: {type(e).__name__}: {e}")
    return None


async def download_track(track: dict) -> str | None:
    """
    Скачиваем один трек через yt-dlp. Возвращает путь до mp3 или None.

    yt-dlp сам разберётся с client_id, track_authorization, выбором стрима
    (progressive/HLS) и конвертацией в mp3 через ffmpeg — нам остаётся только
    подсунуть permalink и подождать.
    """
    title = track.get("title", "track")

    # Нужен permalink_url — yt-dlp работает с публичными ссылками SoundCloud,
    # а не с api-v2 эндпоинтами. В свежих данных он есть всегда.
    track_url = track.get("permalink_url")
    if not track_url:
        # На всякий случай — догружаем по id
        track_id = track.get("id")
        if track_id:
            client_id = await get_client_id()
            if client_id:
                fresh = await fetch_track_fresh(track_id, client_id)
                if fresh:
                    track_url = fresh.get("permalink_url")

    if not track_url:
        logger.error(f"[DOWNLOAD] нет permalink_url для '{title}'")
        return None

    # Локальное имя — чтобы не пересекаться с параллельными скачиваниями
    local_name = uuid.uuid4().hex[:8]
    template = str(DOWNLOADS_DIR / f"{local_name}.%(ext)s")

    logger.info(f"[DOWNLOAD] '{title}' → yt-dlp")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _ytdlp_download_sync, track_url, template
    )

    if result:
        size_mb = Path(result).stat().st_size / 1024 / 1024
        logger.info(f"[DOWNLOAD] ✅ {Path(result).name} ({size_mb:.1f} MB)")
    return result


async def download_artwork(url: str) -> bytes | None:
    """Скачиваем обложку — пригодится как thumbnail у аудио в Telegram."""
    if not url:
        return None
    try:
        async with _http.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        logger.warning(f"[ARTWORK] {e}")
    return None


def hires_artwork(url: str | None) -> str:
    """SoundCloud отдаёт миниатюры с суффиксом -large; меняем на -t500x500 — будет HD."""
    if not url:
        return ""
    return url.replace("-large", "-t500x500")


# ─────────────────────────────────────────────────────────────────────────────
# SoundCloud: альбомы и плейлисты
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_album_tracks(playlist_id: int) -> dict | None:
    """
    Подтягиваем альбом/плейлист со всеми треками.

    Подвох: SoundCloud в ответе на /playlists/{id} возвращает полные данные
    только для первых ~5 треков. Остальные приходят как болванки `{id: ...}`,
    их нужно дотягивать отдельным batch-запросом к /tracks.
    """
    client_id = await get_client_id()
    if not client_id:
        return None

    url = f"https://api-v2.soundcloud.com/playlists/{playlist_id}"
    try:
        async with _http.get(url, params={"client_id": client_id}) as resp:
            if resp.status != 200:
                logger.warning(f"[ALBUM] HTTP {resp.status} для плейлиста {playlist_id}")
                return None
            data = await resp.json()
    except Exception as e:
        logger.error(f"[ALBUM] {e}")
        return None

    tracks = data.get("tracks", [])
    incomplete_ids = [t["id"] for t in tracks if not t.get("title")]

    # Догружаем недостающие треки пачками по 50 (это лимит API)
    if incomplete_ids:
        full_map: dict[int, dict] = {}
        for i in range(0, len(incomplete_ids), 50):
            batch = incomplete_ids[i:i + 50]
            params = {"ids": ",".join(map(str, batch)), "client_id": client_id}
            try:
                async with _http.get(
                    "https://api-v2.soundcloud.com/tracks", params=params
                ) as resp:
                    if resp.status == 200:
                        for t in await resp.json():
                            full_map[t["id"]] = t
            except Exception as e:
                logger.warning(f"[ALBUM] не догрузил батч: {e}")

        # Заменяем болванки на полные объекты, сохраняя исходный порядок
        tracks = [full_map.get(t["id"], t) for t in tracks]

    data["tracks"] = tracks
    return data


# Кэш «альбом для трека»: track_id → dict|None. None означает, что мы уже искали
# и ничего не нашли — не имеет смысла лезть в API повторно.
_track_album_cache: dict[int, dict | None] = {}


async def get_track_album(track: dict) -> dict | None:
    """
    Определяем, входит ли трек в альбом/EP/сборник этого артиста.

    SoundCloud не даёт прямого ответа на этот вопрос, поэтому идём в обход:
    берём список плейлистов пользователя и ищем в них наш трек. Это
    best-effort: API возвращает только первые 50 плейлистов и обрезанный
    список треков в каждом, так что некоторые альбомы могут не находиться.
    """
    track_id = track.get("id")
    user_id = track.get("user", {}).get("id")
    if not track_id or not user_id:
        return None

    if track_id in _track_album_cache:
        return _track_album_cache[track_id]

    client_id = await get_client_id()
    if not client_id:
        return None

    url = f"https://api-v2.soundcloud.com/users/{user_id}/playlists"
    params = {"client_id": client_id, "limit": 50}
    try:
        async with _http.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        logger.warning(f"[ALBUM] поиск упал: {e}")
        return None

    found: dict | None = None
    for pl in data.get("collection", []):
        # Одиночные релизы и обычные плейлисты пропускаем
        if pl.get("set_type") not in ("album", "ep", "compilation"):
            continue
        # Сравниваем id трека с теми, что уже отдали вместе с плейлистом
        if any(t.get("id") == track_id for t in pl.get("tracks") or []):
            found = {
                "id": pl.get("id"),
                "title": pl.get("title"),
                "artist": track.get("user", {}).get("username", "Unknown"),
                "artwork": pl.get("artwork_url") or track.get("artwork_url"),
                "track_count": pl.get("track_count", 0),
            }
            break

    _track_album_cache[track_id] = found
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Genius: поиск и парсинг текстов
# ─────────────────────────────────────────────────────────────────────────────

# UA для скрапинга страниц Genius — у них агрессивная защита от ботов
SCRAPE_HEADERS_DESKTOP = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.google.com/",
    "DNT": "1",
}
SCRAPE_HEADERS_MOBILE = {
    **SCRAPE_HEADERS_DESKTOP,
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
}

# Хвосты, которые Genius пихает к тексту — режем их при очистке
LYRICS_TAIL_NOISE = ("Embed", "URLCopy", "You might also like")


def search_lyrics(query: str) -> list[dict]:
    """Ищем песни через Genius API. Запускается в executor — это блокирующий код."""
    if not GENIUS_TOKEN:
        logger.error("[LYRICS] GENIUS_TOKEN не задан")
        return []

    logger.info(f"[LYRICS] поиск '{query}'")
    try:
        genius = lyricsgenius.Genius(GENIUS_TOKEN, verbose=False, remove_section_headers=False)
        genius.timeout = 10
        response = genius.search_songs(query) or {}
    except Exception as e:
        logger.error(f"[LYRICS] поиск упал: {e}")
        return []

    # Ответ Genius иногда {sections: [{hits}]}, иногда сразу {hits}
    hits: list[dict] = []
    sections = response.get("sections") or []
    if sections:
        hits = sections[0].get("hits") or []
    if not hits:
        hits = response.get("hits") or []

    logger.info(f"[LYRICS] найдено {len(hits)} результатов")

    results = []
    for hit in hits[:LYRICS_RESULTS_LIMIT]:
        info = hit.get("result", {})
        results.append({
            "title": info.get("title") or info.get("full_title") or "Unknown",
            "artist": info.get("primary_artist", {}).get("name", "Unknown"),
            "url": info.get("url", ""),
            "thumbnail": info.get("song_art_image_thumbnail_url", ""),
            "id": info.get("id"),
        })
    return results


def fetch_lyrics(song_url: str) -> str | None:
    """
    Парсим страницу песни на genius.com и достаём текст.

    Genius блочит запросы без правильных заголовков — поэтому ходим
    с десктопным UA, при 403 повторяем с мобильным. Сами тексты лежат
    в div'ах с атрибутом data-lyrics-container="true".
    """
    if not song_url:
        return None

    try:
        page = requests.get(song_url, headers=SCRAPE_HEADERS_DESKTOP, timeout=15)
        if page.status_code == 403:
            logger.info("[LYRICS] 403 на десктопном UA — пробую мобильный")
            page = requests.get(song_url, headers=SCRAPE_HEADERS_MOBILE, timeout=15)

        if page.status_code != 200:
            logger.error(f"[LYRICS] страница недоступна: {page.status_code}")
            return None

        soup = BeautifulSoup(page.text, "html.parser")
        containers = soup.select('div[data-lyrics-container="true"]')
        if not containers:
            logger.warning("[LYRICS] не нашёл контейнеры с текстом")
            return None

        raw = "\n".join(div.get_text(separator="\n") for div in containers)
        return _clean_lyrics(raw)
    except Exception as e:
        logger.error(f"[LYRICS] не спарсил: {type(e).__name__}: {e}")
        return None


def _clean_lyrics(raw: str) -> str | None:
    """Срезаем заголовок Genius сверху и навязчивые ссылки снизу."""
    lines = raw.strip().split("\n")
    if lines and ("Lyrics" in lines[0] or "Contributors" in lines[0]):
        lines = lines[1:]
    while lines and any(noise in lines[-1] for noise in LYRICS_TAIL_NOISE):
        lines.pop()

    result = "\n".join(lines).strip()
    return result or None


# ─────────────────────────────────────────────────────────────────────────────
# Telegram: вспомогательные утилиты
# ─────────────────────────────────────────────────────────────────────────────

async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Кэшируем username — он меняется крайне редко, а get_me() стоит запроса."""
    global _bot_username
    if _bot_username is None:
        _bot_username = (await context.bot.get_me()).username
    return _bot_username


def trim_pending(pending: dict) -> None:
    """Чистим LRU-словарь pending-запросов, если разрослись."""
    if len(pending) > PENDING_CACHE_MAX:
        for k in list(pending.keys())[:PENDING_CACHE_TRIM]:
            pending.pop(k, None)


def is_lyrics_query(query: str) -> tuple[bool, str]:
    """
    Определяем, хочет ли пользователь текст. Возвращает (флаг, очищенный_запрос).

    Длинные суффиксы (" text", " текст") матчим в любом случае,
    короткие (" t", " т") — только если перед ними слово хотя бы из 2 символов,
    чтобы не путать "Travis t" с реальным запросом из двух слов.
    """
    lower = query.lower()
    for suf in LYRICS_SUFFIXES:
        if lower.endswith(suf):
            return True, query[: -len(suf)].strip()

    for suf in LYRICS_SHORT_SUFFIXES:
        if lower.endswith(suf):
            head = query[: -len(suf)].strip()
            # Нужно хотя бы одно "длинное" слово, иначе это слишком похоже на опечатку
            if head and any(len(w) >= 2 for w in head.split()):
                return True, head
    return False, query


def format_duration(ms: int) -> str:
    """Миллисекунды → "M:SS"."""
    return f"{ms // 60000}:{(ms % 60000) // 1000:02d}"


def is_track_blocked(track: dict) -> bool:
    """Платный/обрезанный трек — полный mp3 недоступен, можно скачать только превью."""
    if track.get("policy") in ("BLOCK", "SNIP"):
        return True
    if track.get("monetization_model") == "SUB_HIGH_TIER":
        return True
    transcodings = (track.get("media") or {}).get("transcodings") or []
    # Если все ссылки на стрим ведут в /preview/ — полная версия закрыта
    return bool(transcodings) and all(
        "/preview/" in (t.get("url") or "") for t in transcodings
    )


async def upload_to_storage(
    context: ContextTypes.DEFAULT_TYPE,
    file_path: str,
    title: str,
    artist: str,
    duration: int,
    thumb: bytes | None,
) -> str | None:
    """
    Заливаем mp3 в приватный канал-хранилище и возвращаем file_id.
    Все последующие отправки этого трека делаются по file_id — без перекачивания.
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# Команды и deep-links
# ─────────────────────────────────────────────────────────────────────────────

WELCOME_TEXT = (
    "Привет! Я инлайн-бот для скачивания треков с SoundCloud.\n\n"
    "Используй меня в любом чате:\n"
    "@pliserloadbot название трека — скачать с SoundCloud\n"
    "@pliserloadbot название text — получить текст песни\n\n"
    "Выбери трек — он появится прямо в чате."
)

# Префиксы deeplink-ов: /start <prefix><id>
DEEPLINK_HANDLERS = {
    "track_": "handle_track_deeplink",
    "album_": "handle_album_deeplink",
    "dlall_": "handle_download_all",
}


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — либо приветствие, либо разбор deeplink-а."""
    args = context.args or []
    if args:
        arg = args[0]
        for prefix, handler_name in DEEPLINK_HANDLERS.items():
            if arg.startswith(prefix):
                handler = globals()[handler_name]
                await handler(update, context, arg[len(prefix):])
                return

    await update.message.reply_text(WELCOME_TEXT)


async def handle_track_deeplink(
    update: Update, context: ContextTypes.DEFAULT_TYPE, track_id_str: str
) -> None:
    """/start track_<id> — скачать и прислать один трек в личку."""
    try:
        track_id = int(track_id_str)
    except ValueError:
        await update.message.reply_text("❌ Неверный ID трека.")
        return

    chat_id = update.effective_chat.id

    # Кэш — отдаём готовый file_id мгновенно, без сети
    cached = cache_get(track_id)
    if cached:
        try:
            await context.bot.send_audio(chat_id=chat_id, audio=cached)
            return
        except Exception:
            # file_id мог протухнуть (редко, но бывает) — упадём в полный путь
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

    # Заливаем в storage — получим file_id и закэшируем для будущих запросов.
    # Если storage не настроен — отправляем напрямую пользователю (без кэширования).
    file_id = await upload_to_storage(context, file_path, title, artist, duration, thumb)

    try:
        if file_id:
            cache_set(track_id, file_id)
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=file_id,
                title=title,
                performer=artist,
                duration=duration,
            )
        else:
            with open(file_path, "rb") as f:
                kwargs: dict[str, Any] = {
                    "chat_id": chat_id,
                    "audio": f,
                    "title": title,
                    "performer": artist,
                    "duration": duration,
                }
                if thumb:
                    kwargs["thumbnail"] = thumb
                await context.bot.send_audio(**kwargs)

        # Удаляем "⏳ Скачиваю трек..." — сам трек уже отправлен
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[DEEPLINK] отправка упала: {e}")
        await status.edit_text(f"❌ Ошибка отправки: {title}")
    finally:
        safe_remove(file_path)


async def handle_album_deeplink(
    update: Update, context: ContextTypes.DEFAULT_TYPE, album_id_str: str
) -> None:
    """/start album_<id> — показать треклист альбома с deeplink-ами на каждый трек."""
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
        lines.append(
            f'{i} · <a href="https://t.me/{bot_username}?start=track_{t_id}">{t_title}</a>'
        )
    text = "\n".join(lines)
    if len(text) > TG_MESSAGE_LIMIT:
        text = text[: TG_MESSAGE_LIMIT - 6] + "..."

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⬇️ Скачать все",
            url=f"https://t.me/{bot_username}?start=dlall_{album_id}",
        )
    ]])

    chat_id = update.effective_chat.id
    thumb = await download_artwork(hires_artwork(artwork)) if artwork else None

    # Если текст влезает в подпись фото — одним сообщением,
    # иначе шлём отдельно: фото без подписи + текст с кнопкой
    if thumb and len(text) <= TG_CAPTION_LIMIT:
        await context.bot.send_photo(
            chat_id=chat_id, photo=thumb, caption=text,
            parse_mode="HTML", reply_markup=keyboard,
        )
        return

    if thumb:
        await context.bot.send_photo(chat_id=chat_id, photo=thumb)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_download_all(
    update: Update, context: ContextTypes.DEFAULT_TYPE, album_id_str: str
) -> None:
    """/start dlall_<id> — скачать все треки альбома один за другим."""
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
    status = await update.message.reply_text(
        f"⏳ Скачиваю альбом: {title} ({len(tracks)} треков)..."
    )

    success = 0
    for t in tracks:
        if await _send_album_track(context, chat_id, t):
            success += 1

    try:
        await status.edit_text(
            f"✅ Альбом: {title}\nОтправлено: {success}/{len(tracks)} треков"
        )
    except Exception:
        pass


async def _send_album_track(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, track: dict
) -> bool:
    """Отправляем один трек из альбома. True — если получилось."""
    track_id = track.get("id")
    title = track.get("title", "?")
    artist = track.get("user", {}).get("username", "Unknown")
    duration = track.get("duration", 0) // 1000

    # Кэш — самый быстрый путь
    cached = cache_get(track_id)
    if cached:
        try:
            await context.bot.send_audio(chat_id=chat_id, audio=cached)
            return True
        except Exception:
            pass  # file_id мог протухнуть — дальше качнём заново

    file_path = await download_track(track)
    if not file_path:
        return False

    thumb = await download_artwork(hires_artwork(track.get("artwork_url")))

    try:
        with open(file_path, "rb") as f:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "audio": f,
                "title": title,
                "performer": artist,
                "duration": duration,
            }
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


# ─────────────────────────────────────────────────────────────────────────────
# Inline-режим
# ─────────────────────────────────────────────────────────────────────────────

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главная точка входа в инлайн-режим."""
    query = update.inline_query.query.strip()

    if not query:
        await handle_empty_inline(update, context)
        return

    is_lyrics, cleaned = is_lyrics_query(query)
    if is_lyrics:
        if not cleaned:
            return
        logger.info(f"[INLINE] режим текстов: '{cleaned}'")
        await handle_lyrics_inline(update, context, cleaned)
        return

    await handle_search_inline(update, context, query)


async def handle_empty_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пустой запрос — приветствие + последние скачанные треки (история)."""
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
    history = context.bot_data.get("download_history", [])

    # Показываем последние 9 — итого 10 карточек вместе с приветствием
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
                    message_text=(
                        f"🎵 {item['title']}\n"
                        f"👤 {item['artist']}\n\n"
                        "⏳ Скачиваю..."
                    ),
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏳ Загрузка...", callback_data="noop")
                ]]),
            )
        )
        # Кладём в pending — chosen_inline_result заберёт по result_id
        pending[result_id] = item

    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def handle_search_inline(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
) -> None:
    """Обычный поиск треков на SoundCloud."""
    tracks = await search_soundcloud(query)
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
        results.append(_build_track_card(
            result_id, title, artist, duration_str, artwork, blocked
        ))

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


def _build_track_card(
    result_id: str,
    title: str,
    artist: str,
    duration_str: str,
    artwork: str,
    blocked: bool,
) -> InlineQueryResultArticle:
    """Собираем карточку трека для инлайн-выдачи."""
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
            message_text=(
                f"🎵 {title}\n"
                f"👤 {artist}\n"
                f"⏱ {duration_str}\n\n"
                f"{message_tail}"
            ),
        ),
        reply_markup=keyboard,
    )


async def handle_lyrics_inline(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
) -> None:
    """Инлайн-поиск текстов песен через Genius."""
    loop = asyncio.get_event_loop()
    songs = await loop.run_in_executor(None, search_lyrics, query)

    if not songs:
        await update.inline_query.answer(
            [InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title="Ничего не найдено",
                description="Попробуй другой запрос",
                input_message_content=InputTextMessageContent(
                    message_text="❌ Текст не найден."
                ),
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
                    message_text=(
                        f"📝 {song['title']}\n"
                        f"👤 {song['artist']}\n\n"
                        "⏳ Загружаю текст..."
                    ),
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏳ Загрузка текста...", callback_data="noop")
                ]]),
            )
        )
        pending[result_id] = {
            "title": song["title"],
            "artist": song["artist"],
            "url": song["url"],
        }

    # У текстов своя квота поменьше — поиск дешёвый, держать долго смысла нет
    if len(pending) > 200:
        for k in list(pending.keys())[:50]:
            pending.pop(k, None)

    logger.info(f"[LYRICS] '{query}' → {len(results)} результатов")
    await update.inline_query.answer(results, cache_time=30)


# ─────────────────────────────────────────────────────────────────────────────
# Chosen inline result — пользователь выбрал карточку
# ─────────────────────────────────────────────────────────────────────────────

async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Пользователь выбрал результат из инлайн-выдачи. Здесь решаем:
    это запрос на скачивание трека или на загрузку текста?
    """
    chosen = update.chosen_inline_result
    result_id = chosen.result_id
    inline_message_id = chosen.inline_message_id

    logger.info(
        f"[CHOSEN] result_id={result_id} "
        f"user={chosen.from_user.id} ({chosen.from_user.username}) "
        f"inline_message_id={inline_message_id}"
    )

    # 1. Текст?
    pending_lyrics = context.bot_data.get("pending_lyrics", {})
    lyrics_info = pending_lyrics.pop(result_id, None)
    if lyrics_info:
        await handle_chosen_lyrics(context, inline_message_id, lyrics_info)
        return

    # 2. Скачивание трека?
    pending = context.bot_data.get("pending_downloads", {})
    info = pending.pop(result_id, None)
    if not info:
        logger.warning(f"[CHOSEN] нет данных для {result_id} — сессия истекла")
        await _try_edit_inline(
            context, inline_message_id, "❌ Сессия истекла, попробуй снова."
        )
        return

    if info.get("blocked"):
        logger.info("[CHOSEN] трек заблокирован, скачивание не запускаем")
        return

    await handle_chosen_track(context, inline_message_id, info)


async def handle_chosen_track(
    context: ContextTypes.DEFAULT_TYPE,
    inline_message_id: str | None,
    info: dict,
) -> None:
    """Качаем трек (или достаём из кэша) и подменяем инлайн-сообщение на аудио."""
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
        file_id = await _download_and_upload(context, track, title, artist, duration, artwork)
        if not file_id:
            await _try_edit_inline(
                context, inline_message_id, f"❌ Не удалось скачать: {title}"
            )
            return
        if track_id:
            cache_set(track_id, file_id)

    if not inline_message_id:
        return

    # Проверяем, входит ли трек в альбом — если да, повесим кнопку под аудио
    album = await get_track_album(track)
    keyboard: InlineKeyboardMarkup | None = None
    if album:
        bot_username = await get_bot_username(context)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"📀 {album['title']}",
                url=f"https://t.me/{bot_username}?start=album_{album['id']}",
            )
        ]])

    try:
        await context.bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=InputMediaAudio(
                media=file_id,
                title=title,
                performer=artist,
                duration=duration,
            ),
            reply_markup=keyboard,
        )
        logger.info("[CHOSEN] ✅ инлайн-сообщение заменено на аудио")
        _push_history(context, track, title, artist, duration, artwork)
    except Exception as e:
        logger.error(f"[CHOSEN] editMessageMedia упал: {type(e).__name__}: {e}")
        await _try_edit_inline(
            context,
            inline_message_id,
            f"✅ {title} — {artist}\n\nТрек загружен, но не удалось вставить в чат.",
        )


async def _download_and_upload(
    context: ContextTypes.DEFAULT_TYPE,
    track: dict,
    title: str,
    artist: str,
    duration: int,
    artwork: str,
) -> str | None:
    """Скачать → залить в storage → вернуть file_id. Прибирает за собой временный файл."""
    file_path = await download_track(track)
    if not file_path or not os.path.exists(file_path):
        logger.error("[CHOSEN] download_track вернул пусто")
        return None

    try:
        thumb = await download_artwork(hires_artwork(artwork))
        return await upload_to_storage(context, file_path, title, artist, duration, thumb)
    finally:
        safe_remove(file_path)


def _push_history(
    context: ContextTypes.DEFAULT_TYPE,
    track: dict,
    title: str,
    artist: str,
    duration: int,
    artwork: str,
) -> None:
    """Добавляем трек в историю, держим только последние 20."""
    history = context.bot_data.setdefault("download_history", [])
    history.append({
        "track": track,
        "title": title,
        "artist": artist,
        "duration": duration,
        "artwork": artwork,
    })
    if len(history) > 20:
        context.bot_data["download_history"] = history[-20:]


async def handle_chosen_lyrics(
    context: ContextTypes.DEFAULT_TYPE,
    inline_message_id: str | None,
    info: dict,
) -> None:
    """Грузим текст в фоне и подменяем заглушку на нормальное сообщение."""
    if not inline_message_id:
        logger.warning("[LYRICS] нет inline_message_id, нечего редактировать")
        return

    title = info["title"]
    artist = info["artist"]
    song_url = info["url"]

    loop = asyncio.get_event_loop()
    lyrics = await loop.run_in_executor(None, fetch_lyrics, song_url)

    if not lyrics:
        await _try_edit_inline(
            context, inline_message_id, f"❌ Не удалось загрузить текст: {title}"
        )
        return

    header = f"📝 {title}\n👤 {artist}\n\n"
    max_len = TG_MESSAGE_LIMIT - len(header) - 10
    if len(lyrics) > max_len:
        lyrics = lyrics[:max_len] + "..."

    try:
        await context.bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=header + lyrics,
        )
        logger.info(f"[LYRICS] ✅ текст для '{title}' отправлен")
    except Exception as e:
        logger.error(f"[LYRICS] edit упал: {e}")


async def _try_edit_inline(
    context: ContextTypes.DEFAULT_TYPE,
    inline_message_id: str | None,
    text: str,
) -> None:
    """Тихо обновить текст инлайн-сообщения, проглотив любые ошибки."""
    if not inline_message_id:
        return
    try:
        await context.bot.edit_message_text(inline_message_id=inline_message_id, text=text)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Прочие обработчики
# ─────────────────────────────────────────────────────────────────────────────

async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка кнопки-индикатора '⏳ Загрузка...' — иначе у пользователя крутится колесо."""
    try:
        await update.callback_query.answer()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────────────

async def _on_startup(app: Application) -> None:
    """Инициализация общего HTTP-сэшна и кэша."""
    global _http
    _http = aiohttp.ClientSession(headers=DEFAULT_HEADERS)
    load_cache()
    logger.info("Бот запущен!")


async def _on_shutdown(app: Application) -> None:
    """Аккуратно закрываем HTTP-сэшн."""
    global _http
    if _http is not None:
        await _http.close()
        _http = None


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
        allowed_updates=[
            "message",
            "inline_query",
            "chosen_inline_result",
            "callback_query",
        ]
    )


if __name__ == "__main__":
    main()
