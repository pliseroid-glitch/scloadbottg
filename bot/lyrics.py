"""Genius: поиск и парсинг текстов песен."""

import requests
from bs4 import BeautifulSoup
import lyricsgenius

from .config import GENIUS_TOKEN, LYRICS_RESULTS_LIMIT, logger

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

LYRICS_TAIL_NOISE = ("Embed", "URLCopy", "You might also like")


def search_lyrics(query: str) -> list[dict]:
    """Ищем песни через Genius API. Блокирующий — запускать в executor."""
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
    """Парсим страницу песни на genius.com и достаём текст."""
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
