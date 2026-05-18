"""SoundCloud: client_id, поиск, скачивание, альбомы."""

import re
import uuid
import asyncio
from pathlib import Path
from typing import Any

import aiohttp
import yt_dlp

from .config import (
    BROWSER_UA, DEFAULT_HEADERS, DOWNLOADS_DIR, INLINE_RESULTS_LIMIT, logger,
)

# ─────────────────────────────────────────────────────────────────────────────
# Состояние модуля
# ─────────────────────────────────────────────────────────────────────────────

_client_id: str | None = None
_client_id_lock = asyncio.Lock()
_http: aiohttp.ClientSession | None = None

# Кэш «альбом для трека»: track_id → dict|None
_track_album_cache: dict[int, dict | None] = {}


def init_session(session: aiohttp.ClientSession) -> None:
    """Устанавливаем общий HTTP-сэшн (вызывается из main при старте)."""
    global _http
    _http = session


# ─────────────────────────────────────────────────────────────────────────────
# Client ID
# ─────────────────────────────────────────────────────────────────────────────

async def get_client_id(force: bool = False) -> str | None:
    """
    Достаём публичный client_id со страницы soundcloud.com.
    force=True — выбросить текущий и перечитать.
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
# Поиск и метаданные
# ─────────────────────────────────────────────────────────────────────────────

async def search_tracks(query: str) -> list[dict]:
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
    """Берём свежие данные трека по id."""
    url = f"https://api-v2.soundcloud.com/tracks/{track_id}"
    try:
        async with _http.get(url, params={"client_id": client_id}) as resp:
            if resp.status != 200:
                logger.warning(f"[REFRESH] HTTP {resp.status} для трека {track_id}")
                return None
            data = await resp.json()
            has_auth = bool(data.get("track_authorization"))
            transcodings = (data.get("media") or {}).get("transcodings") or []
            logger.info(
                f"[REFRESH] трек {track_id}: "
                f"track_auth={'есть' if has_auth else 'НЕТ'}, "
                f"transcodings={len(transcodings)}, "
                f"permalink={data.get('permalink_url', 'N/A')[:50]}"
            )
            return data
    except Exception as e:
        logger.warning(f"[REFRESH] {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Скачивание (yt-dlp + ручной fallback)
# ─────────────────────────────────────────────────────────────────────────────

_YTDLP_OPTS_BASE: dict[str, Any] = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "nocheckcertificate": True,
    "http_headers": {"User-Agent": BROWSER_UA},
    "postprocessors": [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "256"}
    ],
}


def _patch_ytdlp_client_id(client_id: str) -> None:
    try:
        from yt_dlp.extractor.soundcloud import SoundcloudBaseIE
        SoundcloudBaseIE._CLIENT_ID = client_id
    except Exception as e:
        logger.warning(f"[DOWNLOAD] не подсунул client_id в yt-dlp: {e}")


def _ytdlp_download_sync(track_url: str, output_template: str) -> str | None:
    opts = {**_YTDLP_OPTS_BASE, "outtmpl": output_template}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track_url, download=True)
            mp3 = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
            return str(mp3) if mp3.exists() else None
    except yt_dlp.utils.DownloadError as e:
        logger.warning(f"[DOWNLOAD] yt-dlp: {e}")
    except Exception as e:
        logger.warning(f"[DOWNLOAD] yt-dlp: {type(e).__name__}: {e}")
    return None


def is_track_blocked(track: dict) -> bool:
    """Платный/обрезанный трек — полный mp3 недоступен."""
    if track.get("policy") in ("BLOCK", "SNIP"):
        return True
    if track.get("monetization_model") == "SUB_HIGH_TIER":
        return True
    if track.get("access") == "preview":
        return True
    transcodings = (track.get("media") or {}).get("transcodings") or []
    return bool(transcodings) and all(
        "/preview/" in (t.get("url") or "") for t in transcodings
    )


async def download_track(track: dict) -> str | None:
    """Скачиваем один трек. yt-dlp → ручной fallback."""
    title = track.get("title", "track")
    track_id = track.get("id")
    local_name = uuid.uuid4().hex[:8]

    client_id = await get_client_id()
    if track_id and client_id:
        fresh = await fetch_track_fresh(track_id, client_id)
        if fresh:
            track = fresh

    if is_track_blocked(track):
        logger.info(f"[DOWNLOAD] '{title}' заблокирован (лейбл/Go+), пропускаю")
        return None

    track_url = track.get("permalink_url")

    # Шаг 1 — yt-dlp
    if track_url and client_id:
        _patch_ytdlp_client_id(client_id)
        template = str(DOWNLOADS_DIR / f"{local_name}.%(ext)s")
        logger.info(f"[DOWNLOAD] '{title}' → yt-dlp")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _ytdlp_download_sync, track_url, template)
        if result:
            size_mb = Path(result).stat().st_size / 1024 / 1024
            logger.info(f"[DOWNLOAD] ✅ yt-dlp: {Path(result).name} ({size_mb:.1f} MB)")
            return result
        logger.info("[DOWNLOAD] yt-dlp не справился, пробую ручной режим")

    # Шаг 2 — fallback
    return await _download_manual(track, local_name)


async def _download_manual(track: dict, local_name: str) -> str | None:
    title = track.get("title", "track")
    track_id = track.get("id")

    client_id = await get_client_id()
    if not client_id:
        return None

    stream_url, protocol, output_path = await _try_resolve(track, client_id, local_name)
    if stream_url:
        return await _fetch_audio(stream_url, protocol, output_path)

    logger.info(f"[DOWNLOAD] обновляю client_id и перезапрашиваю трек '{title}'")
    new_cid = await get_client_id(force=True)
    if not new_cid:
        return None

    if track_id:
        fresh = await fetch_track_fresh(track_id, new_cid)
        if fresh:
            track = fresh

    stream_url, protocol, output_path = await _try_resolve(track, new_cid, local_name)
    if stream_url:
        return await _fetch_audio(stream_url, protocol, output_path)

    logger.error(f"[DOWNLOAD] не удалось скачать '{title}' ни одним способом")
    return None


def _pick_transcoding(transcodings: list[dict], prefer: str = "progressive") -> dict | None:
    if not transcodings:
        return None
    full = [t for t in transcodings if "/preview/" not in (t.get("url") or "")]
    if not full:
        return None
    for t in full:
        if t.get("format", {}).get("protocol") == prefer:
            return t
    for t in full:
        if t.get("format", {}).get("protocol") in ("progressive", "hls"):
            return t
    return full[0]


async def _try_resolve(track: dict, client_id: str, local_name: str):
    transcoding = _pick_transcoding((track.get("media") or {}).get("transcodings") or [])
    if not transcoding:
        return None, None, None

    stream_api_url = transcoding.get("url")
    protocol = transcoding.get("format", {}).get("protocol")
    track_auth = track.get("track_authorization", "")

    logger.info(f"[DOWNLOAD] manual '{track.get('title', '?')}' protocol={protocol}")

    params: dict[str, str] = {"client_id": client_id}
    if track_auth:
        params["track_authorization"] = track_auth

    try:
        async with _http.get(stream_api_url, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error(f"[DOWNLOAD] resolve stream {resp.status}: {body[:300]}")
                return None, None, None
            data = await resp.json()
            stream_url = data.get("url")
            if not stream_url:
                logger.error(f"[DOWNLOAD] resolve stream: ответ без 'url': {data}")
                return None, None, None
    except Exception as e:
        logger.error(f"[DOWNLOAD] resolve stream: {e}")
        return None, None, None

    output_path = DOWNLOADS_DIR / f"{local_name}.mp3"
    return stream_url, protocol, output_path


async def _fetch_audio(stream_url: str, protocol: str | None, output_path: Path) -> str | None:
    if protocol == "progressive":
        return await _download_progressive(stream_url, output_path)
    return await _download_hls(stream_url, output_path)


async def _download_progressive(stream_url: str, output_path: Path) -> str | None:
    try:
        async with _http.get(stream_url) as resp:
            if resp.status != 200:
                logger.error(f"[DOWNLOAD] HTTP {resp.status}")
                return None
            total = 0
            with open(output_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 15):
                    f.write(chunk)
                    total += len(chunk)
        logger.info(f"[DOWNLOAD] ✅ {output_path.name} ({total / 1024 / 1024:.1f} MB)")
        return str(output_path)
    except Exception as e:
        logger.error(f"[DOWNLOAD] progressive: {e}")
        return None


async def _download_hls(stream_url: str, output_path: Path) -> str | None:
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
            logger.info(f"[DOWNLOAD] ✅ {output_path.name} (через ffmpeg)")
            return str(output_path)
    except FileNotFoundError:
        logger.error("[DOWNLOAD] ffmpeg не установлен")
    except Exception as e:
        logger.error(f"[DOWNLOAD] hls: {e}")
    return None


async def download_artwork(url: str) -> bytes | None:
    """Скачиваем обложку."""
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
    """Меняем -large на -t500x500 для HD-обложки."""
    if not url:
        return ""
    return url.replace("-large", "-t500x500")


# ─────────────────────────────────────────────────────────────────────────────
# Альбомы и плейлисты
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_album_tracks(playlist_id: int) -> dict | None:
    """Подтягиваем альбом/плейлист со всеми треками."""
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
        tracks = [full_map.get(t["id"], t) for t in tracks]

    data["tracks"] = tracks
    return data


async def get_track_album(track: dict) -> dict | None:
    """Определяем, входит ли трек в альбом/EP/сборник."""
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
        if pl.get("set_type") not in ("album", "ep", "compilation"):
            continue
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
